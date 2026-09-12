import json
from pathlib import Path

import pytest

from app.transit import get_prop, normalize_car, normalize_transit, parse_guidance, probe_summary

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def raw():
    return load("transit_synthetic.json")


@pytest.mark.parametrize("g, expected", [
    ("수인분당선 (수원 > 정자)", ("수원", "정자")),
    ("신분당선 (정자 > 판교(판교테크노밸리))", ("정자", "판교(판교테크노밸리)")),
    ("간선 470 (종로2가(중) > 광화문(중))", ("종로2가(중)", "광화문(중)")),
    ("정자정자역 환승", (None, None)),
    ("A > B", (None, None)),
    ("", (None, None)),
    (None, (None, None)),
])
def test_parse_guidance(g, expected):
    assert parse_guidance(g) == expected


def test_get_prop_fallback():
    assert get_prop({"properties": {"type": "BUS"}, "type": "X"}, "type") == "BUS"
    assert get_prop({"properties": {"a": 1}, "type": "BUS"}, "type") == "BUS"
    assert get_prop({"type": "BUS"}, "type") == "BUS"
    assert get_prop({}, "type") is None


def test_normalize_transit_routes(raw):
    norm = normalize_transit(raw)
    assert norm["status"] == "OK"
    assert norm["summary"] == {"total": 2, "bus": 1, "subway": 1, "busAndSubway": 0}
    r0, r1 = norm["routes"]
    assert (r0["idx"], r0["type"], r0["total_time_s"], r0["total_distance_m"], r0["transfers"]) == (0, "SUBWAY", 2350, 27600, 1)
    assert r0["fare"] == {"value": 2800, "min": None, "max": None}
    assert [s["type"] for s in r0["steps"]] == ["SUBWAY", "WALKING", "SUBWAY"]
    assert [s["idx"] for s in r0["steps"]] == [0, 1, 2]

    s0, walk, s2 = r0["steps"]
    assert s0["stops"] == ["수원", "신길온천", "정자"]
    assert s0["vehicles"] == [{"type": "급행", "name": "수인분당선"}]
    assert (s0["board_name"], s0["alight_name"]) == ("수원", "정자")
    assert (s0["guidance_from"], s0["guidance_to"]) == ("수원", "정자")
    assert s0["path"][0] == [127.0, 37.266] and len(s0["path"]) == 3
    assert (s0["distance_m"], s0["time_s"]) == (25000, 1900)

    assert walk["guidance"] == "정자정자역 환승"
    assert (walk["guidance_from"], walk["guidance_to"]) == (None, None)
    assert (walk["board_name"], walk["alight_name"]) == (None, None)
    assert walk["stops"] == [] and walk["vehicles"] == [] and len(walk["path"]) == 5

    assert (s2["board_name"], s2["alight_name"]) == ("정자", "판교(판교테크노밸리)")

    # 경로 1 의 구간은 properties 없는 평탄한 모양 → get_prop 폴백
    (b,) = r1["steps"]
    assert (b["type"], b["distance_m"], b["time_s"]) == ("BUS", 1700, 780)
    assert b["vehicles"] == [{"type": "간선", "name": "470"}]
    assert (b["guidance_from"], b["guidance_to"]) == ("종로2가(중)", "광화문(중)")
    assert (b["board_name"], b["alight_name"]) == ("종로2가사거리", "광화문(중)")  # stops 가 guidance 보다 우선
    assert b["path"][-1] == [126.9768, 37.5707]


def test_board_alight_fallback_to_guidance():
    def step(stops):
        return {"properties": {"type": "BUS", "guidance": "일반 10 (가 > 나)", "stops": stops}}

    none, one = normalize_transit({"routes": [{"steps": [step([]), step([{"name": "가정류장"}])]}]})["routes"][0]["steps"]
    assert (none["board_name"], none["alight_name"]) == ("가", "나")
    assert (one["board_name"], one["alight_name"]) == ("가정류장", "나")
    assert none["distance_m"] is None and none["time_s"] is None and none["path"] == []


def test_missing_and_unknown():
    raw = {"status": "NO_RESULTS"}
    assert normalize_transit(raw) == {
        "status": "NO_RESULTS",
        "summary": {"total": None, "bus": None, "subway": None, "busAndSubway": None},
        "routes": [],
    }
    raw = {"status": "OK", "routes": [{"properties": {"type": "X"}, "steps": [{"properties": {"type": "FERRY"}}]}]}
    norm = normalize_transit(raw)
    assert norm["routes"][0]["steps"][0]["type"] == "FERRY"  # 모르는 유형은 그대로 통과
    assert norm["routes"][0]["fare"] == {"value": None, "min": None, "max": None}
    probe = probe_summary(raw, norm)
    assert probe["unknown_step_types"] == ["FERRY"]
    assert probe["walking_points"] == {"n": 0, "min": None, "max": None, "share_2pt": None}
    assert probe["point_format"] == "empty"


@pytest.mark.parametrize("points, fmt", [
    ([[127.0, 37.0], [127.1, 37.1]], "pair"),
    ([{"x": 127.0, "y": 37.0}, {"x": 127.1, "y": 37.1}], "dict:x,y"),
    ([127.0, 37.0, 127.1, 37.1], "flat"),
])
def test_point_formats(points, fmt):
    raw = {"routes": [{"steps": [{"properties": {"type": "WALKING"}, "path": {"points": points}}]}]}
    norm = normalize_transit(raw)
    assert norm["routes"][0]["steps"][0]["path"] == [[127.0, 37.0], [127.1, 37.1]]
    assert probe_summary(raw, norm)["point_format"] == fmt


def test_unparsed_point_string_is_reported():
    raw = {"routes": [{"steps": [{"properties": {"type": "WALKING"}, "path": {"points": "127.0,37.0 127.1,37.1"}}]}]}
    norm = normalize_transit(raw)
    assert norm["routes"][0]["steps"][0]["path"] == []
    assert probe_summary(raw, norm)["point_format"] == "str"


def test_probe_summary(raw):
    norm = normalize_transit(raw)
    p = probe_summary(raw, norm)
    assert p["top_keys"] == ["_note", "properties", "routes", "status"]
    assert p["route_keys"] == ["properties", "steps"]
    assert p["route_prop_keys"] == ["fare", "totalDistance", "totalTime", "transfers", "type"]
    assert p["step_keys"] == ["path", "properties"]
    assert p["step_prop_keys"] == ["distance", "guidance", "stops", "time", "type", "vehicles"]
    assert p["path_keys"] == ["points"]
    assert p["point_format"] == "pair"
    assert p["n_routes"] == 2
    assert p["step_type_counts"] == {"SUBWAY": 2, "WALKING": 1, "BUS": 1}
    assert p["unknown_step_types"] == []
    assert p["vehicle_names"] == {"BUS": ["간선:470"], "SUBWAY": ["급행:수인분당선", "일반:신분당선"]}
    assert p["walking_points"] == {"n": 1, "min": 5, "max": 5, "share_2pt": 0.0}
    # 버스 승차: stops[0] '종로2가사거리' ≠ guidance '종로2가(중)'
    assert p["endpoints_vs_guidance"] == {"checked": 3, "board_eq": 2, "alight_eq": 3}

    r0 = p["routes"][0]
    assert (r0["idx"], r0["type"]) == (0, "SUBWAY")
    assert [s["type"] for s in r0["steps"]] == ["SUBWAY", "WALKING", "SUBWAY"]
    assert [s["n_stops"] for s in r0["steps"]] == [3, 0, 2]
    assert [s["n_vehicles"] for s in r0["steps"]] == [1, 0, 1]
    assert [s["n_path_points"] for s in r0["steps"]] == [3, 5, 2]
    gaps = [s["gap_prev_m"] for s in r0["steps"]]
    assert gaps[0] is None
    assert gaps[1] == pytest.approx(11.1, abs=0.3)  # 위도 0.0001도
    assert gaps[2] == 0.0
    assert p["routes"][1]["steps"] == [{"type": "BUS", "n_stops": 3, "n_vehicles": 1, "n_path_points": 3, "gap_prev_m": None}]
    json.dumps(p)  # 그대로 응답에 실을 수 있어야 한다


def test_normalize_car():
    car = normalize_car(load("car_synthetic.json"))
    assert (car["result_code"], car["result_msg"]) == (0, "길찾기 성공")
    assert (car["distance_m"], car["duration_s"]) == (12000, 1500)
    assert car["fare"] == {"taxi": 15000, "toll": 0}
    # 도로 안·도로 경계·구간 경계의 연속 중복점 제거
    assert car["path"] == [[127.0, 37.26], [127.001, 37.261], [127.002, 37.262], [127.003, 37.263]]


def test_normalize_car_failure_has_no_path():
    raw = {"routes": [{"result_code": 104, "result_msg": "출발지와 도착지가 5 m 이내로 설정된 경우 경로를 탐색할 수 없음",
                       "sections": [{"roads": [{"vertexes": [127.0, 37.0, 127.1, 37.1]}]}]}]}
    car = normalize_car(raw)
    assert car["result_code"] == 104 and car["path"] == []
    assert car["distance_m"] is None and car["fare"] == {"taxi": None, "toll": None}
