"""경기 GBIS 기반정보 — 노선·노선–정류소·정류소 일괄 파일 내려받기 (+ 두 스크립트가 쓰는 읽기 함수).

기반정보 항목조회(data.go.kr 15080658) 1콜로 그날 버전과 파일 URL 을 받고, 노선·노선–정류소·정류소 파일을 받는다.
파일 URL 은 키 없이 열려 있어 호출 한도(개발계정 100콜/일)에 들어가지 않는다.
키는 환경변수 또는 저장소 루트 .env 의 DATA_GO_KR_API_KEY. 출력에 키가 섞여 나오지 않게 가린다.
"""
import argparse
import io
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

from fetch_boundary import ROOT, download, file_line

API = "https://apis.data.go.kr/6410000/baseinfoservice/v2/getBaseInfoItemv2"
KEY_NAME = "DATA_GO_KR_API_KEY"
OUT_DIR = ROOT / "data" / "external" / "gbis"
KINDS = {"route": "route", "routestation": "routeStation", "station": "station"}  # 파일 이름 앞부분 → 응답 항목 이름 앞부분


def read_gbis(path):
    """GBIS 일괄 파일 (UTF-8, 필드 `|`, 레코드 `^`) → DataFrame(str)."""
    text = Path(path).read_text(encoding="utf-8").replace("\n", "").replace("^", "\n")
    return pd.read_csv(io.StringIO(text), sep="|", dtype=str).fillna("")


def latest(kind):
    """external/gbis/ 에서 가장 최근 버전의 kind 파일. 없으면 FileNotFoundError."""
    found = sorted(OUT_DIR.glob(f"*/{kind}[0-9]*V2.txt"))  # route 가 routestation·routeline 에 걸리지 않게 날짜가 바로 뒤따라야 한다
    if not found:
        raise FileNotFoundError(f"GBIS {kind} 파일이 없습니다 — data/scripts/fetch_gbis.py 로 받는다")
    return found[-1]


def api_key():
    """환경변수 → .env (`KEY ="value"` 꼴도 받는다). 인코딩 키가 들어 있어도 디코딩 키로 통일한다."""
    v = os.environ.get(KEY_NAME, "")
    env = ROOT / ".env"
    if not v and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            k, _, val = line.partition("=")
            if k.strip() == KEY_NAME:
                v = val.strip().strip("\"'")
    if not v:
        raise ValueError(f"{KEY_NAME} 가 환경변수에도 .env 에도 없습니다")
    return urllib.parse.unquote(v)


def base_info(key):
    """→ baseInfoItem dict. 결과 코드가 0 이 아니면 ValueError (메시지의 키는 가린다)."""
    url = API + "?" + urllib.parse.urlencode({"serviceKey": key, "format": "json"})
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
    except OSError as e:  # HTTPError·URLError 포함
        raise ValueError(mask(str(e), key)) from None
    try:
        res = json.loads(body)["response"]
        head = res["msgHeader"]
        if head["resultCode"] != 0:
            raise ValueError(f"결과 코드 {head['resultCode']}: {head['resultMessage']}")
        return res["msgBody"]["baseInfoItem"]
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ValueError(f"예상과 다른 응답: {mask(body[:300], key)}") from None


def mask(s, key):
    return s.replace(key, "<KEY>").replace(urllib.parse.quote(key, safe=""), "<KEY>")


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="경기 GBIS 노선·노선–정류소·정류소 일괄 파일 내려받기")
    ap.add_argument("--force", action="store_true", help="같은 버전 파일이 있어도 다시 받는다")
    args = ap.parse_args(argv)
    try:
        item = base_info(api_key())
    except ValueError as e:
        sys.exit(f"실패: {e}")
    for field in KINDS.values():
        version = str(item[f"{field}Version"])
        url = item[f"{field}DownloadUrl"]
        out = OUT_DIR / version / url.rsplit("?", 1)[-1]
        if args.force or not out.exists():
            print(f"내려받는 중: {url}")
            download(url, out)
        else:
            print(f"이미 있음 — 건너뜀 (--force 로 다시 받기): {out.name}")
        print(f"파일: {file_line(out)}")


if __name__ == "__main__":
    main()
