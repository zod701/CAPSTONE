"""인천 노선 목록·경유 정류소 수집 (data.go.kr 15058487).

기존 fetch_gbis의 DATA_GO_KR_API_KEY 읽기/디코딩 방식을 사용한다.
완료한 노선은 재사용하고, 모든 요청이 성공해야 병합용 sections.csv를 쓴다.
"""
import argparse
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd

from fetch_boundary import KST, ROOT
from fetch_gbis import api_key
from incheon_api import ROUTE_URL, parse_page

OUT_DIR = ROOT / "data" / "external" / "incheon_bus"


def fetch_rows(key, operation, **params):
    rows, page = [], 1
    while True:
        url = ROUTE_URL + "/" + operation + "?" + urllib.parse.urlencode(
            {"serviceKey": key, "numOfRows": 1000, "pageNo": page, **params})
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                body = response.read()
        except OSError:
            raise ValueError(f"인천 {operation} 연결 실패 (키·요청 URL 생략)") from None
        result = parse_page(body)
        if result["page"] != page:
            raise ValueError("응답 페이지가 요청과 다릅니다")
        rows.extend(result["rows"])
        if page * result["size"] >= result["total"]:
            return rows
        page += 1


def latest():
    found = sorted(p for p in OUT_DIR.glob("*/sections.csv") if p.with_name("routes.csv").exists())
    return found[-1].parent if found else None


def collect(key, out):
    out = Path(out)
    route_file = out / "routes.csv"
    if route_file.exists():
        routes = pd.read_csv(route_file, dtype=str).fillna("")
    else:
        routes = pd.DataFrame(fetch_rows(key, "getBusRouteNo", routeNo=""))
        if routes.empty or not {"ROUTEID", "ROUTENO", "ROUTETPCD"} <= set(routes.columns):
            raise ValueError("인천 노선 목록이 비었거나 필수 필드가 없습니다")
        out.mkdir(parents=True, exist_ok=True)
        routes.to_csv(route_file, index=False, encoding="utf-8-sig")
    if routes["ROUTEID"].duplicated().any() or not routes["ROUTEID"].str.fullmatch(r"\d{9}").all():
        raise ValueError("인천 노선 ID가 중복되거나 9자리 숫자가 아닙니다")
    sections = []
    for i, rid in enumerate(routes["ROUTEID"], 1):
        path = out / f"route_{rid}.csv"
        if path.exists():
            frame = pd.read_csv(path, dtype=str).fillna("")
        else:
            frame = pd.DataFrame(fetch_rows(key, "getBusRouteSectionList", routeId=rid))
            if frame.empty or not {"ROUTEID", "BSTOPID", "BSTOPSEQ", "DIRCD"} <= set(frame.columns):
                raise ValueError(f"노선 {rid} 경유 정류소가 비었거나 필수 필드가 없습니다")
            if set(frame["ROUTEID"]) != {rid}:
                raise ValueError(f"노선 {rid} 응답의 노선 ID가 다릅니다")
            frame.to_csv(path, index=False, encoding="utf-8-sig")
        sections.append(frame)
        print(f"인천 노선 {i}/{len(routes)}: {rid} ({len(frame)} 정류소)", flush=True)
    joined = pd.concat(sections, ignore_index=True)
    temp = out / "sections.tmp.csv"
    joined.to_csv(temp, index=False, encoding="utf-8-sig")
    temp.replace(out / "sections.csv")
    return len(routes), len(joined)


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="인천 버스 노선·경유 정류소 수집 (실패 후 같은 명령으로 이어받기)")
    ap.add_argument("--out", type=Path, default=OUT_DIR / f"{datetime.now(KST):%Y%m%d}")
    args = ap.parse_args(argv)
    try:
        n, rows = collect(api_key(), args.out)
    except ValueError as exc:
        sys.exit(f"실패: {exc}")
    print(f"완료: {n} 노선 · {rows}행 → {args.out}")


if __name__ == "__main__":
    main()
