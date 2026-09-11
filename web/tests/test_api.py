"""API 스모크 — 합성 픽스처 + httpx.MockTransport 만 쓴다 (카카오·VWorld 호출 없음, 실제 .env 미사용)."""
import copy
import json
import logging
import re
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

FIX = Path(__file__).parent / "fixtures"
TINY = FIX / "tiny_db"
FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
KKEY = "0123456789abcdef0123456789abcdef"  # 가짜 키
VKEY = "00000000-0000-0000-0000-000000000000"
NOTE = "SYNTHETIC - hand-written from documented schema; not a Kakao response"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
TRANSIT = json.loads((FIX / "transit_synthetic.json").read_text(encoding="utf-8"))
CAR = json.loads((FIX / "car_synthetic.json").read_text(encoding="utf-8"))
KEYWORD = json.loads((FIX / "keyword_synthetic.json").read_text(encoding="utf-8"))
ADDRESS = json.loads((FIX / "address_synthetic.json").read_text(encoding="utf-8"))
KEYWORD_PATH = "/v2/local/search/keyword.json"
ADDRESS_PATH = "/v2/local/search/address.json"
BOUNDARIES = {"type": "FeatureCollection", "features": [{
    "type": "Feature",
    "properties": {"sido_cd": "41", "sido_nm": "경기도", "sgg_cd": "41130", "sgg_nm": "성남시"},
    "geometry": {"type": "Polygon",
                 "coordinates": [[[127.0, 37.3], [127.2, 37.3], [127.2, 37.5], [127.0, 37.5], [127.0, 37.3]]]},
}]}
OD = {"sx": 127.0, "sy": 37.266, "ex": 127.1112, "ey": 37.3947}  # 합성 대중교통 1번 경로의 양 끝


class Upstream:
    """호스트별(로컬 검색은 경로별) 합성 응답. 테스트가 `routes[host 또는 path]` 를 바꿔 오류를 흉내 낸다."""

    def __init__(self):
        self.routes = {
            "dapi.kakao.com": lambda req: httpx.Response(200, json=TRANSIT),
            KEYWORD_PATH: lambda req: httpx.Response(200, json=KEYWORD),
            ADDRESS_PATH: lambda req: httpx.Response(200, json=ADDRESS),
            "apis-navi.kakaomobility.com": lambda req: httpx.Response(200, json=CAR),
            "api.vworld.kr": lambda req: httpx.Response(200, content=PNG, headers={"content-type": "image/png"}),
        }
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return (self.routes.get(request.url.path) or self.routes[request.url.host])(request)

    def hosts(self):
        return [r.url.host for r in self.requests]


def make_settings(tmp_path, with_db=True, **overrides):
    processed, ref = tmp_path / "processed", tmp_path / "ref"
    processed.mkdir()
    ref.mkdir()
    if with_db:
        shutil.copy(TINY / "bus_stops.csv", processed)
        shutil.copy(TINY / "subway_stations.csv", processed)
        shutil.copy(TINY / "bus_route_stops.csv", processed)
        shutil.copy(TINY / "line_groups.csv", ref)
        (processed / "admin_sgg_web.geojson").write_text(json.dumps(BOUNDARIES), encoding="utf-8")
    fields = dict(repo_root=tmp_path, processed_dir=processed, ref_dir=ref, frontend_dir=FRONTEND,
                  quota_path=tmp_path / "var" / "quota.json", kakao_rest_key=KKEY, vworld_key=VKEY)
    return Settings(**{**fields, **overrides})


def serve(settings, up):
    return TestClient(create_app(settings=settings, transport=httpx.MockTransport(up)))


@pytest.fixture
def up():
    return Upstream()


@pytest.fixture
def client(tmp_path, up):
    with serve(make_settings(tmp_path), up) as c:
        yield c


def error(r, status, code):
    assert r.status_code == status, r.text
    err = r.json()["error"]
    assert err["code"] == code
    return err


def no_keys(text):
    assert KKEY not in text and VKEY not in text


# --- 앱 구성 ---

def test_import_does_not_build_default_app():
    import app.main as m
    # 기본 앱(load_settings → 실제 .env)은 uvicorn 이 app.main:app 을 가져갈 때만 만든다
    assert "app" not in vars(m)


# --- /api/health ---

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    h = r.json()
    assert (h["ready"], h["bus_stops"], h["subway_stations"]) == (True, 9, 4)
    assert h["keys"] == {"vworld": True, "kakao_rest": True}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00", h["data_mtime"])
    no_keys(r.text)


def test_not_ready_and_no_keys(tmp_path, up):
    with serve(make_settings(tmp_path, with_db=False, kakao_rest_key=None, vworld_key=None), up) as c:
        assert c.get("/api/health").json() == {"ready": False, "bus_stops": 0, "subway_stations": 0,
                                               "data_mtime": None, "keys": {"vworld": False, "kakao_rest": False}}
        for path in ("/api/stops?kinds=subway", "/api/boundaries"):
            err = error(c.get(path), 503, "data_not_built")
            assert err["message"] == "정제 데이터가 없습니다." and "data/README.md" in err["action"]
        for path in ("/api/transit", "/api/car"):
            assert "KAKAO_REST_API_KEY" in error(c.get(path, params=OD), 503, "missing_key")["action"]
        assert "KAKAO_REST_API_KEY" in error(c.get("/api/search", params={"q": "합성"}), 503, "missing_key")["action"]
        assert "VWORLD_API_KEY" in error(c.get("/tiles/vworld/white/12/1581/3493"), 503, "missing_key")["action"]
    assert up.requests == []


# --- /api/stops ---

BUS_KEYS = {"id", "name", "lat", "lon", "sgg", "ars", "src", "merge", "virtual", "snap_m", "label", "aliases"}
SUBWAY_KEYS = {"id", "name", "line", "group", "color", "lat", "lon", "sgg", "transfer", "date", "operator"}
PANGYO_BOX = "127.118,37.382,127.120,37.383"  # 분당구청 상/하행 + 분당구청입구 (+ 미정차 1)


def test_stops_bus_bbox(client):
    body = client.get("/api/stops", params={"bbox": PANGYO_BOX, "kinds": "bus"}).json()
    assert (body["count"], body["truncated"], body["subway"]) == (3, False, [])
    assert sorted(s["id"] for s in body["bus"]) == ["204000101", "204000102", "204000104"]
    item = next(s for s in body["bus"] if s["id"] == "204000101")
    assert set(item) == BUS_KEYS
    assert item == {"id": "204000101", "name": "분당구청", "lat": 37.3825, "lon": 127.119, "sgg": "성남시",
                    "ars": "07101", "src": "GGB204000101|SEB204000101", "merge": "merged_near",
                    "virtual": False, "snap_m": None, "label": "성남시", "aliases": ""}

    body = client.get("/api/stops", params={"bbox": PANGYO_BOX, "kinds": "bus", "virtual": 1}).json()
    assert body["count"] == 4
    assert [s["id"] for s in body["bus"] if s["virtual"] is True] == ["204000103"]


def test_stops_truncation(client):
    q = {"bbox": PANGYO_BOX, "kinds": "bus"}
    body = client.get("/api/stops", params={**q, "limit": 2}).json()
    assert (body["count"], body["truncated"], len(body["bus"])) == (2, True, 2)
    assert client.get("/api/stops", params={**q, "limit": 3}).json()["truncated"] is False


def test_stops_subway_all_without_bbox(client):
    body = client.get("/api/stops", params={"kinds": "subway"}).json()
    assert (body["count"], body["truncated"], body["bus"]) == (4, False, [])
    item = next(s for s in body["subway"] if s["id"] == "bundang-K222")
    assert set(item) == SUBWAY_KEYS
    assert item == {"id": "bundang-K222", "name": "정자", "line": "분당선", "group": "수인분당선",
                    "color": "#F5A200", "lat": 37.3672, "lon": 127.1084, "sgg": "성남시",
                    "transfer": True, "date": "2025-12-31", "operator": "한국철도공사"}


def test_stops_both_kinds_in_bbox(client):
    body = client.get("/api/stops", params={"bbox": "126.97,37.56,126.99,37.58"}).json()
    assert sorted(s["name"] for s in body["bus"]) == ["광화문", "종로2가"]
    assert (body["subway"], body["count"]) == ([], 2)


@pytest.mark.parametrize("params", [
    {},                                          # 기본 kinds 에 bus 가 있는데 bbox 없음
    {"kinds": "bus"},
    {"bbox": "127.1,37.3,127.2"},                # 숫자 3개
    {"bbox": "a,b,c,d"},
    {"bbox": "127.2,37.3,127.1,37.4"},           # min > max
    {"bbox": PANGYO_BOX, "kinds": "tram"},
    {"bbox": PANGYO_BOX, "limit": 0},
    {"bbox": PANGYO_BOX, "limit": 10001},
])
def test_stops_invalid_request(client, params):
    error(client.get("/api/stops", params=params), 400, "invalid_request")


@pytest.mark.parametrize("bbox", ["0,37,1e9,37", "-1e308,37,1e308,37", "127,-1e9,127,1e9", "0,0,1,1"])
def test_stops_huge_or_outside_bbox_is_clamped_and_fast(client, bbox):
    # 높이 0 인 거대 박스가 격자를 수십억 셀 돌던 결함 — 서비스 범위로 잘라 즉시 답한다
    import time
    t = time.perf_counter()
    r = client.get("/api/stops", params={"kinds": "bus,subway", "bbox": bbox})
    assert r.status_code == 200
    assert time.perf_counter() - t < 2.0


def test_stops_snap_distance_is_float(tmp_path, up):
    s = make_settings(tmp_path)
    csv_path = s.processed_dir / "bus_stops.csv"
    text = csv_path.read_text(encoding="utf-8-sig")
    csv_path.write_text(text.replace("contain,,GGB204000401,", "snap,412.5,GGB204000401,"), encoding="utf-8-sig")
    with serve(s, up) as c:
        body = c.get("/api/stops", params={"bbox": "127.119,37.364,127.120,37.365", "kinds": "bus"}).json()
    assert [(b["id"], b["snap_m"]) for b in body["bus"]] == [("204000401", 412.5)]


# --- /api/stops/{key}/routes · /api/routes/{id} (버스 노선 순서표) ---

def test_stop_routes(client):
    body = client.get("/api/stops/204000101/routes").json()
    assert body["stop"] == "204000101"
    assert [r["name"] for r in body["routes"]] == ["9-1", "10", "380"]
    assert body["routes"][2] == {"id": "204000901", "name": "380", "source": "gyeonggi", "type": "일반", "n_stops": 4}
    assert client.get("/api/stops/nope/routes").json() == {"stop": "nope", "routes": []}


def test_route_stops(client):
    body = client.get("/api/routes/204000901").json()
    assert (body["name"], body["n_stops"]) == ("380", 4)
    assert [s["name"] for s in body["stops"]] == ["야탑역", "분당구청", "판교테크노", "삼평동주민센터"]
    error(client.get("/api/routes/nope"), 404, "not_found")


def test_routes_without_route_table(tmp_path, up):
    s = make_settings(tmp_path)
    (s.processed_dir / "bus_route_stops.csv").unlink()
    with serve(s, up) as c:
        assert c.get("/api/health").json()["ready"] is True  # 정류소 DB 는 그대로 쓴다
        for path in ("/api/stops/204000101/routes", "/api/routes/204000901"):
            err = error(c.get(path), 503, "data_not_built")
            assert err["message"] == "버스 노선 순서표가 없습니다." and "build_bus_route_seq.py" in err["action"]


# --- /api/boundaries ---

def test_boundaries(client):
    r = client.get("/api/boundaries")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/geo+json"
    assert r.json() == BOUNDARIES


# --- /api/transit ---

def test_transit_happy_path(client, up):
    r = client.get("/api/transit", params={**OD, "probe": 1})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    no_keys(r.text)
    assert "SYNTHETIC" not in r.text  # 원문을 그대로 넘기지 않는다 (프로브도 키 이름만)
    body = r.json()
    assert (body["status"], body["message"], body["db_ready"]) == ("OK", None, True)
    assert body["summary"] == {"total": 2, "bus": 1, "subway": 1, "busAndSubway": 0}
    assert body["quota"]["transit"] == {"used": 1, "limit": 1000, "remaining": 999}
    assert body["quota"]["car"]["used"] == 0

    req = up.requests[0]
    assert (req.url.host, req.headers["Authorization"]) == ("dapi.kakao.com", f"KakaoAK {KKEY}")
    assert dict(req.url.params) == {"start_x": "127.0", "start_y": "37.266", "end_x": "127.1112", "end_y": "37.3947"}

    r0, r1 = body["routes"]
    s0, s1, s2 = r0["steps"]
    assert (s0["line_group"], s0["color"]) == ("수인분당선", "#F5A200")
    assert (s0["resolution"]["board"]["chosen"]["id"], s0["resolution"]["alight"]["chosen"]["id"]) \
        == ("bundang-K245", "bundang-K222")
    assert (s1["type"], s1["resolution"], s1["color"]) == ("WALKING", None, None)
    assert (s2["line_group"], s2["color"]) == ("신분당선", "#D4003B")
    assert (s2["resolution"]["board"]["chosen"]["id"], s2["resolution"]["alight"]["chosen"]["id"]) \
        == ("shinbundang-D12", "shinbundang-D13")
    bus = r1["steps"][0]["resolution"]
    assert (bus["board"]["chosen"]["id"], bus["board"]["match_level"], bus["board"]["name_conflict"]) \
        == ("100000201", "prefix", True)
    assert bus["alight"]["chosen"]["id"] == "100000202"
    # 정류소 전부의 위치 (DB 에 없는 기흥·종로1가는 선 위 추정: id·level 없음)
    assert [x["id"] for x in s0["stop_locs"]] == ["bundang-K245", None, "bundang-K222"]
    assert [x["id"] for x in s2["stop_locs"]] == ["shinbundang-D12", "shinbundang-D13"]
    assert [(x["id"], x["level"]) for x in r1["steps"][0]["stop_locs"]] \
        == [("100000201", "prefix"), (None, None), ("100000202", "key")]
    assert s1["stop_locs"] is None

    assert (r0["diag_summary"]["n"], r0["diag_summary"]["matched"]) == (4, 4)
    assert r1["diag_summary"]["levels"] == {"key": 1, "alias": 0, "prefix": 1}
    top = body["diag_summary"]
    assert (top["n"], top["matched"], top["match_rate"]) == (6, 6, 1.0)
    assert top["levels"] == {"key": 5, "alias": 0, "prefix": 1}
    assert (top["by_kind"]["bus"]["n"], top["by_kind"]["subway"]["n"]) == (2, 4)
    assert top["unmapped_vehicle_names"] == []

    probe = body["probe"]
    assert probe["n_routes"] == 2
    assert probe["step_type_counts"] == {"SUBWAY": 2, "WALKING": 1, "BUS": 1}

    assert "probe" not in client.get("/api/transit", params=OD).json()
    assert client.get("/api/quota").json()["transit"]["used"] == 2


def test_transit_diag_dedupe_and_unmapped(tmp_path, up):
    variant = copy.deepcopy(TRANSIT["routes"][0])
    variant["steps"][0]["properties"]["vehicles"][0]["name"] = "우이신설선"  # tiny line_groups 에 없는 노선
    raw = {**TRANSIT, "routes": [*TRANSIT["routes"], variant]}
    up.routes["dapi.kakao.com"] = lambda req: httpx.Response(200, json=raw)
    with serve(make_settings(tmp_path), up) as c:
        body = c.get("/api/transit", params=OD).json()
    per_route = [r["diag_summary"] for r in body["routes"]]
    assert [s["n"] for s in per_route] == [4, 2, 4]
    assert [s["unmapped_vehicle_names"] for s in per_route] == [[], [], ["우이신설선"]]
    # 3번째 경로의 승·하차는 1번째와 (종류, 이름 키, 기준점)이 같아 전체 요약에서 한 번만 센다
    assert body["diag_summary"]["n"] == 6
    assert body["diag_summary"]["unmapped_vehicle_names"] == ["우이신설선"]
    step = body["routes"][2]["steps"][0]
    assert (step["line_group"], step["color"], step["resolution"]["board"]["line_filter"]) == (None, None, "unmapped")


def test_transit_without_db(tmp_path, up):
    with serve(make_settings(tmp_path, with_db=False), up) as c:
        body = c.get("/api/transit", params=OD).json()
    assert (body["status"], body["db_ready"], body["diag_summary"]) == ("OK", False, None)
    assert len(body["routes"]) == 2
    assert all("resolution" not in s and "stop_locs" not in s for r in body["routes"] for s in r["steps"])


@pytest.mark.parametrize("status, message", [
    ("STARTNODES_NULL", "출발지 근처에서 대중교통 정류장을 찾지 못했습니다."),
    ("ENDNODES_NULL", "도착지 근처에서 대중교통 정류장을 찾지 못했습니다."),
    ("EQUAL_POINTS", "출발지와 도착지가 같습니다."),
    ("NO_RESULTS", "대중교통 경로가 없습니다."),
])
def test_transit_no_route_status(tmp_path, up, status, message):
    up.routes["dapi.kakao.com"] = lambda req: httpx.Response(200, json={"_note": NOTE, "status": status})
    with serve(make_settings(tmp_path), up) as c:
        r = c.get("/api/transit", params=OD)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert (body["status"], body["routes"], body["message"]) == (status, [], message)
    assert body["quota"]["transit"]["used"] == 1  # 카카오가 200 으로 답했으니 센다


def test_transit_invalid_request_status(tmp_path, up):
    up.routes["dapi.kakao.com"] = lambda req: httpx.Response(200, json={"_note": NOTE, "status": "INVALID_REQUEST"})
    with serve(make_settings(tmp_path), up) as c:
        error(c.get("/api/transit", params=OD), 400, "invalid_request")


def test_transit_map_disabled(tmp_path, up):
    up.routes["dapi.kakao.com"] = lambda req: httpx.Response(403, json={
        "_note": NOTE, "errorType": "NotAuthorizedError", "message": "App(바로가) disabled OPEN_MAP_AND_LOCAL service."})
    with serve(make_settings(tmp_path), up) as c:
        r = c.get("/api/transit", params=OD)
        err = error(r, 503, "kakao_map_disabled")
        assert err["upstream_status"] == 403
        assert "[카카오맵]" in err["action"] and "[사용 설정]" in err["action"]
        assert c.get("/api/quota").json()["transit"]["used"] == 0
    no_keys(r.text)


def test_local_quota_limit_blocks_second_call(tmp_path, up):
    with serve(make_settings(tmp_path, transit_daily_limit=1), up) as c:
        assert c.get("/api/transit", params=OD).status_code == 200
        err = error(c.get("/api/transit", params=OD), 429, "quota_exceeded")
        assert "(1건)" in err["message"]
        assert c.get("/api/quota").json()["transit"] == {"used": 1, "limit": 1, "remaining": 0}
    assert up.hosts() == ["dapi.kakao.com"]  # 두 번째 요청은 카카오를 부르지 않았다


@pytest.mark.parametrize("params", [
    {**OD, "sx": 200},          # 경도 범위 밖
    {**OD, "ey": 40.0},         # 위도 범위 밖
    {**OD, "sx": "abc"},
    {**OD, "sx": "nan"},
    {k: v for k, v in OD.items() if k != "ey"},
])
def test_invalid_coords(client, up, params):
    for path in ("/api/transit", "/api/car"):
        error(client.get(path, params=params), 400, "invalid_request")
    assert up.requests == []


# --- /api/car, /api/quota ---

def test_car(client, up):
    r = client.get("/api/car", params=OD)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    no_keys(r.text)
    body = r.json()
    assert (body["result_code"], body["distance_m"], body["duration_s"]) == (0, 12000, 1500)
    assert body["fare"] == {"taxi": 15000, "toll": 0}
    assert body["path"] == [[127.0, 37.26], [127.001, 37.261], [127.002, 37.262], [127.003, 37.263]]
    assert (body["quota"]["car"]["used"], body["quota"]["transit"]["used"]) == (1, 0)
    req = up.requests[0]
    assert req.url.host == "apis-navi.kakaomobility.com"
    assert dict(req.url.params) == {"origin": "127.0,37.266", "destination": "127.1112,37.3947"}


def test_quota(client):
    q = client.get("/api/quota").json()
    assert set(q) == {"date", "transit", "car", "keyword", "address"}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", q["date"])
    assert q["transit"] == {"used": 0, "limit": 1000, "remaining": 1000}
    assert q["car"] == {"used": 0, "limit": 10000, "remaining": 10000}
    assert q["keyword"] == q["address"] == {"used": 0, "limit": 100000, "remaining": 100000}


# --- /api/search ---

def test_search_merges_addresses_places_regions(client, up):
    r = client.get("/api/search", params={"q": "  합성  "})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    no_keys(r.text)
    assert "SYNTHETIC" not in r.text and "place.example" not in r.text  # 원문·카카오 URL 을 넘기지 않는다
    body = r.json()
    assert set(body) == {"items", "failed", "quota"}
    # 번지까지 맞는 주소 → 장소 → 지역 이름. 좌표가 없거나 범위 밖·이름 없는 장소는 빠진다
    assert [(i["kind"], i["name"]) for i in body["items"]] == [
        ("address", "경기 합성시 합성로 12"), ("address", "경기 합성시 합성동 3-1"),
        ("place", "합성역 합성선"), ("place", "합성테크노밸리"), ("address", "경기 합성시 합성동")]
    assert body["items"][0] == {"kind": "address", "name": "경기 합성시 합성로 12", "detail": "합성타워",
                                "address": "경기 합성시 합성동 1", "lat": 37.3947, "lon": 127.1112}
    assert body["failed"] == []
    assert (body["quota"]["keyword"]["used"], body["quota"]["address"]["used"]) == (1, 1)
    assert (body["quota"]["transit"]["used"], body["quota"]["car"]["used"]) == (0, 0)

    by_path = {q.url.path: q for q in up.requests}
    assert set(by_path) == {KEYWORD_PATH, ADDRESS_PATH}
    for q in by_path.values():
        assert (q.url.host, q.headers["Authorization"]) == ("dapi.kakao.com", f"KakaoAK {KKEY}")
    assert dict(by_path[KEYWORD_PATH].url.params) == {"query": "합성", "size": "10"}  # 앞뒤 공백을 걷어 보낸다
    assert dict(by_path[ADDRESS_PATH].url.params) == {"query": "합성", "size": "5"}


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": "   "}, {"q": "가" * 101}])
def test_search_invalid_query(client, up, params):
    error(client.get("/api/search", params=params), 400, "invalid_request")
    assert up.requests == []


def test_search_query_limit_is_characters(client, up):
    assert client.get("/api/search", params={"q": "가" * 100}).status_code == 200  # 카카오 한도는 글자 수 100


def test_search_partial_failure(tmp_path, up):
    up.routes[ADDRESS_PATH] = lambda req: httpx.Response(500, text="<html>error</html>")
    with serve(make_settings(tmp_path), up) as c:
        r = c.get("/api/search", params={"q": "합성"})
    assert r.status_code == 200
    body = r.json()
    assert [i["name"] for i in body["items"]] == ["합성역 합성선", "합성테크노밸리"]
    assert body["failed"] == [{"kind": "address", "code": "upstream_error",
                               "message": "카카오 주소 검색 요청이 실패했습니다 (HTTP 500)."}]


def test_search_both_fail_map_disabled(tmp_path, up):
    denied = lambda req: httpx.Response(403, json={  # noqa: E731
        "_note": NOTE, "errorType": "NotAuthorizedError", "message": "App(바로가) disabled OPEN_MAP_AND_LOCAL service."})
    up.routes[KEYWORD_PATH] = up.routes[ADDRESS_PATH] = denied
    with serve(make_settings(tmp_path), up) as c:
        r = c.get("/api/search", params={"q": "합성"})
        err = error(r, 503, "kakao_map_disabled")
        assert err["message"] == "카카오맵 서비스가 비활성화되어 있어 장소를 검색할 수 없습니다."
        assert "[카카오맵]" in err["action"]
        q = c.get("/api/quota").json()
        assert (q["keyword"]["used"], q["address"]["used"]) == (0, 0)  # 쿼터를 쓰지 않은 거절은 되돌린다
    no_keys(r.text)


def test_search_local_quota_blocks_only_that_kind(tmp_path, up):
    with serve(make_settings(tmp_path, keyword_daily_limit=1), up) as c:
        assert c.get("/api/search", params={"q": "합성"}).json()["failed"] == []
        body = c.get("/api/search", params={"q": "합성"}).json()
    assert [i["kind"] for i in body["items"]] == ["address"] * 3
    assert [(f["kind"], f["code"]) for f in body["failed"]] == [("keyword", "quota_exceeded")]
    assert "장소 검색" in body["failed"][0]["message"] and "(1건)" in body["failed"][0]["message"]
    assert [q.url.path for q in up.requests].count(KEYWORD_PATH) == 1  # 두 번째 키워드 검색은 카카오를 부르지 않았다
    assert body["quota"]["keyword"] == {"used": 1, "limit": 1, "remaining": 0}


# --- 타일 프록시 ---

def test_tile_proxy(client, up):
    r = client.get("/tiles/vworld/white/12/1581/3493")
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "public, max-age=259200"
    assert up.hosts() == ["api.vworld.kr"]
    assert VKEY.encode() not in r.content and all(VKEY not in v for v in r.headers.values())


# --- 약관 가드 ---

def files_under(root):
    return {p for p in root.rglob("*") if p.is_file()}


def test_tos_guard_nothing_kept(tmp_path, up, caplog):
    caplog.set_level(logging.DEBUG)
    s = make_settings(tmp_path)
    created = files_under(tmp_path)
    with serve(s, up) as c:
        assert c.get("/api/transit", params={**OD, "probe": 1}).status_code == 200
        assert c.get("/api/car", params=OD).status_code == 200
        assert c.get("/api/search", params={"q": "합성"}).status_code == 200
    assert files_under(tmp_path) == created | {s.quota_path}  # 카카오 응답은 디스크에 남지 않는다
    assert set(json.loads(s.quota_path.read_text(encoding="utf-8"))) == {"date", "used"}  # 건수만
    for text in (KKEY, "수인분당선 (수원 > 정자)", "합성로1", "합성타워", "합성테크노밸리"):
        assert text not in caplog.text  # 키·응답 본문을 로그에 남기지 않는다


# --- 정적 파일 (프론트엔드는 동시에 작성 중이라 없으면 건너뛴다) ---

def test_index_html(client):
    if not (FRONTEND / "index.html").is_file():
        pytest.skip("web/frontend/index.html 없음")
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>" in r.text


@pytest.mark.parametrize("path, ctype", [("js/app.js", "text/javascript"), ("css/app.css", "text/css")])
def test_static_mime(client, path, ctype):
    if not (FRONTEND / path).is_file():
        pytest.skip(f"web/frontend/{path} 없음")
    r = client.get("/" + path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(ctype)
    assert r.headers["cache-control"] == "no-cache"   # 고친 JS 가 F5 에서 바로 뜨게
