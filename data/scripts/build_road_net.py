"""택시 시간용 도로망 (`processed/road_net.npz`) — KTDB 수도권 네트워크 원본(`data/nodeLink/`)에서 도로 링크만 뽑는다.

원본은 EMME 형식이다: 노드 `a* id x y`, 링크 `a i j 길이km 모드 유형 차로 vdf 속도 용량 ul3` (cp949, 링크는 방향별).
- 좌표계는 파일에 적혀 있지 않은데 KATEC 이다 — 버스정류장에서 가장 가까운 노드까지 중앙 44 m (EPSG:5186 을 111 km 옮겨도 161 m)
- 유형 101 고속 · 102 도시고속 · 103–108 일반도로만 쓴다. 999(존 연결선)와 200 이상(대중교통)은 뺀다
- 묶음은 고속화 × 서울 안팎, 일반도로는 서울 · 간선 밖 · 이면 밖 · 유료다 (링크의 두 끝이 모두 서울이면 서울)
- 통행료는 엑셀(`LINK_배포자료_2024.xlsx`)의 승용차 합산요금뿐이다 — 단위가 분/대(시간 환산값)라 그대로 두고 `roadnet.py` 에서 원으로 바꾼다

    .\\.venv\\Scripts\\python.exe data\\scripts\\build_road_net.py
"""
import json
from pathlib import Path

import numpy as np
import openpyxl
from pyproj import Transformer
from shapely import contains_xy
from shapely.geometry import shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "data" / "nodeLink" / "01. Node_Link data"
OUT = ROOT / "data" / "processed" / "road_net.npz"
KATEC = ("+proj=tmerc +lat_0=38 +lon_0=128 +k=0.9999 +x_0=400000 +y_0=600000 +ellps=bessel +units=m "
         "+towgs84=-115.80,474.99,674.11,1.16,-2.31,-1.63,6.43 +no_defs")
BBOX = (125.9, 36.7, 128.0, 38.4)   # 수도권과 그 둘레 — 경기 끝에서 돌아가는 길까지 담는다
GROUPS = ("고속화 서울", "고속화 밖", "일반 서울", "간선 밖", "이면 밖", "유료 일반도로")


def group(kind, toll, seoul):
    """신호 없이 달리는 길(고속 · 도시고속 · 램프 108)을 고속화로 묶는다. 통행료 있는 일반도로(신월여의지하도로는 유형 103)는 막히지 않아 따로 둔다."""
    if kind in (101, 102, 108):
        return 0 if seoul else 1
    if toll > 0:
        return 5
    if seoul:   # 서울의 이면 링크는 34개뿐이라 일반도로 하나로 둔다
        return 2
    return 3 if kind <= 106 else 4


def main():
    ids, xs, ys = [], [], []
    with (SRC / "2024node.txt").open(encoding="cp949") as f:
        for line in f:
            p = line.split()
            if p and p[0].startswith("a"):
                ids.append(int(p[1]))
                xs.append(float(p[2]))
                ys.append(float(p[3]))
    lon, lat = Transformer.from_crs(KATEC, 4326, always_xy=True).transform(np.array(xs), np.array(ys))
    keep = (lon > BBOX[0]) & (lat > BBOX[1]) & (lon < BBOX[2]) & (lat < BBOX[3])
    index = {n: i for i, n in enumerate(np.array(ids)[keep])}
    lon, lat = lon[keep], lat[keep]
    fc = json.loads((ROOT / "data" / "processed" / "admin_sgg.geojson").read_text(encoding="utf-8"))["features"]
    seoul = unary_union([shape(c["geometry"]).buffer(0) for c in fc if c["properties"]["sido_nm"] == "서울특별시"])
    in_seoul = contains_xy(seoul, lon, lat)
    toll = {}
    sheet = openpyxl.load_workbook(SRC / "LINK_배포자료_2024.xlsx", read_only=True).worksheets[0]
    for r in sheet.iter_rows(min_row=3, values_only=True):
        if r[23]:
            toll[(r[0], r[1])] = float(r[23])
    fr, to, free_s, grp, length, toll_min = [], [], [], [], [], []
    local = np.zeros(len(lon), bool)
    with (SRC / "2024link.txt").open(encoding="cp949") as f:
        for line in f:
            p = line.split()
            if not p or p[0] != "a" or not 101 <= int(p[5]) <= 108:
                continue
            i, j = index.get(int(p[1])), index.get(int(p[2]))
            km, kmh = float(p[3]), float(p[8])
            if i is None or j is None or km <= 0 or kmh <= 0:
                continue
            fr.append(i)
            to.append(j)
            free_s.append(km / kmh * 3600)
            length.append(km)
            won = toll.get((int(p[1]), int(p[2])), 0.0)
            toll_min.append(won)
            grp.append(group(int(p[5]), won, in_seoul[i] and in_seoul[j]))
            if int(p[5]) >= 103:   # 고속도로는 아무 데서나 타지 못한다 — 출발 · 도착점은 일반도로 노드에 붙인다
                local[i] = local[j] = True
    OUT.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT, lon=lon, lat=lat, fr=np.array(fr, np.int32), to=np.array(to, np.int32),
                        free_s=np.array(free_s), grp=np.array(grp, np.int8), km=np.array(length), toll_min=np.array(toll_min), local=local)
    print(f"노드 {len(lon):,} · 링크 {len(fr):,} · 묶음별 링크 {dict(zip(GROUPS, np.bincount(grp).tolist()))} → {OUT}")


if __name__ == "__main__":
    main()
