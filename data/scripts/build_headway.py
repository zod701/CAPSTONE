"""배차간격 표 (도시철도 = KTDB GTFS 시각표 · 버스 = 경기 GBIS 노선 파일 + 서울 노선 목록).

1) **도시철도** (`headway_rail.csv`): GTFS `stop_times` 는 2025년 3월 **평일 1일의 실제 시각표**다 — 역·방향·시각별
   정차를 세어 시간대(시)별 배차간격을 만든다(배차 = 180분 / 앞뒤 1시간을 합친 3시간 창의 정차 횟수). 급행·지선·순환은 GTFS 가 노선을
   따로 두므로, 한 역에 서는 모든 운행 패턴이 자연히 함께 세어진다. 종착(승차 불가, `pickup_type=1`)은 세지 않는다.
   역은 이름 키 + 노선군(`ref/line_groups.csv` 의 `gtfs_names`)으로 우리 역 DB 에 붙인다 — 별칭도 보고, 그 노선군 행이
   없으면 같은 이름이 300 m 안에 있는 다른 노선군 행에 붙인다(환승역의 운영기관별 행). 개명으로 이름이 아예 다른 역은
   `ref/gtfs_station_map.csv` 로 잇는다. 매핑이 없는 GTFS 노선은 대상 밖(부산·대구 등)이라 건너뛰고 보고서에 적는다.
2) **버스** (`headway_bus.csv`): GTFS 의 버스 시각표는 첫차~막차를 대표 배차로 **균등 분배한 합성값**이라 쓰지 않는다
   (실측: 경기 300번이 04:20부터 정확히 13분 간격). 원천의 배차값을 그대로 쓴다 — 경기 GBIS 노선 파일(요일 유형 4개 ×
   최소·최대 배차 + 방향별 첫·막차) · 서울 노선 목록(`term` = 대표 배차 + 첫·막차, 요일 구분 없음).
"""
import argparse
import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

from fetch_boundary import KST, ROOT, file_line, md_table, rel
from fetch_gbis import latest, read_gbis
from fetch_seoul_routes import latest as latest_seoul_routes
from geoutil import haversine_m
from textnorm import name_key

GTFS_DIR = ROOT / "data" / "csv" / "대중교통GTFS(2025년 기준)" / "202503_GTFS_DataSet"
RAIL_TYPE = "1"          # KTDB route_type: 0 시내/마을버스 · 1 도시철도/경전철 · 3 시외 · 4 일반철도 …
DAY_TYPES = (("평일", ""), ("토요일", "sat"), ("일요일", "sun"), ("공휴일", "we"))
DIR_TAGS = (("<상행>", "상행"), ("<하행>", "하행"), ("<내선>", "내선"), ("<외선>", "외선"))
SAME_STATION_M = 300.0   # 같은 이름이 이 안에 있으면 한 역의 운영기관별 행으로 본다 (매칭 규칙과 같은 값)
BUS_COLUMNS = ["route_id", "route_name", "source", "day_type", "headway_min_m", "headway_max_m",
               "up_first", "up_last", "down_first", "down_last"]
RAIL_COLUMNS = ["station_id", "name", "line_group", "direction", "hour", "n_trips", "n_trips_3h", "headway_m"]


def parse_hm(text):
    """`04:20` · `24:47:00` · `20260911225000` → 자정 기준 분. 값이 없거나 못 읽으면 None."""
    s = str(text or "").strip()
    if not s or s in ("0", "-"):
        return None
    if ":" in s:
        p = s.split(":")
        try:
            return int(p[0]) * 60 + int(p[1])
        except ValueError:
            return None
    if len(s) >= 12 and s.isdigit():   # yyyymmddHHMMSS
        return int(s[8:10]) * 60 + int(s[10:12])
    return None


def hhmm(minutes):
    """분 → `HH:MM` (24 시 이후는 그대로 25:10 처럼 적는다). None 이면 빈 문자열."""
    return "" if minutes is None else f"{minutes // 60:02d}:{minutes % 60:02d}"


def _num(v):
    """배차 분. 빈값·0 은 정보 없음으로 본다(원천이 0 을 그렇게 쓴다)."""
    s = str(v or "").strip()
    return int(s) if s.isdigit() and int(s) > 0 else None


def bus_rows(gbis, seoul, want_ids):
    """경기 노선 파일 + 서울 노선 목록 → 배차 행. 우리 순서표에 있는 노선만, 값이 하나도 없는 요일 유형은 뺀다."""
    out = []
    for r in gbis.to_dict("records"):
        if r["routeId"] not in want_ids:
            continue
        for label, pre in DAY_TYPES:
            col = lambda n: r.get(f"{pre}{n[0].upper()}{n[1:]}" if pre else n, "")  # noqa: E731 — sat/sun/we 는 접두가 붙는다
            lo, hi = _num(col("peekAlloc")), _num(col("npeekAlloc"))
            times = [parse_hm(col(n)) for n in ("upFirstTime", "upLastTime", "downFirstTime", "downLastTime")]
            if lo is None and hi is None and not any(t is not None for t in times):
                continue
            out.append([r["routeId"], r["routeName"], "gyeonggi", label, lo, hi, *map(hhmm, times)])
    for r in seoul.to_dict("records"):
        if r["busRouteId"] not in want_ids:
            continue
        term = _num(r.get("term"))
        first, last = parse_hm(r.get("firstBusTm")), parse_hm(r.get("lastBusTm"))
        if term is None and first is None and last is None:
            continue
        # 서울 원천은 요일 구분이 없고 term 이 대표 배차 하나뿐이다 → 최소·최대에 같은 값
        out.append([r["busRouteId"], r["busRouteNm"], "seoul", "평일", term, term,
                    hhmm(first), hhmm(last), "", ""])
    # 배차는 정수 분(값이 없는 칸은 빈칸) — Int64 라야 CSV 에 10.0 이 아니라 10 으로 적힌다
    return pd.DataFrame(out, columns=BUS_COLUMNS).astype({"headway_min_m": "Int64", "headway_max_m": "Int64"})


def gtfs_line_map(line_groups):
    """`ref/line_groups.csv` → {GTFS 노선명: 노선군}. `gtfs_names` 가 비면 노선군 이름 자체를 쓴다."""
    out = {}
    for g in line_groups:
        group = g["line_group"]
        for n in [group, *(g.get("gtfs_names") or "").split("|")]:
            if n.strip():
                out[n.strip()] = group
    return out


def rail_routes(routes, line_map):
    """GTFS 도시철도 노선 → {route_id: (노선군, 방향)}, 그리고 매핑이 없어 건너뛴 노선명."""
    keep, skipped = {}, Counter()
    for r in routes:
        if r["route_type"] != RAIL_TYPE:
            continue
        group = line_map.get(r["route_short_name"].strip())
        if group is None:
            skipped[r["route_short_name"].strip()] += 1
            continue
        name = r["route_long_name"]
        direction = next((v for tag, v in DIR_TAGS if tag in name), "")
        keep[r["route_id"]] = (group, direction)
    return keep, skipped


def station_map(stops, pairs, stations, overrides=()):
    """GTFS 역 → 우리 역 행. 이름 키(별칭 포함) + 노선군으로 맞추고, 그 노선군 행이 없으면 같은 이름이
    SAME_STATION_M 안에 있는 다른 노선군 행에 붙인다. 개명은 보정표(overrides)로 먼저 잇는다.
    → ({(gtfs 역, 노선군): 역 행}, 못 붙인 {(역 이름, 노선군): 노선 수}, 다른 노선군 행에 붙인 목록)"""
    by_id = {s["station_id"]: s for s in stations}
    fixed = {(o["gtfs_name"], o["line_group"]): by_id[o["station_id"]] for o in overrides}
    by_pair, by_name = {}, defaultdict(list)
    for s in stations:
        for nm in [s["name"], *(s.get("aliases") or "").split("|")]:
            if not nm.strip():
                continue
            k = name_key(nm, "subway")
            by_pair.setdefault((k, s["line_group"]), s)
            if s not in by_name[k]:
                by_name[k].append(s)
    out, missing, other = {}, Counter(), []
    for stop_id, group in pairs:
        info = stops.get(stop_id)
        if info is None:
            continue
        nm, lat, lon = info
        row = fixed.get((nm, group)) or by_pair.get((name_key(nm, "subway"), group))
        if row is None:  # 그 노선군 행이 없다 (환승역을 운영 노선별로 담은 우리 DB 구조) → 같은 실체를 찾는다
            near = [s for s in by_name.get(name_key(nm, "subway"), ())
                    if haversine_m(lon, lat, float(s["lon"]), float(s["lat"])) <= SAME_STATION_M]
            if near:
                row = near[0]
                other.append((nm, group, row["station_id"], row["line_group"]))
        if row is None:
            missing[(nm, group)] += 1
        else:
            out[(stop_id, group)] = row
    return out, missing, other


def rail_counts(stop_times_path, keep):
    """stop_times 를 한 번 훑어 {(gtfs 역, 노선군, 방향): {시: 정차 횟수}} · (역, 노선군) 쌍 · 훑은 줄 수 ·
    {(노선군, 방향): 운행 회차 수} 를 모은다."""
    prefixes = tuple(keep)
    counts = defaultdict(Counter)
    pairs = set()
    trips = defaultdict(set)
    n = 0
    with open(stop_times_path, encoding="utf-8-sig", errors="replace") as f:
        f.readline()
        for line in f:
            n += 1
            if not line.startswith(prefixes):
                continue
            trip, _, dep, stop_id, _, pickup, *_ = line.rstrip("\n").split(",")
            if pickup == "1":       # 승차 불가(종착) 는 기다릴 수 없다
                continue
            group, direction = keep[trip.rsplit("_Ord", 1)[0]]   # trip_id = 노선 id + _Ord###
            minute = parse_hm(dep)
            if minute is None:
                continue
            counts[(stop_id, group, direction)][minute // 60] += 1
            pairs.add((stop_id, group))
            trips[(group, direction)].add(trip)
    return counts, pairs, n, {k: len(v) for k, v in trips.items()}


def rail_frame(counts, mapped):
    """{(역, 노선군, 방향): {시: 횟수}} → 배차 행. 배차 = 180 / 앞뒤 1시간을 합친 3시간 창의 정차 횟수.

    한 시간 칸만 세면 정차 1회인 칸이 모두 60분이 된다 — 시각이 시(時) 경계에 걸려 1회·2회가 번갈아 나오는
    곳도 60분으로 적힌다. 창으로 세면 실제로 한 시간에 한 대인 곳은 60분 그대로 남는다."""
    out = []
    for (stop_id, group, direction), per_hour in counts.items():
        row = mapped.get((stop_id, group))
        if row is None:
            continue
        for hour, n in sorted(per_hour.items()):
            n3 = per_hour.get(hour - 1, 0) + n + per_hour.get(hour + 1, 0)
            out.append([row["station_id"], row["name"], group, direction, hour, n, n3, round(180 / n3, 1)])
    out.sort(key=lambda r: (r[2], r[0], r[3], r[4]))
    return pd.DataFrame(out, columns=RAIL_COLUMNS)


def main(argv=None):
    ap = argparse.ArgumentParser(description="배차간격 표 (도시철도 GTFS 시각표 · 버스 노선 원천)")
    ap.add_argument("--gtfs", type=Path, default=GTFS_DIR, help="KTDB GTFS 데이터셋 디렉터리")
    ap.add_argument("--gbis-routes", type=Path, default=None, help="GBIS 노선 파일 (기본: 가장 최근)")
    ap.add_argument("--seoul-routes", type=Path, default=None, help="서울 노선 목록 (기본: 가장 최근)")
    ap.add_argument("--route-stops", type=Path, default=ROOT / "data" / "processed" / "bus_route_stops.csv")
    ap.add_argument("--stations", type=Path, default=ROOT / "data" / "processed" / "subway_stations.csv")
    ap.add_argument("--line-groups", type=Path, default=ROOT / "data" / "ref" / "line_groups.csv")
    ap.add_argument("--station-map", type=Path, default=ROOT / "data" / "ref" / "gtfs_station_map.csv",
                    help="개명 등으로 이름이 다른 GTFS 역 ↔ 우리 역 보정표")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "data" / "processed")
    ap.add_argument("--report", type=Path, default=ROOT / "data" / "reports" / "headway.md")
    args = ap.parse_args(argv)

    gbis_path = args.gbis_routes or latest("route")
    seoul_path = args.seoul_routes or latest_seoul_routes()
    read = lambda p: list(csv.DictReader(open(p, encoding="utf-8-sig", newline="")))  # noqa: E731

    # --- 버스
    gbis = read_gbis(gbis_path)
    seoul = pd.read_csv(seoul_path, dtype=str).fillna("")
    ours = pd.read_csv(args.route_stops, dtype=str, usecols=["route_id", "source"]).drop_duplicates()
    want = set(ours["route_id"])
    bus = bus_rows(gbis, seoul, want)
    (args.out_dir / "headway_bus.csv").parent.mkdir(parents=True, exist_ok=True)
    bus.to_csv(args.out_dir / "headway_bus.csv", index=False, encoding="utf-8-sig")

    # --- 도시철도
    stations = read(args.stations)
    line_map = gtfs_line_map(read(args.line_groups))
    keep, skipped = rail_routes(read(args.gtfs / "routes.txt"), line_map)
    if not keep:
        sys.exit("GTFS 도시철도 노선을 하나도 매핑하지 못했습니다 — ref/line_groups.csv 의 gtfs_names 를 확인하세요.")
    stop_info = {r["stop_id"]: (r["stop_name"], float(r["stop_lat"]), float(r["stop_lon"]))
                 for r in read(args.gtfs / "stops.txt")}
    counts, pairs, n_lines, n_trips = rail_counts(args.gtfs / "stop_times.txt", keep)
    mapped, missing, other = station_map(stop_info, pairs, stations, read(args.station_map))
    rail = rail_frame(counts, mapped)
    rail.to_csv(args.out_dir / "headway_rail.csv", index=False, encoding="utf-8-sig")

    # --- 보고서
    bus_by_route = bus.drop_duplicates("route_id")
    cover = [(label, int((ours["source"] == label).sum()),
              int(bus_by_route[bus_by_route["source"] == label]["route_id"].isin(
                  set(ours.loc[ours["source"] == label, "route_id"])).sum()))
             for label in ("gyeonggi", "seoul")]
    day_rows = [(label, int((bus["day_type"] == label).sum()),
                 int(bus.loc[bus["day_type"] == label, "headway_min_m"].notna().sum()))
                for label, _ in DAY_TYPES]
    steps = [("GBIS 노선 파일", len(gbis)), ("서울 노선 목록", len(seoul)),
             ("우리 순서표 노선", len(ours)), ("배차를 찾은 노선", bus_by_route["route_id"].nunique()),
             ("버스 출력 행", len(bus)),
             ("GTFS 도시철도 노선 (대상)", len(keep)), ("GTFS 역 × 노선군 쌍", len(pairs)),
             ("우리 역 DB 에 붙은 쌍", len(mapped)), ("훑은 stop_times 행", n_lines),
             ("보정표로 이은 (GTFS 역, 노선군)", len(read(args.station_map))),
             ("다른 노선군 행에 붙인 쌍", len(other)),
             ("도시철도 출력 행", len(rail)), ("출력에 들어간 역", rail["station_id"].nunique())]

    have = set(rail["station_id"])
    pair_have = {(name_key(s["name"], "subway"), s["line_group"]) for s in stations if s["station_id"] in have}
    no_headway = [s for s in stations if s["station_id"] not in have]
    dup_rows = [s for s in no_headway if (name_key(s["name"], "subway"), s["line_group"]) in pair_have]
    absent_rows = [s for s in no_headway if s not in dup_rows]
    steps += [("배차가 없는 역 — 같은 역의 다른 행이 가짐", len(dup_rows)),
              ("배차가 없는 역 — GTFS 원천에 없음", len(absent_rows))]

    peak = rail[rail["hour"] == 8].groupby(["line_group", "direction"])["headway_m"].median().round(1)
    peak_rows = [(g, d, v, n_trips.get((g, d), 0)) for (g, d), v in peak.items()]
    # 원천의 방향 비대칭 — 한쪽 회차가 다른 쪽의 절반도 안 되면 그 방향 배차를 믿기 어렵다
    per_group = defaultdict(dict)
    for (g, d), n in n_trips.items():
        per_group[g][d] = n
    skew_rows = [(g, " · ".join(f"{d} {v:,}" for d, v in sorted(per_dir.items())),
                  f"{min(per_dir.values()) / max(per_dir.values()):.2f}")
                 for g, per_dir in sorted(per_group.items())
                 if len(per_dir) > 1 and min(per_dir.values()) * 2 < max(per_dir.values())]
    miss_rows = [(nm, g, n) for (nm, g), n in missing.most_common(30)]
    skip_rows = [(nm, n) for nm, n in skipped.most_common()]
    inputs = [file_line(p) for p in (gbis_path, seoul_path, args.gtfs / "stop_times.txt",
                                     args.route_stops, args.stations, args.line_groups, args.station_map)]
    table = md_table(steps)
    print(table)
    report = "\n".join([
        "# 배차간격 표 (build_headway.py)",
        "",
        f"- 실행 시각: {datetime.now(KST):%Y-%m-%d %H:%M:%S} KST",
        *[f"- 입력: {s}" for s in inputs],
        f"- 출력: {file_line(args.out_dir / 'headway_bus.csv')}",
        f"- 출력: {file_line(args.out_dir / 'headway_rail.csv')}",
        "- 원천: KTDB 대중교통 GTFS(2025년 3월 **평일 1일** 기준) · 경기도 버스정보 기반정보 노선 파일 · "
        "서울특별시 노선정보조회 `getBusRouteList`",
        "",
        "## 단계별 건수",
        "",
        table,
        "",
        "## 버스 — 원천별 커버리지",
        "",
        md_table([(l, n, m, f"{m / n:.1%}" if n else "-") for l, n, m in cover],
                 ("원천", "우리 노선", "배차를 찾은 노선", "비율")),
        "",
        "요일 유형별 행 수와 최소 배차값이 있는 행:",
        "",
        md_table(day_rows, ("요일 유형", "행", "배차값 있음")),
        "",
        "- 경기 원천의 `peekAlloc`·`npeekAlloc` 은 이름과 달리 첨두/비첨두가 아니라 그 요일의 **최소·최대 배차(분)** 다"
        "(GBIS 공유서비스 항목 정의) → `headway_min_m`·`headway_max_m`.",
        "- 서울 원천은 요일 구분이 없고 `term`(대표 배차) 하나뿐이라 최소·최대에 같은 값을 넣고 요일 유형은 평일로 둔다"
        f" — 받은 날({rel(seoul_path).split('/')[-2]}, 금요일) 기준값이다.",
        "- 0 과 빈값은 정보 없음으로 보고 비운다. 첫·막차는 경기만 방향별(상·하행)로 있다.",
        "",
        "## 도시철도 — 시간대별 배차 (8시, 노선군 중앙값)",
        "",
        md_table(peak_rows, ("노선군", "방향", "배차(분)", "하루 운행 회차")),
        "",
        "- 배차 = 180분 / 3시간 창(그 시간대와 앞뒤 1시간)의 정차 횟수(`n_trips_3h`) — 한 시간 칸만 세면 정차 1회인 칸이 "
        "시각이 시 경계에 걸린 것만으로 60분이 된다. `n_trips` 는 그 시간대만의 횟수다.",
        f"- 정차 1회인 시간대 {int((rail['n_trips'] == 1).sum()):,}행 중 앞뒤 시간도 같은 방향 1회 이하라 60분 이상으로 남은 행 "
        f"{int(((rail['n_trips'] == 1) & (rail['headway_m'] >= 60)).sum()):,}행 — 실제로 한 시간에 한 대 이하인 곳이다.",
        "- 첫·막차 시간대는 창이 운행 없는 시간을 품어 배차가 넓게 나온다 — 이 시간대를 대기로 쓸지는 이 표가 정하지 않는다.",
        "- 24시 이후 시각은 `hour` 24·25 로 둔다(GTFS 표기 그대로).",
        "- 급행·지선·순환은 GTFS 가 노선을 따로 두므로 한 역에 서는 모든 패턴이 함께 세어진다"
        " — 그 역에서 '아무 열차나' 기다리는 배차다.",
        "- 종착(승차 불가)은 세지 않는다 — 그래서 종착역은 한쪽 방향만 나온다(예: GTX-A 동탄).",
        "",
        "## 도시철도 — 원천의 방향 비대칭 (한쪽 회차가 다른 쪽의 절반 미만)",
        "",
        md_table(skew_rows, ("노선군", "방향별 회차", "적은 쪽/많은 쪽")) if skew_rows else "없음",
        "",
        "- GTFS 원천 자체가 한 방향의 회차를 적게 담은 곳이다(7호선 하행 75 vs 상행 204 회차). 그 방향 배차는 실제보다 넓게 나온다.",
        "",
        "## 도시철도 — 배차가 없는 우리 역",
        "",
        md_table([(s["station_id"], s["name"], s["line_group"]) for s in absent_rows],
                 ("station_id", "역", "노선군")) if absent_rows else "없음",
        "",
        "- 같은 역을 운영기관별로 두 행에 담은 곳(1호선 청량리·3호선 지축 등)은 한 행만 배차를 갖는다 — "
        f"그런 행 {len(dup_rows)}개는 위 표에서 뺐다.",
        "",
        "## 도시철도 — 다른 노선군 행에 붙인 (GTFS 역, 노선군)",
        "",
        md_table(other, ("GTFS 역", "GTFS 노선군", "붙인 station_id", "그 행의 노선군")) if other else "없음",
        "",
        "- 우리 역 DB 는 환승역을 운영 노선별 행으로 담는다. GTFS 노선군의 행이 없으면 같은 이름이 300 m 안에 있는 "
        "다른 행에 붙였다 — `line_group` 열은 GTFS 노선군 그대로이므로 '그 역에서 그 노선을 기다리는 배차'로 읽으면 된다.",
        "",
        "## 도시철도 — 우리 역 DB 에 못 붙인 GTFS 역",
        "",
        md_table(miss_rows, ("GTFS 역", "노선군", "노선 수")) if miss_rows else "없음",
        "",
        "- 대부분 대상 지역 밖(인천·충남·강원)이다.",
        "",
        "## 매핑이 없어 건너뛴 GTFS 도시철도 노선 (대상 밖)",
        "",
        md_table(skip_rows, ("GTFS 노선", "노선 수")) if skip_rows else "없음",
        "",
        "## 한계",
        "",
        "- GTFS 는 **평일 1일**(2025년 3월) 기준이다 — 주말·공휴일 시각표가 없다. 버스는 경기 원천이 요일 유형별 값을 준다.",
        "- GTFS 의 버스 시각표는 첫차~막차를 대표 배차로 균등 분배한 합성값이라(경기 300번이 정확히 13분 간격) 쓰지 않았다.",
        "- GTFS 기준 시점이 우리 정류소·역 DB 보다 이르다 — 그 뒤 생긴 역은 배차가 없다.",
        "",
    ])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(f"\n보고서: {rel(args.report)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
