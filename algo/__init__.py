"""바로가 앵커 선택 — 택시와 대중교통이 만나는 지점을 고르는 휴리스틱.

정류소·노선·배차 DB(`StopsDB`·`RoutesDB`·`LinesDB`·`HeadwayDB`)는 백엔드에 한 벌만 있고, 이름 정규화·격자
색인(`textnorm`·`geoutil`)은 ETL 과 공유하는 계약이라 `data/scripts/` 에 있다. 여기서는 **읽기만** 한다.
어느 모듈을 먼저 import 하든 두 경로가 잡히도록 여기서 추가한다.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT / "web" / "backend", _ROOT / "data" / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
