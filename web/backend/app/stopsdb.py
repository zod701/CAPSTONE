"""정제된 정류소·역 CSV 를 메모리에 올리고 공간·이름 색인을 둔다.

`name_key`·별칭 색인은 미정차(가상) 정류소를 뺀다 — 타고 내릴 수 없는 노드라 매칭 후보가 아니다.
공간 색인은 전체 행으로 만들고 질의할 때 가상 행을 거른다.
"""
import csv
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from geoutil import GridIndex
from textnorm import name_key

KINDS = ("bus", "subway")
HEAD_LEN = 4  # 접두 일치(절단된 이름) 후보를 찾는 이름 키 앞부분 길이 — resolver.PREFIX_MIN_LEN 과 같다
_KST = timezone(timedelta(hours=9))  # config 를 import 하지 않으므로 고정 오프셋을 여기 둔다
_FILES = {"bus": "bus_stops.csv", "subway": "subway_stations.csv"}


def _read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _flag(v):
    return str(v).strip().lower() in ("1", "true")


def _line_norm(s):
    """차량 이름·노선군 비교용: 공백 제거, 소문자, 앞의 '수도권' 제거."""
    return "".join((s or "").split()).lower().removeprefix("수도권")


class StopsDB:
    def __init__(self, bus_rows, subway_rows, line_groups):
        self._rows = {k: [] for k in KINDS}
        self._by_key = {k: defaultdict(list) for k in KINDS}
        self._by_alias = {k: defaultdict(list) for k in KINDS}
        self._by_head = {k: defaultdict(list) for k in KINDS}
        for kind, src in zip(KINDS, (bus_rows, subway_rows)):
            rows = self._rows[kind]
            for r in src:
                r = dict(r)
                r["lat"] = float(r["lat"])
                r["lon"] = float(r["lon"])
                r["is_virtual"] = kind == "bus" and _flag(r.get("is_virtual"))
                if kind == "subway":
                    r["is_transfer"] = _flag(r.get("is_transfer"))
                r["alias_keys"] = {name_key(a, kind) for a in (r.get("aliases") or "").split("|") if a}
                i = len(rows)
                rows.append(r)
                if not r["is_virtual"]:
                    self._by_key[kind][r["name_key"]].append(i)
                    for k in r["alias_keys"]:
                        self._by_alias[kind][k].append(i)
                    if len(r["name_key"]) >= HEAD_LEN:
                        self._by_head[kind][r["name_key"][:HEAD_LEN]].append(i)
        self._grid = {k: GridIndex([r["lon"] for r in v], [r["lat"] for r in v]) for k, v in self._rows.items()}
        self.counts = {k: len(v) for k, v in self._rows.items()}
        self.data_mtime = None

        self._colors = {}
        self._groups = {}  # 정규화한 이름 → line_group (먼저 나온 것 우선)
        for g in line_groups:
            group = g["line_group"]
            self._colors[group] = g.get("color") or None
            for n in [group, *(g.get("kakao_names") or "").split("|")]:
                if _line_norm(n):
                    self._groups.setdefault(_line_norm(n), group)

    @classmethod
    def load(cls, processed_dir, ref_dir):
        """processed/ 의 두 CSV 와 ref/line_groups.csv 를 읽는다. 없으면 FileNotFoundError."""
        paths = [Path(processed_dir) / _FILES[k] for k in KINDS]
        db = cls(*(_read_csv(p) for p in paths), _read_csv(Path(ref_dir) / "line_groups.csv"))
        newest = max(p.stat().st_mtime for p in paths)
        db.data_mtime = datetime.fromtimestamp(newest, _KST).isoformat(timespec="seconds")
        return db

    def row(self, kind, i):
        return self._rows[kind][i]

    def bbox(self, kind, minlon, minlat, maxlon, maxlat, include_virtual=False):
        rows = self._rows[kind]
        # 넓은 박스는 GridIndex.bbox 가 셀 개수를 보고 선형 탐색으로 바꾼다
        idx = sorted(self._grid[kind].bbox(minlon, minlat, maxlon, maxlat))
        return [i for i in idx if include_virtual or not rows[i]["is_virtual"]]

    def near(self, kind, lon, lat, r_m, include_virtual=False):
        rows = self._rows[kind]
        return [(i, d) for i, d in self._grid[kind].near(lon, lat, r_m)
                if include_virtual or not rows[i]["is_virtual"]]

    def by_key(self, kind, key):
        return list(self._by_key[kind].get(key, ()))

    def by_alias(self, kind, key):
        return list(self._by_alias[kind].get(key, ()))

    def by_head(self, kind, head):
        """이름 키가 head(HEAD_LEN 글자)로 시작하는 행 — 접두 일치 후보."""
        return list(self._by_head[kind].get(head, ()))

    def kakao_group(self, vehicle_name):
        return self._groups.get(_line_norm(vehicle_name))

    def group_color(self, group):
        return self._colors.get(group)
