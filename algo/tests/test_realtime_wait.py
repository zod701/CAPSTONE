"""첫 승차 대기 — 실시간 도착 + 정류장까지 도보. 합성 항목만 쓴다."""
import math

import pytest
from geoutil import EARTH_R

from app.linesdb import LinesDB

from algo.anchors import (BUS_S_PER_STOP, GAP_MIN_S, RAIL_S_PER_STATION, arrival, arrival_s, propose_gap, propose_perturb,
                          realtime_first_wait, subway_toward, walk_s, with_realtime_first_wait)

O = (127.0, 37.3)


def move(pt, east_m, north_m):
    lon, lat = pt
    return (lon + math.degrees(east_m / (EARTH_R * math.cos(math.radians(lat)))),
            lat + math.degrees(north_m / EARTH_R))


def bus_step(route_ids=("r1",), headway_m=None, wait_s=300, board=None, alight=None):
    return {"type": "BUS", "time_s": 600, "route_ids": list(route_ids), "headway_m": headway_m, "wait_s": wait_s,
            "vehicles": [{"name": "11"}],
            "resolution": {"board": {"chosen": board}, "alight": {"chosen": alight}}}


def bus_item(route_id, eta_s=None, n_stops=None):
    return {"route_id": route_id, "eta_s": eta_s, "n_stops_ahead": n_stops}


def rail_item(headsign, eta_s=None, n_stops=None):
    return {"headsign": headsign, "eta_s": eta_s, "n_stops_ahead": n_stops}


# --- 도착까지 남은 시간 ---

def test_arrival_uses_prediction_then_stops_ahead():
    # 예측 초가 있으면 그대로, 없으면 남은 정류소·역 수 × 한 칸 시간, 둘 다 없으면 모름(운행종료 문구 등)
    assert arrival_s(bus_item("r1", eta_s=90), "bus") == 90.0
    assert arrival_s(bus_item("r1", n_stops=3), "bus") == 3 * BUS_S_PER_STOP
    assert arrival_s(rail_item("x", n_stops=2), "subway") == 2 * RAIL_S_PER_STATION
    assert arrival_s(bus_item("r1"), "bus") is None


def test_stops_ahead_is_aged_by_the_reception_lag():
    # '2역 전' 을 268초 전에 받았다면 열차는 지금 들어오고 있다 — 남은 수로 잡은 시간에서 수신 경과를 뺀다
    assert arrival_s(dict(rail_item("x", n_stops=2), age_s=100), "subway") == 2 * RAIL_S_PER_STATION - 100
    assert arrival_s(dict(rail_item("x", n_stops=2), age_s=2 * RAIL_S_PER_STATION), "subway") == 0.0
    # 빼서 음수면 이미 지나갔거나 들어오는 중이다 — 모름
    assert arrival_s(dict(rail_item("x", n_stops=2), age_s=300), "subway") is None


def test_prediction_is_not_aged_twice():
    # 예측 초는 파서가 이미 경과를 뺀 값이다 — 여기서 또 빼지 않는다
    assert arrival_s(dict(rail_item("x", eta_s=90), age_s=200), "subway") == 90.0


# --- 버스 ---

def test_bus_arriving_before_you_reach_the_stop_is_missed():
    # 정류장까지 5분을 걷는데 3분 뒤 오는 차는 못 탄다 — 12분 뒤 차까지 7분을 기다린다
    items = [bus_item("r1", eta_s=180), bus_item("r1", eta_s=720), bus_item("other", eta_s=400)]
    assert realtime_first_wait(bus_step(), items, 300) == (420, "realtime")


def test_bus_counts_only_routes_of_the_leg():
    # 다른 노선의 차는 이 구간에 태워 주지 않는다
    assert realtime_first_wait(bus_step(), [bus_item("other", eta_s=400)], 60) == (None, "no_match")


def test_bus_all_known_missed_rolls_forward_by_headway():
    # 알려진 차(2분 뒤)를 도보 5분 동안 놓치면 그 뒤 배차 10분짜리 다음 차 — 12분 뒤 도착, 대기 7분
    wait, source = realtime_first_wait(bus_step(headway_m=10), [bus_item("r1", eta_s=120)], 300)
    assert (wait, source) == (420, "realtime+headway")


def test_bus_all_missed_without_headway_is_unknown():
    assert realtime_first_wait(bus_step(), [bus_item("r1", eta_s=120)], 300) == (None, "missed_all")


def test_bus_with_no_prediction_at_all():
    # 노선은 맞는데 예측도 남은 정류소 수도 없다(운행종료·출발대기) — 실시간으로 매기지 않는다
    assert realtime_first_wait(bus_step(), [bus_item("r1")], 60) == (None, "no_prediction")


# --- 도시철도 ---

def rail_step():
    return {"type": "SUBWAY", "time_s": 600, "wait_s": 300, "headway_m": 6.0, "vehicles": [{"name": "수인분당선"}]}


def test_subway_counts_only_trains_toward_the_alighting_station():
    # 이매 쪽으로 가는 열차만 — 수내방면 열차는 지금 도착해도 반대 방향이다. 2역 전 열차를 도보 100초 뒤 탄다
    items = [rail_item("왕십리행 - 이매방면", n_stops=2), rail_item("인천행 - 수내방면", eta_s=0)]
    wait, source = realtime_first_wait(rail_step(), items, 100, toward={"이매"})
    assert (wait, source) == (2 * RAIL_S_PER_STATION - 100, "realtime_stops")


def test_subway_without_direction_is_not_guessed():
    # 방향을 모르면 어느 열차를 기다리는지 가를 수 없다 — 실시간으로 매기지 않는다
    assert realtime_first_wait(rail_step(), [rail_item("왕십리행 - 이매방면", eta_s=60)], 0) == (None, "direction_unknown")


def lrow(cid, seq, sid, name):
    return {"chain_id": cid, "chain_name": cid, "line_group": "수인분당선", "line_raw": "x", "loop": "0",
            "seq": str(seq), "station_id": sid, "phys_id": sid, "name": name,
            "lat": "37.3", "lon": "127.0", "status": "db", "source": "comp", "src_no": "", "src_name": ""}


def test_toward_is_the_neighbor_on_the_alighting_side_whatever_the_chain_order():
    # 덩어리 기재 방향과 무관하게, 하차역 쪽 이웃 역 이름이 방면이다 (실시간 역명표가 없으면 '역' 을 뗀 우리 이름)
    names = ["야탑역", "이매역", "서현역", "수내역"]
    forward = LinesDB([lrow("c1", k, "s%d" % k, n) for k, n in enumerate(names)])
    backward = LinesDB([lrow("c2", k, "s%d" % (3 - k), n) for k, n in enumerate(reversed(names))])
    assert subway_toward(forward, None, "s2", "s0") == {"이매"}
    assert subway_toward(backward, None, "s2", "s0") == {"이매"}
    assert subway_toward(forward, None, "s2", "s3") == {"수내"}
    assert subway_toward(forward, None, "s2", "x") == set()


# --- 경로에 반영 ---

def chosen(sid, pt):
    return {"id": sid, "name": sid, "lon": pt[0], "lat": pt[1], "sgg_nm": "수원시", "line_group": None}


def two_leg_route(first_wait=120):
    s1 = bus_step(wait_s=first_wait, board=chosen("p", move(O, 415, 0)), alight=chosen("m1", move(O, 0, 3000)))
    s2 = dict(bus_step(route_ids=("r2",), wait_s=120, board=chosen("m2", move(O, 0, 3300)),
                       alight=chosen("q", move(O, 0, 6000))), vehicles=[{"name": "22"}])
    return {"idx": 0, "total_time_s": 1200, "transfers": 1, "fare": {"value": 1500, "min": None, "max": None},
            "steps": [s1, {"type": "WALKING", "time_s": 120}, s2], "wait_s": first_wait + 120}


def test_route_copy_gets_realtime_first_wait_net_of_walking():
    # 정류장까지 걷는 시간을 빼고 대기를 매기고, 경로 대기 합도 다시 낸다. 원본 경로는 그대로다
    route = two_leg_route()
    walk = walk_s(415)
    items = {"p": [bus_item("r1", eta_s=walk + 600)]}
    (rt,), diag = with_realtime_first_wait([route], O, items)
    first = rt["steps"][0]
    assert (first["wait_s"], first["static_wait_s"], first["wait_source"]) == (600, 120, "realtime")
    assert rt["wait_s"] == 600 + 120 and diag == {"realtime": 1}
    assert (route["steps"][0]["wait_s"], route["wait_s"]) == (120, 240)


def test_stop_without_arrivals_keeps_the_static_wait():
    (rt,), diag = with_realtime_first_wait([two_leg_route()], O, {})
    assert rt["steps"][0]["wait_s"] == 120 and diag == {"no_arrivals": 1}


def test_realtime_wait_can_open_a_gap_the_static_wait_hid():
    # 정적 배차로는 2분 대기라 공백이 아니었는데, 실시간으로 보니 다음 차가 20분 뒤다 — 그 구간이 D 후보가 된다
    route = two_leg_route()
    assert not [c for c in propose_gap(O, move(O, 0, 6200), [route], vot=300.0)[0] if c["anchor"] == "p>m1"]
    walk = walk_s(415)
    (rt,), _ = with_realtime_first_wait([route], O, {"p": [bus_item("r1", eta_s=walk + 1200)]})
    assert rt["steps"][0]["wait_s"] >= GAP_MIN_S
    assert [c["anchor"] for c in propose_gap(O, move(O, 0, 6200), [rt], vot=300.0)[0]] == ["p>m1"]


def test_perturb_makes_only_the_requested_types():
    # A 와 B 는 넘길 경로가 달라 따로 부른다 — 요청한 유형만 나온다
    route, dest = two_leg_route(), move(O, 0, 6200)
    assert {c["hybrid"] for c in propose_perturb(O, dest, [route], vot=300.0, hybrids=("A",))[0]} == {"A"}
    assert {c["hybrid"] for c in propose_perturb(O, dest, [route], vot=300.0, hybrids=("B",))[0]} == {"B"}


def test_last_mile_uses_the_realtime_first_wait_like_the_baseline():
    # B 는 걸어서 첫 정류장에 닿는다 — 실시간 사본을 넘기면 첫 대기가 기준선과 같은 실시간 값(10분)이 된다
    route, dest = two_leg_route(), move(O, 0, 6200)
    (rt,), _ = with_realtime_first_wait([route], O, {"p": [bus_item("r1", eta_s=walk_s(415) + 600)]})
    static = next(c for c in propose_perturb(O, dest, [route], vot=300.0, hybrids=("B",))[0] if c["anchor"] == "m1")
    live = next(c for c in propose_perturb(O, dest, [rt], vot=300.0, hybrids=("B",))[0] if c["anchor"] == "m1")
    assert (static["wait_s"], live["wait_s"]) == (120, 600)


def test_arrival_tells_prediction_from_stops_estimate():
    # 예측 초와 남은 수 근사는 오차가 달라 근거를 함께 넘긴다
    assert arrival(bus_item("r1", eta_s=90), "bus") == (90.0, "prediction")
    assert arrival(bus_item("r1", n_stops=3), "bus") == (3 * BUS_S_PER_STOP, "stops")
    assert arrival(bus_item("r1"), "bus") == (None, None)


def test_wait_source_follows_the_bus_you_actually_catch():
    # 먼저 오는 차가 남은 수 근사면 realtime_stops, 예측 초면 realtime — 탈 차의 근거를 따른다
    stops_first = [bus_item("r1", n_stops=5), bus_item("r1", eta_s=900)]
    assert realtime_first_wait(bus_step(), stops_first, 60) == (5 * BUS_S_PER_STOP - 60, "realtime_stops")
    prediction_first = [bus_item("r1", eta_s=200), bus_item("r1", n_stops=10)]
    assert realtime_first_wait(bus_step(), prediction_first, 60) == (140, "realtime")


# --- 도시철도 남은 역 수: 지나올 구간의 실제 거리 ---
from algo.anchors import RAIL_KMH, subway_upstream_s   # noqa: E402


def lrow_at(cid, seq, sid, pt):
    return dict(lrow(cid, seq, sid, sid), lat=str(pt[1]), lon=str(pt[0]))


def rail_s(m):
    return m / 1000 / RAIL_KMH * 3600


def test_upstream_sums_real_segments_on_the_side_trains_come_from():
    # 역 간격 1 · 2 · 3 km 인 덩어리에서 s1 → s0 로 가면 열차는 s2 · s3 쪽에서 온다 — 1역 전 2 km, 2역 전 5 km
    pts = [O, move(O, 0, 1000), move(O, 0, 3000), move(O, 0, 6000)]
    forward = LinesDB([lrow_at("c1", k, "s%d" % k, p) for k, p in enumerate(pts)])
    up = subway_upstream_s(forward, "s1", "s0")
    assert up == pytest.approx([rail_s(2000), rail_s(5000)], rel=1e-3)
    # s1 → s3 로 가면 열차는 s0 쪽에서 온다 — 1역 전 1 km, 그 너머는 덩어리 끝이라 끊긴다
    assert subway_upstream_s(forward, "s1", "s3") == pytest.approx([rail_s(1000)], rel=1e-3)
    # 덩어리 기재 순서를 뒤집어도 같다 (덩어리에는 방향이 없다)
    backward = LinesDB([lrow_at("c2", k, "s%d" % (3 - k), p) for k, p in enumerate(reversed(pts))])
    assert subway_upstream_s(backward, "s1", "s0") == pytest.approx(up, rel=1e-3)


def test_arrival_uses_upstream_distance_when_given():
    # '2역 전' 이 지나올 구간이 5 km 면 역당 상수가 아니라 그 거리로 잡는다. 목록보다 먼 역은 상수로 떨어진다
    up = [rail_s(2000), rail_s(5000)]
    got, basis = arrival(dict(rail_item("x", n_stops=2), age_s=60), "subway", up)
    assert (basis, got) == ("stops", pytest.approx(rail_s(5000) - 60))
    assert arrival(rail_item("x", n_stops=3), "subway", up) == (3 * RAIL_S_PER_STATION, "stops")


def test_realtime_wait_passes_upstream_through():
    # 방면이 맞는 '2역 전' 열차를 지나올 거리로 잡고, 정류장까지 걷는 100초를 뺀다
    up = [rail_s(2000), rail_s(5000)]
    wait, source = realtime_first_wait(rail_step(), [rail_item("왕십리행 - 이매방면", n_stops=2)], 100,
                                       toward={"이매"}, upstream_s=up)
    assert (source, wait) == ("realtime_stops", pytest.approx(rail_s(5000) - 100))

