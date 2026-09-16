"""API 스모크 — 합성 픽스처 + httpx.MockTransport 만 쓴다 (카카오·VWorld 호출 없음, 실제 .env 미사용)."""
import copy
import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

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
DKEY = "fakedatagokrkey0123456789abcdefghij0123456789abcdefghij01234567"  # 가짜 키 (64자 영숫자)
SKEY = "fakeseoulsubwaylivekey1234567"                                    # 가짜 키 (30자)
NOTE = "SYNTHETIC - hand-written from documented schema; not a Kakao response"
NOTE_PUBLIC = "SYNTHETIC - hand-written from documented schema; not a real API response"
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

# 실시간 도착 — 원천 셋은 호스트가 서로 달라 호스트 키로 갈린다. 값은 tiny DB 의 노선·역에 맞춰 손으로 썼다.
GG_ARRIVALS = {"_note": NOTE_PUBLIC, "response": {
    "msgHeader": {"queryTime": "2026-09-12 13:30:09.952", "resultCode": 0,
                  "resultMessage": "정상적으로 처리되었습니다."},
    "msgBody": {"busArrivalList": [
        {"routeId": 204000901, "routeName": 380, "routeTypeCd": 13, "staOrder": 2, "stationId": 204000101,
         "flag": "PASS", "routeDestId": 204000401, "routeDestName": "야탑역",
         "predictTime1": 3, "locationNo1": 2, "crowded1": 2, "remainSeatCnt1": 12, "plateNo1": "경기70아1234",
         "predictTime2": 11, "locationNo2": 7, "crowded2": 3, "remainSeatCnt2": -1, "plateNo2": "경기70아5678"},
        {"routeId": 204000902, "routeName": 10, "staOrder": 1, "stationId": 204000101, "flag": "PASS",
         "routeDestName": "분당구청입구", "predictTime1": "", "predictTime2": "", "locationNo1": "",
         "crowded1": "", "remainSeatCnt1": "", "plateNo1": ""}]}}}   # 도착정보 없는 노선
SEOUL_ARRIVALS = {"_note": NOTE_PUBLIC,
                  "msgHeader": {"headerMsg": "정상적으로 처리되었습니다.", "headerCd": "0", "itemCount": 0},
                  "msgBody": {"itemList": [
                      {"stId": "100000201", "stNm": "종로2가", "arsId": "01201", "staOrd": "1",
                       "busRouteId": "100000901", "rtNm": "9", "routeType": "3",
                       "arrmsg1": "곧 도착", "traTime1": "115", "sectOrd1": "1", "isLast1": "0",
                       "plainNo1": "서울74사4169",
                       "arrmsg2": "5분32초후[2번째 전]", "traTime2": "332", "isLast2": "1"},
                      {"stId": "100000201", "rtNm": "99", "busRouteId": "100000902", "staOrd": "1",
                       "arrmsg1": "운행종료", "traTime1": "0", "sectOrd1": "0", "isLast1": "0",
                       "plainNo1": " "}]}}   # 운행종료는 traTime 0 으로 온다 (0초 후가 아니다)
def subway_arrivals(stamp=None):
    """지하철 합성 응답. `recptnDt` 는 부를 때 찍는다 — 도착 예측은 수신 시각 기준이라 값을 굳히면
    시간이 지날수록 '예측이 이미 지났다'(eta_s=None)가 되어 테스트가 무의미해진다."""
    got = stamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")   # 원천과 같은 지역시각 문자열
    return {"_note": NOTE_PUBLIC,
            "errorMessage": {"status": 200, "code": "INFO-000", "message": "정상 처리되었습니다.",
                             "link": "", "developerMessage": "", "total": 3},
            "realtimeArrivalList": [
        {"subwayId": "1075", "statnNm": "정자", "updnLine": "상행",
         "trainLineNm": "청량리행 - 수내방면", "barvlDt": "240", "arvlCd": "99",
         "arvlMsg2": "[2]번째 전역 (수내)", "arvlMsg3": "수내", "btrainSttus": "일반",
         "btrainNo": "K1234", "bstatnNm": "청량리", "recptnDt": got},
        {"subwayId": "1075", "statnNm": "정자", "updnLine": "하행",
         "trainLineNm": "수원행 - 미금방면", "barvlDt": "0", "arvlCd": "2",
         "arvlMsg2": "정자역 출발", "btrainSttus": "급행", "btrainNo": "K3080",
         "bstatnNm": "수원", "recptnDt": got},
        {"subwayId": "1077", "statnNm": "정자", "updnLine": "하행",
         "trainLineNm": "광교행 - 판교방면", "barvlDt": "60", "arvlCd": "1",
         "arvlMsg2": "[1]번째 전역 (판교)", "btrainSttus": "일반", "btrainNo": "D5678",
         "bstatnNm": "광교", "recptnDt": got}]}


class Upstream:
    """호스트별(로컬 검색은 경로별) 합성 응답. 테스트가 `routes[host 또는 path]` 를 바꿔 오류를 흉내 낸다."""

    def __init__(self):
        self.routes = {
            "dapi.kakao.com": lambda req: httpx.Response(200, json=TRANSIT),
            KEYWORD_PATH: lambda req: httpx.Response(200, json=KEYWORD),
            ADDRESS_PATH: lambda req: httpx.Response(200, json=ADDRESS),
            "apis-navi.kakaomobility.com": lambda req: httpx.Response(200, json=CAR),
            "api.vworld.kr": lambda req: httpx.Response(200, content=PNG, headers={"content-type": "image/png"}),
            # 실시간 도착 세 원천 — 지하철은 경로에 키·역명이 들어가 경로 키로는 못 잡는다
            "apis.data.go.kr": lambda req: httpx.Response(200, json=GG_ARRIVALS),
            "ws.bus.go.kr": lambda req: httpx.Response(200, json=SEOUL_ARRIVALS),
            "swopenapi.seoul.go.kr": lambda req: httpx.Response(200, json=subway_arrivals()),
        }
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return (self.routes.get(request.url.path) or self.routes[request.url.host])(request)

    def hosts(self):
        return [r.url.host for r in self.requests]


def make_settings(tmp_path, with_db=True, with_headway=True, **overrides):
    processed, ref = tmp_path / "processed", tmp_path / "ref"
    processed.mkdir()
    ref.mkdir()
    if with_db:
        shutil.copy(TINY / "bus_stops.csv", processed)
        shutil.copy(TINY / "subway_stations.csv", processed)
        shutil.copy(TINY / "bus_route_stops.csv", processed)
        shutil.copy(TINY / "subway_line_seq.csv", processed)
        shutil.copy(TINY / "subway_live_stations.csv", processed)
        shutil.copy(TINY / "line_groups.csv", ref)
        if with_headway:
            shutil.copy(TINY / "headway_bus.csv", processed)
            shutil.copy(TINY / "headway_rail.csv", processed)
        (processed / "admin_sgg_web.geojson").write_text(json.dumps(BOUNDARIES), encoding="utf-8")
    fields = dict(repo_root=tmp_path, processed_dir=processed, ref_dir=ref, frontend_dir=FRONTEND,
                  quota_path=tmp_path / "var" / "quota.json", kakao_rest_key=KKEY, vworld_key=VKEY,
                  data_go_kr_key=DKEY, seoul_subway_live_key=SKEY)
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
    assert all(k not in text for k in (KKEY, VKEY, DKEY, SKEY))


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
    assert (h["ready"], h["bus_stops"], h["subway_stations"]) == (True, 17, 5)
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
    assert (body["count"], body["truncated"], body["bus"]) == (5, False, [])
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
    # 정류소 전부의 위치. 도시철도 구간은 역 순서표에서 읽는다 — 옛 이름 '신길온천' 도 제자리에 놓인다(DB 는 '능길')
    assert [(x["id"], x["level"]) for x in s0["stop_locs"]]         == [("bundang-K245", "route"), ("bundang-K240", "route"), ("bundang-K222", "route")]
    # 버스는 순서표에 없는 노선이라(470) 이름·기하로 찾는다 — DB 에 없는 종로1가는 선 위 추정(id·level 없음)
    assert [x["id"] for x in s2["stop_locs"]] == ["shinbundang-D12", "shinbundang-D13"]
    assert [(x["id"], x["level"]) for x in r1["steps"][0]["stop_locs"]] \
        == [("100000201", "prefix"), (None, None), ("100000202", "key")]
    assert s1["stop_locs"] is None

    assert (r0["diag_summary"]["n"], r0["diag_summary"]["matched"]) == (4, 4)
    assert r1["diag_summary"]["levels"] == {"key": 1, "alias": 0, "parts": 0, "prefix": 1, "route": 0}
    top = body["diag_summary"]
    assert (top["n"], top["matched"], top["match_rate"]) == (6, 6, 1.0)
    assert top["levels"] == {"key": 5, "alias": 0, "parts": 0, "prefix": 1, "route": 0}
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


# 순서표에 있는 노선(380)으로 만든 버스 경로 — 배차표에서 대기시간을 찾으려면 노선이 가려져야 한다
BUS_380 = {"properties": {"type": "BUS", "totalTime": 780, "totalDistance": 4200, "transfers": 0,
                          "fare": {"value": 1450}},
           "steps": [{"properties": {"type": "BUS", "time": 780, "distance": 4200,
                                     "guidance": "380 (분당구청 > 판교테크노)",
                                     "vehicles": [{"type": "BUS", "name": "380"}],
                                     "stops": [{"name": "분당구청"}, {"name": "판교테크노"}]},
                      "path": {"points": [[127.1190, 37.3825], [127.1123, 37.3948]]}}]}


def test_transit_wait_from_headway(tmp_path, up):
    """카카오 시간에는 대기가 없다 — 배차(380 은 10~20분, 중간 15분)의 절반을 더한 값도 함께 준다.
    도착정보가 비면(원천 resultCode 4) 첫 승차도 배차 추정으로 남는다."""
    up.routes["dapi.kakao.com"] = lambda req: httpx.Response(200, json={**TRANSIT, "routes": [BUS_380]})
    up.routes["apis.data.go.kr"] = lambda req: httpx.Response(200, json=GG_EMPTY)
    with serve(make_settings(tmp_path), up) as c:
        body = c.get("/api/transit", params=OD).json()
    assert body["wait_basis"]["day_type"] in ("평일", "토요일", "일요일")
    r = body["routes"][0]
    step = r["steps"][0]
    assert step["route_ids"] == ["204000901"]
    assert (step["headway_m"], step["wait_s"]) == (15.0, 450)
    assert (r["wait_s"], r["total_with_wait_s"]) == (450, 1230)


GG_EMPTY = {"_note": NOTE_PUBLIC, "response": {"msgHeader": {"resultCode": 4, "resultMessage": "결과가 존재하지 않습니다."}}}


def test_transit_first_wait_uses_realtime(tmp_path, up):
    """[경로 검색] 결과의 첫 승차 대기는 실시간 도착정보로 — 정류장까지 걸어가는 시간을 빼고 그 뒤 첫 차까지.
    380 은 모의 경기 도착정보에서 3분 뒤(예측 초)라 출처가 realtime 이다."""
    from algo import anchors
    from geoutil import haversine_m
    up.routes["dapi.kakao.com"] = lambda req: httpx.Response(200, json={**TRANSIT, "routes": [BUS_380]})
    near = {**OD, "sx": 127.1190, "sy": 37.3822}   # 분당구청 정류장 코앞에서 출발
    with serve(make_settings(tmp_path), up) as c:
        body = c.get("/api/transit", params=near).json()
    route = body["routes"][0]
    step = route["steps"][0]
    board = step["resolution"]["board"]["chosen"]
    walk = anchors.walk_s(haversine_m(near["sx"], near["sy"], board["lon"], board["lat"]))
    assert (step["wait_source"], step["static_wait_s"], step["walk_to_stop_s"]) == ("realtime", 450, round(walk))
    assert step["wait_s"] == round(180 - walk)
    assert (route["wait_s"], route["total_with_wait_s"]) == (step["wait_s"], 780 + step["wait_s"])
    assert body["realtime"]["realtime"] == 1


def test_transit_realtime_gets_subway_upstream_times(tmp_path, up, monkeypatch):
    """남은 역 수로 도착을 어림할 때 쓰는 '열차가 지나올 역 사이 누적 시간' 을 방면과 같은 (승차역, 하차역) 키로 넘긴다.
    합성 경로의 첫 승차는 수인분당선 수원 → 정자 — 순서표로 읽을 수 있는 구간이다."""
    from algo import anchors
    seen = {}
    real = anchors.with_realtime_first_wait

    def spy(routes, o, arrivals, toward=None, upstream=None):
        seen.update(toward=toward, upstream=upstream)
        return real(routes, o, arrivals, toward, upstream)

    monkeypatch.setattr(anchors, "with_realtime_first_wait", spy)
    with serve(make_settings(tmp_path), up) as c:
        assert c.get("/api/transit", params=OD).status_code == 200
    pair = ("bundang-K245", "bundang-K222")
    assert pair in seen["toward"] and set(seen["upstream"]) == set(seen["toward"])
    assert seen["upstream"][pair] == anchors.subway_upstream_s(c.app.state.lines, *pair)


def test_transit_wait_is_empty_when_route_is_not_in_the_table(tmp_path, up):
    """합성 경로의 버스 470 은 순서표에 없어 노선을 못 가린다 → 경로 합은 주지 않는다."""
    with serve(make_settings(tmp_path), up) as c:
        body = c.get("/api/transit", params=OD).json()
    bus = body["routes"][1]
    assert (bus["steps"][0]["route_ids"], bus["steps"][0]["wait_s"]) == ([], None)
    assert (bus["wait_s"], bus["total_with_wait_s"]) == (None, None)


def test_transit_without_headway_table(tmp_path, up):
    with serve(make_settings(tmp_path, with_headway=False), up) as c:
        body = c.get("/api/transit", params=OD).json()
    assert body["wait_basis"] is None
    assert all("wait_s" not in r for r in body["routes"])


# 하이브리드 — tiny 노선 380 이 지나는 야탑역 → 판교테크노 (앵커 후보가 나오는 좌표)
HYB_OD = {"sx": 127.1195, "sy": 37.3645, "ex": 127.1123, "ey": 37.3948}


def test_hybrid_plan(tmp_path, up):
    with serve(make_settings(tmp_path), up) as c:
        r = c.get("/api/hybrid", params={**HYB_OD, "top": 2})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    # 선호는 시간 중시로 고정, 기준선은 같은 잣대로 잰 최소 시간 대중교통 경로
    from algo import anchors
    assert body["pref"] == "time" and body["vot"] == anchors.VOT["time"]
    assert "comparisons" not in body
    base = body["baseline"]
    assert base["label"] == "최소 시간 경로" and base["time_s"] > 0 and base["route"]["steps"]
    assert body["diag"]["picked"] <= 2 and body["diag"]["verified"] <= body["diag"]["picked"]
    for row in body["routes"]:
        assert row["hybrid"] in ("A", "B")
        assert row["anchor"]["name"] and row["anchor"]["lat"] and row["anchor"]["lon"]
        assert row["time_s"] > 0 and row["fare"] >= 0 and row["taxi"]["path"]
        assert isinstance(row["pareto"], bool) and isinstance(row["recommended"], bool)
        assert row["transit"] is None or row["transit"]["steps"]
    assert body["quota"]["transit"]["used"] >= 1   # 기준 경로만도 1콜
    # 검증 뒤 기준선보다 5분 이상 빠르지 않은 후보는 뺀다
    assert body["diag"]["too_little_saving"] >= 0 and body["diag"]["min_saving_s"] == 300
    assert all(base["time_s"] - row["time_s"] >= 300 for row in body["routes"])
    no_keys(r.text)


def test_hybrid_asks_for_both_taxi_directions(tmp_path, up, monkeypatch):
    """(b) 역추적을 first-mile(A)·last-mile(B) 두 방향으로 돌린다 — (a) 섭동이 내는 B 와는 다른 후보다."""
    from algo import anchors
    seen = []
    real = anchors.propose_backtrack

    def spy(*args, **kw):
        seen.append(kw.get("hybrid", "A"))
        return real(*args, **kw)

    merged = []
    real_merge = anchors.merge_candidates

    def merge_spy(*groups, top, **kw):
        merged.append(len(groups))
        return real_merge(*groups, top=top, **kw)

    monkeypatch.setattr(anchors, "propose_backtrack", spy)
    monkeypatch.setattr(anchors, "merge_candidates", merge_spy)
    with serve(make_settings(tmp_path), up) as c:
        body = c.get("/api/hybrid", params={**HYB_OD, "top": 2}).json()
    assert sorted(seen[:2]) == ["A", "B"]   # 그 뒤로는 C 가 돌아가는 정류장마다 A 방향으로 다시 부를 수 있다
    assert body["diag"]["backtrack_a"]["hybrid"] == "A" and body["diag"]["backtrack_b"]["hybrid"] == "B"
    # D 후보도 함께 내고, 병합은 algo 규칙(택시 양끝 비교)으로 한다 — (b)A · (b)B · (a) · D 네 묶음
    assert "anchors" in body["diag"]["gap"]
    assert merged and merged[0] == 4   # C 는 예산을 따로 둬 한 번 더 병합할 수 있다
    assert "detour" in body["diag"]


def test_hybrid_uses_realtime_first_wait_for_baseline_and_gap(tmp_path, up, monkeypatch):
    """첫 승차 대기를 실시간으로 바꾼 경로를 기준선 · D · (a) 의 B 에 쓰고, (a) 의 A 에만 원래 경로를 준다 (algo/cli 와 같은 규칙)."""
    from algo import anchors
    seen = {}
    real_rt, real_gap, real_perturb = anchors.with_realtime_first_wait, anchors.propose_gap, anchors.propose_perturb

    def rt_spy(routes, o, arrivals, toward=None, upstream=None):
        out = real_rt(routes, o, arrivals, toward, upstream)
        seen.update(rt_in=routes, rt_out=out[0], arrivals=arrivals)
        return out

    def gap_spy(o, d, routes, **kw):
        seen["gap"] = routes
        return real_gap(o, d, routes, **kw)

    def perturb_spy(o, d, routes, **kw):
        seen.setdefault("perturb", {})[kw.get("hybrids")] = routes
        return real_perturb(o, d, routes, **kw)

    for name, spy in (("with_realtime_first_wait", rt_spy), ("propose_gap", gap_spy), ("propose_perturb", perturb_spy)):
        monkeypatch.setattr(anchors, name, spy)
    with serve(make_settings(tmp_path), up) as c:
        body = c.get("/api/hybrid", params={**HYB_OD, "top": 2}).json()
    assert seen["gap"] is seen["rt_out"]        # D 는 실시간 사본에서
    # (a) 는 A·B 를 나눠 부른다 — A 는 택시로 정류장에 닿아 원래 경로, B 는 걸어서 닿아 실시간 사본
    assert seen["perturb"][("A",)] is seen["rt_in"]
    assert seen["perturb"][("B",)] is seen["rt_out"]
    assert "perturb_a" in body["diag"] and "perturb_b" in body["diag"]
    assert seen["arrivals"]                     # 첫 승차 정류장·역의 도착정보를 받아 넘겼다
    assert set(up.hosts()) & {"ws.bus.go.kr", "apis.data.go.kr", "swopenapi.seoul.go.kr"}
    assert body["diag"]["realtime"] is not None


def test_hybrid_reuses_the_search_result(tmp_path, up, monkeypatch):
    """[경로 검색] 결과를 본문으로 보내면 기준 경로(출발 → 도착 대중교통)를 다시 부르지 않는다."""
    import app.api as api_mod
    od = (HYB_OD["sx"], HYB_OD["sy"], HYB_OD["ex"], HYB_OD["ey"])
    with serve(make_settings(tmp_path), up) as c:
        routes = c.get("/api/transit", params=HYB_OD).json()["routes"]
        calls = []
        real = api_mod.transit_payload

        async def spy(st, sx, sy, ex, ey, *args, **kw):
            calls.append((sx, sy, ex, ey))
            return await real(st, sx, sy, ex, ey, *args, **kw)

        monkeypatch.setattr(api_mod, "transit_payload", spy)
        r = c.post("/api/hybrid", params={**HYB_OD, "top": 2}, json={"routes": routes})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["diag"]["base_source"] == "reused"
    assert od not in calls          # 앵커에서 다시 부르는 구간(A · (b) 의 B · C)만 남는다
    assert body["baseline"]["route"]["steps"]


def test_hybrid_get_still_calls_the_base_route(client):
    assert client.get("/api/hybrid", params={**HYB_OD, "top": 1}).json()["diag"]["base_source"] == "called"


@pytest.mark.parametrize("payload", [{"routes": "x"}, {"routes": []}, {"routes": [{"steps": [{"type": "BUS"}]}]}, ["routes"]])
def test_hybrid_reuse_rejects_a_bad_body(client, payload):
    error(client.post("/api/hybrid", params=HYB_OD, json=payload), 400, "invalid_request")


def test_hybrid_ignores_pref(client):
    """선호는 더 받지 않는다 — 옛 화면이 보내도 시간 중시로 찾는다."""
    body = client.get("/api/hybrid", params={**HYB_OD, "top": 1, "pref": "cost"}).json()
    assert body["pref"] == "time"


def test_hybrid_without_db(tmp_path, up):
    with serve(make_settings(tmp_path, with_db=False), up) as c:
        error(c.get("/api/hybrid", params=HYB_OD), 503, "data_not_built")


def test_hybrid_without_headway_table(tmp_path, up):
    """배차표가 없으면 앵커 표(첫·막차)도 없다 — 하이브리드만 막히고 나머지는 그대로다."""
    with serve(make_settings(tmp_path, with_headway=False), up) as c:
        error(c.get("/api/hybrid", params=HYB_OD), 503, "data_not_built")
        assert c.get("/api/transit", params=OD).status_code == 200


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
    # 두 번째 요청은 카카오를 부르지 않았다 (첫 요청의 첫 승차 도착정보 원천은 세지 않는다)
    assert [h for h in up.hosts() if "kakao" in h] == ["dapi.kakao.com"]


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
    assert set(q) == {"date", "transit", "car", "keyword", "address",
                      "gyeonggi_bus", "seoul_bus", "seoul_subway"}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", q["date"])
    assert q["transit"] == {"used": 0, "limit": 1000, "remaining": 1000}
    assert q["car"] == {"used": 0, "limit": 10000, "remaining": 10000}
    assert q["keyword"] == q["address"] == {"used": 0, "limit": 100000, "remaining": 100000}
    # 도착정보 세 원천은 개발계정 한도와 같게 1,000건/일
    assert q["gyeonggi_bus"] == q["seoul_bus"] == q["seoul_subway"] == {"used": 0, "limit": 1000, "remaining": 1000}


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


# --- /api/arrivals/stop (버스 정류장 실시간 도착) ---

BUS_ITEM_KEYS = {"source", "route_id", "route_name", "route_type", "eta_s", "n_stops_ahead", "crowding",
                 "seats", "is_last", "vehicle", "message", "dest"}
RAIL_ITEM_KEYS = {"direction", "eta_s", "age_s", "n_stops_ahead", "message", "train_type", "dest",
                  "headsign", "train_no"}


def test_arrivals_stop_merges_both_bis(client, up):
    r = client.get("/api/arrivals/stop/204000101")   # source_ids = GGB…|SEB… (두 BIS 가 등록한 정류장)
    assert r.status_code == 200
    assert "cache-control" not in r.headers   # no-store 는 카카오 경유 응답에만 붙인다 (공공데이터는 안 붙인다)
    no_keys(r.text)
    assert "SYNTHETIC" not in r.text    # 원문을 그대로 넘기지 않는다
    body = r.json()
    assert set(body) == {"stop", "name", "items", "failed", "quota"}
    assert (body["stop"], body["name"], body["failed"]) == ("204000101", "분당구청", [])
    assert set(body["items"][0]) == BUS_ITEM_KEYS
    # 두 원천을 도착 임박 순으로 섞고, 값 없는 항목(도착정보 없음·운행종료)은 뒤로 보낸다
    assert [(i["source"], i["route_name"], i["eta_s"]) for i in body["items"]] == [
        ("seoul", "9", 115), ("gyeonggi", "380", 180), ("seoul", "9", 332), ("gyeonggi", "380", 660),
        ("gyeonggi", "10", None), ("seoul", "99", None)]
    assert body["items"][1] == {"source": "gyeonggi", "route_id": "204000901", "route_name": "380",
                                "route_type": "일반", "eta_s": 180, "n_stops_ahead": 2, "crowding": 2,
                                "seats": 12, "is_last": None, "vehicle": "경기70아1234", "message": None,
                                "dest": "야탑역"}
    assert body["items"][2] == {"source": "seoul", "route_id": "100000901", "route_name": "9",
                                "route_type": None, "eta_s": 332, "n_stops_ahead": 2, "crowding": None,
                                "seats": None, "is_last": True, "vehicle": None,
                                "message": "5분32초후[2번째 전]", "dest": None}
    assert [i["message"] for i in body["items"][4:]] == ["도착 정보가 없습니다.", "운행종료"]
    assert (body["quota"]["gyeonggi_bus"]["used"], body["quota"]["seoul_bus"]["used"]) == (1, 1)
    assert body["quota"]["seoul_subway"]["used"] == 0
    # 업스트림 정류소 id 는 source_ids 에서 접두어를 뗀 값이다 (stop_key 를 그대로 쓰지 않는다)
    by_host = {q.url.host: q for q in up.requests}
    assert set(by_host) == {"apis.data.go.kr", "ws.bus.go.kr"}
    assert dict(by_host["apis.data.go.kr"].url.params) == {"format": "json", "stationId": "204000101",
                                                           "serviceKey": DKEY}
    assert dict(by_host["ws.bus.go.kr"].url.params) == {"stId": "204000101", "resultType": "json",
                                                        "serviceKey": DKEY}


def test_arrivals_stop_single_bis(tmp_path, up):
    with serve(make_settings(tmp_path), up) as c:
        gg = c.get("/api/arrivals/stop/204000102").json()      # GGB 만 등록
        seoul = c.get("/api/arrivals/stop/100000201").json()   # SEB 만 등록
    assert up.hosts() == ["apis.data.go.kr", "ws.bus.go.kr"]   # 등록한 BIS 만 부른다
    assert (gg["name"], gg["failed"]) == ("분당구청", [])
    assert {i["source"] for i in gg["items"]} == {"gyeonggi"}
    # 서울 응답도 busRouteId 를 주므로 순서표에서 노선 유형을 채운다
    assert [(i["route_name"], i["route_type"]) for i in seoul["items"]] \
        == [("9", "마을"), ("9", "마을"), ("99", "마을")]
    assert (seoul["quota"]["gyeonggi_bus"]["used"], seoul["quota"]["seoul_bus"]["used"]) == (1, 1)


def test_arrivals_stop_partial_failure(tmp_path, up):
    # 서울 버스 실측: 미신청 키·키 오류는 HTTP 401 {"error": {...}}
    up.routes["ws.bus.go.kr"] = lambda req: httpx.Response(401, json={"error": {"code": "401"}})
    with serve(make_settings(tmp_path), up) as c:
        r = c.get("/api/arrivals/stop/204000101")
        q = c.get("/api/quota").json()
    assert r.status_code == 200
    body = r.json()
    assert {i["source"] for i in body["items"]} == {"gyeonggi"}
    assert body["failed"] == [{"source": "seoul", "code": "arrivals_auth",
                               "message": "서울 버스 도착정보 원천이 API 키를 거부했습니다."}]
    assert (q["gyeonggi_bus"]["used"], q["seoul_bus"]["used"]) == (1, 0)  # 쿼터를 쓰지 않은 거절은 되돌린다
    no_keys(r.text)


def test_arrivals_stop_both_fail(tmp_path, up):
    fail = lambda req: httpx.Response(500, text="<html>error</html>")  # noqa: E731
    up.routes["apis.data.go.kr"] = up.routes["ws.bus.go.kr"] = fail
    with serve(make_settings(tmp_path), up) as c:
        r = c.get("/api/arrivals/stop/204000101")
    err = error(r, 502, "upstream_error")   # 둘 다 실패하면 첫 원천(경기)의 오류를 봉투로 올린다
    assert err["message"] == "경기 버스 도착정보 요청이 실패했습니다 (HTTP 500)."
    assert err["upstream_status"] == 500
    no_keys(r.text)


@pytest.mark.parametrize("stop_key, code, message", [
    ("204000103", "no_realtime", "미정차 정류소에는 실시간 도착 정보가 없습니다."),
    ("nope", "not_found", "정류장을 찾을 수 없습니다."),
])
def test_arrivals_stop_rejected_before_upstream(client, up, stop_key, code, message):
    err = error(client.get(f"/api/arrivals/stop/{stop_key}"), 404, code)
    assert err["message"] == message
    assert up.requests == []


def test_arrivals_stop_quota_blocks_only_that_source(tmp_path, up):
    with serve(make_settings(tmp_path, gyeonggi_bus_daily_limit=1), up) as c:
        assert c.get("/api/arrivals/stop/204000101").json()["failed"] == []
        body = c.get("/api/arrivals/stop/204000301").json()   # 다른 정류장 — 캐시가 아니라 새 호출
    assert {i["source"] for i in body["items"]} == {"seoul"}
    assert [(f["source"], f["code"]) for f in body["failed"]] == [("gyeonggi", "quota_exceeded")]
    assert "경기 버스 도착정보" in body["failed"][0]["message"] and "(1건)" in body["failed"][0]["message"]
    assert up.hosts().count("apis.data.go.kr") == 1   # 두 번째 요청은 원천을 부르지 않았다
    assert body["quota"]["gyeonggi_bus"] == {"used": 1, "limit": 1, "remaining": 0}


def test_arrivals_stop_cache_saves_quota(client, up):
    first = client.get("/api/arrivals/stop/204000101").json()
    again = client.get("/api/arrivals/stop/204000101").json()
    assert again["items"] == first["items"]
    assert (again["quota"]["gyeonggi_bus"]["used"], again["quota"]["seoul_bus"]["used"]) == (1, 1)
    assert sorted(up.hosts()) == ["apis.data.go.kr", "ws.bus.go.kr"]  # 15초 캐시 — 두 번째는 부르지 않았다


def test_arrivals_zero_ttl_always_calls(tmp_path, up):
    with serve(make_settings(tmp_path, arrivals_cache_ttl_s=0), up) as c:
        c.get("/api/arrivals/stop/204000101")
        stop = c.get("/api/arrivals/stop/204000101").json()
        c.get("/api/arrivals/station/bundang-K222")
        station = c.get("/api/arrivals/station/bundang-K222").json()
    assert [up.hosts().count(h) for h in ("apis.data.go.kr", "ws.bus.go.kr", "swopenapi.seoul.go.kr")] == [2, 2, 2]
    assert stop["quota"]["gyeonggi_bus"]["used"] == 2
    assert station["quota"]["seoul_subway"]["used"] == 2


def test_arrivals_without_db(tmp_path, up):
    with serve(make_settings(tmp_path, with_db=False), up) as c:
        for path in ("/api/arrivals/stop/204000101", "/api/arrivals/station/bundang-K222"):
            err = error(c.get(path), 503, "data_not_built")
            assert err["message"] == "정제 데이터가 없습니다."
    assert up.requests == []


# --- /api/arrivals/station (도시철도 역 실시간 도착) ---

def test_arrivals_station_filters_by_line(client, up):
    r = client.get("/api/arrivals/station/bundang-K222")
    assert r.status_code == 200
    no_keys(r.text)
    assert "SYNTHETIC" not in r.text
    body = r.json()
    assert set(body) == {"station", "name", "line_group", "items", "failed", "quota"}
    assert (body["station"], body["name"], body["line_group"], body["failed"]) \
        == ("bundang-K222", "정자", "수인분당선", [])   # 원천이 하나라 failed 는 모양만 맞춘 빈 목록
    assert set(body["items"][0]) == RAIL_ITEM_KEYS
    # 예측이 있는 열차가 먼저, '출발'(이 역을 떠난 열차)은 eta 가 없어 뒤로 간다
    assert [(i["direction"], i["eta_s"], i["n_stops_ahead"], i["message"]) for i in body["items"]] == [
        ("상행", 240, 2, "[2]번째 전역 (수내)"),
        ("하행", None, None, "정자역 출발")]
    assert [i["train_no"] for i in body["items"]] == ["K1234", "K3080"]
    assert all(i["age_s"] == 0 for i in body["items"])      # 방금 수신한 값 → 보정 0
    assert body["quota"]["seoul_subway"]["used"] == 1
    # 환승역은 역명이 같아 한 응답에 여러 노선이 섞여 온다 — 걸러 쓰므로 노선이 늘어도 쿼터는 한 건이다
    other = client.get("/api/arrivals/station/shinbundang-D12").json()
    assert (other["line_group"], [i["dest"] for i in other["items"]]) == ("신분당선", ["광교"])
    assert other["quota"]["seoul_subway"]["used"] == 1
    assert up.hosts() == ["swopenapi.seoul.go.kr"]
    # 페이지 크기는 넉넉히 — 20 이면 노선이 여럿 걸친 역(서울역 total=22)에서 뒤 노선이 잘린다
    assert all(unquote(q.url.path).endswith("/0/60/정자") for q in up.requests)


def test_arrivals_station_uses_live_name(client, up):
    body = client.get("/api/arrivals/station/bundang-K240").json()   # DB '능길' ↔ 실시간 '신길온천' (실측 사례)
    assert body["name"] == "능길"
    assert unquote(up.requests[0].url.path).endswith("/0/60/신길온천")
    assert SKEY in unquote(up.requests[0].url.path)          # 지하철은 전용 키를 경로에 넣는다
    no_keys(json.dumps(body, ensure_ascii=False))            # 그래도 응답에는 새지 않는다


def test_arrivals_station_no_realtime_and_not_found(client, up):
    err = error(client.get("/api/arrivals/station/bundang-K245"), 404, "no_realtime")  # 매핑표에 실시간 이름이 없다
    assert err["message"] == "이 역은 실시간 도착 정보를 제공하지 않습니다."
    assert "headway_rail.csv" in err["action"] and "headway_bus.csv" in err["action"]
    assert error(client.get("/api/arrivals/station/nope"), 404, "not_found")["message"] == "역을 찾을 수 없습니다."
    assert up.requests == []


def test_arrivals_station_without_map(tmp_path, up):
    s = make_settings(tmp_path)
    (s.processed_dir / "subway_live_stations.csv").unlink()
    with serve(s, up) as c:
        err = error(c.get("/api/arrivals/station/bundang-K222"), 503, "data_not_built")
        assert err["message"] == "도시철도 실시간 역 매핑표가 없습니다."
        assert "build_subway_live_map.py" in err["action"]
        assert c.get("/api/health").json()["ready"] is True            # 나머지 데이터는 그대로 쓴다
        assert c.get("/api/arrivals/stop/204000101").status_code == 200  # 버스 도착은 매핑표를 쓰지 않는다


@pytest.mark.parametrize("path, field, env", [
    ("/api/arrivals/stop/204000101", "data_go_kr_key", "DATA_GO_KR_API_KEY"),
    ("/api/arrivals/station/bundang-K222", "seoul_subway_live_key", "SEOUL_SUBWAY_LIVE_API"),
])
def test_arrivals_missing_key(tmp_path, up, path, field, env):
    with serve(make_settings(tmp_path, **{field: None}), up) as c:
        err = error(c.get(path), 503, "missing_key")
    assert env in err["action"]
    assert up.requests == []   # 키가 없으면 원천을 부르지 않는다


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
        # 공공데이터 도착정보는 저장이 허용되지만 15초 캐시는 메모리 전용이다 (디스크에는 건수만)
        assert c.get("/api/arrivals/stop/204000101").status_code == 200
        assert c.get("/api/arrivals/station/bundang-K222").status_code == 200
    assert files_under(tmp_path) == created | {s.quota_path}  # 카카오 응답은 디스크에 남지 않는다
    assert set(json.loads(s.quota_path.read_text(encoding="utf-8"))) == {"date", "used"}  # 건수만
    for text in (KKEY, DKEY, SKEY, "수인분당선 (수원 > 정자)", "합성로1", "합성타워", "합성테크노밸리",
                 "경기70아1234", "운행종료", "정자역 출발"):
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
