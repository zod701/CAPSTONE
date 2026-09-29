from copy import deepcopy

from algo.anchors import merge_candidates, top_n, _near
from app.hybrid_trace import HybridTrace, STAGES


def candidate(name, score, route="r", lon=127.1):
    return {"anchor": name, "name": name, "hybrid": "A", "lon": lon, "lat": 37.1,
            "score_s": score, "ride_id": route}


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


def test_selection_trace_records_real_reasons_without_changing_selection():
    candidates = [candidate("a", 100), candidate("b", 110, lon=127.2),
                  candidate("c", 120), candidate("d", 130, "other", lon=127.3),
                  candidate("e", 140, "last", lon=127.4)]
    before = deepcopy(candidates)
    trace = HybridTrace((127, 37), (128, 38), 500, {"time_s": 1000, "fare": 2000})
    trace.add(candidates, "test", {})
    picked = top_n(candidates, 3, trace=trace.reject)
    assert picked == top_n(candidates, 3)
    assert trace.records[id(candidates[2])]["stage"] == 1
    assert "노선" in trace.records[id(candidates[2])]["reason"]
    assert "예산" in trace.records[id(candidates[4])]["reason"]
    assert merge_candidates(picked, top=2, trace=trace.reject) == merge_candidates(picked, top=2)
    assert trace.records[id(candidates[3])]["stage"] == 2
    assert candidates == before


def test_merge_related_candidate_and_counts_are_consistent():
    a, b, c = candidate("a", 100), candidate("b", 110), candidate("c", 120, lon=127.3)
    trace = HybridTrace((127, 37), (128, 38), 500, {"time_s": 1000, "fare": 2000})
    trace.add([a, b, c], "test", {"dropped_service": 3})
    picks = merge_candidates([a, b, c], top=2, trace=trace.reject)
    assert picks == [a, c]
    assert trace.records[id(b)]["related"] == trace.records[id(a)]["id"]
    row = {"time_s": 400, "fare": 3000, "pareto": True}
    row["trace_id"] = trace.verified(a, row)
    trace.reject(c, "경로 검증", "API 실패")
    payload = trace.finish([row])
    assert payload["total"] == 3 and payload["omitted"] == 0
    assert payload["stages"][2]["removed"] == 1
    assert payload["stages"][3]["removed"] == 1
    assert payload["stages"][-1]["remaining"] == 1
    assert payload["candidates"][0]["result_index"] == 0
    for stage in payload["stages"]:
        assert stage["entered"] == stage["removed"] + stage["remaining"]
    assert len(payload["stages"]) == len(STAGES)


def test_map_limit_keeps_verified_results_and_total_counts():
    trace = HybridTrace((127, 37), (128, 38), 500, {"time_s": 1000, "fare": 2000})
    candidates = [candidate(str(i), i) for i in range(520)]
    trace.add(candidates, "test", {})
    for c in candidates[:-1]:
        trace.reject(c, "병합·검증 예산", "예산")
    row = {"time_s": 400, "fare": 3000, "pareto": False}
    row["trace_id"] = trace.verified(candidates[-1], row)
    payload = trace.finish([row])
    assert len(payload["candidates"]) == 520 and payload["omitted"] == 20
    assert payload["map_limit"] == 500
    assert payload["candidates"][0]["id"] == row["trace_id"]
    assert payload["stages"][0]["entered"] == 520


def test_duplicate_and_time_filters_record_distinct_stages():
    a, b = candidate("a", 100), candidate("b", 110)
    trace = HybridTrace((127, 37), (128, 38), 500, {})
    trace.add([a, b], "test", {})
    trace.reject(a, "중복 제외", "같은 경로")
    trace.reject(b, "시간 절약 검사", "절약 부족")
    result = trace.finish([])
    assert result["stages"][4]["removed"] == 1
    assert result["stages"][5]["removed"] == 1
    assert result["stages"][-1]["remaining"] == 0


def test_anchor_markers_identify_stops_not_origin_or_destination():
    trace = HybridTrace((127, 37), (128, 38), 500, {})
    a = candidate("stop-a", 100)
    b = dict(a, hybrid="B")
    d = dict(a, hybrid="D", anchor="x>y", name="X→Y", from_lon=127.2, from_lat=37.2)
    c = dict(d, hybrid="C", via="taxi_to_d", anchor="x>D", name="X→목적지", lon=128, lat=38)
    trace.add([a, b, d, c], "test", {})
    assert trace.records[id(a)]["anchors"] == trace.records[id(b)]["anchors"]
    assert trace.records[id(b)]["anchors"][0]["lon"] == a["lon"]
    assert [p["id"] for p in trace.records[id(d)]["anchors"]] == ["x", "y"]
    assert [p["id"] for p in trace.records[id(c)]["anchors"]] == ["x"]
