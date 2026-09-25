"""/api 라우터 — 상태·정류소·정류장 경유 노선·경계·대중교통·자동차·장소 검색·실시간 도착·쿼터.

카카오 응답(raw)은 요청 처리 중 지역 변수로만 두고 정규화한 결과만 돌려준다 — 저장·캐시·로그 금지.
공공데이터 도착정보는 저장이 허용되어 realtime.py 가 15초만 메모리에 둔다 — 두 규칙을 섞지 않는다.
"""
import asyncio
import math
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Query, Request, Response
from fastapi.responses import FileResponse

from . import hybrid as hybrid_plan
from . import places
from .config import KST
from .errors import ApiError
from .resolver import resolve_route, summarize
from .transit import normalize_car, normalize_transit, probe_summary
from .wait import add_wait

router = APIRouter(prefix="/api")

Lon = Annotated[float, Query(ge=124, le=132)]
Lat = Annotated[float, Query(ge=33, le=39)]
MAX_LIMIT = 10000
WORLD = (-180.0, -90.0, 180.0, 90.0)
NO_ROUTE = {
    "STARTNODES_NULL": "출발지 근처에서 대중교통 정류장을 찾지 못했습니다.",
    "ENDNODES_NULL": "도착지 근처에서 대중교통 정류장을 찾지 못했습니다.",
    "EQUAL_POINTS": "출발지와 도착지가 같습니다.",
    "NO_RESULTS": "대중교통 경로가 없습니다.",
}


def _not_built():
    return ApiError("data_not_built", "정제 데이터가 없습니다.",
                    action="data 파이프라인을 먼저 실행하세요 (data/README.md).", status=503)


def _bad(message):
    return ApiError("invalid_request", message, status=400)


@router.get("/health")
def health(request: Request):
    s, db = request.app.state.settings, request.app.state.stops
    return {
        "ready": db is not None,
        "bus_stops": db.counts["bus"] if db else 0,
        "subway_stations": db.counts["subway"] if db else 0,
        "data_mtime": db.data_mtime if db else None,
        "keys": {"vworld": bool(s.vworld_key), "kakao_rest": bool(s.kakao_rest_key)},  # 값은 절대 내보내지 않는다
    }


SERVICE_BOX = (124.0, 33.0, 132.0, 39.5)   # 프론트 maxBounds 와 같다


def _parse_bbox(text):
    """→ 서비스 범위로 잘라낸 [minLon, minLat, maxLon, maxLat]. 거부 대신 절단 — 지도 가장자리 화면도 받는다."""
    try:
        box = [float(v) for v in text.split(",")]
    except ValueError:
        box = []
    if len(box) != 4 or not all(map(math.isfinite, box)) or box[0] > box[2] or box[1] > box[3]:
        raise _bad("bbox 는 minLon,minLat,maxLon,maxLat 형식의 숫자 4개여야 합니다.")
    lo_lon, lo_lat, hi_lon, hi_lat = SERVICE_BOX
    return [min(max(box[0], lo_lon), hi_lon), min(max(box[1], lo_lat), hi_lat),
            min(max(box[2], lo_lon), hi_lon), min(max(box[3], lo_lat), hi_lat)]


def _bus_item(db, r):
    return {"id": r["stop_key"], "name": r["name"], "lat": r["lat"], "lon": r["lon"], "sgg": r["sgg_nm"],
            "ars": r["ars_nos"], "src": r["source_ids"], "merge": r["merge_status"], "virtual": r["is_virtual"],
            "snap_m": float(r["snap_dist_m"]) if r["snap_dist_m"] else None,
            "label": r["raw_city_label"], "aliases": r["aliases"]}


def _subway_item(db, r):
    return {"id": r["station_id"], "name": r["name"], "line": r["line_raw"], "group": r["line_group"],
            "color": db.group_color(r["line_group"]), "lat": r["lat"], "lon": r["lon"], "sgg": r["sgg_nm"],
            "transfer": r["is_transfer"], "date": r["data_date"], "operator": r["operator"]}


ITEMS = {"bus": _bus_item, "subway": _subway_item}


@router.get("/stops")
def stops(request: Request, bbox: str | None = None, kinds: str = "bus,subway", virtual: bool = False,
          limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 5000):
    """화면 범위의 정류소·역. limit 는 종류별 상한 — 넘치면 앞에서 자르고 truncated=true."""
    want = list(dict.fromkeys(k.strip() for k in kinds.split(",") if k.strip()))
    if not want or set(want) - set(ITEMS):
        raise _bad("kinds 는 bus, subway 중에서 고르세요.")
    box = _parse_bbox(bbox) if bbox else None
    if box is None and "bus" in want:
        raise _bad("버스 정류장을 조회하려면 bbox 가 필요합니다.")
    db = request.app.state.stops
    if db is None:
        raise _not_built()
    out = {"count": 0, "truncated": False, "bus": [], "subway": []}
    for kind in want:
        idx = db.bbox(kind, *(box or WORLD), include_virtual=virtual)
        out["truncated"] |= len(idx) > limit
        out[kind] = [ITEMS[kind](db, db.row(kind, i)) for i in idx[:limit]]
        out["count"] += len(out[kind])
    return out


def _routes_db(request):
    db = request.app.state.routes
    if db is None:
        raise ApiError("data_not_built", "버스 노선 순서표가 없습니다.",
                       action="data 파이프라인의 build_bus_route_seq.py 를 실행하세요 (data/README.md).", status=503)
    return db


@router.get("/stops/{stop_key}/routes")
def stop_routes(request: Request, stop_key: str):
    """버스 정류장을 지나는 노선 (미정차 제외, 이름 자연 정렬). 모르는 정류장이면 빈 목록."""
    return {"stop": stop_key, "routes": _routes_db(request).routes_at(stop_key)}


@router.get("/routes/{route_id}")
def route_stops(request: Request, route_id: str):
    """노선이 지나는 정류장 (운행 순서, 미정차 제외, 같은 정류장은 한 번)."""
    route = _routes_db(request).route(route_id)
    if route is None:
        raise ApiError("not_found", "노선을 찾을 수 없습니다.", status=404)
    return route


@router.get("/boundaries")
def boundaries(request: Request):
    path = request.app.state.settings.processed_dir / "admin_sgg_web.geojson"
    if not path.is_file():
        raise _not_built()
    return FileResponse(path, media_type="application/geo+json")


def _diagnose(db, routes_db, lines_db, routes, settings):
    """경로마다 승·하차 매칭 → route.diag_summary. 전체 요약은 (종류, 이름 키, 기준점) 이 같은 항목을 한 번만 센다.
    순서표가 있으면 버스 구간의 진행 방향(상·하행)을 운행 순서로 가리고, 구간의 경유 정류소·역을 순서표에서 읽는다."""
    seen, unique, unmapped_all = set(), [], set()
    memo = {}  # 경로 사이에 똑같은 구간의 정류소 위치 (이 요청 안에서만)
    for route in routes:
        entries = resolve_route(db, route, settings.radius_m, settings.ambig_gap_m,
                                memo=memo, routes=routes_db, lines=lines_db)
        unmapped = {v["name"] for s in route["steps"] if s["type"] == "SUBWAY"
                    for v in s["vehicles"] if db.kakao_group(v["name"]) is None}
        route["diag_summary"] = summarize(entries, unmapped)
        unmapped_all |= unmapped
        for e in entries:
            a = e["anchor"]
            key = (e["kind"], e["query_key"], round(a[0], 5) if a else None, round(a[1], 5) if a else None)
            if key not in seen:
                seen.add(key)
                unique.append(e)
    return summarize(unique, unmapped_all)


async def transit_payload(st, sx, sy, ex, ey, probe=False, realtime=False):
    """대중교통 경로 한 번 — 정규화 + 접지·대기까지 끝낸 응답. 하이브리드도 기준 경로를 이것으로 받는다.

    `realtime` 이면 첫 승차 대기를 실시간 도착정보로 바꾼다(첫 승차 정류장·역마다 도착정보 1콜, 15초 캐시).
    '출발지에서 걸어서 첫 정류장에 닿는다' 는 전제라 [경로 검색] 결과에만 켠다 — 하이브리드는 택시로 정류장에 닿는
    A 에 원래 경로가 필요해 받은 뒤 스스로 바꾸고, 앵커에서 다시 부르는 경로에는 켜지 않는다.
    """
    raw = await st.kakao.transit(sx, sy, ex, ey)
    norm = normalize_transit(raw)
    if probe:
        norm["probe"] = probe_summary(raw, norm)
    status = norm["status"]
    if status == "INVALID_REQUEST":
        raise ApiError("invalid_request", "카카오가 대중교통 경로 요청이 올바르지 않다고 응답했습니다 (INVALID_REQUEST).",
                       action="출발지·도착지 좌표를 확인하세요.", status=400)
    norm["message"] = None
    if status != "OK":
        norm["routes"] = []
        norm["message"] = NO_ROUTE.get(status, "대중교통 경로를 찾지 못했습니다.")
    norm["db_ready"] = st.stops is not None
    norm["diag_summary"] = (_diagnose(st.stops, st.routes, st.lines, norm["routes"], st.settings)
                            if st.stops else None)
    # 카카오 시간에는 대기가 없다 — 배차표가 있으면 구간마다 기다릴 시간을 더한 값도 함께 준다
    norm["wait_basis"] = add_wait(norm["routes"], st.headway, datetime.now(KST), st.routes)
    norm["realtime"] = None   # 첫 승차 대기 진단 — realtime · realtime_stops · realtime+headway · no_arrivals … (안 썼으면 None)
    if realtime and norm["routes"] and st.stops is not None and st.lines is not None:
        try:
            norm["routes"], norm["realtime"] = await hybrid_plan.realtime_first_waits(
                st, realtime_items, (sx, sy), norm["routes"])
        except ApiError:   # algo 패키지가 없으면 배차로 추정한 대기로 남는다
            pass
    return norm


@router.get("/transit")
async def transit(request: Request, response: Response, sx: Lon, sy: Lat, ex: Lon, ey: Lat, probe: bool = False):
    st = request.app.state
    norm = await transit_payload(st, sx, sy, ex, ey, probe, realtime=True)
    norm["quota"] = st.quota.snapshot()
    response.headers["Cache-Control"] = "no-store"
    return norm


HybridTop = Annotated[int, Query(ge=1, le=hybrid_plan.MAX_TOP)]
HybridTmax = Annotated[int, Query(ge=3, le=40)]


@router.get("/hybrid")
async def hybrid(request: Request, response: Response, sx: Lon, sy: Lat, ex: Lon, ey: Lat,
                 top: HybridTop = 5, t_max: HybridTmax = 30):
    """택시 ↔ 대중교통 연계 경로. 앵커마다 카카오를 부르므로 화면의 버튼을 눌렀을 때만 돈다. 기준 경로도 새로 부른다."""
    return await _hybrid(request, response, (sx, sy), (ex, ey), top, t_max, None)


@router.post("/hybrid")
async def hybrid_reuse(request: Request, response: Response, sx: Lon, sy: Lat, ex: Lon, ey: Lat,
                       body: Annotated[dict, Body()], top: HybridTop = 5, t_max: HybridTmax = 30):
    """같은 출발·도착의 [경로 검색] 결과(화면이 들고 있는 routes)를 본문으로 받아 기준 경로로 다시 쓴다 — 대중교통 1콜을 아낀다.

    카카오 응답을 서버에 두지 않는 규칙은 그대로다: 본문은 이 요청을 처리하는 동안에만 쓰고 버린다(web/README.md §6).
    화면은 결과를 받은 지 5분 안이고 출발·도착이 그대로일 때만 보낸다(app.js). 첫 승차 대기는 이 요청 시각의 실시간으로 다시 매긴다.
    본문은 브라우저가 보낸 것이라 믿지 않는다 — 모양이 맞지 않으면 400.
    """
    routes = body.get("routes") if isinstance(body, dict) else None
    if not (isinstance(routes, list) and routes
            and all(isinstance(r, dict) and isinstance(r.get("steps"), list) for r in routes)):
        raise ApiError("invalid_request", "기준으로 쓸 대중교통 경로가 올바르지 않습니다.",
                       action="[경로 검색]을 다시 한 뒤 찾으세요.", status=400)
    try:
        return await _hybrid(request, response, (sx, sy), (ex, ey), top, t_max, routes)
    except (KeyError, TypeError, AttributeError, ValueError) as err:   # 모양은 맞는데 속 항목이 빠진 본문
        raise ApiError("invalid_request", "기준으로 쓸 대중교통 경로가 올바르지 않습니다.",
                       action="[경로 검색]을 다시 한 뒤 찾으세요.", status=400) from err


async def _hybrid(request, response, o, d, top, t_max, base_routes):
    st = request.app.state
    out = await hybrid_plan.plan(st, transit_payload, o, d, arrivals=realtime_items, base_routes=base_routes,
                                 top=top, t_max_s=t_max * 60)
    out["quota"] = st.quota.snapshot()
    response.headers["Cache-Control"] = "no-store"
    return out


@router.get("/car")
async def car(request: Request, response: Response, sx: Lon, sy: Lat, ex: Lon, ey: Lat):
    st = request.app.state
    out = normalize_car(await st.kakao.car(sx, sy, ex, ey))
    out["quota"] = st.quota.snapshot()
    response.headers["Cache-Control"] = "no-store"
    return out


QUERY_MAX = 100          # 카카오 로컬 검색어 한도 (넘으면 카카오가 400 — 실측)
KEYWORD_SIZE = 10        # 카카오 최대 15
ADDRESS_SIZE = 5         # 카카오 최대 30. 주소는 보통 1–2건


@router.get("/search")
async def search(request: Request, response: Response, q: str = ""):
    """주소·장소 이름 → 출발·도착 후보. 키워드·주소 검색을 동시에 부르고, 한쪽만 실패하면 다른 쪽 결과와
    failed 를 준다(둘 다 실패하면 그 오류)."""
    q = q.strip()
    if not q:
        raise _bad("검색어를 입력하세요.")
    if len(q) > QUERY_MAX:
        raise _bad(f"검색어는 {QUERY_MAX}자 이하로 입력하세요.")
    st = request.app.state
    kw, ad = await asyncio.gather(st.kakao.keyword(q, KEYWORD_SIZE), st.kakao.address(q, ADDRESS_SIZE),
                                  return_exceptions=True)
    for r in (kw, ad):
        if isinstance(r, BaseException) and not isinstance(r, ApiError):
            raise r  # 우리 쪽 결함은 그대로 500 으로
    if isinstance(kw, ApiError) and isinstance(ad, ApiError):
        raise kw
    failed = [{"kind": kind, "code": e.code, "message": e.message}
              for kind, e in (("keyword", kw), ("address", ad)) if isinstance(e, ApiError)]
    items = places.merge(None if isinstance(kw, ApiError) else kw, None if isinstance(ad, ApiError) else ad)
    response.headers["Cache-Control"] = "no-store"
    return {"items": items, "failed": failed, "quota": st.quota.snapshot()}


def _live_db(st):
    db = st.live_stations
    if db is None:
        raise ApiError("data_not_built", "도시철도 실시간 역 매핑표가 없습니다.",
                       action="data 파이프라인의 build_subway_live_map.py 를 실행하세요 (data/README.md).", status=503)
    return db


def _no_realtime(message):
    return ApiError("no_realtime", message, status=404,
                    action="배차 간격 표(data/processed/headway_rail.csv · headway_bus.csv)로 추정한 대기시간을 쓰세요.")


BIS_SOURCE = {"GGB": "gyeonggi", "SEB": "seoul"}   # processed/bus_stops.csv 의 source_ids 접두어


FAR = 10**6   # 남은 역·정류소 수가 없는 항목을 맨 뒤로 보내는 값


def _eta_order(item):
    """도착 임박 순. 예측이 없는 항목(운행종료·출발대기·이미 출발)은 뒤로 보내고, 그중에서는 남은 역·정류소
    수가 적은 쪽을 앞에 둔다 — 그 값도 없으면(도착·출발 문구) 가장 뒤다."""
    return (item["eta_s"] is None, item["eta_s"] or 0,
            item["n_stops_ahead"] if item["n_stops_ahead"] is not None else FAR)


async def arrivals_stop_payload(st, stop_key):
    """버스 정류장 실시간 도착 → {stop, name, items, failed}. 하이브리드도 첫 승차 대기를 이것으로 받는다."""
    if st.stops is None:
        raise _not_built()
    row = st.stops.by_id("bus", stop_key)
    if row is None:
        raise ApiError("not_found", "정류장을 찾을 수 없습니다.", status=404)
    if row["is_virtual"]:
        raise _no_realtime("미정차 정류소에는 실시간 도착 정보가 없습니다.")
    # 업스트림 id 는 source_ids 에서 접두어를 뗀 9자리다 — stop_key 를 그대로 쓰면 '105000067-GGB' 꼴로
    # 갈라 둔 행(17개)이 깨진다
    calls = []   # [(원천, 업스트림 정류소 id)]
    for sid in row["source_ids"].split("|"):
        source = BIS_SOURCE.get(sid[:3])
        if source and source not in {s for s, _ in calls}:
            calls.append((source, sid[3:]))
    if not calls:
        raise _no_realtime("이 정류장은 실시간 도착 정보를 제공하지 않습니다.")
    fetch = {"gyeonggi": st.realtime.gyeonggi_stop, "seoul": st.realtime.seoul_stop}
    got = await asyncio.gather(*(fetch[source](up_id) for source, up_id in calls), return_exceptions=True)
    for r in got:
        if isinstance(r, BaseException) and not isinstance(r, ApiError):
            raise r  # 우리 쪽 결함은 그대로 500 으로
    if all(isinstance(r, ApiError) for r in got):
        raise got[0]
    types = {r["id"]: r["type"] or None for r in st.routes.routes_at(stop_key)} if st.routes else {}
    items, failed = [], []
    for (source, _), r in zip(calls, got):
        if isinstance(r, ApiError):
            failed.append({"source": source, "code": r.code, "message": r.message})
            continue
        for it in r:
            if it["route_type"] is None and it["route_id"]:
                it["route_type"] = types.get(it["route_id"])   # 순서표의 카카오 유형 이름 (프론트 BUS_COLOR 키)
            items.append(it)
    items.sort(key=_eta_order)
    return {"stop": stop_key, "name": row["name"], "items": items, "failed": failed}


@router.get("/arrivals/stop/{stop_key}")
async def arrivals_stop(request: Request, stop_key: str):
    """버스 정류장 실시간 도착. 그 정류장을 등록한 BIS 전부를 동시에 부르고 합친다
    (한쪽만 실패하면 다른 쪽 결과 + failed — /api/search 와 같다)."""
    st = request.app.state
    out = await arrivals_stop_payload(st, stop_key)
    out["quota"] = st.quota.snapshot()
    return out


async def arrivals_station_payload(st, station_id):
    """도시철도 역 실시간 도착 → {station, name, line_group, items, failed}. 하이브리드도 이것으로 받는다."""
    if st.stops is None:
        raise _not_built()
    row = st.stops.by_id("subway", station_id)
    if row is None:
        raise ApiError("not_found", "역을 찾을 수 없습니다.", status=404)
    live = _live_db(st).live_for(station_id)
    if live is None:
        raise _no_realtime("이 역은 실시간 도착 정보를 제공하지 않습니다.")
    items = await st.realtime.subway_station(*live)   # (실시간 역명, subwayId)
    items.sort(key=_eta_order)
    return {"station": station_id, "name": row["name"], "line_group": row["line_group"],
            "items": items, "failed": []}


@router.get("/arrivals/station/{station_id}")
async def arrivals_station(request: Request, station_id: str):
    """도시철도 역 실시간 도착. 실시간 API 는 역명 정확 일치만 받으므로 매핑표에서 역명·노선을 찾는다.

    원천이 하나라 부분 실패가 없다 — 오류는 그대로 봉투로 올린다. `failed` 는 모양을 맞추려 항상 [] 로 둔다
    (프론트가 두 도착정보 엔드포인트를 같은 코드로 다루게).
    """
    st = request.app.state
    out = await arrivals_station_payload(st, station_id)
    out["quota"] = st.quota.snapshot()
    return out


async def realtime_items(st, kind, stop_id):
    """첫 승차 지점 하나의 실시간 도착 — 정류장이면 BIS, 역이면 도시철도 원천 (하이브리드가 넘겨받는다)."""
    payload = arrivals_station_payload if kind == "subway" else arrivals_stop_payload
    return await payload(st, stop_id)


@router.get("/quota")
def quota(request: Request):
    return request.app.state.quota.snapshot()
