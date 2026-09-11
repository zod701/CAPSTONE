"""행정동 경계(vuski/admdongkor ver20260701) 내려받기 + 검증.

이미 받은 파일이 있으면 다시 받지 않는다(--force 로 강제). 피처 수가 기대와 다르면 실패한다.
보고서용 공통 함수(sha256, md_table 등)는 build_boundaries.py 도 가져다 쓴다.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.request
from collections import Counter
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
URL = ("https://raw.githubusercontent.com/vuski/admdongkor/master/"
       "ver20260701/HangJeongDong_ver20260701.geojson")
DEFAULT_OUT = ROOT / "data" / "external" / "admdongkor" / "HangJeongDong_ver20260701.geojson"
EXPECT = {"total": 3558, "11": 427, "41": 602}
PROPS = ("adm_nm", "adm_cd", "adm_cd2", "sido", "sidonm", "sgg", "sggnm")
KST = timezone(timedelta(hours=9))  # 고정 UTC+9 (venv 에 tzdata 없음)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path):
    """저장소 기준 상대 경로 (보고서에 사용자 경로를 남기지 않는다)."""
    p = Path(path).resolve()
    return p.relative_to(ROOT).as_posix() if p.is_relative_to(ROOT) else str(p)


def file_line(path):
    return f"`{rel(path)}` — {Path(path).stat().st_size:,} B, SHA-256 `{sha256(path)[:12]}`"


def md_table(rows, headers=("단계", "건수")):
    fmt = lambda v: f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) else str(v)
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(fmt(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)


def download(url, out):
    # .part 에 받은 뒤 바꿔 넣는다 — 중간에 끊긴 파일이 "이미 있음"으로 건너뛰어지지 않게
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(tmp, out)


def verify(fc):
    """FeatureCollection 검증 → [(단계, 건수)]. 기대와 다르면 ValueError."""
    if not isinstance(fc, dict) or fc.get("type") != "FeatureCollection" \
            or not isinstance(fc.get("features"), list):
        raise ValueError("GeoJSON FeatureCollection 이 아닙니다.")
    feats = fc["features"]
    missing = Counter(k for f in feats for k in PROPS if k not in (f.get("properties") or {}))
    if missing:
        raise ValueError(f"속성이 빠진 피처가 있습니다: {dict(missing)}")
    by_sido = Counter(str(f["properties"]["sido"]) for f in feats)
    got = {"total": len(feats), "11": by_sido["11"], "41": by_sido["41"]}
    if got != EXPECT:
        raise ValueError(f"피처 수가 기대와 다릅니다: {got} (기대 {EXPECT})")
    return [("행정동 피처 (전체)", got["total"]),
            ("서울(11) 행정동", got["11"]),
            ("경기(41) 행정동", got["41"])]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="행정동 경계(admdongkor) 내려받기 + 검증")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--force", action="store_true", help="파일이 있어도 다시 받는다")
    args = ap.parse_args()

    if args.force or not args.out.exists():
        print(f"내려받는 중: {URL}")
        download(URL, args.out)
    else:
        print("이미 있음 — 내려받기 건너뜀 (--force 로 다시 받기)")
    try:
        steps = verify(json.loads(args.out.read_text(encoding="utf-8")))
    except ValueError as e:  # JSONDecodeError 포함
        sys.exit(f"검증 실패: {e}")
    print(f"파일: {file_line(args.out)}")
    print(md_table(steps))


if __name__ == "__main__":
    main()
