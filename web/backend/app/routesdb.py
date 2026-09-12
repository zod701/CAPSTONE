"""버스 노선–정류소 순서표(processed/bus_route_stops.csv)를 메모리에 올린다 — 정류장 → 경유 노선, 노선 → 정류장.

미정차(통과 노드, is_virtual=1)는 타고 내릴 수 없어 뺀다. 한 노선에 같은 정류장이 두 번(회차해 되돌아옴) 나오면 처음 한 번만 둔다.
파일은 노선마다 운행 순서로 쓰여 있다(data/README.md) — 그 순서를 그대로 쓴다.
정류장 이름·좌표는 순서표 값(in_db=1 이면 정류소 DB 값, 0 이면 원천 값)을 정류장마다 한 번만 둔다.
노선 유형 `route_type` 은 카카오 버스 유형 이름에 맞춘 값(웹 BUS_COLOR 의 키) — 이 열이 없는 옛 순서표면 빈칸.
매칭이 진행 방향을 가릴 때 쓰도록 이름 색인(`routes_by_name`) · 정류장별 운행 순서(`order_of`) ·
운행 순서 전체(`sequence`, 회차해 다시 지나는 정류장을 반복해 담는다)도 둔다.
"""
import csv
import re
import sys
from pathlib import Path

FILE = "bus_route_stops.csv"


def _name_order(name):
    """노선 이름 자연 정렬 키: 숫자 조각은 수로 비교한다 (9-1 < 10 < 380)."""
    return [(0, int(t)) if t.isdigit() else (1, t) for t in re.split(r"(\d+)", name) if t]


def _name_norm(name):
    """노선 이름 비교용: 공백 제거, 소문자 (카카오 차량 이름 ↔ 순서표 노선 이름)."""
    return "".join((name or "").split()).lower()


def _name_loose(name):
    """이름이 그대로 안 맞을 때 쓰는 느슨한 형태: 괄호 안(운행 패턴·경유지)과 끝의 변형 글자를 뗀다.
    카카오는 요일별 운행을 `11-A(평일 출퇴근)`·`11-C(주말)` 로 갈라 부르지만 순서표에는 `11` 하나뿐이다
    (반대로 순서표에만 `(임진각)`·`(예약)` 이 붙은 이름이 244개 있다)."""
    return re.sub(r"(?<=[0-9])-?[a-z]$", "", re.sub(r"\([^)]*\)", "", _name_norm(name)))


class RoutesDB:
    def __init__(self, rows):
        self._stops = {}   # stop_key → (이름, lat, lon)
        self._seq = {}     # route_id → [stop_key…] (운행 순서 그대로, 회차해 다시 지나는 정류장은 반복)
        self._order = {}   # route_id → {stop_key: (첫 순서, 마지막 순서)} (운행 순서, 회차해 다시 지나면 둘이 다르다)
        meta = {}          # route_id → (이름, 원천, 유형)
        seen = {}          # route_id → 지금까지 읽은 정류장 수
        for r in rows:
            if r["is_virtual"].strip() == "1":
                continue
            key = sys.intern(r["stop_key"])  # 같은 정류장 키 문자열을 노선끼리 공유
            if key not in self._stops:
                self._stops[key] = (r["name"], float(r["lat"]), float(r["lon"]))
            rid = r["route_id"]
            if rid not in meta:
                meta[rid] = (r["route_name"], r["source"], r.get("route_type") or "")
                self._order[rid], self._seq[rid] = {}, []
            order = self._order[rid]
            i = seen[rid] = seen.get(rid, -1) + 1
            order[key] = (order[key][0], i) if key in order else (i, i)
            self._seq[rid].append(key)
        self._routes = {rid: (*meta[rid], tuple(o)) for rid, o in self._order.items()}  # route_id → (이름, 원천, 유형, 정류장 키…)
        self._seq = {rid: tuple(ks) for rid, ks in self._seq.items()}
        by_name, by_loose = {}, {}
        for rid, (name, *_) in self._routes.items():
            by_name.setdefault(_name_norm(name), []).append(rid)
            by_loose.setdefault(_name_loose(name), []).append(rid)
        self._by_name = {n: tuple(v) for n, v in by_name.items()}
        self._by_loose = {n: tuple(v) for n, v in by_loose.items()}
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

    def routes_by_name(self, name):
        """이름이 같은 노선 — 같은 번호가 시·군마다 있어 여러 개다(마을 '5' 는 21개).
        그대로 맞는 이름이 없으면 괄호·변형 글자를 뗀 이름으로 한 번 더 찾는다(_name_loose). 없으면 빈 튜플."""
        return self._by_name.get(_name_norm(name)) or self._by_loose.get(_name_loose(name), ())

    def sequence(self, route_id):
        """노선의 정류장 키를 운행 순서 그대로 (회차해 다시 지나는 정류장은 반복). 모르는 노선이면 빈 튜플."""
        return self._seq.get(route_id, ())

    def stop(self, stop_key):
        """정류장의 (이름, lat, lon) — 순서표 값(정류소 DB 에 없는 정류소도 있다). 모르면 None."""
        return self._stops.get(stop_key)

    def order_of(self, route_id):
        """노선의 {정류장 키: (첫 순서, 마지막 순서)}. 경기 순서표는 상행 뒤에 하행이 이어지므로
        길 건너 반대 방향 정류장은 순서가 크게 다르다 — 승차 → 하차 방향 판정에 쓴다."""
        return self._order.get(route_id, {})

    def name_of(self, route_id):
        """노선 이름 — 모르는 노선이면 None. 같은 번호가 시·군마다 있어 이름은 노선을 하나로 가리지 못한다."""
        r = self._routes.get(route_id)
        return r[0] if r else None

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
