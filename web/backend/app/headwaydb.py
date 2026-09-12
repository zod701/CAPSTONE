"""배차간격 표(processed/headway_bus.csv · headway_rail.csv)를 메모리에 올린다 — 대기시간 추정용.

버스는 노선 × 요일 유형 → 그 요일의 최소·최대 배차(분)다. 경기 원천은 둘이 다르고(전체의 84%) 서울 원천은
대표 배차 하나뿐이라 최소·최대가 같다. 시간대 구분은 원천에 없다. 0·빈값은 정보 없음이라 담지 않는다.

도시철도는 역 × 노선군 × 방향 × 시간대(5~25시) → 배차(분)다. 우리는 승·하차 역만 알 뿐 그 열차가 GTFS 의
어느 방향(상·하행)인지 가릴 수 없어 조회할 때 방향을 합친다 — 한 방향만 고르면 원천이 회차를 적게 담은 방향에서
배차가 실제보다 크게 나온다(7호선 하행 75 vs 상행 204 회차, data/reports/headway.md).
"""
import csv
from pathlib import Path
from statistics import fmean

BUS_FILE = "headway_bus.csv"
RAIL_FILE = "headway_rail.csv"


def _pos(v):
    """양수 분이면 float, 아니면 None (0 과 빈칸은 정보 없음)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


class HeadwayDB:
    def __init__(self, bus_rows=(), rail_rows=()):
        self._bus = {}    # (route_id, 요일 유형) → 대표 배차(분)
        self._rail = {}   # (station_id, 노선군, 시간) → [방향별 배차(분)…]
        for r in bus_rows:
            lo = _pos(r["headway_min_m"])
            hi = _pos(r["headway_max_m"])
            vals = [v for v in (lo, hi) if v is not None]
            if vals:  # 최소~최대의 중간값 하나로 쓴다 (서울은 둘이 같아 그 값 그대로)
                self._bus[(r["route_id"], r["day_type"])] = fmean(vals)
        for r in rail_rows:
            v = _pos(r["headway_m"])
            if v is not None:
                self._rail.setdefault((r["station_id"], r["line_group"], int(r["hour"])), []).append(v)
        self.counts = {"bus_routes": len({rid for rid, _ in self._bus}), "bus_rows": len(self._bus),
                       "rail_stations": len({s for s, _, _ in self._rail}), "rail_rows": len(self._rail)}

    @classmethod
    def load(cls, processed_dir):
        """두 표를 읽는다. 한쪽만 있으면 그쪽만, 둘 다 없으면 FileNotFoundError."""
        rows = []
        for name in (BUS_FILE, RAIL_FILE):
            path = Path(processed_dir) / name
            if not path.is_file():
                rows.append(())
                continue
            with open(path, encoding="utf-8-sig", newline="") as f:
                rows.append(list(csv.DictReader(f)))
        if not any(rows):
            raise FileNotFoundError(Path(processed_dir) / BUS_FILE)
        return cls(*rows)

    def bus(self, route_id, day_type):
        """노선의 그 요일 유형 배차(분) — 값이 없으면 None (요일 되돌림은 호출자가 정한다)."""
        return self._bus.get((route_id, day_type))

    def rail(self, station_id, line_group, hour):
        """역·노선군의 그 시간대 배차(분) — 방향을 합친 평균. 값이 없으면 None."""
        vals = self._rail.get((station_id, line_group, hour))
        return fmean(vals) if vals else None
