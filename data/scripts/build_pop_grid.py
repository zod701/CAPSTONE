"""격자별 총인구 (`processed/pop_grid.csv`) — 통계청 SGIS 격자통계(인구) 원본(`data/population/`)에서 뽑는다.

칸 번호를 분석 격자(`analysis/grid_cbd.py`)와 같은 `<크기>m_<x>_<y>` 로 바꿔 그대로 잇는다. 둘 다 EPSG:5179 국가 격자의 왼쪽 아래 좌표다.
- 1 km 코드 `다사0001`: 100 km 칸 `다사` · x 00 km · y 01 km
- 500 m 코드 `다사35a63b`: x 35 km · y 63 km 칸의 사분면 — `a` 는 그대로, `b` 는 그 축으로 500 m 더한다

원본에 값이 없는 칸은 인구 0 으로 본다. 원본의 작은 값은 뭉뚱그려져 있다(1 km · 500 m 모두 10 이하는 0 · 5 · 8 이 거의 전부 — 수도권 9개 100 km 칸의 500 m 인구 합 중 0.56%).

    .\\.venv\\Scripts\\python.exe data\\scripts\\build_pop_grid.py
"""
import csv
from pathlib import Path

from fetch_boundary import ROOT

SRC = ROOT / "data" / "population"
OUT = ROOT / "data" / "processed" / "pop_grid.csv"
SQUARE_X = {"가": 700_000, "나": 800_000, "다": 900_000, "라": 1_000_000}
SQUARE_Y = {"바": 1_800_000, "사": 1_900_000, "아": 2_000_000}
HALF = {"a": 0, "b": 500}


def cell_id(code):
    """SGIS 격자 코드 → 분석 격자 칸 번호."""
    x0, y0 = SQUARE_X[code[0]], SQUARE_Y[code[1]]
    rest = code[2:]
    if len(rest) == 4:
        return f"1000m_{x0 + int(rest[:2]) * 1000}_{y0 + int(rest[2:]) * 1000}"
    return f"500m_{x0 + int(rest[:2]) * 1000 + HALF[rest[2]]}_{y0 + int(rest[3:5]) * 1000 + HALF[rest[5]]}"


def main():
    rows = []
    for path in sorted(SRC.glob("_census_*/*.csv")):
        with path.open(encoding="cp949", newline="") as f:
            rows += [(cell_id(r[1]), int(r[3])) for r in csv.reader(f) if r[2] == "to_in_001"]
    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cell", "pop"])
        w.writerows(rows)
    print(f"{len(rows)} 칸 · {Path(OUT).name}")


if __name__ == "__main__":
    main()
