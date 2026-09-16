"""하이브리드 후보를 확인하는 분기 (app.hybrid._verify) — 카카오 대신 가짜 호출을 센다.

유형마다 드는 콜이 다르다. A(first-mile)와 B(last-mile)를 뒤집어 쓰거나, (b) 역추적이 낸 B 를
(a) 섭동이 낸 B 처럼 다루면 있지도 않은 앞구간을 기준 경로에서 찾게 된다 — 그 경계를 못으로 박아 둔다.
"""
import asyncio
from types import SimpleNamespace

import pytest

from algo import anchors, compare
from app.hybrid import _verify, static_copy

O = (127.0, 37.30)
D = (127.20, 37.40)
ANCHOR = {"lon": 127.10, "lat": 37.35}
ANCHOR_PT = (127.10, 37.35)


def car_raw(duration=600, taxi=9000):
    return {"routes": [{"result_code": 0,
                        "summary": {"distance": 5000, "duration": duration, "fare": {"taxi": taxi, "toll": 0}},
                        "sections": [{"roads": [{"vertexes": [127.0, 37.3, 127.1, 37.35]}]}]}]}


NOTHING = object()   # "경로가 하나도 없다" 와 "기본 경로" 를 가르는 표시


def _at(pt):
    return {"id": "stop-1", "name": "정류장", "lon": pt[0], "lat": pt[1]}


def transit_route(board=ANCHOR_PT, alight=None, time_s=1200, fare=1500, wait_s=300):
    """승·하차가 접지된 대중교통 경로 하나 (compare.reconstruct 가 읽는 모양).

    양 끝을 요청한 좌표에 맞춰 두면 첫·끝 도보가 0 이라 시간이 `승차 + 대기` 로 딱 떨어진다.
    """
    return {"steps": [{"type": "BUS", "time_s": time_s, "wait_s": wait_s,
                       "resolution": {"board": {"chosen": _at(board)},
                                      "alight": {"chosen": _at(alight or D)}}}],
            "fare": {"value": fare}, "transfers": 0, "wait_s": wait_s}


class Calls:
    """가짜 카카오 — 무엇을 몇 번 불렀는지만 센다."""

    def __init__(self, legs=NOTHING):
        self.car, self.transit = [], []
        self.legs = transit_route() if legs is NOTHING else legs

    async def car_call(self, sx, sy, ex, ey):
        self.car.append(((sx, sy), (ex, ey)))
        return car_raw()

    async def transit_call(self, st, sx, sy, ex, ey):
        self.transit.append(((sx, sy), (ex, ey)))
        return {"routes": [] if self.legs is None else [self.legs]}


def cand(hybrid, strategy, **extra):
    base = {"hybrid": hybrid, "strategy": strategy, "anchor": "stop-1", "name": "환승 정류장",
            "lat": ANCHOR["lat"], "lon": ANCHOR["lon"], "kind": "bus", "line_group": None,
            "sgg_nm": "성남시", "ride_name": "380", "headway_m": 10.0,
            "wait_s": 300, "ride_s": 900, "walk_s": 120, "transit_fare_cap": 1450}
    return {**base, **extra}


def run(c, calls, base_routes=()):
    st = SimpleNamespace(kakao=SimpleNamespace(car=calls.car_call))
    return asyncio.run(_verify(st, calls.transit_call, O, D, c, anchors.VOT["time"],
                               anchors, compare, list(base_routes)))


def test_first_mile_calls_car_to_anchor_and_transit_from_anchor():
    calls = Calls()
    row = run(cand("A", "b"), calls)
    assert calls.car == [(O, (ANCHOR["lon"], ANCHOR["lat"]))]          # 택시는 출발지 → 앵커
    assert calls.transit == [((ANCHOR["lon"], ANCHOR["lat"]), D)]      # 대중교통은 앵커 → 도착지
    # 택시 뒤에 차를 잡아야 하므로 연결 버퍼가 붙는다 (algo 와 한 벌인 규칙)
    assert row["time_s"] == pytest.approx(anchors.taxi_connect_s(600) + 1500)
    assert row["taxi_counted_s"] == pytest.approx(anchors.taxi_connect_s(600))   # 화면이 "연결 여유 포함" 으로 보인다
    assert row["fare"] == 9000 + 1500
    assert row["transit"] is not None


@pytest.mark.parametrize("strategy", ["a", "b"])
def test_last_mile_takes_taxi_from_anchor_to_destination(strategy):
    calls = Calls()
    row = run(cand("B", strategy), calls, base_routes=[transit_route()])
    assert calls.car == [((ANCHOR["lon"], ANCHOR["lat"]), D)]          # 택시는 앵커 → 도착지
    assert row["time_s"] > 0 and row["fare"] >= 9000


def test_backtrack_last_mile_asks_for_the_leg_to_the_anchor():
    """(b) 의 B 는 앞구간이 기준 경로에 없다 — 대중교통 1콜을 더 쓰고 그 경로를 그대로 화면에 준다."""
    calls = Calls(legs=transit_route(board=O, alight=ANCHOR_PT))
    row = run(cand("B", "b"), calls)
    assert calls.transit == [(O, (ANCHOR["lon"], ANCHOR["lat"]))]
    assert row["transit"] is calls.legs
    # 대중교통 + 택시(호출 대기 포함). 내려서 타는 쪽이라 연결 버퍼는 없다
    assert row["time_s"] == pytest.approx(1500 + anchors.taxi_plan_s(600, connect=False))
    assert row["taxi_counted_s"] == pytest.approx(anchors.taxi_plan_s(600, connect=False))
    assert row["fare"] == 9000 + 1500


def test_perturb_last_mile_reuses_the_base_route_without_a_call():
    """(a) 의 B 는 앞구간이 기준 경로에 있다 — 대중교통을 부르지 않고 앵커에서 자른다."""
    calls = Calls()
    base = transit_route()
    row = run(cand("B", "a"), calls, base_routes=[base])
    assert calls.transit == []
    assert row["transit"]["steps"] == base["steps"][:1]
    assert row["time_s"] == pytest.approx(900 + 300 + 120 + anchors.taxi_plan_s(600, connect=False))   # 승차 + 대기 + 도보 + 호출 대기 + 택시
    assert row["fare"] == 9000 + 1450                              # 기준 경로 요금이 상한


def test_no_leg_from_the_anchor_drops_the_candidate():
    calls = Calls(legs=None)   # 앵커에서 갈 수 있는 대중교통 경로가 없다
    assert run(cand("A", "b"), calls) is None


# --- D: 기준 경로 한가운데 공백 한 구간만 택시로 ---

X = {"id": "gx", "name": "타는곳", "lon": 127.05, "lat": 37.32}
Y = {"id": "gy", "name": "내리는곳", "lon": 127.15, "lat": 37.38}


def three_step_route():
    """버스 → 환승 도보 → 버스. 두 번째 버스의 승차가 X, 하차가 Y (대기 공백), 도보 앞뒤가 X·Y 인 경로도 만든다."""
    def ride(board, alight, t):
        return {"type": "BUS", "time_s": t, "wait_s": 300,
                "resolution": {"board": {"chosen": board}, "alight": {"chosen": alight}}}
    first = ride({"id": "o1", "lon": O[0], "lat": O[1]}, X, 600)
    walk = {"type": "WALKING", "time_s": 400}
    second = ride(Y, {"id": "d1", "lon": D[0], "lat": D[1]}, 900)
    return {"steps": [first, walk, second], "fare": {"value": 1500}, "transfers": 1, "wait_s": 600}


def gap_cand(gap, resume_ride, **extra):
    return cand("D", "a", anchor="gx>gy", name="타는곳→내리는곳", gap=gap, gap_s=1200,
                from_lon=X["lon"], from_lat=X["lat"], lon=Y["lon"], lat=Y["lat"],
                base_time_s=3000, resume_ride=resume_ride, pre_ride=True, link_s=60, board_offset_s=900, **extra)


@pytest.mark.parametrize("resume, taxi_s", [(True, None), (False, 600)])
def test_gap_replaces_one_leg_with_one_car_call(resume, taxi_s):
    calls = Calls()
    row = run(gap_cand("walk", resume), calls, base_routes=[three_step_route()])
    assert calls.car == [((X["lon"], X["lat"]), (Y["lon"], Y["lat"]))]   # 택시는 공백의 시작 → 끝
    assert calls.transit == []                                           # 나머지는 기준 경로에 있다
    # 기준 − 공백 + 택시(호출 대기 포함). 내린 뒤 다시 차를 타면 연결 버퍼도 씌운다 — 목적지까지면 버퍼 없음
    assert row["time_s"] == pytest.approx(3000 - 1200 + anchors.taxi_plan_s(600, connect=resume))
    # 요금은 응답 ETA 로 환승이 이어지는지 다시 판정한 값 (algo/cli 와 같다)
    transit, _ = anchors.transfer_fare(1450, True, resume, 60, anchors.taxi_connect_s(600))
    assert row["fare"] == pytest.approx(9000 + transit)
    assert row["anchor"]["from_lat"] == X["lat"] and row["gap"]["kind"] == "walk"


def test_gap_is_not_mistaken_for_perturb_last_mile():
    """D 도 strategy 가 "a" 다 — 유형으로 먼저 가르지 않으면 (a) 의 B 로 떨어져 택시를 앵커 → 도착지로 부른다."""
    calls = Calls()
    run(gap_cand("walk", True), calls, base_routes=[three_step_route()])
    assert calls.car[0][1] != D


def test_walk_gap_route_drops_only_the_transfer_walk():
    base = three_step_route()
    row = run(gap_cand("walk", True), Calls(), base_routes=[base])
    assert [st["type"] for st in row["transit"]["steps"]] == ["BUS", "BUS"]
    assert row["gap"]["at"] == 1   # 화면은 이 자리에 택시 구간을 끼운다 → 버스 · 택시 · 버스


def test_wait_gap_route_drops_only_that_ride():
    base = three_step_route()
    base["steps"][2]["resolution"]["board"]["chosen"] = X     # 두 번째 버스가 X 에서 타 Y 에서 내린다
    base["steps"][2]["resolution"]["alight"]["chosen"] = Y
    row = run(gap_cand("wait", False), Calls(), base_routes=[base])
    assert [st["type"] for st in row["transit"]["steps"]] == ["BUS", "WALKING"]
    assert row["gap"]["at"] == 2   # 버스 · 도보 · 택시


def test_gap_shows_the_route_it_was_built_from():
    """D 는 첫 대기를 실시간으로 바꾼 경로에서 나온다 — 화면의 구간 대기도 그 경로여야 총 시간과 맞는다."""
    static, live = three_step_route(), three_step_route()
    live["steps"][0]["wait_source"] = "realtime"
    st = SimpleNamespace(kakao=SimpleNamespace(car=Calls().car_call))
    row = asyncio.run(_verify(st, Calls().transit_call, O, D, gap_cand("walk", True), anchors.VOT["time"],
                              anchors, compare, [static], [live]))
    assert row["transit"]["steps"][0].get("wait_source") == "realtime"


def test_perturb_last_mile_shows_the_realtime_route():
    """(a) 의 B 는 걸어서 첫 정류장에 닿아 실시간 사본에서 나온다 — 화면의 앞구간도 그 사본에서 자른다."""
    static, live = transit_route(), transit_route()
    live["steps"][0]["wait_source"] = "realtime"
    st = SimpleNamespace(kakao=SimpleNamespace(car=Calls().car_call))
    row = asyncio.run(_verify(st, Calls().transit_call, O, D, cand("B", "a"), anchors.VOT["time"],
                              anchors, compare, [static], [live]))
    assert row["transit"]["steps"][0].get("wait_source") == "realtime"


# --- C: 기준 경로가 돌아가기 시작하는 정류장에서 내려 다시 찾는다 ---

def named_route():
    """D 테스트 경로에 정류장 이름을 채운 것 — C 는 algo 가 경로 위 정류장 시각을 이름과 함께 매긴다."""
    route = three_step_route()
    route["steps"][0]["resolution"]["board"]["chosen"]["name"] = "출발정류장"
    route["steps"][2]["resolution"]["alight"]["chosen"]["name"] = "도착정류장"
    return route


def detour_cand(via, **extra):
    base = dict(point_id="gx", point_name="타는곳", loop_s=900, from_lon=X["lon"], from_lat=X["lat"],
                head_s=900, transit_fare_cap=1450, pre_ride=True, via=via)
    if via == "taxi_to_d":
        base.update(anchor="gx>D", name="타는곳→목적지", lon=D[0], lat=D[1])
    else:
        base.update(anchor="gx>stop-1", name="타는곳→환승 정류장", link_s=200)
    base.update(extra)
    return cand("C", "c", **base)


def test_detour_taxi_to_destination_uses_one_car_call():
    """택시로 목적지까지 — 자동차 1콜. 시간 = 그 정류장까지(head_s) + 호출 대기 + 택시, 요금 = 택시 + 기준 요금 상한."""
    calls = Calls()
    row = run(detour_cand("taxi_to_d"), calls, base_routes=[named_route()])
    assert calls.car == [((X["lon"], X["lat"]), D)] and calls.transit == []
    assert row["time_s"] == pytest.approx(900 + anchors.taxi_plan_s(600, connect=False))
    assert row["fare"] == 9000 + 1450
    # 화면: 돌아가기 시작하는 정류장(첫 버스의 하차)까지 자른 경로 + 그 뒤 택시
    assert row["detour"]["via"] == "taxi_to_d" and row["detour"]["taxi_at"] == 1
    assert [st["type"] for st in row["transit"]["steps"]] == ["BUS"]


def test_detour_reanchor_calls_car_and_transit_and_judges_transfer():
    """곧장 가는 노선의 앵커로 — 자동차 1 + 대중교통 1콜. 택시 뒤에 차를 잡으므로 연결 버퍼, 환승은 응답 ETA 로 다시 판정."""
    calls = Calls()   # 앵커 → 도착지 대중교통 (승차 앵커, 하차 도착지)
    row = run(detour_cand("reanchor"), calls, base_routes=[named_route()])
    assert calls.car == [((X["lon"], X["lat"]), (ANCHOR["lon"], ANCHOR["lat"]))]
    assert calls.transit == [((ANCHOR["lon"], ANCHOR["lat"]), D)]
    plan = anchors.taxi_plan_s(600, connect=True)
    assert row["time_s"] == pytest.approx(900 + plan + 1500)
    _, kept = anchors.transfer_fare(1450, True, True, 200, plan)
    tail = 1500
    assert row["fare"] == pytest.approx(9000 + 1450 + (max(0.0, tail - anchors.TRANSFER_REBASE_FARE) if kept else tail))
    # 화면: 앞구간(돌아가기 전까지) + 다시 부른 경로, 택시는 그 사이
    assert row["detour"]["taxi_at"] == 1
    assert [st["type"] for st in row["transit"]["steps"]] == ["BUS", "BUS"]


def test_detour_cuts_a_ride_at_its_middle_stop():
    """돌아가기 시작하는 정류장이 승차 구간 중간이면 그 정류장에서 구간을 자른다 — 시간은 algo 가 정류장 시각을 잡은 거리 비율."""
    locs = [{"id": "s0", "name": "처음", "lon": 127.00, "lat": 37.30},
            {"id": "s1", "name": "중간", "lon": 127.05, "lat": 37.30},
            {"id": "s2", "name": "끝", "lon": 127.15, "lat": 37.30}]
    ride = {"type": "BUS", "time_s": 900, "wait_s": 0, "stops": [x["name"] for x in locs], "stop_locs": locs,
            "path": [[x["lon"], x["lat"]] for x in locs],
            "resolution": {"board": {"chosen": locs[0]}, "alight": {"chosen": locs[2]}}}
    route = {"steps": [ride], "fare": {"value": 1500}, "transfers": 0, "wait_s": 0}
    mid = next(p for p in anchors.route_points(route, O) if p["id"] == "s1")
    row = run(detour_cand("taxi_to_d", point_id="s1", point_name="중간", from_lon=127.05, from_lat=37.30,
                          head_s=mid["t"]), Calls(), base_routes=[route])
    cut = row["transit"]["steps"][0]
    assert cut["stops"] == ["처음", "중간"] and len(cut["path"]) == 2 and cut["alight_name"] == "중간"
    assert cut["time_s"] == pytest.approx(mid["t"])   # 900 × 거리 비율(1/3)


def test_static_copy_restores_the_scheduled_first_wait():
    """[경로 검색] 결과에 든 실시간 첫 대기를 배차 추정으로 되돌린다 — 원본은 그대로 두고 경로 대기 합도 다시 낸다."""
    live = transit_route()
    live["steps"][0].update(static_wait_s=300, wait_s=40, wait_source="realtime", walk_to_stop_s=120)
    live.update(wait_s=40, total_time_s=1000, total_with_wait_s=1040)
    back = static_copy([live])[0]
    step = back["steps"][0]
    assert step["wait_s"] == 300 and not {"wait_source", "static_wait_s", "walk_to_stop_s"} & set(step)
    assert (back["wait_s"], back["total_with_wait_s"]) == (300, 1300)
    assert live["steps"][0]["wait_s"] == 40 and live["wait_s"] == 40


def test_static_copy_keeps_unknown_waits_unknown():
    route = transit_route()
    route["steps"][0].update(static_wait_s=None, wait_s=500, wait_source="realtime")
    route.update(total_time_s=1000)
    back = static_copy([route])[0]
    assert (back["steps"][0]["wait_s"], back["wait_s"], back["total_with_wait_s"]) == (None, None, None)
