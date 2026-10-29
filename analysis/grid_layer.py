"""grid_cbd.py 결과 CSV → 1 km 칸 사각형 GeoJSON (지도용). 도착지마다 세 지표를 열로 붙인다.

- `best_<도착지>`: 일반화 비용(시간 + 요금 ÷ VOT)이 가장 작은 수단 — transit · taxi · first · last · both
- `ratio_<도착지>`: 대중교통만 시간 ÷ 택시 시간 (대중교통 경로가 없으면 빈칸)
- `save_<도착지>`: 대중교통만 시간 − 택시가 낀 세 수단 중 가장 짧은 시간 (분, 대중교통 경로가 없으면 빈칸)

    .\\.venv\\Scripts\\python.exe analysis\\grid_layer.py analysis\\out\\grid_cbd_20261029T1634.csv
"""
import argparse
import csv
import json
import math
from pathlib import Path

from grid_cbd import CELL_M, DESTS  # 경로 설정도 여기서 된다

from algo import anchors  # noqa: E402

MODES = ("transit", "taxi", "first", "last", "both")
HYBRID = ("first", "last", "both")


def _f(v):
    return float(v) if v not in ("", None) else None


def cell_metrics(r, vot):
    t = {m: _f(r[f"{m}_time_s"]) for m in MODES}
    gc = {m: t[m] / 60 + _f(r[f"{m}_fare"]) / vot for m in MODES if t[m] is not None}
    hyb = [t[m] for m in HYBRID if t[m] is not None]
    tr, tx = t["transit"], t["taxi"]
    return {"best": min(gc, key=gc.get),
            "ratio": round(tr / tx, 3) if tr is not None else None,
            "save": round((tr - min(hyb)) / 60, 1) if tr is not None and hyb else None}


def main(argv=None):
    ap = argparse.ArgumentParser(description="격자 결과 CSV → 칸 사각형 GeoJSON")
    ap.add_argument("csv", type=Path)
    ap.add_argument("--vot", type=float, default=anchors.VOT["time"], help="원/분 (기본: 탐색과 같은 값)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    cells = {}
    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            c = cells.setdefault(r["cell"], {"cell": r["cell"], "sido": r["sido"], "sgg": r["sgg"],
                                             "lon": float(r["lon"]), "lat": float(r["lat"])})
            for k, v in cell_metrics(r, args.vot).items():
                c[f"{k}_{r['dest']}"] = v
    # grid_cbd.grid_cells 와 같은 등장방형 칸 (37.5° 기준)
    hlat = math.degrees(CELL_M / 6371008.8) / 2
    hlon = hlat / math.cos(math.radians(37.5))
    feats = []
    for c in cells.values():
        x, y = c["lon"], c["lat"]
        ring = [[x - hlon, y - hlat], [x + hlon, y - hlat], [x + hlon, y + hlat], [x - hlon, y + hlat], [x - hlon, y - hlat]]
        feats.append({"type": "Feature", "properties": c, "geometry": {"type": "Polygon", "coordinates": [ring]}})
    out = args.out or args.csv.with_suffix(".geojson")
    out.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False), encoding="utf-8")
    print(f"{len(feats)} 칸 · 도착지 {', '.join(DESTS)} · VOT {args.vot:g} 원/분 → {out}")


if __name__ == "__main__":
    main()
