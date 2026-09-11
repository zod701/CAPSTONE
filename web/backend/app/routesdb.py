"""버스 노선–정류소 순서표(processed/bus_route_stops.csv)를 메모리에 올린다 — 정류장 → 경유 노선, 노선 → 정류장.

미정차(통과 노드, is_virtual=1)는 타고 내릴 수 없어 뺀다. 한 노선에 같은 정류장이 두 번(회차해 되돌아옴) 나오면 처음 한 번만 둔다.
파일은 노선마다 운행 순서로 쓰여 있다(data/README.md) — 그 순서를 그대로 쓴다.
정류장 이름·좌표는 순서표 값(in_db=1 이면 정류소 DB 값, 0 이면 원천 값)을 정류장마다 한 번만 둔다.
노선 유형 `route_type` 은 카카오 버스 유형 이름에 맞춘 값(웹 BUS_COLOR 의 키) — 이 열이 없는 옛 순서표면 빈칸.
"""
import csv
import re
import sys
from pathlib import Path

FILE = "bus_route_stops.csv"


def _name_order(name):
    """노선 이름 자연 정렬 키: 숫자 조각은 수로 비교한다 (9-1 < 10 < 380)."""
    return [(0, int(t)) if t.isdigit() else (1, t) for t in re.split(r"(\d+)", name) if t]


class RoutesDB:
    def __init__(self, rows):
        self._stops = {}   # stop_key → (이름, lat, lon)
        keys = {}          # route_id → {stop_key: None} (운행 순서, 중복 없음)
        meta = {}          # route_id → (이름, 원천, 유형)
        for r in rows:
            if r["is_virtual"].strip() == "1":
                continue
            key = sys.intern(r["stop_key"])  # 같은 정류장 키 문자열을 노선끼리 공유
            if key not in self._stops:
                self._stops[key] = (r["name"], float(r["lat"]), float(r["lon"]))
            rid = r["route_id"]
            if rid not in meta:
                meta[rid] = (r["route_name"], r["source"], r.get("route_type") or "")
                keys[rid] = {}
            keys[rid].setdefault(key, None)
        self._routes = {rid: (*meta[rid], tuple(ks)) for rid, ks in keys.items()}  # route_id → (이름, 원천, 유형, 정류장 키…)
        rank = {rid: i for i, rid in enumerate(sorted(self._routes, key=lambda rid: (
            _name_order(self._routes[rid][0]), self._routes[rid][1], rid)))}
        by_stop = {}
        for rid, (*_, ks) in self._routes.items():
            for k in ks:
                by_stop.setdefault(k, []).append(rid)
        self._by_stop = {k: tuple(sorted(v, key=rank.__getitem__)) for k, v in by_stop.items()}

    @classmethod
    def load(cls, processed_dir):
        """processed/bus_route_stops.csv 를 읽는다. 없으면 FileNotFoundError."""
        with open(Path(processed_dir) / FILE, encoding="utf-8-sig", newline="") as f:
            return cls(csv.DictReader(f))

    def routes_at(self, stop_key):
        """정류장을 지나는 노선 (이름 자연 정렬). 모르는 정류장·미정차면 빈 목록."""
        return [self._brief(rid) for rid in self._by_stop.get(stop_key, ())]

    def route(self, route_id):
        """노선이 지나는 정류장 (운행 순서). 모르는 노선이면 None."""
        if route_id not in self._routes:
            return None
        stops = []
        for k in self._routes[route_id][-1]:
            name, lat, lon = self._stops[k]
            stops.append({"key": k, "name": name, "lat": lat, "lon": lon})
        return {**self._brief(route_id), "stops": stops}

    def _brief(self, rid):
        name, source, rtype, ks = self._routes[rid]
        return {"id": rid, "name": name, "source": source, "type": rtype, "n_stops": len(ks)}
