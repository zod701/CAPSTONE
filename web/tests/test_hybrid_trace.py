from algo import search
from algo.anchors import _near
from app.hybrid_trace import LIMIT, STAGES, VERIFY, HybridTrace, _geometry, _itinerary

O, D = (127, 37), (128, 38)
GEN = {"heads": {}, "tails": {}, "pairs": {}, "line_head": {"buffers": {"bus": []}}, "line_tail": {"buffers": {}}}


def point(pid, lon=127.1, lat=37.1):
    return {"id": pid, "name": pid, "lon": lon, "lat": lat}


def candidate(cid, head="route", tail="route", head_steps=(), tail_steps=({"type": "BUS"},), lon=127.1):
    h = search._part(dict(point("O", *O), name="출발지") if head == "origin" else point("u" + cid, lon),
                     list(head_steps), 0.0, 0.0, source=head)
    t = search._part(dict(point("D", *D), name="목적지") if tail == "destination" else point("v" + cid, lon + 0.1),
                     list(tail_steps), 0.0, 0.0, source=tail)
    return {"id": cid, "head": h, "tail": t, "time_s": 600.0, "fare": 5000.0}


def trace_of(cands):
    return HybridTrace(O, D, 500, {"time_s": 1000, "fare": 2000}, cands, GEN)


def row(steps, taxi_at, time_s=400, fare=3000, pareto=True):
    return {"time_s": time_s, "fare": fare, "pareto": pareto, "taxi_at": taxi_at, "transit": {"steps": steps}}


def test_search_buffer_records_actual_expansion_separately_by_kind():
    class Stops:
        def near(self, kind, lon, lat, radius):
            return [(0, 500)] if kind == "bus" and radius >= 800 else []

        def row(self, kind, index):
            return {"stop_key": "stop"}

    used = {}
    _near(Stops(), "bus", (127, 37), (400, 800, 1500), radius_used=used)
    _near(Stops(), "subway", (127, 37), (400, 800, 1500), radius_used=used)
    assert used == {"bus": 800, "subway": 1500}


def test_stage_counts_are_consistent():
    a, b, c, e = (candidate(x) for x in "abce")
    trace = trace_of([a, b, c, e])
    r = row([{"type": "BUS"}], 0)
    r["trace_id"] = trace.verified(a, r)
    trace.reject(b, "경로 검증", "경로 없음")
    trace.reject(c, "경로 검증", "API 실패")
    trace.reject(e, "검증 순서·예산", "차례가 오지 않음")
    payload = trace.finish([r])
    assert payload["total"] == 4 and payload["omitted"] == 0 and payload["verify_stage"] == VERIFY
    assert payload["stages"][STAGES.index("검증 순서·예산")]["removed"] == 1
    assert payload["stages"][STAGES.index("경로 검증")]["removed"] == 2
    assert payload["stages"][-1]["remaining"] == 1
    assert payload["candidates"][0]["result_index"] == 0
    for stage in payload["stages"]:
        assert stage["entered"] == stage["removed"] + stage["remaining"]


def test_only_tried_and_top_estimated_candidates_are_sent():
    cands = [candidate(str(i)) for i in range(LIMIT + 20)]
    trace = trace_of(cands)
    for c in cands[:-1]:
        trace.reject(c, "검증 순서·예산", "예산")
    r = row([{"type": "BUS"}], 0, pareto=False)
    r["trace_id"] = trace.verified(cands[-1], r)
    payload = trace.finish([r])
    assert len(payload["candidates"]) == LIMIT + 1 and payload["omitted"] == 19
    assert payload["candidates"][0]["id"] == r["trace_id"]
    assert payload["stages"][0]["entered"] == LIMIT + 20


def test_verification_and_time_filters_record_distinct_stages():
    a, b = candidate("a"), candidate("b")
    trace = trace_of([a, b])
    trace.reject(a, "경로 검증", "조회 실패")
    trace.reject_id("b", "시간 절약 검사", "절약 부족")
    result = trace.finish([])
    assert result["stages"][STAGES.index("시간 절약 검사")]["removed"] == 1
    assert result["stages"][STAGES.index("경로 검증")]["removed"] == 1
    assert result["stages"][-1]["remaining"] == 0


def test_anchor_markers_identify_stops_not_origin_or_destination():
    first, middle, last = candidate("f", head="origin"), candidate("m"), candidate("l", tail="destination", tail_steps=())
    payload = trace_of([first, middle, last]).finish([])
    anchors = {c["id"]: [p["id"] for p in c["anchors"]] for c in payload["candidates"]}
    assert anchors == {"f": ["vf"], "m": ["um", "vm"], "l": ["ul"]}
    shapes = {c["id"]: c["shape"] for c in payload["candidates"]}
    assert shapes == {"f": "출발지 → 기준 경로", "m": "기준 경로 → 기준 경로", "l": "기준 경로 → 목적지"}


def test_estimated_and_verified_itineraries_are_kept_apart():
    ride = {"type": "BUS", "board_name": "vc", "alight_name": "신사역", "fare_routes": [{"name": "400"}]}
    c = candidate("c", head="origin", tail_steps=[ride])
    trace = trace_of([c])
    r = row([{"type": "BUS", "board_name": "vc", "alight_name": "논현역", "vehicles": [{"name": "140"}]}], 0)
    r["trace_id"] = trace.verified(c, r)
    record = trace.finish([r])["candidates"][0]
    assert record["itinerary"] == ["출발지", "출발지에서 vc까지 택시", "vc에서 400 버스 탑승", "신사역 하차", "도보", "목적지 도착"]
    assert "논현역 하차" in record["verified"]["itinerary"] and "신사역 하차" not in record["verified"]["itinerary"]


def test_middle_taxi_itinerary_has_correct_order():
    ride = {"type": "SUBWAY", "board_name": "출발역", "alight_name": "A", "line_group": "2호선"}
    tail = {"type": "BUS", "board_name": "B", "alight_name": "종점", "vehicles": [{"name": "100"}, {"name": "200"}]}
    parts = _itinerary([ride, {"type": "TAXI"}, tail], "A", "B")
    assert parts.index("A 하차") < parts.index("A에서 B까지 택시") < parts.index("B에서 100 / 200 버스 탑승")
    assert _itinerary([ride, {"type": "TAXI"}], "A", "목적지")[-2:] == ["A에서 목적지까지 택시", "목적지 도착"]


def test_geometry_connects_transit_stops_and_taxi_without_route_queries():
    origin, destination = [127, 37], [128, 38]
    board, alight = [127.1, 37.1], [127.5, 37.5]
    ride = {"type": "BUS", "stop_locs": [{"lon": p[0], "lat": p[1]} for p in [board, alight]]}
    segments = _geometry([{"type": "TAXI"}, ride], [origin, board], origin, destination)
    assert segments == [{"type": "TAXI", "path": [origin, board]},
                        {"type": "BUS", "path": [board, alight], "vehicles": []},
                        {"type": "WALKING", "path": [alight, destination]}]
    segments = _geometry([ride, {"type": "TAXI"}], [alight, destination], origin, destination)
    assert segments[0]["path"] == [origin, board]
    assert segments[-1] == {"type": "TAXI", "path": [alight, destination]}


def test_geometry_preserves_bus_types_and_uses_local_subway_color():
    path = [[127, 37], [128, 38]]
    bus = {"type": "BUS", "path": path, "fare_routes": [{"name": "140", "type": "간선"}]}
    rail = {"type": "SUBWAY", "path": path, "line_group": "수인분당선"}
    def color(group):
        assert group == "수인분당선"
        return "#FABE00"
    assert _geometry([bus], path, *path)[0]["vehicles"] == [{"name": "140", "type": "간선"}]
    assert _geometry([rail], path, *path, color)[0]["color"] == "#FABE00"
    rail["color"] = "#123456"
    assert _geometry([rail], path, *path, color)[0]["color"] == "#123456"
