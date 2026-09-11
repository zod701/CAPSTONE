import json
import math
from pathlib import Path

import pytest

from app.resolver import _passes, _path_frame, resolve_endpoint, resolve_route, summarize
from app.stopsdb import StopsDB
from geoutil import EARTH_R, local_xy, point_polyline_m

TINY = Path(__file__).parent / "fixtures" / "tiny_db"
RADIUS = {"bus": 300.0, "subway": 1000.0}
GAP = {"bus": 10.0, "subway": 150.0}


@pytest.fixture(scope="module")
def db():
    return StopsDB.load(TINY, TINY)


def pos(db, kind, id_):
    col = "stop_key" if kind == "bus" else "station_id"
    r = next(db.row(kind, i) for i in range(db.counts[kind]) if db.row(kind, i)[col] == id_)
    return [r["lon"], r["lat"]]


def move(pt, east_m=0.0, north_m=0.0):
    lon, lat = pt
    return [lon + math.degrees(east_m / (EARTH_R * math.cos(math.radians(lat)))),
            lat + math.degrees(north_m / EARTH_R)]


def bus(db, name, anchor):
    return resolve_endpoint(db, "bus", "board", name, anchor, [], RADIUS["bus"], GAP["bus"])


def subway(db, name, anchor, vehicles):
    return resolve_endpoint(db, "subway", "board", name, anchor, vehicles, RADIUS["subway"], GAP["subway"])


def jeongja_mid(db):
    p, q = pos(db, "subway", "bundang-K222"), pos(db, "subway", "shinbundang-D12")
    return [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2]


# --- 버스 ---

def test_updown_pair_matched_when_anchor_near_one(db):
    e = bus(db, "분당구청", move(pos(db, "bus", "204000101"), north_m=-5))
    assert (e["status"], e["match_level"], e["chosen"]["id"]) == ("matched", "key", "204000101")
    assert e["error_m"] == pytest.approx(5.0, abs=0.2)
    assert e["second_gap_m"] == pytest.approx(30.0, abs=0.5)
    assert e["n_candidates"] == 3  # 상/하행 2 (key) + 분당구청입구 (prefix); 미정차 제외
    assert e["n_global_same_name"] == 2
    assert e["line_filter"] == "n/a"
    assert e["chosen"]["line_group"] is None and e["chosen"]["sgg_nm"] == "성남시"
    assert e["nearest_any"] is None and e["nearest_same_name_m"] is None


def test_updown_pair_equidistant_is_ambiguous(db):
    e = bus(db, "분당구청", move(pos(db, "bus", "204000101"), north_m=15))
    assert e["status"] == "ambiguous"
    assert e["second_gap_m"] == pytest.approx(0.0, abs=0.5)
    assert e["chosen"]["id"] in ("204000101", "204000102")


def test_virtual_stop_never_chosen(db):
    e = bus(db, "분당구청(미정차)", pos(db, "bus", "204000103"))  # 기준점이 미정차 노드 바로 위
    assert (e["status"], e["chosen"]["id"]) == ("matched", "204000101")
    assert e["error_m"] == pytest.approx(3.0, abs=0.2)


def test_key_level_beats_nearer_prefix(db):
    e = bus(db, "분당구청", pos(db, "bus", "204000104"))  # '분당구청입구' 바로 위
    assert (e["match_level"], e["chosen"]["id"]) == ("key", "204000101")
    assert e["error_m"] == pytest.approx(20.0, abs=0.2)


def test_truncated_name_matches_by_prefix(db):
    anchor = move(pos(db, "bus", "204000201"), east_m=10)  # DB 이름 '판교테크노' (절단)
    e = bus(db, "판교테크노밸리", anchor)
    assert (e["status"], e["match_level"], e["chosen"]["id"]) == ("matched", "prefix", "204000201")
    short = bus(db, "판교테", anchor)  # 4글자 미만 접두는 받지 않는다
    assert short["status"] == "unmatched" and short["chosen"] is None
    assert short["nearest_any"]["id"] == "204000201"


def test_alias_level(db):
    e = bus(db, "삼평동행정복지센터", move(pos(db, "bus", "204000301"), north_m=8))
    assert (e["status"], e["match_level"], e["chosen"]["id"]) == ("matched", "alias", "204000301")
    assert e["chosen"]["name"] == "삼평동주민센터"


def test_far_same_name_unmatched(db):
    e = bus(db, "분당구청", move(pos(db, "bus", "204000101"), north_m=-2000))
    assert e["status"] == "unmatched"
    assert e["chosen"] is None and e["match_level"] is None and e["n_candidates"] == 0
    assert e["nearest_same_name_m"] == pytest.approx(2000.0, abs=1.0)
    assert e["n_global_same_name"] == 2
    assert e["nearest_any"]["id"] == "204000401" and e["nearest_any"]["name"] == "야탑역"
    assert e["nearest_any"]["dist_m"] == pytest.approx(50.0, abs=0.5)


def test_empty_name_and_missing_anchor(db):
    a = pos(db, "bus", "204000101")
    for name in ("", None, "(중)"):
        e = bus(db, name, a)
        assert (e["status"], e["query_key"]) == ("unmatched", "")
        assert e["nearest_any"]["id"] == "204000101"
        assert e["nearest_same_name_m"] is None and e["n_global_same_name"] == 0
    e = bus(db, "분당구청", None)
    assert (e["status"], e["anchor"], e["chosen"]) == ("skipped", None, None)


# --- 지하철 ---

def test_subway_line_filter_applied(db):
    e = subway(db, "정자", jeongja_mid(db), ["신분당선"])
    assert (e["status"], e["line_filter"], e["n_candidates"]) == ("matched", "applied", 1)
    assert e["chosen"]["id"] == "shinbundang-D12" and e["chosen"]["line_group"] == "신분당선"
    assert subway(db, "정자", jeongja_mid(db), ["수인분당선"])["chosen"]["id"] == "bundang-K222"


def test_subway_same_station_rows_never_ambiguous(db):
    # 매핑 안 된 차량 이름 → 두 노선 행이 모두 후보지만 같은 역이다
    e = subway(db, "정자", jeongja_mid(db), ["자기부상철도"])
    assert (e["status"], e["line_filter"], e["n_candidates"]) == ("matched", "unmapped", 2)
    assert e["second_gap_m"] is None


def test_subway_line_mismatch_falls_back_to_all(db):
    e = subway(db, "정자", jeongja_mid(db), ["경강선"])
    assert (e["status"], e["line_filter"], e["n_candidates"]) == ("matched", "mismatch", 2)
    assert e["chosen"]["id"] in ("bundang-K222", "shinbundang-D12")


def test_subway_same_name_far_apart_is_ambiguous():
    base = [127.0, 37.5]
    rows = [{"station_id": sid, "name": "시청", "name_key": "시청", "aliases": "", "line_group": "1호선",
             "lon": p[0], "lat": p[1], "sgg_nm": "중구"}
            for sid, p in (("a-1", base), ("b-1", move(base, east_m=500)))]
    e = resolve_endpoint(StopsDB([], rows, []), "subway", "board", "시청", move(base, east_m=250),
                         [], 1000.0, 150.0)
    assert e["status"] == "ambiguous" and e["second_gap_m"] == pytest.approx(0.0, abs=0.5)


# --- 경로 ---

def step(type_, path, stops=(), vehicles=(), guidance_from=None, guidance_to=None):
    """transit.normalize_transit 출력(§6.5) 모양의 합성 구간."""
    stops = list(stops)
    return {"idx": 0, "type": type_, "distance_m": None, "time_s": None, "guidance": "",
            "vehicles": [{"type": t, "name": n} for t, n in vehicles], "stops": stops, "path": path,
            "board_name": stops[0] if stops else guidance_from,
            "alight_name": stops[-1] if len(stops) >= 2 else guidance_to,
            "guidance_from": guidance_from, "guidance_to": guidance_to}


def test_resolve_route(db):
    suwon, jeongja_bd = pos(db, "subway", "bundang-K245"), pos(db, "subway", "bundang-K222")
    jeongja_sb, pangyo = pos(db, "subway", "shinbundang-D12"), pos(db, "subway", "shinbundang-D13")
    route = {"idx": 0, "type": 2, "steps": [
        step("SUBWAY", [move(suwon, east_m=30), move(jeongja_bd, north_m=20)], ["수원", "미금", "정자"],
             [("급행", "수인분당선")], "수원", "정자"),
        step("WALKING", [move(jeongja_bd, north_m=20), jeongja_mid(db), move(jeongja_sb, east_m=-5)]),
        step("SUBWAY", [move(jeongja_sb, east_m=-5), move(pangyo, north_m=-15)], ["정자", "판교"],
             [("일반", "신분당선")], "정자", "판교(판교테크노밸리)"),
        # 경로 없는 버스 구간: 승차 기준점 = 앞 구간 끝점, 하차 기준점 없음(마지막 구간)
        step("BUS", [], ["판교테크노밸리", "분당구청"], [("간선", "380")], "판교역", "분당구청"),
    ]}
    entries = resolve_route(db, route, RADIUS, GAP)
    s0, s1, s2, s3 = route["steps"]
    assert [e["role"] for e in entries] == ["board", "alight"] * 3

    assert (s0["line_group"], s0["color"]) == ("수인분당선", "#F5A200")
    assert s0["resolution"]["board"]["chosen"]["id"] == "bundang-K245"
    assert s0["resolution"]["board"]["error_m"] == pytest.approx(30.0, abs=0.5)
    assert s0["resolution"]["alight"]["chosen"]["id"] == "bundang-K222"
    assert s0["resolution"]["alight"]["line_filter"] == "applied"

    assert (s1["resolution"], s1["line_group"], s1["color"]) == (None, None, None)

    assert (s2["line_group"], s2["color"]) == ("신분당선", "#D4003B")
    assert s2["resolution"]["board"]["chosen"]["id"] == "shinbundang-D12"
    alight = s2["resolution"]["alight"]
    assert (alight["query_name"], alight["chosen"]["id"]) == ("판교", "shinbundang-D13")
    assert alight["anchor"] == s2["path"][-1]
    assert alight["name_conflict"] is False  # '판교' == '판교(판교테크노밸리)' (키 기준)

    board = s3["resolution"]["board"]
    assert board["anchor"] == s2["path"][-1]
    assert (board["status"], board["match_level"], board["chosen"]["id"]) == ("matched", "prefix", "204000201")
    assert board["name_conflict"] is True  # stops[0] '판교테크노밸리' ≠ guidance '판교역'
    assert s3["resolution"]["alight"]["status"] == "skipped"
    assert s3["resolution"]["alight"]["name_conflict"] is False
    assert (s3["line_group"], s3["color"]) == (None, None)

    s = summarize(entries)
    assert (s["n"], s["matched"], s["skipped"], s["match_rate"]) == (6, 5, 1, 1.0)
    assert (s["by_kind"]["subway"]["n"], s["by_kind"]["bus"]["n"]) == (4, 2)
    json.dumps([route, s])  # API 가 그대로 돌려준다 (행의 set 등이 새면 실패)
    assert resolve_route(db, {"steps": []}, RADIUS, GAP) == []

    # 정류소 위치: 승·하차는 매칭 결과, DB 에 없는 '미금' 은 선 위 추정, 도보는 없음, 선 없는 구간은 매칭된 쪽만
    assert [x["id"] for x in s0["stop_locs"]] == ["bundang-K245", None, "bundang-K222"]
    assert [x["level"] for x in s0["stop_locs"]] == ["key", None, "key"]
    assert s1["stop_locs"] is None
    assert s3["stop_locs"] == [{"lon": pos(db, "bus", "204000201")[0], "lat": pos(db, "bus", "204000201")[1],
                                "id": "204000201", "level": "prefix"}, None]


# --- 정류소 위치 (지도 이름 표시용) ---

def along_m(path, loc):
    """선에 가장 가까운 곳까지 선을 따라간 거리 (되돌아오지 않는 선에서만 의미가 있다)."""
    xy, cum = _path_frame(path)
    return min(_passes(xy, cum, *local_xy(loc["lon"], loc["lat"], *path[0]), 1e9))[1]


def test_stop_locs_bus_levels_far_same_name_and_interpolation(db):
    path = [move(pos(db, "bus", "204000401"), east_m=10), move(pos(db, "bus", "204000101"), east_m=15),
            move(pos(db, "bus", "204000301"), east_m=10), move(pos(db, "bus", "204000201"), east_m=10)]
    stops = ["야탑역", "분당구청", "광화문", "삼평동행정복지센터", "없는정류장", "판교테크노밸리", "종점"]
    route = {"steps": [step("BUS", path, stops, [("간선", "380")])]}
    resolve_route(db, route, RADIUS, GAP)
    locs = route["steps"][0]["stop_locs"]
    assert len(locs) == len(stops)
    assert locs[0]["id"] == "204000401"                                   # 승차 = 매칭 결과
    assert locs[1]["id"] in ("204000101", "204000102") and locs[1]["level"] == "key"  # key 가 가까운 prefix(입구)를 이긴다
    assert (locs[3]["id"], locs[3]["level"]) == ("204000301", "alias")
    assert (locs[5]["id"], locs[5]["level"]) == ("204000201", "prefix")  # 중간 정류소도 절단 이름을 찾는다
    # 20 km 밖 같은 이름(광화문)은 후보가 아니다 → DB 에 없는 이름과 똑같이 선 위 추정
    for k in (2, 4, 6):
        assert (locs[k]["id"], locs[k]["level"]) == (None, None)
        assert point_polyline_m(locs[k]["lon"], locs[k]["lat"], path) < 0.5
    s = [along_m(path, x) for x in locs]
    assert s == sorted(s)
    assert s[2] == pytest.approx((s[1] + s[3]) / 2, abs=1.0)              # 순번 비율로 보간
    assert s[4] == pytest.approx((s[3] + s[5]) / 2, abs=1.0)
    assert [locs[6]["lon"], locs[6]["lat"]] == pytest.approx(path[-1])   # 못 찾은 하차 = 선 끝
    json.dumps(route)


def test_stop_locs_out_and_back_keeps_stop_order(db):
    # 분당구청을 지나 삼평동에서 되돌아와 다시 분당구청을 지나는 구간 — 같은 이름이 두 번 나온다.
    # 두 번째 분당구청이 첫 번째 통과 지점에 붙으면 그 뒤 추정 위치(없는정류장)가 선 한가운데로 끌려간다.
    gucheong = pos(db, "bus", "204000101")
    path = [move(pos(db, "bus", "204000401"), east_m=10), move(gucheong, east_m=15),
            move(pos(db, "bus", "204000301"), east_m=10), move(gucheong, east_m=-15),
            move(gucheong, east_m=-15, north_m=-1500)]
    stops = ["야탑역", "분당구청", "삼평동주민센터", "분당구청", "없는정류장", "종점"]
    route = {"steps": [step("BUS", path, stops, [("간선", "380")])]}
    resolve_route(db, route, RADIUS, GAP)
    locs = route["steps"][0]["stop_locs"]
    assert [x["level"] for x in locs] == ["key", "key", "key", "key", None, None]  # 종점은 DB 에 없어 선 끝
    assert locs[1]["id"] in ("204000101", "204000102") and locs[3]["id"] in ("204000101", "204000102")
    # 없는정류장은 두 번째 분당구청(돌아오는 길)과 선 끝 사이 — 되돌아오는 마지막 구간 위, 분당구청보다 남쪽
    assert point_polyline_m(locs[4]["lon"], locs[4]["lat"], path[3:]) < 0.5
    assert locs[4]["lat"] < gucheong[1] - 0.003


def test_stop_locs_subway_prefers_same_line(db):
    k222 = pos(db, "subway", "bundang-K222")  # 수인분당선 정자 — 선이 바로 위를 지나지만 노선이 다르다
    path = [pos(db, "subway", "shinbundang-D13"), k222, move(k222, north_m=-3000)]
    route = {"steps": [step("SUBWAY", path, ["판교", "정자", "미금"], [("일반", "신분당선")])]}
    resolve_route(db, route, RADIUS, GAP)
    locs = route["steps"][0]["stop_locs"]
    assert [x["id"] for x in locs] == ["shinbundang-D13", "shinbundang-D12", None]
    assert [locs[2]["lon"], locs[2]["lat"]] == pytest.approx(path[-1])


def test_stop_locs_memo_shares_work_not_objects(db):
    path = [move(pos(db, "bus", "204000401"), east_m=10), move(pos(db, "bus", "204000301"), east_m=10)]
    make = lambda: {"steps": [step("BUS", path, ["야탑역", "분당구청", "삼평동주민센터"], [("간선", "380")])]}
    memo = {}
    a, b = make(), make()
    resolve_route(db, a, RADIUS, GAP, memo=memo)
    resolve_route(db, b, RADIUS, GAP, memo=memo)
    la, lb = a["steps"][0]["stop_locs"], b["steps"][0]["stop_locs"]
    assert len(memo) == 1 and la == lb
    assert all(x is not y for x, y in zip(la, lb))  # 응답 안에서 같은 객체를 나눠 쓰지 않는다
    c = make()
    resolve_route(db, c, RADIUS, GAP)
    assert c["steps"][0]["stop_locs"] == la  # memo 없이도 같은 결과


# --- 요약 ---

def _e(kind, status, err=None, level=None):
    return {"kind": kind, "status": status, "error_m": err, "match_level": level}


def test_summarize_math():
    entries = [
        _e("bus", "matched", 10.0, "key"), _e("bus", "matched", 20.0, "key"),
        _e("bus", "ambiguous", 30.0, "alias"), _e("bus", "unmatched"),
        _e("subway", "matched", 40.0, "key"), _e("subway", "matched", 50.0, "prefix"),
        _e("subway", "skipped"),
    ]
    s = summarize(entries, ["우주선", "GTX-A", "우주선", None])
    assert (s["n"], s["matched"], s["ambiguous"], s["unmatched"], s["skipped"]) == (7, 4, 1, 1, 1)
    assert s["match_rate"] == pytest.approx(4 / 6)
    assert s["found_rate"] == pytest.approx(5 / 6)
    assert s["error_m"] == {"median": 30.0, "p90": 46.0, "max": 50.0}
    assert s["levels"] == {"key": 3, "alias": 1, "prefix": 1}
    assert s["unmapped_vehicle_names"] == ["GTX-A", "우주선"]

    b, m = s["by_kind"]["bus"], s["by_kind"]["subway"]
    assert "by_kind" not in b
    assert (b["n"], b["match_rate"], b["found_rate"]) == (4, 0.5, 0.75)
    assert b["error_m"] == {"median": 20.0, "p90": 28.0, "max": 30.0}
    assert (m["n"], m["skipped"], m["match_rate"]) == (3, 1, 1.0)
    assert m["error_m"] == {"median": 45.0, "p90": 49.0, "max": 50.0}


def test_summarize_empty():
    s = summarize([])
    assert (s["n"], s["match_rate"], s["found_rate"], s["error_m"]) == (0, None, None, None)
    assert s["levels"] == {"key": 0, "alias": 0, "prefix": 0}
    assert s["by_kind"]["bus"]["n"] == 0 and s["unmapped_vehicle_names"] == []
    only_skipped = summarize([_e("bus", "skipped")])
    assert only_skipped["match_rate"] is None and only_skipped["found_rate"] is None
