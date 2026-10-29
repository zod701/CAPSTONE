"""도시철도 역 사이 실제 운행 시간 (`processed/rail_runs.csv`) — KTDB GTFS 의 2025년 3월 평일 1일 시각표.

열차마다 이웃한 두 정차의 **도착 시각 차**(앞 역 정차 포함)를 모아, 노선군 · 우리 역 실체(`phys_id`) 쌍별 중앙값을 낸다.
급행은 서는 역끼리만 이웃이라 건너뛴 쌍으로 들어간다 — 쓰는 쪽(`algo/reach.py`)은 역 순서표에서 이웃한 쌍만 읽는다.
GTFS 역 → 우리 역은 `build_headway` 의 규칙(이름 키 + 노선군, 같은 실체 300 m, 개명 보정표)을 그대로 쓴다.
배차표(`build_headway`)와 따로 두는 것은 그 스크립트가 버스 원천의 최신 파일도 함께 다시 읽기 때문이다.
"""
import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

from build_headway import GTFS_DIR, gtfs_line_map, rail_routes, station_map
from fetch_boundary import ROOT

COLUMNS = ["line_group", "from_phys", "to_phys", "run_s", "n_trips"]


def parse_s(text):
    """'HH:MM:SS' → 자정부터의 초 (24시를 넘는 값 그대로). 형식이 틀리면 None."""
    parts = (text or "").strip().split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    h, m, s = (int(p) for p in parts)
    return h * 3600 + m * 60 + s


def rail_runs(stop_times_path, keep):
    """stop_times 를 훑어 {(노선군, GTFS 역 a, GTFS 역 b): [a 도착 → b 도착 초…]} — 같은 열차의 이웃한 정차."""
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
    out = defaultdict(list)
    for trip, rows in trips.items():
        group = keep[trip.rsplit("_Ord", 1)[0]][0]   # trip_id = 노선 id + _Ord###
        rows.sort()
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


def main(argv=None):
    ap = argparse.ArgumentParser(description="도시철도 역 사이 실제 운행 시간 (GTFS 시각표)")
    ap.add_argument("--gtfs", type=Path, default=GTFS_DIR)
    ap.add_argument("--stations", type=Path, default=ROOT / "data" / "processed" / "subway_stations.csv")
    ap.add_argument("--line-seq", type=Path, default=ROOT / "data" / "processed" / "subway_line_seq.csv")
    ap.add_argument("--line-groups", type=Path, default=ROOT / "data" / "ref" / "line_groups.csv")
    ap.add_argument("--station-map", type=Path, default=ROOT / "data" / "ref" / "gtfs_station_map.csv")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "processed" / "rail_runs.csv")
    args = ap.parse_args(argv)
    read = lambda p: list(csv.DictReader(open(p, encoding="utf-8-sig", newline="")))  # noqa: E731

    keep, _ = rail_routes(read(args.gtfs / "routes.txt"), gtfs_line_map(read(args.line_groups)))
    stop_info = {r["stop_id"]: (r["stop_name"], float(r["stop_lat"]), float(r["stop_lon"]))
                 for r in read(args.gtfs / "stops.txt")}
    runs = rail_runs(args.gtfs / "stop_times.txt", keep)
    pairs = {(s, g) for g, a, b in runs for s in (a, b)}
    mapped, missing, _ = station_map(stop_info, pairs, read(args.stations), read(args.station_map))
    phys = {r["station_id"]: r["phys_id"] for r in read(args.line_seq)}
    rows = run_rows(runs, mapped, phys)
    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows)
    print("GTFS 이웃 정차 쌍 %d · 우리 실체 쌍 %d · 못 붙인 GTFS 역 %d → %s" % (len(runs), len(rows), len(missing), args.out))


if __name__ == "__main__":
    main()
