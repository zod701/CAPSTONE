"""버스 칸의 출퇴근 차내 시간 배수 (`processed/bus_hop_peak.csv.gz`) — 서울시 TPSS 구간 운행시간(OA-21217)의 평일 08 · 18시 ÷ 14시.

공간 분석은 14시에 맞춘 차내 시간에 이 배수만 곱한다 — 시간의 수준은 건드리지 않는다.
- 노선의 앞뒤 5칸(11칸)을 묶은 합의 비를 쓴다. 칸마다의 비는 잡음이 커서 합이 위로 치우쳤다(월 · 수 · 금으로 맞추고 화 · 목의 10칸 묶음 합계 예측/실측:
  칸마다 1.17 · 묶음 1.04, 로그 절대오차는 둘 다 0.08, 배수 없음 0.13). 묶음에 값 있는 칸이 3개 미만이면 표로 메운다
- 표 = 방향(광화문에서 멀어짐 · 가까워짐) × 시작 정류장의 광화문 거리대 × 노선 부류(`FAST` · 그 밖). TPSS 는 서울 노선과 경기 노선의 서울 안 칸이 대부분이고
  인천 노선은 없다 — 표는 그 칸들로 맞춘 값을 경기 · 인천 안쪽에 옮겨 쓰는 근사다(`src` = t)
- 폴더의 주간 파일을 모두 읽고 평일 중 공휴일(`HOLIDAYS`)과 그 전날(`PRE_HOLIDAYS`)을 뺀다. 공휴일은 그날 총 운행횟수가 평일의 0.68–0.78 이었고,
  전날은 운행은 평일 그대로인데 08시 ÷ 14시가 0.92–1.02 로 낮았다(다른 평일 1.00–1.39, 2026-09-21 ~ 10-25 18일).
  날마다 08시가 크게 흔들려 한 주만 쓰면 표 값이 주마다 최대 0.20 움직였다

    .\\.venv\\Scripts\\python.exe data\\scripts\\build_bus_peak.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "data" / "csv" / "서울시 노선별 정류장 구간별 평균 운행시간 정보"   # tpss_route_section_speedh_<주>.csv 전부
HOLIDAYS = {"20260924", "20260925", "20261005", "20261009"}        # 추석 · 개천절 대체 · 한글날 (평일인 날만)
PRE_HOLIDAYS = {"20260923", "20261002", "20261008"}                # 공휴일 · 연휴 전 평일
STOPS = ROOT / "data" / "processed" / "bus_route_stops.csv"
OUT = ROOT / "data" / "processed" / "bus_hop_peak.csv.gz"
CBD = (126.9765, 37.5709)   # 광화문
ZONE_KM = (7, 15, 25)
FAST = {"광역", "직행", "좌석", "공항", "시외", "급행간선"}
WINDOW = 5                  # 앞뒤 칸 수
MIN_HOPS = 3
HOURS = {"am": "08", "pm": "18"}
KEY = ["route_id", "stop_key", "next_key"]


def read_week(path):
    c = pd.read_csv(path, encoding="cp949", nrows=0).columns
    want = {"08": c[5 + 8], "14": c[5 + 14], "18": c[5 + 18]}
    assert all(want[h].endswith(f"{h}시") for h in want), want
    t = pd.read_csv(path, encoding="cp949", dtype=str, usecols=[*c[:4], *want.values()])
    t = t.rename(columns={c[0]: "date", c[1]: "route_id", c[2]: "stop_key", c[3]: "next_key", **{v: k for k, v in want.items()}})
    t = t[(t.date.pipe(pd.to_datetime, format="%Y%m%d").dt.dayofweek < 5) & ~t.date.isin(HOLIDAYS | PRE_HOLIDAYS)].copy()
    t[list(want)] = t[list(want)].apply(pd.to_numeric, errors="coerce")
    t[list(want)] = t[list(want)].where(t[list(want)] > 0)   # 0 은 값 없음
    return t


def load_tpss():
    files = sorted(SRC.glob("tpss_route_section_speedh_*.csv"))
    t = pd.concat([read_week(p) for p in files])
    print(f"{len(files)}주 · 평일 {t.date.nunique()}일 ({t.date.min()} ~ {t.date.max()})")
    return t.groupby(KEY)[["08", "14", "18"]].median()


def km_to_cbd(lon, lat):
    return np.hypot((lon - CBD[0]) * 88.0, (lat - CBD[1]) * 111.0)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    rs = pd.read_csv(STOPS, dtype=str, encoding="utf-8-sig")
    rs["seq"] = rs.seq.astype(int)
    rs[["lon", "lat"]] = rs[["lon", "lat"]].apply(pd.to_numeric, errors="coerce")
    rs = rs.dropna(subset=["lon", "lat"]).sort_values(["route_id", "seq"])
    for col, src in (("next_key", "stop_key"), ("lon2", "lon"), ("lat2", "lat")):
        rs[col] = rs.groupby("route_id")[src].shift(-1)
    h = rs.dropna(subset=["next_key", "lon2"]).join(load_tpss(), on=KEY)
    d1, d2 = km_to_cbd(h.lon, h.lat), km_to_cbd(h.lon2, h.lat2)
    h["out"], h["zone"], h["fast"] = d2 > d1, np.digitize(d1, ZONE_KM), h.route_type.isin(FAST)
    g = ["out", "zone", "fast"]
    for name, hh in HOURS.items():
        both = h[hh].notna() & h["14"].notna()
        a, b = h[hh].where(both), h["14"].where(both)
        roll = lambda s, how: getattr(s.groupby(h.route_id).rolling(2 * WINDOW + 1, center=True, min_periods=1), how)().droplevel(0)
        win = (roll(a, "sum") / roll(b, "sum")).where(roll(b, "count") >= MIN_HOPS)
        table = a.groupby([h[c] for c in g]).sum() / b.groupby([h[c] for c in g]).sum()
        filled = h[g].join(table.rename("t"), on=g)["t"]
        h["r_" + name] = win.fillna(filled).clip(0.7, 2.0).round(3)
        h["w_" + name] = win.notna()
        print(f"{name}: 묶음 값 {win.notna().mean():.1%} · 표 {len(table)}칸 · 비어 있는 칸 {h['r_' + name].isna().sum()}")
        print(table.round(3).unstack("fast").to_string())
    h["src"] = np.where(h.w_am & h.w_pm, "w", "t")
    out = h.drop_duplicates(KEY)[KEY + ["r_am", "r_pm", "src"]]
    out.to_csv(OUT, index=False, encoding="utf-8", compression="gzip")
    print("→", OUT.name, len(out), "칸, 묶음 값", f"{(out.src == 'w').mean():.1%}")


if __name__ == "__main__":
    main()
