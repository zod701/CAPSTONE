"""실시간 지하철 도착 조회용 역명 매핑표 (역 하나 = 한 행, `subway_stations.csv` 와 1:1).

서울 열린데이터광장 '실시간 지하철' 도착 API 는 역명 **정확 일치**만 받는다 — 우리 표시 이름으로 물으면
조용히 0 건이 온다(`양재(서초구청)`→`양재`, `서울역`→`서울`, 경의중앙·경춘·수인분당의 `○○역` 접미 전부).
그래서 역마다 질의에 쓸 **실시간 원문 이름**(`live_name`)과 응답을 거를 키(`live_statn_id`)를 미리 이어 둔다.
매칭은 실시간 역정보 파일의 (노선군, 이름 키)로 한다 — 파일의 `호선이름` 문자열이 우리 `line_group` 과 같아
노선군 매핑 참조표가 따로 필요 없고 `SUBWAY_ID` 를 파일에서 그대로 끌어온다.

개명·방향 표기로 이름이 아예 다른 역은 `ref/subway_live_names.csv` 로 잇고 그 표를 **가장 먼저** 적용한다
(자동 매칭을 먼저 돌리면 `신길온천`처럼 원천에 살아 있는 옛 이름이 엉뚱한 행에 붙을 수 있다). 실시간 자체가
없는 노선군(용인에버라인·의정부경전철·김포골드라인)은 재시도 대상이 아니라 UI 에서 '실시간 미제공' 이다.
"""
import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

from fetch_boundary import KST, ROOT, file_line, md_table, rel
from textnorm import name_key

LIVE_XLSX = ROOT / "data" / "csv" / "실시간도착_역정보(20260902).xlsx"
# 실시간 역정보 파일에 SUBWAY_ID 자체가 없는 노선군. 원천이 바뀌면(추가·제공 시작) 멈춘다.
NO_LIVE_GROUPS = ("김포골드라인", "용인에버라인", "의정부경전철")
COLUMNS = ["station_id", "name", "line_group", "live_subway_id", "live_statn_id", "live_name",
           "match", "note", "data_date"]


def file_date(path):
    """파일 이름의 `(20260902)` → `2026-09-02`. 이 원천에는 기준일 열이 없어 이름에서 읽는다."""
    m = re.search(r"(\d{4})(\d{2})(\d{2})", Path(path).name)
    if not m:
        raise ValueError(f"파일 이름에서 기준일(YYYYMMDD)을 찾지 못했습니다: {Path(path).name}")
    return "-".join(m.groups())


def line_ids(live, line_groups, no_live_groups=NO_LIVE_GROUPS):
    """실시간 역정보 → {노선군: SUBWAY_ID}. `호선이름` 이 우리 `line_group` 과 같은 문자열이다.
    한 이름에 SUBWAY_ID 가 둘이거나, 실시간에 없는 노선군이 기대와 다르면 원천이 바뀐 신호 → ValueError."""
    out = {}
    for r in live:
        got = out.setdefault(r["호선이름"], r["SUBWAY_ID"])
        if got != r["SUBWAY_ID"]:
            raise ValueError(f"실시간 역정보의 한 노선에 SUBWAY_ID 가 둘입니다: {r['호선이름']} {got}/{r['SUBWAY_ID']}")
    groups = {g["line_group"] for g in line_groups}
    absent = tuple(sorted(groups - set(out)))
    if absent != tuple(sorted(no_live_groups)):
        raise ValueError(f"실시간이 없는 노선군이 기대와 다릅니다: {absent} (기대 {tuple(sorted(no_live_groups))})")
    return {g: sid for g, sid in out.items() if g in groups}


def live_index(live):
    """실시간 역정보 → {(SUBWAY_ID, 이름 키): 행}. 같은 노선에 같은 이름 키가 둘이면 매칭이 모호해진다 → ValueError."""
    out = {}
    for r in live:
        k = (r["SUBWAY_ID"], name_key(r["STATN_NM"], "subway"))
        if k in out:
            raise ValueError(f"실시간 역정보에 같은 노선·이름 키가 둘 있습니다: {r['호선이름']} {r['STATN_NM']}")
        out[k] = r
    return out


def auto_hit(station, sid_of, by_key):
    """자동 매칭 → (실시간 행, 쓰인 별칭) 또는 None. 이름 키로 먼저, 그다음 `aliases` 를 `|` 로 쪼개 본다."""
    sid = sid_of.get(station["line_group"])
    if sid is None:
        return None
    names = [nm for nm in (station.get("aliases") or "").split("|") if nm.strip()]
    for i, nm in enumerate([station["name"], *names]):
        hit = by_key.get((sid, name_key(nm, "subway")))
        if hit is not None:
            return hit, ("" if i == 0 else nm)
    return None


def override_map(overrides, stations, sid_of, by_key):
    """보정표 → {station_id: (live_name, SUBWAY_ID, STATN_ID, note)}. `live_name` 이 비면 실시간 미제공 역이다.

    대상 역이 역 DB 에 없거나, 이름이 역 DB 와 다르거나, **이미 자동으로 맞거나**, 적어 둔 실시간 역이
    원천에 없으면 전부 원천이 바뀐 신호이므로 ValueError (다른 보정표 스크립트들과 같은 규칙)."""
    by_id = {s["station_id"]: s for s in stations}
    out = {}
    for r in overrides:
        sid, live_sid = r["station_id"], r["live_subway_id"]
        s = by_id.get(sid)
        if s is None:
            raise ValueError(f"보정표의 역이 역 DB 에 없습니다: {sid} ({r['our_name']})")
        if name_key(s["name"], "subway") != name_key(r["our_name"], "subway"):
            raise ValueError(f"보정표의 역 이름이 역 DB 와 다릅니다: {sid} {r['our_name']!r} ≠ {s['name']!r}")
        if sid in out:
            raise ValueError(f"보정표에 같은 역이 두 번 있습니다: {sid}")
        hit = auto_hit(s, sid_of, by_key)
        if hit is not None:
            raise ValueError(f"보정표가 필요 없습니다(자동으로 맞습니다): {sid} {s['name']} → {hit[0]['STATN_NM']}")
        if r["live_name"]:
            if live_sid != sid_of.get(s["line_group"]):
                raise ValueError(f"보정표의 SUBWAY_ID 가 그 역의 노선군과 다릅니다: {sid} {s['line_group']} {live_sid}")
            live = by_key.get((live_sid, name_key(r["live_name"], "subway")))
            if live is None or live["STATN_ID"] != r["live_statn_id"]:
                raise ValueError("보정표의 실시간 역이 실시간 역정보에 없습니다: "
                                 f"{sid} → {r['live_name']} ({live_sid}/{r['live_statn_id']})")
        out[sid] = (r["live_name"], live_sid, r["live_statn_id"], r["note"])
    return out


def match_row(station, sid_of, by_key, ov, data_date):
    """우리 역 한 행 → 출력 행. 보정표 → 이름 키 → 별칭 순으로 보고, 못 맞추면 이유를 남긴다."""
    if station["station_id"] in ov:
        live_name, subway_id, statn_id, note = ov[station["station_id"]]
        match = "override" if live_name else "no_live_station"
    elif station["line_group"] not in sid_of:
        subway_id = statn_id = live_name = ""
        match, note = "no_live_line", "노선군 자체가 실시간 역정보에 없다"
    else:
        hit = auto_hit(station, sid_of, by_key)
        if hit is None:
            subway_id = statn_id = live_name = ""
            match, note = "no_live_station", "실시간 역정보에 같은 이름이 없다"
        else:
            live, alias = hit
            subway_id, statn_id, live_name = live["SUBWAY_ID"], live["STATN_ID"], live["STATN_NM"]
            match = "alias" if alias else "exact"
            note = f"별칭 {alias}" if alias else ""
    return [station["station_id"], station["name"], station["line_group"],
            subway_id, statn_id, live_name, match, note, data_date]


def build(live, stations, line_groups, overrides, data_date, no_live_groups=NO_LIVE_GROUPS):
    """→ (출력 DataFrame, 보고용 dict). 보정표가 낡았거나 실시간 원천이 바뀌면 ValueError."""
    sid_of = line_ids(live, line_groups, no_live_groups)
    by_key = live_index(live)
    ov = override_map(overrides, stations, sid_of, by_key)
    rows = [match_row(s, sid_of, by_key, ov, data_date) for s in stations]
    applied = sum(1 for s in stations if s["station_id"] in ov)
    if applied != len(overrides):
        raise ValueError(f"보정표 {len(overrides)}행 중 {applied}행만 출력에 반영됐습니다")

    out = pd.DataFrame(rows, columns=COLUMNS)
    matches = Counter(out["match"])
    queryable = out[out["live_name"] != ""]
    renamed = int((queryable["name"] != queryable["live_name"]).sum())
    steps = [("실시간 역정보 행", len(live)), ("우리 역 DB 행", len(stations)),
             ("보정표 행 (ref/subway_live_names.csv)", len(overrides)),
             ("정확 일치 (exact)", matches["exact"]), ("별칭 일치 (alias)", matches["alias"]),
             ("보정표로 이음 (override)", matches["override"]),
             ("실시간에 그 역이 없음 (no_live_station)", matches["no_live_station"]),
             ("실시간에 그 노선군이 없음 (no_live_line)", matches["no_live_line"]),
             ("실시간 조회 가능 행", len(queryable)),
             ("유일 live_name (한 번에 묶을 수 있는 호출 수)", queryable["live_name"].nunique()),
             ("우리 이름과 실시간 이름의 표기가 다른 행", renamed)]

    used = set(queryable["live_statn_id"])
    left = defaultdict(list)
    for r in live:
        if r["STATN_ID"] not in used:
            left[r["호선이름"]].append(r["STATN_NM"])
    steps.append(("실시간 역정보에만 있는 역", sum(len(v) for v in left.values())))

    our_n, live_n = Counter(s["line_group"] for s in stations), Counter(r["호선이름"] for r in live)
    lines = [(g, sid_of.get(g, "-"), our_n[g], live_n[g]) for g in sorted(our_n)]
    dup = queryable[queryable.duplicated("live_statn_id", keep=False)]
    dups = [(sid, g["live_name"].iloc[0], " · ".join(g["station_id"]), " · ".join(g["line_group"]))
            for sid, g in dup.groupby("live_statn_id")]
    info = {
        "steps": steps,
        "renamed": renamed,
        "lines": lines,
        "no_live": [(g, our_n[g]) for g in sorted(no_live_groups)],
        "overrides": [(r["station_id"], r["our_name"], r["live_name"] or "(실시간 미제공)",
                       r["live_statn_id"], r["verified"], r["note"]) for r in overrides],
        "missing": [(r[0], r[1], r[2], r[7]) for r in rows if r[6] == "no_live_station"],
        "leftover": [(g, len(v), " · ".join(v)) for g, v in sorted(left.items())],
        "dups": dups,
    }
    return out, info


def main(argv=None):
    ap = argparse.ArgumentParser(description="실시간 지하철 도착 조회용 역명 매핑표")
    ap.add_argument("--live", type=Path, default=LIVE_XLSX, help="실시간도착 역정보 XLSX")
    ap.add_argument("--stations", type=Path, default=ROOT / "data" / "processed" / "subway_stations.csv")
    ap.add_argument("--line-groups", type=Path, default=ROOT / "data" / "ref" / "line_groups.csv")
    ap.add_argument("--live-names", type=Path, default=ROOT / "data" / "ref" / "subway_live_names.csv",
                    help="개명·방향 표기로 이름이 다른 역 ↔ 실시간 이름 보정표")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "processed" / "subway_live_stations.csv")
    ap.add_argument("--report", type=Path, default=ROOT / "data" / "reports" / "subway_live_map.md")
    args = ap.parse_args(argv)

    read = lambda p: list(csv.DictReader(open(p, encoding="utf-8-sig", newline="")))  # noqa: E731
    live = pd.read_excel(args.live, dtype=str).fillna("").to_dict("records")
    try:
        out, info = build(live, read(args.stations), read(args.line_groups), read(args.live_names),
                          file_date(args.live))
    except ValueError as e:
        sys.exit(f"실패: {e}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.out, index=False, encoding="utf-8-sig")

    table = md_table(info["steps"])
    print(table)
    inputs = [file_line(p) for p in (args.live, args.stations, args.line_groups, args.live_names)]
    report = "\n".join([
        "# 실시간 지하철 역명 매핑표 (build_subway_live_map.py)",
        "",
        f"- 실행 시각: {datetime.now(KST):%Y-%m-%d %H:%M:%S} KST",
        *[f"- 입력: {s}" for s in inputs],
        f"- 출력: {file_line(args.out)}",
        "- 원천: 서울 열린데이터광장 실시간 지하철 도착 API 의 역정보 파일(SUBWAY_ID·STATN_ID·STATN_NM·호선이름)",
        "",
        "## 단계별 건수",
        "",
        table,
        "",
        "- `live_name` 은 **실시간 원문 이름**이다 — 도착 조회는 역명 정확 일치만 받으므로 우리 표시 이름으로 "
        f"물으면 조용히 0 건이 온다(표기가 다른 행 {info['renamed']}개).",
        "- 같은 `live_name` 은 한 호출로 묶인다(환승역은 한 응답에 여러 노선 행이 섞여 온다) — 응답은 "
        "`live_statn_id` 로 걸러 쓴다.",
        "",
        "## 노선군 → SUBWAY_ID",
        "",
        md_table(info["lines"], ("노선군", "SUBWAY_ID", "우리 역", "실시간 역")),
        "",
        "- 파일의 `호선이름` 문자열이 우리 `line_group` 과 같아 노선군 참조표가 따로 필요 없다.",
        "",
        "## 실시간이 없는 노선군 (UI 에서 '실시간 미제공')",
        "",
        md_table(info["no_live"], ("노선군", "우리 역")) if info["no_live"] else "없음",
        "",
        "- 역정보 파일에 SUBWAY_ID 자체가 없다 — 재조회·재시도 대상이 아니다.",
        "",
        "## 보정표로 이은 역 (ref/subway_live_names.csv)",
        "",
        md_table(info["overrides"], ("station_id", "우리 이름", "실시간 이름", "live_statn_id", "실호출 확인", "비고")),
        "",
        "- 보정표를 자동 매칭보다 **먼저** 적용한다 — 원천에 살아 있는 옛 이름(`신길온천`)이 엉뚱한 행에 붙지 않게.",
        "- 대상 역이 역 DB 에 없거나 이름이 다르거나 이미 자동으로 맞으면 스크립트가 멈춘다(원천이 바뀐 신호).",
        "",
        "## 실시간에 그 역이 없는 우리 역",
        "",
        md_table(info["missing"], ("station_id", "역", "노선군", "이유")) if info["missing"] else "없음",
        "",
        "- 진접선 3역은 열차 목적지(`진접행`)로는 나오지만 역별 실시간 조회가 빠져 있다. 목적지 문자열을 역 "
        "조회 키로 다시 쓰면 0 건을 맞는다.",
        "",
        "## 같은 실시간 역에 붙은 우리 역 두 행",
        "",
        md_table(info["dups"], ("live_statn_id", "실시간 이름", "우리 station_id", "노선군")) if info["dups"] else "없음",
        "",
        "- 같은 역을 운영기관별 두 행으로 담은 우리 역 DB 쪽 중복이다(이 단계에서 손대지 않았다). 조회할 때 "
        "`live_name` 단위로 묶지 않으면 쿼터를 두 번 쓴다.",
        "",
        "## 실시간 역정보에만 있는 역 (우리 역 DB 밖)",
        "",
        md_table(info["leftover"], ("노선군", "역 수", "역")) if info["leftover"] else "없음",
        "",
        "- 대부분 대상 지역 밖(인천·충남·강원)이거나 우리 역 DB 의 커버리지 경계다. 서울·경기인데 우리 역 DB 에 "
        "없는 것(2호선 까치산, 경춘선 청량리·회기, 수인분당선 청량리)은 역 DB 빌드 쪽에서 판단할 일이다.",
        "",
        "## 한계",
        "",
        "- 이 표는 역 식별 정보만 담는다 — 도착 응답은 저장하지 않는다.",
        "- 실시간 API 는 결과가 없을 때 `errorMessage` 없이 빈 `realtimeArrivalList` 를 200 으로 준다. "
        "GTX-A 는 9역이 다 매핑되는데도 응답이 빌 수 있다(운행시간·제공 여부) — 장애로 취급하지 않는다.",
        "- 환승역 응답은 페이지 크기에 잘린다(서울역 total=22). `/0/60/` 처럼 넉넉히 요청하고 `total` 과 실제 "
        "건수를 비교해 잘림을 로그로 남긴다.",
        "- 보정 6행 중 `verified=no` 3행(능길역 2행·세종대왕릉역)은 역정보 파일 근거만이다 — 1콜로 확인할 일이 남았다.",
        "",
    ])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(f"\n보고서: {rel(args.report)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
