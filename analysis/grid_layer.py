"""grid_cbd.py 결과(.csv.gz) → 1 km 칸 사각형 GeoJSON (지도용). 하이퍼파라미터를 골라 도착지마다 여섯 지표를 열로 붙인다.

- `--max-rides K`: 대중교통 탑승 횟수 상한 · `--cap 원`: 한 통행의 택시 요금 합 상한 (택시만 · 섞은 경로 모두)
- `best_<도착지>`: 그 조건에서 가장 빠른 수단 — transit · taxi · first · last · both
- `ratio_<도착지>`: 대중교통만 시간 ÷ 택시 시간 (대중교통 경로가 없으면 빈칸, 택시는 상한과 무관)
- `save_<도착지>`: 대중교통만 시간 − 택시가 낀 세 수단 중 가장 짧은 시간 (분, 대중교통 경로가 없으면 빈칸)
- `won_<도착지>`: 아낀 1분당 추가 비용(원/분) — 택시가 낀 경로마다 (택시비 + 대중교통 요금 차) ÷ 대중교통만보다 아낀 분 중 가장 싼 값.
  시간가치가 이보다 높으면 택시를 섞는 쪽이 낫다(VOT 없이 지역을 가르는 값). 1분 넘게 아끼는 경로가 없거나 대중교통 경로가 없으면 빈칸
- `close_<도착지>`: 격차 회수율 — save ÷ (대중교통만 시간 − 택시 시간). 대중교통의 불리함 중 하이브리드로 되찾는 몫.
  대중교통으로는 못 가고 하이브리드로만 가면 −1, 대중교통이 택시보다 빠르면(메울 격차 없음) 빈칸
- `type_<도착지>`: 유형 — good(시간비 < `RATIO_CUT`) · firstlast(시간비 ≥ 기준 · 회수율 ≥ `CLOSE_CUT`, 첫·끝 구간 문제)
  · trunk(시간비 ≥ 기준 · 회수율 < 기준, 간선 부족) · hybrid_only(하이브리드로만 감)

    .\\.venv\\Scripts\\python.exe analysis\\grid_layer.py analysis\\out\\grid_cbd_평시.csv.gz --max-rides 4 --cap 20000
"""
import argparse
import csv
import gzip
import json
import math
from pathlib import Path

from grid_cbd import CELL_M, DESTS

HYBRID = ("first", "last", "both")
RATIO_CUT = 1.5    # 유형을 가르는 시간비 — 서울 격자 중앙(약 1.6배) 아래
CLOSE_CUT = 0.5    # 유형을 가르는 회수율 — 격차의 절반
HYBRID_ONLY = -1   # close 에서 '하이브리드로만 갈 수 있음'


def cell_metrics(rows, max_rides, cap):
    """한 칸 · 한 도착지의 행들 → {best, ratio, save, won, close, type}."""
    taxi = next(r for r in rows if r["mode"] == "taxi")
    ok = [r for r in rows if (r["mode"] == "taxi" or int(r["max_rides"]) == max_rides) and float(r["taxi_fare"]) <= cap]
    time_of = lambda modes: min((float(r["time_s"]) for r in ok if r["mode"] in modes), default=None)  # noqa: E731
    tr, hyb = time_of(("transit",)), time_of(HYBRID)
    best = min(ok, key=lambda r: float(r["time_s"]))["mode"] if ok else None
    won = None
    transit = min((r for r in ok if r["mode"] == "transit"), key=lambda r: float(r["time_s"]), default=None)
    if transit is not None:
        per_min = [(float(r["taxi_fare"]) + float(r["transit_fare"]) - float(transit["transit_fare"]))
                   / ((float(transit["time_s"]) - float(r["time_s"])) / 60)
                   for r in ok if r["mode"] in HYBRID and float(transit["time_s"]) - float(r["time_s"]) >= 60]
        won = round(min(per_min)) if per_min else None
    tx = float(taxi["time_s"])
    close = kind = None
    if tr is None:
        if hyb is not None:
            close, kind = HYBRID_ONLY, "hybrid_only"
    else:
        if tr > tx and hyb is not None:
            close = round((tr - hyb) / (tr - tx), 3)
        if tr / tx < RATIO_CUT:
            kind = "good"
        elif close is not None:
            kind = "firstlast" if close >= CLOSE_CUT else "trunk"
    return {"best": best,
            "ratio": round(tr / tx, 3) if tr is not None else None,
            "save": round((tr - hyb) / 60, 1) if tr is not None and hyb is not None else None,
            "won": won, "close": close, "type": kind}


def main(argv=None):
    ap = argparse.ArgumentParser(description="격자 결과 → 칸 사각형 GeoJSON")
    ap.add_argument("src", type=Path)
    ap.add_argument("--max-rides", type=int, required=True, help="대중교통 탑승 횟수 상한 (1~4)")
    ap.add_argument("--cap", type=float, required=True, help="한 통행의 택시 요금 합 상한 (원)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    groups, cells = {}, {}
    with gzip.open(args.src, "rt", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            groups.setdefault((r["cell"], r["dest"]), []).append(r)
            cells.setdefault(r["cell"], {"cell": r["cell"], "sido": r["sido"], "sgg": r["sgg"],
                                         "lon": float(r["lon"]), "lat": float(r["lat"])})
    for (cid, dest), rows in groups.items():
        for k, v in cell_metrics(rows, args.max_rides, args.cap).items():
            cells[cid][f"{k}_{dest}"] = v
    # grid_cbd.grid_cells 와 같은 등장방형 칸 (37.5° 기준)
    hlat = math.degrees(CELL_M / 6371008.8) / 2
    hlon = hlat / math.cos(math.radians(37.5))
    feats = []
    for c in cells.values():
        x, y = c["lon"], c["lat"]
        ring = [[x - hlon, y - hlat], [x + hlon, y - hlat], [x + hlon, y + hlat], [x - hlon, y + hlat], [x - hlon, y - hlat]]
        feats.append({"type": "Feature", "properties": c, "geometry": {"type": "Polygon", "coordinates": [ring]}})
    params = {"src": args.src.name, "max_rides": args.max_rides, "cap": args.cap,
              "sidos": [s for s in ("서울특별시", "인천광역시", "경기도") if any(c["sido"] == s for c in cells.values())]}
    stem = args.src.name.removesuffix(".csv.gz")
    out = args.out or args.src.with_name(f"{stem}_k{args.max_rides}_cap{args.cap:.0f}.geojson")
    out.write_text(json.dumps({"type": "FeatureCollection", "params": params, "features": feats}, ensure_ascii=False),
                   encoding="utf-8")
    print(f"{len(feats)} 칸 · 도착지 {', '.join(DESTS)} · 탑승 ≤ {args.max_rides} · 택시비 ≤ {args.cap:,.0f}원 → {out}")


if __name__ == "__main__":
    main()
