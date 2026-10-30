"""도시철도 역 사이 실제 운행 시간 (`processed/rail_runs.csv`) · 급행 정차 패턴 (`processed/rail_express.csv`)
— KTDB GTFS 의 2025년 3월 평일 1일 시각표.

열차마다 이웃한 두 정차의 **도착 시각 차**(앞 역 정차 포함)를 모아, 노선군 · 우리 역 실체(`phys_id`) 쌍별 중앙값을 낸다.
급행은 서는 역끼리만 이웃이라 건너뛴 쌍으로 들어간다 — 쓰는 쪽(`algo/reach.py`)은 역 순서표에서 이웃한 쌍만 읽는다.

**급행 패턴**: 역 순서표에서 이웃하지 않은 두 역을 잇는 열차(건너뛰는 열차)를 정차역 순서가 같은 것끼리 묶는다.
패턴마다 첫 역부터의 누적 운행 초(열차별 중앙값)와 역마다 시간대별 출발 횟수를 낸다 — 탐색이 급행을 노선 하나로 탄다.
대상 지역 밖 역(천안 등)은 잘라 내고 남은 역으로 본다. 순환선(2호선)은 끝과 처음이 이웃이다.

GTFS 역 → 우리 역은 `build_headway` 의 규칙(이름 키 + 노선군, 같은 실체 300 m, 개명 보정표)을 그대로 쓴다.
배차표(`build_headway`)와 따로 두는 것은 그 스크립트가 버스 원천의 최신 파일도 함께 다시 읽기 때문이다.
"""
import argparse
import csv
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from build_headway import GTFS_DIR, gtfs_line_map, rail_routes, station_map
from fetch_boundary import ROOT

COLUMNS = ["line_group", "from_phys", "to_phys", "run_s", "n_trips"]
EXPRESS_COLUMNS = ["pattern_id", "line_group", "seq", "station_id", "cum_s", "n_trips", "trips_by_hour"]


def parse_s(text):
    """'HH:MM:SS' → 자정부터의 초 (24시를 넘는 값 그대로). 형식이 틀리면 None."""
    parts = (text or "").strip().split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    h, m, s = (int(p) for p in parts)
    return h * 3600 + m * 60 + s


def rail_trips(stop_times_path, keep):
    """stop_times 를 훑어 {열차: (노선군, [(순번, 도착 초, GTFS 역)…] 순번 차례)}."""
    prefixes = tuple(keep)
    trips = defaultdict(list)
    with open(stop_times_path, encoding="utf-8-sig", errors="replace") as f:
        f.readline()
        for line in f:
            if not line.startswith(prefixes):
                continue
            trip, arr, dep, stop_id, seq, *_ = line.rstrip("\n").split(",")
            t = parse_s(arr) if parse_s(arr) is not None else parse_s(dep)
            if t is not None and seq.isdigit():
                trips[trip].append((int(seq), t, stop_id))
    return {trip: (keep[trip.rsplit("_Ord", 1)[0]][0], sorted(rows)) for trip, rows in trips.items()}   # trip_id = 노선 id + _Ord###


def rail_runs(trips):
    """{(노선군, GTFS 역 a, GTFS 역 b): [a 도착 → b 도착 초…]} — 같은 열차의 이웃한 정차."""
    out = defaultdict(list)
    for group, rows in trips.values():
        for (_, ta, a), (_, tb, b) in zip(rows, rows[1:]):
            if tb > ta:
                out[(group, a, b)].append(tb - ta)
    return out


def run_rows(runs, mapped, phys):
    """GTFS 역 쌍 → 우리 실체 쌍으로 접어 중앙값. 못 붙인 역이 낀 쌍과 같은 실체끼리(환승 행)는 뺀다."""
    pooled = defaultdict(list)
    for (group, a, b), secs in runs.items():
        ra, rb = mapped.get((a, group)), mapped.get((b, group))
        if ra is None or rb is None:
            continue
        pa, pb = phys.get(ra["station_id"]), phys.get(rb["station_id"])
        if pa and pb and pa != pb:
            pooled[(group, pa, pb)] += secs
    return [[g, a, b, round(statistics.median(v)), len(v)] for (g, a, b), v in sorted(pooled.items())]


def express_rows(trips, mapped, line_seq):
    """건너뛰는 열차 → 급행 패턴 행. 역은 그 노선군 순서표의 역 id 로 적는다(탐색 노드와 같다)."""
    chains = defaultdict(list)
    for r in line_seq:
        chains[r["chain_id"]].append(r)
    adjacent, station_of = set(), {}
    for rows in chains.values():
        rows.sort(key=lambda r: int(r["seq"]))
        group = rows[0]["line_group"]
        pairs = list(zip(rows, rows[1:])) + ([(rows[-1], rows[0])] if rows[0]["loop"].strip() == "1" else [])
        for a, b in pairs:
            adjacent |= {(group, a["phys_id"], b["phys_id"]), (group, b["phys_id"], a["phys_id"])}
        for r in rows:
            station_of.setdefault((group, r["phys_id"]), r["station_id"])
    phys = {r["station_id"]: r["phys_id"] for r in line_seq}
    patterns = defaultdict(list)
    for group, rows in trips.values():
        stops = []
        for _, t, stop in rows:   # 대상 밖 · 못 붙인 역은 잘라 낸다
            m = mapped.get((stop, group))
            sid = station_of.get((group, phys.get(m["station_id"]))) if m else None
            if sid and (not stops or stops[-1][0] != sid):
                stops.append((sid, t))
        if len(stops) >= 2 and any((group, phys[a], phys[b]) not in adjacent for (a, _), (b, _) in zip(stops, stops[1:])):
            patterns[(group, tuple(s for s, _ in stops))].append([t for _, t in stops])
    out = []
    for n, ((group, sids), runs) in enumerate(sorted(patterns.items(), key=lambda x: (x[0][0], -len(x[1]), x[0][1]))):
        pid = f"{group}:급행{n + 1}"
        for i, sid in enumerate(sids):
            cum = statistics.median(ts[i] - ts[0] for ts in runs)
            hours = Counter(ts[i] // 3600 for ts in runs) if i < len(sids) - 1 else Counter()   # 끝 역은 타는 곳이 아니다
            out.append([pid, group, i + 1, sid, round(cum), len(runs), "|".join(f"{h}:{c}" for h, c in sorted(hours.items()))])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="도시철도 역 사이 실제 운행 시간 · 급행 정차 패턴 (GTFS 시각표)")
    ap.add_argument("--gtfs", type=Path, default=GTFS_DIR)
    ap.add_argument("--stations", type=Path, default=ROOT / "data" / "processed" / "subway_stations.csv")
    ap.add_argument("--line-seq", type=Path, default=ROOT / "data" / "processed" / "subway_line_seq.csv")
    ap.add_argument("--line-groups", type=Path, default=ROOT / "data" / "ref" / "line_groups.csv")
    ap.add_argument("--station-map", type=Path, default=ROOT / "data" / "ref" / "gtfs_station_map.csv")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "processed" / "rail_runs.csv")
    ap.add_argument("--express-out", type=Path, default=ROOT / "data" / "processed" / "rail_express.csv")
    args = ap.parse_args(argv)
    read = lambda p: list(csv.DictReader(open(p, encoding="utf-8-sig", newline="")))  # noqa: E731

    keep, _ = rail_routes(read(args.gtfs / "routes.txt"), gtfs_line_map(read(args.line_groups)))
    stop_info = {r["stop_id"]: (r["stop_name"], float(r["stop_lat"]), float(r["stop_lon"]))
                 for r in read(args.gtfs / "stops.txt")}
    trips = rail_trips(args.gtfs / "stop_times.txt", keep)
    runs = rail_runs(trips)
    pairs = {(s, g) for g, a, b in runs for s in (a, b)}
    mapped, missing, _ = station_map(stop_info, pairs, read(args.stations), read(args.station_map))
    line_seq = read(args.line_seq)
    phys = {r["station_id"]: r["phys_id"] for r in line_seq}
    rows = run_rows(runs, mapped, phys)
    express = express_rows(trips, mapped, line_seq)
    for path, columns, body in ((args.out, COLUMNS, rows), (args.express_out, EXPRESS_COLUMNS, express)):
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(columns)
            w.writerows(body)
    by_group = Counter(r[1] for r in express if r[2] == 1)
    trips_by_group = Counter()
    for r in express:
        if r[2] == 1:
            trips_by_group[r[1]] += r[5]
    print("GTFS 이웃 정차 쌍 %d · 우리 실체 쌍 %d · 못 붙인 GTFS 역 %d → %s" % (len(runs), len(rows), len(missing), args.out))
    print("급행 패턴 %d (%s) → %s" % (sum(by_group.values()), " · ".join(
        f"{g} {n}개 {trips_by_group[g]}회" for g, n in sorted(by_group.items())), args.express_out))


if __name__ == "__main__":
    main()
