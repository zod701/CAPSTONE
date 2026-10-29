"""공식 인천 노선 XML 스키마의 합성 데이터로 수집·DB 병합 계약을 검증한다."""
import io
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import pytest

import fetch_incheon_routes as fetch
from build_bus_route_seq import build, normalize_incheon
from incheon_api import ROUTE_TYPES


ROUTES = pd.DataFrame([{"ROUTEID": "165000004", "ROUTENO": "4", "ROUTETPCD": "2"}])
STOPS = pd.DataFrame([
    {"stop_key": "165000111", "name": "다른 승강장", "lat": "37.4", "lon": "126.7",
     "source_ids": "GGB165000111", "is_virtual": "0"},
    {"stop_key": "165000111-ICB", "name": "인천 승강장", "lat": "37.5", "lon": "126.6",
     "source_ids": "ICB165000111", "is_virtual": "0"},
])
SECTION = {"ROUTEID": "165000004", "BSTOPID": "165000111", "BSTOPSEQ": "1", "PATHSEQ": "8",
           "DIRCD": "0", "BSTOPNM": "인천 승강장", "POSX": "155069.958606", "POSY": "470909.093441"}


def test_incheon_uses_icb_platform_and_preserves_sequence_gaps():
    sections = pd.DataFrame([SECTION, {**SECTION, "BSTOPSEQ": "2", "BSTOPID": "169999999"},
                             {**SECTION, "BSTOPSEQ": "3", "DIRCD": "1"}])
    src, missing = normalize_incheon(ROUTES, sections, STOPS)
    assert src["seq"].tolist() == [1, 3]  # PATHSEQ와 정류소 순번을 혼동하지 않는다
    assert src["updown"].tolist() == ["상행", "하행"]
    assert len(missing) == 1 and missing[0]["BSTOPID"] == "169999999"
    assert src["src_lon"].tolist() == [126.6, 126.6]  # TM 값을 경위도로 사용하지 않는다
    out, _ = build(src, STOPS, lambda lons, lats: [], {"165000004": ROUTE_TYPES["2"]})
    assert out["stop_key"].tolist() == ["165000111-ICB", "165000111-ICB"]
    assert out["source"].tolist() == ["incheon", "incheon"]
    assert out["route_type"].tolist() == ["간선", "간선"]


def test_tago_ids_shifted_by_two_and_wgs84_fallback():
    stops = pd.concat([STOPS, pd.DataFrame([{"stop_key": "232000413", "name": "백석마을", "lat": "37.68",
                                             "lon": "126.62", "source_ids": "GGB232000413", "is_virtual": "0"}])])
    tago = {"ROUTEID": "165000004", "DIRCD": "0", "gpslati": "", "gpslong": ""}
    sections = pd.DataFrame([{**tago, "BSTOPSEQ": "1", "BSTOPID": "365000111", "BSTOPNM": "인천 승강장"},
                             {**tago, "BSTOPSEQ": "2", "BSTOPID": "432000413", "BSTOPNM": "백석마을"},
                             {**tago, "BSTOPSEQ": "3", "BSTOPID": "369999999", "BSTOPNM": "신설",
                              "gpslati": "37.45", "gpslong": "126.63"}])
    src, missing = normalize_incheon(ROUTES, sections, stops)
    assert not missing
    assert src["station_id"].tolist() == ["165000111", "232000413", "369999999"]
    assert src["src_lon"].tolist() == [126.6, 126.62, 126.63]


def test_duplicate_sequence_fails():
    with pytest.raises(ValueError, match="중복"):
        normalize_incheon(ROUTES, pd.DataFrame([SECTION, SECTION]), STOPS)


def test_fetch_paginates_with_existing_key_encoding(monkeypatch):
    requests = []
    def urlopen(url, timeout):
        params = parse_qs(urlsplit(url).query)
        requests.append(params)
        assert params["serviceKey"] == ["fake+key="]
        page = int(params["pageNo"][0])
        return io.BytesIO((f"<ServiceResult><msgHeader><resultCode>0</resultCode><totalCount>2</totalCount>"
                           f"<numOfRows>1</numOfRows><pageNo>{page}</pageNo></msgHeader><msgBody>"
                           f"<itemList><ROUTEID>{page}</ROUTEID></itemList></msgBody></ServiceResult>").encode())
    monkeypatch.setattr(fetch.urllib.request, "urlopen", urlopen)
    rows = fetch.fetch_rows("fake+key=", "getBusRouteNo", routeNo="4")
    assert rows == [{"ROUTEID": "1"}, {"ROUTEID": "2"}]
    assert len(requests) == 2


def test_failed_collection_does_not_publish_partial_and_resumes(tmp_path, monkeypatch):
    routes = [*ROUTES.to_dict("records"), {"ROUTEID": "165000005", "ROUTENO": "5", "ROUTETPCD": "2"}]
    calls = []
    def first(key, operation, **params):
        calls.append((operation, params))
        if operation == "getBusRouteNo":
            return routes
        if params["routeId"] == "165000005":
            raise ValueError("합성 서비스 장애")
        return [SECTION]
    monkeypatch.setattr(fetch, "fetch_rows", first)
    with pytest.raises(ValueError, match="장애"):
        fetch.collect("fake-key", tmp_path)
    assert not (tmp_path / "sections.csv").exists()
    assert (tmp_path / "route_165000004.csv").exists()
    def resume(key, operation, **params):
        assert operation == "getBusRouteSectionList" and params["routeId"] == "165000005"
        return [{**SECTION, "ROUTEID": "165000005"}]
    monkeypatch.setattr(fetch, "fetch_rows", resume)
    assert fetch.collect("fake-key", tmp_path) == (2, 2)
    assert len(pd.read_csv(tmp_path / "sections.csv")) == 2
