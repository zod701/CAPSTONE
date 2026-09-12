import json
import math
from pathlib import Path

import pytest

from app.linesdb import LinesDB
from app.resolver import ROLES, _passes, _path_frame, resolve_endpoint, resolve_route, summarize
from app.routesdb import RoutesDB
from app.stopsdb import StopsDB
from geoutil import EARTH_R, local_xy, point_polyline_m
from textnorm import name_key

TINY = Path(__file__).parent / "fixtures" / "tiny_db"
RADIUS = {"bus": 300.0, "subway": 1000.0}
GAP = {"bus": 10.0, "subway": 150.0}


@pytest.fixture(scope="module")
def db():
    return StopsDB.load(TINY, TINY)


@pytest.fixture(scope="module")
def rdb():
    return RoutesDB.load(TINY)


@pytest.fixture(scope="module")
def ldb():
    return LinesDB.load(TINY)


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


# --- 진행 방향 (버스 노선 순서) ---

def bus_leg(db, board_anchor, stops, vehicle, alight_anchor):
    """승·하차 기준점만 있는 버스 한 구간."""
    return {"steps": [step("BUS", [board_anchor, alight_anchor], stops, [vehicle])]}


def gucheong_mid(db):
    """상·하행 분당구청(30 m) 한가운데 — 거리만으로는 어느 쪽인지 가릴 수 없다."""
    return move(pos(db, "bus", "204000101"), north_m=15)


def test_bus_route_order_resolves_direction(db, rdb):
    # 마을 10 은 상행 분당구청(204000101)에만 선다 — 노선 순서가 승차 → 하차면 반대편(204000102)이 떨어진다
    make = lambda: bus_leg(db, gucheong_mid(db), ["분당구청", "분당구청입구"], ("마을", "10"),
                           pos(db, "bus", "204000104"))
    plain = make()
    resolve_route(db, plain, RADIUS, GAP)
    assert plain["steps"][0]["resolution"]["board"]["status"] == "ambiguous"

    r = make()
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    e = r["steps"][0]["resolution"]["board"]
    assert (e["status"], e["line_filter"], e["n_candidates"]) == ("matched", "applied", 1)
    assert e["chosen"]["id"] == "204000101" and e["second_gap_m"] is None
    assert r["steps"][0]["resolution"]["alight"]["chosen"]["id"] == "204000104"


def test_bus_route_serving_both_sides_stays_ambiguous(db, rdb):
    # 9-1 은 상·하행 분당구청에 모두 선다 → 방향을 가릴 근거가 없으니 모호로 남긴다
    r = bus_leg(db, gucheong_mid(db), ["분당구청", "판교테크노"], ("좌석", "9-1"),
                pos(db, "bus", "204000201"))
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    board, alight = r["steps"][0]["resolution"]["board"], r["steps"][0]["resolution"]["alight"]
    assert (board["status"], board["line_filter"], board["n_candidates"]) == ("ambiguous", "applied", 2)
    # 하차 판교테크노는 9-1 에 없다 → 그쪽은 거르지 않고 그대로 매칭한다
    assert (alight["line_filter"], alight["chosen"]["id"]) == ("mismatch", "204000201")


def test_bus_route_unknown_name_changes_nothing(db, rdb):
    r = bus_leg(db, gucheong_mid(db), ["분당구청", "분당구청입구"], ("간선", "9999"),
                pos(db, "bus", "204000104"))
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    e = r["steps"][0]["resolution"]["board"]
    assert (e["status"], e["line_filter"], e["n_candidates"]) == ("ambiguous", "unmapped", 3)


def updown_route():
    """경기 순서표처럼 상행 뒤에 하행이 한 순번열로 이어지는 노선 — 분당구청은 상행 상행(204000101) · 하행(204000102)."""
    seq = [("상행", "204000401", "야탑역"), ("상행", "204000101", "분당구청"), ("상행", "204000201", "판교테크노"),
           ("하행", "204000301", "삼평동주민센터"), ("하행", "204000102", "분당구청"), ("하행", "204000401", "야탑역")]
    return RoutesDB([{"route_id": "204000904", "route_name": "77", "route_type": "일반", "route_type_src": "",
                      "source": "gyeonggi", "seq": str(i + 1), "updown": u, "stop_key": k, "name": n,
                      "lat": "37.0", "lon": "127.0", "in_db": "1", "is_virtual": "0"}
                     for i, (u, k, n) in enumerate(seq)])


@pytest.mark.parametrize("stops, anchors, role, expected", [
    # 하행: 분당구청 → 야탑역. '승차 < 하차' 만 보면 상행 분당구청(순서 1 → 5, 간격 4)도 지나지만,
    # 구간 정류소가 2개(간격 1)라 하행 분당구청(순서 4 → 5)이 이긴다
    (["분당구청", "야탑역"], ("mid", "204000401"), "board", "204000102"),
    # 상행: 야탑역 → 분당구청. 같은 이유로 상행 분당구청(0 → 1)이 이긴다
    (["야탑역", "분당구청"], ("204000401", "mid"), "alight", "204000101"),
])
def test_bus_route_gap_picks_the_travelled_direction(db, stops, anchors, role, expected):
    pt = lambda a: gucheong_mid(db) if a == "mid" else pos(db, "bus", a)
    r = {"steps": [step("BUS", [pt(anchors[0]), pt(anchors[1])], stops, [("일반", "77")])]}
    resolve_route(db, r, RADIUS, GAP, routes=updown_route())
    e = r["steps"][0]["resolution"][role]
    assert (e["status"], e["line_filter"], e["chosen"]["id"]) == ("matched", "applied", expected)


def _bus_rows(*rows):
    """(키, 이름, 좌표) → StopsDB 가 읽는 최소 버스 행."""
    return [{"stop_key": k, "name": n, "name_key": name_key(n, "bus"), "aliases": "",
             "lon": p[0], "lat": p[1], "sgg_nm": "용산구", "is_virtual": "0", "line_group": ""}
            for k, n, p in rows]


def test_gap_fit_beats_higher_name_level():
    """간격이 맞는 후보가 이름 수준이 낮아도 이긴다 — 순서표가 말한 자리가 표기보다 강한 증거다.

    이름 수준으로만 고르면 간격이 한 칸 어긋난 토막 일치(parts)가 간격이 맞는 접두 일치(prefix)를 조용히 밀어낸다.
    """
    base = [126.965, 37.545]
    board, fit, offgap = base, move(base, east_m=400), move(base, east_m=450)
    db = StopsDB(_bus_rows(("100000801", "가나다승차장", board),
                           ("100000802", "오거리.사아병원앞", fit),      # 간격 정확(순번 1) · prefix
                           ("100000803", "사아병원.오거리", offgap)),   # 간격 한 칸 어긋남(순번 2) · parts
                 [], [])
    seq = [("100000801", "가나다승차장"), ("100000802", "오거리.사아병원앞"), ("100000803", "사아병원.오거리")]
    rdb = RoutesDB([{"route_id": "100000905", "route_name": "503", "route_type": "간선", "route_type_src": "",
                     "source": "seoul", "seq": str(i + 1), "updown": "", "stop_key": k, "name": n,
                     "lat": "37.545", "lon": "126.965", "in_db": "1", "is_virtual": "0"}
                    for i, (k, n) in enumerate(seq)])
    # 구간 정류소 2개(간격 1) — 하차 이름은 두 후보 어디와도 글자까지 같지 않다
    route = {"steps": [step("BUS", [board, fit], ["가나다승차장", "오거리.사아병원"], [("간선", "503")])]}
    resolve_route(db, route, RADIUS, GAP, routes=rdb)
    e = route["steps"][0]["resolution"]["alight"]
    assert e["chosen"]["id"] == "100000802"      # 간격이 맞는 쪽
    assert e["match_level"] == "prefix"           # 이름 수준은 더 낮다
    # 밀어낸 경쟁 후보는 사라지지 않고 거리 차로 남는다(50 m 라 모호 기준 10 m 밖 → matched)
    assert e["status"] == "matched" and e["second_gap_m"] is not None


def test_stop_options_prefers_on_route_over_name_level(db, rdb):
    """중간 정류소 점수에서 '노선이 서는 접두 후보' 가 '노선에 서지 않는 이름 일치 후보' 를 이긴다.
    LEVELS 에 수준을 더하면 감점이 이름 수준 감점에 밀릴 수 있어 고정한다."""
    from app.resolver import LEVELS, OFF_ROUTE_PENALTY
    assert OFF_ROUTE_PENALTY > 100 * (len(LEVELS) - 1)


def test_subway_keeps_line_group_filter(db, rdb):
    # 노선 순서표를 넘겨도 지하철은 노선군 필터 그대로다
    route = {"steps": [step("SUBWAY", [pos(db, "subway", "shinbundang-D12"), pos(db, "subway", "shinbundang-D13")],
                            ["정자", "판교"], [("일반", "신분당선")])]}
    resolve_route(db, route, RADIUS, GAP, routes=rdb)
    e = route["steps"][0]["resolution"]["board"]
    assert (e["line_filter"], e["chosen"]["id"]) == ("applied", "shinbundang-D12")


# --- 토막 순서가 뒤바뀐 복합 이름(parts) · 노선 순서로 고른 승·하차(route) ---

def gana(db):
    """복합 이름 쌍의 한쪽(204000501) 위치 — 40 m 동쪽에 같은 이름의 짝(204000502)이 있다.
    88·89 는 이쪽에만 서고, 90 은 양쪽에 연속으로 선다."""
    return pos(db, "bus", "204000501")


RESCUE_STOPS = ["가나센터.다라역", "마바공원", "사아병원.오거리", "자차상가"]  # 하차 '자차시장' 을 카카오가 다르게 부른다


def rescue_leg(db, stops, vehicle="88", east_m=10.0, north_m=0.0):
    """가나센터.다라역 → 자차시장 곁 구간 (하차 기준점은 자차시장에서 east_m·north_m)."""
    return bus_leg(db, gana(db), stops, ("일반", vehicle),
                   move(pos(db, "bus", "204000513"), east_m=east_m, north_m=north_m))


def test_parts_level_matches_reordered_name(db):
    # 카카오가 복합 이름의 토막 순서를 반대로 준다 — DB '사아병원.오거리' ↔ 질의 '오거리.사아병원'
    e = bus(db, "오거리.사아병원", move(pos(db, "bus", "204000512"), north_m=8))
    assert (e["status"], e["match_level"], e["chosen"]["id"]) == ("matched", "parts", "204000512")
    assert (e["error_m"], e["n_candidates"]) == (pytest.approx(8.0, abs=0.2), 1)
    assert e["name_mismatch"] is True and e["query_key"] == "오거리사아병원"
    # 이름 글자가 달랐다는 신호는 남긴다 — 정류소 DB 가 낡았는지(신설·개명) 보려면 이 값이 있어야 한다
    assert e["nearest_any"]["id"] == "204000512"
    assert e["n_global_same_name"] == 0 and e["nearest_same_name_m"] is None
    # 별칭 쪽 토막도 본다 (병합으로 모인 다른 원천 이름이 뒤바뀐 표기일 수 있다)
    a = bus(db, "오거리시장.사아병원", move(pos(db, "bus", "204000512"), north_m=8))
    assert (a["status"], a["match_level"], a["chosen"]["id"]) == ("matched", "parts", "204000512")


def test_parts_keeps_every_token(db):
    # 토막을 하나라도 버리는 일치는 후보가 아니다(부록 A R16 유지) — 토막 하나·토막 접두는 안 맞는다
    e = bus(db, "가나센터.다라역", move(gana(db), north_m=350))  # 다라역(260 m)·다라역10번출구(210 m) 곁, 정답은 반경 밖
    assert (e["status"], e["chosen"], e["n_candidates"]) == ("unmatched", None, 0)
    assert e["nearest_any"]["id"] == "204000511"
    assert e["nearest_same_name_m"] == pytest.approx(350.0, abs=1.0)
    # 거꾸로도 안 붙는다: 뒤 토막만 준 이름은 90 m 옆 동명 정류소를 고르고 복합 이름을 집지 않는다
    one = bus(db, "다라역", move(gana(db), north_m=8))
    assert (one["match_level"], one["chosen"]["id"]) == ("key", "204000503")


def test_reordered_twin_pair_is_ambiguous_without_route(db):
    # 길 양쪽 쌍(40 m)은 토막 순서로 갈리지 않는다 — 모호 기준(10 m)을 넘겨도 모호다(거리가 증거가 못 된다)
    e = bus(db, "다라역.가나센터", move(gana(db), north_m=8))
    assert (e["status"], e["match_level"], e["n_candidates"]) == ("ambiguous", "parts", 2)
    assert e["chosen"]["id"] in ("204000501", "204000502")
    assert e["second_gap_m"] == pytest.approx(32.8, abs=1.0)


def test_key_level_unchanged_by_parts(db):
    # 이름이 그대로 맞는 후보가 있으면 토막 수준은 끼어들지 않는다 (key 는 거리 기준 모호 판정 그대로)
    e = bus(db, "가나센터.다라역", move(gana(db), north_m=8))
    assert (e["status"], e["match_level"], e["chosen"]["id"]) == ("matched", "key", "204000501")
    assert (e["n_candidates"], e["name_mismatch"]) == (2, False)
    assert e["nearest_any"] is None and e["rescue"] is None


def test_reordered_name_resolved_by_route_order(db, rdb):
    # 신고 사례: 88 은 쌍의 한쪽(501)에만 선다 → 토막 후보 2개가 순서표로 하나가 된다
    r = bus_leg(db, move(gana(db), north_m=8), ["다라역.가나센터", "마바공원"], ("일반", "88"),
                pos(db, "bus", "204000511"))
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    e = r["steps"][0]["resolution"]["board"]
    assert (e["status"], e["match_level"], e["line_filter"]) == ("matched", "parts", "applied")
    assert (e["chosen"]["id"], e["n_candidates"]) == ("204000501", 1)
    assert e["rescue"] is None and e["name_mismatch"] is True  # 이름 근거로 찾았다 — 구제가 아니다
    # 뒤바뀐 이름도 구간 이름 검사에서 맞은 것으로 세어 순서표 구간이 열린다
    locs = r["steps"][0]["stop_locs"]
    assert [(x["id"], x["level"]) for x in locs] == [("204000501", "route"), ("204000511", "route")]


def test_route_serving_both_reordered_sides_stays_ambiguous(db, rdb):
    # 90 은 쌍의 양쪽에 연속으로 선다(502 → 501) → 순번이 한 칸 차이라 간격이 증거가 못 된다
    r = bus_leg(db, move(gana(db), north_m=8), ["다라역.가나센터", "마바공원"], ("일반", "90"),
                pos(db, "bus", "204000511"))
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    e = r["steps"][0]["resolution"]["board"]
    assert (e["status"], e["match_level"], e["line_filter"]) == ("ambiguous", "parts", "applied")
    assert e["n_candidates"] == 2 and e["chosen"]["id"] in ("204000501", "204000502")


def test_route_order_rescues_name_missing_from_db(db, rdb):
    # 개명 등으로 이름이 DB·순서표에 아예 없으면 이름을 버리고 노선 순서로 고른다 (간격 3 = 구간 정류소 4개 - 1)
    r = rescue_leg(db, RESCUE_STOPS)
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    e = r["steps"][0]["resolution"]["alight"]
    assert (e["status"], e["match_level"], e["line_filter"]) == ("matched", "route", "route")
    assert (e["chosen"]["id"], e["n_candidates"]) == ("204000513", 1)
    assert e["error_m"] == pytest.approx(10.0, abs=0.3)
    assert e["rescue"] == {"route_ids": ["204000905"], "gap": 3, "n_rivals": 0, "n_in_radius": 1}
    assert e["name_mismatch"] is True and e["query_key"] == "자차상가"
    assert e["nearest_any"]["id"] == "204000513" and e["nearest_same_name_m"] is None
    # 하차가 잡히니 순서표 구간이 열려 중간 정류소까지 제자리에 놓인다
    locs = r["steps"][0]["stop_locs"]
    assert [x["id"] for x in locs] == ["204000501", "204000511", "204000512", "204000513"]
    assert [x["level"] for x in locs] == ["route"] * 4
    json.dumps(r)  # 행의 토막 집합(set)이 응답에 새지 않는다


def test_route_rescue_needs_exact_gap(db, rdb):
    # 구간 정류소를 하나 빼면 기대 간격이 2 라 순번 3 인 자차시장이 떨어진다 — 조용한 오답 대신 미매칭이다
    r = rescue_leg(db, ["가나센터.다라역", "마바공원", "자차상가"])
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    board, alight = (r["steps"][0]["resolution"][k] for k in ROLES)
    assert (alight["status"], alight["match_level"], alight["line_filter"]) == ("unmatched", None, "route_gap")
    assert (alight["chosen"], alight["n_candidates"], alight["rescue"]) == (None, 0, None)
    assert alight["nearest_any"]["id"] == "204000513"
    assert board["line_filter"] == "applied"  # 승차는 그대로 노선으로 가린다


def test_route_rescue_rejects_near_order_rival(db, rdb):
    # 89 는 자차시장 다음 150 m 옆 차카빌딩에도 선다 — 순번이 한 칸 차이라 정류소 개수가 하나 틀리면 답이 뒤집힌다
    r = rescue_leg(db, RESCUE_STOPS, vehicle="89")
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    e = r["steps"][0]["resolution"]["alight"]
    assert (e["status"], e["match_level"], e["line_filter"]) == ("ambiguous", "route", "route")
    assert (e["n_candidates"], e["chosen"]["id"]) == (2, "204000513")
    assert e["rescue"]["n_in_radius"] == 2


def test_rescue_restores_gap_filter_on_the_other_end(db, rdb):
    # 90 은 쌍의 양쪽에 다 서니 승차는 순서 간격으로만 갈린다 — 하차 후보가 0개면 그 판정까지 함께 꺼져 있었다
    r = bus_leg(db, move(gana(db), north_m=8), ["가나센터.다라역", "없는정류장"], ("일반", "90"),
                pos(db, "bus", "204000511"))
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    board, alight = (r["steps"][0]["resolution"][k] for k in ROLES)
    assert (board["status"], board["match_level"], board["n_candidates"]) == ("matched", "key", 1)
    assert board["chosen"]["id"] == "204000501"
    assert (alight["status"], alight["match_level"], alight["chosen"]["id"]) == ("matched", "route", "204000511")


def test_no_rescue_outside_radius(db, rdb):
    # 반경(300 m) 밖의 노선 정류장은 구제 후보가 아니다 — 토막·순서만으로 먼 곳을 집는 오매칭을 반경이 막는다
    r = rescue_leg(db, RESCUE_STOPS, east_m=0.0, north_m=400.0)
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    e = r["steps"][0]["resolution"]["alight"]
    assert (e["status"], e["line_filter"], e["rescue"]) == ("unmatched", "mismatch", None)
    assert e["n_candidates"] == 0


def test_no_rescue_without_stop_count(db, rdb):
    # 구간 정류소 개수를 모르면(stops 1개) 간격을 볼 수 없다 → 근접만으로는 길 양쪽을 못 가리므로 구제하지 않는다
    near = move(pos(db, "bus", "204000513"), east_m=10)
    r = {"steps": [step("BUS", [near, move(near, east_m=5)], ["자차상가"], [("일반", "88")])]}
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    e = r["steps"][0]["resolution"]["board"]
    assert (e["status"], e["line_filter"], e["rescue"]) == ("unmatched", "mismatch", None)
    assert e["nearest_any"]["id"] == "204000513"


def test_no_rescue_when_route_unknown(db, rdb):
    # 차량 이름으로 노선을 못 찾으면 구제할 순서표가 없다 → 예전처럼 미매칭이다
    r = rescue_leg(db, RESCUE_STOPS, vehicle="7777")
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    board, alight = (r["steps"][0]["resolution"][k] for k in ROLES)
    assert (alight["status"], alight["line_filter"], alight["rescue"]) == ("unmatched", "unmapped", None)
    assert (board["status"], board["match_level"], board["n_candidates"]) == ("matched", "key", 2)


def test_rescue_never_chooses_stop_missing_from_db(db, rdb):
    # 순서표에만 있는 정류장(in_db=0)은 구제 후보가 아니다 — chosen 에 담으면 실시간 도착·지도 칩이 깨진다
    ghost = [126.97, 37.573]  # 100000999 새정류소 자리 (정류소 DB 에는 없다)
    r = bus_leg(db, pos(db, "bus", "100000201"), ["종로2가", "없는이름"], ("마을", "99"), ghost)
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    e = r["steps"][0]["resolution"]["alight"]
    assert (e["status"], e["line_filter"], e["chosen"]) == ("unmatched", "mismatch", None)
    assert db.by_id("bus", "100000999") is None


def test_prefix_level_is_ranked_by_distance(db):
    # 접두 일치는 이름 글자가 그대로 남은 검사라 거리가 증거다 — 경쟁 후보가 있어도 거리 차가 크면 matched 다
    # (prefix 를 WEAK_LEVELS 에 넣으면 이 항목이 ambiguous 로 뒤집힌다)
    e = bus(db, "분당구청입", pos(db, "bus", "204000104"))
    assert (e["status"], e["match_level"], e["chosen"]["id"]) == ("matched", "prefix", "204000104")
    assert e["n_candidates"] == 3 and e["second_gap_m"] == pytest.approx(20.0, abs=0.5)


# --- 진행 방향 판정의 경계 (합성 정류소 DB — tiny_db 의 후보 개수를 흔들지 않는다) ---

SYN = [127.15, 37.44]


def syn_db(rows):
    """(키, 이름, 북쪽 m, 동쪽 m) 목록 → 버스 정류소만 있는 StopsDB."""
    out = []
    for key, name, north_m, east_m in rows:
        lon, lat = move(SYN, east_m=east_m, north_m=north_m)
        out.append({"stop_key": key, "name": name, "name_key": name_key(name, "bus"), "aliases": "",
                    "lat": lat, "lon": lon, "sgg_nm": "성남시", "is_virtual": "0"})
    return StopsDB(out, [], [])


def syn_routes(rows, defs):
    """[(노선 이름, [정류장 키…])] → RoutesDB (이름·좌표는 같은 정류소 목록에서 가져온다)."""
    by = {r[0]: r for r in rows}
    out = []
    for i, (rname, keys) in enumerate(defs):
        for j, k in enumerate(keys):
            _, name, north_m, east_m = by[k]
            lon, lat = move(SYN, east_m=east_m, north_m=north_m)
            out.append({"route_id": f"SYN{i}", "route_name": rname, "route_type": "일반", "route_type_src": "",
                        "source": "gyeonggi", "seq": str(j + 1), "updown": "상행", "stop_key": k,
                        "name": name, "lat": str(lat), "lon": str(lon), "in_db": "1", "is_virtual": "0"})
    return RoutesDB(out)


STALE = [("V01", "정답정류장", 0, 0), ("V02", "딴정류장", 250, 0),
         ("V10", "중간", 700, 0), ("V20", "끝", 1200, 0)]


def test_name_match_is_not_traded_for_stale_route_table():
    # 이름이 그대로 맞는 후보(0 m)가 있으면 구제하지 않는다 — 그 정류장이 순서표에 없는 것은 순서표가 낡았다는 뜻이다.
    # 구제가 끼어들면 250 m 떨어진 노선 정류장을 matched 로 보고한다(실데이터: 어느 노선에도 없는 실차 정류소 136곳)
    db = syn_db(STALE)
    rdb = syn_routes(STALE, [("111", ["V02", "V10", "V20"])])
    make = lambda: bus_leg(db, move(SYN), ["정답정류장", "중간", "끝"], ("일반", "111"),
                           move(SYN, north_m=1200))
    plain, r = make(), make()
    resolve_route(db, plain, RADIUS, GAP)
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    for e in (plain["steps"][0]["resolution"]["board"], r["steps"][0]["resolution"]["board"]):
        assert (e["status"], e["match_level"], e["chosen"]["id"]) == ("matched", "key", "V01")
        assert e["error_m"] == pytest.approx(0.0, abs=0.5) and e["rescue"] is None
    # 그 역할만 노선으로 가리지 않는다 — 다른 쪽 끝의 판정은 그대로다
    assert r["steps"][0]["resolution"]["board"]["line_filter"] == "mismatch"
    alight = r["steps"][0]["resolution"]["alight"]
    assert (alight["line_filter"], alight["chosen"]["id"]) == ("applied", "V20")


PREF = [("Q00", "출발", 0, 0), ("Q10", "중간", 300, 0),
        ("Q02", "가나다라역", 900, 0), ("Q01", "가나다라", 900, 240)]


@pytest.mark.parametrize("keys, level, chosen, err", [
    # 접두 후보(Q01)가 그 노선에 서지 않는다 → 이름을 버리고 순서표가 고른다 (간격 2 = 구간 정류소 3개 - 1)
    (["Q00", "Q10", "Q02"], "route", "Q02", 0.0),
    # 접두 후보가 그 노선에 선다 → 간격이 정확히 맞는 Q02 를 두고도 이름 근거가 이긴다
    (["Q00", "Q10", "Q02", "Q01"], "prefix", "Q01", 240.0),
])
def test_rescue_overrides_only_off_route_prefix(keys, level, chosen, err):
    # 접두 일치는 앞 4글자만 같은 가장 느슨한 검사다('가나다라' ↔ '가나다라.마바센터') — 그 후보가 노선에 서지 않으면
    # 앞 글자가 겹친 다른 정류소일 때가 많아 순서표로 고른다. 글자가 맞은 후보(key·alias·parts)는 이렇게 버리지 않는다
    db = syn_db(PREF)
    rdb = syn_routes(PREF, [("505", keys)])
    r = bus_leg(db, move(SYN), ["출발", "중간", "가나다라.마바센터"], ("일반", "505"), move(SYN, north_m=900))
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    e = r["steps"][0]["resolution"]["alight"]
    assert (e["status"], e["match_level"], e["chosen"]["id"]) == ("matched", level, chosen)
    assert e["error_m"] == pytest.approx(err, abs=1.0)
    assert (e["name_mismatch"], e["n_candidates"]) == (True, 1)


TWIN_END = [("B", "사아병원.오거리", 0, 0), ("M1", "중간1", 100, 0), ("M2", "중간2", 200, 0),
            ("M3", "중간3", 300, 0), ("D", "구청", 400, 0), ("N1", "기타1", 500, 0),
            ("N2", "기타2", 600, 0), ("N3", "기타3", 700, 0), ("U", "구청", 430, 0)]


@pytest.mark.parametrize("stops", [
    ["오거리.사아병원", "a", "b", "c", "구청"],   # want=4 → B→D(간격 4)가 정확히 맞는다
    ["오거리.사아병원", "a", "b", "구청"],        # want=3 → 정확히 맞는 짝이 없다 (B→D 4, B→U 8)
])
def test_weak_end_does_not_disable_the_other_end(stops):
    # 간격 제한은 역할마다 따로 본다 — 한쪽 끝이 약한 수준(parts)이라고 다른 쪽 끝의 방향 판정까지 꺼지면,
    # 하차가 기준점에 3 m 더 가까운 상행 쪽(U, 간격 8)으로 뒤집힌다. 구간 정류소 개수는 이름 없는 정류소가
    # 빠지는 것만으로도 하나 모자란다(transit.normalize_transit)
    db = syn_db(TWIN_END)
    rdb = syn_routes(TWIN_END, [("77a", ["B", "M1", "M2", "M3", "D", "N1", "N2", "N3", "U"])])
    r = bus_leg(db, move(SYN, north_m=8), stops, ("일반", "77a"), move(SYN, north_m=427))
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    board, alight = (r["steps"][0]["resolution"][k] for k in ROLES)
    assert (alight["status"], alight["match_level"], alight["chosen"]["id"]) == ("matched", "key", "D")
    assert (alight["n_candidates"], alight["line_filter"]) == (1, "applied")
    assert (board["status"], board["match_level"], board["chosen"]["id"]) == ("matched", "parts", "B")


MIXED = [("W00", "출발", 0, 0), ("W01", "강남역삼동", 760, 60),
         ("W10", "역삼.강남", 800, 0), ("W11", "역삼.강남", 800, 45)]


@pytest.mark.parametrize("keys, n", [
    (["W00", "W01", "W10", "W11"], 3),   # 접두 후보(W01)가 그 노선에 선다
    (["W00", "W10", "W11"], 2),
])
def test_prefix_candidate_does_not_disable_weak_protection(keys, n):
    # 판정은 가장 높은 수준(parts)에서 내려진다 — 같은 반경에 접두 후보가 섞였다고 경쟁 후보 보호가 꺼지면
    # 한 칸 어긋나면 뒤집히는 짝이 모호 표시 없이 matched 로 나간다
    db = syn_db(MIXED)
    rdb = syn_routes(MIXED, [("222", keys)])
    stops = ["출발", *["x"] * (len(keys) - 2), "강남.역삼"]
    r = bus_leg(db, move(SYN), stops, ("일반", "222"), move(SYN, north_m=800, east_m=10))
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    e = r["steps"][0]["resolution"]["alight"]
    assert (e["status"], e["match_level"], e["n_candidates"]) == ("ambiguous", "parts", n)
    assert e["chosen"]["id"] == "W11"  # 간격이 맞는 쪽 — 25 m 더 가까운 W10 은 경쟁 후보다


CROSS = [("X01", "가나", 0, 0), ("X02", "가나", 0, 45), ("X10", "중간", 150, 0),
         ("X20", "다라", 300, 0), ("X21", "다라", 300, 45)]


def test_same_name_routes_unlinked_pair_is_flagged():
    # 이름이 같은 노선이 둘이면 승·하차를 역할별로 따로 골라 **어느 노선도 잇지 못하는 짝**이 나온다 —
    # 둘 중 하나는 반드시 오답이므로 둘 다 모호로 내린다
    db = syn_db(CROSS)
    rdb = syn_routes(CROSS, [("503", ["X01", "X10", "X20"]), ("503", ["X02", "X10", "X21"])])
    r = bus_leg(db, move(SYN, east_m=5), ["가나", "중간", "다라"], ("일반", "503"),
                move(SYN, north_m=300, east_m=40))
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    board, alight = (r["steps"][0]["resolution"][k] for k in ROLES)
    assert (board["chosen"]["id"], alight["chosen"]["id"]) == ("X01", "X21")  # X01 은 앞 노선, X21 은 뒤 노선
    assert [e["status"] for e in (board, alight)] == ["ambiguous"] * 2
    assert [e["line_filter"] for e in (board, alight)] == ["unlinked"] * 2


RIVAL = [("U00", "앞정류장", -60, 0), ("U01", "출발", 0, 0), ("U02", "중간", 150, 0), ("U03", "도착", 300, 0)]


def test_near_order_rival_is_never_chosen():
    # 경쟁 후보(순번 이웃)는 모호 근거일 뿐이다 — 간격이 맞는 U01 을 담고, 기준점에 50 m 더 가까운
    # U00(승차보다 앞 정류장)을 chosen 에 담지 않는다. chosen 은 지도 칩·실시간 도착이 그대로 쓴다
    db = syn_db(RIVAL)
    rdb = syn_routes(RIVAL, [("77", ["U00", "U01", "U02", "U03"])])
    r = bus_leg(db, move(SYN, north_m=-55), ["개명된출발", "중간", "도착"], ("일반", "77"),
                move(SYN, north_m=300))
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    e = r["steps"][0]["resolution"]["board"]
    assert (e["status"], e["match_level"], e["chosen"]["id"]) == ("ambiguous", "route", "U01")
    assert (e["n_candidates"], e["line_filter"]) == (3, "route")
    assert e["error_m"] == pytest.approx(55.0, abs=0.5)
    assert e["second_gap_m"] == pytest.approx(-50.0, abs=0.5)  # 경쟁 후보가 chosen 보다 가까우면 음수다
    assert e["rescue"] == {"route_ids": ["SYN0"], "gap": 2, "n_rivals": 2, "n_in_radius": 3}


LINE3 = [("A", "출발", 0, 0), ("B", "중간", 100, 0), ("C", "도착", 200, 0)]


def test_rival_that_cannot_be_boarded_is_dropped():
    # 하차(C) 뒤에서 타는 짝은 순번이 붙어 있어도 경쟁 후보가 아니다 — 탈 수 없는 정류장은 후보에서 뺀다
    db = syn_db(LINE3)
    rdb = syn_routes(LINE3, [("77", ["A", "B", "C"])])
    r = bus_leg(db, move(SYN, north_m=110), ["개명된정류장", "도착"], ("일반", "77"), move(SYN, north_m=200))
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    e = r["steps"][0]["resolution"]["board"]
    assert (e["chosen"]["id"], e["n_candidates"]) == ("B", 2)  # A(순번 0)는 경쟁 후보, C(하차)는 빠진다
    assert e["rescue"]["n_rivals"] == 1


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


def bus_path(db):
    """야탑역 → (하행 분당구청 쪽을 스쳐) → 판교테크노 — 선은 204000102 쪽으로 지난다."""
    return [move(pos(db, "bus", "204000401"), east_m=10), move(pos(db, "bus", "204000102"), north_m=10),
            move(pos(db, "bus", "204000201"), east_m=10)]


def test_stop_locs_reads_route_window(db, rdb):
    # 노선과 승·하차가 정해지면 그 사이 정류소는 순서표에서 그대로 읽는다 (선은 하행 쪽을 지나지만 380 은 상행에 선다)
    make = lambda: {"steps": [step("BUS", bus_path(db), ["야탑역", "분당구청", "판교테크노"], [("일반", "380")])]}
    plain = make()
    resolve_route(db, plain, RADIUS, GAP)
    assert [x["id"] for x in plain["steps"][0]["stop_locs"]] == ["204000401", "204000102", "204000201"]

    r = make()
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    locs = r["steps"][0]["stop_locs"]
    assert [x["id"] for x in locs] == ["204000401", "204000101", "204000201"]
    assert [x["level"] for x in locs] == ["route"] * 3
    assert [locs[1]["lon"], locs[1]["lat"]] == pos(db, "bus", "204000101")  # 좌표는 정류소 DB 값


def test_stop_locs_route_side_without_window(db, rdb):
    # 380 의 야탑역 → 삼평동주민센터 사이에는 정류소가 2개라 3개짜리 구간을 찾을 수 없다 → 기하로 고르되
    # 노선이 서지 않는 쪽은 감점한다.
    # 하차 기준점은 하차 이름과 같은 곳에 둔다 — 어긋나게 두면 승·하차 이름 매칭이 아니라 노선 순서 구제를 시험하게 된다
    path = bus_path(db)[:-1] + [move(pos(db, "bus", "204000301"), east_m=10)]
    r = {"steps": [step("BUS", path, ["야탑역", "분당구청", "삼평동주민센터"], [("일반", "380")])]}
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    locs = r["steps"][0]["stop_locs"]
    assert (locs[1]["id"], locs[1]["level"]) == ("204000101", "key")   # 순서표가 아니라 이름 수준


def test_stop_locs_window_places_stop_missing_from_db(db, rdb):
    # 순서표에만 있는 정류소(in_db=0)도 순서표 좌표로 찍는다 — 이름으로는 DB 에서 찾을 수 없다
    path = [pos(db, "bus", "100000201"), pos(db, "bus", "100000202")]
    r = {"steps": [step("BUS", path, ["종로2가", "새정류소", "광화문"], [("마을", "99")])]}
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    locs = r["steps"][0]["stop_locs"]
    assert [x["id"] for x in locs] == ["100000201", "100000999", "100000202"]
    assert [locs[1]["lon"], locs[1]["lat"]] == [126.97, 37.573]
    assert db.by_id("bus", "100000999") is None


def test_stop_locs_window_rejected_when_names_disagree(db, rdb):
    # 구간 정류소 이름이 순서표와 어긋나면(노선 변형·낡은 순서표) 구간을 믿지 않고 기하로 돌아간다
    path = bus_path(db) + [move(pos(db, "bus", "204000301"), east_m=10)]
    r = {"steps": [step("BUS", path, ["야탑역", "없는정류장1", "없는정류장2", "삼평동주민센터"], [("일반", "380")])]}
    resolve_route(db, r, RADIUS, GAP, routes=rdb)
    assert [x["level"] for x in r["steps"][0]["stop_locs"]] == ["key", None, None, "key"]


@pytest.mark.parametrize("stops, ends, expected", [
    (["수원", "신길온천", "정자"], ("bundang-K245", "bundang-K222"),
     ["bundang-K245", "bundang-K240", "bundang-K222"]),
    # 덩어리에는 방향이 없다 — 거꾸로 타면 구간도 거꾸로
    (["정자", "신길온천", "수원"], ("bundang-K222", "bundang-K245"),
     ["bundang-K222", "bundang-K240", "bundang-K245"]),
])
def test_stop_locs_subway_reads_line_window(db, ldb, stops, ends, expected):
    # 카카오가 옛 이름 '신길온천' 을 줘 이름으로는 못 찾는 역(DB 는 '능길')도 순서표 구간이 제자리에 놓는다
    make = lambda: {"steps": [step("SUBWAY", [pos(db, "subway", ends[0]), pos(db, "subway", ends[1])],
                                   stops, [("일반", "수인분당선")])]}
    plain = make()
    resolve_route(db, plain, RADIUS, GAP)
    assert [x["id"] for x in plain["steps"][0]["stop_locs"]] == [expected[0], None, expected[2]]

    r = make()
    resolve_route(db, r, RADIUS, GAP, lines=ldb)
    locs = r["steps"][0]["stop_locs"]
    assert [x["id"] for x in locs] == expected
    assert [x["level"] for x in locs] == ["route"] * 3


def test_stop_locs_subway_window_wraps_on_loop_line():
    # 순환선은 끝에서 처음으로 이어 붙여 구간을 뗀다 (라 → 마 → 가)
    names = ["가나", "다라", "마바", "사아", "자차"]
    base = [127.0, 37.5]
    pts = [move(base, east_m=500 * i) for i in range(5)]
    rows = [{"station_id": f"loop-{i}", "name": n, "name_key": n, "aliases": "", "line_group": "2호선",
             "lon": p[0], "lat": p[1], "sgg_nm": "중구"} for i, (n, p) in enumerate(zip(names, pts))]
    db = StopsDB([], rows, [])
    ldb = LinesDB([{"chain_id": "loop", "line_group": "2호선", "loop": "1", "seq": str(i + 1),
                    "station_id": f"loop-{i}", "phys_id": f"loop-{i}", "name": n,
                    "lat": p[1], "lon": p[0]} for i, (n, p) in enumerate(zip(names, pts))])
    route = {"steps": [step("SUBWAY", [pts[3], pts[0]], ["사아", "자차", "가나"], [("일반", "2호선")])]}
    resolve_route(db, route, RADIUS, GAP, lines=ldb)
    locs = route["steps"][0]["stop_locs"]
    assert [x["id"] for x in locs] == ["loop-3", "loop-4", "loop-0"]
    assert [x["level"] for x in locs] == ["route"] * 3


def test_stop_locs_subway_express_falls_back_to_geometry(db, ldb):
    # 급행은 카카오가 정차역만 준다 — 역 개수가 안 맞으면 구간을 못 찾고 이름·기하로 돌아간다
    r = {"steps": [step("SUBWAY", [pos(db, "subway", "bundang-K245"), pos(db, "subway", "bundang-K222")],
                        ["수원", "정자"], [("급행", "수인분당선")])]}
    resolve_route(db, r, RADIUS, GAP, lines=ldb)
    assert [x["level"] for x in r["steps"][0]["stop_locs"]] == ["key", "key"]


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
    assert s["levels"] == {"key": 3, "alias": 1, "parts": 0, "prefix": 1, "route": 0}
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
    assert s["levels"] == {"key": 0, "alias": 0, "parts": 0, "prefix": 0, "route": 0}
    assert s["by_kind"]["bus"]["n"] == 0 and s["unmapped_vehicle_names"] == []
    only_skipped = summarize([_e("bus", "skipped")])
    assert only_skipped["match_rate"] is None and only_skipped["found_rate"] is None
