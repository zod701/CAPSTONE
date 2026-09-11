// 백엔드 호출 + 오류 봉투 해석. 다른 모듈이 함께 쓰는 소도구(esc, safeColor)도 여기 둔다.

export class ApiError extends Error {
  constructor(message, { code = "error", action = null, status = 0, upstreamStatus = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.action = action;
    this.status = status;
    this.upstreamStatus = upstreamStatus;
  }
}

// 비-2xx → 서버의 {"error": {...}} 를 ApiError 로. AbortError 는 그대로 던진다(호출자가 무시).
export async function getJSON(path, params = {}, { signal } = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  const url = qs.toString() ? `${path}?${qs}` : path;

  let res;
  try {
    res = await fetch(url, { signal, headers: { Accept: "application/json" } });
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new ApiError("서버에 연결할 수 없습니다.", {
      code: "network",
      action: "uvicorn 서버가 실행 중인지 확인한 뒤 새로고침하세요 (web/README.md).",
    });
  }

  let body = null;
  try {
    body = await res.json();
  } catch (err) {
    if (err.name === "AbortError") throw err;
  }

  if (!res.ok) {
    const e = body && body.error;
    if (e) {
      throw new ApiError(e.message || `요청이 실패했습니다 (HTTP ${res.status}).`, {
        code: e.code || "error",
        action: e.action || null,
        status: res.status,
        upstreamStatus: e.upstream_status ?? null,
      });
    }
    throw new ApiError(`요청이 실패했습니다 (HTTP ${res.status}).`, { code: "http_error", status: res.status });
  }
  if (body === null) {
    throw new ApiError("서버 응답을 해석할 수 없습니다.", { code: "bad_response", status: res.status });
  }
  return body;
}

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

// innerHTML 에 넣는 모든 외부 문자열(정류소명·카카오 이름 등)은 이것을 거친다.
export function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
}

export function safeColor(c, fallback = "#607D8B") {
  return /^#[0-9a-fA-F]{6}$/.test(c || "") ? c : fallback;
}
