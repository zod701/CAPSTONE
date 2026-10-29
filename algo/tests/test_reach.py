"""노선 단위 탐색 (`algo.reach`) — 합성 노선 두 개와 갈아타기 도보 하나."""
import pytest
from geoutil import haversine_m

from algo import anchors, reach
from app.linesdb import LinesDB
from app.routesdb import RoutesDB

# A: s1 → s2 → s3 (동쪽으로), B: t3 → t4. s3 와 t3 은 약 110 m 떨어져 걸어서 갈아탄다
PTS = {"s1": (127.000, 37.30), "s2": (127.010, 37.30), "s3": (127.020, 37.30),
       "t3": (127.0212, 37.30), "t4": (127.040, 37.30), "w5": (127.0235, 37.30)}


def rows(rid, keys, rtype="일반"):
    return [{"is_virtual": "0", "stop_key": k, "name": k, "lat": PTS[k][1], "lon": PTS[k][0], "route_id": rid,
             "route_name": rid, "source": "gyeonggi", "route_type": rtype} for k in keys]


class Stops:
    """`StopsDB` 의 near · row 만 — 버스 정류장 다섯 곳."""

    def __init__(self):
        self.keys = list(PTS)

    def near(self, kind, lon, lat, r):
        if kind != "bus":
            return []
        return [(i, d) for i, k in enumerate(self.keys) if (d := haversine_m(lon, lat, *PTS[k])) <= r]

    def row(self, kind, i):
        return {"stop_key": self.keys[i]}


def net():
    # C: w5 → t4. w5 는 t3 에서 약 200 m, s3 에서 300 m 넘게 떨어져 있다 — s3 에서 내려 걸어서는 바로 못 간다
    routes = RoutesDB(rows("A", ["s1", "s2", "s3"]) + rows("B", ["t3", "t4"]) + rows("C", ["w5", "t4"]))
    return reach.Network(Stops(), routes, LinesDB([]))


def ride(a, b):
    return haversine_m(*PTS[a], *PTS[b]) / 1000 / anchors.BUS_KMH["일반"] * 3600


def test_forward_reaches_through_a_walked_transfer_within_the_ride_limit():
    n = net()
    waits = reach._Waits(n, None, None, None)
    w = anchors.WAIT_UNKNOWN_S
    one, _ = reach.search(n, waits, {("bus", "s1"): 60.0}, limit_s=1e9, max_rides=1)
    assert ("bus", "t4") not in one and one[("bus", "s3")] == pytest.approx(60 + w + ride("s1", "s2") + ride("s2", "s3"))
    two, par = reach.search(n, waits, {("bus", "s1"): 60.0}, limit_s=1e9, max_rides=2)
    walk = anchors.walk_s(haversine_m(*PTS["s3"], *PTS["t3"]))
    assert two[("bus", "t4")] == pytest.approx(one[("bus", "s3")] + walk + w + ride("t3", "t4"))
    start, path = reach.legs(n, par, ("bus", "t4"))
    assert start == ("bus", "s1") and [leg[0] for leg in path] == ["ride", "walk", "ride"]


def test_backward_gives_time_left_to_the_destination_and_respects_the_limit():
    n = net()
    waits = reach._Waits(n, None, None, None)
    w = anchors.WAIT_UNKNOWN_S
    back, par = reach.search(n, waits, {("bus", "t4"): 30.0}, limit_s=1e9, max_rides=2, backward=True)
    walk = anchors.walk_s(haversine_m(*PTS["s3"], *PTS["t3"]))
    assert back[("bus", "t3")] == pytest.approx(w + ride("t3", "t4") + 30)
    assert back[("bus", "s1")] == pytest.approx(w + ride("s1", "s3") + walk + back[("bus", "t3")], rel=1e-6)
    start, path = reach.legs(n, par, ("bus", "s1"), backward=True)
    assert start == ("bus", "t4") and [leg[0] for leg in path] == ["ride", "walk", "ride"]
    tight, _ = reach.search(n, waits, {("bus", "t4"): 30.0}, limit_s=back[("bus", "t3")], max_rides=2, backward=True)
    assert ("bus", "s1") not in tight


def test_one_walk_per_transfer_whatever_the_scan_order():
    n = net()
    tau, par = reach.search(n, reach._Waits(n, None, None, None), {("bus", "s1"): 60.0}, limit_s=1e9, max_rides=1)
    # s3 에서 내려 t3 까지 걷고, 거기서 다시 w5 까지 걸어 이어 가지는 않는다
    assert par[("bus", "t3")][0] == "walk" and ("bus", "w5") not in tau


def test_rest_bound_stops_spreading_where_the_far_end_cannot_be_reached_in_time():
    n = net()
    waits = reach._Waits(n, None, None, None)
    far = {("bus", "t4")}   # t4 에서는 끝까지 아무리 빨라도 상한을 넘는다고 두면 거기는 펼치지 않는다
    tau, _ = reach.search(n, waits, {("bus", "s1"): 60.0}, limit_s=1e9, max_rides=2,
                          rest=lambda node: 2e9 if node in far else 0.0)
    assert ("bus", "s3") in tau and ("bus", "t4") not in tau
