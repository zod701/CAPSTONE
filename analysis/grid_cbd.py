"""1 km 격자 × 도심 세 곳 — 대중교통 · 택시 · 둘을 섞은 경로의 시간과 요금 (내부 추정만, 카카오 0).

출발점은 서울 · 경기 경계 안 1 km 격자의 중심이다(인천은 버스 노선표가 없어 뺀다). 도착지는 광화문(CBD) · 강남(GBD) · 여의도(YBD).
출발점마다 노선 단위 탐색(`algo.reach`)을 두 번 한다 — 걸어서 시작(출발지 1 km 안 정류장) · 택시로 시작(택시 30분 안 정류장,
택시 시간과 요금을 일반화 비용으로 넣는다). 그 결과로 다섯 가지를 낸다:

- `transit`: 대중교통만 (걸어서 타고 걸어서 내린다)
- `taxi`: 택시만
- `first`: 택시 → 대중교통 (출발 쪽 택시)
- `last`: 대중교통 → 택시 (도착 쪽 택시)
- `both`: 택시 → 대중교통 → 택시

중간에 택시를 타는 꼴(대중교통 → 택시 → 대중교통)은 넣지 않았다. 경로는 일반화 비용(시간 + 택시 요금 ÷ VOT)으로 고르고,
대중교통 요금은 고른 경로에 붙인다. 배차를 모르는 노선(기본 대기)이 낀 경로는 `*_uncertain` 으로 표시한다(method.md E35).

    .\\.venv\\Scripts\\python.exe analysis\\grid_cbd.py --workers 12
"""
import argparse
import csv
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
DESTS = {"CBD": ("광화문", 126.9765, 37.5709), "GBD": ("강남", 127.0276, 37.4979), "YBD": ("여의도", 126.9243, 37.5216)}
SIDOS = ("서울특별시", "경기도")
MODES = ("transit", "taxi", "first", "last", "both")
_G = {}


def grid_cells(boundaries):
    """서울 · 경기 경계 안 1 km 격자 중심 → [(cell_id, lon, lat, 시도, 시군구)]. 격자는 위도 37.5° 의 등장방형 근사다."""
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
    net = reach.network(dbs["stops"], dbs["routes"], dbs["lines"])
    at = datetime.fromisoformat(at_iso)
    _G.update(anchors=anchors, fares=fares, reach=reach, search=search, dbs=dbs, net=net, at=at,
              waits=reach._Waits(net, dbs["headway"], dbs["hours"], at), vot=anchors.VOT["time"])
    _G["dests"] = {k: _dest(lon, lat) for k, (_, lon, lat) in DESTS.items()}


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


def _dest(lon, lat):
    """도착지 → 걸어서 내리는 정류장 {정류장: 도보 초} · 택시로 갈 수 있는 정류장 [(정류장, 택시 초, 택시 요금)] · 시군구."""
    a = _G["anchors"]
    sgg = (_G["dbs"]["stops"].row("bus", _G["dbs"]["stops"].near("bus", lon, lat, 2000.0)[0][0]) or {}).get("sgg_nm")
    walk = {n: a.walk_s(d) for n, (d, _) in _nodes_near((lon, lat), ACCESS_M).items()}
    reach_m = a.taxi_reach_m(a.T_MAX_S, _G["at"])
    taxi = [(n, *_taxi(d, s, sgg, False)) for n, (d, s) in _nodes_near((lon, lat), reach_m).items() if d >= a.TAXI_MIN_M]
    return {"pt": (lon, lat), "sgg": sgg, "walk": walk, "taxi": taxi}


def _path_info(par, node):
    """탐색 결과의 한 경로 → (승차 수, 대중교통 요금, 배차 모르는 승차가 있나, 시작 정류장)."""
    reach, search, fares, net, waits = _G["reach"], _G["search"], _G["fares"], _G["net"], _G["waits"]
    start, path = reach.legs(net, par, node)
    rides = [leg for leg in path if leg[0] == "ride"]
    steps, fallback = search._ride_steps(net, waits, path)
    fare = fares.estimate(steps, fallback, at=_G["at"])["value"] if steps else 0.0
    unknown = any(waits.ride_wait(p, i, j) == _G["anchors"].WAIT_UNKNOWN_S for _, p, i, j in rides)
    return len(rides), fare, unknown, start


def _best_walk_out(tau, dest):
    """걸어서 내린다 — 시간(일반화 비용) 최소인 내릴 정류장과 값."""
    got = [(tau[n] + w, n) for n, w in dest["walk"].items() if n in tau]
    return min(got) if got else (math.inf, None)


def _best_taxi_out(tau, dest):
    """도착 쪽 택시 — 일반화 비용(시간 + 요금 ÷ VOT) 최소인 내릴 정류장 → (일반화 초, 정류장, 택시 초, 택시 요금)."""
    vot = _G["vot"]
    best = (math.inf, None, 0.0, 0.0)
    for n, ts, tf in dest["taxi"]:
        if n in tau:
            g = tau[n] + ts + tf / vot * 60.0
            if g < best[0]:
                best = (g, n, ts, tf)
    return best


def _one(cell):
    cid, lon, lat, sido, sgg = cell
    a, reach, net, waits, vot = _G["anchors"], _G["reach"], _G["net"], _G["waits"], _G["vot"]
    o = (lon, lat)
    walk_in = {n: a.walk_s(d) for n, (d, _) in _nodes_near(o, ACCESS_M).items()}
    tau_w, par_w = reach.search(net, waits, walk_in, limit_s=LIMIT_S, max_rides=MAX_RIDES) if walk_in else ({}, {})
    # 택시로 시작 — 표지는 택시 시간 + 요금 ÷ VOT(일반화 초). 어느 정류장에서 탔는지로 택시 몫을 되찾는다
    first_taxi = {}
    for n, (d, s) in _nodes_near(o, a.taxi_reach_m(a.T_MAX_S, _G["at"])).items():
        if d >= a.TAXI_MIN_M:
            first_taxi[n] = _taxi(d, sgg, s, True)
    seeds = {n: ts + tf / vot * 60.0 for n, (ts, tf) in first_taxi.items()}
    tau_t, par_t = reach.search(net, waits, seeds, limit_s=LIMIT_S + 3600.0, max_rides=MAX_RIDES) if seeds else ({}, {})

    rows = []
    for key, dest in _G["dests"].items():
        straight = haversine_m(lon, lat, *dest["pt"])
        row = {"cell": cid, "lon": round(lon, 5), "lat": round(lat, 5), "sido": sido, "sgg": sgg, "dest": key,
               "straight_m": round(straight)}
        # 택시만
        ts, tf = _taxi(straight, sgg, dest["sgg"], False)
        row.update(taxi_time_s=round(ts), taxi_fare=round(tf))
        # 대중교통만
        g, n = _best_walk_out(tau_w, dest)
        if n is not None:
            rides, fare, unk, _ = _path_info(par_w, n)
            row.update(transit_time_s=round(g), transit_fare=round(fare), transit_rides=rides, transit_uncertain=int(unk))
        # 대중교통 → 택시
        g, n, lts, ltf = _best_taxi_out(tau_w, dest)
        if n is not None:
            rides, fare, unk, _ = _path_info(par_w, n)
            row.update(last_time_s=round(g - ltf / vot * 60.0), last_fare=round(fare + ltf), last_rides=rides,
                       last_uncertain=int(unk))
        # 택시 → 대중교통, 택시 → 대중교통 → 택시
        for mode, (g, n, lts, ltf) in (("first", (*_best_walk_out(tau_t, dest), 0.0, 0.0)), ("both", _best_taxi_out(tau_t, dest))):
            if n is None:
                continue
            rides, fare, unk, start = _path_info(par_t, n)
            fts, ftf = first_taxi[start]
            row.update({mode + "_time_s": round(g - (ftf + ltf) / vot * 60.0), mode + "_fare": round(fare + ftf + ltf),
                        mode + "_rides": rides, mode + "_uncertain": int(unk)})
        rows.append(row)
    return rows


COLUMNS = ["cell", "lon", "lat", "sido", "sgg", "dest", "straight_m", "taxi_time_s", "taxi_fare"] + [
    f"{m}_{f}" for m in ("transit", "first", "last", "both") for f in ("time_s", "fare", "rides", "uncertain")]


def main(argv=None):
    ap = argparse.ArgumentParser(description="1 km 격자 × 도심 세 곳 — 대중교통 · 택시 · 하이브리드 (내부 추정)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="앞에서 N 칸만 (시험용)")
    ap.add_argument("--at", default=None, help="계산 시각 ISO (기본: 지금, 모든 OD 에 같은 시각)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    from app.config import KST
    at = args.at or datetime.now(KST).replace(tzinfo=None, microsecond=0).isoformat()
    cells = grid_cells(ROOT / "data" / "processed" / "admin_sgg.geojson")
    if args.limit:
        cells = cells[:: max(1, len(cells) // args.limit)][: args.limit]
    out = args.out or ROOT / "analysis" / "out" / f"grid_cbd_{at[:16].replace(':', '').replace('-', '')}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    print("격자 %d 칸 × 도착지 %d = OD %d · 시각 %s · 일꾼 %d → %s" % (len(cells), len(DESTS), len(cells) * len(DESTS), at, args.workers, out),
          flush=True)
    t0, done = time.perf_counter(), 0
    with open(out, "w", encoding="utf-8-sig", newline="") as f, \
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
