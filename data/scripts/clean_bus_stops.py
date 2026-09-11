"""버스 정류소 정제 (국토교통부 전국 버스정류장 CSV + 두 BIS 최신 정류소 파일 → 서울·경기 정류소).

0) 전국 원본(연 1회 갱신)의 GGB·SEB 레코드를 두 BIS 의 최신 정류소 파일로 바꾼다 — refresh() 참고
1) 관리기관 접두 GGB(경기BIS)·SEB(서울BIS)만 쓴다. 타 BIS 는 suffix 체계가 달라 병합할 수 없어
   대상 지역 안의 건수만 센다.
2) 좌표 → RegionIndex 로 시군 판정 (도시명은 믿지 않는다 — SEB 는 소재지와 무관하게 전부 서울특별시)
3) 정류장번호 뒤 9자리(suffix)가 같은 GGB·SEB 쌍 = 한 정류소의 이중 등록. 판정(먼저 맞는 규칙):
   override → ≤ 50 m 병합(이름 무관) → 50–300 m 이고 이름 호환이면 병합 → 그 외 분리
4) 대표 = 소재지 주인(GGB 판정이 서울이면 SEB, 아니면 GGB). 좌표·지역·도시명은 대표 것
5) contain/snap 행만 남기고, 제외 레코드·50 m 초과 쌍은 검토용 CSV 로 보고한다
"""
import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

from build_boundaries import ATTRIBUTION
from fetch_boundary import KST, ROOT, file_line, md_table, rel
from fetch_gbis import latest, read_gbis
from geoutil import GridIndex, haversine_m
from regions import RegionIndex
from textnorm import is_virtual, name_key, name_norm

MERGE_NEAR_M = 50.0        # 이하면 이름과 무관하게 병합
MERGE_FAR_MAX_M = 300.0    # 이하이고 이름이 호환되면 병합
SNAP_MAX_M = 2000.0
UPDOWN_M = (15.0, 60.0)    # 상/하행 동명 쌍 거리 범위
RESIDUAL_DUP_M = 5.0       # 병합 후 이보다 가까운 동명 쌍은 검토 대상
PREFIXES = ("GGB", "SEB")
IN_SCOPE = ("contain", "snap")
REGION = ("sido_cd", "sido_nm", "sgg_cd", "sgg_nm", "region_method", "snap_dist_m")
COLUMNS = ["stop_key", "name", "name_norm", "name_key", "aliases", "lat", "lon", "sido_cd", "sido_nm",
           "sgg_cd", "sgg_nm", "region_method", "snap_dist_m", "source_ids", "rep_source", "ars_nos",
           "merge_status", "twin_dist_m", "is_virtual", "raw_city_label"]
DROPPED_COLUMNS = ["정류장번호", "정류장명", "위도", "경도", "도시명", "reason", "detail"]
REVIEW_COLUMNS = ["stop_key", "ggb_id", "seb_id", "ggb_name", "seb_name", "dist_m", "ggb_ars", "seb_ars",
                  "sgg_nm", "names_compatible", "decision"]
DECISIONS = ("merged_near", "merged_far", "split", "override_merge", "override_split")
FERRY_TYPE = "한강선착장"   # 서울 정류소 파일에 버스 정류소처럼 들어 있는 한강 배 선착장
MOVED_M = 50.0              # 교체 보고: 최신 파일 좌표가 전국 원본에서 이보다 멀면 "옮김"
_SUFFIX = re.compile(r"^\d{9}$")


def ars_no(s):
    """모바일단축번호 → 5자리 (앞자리 0 복원: 4282 → 04282). 0·빈 값은 ""."""
    s = (s or "").strip()
    return s.zfill(5) if s.isdigit() and int(s) else ""


def compatible(a, b):
    """두 이름의 bus 키가 같거나, 한쪽이 다른 쪽의 접두(짧은 쪽 ≥ 3자)면 호환."""
    ka, kb = name_key(a, "bus"), name_key(b, "bus")
    return ka == kb or (min(len(ka), len(kb)) >= 3 and (ka.startswith(kb) or kb.startswith(ka)))


def decide(dist_m, ggb_name, seb_name, action=""):
    """쌍 판정 — override → ≤ 50 m → 50–300 m 이름 호환 → 분리."""
    if action:
        return f"override_{action}"
    if dist_m <= MERGE_NEAR_M:
        return "merged_near"
    if dist_m <= MERGE_FAR_MAX_M and compatible(ggb_name, seb_name):
        return "merged_far"
    return "split"


def representative(ggb_sido, seb_sido, keep=""):
    """대표 접두. override keep → 소재지 주인: GGB 판정 시도(없으면 SEB 판정 시도)가 서울이면 SEB, 아니면 GGB.
    고른 쪽이 대상 밖이고 짝은 대상 안이면 짝을 대표로 한다 (경계에 걸친 쌍을 통째로 잃지 않게)."""
    if keep:
        return keep
    rep = "SEB" if (ggb_sido or seb_sido) == "11" else "GGB"
    sido = {"GGB": ggb_sido, "SEB": seb_sido}
    twin = "GGB" if rep == "SEB" else "SEB"
    return twin if not sido[rep] and sido[twin] else rep


def parse_overrides(df):
    """bus_pair_overrides.csv → {stop_key: (action, keep)}. 값이 틀리면 ValueError."""
    out = {}
    for r in df.to_dict("records"):
        k, action, keep = r["stop_key"].strip(), r["action"].strip(), r["keep"].strip()
        if action not in ("merge", "split") or keep not in ("", *PREFIXES):
            raise ValueError(f"override 값이 잘못됐습니다 (action=merge|split, keep=GGB|SEB|빈칸): {r}")
        if k in out:
            raise ValueError(f"override stop_key 가 중복됩니다: {k}")
        out[k] = (action, keep)
    return out


def _row(stop_key, r, status, twin_dist_m=None):
    """레코드 하나 → 출력 행 (단독·분리, 병합 행의 바탕)."""
    return {"stop_key": stop_key, "name": r["정류장명"], "aliases": "", "lat": r["lat"], "lon": r["lon"],
            **{k: r[k] for k in REGION}, "source_ids": r["정류장번호"], "rep_source": r["prefix"],
            "ars_nos": ars_no(r["모바일단축번호"]), "merge_status": status, "twin_dist_m": twin_dist_m,
            "raw_city_label": r["도시명"]}


def merged_row(stop_key, rep, twin, status, dist_m):
    """쌍 → 한 행. 이름은 대표 것, 단 대표 키가 짝 키의 엄격한 접두(절단)면 짝 것. 다른 이름은 aliases."""
    rk, tk = name_key(rep["정류장명"], "bus"), name_key(twin["정류장명"], "bus")
    named, other = (twin, rep) if tk != rk and tk.startswith(rk) else (rep, twin)
    ggb, seb = (rep, twin) if rep["prefix"] == "GGB" else (twin, rep)
    row = _row(stop_key, rep, status, dist_m)
    row["name"] = named["정류장명"]
    row["aliases"] = other["정류장명"] if other["정류장명"] != named["정류장명"] else ""
    row["source_ids"] = f"{ggb['정류장번호']}|{seb['정류장번호']}"
    row["ars_nos"] = "|".join(dict.fromkeys(a for a in (ars_no(rep["모바일단축번호"]),
                                                        ars_no(twin["모바일단축번호"])) if a))
    return row


def same_key_pairs(keys, lons, lats, r_m):
    """같은 키이면서 r_m 이내인 행 쌍 [(i, j, d)] (i < j)."""
    grid = GridIndex(lons, lats)
    return [(i, j, d) for i in range(len(keys)) for j, d in grid.near(lons[i], lats[i], r_m)
            if j > i and keys[j] == keys[i]]


def updown_pairs(pairs, suffixes):
    """상/하행 지표: 같은 name_key, 다른 suffix, UPDOWN_M 범위 안의 쌍 → (행 쌍 수, suffix 쌍 집합).
    병합 전에는 이중 등록된 두 정류소 사이가 최대 4 행 쌍으로 세어지므로 suffix 쌍 수가 병합 후와 비교할 값이다."""
    lo, hi = UPDOWN_M
    hits = [frozenset((suffixes[i], suffixes[j])) for i, j, d in pairs
            if lo <= d <= hi and suffixes[i] != suffixes[j]]
    return len(hits), set(hits)


def _fmt(v, nd):
    return "" if v is None or pd.isna(v) else f"{v:.{nd}f}"


def _cell(s):
    """markdown 표 칸 — source_ids 의 | 가 칸을 가르지 않게."""
    return str(s).replace("|", "\\|")


def refresh(raw, seoul, gbis, assign, coord_overrides=None):
    """전국 원본의 GGB·SEB 레코드를 두 BIS 의 최신 정류소 파일로 바꾼다 → (전국 원본 꼴 DataFrame, [(단계, 건수)],
    바꾼 좌표 [(정류장번호, 이름, 최신 파일 좌표, 보정 좌표, 이동 m)]).

    - GGB = GBIS 정류소 파일 전부. 전국 원본에만 있는 GGB 는 GBIS 에서 빠진 것이라 폐지로 보고 뺀다
    - SEB = 서울 정류소 파일(배 선착장 제외). 이 파일은 서울 소재 정류소만 담는다 — 전국 원본에만 있는 SEB 는
      서울 소재면 폐지로 보고 빼고, 서울 밖(서울 BIS 가 등록한 경기 소재 정류소)이면 전국 원본 것을 그대로 둔다
    - coord_overrides(ref/bus_coord_overrides.csv): 최신 파일 좌표가 틀린 레코드의 좌표를 참조표 값으로 바꾼다.
      대상 레코드가 없거나 이름(name_key)이 다르면 원천이 바뀐 신호이므로 ValueError
    - 도시명은 전국 원본 것을 잇는다(새 정류소는 빈칸) — 원 도시명 교차표가 계속 전국 원본 표기를 뜻하게
    - 타 BIS 레코드는 그대로
    """
    raw = raw.reset_index(drop=True)
    prefix = raw["정류장번호"].str[:3]
    nat = {p: raw[prefix == p].set_index("정류장번호") for p in PREFIXES}
    label = dict(zip(raw["정류장번호"], raw["도시명"]))

    ferry = seoul["정류소타입"] == FERRY_TYPE
    s = seoul[~ferry]
    fresh = {"GGB": pd.DataFrame({"정류장번호": "GGB" + gbis["stationId"], "정류장명": gbis["stationName"],
                                  "위도": gbis["y"], "경도": gbis["x"], "모바일단축번호": gbis["mobileNo"]}),
             "SEB": pd.DataFrame({"정류장번호": "SEB" + s["NODE_ID"], "정류장명": s["정류소명"],
                                  "위도": s["Y좌표"], "경도": s["X좌표"], "모바일단축번호": s["ARS_ID"]})}
    for f in fresh.values():
        f["도시명"] = f["정류장번호"].map(label).fillna("")
    listed = {"GGB": set(fresh["GGB"]["정류장번호"]), "SEB": set("SEB" + seoul["NODE_ID"])}  # 배 선착장도 "목록에 있음"

    steps = [("SEB 서울 정류소 파일 중 배 선착장 — 뺌", int(ferry.sum()))]
    kept_old = []
    for p, file_label in (("GGB", "경기 GBIS 정류소 파일"), ("SEB", "서울 정류소 파일")):
        f, n = fresh[p], nat[p]
        both = f[f["정류장번호"].isin(n.index)]
        old = n.loc[both["정류장번호"]]
        moved = sum(haversine_m(float(a), float(b), float(c), float(d)) > MOVED_M
                    for a, b, c, d in zip(both["경도"], both["위도"], old["경도"], old["위도"]))
        renamed = int((both["정류장명"].map(lambda v: name_key(v, "bus")).values
                       != old["정류장명"].map(lambda v: name_key(v, "bus")).values).sum())
        gone = n[~n.index.isin(listed[p])].reset_index()
        steps += [(f"{p} {file_label} 행", len(f)), (f"{p} 전국 원본 행", len(n)), (f"{p} 둘 다 있음 → 최신 값", len(both)),
                  (f"{p}   그중 이름 키가 바뀜", renamed), (f"{p}   그중 {MOVED_M:.0f} m 넘게 옮김", moved),
                  (f"{p} 최신 파일에만 있음 (새 정류소)", len(f) - len(both))]
        if p == "GGB":
            steps.append(("GGB 전국 원본에만 → 폐지로 보고 뺌", len(gone)))
            continue
        where = assign(pd.to_numeric(gone["경도"], errors="coerce").tolist(),
                       pd.to_numeric(gone["위도"], errors="coerce").tolist())
        outside = pd.Series([r["sido_cd"] != "11" for r in where], index=gone.index, dtype=bool)
        kept_old.append(gone[outside])
        steps += [("SEB 전국 원본에만 · 서울 소재 → 폐지로 보고 뺌", int((~outside).sum())),
                  ("SEB 전국 원본에만 · 서울 밖 → 전국 원본 유지 (서울 파일이 담지 않는 범위)", int(outside.sum()))]
    out = pd.concat([raw[~prefix.isin(PREFIXES)], *fresh.values(), *kept_old], ignore_index=True)

    moved = []
    for r in coord_overrides.to_dict("records") if coord_overrides is not None else []:
        hit = out.index[out["정류장번호"] == r["source_id"]]
        if len(hit) != 1:
            raise ValueError(f"좌표 보정이 어느 레코드에도 맞지 않습니다: {r['source_id']}")
        i = hit[0]
        if name_key(out.at[i, "정류장명"], "bus") != name_key(r["name"], "bus"):
            raise ValueError(f"좌표 보정의 이름이 원천과 다릅니다: {r['source_id']} {r['name']!r} ≠ {out.at[i, '정류장명']!r}")
        d = haversine_m(float(out.at[i, "경도"]), float(out.at[i, "위도"]), float(r["lon"]), float(r["lat"]))
        moved.append((r["source_id"], out.at[i, "정류장명"], f"{out.at[i, '위도']}, {out.at[i, '경도']}",
                      f"{r['lat']}, {r['lon']}", d))
        out.at[i, "경도"], out.at[i, "위도"] = r["lon"], r["lat"]
    steps.append(("좌표 보정 (ref/bus_coord_overrides.csv)", len(moved)))
    return out[raw.columns].fillna(""), steps, moved


def clean(raw, assign, overrides=None):
    """전국 원본 DataFrame → (bus_stops DataFrame, 보고용 dict).
    assign(lons, lats) 는 RegionIndex.assign 과 같은 dict 목록을 돌려준다.
    suffix 형식·유일성 위반, 쌍이 아닌 override, stop_key 중복, 대상 레코드 누락은 ValueError."""
    overrides = overrides or {}
    df = raw.reset_index(drop=True)
    df = df.assign(prefix=df["정류장번호"].str[:3], lon=pd.to_numeric(df["경도"], errors="coerce"),
                   lat=pd.to_numeric(df["위도"], errors="coerce"))
    df = pd.concat([df, pd.DataFrame(assign(df["lon"].tolist(), df["lat"].tolist()), index=df.index)], axis=1)
    ours = df["prefix"].isin(PREFIXES)
    others = df[~ours & df["region_method"].isin(IN_SCOPE)]
    steps = [("입력 레코드 (GGB·SEB 는 최신 파일로 교체한 뒤)", len(df))]
    steps += [(f"{p} 레코드", int((df["prefix"] == p).sum())) for p in PREFIXES]
    steps += [("GGB+SEB 유지", int(ours.sum()))]
    steps += [(f"타 BIS 중 대상 지역 {m} — 제외", int((others["region_method"] == m).sum())) for m in IN_SCOPE]

    g = df[ours].reset_index(drop=True)
    g["suffix"] = g["정류장번호"].str[3:]
    bad = g.loc[~g["suffix"].str.match(_SUFFIX), "정류장번호"].tolist()
    if bad:
        raise ValueError(f"정류장번호 뒤 9자리가 숫자가 아닙니다: {bad[:10]}")
    dup = g.loc[g.duplicated(["prefix", "suffix"]), "정류장번호"].tolist()
    if dup:
        raise ValueError(f"접두 안에서 suffix 가 중복됩니다: {dup[:10]}")
    g["in_scope"] = g["region_method"].isin(IN_SCOPE)

    groups = defaultdict(dict)
    for r in g.to_dict("records"):
        groups[r["suffix"]][r["prefix"]] = r
    pair_keys = {s for s, m in groups.items() if len(m) == 2}
    unknown = sorted(set(overrides) - pair_keys)
    if unknown:
        raise ValueError(f"override stop_key 가 GGB·SEB 쌍이 아닙니다: {unknown}")

    rows, pairs = [], []
    for sfx, m in groups.items():
        live = [r for r in m.values() if r["drop_reason"] != "invalid_coord"]
        if len(m) == 1 or len(live) < 2:
            # 짝이 없거나 한쪽 좌표가 무효면 거리를 잴 수 없다 — 남은 쪽은 단독, 무효 쪽은 제외 목록으로
            for r in live or m.values():
                rows.append(_row(sfx, r, "single"))
            continue
        a, b = m["GGB"], m["SEB"]
        d = haversine_m(a["lon"], a["lat"], b["lon"], b["lat"])  # 반올림은 출력 때만 (50 m 경계 판정은 원값으로)
        action, keep = overrides.get(sfx, ("", ""))
        decision = decide(d, a["정류장명"], b["정류장명"], action)
        rep = m[representative(a["sido_cd"], b["sido_cd"], keep)]
        twin = b if rep is a else a
        if decision in ("split", "override_split"):
            rows += [_row(sfx, rep, "split", d), _row(f"{sfx}-{twin['prefix']}", twin, "split", d)]
        else:
            rows.append(merged_row(sfx, rep, twin, decision, d))
        pairs.append({"stop_key": sfx, "ggb": a, "seb": b, "rep": rep, "dist_m": d, "decision": decision,
                      "in_scope": a["in_scope"] or b["in_scope"], "override": (action, keep) if action else None})

    out = pd.DataFrame(rows)
    out = out[out["region_method"].isin(IN_SCOPE)].sort_values("stop_key").reset_index(drop=True)
    dup = sorted(set(out.loc[out["stop_key"].duplicated(), "stop_key"]))
    if dup:
        raise ValueError(f"stop_key 가 유일하지 않습니다: {dup[:10]}")
    # 제외 = 어느 출력 행의 source_ids 에도 없는 레코드 (대상 밖이어도 대상 안 짝과 병합되면 남는다)
    kept = set(out["source_ids"].str.split("|").explode())
    lost = sorted(set(g.loc[g["in_scope"], "정류장번호"]) - kept)
    if lost:  # override keep 이 대상 밖 레코드를 대표로 고른 경우
        raise ValueError(f"대상 지역 레코드가 출력에서 빠졌습니다 (override keep 확인): {lost[:10]}")
    dropped = g[~g["정류장번호"].isin(kept)]
    steps.append(("좌표 무효 (invalid_coord)", int((g["drop_reason"] == "invalid_coord").sum())))
    steps += [(f"지역 판정 {m}", int((g["region_method"] == m).sum())) for m in IN_SCOPE]
    steps += [("대상 지역 레코드 (contain+snap)", int(g["in_scope"].sum())),
              ("대상 밖이지만 대상 안 짝과 병합되어 남은 레코드", int((~g["in_scope"]).sum()) - len(dropped))]
    # nearest 의 거리는 행마다 달라 묶이지 않으므로 떼고 센다
    drop_n = Counter(re.sub(r" \d+ m$", "", d) for d in dropped["detail"])
    steps += [(f"제외 ({k})", n) for k, n in drop_n.most_common()]
    steps.append(("제외 합계 (reports/bus_dropped.csv)", len(dropped)))

    out["name_norm"] = out["name"].map(name_norm)
    out["name_key"] = out["name"].map(lambda s: name_key(s, "bus"))
    out["is_virtual"] = out["name"].map(is_virtual).astype(int)

    scoped = [p for p in pairs if p["in_scope"]]
    dec_n = Counter(p["decision"] for p in scoped)
    steps += [("GGB·SEB suffix 쌍 (전국)", len(pairs)),
              ("대상 쌍 (한쪽 이상 contain/snap)", len(scoped))]
    steps += [(f"쌍 판정 {k}", dec_n[k]) for k in DECISIONS]
    steps += [(f"최종 행 {s}", int((out["merge_status"] == s).sum()))
              for s in ("single", "merged_near", "merged_far", "override_merge", "split")]
    steps.append(("최종 행 (stop_key 유일)", len(out)))

    src = g[g["in_scope"]].reset_index(drop=True)
    src_keys = [name_key(s, "bus") for s in src["정류장명"]]
    before = same_key_pairs(src_keys, src["lon"].tolist(), src["lat"].tolist(), UPDOWN_M[1])
    after = same_key_pairs(out["name_key"].tolist(), out["lon"].tolist(), out["lat"].tolist(), UPDOWN_M[1])
    (n_before, sp_before), (n_after, sp_after) = (updown_pairs(before, src["suffix"].tolist()),
                                                  updown_pairs(after, out["stop_key"].str[:9].tolist()))
    residual = [(i, j, d) for i, j, d in after if d < RESIDUAL_DUP_M]
    seb_gg = int(((g["prefix"] == "SEB") & g["도시명"].str.startswith("서울특별시")
                  & (g["sido_cd"] == "41")).sum())
    up = f"상/하행 동명 쌍 {UPDOWN_M[0]:.0f}–{UPDOWN_M[1]:.0f} m"
    steps += [(f"{up} — 병합 전 행 쌍 (대상 레코드)", n_before),
              (f"{up} — 병합 전 suffix 쌍 (이중 등록 중복 제거)", len(sp_before)),
              (f"{up} — 병합 후 행 쌍 (최종 행)", n_after),
              (f"{up} — 병합 전 suffix 쌍 중 병합 후 빠진 쌍", len(sp_before - sp_after)),
              (f"{up} — 병합 후 새로 생긴 suffix 쌍", len(sp_after - sp_before)),
              (f"잔여 동명 쌍 < {RESIDUAL_DUP_M:.0f} m (병합 후, 검토용)", len(residual)),
              ("SEB·도시명 서울특별시인데 경기도 시군 판정", seb_gg),
              ("미정차 가상 노드 (is_virtual=1)", int(out["is_virtual"].sum()))]

    out["lat"] = out["lat"].map(lambda v: _fmt(v, 7))
    out["lon"] = out["lon"].map(lambda v: _fmt(v, 7))
    out["snap_dist_m"] = out["snap_dist_m"].map(lambda v: _fmt(v, 1))
    out["twin_dist_m"] = out["twin_dist_m"].map(lambda v: _fmt(v, 1))
    info = {"steps": steps, "others": others, "records": g, "dropped": dropped, "pairs": scoped,
            "residual": residual, "updown": (n_before, len(sp_before), n_after)}
    return out[COLUMNS], info


def review_rows(pairs):
    """50 m 초과 대상 쌍 → bus_pairs_review.csv 행 (먼 순)."""
    rows = []
    for p in sorted(pairs, key=lambda p: -p["dist_m"]):
        if p["dist_m"] <= MERGE_NEAR_M:
            continue
        a, b = p["ggb"], p["seb"]
        rows.append({"stop_key": p["stop_key"], "ggb_id": a["정류장번호"], "seb_id": b["정류장번호"],
                     "ggb_name": a["정류장명"], "seb_name": b["정류장명"], "dist_m": f"{p['dist_m']:.1f}",
                     "ggb_ars": ars_no(a["모바일단축번호"]), "seb_ars": ars_no(b["모바일단축번호"]),
                     "sgg_nm": p["rep"]["sgg_nm"], "names_compatible": int(compatible(a["정류장명"], b["정류장명"])),
                     "decision": p["decision"]})
    return pd.DataFrame(rows, columns=REVIEW_COLUMNS)


def crosstab_rows(records):
    """(접두, 원 도시명 시도) × 판정 시도 교차표."""
    cols = ("서울특별시", "경기도", "대상 밖")
    judged = records["sido_nm"].where(records["region_method"].isin(IN_SCOPE), "대상 밖")
    ct = Counter(zip(records["prefix"], records["도시명"].str.split().str[0], judged))
    keys = sorted({(p, s) for p, s, _ in ct}, key=lambda k: (k[0], -sum(ct[(*k, c)] for c in cols)))
    return [(p, s, *(ct[(p, s, c)] for c in cols)) for p, s in keys], ("접두", "원 도시명 시도", *cols)


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="버스 정류소 정제 (서울·경기, GGB·SEB 병합)")
    ap.add_argument("--src", type=Path, default=ROOT / "data" / "csv" / "국토교통부_전국 버스정류장 위치정보_20251031.csv")
    ap.add_argument("--seoul-stations", type=Path, default=ROOT / "data" / "csv" / "서울시버스정류소위치정보(20260902).xlsx")
    ap.add_argument("--gbis-stations", type=Path, default=None,
                    help="GBIS 정류소 파일 (기본: data/external/gbis/ 의 가장 최근 버전 — fetch_gbis.py 로 받는다)")
    ap.add_argument("--boundaries", type=Path, default=ROOT / "data" / "processed" / "admin_sgg.geojson")
    ap.add_argument("--overrides", type=Path, default=ROOT / "data" / "ref" / "bus_pair_overrides.csv")
    ap.add_argument("--coord-overrides", type=Path, default=ROOT / "data" / "ref" / "bus_coord_overrides.csv")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "processed" / "bus_stops.csv")
    ap.add_argument("--report", type=Path, default=ROOT / "data" / "reports" / "bus_stops.md")
    ap.add_argument("--review", type=Path, default=ROOT / "data" / "reports" / "bus_pairs_review.csv")
    ap.add_argument("--dropped", type=Path, default=ROOT / "data" / "reports" / "bus_dropped.csv")
    args = ap.parse_args(argv)
    try:
        args.gbis_stations = args.gbis_stations or latest("station")
    except FileNotFoundError as e:
        sys.exit(f"실패: {e}")

    inputs = [file_line(p) for p in (args.src, args.seoul_stations, args.gbis_stations, args.boundaries, args.overrides,
                                     args.coord_overrides)]
    read_ref = lambda p: pd.read_csv(p, dtype=str, encoding="utf-8-sig").fillna("")  # noqa: E731
    raw = pd.read_csv(args.src, encoding="cp949", dtype=str).fillna("")
    seoul = pd.read_excel(args.seoul_stations, dtype=str).fillna("")
    gbis = read_gbis(args.gbis_stations)
    regions = RegionIndex(args.boundaries, snap_max_m=SNAP_MAX_M)
    try:
        records, refresh_steps, moved = refresh(raw, seoul, gbis, regions.assign, read_ref(args.coord_overrides))
        overrides = parse_overrides(read_ref(args.overrides))
        out, info = clean(records, regions.assign, overrides)
    except ValueError as e:
        sys.exit(f"실패: {e}")
    print(md_table(refresh_steps))
    steps = info["steps"] + [("시군 경계 동률 (n_ties, 전국 레코드)", regions.n_ties)]

    dropped = info["dropped"].rename(columns={"drop_reason": "reason"})[DROPPED_COLUMNS]
    review = review_rows(info["pairs"])
    for path, df in ((args.out, out), (args.dropped, dropped), (args.review, review)):
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False, encoding="utf-8-sig")
    table = md_table(steps)
    print(table)

    other_rows = [(p, "·".join(dict.fromkeys(o["관리도시명"])),
                   *(int((o["region_method"] == m).sum()) for m in IN_SCOPE))
                  for p, o in sorted(info["others"].groupby("prefix"), key=lambda kv: -len(kv[1]))]
    in_label = info["dropped"][info["dropped"]["도시명"].str.match(r"^(서울특별시|경기도)")]
    label_rows = [(r["정류장번호"], r["정류장명"], r["도시명"], r["detail"]) for _, r in in_label.iterrows()]
    snapped = out[out["region_method"] == "snap"]
    snap_rows = [(r["stop_key"], r["name"], r["sgg_nm"], r["snap_dist_m"], _cell(r["source_ids"]),
                  f"{r['lat']}, {r['lon']}") for _, r in snapped.iterrows()]
    res_rows = [(out.at[i, "stop_key"], out.at[j, "stop_key"], out.at[i, "name"], out.at[j, "name"], f"{d:.1f}",
                 out.at[i, "sgg_nm"], _cell(out.at[i, "source_ids"]), _cell(out.at[j, "source_ids"]))
                for i, j, d in info["residual"]]
    ov_rows = [(p["stop_key"], *p["override"], p["decision"], p["rep"]["prefix"])
               for p in info["pairs"] if p["override"]]
    n_far = sum(1 for p in info["pairs"] if p["dist_m"] > MERGE_NEAR_M)
    ct_rows, ct_headers = crosstab_rows(info["records"])
    table_or_none = lambda rows, headers: md_table(rows, headers) if rows else "없음"
    report = "\n".join([
        "# 버스 정류소 정제 (clean_bus_stops.py)",
        "",
        f"- 실행 시각: {datetime.now(KST):%Y-%m-%d %H:%M:%S} KST",
        *[f"- 입력: {s}" for s in inputs],
        *[f"- 출력: {file_line(p)}" for p in (args.out, args.review, args.dropped)],
        f"- 파라미터: 근접 병합 ≤ {MERGE_NEAR_M:.0f} m (이름 무관), 원거리 병합 ≤ {MERGE_FAR_MAX_M:.0f} m (이름 호환), "
        f"snap 최대 {SNAP_MAX_M:,.0f} m, 상/하행 {UPDOWN_M[0]:.0f}–{UPDOWN_M[1]:.0f} m, 잔여 동명 < {RESIDUAL_DUP_M:.0f} m",
        "- 원천: 국토교통부 전국 버스정류장 위치정보 (data.go.kr 15067528) — GGB·SEB 레코드는 경기도 버스정보 기반정보 "
        "정류소 파일 (data.go.kr 15080658) · 서울 열린데이터광장 OA-15067 서울시 버스정류소 위치정보(공공누리 1유형)로 교체",
        f"- {ATTRIBUTION}",
        "",
        "## 최신 정류소 파일로 교체 (refresh)",
        "",
        "전국 원본은 연 1회 갱신이라 새로 생긴·없어진·바뀐 정류소를 놓친다. 두 BIS 의 최신 정류소 파일이 있는 레코드는 최신 값을 쓰고, "
        "최신 파일에서 빠진 레코드는 폐지로 본다. 서울 파일은 서울 소재 정류소만 담으므로, 서울 BIS 가 등록한 서울 밖 정류소는 "
        "전국 원본 것을 유지한다.",
        "",
        md_table(refresh_steps),
        "",
        "### 좌표 보정 (ref/bus_coord_overrides.csv) — 최신 파일 좌표가 틀린 레코드",
        "",
        table_or_none([(sid, nm, a, b, f"{d:,.0f}") for sid, nm, a, b, d in moved],
                      ("정류장번호", "이름", "최신 파일 좌표", "보정 좌표", "이동 m")),
        "",
        "## 단계별 건수",
        "",
        table,
        "",
        "- 제외의 `far`(후보 상자 안에 피처 없음)와 `nearest:<시도>`(가장 가까운 피처가 인접 시도이거나 snap 반경 밖)는 "
        "모두 대상 지역 밖 원거리 레코드다. 상세는 `reports/bus_dropped.csv`.",
        "- 상/하행 지표는 같은 name_key·다른 suffix·거리 범위 안의 쌍 수다. 병합 전 행 쌍은 이중 등록된 두 정류소 "
        "사이를 최대 4번(GGB–GGB, SEB–SEB, GGB–SEB, SEB–GGB) 세므로, 병합 후 행 쌍과 비교할 값은 병합 전 suffix 쌍이다. "
        "병합 후 빠진 쌍은 대표 좌표를 고르면서 거리가 범위를 벗어났거나(병합 전에는 네 조합 중 하나만 범위 안이어도 셈) "
        "절단 규칙으로 이름(name_key)이 바뀐 경우다 — 병합이 상/하행 행을 합친 것이 아니다(서로 다른 suffix 는 병합하지 않는다).",
        "",
        "## 타 BIS — 대상 지역 안이지만 suffix 체계가 달라 제외",
        "",
        table_or_none(other_rows, ("접두", "관리도시명", "contain", "snap")),
        "",
        "## GGB·SEB 쌍 — 검토 파일과 override",
        "",
        f"- {MERGE_NEAR_M:.0f} m 초과 대상 쌍 {n_far:,}개(원거리 병합·분리)는 `reports/bus_pairs_review.csv` (먼 순). "
        "잘못된 판정은 `ref/bus_pair_overrides.csv` 에 `stop_key,action,keep,note` 로 적어 바로잡는다 "
        "(override_split 행의 merge_status 도 `split`).",
        "",
        md_table(ov_rows, ("override stop_key", "action", "keep", "판정", "대표")) if ov_rows
        else "- override 적용: 없음",
        "",
        "## 원 도시명 시도 × 판정 시도 (GGB·SEB 레코드)",
        "",
        md_table(ct_rows, ct_headers),
        "",
        "## 도시명은 서울·경기인데 제외된 레코드",
        "",
        table_or_none(label_rows, ("정류장번호", "정류장명", "도시명", "판정")),
        "",
        f"## snap 된 행 ({len(snap_rows)})",
        "",
        table_or_none(snap_rows, ("stop_key", "name", "sgg_nm", "snap_dist_m", "source_ids", "위도, 경도")),
        "",
        f"## 잔여 동명 쌍 < {RESIDUAL_DUP_M:.0f} m (병합 후, 검토용 — 자동 처리하지 않음)",
        "",
        table_or_none(res_rows, ("stop_key A", "stop_key B", "name A", "name B", "거리 m", "sgg_nm",
                                 "source_ids A", "source_ids B")),
        "",
    ])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    for p in (args.out, args.review, args.dropped, args.report):
        print(f"출력: {rel(p)}")


if __name__ == "__main__":
    main()
