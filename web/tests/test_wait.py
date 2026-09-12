"""배차간격으로 추정한 대기시간 (app.wait) — 손으로 쓴 구간 dict 만 쓴다."""
from datetime import datetime

import pytest

from app.headwaydb import HeadwayDB
from app.wait import add_wait, day_type, rail_hour

BUS = [
    {"route_id": "A", "day_type": "평일", "headway_min_m": "10", "headway_max_m": "20"},   # 중간 15분
    {"route_id": "A", "day_type": "토요일", "headway_min_m": "20", "headway_max_m": "40"},  # 중간 30분
    {"route_id": "B", "day_type": "평일", "headway_min_m": "10", "headway_max_m": "10"},
    {"route_id": "C", "day_type": "평일", "headway_min_m": "0", "headway_max_m": ""},       # 정보 없음
]
RAIL = [
    {"station_id": "S1", "line_group": "1호선", "direction": "상행", "hour": "8", "headway_m": "6"},
    {"station_id": "S1", "line_group": "1호선", "direction": "하행", "hour": "8", "headway_m": "4"},
    {"station_id": "S1", "line_group": "1호선", "direction": "상행", "hour": "9", "headway_m": "20"},
]
DB = HeadwayDB(BUS, RAIL)
MON_8 = datetime(2026, 9, 14, 8, 0)   # 월요일 8시
SAT_8 = datetime(2026, 9, 12, 8, 0)   # 토요일 8시


def bus_step(route_ids, time_s=600):
    return {"type": "BUS", "time_s": time_s, "route_ids": list(route_ids)}


def subway_step(station, group="1호선", time_s=600):
    return {"type": "SUBWAY", "time_s": time_s, "line_group": group,
            "resolution": {"board": {"chosen": {"id": station}}}}


def route(steps, total=None):
    return {"steps": steps, "total_time_s": total if total is not None else sum(s.get("time_s") or 0 for s in steps)}


def test_day_type_and_rail_hour():
    assert (day_type(MON_8), day_type(SAT_8), day_type(datetime(2026, 9, 13))) == ("평일", "토요일", "일요일")
    assert (rail_hour(datetime(2026, 9, 14, 0, 30)), rail_hour(datetime(2026, 9, 14, 1, 30))) == (24, 25)
    assert (rail_hour(datetime(2026, 9, 14, 3)), rail_hour(datetime(2026, 9, 14, 5))) == (None, 5)


def test_bus_wait_is_half_the_headway():
    r = route([bus_step(["A"])])
    basis = add_wait([r], DB, MON_8)
    assert basis == {"day_type": "평일", "hour": 8}
    assert (r["steps"][0]["headway_m"], r["steps"][0]["wait_s"]) == (15.0, 450)
    assert (r["wait_s"], r["total_with_wait_s"]) == (450, 1050)


def test_several_routes_in_one_step_add_frequencies():
    """10분·15분을 함께 기다리면 유효 배차 6분 — 한 노선만 볼 때(대기 5분)보다 짧다."""
    r = route([bus_step(["A", "B"])])
    add_wait([r], DB, MON_8)
    assert (r["steps"][0]["headway_m"], r["steps"][0]["wait_s"]) == (6.0, 180)


def test_unknown_route_is_left_out_but_others_count():
    r = route([bus_step(["B", "C"])])   # C 는 배차 정보 없음 → B 만으로 센다
    add_wait([r], DB, MON_8)
    assert r["steps"][0]["headway_m"] == 10.0


def test_weekend_falls_back_to_weekday_when_missing():
    sat = route([bus_step(["A"])])
    add_wait([sat], DB, SAT_8)
    assert sat["steps"][0]["headway_m"] == 30.0   # 토요일 값이 있으면 그 값
    only_weekday = route([bus_step(["B"])])
    add_wait([only_weekday], DB, SAT_8)
    assert only_weekday["steps"][0]["headway_m"] == 10.0   # 토요일 행이 없으면 평일로


def test_subway_uses_station_group_and_hour_averaging_directions():
    r = route([subway_step("S1")])
    add_wait([r], DB, MON_8)
    assert (r["steps"][0]["headway_m"], r["steps"][0]["wait_s"]) == (5.0, 150)   # (6+4)/2


def test_later_step_is_looked_up_at_the_hour_it_is_boarded():
    """앞 구간을 타고 오면 9시가 된다 — 그 시간대 배차(20분)로 대기를 잡는다."""
    r = route([bus_step(["B"], time_s=3600), subway_step("S1")])
    add_wait([r], DB, MON_8)
    assert r["steps"][1]["headway_m"] == 20.0
    assert r["wait_s"] == 300 + 600


def test_missing_headway_leaves_route_total_empty():
    r = route([bus_step(["C"]), subway_step("S1")])
    add_wait([r], DB, MON_8)
    assert r["steps"][0]["wait_s"] is None
    assert r["steps"][1]["wait_s"] == 150   # 구간별 값은 그대로 준다
    assert (r["wait_s"], r["total_with_wait_s"]) == (None, None)


@pytest.mark.parametrize("step", [
    {"type": "SUBWAY", "time_s": 600, "line_group": "1호선", "resolution": {"board": {"chosen": None}}},
    {"type": "SUBWAY", "time_s": 600, "line_group": None, "resolution": {"board": {"chosen": {"id": "S1"}}}},
    {"type": "SUBWAY", "time_s": 600, "line_group": "1호선"},   # 매칭을 돌리지 않은 응답
])
def test_subway_without_matching_has_no_wait(step):
    r = route([step])
    add_wait([r], DB, MON_8)
    assert (r["steps"][0]["wait_s"], r["wait_s"]) == (None, None)


def test_walking_only_route_has_no_wait_but_is_complete():
    r = route([{"type": "WALKING", "time_s": 300}])
    add_wait([r], DB, MON_8)
    assert (r["wait_s"], r["total_with_wait_s"]) == (0, 300)


class FakeRoutes:
    """RoutesDB 자리 — 노선 이름만 준다."""

    def __init__(self, names):
        self.names = names

    def name_of(self, route_id):
        return self.names.get(route_id)


def test_same_name_candidates_count_as_one_route():
    """'5' 번 마을버스 후보가 둘 남은 것은 함께 기다릴 수 있는 두 노선이 아니다 — 평균 배차 하나로 본다."""
    db = HeadwayDB([{"route_id": "X1", "day_type": "평일", "headway_min_m": "10", "headway_max_m": "10"},
                    {"route_id": "X2", "day_type": "평일", "headway_min_m": "30", "headway_max_m": "30"},
                    {"route_id": "Y", "day_type": "평일", "headway_min_m": "20", "headway_max_m": "20"}], [])
    names = FakeRoutes({"X1": "5", "X2": "5", "Y": "7"})
    same = route([bus_step(["X1", "X2"])])
    add_wait([same], db, MON_8, names)
    assert same["steps"][0]["headway_m"] == 20.0            # (10 + 30) / 2 — 빈도 합(7.5분)이 아니다

    both = route([bus_step(["X1", "X2", "Y"])])
    add_wait([both], db, MON_8, names)
    assert both["steps"][0]["headway_m"] == 10.0            # 20분짜리 '5' 와 20분짜리 '7' 을 함께 기다린다
