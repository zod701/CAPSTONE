"""카카오 로컬 검색 정규화 — 합성 픽스처(문서 스키마로 손으로 씀)만 쓴다."""
import json
from pathlib import Path

from app.places import address_items, merge, place_items

FIX = Path(__file__).parent / "fixtures"
KEYWORD = json.loads((FIX / "keyword_synthetic.json").read_text(encoding="utf-8"))
ADDRESS = json.loads((FIX / "address_synthetic.json").read_text(encoding="utf-8"))

STATION = {"kind": "place", "name": "합성역 합성선", "detail": "지하철역", "address": "경기 합성시 합성로 12",
           "lat": 37.3947, "lon": 127.1112}
VALLEY = {"kind": "place", "name": "합성테크노밸리", "detail": "산업단지", "address": "경기 합성시 합성동 2",
          "lat": 37.4, "lon": 127.1}
ROAD_ADDR = {"kind": "address", "name": "경기 합성시 합성로 12", "detail": "합성타워", "address": "경기 합성시 합성동 1",
             "lat": 37.3947, "lon": 127.1112}
JIBUN_ADDR = {"kind": "address", "name": "경기 합성시 합성동 3-1", "detail": "", "address": "경기 합성시 합성로 30",
              "lat": 37.39, "lon": 127.12}
REGION = {"kind": "address", "name": "경기 합성시 합성동", "detail": "", "address": "", "lat": 37.395, "lon": 127.105}


def test_place_items_drop_bad_coords_and_nameless():
    # 좌표 빈 값 · 서비스 범위 밖 · 이름 없음은 뺀다. 분류는 대분류, 없으면 분류 경로의 끝. 주소는 도로명 → 지번
    assert place_items(KEYWORD) == [STATION, VALLEY]


def test_place_items_odd_values():
    raw = {"documents": [
        "not-a-dict",
        {"place_name": "  공백  ", "x": "127.0", "y": "37.0", "category_name": " 가 >  나 > ", "address_name": None},
        {"place_name": "무한", "x": "inf", "y": "37.0"},
        {"place_name": "숫자좌표", "x": 127.5, "y": 37.5},
    ]}
    assert place_items(raw) == [
        {"kind": "place", "name": "공백", "detail": "나", "address": "", "lat": 37.0, "lon": 127.0},
        {"kind": "place", "name": "숫자좌표", "detail": "", "address": "", "lat": 37.5, "lon": 127.5},
    ]
    assert place_items({}) == [] and place_items({"documents": None}) == []


def test_address_items_split_specific_and_region():
    # 도로명으로 찾으면 지번을, 지번으로 찾으면 도로명을 곁들인다. 건물 이름은 detail. 지역 이름뿐인 것은 뒤로
    assert address_items(ADDRESS) == ([ROAD_ADDR, JIBUN_ADDR], [REGION])


def test_address_items_road_only_and_same_text():
    raw = {"documents": [
        {"address_name": "경기 합성시 합성로", "address_type": "ROAD", "x": "127.1", "y": "37.4",
         "address": None, "road_address": {"address_name": "경기 합성시 합성로", "building_name": ""}},
        {"address_name": "경기 합성시 합성동 9", "address_type": "REGION_ADDR", "x": "127.1", "y": "37.4",
         "address": {"address_name": "경기 합성시 합성동 9"}, "road_address": {"address_name": "경기 합성시 합성동 9"}},
    ]}
    specific, general = address_items(raw)
    assert [(i["name"], i["address"]) for i in specific] == [("경기 합성시 합성동 9", "")]  # 같은 글이면 곁들이지 않는다
    assert [(i["name"], i["address"]) for i in general] == [("경기 합성시 합성로", "")]


def test_merge_order_and_missing_side():
    # 번지까지 맞는 주소 → 장소 → 지역 이름
    assert merge(KEYWORD, ADDRESS) == [ROAD_ADDR, JIBUN_ADDR, STATION, VALLEY, REGION]
    assert merge(KEYWORD, None) == [STATION, VALLEY]
    assert merge(None, ADDRESS) == [ROAD_ADDR, JIBUN_ADDR, REGION]
    assert merge(None, None) == []
