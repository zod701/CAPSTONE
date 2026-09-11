"""도시철도 역 순서표 (노선정보 표준데이터 `정거장구성` → 노선별 역 순서, 서울·경기).

1) ref/subway_seq_lines.csv 에 적힌 노선정보 행만 쓴다 — 원천 노선번호가 틀리거나 겹치는 행이 있어(분당선에
   수인선 번호, 경부선·1호선 같은 번호) 노선번호 + 노선명으로 고르고, 역 DB 의 운영 노선(line_raw)에 잇는다
2) `정거장구성` 을 `,`·`+` 로 나눠 `역번호-역명` 으로 (따옴표·줄바꿈은 무시, 지선 번호 `211-1-용답` 을 받는다)
3) ref/subway_seq_fixes.csv 보정: 옛 역명·약칭(rename), 원천이 오래돼 빠진 역·잇는 역(insert_*),
   잘못 놓인 역(move_before), 목록에 섞인 지선(branch → 분기역부터 시작하는 별도 순서), 순환선(loop)
4) 역마다 역 DB(processed/subway_stations.csv) 행을 찾는다: 같은 운영 노선 → 같은 노선군 → 다른 노선의 같은 이름
   (이웃 역에서 GUARD_M 안 — 동명이역을 막는다). 못 찾으면 원천 역 파일 주소로 권역 밖(순서에서 뺀다) · 권역 안인데
   역 DB 에 없음(좌표 결함 — 이름만 남긴다)을 가르고, 그래도 없으면 실패
5) GTX-A 는 노선정보에 없어 ref/gtxa_stations.csv 로 만든다 — 북부(운정중앙–서울역)·남부(수서–동탄) 두 순서
   (서울역–수서가 아직 이어지지 않았다)

출력 한 행 = 순서 안의 역 하나. 같은 물리 역(이름 키 같고 300 m 안)은 phys_id 가 같아, 순서끼리는 phys_id 로 이어진다.
"""
import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

from clean_subway_stations import collapse_ws, station_clusters
from fetch_boundary import KST, ROOT, file_line, md_table, rel
from geoutil import haversine_m
from textnorm import name_key

SHEET = "표준데이터 노선(전체)"
RAW_SHEET = "표준데이터 역사"
GUARD_M = 8000.0      # 다른 노선의 같은 이름 역을 받아들이는 최대 거리 (가장 가까운 확정 이웃에서)
LONG_GAP_M = 4000.0   # 보고서에 올리는 긴 인접 구간
NEAR_M = 50.0         # 서로 다른 역인데 이보다 가까우면 좌표 의심
GTXA_LINE = "GTX-A"
GTXA_SEGMENTS = (("north", "북부"), ("south", "남부"))
EXPECTED_PARTS = {GTXA_LINE: (2, "서울역–수서 미연결")}  # 노선군이 원래 여러 덩어리인 경우
ACTIONS = ("rename", "insert_before", "insert_after", "move_before", "branch", "loop")
IN_SCOPE_ADDR = re.compile(r"^(서울|경기)")
COLUMNS = ["chain_id", "chain_name", "line_group", "line_raw", "loop", "seq", "station_id", "phys_id", "name",
           "lat", "lon", "status", "source", "src_no", "src_name"]

_SPLIT = re.compile(r"\s*[,+]\s*")
_TOKEN = re.compile(r"^\s*([^-\s]+(?:-\d+)?)\s*-\s*(.+?)\s*$")


def key(s):
    return name_key(s, "subway")


def norm_no(s):
    """역번호 비교용: `0405`=`405`, `D004`=`D04`, `211-1`=`2111`."""
    m = re.match(r"^([A-Za-z]*)0*(\d*)(.*)$", re.sub(r"[^0-9A-Za-z]", "", s or ""))
    return (m.group(1).upper() + m.group(2) + m.group(3)) if m else ""


def parse_composition(s):
    """`정거장구성` → ([(역번호, 역명)], [해석하지 못한 토큰])."""
    toks, bad = [], []
    for t in _SPLIT.split((s or "").replace('"', " ").strip()):
        if not t.strip():
            continue
        m = _TOKEN.match(t)
        if m:
            toks.append(m.groups())
        else:
            bad.append(t.strip())
    return toks, bad


def _tok(name, source, no="", src_name=""):
    return {"name": name, "src_no": no, "src_name": src_name, "source": source}


def _find(tokens, name):
    k = key(name)
    return [i for i, t in enumerate(tokens) if key(t["name"]) == k]


def apply_fixes(chains, fixes):
    """chains = {chain_id: {"meta": {...}, "tokens": [...]}} 를 파일 순서대로 고친다(제자리). 대상 역이 없는 보정은
    원천이 바뀐 신호이므로 ValueError. → 적용 기록 [(chain_id, action, target, value, branch)]"""
    log = []
    for f in fixes:
        cid, act, target = f["chain_id"], f["action"], f["target"]
        names = [v.strip() for v in f["value"].split("|") if v.strip()]
        if act not in ACTIONS:
            raise ValueError(f"알 수 없는 보정 {act!r} ({cid})")
        if cid not in chains:
            raise ValueError(f"보정의 chain_id 가 순서표에 없습니다: {cid}")
        ch = chains[cid]
        toks = ch["tokens"]
        if act == "loop":
            ch["meta"]["loop"] = 1
            log.append((cid, act, "", "", ""))
            continue
        pos = _find(toks, target)
        if not pos:
            raise ValueError(f"{cid} 보정 {act}: 대상 역 {target!r} 이 목록에 없습니다")
        if act != "rename" and not names:
            raise ValueError(f"{cid} 보정 {act}: value 가 비었습니다")
        if act == "rename":
            for i in pos:
                toks[i]["name"] = names[0]
                toks[i]["source"] = "renamed"
        elif act in ("insert_before", "insert_after"):
            at = pos[0] if act == "insert_before" else pos[0] + 1
            toks[at:at] = [_tok(n, "added") for n in names]
        elif act == "move_before":
            t = toks.pop(pos[0])
            dst = _find(toks, names[0])
            if not dst:
                raise ValueError(f"{cid} 보정 move_before: 기준 역 {names[0]!r} 이 목록에 없습니다")
            toks.insert(dst[0], {**t, "source": "moved"})
        else:  # branch — 목록에 있으면 떼어 오고, 없으면 새로 넣는다. 새 순서는 분기역부터
            bid = f"{cid}:{f['branch']}"
            if not f["branch"] or bid in chains:
                raise ValueError(f"{cid} 보정 branch: 지선 이름이 없거나 겹칩니다 ({bid})")
            moved = []
            for n in names:
                p = _find(toks, n)
                moved.append(toks.pop(p[0]) if p else _tok(n, "added"))
            junction = {**toks[_find(toks, target)[0]], "source": "junction"}
            meta = ch["meta"]
            chains[bid] = {"meta": {**meta, "chain_id": bid, "chain_name": f"{meta['chain_name']} · {f['branch']} 지선",
                                    "loop": 0}, "tokens": [junction, *moved]}
        log.append((cid, act, target, "|".join(names), f.get("branch", "")))
    return log


class Resolver:
    """순서 안의 역 이름 → 역 DB 행 (같은 운영 노선 → 같은 노선군 → 이웃 역 GUARD_M 안의 다른 노선)."""

    def __init__(self, db, raw):
        self.by_key = defaultdict(list)
        for r in db.to_dict("records"):
            self.by_key[key(r["name"])].append(r)
        self.raw_addr = defaultdict(list)
        for r in raw.to_dict("records"):
            self.raw_addr[key(r["역사명"])].append(r["역사도로명주소"])

    @staticmethod
    def _pick(rows, no, near):
        """여럿이면 역번호가 같은 행 → 가까운 이웃에 가까운 행 → station_id 순."""
        same = [r for r in rows if norm_no(r["station_id"].split("-", 1)[1]) == norm_no(no)]
        rows = same or rows
        if near is not None and len(rows) > 1:
            rows = sorted(rows, key=lambda r: haversine_m(near[0], near[1], r["lon"], r["lat"]))
        else:
            rows = sorted(rows, key=lambda r: r["station_id"])
        return rows[0]

    def resolve(self, meta, tokens):
        """→ (역 목록, 권역 밖으로 뺀 토큰 [(위치, 토큰)], 찾지 못한 토큰 [토큰]).
        역 = 토큰 + row(역 DB 행 또는 None) + status(db | missing) + how(line | group | other | missing)."""
        n = len(tokens)
        rows, how = [None] * n, [None] * n
        for i, t in enumerate(tokens):  # 1) 같은 운영 노선 → 같은 노선군
            cands = self.by_key.get(key(t["name"]), [])
            for scope, field in (("line", "line_raw"), ("group", "line_group")):
                hit = [r for r in cands if r[field] == meta[field]]
                if hit:
                    rows[i], how[i] = self._pick(hit, t["src_no"], None), scope
                    break

        def neighbor(i):
            for d in range(1, n):
                for j in (i - d, i + d):
                    if 0 <= j < n and rows[j] is not None:
                        return rows[j]["lon"], rows[j]["lat"]
            return None

        for i, t in enumerate(tokens):  # 2) 다른 노선의 같은 이름 (갈라지는 역) — 이웃에서 GUARD_M 안만
            if rows[i] is None:
                near = neighbor(i)
                cands = [r for r in self.by_key.get(key(t["name"]), [])
                         if near and haversine_m(near[0], near[1], r["lon"], r["lat"]) <= GUARD_M]
                if cands:
                    rows[i], how[i] = self._pick(cands, t["src_no"], near), "other"
        out, dropped, unresolved = [], [], []
        for i, t in enumerate(tokens):  # 3) 원천 역 파일 주소로 권역 밖 / 권역 안인데 DB 에 없음
            if rows[i] is not None:
                out.append({**t, "row": rows[i], "status": "db", "how": how[i], "pos": i})
                continue
            addrs = self.raw_addr.get(key(t["name"]), [])
            if any(IN_SCOPE_ADDR.match(a) for a in addrs):
                out.append({**t, "row": None, "status": "missing", "how": "missing", "pos": i})
            elif addrs:
                dropped.append((i, t))
            else:
                unresolved.append(t)
        return out, dropped, unresolved


def gtxa_chains(gtxa):
    """ref/gtxa_stations.csv → {구간: 순서 토큰} (구간마다 seq 순). 구간끼리는 잇지 않는다."""
    g = gtxa.assign(_q=pd.to_numeric(gtxa["seq"])).sort_values("_q")
    return {seg: [_tok(r["name"], "ref", r["station_no"], r["name"]) for r in g[g["segment"] == seg].to_dict("records")]
            for seg, _ in GTXA_SEGMENTS}


def select_lines(src, lines):
    """ref/subway_seq_lines.csv 행마다 노선정보 행 하나 (노선번호 + 공백 정리한 노선명). 없거나 여럿이면 ValueError."""
    src = src.assign(_name=src["노선명"].map(collapse_ws))
    picked = []
    for r in lines.to_dict("records"):
        m = src[(src["노선번호"] == r["src_line_no"]) & (src["_name"] == r["src_line_name"])]
        if len(m) != 1:
            raise ValueError(f"노선정보에서 {r['src_line_no']} {r['src_line_name']!r} 행이 {len(m)}개입니다 (1개여야 함)")
        picked.append((r, m.iloc[0]))
    return picked


def build(src, lines, fixes, db, raw, line_map, gtxa=None):
    """→ (출력 DataFrame, 보고용 dict). 원천·참조표가 맞지 않으면 ValueError."""
    dup = lines["chain_id"][lines["chain_id"].duplicated()].tolist()
    if dup:
        raise ValueError(f"chain_id 가 겹칩니다: {dup}")
    lm = line_map.set_index("raw_line_name")
    chains, bad_tokens, seps = {}, [], Counter()
    for r, s in select_lines(src, lines):
        if r["line_raw"] not in lm.index:
            raise ValueError(f"노선 매핑(subway_line_map.csv)에 없는 line_raw: {r['line_raw']!r}")
        comp = s["정거장구성"]
        seps.update({"쉼표": comp.count(","), "더하기": comp.count("+"), "줄바꿈": comp.count("\n"),
                     "따옴표 행": int('"' in comp)})
        toks, bad = parse_composition(comp)
        bad_tokens += [(r["chain_id"], b) for b in bad]
        chains[r["chain_id"]] = {
            "meta": {"chain_id": r["chain_id"], "chain_name": collapse_ws(s["노선명"]), "line_raw": r["line_raw"],
                     "line_group": lm.at[r["line_raw"], "line_group"], "loop": 0,
                     "src_date": str(s["데이터기준일자"])[:10]},
            "tokens": [_tok(nm, "comp", no, nm) for no, nm in toks]}
    if bad_tokens:
        raise ValueError(f"정거장구성을 해석하지 못한 토큰: {bad_tokens[:10]}")
    n_src_tokens = sum(len(c["tokens"]) for c in chains.values())
    if gtxa is not None and len(gtxa):
        segs = gtxa_chains(gtxa)
        for seg, label in GTXA_SEGMENTS:
            if segs[seg]:
                cid = f"gtxa:{label}"
                chains[cid] = {"meta": {"chain_id": cid, "chain_name": f"GTX-A {label} (ref/gtxa_stations.csv)",
                                        "line_raw": GTXA_LINE, "line_group": lm.at[GTXA_LINE, "line_group"], "loop": 0,
                                        "src_date": ""}, "tokens": segs[seg]}
    log = apply_fixes(chains, fixes.to_dict("records"))

    db = db.assign(lat=pd.to_numeric(db["lat"]), lon=pd.to_numeric(db["lon"]))
    clusters = station_clusters(db["name"].map(key).tolist(), db["lon"].tolist(), db["lat"].tolist())
    rep = {}
    for sid, c in zip(db["station_id"], clusters):
        rep[c] = min(rep.get(c, sid), sid)
    phys = {sid: rep[c] for sid, c in zip(db["station_id"], clusters)}

    resolver = Resolver(db, raw)
    rows, dropped, unresolved, interior, how = [], {}, [], [], Counter()
    for cid, ch in chains.items():
        meta = ch["meta"]
        entries, drop, unres = resolver.resolve(meta, ch["tokens"])
        unresolved += [(cid, t["name"]) for t in unres]
        dropped[cid] = [t["name"] for _, t in drop]
        kept = [e["pos"] for e in entries]
        interior += [(cid, t["name"]) for i, t in drop if kept and kept[0] < i < kept[-1]]
        for seq, e in enumerate(entries, 1):
            r = e["row"]
            how[e["how"]] += 1
            rows.append({**{k: meta[k] for k in ("chain_id", "chain_name", "line_group", "line_raw", "loop")},
                         "seq": seq, "station_id": r["station_id"] if r else "",
                         "phys_id": phys[r["station_id"]] if r else f"missing:{key(e['name'])}",
                         "name": r["name"] if r else e["name"], "lat": r["lat"] if r else None,
                         "lon": r["lon"] if r else None, "status": e["status"], "source": e["source"],
                         "src_no": e["src_no"], "src_name": e["src_name"]})
    if unresolved:
        raise ValueError("역 DB 에도 원천 역 파일에도 없는 이름 (보정표에 rename 이 필요): "
                         + ", ".join(f"{c} {n}" for c, n in unresolved))
    out = pd.DataFrame(rows, columns=COLUMNS)
    info = {"chains": chains, "log": log, "dropped": dropped, "interior": interior, "how": how, "seps": seps,
            "n_src_rows": len(src), "n_used_rows": len(lines), "n_src_tokens": n_src_tokens,
            "uncovered": db[~db["station_id"].isin(set(out["station_id"]))]}
    return out, info


def edges(out):
    """순서표 → 인접 역 쌍 [(chain_id, 앞 행, 뒤 행)] (순환선은 끝 → 처음도)."""
    pairs = []
    for cid, g in out.groupby("chain_id", sort=False):
        rs = g.sort_values("seq").to_dict("records")
        pairs += [(cid, a, b) for a, b in zip(rs, rs[1:])]
        if rs and rs[0]["loop"] == 1 and len(rs) > 2:
            pairs.append((cid, rs[-1], rs[0]))
    return pairs


def components(out):
    """노선군마다 phys_id 연결 요소 수 (순서끼리 같은 물리 역으로 이어져야 한 덩어리)."""
    res = {}
    by_group = defaultdict(list)
    for cid, a, b in edges(out):
        by_group[a["line_group"]].append((a["phys_id"], b["phys_id"]))
    for g, sub in out.groupby("line_group"):
        parent = {p: p for p in sub["phys_id"]}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a, b in by_group[g]:
            parent[find(a)] = find(b)
        roots = defaultdict(list)
        for p in parent:
            roots[find(p)].append(p)
        res[g] = sorted(roots.values(), key=len, reverse=True)
    return res


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="도시철도 역 순서표 (서울·경기)")
    ap.add_argument("--src", type=Path, default=ROOT / "data" / "csv" / "전체_도시철도노선정보_20260630.xlsx")
    ap.add_argument("--raw-stations", type=Path, default=ROOT / "data" / "csv" / "전체_도시철도역사정보_20260630.xlsx",
                    help="원천 역 파일 — 역 DB 에 없는 이름이 권역 밖인지 가르는 데만 쓴다 (주소)")
    ap.add_argument("--stations", type=Path, default=ROOT / "data" / "processed" / "subway_stations.csv")
    ap.add_argument("--lines", type=Path, default=ROOT / "data" / "ref" / "subway_seq_lines.csv")
    ap.add_argument("--fixes", type=Path, default=ROOT / "data" / "ref" / "subway_seq_fixes.csv")
    ap.add_argument("--line-map", type=Path, default=ROOT / "data" / "ref" / "subway_line_map.csv")
    ap.add_argument("--gtxa", type=Path, default=ROOT / "data" / "ref" / "gtxa_stations.csv")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "processed" / "subway_line_seq.csv")
    ap.add_argument("--report", type=Path, default=ROOT / "data" / "reports" / "subway_line_seq.md")
    args = ap.parse_args(argv)

    paths = (args.src, args.raw_stations, args.stations, args.lines, args.fixes, args.line_map, args.gtxa)
    inputs = [file_line(p) for p in paths if p.exists()]
    read = lambda p: pd.read_csv(p, dtype=str, encoding="utf-8-sig").fillna("")  # noqa: E731
    src = pd.read_excel(args.src, sheet_name=SHEET, dtype=str).fillna("")
    raw = pd.read_excel(args.raw_stations, sheet_name=RAW_SHEET, dtype=str).fillna("")
    gtxa = read(args.gtxa) if args.gtxa.exists() else None
    try:
        out, info = build(src, read(args.lines), read(args.fixes), read(args.stations), raw, read(args.line_map), gtxa)
    except ValueError as e:
        sys.exit(f"실패: {e}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.out, index=False, encoding="utf-8-sig", float_format="%.7f")

    how, seps, chains = info["how"], info["seps"], info["chains"]
    n_drop = sum(len(v) for v in info["dropped"].values())
    steps = [("노선정보 행", info["n_src_rows"]), ("쓴 행 (ref/subway_seq_lines.csv)", info["n_used_rows"]),
             ("쓴 행의 정거장구성 토큰", info["n_src_tokens"]),
             *[(f"구분자 {k}", v) for k, v in seps.items()],
             ("보정 적용 (ref/subway_seq_fixes.csv)", len(info["log"])),
             *[(f"보정 {a}", n) for a, n in Counter(x[1] for x in info["log"]).most_common()],
             ("권역 밖이라 뺀 역", n_drop),
             ("역 DB 행 찾음 — 같은 운영 노선", how["line"]), ("역 DB 행 찾음 — 같은 노선군", how["group"]),
             (f"역 DB 행 찾음 — 다른 노선의 같은 역 (이웃 {GUARD_M / 1000:.0f} km 안)", how["other"]),
             ("권역 안인데 역 DB 에 없음 (이름만)", how["missing"]),
             ("순서 (chain)", out["chain_id"].nunique()), ("출력 행", len(out)),
             ("역 DB 행 중 어느 순서에도 없는 것", len(info["uncovered"]))]
    table = md_table(steps)
    print(table)

    chain_rows = []
    for cid, g in out.groupby("chain_id", sort=False):
        meta = chains[cid]["meta"]
        pts = g.sort_values("seq")
        gaps = [haversine_m(a["lon"], a["lat"], b["lon"], b["lat"])
                for a, b in zip(pts.to_dict("records"), pts.to_dict("records")[1:]) if a["status"] == b["status"] == "db"]
        src_n = Counter(g["source"])
        chain_rows.append((cid, meta["line_group"], meta["line_raw"], len(g), src_n["comp"] + src_n["renamed"] + src_n["moved"],
                           src_n["added"], src_n["junction"], len(info["dropped"].get(cid, [])),
                           int((g["status"] == "missing").sum()), "순환" if meta["loop"] else "",
                           f"{max(gaps) / 1000:.1f}" if gaps else "-", meta["src_date"] or "-"))
    fix_rows = []
    for cid, act, target, value, branch in info["log"]:  # 보정마다 실제로 붙은 역 DB 행 (검토용)
        if act == "branch":
            sub = out[out["chain_id"] == f"{cid}:{branch}"]
        else:
            want = {key(target)} if act == "move_before" else {key(v) for v in value.split("|") if v}
            sub = out[(out["chain_id"] == cid) & (out["source"] != "comp") & out["name"].map(lambda n: key(n) in want)]
        ids = ", ".join(dict.fromkeys(f"{r.name}={r.station_id or '없음'}" for r in sub.itertuples()))
        fix_rows.append((cid, act, target or "-", value or "-", branch or "-", ids or "-"))
    long_rows, near_rows = [], []
    for cid, a, b in edges(out):
        if a["status"] == b["status"] == "db" and a["phys_id"] != b["phys_id"]:
            d = haversine_m(a["lon"], a["lat"], b["lon"], b["lat"])
            if d > LONG_GAP_M:
                long_rows.append((cid, a["name"], b["name"], f"{d / 1000:.1f}"))
            elif d < NEAR_M:
                near_rows.append((cid, f"{a['station_id']} {a['name']}", f"{b['station_id']} {b['name']}", f"{d:.0f}"))
    missing_rows = [(r["chain_id"], r["seq"], r["name"], r["src_no"]) for _, r in out[out["status"] == "missing"].iterrows()]
    drop_rows = [(cid, len(v), ", ".join(v)) for cid, v in info["dropped"].items() if v]
    comp_rows = []
    for g, cs in components(out).items():
        want, why = EXPECTED_PARTS.get(g, (1, ""))
        comp_rows.append((g, len(cs), want, "" if len(cs) == want else "**확인**", why or "-",
                          " / ".join(f"{len(c)}역" for c in cs)))
    uncovered = [(r["station_id"], r["name"], r["line_raw"]) for _, r in info["uncovered"].iterrows()]
    table_or_none = lambda rows, headers: md_table(rows, headers) if rows else "없음"  # noqa: E731
    report = "\n".join([
        "# 도시철도 역 순서표 (build_subway_seq.py)",
        "",
        f"- 실행 시각: {datetime.now(KST):%Y-%m-%d %H:%M:%S} KST",
        *[f"- 입력: {s}" for s in inputs],
        f"- 출력: {file_line(args.out)}",
        f"- 파라미터: 노선정보 시트 `{SHEET}`, 다른 노선의 같은 역 허용 {GUARD_M:,.0f} m, 긴 구간 > {LONG_GAP_M:,.0f} m, "
        f"좌표 의심 < {NEAR_M:.0f} m",
        "- 순서 한 줄 = 운영 노선(또는 지선) 하나. 같은 노선군의 순서끼리는 같은 물리 역(phys_id)으로 이어진다. "
        "`loop=1` 이면 끝 역 다음이 첫 역이다.",
        "",
        "## 단계별 건수",
        "",
        table,
        "",
        "## 순서별",
        "",
        md_table(chain_rows, ("chain_id", "노선군", "운영 노선", "역", "원천", "보충", "분기역", "권역 밖", "DB 없음", "순환",
                              "최대 인접 km", "원천 기준일")),
        "",
        "## 적용한 보정 (ref/subway_seq_fixes.csv)",
        "",
        md_table(fix_rows, ("chain_id", "action", "대상", "값", "지선", "찾은 역")),
        "",
        "## 노선군 연결 — 순서들이 한 덩어리로 이어지는가",
        "",
        md_table(comp_rows, ("노선군", "연결 요소", "기대", "", "이유", "요소별 역 수")),
        "",
        f"## 긴 인접 구간 (> {LONG_GAP_M / 1000:.0f} km) — 실제 역 간격인지 확인",
        "",
        table_or_none(long_rows, ("chain_id", "역", "다음 역", "km")),
        "",
        f"## 좌표 의심 — 인접한 다른 역이 {NEAR_M:.0f} m 안 (역 DB 좌표 결함)",
        "",
        table_or_none(near_rows, ("chain_id", "역", "다음 역", "m")),
        "",
        "## 권역 안인데 역 DB 에 없는 역 (이름만 남김)",
        "",
        table_or_none(missing_rows, ("chain_id", "순번", "역", "원천 역번호")),
        "",
        "## 권역 밖이라 뺀 역",
        "",
        table_or_none(drop_rows, ("chain_id", "역 수", "역")),
        "",
        "## 목록 중간에서 뺀 권역 밖 역 (있으면 앞뒤 역이 잘못 붙는다)",
        "",
        table_or_none(info["interior"], ("chain_id", "역")),
        "",
        "## 역 DB 행 중 어느 순서에도 없는 것",
        "",
        table_or_none(uncovered, ("station_id", "name", "line_raw")),
        "",
    ])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(f"출력: {rel(args.out)}")
    print(f"보고서: {rel(args.report)}")


if __name__ == "__main__":
    main()
