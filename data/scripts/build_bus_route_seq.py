"""버스 노선–정류소 순서표 (서울 노선별 정류소 + 경기 GBIS 노선–정류소 → 노선별 정류소 순서, 서울·경기).

1) 원천 두 개를 같은 꼴로: 서울 OA-1095(ROUTE_ID·순번·NODE_ID) · 경기 GBIS(routeId·staOrder·stationId·upDown).
   두 원천의 노선은 겹치지 않는다(경기로 드나드는 서울 노선은 서울 원천에만, 경기 노선은 GBIS 에만) — 노선 ID 가 겹치면 실패.
   서울 원천에 섞인 배(한강버스) 노선은 뺀다
2) 순번은 원천 그대로 둔다 = 운행 순서. 서울은 회차를 지나 한 줄로 이어지고(상·하행 열 없음), 경기는 상행 → 하행이
   한 순번열에 이어진다(첫 하행 = 회차 정류소). 같은 노선에서 순번이 작을수록 상류다
3) 정류소 ID → 정류소 DB(processed/bus_stops.csv) 행: 자기 BIS 레코드(서울 SEB·경기 GGB + ID)가 들어간 행 →
   ID 가 곧 stop_key 인 행(다른 BIS 만 등록한 정류소) → 둘 다 없으면 원천 좌표로 시군을 판정해, 대상 안이면 원천
   이름·좌표로 남기고(in_db=0 — 정류소 파일에 없는 통과 노드 등) 대상 밖이면 뺀다(순번이 비어도 순서는 그대로)
"""
import argparse
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd

from build_boundaries import ATTRIBUTION
from fetch_boundary import KST, ROOT, file_line, md_table, rel
from fetch_gbis import latest, read_gbis
from fetch_seoul_routes import latest as latest_seoul_routes
from geoutil import haversine_m
from regions import RegionIndex
from textnorm import is_virtual, name_key

SNAP_MAX_M = 2000.0      # 정류소 정제와 같은 값
COORD_WARN_M = 300.0     # 원천 좌표와 DB 좌표가 이보다 멀면 보고 (같은 ID 가 다른 정류소에 다시 쓰였을 수 있다)
LONG_GAP_M = 10000.0     # 보고서에 세는 긴 인접 구간
IN_SCOPE = ("contain", "snap")
PREFIX = {"seoul": "SEB", "gyeonggi": "GGB"}
SRC_COLUMNS = ["route_id", "route_name", "source", "seq", "updown", "station_id", "src_name", "src_lon", "src_lat"]
COLUMNS = ["route_id", "route_name", "route_type", "route_type_src", "source", "seq", "updown", "stop_key", "name",
           "lat", "lon", "in_db", "is_virtual"]
FERRY_ROUTE = re.compile(r"^한강버스")  # 서울 노선 원천에 버스처럼 들어 있는 한강 배 노선
# GBIS routeTypeCd → (route_type, 원천 유형 이름). 원천 이름은 GBIS 공유서비스 노선정보 매뉴얼의 코드표.
# route_type 은 같은 부류면 카카오 버스 유형 이름(간선·지선·순환·광역·직행·일반·마을·시외·공항 — 웹 BUS_COLOR 의 키)을 쓰고,
# 대응하는 카카오 유형이 없으면 원천의 짧은 이름을 쓴다(좌석·따복·수요응답)
GBIS_TYPES = {
    "11": ("직행", "직행좌석형시내버스"), "12": ("좌석", "좌석형시내버스"), "13": ("일반", "일반형시내버스"),
    "14": ("광역", "광역급행형시내버스"), "15": ("따복", "따복형시내버스"), "16": ("순환", "경기순환버스"),
    "21": ("직행", "직행좌석형농어촌버스"), "22": ("좌석", "좌석형농어촌버스"), "23": ("일반", "일반형농어촌버스"),
    "30": ("마을", "마을버스"), "41": ("시외", "고속형시외버스"), "42": ("시외", "좌석형시외버스"),
    "43": ("시외", "일반형시외버스"),
    "50": ("수요응답", "수요응답형(DRT)"),  # 매뉴얼 코드표에 없다 — 노선이 모두 똑버스·DRT 운수사다
    "51": ("공항", "리무진공항버스"), "52": ("공항", "좌석형공항버스"), "53": ("공항", "일반형공항버스"),
}


# 서울 노선정보조회 routeType → (route_type, 원천 유형 이름). 이름은 서울 노선 기본정보 컬럼정의(1–6·10),
# 7·8 은 노선명 꼬리(인천·경기), 14·15 는 컬럼정의에 없어 노선으로 확인했다(한강버스 · N버스)
SEOUL_TYPES = {
    "1": ("공항", "공항"), "2": ("마을", "마을"), "3": ("간선", "간선"), "4": ("지선", "지선"), "5": ("순환", "순환"),
    "6": ("광역", "광역"), "7": ("인천", "인천"), "8": ("경기", "경기"), "10": ("관광", "관광"),
    "14": ("한강버스", "한강버스"), "15": ("심야", "심야"),
}


def _types(ids, codes, table, label):
    unknown = sorted(set(codes) - set(table))
    if unknown:
        raise ValueError(f"{label} 노선유형코드가 코드표에 없습니다: {unknown}")
    return {rid: table[c] for rid, c in zip(ids, codes)}


def gbis_route_types(routes):
    """GBIS 노선 파일 → {routeId: (route_type, route_type_src)}. 코드표에 없는 유형이 나오면 ValueError (원천이 바뀐 신호)."""
    return _types(routes["routeId"], routes["routeTypeCd"], GBIS_TYPES, "GBIS")


def seoul_route_types(routes):
    """서울 노선 목록(fetch_seoul_routes.py) → {busRouteId: (route_type, route_type_src)}. 모르는 코드면 ValueError."""
    return _types(routes["busRouteId"], routes["routeType"], SEOUL_TYPES, "서울")


def normalize(seoul, gbis):
    """두 원천 → SRC_COLUMNS 꼴 하나 (배 노선은 뺀다). 정류소 ID 형식·(노선, 순번) 중복·노선 ID 겹침은 ValueError."""
    seoul = seoul[~seoul["노선명"].str.match(FERRY_ROUTE)]
    s = pd.DataFrame({"route_id": seoul["ROUTE_ID"], "route_name": seoul["노선명"], "source": "seoul",
                      "seq": seoul["순번"], "updown": "", "station_id": seoul["NODE_ID"],
                      "src_name": seoul["정류소명"], "src_lon": seoul["X좌표"], "src_lat": seoul["Y좌표"]})
    g = pd.DataFrame({"route_id": gbis["routeId"], "route_name": gbis["routeName"], "source": "gyeonggi",
                      "seq": gbis["staOrder"], "updown": gbis["upDown"], "station_id": gbis["stationId"],
                      "src_name": gbis["stationName"], "src_lon": gbis["x"], "src_lat": gbis["y"]})
    both = set(s["route_id"]) & set(g["route_id"])
    if both:
        raise ValueError(f"서울·경기 원천의 노선 ID 가 겹칩니다: {sorted(both)[:10]}")
    src = pd.concat([s, g], ignore_index=True)
    bad = src[~src["station_id"].str.fullmatch(r"\d{9}")]
    if len(bad):
        raise ValueError(f"정류소 ID 가 9자리 숫자가 아닙니다: {bad[['source', 'route_id', 'station_id']].values[:5].tolist()}")
    src["seq"] = pd.to_numeric(src["seq"], errors="raise").astype(int)
    dup = src[src.duplicated(["route_id", "seq"], keep=False)]
    if len(dup):
        raise ValueError(f"(노선, 순번) 이 겹칩니다: {dup[['source', 'route_id', 'seq']].values[:5].tolist()}")
    src["src_lon"] = pd.to_numeric(src["src_lon"])
    src["src_lat"] = pd.to_numeric(src["src_lat"])
    return src[SRC_COLUMNS]


def build(src, stops, assign, types=None):
    """→ (출력 DataFrame, 보고용 dict). assign(lons, lats) 는 RegionIndex.assign 과 같은 dict 목록을 돌려준다.
    types = {route_id: (route_type, route_type_src)} — 없는 노선은 빈칸."""
    by_source_id = {sid: k for k, ids in zip(stops["stop_key"], stops["source_ids"]) for sid in ids.split("|")}
    own = (src["source"].map(PREFIX) + src["station_id"]).map(by_source_id)
    in_keys = src["station_id"].isin(set(stops["stop_key"]))
    # DB 에 없는 정류소 — 원천 좌표로 시군 판정 (정류소 하나에 한 번)
    unres = src[own.isna() & ~in_keys].drop_duplicates("station_id")
    judged = {sid: r["region_method"] in IN_SCOPE
              for sid, r in zip(unres["station_id"], assign(unres["src_lon"].tolist(), unres["src_lat"].tolist()))}
    how = (pd.Series("outside", index=src.index).mask(src["station_id"].map(judged).eq(True), "new")
           .mask(in_keys, "key").mask(own.notna(), "own"))
    src = src.assign(how=how, stop_key=own.fillna(src["station_id"]))

    kept, dropped = src[src["how"] != "outside"].copy(), src[src["how"] == "outside"]
    db = stops.set_index("stop_key")
    in_db = kept["how"].isin(["own", "key"])
    kept["in_db"] = in_db.astype(int)
    kept["name"] = kept["src_name"].where(~in_db, kept["stop_key"].map(db["name"]))
    kept["lat"] = kept["src_lat"].where(~in_db, pd.to_numeric(kept["stop_key"].map(db["lat"])))
    kept["lon"] = kept["src_lon"].where(~in_db, pd.to_numeric(kept["stop_key"].map(db["lon"])))
    kept["is_virtual"] = kept["src_name"].map(is_virtual).where(
        ~in_db, kept["stop_key"].map(db["is_virtual"]).eq("1")).astype(int)
    t = kept["route_id"].map(types or {})
    kept["route_type"] = t.map(lambda v: v[0] if isinstance(v, tuple) else "")
    kept["route_type_src"] = t.map(lambda v: v[1] if isinstance(v, tuple) else "")
    out = kept.sort_values(["source", "route_id", "seq"]).reset_index(drop=True)

    # 보고용: 목록 중간에서 뺀 대상 밖 정류소 (앞뒤 정류소가 순번 차이만큼 떨어져 이어진다)
    span = out.groupby("route_id")["seq"].agg(["min", "max"])
    d = dropped.join(span, on="route_id")
    interior = d[(d["seq"] > d["min"]) & (d["seq"] < d["max"])]
    empty = sorted(set(src["route_id"]) - set(out["route_id"]))

    stat = out[out["in_db"] == 1].drop_duplicates(["source", "station_id"])
    dist = [haversine_m(a, b, c, e) for a, b, c, e in zip(stat["src_lon"], stat["src_lat"], stat["lon"], stat["lat"])]
    stat = stat.assign(dist_m=dist)
    renamed = stat[stat["src_name"].map(lambda n: name_key(n, "bus")) != stat["name"].map(lambda n: name_key(n, "bus"))]

    gaps = []
    for rid, g in out.groupby("route_id", sort=False):
        r = g.to_dict("records")
        gaps += [(rid, a, b, haversine_m(a["lon"], a["lat"], b["lon"], b["lat"])) for a, b in zip(r, r[1:])]
    info = {"src": src, "dropped": dropped, "interior": interior, "empty": empty, "coord": stat,
            "renamed": renamed, "gaps": gaps}
    return out[COLUMNS], info


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="버스 노선–정류소 순서표 (서울·경기)")
    ap.add_argument("--seoul", type=Path, default=ROOT / "data" / "csv" / "서울시버스노선별정류소정보(20260902).xlsx")
    ap.add_argument("--gbis", type=Path, default=None,
                    help="GBIS 노선–정류소 파일 (기본: data/external/gbis/ 의 가장 최근 버전 — fetch_gbis.py 로 받는다)")
    ap.add_argument("--gbis-routes", type=Path, default=None, help="GBIS 노선 파일 — 노선 유형 (기본: 가장 최근 버전)")
    ap.add_argument("--seoul-routes", type=Path, default=None,
                    help="서울 노선 목록 — 노선 유형 (기본: 가장 최근 날짜 — fetch_seoul_routes.py 로 받는다)")
    ap.add_argument("--stops", type=Path, default=ROOT / "data" / "processed" / "bus_stops.csv")
    ap.add_argument("--boundaries", type=Path, default=ROOT / "data" / "processed" / "admin_sgg.geojson")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "processed" / "bus_route_stops.csv")
    ap.add_argument("--report", type=Path, default=ROOT / "data" / "reports" / "bus_route_stops.md")
    args = ap.parse_args(argv)
    try:
        args.gbis = args.gbis or latest("routestation")
        args.gbis_routes = args.gbis_routes or latest("route")
        args.seoul_routes = args.seoul_routes or latest_seoul_routes()
    except FileNotFoundError as e:
        sys.exit(f"실패: {e}")

    inputs = [file_line(p) for p in (args.seoul, args.gbis, args.gbis_routes, args.seoul_routes, args.stops,
                                     args.boundaries)]
    seoul = pd.read_excel(args.seoul, dtype=str).fillna("")
    gbis = read_gbis(args.gbis)
    stops = pd.read_csv(args.stops, dtype=str, encoding="utf-8-sig").fillna("")
    regions = RegionIndex(args.boundaries, snap_max_m=SNAP_MAX_M)
    try:
        seoul_types = seoul_route_types(pd.read_csv(args.seoul_routes, dtype=str, encoding="utf-8-sig").fillna(""))
        types = {**seoul_types, **gbis_route_types(read_gbis(args.gbis_routes))}
        out, info = build(normalize(seoul, gbis), stops, regions.assign, types)
    except ValueError as e:
        sys.exit(f"실패: {e}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.out, index=False, encoding="utf-8-sig", float_format="%.7f")

    src = info["src"]
    how_labels = [("own", "정류소 DB 행 — 자기 BIS 레코드"), ("key", "정류소 DB 행 — 다른 BIS 만 등록 (ID = stop_key)"),
                  ("new", "DB 에 없음 · 대상 안 → 원천 이름·좌표 (in_db=0)"), ("outside", "DB 에 없음 · 대상 밖 → 뺌")]
    ferry = seoul[seoul["노선명"].str.match(FERRY_ROUTE)]
    steps = [("서울 원천 중 배 노선 (한강버스) — 뺌", f"{ferry['ROUTE_ID'].nunique()} 노선 · {len(ferry)} 행")]
    for label, sub, o in (("서울", src[src["source"] == "seoul"], out[out["source"] == "seoul"]),
                          ("경기", src[src["source"] == "gyeonggi"], out[out["source"] == "gyeonggi"])):
        steps += [(f"{label} 원천 행", len(sub)), (f"{label} 원천 노선", sub["route_id"].nunique()),
                  (f"{label} 원천 정류소 (고유 ID)", sub["station_id"].nunique())]
        for h, text in how_labels:
            hs = sub[sub["how"] == h]
            steps.append((f"{label} {text} — 행 / 고유 정류소", f"{len(hs):,} / {hs['station_id'].nunique():,}"))
        steps += [(f"{label} 출력 행", len(o)), (f"{label} 출력 노선", o["route_id"].nunique())]
    steps += [("출력 행", len(out)), ("출력 노선", out["route_id"].nunique()),
              ("출력 고유 정류소 (stop_key)", out["stop_key"].nunique()),
              ("출력 행 중 in_db=0", int((out["in_db"] == 0).sum())),
              ("출력 행 중 미정차 (is_virtual=1)", int(out["is_virtual"].sum())),
              ("정류소가 모두 대상 밖이라 빠진 노선", len(info["empty"])),
              ("목록 중간에서 뺀 대상 밖 정류소 (행)", len(info["interior"])),
              (f"인접 정류소 직선거리 > {LONG_GAP_M / 1000:.0f} km (구간)", sum(1 for *_, d in info["gaps"] if d > LONG_GAP_M)),
              ("정류소 DB 행 중 어느 노선에도 없는 것", int((~stops["stop_key"].isin(set(out["stop_key"]))).sum())),
              ("시군 경계 동률 (n_ties, DB 에 없는 정류소)", regions.n_ties)]
    table = md_table(steps)
    print(table)

    coord = info["coord"]
    q = coord["dist_m"].quantile([0.5, 0.9, 0.99]).tolist()
    far = coord[coord["dist_m"] > COORD_WARN_M].sort_values("dist_m", ascending=False)
    far_rows = [(r.source, r.station_id, r.stop_key, r.src_name, r.name, f"{r.dist_m:,.0f}") for r in far.itertuples()]
    n_stops = out.groupby("route_id").size()
    route_rows = [(label, o["route_id"].nunique(), int(o.groupby("route_id").size().median()),
                   int(o.groupby("route_id").size().max()))
                  for label, o in (("서울", out[out["source"] == "seoul"]), ("경기", out[out["source"] == "gyeonggi"]))]
    per_route = out.drop_duplicates("route_id")
    type_rows = [(s, t or "(없음)", ts or "-", n) for (s, t, ts), n in
                 per_route.groupby(["source", "route_type", "route_type_src"]).size().sort_values(ascending=False).items()]
    inter = info["interior"].groupby(["source", "route_id", "route_name"]).agg(n=("seq", "size"),
                                                                                 names=("src_name", lambda s: ", ".join(s)))
    inter_rows = [(s, rid, name, int(r.n), r.names[:80]) for (s, rid, name), r in inter.sort_values("n", ascending=False).iterrows()]
    new = out[out["in_db"] == 0].drop_duplicates("stop_key")
    new_by = Counter(new["source"])
    new_v = Counter(new.loc[new["is_virtual"] == 1, "source"])
    long_rows = sorted(((rid, a["route_name"], a["name"], b["name"], f"{d / 1000:.1f}") for rid, a, b, d in info["gaps"]
                        if d > LONG_GAP_M), key=lambda t: -float(t[4]))
    table_or_none = lambda rows, headers: md_table(rows, headers) if rows else "없음"  # noqa: E731
    report = "\n".join([
        "# 버스 노선–정류소 순서표 (build_bus_route_seq.py)",
        "",
        f"- 실행 시각: {datetime.now(KST):%Y-%m-%d %H:%M:%S} KST",
        *[f"- 입력: {s}" for s in inputs],
        f"- 출력: {file_line(args.out)}",
        f"- 파라미터: snap 최대 {SNAP_MAX_M:,.0f} m, 좌표 차이 경고 > {COORD_WARN_M:,.0f} m, 긴 인접 구간 > {LONG_GAP_M / 1000:.0f} km",
        "- 원천: 서울 열린데이터광장 OA-1095 서울시 버스노선별 정류소 정보(공공누리 1유형) · "
        "경기도 버스정보 기반정보 노선–정류소 (data.go.kr 15080658)",
        f"- {ATTRIBUTION}",
        "- 한 행 = 노선의 정류소 하나. `seq` 는 원천 순번 그대로(운행 순서, 비어 있을 수 있음) — 같은 노선에서 작을수록 상류. "
        "경기의 `updown` 은 상행 → 하행 순으로 한 순번열에 이어지고 첫 하행이 회차 정류소다. 서울은 `updown` 이 없다.",
        "- `in_db=1` 이면 이름·좌표가 정류소 DB 값, `in_db=0` 이면 원천 값(정류소 DB 에 없는 정류소 — 이름 매칭 대상이 아니다).",
        "",
        "## 단계별 건수",
        "",
        table,
        "",
        "## 노선 수와 노선당 정류소",
        "",
        md_table(route_rows, ("원천", "노선", "정류소 중앙", "최대")),
        "",
        "## 노선 유형 (route_type = 카카오 버스 유형 이름에 맞춘 값, route_type_src = 원천 유형 이름)",
        "",
        "- 경기: GBIS 노선 파일의 노선유형코드. 서울: 서울 노선정보조회 노선 목록의 routeType. 목록에 없는 노선은 빈칸.",
        "",
        md_table(type_rows, ("원천", "route_type", "route_type_src", "노선")),
        "",
        f"## 원천 좌표 vs 정류소 DB 좌표 (in_db=1 고유 정류소 {len(coord):,})",
        "",
        f"- 중앙 {q[0]:.1f} m · p90 {q[1]:.1f} m · p99 {q[2]:.1f} m · 최대 {coord['dist_m'].max():,.0f} m",
        f"- 이름 키가 다른 정류소: {len(info['renamed']):,} (출력 이름은 DB 값)",
        "",
        f"### {COORD_WARN_M:,.0f} m 넘게 다른 정류소 ({len(far_rows)}) — 같은 ID 가 다른 위치로 옮겨졌거나 다른 정류소에 다시 쓰였을 수 있다",
        "",
        table_or_none(far_rows, ("원천", "정류소 ID", "stop_key", "원천 이름", "DB 이름", "거리 m")),
        "",
        f"## DB 에 없는 대상 안 정류소 (in_db=0, 고유 {len(new):,} — 서울 {new_by['seoul']:,} · 경기 {new_by['gyeonggi']:,})",
        "",
        f"- 미정차(통과 노드) {int(new['is_virtual'].sum()):,} (서울 `(가상)` {int(new_v['seoul']):,} · "
        f"경기 `(미정차)` {int(new_v['gyeonggi']):,}) — 서울 노선 원천의 `(가상)` 노드는 서울 정류소 파일에 없다.",
        f"- 나머지 {int((new['is_virtual'] == 0).sum()):,} 은 노선 원천에만 있고 정류소 파일에는 없는 정류소다 "
        "(두 파일의 기준일 차이 등).",
        "",
        f"## 목록 중간에서 뺀 대상 밖 정류소 ({len(inter_rows)} 노선) — 앞뒤 정류소가 순번 차이를 두고 이어진다",
        "",
        table_or_none(inter_rows, ("원천", "route_id", "노선", "뺀 정류소", "이름")),
        "",
        f"## 인접 정류소 직선거리 > {LONG_GAP_M / 1000:.0f} km ({len(long_rows)}) — 고속도로 구간·대상 밖 구간 건너뜀",
        "",
        table_or_none(long_rows[:40], ("route_id", "노선", "정류소", "다음 정류소", "km")),
        "",
        f"## 정류소가 모두 대상 밖이라 빠진 노선 ({len(info['empty'])})",
        "",
        ", ".join(info["empty"]) or "없음",
        "",
    ])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(f"노선당 정류소 중앙 {int(n_stops.median())}")
    print(f"출력: {rel(args.out)}")
    print(f"보고서: {rel(args.report)}")


if __name__ == "__main__":
    main()
