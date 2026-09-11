"""서울 버스 노선 목록 내려받기 — 노선 유형(routeType)·배차간격(term) 등.

서울특별시_노선정보조회 서비스(data.go.kr 15000193) getBusRouteList 를 빈 검색어로 1콜 부르면 서울 BIS 의 노선 전부
(서울로 드나드는 경기·인천 노선 포함)가 온다. 키는 fetch_gbis 와 같은 DATA_GO_KR_API_KEY (서비스별 활용신청이 필요하다).
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime

import pandas as pd

from fetch_boundary import KST, ROOT, file_line
from fetch_gbis import api_key, mask

API = "http://ws.bus.go.kr/api/rest/busRouteInfo/getBusRouteList"
OUT_DIR = ROOT / "data" / "external" / "seoul_bus"
FILE = "busRouteList.csv"


def latest():
    """external/seoul_bus/ 에서 가장 최근 날짜의 노선 목록. 없으면 FileNotFoundError."""
    found = sorted(OUT_DIR.glob(f"*/{FILE}"))
    if not found:
        raise FileNotFoundError("서울 노선 목록이 없습니다 — data/scripts/fetch_seoul_routes.py 로 받는다")
    return found[-1]


def route_list(key):
    """→ 노선 목록 DataFrame(str). 결과 코드가 0 이 아니거나 비었으면 ValueError (메시지의 키는 가린다)."""
    url = API + "?" + urllib.parse.urlencode({"serviceKey": key, "strSrch": "", "resultType": "json"})
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            body = r.read().decode("utf-8", "replace")
    except OSError as e:  # HTTPError(401 = 활용신청 전)·URLError 포함
        raise ValueError(mask(str(e), key)) from None
    try:
        res = json.loads(body)
        head = res["msgHeader"]
        if str(head["headerCd"]) != "0":
            raise ValueError(f"결과 코드 {head['headerCd']}: {head['headerMsg']}")
        items = res["msgBody"]["itemList"] or []
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ValueError(f"예상과 다른 응답: {mask(body[:300], key)}") from None
    if not items:
        raise ValueError("노선 목록이 비었습니다")
    return pd.DataFrame(items, dtype=str)


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="서울 버스 노선 목록 내려받기 (노선 유형)")
    ap.add_argument("--force", action="store_true", help="오늘 받은 파일이 있어도 다시 받는다")
    args = ap.parse_args(argv)
    out = OUT_DIR / f"{datetime.now(KST):%Y%m%d}" / FILE
    if out.exists() and not args.force:
        print("이미 있음 — 건너뜀 (--force 로 다시 받기)")
    else:
        try:
            df = route_list(api_key())
        except ValueError as e:
            sys.exit(f"실패: {e}")
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"노선 {len(df):,}개")
    print(f"파일: {file_line(out)}")


if __name__ == "__main__":
    main()
