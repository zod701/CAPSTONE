"""바로가 FastAPI 앱 — /api · /tiles 라우터 + 프론트엔드 정적 파일.

실행(저장소 루트): .venv/Scripts/python.exe -m uvicorn app.main:app --app-dir web/backend  (web/README.md)
쿼터 잠금은 프로세스 안에서만 유효하므로 단일 워커로 띄운다.
"""
import mimetypes
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import api, tiles
from .config import Settings, load_settings
from .errors import ApiError, install_handlers
from .headwaydb import HeadwayDB
from .hybrid import AnchorTables
from .kakao import KakaoClient
from .linesdb import LinesDB
from .livestationsdb import LiveStationsDB
from .quota import QuotaGuard
from .realtime import RealtimeClient
from .routesdb import RoutesDB
from .stopsdb import StopsDB

# Windows 레지스트리가 .js 를 text/plain 으로 줄 수 있다 — 그러면 브라우저가 ES 모듈을 거부한다
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


class _RevalidatingStatic(StaticFiles):
    """프론트 파일은 매번 ETag 로 재검증(304)하게 한다. Cache-Control 이 없으면 브라우저가 휴리스틱으로
    캐시해, 고친 JS 가 F5 에서 안 뜨고 옛 모듈과 새 모듈이 섞일 수 있다."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or load_settings()  # 정적 마운트가 디렉터리를 알아야 하므로 여기서 정한다

    @asynccontextmanager
    async def lifespan(app):
        st = app.state
        st.settings = settings
        st.quota = QuotaGuard(settings.quota_path,
                              {"transit": settings.transit_daily_limit, "car": settings.car_daily_limit,
                               "keyword": settings.keyword_daily_limit, "address": settings.address_daily_limit,
                               "gyeonggi_bus": settings.gyeonggi_bus_daily_limit,
                               "seoul_bus": settings.seoul_bus_daily_limit,
                               "seoul_subway": settings.seoul_subway_daily_limit})
        try:
            st.stops = StopsDB.load(settings.processed_dir, settings.ref_dir)
        except FileNotFoundError:
            st.stops = None  # /api/health ready=false, 정류소·경계 API 는 data_not_built
        try:
            st.routes = RoutesDB.load(settings.processed_dir)
        except FileNotFoundError:
            st.routes = None  # 정류장 경유 노선 API 는 data_not_built
        try:
            st.lines = LinesDB.load(settings.processed_dir)
        except FileNotFoundError:
            st.lines = None   # 도시철도 구간은 이름·기하로만 찾는다
        try:
            st.headway = HeadwayDB.load(settings.processed_dir)
        except FileNotFoundError:
            st.headway = None   # 대기시간을 더한 소요 시간만 빠진다
        try:
            st.anchor_tables = AnchorTables.load(settings.processed_dir)
        except (FileNotFoundError, ApiError):
            st.anchor_tables = None   # 하이브리드 경로만 빠진다 (algo 패키지나 표가 없다)
        try:
            st.live_stations = LiveStationsDB.load(settings.processed_dir)
        except FileNotFoundError:
            st.live_stations = None   # 역 실시간 도착 API 만 data_not_built
        async with httpx.AsyncClient(transport=transport, timeout=settings.http_timeout_s) as http:
            st.http = http
            st.kakao = KakaoClient(http, settings.kakao_rest_key, st.quota, settings.http_timeout_s)
            st.realtime = RealtimeClient(http, st.quota, data_go_kr_key=settings.data_go_kr_key,
                                         subway_key=settings.seoul_subway_live_key,
                                         timeout_s=settings.arrivals_timeout_s,
                                         ttl_s=settings.arrivals_cache_ttl_s)
            yield

    app = FastAPI(title="바로가", lifespan=lifespan)
    install_handlers(app)
    app.include_router(api.router)
    app.include_router(tiles.router)
    # 마지막에: 위 라우트에 걸리지 않은 경로를 전부 정적 파일로
    app.mount("/", _RevalidatingStatic(directory=settings.frontend_dir, html=True), name="frontend")
    return app


def __getattr__(name):
    # `app.main:app` 은 처음 가져갈 때 만든다 — create_app 만 import 하는 테스트가 실제 .env 를 읽지 않게
    if name == "app":
        globals()["app"] = app = create_app()
        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
