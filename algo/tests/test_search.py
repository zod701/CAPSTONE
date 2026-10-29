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
    near = stop("n", 127.017, 37.3009)            # 출발지에서 약 100 m — 걷는 편이 빠르다
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
    """노선을 더 타서 u 에 닿는 앞부분 — 검증 때 O → u 대중교통을 부른다."""
    point = {"id": "u", "name": "u", "lon": U["lon"], "lat": U["lat"], "kind": "bus"}
    return search._part(point, [ride(O, U, 500)], 700.0, 1650.0, source="line", at=DAY,
                        query=search.key("transit", OP, (U["lon"], U["lat"])))


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


def test_propose_extends_a_kakao_part_by_one_more_ride():
    # 기준 경로를 타고 u 에서 내려, 카카오가 주지 않은 노선 L 로 한 번 갈아타 x 까지 — 같은 탐색에서 나온다
    from app.linesdb import LinesDB
    from app.routesdb import RoutesDB
    pts = {"u": (U["lon"], U["lat"]), "x": (127.03, 37.327)}

    class Near:
        keys = list(pts)

        def near(self, kind, lon, lat, r):
            return [(i, d) for i, k in enumerate(self.keys) if kind == "bus" and (d := search.haversine_m(lon, lat, *pts[k])) <= r]

        def row(self, kind, i):
            k = self.keys[i]
            return {"stop_key": k, "name": k, "lon": pts[k][0], "lat": pts[k][1]}

        def by_id(self, kind, sid):
            return None

    routes = RoutesDB([{"is_virtual": "0", "stop_key": k, "name": k, "lat": pts[k][1], "lon": pts[k][0], "route_id": "L",
                        "route_name": "L", "source": "gyeonggi", "route_type": "일반"} for k in ("u", "x")])
    cands, diag = search.propose(Near(), routes, LinesDB([]), OP, DP, [ROUTE], [ROUTE], vot=VOT, baseline_s=100000, at=DAY)
    head = next(c["head"] for c in cands if c["head"]["point"]["id"] == "x")
    assert head["source"] == "line" and head["query"] == search.key("transit", OP, pts["x"])
    assert head["steps"][0]["fare_routes"][0]["id"] == "11" and head["steps"][-1]["fare_routes"][0]["id"] == "L"
    ride_ux = anchors.bus_hop_s(search.haversine_m(*pts["u"], *pts["x"]), None, "일반")
    assert head["time_s"] == pytest.approx(720 + anchors.WAIT_UNKNOWN_S + ride_ux)   # 기준 경로 u 까지(응답) + 대기 + 차내
    assert diag["heads"]["line"] >= 1



# 택시 최대 2회 — 출발지에서 택시로 기준 경로 승차 지점까지 가서 그 경로를 타는 앞부분
FAR = {**ROUTE, "steps": [dict(ROUTE["steps"][0], time_s=1500)] + ROUTE["steps"][1:]}   # 출발지 → u 버스가 25분이나 걸린다


def test_taxi_heads_board_the_base_route_by_taxi_only_when_faster():
    heads = search.route_taxi_heads([FAR], OP, Stops(), sgg_o=None, vot=VOT, at=DAY)
    by = {h["point"]["id"]: h for h in heads}
    # v 는 u 에서 22번에, D 는 v 에서 33번에 올라타는 쪽이 빠르다 — 정류소마다 가장 나은 승차 지점 하나
    assert {k: h["taxi_from"]["id"] for k, h in by.items()} == {"v": "u", "D": "v"}
    v = by["v"]
    taxi_s = anchors.taxi_plan_s(anchors.taxi_s_for(search.haversine_m(*OP, U["lon"], U["lat"]), DAY), True)
    assert v["time_s"] == pytest.approx(taxi_s + 120 + 3600) and v["lower_s"] == pytest.approx(anchors.taxi_plan_s(0, True) + 3720)
    assert v["fare"] > search._part(V, v["steps"], 0, 1650, source="route", at=DAY)["fare"]   # 첫 택시 요금이 들어간다
    # 원래 경로가 그 승차 지점까지 더 빠르면 택시로 올라타지 않는다 — ROUTE 는 u 까지 12분이라 u 에서는 올라타지 않는다
    assert all(h["taxi_from"]["id"] != "u" for h in search.route_taxi_heads([ROUTE], OP, Stops(), sgg_o=None, vot=VOT, at=DAY))


def test_verify_calls_the_first_taxi_and_adds_its_time_and_fare():
    head = search.route_taxi_heads([FAR], OP, Stops(), sgg_o=None, vot=VOT, at=DAY)
    cands, _ = search.pair(head, [destination()], vot=VOT, baseline_s=100000, at=DAY)
    c, api = find(cands, "v", "D"), Api()
    first = search.key("car", OP, (U["lon"], U["lat"]))
    assert first in search.queries(c, OP, DP)
    r = asyncio.run(search.verify(api, c, OP, DP, VOT, DAY))
    assert sorted(api.calls) == sorted(search.queries(c, OP, DP))
    assert r["first_taxi_s"] == pytest.approx(anchors.taxi_plan_s(600, True))
    assert r["time_s"] == pytest.approx(r["first_taxi_s"] + 120 + 3600 + anchors.taxi_plan_s(600, False))
    assert r["fare"] == pytest.approx(9000 * 2 + r["fare_info"]["value"])
    assert r["transit_time_s"] == pytest.approx(120 + 3600)


def test_rest_takes_the_faster_of_taxi_and_rail():
    # 짧으면 택시 고정분보다 전철 연결이 짧고, 길면 전철이 거리당 빠르다 — 지금 계수에서는 전철 쪽이 하한이 된다
    for m in (500, 3000, 15000):
        rail = m / 1000 / search.REST_RAIL_KMH * 3600 + search.REST_RAIL_CONNECT_S
        assert search.rest_s(m, DAY) == pytest.approx(min(anchors.taxi_s_for(m, DAY), rail))
    assert search.rest_s(0, DAY) == pytest.approx(min(anchors.taxi_s_for(0, DAY), search.REST_RAIL_CONNECT_S))


def test_band_drops_only_candidates_clearly_behind_on_both_axes():
    def c(name, minutes, fare):
        return {"name": name, "time_s": minutes * 60.0, "fare": fare}
    cands = [c("fast", 30, 20000), c("cheap", 50, 8000), c("both_worse", 40, 21500),   # fast 보다 10분 느리고 1,500원 비싸다
             c("near", 32, 20500), c("slower_cheaper", 35, 19500)]
    kept, dropped = search._band(cands, 180, 1000)
    assert sorted(k["name"] for k in kept) == ["cheap", "fast", "near", "slower_cheaper"] and dropped == 1
    # 띠를 0 으로 두면 보통의 Pareto 지배 — near · slower_cheaper 도 fast 에 밀리지 않으면 남는다
    assert sorted(k["name"] for k in search._band(cands, 0, 0)[0]) == ["cheap", "fast", "slower_cheaper"]
