"""카카오 REST 클라이언트 (대중교통·자동차·로컬 장소/주소 검색).

약관상 응답은 요청 처리 중 메모리에만 둔다 — 저장·캐시·로그 금지(로컬 검색은 짧은 캐시도 금지). 오류 본문은 키를
에코할 수 있어 사용자 메시지에는 마스킹한 필드만 넣고, 본문·예외 문자열은 어디에도 남기지 않는다.
"""
import json
import re

import httpx

from .errors import ApiError
from .quota import LABELS, QuotaGuard, exceeded_error

TRANSIT_URL = "https://dapi.kakao.com/v2/routing/publictraffic"
CAR_URL = "https://apis-navi.kakaomobility.com/v1/directions"
KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
ADDRESS_URL = "https://dapi.kakao.com/v2/local/search/address.json"

_SECRET = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}")

_MAP_DISABLED_ACTION = ("developers.kakao.com → 내 애플리케이션 → [바로가] → [제품 설정] → [카카오맵] → "
                        "[사용 설정]을 ON 으로 바꾼 뒤 다시 시도하세요 (반영까지 수 분 걸릴 수 있음).")
# 카카오맵 사용 설정 하나가 대중교통과 로컬 검색을 함께 연다 (거절 문구 OPEN_MAP_AND_LOCAL)
_MAP_DISABLED_WHAT = {"transit": "대중교통 경로를 조회할", "keyword": "장소를 검색할", "address": "주소를 검색할"}


def redact(text: str) -> str:
    """32자리 hex(카카오 REST 키)와 UUID(VWorld 키)를 가린다."""
    return _SECRET.sub("<REDACTED>", text or "")


def _detail(body):
    """오류 본문의 errorType / status / message / msg (마스킹)."""
    if not isinstance(body, dict):
        return ""
    return " / ".join(redact(str(body[k]))[:200] for k in ("errorType", "status", "message", "msg")
                      if body.get(k) not in (None, ""))


def _with_detail(head, detail):
    return f"{head}: {detail}" if detail else f"{head}."


def map_error(kind: str, status_code: int, body_text: str, limit: int | None = None) -> ApiError:
    """카카오 비-200 응답 → ApiError (순수 함수). limit 는 쿼터 소진 문구에 넣을 우리 쪽 일일 한도."""
    try:
        body = json.loads(body_text)
    except ValueError:
        body = None
    label = LABELS.get(kind, kind)
    if status_code == 403 and ("OPEN_MAP_AND_LOCAL" in body_text or "disabled" in body_text):
        what = _MAP_DISABLED_WHAT.get(kind, f"{label} 요청을 처리할")
        return ApiError("kakao_map_disabled", f"카카오맵 서비스가 비활성화되어 있어 {what} 수 없습니다.",
                        action=_MAP_DISABLED_ACTION, status=503, upstream_status=403)
    if status_code == 403:
        return ApiError("kakao_forbidden", _with_detail(f"카카오가 {label} 요청을 거부했습니다 (HTTP 403)", _detail(body)),
                        action="developers.kakao.com 콘솔에서 앱의 제품 설정과 허용 IP 를 확인하세요.",
                        status=502, upstream_status=403)
    if status_code == 401:
        return ApiError("kakao_auth", "카카오 REST API 키가 올바르지 않습니다.",
                        action=".env 의 KAKAO_REST_API_KEY 를 확인하세요.", status=503, upstream_status=401)
    # 로컬 API 는 code 를 문자열로 준다 ('-2' 실측) — -10 도 두 형식을 다 받는다
    if status_code == 429 or (status_code == 400 and isinstance(body, dict) and str(body.get("code")) == "-10"):
        return exceeded_error(kind, limit, upstream_status=status_code)
    return ApiError("upstream_error",
                    _with_detail(f"카카오 {label} 요청이 실패했습니다 (HTTP {status_code})", _detail(body)),
                    status=502, upstream_status=status_code)


class KakaoClient:
    def __init__(self, http: httpx.AsyncClient, rest_key: str | None, quota: QuotaGuard, timeout_s: float = 10.0):
        self.http = http
        self.rest_key = rest_key
        self.quota = quota
        self.timeout_s = timeout_s

    async def transit(self, sx, sy, ex, ey) -> dict:
        return await self._get("transit", TRANSIT_URL, {"start_x": sx, "start_y": sy, "end_x": ex, "end_y": ey})

    async def car(self, sx, sy, ex, ey) -> dict:
        return await self._get("car", CAR_URL, {"origin": f"{sx},{sy}", "destination": f"{ex},{ey}"})

    async def keyword(self, query, size) -> dict:
        """키워드로 장소 검색 (size 1–15)."""
        return await self._get("keyword", KEYWORD_URL, {"query": query, "size": size})

    async def address(self, query, size) -> dict:
        """주소 검색 (size 1–30). 장소 이름에는 0건이다."""
        return await self._get("address", ADDRESS_URL, {"query": query, "size": size})

    async def _get(self, kind, url, params):
        if not self.rest_key:
            raise ApiError("missing_key", "카카오 REST API 키가 설정되지 않았습니다.",
                           action=".env 에 KAKAO_REST_API_KEY 를 추가하고 서버를 다시 시작하세요.", status=503)
        # 부르기 전에 1건을 예약한다 (동시 요청도 한도를 못 넘게). 쿼터를 안 쓴 결과만 되돌린다.
        day = self.quota.reserve(kind)
        label = LABELS[kind]
        # httpx 예외 문자열은 쓰지 않는다 (from None: 연쇄 traceback 에도 남기지 않음)
        try:
            r = await self.http.get(url, params=params, headers={"Authorization": f"KakaoAK {self.rest_key}"},
                                    timeout=self.timeout_s)
        except httpx.TimeoutException:  # 카카오에 닿았을 수 있으니 예약을 그대로 둔다
            raise ApiError("upstream_timeout", f"카카오 {label} 응답이 {self.timeout_s:g}초 안에 오지 않았습니다.",
                           action="잠시 후 다시 시도하세요.", status=504) from None
        except httpx.ConnectError:
            self.quota.release(kind, day)
            raise ApiError("upstream_unreachable", "카카오 서버에 연결할 수 없습니다.",
                           action="인터넷 연결을 확인하세요.", status=502) from None
        except httpx.TransportError:  # 연결 뒤 끊김 등 — 닿았을 수 있으니 예약을 그대로 둔다
            raise ApiError("upstream_error", f"카카오 {label} 응답을 받지 못했습니다.",
                           action="잠시 후 다시 시도하세요.", status=502) from None
        if r.status_code == 200:
            return r.json()
        err = map_error(kind, r.status_code, r.text, limit=self.quota.limits.get(kind))
        err.message = err.message.replace(self.rest_key, "<REDACTED>")  # hex 형식이 아닌 키가 에코된 경우까지
        if err.code == "quota_exceeded":
            self.quota.mark_exhausted(kind, day)
        elif err.code != "upstream_error":  # 401·403: 카카오 쿼터를 쓰지 않은 거절
            self.quota.release(kind, day)
        raise err
