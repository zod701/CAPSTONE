"""인천 공식 XML 명세로 만든 합성 응답. 실제 키·네트워크는 사용하지 않는다."""
import asyncio
import csv
import shutil
from xml.sax.saxutils import escape

import httpx
import pytest

from app.api import _prefer_seoul_bus
from app.errors import ApiError
from app.realtime import KIND_IC, KIND_IC_ROUTE, bus_incheon
from incheon_api import IncheonError, parse_page
from web.tests.test_api import TINY, make_settings, serve
from web.tests.test_realtime import Clock, Upstream, make, used


def xml(rows=(), *, code="00", page=1, size=1000, total=None):
    items = "".join("<itemList>" + "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in row.items())
                    + "</itemList>" for row in rows)
    return (f"<ServiceResult><msgHeader><resultCode>{code}</resultCode><resultMsg>합성 응답</resultMsg>"
            f"<totalCount>{len(rows) if total is None else total}</totalCount><pageNo>{page}</pageNo>"
            f"<numOfRows>{size}</numOfRows></msgHeader><msgBody>{items}</msgBody></ServiceResult>")


ROW = {"ROUTEID": "165000004", "BSTOPID": "165000111", "ARRIVALESTIMATETIME": "491",
       "REST_STOP_COUNT": "14", "BUS_NUM_PLATE": "인천00가0000", "REMAIND_SEAT": "0",
       "CONGESTION": "2", "LASTBUSYN": "1"}
ROUTE = {"ROUTEID": "165000004", "ROUTENO": "테스트4", "ROUTETPCD": "2"}
LIMITS = {KIND_IC: 1000, KIND_IC_ROUTE: 1000}


def test_xml_arrivals_units_and_sentinels():
    rows = bus_incheon(parse_page(xml([ROW, {**ROW, "ARRIVALESTIMATETIME": "-1", "REMAIND_SEAT": "255",
                                              "CONGESTION": "255", "REST_STOP_COUNT": "-1"}])))
    assert rows[0]["eta_s"] == 491  # 분으로 오인하지 않는다
    assert rows[0]["seats"] == 0 and rows[0]["crowding"] == 2 and rows[0]["is_last"] is True
    assert rows[0]["n_stops_ahead"] == 14 and rows[0]["source"] == "incheon"
    assert rows[0]["route_name"] is None  # 노선 ID를 노선 번호로 표시하지 않는다
    assert all(rows[1][k] is None for k in ("eta_s", "seats", "crowding", "n_stops_ahead"))
    assert bus_incheon(parse_page(xml([{**ROW, "ARRIVALESTIMATETIME": "0"}])))[0]["eta_s"] == 0
    assert bus_incheon(parse_page(xml())) == []


@pytest.mark.parametrize("body", ["not XML", "<response/>", xml(total=2), xml(page=0), xml(size=0)])
def test_invalid_response_is_not_empty_arrivals(body):
    with pytest.raises(IncheonError):
        parse_page(body)


def test_request_key_pagination_cache_and_metadata(tmp_path):
    requests = []
    def upstream(req):
        requests.append(req)
        assert req.url.params["serviceKey"] == "fake+key="
        if req.url.path.endswith("getBusRouteId"):
            return httpx.Response(200, text=xml([ROUTE]))
        assert req.url.params["bstopId"] == "165000111"
        page = int(req.url.params["pageNo"])
        return httpx.Response(200, text=xml([{**ROW, "ARRIVALESTIMATETIME": page * 100}], page=page, size=1, total=2))
    clock = Clock()
    client, quota = make(tmp_path, upstream, key="fake%2Bkey%3D", limits=LIMITS, clock=clock)
    async def run():
        first, same = await asyncio.gather(client.incheon_stop("165000111"), client.incheon_stop("165000111"))
        assert first == same and [r["eta_s"] for r in first] == [100, 200]
        assert len(requests) == 2
        first[0]["route_name"] = "캐시 오염 금지"
        assert (await client.incheon_stop("165000111"))[0]["route_name"] is None
        assert await client.incheon_route("165000004") == [{"id": "165000004", "name": "테스트4", "type": "간선"}]
        clock.now += 16
        await client.incheon_route("165000004")
        assert len(requests) == 3
    asyncio.run(run())
    assert used(quota, KIND_IC) == 2 and used(quota, KIND_IC_ROUTE) == 1


@pytest.mark.parametrize("code,expected", [("04", "upstream_error"), ("20", "arrivals_auth"),
                                         ("30", "arrivals_auth"), ("31", "arrivals_auth"),
                                         ("22", "quota_exceeded")])
@pytest.mark.parametrize("gateway", [False, True])
def test_errors_are_sanitized_and_not_cached(tmp_path, code, expected, gateway):
    key = "fake-incheon-secret"
    body = (f"<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>{code}</returnReasonCode>"
            f"<returnAuthMsg>{key}</returnAuthMsg></cmmMsgHeader></OpenAPI_ServiceResponse>") if gateway else xml(code=code)
    up = Upstream(text=body)
    client, quota = make(tmp_path, up, key=key, limits=LIMITS)
    with pytest.raises(ApiError) as exc:
        asyncio.run(client.incheon_stop("165000111"))
    assert exc.value.code == expected and key not in str(exc.value.to_body())
    assert client.cache.get((KIND_IC, "165000111")) is None
    assert used(quota, KIND_IC) == (0 if expected == "arrivals_auth" else 1000 if expected == "quota_exceeded" else 1)


def test_missing_key_and_incheon_limit(tmp_path):
    up = Upstream(text=xml([ROW]))
    client, _ = make(tmp_path, up, key=None, limits=LIMITS)
    with pytest.raises(ApiError) as exc:
        asyncio.run(client.incheon_stop("165000111"))
    assert exc.value.code == "missing_key" and not up.requests
    client, _ = make(tmp_path, up, limits={**LIMITS, KIND_IC: 0})
    with pytest.raises(ApiError) as exc:
        asyncio.run(client.incheon_stop("165000111"))
    assert exc.value.code == "quota_exceeded" and not up.requests


def test_later_page_failure_keeps_successful_page_quota(tmp_path):
    def upstream(req):
        if req.url.params["pageNo"] == "1":
            return httpx.Response(200, text=xml([ROW], page=1, size=1, total=2))
        return httpx.Response(200, text=xml(code="30"))
    client, quota = make(tmp_path, upstream, limits=LIMITS)
    with pytest.raises(ApiError):
        asyncio.run(client.incheon_stop("165000111"))
    assert used(quota, KIND_IC) == 1
    assert client.cache.get((KIND_IC, "165000111")) is None


def test_incheon_api_uses_source_id_and_resolves_route_number(tmp_path):
    db = tmp_path / "db"
    shutil.copytree(TINY, db)
    path = db / "bus_stops.csv"
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields, rows = reader.fieldnames, list(reader)
    # 병합/분리 키를 업스트림에 보내지 않고 ICB 뒤의 원천 ID를 써야 한다.
    sample = {**rows[0], "stop_key": "165000111-ICB", "source_ids": "ICB165000111", "is_virtual": "0"}
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows([*rows, sample])
    requests = []
    def upstream(req):
        requests.append(req)
        if req.url.path.endswith("getBusRouteId"):
            return httpx.Response(200, text=xml([ROUTE]))
        assert req.url.params["bstopId"] == "165000111"
        return httpx.Response(200, text=xml([ROW]))
    settings = make_settings(tmp_path, processed_dir=db)
    with serve(settings, upstream) as c:
        response = c.get("/api/arrivals/stop/165000111-ICB")
        assert response.status_code == 200
        body = response.json()
        assert body["items"][0]["route_name"] == "테스트4"
        assert body["items"][0]["route_type"] == "간선"
        assert body["items"][0]["eta_s"] == 491 and body["failed"] == []
        assert body["quota"][KIND_IC]["used"] == body["quota"][KIND_IC_ROUTE]["used"] == 1
        c.get("/api/arrivals/stop/165000111-ICB")
        assert len(requests) == 2


def test_source_preference_keeps_incheon_and_avoids_duplicates():
    ic = bus_incheon(parse_page(xml([ROW])))[0]
    seoul = {**ic, "source": "seoul", "eta_s": None}
    assert _prefer_seoul_bus([ic]) == [ic]
    assert _prefer_seoul_bus([seoul, ic]) == [ic]
    assert _prefer_seoul_bus([{**seoul, "eta_s": 100}, ic])[0]["source"] == "seoul"
