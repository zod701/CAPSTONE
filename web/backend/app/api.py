"""/api 라우터 — 상태·정류소·정류장 경유 노선·경계·대중교통·자동차·장소 검색·쿼터.

카카오 응답(raw)은 요청 처리 중 지역 변수로만 두고 정규화한 결과만 돌려준다 — 저장·캐시·로그 금지.
"""
import asyncio
import math
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse

from . import places
from .errors import ApiError
from .resolver import resolve_route, summarize
from .transit import normalize_car, normalize_transit, probe_summary

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


def _diagnose(db, routes, settings):
    """경로마다 승·하차 매칭 → route.diag_summary. 전체 요약은 (종류, 이름 키, 기준점) 이 같은 항목을 한 번만 센다."""
    seen, unique, unmapped_all = set(), [], set()
    memo = {}  # 경로 사이에 똑같은 구간의 정류소 위치 (이 요청 안에서만)
    for route in routes:
        entries = resolve_route(db, route, settings.radius_m, settings.ambig_gap_m, memo=memo)
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


@router.get("/transit")
async def transit(request: Request, response: Response, sx: Lon, sy: Lat, ex: Lon, ey: Lat, probe: bool = False):
    st = request.app.state
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
    norm["diag_summary"] = _diagnose(st.stops, norm["routes"], st.settings) if st.stops else None
    norm["quota"] = st.quota.snapshot()
    response.headers["Cache-Control"] = "no-store"
    return norm


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


@router.get("/quota")
def quota(request: Request):
    return request.app.state.quota.snapshot()
