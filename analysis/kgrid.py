"""국가 격자 좌표계 EPSG:5179 (Korea 2000 / Unified CS — GRS80 횡메르카토르) ↔ 경위도. pyproj 없이 쓴다.

통계청 인구 격자 등 공공 격자가 이 좌표의 미터 단위로 칸을 나눈다 — 격자 칸을 이 좌표의 배수로 깔면 그 자료와 칸이 맞물린다.
식은 Snyder(1987) 횡메르카토르 급수. 수도권 범위에서 QGIS(PROJ) 변환과 cm 단위로 같다(analysis/grid_cbd.py 의 격자에서 확인).
"""
import math

A = 6378137.0
F = 1 / 298.257222101
E2 = F * (2 - F)
EP2 = E2 / (1 - E2)
K0 = 0.9996
LAT0, LON0 = math.radians(38.0), math.radians(127.5)
FE, FN = 1_000_000.0, 2_000_000.0


def _arc(phi):
    """적도에서 위도 phi 까지의 자오선 길이(m)."""
    e4, e6 = E2 * E2, E2 ** 3
    return A * ((1 - E2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi
                - (3 * E2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * phi)
                + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * phi)
                - (35 * e6 / 3072) * math.sin(6 * phi))


M0 = _arc(LAT0)


def to_xy(lon, lat):
    """경위도(도) → EPSG:5179 (x, y) m."""
    phi, lam = math.radians(lat), math.radians(lon)
    n = A / math.sqrt(1 - E2 * math.sin(phi) ** 2)
    t, c = math.tan(phi) ** 2, EP2 * math.cos(phi) ** 2
    a = math.cos(phi) * (lam - LON0)
    x = FE + K0 * n * (a + (1 - t + c) * a ** 3 / 6 + (5 - 18 * t + t * t + 72 * c - 58 * EP2) * a ** 5 / 120)
    y = FN + K0 * (_arc(phi) - M0 + n * math.tan(phi) * (a * a / 2 + (5 - t + 9 * c + 4 * c * c) * a ** 4 / 24
                                                         + (61 - 58 * t + t * t + 600 * c - 330 * EP2) * a ** 6 / 720))
    return x, y


def to_lonlat(x, y):
    """EPSG:5179 (x, y) m → 경위도(도)."""
    mu = (M0 + (y - FN) / K0) / (A * (1 - E2 / 4 - 3 * E2 ** 2 / 64 - 5 * E2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - E2)) / (1 + math.sqrt(1 - E2))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu) + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu) + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    s = math.sin(phi1)
    n1, t1, c1 = A / math.sqrt(1 - E2 * s * s), math.tan(phi1) ** 2, EP2 * math.cos(phi1) ** 2
    r1 = A * (1 - E2) / (1 - E2 * s * s) ** 1.5
    d = (x - FE) / (n1 * K0)
    phi = phi1 - (n1 * math.tan(phi1) / r1) * (d * d / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * EP2) * d ** 4 / 24
                                               + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * EP2 - 3 * c1 * c1) * d ** 6 / 720)
    lam = LON0 + (d - (1 + 2 * t1 + c1) * d ** 3 / 6
                  + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * EP2 + 24 * t1 * t1) * d ** 5 / 120) / math.cos(phi1)
    return math.degrees(lam), math.degrees(phi)
