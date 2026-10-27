"""택시 1회 경로 하나의 규칙 (`algo.search`) — 합성 경로와 가짜 응답만 쓴다."""
import asyncio
from datetime import datetime

import pytest

from algo import anchors, search

DAY = datetime(2026, 9, 11, 14, 0)
VOT = 300.0


def stop(sid, lon, lat, sgg=None):
    return {"id": sid, "name": sid, "lon": lon, "lat": lat, "sgg_nm": sgg}


def ride(a, b, time_s, wait=120, km=5, rid="11"):
    return {"type": "BUS", "time_s": time_s, "wait_s": wait, "distance_m": km * 1000,
            "fare_routes": [{"id": rid, "name": rid, "source": "gyeonggi", "type": "일반"}],
            "resolution": {"board": {"chosen": a}, "alight": {"chosen": b}}}


class Stops:
    def by_id(self, kind, sid):
        return None


# 출발지 → u 10분 · u → v 60분(대기 2분) · v → 목적지 10분. 대기가 짧아 '공백' 은 아니지만 긴 차내 구간을 택시로 건너뛸 수 있다
O, U, V, D = stop("O", 127.017, 37.3), stop("u", 127.017, 37.327), stop("v", 127, 37.354), stop("D", 127.002, 37.39)
ROUTE = {"fare": {"value": 1650}, "total_time_s": 4800, "wait_s": 360,
         "steps": [ride(O, U, 600), ride(U, V, 3600, rid="22"), ride(V, D, 600, rid="33")]}
OP, DP = (O["lon"], O["lat"]), (D["lon"], D["lat"])


def origin():
    return search._part({"id": "O", "name": "출발지", "lon": OP[0], "lat": OP[1]}, [], 0.0, 0.0, source="origin")


def destination():
    return search._part({"id": "D", "name": "목적지", "lon": DP[0], "lat": DP[1]}, [], 0.0, 0.0, source="destination")


def parts(route=ROUTE, tail_route=None):
    heads, _ = search.route_heads([route], OP, Stops(), vot=VOT, at=DAY)
    tails = search.route_tails([tail_route or route], DP, Stops(), vot=VOT, at=DAY)
    return [origin()] + heads, tails + [destination()]


def find(cands, u, v):
    return next(c for c in cands if c["head"]["point"]["id"] == u and c["tail"]["point"]["id"] == v)


def test_long_ride_in_the_middle_is_skipped_by_taxi_without_a_gap_threshold():
    heads, tails = parts()
    cands, _ = search.pair(heads, tails, vot=VOT, baseline_s=5160, at=DAY)
    c = find(cands, "u", "v")
    head_s, tail_s = 600 + 120, 600 + 120          # 출발·끝 도보 0 — 정류장이 출발지·목적지와 같은 자리다
    assert c["head"]["time_s"] == pytest.approx(head_s) and c["tail"]["time_s"] == pytest.approx(tail_s)
    assert c["lower_s"] == pytest.approx(head_s + anchors.taxi_plan_s(0, True) + tail_s)
    assert c["time_s"] == pytest.approx(head_s + anchors.taxi_plan_s(c["eta_s"], True) + tail_s)


def test_the_four_old_shapes_are_all_pairs_of_one_rule():
    heads, tails = parts()
    cands, _ = search.pair(heads, tails, vot=VOT, baseline_s=5160, at=DAY)
    shapes = {(c["head"]["source"], c["tail"]["source"]) for c in cands}
    assert {("origin", "route"), ("route", "destination"), ("route", "route")} <= shapes


def test_full_taxi_and_short_taxi_are_not_hybrids():
    near = stop("n", 127.017, 37.3065)            # 출발지에서 약 720 m
    route = {**ROUTE, "steps": [ride(O, near, 300), ride(near, D, 3000)]}
    heads, tails = parts(route)
    cands, diag = search.pair(heads, tails, vot=VOT, baseline_s=100000, at=DAY)
    assert not any(c["head"]["source"] == "origin" and c["tail"]["source"] == "destination" for c in cands)
    assert not any(c["head"]["source"] == "origin" and c["tail"]["point"]["id"] == "n" for c in cands)
    assert diag["too_close"] >= 1


def test_pairs_that_cannot_save_five_minutes_are_not_made():
    heads, tails = parts()
    cands, diag = search.pair(heads, tails, vot=VOT, baseline_s=1000, at=DAY)
    assert cands == [] and diag["no_saving"] > 0


def test_taxi_fare_is_charged_where_the_taxi_is_boarded():
    heads, tails = parts()
    heads = [dict(h, point=dict(h["point"], sgg_nm="가평군")) if h["point"]["id"] == "u" else h for h in heads]
    cands, _ = search.pair(heads, tails, vot=VOT, baseline_s=5160, at=DAY)
    c = find(cands, "u", "v")
    km = anchors.taxi_dist_km(U["lon"], U["lat"], V["lon"], V["lat"])
    assert c["taxi_fare"] == pytest.approx(anchors.taxi_fare(km, "가평군", None, DAY))


def test_heads_follow_the_walked_copy_and_tails_the_scheduled_route():
    # 앞부분은 걸어서 타는 실시간 사본(첫 대기 10분), 뒷부분은 택시로 닿아 타는 배차 추정 경로(첫 대기 2분)
    live = {**ROUTE, "steps": [dict(ROUTE["steps"][0], wait_s=600)] + ROUTE["steps"][1:]}
    heads, tails = parts(live, tail_route=ROUTE)
    assert next(h for h in heads if h["point"]["id"] == "u")["time_s"] == pytest.approx(600 + 600)
    assert next(t for t in tails if t["point"]["id"] == "O")["time_s"] == pytest.approx(4800 - 0 + 360)


class Api:
    """가짜 카카오 — 부른 질의를 센다."""

    def __init__(self, leg=None):
        self.calls, self.leg = [], leg

    async def car(self, a, b):
        self.calls.append(search.key("car", a, b))
        return {"duration_s": 600, "fare": {"taxi": 9000, "toll": 0}, "path": [list(a), list(b)]}

    async def transit(self, a, b):
        self.calls.append(search.key("transit", a, b))
        return {"routes": [self.leg] if self.leg else []}


def line_head():
    """출발지에서 걸어 타는 직행 노선으로 u 에 닿는 앞부분 — 검증 때 O → u 대중교통을 부른다."""
    end = {"anchor": "u", "name": "u", "lon": U["lon"], "lat": U["lat"], "kind": "bus", "sgg_nm": None,
           "line_group": None, "fare_step": ride(O, U, 500), "time_s": 700.0, "transit_fare": 1650.0,
           "walk_s": 0.0, "ride_name": "11", "headway_m": 10.0}
    return search.line_parts([end], OP, DP, "head", at=DAY)[0]


def test_verify_uses_one_car_call_for_parts_cut_from_the_base_route():
    heads, tails = parts()
    cands, _ = search.pair(heads, tails, vot=VOT, baseline_s=5160, at=DAY)
    c, api = find(cands, "u", "v"), Api()
    r = asyncio.run(search.verify(api, c, OP, DP, VOT, DAY))
    assert api.calls == list(search.queries(c, OP, DP)) == [search.key("car", (U["lon"], U["lat"]), (V["lon"], V["lat"]))]
    assert r["time_s"] == pytest.approx(720 + anchors.taxi_plan_s(600, True) + 720)
    assert r["fare"] == pytest.approx(9000 + r["fare_info"]["value"])
    assert [s["fare_routes"][0]["id"] for s in r["head_steps"] + r["tail_steps"]] == ["11", "33"]


def test_verify_requeries_a_line_part_and_uses_its_route():
    leg = {"fare": {"value": 1500}, "total_time_s": 900, "steps": [ride(O, U, 900, wait=60)]}
    tails = [t for t in parts()[1] if t["point"]["id"] == "v"]
    cands, _ = search.pair([line_head()], tails, vot=VOT, baseline_s=5160, at=DAY)
    c, api = cands[0], Api(leg)
    r = asyncio.run(search.verify(api, c, OP, DP, VOT, DAY))
    assert sorted(api.calls) == sorted(search.queries(c, OP, DP))
    assert search.key("transit", OP, (U["lon"], U["lat"])) in api.calls
    assert r["head_steps"] == leg["steps"] and r["transit_time_s"] == pytest.approx(900 + 60 + 720)
    assert c["lower_s"] == pytest.approx(anchors.taxi_plan_s(0, True) + 720)   # 다시 부르는 앞부분은 하한이 0


def test_verify_drops_a_line_part_without_a_route():
    tails = [t for t in parts()[1] if t["point"]["id"] == "v"]
    cands, _ = search.pair([line_head()], tails, vot=VOT, baseline_s=5160, at=DAY)
    assert asyncio.run(search.verify(Api(), cands[0], OP, DP, VOT, DAY)) is None


def test_run_stops_at_the_query_budget():
    heads, tails = parts()
    cands, _ = search.pair(heads, tails, vot=VOT, baseline_s=5160, at=DAY)
    api = Api()
    found, run = asyncio.run(search.run(cands, {"T0": search.Cost(5160, 1650)}, api, OP, DP, vot=VOT,
                                        baseline_s=5160, at=DAY, budget=2, parallel=3))
    assert len(api.calls) == len(run.query_order) <= 2 and found
