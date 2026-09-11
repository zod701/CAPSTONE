import logging

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import tiles
from app.config import Settings
from app.errors import install_handlers

VKEY = "00000000-0000-0000-0000-000000000000"  # 가짜 키
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class Upstream:
    def __init__(self, handler):
        self.handler = handler
        self.urls = []

    def __call__(self, request):
        self.urls.append(str(request.url))
        return self.handler(request)


def client_for(tmp_path, handler, key=VKEY):
    app = FastAPI()
    install_handlers(app)
    app.include_router(tiles.router)
    app.state.settings = Settings(repo_root=tmp_path, processed_dir=tmp_path, ref_dir=tmp_path,
                                  frontend_dir=tmp_path, quota_path=tmp_path / "quota.json",
                                  kakao_rest_key=None, vworld_key=key)
    up = Upstream(handler)
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(up))
    return TestClient(app), up


def png(request, cache="public, max-age=86400"):
    headers = {"content-type": "image/png"}
    if cache:
        headers["cache-control"] = cache
    return httpx.Response(200, content=PNG, headers=headers)


def no_key_leak(resp, caplog):
    assert VKEY not in resp.text
    assert all(VKEY not in v for v in resp.headers.values())
    assert VKEY not in caplog.text


def test_image_passthrough(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    client, up = client_for(tmp_path, png)
    r = client.get("/tiles/vworld/white/12/1581/3493")
    assert r.status_code == 200
    assert r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "public, max-age=86400"
    assert up.urls == [f"https://api.vworld.kr/req/wmts/1.0.0/{VKEY}/white/12/1581/3493.png"]
    no_key_leak(r, caplog)  # httpx 의 요청 로그(전체 URL)가 꺼져 있어야 한다


def test_satellite_jpeg_and_default_cache(tmp_path):
    client, up = client_for(tmp_path, lambda req: httpx.Response(200, content=b"jpg", headers={"content-type": "image/jpeg"}))
    r = client.get("/tiles/vworld/Satellite/6/0/63")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=259200"
    assert up.urls[0].endswith("/Satellite/6/0/63.jpeg")


@pytest.mark.parametrize("path", [
    "/tiles/vworld/Nope/12/1/1",
    "/tiles/vworld/white/5/1/1",
    "/tiles/vworld/white/20/1/1",
    "/tiles/vworld/white/12/4096/1",
    "/tiles/vworld/white/12/1/4096",
    "/tiles/vworld/white/12/-1/1",
])
def test_bad_tile_is_404(tmp_path, path):
    client, up = client_for(tmp_path, png)
    assert client.get(path).status_code == 404
    assert up.urls == []


def test_non_integer_is_invalid_request(tmp_path):
    client, up = client_for(tmp_path, png)
    r = client.get("/tiles/vworld/white/abc/1/1")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert up.urls == []


@pytest.mark.parametrize("handler", [
    lambda req: httpx.Response(200, text="<ServiceException>bad key</ServiceException>", headers={"content-type": "text/xml"}),
    lambda req: httpx.Response(500, content=PNG, headers={"content-type": "image/png"}),
])
def test_bad_upstream_is_bare_502(tmp_path, handler):
    client, _ = client_for(tmp_path, handler)
    r = client.get("/tiles/vworld/Base/12/1581/3493")
    assert r.status_code == 502
    assert r.content == b""


@pytest.mark.parametrize("exc", [
    httpx.ConnectError(f"cannot reach https://api.vworld.kr/req/wmts/1.0.0/{VKEY}/Base/12/1581/3493.png"),
    httpx.ReadTimeout(f"timeout {VKEY}"),
    RuntimeError(f"boom {VKEY}"),
])
def test_exception_is_bare_502_without_key(tmp_path, caplog, exc):
    caplog.set_level(logging.DEBUG)

    def boom(request):
        raise exc

    client, _ = client_for(tmp_path, boom)
    r = client.get("/tiles/vworld/Base/12/1581/3493")
    assert r.status_code == 502
    assert r.content == b""
    no_key_leak(r, caplog)


def test_missing_key_is_503(tmp_path):
    client, up = client_for(tmp_path, png, key=None)
    r = client.get("/tiles/vworld/white/12/1581/3493")
    assert r.status_code == 503
    err = r.json()["error"]
    assert err["code"] == "missing_key"
    assert "VWORLD_API_KEY" in err["action"]
    assert up.urls == []
