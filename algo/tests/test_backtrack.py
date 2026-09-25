"""앵커 후보 생성 (b) 직행 노선 역추적 — 인라인 합성 DB 만 쓴다.

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

from algo.anchors import RAIL_KMH, _chain_km, propose_backtrack, propose_perturb, taxi_fare, top_n, transit_fare


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
    kw.setdefault("at", DAY)
    return propose_backtrack(StopsDB(bus, sub, LINE_GROUPS), RoutesDB(routes), LinesDB(lines),
                             O, D, vot=300.0, headway=hw, **kw)


# --- 버스 ---

def test_direct_route_becomes_one_anchor():
    # O 근처 P 에서 타 D 근처 Q 에 내리는 노선 하나 → 앵커 하나
    cands, diag = run(bus=[bstop("p", P), bstop("q", Q)],
                      routes=[rrow("r1", 1, "p", P), rrow("r1", 2, "q", Q)])
    assert [(c["anchor"], c["kind"], c["ride_id"]) for c in cands] == [("p", "bus", "r1")]
    assert (diag["bus_pairs"], diag["anchors"]) == (1, 1)


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


# --- 상위 N ---

def test_top_n_spreads_across_routes():
    # 한 노선이 여러 정류소를 지나도 상위가 그 노선으로만 채워지지 않는다
    cands = [{"ride_id": "r1", "score_s": 1}, {"ride_id": "r1", "score_s": 2},
             {"ride_id": "r1", "score_s": 3}, {"ride_id": "r2", "score_s": 4}]
    assert [c["score_s"] for c in top_n(cands, 3, max_per_route=2)] == [1, 2, 4]


# --- (a) 기준 경로 섭동 ---

def chosen(sid, pt, sgg="수원시"):
    return {"id": sid, "name": sid, "lon": pt[0], "lat": pt[1], "sgg_nm": sgg, "line_group": None}


def ride_step(board, alight, time_s=600, vehicle="11", kind="BUS"):
    return {"idx": 0, "type": kind, "time_s": time_s, "distance_m": 1000, "vehicles": [{"name": vehicle}],
            "stops": [], "path": [], "resolution": {"board": {"chosen": board}, "alight": {"chosen": alight}}}


def base_route(steps, fare=1500):
    return {"idx": 0, "total_time_s": sum(s["time_s"] for s in steps), "transfers": 0,
            "fare": {"value": fare, "min": None, "max": None}, "steps": steps}


def test_perturb_makes_first_and_last_mile_candidates():
    # 한 승차 구간에서 앞을 택시로 바꾸면 A, 뒤를 바꾸면 B 가 나온다
    route = base_route([ride_step(chosen("p", P), chosen("q", QF))])
    cands, diag = propose_perturb(O, D, [route], vot=300.0, at=DAY)
    assert sorted(c["hybrid"] for c in cands) == ["A", "B"]
    assert {c["anchor"] for c in cands} == {"p", "q"} and diag["pairs"] == 2


@pytest.mark.parametrize("hybrid", ["A", "B"])
@pytest.mark.parametrize("waits,expected", [((0, 0), 0), ((120, 180), 300),
                                            ((None, 120), 1020), ((120, None), 1020)])
def test_perturb_wait_sum_uses_defaults_for_unknown_rides(hybrid, waits, expected):
    first = dict(ride_step(chosen("p", P), chosen("m", P2)), wait_s=waits[0])
    last = dict(ride_step(chosen("m", P2), chosen("q", QF)), wait_s=waits[1])
    route = base_route([first, walk_step(60), last])
    cands, _ = propose_perturb(O, D, [route], vot=300, hybrids=(hybrid,), at=DAY)
    edge = 0 if hybrid == "A" else 2
    candidate = next(c for c in cands if c["step_index"] == edge)
    assert candidate["wait_s"] == expected


@pytest.mark.parametrize("hybrid", ["A", "B"])
def test_perturb_zero_wait_is_better_than_one_second(hybrid):
    scores = []
    for wait in (0, 1):
        route = base_route([dict(ride_step(chosen("p", P), chosen("q", QF)), wait_s=wait)])
        candidates, _ = propose_perturb(O, D, [route], vot=300, hybrids=(hybrid,), at=DAY)
        candidate, = candidates
        scores.append(candidate["score_s"])
    assert scores[1] - scores[0] == pytest.approx(1)


def test_perturb_skips_anchor_beyond_taxi_reach():
    # 택시로 T_max 안에 닿지 않는 지점은 앵커가 아니다
    route = base_route([ride_step(chosen("far", FAR), chosen("q", QF))])
    cands, diag = propose_perturb(O, D, [route], vot=300.0, at=DAY)
    assert [c["hybrid"] for c in cands] == ["B"] and diag["too_far"] == 1


def test_perturb_needs_grounded_stop():
    # 접지가 승·하차를 정하지 못한 구간은 앵커를 만들 수 없다
    route = base_route([ride_step(None, None)])
    cands, diag = propose_perturb(O, D, [route], vot=300.0, at=DAY)
    assert (cands, diag["no_anchor"]) == ([], 2)


def test_perturb_carries_fare_cap():
    # 잘라낸 경로의 요금은 알 수 없다 — 기준 경로 요금을 상한으로 들고 간다
    route = base_route([ride_step(chosen("p", P), chosen("q", QF))], fare=2350)
    cands, _ = propose_perturb(O, D, [route], vot=300.0, at=DAY)
    assert {c["transit_fare_cap"] for c in cands} == {2350}


# --- 대칭판: B 유형 (걸어서 타고 하차 지점에서 택시) ---

PB = move(O, 300, 0)       # 출발지에서 걸어갈 수 있는 승차 후보
QB = move(D, 3000, 0)      # 목적지에서 택시로만 닿는 하차 후보


def test_symmetric_direction_anchors_on_the_alighting_stop():
    # B 는 걸어서 타고 하차 지점에서 택시로 마무리한다 — 앵커가 하차 지점이고 승차 지점은 따로 들고 간다
    cands, diag = run(bus=[bstop("pb", PB), bstop("qb", QB)],
                      routes=[rrow("r1", 1, "pb", PB), rrow("r1", 2, "qb", QB)], hybrid="B")
    assert [(c["anchor"], c["hybrid"], c["board"]) for c in cands] == [("qb", "B", "pb")]
    assert diag["hybrid"] == "B"


def test_direction_decides_which_end_gets_the_taxi():
    # 같은 노선·같은 정류소라도 어느 쪽 끝에 택시를 두느냐로 성립 여부가 갈린다
    args = dict(bus=[bstop("pb", PB), bstop("qb", QB)],
                routes=[rrow("r1", 1, "pb", PB), rrow("r1", 2, "qb", QB)])
    assert run(hybrid="A", **args)[0] == []          # D 에서 3 km 떨어진 하차점은 도보권 밖이다
    assert len(run(hybrid="B", **args)[0]) == 1      # 택시로는 닿는다


def test_b_wait_counts_only_routes_from_the_same_boarding_stop():
    # 한 하차 지점으로 오는 노선이라도 타는 곳이 다르면 함께 기다릴 수 없다 — 빈도를 더하지 않는다
    p2 = move(O, 300, 200)
    cands, _ = run(bus=[bstop("p1", PB), bstop("p2", p2), bstop("qb", QB)],
                   routes=[rrow("r1", 1, "p1", PB, name="11"), rrow("r1", 2, "qb", QB, name="11"),
                           rrow("r2", 1, "p2", p2, name="22"), rrow("r2", 2, "qb", QB, name="22")],
                   hw=HeadwayDB([hbus("r1", 10, 10), hbus("r2", 10, 10)]), hybrid="B")
    assert len(cands) == 1 and cands[0]["headway_m"] == pytest.approx(10.0)


def test_b_wait_adds_frequencies_at_the_same_boarding_stop():
    # 같은 곳에서 타는 노선이 둘이면 B 에서도 빈도를 더한다 — 10분·15분이면 6분
    cands, _ = run(bus=[bstop("pb", PB), bstop("qb", QB)],
                   routes=[rrow("r1", 1, "pb", PB, name="11"), rrow("r1", 2, "qb", QB, name="11"),
                           rrow("r2", 1, "pb", PB, name="22"), rrow("r2", 2, "qb", QB, name="22")],
                   hw=HeadwayDB([hbus("r1", 10, 10), hbus("r2", 15, 15)]), hybrid="B")
    assert cands[0]["headway_m"] == pytest.approx(6.0)


def test_b_fare_is_charged_from_the_anchor_city():
    # B 는 하차 지점에서 택시를 탄다 — 요금 유형·심야는 그곳 기준이다 (가평군 = 나형)
    got = run(bus=[bstop("pb", PB, sgg="수원시"), bstop("qb", QB, sgg="가평군")],
              routes=[rrow("r1", 1, "pb", PB), rrow("r1", 2, "qb", QB)], hybrid="B")[0][0]
    assert got["fare"] == pytest.approx(taxi_fare(got["taxi_km"], "가평군", "가평군", DAY))


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
                   hybrid="B")
    assert len(cands) == 1 and cands[0]["ride_id"] == "loc"


# --- (a) 의 D: 공백 메우기 ---
from algo.anchors import GAP_MIN_S, TAXI_BASE_S, TAXI_BUFFER_S, TAXI_PICKUP_S, propose_gap   # noqa: E402

M1, M2, M3 = move(O, 0, 3000), move(O, 1500, 3000), move(O, 0, 6000)   # 환승 도보 M1 → M2 는 1.5 km


def walk_step(time_s):
    return {"idx": 0, "type": "WALKING", "time_s": time_s, "distance_m": 300, "vehicles": [], "stops": [], "path": []}


def gap_route(wait1=120, walk_mid=120, wait2=120, wait3=120):
    """P →(11) M1 · 환승 도보 · M2 →(22) M3 · M3 →(33) Q 인 세 구간 경로 — 대기는 구간마다 따로 준다."""
    r1 = dict(ride_step(chosen("p", P), chosen("m1", M1), vehicle="11"), wait_s=wait1)
    r2 = dict(ride_step(chosen("m2", M2), chosen("m3", M3), vehicle="22"), wait_s=wait2)
    r3 = dict(ride_step(chosen("m3b", M3), chosen("q", Q), vehicle="33"), wait_s=wait3)
    for r in (r1, r2, r3):
        name = r["vehicles"][0]["name"]
        r["fare_routes"] = [{"id": name, "name": name, "source": "gyeonggi", "type": "일반"}]
    route = base_route([r1, walk_step(walk_mid), r2, r3])
    route["wait_s"] = sum(w for w in (wait1, wait2, wait3) if w is not None) if None not in (wait1, wait2, wait3) else None
    return route


def test_gap_floor_is_taxi_fixed_cost_plus_buffer():
    # 거리 0 인 택시도 호출 대기·고정 비용·연결 버퍼만큼은 든다 — 이보다 짧은 공백은 메워도 이득이 없다
    assert GAP_MIN_S == TAXI_PICKUP_S + TAXI_BASE_S + TAXI_BUFFER_S


def test_long_wait_in_the_middle_becomes_a_gap_candidate():
    # 가운데 구간(22)의 대기가 15분이면 그 정류장에서 택시로 하차 지점까지 간다 — 뒤에 탈 차(33)가 남아 있다
    cands, diag = propose_gap(O, D, [gap_route(wait2=900)], vot=300.0, at=DAY)
    assert [(c["anchor"], c["gap"], c["resume_ride"]) for c in cands] == [("m2>m3", "wait", True)]
    assert cands[0]["gap_s"] == 900 + 600 and diag["gaps_wait"] == 1


def test_gap_preserves_zero_remaining_wait():
    candidates, _ = propose_gap(O, D, [gap_route(wait1=0, wait2=900, wait3=0)], vot=300, at=DAY)
    candidate, = candidates
    assert candidate["wait_s"] == 0


def test_short_gaps_make_no_candidate():
    # 대기·도보가 모두 짧으면 메울 공백이 없다
    cands, diag = propose_gap(O, D, [gap_route()], vot=300.0, at=DAY)
    assert (cands, diag["below_min"]) == ([], 4)


def test_long_transfer_walk_is_filled():
    # 1.5 km 환승 도보가 20분이면 앞 하차 지점에서 뒤 승차 지점까지 택시로 간다
    cands, _ = propose_gap(O, D, [gap_route(walk_mid=1200)], vot=300.0, at=DAY)
    assert [(c["anchor"], c["gap"]) for c in cands] == [("m1>m2", "walk")]


def test_last_leg_gap_does_not_need_a_connection_buffer():
    # 마지막 구간을 택시로 메우면 내려서 탈 차가 없다 — 검증에서 연결 버퍼를 씌우지 않는다
    # M3 → Q 는 직선 4 km(택시 약 16분) — 상한을 20분으로 명시하고 버퍼 판정만 본다
    cands, _ = propose_gap(O, D, [gap_route(wait3=900)], vot=300.0, at=DAY, t_max_s=20 * 60)
    assert [(c["anchor"], c["resume_ride"]) for c in cands] == [("m3b>q", False)]


def test_route_with_unknown_wait_is_skipped():
    # 대기를 모르는 구간이 있으면 기준 시간의 대기 합이 없어 무엇을 빼는지 정의되지 않는다
    cands, diag = propose_gap(O, D, [gap_route(wait1=None, wait2=900)], vot=300.0, at=DAY)
    assert (cands, diag["route_wait_unknown"]) == ([], 1)


def test_gap_time_is_baseline_minus_gap_plus_taxi():
    # 남은 구간(차내·대기·끝 도보)과 택시를 더하면 기준 시간에서 공백을 빼고 택시를 더한 값과 같다
    c = propose_gap(O, D, [gap_route(wait2=900)], vot=300.0, at=DAY)[0][0]
    assert c["ride_s"] + c["wait_s"] + c["walk_s"] == pytest.approx(c["base_time_s"] - c["gap_s"])


# --- 검증할 후보 고르기 ---
from algo.anchors import merge_candidates   # noqa: E402


def cand_at(hybrid, anchor, pt, score, frm=None):
    c = {"hybrid": hybrid, "anchor": anchor, "lon": pt[0], "lat": pt[1], "score_s": score}
    if frm is not None:
        c["from_lon"], c["from_lat"] = frm
    return c


def test_merge_folds_near_anchors_of_the_same_type():
    # 환승역의 노선별 두 id(50 m 떨어짐)는 같은 검증 쿼리다 — 점수가 낮은 쪽 하나만 남는다
    picks = merge_candidates([cand_at("A", "4호선-오이도", P, 10)], [cand_at("A", "수인-오이도", move(P, 50, 0), 12)], top=5)
    assert [c["anchor"] for c in picks] == ["4호선-오이도"]


def test_merge_keeps_different_types_at_the_same_spot():
    # 같은 정류소라도 A(택시로 타러 감)와 B(내려서 택시)는 다른 쿼리다
    picks = merge_candidates([cand_at("A", "s", P, 10)], [cand_at("B", "s", P, 12)], top=5)
    assert [c["hybrid"] for c in picks] == ["A", "B"]


def test_merge_compares_both_ends_for_gap_candidates():
    # D 는 택시 양끝이 모두 앵커다 — 도착점이 같아도 출발점이 멀면 다른 공백이라 둘 다 남는다
    far_from = move(M2, 0, -2000)
    picks = merge_candidates([cand_at("D", "m2>m3", M3, 10, frm=M2)],
                             [cand_at("D", "x>m3", move(M3, 30, 0), 12, frm=far_from),
                              cand_at("D", "m2b>m3b", move(M3, 30, 0), 14, frm=move(M2, 40, 0))], top=5)
    assert [c["anchor"] for c in picks] == ["m2>m3", "x>m3"]


# --- D: 환승 인정 시간 ---
from algo.anchors import taxi_connect_s   # noqa: E402
from algo.fares import TRANSFER_WINDOW_NIGHT_S, TRANSFER_WINDOW_S, transfer_window_s   # noqa: E402


def test_transfer_window_depends_on_the_boarding_time():
    # 다음 차에 타는 시각 기준 — 21시부터 익일 7시 전까지 60분, 그 밖 30분. 시각을 모르면 짧은 쪽
    assert (transfer_window_s(datetime(2026, 9, 14, 20, 59)), transfer_window_s(datetime(2026, 9, 14, 21, 0)),
            transfer_window_s(datetime(2026, 9, 15, 6, 59)), transfer_window_s(datetime(2026, 9, 15, 7, 0)),
            transfer_window_s(None)) == (TRANSFER_WINDOW_S, TRANSFER_WINDOW_NIGHT_S, TRANSFER_WINDOW_NIGHT_S,
                                         TRANSFER_WINDOW_S, TRANSFER_WINDOW_S)


def test_short_taxi_keeps_the_transfer():
    # 환승 도보 2분 + 택시(버퍼 포함) + 다음 차 대기 2분이 30분 안이면 요금은 기준 경로 그대로다
    c = propose_gap(O, D, [gap_route(wait2=900)], vot=300.0, at=DAY)[0][0]
    assert c["link_s"] == 120 + 120
    assert (c["transfer_kept"], c["transit_fare"]) == (True, 1650)
    assert c["link_s"] + taxi_connect_s(c["taxi_s"]) <= TRANSFER_WINDOW_S


def test_long_wait_for_the_next_ride_breaks_the_transfer_by_day_only():
    # 택시에서 내려 다음 차(33)를 15분 기다리면 낮에는 30분을 넘겨 기본요금을 새로 내지만, 밤에는 60분 안이다
    route = gap_route(wait2=900, wait3=900)
    by_day = next(c for c in propose_gap(O, D, [route], vot=300.0, at=DAY)[0] if c["anchor"] == "m2>m3")
    at_night = next(c for c in propose_gap(O, D, [route], vot=300.0, at=datetime(2026, 9, 14, 22, 0))[0]
                    if c["anchor"] == "m2>m3")
    assert (by_day["transfer_kept"], by_day["transit_fare"]) == (False, 3300)
    assert (at_night["transfer_kept"], at_night["transit_fare"]) == (True, 1650)


def test_first_leg_gap_has_no_transfer_to_keep():
    # 첫 구간을 메우면 앞에 탄 차가 없다
    cands, _ = propose_gap(O, D, [gap_route(wait1=900)], vot=300.0, at=DAY)
    c = next(c for c in cands if c["anchor"] == "p>m1")
    assert (c["transfer_kept"], c["transit_fare"]) == (None, 1650)


# --- 택시를 쓰는 최소 기준 ---
from algo.anchors import TAXI_MIN_M   # noqa: E402


def test_backtrack_skips_taxi_shorter_than_the_minimum_distance():
    # 출발지에서 800 m 떨어진 승차 지점까지 택시를 타지는 않는다 — 기본거리도 안 되는 거리다
    near = move(O, 800, 0)
    cands, diag = run(bus=[bstop("n", near), bstop("q", Q)],
                      routes=[rrow("r1", 1, "n", near), rrow("r1", 2, "q", Q)])
    assert (cands, diag["too_short"]) == ([], 1) and 800 < TAXI_MIN_M


def test_gap_skips_taxi_that_saves_too_little():
    # 공백은 하한을 넘었지만(8분 대기) 택시 구간을 빼면 5분도 못 줄인다 — 후보로 두지 않는다
    cands, diag = propose_gap(O, D, [gap_route(wait2=480)], vot=300.0, at=DAY)
    assert (cands, diag["saves_little"]) == ([], 1)


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
