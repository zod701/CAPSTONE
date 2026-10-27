"""직행 노선 끝점 (`anchors.direct_ends`) — 인라인 합성 DB 만 쓴다.

좌표는 기준점에서 미터로 옮겨 배치한다 — 반경·거리 조건이 테스트에서 그대로 읽힌다.
"""
import math
from datetime import datetime

import pytest
from geoutil import EARTH_R, haversine_m

from app.headwaydb import HeadwayDB
from app.linesdb import LinesDB
from app.routesdb import RoutesDB
from app.stopsdb import StopsDB

from algo.anchors import RAIL_KMH, _chain_km, direct_ends, transit_fare


def move(pt, east_m, north_m):
    lon, lat = pt
    return (lon + math.degrees(east_m / (EARTH_R * math.cos(math.radians(lat)))),
            lat + math.degrees(north_m / EARTH_R))


O = (127.0, 37.3)
D = move(O, 0, 10000)          # 목적지 — 북쪽 10 km
P = move(O, 1500, 0)           # 출발지 근처 승차 후보 (택시 반경 안, 택시 최소 거리 1.1 km 밖)
P2 = move(O, 2000, 500)
Q = move(D, 200, 0)            # 목적지 근처 하차 후보 (400 m 반경 안)
QF = move(D, 1500, 0)          # 목적지에서 택시로 갈 만한 하차 후보 (택시 최소 거리 밖)
FAR = move(O, 20000, 0)        # 크게 돌아가는 지점
DAY = datetime(2026, 9, 11, 14, 0)     # 금요일 낮 = 평일
LINE_GROUPS = [{"line_group": "1호선", "color": "#123456", "kakao_names": ""}]


def bstop(key, pt, name=None, virtual="0", sgg="수원시"):
    lon, lat = pt
    return {"stop_key": key, "name": name or key, "name_norm": name or key, "name_key": name or key,
            "aliases": "", "lat": str(lat), "lon": str(lon), "sgg_nm": sgg, "is_virtual": virtual}


def sstop(sid, pt, group="1호선"):
    lon, lat = pt
    return {"station_id": sid, "name": sid, "name_norm": sid, "name_key": sid, "aliases": "",
            "line_raw": group, "line_code": "", "line_id": "", "line_group": group,
            "lat": str(lat), "lon": str(lon), "sgg_nm": "수원시", "is_transfer": "0", "data_date": ""}


def rrow(rid, seq, key, pt, name=None, rtype="일반", virtual="0"):
    lon, lat = pt
    return {"route_id": rid, "route_name": name or rid, "route_type": rtype, "route_type_src": "",
            "source": "gyeonggi", "seq": str(seq), "updown": "상행", "stop_key": key,
            "name": key, "lat": str(lat), "lon": str(lon), "in_db": "1", "is_virtual": virtual}


def lrow(cid, seq, sid, pt, group="1호선", phys=None, loop="0"):
    lon, lat = pt
    return {"chain_id": cid, "chain_name": cid, "line_group": group, "line_raw": group, "loop": loop,
            "seq": str(seq), "station_id": sid, "phys_id": phys or sid, "name": sid,
            "lat": str(lat), "lon": str(lon), "status": "db", "source": "comp", "src_no": "", "src_name": ""}


def hbus(rid, lo, hi, day="평일"):
    return {"route_id": rid, "route_name": rid, "source": "gyeonggi", "day_type": day,
            "headway_min_m": str(lo), "headway_max_m": str(hi),
            "up_first": "04:00", "up_last": "23:00", "down_first": "", "down_last": ""}


def run(bus=(), sub=(), routes=(), lines=(), hw=None, **kw):
    """기본은 택시에서 내려 타는 쪽(tail) — O 쪽 택시 반경에서 타고 D 근처에서 내린다."""
    kw.setdefault("at", DAY)
    kw.setdefault("side", "tail")
    return direct_ends(StopsDB(bus, sub, LINE_GROUPS), RoutesDB(routes), LinesDB(lines),
                       O, D, vot=300.0, headway=hw, **kw)


# --- 버스 ---

def test_direct_route_becomes_one_anchor():
    # O 근처 P 에서 타 D 근처 Q 에 내리는 노선 하나 → 앵커 하나
    cands, diag = run(bus=[bstop("p", P), bstop("q", Q)],
                      routes=[rrow("r1", 1, "p", P), rrow("r1", 2, "q", Q)])
    assert [(c["anchor"], c["kind"], c["ride_id"]) for c in cands] == [("p", "bus", "r1")]
    assert (diag["bus_pairs"], diag["ends"]) == (1, 1)


def test_reverse_direction_is_dropped():
    # 같은 노선이 Q → P 순서면 그 노선으로는 D 로 갈 수 없다
    cands, _ = run(bus=[bstop("p", P), bstop("q", Q)],
                   routes=[rrow("r1", 1, "q", Q), rrow("r1", 2, "p", P)])
    assert cands == []


def test_loop_route_takes_upstream_occurrence():
    # 회차해 P 를 두 번 지나는 노선 — 먼저 지나는 쪽에서 타야 Q 로 간다
    cands, _ = run(bus=[bstop("p", P), bstop("q", Q)],
                   routes=[rrow("r1", 1, "p", P), rrow("r1", 2, "q", Q), rrow("r1", 3, "p", P)])
    assert len(cands) == 1 and cands[0]["rides"][0]["n_stops"] == 1


def test_virtual_stop_is_not_an_anchor():
    # 미정차(통과 노드)는 타고 내릴 수 없다 — 순서표에서도 정류소 DB 공간질의에서도 빠진다
    cands, _ = run(bus=[bstop("p", P, virtual="1"), bstop("q", Q)],
                   routes=[rrow("r1", 1, "p", P, virtual="1"), rrow("r1", 2, "q", Q)])
    assert cands == []


def test_no_shared_route_means_no_candidate():
    # O 근처 노선과 D 근처 노선이 겹치지 않으면 후보가 없다
    cands, _ = run(bus=[bstop("p", P), bstop("q", Q)],
                   routes=[rrow("r1", 1, "p", P), rrow("r1", 2, "p2", P2),
                           rrow("r2", 1, "q", Q), rrow("r2", 2, "q", Q)])
    assert cands == []


def test_far_detour_ride_is_dropped():
    # 승차 구간이 직선 대비 크게 돌면 반대 방향 짝이다 (경기 순서표는 상·하행이 한 순번열)
    cands, diag = run(bus=[bstop("p", P), bstop("q", Q)],
                      routes=[rrow("r1", 1, "p", P), rrow("r1", 2, "far", FAR), rrow("r1", 3, "q", Q)])
    assert (cands, diag["dropped_detour"]) == ([], 1)


def test_anchor_is_deduped_across_routes():
    # 같은 P 에서 D 로 가는 노선이 둘이면 앵커는 하나, 노선은 둘 — 검증 콜은 앵커마다 하나다
    cands, _ = run(bus=[bstop("p", P), bstop("q", Q)],
                   routes=[rrow("r1", 1, "p", P), rrow("r1", 2, "q", Q),
                           rrow("r2", 1, "p", P), rrow("r2", 2, "q", Q)])
    assert len(cands) == 1 and len(cands[0]["rides"]) == 2


# --- 지하철 ---


@pytest.mark.parametrize("loop,i,j,path", [
    ("1", 0, 3, [0, 3]),
    ("1", 3, 0, [3, 0]),
    ("1", 1, 3, [1, 0, 3]),
    ("1", 3, 1, [3, 0, 1]),
    ("1", 0, 1, [0, 1]),
    ("1", 2, 2, [2]),
    ("0", 0, 3, [0, 1, 2, 3]),
    ("0", 3, 0, [3, 2, 1, 0]),
])
def test_chain_distance_respects_loop_boundary(loop, i, j, path):
    points = [O, move(O, 1000, 0), FAR, move(O, 0, 1000)]
    lines = LinesDB([lrow("c1", k, str(k), pt, loop=loop) for k, pt in enumerate(points)])
    expected = sum(haversine_m(*points[a], *points[b]) for a, b in zip(path, path[1:])) / 1000
    assert _chain_km(lines, "c1", i, j) == pytest.approx(expected)


@pytest.mark.parametrize("reverse", [False, True])
def test_loop_boundary_candidate_uses_short_ride_distance_and_time(reverse):
    rows = [lrow("c1", 1, "ps", P, loop="1"), lrow("c1", 2, "far", FAR, loop="1"),
            lrow("c1", 3, "qs", Q, loop="1")]
    if reverse:
        rows = [dict(r, seq=str(4 - int(r["seq"]))) for r in rows]
    cands, _ = run(sub=[sstop("ps", P), sstop("qs", Q)], lines=rows)
    assert len(cands) == 1
    expected_km = haversine_m(*P, *Q) / 1000
    assert cands[0]["ride_km"] == pytest.approx(expected_km)
    assert cands[0]["ride_s"] == pytest.approx(expected_km / RAIL_KMH * 3600)


def test_chain_written_backwards_still_yields_candidate():
    # 덩어리에는 방향이 없다 — D 쪽 역이 먼저 적혀 있어도 후보가 나와야 한다 (method.md §6.6 회귀)
    cands, _ = run(sub=[sstop("ps", P), sstop("qs", Q)],
                   lines=[lrow("c1", 1, "qs", Q), lrow("c1", 2, "ps", P)])
    assert [c["anchor"] for c in cands] == ["ps"]


def test_transfer_rows_fold_into_one_physical_station():
    # 환승역은 운영 노선별로 행이 여럿이다 — 같은 실체면 짝을 한 번만 만든다
    q2 = move(D, 210, 0)
    cands, diag = run(sub=[sstop("ps", P), sstop("qs1", Q), sstop("qs2", q2)],
                      lines=[lrow("c1", 1, "ps", P), lrow("c1", 2, "qs1", Q, phys="Q"),
                             lrow("c2", 1, "ps", P), lrow("c2", 2, "qs2", q2, phys="Q")],
                      phys={"ps": "PS", "qs1": "Q", "qs2": "Q"})
    assert (len(cands), diag["rail_pairs"]) == (1, 1)


# --- 대기 ---

def test_headways_of_different_routes_add_up():
    # 한 앵커에서 함께 기다리는 노선이 둘이면 빈도를 더한다 — 10분·15분이면 유효 6분
    cands, _ = run(bus=[bstop("p", P), bstop("q", Q)],
                   routes=[rrow("r1", 1, "p", P, name="11"), rrow("r1", 2, "q", Q, name="11"),
                           rrow("r2", 1, "p", P, name="22"), rrow("r2", 2, "q", Q, name="22")],
                   hw=HeadwayDB([hbus("r1", 10, 10), hbus("r2", 15, 15)]))
    assert cands[0]["headway_m"] == pytest.approx(6.0)


def test_same_named_routes_average_instead_of_adding():
    # 같은 번호의 후보가 여럿인 것은 식별 모호일 뿐이다 — 빈도를 더하면 같은 버스를 두 대로 센다 (R21)
    cands, _ = run(bus=[bstop("p", P), bstop("q", Q)],
                   routes=[rrow("r1", 1, "p", P, name="5"), rrow("r1", 2, "q", Q, name="5"),
                           rrow("r2", 1, "p", P, name="5"), rrow("r2", 2, "q", Q, name="5")],
                   hw=HeadwayDB([hbus("r1", 10, 10), hbus("r2", 20, 20)]))
    assert cands[0]["headway_m"] == pytest.approx(15.0)


def test_unknown_headway_keeps_candidate_alive():
    # 배차를 몰라도 후보는 남는다 — 대신 0 이 아닌 기본 대기를 매겨 아는 노선보다 유리해지지 않게 한다
    cands, _ = run(bus=[bstop("p", P), bstop("q", Q)],
                   routes=[rrow("r1", 1, "p", P), rrow("r1", 2, "q", Q)],
                   hw=HeadwayDB([]))
    assert len(cands) == 1 and cands[0]["wait_s"] is None and cands[0]["score_s"] > 0


# --- 첫·막차 ---

def test_route_past_last_bus_is_dropped():
    # 막차가 23:00 인 노선은 23:30 에 후보가 아니다
    args = dict(bus=[bstop("p", P), bstop("q", Q)],
                routes=[rrow("r1", 1, "p", P), rrow("r1", 2, "q", Q)],
                hours={"r1": {"평일": [(4 * 60, 23 * 60)]}})
    assert run(at=datetime(2026, 9, 11, 23, 30), **args)[0] == []
    assert len(run(at=DAY, **args)[0]) == 1


def test_night_route_crossing_midnight_runs_late():
    # 첫차가 막차보다 늦으면 자정을 넘긴 것이다 (04:30 → 01:00)
    args = dict(bus=[bstop("p", P), bstop("q", Q)],
                routes=[rrow("r1", 1, "p", P), rrow("r1", 2, "q", Q)],
                hours={"r1": {"평일": [(4 * 60 + 30, 60)]}})
    assert len(run(at=datetime(2026, 9, 12, 0, 30), **args)[0]) == 1
    assert run(at=datetime(2026, 9, 11, 3, 0), **args)[0] == []


# --- 대칭판: 걸어서 타는 쪽(head) — 하차 지점이 끝점 ---

PB = move(O, 300, 0)       # 출발지에서 걸어갈 수 있는 승차 후보
QB = move(D, 3000, 0)      # 목적지에서 택시로만 닿는 하차 후보


def test_symmetric_direction_anchors_on_the_alighting_stop():
    # 걸어서 타는 쪽은 하차 지점에서 택시를 탄다 — 끝점이 하차 지점이고 승차 지점은 따로 들고 간다
    cands, diag = run(bus=[bstop("pb", PB), bstop("qb", QB)],
                      routes=[rrow("r1", 1, "pb", PB), rrow("r1", 2, "qb", QB)], side="head")
    assert [(c["anchor"], c["board"]) for c in cands] == [("qb", "pb")]
    assert diag["side"] == "head"


def test_direction_decides_which_end_gets_the_taxi():
    # 같은 노선·같은 정류소라도 어느 쪽 끝에 택시를 두느냐로 성립 여부가 갈린다
    args = dict(bus=[bstop("pb", PB), bstop("qb", QB)],
                routes=[rrow("r1", 1, "pb", PB), rrow("r1", 2, "qb", QB)])
    assert run(side="tail", **args)[0] == []         # D 에서 3 km 떨어진 하차점은 도보권 밖이다
    assert len(run(side="head", **args)[0]) == 1     # 택시로는 닿는다


def test_head_wait_counts_only_routes_from_the_same_boarding_stop():
    # 한 하차 지점으로 오는 노선이라도 타는 곳이 다르면 함께 기다릴 수 없다 — 빈도를 더하지 않는다
    p2 = move(O, 300, 200)
    cands, _ = run(bus=[bstop("p1", PB), bstop("p2", p2), bstop("qb", QB)],
                   routes=[rrow("r1", 1, "p1", PB, name="11"), rrow("r1", 2, "qb", QB, name="11"),
                           rrow("r2", 1, "p2", p2, name="22"), rrow("r2", 2, "qb", QB, name="22")],
                   hw=HeadwayDB([hbus("r1", 10, 10), hbus("r2", 10, 10)]), side="head")
    assert len(cands) == 1 and cands[0]["headway_m"] == pytest.approx(10.0)


def test_head_wait_adds_frequencies_at_the_same_boarding_stop():
    # 같은 곳에서 타는 노선이 둘이면 걸어서 타는 쪽에서도 빈도를 더한다 — 10분·15분이면 6분
    cands, _ = run(bus=[bstop("pb", PB), bstop("qb", QB)],
                   routes=[rrow("r1", 1, "pb", PB, name="11"), rrow("r1", 2, "qb", QB, name="11"),
                           rrow("r2", 1, "pb", PB, name="22"), rrow("r2", 2, "qb", QB, name="22")],
                   hw=HeadwayDB([hbus("r1", 10, 10), hbus("r2", 15, 15)]), side="head")
    assert cands[0]["headway_m"] == pytest.approx(6.0)


# --- 대중교통 요금 축 ---

def test_transit_fare_by_route_type():
    # 공항·시외·광역은 일반 버스보다 몇 배 비싸다 — 순위용 근사값
    assert (transit_fare("공항"), transit_fare("광역"), transit_fare("일반"),
            transit_fare(None, kind="subway")) == (8000.0, 3200.0, 1650.0, 1550.0)


def test_cheap_route_type_wins_when_fare_dominates():
    # 공항버스가 더 빨라도 요금이 5배가 넘으면 7 km 구간에서는 일반 버스가 앞선다
    # (요금을 세지 않으면 시간만 짧은 공항버스가 상위를 차지한다 — 배곧 → 안산 중앙역 실측)
    qs = move(O, 300, 7000)     # D 에서 3 km — 택시로 닿는다
    cands, _ = run(bus=[bstop("pb", PB), bstop("qs", qs)],
                   routes=[rrow("air", 1, "pb", PB, rtype="공항"), rrow("air", 2, "qs", qs, rtype="공항"),
                           rrow("loc", 1, "pb", PB, rtype="일반"), rrow("loc", 2, "qs", qs, rtype="일반")],
                   side="head")
    assert len(cands) == 1 and cands[0]["ride_id"] == "loc"


# --- 환승 인정 시간 ---
from algo.fares import TRANSFER_WINDOW_NIGHT_S, TRANSFER_WINDOW_S, transfer_window_s   # noqa: E402


def test_transfer_window_depends_on_the_boarding_time():
    # 다음 차에 타는 시각 기준 — 21시부터 익일 7시 전까지 60분, 그 밖 30분. 시각을 모르면 짧은 쪽
    assert (transfer_window_s(datetime(2026, 9, 14, 20, 59)), transfer_window_s(datetime(2026, 9, 14, 21, 0)),
            transfer_window_s(datetime(2026, 9, 15, 6, 59)), transfer_window_s(datetime(2026, 9, 15, 7, 0)),
            transfer_window_s(None)) == (TRANSFER_WINDOW_S, TRANSFER_WINDOW_NIGHT_S, TRANSFER_WINDOW_NIGHT_S,
                                         TRANSFER_WINDOW_S, TRANSFER_WINDOW_S)


# --- 후보로 쓰지 않는 노선 유형 ---

def test_demand_responsive_bus_is_never_a_candidate():
    # 수요응답형(똑버스)은 호출로 구역 안을 달려 정해진 노선이 없다 — 순서표상 직행이어도 후보가 아니다
    args = dict(bus=[bstop("p", P), bstop("q", Q)])
    assert run(routes=[rrow("r1", 1, "p", P, rtype="수요응답"), rrow("r1", 2, "q", Q, rtype="수요응답")], **args)[0] == []
    assert len(run(routes=[rrow("r1", 1, "p", P, rtype="일반"), rrow("r1", 2, "q", Q, rtype="일반")], **args)[0]) == 1


def test_demand_responsive_bus_does_not_join_the_frequency_sum():
    # 같은 정류장에서 일반 버스와 똑버스가 함께 서도 똑버스는 함께 기다리는 노선으로 세지 않는다
    cands, _ = run(bus=[bstop("p", P), bstop("q", Q)],
                   routes=[rrow("r1", 1, "p", P, name="11"), rrow("r1", 2, "q", Q, name="11"),
                           rrow("drt", 1, "p", P, name="똑버스01", rtype="수요응답"),
                           rrow("drt", 2, "q", Q, name="똑버스01", rtype="수요응답")],
                   hw=HeadwayDB([hbus("r1", 20, 20), hbus("drt", 4, 4)]))
    assert [r["id"] for r in cands[0]["rides"]] == ["r1"] and cands[0]["headway_m"] == pytest.approx(20.0)
