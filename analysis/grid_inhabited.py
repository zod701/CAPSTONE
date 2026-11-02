"""grid_layer.py 의 GeoJSON → 사람이 사는 칸만 남긴 GeoJSON. 칸마다 인구(`pop`)를 붙인다.

빼는 칸 두 가지:
- 인구 0 칸: `processed/pop_grid.csv` 에 없거나 0 — 산지 · 농지처럼 아무도 출발하지 않는 칸이 지역 값을 흐린다
- 육지 연결로가 없는 섬의 칸: 시도 경계를 합친 땅덩어리 중 본토(가장 큰 덩어리)도 아니고 `ref/island_links.csv` 의 점도 품지 않은 덩어리.
  내부 모델은 배를 모르고 택시를 직선거리로 재서 이런 섬에도 값이 나온다. 경계 합치기로 본토에 붙는 강화도는 표에 없어도 된다

    .\\.venv\\Scripts\\python.exe analysis\\grid_inhabited.py analysis\\out\\grid_cbd_평시_500m_k4_capr0.3.geojson

→ `grid_cbd_평시_500m_거주_k4_capr0.3.geojson` (같은 폴더). grid_maps_run.py 로 그대로 그린다.
"""
import argparse
import csv
import json
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent.parent
SIDOS = ("서울특별시", "인천광역시", "경기도")


def island_parts():
    """육지 연결로가 없는 땅덩어리들."""
    fc = json.loads((ROOT / "data" / "processed" / "admin_sgg.geojson").read_text(encoding="utf-8"))["features"]
    land = unary_union([shape(f["geometry"]).buffer(0) for f in fc if f["properties"]["sido_nm"] in SIDOS])
    parts = sorted(land.geoms, key=lambda g: -g.area)[1:]
    with (ROOT / "data" / "ref" / "island_links.csv").open(encoding="utf-8", newline="") as f:
        links = [Point(float(r["lon"]), float(r["lat"])) for r in csv.DictReader(f)]
    return [g for g in parts if not any(g.contains(p) for p in links)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="사람이 사는 칸만 남기기")
    ap.add_argument("src", type=Path)
    args = ap.parse_args(argv)
    with (ROOT / "data" / "processed" / "pop_grid.csv").open(encoding="utf-8", newline="") as f:
        pop = {r["cell"]: int(r["pop"]) for r in csv.DictReader(f)}
    tree = STRtree(island_parts())
    src = json.loads(args.src.read_text(encoding="utf-8"))
    kept, n_island, pop_island, n_empty = [], 0, 0, 0
    for feat in src["features"]:
        p = feat["properties"]
        v = pop.get(p["cell"], 0)
        if len(tree.query(Point(p["lon"], p["lat"]), predicate="intersects")):
            n_island += 1
            pop_island += v
        elif v == 0:
            n_empty += 1
        else:
            p["pop"] = v
            kept.append(feat)
    src["features"] = kept
    out = args.src.with_name(args.src.name.replace("_k", "_거주_k", 1))
    out.write_text(json.dumps(src, ensure_ascii=False), encoding="utf-8")
    print(f"섬 {n_island} 칸({pop_island:,}명) · 인구 0 {n_empty} 칸 빼고 {len(kept)} 칸({sum(f['properties']['pop'] for f in kept):,}명) → {out.name}")


if __name__ == "__main__":
    main()
