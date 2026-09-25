"""카카오 로컬 검색 응답 → 출발·도착 후보 목록 (순수 함수 — 저장·로그 없음).

키워드 검색(장소)과 주소 검색을 함께 부르고 합친다. 실측(2026-09-11): 주소 검색은 장소 이름에 0건이고,
키워드 검색은 주소를 넣으면 그 주소의 건물들을 준다. 그래서 순서는 번지까지 맞는 주소 → 장소 → 지역·도로 이름뿐인 주소.
좌표는 문자열 x(경도)·y(위도) — 숫자가 아니거나 경로 검색이 받는 범위 밖이면 뺀다. 카카오 id·전화·URL 은 넘기지 않는다.
"""
import math

LON_RANGE = (124.0, 132.0)  # /api/transit · /api/car 가 받는 범위와 같다
LAT_RANGE = (33.0, 39.0)
SPECIFIC = ("ROAD_ADDR", "REGION_ADDR")  # 건물번호·번지까지 있는 주소 (REGION·ROAD 는 지역·도로 이름뿐)


def _text(v):
    return v.strip() if isinstance(v, str) else ""


def reverse_address(raw: dict) -> str:
    for doc in raw.get("documents") or []:
        for kind in ("road_address", "address"):
            address = _text((doc.get(kind) or {}).get("address_name"))
            if address:
                return address
    return ""


def _latlon(doc):
    try:
        lon, lat = float(doc.get("x")), float(doc.get("y"))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    if not (LON_RANGE[0] <= lon <= LON_RANGE[1] and LAT_RANGE[0] <= lat <= LAT_RANGE[1]):
        return None
    return lat, lon


def _category(doc):
    """'지하철역' 같은 대분류, 없으면 분류 경로의 끝 ('부동산 > 산업단지' → '산업단지')."""
    group = _text(doc.get("category_group_name"))
    if group:
        return group
    parts = [p.strip() for p in _text(doc.get("category_name")).split(">") if p.strip()]
    return parts[-1] if parts else ""


def _item(kind, name, detail, address, ll):
    return {"kind": kind, "name": name, "detail": detail, "address": address, "lat": ll[0], "lon": ll[1]}


def place_items(raw: dict) -> list[dict]:
    """키워드 검색 → [{kind: 'place', name, detail(분류), address(도로명, 없으면 지번), lat, lon}]."""
    out = []
    for d in raw.get("documents") or []:
        if not isinstance(d, dict):
            continue
        ll, name = _latlon(d), _text(d.get("place_name"))
        if ll and name:
            address = _text(d.get("road_address_name")) or _text(d.get("address_name"))
            out.append(_item("place", name, _category(d), address, ll))
    return out


def address_items(raw: dict) -> tuple[list[dict], list[dict]]:
    """주소 검색 → (번지까지 맞는 주소, 지역·도로 이름뿐인 주소).
    항목: {kind: 'address', name(찾은 형식의 주소), detail(건물 이름), address(다른 형식 — 도로명↔지번), lat, lon}."""
    specific, general = [], []
    for d in raw.get("documents") or []:
        if not isinstance(d, dict):
            continue
        ll, name = _latlon(d), _text(d.get("address_name"))
        if not (ll and name):
            continue
        road = d.get("road_address") if isinstance(d.get("road_address"), dict) else {}
        jibun = d.get("address") if isinstance(d.get("address"), dict) else {}
        kind = d.get("address_type")
        other = _text((jibun if kind in ("ROAD", "ROAD_ADDR") else road).get("address_name"))
        item = _item("address", name, _text(road.get("building_name")), "" if other == name else other, ll)
        (specific if kind in SPECIFIC else general).append(item)
    return specific, general


def merge(keyword_raw: dict | None, address_raw: dict | None) -> list[dict]:
    """두 검색 결과를 한 목록으로. 받지 못한 쪽은 None."""
    places = place_items(keyword_raw) if keyword_raw is not None else []
    specific, general = address_items(address_raw) if address_raw is not None else ([], [])
    return [*specific, *places, *general]
