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

    def by_id(self, kind, sid):
        return None   # 시도를 모른다 — 서울 밖으로 본다


def net():
    # C: w5 → t4. w5 는 t3 에서 약 200 m, s3 에서 300 m 넘게 떨어져 있다 — s3 에서 내려 걸어서는 바로 못 간다
    routes = RoutesDB(rows("A", ["s1", "s2", "s3"]) + rows("B", ["t3", "t4"]) + rows("C", ["w5", "t4"]))
    return reach.Network(Stops(), routes, LinesDB([]))


def ride(a, b):
    return anchors.bus_hop_s(haversine_m(*PTS[a], *PTS[b]), None, "일반")


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
    assert back[("bus", "s1")] == pytest.approx(w + ride("s1", "s2") + ride("s2", "s3") + walk + back[("bus", "t3")], rel=1e-6)
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


class FixedHeadway(reach._Waits):
    """노선별 배차(분)를 직접 준다."""

    def __init__(self, n, minutes):
        super().__init__(n, None, None, None)
        self.minutes = minutes

    def _bus_headway(self, rid):
        return self.minutes.get(rid)


def test_ride_wait_adds_lines_through_both_stops_but_not_a_slow_detour():
    # A · D 는 s1 → s2 를 곧장, E 는 w5 로 크게 돌아 s2 에 간다. F 는 s1 에서 타지만 s2 를 지나지 않는다
    routes = RoutesDB(rows("A", ["s1", "s2", "s3"]) + rows("D", ["s1", "s2"]) + rows("E", ["s1", "w5", "s2"])
                      + rows("F", ["s1", "t4"]))
    n = reach.Network(Stops(), routes, LinesDB([]))
    waits = FixedHeadway(n, {"A": 10, "D": 10, "E": 10, "F": 10})
    a = next(p for p, pat in enumerate(n.patterns) if pat[1] == "A")
    assert waits.ride_wait(a, 0, 1) == pytest.approx(10 * 60 / 2 / 2)   # A · D 둘 — 배차 5분 꼴
    assert waits.ride_wait(a, 0, 2) == pytest.approx(10 * 60 / 2)       # s3 까지는 A 뿐
    assert waits.wait(a, ("bus", "s1")) == pytest.approx(10 * 60 / 2)   # 내릴 곳을 모르면 노선 하나
    # 배차를 모르는 노선은 빼고, 아무도 모르면 기본 대기
    assert FixedHeadway(n, {"D": 10}).ride_wait(a, 0, 1) == pytest.approx(10 * 60 / 2)
    assert FixedHeadway(n, {}).ride_wait(a, 0, 1) == anchors.WAIT_UNKNOWN_S



def test_rail_hops_use_timetable_runs_and_loops_cross_the_seam():
    # 순환선 r1 → r2 → r3 (→ r1). r1 → r2 만 시각표 값이 있고 나머지는 거리 ÷ 노선군 속도
    pts = {"r1": (127.00, 37.50), "r2": (127.01, 37.50), "r3": (127.01, 37.51)}
    rows = [{"chain_id": "L2", "loop": "1", "line_group": "2호선", "seq": str(i), "station_id": sid, "phys_id": sid,
             "name": sid, "lat": pts[sid][1], "lon": pts[sid][0]} for i, sid in enumerate(pts, 1)]
    lines = LinesDB(rows, [{"line_group": "2호선", "from_phys": "r1", "to_phys": "r2", "run_s": "100"}])
    n = reach.Network(Stops(), RoutesDB([]), lines)
    tau, _ = reach.search(n, reach._Waits(n, None, None, None), {("subway", "r3"): 0.0}, limit_s=1e9, max_rides=1)
    board = anchors.WAIT_UNKNOWN_S + anchors.RAIL_STATION_S
    kmh = anchors.RAIL_KMH_BY_GROUP["2호선"]
    # r3 → r1 은 이음매를 넘는 한 칸(거리 ÷ 속도), r1 → r2 는 시각표 100초
    r3_r1 = haversine_m(*pts["r3"], *pts["r1"]) / 1000 / kmh * 3600
    assert tau[("subway", "r1")] == pytest.approx(board + r3_r1)
    assert tau[("subway", "r2")] == pytest.approx(min(board + r3_r1 + 100, board + haversine_m(*pts["r3"], *pts["r2"]) / 1000 / kmh * 3600))
