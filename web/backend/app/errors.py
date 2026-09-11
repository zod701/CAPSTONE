"""API 오류 봉투 `{"error": {"code", "message", "action", "upstream_status"}}` (문구는 한국어)."""
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, code, message, *, action=None, status=500, upstream_status=None):
        super().__init__(code)  # str(exc) 에는 코드만 — 메시지·상세가 로그로 새지 않게
        self.code = code
        self.message = message
        self.action = action
        self.status = status
        self.upstream_status = upstream_status

    def to_body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message,
                          "action": self.action, "upstream_status": self.upstream_status}}


async def _api_error(request, exc):
    return JSONResponse(exc.to_body(), status_code=exc.status)


async def _invalid_request(request, exc):
    # 파라미터 이름만 알린다 (입력값은 되풀이하지 않는다)
    names = list(dict.fromkeys(str(e["loc"][-1]) for e in exc.errors() if e.get("loc")))
    msg = "요청 값이 올바르지 않습니다" + (f": {', '.join(names)}" if names else "") + "."
    return JSONResponse(ApiError("invalid_request", msg, status=400).to_body(), status_code=400)


def install_handlers(app):
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(RequestValidationError, _invalid_request)
