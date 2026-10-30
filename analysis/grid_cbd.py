"""1 km 격자 × 도심 세 곳 — 대중교통 · 택시 · 둘을 섞은 경로의 시간과 요금 (내부 추정만, 카카오 0).

출발점은 서울 · 인천 · 경기 경계 안 1 km 격자의 중심이다. 도착지는 광화문(CBD) · 강남(GBD) · 여의도(YBD).
탐색에는 이동 선호(VOT · 환승 벌점)를 넣지 않는다 — 시간만 최소로 하고, 선호는 분석에서 고르는 하이퍼파라미터로 남긴다:

- **탑승 횟수 상한** `max_rides` (1~4): 노선 단위 탐색(`algo.reach`)의 회차가 곧 탑승 횟수라, 한 번 탐색하며 회차마다
  'k 번까지 타고' 의 최소 시간을 적는다.
- **택시 요금 상한** `cap_first` · `cap_last` (`CAPS`, 택시 한 번당): 출발 쪽은 상한마다 탐색을 따로 한다 — 그 요금 안에 택시로 갈 수
  있는 정류장(30분 안)만 택시 시간을 표지로 넣고 출발한다. 도착 쪽은 탐색 뒤 그 요금 안에 택시로 도착지에 가는 내릴 정류장을 고른다.
- **출발 시각** (`--band` 평시 · 첨두 · 새벽, 또는 `--at`): 배차 · 운행 시간 · 택시 시간과 요금이 시각마다 달라 시각마다 따로 돌린다.

한 행 = (칸, 도착지, 수단, cap_first, cap_last, max_rides) 의 최소 시간 경로 하나. 수단:

- `transit`: 대중교통만 (걸어서 타고 걸어서 내린다) · `taxi`: 택시만 (상한 없이 한 행)
- `first`: 택시 → 대중교통 · `last`: 대중교통 → 택시 · `both`: 택시 → 대중교통 → 택시

`rides` 는 고른 경로가 실제로 탄 횟수(≤ max_rides, 0 이면 걷거나 택시만), `transit_fare` 는 그 경로의 대중교통 요금,
`taxi_fare` 는 택시 요금 합이다. 배차를 모르는 노선(기본 대기)이 낀 경로는 `uncertain` 으로 표시한다(method.md E35).
중간에 택시를 타는 꼴(대중교통 → 택시 → 대중교통)은 넣지 않았다. 통합요금 밖 노선(공항 · 시외 · 관광 버스)은 탐색에서 뺀다.

    .\\.venv\\Scripts\\python.exe analysis\\grid_cbd.py --workers 12 --band 평시
"""
import argparse
import csv
import gzip
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "web" / "backend", ROOT / "data" / "scripts"):
    sys.path.insert(0, str(p))

from geoutil import haversine_m  # noqa: E402

CELL_M = 1000.0
ACCESS_M = 1000.0            # 걸어서 타고 내리는 정류장 반경 — 내부 추정 검증(method.md E34)과 같은 값
MAX_RIDES = 4
LIMIT_S = 4 * 3600.0
CAPS = (7000, 10000, 15000, 20000, 30000)   # 택시 한 번의 요금 상한(원) — 분석에서 고른다
BANDS = {"평시": "2026-11-04T14:00", "첨두": "2026-11-04T08:00", "새벽": "2026-11-04T05:30"}   # 수요일
DESTS = {"CBD": ("광화문", 126.9765, 37.5709), "GBD": ("강남", 127.0276, 37.4979), "YBD": ("여의도", 126.9243, 37.5216)}
SIDOS = ("서울특별시", "인천광역시", "경기도")
# 통합요금 · 환승할인 밖의 노선은 뺀다 — 시간만 보는 탐색이 정류장이 적은 공항 리무진을 시내 짧은 구간에 끼워 탔다(method.md E45)
EXCLUDED_TYPES = frozenset({"공항", "시외", "관광"})
_G = {}


def grid_cells(boundaries):
    """서울 · 인천 · 경기 경계 안 1 km 격자 중심 → [(cell_id, lon, lat, 시도, 시군구)]. 격자는 위도 37.5° 의 등장방형 근사다."""
    import json
    from regions import RegionIndex
    fc = json.loads(Path(boundaries).read_text(encoding="utf-8"))
    xs, ys = [], []
    for f in fc["features"]:
        if f["properties"]["sido_nm"] in SIDOS:
            from shapely.geometry import shape
            b = shape(f["geometry"]).bounds
            xs += [b[0], b[2]]
            ys += [b[1], b[3]]
    dlat = math.degrees(CELL_M / 6371008.8)
    dlon = dlat / math.cos(math.radians(37.5))
    pts = [(min(xs) + (i + 0.5) * dlon, min(ys) + (j + 0.5) * dlat)
           for j in range(int((max(ys) - min(ys)) / dlat) + 1) for i in range(int((max(xs) - min(xs)) / dlon) + 1)]
    idx = RegionIndex(boundaries, snap_max_m=0.0)
    out = []
    for (lon, lat), r in zip(pts, idx.assign([p[0] for p in pts], [p[1] for p in pts])):
        if r.get("sido_nm") in SIDOS and r.get("region_method") == "contain":
            out.append(("%.5f_%.5f" % (lon, lat), lon, lat, r["sido_nm"], r["sgg_nm"]))
    return out


def _init(at_iso):
    import algo  # noqa: F401
    from algo import anchors, fares, reach, search
    from algo.cli import load_dbs
    from app.config import load_settings
    dbs = load_dbs(load_settings())
    net = reach.network(dbs["stops"], dbs["routes"], dbs["lines"], EXCLUDED_TYPES)
    at = datetime.fromisoformat(at_iso)
    _G.update(anchors=anchors, fares=fares, reach=reach, search=search, dbs=dbs, net=net, at=at,
              waits=reach._Waits(net, dbs["headway"], dbs["hours"], at))
    _G["dests"] = {k: _dest(lon, lat) for k, (_, lon, lat) in DESTS.items()}
    _G["outs"] = {n for d in _G["dests"].values() for n in (*d["walk"], *(t[0] for t in d["taxi"]))}


def _nodes_near(pt, radius_m):
    """pt 반경 안 노선이 서는 정류장 → {정류장: (거리 m, 시군구)}."""
    dbs, net, reach = _G["dbs"], _G["net"], _G["reach"]
    out = {}
    for kind in ("bus", "subway"):
        for i, dist in dbs["stops"].near(kind, *pt, radius_m):
            row = dbs["stops"].row(kind, i)
            node = (kind, row[reach.ID_COL[kind]])
            if node in net.at and dist < out.get(node, (math.inf,))[0]:
                out[node] = (dist, row.get("sgg_nm"))
    return out


def _taxi(dist_m, sgg_a, sgg_b, connect):
    """택시 한 번 → (시간 초, 요금 원). 시간은 호출 대기 + 이동(+ 내려서 차를 타면 연결 버퍼)."""
    a, at = _G["anchors"], _G["at"]
    return (a.taxi_plan_s(a.taxi_s_for(dist_m, at), connect),
            a.taxi_fare(a.road_km(dist_m), sgg_a, sgg_b, at))


def _cap_index(fare):
    """요금이 들어가는 가장 작은 상한의 번호 (가장 큰 상한도 넘으면 None)."""
    return next((i for i, c in enumerate(CAPS) if fare <= c), None)


def _dest(lon, lat):
    """도착지 → 걸어서 내리는 정류장 {정류장: 도보 초} · 택시로 갈 수 있는 정류장 [(정류장, 택시 초, 택시 요금, 상한 번호)] · 시군구."""
    a = _G["anchors"]
    sgg = (_G["dbs"]["stops"].row("bus", _G["dbs"]["stops"].near("bus", lon, lat, 2000.0)[0][0]) or {}).get("sgg_nm")
    walk = {n: a.walk_s(d) for n, (d, _) in _nodes_near((lon, lat), ACCESS_M).items()}
    reach_m = a.taxi_reach_m(a.T_MAX_S, _G["at"])
    taxi = []
    for n, (d, s) in _nodes_near((lon, lat), reach_m).items():
        if d >= a.TAXI_MIN_M:
            ts, tf = _taxi(d, s, sgg, False)
            if _cap_index(tf) is not None:
                taxi.append((n, ts, tf, _cap_index(tf)))
    return {"pt": (lon, lat), "sgg": sgg, "walk": walk, "taxi": taxi}


def _rounds(seeds):
    """seeds {정류장: 초} 에서 탐색 → 회차 k = 1..MAX_RIDES 마다 {내릴 후보 정류장: (초, 부모)} (일찍 끝나면 앞 회차 값을 잇는다)."""
    reach, net, waits, outs = _G["reach"], _G["net"], _G["waits"], _G["outs"]
    snaps = {}

    def on_round(k, tau, par):
        snaps[k] = {n: (tau[n], par[n]) for n in outs if n in tau}

    if seeds:
        reach.search(net, waits, seeds, limit_s=LIMIT_S, max_rides=MAX_RIDES, on_round=on_round)
    out, last = [], {}
    for k in range(1, MAX_RIDES + 1):
        last = snaps.get(k, last)
        out.append(last)
    return out


def _exits(snap, dest):
    """한 회차의 표지 → (걸어서 내림 (초, 정류장, 0, 0) | None, {상한: 그 요금 안 택시로 내림 (초, 정류장, 택시 초, 택시 요금)})."""
    walk = min(((snap[n][0] + w, n, 0.0, 0.0) for n, w in dest["walk"].items() if n in snap), default=None)
    by_cap = [None] * len(CAPS)
    for n, ts, tf, ci in dest["taxi"]:
        if n in snap and (by_cap[ci] is None or snap[n][0] + ts < by_cap[ci][0]):
            by_cap[ci] = (snap[n][0] + ts, n, ts, tf)
    taxi, best = {}, None
    for ci, c in enumerate(CAPS):   # 상한이 크면 작은 상한의 정류장도 쓸 수 있다
        if by_cap[ci] is not None and (best is None or by_cap[ci][0] < best[0]):
            best = by_cap[ci]
        if best is not None:
            taxi[c] = best
    return walk, taxi


def _path_info(entry, node, cache):
    """표지의 부모 하나 → (승차 수, 대중교통 요금, 배차 모르는 승차가 있나, 시작 정류장)."""
    key = (id(entry), node)   # 출발 표지 ("access",) 는 모든 정류장이 같은 객체라 정류장도 키에 넣는다
    if key not in cache:
        reach, search, fares, net, waits = _G["reach"], _G["search"], _G["fares"], _G["net"], _G["waits"]
        start, path = reach.legs(net, {node: entry}, node)
        rides = [leg for leg in path if leg[0] == "ride"]
        steps, fallback = search._ride_steps(net, waits, path)
        fare = fares.estimate(steps, fallback, at=_G["at"])["value"] if steps else 0.0
        unknown = not all(waits.known(p, i, j) for _, p, i, j in rides)
        cache[key] = (len(rides), fare, unknown, start)
    return cache[key]


def _one(cell):
    cid, lon, lat, sido, sgg = cell
    a = _G["anchors"]
    o = (lon, lat)
    first_taxi = {}
    for n, (d, s) in _nodes_near(o, a.taxi_reach_m(a.T_MAX_S, _G["at"])).items():
        if d >= a.TAXI_MIN_M:
            ts, tf = _taxi(d, sgg, s, True)
            if tf <= CAPS[-1]:
                first_taxi[n] = (ts, tf)
    searches = {None: _rounds({n: a.walk_s(d) for n, (d, _) in _nodes_near(o, ACCESS_M).items()})}
    prev = (None, -1)
    for c in CAPS:   # 상한마다 그 요금 안 정류장만 택시 시간으로 출발 — 정류장이 늘지 않으면 앞 결과를 쓴다
        seeds = {n: ts for n, (ts, tf) in first_taxi.items() if tf <= c}
        searches[c] = searches[prev[0]] if len(seeds) == prev[1] else _rounds(seeds)
        prev = (c, len(seeds))

    rows, cache = [], {}
    for key, dest in _G["dests"].items():
        base = {"cell": cid, "lon": round(lon, 5), "lat": round(lat, 5), "sido": sido, "sgg": sgg, "dest": key,
                "straight_m": round(haversine_m(lon, lat, *dest["pt"]))}
        ts, tf = _taxi(base["straight_m"], sgg, dest["sgg"], False)
        rows.append({**base, "mode": "taxi", "cap_first": "", "cap_last": "", "max_rides": 0, "time_s": round(ts),
                     "rides": 0, "transit_fare": 0, "taxi_fare": round(tf), "uncertain": 0})
        for cap_first, per_round in searches.items():
            for k, snap in enumerate(per_round, 1):
                walk, taxi = _exits(snap, dest)
                for cap_last, ex in ((None, walk), *taxi.items()):
                    if ex is None:
                        continue
                    t, n, _, ltf = ex
                    rides, fare, unknown, start = _path_info(snap[n][1], n, cache)
                    ftf = first_taxi[start][1] if cap_first is not None else 0.0
                    mode = {(False, False): "transit", (True, False): "first", (False, True): "last",
                            (True, True): "both"}[(cap_first is not None, cap_last is not None)]
                    rows.append({**base, "mode": mode, "cap_first": cap_first or "", "cap_last": cap_last or "",
                                 "max_rides": k, "time_s": round(t), "rides": rides, "transit_fare": round(fare),
                                 "taxi_fare": round(ftf + ltf), "uncertain": int(unknown)})
    return rows


COLUMNS = ["cell", "lon", "lat", "sido", "sgg", "dest", "straight_m", "mode", "cap_first", "cap_last", "max_rides",
           "time_s", "rides", "transit_fare", "taxi_fare", "uncertain"]


def main(argv=None):
    ap = argparse.ArgumentParser(description="1 km 격자 × 도심 세 곳 — 대중교통 · 택시 · 하이브리드 (내부 추정)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="고르게 N 칸만 (시험용)")
    ap.add_argument("--sido", choices=SIDOS, default=None, help="이 시도의 칸만 출발점으로 (도착지는 그대로)")
    ap.add_argument("--band", choices=list(BANDS), default=None, help="출발 시각대 — " + " · ".join(
        f"{b} {at[11:]}" for b, at in BANDS.items()) + " (수요일)")
    ap.add_argument("--at", default=None, help="계산 시각 ISO (--band 대신, 모든 OD 에 같은 시각)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    if not (args.band or args.at):
        ap.error("--band 나 --at 중 하나를 준다")
    at = args.at or BANDS[args.band]
    cells = grid_cells(ROOT / "data" / "processed" / "admin_sgg.geojson")
    if args.sido:
        cells = [c for c in cells if c[3] == args.sido]
    if args.limit:
        cells = cells[:: max(1, len(cells) // args.limit)][: args.limit]
    tag = (args.band or at[:16].replace(":", "").replace("-", "")) + (f"_{args.sido[:2]}" if args.sido else "")
    out = args.out or ROOT / "analysis" / "out" / f"grid_cbd_{tag}.csv.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    print("격자 %d 칸 × 도착지 %d = OD %d · 시각 %s · 일꾼 %d → %s" % (len(cells), len(DESTS), len(cells) * len(DESTS), at, args.workers, out),
          flush=True)
    t0, done = time.perf_counter(), 0
    with gzip.open(out, "wt", encoding="utf-8", newline="") as f, \
            ProcessPoolExecutor(args.workers, initializer=_init, initargs=(at,)) as pool:
        w = csv.DictWriter(f, COLUMNS)
        w.writeheader()
        for rows in pool.map(_one, cells, chunksize=8):
            w.writerows(rows)
            done += 1
            if done % 500 == 0:
                el = time.perf_counter() - t0
                print("  %d / %d 칸 · %.0f초 · 남은 예상 %.0f분" % (done, len(cells), el, el / done * (len(cells) - done) / 60), flush=True)
    print("끝 %.0f초" % (time.perf_counter() - t0))


if __name__ == "__main__":
    main()
