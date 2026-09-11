"""거리 계산과 점 격자 색인 (표준 라이브러리만 사용 — 백엔드도 가져다 쓴다)."""
import math
from collections import defaultdict

EARTH_R = 6371008.8  # m


def haversine_m(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    # min(nan, 1.0) 은 nan 을 돌려주지만 min(1.0, nan) 은 1.0 이다 — 순서를 바꾸면 반지구 거리가 나온다
    return 2 * EARTH_R * math.asin(min(math.sqrt(a), 1.0))


def local_xy(lon, lat, lon0, lat0):
    """(lon0, lat0) 기준 등장방형 투영 (m). 수 km 범위에서 충분히 정확하다."""
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * EARTH_R
    y = math.radians(lat - lat0) * EARTH_R
    return x, y


def _point_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def point_polyline_m(lon, lat, pts):
    """점에서 폴리라인([[lon, lat], ...])까지의 최단 거리 (m). 빈 선이면 inf."""
    if not pts:
        return math.inf
    xy = [local_xy(p[0], p[1], lon, lat) for p in pts]
    if len(xy) == 1:
        return math.hypot(*xy[0])
    return min(_point_segment(0.0, 0.0, *xy[i], *xy[i + 1]) for i in range(len(xy) - 1))


class GridIndex:
    """고정 크기 경위도 셀에 점 번호를 담는 색인. bbox·반경 질의용."""

    def __init__(self, lons, lats, cell=0.01):
        self.lons = list(lons)
        self.lats = list(lats)
        if len(self.lons) != len(self.lats):
            raise ValueError("lons and lats must have the same length")
        self.cell = cell
        self._cells = defaultdict(list)
        for i, (x, y) in enumerate(zip(self.lons, self.lats)):
            self._cells[self._key(x, y)].append(i)

    def __len__(self):
        return len(self.lons)

    def _key(self, lon, lat):
        return (math.floor(lon / self.cell), math.floor(lat / self.cell))

    def bbox(self, minlon, minlat, maxlon, maxlat):
        """bbox 안에 드는 점 번호 목록 (순서 무관)."""
        x0, y0 = self._key(minlon, minlat)
        x1, y1 = self._key(maxlon, maxlat)
        # 셀이 점보다 많으면 셀을 도는 대신 점을 훑는다 — 높이 0 인 거대 박스는 면적 0 이어도 셀이 수십억 개다
        if (x1 - x0 + 1) * (y1 - y0 + 1) > len(self.lons):
            return [i for i, (x, y) in enumerate(zip(self.lons, self.lats))
                    if minlon <= x <= maxlon and minlat <= y <= maxlat]
        out = []
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                for i in self._cells.get((cx, cy), ()):
                    if minlon <= self.lons[i] <= maxlon and minlat <= self.lats[i] <= maxlat:
                        out.append(i)
        return out

    def near(self, lon, lat, r_m):
        """반경 r_m 안의 점을 거리순으로 [(번호, 거리 m), ...]."""
        dlat = math.degrees(r_m / EARTH_R)
        dlon = dlat / max(math.cos(math.radians(lat)), 1e-6)
        hits = []
        for i in self.bbox(lon - dlon, lat - dlat, lon + dlon, lat + dlat):
            d = haversine_m(lon, lat, self.lons[i], self.lats[i])
            if d <= r_m:
                hits.append((i, d))
        hits.sort(key=lambda t: t[1])
        return hits
