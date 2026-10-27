"""공개 성인 교통카드 요금의 경계값과 잘린 경로·택시 환승 계산."""
from datetime import datetime

import pytest

from algo.fares import estimate
from algo.anchors import prefix_steps, route_points

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


def test_bus_choices_price_the_whole_transfer_chain_without_mutation():
    step = bus(8, "gg")
    step["fare_routes"] += bus(8, "seoul", kind="지선", source="seoul")["fare_routes"]
    result = estimate([step, rail(8)], 9999, at=DAY)
    assert (result["min"], result["max"], result["value"]) == (1750, 1850, 1850)
    assert result["source"] == "rules_range"
    assert len(step["fare_routes"]) == 2


def test_same_tariff_choices_still_resolve_same_bus_reboarding():
    step = bus(1, "a")
    step["fare_routes"] += bus(1, "b")["fare_routes"]
    result = estimate([step, bus(1, "a")], 9999, at=DAY)
    assert (result["min"], result["max"]) == (1650, 3300)


def test_bus_choices_can_have_identical_final_fares():
    step = bus(1, "a", kind="마을", source="seoul")
    step["fare_routes"] += bus(1, "b", kind="지선", source="seoul")["fare_routes"]
    result = estimate([step, rail(1)], 9999, at=DAY)
    assert (result["min"], result["max"]) == (1550, 1550)


def test_unknown_choice_keeps_existing_fallback():
    step = bus(8)
    step["fare_routes"] += bus(8, "unknown", kind="공항")["fare_routes"]
    result = estimate([step, rail(8)], 4200, at=DAY)
    assert result["source"] == "fallback" and result["value"] == 4200
    assert "min" not in result and "max" not in result


def test_multiple_boarding_choices_and_candidate_range():
    first = bus(8, "a")
    first["fare_routes"] += bus(8, "b", kind="지선", source="seoul")["fare_routes"]
    second = bus(8, "c")
    second["fare_routes"] += bus(8, "d", kind="지선", source="seoul")["fare_routes"]
    result = estimate([first, {"type": "TAXI", "time_s": 600}, second], 4200, at=DAY)
    assert (result["min"], result["max"], result["value"]) == (1700, 1850, 1850)
    assert result["transfer_kept"] is True
    expired = estimate([first, {"type": "TAXI", "time_s": 1801}, second], 4200, at=DAY)
    assert (expired["min"], expired["max"]) == (3000, 3300)
    assert expired["transfer_kept"] is False


def test_midride_prefix_uses_fractional_distance_without_mutating_original():
    locs = [{"id": str(i), "name": str(i), "lon": 127 + i * .1, "lat": 37} for i in range(3)]
    step = {**bus(30), "stop_locs": locs, "stops": [str(i) for i in range(3)]}
    route = {"steps": [step]}
    point = route_points(route, (127, 37))[1]
    prefix = prefix_steps(route, point)
    assert prefix[0]["distance_m"] == pytest.approx(15000)
    assert value(prefix) == 1750
    assert step["distance_m"] == 30000 and len(step["stop_locs"]) == 3


def test_taxi_between_rides_does_not_charge_the_skipped_distance():
    assert estimate([bus(4), {"type": "TAXI", "time_s": 600}, bus(4, "b")], 4000, at=DAY)["value"] == 1650
