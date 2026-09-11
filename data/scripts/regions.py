"""좌표 → 시군구 판정 (`admin_sgg.geojson` 기반).

1) 좌표가 비었거나 한국 범위 밖이면 `invalid_coord`
2) 대상 시군(서울 25구·경기 31시군)에 포함 → `contain` (여러 곳에 걸치면 대상 우선, sgg_cd 낮은 쪽)
3) 인접 시도에만 포함 → 제외 `outside_region` (`contained:<시도>`)
4) 어디에도 없으면 가장 가까운 피처를 찾아, 그것이 대상이고 snap_max_m 이내면 `snap`
   (서해안 매립지 정류소가 경계에서 0.4–1.9 km 떨어져 있다), 아니면 제외
"""
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import shape

from geoutil import EARTH_R

LON_RANGE = (124.0, 132.0)
LAT_RANGE = (33.0, 39.0)
BOX_DEG = 0.03  # snap 후보 상자 반폭 (위도 39°에서도 동서 2.5 km 이상)
_ORIGIN = shapely.Point(0.0, 0.0)
_REGION = ("sido_cd", "sido_nm", "sgg_cd", "sgg_nm")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return math.nan


def _hit(p, method, snap_dist_m=None, detail=""):
    return {**{k: p[k] for k in _REGION}, "region_method": method, "snap_dist_m": snap_dist_m,
            "drop_reason": "", "detail": detail}


def _drop(reason, detail=""):
    return {**{k: "" for k in _REGION}, "region_method": "", "snap_dist_m": None,
            "drop_reason": reason, "detail": detail}


class RegionIndex:
    """assign() 뒤 `n_ties` = 둘 이상의 피처에 걸쳐 규칙으로 고른 점 수."""

    def __init__(self, geojson_path, snap_max_m=2000.0):
        fc = json.loads(Path(geojson_path).read_text(encoding="utf-8"))
        self.props = [f["properties"] for f in fc["features"]]
        self.geoms = np.array([shape(f["geometry"]) for f in fc["features"]], dtype=object)
        self.tree = shapely.STRtree(self.geoms)
        self.snap_max_m = snap_max_m
        # 상자가 snap 반경보다 작으면 가까운 대상을 놓친다
        self._box = max(BOX_DEG, math.degrees(snap_max_m / EARTH_R) / math.cos(math.radians(LAT_RANGE[1])))
        order = sorted(range(len(self.props)),
                       key=lambda j: (not self.props[j]["in_scope"], self.props[j]["sgg_cd"]))
        self._rank = {j: r for r, j in enumerate(order)}
        self.n_ties = 0

    def assign(self, lons, lats):
        """점마다 dict: sido_cd, sido_nm, sgg_cd, sgg_nm, region_method('contain'|'snap'|''),
        snap_dist_m(float|None), drop_reason(''|'outside_region'|'invalid_coord'), detail."""
        lon = np.array([_num(v) for v in lons], dtype=float)
        lat = np.array([_num(v) for v in lats], dtype=float)
        ok = (np.isfinite(lon) & np.isfinite(lat) & (lon >= LON_RANGE[0]) & (lon <= LON_RANGE[1])
              & (lat >= LAT_RANGE[0]) & (lat <= LAT_RANGE[1]))
        idx = np.flatnonzero(ok)
        pi, fj = self.tree.query(shapely.points(lon[idx], lat[idx]), predicate="intersects")
        hits = defaultdict(list)
        for i, j in zip(idx[pi].tolist(), fj.tolist()):
            hits[i].append(j)

        out = []
        self.n_ties = 0
        for i, valid in enumerate(ok.tolist()):
            if not valid:
                out.append(_drop("invalid_coord"))
                continue
            js = sorted(hits.get(i, ()), key=self._rank.__getitem__)
            if not js:
                out.append(self._snap(float(lon[i]), float(lat[i])))
                continue
            p = self.props[js[0]]
            detail = ""
            if len(js) > 1:
                self.n_ties += 1
                detail = "tie:" + ",".join(self.props[j]["sgg_cd"] for j in js)
            out.append(_hit(p, "contain", detail=detail) if p["in_scope"]
                       else _drop("outside_region", f"contained:{p['sido_nm']}"))
        return out

    def _snap(self, lon, lat):
        rect = (lon - self._box, lat - self._box, lon + self._box, lat + self._box)
        cands = self.tree.query(shapely.box(*rect), predicate="intersects")
        # 점 기준 등장방형 투영(geoutil.local_xy 와 같은 식)으로 미터 거리
        k = np.array([math.radians(1) * math.cos(math.radians(lat)), math.radians(1)]) * EARTH_R
        best = None
        for j in cands.tolist():
            g = shapely.clip_by_rect(self.geoms[j], *rect)  # 상자 밖 부분은 snap 반경보다 멀다
            if g.is_empty:
                continue
            d = float(shapely.distance(shapely.transform(g, lambda xy: (xy - (lon, lat)) * k), _ORIGIN))
            if best is None or (d, self._rank[j]) < best[:2]:
                best = (d, self._rank[j], j)
        if best is None:
            return _drop("outside_region", "far")
        d, _, j = best
        p = self.props[j]
        if p["in_scope"] and d <= self.snap_max_m:
            return _hit(p, "snap", snap_dist_m=round(d, 1))
        return _drop("outside_region", f"nearest:{p['sido_nm']} {d:.0f} m")
