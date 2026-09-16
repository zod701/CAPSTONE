"""C 유형 — 경로가 돌아가기 시작하는 정류장에서 내려 경로를 다시 찾는다. 합성 경로·DB 만 쓴다."""
import math
from datetime import datetime

import pytest
from geoutil import EARTH_R

from app.linesdb import LinesDB
from app.routesdb import RoutesDB
from app.stopsdb import StopsDB

from algo.anchors import detour_points, merge_candidates, propose_detour, route_points, walk_s

O = (127.0, 37.3)
DAY = datetime(2026, 9, 11, 14, 0)
LINE_GROUPS = [{"line_group": "1호선", "color": "#123456", "kakao_names": ""}]


def move(pt, east_m, north_m):
    lon, lat = pt
    return (lon + math.degrees(east_m / (EARTH_R * math.cos(math.radians(lat)))),
            lat + math.degrees(north_m / EARTH_R))


D = move(O, 0, 10000)
NAMES = ["s0", "s1", "s2", "s3", "s4", "s5"]
# 북쪽으로 가다가 s2(목적지까지 5.0 km)에서 동쪽으로 꺾여 멀어지고(s3 6.7 km) s4(4.2 km)에서 다시 가까워지는 버스
LOOP = [move(O, 0, 500), move(O, 0, 3000), move(O, 0, 5000), move(O, 3000, 4000), move(O, 3000, 7000), move(O, 0, 9500)]


def loop_route(points=LOOP, wait=300, ride=3000):
    locs = [{"lon": p[0], "lat": p[1], "id": n, "level": "key"} for n, p in zip(NAMES, points)]
    ends = [{"id": NAMES[k], "name": NAMES[k], "lon": points[k][0], "lat": points[k][1], "sgg_nm": "수원시",
             "line_group": None} for k in (0, -1)]
    step = {"type": "BUS", "time_s": ride, "wait_s": wait, "distance_m": 15000, "vehicles": [{"name": "99"}],
            "stops": list(NAMES), "stop_locs": locs, "route_ids": ["r99"], "path": [],
            "resolution": {"board": {"chosen": ends[0]}, "alight": {"chosen": ends[1]}}}
    return {"idx": 0, "total_time_s": ride, "transfers": 0, "fare": {"value": 1500, "min": None, "max": None},
            "steps": [step], "wait_s": wait}


def test_points_split_the_ride_time_by_distance_between_stops():
    # 구간 시간은 정류장 사이 거리 비율로 나눈다 — 출발 → 첫 정류장 도보와 대기가 앞에 붙는다
    pts = route_points(loop_route(), O)
    seg = [2500, 2000, math.hypot(3000, 1000), 3000, math.hypot(3000, 2500)]
    start = walk_s(500) + 300
    assert [p["id"] for p in pts] == NAMES
    assert pts[0]["t"] == pytest.approx(start, rel=1e-3)
    assert pts[2]["t"] == pytest.approx(start + 3000 * 4500 / sum(seg), rel=1e-3)
    assert pts[-1]["last"] and not pts[-2]["last"]


def test_detour_starts_where_distance_to_destination_starts_growing():
    # 목적지까지 5.0 km 인 s2 에서 6.7 km 로 멀어졌다가 s4(4.2 km)에서 다시 가까워진다 — 돌아가는 비용은 s2 → s4
    (dp,) = detour_points(loop_route(), O, D)
    pts = route_points(loop_route(), O)
    assert dp["point"]["id"] == "s2"
    assert dp["rise_m"] == pytest.approx(math.hypot(3000, 6000) - 5000, rel=1e-2)
    assert dp["loop_s"] == pytest.approx(pts[4]["t"] - pts[2]["t"])


def test_small_wiggle_is_not_a_detour():
    # 150 m 남짓 멀어졌다 돌아오는 것은 도로를 따라가는 흔들림이다
    wiggle = [move(O, 0, 500), move(O, 0, 3000), move(O, 0, 5000), move(O, 700, 4900), move(O, 0, 7000), move(O, 0, 9500)]
    assert detour_points(loop_route(wiggle), O, D) == []


def bstop(key, pt):
    lon, lat = pt
    return {"stop_key": key, "name": key, "name_norm": key, "name_key": key, "aliases": "",
            "lat": str(lat), "lon": str(lon), "sgg_nm": "수원시", "is_virtual": "0"}


def rrow(rid, seq, key, pt):
    lon, lat = pt
    return {"route_id": rid, "route_name": rid, "route_type": "일반", "route_type_src": "", "source": "gyeonggi",
            "seq": str(seq), "updown": "상행", "stop_key": key, "name": key, "lat": str(lat), "lon": str(lon),
            "in_db": "1", "is_virtual": "0"}


def test_detour_point_searches_taxi_to_destination_and_direct_lines():
    # s2 에서 내려 (1) 택시로 목적지까지, (2) 2.1 km 떨어진 a 에서 목적지 앞 q 로 곧장 가는 노선을 찾는다
    a, q = move(O, 1500, 6500), move(D, 100, 0)
    stops = StopsDB([bstop("s2", LOOP[2]), bstop("a", a), bstop("q", q)], [], LINE_GROUPS)
    routes = RoutesDB([rrow("r1", 1, "a", a), rrow("r1", 2, "q", q)])
    cands, diag = propose_detour(stops, routes, LinesDB([]), O, D, [loop_route()], vot=300.0, at=DAY, t_max_s=1500)
    by_via = {c["via"]: c for c in cands}
    head = route_points(loop_route(), O)[2]["t"]
    assert set(by_via) == {"taxi_to_d", "reanchor"} and diag["points_used"] == 1
    assert (by_via["reanchor"]["anchor"], by_via["reanchor"]["hybrid"]) == ("s2>a", "C")
    assert by_via["reanchor"]["head_s"] == pytest.approx(head) and by_via["taxi_to_d"]["head_s"] == pytest.approx(head)
    assert by_via["reanchor"]["time_s"] > head


def test_no_detour_means_no_candidate():
    straight = [move(O, 0, 500), move(O, 0, 3000), move(O, 0, 5000), move(O, 0, 6000), move(O, 0, 8000), move(O, 0, 9500)]
    stops = StopsDB([bstop("s2", straight[2])], [], LINE_GROUPS)
    cands, diag = propose_detour(stops, RoutesDB([]), LinesDB([]), O, D, [loop_route(straight)], vot=300.0, at=DAY)
    assert (cands, diag["points_found"]) == ([], 0)


def test_merge_keeps_detour_candidates_with_different_pickups():
    # C 는 택시 양끝이 모두 앵커다 — 내리는 곳이 같아도 택시를 타는 정류장이 멀면 다른 후보다
    far = move(LOOP[2], 0, -2000)
    c1 = {"hybrid": "C", "anchor": "s2>a", "lon": D[0], "lat": D[1], "from_lon": LOOP[2][0], "from_lat": LOOP[2][1], "score_s": 1}
    c2 = dict(c1, anchor="x>a", from_lon=far[0], from_lat=far[1], score_s=2)
    assert [c["anchor"] for c in merge_candidates([c1, c2], top=5)] == ["s2>a", "x>a"]
