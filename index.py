"""Vercel 진입점 — 저장소 루트에서 `app`(FastAPI)을 찾는다(pyproject.toml `tool.vercel.entrypoint`).

앱 패키지는 web/backend/app 에 있어 그 경로를 먼저 잡는다. 로컬 실행은 그대로 uvicorn(web/README.md).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "web" / "backend"))

from app.main import create_app  # noqa: E402

app = create_app()
