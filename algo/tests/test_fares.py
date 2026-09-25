"""공개 성인 교통카드 요금의 경계값과 잘린 경로·택시 환승 계산."""
from datetime import datetime

import pytest

from algo.fares import candidate_fare, estimate
from algo.anchors import prefix_steps, propose_perturb, route_points

DAY = datetime(2026, 9, 23, 12)


def bus(km, rid="a", kind="일반", source="gyeonggi", wait=0):
    return {"type": "BUS", "distance_m": km * 1000, "time_s": 600, "wait_s": wait,
            "fare_routes": [{"id": rid, "name": rid, "source": source, "type": kind}]}


def rail(km, line="2호선"):
    return {"type": "SUBWAY", "line_group": line, "distance_m": km * 1000, "time_s": 600, "wait_s": 0}


def value(steps, at=DAY):
    result = estimate(steps, 9999, at=at)
    assert result["source"] == "rules_estimate"
    return result["value"]


@pytest.mark.parametrize("km, expected", [(10, 1650), (10.001, 1750), (15, 1750), (40, 2250), (40.001, 2350), (90, 2350)])
def test_gyeonggi_general_distance_and_cap(km, expected):
    assert value([bus(km)]) == expected


@pytest.mark.parametrize("kind, base", [("간선", 1500), ("지선", 1500), ("순환", 1400), ("마을", 1200), ("광역", 3000)])
def test_seoul_single_bus_flat_fare(kind, base):
    assert value([bus(60, kind=kind, source="seoul")]) == base


@pytest.mark.parametrize("kind, expected", [("좌석", 2650), ("직행", 3200), ("광역", 3900), ("순환", 4150)])
def test_gyeonggi_express_is_not_always_flat(kind, expected):
    assert value([bus(80, kind=kind)]) == expected


@pytest.mark.parametrize("km, expected", [(10, 1550), (10.001, 1650), (50, 2350), (50.001, 2450), (58, 2450), (58.001, 2550)])
def test_rail_distance_thresholds(km, expected):
    assert value([rail(km)]) == expected


def test_transfer_accumulates_distance_instead_of_subtracting_base_from_tail():
    # 각각 8km이면 단독요금은 기본요금뿐이지만, 연결하면 16km여서 200원이 추가된다.
    assert value([bus(8), {"type": "TAXI", "time_s": 600}, bus(8, "b")]) == 1850
    assert value([bus(8, source="seoul", kind="간선"), rail(8)]) == 1750


def test_express_transfer_has_30km_allowance():
    assert value([bus(9), bus(24, "b", kind="직행")]) == 3300


def test_transfer_never_exceeds_sum_of_independent_fares():
    assert value([bus(100, source="seoul", kind="간선"), bus(100, "b", source="seoul", kind="간선")]) == 3000


def test_already_paid_distance_is_not_refunded_when_allowance_increases():
    assert value([bus(16), bus(1, "b", kind="직행")]) == 3400


@pytest.mark.parametrize("gap, expected", [(1800, 1850), (1801, 3300)])
def test_taxi_transfer_time_boundary(gap, expected):
    assert value([bus(8), {"type": "TAXI", "time_s": gap}, bus(8, "b")]) == expected


def test_wait_and_walk_count_but_taxi_distance_does_not():
    steps = [bus(8), {"type": "WALKING", "time_s": 300},
             {"type": "TAXI", "time_s": 1000, "distance_m": 999999}, bus(8, "b", wait=501)]
    assert value(steps) == 3300
    assert value(steps, datetime(2026, 9, 23, 22)) == 1850


def test_same_bus_and_sixth_boarding_pay_again():
    assert value([bus(2), bus(2)]) == 3300
    assert value([bus(1, str(i)) for i in range(6)]) == 3300


def test_subway_internal_transfer_is_one_ride_but_taxi_exit_is_not():
    assert value([rail(8), rail(8, "3호선")]) == 1750
    assert value([rail(8), {"type": "TAXI", "time_s": 600}, rail(8)]) == 3100


def test_early_discount_applies_only_to_first_boarding_and_uses_arrival_time():
    early = datetime(2026, 9, 23, 6)
    assert value([bus(8), bus(8, "b")], early) == 1650
    assert value([bus(1, kind="마을", source="seoul"), bus(1, "b", kind="간선", source="seoul")], early) == 1260
    assert estimate([rail(8)], 9999, at=datetime(2026, 9, 23, 6, 29), initial_walk_s=120)["value"] == 1550


@pytest.mark.parametrize("step", [rail(8, "신분당선"), rail(8, "GTX-A"), bus(8, kind="공항"),
                                  bus(8, kind="마을"), {"type": "BUS", "distance_m": 1000}])
def test_unknown_tariffs_are_explicit_fallbacks(step):
    result = estimate([step], 4200)
    assert result["value"] == 4200 and result["source"] == "fallback" and result["reason"]


def test_missing_distance_is_not_zero_and_empty_prefix_is_free():
    step = bus(8)
    step["distance_m"] = None
    assert estimate([step], 4000)["source"] == "fallback"
    assert value([]) == 0


def test_midride_prefix_uses_fractional_distance_without_mutating_original():
    locs = [{"id": str(i), "name": str(i), "lon": 127 + i * .1, "lat": 37} for i in range(3)]
    step = {**bus(30), "stop_locs": locs, "stops": [str(i) for i in range(3)]}
    route = {"steps": [step]}
    point = route_points(route, (127, 37))[1]
    prefix = prefix_steps(route, point)
    assert prefix[0]["distance_m"] == pytest.approx(15000)
    assert value(prefix) == 1750
    assert step["distance_m"] == 30000 and len(step["stop_locs"]) == 3


def test_candidate_verification_uses_actual_taxi_time_and_tail_wait():
    cand = {"hybrid": "C", "via": "reanchor", "transit_fare_cap": 2500,
            "fare_steps": [bus(8), {"type": "TAXI", "time_s": 0}]}
    tail = {"steps": [bus(8, "b", wait=200)], "fare": {"value": 1650}}
    assert candidate_fare(cand, 1600, tail=tail, at=DAY)["value"] == 1850
    assert candidate_fare(cand, 1601, tail=tail, at=DAY)["value"] == 3300


def test_zero_tail_fare_is_not_replaced_by_candidate_fare():
    cand = {"hybrid": "C", "via": "reanchor", "transit_fare_cap": 2500, "tail_fare": 5000}
    assert candidate_fare(cand, 600, tail={"steps": [], "fare": {"value": 0}})["value"] == 2500


def test_d_does_not_keep_the_removed_bus_fare_or_distance():
    cand = {"hybrid": "D", "transit_fare_cap": 4000,
            "fare_steps": [bus(4), {"type": "TAXI", "time_s": 0}, bus(4, "b")]}
    assert candidate_fare(cand, 600, at=DAY)["value"] == 1650


def test_perturb_b_prices_retained_prefix_instead_of_whole_route():
    first, second = bus(5), bus(30, "b")
    points = [{"id": str(i), "name": str(i), "lon": 127 + i * .03, "lat": 37} for i in range(3)]
    for i, step in enumerate((first, second)):
        step["resolution"] = {"board": {"chosen": points[i]}, "alight": {"chosen": points[i + 1]}}
    cands, _ = propose_perturb((127, 37), (127.12, 37), [{"steps": [first, second], "fare": {"value": 2150}}],
                                vot=500, at=DAY, hybrids=("B",))
    c = next(c for c in cands if c["step_index"] == 0)
    assert c["fare_info"]["value"] == 1650
