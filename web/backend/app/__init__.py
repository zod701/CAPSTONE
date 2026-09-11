"""바로가 백엔드.

이름 정규화·격자 색인(`textnorm`, `geoutil`)은 ETL 과 공유하는 계약이라 `data/scripts/` 에 한 벌만 있다.
어느 모듈을 먼저 import 하든 그 경로가 잡히도록 여기서 추가한다.
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[3] / "data" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
