"""VWorld WMTS 타일 프록시 — 키는 서버에만 둔다(D-10).

업스트림 URL 경로에 키가 들어가므로 httpx 예외 문자열과 요청 로그에 키가 섞인다. 그래서 모든 예외를
본문 없는 502 로 바꾸고, httpx 의 요청 로그(INFO, 전체 URL 포함)는 끈다.
"""
import logging

from fastapi import APIRouter, Request, Response

from .errors import ApiError

router = APIRouter()

LAYERS = {"Base": "png", "white": "png", "midnight": "png", "Hybrid": "png", "Satellite": "jpeg"}
UPSTREAM = "https://api.vworld.kr/req/wmts/1.0.0/{key}/{layer}/{z}/{y}/{x}.{ext}"
DEFAULT_CACHE = "public, max-age=259200"  # VWorld 실측 캐시 3일

logging.getLogger("httpx").setLevel(logging.WARNING)


@router.get("/tiles/vworld/{layer}/{z}/{y}/{x}")
async def vworld_tile(request: Request, layer: str, z: int, y: int, x: int):
    ext = LAYERS.get(layer)
    if ext is None or not 6 <= z <= 19 or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        return Response(status_code=404)
    key = request.app.state.settings.vworld_key
    if not key:
        raise ApiError("missing_key", "VWorld API 키가 설정되지 않았습니다.",
                       action=".env 에 VWORLD_API_KEY 를 추가하고 서버를 다시 시작하세요.", status=503)
    try:
        r = await request.app.state.http.get(UPSTREAM.format(key=key, layer=layer, z=z, y=y, x=x, ext=ext))
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or not ctype.lower().startswith("image/"):
            return Response(status_code=502)
        return Response(r.content, media_type=ctype,
                        headers={"Cache-Control": r.headers.get("cache-control") or DEFAULT_CACHE})
    except Exception:  # 예외 문자열(키가 든 URL)을 전달·로그하지 않는다
        return Response(status_code=502)
