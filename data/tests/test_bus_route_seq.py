"""버스 노선–정류소 순서표 — 합성 데이터만 쓴다."""
import pandas as pd
import pytest

from build_bus_route_seq import COLUMNS, build, gbis_route_types, normalize, seoul_route_types
from fetch_gbis import read_gbis

SEOUL_COLS = ["ROUTE_ID", "노선명", "순번", "NODE_ID", "ARS_ID", "정류소명", "X좌표", "Y좌표"]
GBIS_COLS = ["routeId", "routeName", "upDown", "staOrder", "stationId", "stationName", "x", "y"]


def _seoul(*rows):
    """(ROUTE_ID, 노선명, 순번, NODE_ID, 정류소명, X, Y)"""
    return pd.DataFrame([(r[0], r[1], r[2], r[3], "01001", *r[4:]) for r in rows], columns=SEOUL_COLS, dtype=str)


def _gbis(*rows):
    return pd.DataFrame(list(rows), columns=GBIS_COLS, dtype=str)


def _stub_assign(lons, lats):
    """경도 128 미만 = 대상 안(contain), 이상 = 대상 밖 (RegionIndex.assign 과 같은 dict)."""
    return [{"region_method": "contain" if float(x) < 128 else "", "drop_reason": "" if float(x) < 128 else "outside_region"}
            for x in lons]


# 정류소 DB: 병합된 정류소 · 분리된 쌍(대표 SEB, GGB 쪽은 -GGB) · 경기 BIS 만 등록한 정류소 · 미정차
STOPS = pd.DataFrame([
    ("100000001", "시청", "37.5000000", "126.9000000", "GGB100000001|SEB100000001", "0"),
    ("100000002", "갈림(서울쪽)", "37.5100000", "126.9100000", "SEB100000002", "0"),
    ("100000002-GGB", "갈림(경기쪽)", "37.5200000", "126.9200000", "GGB100000002", "0"),
    ("200000003", "경기정류소", "37.3000000", "127.1000000", "GGB200000003", "0"),
    ("200000004", "가상IC(미정차)", "37.3100000", "127.1100000", "GGB200000004", "1"),
], columns=["stop_key", "name", "lat", "lon", "source_ids", "is_virtual"])


def test_read_gbis(tmp_path):
    p = tmp_path / "routestation20260911V2.txt"
    p.write_text("routeId|routeName|upDown|staOrder|stationId|stationName|x|y^"
                 "200000006|300|상행|1|214001355|수월암리공단|127.0248|37.1185833^"
                 "200000006|300|하행|2|214000161|수월암2리입구|127.0308833|37.1155", encoding="utf-8")
    g = read_gbis(p)
    assert g.columns.tolist() == GBIS_COLS
    assert g[["upDown", "staOrder", "stationId"]].values.tolist() == [["상행", "1", "214001355"], ["하행", "2", "214000161"]]


def test_build_resolution_order_and_drop():
    seoul = _seoul(("S1", "101", "1", "100000001", "시청앞", "126.9001", "37.5001"),      # 자기 BIS(SEB) 병합 행
                   ("S1", "101", "2", "100000002", "갈림", "126.9100", "37.5100"),        # 분리 쌍 → SEB 쪽 대표 행
                   ("S1", "101", "3", "200000003", "경기정류소", "127.1000", "37.3000"),  # SEB 레코드 없음 → ID = stop_key
                   ("S1", "101", "4", "300000009", "인천어딘가", "128.5000", "37.4000"),  # DB 에 없음 · 대상 밖 → 뺌
                   ("S1", "101", "5", "300000010", "새정류소", "126.9500", "37.5500"),    # DB 에 없음 · 대상 안 → in_db=0
                   ("S1", "101", "6", "300000011", "합정역(가상)", "126.9600", "37.5600"))  # 서울 통과 노드 표기
    gbis = _gbis(("G1", "7", "상행", "1", "100000002", "갈림", "126.92", "37.52"),       # 분리 쌍 → GGB 쪽 행
                 ("G1", "7", "상행", "2", "200000004", "가상IC(미정차)", "127.11", "37.31"),
                 ("G1", "7", "하행", "3", "100000001", "시청", "126.90", "37.50"))
    out, info = build(normalize(seoul, gbis), STOPS, _stub_assign)
    assert out.columns.tolist() == COLUMNS
    s = out[out["route_id"] == "S1"]
    assert s[["seq", "stop_key", "in_db", "is_virtual"]].values.tolist() == [
        [1, "100000001", 1, 0], [2, "100000002", 1, 0], [3, "200000003", 1, 0],
        [5, "300000010", 0, 0], [6, "300000011", 0, 1]]           # 순번 4 는 비어도 순서는 그대로
    first, new = s.iloc[0], s.iloc[3]
    assert (first["name"], first["lat"], first["lon"]) == ("시청", 37.5, 126.9)         # in_db=1 → DB 이름·좌표
    assert (new["name"], new["lat"], new["lon"]) == ("새정류소", 37.55, 126.95)          # in_db=0 → 원천 이름·좌표
    g = out[out["route_id"] == "G1"]
    assert g[["seq", "updown", "stop_key", "is_virtual"]].values.tolist() == [
        [1, "상행", "100000002-GGB", 0], [2, "상행", "200000004", 1], [3, "하행", "100000001", 0]]
    assert set(s["updown"]) == {""} and set(out["source"]) == {"seoul", "gyeonggi"}
    assert info["src"]["how"].tolist() == ["own", "own", "key", "outside", "new", "new", "own", "own", "own"]
    assert info["interior"][["route_id", "seq"]].values.tolist() == [["S1", 4]]
    assert info["empty"] == []


def test_route_types_attached_and_unknown_code_fails():
    routes = pd.DataFrame([("G1", "14"), ("G2", "50")], columns=["routeId", "routeTypeCd"])
    types = gbis_route_types(routes)
    assert types == {"G1": ("광역", "광역급행형시내버스"), "G2": ("수요응답", "수요응답형(DRT)")}
    seoul = _seoul(("S1", "101", "1", "100000001", "시청", "126.9", "37.5"))
    gbis = _gbis(("G1", "M5107", "상행", "1", "200000003", "경기정류소", "127.1", "37.3"))
    out, _ = build(normalize(seoul, gbis), STOPS, _stub_assign, types)
    assert out[["route_id", "route_type", "route_type_src"]].values.tolist() == [
        ["G1", "광역", "광역급행형시내버스"], ["S1", "", ""]]                     # 유형이 없는 노선은 빈칸
    with pytest.raises(ValueError, match="코드표"):
        gbis_route_types(pd.DataFrame([("G9", "99")], columns=["routeId", "routeTypeCd"]))


def test_seoul_route_types():
    routes = pd.DataFrame([("100100026", "147", "3"), ("100100567", "N13", "15")],
                          columns=["busRouteId", "busRouteNm", "routeType"])
    assert seoul_route_types(routes) == {"100100026": ("간선", "간선"), "100100567": ("심야", "심야")}
    with pytest.raises(ValueError, match="서울 노선유형코드"):
        seoul_route_types(routes.assign(routeType="99"))


def test_ferry_routes_dropped():
    seoul = _seoul(("S1", "101", "1", "100000001", "시청", "126.9", "37.5"),
                   ("F1", "한강버스(서부)", "1", "115000952", "한강버스.마곡선착장", "126.84", "37.57"))
    assert normalize(seoul, _gbis())["route_id"].tolist() == ["S1"]


def test_build_route_entirely_outside_is_reported():
    seoul = _seoul(("S1", "101", "1", "100000001", "시청", "126.9", "37.5"),
                   ("S9", "999", "1", "300000009", "인천어딘가", "128.5", "37.4"))
    out, info = build(normalize(seoul, _gbis()), STOPS, _stub_assign)
    assert out["route_id"].tolist() == ["S1"] and info["empty"] == ["S9"] and len(info["interior"]) == 0


@pytest.mark.parametrize("seoul, gbis, msg", [
    (_seoul(("R1", "1", "1", "100000001", "시청", "126.9", "37.5")),
     _gbis(("R1", "7", "상행", "1", "100000001", "시청", "126.9", "37.5")), "노선 ID 가 겹칩니다"),
    (_seoul(("S1", "1", "1", "10000001", "시청", "126.9", "37.5")), _gbis(), "9자리"),
    (_seoul(("S1", "1", "1", "100000001", "시청", "126.9", "37.5"), ("S1", "1", "1", "100000002", "갈림", "126.9", "37.5")),
     _gbis(), "순번"),
])
def test_normalize_rejects_bad_source(seoul, gbis, msg):
    with pytest.raises(ValueError, match=msg):
        normalize(seoul, gbis)
