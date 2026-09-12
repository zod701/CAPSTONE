"""공공데이터 실시간 도착정보 클라이언트 (경기 GBIS 버스 · 서울 TOPIS 버스 · 서울 도시철도).

키가 URL 에 들어간다(버스는 serviceKey 파라미터, 지하철은 경로 한 칸) — 그래서 httpx 예외 문자열·요청 로그를
쓰지 않고, 사용자 문구에는 원천의 아는 필드만 마스킹해 넣는다(tiles.py·kakao.py 규칙을 둘 다 지킨다).
카카오와 달리 공공데이터는 저장이 허용되므로 같은 (원천, 키) 요청을 15초만 메모리에 둔다 — 카카오 응답은
여기 담지 않는다. 정규화는 순수 함수(bus_gyeonggi·bus_seoul·subway)로 떼어 두어 원문 없이 테스트한다.
"""
import asyncio
import logging
import re
import time
from datetime import datetime
from urllib.parse import quote, unquote

import httpx

from .errors import ApiError
from .kakao import redact
from .quota import LABELS, QuotaGuard, exceeded_error

GG_STOP_URL = "https://apis.data.go.kr/6410000/busarrivalservice/v2/getBusArrivalListv2"
SEOUL_STOP_URL = "http://ws.bus.go.kr/api/rest/arrive/getLowArrInfoByStId"
SUBWAY_PAGE = 60   # 환승역은 한 응답에 여러 노선이 섞여 온다 — 서울역 total=22 라 20 이면 뒤 노선이 잘린다
SUBWAY_URL = ("http://swopenapi.seoul.go.kr/api/subway/{key}/json/realtimeStationArrival"
              "/0/" + str(SUBWAY_PAGE) + "/{name}")

KIND_GG = "gyeonggi_bus"
KIND_SEOUL = "seoul_bus"
KIND_SUBWAY = "seoul_subway"
# 원천별 (키 환경변수, 활용신청 창구) — missing_key·arrivals_auth 문구에 쓴다
KEY_ENV = {KIND_GG: ("DATA_GO_KR_API_KEY", "data.go.kr"), KIND_SEOUL: ("DATA_GO_KR_API_KEY", "data.go.kr"),
           KIND_SUBWAY: ("SEOUL_SUBWAY_LIVE_API", "서울 열린데이터광장")}

logging.getLogger("httpx").setLevel(logging.WARNING)  # 요청 로그(INFO)에 키가 든 URL 이 찍힌다
log = logging.getLogger(__name__)                     # 건수만 남긴다 (원천 본문·키·역명은 넣지 않는다)

# 도시철도 arvlCd — 0 진입 · 1 도착 · 2 출발 · 3 전역출발 · 4 전역진입 · 5 전역도착 · 99 운행중
ARVL_NOW = ("0", "1")    # 지금 이 역에 들어오는 중·도착 = 기다릴 시간이 0
_STOPS_AHEAD = re.compile(r"\[(\d+)번째 전\]")
_STOPS_AHEAD_RAIL = re.compile(r"\[(\d+)\]번째 전역")   # 도시철도는 괄호 위치가 다르다 ('[4]번째 전역 (덕정)')
# 게이트웨이 거절은 format=json 이어도 XML 로 온다 — 숫자 코드만 꺼낸다(본문은 키를 에코할 수 있다)
_REASON = re.compile(r"<returnReasonCode>(\d+)</returnReasonCode>")
MSG_MAX = 200          # 사용자 문구 길이 상한


def mask(text, *keys):
    """원천 문구에서 우리 키를 가린다. redact 의 정규식(32자 hex·UUID)에 안 걸리는 키(64자·30자 영숫자)라
    키 문자열을 직접 치우는 이 단계가 필수다. 자르기는 여기서 하지 않는다 — 먼저 자르면 200자 뒤에 있던
    키가 쪼개져 조각이 응답에 남는다(치환이 다 끝난 뒤 _clip)."""
    out = redact(text or "")
    for k in keys:
        if k:
            out = out.replace(k, "<REDACTED>")
    return out


def _clip(text):
    return (text or "")[:MSG_MAX]


def _age_s(recptn, now):
    """원천이 이 값을 받은 뒤 지난 초. 원천 시계가 우리보다 앞설 수 있어(실측 -136초) 음수는 0 으로 본다.
    못 읽으면 None."""
    try:
        got = datetime.strptime(_text(recptn), "%Y-%m-%d %H:%M:%S").timestamp()
    except (ValueError, TypeError):
        return None
    return max(0, int(now - got))


def _key_forms(key):
    """마스킹할 키 형태 전부 — .env 원문과 실제로 보낸 형태(경기는 unquote, 지하철 경로는 quote).
    보낸 값과 가리는 값이 다르면 원천이 에코한 키가 그대로 응답에 나간다."""
    return (key, unquote(key), quote(key, safe=""))


def _rows(v):
    """목록 필드 → dict 목록. 원천이 단건을 배열이 아닌 객체로 주는 변형을 흡수한다 — 경기 GBIS 는
    도착정보가 1개 노선뿐일 때 busArrivalList 를 객체로 준다(실측). dict 이 아닌 원소는 버린다."""
    if isinstance(v, dict):
        return [v]
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _text(v):
    """문자열·숫자 → 다듬은 문자열, 그 밖(None·빈 문자열)은 ''. 경기는 routeName 을 int 로도 준다."""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return ""


def _i(v):
    """GBIS 실시간 필드: 값이 없으면 빈 문자열로 온다 → None."""
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _int(v):
    """서울 응답은 숫자도 문자열로 준다('332'). 숫자가 아니면 None."""
    try:
        return int(_text(v))
    except ValueError:
        return None


def _upstream(kind, detail=None, *, status_code=None):
    """원천이 알린 오류. 아는 필드(resultMessage·headerMsg·errorMessage.message)만 마스킹해 붙인다."""
    head = f"{LABELS[kind]} 요청이 실패했습니다" + (f" (HTTP {status_code})" if status_code else "")
    detail = mask(_text(detail))          # 키는 _items 가 알고 있다 → 거기서 한 번 더 가리고 자른다
    return ApiError("upstream_error", f"{head}: {detail}" if detail else f"{head}.",
                    action="잠시 후 다시 시도하세요.", status=502, upstream_status=status_code)


def _auth_error(kind, upstream_status=401):
    env, portal = KEY_ENV[kind]
    return ApiError("arrivals_auth", f"{LABELS[kind]} 원천이 API 키를 거부했습니다.",
                    action=f".env 의 {env} 와 {portal} 활용신청 상태를 확인하세요.",
                    status=503, upstream_status=upstream_status)


def _gateway_error(kind, body_text, limit):
    """JSON 이 아닌 응답 = 공공데이터포털 게이트웨이 거절. 본문·예외 문자열은 남기지 않고
    숫자 코드만 본다(30=등록되지 않은 키, 22=일 트래픽 초과)."""
    m = _REASON.search(body_text or "")
    code = m.group(1) if m else None
    if code == "30":
        return _auth_error(kind, upstream_status=200)
    if code == "22":
        return exceeded_error(kind, limit)
    return ApiError("upstream_error", f"{LABELS[kind]} 응답을 해석할 수 없습니다.",
                    action="잠시 후 다시 시도하세요.", status=502)


# --- 정규화 (순수 함수 — 저장·로그 없음) ---

def _gg_item(row, n):
    """경기 busArrivalList 한 줄의 n(1·2)번째 차량 → item."""
    sec = _i(row.get(f"predictTimeSec{n}"))      # 초 단위 — 도착정보가 있는 노선이면 목록 응답에도 온다(실측)
    minutes = _i(row.get(f"predictTime{n}"))
    eta = sec if sec is not None else (minutes * 60 if minutes is not None else None)
    crowd = _i(row.get(f"crowded{n}"))
    seats = _i(row.get(f"remainSeatCnt{n}"))
    return {
        "source": "gyeonggi",
        "route_id": _text(row.get("routeId")) or None,
        "route_name": _text(row.get("routeName")) or None,
        "route_type": None,                      # 라우터가 순서표에서 채운다 (routeTypeCd 코드표는 미실측)
        "eta_s": eta,
        "n_stops_ahead": _i(row.get(f"locationNo{n}")),
        "crowding": crowd if crowd in (1, 2, 3, 4) else None,   # 0 은 미정의 (predictTime2 없이도 0 으로 온다)
        "seats": seats if seats is not None and seats > 0 else None,  # 0 은 '0석'과 '미제공'을 구분할 수 없다
        "is_last": None,                         # 경기 응답에 막차 표시가 없다
        "vehicle": _text(row.get(f"plateNo{n}")) or None,
        "message": None if eta is not None else "도착 정보가 없습니다.",
        "dest": _text(row.get("routeDestName")) or None,
    }


def bus_gyeonggi(raw: dict) -> list[dict]:
    """경기 GBIS 정류소 도착 응답 → items. resultCode 0 만 정상, 4 는 빈 결과(없는 정류소 — 실측)."""
    body = raw.get("response") if isinstance(raw.get("response"), dict) else {}
    head = body.get("msgHeader") if isinstance(body.get("msgHeader"), dict) else {}
    code = head.get("resultCode")
    if code == 4:
        return []
    if code != 0:
        # TODO: 0·4 밖의 코드는 미실측이다 — '데이터 없음' 코드를 실측하면 그 코드만 빈 목록으로 옮긴다.
        raise _upstream(KIND_GG, head.get("resultMessage"))
    msg_body = body.get("msgBody") if isinstance(body.get("msgBody"), dict) else {}
    items = []
    for row in _rows(msg_body.get("busArrivalList")):   # 1개 노선뿐이면 배열이 아닌 객체로 온다 (실측)
        items.append(_gg_item(row, 1))           # 실시간이 없는 노선도 eta_s=None 으로 보여 준다
        second = _gg_item(row, 2)
        if second["eta_s"] is not None:          # 2호차는 값이 있을 때만
            items.append(second)
    return items


def _seoul_item(row, n):
    """서울 itemList 한 줄의 n(1·2)번째 차량 → item."""
    msg = _text(row.get(f"arrmsg{n}"))
    sec = _int(row.get(f"traTime{n}"))           # 0 은 '0초 후'가 아니라 '예측 없음'이다 (운행종료·출발대기)
    eta = sec if sec is not None and sec > 0 else (0 if "곧 도착" in msg else None)
    ahead = _STOPS_AHEAD.search(msg)             # sectOrd 는 남은 정류소 수가 아니다
    last = _text(row.get(f"isLast{n}"))
    return {
        "source": "seoul",
        "route_id": _text(row.get("busRouteId")) or None,   # 실측: 우리 route_id 와 같은 값
        "route_name": _text(row.get("rtNm")) or None,
        "route_type": None,                      # 라우터가 순서표에서 채운다 (routeType 코드표는 미실측)
        "eta_s": eta,
        "n_stops_ahead": int(ahead.group(1)) if ahead else None,
        "crowding": None,                        # 서울 응답에 혼잡도가 없다
        "seats": None,
        "is_last": True if last == "1" else (False if last == "0" else None),
        "vehicle": _text(row.get(f"plainNo{n}")) or None,   # 2호차 필드는 미실측 → 없으면 None
        "message": msg or None,                  # 원천 문구 그대로 (운행종료·출발대기·곧 도착 …)
        "dest": None,                            # 종점 필드가 실측 목록에 없다
    }


def bus_seoul(raw: dict) -> list[dict]:
    """서울 TOPIS 정류소 도착 응답 → items. headerCd '0' 만 정상."""
    head = raw.get("msgHeader") if isinstance(raw.get("msgHeader"), dict) else {}
    if _text(head.get("headerCd")) != "0":
        # TODO: '데이터 없음' headerCd 값은 미실측이다 — 실측하면 그 코드만 빈 목록으로 옮긴다.
        raise _upstream(KIND_SEOUL, head.get("headerMsg"))
    body = raw.get("msgBody") if isinstance(raw.get("msgBody"), dict) else {}
    items = []
    for row in _rows(body.get("itemList")):      # itemCount 는 25건에도 0 으로 왔다 — 쓰지 않는다
        items.append(_seoul_item(row, 1))
        second = _seoul_item(row, 2)
        if second["eta_s"] is not None:
            items.append(second)
    return items


def subway(raw: dict, now=None) -> list[dict]:
    """서울 도시철도 실시간 도착 응답 → items (역 전체). 환승역은 한 응답에 여러 노선이 섞여 오므로
    노선 필터는 호출자가 line_id 로 한다 — 그래야 역 하나를 한 번만 부르고 걸러 쓴다.
    정상과 오류의 최상위 모양이 다르다 — 없는 역명은 errorMessage 래퍼 없이 루트로 온다.

    `eta_s` 는 **기다릴 초**다. 원천의 `barvlDt` 는 수신 시각(`recptnDt`) 기준 예측이라 그만큼 빼고(실측 경과 41~281초),
    빼서 음수가 되면 예측이 이미 지난 것이므로 None(모름)으로 둔다. `barvlDt=0` 은 두 가지가 섞여 있어 `arvlCd` 로 가른다 —
    진입·도착이면 0, 그 밖(출발·운행중·전역 계열)이면 None 이다. 0 을 그냥 담으면 20역 떨어진 열차가 '지금 도착'이 된다.
    `age_s`(수신 경과)를 함께 주어 오래된 행을 버릴 수 있게 한다."""
    now = time.time() if now is None else now
    err = raw.get("errorMessage") if isinstance(raw.get("errorMessage"), dict) else raw
    code = _text(err.get("code"))
    if code == "INFO-200":                       # 해당하는 데이터가 없습니다 (없는 역명 — 실측)
        return []
    if code != "INFO-000":
        # TODO: 그 밖의 코드(INFO-100 인증·INFO-300 요청 제한 계열)는 미실측 — 실측하면 여기서 갈라 낸다.
        raise _upstream(KIND_SUBWAY, err.get("message"))
    rows = _rows(raw.get("realtimeArrivalList"))
    total = _int(err.get("total"))
    if total is not None and total > len(rows):   # 잘리면 뒤 노선이 통째로 빠져 '열차 없음' 처럼 보인다
        log.warning("도시철도 실시간 응답이 페이지 크기(%s)에 잘렸습니다: total=%s, 받은 %s건",
                    SUBWAY_PAGE, total, len(rows))
    items = []
    for row in rows:
        msg = _text(row.get("arvlMsg2"))
        ahead = _STOPS_AHEAD_RAIL.search(msg)
        age = _age_s(row.get("recptnDt"), now)
        left = _int(row.get("barvlDt"))
        if left:                                 # 예측이 있다 → 수신 뒤 지난 만큼 뺀다
            eta = left - (age or 0)
            eta = eta if eta >= 0 else None      # 예측 시각이 이미 지났다 = 모름 (0 으로 우기지 않는다)
        else:                                    # barvlDt 가 0 이거나 없다 → 도착 코드로 가른다
            eta = 0 if _text(row.get("arvlCd")) in ARVL_NOW else None
        items.append({
            "line_id": _text(row.get("subwayId")) or None,      # 노선 필터용 내부 키 (응답에는 나가지 않는다)
            "direction": _text(row.get("updnLine")) or None,    # '내선'·'외선' 도 원문 그대로
            "eta_s": eta,
            "age_s": age,                        # 이 값을 받은 뒤 지난 초 — 오래된 행을 버리는 판단에 쓴다
            # barvlDt 가 '0' 으로만 오는 역이 많다 — 남은 역 수라도 있어야 임박한 열차를 가릴 수 있다
            "n_stops_ahead": int(ahead.group(1)) if ahead else None,
            "message": msg or None,
            "train_type": _text(row.get("btrainSttus")) or None,
            "dest": _text(row.get("bstatnNm")) or None,
            "headsign": _text(row.get("trainLineNm")) or None,
            "train_no": _text(row.get("btrainNo") or row.get("trainNo")) or None,  # 실측 기록에 두 이름이 다 있다
        })
    return items


def line_only(items, subway_id=None):
    """역 전체 items → 그 노선만, 내부 키(line_id) 를 뺀 응답 모양. 캐시에는 역 전체가 들어 있다."""
    want = _text(subway_id) or None
    return [{k: v for k, v in it.items() if k != "line_id"}
            for it in items if not want or it["line_id"] == want]


class TtlCache:
    """같은 (원천, 키) 요청이 몰릴 때 쿼터를 아끼는 짧은 메모리 캐시. 공공데이터라 허용된다
    (카카오 응답은 여기 담지 않는다). clock 은 테스트가 넣는다 — QuotaGuard 와 같은 방식."""

    def __init__(self, ttl_s, clock=time.monotonic):
        self.ttl_s, self.clock, self._d = ttl_s, clock, {}

    def get(self, key):
        hit = self._d.get(key)
        if hit is None or self.clock() - hit[0] >= self.ttl_s:
            return None
        return [dict(it) for it in hit[1]]       # 호출자가 고쳐도(route_type 채우기) 캐시가 오염되지 않게

    def put(self, key, items):
        now = self.clock()
        for k, (t, _) in list(self._d.items()):  # 만료 항목 정리 (15초 분량이라 작다)
            if now - t >= self.ttl_s:
                del self._d[k]
        self._d[key] = (now, [dict(it) for it in items])


class RealtimeClient:
    """세 원천을 같은 item 모양으로 돌려준다. Settings 는 받지 않고 값만 받는다(KakaoClient 와 같다)."""

    def __init__(self, http: httpx.AsyncClient, quota: QuotaGuard, *, data_go_kr_key: str | None = None,
                 subway_key: str | None = None, timeout_s: float = 5.0, ttl_s: float = 15.0,
                 clock=time.monotonic, now=time.time):
        self.http = http
        self.quota = quota
        self.data_go_kr_key = data_go_kr_key
        self.subway_key = subway_key
        self.timeout_s = timeout_s
        self.now = now          # 벽시계 — 도시철도 예측을 수신 시각 기준으로 보정한다(캐시의 clock 과 다른 축)
        self.cache = TtlCache(ttl_s, clock)
        self._inflight = {}   # cache_key → 진행 중 호출의 Future (같은 키를 동시에 부르면 합류한다)

    async def gyeonggi_stop(self, stop_key: str) -> list[dict]:
        """경기 GBIS: 정류소를 지나는 노선들의 실시간 도착. serviceKey 는 unquote 한 값을 넘긴다 —
        인코딩된 키가 들어오면 httpx 가 다시 인코딩해 이중 인코딩된다(서울 버스는 반대다)."""
        key = self._key(KIND_GG)
        params = {"format": "json", "stationId": stop_key, "serviceKey": unquote(key)}
        return await self._items(KIND_GG, key, (KIND_GG, stop_key), GG_STOP_URL, params, bus_gyeonggi)

    async def seoul_stop(self, stop_key: str) -> list[dict]:
        """서울 TOPIS: 정류소 실시간 도착. serviceKey 는 .env 원문을 그대로 넘겨야 통했다(경기와 반대)."""
        key = self._key(KIND_SEOUL)
        params = {"stId": stop_key, "resultType": "json", "serviceKey": key}
        return await self._items(KIND_SEOUL, key, (KIND_SEOUL, stop_key), SEOUL_STOP_URL, params, bus_seoul)

    async def subway_station(self, live_name: str, subway_id: str | None = None) -> list[dict]:
        """서울 도시철도: 실시간 API 이름으로 부르고 subway_id(노선)로 거른다. 역명은 경로 한 칸이라 quote 한다.
        캐시 키는 역명뿐이다 — 환승역은 한 응답에 여러 노선이 섞여 오므로 한 번 불러 노선별로 걸러 쓴다."""
        key = self._key(KIND_SUBWAY)
        url = SUBWAY_URL.format(key=quote(key, safe=""), name=quote(live_name))
        rows = await self._items(KIND_SUBWAY, key, (KIND_SUBWAY, live_name), url, None,
                                 lambda body: subway(body, self.now()))
        return line_only(rows, subway_id)

    def _key(self, kind):
        key = self.subway_key if kind == KIND_SUBWAY else self.data_go_kr_key
        if not key:
            env = KEY_ENV[kind][0]
            raise ApiError("missing_key", f"{LABELS[kind]} API 키가 설정되지 않았습니다.",
                           action=f".env 에 {env} 를 추가하고 서버를 다시 시작하세요.", status=503)
        return key

    async def _items(self, kind, key, cache_key, url, params, parse):
        """캐시 → 진행 중인 같은 호출에 합류 → 쿼터 예약 → 호출 → 정규화.
        캐시 히트와 합류는 쿼터를 쓰지 않고, 오류는 캐시하지 않는다."""
        hit = self.cache.get(cache_key)
        if hit is not None:
            return hit
        flight = self._inflight.get(cache_key)
        if flight is not None:
            # 캐시는 응답이 온 뒤에 채워진다 — 같은 정류장을 '동시에' 보는 요청은 여기서 결과를 나눈다
            return [dict(it) for it in await asyncio.shield(flight)]
        fut = asyncio.get_running_loop().create_future()
        self._inflight[cache_key] = fut
        try:
            items = await self._fetch(kind, key, url, params, parse)
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()          # 합류한 요청이 없을 때의 asyncio 경고를 막는다
            raise
        else:
            fut.set_result(items)
            self.cache.put(cache_key, items)
            return items
        finally:
            del self._inflight[cache_key]

    async def _fetch(self, kind, key, url, params, parse):
        """쿼터 예약 → 호출 → 정규화 (캐시·합류는 _items 가 한다)."""
        day = self.quota.reserve(kind)  # 확인·차감을 한 잠금 안에서 (동시 요청도 한도를 못 넘게)
        label = LABELS[kind]
        # httpx 예외 문자열에는 키가 든 URL 이 들어 있다 (from None: 연쇄 traceback 에도 남기지 않음)
        try:
            r = await self.http.get(url, params=params, timeout=self.timeout_s)
        except httpx.TimeoutException:  # 원천에 닿았을 수 있으니 예약을 그대로 둔다
            raise ApiError("upstream_timeout", f"{label} 응답이 {self.timeout_s:g}초 안에 오지 않았습니다.",
                           action="잠시 후 다시 시도하세요.", status=504) from None
        except httpx.ConnectError:
            self.quota.release(kind, day)
            raise ApiError("upstream_unreachable", f"{label} 서버에 연결할 수 없습니다.",
                           action="인터넷 연결을 확인하세요.", status=502) from None
        except httpx.TransportError:  # 연결 뒤 끊김 등 — 닿았을 수 있으니 예약을 그대로 둔다
            raise ApiError("upstream_error", f"{label} 응답을 받지 못했습니다.",
                           action="잠시 후 다시 시도하세요.", status=502) from None
        try:
            items = parse(self._body(kind, r))
        except ApiError as err:
            # 원천이 키를 에코한 경우까지 마지막에 한 번 더 — 보낸 형태까지 가린 뒤에 자른다
            err.message = _clip(mask(err.message, *_key_forms(key)))
            if err.code == "quota_exceeded":
                self.quota.mark_exhausted(kind, day)
            elif err.code == "arrivals_auth":     # 원천 쿼터를 쓰지 않은 거절
                self.quota.release(kind, day)
            raise
        return items

    def _body(self, kind, r):
        """HTTP 상태·본문 형식 판정 → 본문 dict. 본문 문자열은 어디에도 남기지 않는다(키를 에코할 수 있다)."""
        if r.status_code == 401:                  # 서울 버스 실측: 미신청 키·키 오류
            raise _auth_error(kind)
        if r.status_code == 429:
            raise exceeded_error(kind, self.quota.limits.get(kind), upstream_status=429)
        if r.status_code != 200:
            raise _upstream(kind, status_code=r.status_code)
        try:
            body = r.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise _gateway_error(kind, r.text, self.quota.limits.get(kind))
        return body
