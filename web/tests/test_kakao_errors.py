"""카카오 클라이언트 오류 매핑 — 응답 본문은 모두 문서 스키마로 손으로 쓴 합성 데이터."""
import asyncio
import json

import httpx
import pytest

from app.errors import ApiError
from app.kakao import ADDRESS_URL, CAR_URL, KEYWORD_URL, TRANSIT_URL, KakaoClient, map_error, redact
from app.quota import QuotaGuard

KEY = "0123456789abcdef0123456789abcdef"  # 가짜 키
NOTE = "SYNTHETIC - hand-written from documented schema; not a Kakao response"
LIMITS = {"transit": 1000, "car": 10000}


def body(**kw):
    return {"_note": NOTE, **kw}


class Upstream:
    """MockTransport 핸들러: 요청을 기록하고 정해진 응답 또는 예외를 돌려준다."""

    def __init__(self, status=200, json_body=None, text=None, exc=None):
        self.status, self.json_body, self.text, self.exc = status, json_body, text, exc
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        if self.text is not None:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(self.status, json=self.json_body)


def make(tmp_path, upstream, key=KEY, limits=LIMITS):
    quota = QuotaGuard(tmp_path / "quota.json", limits)
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return KakaoClient(http, key, quota), quota


def run(client, kind="transit"):
    fn = client.transit if kind == "transit" else client.car
    return asyncio.run(fn(127.0, 37.2, 127.1, 37.3))


def used(quota, kind="transit"):
    return quota.snapshot()[kind]["used"]


def raised(client, kind="transit"):
    with pytest.raises(ApiError) as ei:
        run(client, kind)
    err = ei.value
    assert KEY not in json.dumps(err.to_body(), ensure_ascii=False)
    return err


def test_ok_returns_json_and_records(tmp_path):
    up = Upstream(200, body(status="OK", routes=[]))
    client, quota = make(tmp_path, up)
    assert run(client)["status"] == "OK"
    assert used(quota) == 1
    req = up.requests[0]
    assert str(req.url).startswith(TRANSIT_URL + "?")
    assert req.headers["Authorization"] == f"KakaoAK {KEY}"
    assert dict(req.url.params) == {"start_x": "127.0", "start_y": "37.2", "end_x": "127.1", "end_y": "37.3"}


def test_car_params(tmp_path):
    up = Upstream(200, body(routes=[]))
    client, quota = make(tmp_path, up)
    run(client, "car")
    req = up.requests[0]
    assert str(req.url).startswith(CAR_URL + "?")
    assert dict(req.url.params) == {"origin": "127.0,37.2", "destination": "127.1,37.3"}
    assert used(quota, "car") == 1 and used(quota, "transit") == 0


def test_local_search_params(tmp_path):
    up = Upstream(200, body(meta={}, documents=[]))
    client, quota = make(tmp_path, up, limits={**LIMITS, "keyword": 5, "address": 5})
    asyncio.run(client.keyword("판교역", 10))
    asyncio.run(client.address("판교역로 166", 5))
    kw, ad = up.requests
    assert str(kw.url).startswith(KEYWORD_URL + "?") and str(ad.url).startswith(ADDRESS_URL + "?")
    assert dict(kw.url.params) == {"query": "판교역", "size": "10"}
    assert dict(ad.url.params) == {"query": "판교역로 166", "size": "5"}
    assert kw.headers["Authorization"] == f"KakaoAK {KEY}"
    assert (used(quota, "keyword"), used(quota, "address"), used(quota)) == (1, 1, 0)


@pytest.mark.parametrize("kind, what", [("transit", "대중교통 경로를 조회할"), ("keyword", "장소를 검색할"),
                                        ("address", "주소를 검색할"), ("car", "자동차 길찾기 요청을 처리할")])
def test_map_disabled_message_by_kind(kind, what):
    err = map_error(kind, 403, json.dumps(body(errorType="NotAuthorizedError", message="disabled OPEN_MAP_AND_LOCAL")))
    assert (err.code, err.status) == ("kakao_map_disabled", 503)
    assert err.message == f"카카오맵 서비스가 비활성화되어 있어 {what} 수 없습니다."


def test_quota_code_as_string():
    # 로컬 API 는 code 를 문자열로 준다
    err = map_error("keyword", 400, json.dumps(body(code="-10", msg="API limit has been exceeded.")), limit=100000)
    assert (err.code, err.status, err.upstream_status) == ("quota_exceeded", 429, 400)
    assert err.message == "오늘의 장소 검색 호출 한도(100000건)를 모두 사용했습니다."
    assert map_error("keyword", 400, json.dumps(body(code="-2", msg="Max (query) length 100"))).code == "upstream_error"


def test_map_disabled_403(tmp_path):
    up = Upstream(403, body(errorType="NotAuthorizedError", message="App(바로가) disabled OPEN_MAP_AND_LOCAL service."))
    client, quota = make(tmp_path, up)
    err = raised(client)
    assert (err.code, err.status, err.upstream_status) == ("kakao_map_disabled", 503, 403)
    assert err.message == "카카오맵 서비스가 비활성화되어 있어 대중교통 경로를 조회할 수 없습니다."
    assert "[카카오맵]" in err.action and "[사용 설정]" in err.action
    assert used(quota) == 0


def test_other_403(tmp_path):
    up = Upstream(403, body(errorType="NotAuthorizedError", message="ip mismatch"))
    client, quota = make(tmp_path, up)
    err = raised(client)
    assert (err.code, err.status, err.upstream_status) == ("kakao_forbidden", 502, 403)
    assert used(quota) == 0


def test_quota_minus10_marks_exhausted(tmp_path):
    up = Upstream(400, body(code=-10, errorType="BadRequest", message="API limit has been exceeded."))
    client, quota = make(tmp_path, up)
    err = raised(client)
    assert (err.code, err.status, err.upstream_status) == ("quota_exceeded", 429, 400)
    assert err.message == "오늘의 대중교통 경로 조회 호출 한도(1000건)를 모두 사용했습니다."
    assert quota.snapshot()["transit"] == {"used": 1000, "limit": 1000, "remaining": 0}
    # 다음 요청은 카카오를 부르지 않고 막힌다
    assert raised(client).code == "quota_exceeded"
    assert len(up.requests) == 1


def test_429_is_quota():
    err = map_error("car", 429, "", limit=10000)
    assert (err.code, err.status, err.upstream_status) == ("quota_exceeded", 429, 429)
    assert "자동차 길찾기" in err.message and "(10000건)" in err.message


def test_401_auth(tmp_path):
    up = Upstream(401, body(errorType="AccessDeniedError", message=f"wrong appKey({KEY}) format"))
    client, quota = make(tmp_path, up)
    err = raised(client)
    assert (err.code, err.status, err.upstream_status) == ("kakao_auth", 503, 401)
    assert err.message == "카카오 REST API 키가 올바르지 않습니다."
    assert used(quota) == 0


def test_other_400_is_redacted(tmp_path):
    up = Upstream(400, body(errorType="InvalidArgument", message=f"wrong appKey({KEY}) format"))
    client, quota = make(tmp_path, up)
    err = raised(client)
    assert (err.code, err.status, err.upstream_status) == ("upstream_error", 502, 400)
    assert "HTTP 400" in err.message and "InvalidArgument" in err.message
    assert "<REDACTED>" in err.message
    assert used(quota) == 1


def test_route_status_in_error_detail():
    err = map_error("transit", 400, json.dumps(body(status="INVALID_REQUEST")))
    assert err.code == "upstream_error"
    assert err.message == "카카오 대중교통 경로 조회 요청이 실패했습니다 (HTTP 400): INVALID_REQUEST"


def test_non_hex_key_echo_is_redacted(tmp_path):
    odd_key = "not-a-hex-key-zz"
    up = Upstream(400, body(msg=f"bad key {odd_key}"))
    client, _ = make(tmp_path, up, key=odd_key)
    with pytest.raises(ApiError) as ei:
        run(client)
    assert odd_key not in ei.value.message


def test_non_json_error_body(tmp_path):
    client, quota = make(tmp_path, Upstream(500, text="<html>Internal Server Error</html>"))
    err = raised(client)
    assert err.code == "upstream_error"
    assert err.message == "카카오 대중교통 경로 조회 요청이 실패했습니다 (HTTP 500)."
    assert used(quota) == 1


def test_connect_error_not_recorded(tmp_path):
    up = Upstream(exc=httpx.ConnectError(f"connect failed KakaoAK {KEY}"))
    client, quota = make(tmp_path, up)
    err = raised(client)
    assert (err.code, err.status) == ("upstream_unreachable", 502)
    assert err.__suppress_context__
    assert used(quota) == 0


def test_timeout_recorded(tmp_path):
    client, quota = make(tmp_path, Upstream(exc=httpx.ReadTimeout("timed out")))
    err = raised(client)
    assert (err.code, err.status) == ("upstream_timeout", 504)
    assert used(quota) == 1


def test_other_transport_error_recorded(tmp_path):
    client, quota = make(tmp_path, Upstream(exc=httpx.ReadError("connection reset")))
    err = raised(client)
    assert (err.code, err.status) == ("upstream_error", 502)
    assert used(quota) == 1


def test_missing_key_no_call(tmp_path):
    up = Upstream(200, body(status="OK"))
    for key in (None, ""):
        client, quota = make(tmp_path, up, key=key)
        err = raised(client)
        assert (err.code, err.status) == ("missing_key", 503)
        assert "KAKAO_REST_API_KEY" in err.action
    assert up.requests == []
    assert used(quota) == 0


def test_local_limit_blocks_without_call(tmp_path):
    up = Upstream(200, body(status="OK"))
    client, quota = make(tmp_path, up, limits={"transit": 1, "car": 1})
    run(client)
    err = raised(client)
    assert (err.code, err.upstream_status) == ("quota_exceeded", None)
    assert len(up.requests) == 1


def test_redact():
    assert redact(f"key={KEY}.") == "key=<REDACTED>."
    assert redact("id 00000000-0000-0000-0000-000000000000 end") == "id <REDACTED> end"
    assert redact(f'appKey("{KEY.upper()}")') == 'appKey("<REDACTED>")'
    assert redact("짧은 abc123 값") == "짧은 abc123 값"
    assert redact(None) == ""
