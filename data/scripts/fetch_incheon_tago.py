"""인천 노선 목록·경유 정류소 수집 — 국토교통부 TAGO 버스노선정보 (data.go.kr 15098529, cityCode=23).

인천시 자체 API(fetch_incheon_routes.py)가 HTTP_ERROR(04)를 돌려주는 동안의 대체 원천이다.
출력은 fetch_incheon_routes.py 와 같은 열(ROUTEID·ROUTENO·ROUTETPCD / ROUTEID·BSTOPID·BSTOPSEQ·DIRCD·BSTOPNM)로 쓰고
ID 앞의 'ICB' 를 떼어 9자리로 맞춘다 — build_bus_route_seq.py 가 그대로 병합한다.
완료한 노선은 재사용하고, 모든 요청이 성공해야 병합용 sections.csv를 쓴다.
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd

from fetch_boundary import KST
from fetch_gbis import api_key
from fetch_incheon_routes import OUT_DIR

TAGO_URL = "https://apis.data.go.kr/1613000/BusRouteInfoInqireService"
CITY_CODE = 23
# TAGO routetp → 인천 API ROUTETPCD (incheon_api.ROUTE_TYPES)
TYPE_CODES = {"지선버스": "1", "간선버스": "2", "좌석버스": "3", "광역버스": "4", "공항버스": "5",
              "마을버스": "6", "순환버스": "7", "급행버스": "8"}


def fetch_items(key, operation, **params):
    items, page = [], 1
    while True:
        url = TAGO_URL + "/" + operation + "?" + urllib.parse.urlencode(
            {"serviceKey": key, "_type": "json", "numOfRows": 1000, "pageNo": page, "cityCode": CITY_CODE, **params})
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                res = json.load(response)["response"]
        except (OSError, ValueError, KeyError):
            raise ValueError(f"TAGO {operation} 연결·응답 실패 (키·요청 URL 생략)") from None
        if res["header"]["resultCode"] != "00":
            raise ValueError(f"TAGO {operation} 오류 ({res['header']['resultCode']})")
        body = res["body"]
        got = body["items"]["item"] if body["items"] else []
        items.extend(got if isinstance(got, list) else [got])
        if page * body.get("numOfRows", 1000) >= body.get("totalCount", 0):  # 단건 조회(getRouteInfoIem)는 쪽 정보가 없다
            return items
        page += 1


def strip_icb(ids):
    out = ids.astype(str).str.removeprefix("ICB")
    if not out.str.fullmatch(r"\d{9}").all():
        raise ValueError("TAGO ID 가 'ICB' + 9자리 숫자가 아닙니다")
    return out


def collect(key, out):
    out = Path(out)
    route_file = out / "routes.csv"
    if route_file.exists():
        routes = pd.read_csv(route_file, dtype=str).fillna("")
    else:
        raw = pd.DataFrame(fetch_items(key, "getRouteNoList"))
        if raw.empty or not {"routeid", "routeno", "routetp"} <= set(raw.columns):
            raise ValueError("TAGO 인천 노선 목록이 비었거나 필수 필드가 없습니다")
        unknown = set(raw["routetp"]) - set(TYPE_CODES)
        if unknown:
            raise ValueError(f"모르는 노선 유형: {sorted(unknown)}")
        routes = raw.assign(ROUTEID=strip_icb(raw["routeid"]), ROUTENO=raw["routeno"].astype(str),
                            ROUTETPCD=raw["routetp"].map(TYPE_CODES))
        out.mkdir(parents=True, exist_ok=True)
        routes.to_csv(route_file, index=False, encoding="utf-8-sig")
    if routes["ROUTEID"].duplicated().any():
        raise ValueError("인천 노선 ID가 중복됩니다")
    sections = []
    for i, (rid, tago_id) in enumerate(zip(routes["ROUTEID"], routes["routeid"]), 1):
        path = out / f"route_{rid}.csv"
        if path.exists():
            frame = pd.read_csv(path, dtype=str).fillna("")
        else:
            raw = pd.DataFrame(fetch_items(key, "getRouteAcctoThrghSttnList", routeId=tago_id))
            if raw.empty or not {"routeid", "nodeid", "nodeord", "updowncd"} <= set(raw.columns):
                raise ValueError(f"노선 {rid} 경유 정류소가 비었거나 필수 필드가 없습니다")
            if set(raw["routeid"]) != {tago_id}:
                raise ValueError(f"노선 {rid} 응답의 노선 ID가 다릅니다")
            frame = raw.assign(ROUTEID=rid, BSTOPID=strip_icb(raw["nodeid"]), BSTOPSEQ=raw["nodeord"].astype(str),
                               DIRCD=raw["updowncd"].astype(str), BSTOPNM=raw["nodenm"])
            frame.to_csv(path, index=False, encoding="utf-8-sig")
        sections.append(frame)
        print(f"인천 노선 {i}/{len(routes)}: {rid} ({len(frame)} 정류소)", flush=True)
    joined = pd.concat(sections, ignore_index=True)
    temp = out / "sections.tmp.csv"
    joined.to_csv(temp, index=False, encoding="utf-8-sig")
    temp.replace(out / "sections.csv")
    # 노선 정보(평일 배차 intervaltime · 첫·막차) — 배차 표(build_headway.py)가 읽는다. 응답이 빈 노선은 ID 만 남긴다.
    infos = []
    for rid, tago_id in zip(routes["ROUTEID"], routes["routeid"]):
        path = out / f"info_{rid}.csv"
        if path.exists():
            frame = pd.read_csv(path, dtype=str).fillna("")
        else:
            frame = pd.DataFrame(fetch_items(key, "getRouteInfoIem", routeId=tago_id) or [{}]).assign(ROUTEID=rid)
            frame.to_csv(path, index=False, encoding="utf-8-sig")
        infos.append(frame)
    pd.concat(infos, ignore_index=True).to_csv(out / "info.csv", index=False, encoding="utf-8-sig")
    print(f"인천 노선 정보 {len(infos)} 노선", flush=True)
    return len(routes), len(joined)


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="인천 버스 노선·경유 정류소 수집 — TAGO (실패 후 같은 명령으로 이어받기)")
    ap.add_argument("--out", type=Path, default=OUT_DIR / f"{datetime.now(KST):%Y%m%d}_tago")
    args = ap.parse_args(argv)
    try:
        n, rows = collect(api_key(), args.out)
    except ValueError as exc:
        sys.exit(f"실패: {exc}")
    print(f"완료: {n} 노선 · {rows}행 → {args.out}")


if __name__ == "__main__":
    main()
