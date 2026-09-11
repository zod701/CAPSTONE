"""도시철도역 정제 (전국도시철도역사정보표준데이터 → 서울·경기 역).

0) 원천 행 보정 — 다른 역의 좌표가 들어간 행(ref/subway_coord_overrides.csv), 서지 않는 노선으로 잘못 들어간 행
   (ref/subway_station_drops.csv). (line_raw, 역번호) 로 찾고 역 이름까지 맞아야 적용한다
1) 노선명 공백 정리(`line_raw`), 기준일 파싱(형식 3종 — 빈 값·무효는 빈 문자열)
2) 좌표 파싱, (line_raw, 역번호) 중복 제거 — 최신 기준일 행을 남기고 다른 이름은 aliases
3) RegionIndex 로 시군 판정 → contain/snap 만 남긴다
4) ref/subway_line_map.csv 조인 — 대상 노선이 매핑에 없으면 실패
5) station_id = {line_id}-{역번호} (역번호만으로는 유일하지 않다)
"""
import argparse
import itertools
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

from build_boundaries import ATTRIBUTION
from fetch_boundary import KST, ROOT, file_line, md_table, rel
from geoutil import GridIndex, haversine_m
from regions import LAT_RANGE, LON_RANGE, RegionIndex
from textnorm import name_key, name_norm

SHEET = "표준데이터 역사"
DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y%m%d")
EMPTY, INVALID = "빈 값", "무효"
STALE_BEFORE = "2024-01-01"   # 이보다 이른 기준일은 "오래됨"
SNAP_MAX_M = 2000.0
GTXA_LINE = "GTX-A"           # 원천 XLSX 에 없는 노선 — ref/gtxa_stations.csv 로 들어온다
GTXA_DATE = "2026-09-11"      # 참조표 작성일 (VWorld 조회일)
CLUSTER_M = 300.0             # 같은 name_key 가 이 거리 안에서 이어지면 한 물리 역
TWIN_M = 100.0                # 같은 노선의 다른 역이 이보다 가까우면 좌표 복사 의심
COLUMNS = ["station_id", "name", "name_norm", "name_key", "aliases", "line_raw", "line_code", "line_id",
           "line_group", "lat", "lon", "sido_cd", "sido_nm", "sgg_cd", "sgg_nm", "region_method",
           "operator", "is_transfer", "data_date"]
_ADDR_IN_SCOPE = re.compile(r"^(서울|경기)")


def collapse_ws(s):
    """`수도권  도시철도 9호선` → `수도권 도시철도 9호선`."""
    return re.sub(r"\s+", " ", s or "").strip()


def parse_date(s):
    """→ (ISO 날짜 또는 "", 형식). 형식은 DATE_FORMATS 중 하나, EMPTY 또는 INVALID."""
    s = (s or "").strip()
    if not s:
        return "", EMPTY
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat(), fmt
        except ValueError:
            pass
    return "", INVALID  # 예: 1900-01-00


def dedupe(df):
    """(line_raw, 역번호) 중복 → 최신 data_date 행 하나 (빈 날짜는 맨 뒤, 같으면 파일 순서).
    버린 행의 다른 이름은 aliases 로. → (df, 중복 그룹 목록)"""
    key = ["line_raw", "역번호"]
    # ISO 문자열 내림차순이면 빈 날짜("")가 맨 뒤로 간다
    ranked = df.assign(_i=range(len(df))).sort_values(["data_date", "_i"], ascending=[False, True])
    aliases, groups = {}, []
    for _, g in ranked[ranked.duplicated(key, keep=False)].groupby(key, sort=False):
        top, rest = g.iloc[0], g.iloc[1:]
        alias = "|".join(dict.fromkeys(n for n in rest["역사명"] if n != top["역사명"]))
        aliases[top["_i"]] = alias
        groups.append({"line_raw": top["line_raw"], "역번호": top["역번호"],
                       "kept": (top["역사명"], top["data_date"]),
                       "dropped": list(zip(rest["역사명"], rest["data_date"])), "aliases": alias})
    kept = ranked.drop_duplicates(key).sort_values("_i")
    kept["aliases"] = [aliases.get(i, "") for i in kept["_i"]]
    return kept.drop(columns="_i").reset_index(drop=True), groups


def ref_station_rows(ref, line, data_date):
    """원천 XLSX 에 없는 노선의 참조표(ref/gtxa_stations.csv 등)를 원천 행 모양으로 바꾼다.
    그러면 좌표 검사·지역 판정·노선 매핑·station_id 를 원천 역과 같은 과정으로 거친다."""
    return pd.DataFrame({
        "역번호": ref["station_no"], "역사명": ref["name"], "노선번호": line, "노선명": line,
        "환승역구분": ref["is_transfer"].map({"1": "환승역", "0": "일반역"}).fillna("일반역"),
        "역위도": ref["lat"], "역경도": ref["lon"], "운영기관명": ref["operator"],
        "역사도로명주소": "", "데이터기준일자": data_date,
    }).astype(str)


def join_lines(df, line_map, station_overrides=None):
    """line_raw → line_id, line_group. 매핑에 없는 노선이 있으면 ValueError (노선·행 수 목록).

    station_overrides: (line_raw, station_no) → line_group. 운영 노선명 하나가 승객 노선 둘에 걸친 경우
    (경원선 용산–왕십리는 경의중앙선, 청량리 이북은 1호선)를 역 단위로 바로잡는다. 어느 행에도 안 맞는
    보정은 원천이 바뀐 신호이므로 ValueError."""
    m = line_map.set_index("raw_line_name")
    missing = sorted(set(df["line_raw"]) - set(m.index))
    if missing:
        n = df["line_raw"].value_counts()
        raise ValueError("노선 매핑(subway_line_map.csv)에 없는 대상 노선: "
                         + ", ".join(f"{s!r} {n[s]}행" for s in missing))
    out = df.assign(line_id=df["line_raw"].map(m["line_id"]), line_group=df["line_raw"].map(m["line_group"]))
    if station_overrides is None or not len(station_overrides):
        return out
    ov = {(r["line_raw"], r["station_no"]): r["line_group"] for r in station_overrides.to_dict("records")}
    unknown_groups = sorted(set(ov.values()) - set(line_map["line_group"]))
    if unknown_groups:
        raise ValueError(f"역 단위 노선군 보정에 없는 line_group: {unknown_groups}")
    keys = list(zip(out["line_raw"], out["역번호"]))
    stale = sorted(set(ov) - set(keys))
    if stale:
        raise ValueError(f"역 단위 노선군 보정이 어느 역에도 맞지 않습니다: {stale}")
    out["line_group"] = [ov.get(k, g) for k, g in zip(keys, out["line_group"])]
    return out


def station_ids(df):
    ids = df["line_id"] + "-" + df["역번호"]
    dup = sorted(set(ids[ids.duplicated()]))
    if dup:
        raise ValueError(f"station_id 가 유일하지 않습니다: {', '.join(dup)}")
    return ids


def station_clusters(keys, lons, lats, max_m=CLUSTER_M):
    """같은 name_key 이면서 max_m 이내로 이어지는 행을 한 물리 역으로 묶는다 (단일 연결).
    → 행마다 묶음 번호"""
    parent = list(range(len(keys)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    by_key = defaultdict(list)
    for i, k in enumerate(keys):
        by_key[k].append(i)
    for idx in by_key.values():
        for a, b in itertools.combinations(idx, 2):
            if haversine_m(lons[a], lats[a], lons[b], lats[b]) <= max_m:
                parent[find(a)] = find(b)
    return [find(i) for i in range(len(keys))]


def coord_twins(out, max_m=TWIN_M):
    """같은 line_raw 인데 name_key 가 다른 두 행이 max_m 이내 → [(거리 m, i, j)].
    한 노선의 역 간격은 수백 m 이상이므로 원천이 다른 역 좌표를 복사한 것으로 본다."""
    lons, lats = out["lon"].tolist(), out["lat"].tolist()
    lines, keys = out["line_raw"].tolist(), out["name_key"].tolist()
    grid = GridIndex(lons, lats)
    return [(d, i, j) for i in range(len(out)) for j, d in grid.near(lons[i], lats[i], max_m)
            if j > i and lines[j] == lines[i] and keys[j] != keys[i]]


def fix_source_rows(df, coord_overrides=None, drops=None):
    """원천 행 보정. 행은 (line_raw, 역번호) 로 찾고 역 이름(name_key)까지 같아야 한다 — 어느 행에도 맞지 않거나
    이름이 다르면 원천이 바뀐 신호이므로 ValueError.
    coord_overrides: 다른 역(동명이역·옆 역)의 좌표가 들어간 행의 좌표를 참조표 값으로 바꾼다
    drops: 그 노선이 서지 않는 역인데 원천에 들어간 행을 뺀다
    → (df, 바꾼 좌표 [(line_raw, 역번호, 역사명, 원천 좌표, 보정 좌표, 이동 m, 방법)], 뺀 행 [(line_raw, 역번호, 역사명, 비고)])"""
    df = df.copy()
    keys = list(zip(df["line_raw"], df["역번호"]))

    def locate(r, what):
        hit = [i for i, k in zip(df.index, keys) if k == (r["line_raw"], r["station_no"])]
        if not hit:
            raise ValueError(f"{what}가 어느 역에도 맞지 않습니다: {r['line_raw']} {r['station_no']}")
        for i in hit:
            if name_key(df.at[i, "역사명"], "subway") != name_key(r["name"], "subway"):
                raise ValueError(f"{what}의 역 이름이 원천과 다릅니다: {r['line_raw']} {r['station_no']} "
                                 f"{r['name']!r} ≠ {df.at[i, '역사명']!r}")
        return hit

    moved, removed, drop_idx = [], [], []
    for r in coord_overrides.to_dict("records") if coord_overrides is not None else []:
        for i in locate(r, "좌표 보정"):
            old_lon, old_lat = df.at[i, "역경도"], df.at[i, "역위도"]
            try:
                d = haversine_m(float(old_lon), float(old_lat), float(r["lon"]), float(r["lat"]))
            except ValueError:
                d = float("nan")
            df.at[i, "역경도"], df.at[i, "역위도"] = r["lon"], r["lat"]
            moved.append((r["line_raw"], r["station_no"], df.at[i, "역사명"], f"{old_lat}, {old_lon}",
                          f"{r['lat']}, {r['lon']}", d, r.get("method", "")))
    for r in drops.to_dict("records") if drops is not None else []:
        hit = locate(r, "행 제외")
        drop_idx += hit
        removed += [(r["line_raw"], r["station_no"], df.at[i, "역사명"], r.get("note", "")) for i in hit]
    return df.drop(index=drop_idx), moved, removed


def clean(raw, assign, line_map, station_overrides=None, coord_overrides=None, drops=None):
    """원본 DataFrame → (출력 DataFrame, 보고용 dict).
    assign(lons, lats) 는 RegionIndex.assign 과 같은 dict 목록을 돌려준다.
    대상 노선 미매핑·station_id 중복·맞지 않는 역 단위 보정·맞지 않는 원천 행 보정은 ValueError."""
    df = raw.copy()
    df["line_raw"] = df["노선명"].map(collapse_ws)
    parsed = [parse_date(s) for s in df["데이터기준일자"]]
    df["data_date"] = [d for d, _ in parsed]
    df["date_fmt"] = [f for _, f in parsed]
    fmts = Counter(df["date_fmt"])
    invalid = sorted(set(df.loc[df["date_fmt"] == INVALID, "데이터기준일자"]))
    steps = [("원본 행", len(df)),
             ("노선명 공백 정리된 행 (연속 공백 → 한 칸)", int((df["line_raw"] != df["노선명"]).sum()))]
    steps += [(f"기준일 형식 {f}", fmts[f]) for f in (*DATE_FORMATS, EMPTY)]
    steps.append((f"기준일 형식 {INVALID} → 빈 값 ({', '.join(invalid) or '-'})", fmts[INVALID]))
    df, moved, removed = fix_source_rows(df, coord_overrides, drops)
    steps += [("좌표 보정 (ref/subway_coord_overrides.csv)", len(moved)),
              ("원천 오류 행 제외 (ref/subway_station_drops.csv)", len(removed))]

    df["lat"] = pd.to_numeric(df["역위도"], errors="coerce")
    df["lon"] = pd.to_numeric(df["역경도"], errors="coerce")
    ok = df["lat"].between(*LAT_RANGE) & df["lon"].between(*LON_RANGE)
    steps.append(("좌표 무효 제외", int((~ok).sum())))
    df, dups = dedupe(df[ok])
    steps += [("(line_raw, 역번호) 중복 제거", sum(len(g["dropped"]) for g in dups)),
              ("중복 제거 후", len(df))]

    df = pd.concat([df, pd.DataFrame(assign(df["lon"].tolist(), df["lat"].tolist()))], axis=1)
    inside = df["region_method"].isin(["contain", "snap"])
    dropped = df[~inside]
    steps += [(f"지역 판정 {m}", int((df["region_method"] == m).sum())) for m in ("contain", "snap")]
    # nearest 의 거리는 행마다 달라 묶이지 않으므로 떼고 센다
    drop_n = Counter(re.sub(r" \d+ m$", "", d) for d in dropped["detail"])
    steps += [(f"제외 ({k})", n) for k, n in drop_n.most_common()]

    df = join_lines(df[inside].reset_index(drop=True), line_map, station_overrides)
    n_override = 0 if station_overrides is None else len(station_overrides)
    steps.append(("역 단위 노선군 보정 (ref/subway_station_line_overrides.csv)", n_override))
    df["station_id"] = station_ids(df)
    df = df.rename(columns={"역사명": "name", "노선번호": "line_code", "운영기관명": "operator"})
    df["name_norm"] = df["name"].map(name_norm)
    df["name_key"] = df["name"].map(lambda s: name_key(s, "subway"))
    df["is_transfer"] = df["환승역구분"].str.contains("환승").astype(int)
    out = df[COLUMNS]

    clusters = station_clusters(out["name_key"].tolist(), out["lon"].tolist(), out["lat"].tolist())
    steps += [("대상 노선 (line_raw)", out["line_raw"].nunique()),
              ("최종 역 행 (station_id 유일)", len(out)),
              ("환승역 표시 (is_transfer=1)", int(out["is_transfer"].sum())),
              (f"물리 역 (같은 name_key, {CLUSTER_M:.0f} m 이내 묶음)", len(set(clusters)))]
    info = {
        "steps": steps, "dups": dups, "clusters": clusters, "date_fmt": df["date_fmt"], "moved": moved, "removed": removed,
        "snapped": out[out["region_method"] == "snap"],
        # 주소는 서울·경기인데 좌표가 대상 밖 → 원천 좌표 오류 의심
        "addr_suspect": dropped[dropped["역사도로명주소"].str.match(_ADDR_IN_SCOPE)],
    }
    return out, info


def line_rows(out, date_fmt, line_map):
    """노선(line_raw)별 행 수·기준일 범위·오래됨/빈/무효 날짜 표 (매핑 파일 순서).
    date_fmt 는 out 과 같은 색인의 기준일 형식 (parse_date 의 두 번째 값)."""
    order = {s: i for i, s in enumerate(line_map["raw_line_name"])}
    rows = []
    for line, g in sorted(out.groupby("line_raw"), key=lambda kv: order[kv[0]]):
        dates = g["data_date"][g["data_date"] != ""]
        fmt = date_fmt.loc[g.index]
        n = {"오래됨": int((dates < STALE_BEFORE).sum()),
             "빈 날짜": int((fmt == EMPTY).sum()), "무효 날짜": int((fmt == INVALID).sum())}
        flag = " · ".join(k for k, v in n.items() if v)
        rows.append((line, "·".join(dict.fromkeys(g["line_group"])), "·".join(dict.fromkeys(g["operator"])), len(g),
                     f"{dates.min()} ~ {dates.max()}" if len(dates) else "-", *n.values(), flag))
    return rows


def homonym_rows(out, clusters):
    """같은 name_key 인데 CLUSTER_M 밖으로 떨어진 역 (서로 다른 물리 역)."""
    by_key = defaultdict(lambda: defaultdict(list))
    for (_, r), c in zip(out.iterrows(), clusters):
        by_key[r["name_key"]][c].append(r)
    rows = []
    for k, cs in sorted(by_key.items()):
        if len(cs) > 1:
            desc = " / ".join(f"{rs[0]['sgg_nm']} {'·'.join(dict.fromkeys(r['line_raw'] for r in rs))}"
                              for rs in cs.values())
            rows.append((k, len(cs), desc))
    return rows


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="도시철도역 정제 (서울·경기)")
    ap.add_argument("--src", type=Path, default=ROOT / "data" / "csv" / "전체_도시철도역사정보_20260630.xlsx")
    ap.add_argument("--boundaries", type=Path, default=ROOT / "data" / "processed" / "admin_sgg.geojson")
    ap.add_argument("--line-map", type=Path, default=ROOT / "data" / "ref" / "subway_line_map.csv")
    ap.add_argument("--station-overrides", type=Path,
                    default=ROOT / "data" / "ref" / "subway_station_line_overrides.csv")
    ap.add_argument("--gtxa", type=Path, default=ROOT / "data" / "ref" / "gtxa_stations.csv",
                    help="원천에 없는 GTX-A 역 (VWorld 지오코딩 + 운영사 자료 교차 확인, method.md E8)")
    ap.add_argument("--coord-overrides", type=Path, default=ROOT / "data" / "ref" / "subway_coord_overrides.csv",
                    help="다른 역의 좌표가 들어간 원천 행의 좌표 (VWorld 장소 검색 교차 확인, method.md E8)")
    ap.add_argument("--drops", type=Path, default=ROOT / "data" / "ref" / "subway_station_drops.csv",
                    help="서지 않는 노선으로 원천에 잘못 들어간 행")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "processed" / "subway_stations.csv")
    ap.add_argument("--report", type=Path, default=ROOT / "data" / "reports" / "subway_stations.md")
    args = ap.parse_args(argv)

    inputs = [file_line(p) for p in (args.src, args.boundaries, args.line_map)]
    inputs += [file_line(p) for p in (args.station_overrides, args.gtxa, args.coord_overrides, args.drops) if p.exists()]
    raw = pd.read_excel(args.src, sheet_name=SHEET, dtype=str).fillna("")
    n_src = len(raw)
    if args.gtxa.exists():
        gtxa = pd.read_csv(args.gtxa, dtype=str, encoding="utf-8-sig").fillna("")
        raw = pd.concat([raw, ref_station_rows(gtxa, GTXA_LINE, GTXA_DATE)], ignore_index=True).fillna("")
    line_map = pd.read_csv(args.line_map, dtype=str, encoding="utf-8-sig").fillna("")
    read_ref = lambda p: pd.read_csv(p, dtype=str, encoding="utf-8-sig").fillna("") if p.exists() else None  # noqa: E731
    station_overrides = read_ref(args.station_overrides)
    regions = RegionIndex(args.boundaries, snap_max_m=SNAP_MAX_M)
    try:
        out, info = clean(raw, regions.assign, line_map, station_overrides,
                          read_ref(args.coord_overrides), read_ref(args.drops))
    except ValueError as e:
        sys.exit(f"실패: {e}")
    steps = [("원천 XLSX 행", n_src), (f"참조표 추가 역 ({GTXA_LINE}, ref/gtxa_stations.csv)", len(raw) - n_src)]
    steps += info["steps"] + [("시군 경계 동률 (n_ties)", regions.n_ties)]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.out, index=False, encoding="utf-8-sig", float_format="%.7f")
    table = md_table(steps)
    print(table)

    groups = dict.fromkeys(line_map["line_group"])
    group_rows = [(g, int((out["line_group"] == g).sum()),
                   ", ".join(dict.fromkeys(out.loc[out["line_group"] == g, "line_raw"])) or "-")
                  for g in groups]
    group_rows.append(("합계", len(out), f"line_raw {out['line_raw'].nunique()}개"))
    dup_rows = [(g["line_raw"], g["역번호"], f"{g['kept'][0]} ({g['kept'][1] or '날짜 없음'})",
                 ", ".join(f"{n} ({d or '날짜 없음'})" for n, d in g["dropped"]), g["aliases"] or "-")
                for g in info["dups"]]
    suspect = [(r["노선명"], r["역번호"], r["역사명"], r["역사도로명주소"], f"{r['lat']:.6f}, {r['lon']:.6f}",
                r["detail"]) for _, r in info["addr_suspect"].iterrows()]
    label = lambda k: f"{out.at[k, 'station_id']} {out.at[k, 'name']}"
    twins = [(out.at[i, "line_raw"], f"{d:.1f}", label(i), label(j)) for d, i, j in coord_twins(out)]
    snapped = [(r["station_id"], r["name"], r["sgg_nm"]) for _, r in info["snapped"].iterrows()]
    homonyms = homonym_rows(out, info["clusters"])
    table_or_none = lambda rows, headers: md_table(rows, headers) if rows else "없음"
    report = "\n".join([
        "# 도시철도역 정제 (clean_subway_stations.py)",
        "",
        f"- 실행 시각: {datetime.now(KST):%Y-%m-%d %H:%M:%S} KST",
        *[f"- 입력: {s}" for s in inputs],
        f"- 출력: {file_line(args.out)}",
        f"- 파라미터: 시트 `{SHEET}`, 기준일 형식 {' / '.join(f'`{f}`' for f in DATE_FORMATS)}, "
        f"오래됨 기준 < {STALE_BEFORE}, snap 최대 {SNAP_MAX_M:,.0f} m, 물리 역 묶음 {CLUSTER_M:.0f} m, "
        f"좌표 복사 의심 {TWIN_M:.0f} m",
        f"- {ATTRIBUTION}",
        "",
        "## 단계별 건수",
        "",
        table,
        "",
        "## 중복 (line_raw, 역번호) — 최신 기준일 유지",
        "",
        table_or_none(dup_rows, ("line_raw", "역번호", "남긴 행", "버린 행", "aliases")),
        "",
        "## 좌표 보정 — 원천 좌표가 다른 역의 것 (ref/subway_coord_overrides.csv)",
        "",
        table_or_none([(a, b, c, d, e, f"{m / 1000:,.2f}", meth) for a, b, c, d, e, m, meth in info["moved"]],
                      ("line_raw", "역번호", "역사명", "원천 위도, 경도", "보정 위도, 경도", "옮긴 거리 km", "방법")),
        "",
        "## 원천 오류 행 제외 (ref/subway_station_drops.csv)",
        "",
        table_or_none(info["removed"], ("line_raw", "역번호", "역사명", "비고")),
        "",
        "## 노선군별 역 행",
        "",
        md_table(group_rows, ("line_group", "행", "line_raw")),
        "",
        "- **GTX-A 원천 부재**: 원천 표준데이터에 GTX-A 역이 없다. `ref/line_groups.csv` 에 노선군·색만 두었으므로 "
        "카카오 경로의 GTX-A 승·하차역은 매칭되지 않는다.",
        "",
        f"## 노선별 기준일 (오래됨 = {STALE_BEFORE} 이전)",
        "",
        md_table(line_rows(out, info["date_fmt"], line_map),
                 ("line_raw", "line_group", "운영기관", "행", "기준일 범위", "오래됨", "빈 날짜", "무효 날짜",
                  "표시")),
        "",
        "## 좌표 오류 의심 — 주소는 서울·경기인데 좌표가 대상 밖이라 제외된 행",
        "",
        table_or_none(suspect, ("노선명", "역번호", "역사명", "역사도로명주소", "위도, 경도", "판정")),
        "",
        f"## 좌표 복사 의심 — 같은 노선의 다른 이름 역이 {TWIN_M:.0f} m 이내 (한쪽 좌표가 틀림, 출력에는 그대로 둠)",
        "",
        table_or_none(twins, ("line_raw", "거리 m", "역 A", "역 B")),
        "",
        "## snap 된 역",
        "",
        table_or_none(snapped, ("station_id", "name", "sgg_nm")),
        "",
        f"## 동명이역 — 같은 name_key 인데 {CLUSTER_M:.0f} m 밖 (서로 다른 물리 역)",
        "",
        table_or_none(homonyms, ("name_key", "묶음", "시군 노선")),
        "",
    ])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(f"출력: {rel(args.out)}")
    print(f"보고서: {rel(args.report)}")


if __name__ == "__main__":
    main()
