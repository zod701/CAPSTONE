"""하이브리드 웹 처리 (app.hybrid) — 후보 생성·검증 규칙은 algo/tests/test_search.py 가 보고, 여기서는
기준 경로 사본 · 화면에 줄 항목 · 실패 기록 · 5분 기준처럼 웹이 맡는 부분만 본다. 카카오 대신 가짜 호출을 쓴다.
"""
import asyncio
from types import SimpleNamespace

import pytest

from algo import anchors, search
from algo.lazy_pareto import QueryUnavailable
from app.errors import ApiError
from app.hybrid import _Live, _row, _same_card, static_copy

O = (127.0, 37.30)
D = (127.20, 37.40)
ANCHOR_PT = (127.10, 37.35)


def _at(pt, sid="stop-1"):
    return {"id": sid, "name": "정류장", "lon": pt[0], "lat": pt[1]}


def transit_route(board=ANCHOR_PT, alight=None, time_s=1200, fare=1500, wait_s=300):
    """승·하차가 접지된 대중교통 경로 하나 (compare.reconstruct 가 읽는 모양)."""
    return {"steps": [{"type": "BUS", "time_s": time_s, "wait_s": wait_s,
                       "resolution": {"board": {"chosen": _at(board)},
                                      "alight": {"chosen": _at(alight or D, "stop-2")}}}],
            "fare": {"value": fare}, "transfers": 0, "wait_s": wait_s}


def part(source, point, steps=()):
    return search._part(point, list(steps), 0.0, 0.0, source=source)


def test_row_puts_the_taxi_between_the_parts_and_names_its_ends():
    u, v = {"id": "u", "name": "타는곳", "lon": 127.05, "lat": 37.32}, {"id": "v", "name": "내리는곳", "lon": 127.1, "lat": 37.35}
    head, tail = [{"type": "BUS", "time_s": 600}], [{"type": "SUBWAY", "time_s": 900}, {"type": "WALKING", "time_s": 60}]
    cand = {"head": part("route", u, head), "tail": part("line", v, tail)}
    result = {"head_steps": head, "tail_steps": tail, "time_s": 2400, "fare": 12000, "fare_info": {"value": 1650},
              "transit_time_s": 1560, "taxi_s": 840, "car": {"path": [[0, 0], [1, 1]]}}
    row = _row(cand, result)
    assert row["taxi_at"] == 1 and row["transit"]["steps"] == head + tail
    assert (row["from"]["name"], row["to"]["name"], row["head"], row["tail"]) == ("타는곳", "내리는곳", "route", "line")
    assert row["taxi_counted_s"] == 840 and row["taxi"] is result["car"]


def test_row_leaves_origin_and_destination_ends_empty():
    cand = {"head": part("origin", {"id": "O", "name": "출발지", "lon": O[0], "lat": O[1]}),
            "tail": part("route", {"id": "v", "name": "내리는곳", "lon": 127.1, "lat": 37.35}, [{"type": "BUS"}])}
    row = _row(cand, {"head_steps": [], "tail_steps": [{"type": "BUS"}], "time_s": 1, "fare": 1, "fare_info": None,
                      "transit_time_s": 1, "taxi_s": 1, "car": {}})
    assert row["from"] is None and row["taxi_at"] == 0 and row["to"]["id"] == "v"



def test_row_puts_a_first_taxi_before_the_base_route_it_boards():
    b = {"id": "b", "name": "올라타는곳", "lon": 127.03, "lat": 37.31}
    u = {"id": "u", "name": "내리는곳", "lon": 127.1, "lat": 37.35}
    head = [{"type": "SUBWAY", "time_s": 1800}]
    cand = {"head": {**part("taxi_route", u, head), "taxi_from": b},
            "tail": part("destination", {"id": "D", "name": "목적지", "lon": D[0], "lat": D[1]})}
    first = {"path": [[0, 0], [1, 1]], "duration_s": 300}
    result = {"head_steps": head, "tail_steps": [], "time_s": 3000, "fare": 20000, "fare_info": None,
              "transit_time_s": 1800, "taxi_s": 690, "car": {"path": []}, "first_car": first, "first_taxi_s": 450}
    row = _row(cand, result)
    assert row["first_taxi"] == {"to": {"id": "b", "name": "올라타는곳", "lon": 127.03, "lat": 37.31},
                                 "taxi": first, "counted_s": 450}
    assert row["taxi_at"] == 1 and row["head"] == "taxi_route" and row["from"]["id"] == "u"
    assert _row({**cand, "head": part("route", u, head)}, {**result, "first_car": None})["first_taxi"] is None

def test_same_station_named_twice_makes_one_card():
    # 정류소 DB(탄현역)와 경로 응답(탄현)이 같은 역을 50 m 남짓 달리 준다 — 시간·요금이 같으면 한 카드다
    a = {"from": {"lon": 126.7620, "lat": 37.6940}, "to": None, "time_s": 1212, "fare": 8050}
    b = dict(a, **{"from": {"lon": 126.7625, "lat": 37.6943}, "time_s": 1190})
    assert _same_card(a, b, O, D)
    assert not _same_card(a, dict(b, fare=8100), O, D)
    assert not _same_card(a, dict(b, **{"from": {"lon": 126.77, "lat": 37.70}}), O, D)


def test_live_records_a_failed_query_and_reports_it_as_unknown():
    async def car(*args):
        raise ApiError("upstream_error", "카카오 응답 없음")

    live = _Live(SimpleNamespace(kakao=SimpleNamespace(car=car)), None, QueryUnavailable)
    with pytest.raises(QueryUnavailable):
        asyncio.run(live.car(O, D))
    assert live.failed == [{"query": "car", "code": "upstream_error", "message": "카카오 응답 없음"}]


def test_plan_excludes_candidate_without_five_minute_saving(monkeypatch):
    import app.hybrid as hybrid
    u = {"id": "u", "name": "u", "lon": ANCHOR_PT[0], "lat": ANCHOR_PT[1]}
    cand = {"id": "c0", "head": part("origin", {"id": "O", "name": "출발지", "lon": O[0], "lat": O[1]}),
            "tail": part("route", u, [{"type": "BUS"}]), "time_s": 1000, "fare": 9000, "lower_s": 0}
    monkeypatch.setattr(search, "propose", lambda *a, **kw: ([cand], {
        "heads": {}, "tails": {}, "pairs": {}, "line_head": {}, "line_tail": {}}))

    async def run(cands, known, api, o, d, **kw):
        result = {"head_steps": [], "tail_steps": [{"type": "BUS", "time_s": 900}], "time_s": 1100, "fare": 10500,
                  "fare_info": None, "transit_time_s": 900, "taxi_s": 200, "car": {"path": [[0, 0], [1, 1]]}}
        return {"c0": result}, SimpleNamespace(status={"c0": "infeasible"}, query_order=[], complete=True)

    monkeypatch.setattr(search, "run", run)
    st = SimpleNamespace(stops=SimpleNamespace(group_color=None), routes=object(), lines=object(), headway=None,
                         anchor_tables=SimpleNamespace(phys={}, hours={}))
    result = asyncio.run(hybrid.plan(st, None, O, D, base_routes=[transit_route(board=O, alight=D, time_s=1200, wait_s=0)]))
    assert result["routes"] == []
    assert result["diag"]["too_little_saving"] == 1


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


def test_static_copy_restores_default_for_unknown_static_wait():
    route = transit_route()
    route["steps"][0].update(static_wait_s=None, wait_s=500, wait_source="realtime")
    route.update(total_time_s=1000)
    back = static_copy([route])[0]
    assert (back["steps"][0]["wait_s"], back["wait_s"], back["total_with_wait_s"]) == (900, 900, 1900)


def test_realtime_replaces_default_and_reuse_restores_default_source():
    from app.wait import apply_wait_defaults
    route = transit_route(board=O, alight=ANCHOR_PT, wait_s=None)
    route["total_time_s"] = 1200
    route["steps"][0]["route_ids"] = ["r1"]
    apply_wait_defaults(route)
    live, _ = anchors.with_realtime_first_wait([route], O, {
        "stop-1": [{"route_id": "r1", "eta_s": 0}]})
    assert live[0]["steps"][0]["wait_source"] == "realtime"
    assert live[0]["wait_s"] == 0
    restored = static_copy(live)[0]
    assert restored["steps"][0]["wait_source"] == "default"
    assert restored["wait_s"] == 900
    assert restored["total_with_wait_s"] == 2100
    assert live[0]["wait_s"] == 0
