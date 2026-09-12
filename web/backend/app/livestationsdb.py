"""도시철도 실시간 도착 역 매핑표(processed/subway_live_stations.csv)를 메모리에 올린다 — 역 id → (실시간 역명, 노선 id).

실시간 API 는 **역명 정확 일치**만 받고 우리 DB 와 이름이 다른 역이 있다(DB '능길' ↔ 실시간 '신길온천').
환승역은 역명이 같아 노선(subwayId)으로 걸러야 하므로 둘을 함께 둔다.
`live_name` 이 빈 행(실시간을 제공하지 않는 노선·역 — 용인에버라인·의정부경전철 …)은 담지 않는다 → `live_for` 가 None.
"""
import csv
from pathlib import Path

FILE = "subway_live_stations.csv"


class LiveStationsDB:
    def __init__(self, rows):
        self._live = {}   # station_id → (실시간 역명, subwayId)
        for r in rows:
            name = (r["live_name"] or "").strip()
            if name:
                self._live.setdefault(r["station_id"],
                                      (name, (r["live_subway_id"] or "").strip() or None))

    @classmethod
    def load(cls, processed_dir):
        """processed/subway_live_stations.csv 를 읽는다. 없으면 FileNotFoundError."""
        with open(Path(processed_dir) / FILE, encoding="utf-8-sig", newline="") as f:
            return cls(csv.DictReader(f))

    def live_for(self, station_id):
        """역의 (실시간 역명, subwayId). 실시간 도착을 제공하지 않는 역이면 None."""
        return self._live.get(station_id)
