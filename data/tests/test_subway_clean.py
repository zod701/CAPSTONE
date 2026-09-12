import csv
import json
import re
from pathlib import Path

import pandas as pd
import pytest

import clean_subway_stations as css
from clean_subway_stations import (COLUMNS, EMPTY, INVALID, clean, collapse_ws, coord_twins, dedupe,
                                   fix_source_rows, join_lines, parse_date, station_clusters, station_ids)

REF = Path(__file__).resolve().parents[1] / "ref"
SRC_COLS = ["역번호", "역사명", "노선번호", "노선명", "영문역사명", "한자역사명", "환승역구분", "환승노선번호",
            "환승노선명", "역위도", "역경도", "운영기관명", "역사도로명주소", "역사전화번호", "데이터기준일자"]
LINE_MAP = pd.DataFrame([("7호선", "seoul7", "7호선"),
                         ("수도권 도시철도 9호선", "metro9", "9호선"),
                         ("경인선", "gyeongin", "1호선")],
                        columns=["raw_line_name", "line_id", "line_group"], dtype=str)


def _row(no, name, line, date="2025-12-31", lon="126.98", lat="37.48", transfer="일반역",
         code="S1107", op="서울교통공사"):
    r = dict.fromkeys(SRC_COLS, "")
    r.update({"역번호": no, "역사명": name, "노선명": line, "노선번호": code, "환승역구분": transfer,
              "역경도": lon, "역위도": lat, "운영기관명": op, "데이터기준일자": date})
    return r


def _raw(*rows):
    return pd.DataFrame(list(rows), dtype=str)


def _stub_assign(lons, lats):
    """경도 128 미만 = 서울 서초구 포함, 이상 = 대상 밖 (RegionIndex.assign 과 같은 dict)."""
    inside = {"sido_cd": "11", "sido_nm": "서울특별시", "sgg_cd": "11650", "sgg_nm": "서초구",
              "region_method": "contain", "snap_dist_m": None, "drop_reason": "", "detail": ""}
    outside = {"sido_cd": "", "sido_nm": "", "sgg_cd": "", "sgg_nm": "", "region_method": "",
               "snap_dist_m": None, "drop_reason": "outside_region", "detail": "far"}
    return [dict(inside if float(x) < 128 else outside) for x in lons]


@pytest.mark.parametrize("s, want", [
    ("2025-12-31", ("2025-12-31", "%Y-%m-%d")),
    ("2025-12-31 00:00:00", ("2025-12-31", "%Y-%m-%d %H:%M:%S")),
    ("20240812", ("2024-08-12", "%Y%m%d")),
    (" 2025-04-08 ", ("2025-04-08", "%Y-%m-%d")),
    ("", ("", EMPTY)),
    (None, ("", EMPTY)),
    ("1900-01-00", ("", INVALID)),   # 원천의 무효 날짜
    ("2025-02-30", ("", INVALID)),
    ("2025.12.31", ("", INVALID)),
])
def test_parse_date(s, want):
    assert parse_date(s) == want


@pytest.mark.parametrize("s, want", [
    ("수도권  도시철도 9호선", "수도권 도시철도 9호선"),
    (" 경의\t중앙선 ", "경의 중앙선"),
    ("2호선", "2호선"),
])
def test_collapse_ws(s, want):
    assert collapse_ws(s) == want


def test_dedupe_keeps_latest_with_alias():
    df = pd.DataFrame([
        ("4호선", "0432", "총신대입구(이수)", "2024-12-31"),   # 다른 노선 — 중복 아님
        ("7호선", "0736", "총신대입구(이수)", "2024-12-31"),   # 옛 이름 → alias
        ("7호선", "0736", "이수", "2025-12-31"),              # 최신 → 남김 (파일에서 뒤에 있어도)
        ("경인선", "1809", "주안역", ""),                     # 빈 날짜는 맨 뒤
        ("경인선", "1809", "주안역", "2025-04-08"),           # 같은 이름 → alias 없음
    ], columns=["line_raw", "역번호", "역사명", "data_date"])
    kept, groups = dedupe(df)
    assert kept[["line_raw", "역번호", "역사명", "data_date", "aliases"]].values.tolist() == [
        ["4호선", "0432", "총신대입구(이수)", "2024-12-31", ""],
        ["7호선", "0736", "이수", "2025-12-31", "총신대입구(이수)"],
        ["경인선", "1809", "주안역", "2025-04-08", ""],
    ]
    by_no = {g["역번호"]: g for g in groups}
    assert sorted(by_no) == ["0736", "1809"]
    assert by_no["0736"]["kept"] == ("이수", "2025-12-31")
    assert by_no["0736"]["dropped"] == [("총신대입구(이수)", "2024-12-31")]
    assert by_no["1809"]["aliases"] == ""


def test_dedupe_alias_order_and_distinct():
    df = pd.DataFrame([("2호선", "0201", "A", "2025-01-01"), ("2호선", "0201", "B", ""),
                       ("2호선", "0201", "C", "2024-01-01"), ("2호선", "0201", "B", "2023-01-01")],
                      columns=["line_raw", "역번호", "역사명", "data_date"])
    kept, groups = dedupe(df)
    assert kept[["역사명", "aliases"]].values.tolist() == [["A", "C|B"]]
    assert len(groups) == 1 and len(groups[0]["dropped"]) == 3


def test_join_lines_unmapped_raises():
    df = pd.DataFrame({"line_raw": ["7호선", "신규선", "신규선"]})
    with pytest.raises(ValueError, match="'신규선' 2행"):
        join_lines(df, LINE_MAP)
    got = join_lines(df.iloc[:1], LINE_MAP)
    assert got[["line_id", "line_group"]].values.tolist() == [["seoul7", "7호선"]]


def test_join_lines_station_override():
    # 경원선 용산–왕십리는 경의중앙선, 청량리 이북은 1호선 — 운영 노선명 하나가 승객 노선 둘에 걸친다
    line_map = pd.concat([LINE_MAP, pd.DataFrame([("경원선", "gyeongwon", "1호선"),
                                                  ("경의중앙선", "gyeongui", "경의중앙선")],
                                                 columns=LINE_MAP.columns)], ignore_index=True)
    df = pd.DataFrame({"line_raw": ["경원선", "경원선"], "역번호": ["1010", "1015"]})
    ov = pd.DataFrame([("경원선", "1010", "경의중앙선", "한남")],
                      columns=["line_raw", "station_no", "line_group", "note"])
    got = join_lines(df, line_map, ov)
    assert got[["line_id", "line_group"]].values.tolist() == [["gyeongwon", "경의중앙선"], ["gyeongwon", "1호선"]]
    # 어느 역에도 안 맞는 보정 = 원천이 바뀐 신호
    with pytest.raises(ValueError, match="맞지 않습니다"):
        join_lines(df, line_map, ov.assign(station_no="9999"))
    with pytest.raises(ValueError, match="line_group"):
        join_lines(df, line_map, ov.assign(line_group="없는노선"))


def test_real_station_overrides_apply_to_gyeongui():
    ov = pd.read_csv(REF / "subway_station_line_overrides.csv", dtype=str, encoding="utf-8-sig")
    assert set(ov["line_group"]) == {"경의중앙선"}
    assert sorted(ov["station_no"]) == ["1003", "1008", "1009", "1010", "1011", "1012", "1013"]


def test_ref_station_rows_go_through_clean():
    ref = pd.DataFrame([("N01", "운정중앙", "north", "1", "0", "지티엑스에이운영", "126.98", "37.48", "", ""),
                        ("X110", "구성", "south", "3", "1", "지티엑스에이운영", "126.99", "37.49", "", "")],
                       columns=["station_no", "name", "segment", "seq", "is_transfer", "operator", "lon", "lat",
                                "method", "checks"])
    rows = css.ref_station_rows(ref, "GTX-A", "2026-09-11")
    line_map = pd.concat([LINE_MAP, pd.DataFrame([("GTX-A", "gtxa", "GTX-A")], columns=LINE_MAP.columns)])
    out, _ = clean(rows, _stub_assign, line_map)
    assert out[["station_id", "name_key", "line_group", "is_transfer", "data_date"]].values.tolist() == [
        ["gtxa-N01", "운정중앙", "GTX-A", 0, "2026-09-11"], ["gtxa-X110", "구성", "GTX-A", 1, "2026-09-11"]]


def test_real_gtxa_reference():
    ref = pd.read_csv(REF / "gtxa_stations.csv", dtype=str, encoding="utf-8-sig")
    assert ref["station_no"].is_unique and len(ref) == 9
    # 첨부 노선도 기준 두 구간 (2026-09-11 사용자 확인): 운정중앙–서울역 / 수서–동탄
    seg = {s: g.sort_values("seq", key=lambda c: c.astype(int))["name"].tolist() for s, g in ref.groupby("segment")}
    assert seg == {"north": ["운정중앙", "킨텍스", "대곡", "연신내", "서울역"], "south": ["수서", "성남", "구성", "동탄"]}
    assert ref["lon"].astype(float).between(126.6, 127.3).all() and ref["lat"].astype(float).between(37.1, 37.8).all()


COORD_COLS = ["line_raw", "station_no", "name", "lon", "lat", "method"]
DROP_COLS = ["line_raw", "station_no", "name", "note"]


def _src_rows():
    return pd.DataFrame([("경의중앙선", "1204", "양원역", "129.091321", "36.963729"),   # 경북 양원역(영동선) 좌표가 들어간 행
                         ("경의중앙선", "1019", "광운대역", "127.062", "37.6239"),       # 경의중앙선은 광운대에 서지 않는다
                         ("경춘선", "1019", "광운대역", "127.061975", "37.623635")],
                        columns=["line_raw", "역번호", "역사명", "역경도", "역위도"])


def test_fix_source_rows_coords_and_drops():
    co = pd.DataFrame([("경의중앙선", "1204", "양원", "127.107918", "37.606622", "VWorld")], columns=COORD_COLS)
    dr = pd.DataFrame([("경의중앙선", "1019", "광운대", "서지 않는다")], columns=DROP_COLS)
    out, moved, removed = fix_source_rows(_src_rows(), co, dr)
    assert out[["line_raw", "역번호", "역경도", "역위도"]].values.tolist() == [
        ["경의중앙선", "1204", "127.107918", "37.606622"], ["경춘선", "1019", "127.061975", "37.623635"]]
    (line, no, name, old, new, dist, method), = moved
    assert (line, no, name, old, new, method) == ("경의중앙선", "1204", "양원역", "36.963729, 129.091321",
                                                   "37.606622, 127.107918", "VWorld")
    assert dist == pytest.approx(189_500, abs=1_000)  # 경북 봉화 → 서울 중랑
    assert removed == [("경의중앙선", "1019", "광운대역", "서지 않는다")]
    assert fix_source_rows(_src_rows())[0].equals(_src_rows())  # 보정표가 없으면 그대로


@pytest.mark.parametrize("co, dr, msg", [
    (pd.DataFrame([("경의중앙선", "9999", "양원", "127.1", "37.6", "")], columns=COORD_COLS), None, "맞지 않습니다"),
    (pd.DataFrame([("경의중앙선", "1204", "구리", "127.1", "37.6", "")], columns=COORD_COLS), None, "이름이 원천과 다릅니다"),
    (None, pd.DataFrame([("경의중앙선", "1019", "회기", "")], columns=DROP_COLS), "이름이 원천과 다릅니다"),
    (None, pd.DataFrame([("경의중앙선", "1015", "회기", "")], columns=DROP_COLS), "맞지 않습니다"),
])
def test_fix_source_rows_mismatch_means_source_changed(co, dr, msg):
    with pytest.raises(ValueError, match=msg):
        fix_source_rows(_src_rows(), co, dr)


def test_clean_applies_coord_fix_before_region():
    # 대상 밖 좌표(경도 ≥ 128)가 들어간 행도 보정하면 지역 판정을 통과한다
    raw = _raw(_row("0736", "이수", "7호선", lon="129.09", lat="36.96"), _row("0737", "남성", "7호선"))
    co = pd.DataFrame([("7호선", "0736", "이수", "126.98", "37.48", "VWorld")], columns=COORD_COLS)
    dr = pd.DataFrame([("7호선", "0737", "남성", "")], columns=DROP_COLS)
    out, info = clean(raw, _stub_assign, LINE_MAP, coord_overrides=co, drops=dr)
    assert out[["station_id", "lon"]].values.tolist() == [["seoul7-0736", 126.98]]
    steps = dict(info["steps"])
    assert (steps["좌표 보정 (ref/subway_coord_overrides.csv)"], steps["원천 오류 행 제외 (ref/subway_station_drops.csv)"]) == (1, 1)


def test_real_coord_overrides_and_drops():
    co = pd.read_csv(REF / "subway_coord_overrides.csv", dtype=str, encoding="utf-8-sig")
    assert list(co.columns) == [*COORD_COLS, "checks"]
    assert co["lon"].astype(float).between(126.6, 127.3).all() and co["lat"].astype(float).between(37.3, 37.8).all()
    dr = pd.read_csv(REF / "subway_station_drops.csv", dtype=str, encoding="utf-8-sig")
    assert list(dr.columns) == DROP_COLS


def test_station_ids_unique():
    df = pd.DataFrame({"line_id": ["seoul7", "incheon7"], "역번호": ["0751", "0751"]})
    assert station_ids(df).tolist() == ["seoul7-0751", "incheon7-0751"]
    # 두 노선을 같은 line_id 로 잘못 매핑하면 같은 역번호가 부딪친다
    with pytest.raises(ValueError, match="seoul7-0751"):
        station_ids(df.assign(line_id="seoul7"))


def test_clean_output():
    raw = _raw(
        _row("0736", "총신대입구(이수)", "7호선", date="2024-12-31", transfer="환승역"),
        _row("0736", "이수", "7호선", date="2025-12-31 00:00:00", transfer="환승역"),
        _row("4126", "언주역", "수도권  도시철도 9호선", date="1900-01-00", code="S1109",
             transfer="도시철도 일반역", op="서울시메트로9호선㈜"),
        _row("4131", "삼전", "수도권  도시철도 9호선", date="", code="S1109", transfer="도시철도 환승역"),
        _row("0999", "좌표없음", "7호선", lat="", lon="abc"),
        # 대상 밖 노선은 매핑에 없어도 된다
        _row("0101", "다대포해수욕장", "부산 도시철도 1호선", lon="128.96", lat="35.05"),
    )
    out, info = clean(raw, _stub_assign, LINE_MAP)
    assert list(out.columns) == COLUMNS
    assert out["station_id"].tolist() == ["seoul7-0736", "metro9-4126", "metro9-4131"]
    r = out.iloc[0]
    assert (r["name"], r["aliases"], r["data_date"], r["is_transfer"]) == \
        ("이수", "총신대입구(이수)", "2025-12-31", 1)
    assert (r["line_raw"], r["line_code"], r["line_group"], r["operator"]) == \
        ("7호선", "S1107", "7호선", "서울교통공사")
    assert (r["sgg_cd"], r["sgg_nm"], r["region_method"]) == ("11650", "서초구", "contain")
    r = out.iloc[1]
    assert (r["line_raw"], r["line_id"], r["name_key"], r["data_date"], r["is_transfer"]) == \
        ("수도권 도시철도 9호선", "metro9", "언주", "", 0)
    assert out.iloc[2]["is_transfer"] == 1
    steps = dict(info["steps"])
    assert steps["원본 행"] == 6
    assert steps["노선명 공백 정리된 행 (연속 공백 → 한 칸)"] == 2
    assert steps["기준일 형식 무효 → 빈 값 (1900-01-00)"] == 1
    assert steps[f"기준일 형식 {EMPTY}"] == 1
    assert steps["좌표 무효 제외"] == 1
    assert steps["(line_raw, 역번호) 중복 제거"] == 1
    assert steps["제외 (far)"] == 1
    assert info["date_fmt"].tolist() == ["%Y-%m-%d %H:%M:%S", INVALID, EMPTY]


def test_clean_unmapped_in_scope_line_fails():
    raw = _raw(_row("0736", "이수", "7호선"), _row("9901", "새역", "신규선"))
    with pytest.raises(ValueError, match="신규선"):
        clean(raw, _stub_assign, LINE_MAP)


def test_station_clusters():
    # 같은 이름 ≈89 m → 한 역, ≈890 m → 다른 역, 다른 이름은 가까워도 다른 역
    keys = ["서울", "서울", "서울", "시청"]
    lons = [126.970, 126.971, 126.980, 126.9705]
    lats = [37.55, 37.55, 37.55, 37.55]
    c = station_clusters(keys, lons, lats)
    assert c[0] == c[1] and len({c[0], c[2], c[3]}) == 3


def test_coord_twins_same_line_only():
    # 원천의 5호선 마곡이 발산 좌표를 복사한 경우 — 다른 노선(9호선)의 가까운 역은 정상
    out = pd.DataFrame({"line_raw": ["5호선", "5호선", "9호선", "5호선"],
                        "name_key": ["마곡", "발산", "마곡나루", "마곡"],
                        "lon": [126.83769, 126.83763, 126.83770, 126.8254],
                        "lat": [37.55865, 37.55869, 37.55866, 37.5602]})
    [(d, i, j)] = coord_twins(out)
    assert (i, j) == (0, 1) and d == pytest.approx(6.9, abs=0.3)


# --- main() 끝에서 끝까지: 합성 XLSX + 경계 GeoJSON ---

def _square(sido_cd, sido_nm, sgg_cd, sgg_nm, in_scope, minlon, minlat, maxlon, maxlat):
    ring = [[minlon, minlat], [maxlon, minlat], [maxlon, maxlat], [minlon, maxlat], [minlon, minlat]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {"sido_cd": sido_cd, "sido_nm": sido_nm, "sgg_cd": sgg_cd, "sgg_nm": sgg_nm,
                           "in_scope": in_scope, "n_dong": 1}}


@pytest.fixture
def inputs(tmp_path):
    src = tmp_path / "stations.xlsx"
    _raw(
        _row("0736", "이수", "7호선", date="2025-12-31 00:00:00", lon="126.981766", lat="37.485258",
             transfer="환승역"),
        _row("0736", "총신대입구(이수)", "7호선", date="2024-12-31", lon="126.981766", lat="37.485258",
             transfer="환승역"),
        _row("4126", "언주", "수도권  도시철도 9호선", date="1900-01-00", lon="126.99", lat="37.49"),
        _row("1809", "주안역", "경인선", date="2025-04-08", lon="126.680702", lat="37.465041"),
        _row("0101", "다대포해수욕장", "부산 도시철도 1호선", date="20240812", lon="128.96", lat="35.05"),
    ).to_excel(src, sheet_name=css.SHEET, index=False)
    geo = tmp_path / "admin_sgg.geojson"
    geo.write_text(json.dumps({"type": "FeatureCollection", "features": [
        _square("11", "서울특별시", "11650", "서초구", True, 126.97, 37.47, 127.00, 37.50),
        _square("28", "인천광역시", "28000", "인천광역시", False, 126.60, 37.40, 126.75, 37.50),
    ]}, ensure_ascii=False), encoding="utf-8")
    line_map = tmp_path / "subway_line_map.csv"
    LINE_MAP.to_csv(line_map, index=False, encoding="utf-8-sig")
    return {"src": src, "geo": geo, "line_map": line_map, "out": tmp_path / "out" / "subway_stations.csv",
            "report": tmp_path / "out" / "subway_stations.md",
            "overrides": tmp_path / "no_overrides.csv",   # 없는 파일 = 역 단위 보정 없음
            "gtxa": tmp_path / "no_gtxa.csv",             # 없는 파일 = 참조표 추가 역 없음
            "coords": tmp_path / "no_coords.csv",         # 없는 파일 = 좌표 보정 없음
            "drops": tmp_path / "no_drops.csv"}           # 없는 파일 = 행 제외 없음


def _argv(p):
    return ["--src", str(p["src"]), "--boundaries", str(p["geo"]), "--line-map", str(p["line_map"]),
            "--station-overrides", str(p["overrides"]), "--gtxa", str(p["gtxa"]),
            "--coord-overrides", str(p["coords"]), "--drops", str(p["drops"]),
            "--out", str(p["out"]), "--report", str(p["report"])]


def test_main_end_to_end(inputs, capsys):
    css.main(_argv(inputs))
    with open(inputs["out"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == COLUMNS
    got = [dict(zip(rows[0], r)) for r in rows[1:]]
    assert [(r["station_id"], r["name"], r["aliases"], r["lat"], r["data_date"]) for r in got] == [
        ("seoul7-0736", "이수", "총신대입구(이수)", "37.4852580", "2025-12-31"),
        ("metro9-4126", "언주", "", "37.4900000", ""),
    ]
    report = inputs["report"].read_text(encoding="utf-8")
    assert "GTX-A 원천 부재" in report and "총신대입구(이수)" in report
    assert "| 최종 역 행 (station_id 유일) | 2 |" in capsys.readouterr().out


def test_main_unmapped_in_scope_line_exits(inputs):
    LINE_MAP.iloc[[0, 2]].to_csv(inputs["line_map"], index=False, encoding="utf-8-sig")
    with pytest.raises(SystemExit) as e:
        css.main(_argv(inputs))
    assert e.value.code not in (0, None)
    assert "수도권 도시철도 9호선" in str(e.value.code)
    assert not inputs["out"].exists()


# --- 손으로 쓴 참조표 (data/ref) ---

def _read_ref(name):
    with open(REF / name, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_ref_tables_consistent():
    lines, groups = _read_ref("subway_line_map.csv"), _read_ref("line_groups.csv")
    assert list(lines[0]) == ["raw_line_name", "line_id", "line_group"]
    # gtfs_names 는 배차 표(build_headway.py)가 GTFS 노선명을 잇는 데 쓴다
    assert list(groups[0]) == ["line_group", "label", "color", "kakao_names", "gtfs_names"]
    raw = [r["raw_line_name"] for r in lines]
    assert len(set(raw)) == len(raw) and all(collapse_ws(s) == s for s in raw)
    assert len({r["line_id"] for r in lines}) == len(lines)
    # 매핑의 노선군마다 한 행 (GTX-A 는 원천 XLSX 에 없고 ref/gtxa_stations.csv 로 들어온다)
    assert sorted(g["line_group"] for g in groups) == sorted({r["line_group"] for r in lines} | {"GTX-A"})
    for g in groups:
        assert re.fullmatch(r"#[0-9A-F]{6}", g["color"]), g
        assert g["kakao_names"].split("|")[0] == g["line_group"], g
    # 백엔드 kakao_group 정규화(공백 제거·소문자·앞 "수도권" 제거) 뒤에도 노선군끼리 겹치지 않아야 한다
    norm = lambda s: re.sub(r"\s+", "", s).lower().removeprefix("수도권")
    owner = {}
    for g in groups:
        for n in map(norm, [g["line_group"], *g["kakao_names"].split("|")]):
            assert owner.setdefault(n, g["line_group"]) == g["line_group"], n
