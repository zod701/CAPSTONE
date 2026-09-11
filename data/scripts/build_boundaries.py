"""행정동 → 시군구 경계 (서울 25구·경기 31시군 + 인접 시도 4피처).

경기 일반구는 시 단위로 합친다(41111~41117 → 41110 수원시). 인접 시도(인천·강원·충북·충남)는
시도 단위 1피처(`in_scope=false`)로 두어, 정류소가 "경계 밖"인지 "인접 시도 안"인지 가른다.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import shapely
from shapely.geometry import mapping, shape

from fetch_boundary import DEFAULT_OUT, KST, ROOT, file_line, md_table, rel, sha256, verify

SCOPE = {"11": 25, "41": 31}                 # 시도 → 기대 시군구 수
NEIGHBOURS = ("28", "51", "43", "44")        # 인천·강원·충북·충남
SIMPLIFY_TOL = 0.0003                        # 도 (≈30 m)
WEB_DECIMALS = 5
WEB_MAX_BYTES = 1_000_000
ATTRIBUTION = "행정경계: 통계청 SGIS(공공누리 1유형), 가공 vuski/admdongkor (CC BY 4.0)"
_CITY_GU = re.compile(r"^(.+?시).+구$")


def sgg_code(adm_cd2):
    """행정동 코드(10자리) → 시군구 코드(5자리). 경기는 일반구를 시로 합친다."""
    s = str(adm_cd2)
    if s.startswith("11"):
        return s[:5]
    if s.startswith("41"):
        return s[:4] + "0"
    raise ValueError(f"서울·경기 행정동 코드가 아닙니다: {adm_cd2!r}")


def sgg_name(sggnm, sido):
    """경기 `수원시장안구` → `수원시`. 서울 자치구는 그대로."""
    if str(sido) == "41":
        m = _CITY_GU.match(sggnm)
        if m:
            return m.group(1)
    return sggnm


def build(fc):
    """행정동 FeatureCollection → (피처 목록 [(props, geom)], 단계표)."""
    groups = {}
    for f in fc["features"]:
        p = f["properties"]
        sido = str(p["sido"])
        if sido in SCOPE:
            cd, nm, in_scope = sgg_code(p["adm_cd2"]), sgg_name(p["sggnm"], sido), True
        elif sido in NEIGHBOURS:
            cd, nm, in_scope = sido + "000", p["sidonm"], False
        else:
            continue
        g = groups.setdefault(cd, {"sido_cd": sido, "sido_nm": p["sidonm"], "in_scope": in_scope,
                                   "names": set(), "geoms": []})
        g["names"].add(nm)
        g["geoms"].append(shape(f["geometry"]))

    feats, n_invalid = [], 0
    for cd, g in sorted(groups.items(), key=lambda kv: (not kv[1]["in_scope"], kv[0])):
        if len(g["names"]) != 1:
            raise ValueError(f"{cd}: 시군구 이름이 하나로 모이지 않습니다 {sorted(g['names'])}")
        n_invalid += int((~shapely.is_valid(g["geoms"])).sum())
        geom = shapely.union_all(shapely.make_valid(g["geoms"]))
        props = {"sido_cd": g["sido_cd"], "sido_nm": g["sido_nm"], "sgg_cd": cd,
                 "sgg_nm": g["names"].pop(), "in_scope": g["in_scope"], "n_dong": len(g["geoms"])}
        feats.append((props, geom))

    got = {s: sum(1 for p, _ in feats if p["in_scope"] and p["sido_cd"] == s) for s in SCOPE}
    if got != SCOPE:
        raise ValueError(f"대상 시군구 수가 기대와 다릅니다: {got} (기대 {SCOPE})")
    if any(p["sgg_nm"].endswith("구") for p, _ in feats if p["sido_cd"] == "41"):
        raise ValueError("경기 시군 이름에 일반구가 남아 있습니다.")

    raw_gg = {f["properties"]["sgg"] for f in fc["features"] if str(f["properties"]["sido"]) == "41"}
    nb = [p for p, _ in feats if not p["in_scope"]]
    steps = [
        ("인접 시도 행정동 (28·51·43·44)", sum(p["n_dong"] for p in nb)),
        ("make_valid 대상 (무효 행정동)", n_invalid),
        ("경기 원 시군구 코드 (일반구 포함)", len(raw_gg)),
        ("서울 자치구 (in_scope)", got["11"]),
        ("경기 시군 (in_scope, 일반구 병합)", got["41"]),
        ("인접 시도 피처 (in_scope=false)", len(nb)),
    ]
    return feats, steps


def web_geom(geom):
    g = shapely.simplify(geom, SIMPLIFY_TOL, preserve_topology=True)
    return shapely.transform(g, lambda xy: xy.round(WEB_DECIMALS))


def fc_bytes(features):
    fc = {"type": "FeatureCollection",
          "features": [{"type": "Feature", "properties": p, "geometry": mapping(g)} for p, g in features]}
    return json.dumps(fc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="행정동 → 시군구 경계 (서울 25구·경기 31시군 + 인접 시도)")
    ap.add_argument("--src", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "processed" / "admin_sgg.geojson")
    ap.add_argument("--web-out", type=Path, default=ROOT / "data" / "processed" / "admin_sgg_web.geojson")
    ap.add_argument("--report", type=Path, default=ROOT / "data" / "reports" / "boundaries.md")
    args = ap.parse_args()

    src_line = file_line(args.src)
    try:
        fc = json.loads(args.src.read_text(encoding="utf-8"))
        steps = verify(fc)
        feats, build_steps = build(fc)
    except ValueError as e:
        sys.exit(f"실패: {e}")
    steps += build_steps

    web_keys = ("sido_cd", "sido_nm", "sgg_cd", "sgg_nm")
    web = [({k: p[k] for k in web_keys}, web_geom(g)) for p, g in feats if p["in_scope"]]
    full_b, web_b = fc_bytes(feats), fc_bytes(web)
    if len(web_b) >= WEB_MAX_BYTES:  # 쓰기 전에 확인 — 실패한 실행이 큰 파일을 남기지 않게
        sys.exit(f"실패: 웹용 경계가 너무 큽니다 ({len(web_b):,} B ≥ {WEB_MAX_BYTES:,} B)")
    for path, b in ((args.out, full_b), (args.web_out, web_b)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b)
    steps += [(f"{rel(args.out)} 피처", len(feats)), (f"{rel(args.web_out)} 피처", len(web))]

    table = md_table(steps)
    print(table)
    scope_rows = [(p["sgg_cd"], p["sgg_nm"], p["sido_nm"], p["n_dong"]) for p, _ in feats if p["in_scope"]]
    nb_rows = [(p["sgg_cd"], p["sgg_nm"], p["n_dong"]) for p, _ in feats if not p["in_scope"]]
    out_rows = [(f"`{rel(p)}`", f"{p.stat().st_size:,} B", n, f"`{sha256(p)[:12]}`")
                for p, n in ((args.out, len(feats)), (args.web_out, len(web)))]
    report = "\n".join([
        "# 행정경계 (build_boundaries.py)",
        "",
        f"- 실행 시각: {datetime.now(KST):%Y-%m-%d %H:%M:%S} KST",
        f"- 입력: {src_line}",
        f"- 파라미터: 대상 시도 11(서울)·41(경기), 인접 시도 {'·'.join(NEIGHBOURS)}, "
        f"웹용 simplify {SIMPLIFY_TOL}° (preserve_topology), 좌표 소수 {WEB_DECIMALS}자리, "
        f"웹용 상한 {WEB_MAX_BYTES:,} B",
        f"- {ATTRIBUTION}",
        "",
        "## 단계별 건수",
        "",
        table,
        "",
        f"## 대상 시군구 ({len(scope_rows)})",
        "",
        md_table(scope_rows, ("sgg_cd", "sgg_nm", "시도", "n_dong")),
        "",
        "## 인접 시도 (in_scope=false, 판정에서 제외 사유로만 쓴다)",
        "",
        md_table(nb_rows, ("sgg_cd", "sgg_nm", "n_dong")),
        "",
        "## 산출물",
        "",
        md_table(out_rows, ("파일", "크기", "피처", "SHA-256")),
        "",
    ])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(f"보고서: {rel(args.report)}")


if __name__ == "__main__":
    main()
