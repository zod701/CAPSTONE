import math

import pandas as pd
import pytest

from clean_bus_stops import (COLUMNS, ars_no, clean, compatible, decide, parse_overrides, refresh, representative,
                             review_rows)
from geoutil import EARTH_R

RAW_COLUMNS = ["정류장번호", "정류장명", "위도", "경도", "정보수집일", "모바일단축번호", "도시코드", "도시명", "관리도시명"]
SEOUL = {"sido_cd": "11", "sido_nm": "서울특별시", "sgg_cd": "11680", "sgg_nm": "강남구"}
GYEONGGI = {"sido_cd": "41", "sido_nm": "경기도", "sgg_cd": "41130", "sgg_nm": "성남시"}
NONE = {"sido_cd": "", "sido_nm": "", "sgg_cd": "", "sgg_nm": ""}
SEOUL_LON, GG_LON, OUT_LON = 126.9, 127.1, 127.6


def stub_assign(lons, lats):
    """경도로 가르는 가짜 판정: < 127.0 서울, < 127.4 경기, < 127.5 경기 snap, 그 밖은 강원(인접 시도)."""
    out = []
    for lon, lat in zip(lons, lats):
        if not (math.isfinite(lon) and math.isfinite(lat)):
            out.append({**NONE, "region_method": "", "snap_dist_m": None, "drop_reason": "invalid_coord",
                        "detail": ""})
        elif lon < 127.5:
            region, method, snap = ((SEOUL, "contain", None) if lon < 127.0 else
                                    (GYEONGGI, "contain", None) if lon < 127.4 else (GYEONGGI, "snap", 123.4))
            out.append({**region, "region_method": method, "snap_dist_m": snap, "drop_reason": "", "detail": ""})
        else:
            out.append({**NONE, "region_method": "", "snap_dist_m": None, "drop_reason": "outside_region",
                        "detail": "contained:강원특별자치도"})
    return out


def north(lat, m):
    return lat + math.degrees(m / EARTH_R)


def rec(sid, name, lon, lat, ars="0", city=None):
    city = city or ("서울특별시" if lon < 127.0 else "경기도 성남시")
    return {"정류장번호": sid, "정류장명": name, "위도": f"{lat:.7f}" if lat is not None else "",
            "경도": f"{lon:.7f}", "정보수집일": "2025-10-31", "모바일단축번호": ars, "도시코드": "",
            "도시명": city, "관리도시명": ""}


def pair(suffix, g_name, s_name, dist_m, lon=SEOUL_LON, lat=37.5, g_ars="0", s_ars="0"):
    """GGB·SEB 쌍 — SEB 가 GGB 에서 dist_m 북쪽."""
    return [rec(f"GGB{suffix}", g_name, lon, lat, g_ars), rec(f"SEB{suffix}", s_name, lon, north(lat, dist_m), s_ars)]


def run(records, overrides=None):
    out, info = clean(pd.DataFrame(records, columns=RAW_COLUMNS), stub_assign, overrides)
    return out.set_index("stop_key", drop=False), info


def test_near_pair_merged_without_name_check():
    out, info = run(pair("100000001", "보훈병원", "농협둔촌동지점", 5, g_ars="25190", s_ars="25190"))
    assert list(out.columns) == COLUMNS
    r = out.loc["100000001"]
    assert (r["merge_status"], r["rep_source"], r["name"], r["aliases"]) == \
        ("merged_near", "SEB", "농협둔촌동지점", "보훈병원")
    assert (r["source_ids"], r["ars_nos"], r["twin_dist_m"]) == ("GGB100000001|SEB100000001", "25190", "5.0")
    assert r["lat"] == f"{north(37.5, 5):.7f}" and r["lon"] == "126.9000000"  # 대표(SEB) 좌표
    assert (r["sgg_nm"], r["raw_city_label"], r["is_virtual"]) == ("강남구", "서울특별시", 0)
    assert len(out) == 1


def test_far_compatible_truncated_name_takes_longer_name():
    # 서울이라 대표는 SEB 인데 SEB 이름이 잘려 있다 → 이름은 GGB 것, 잘린 이름은 alias
    out, _ = run(pair("100000002", "서울숲.성수아트홀", "서울숲.성수아", 120))
    r = out.loc["100000002"]
    assert (r["merge_status"], r["rep_source"]) == ("merged_far", "SEB")
    assert (r["name"], r["aliases"], r["name_key"]) == ("서울숲.성수아트홀", "서울숲.성수아", "서울숲성수아트홀")
    assert r["lat"] == f"{north(37.5, 120):.7f}"


def test_far_incompatible_pair_split_keys():
    out, info = run(pair("123456789", "개포1단지", "개현초등학교", 200))
    assert sorted(out.index) == ["123456789", "123456789-GGB"]
    seb, ggb = out.loc["123456789"], out.loc["123456789-GGB"]
    assert (seb["source_ids"], seb["rep_source"], seb["name"]) == ("SEB123456789", "SEB", "개현초등학교")
    assert (ggb["source_ids"], ggb["rep_source"], ggb["name"]) == ("GGB123456789", "GGB", "개포1단지")
    assert seb["merge_status"] == ggb["merge_status"] == "split"
    assert seb["aliases"] == ggb["aliases"] == ""
    assert [p["decision"] for p in info["pairs"]] == ["split"]


def test_near_threshold_uses_unrounded_distance():
    # 50.04 m 는 0.1 m 반올림하면 50.0 이지만 판정은 원값으로 → 이름이 달라 분리
    out, info = run(pair("100000009", "가나다", "라마바", 50.04))
    assert [p["decision"] for p in info["pairs"]] == ["split"]
    assert out.loc["100000009", "twin_dist_m"] == "50.0"


def test_representative_is_location_owner():
    recs = (pair("100000003", "강남역", "강남역", 10)
            + pair("204000003", "성남시청", "성남시청", 10, lon=GG_LON, g_ars="4282", s_ars="0"))
    out, _ = run(recs)
    seoul, gg = out.loc["100000003"], out.loc["204000003"]
    assert (seoul["rep_source"], seoul["raw_city_label"]) == ("SEB", "서울특별시")
    assert (gg["rep_source"], gg["raw_city_label"], gg["lat"]) == ("GGB", "경기도 성남시", "37.5000000")
    assert gg["ars_nos"] == "04282"


@pytest.mark.parametrize("ggb, seb, keep, want", [
    ("11", "11", "", "SEB"),
    ("41", "41", "", "GGB"),
    ("41", "11", "", "GGB"),   # GGB 판정이 기준
    ("11", "41", "", "SEB"),
    ("", "11", "", "SEB"),     # GGB 가 대상 밖이면 SEB 판정으로
    ("", "41", "", "SEB"),     # 규칙상 GGB 지만 대상 밖 → 대상 안인 짝
    ("11", "", "", "GGB"),     # 규칙상 SEB 지만 대상 밖 → 대상 안인 짝
    ("11", "11", "GGB", "GGB"),
])
def test_representative_rule(ggb, seb, keep, want):
    assert representative(ggb, seb, keep) == want


def test_boundary_pair_keeps_in_scope_member():
    # GGB 는 인접 시도(강원) 쪽, SEB 는 경기 쪽 — 2 m 떨어진 한 정류소를 통째로 잃지 않는다
    recs = [rec("GGB300000001", "경계정류장", 127.50001, 37.5), rec("SEB300000001", "경계정류장", 127.49999, 37.5)]
    out, info = run(recs)
    r = out.loc["300000001"]
    assert (r["rep_source"], r["sgg_nm"], r["region_method"], r["merge_status"]) == ("SEB", "성남시", "snap", "merged_near")
    assert r["snap_dist_m"] == "123.4"
    assert r["source_ids"] == "GGB300000001|SEB300000001"
    assert len(info["dropped"]) == 0  # GGB 레코드는 대상 밖이지만 병합 행의 source_ids 에 남는다
    assert ("대상 밖이지만 대상 안 짝과 병합되어 남은 레코드", 1) in info["steps"]


def test_override_keep_out_of_scope_member_fails():
    recs = [rec("GGB300000002", "경계정류장", 127.50001, 37.5), rec("SEB300000002", "경계정류장", 127.49999, 37.5)]
    with pytest.raises(ValueError, match="override keep"):
        run(recs, {"300000002": ("merge", "GGB")})


def test_ars_no():
    assert [ars_no(s) for s in ("4282", "0", "", "25190", " 123 ")] == ["04282", "", "", "25190", "00123"]


def test_ars_nos_distinct_rep_first():
    out, _ = run(pair("204000001", "정류장", "정류장", 3, lon=GG_LON, g_ars="5008", s_ars="48087")
                 + pair("100000001", "정류장", "정류장", 3, g_ars="0", s_ars="0"))
    assert out.loc["204000001", "ars_nos"] == "05008|48087"   # 경기 → 대표 GGB 먼저
    assert out.loc["100000001", "ars_nos"] == ""


@pytest.mark.parametrize("a, b, want", [
    ("판교역", "판교역", True),
    ("종로2가(중)", "종로2가", True),
    ("고등동행정복지센터.판교밸리자이아파트", "고등동행정복지센터.판교밸리자", True),
    ("개포1단지", "개현초등학교", False),
    ("AB", "ABC", False),        # 짧은 쪽이 3자 미만이면 접두 호환 아님
    ("ABC", "abcd", True),
])
def test_compatible(a, b, want):
    assert compatible(a, b) is want


@pytest.mark.parametrize("dist, a, b, action, want", [
    (50.0, "가나다", "라마바", "", "merged_near"),
    (50.01, "가나다", "가나다", "", "merged_far"),
    (300.0, "가나다", "가나다라", "", "merged_far"),
    (300.01, "가나다", "가나다", "", "split"),
    (120.0, "가나다", "라마바", "", "split"),
    (5.0, "가나다", "가나다", "split", "override_split"),
    (900.0, "가나다", "라마바", "merge", "override_merge"),
])
def test_decide(dist, a, b, action, want):
    assert decide(dist, a, b, action) == want


def test_overrides_merge_split_keep():
    recs = (pair("100000010", "개포1단지", "개현초등학교", 200)     # 원래 split → 강제 병합
            + pair("100000011", "강남역", "강남역", 5)              # 원래 병합 → 강제 분리
            + pair("100000012", "역삼역", "역삼역", 5)              # 서울이지만 GGB 를 대표로
            + pair("100000013", "선릉역", "선릉역", 5))             # 분리 + GGB 가 맨 suffix
    overrides = {"100000010": ("merge", ""), "100000011": ("split", ""), "100000012": ("merge", "GGB"),
                 "100000013": ("split", "GGB")}
    out, info = run(recs, overrides)
    assert out.loc["100000010", "merge_status"] == "override_merge"
    assert out.loc["100000011", "merge_status"] == out.loc["100000011-GGB", "merge_status"] == "split"
    assert out.loc["100000011", "source_ids"] == "SEB100000011"
    assert (out.loc["100000012", "rep_source"], out.loc["100000012", "lat"]) == ("GGB", "37.5000000")
    assert out.loc["100000013", "source_ids"] == "GGB100000013"
    assert out.loc["100000013-SEB", "source_ids"] == "SEB100000013"
    assert {p["stop_key"]: p["decision"] for p in info["pairs"]} == {
        "100000010": "override_merge", "100000011": "override_split", "100000012": "override_merge",
        "100000013": "override_split"}


def test_override_must_name_a_pair():
    with pytest.raises(ValueError, match="쌍이 아닙니다"):
        run([rec("GGB100000020", "단독", SEOUL_LON, 37.5)], {"100000020": ("split", "")})


def test_parse_overrides():
    df = pd.DataFrame([{"stop_key": "100000010", "action": "merge", "keep": "", "note": "검토"},
                       {"stop_key": "100000011", "action": "split", "keep": "SEB", "note": ""}])
    assert parse_overrides(df) == {"100000010": ("merge", ""), "100000011": ("split", "SEB")}
    assert parse_overrides(pd.DataFrame(columns=["stop_key", "action", "keep", "note"])) == {}
    for bad in ({"action": "drop"}, {"keep": "ICB"}):
        with pytest.raises(ValueError):
            parse_overrides(pd.DataFrame([{"stop_key": "1", "action": "merge", "keep": "", "note": "", **bad}]))
    with pytest.raises(ValueError, match="중복"):
        parse_overrides(pd.concat([df, df]))


def test_updown_pair_survives_merge():
    # 광교대광로제비앙 상·하행: 서로 다른 suffix, 30 m — 병합 대상이 아니다
    recs = [rec("GGB203000317", "광교대광로제비앙", GG_LON, 37.3),
            rec("GGB203000318", "광교대광로제비앙", GG_LON, north(37.3, 30))]
    out, info = run(recs)
    assert sorted(out.index) == ["203000317", "203000318"]
    assert (out["merge_status"] == "single").all()
    n_rows_before, n_suffix_before, n_after = info["updown"]
    assert n_rows_before == n_suffix_before == n_after == 1


def test_updown_double_registered_counts_rows_and_suffix_pairs():
    # 두 정류소가 모두 GGB·SEB 이중 등록 → 병합 전 행 쌍 4, suffix 쌍 1, 병합 후 1
    recs = pair("100000030", "상행하행", "상행하행", 1) + pair("100000031", "상행하행", "상행하행", 1, lat=north(37.5, 30))
    out, info = run(recs)
    assert len(out) == 2
    assert info["updown"] == (4, 1, 1)


def test_drops_other_bis_invalid_and_residual():
    recs = [rec("GGB400000001", "강원쪽", OUT_LON, 37.5),                 # 인접 시도 → 제외
            rec("GGB400000002", "좌표없음", SEOUL_LON, None),             # 좌표 무효 → 제외
            rec("ICB400000003", "인천BIS", SEOUL_LON, 37.5),             # 타 BIS → 세기만
            rec("GGB400000004", "동명", GG_LON, 37.2), rec("GGB400000005", "동명", GG_LON, north(37.2, 2)),
            rec("GGB277100001", "차고지(미정차)", GG_LON, 37.1)]
    out, info = run(recs)
    assert sorted(out.index) == ["277100001", "400000004", "400000005"]
    assert out.loc["277100001", "is_virtual"] == 1
    assert dict(zip(info["dropped"]["정류장번호"], info["dropped"]["drop_reason"])) == {
        "GGB400000001": "outside_region", "GGB400000002": "invalid_coord"}
    assert info["others"]["정류장번호"].tolist() == ["ICB400000003"]
    assert len(info["residual"]) == 1   # 2 m 동명 쌍 — 검토용으로만 보고


@pytest.mark.parametrize("blank", ["GGB", "SEB"])
def test_pair_with_invalid_coord_member_becomes_single(blank):
    # 짝 한쪽 좌표가 비면 거리를 잴 수 없다 — 20,015 km 짜리 split 이 아니라 남은 쪽이 단독 정류소
    lat = {"GGB": 37.5, "SEB": 37.5, blank: None}
    recs = [rec("GGB100000050", "가", SEOUL_LON, lat["GGB"]), rec("SEB100000050", "가", SEOUL_LON, lat["SEB"])]
    out, info = run(recs)
    live = "SEB" if blank == "GGB" else "GGB"
    r = out.loc["100000050"]
    assert (r["merge_status"], r["source_ids"], r["twin_dist_m"]) == ("single", f"{live}100000050", "")
    assert len(out) == 1
    assert info["dropped"]["정류장번호"].tolist() == [f"{blank}100000050"]
    assert review_rows(info["pairs"]).empty


def test_suffix_must_be_nine_digits_and_unique():
    with pytest.raises(ValueError, match="숫자"):
        run([rec("GGB12345678X", "가", SEOUL_LON, 37.5)])
    with pytest.raises(ValueError, match="중복"):
        run([rec("GGB123456789", "가", SEOUL_LON, 37.5), rec("GGB123456789", "나", SEOUL_LON, 37.6)])


def test_review_rows_only_far_pairs():
    _, info = run(pair("100000040", "가나다", "가나다", 10) + pair("100000041", "가나다라", "가나다", 150)
                  + pair("100000042", "개포1단지", "개현초등학교", 200))
    rv = review_rows(info["pairs"])
    assert rv["stop_key"].tolist() == ["100000042", "100000041"]   # 먼 순, 50 m 이하는 빠진다
    assert rv["names_compatible"].tolist() == [0, 1]
    assert rv["decision"].tolist() == ["split", "merged_far"]
    assert rv.iloc[0]["dist_m"] == "200.0"


# --- 최신 정류소 파일로 교체 (refresh) ---

NATIONAL = pd.DataFrame([
    rec("GGB200000001", "옛이름", GG_LON, 37.5), rec("GGB200000002", "폐지된곳", GG_LON, 37.6),
    rec("SEB100000001", "서울역앞", SEOUL_LON, 37.5), rec("SEB100000002", "없어진곳", SEOUL_LON, 37.6),
    rec("SEB200000003", "경기소재", GG_LON, 37.7), rec("ICB300000001", "인천정류소", GG_LON, 37.4, city="인천광역시"),
], columns=RAW_COLUMNS)
GBIS_STATIONS = pd.DataFrame([("200000001", "24001", "새이름", "N", "성남", f"{GG_LON + 0.001:.7f}", "37.5000000"),
                              ("200000009", "", "새경기정류소", "N", "성남", f"{GG_LON:.7f}", "37.8000000")],
                             columns=["stationId", "mobileNo", "stationName", "centerYn", "regionName", "x", "y"])
SEOUL_STATIONS = pd.DataFrame([("100000001", "01001", "서울역앞", "126.9000000", "37.5000000", "중앙차로"),
                               ("100000009", "00009", "한강버스.어딘가선착장", "126.9500000", "37.5200000", "한강선착장"),
                               ("100000010", "01010", "새서울정류소", "126.9100000", "37.5500000", "일반차로")],
                              columns=["NODE_ID", "ARS_ID", "정류소명", "X좌표", "Y좌표", "정류소타입"])


def test_refresh_replaces_adds_and_drops():
    out, steps, moved = refresh(NATIONAL, SEOUL_STATIONS, GBIS_STATIONS, stub_assign)
    rows = out.set_index("정류장번호")
    assert sorted(rows.index) == ["GGB200000001", "GGB200000009", "ICB300000001",   # 타 BIS 는 그대로
                                  "SEB100000001", "SEB100000010", "SEB200000003"]    # 경기 소재 SEB 는 서울 파일 밖 → 유지
    # GGB200000002(GBIS 에 없음) · SEB100000002(서울 소재인데 서울 파일에 없음) = 폐지, 배 선착장은 뺀다
    assert rows.at["GGB200000001", "정류장명"] == "새이름" and rows.at["GGB200000001", "모바일단축번호"] == "24001"
    assert rows.at["GGB200000001", "도시명"] == "경기도 성남시"                   # 도시명은 전국 원본 것을 잇는다
    assert rows.at["GGB200000009", "도시명"] == "" and rows.at["SEB100000010", "정류장명"] == "새서울정류소"
    assert list(out.columns) == RAW_COLUMNS and moved == []
    s = dict(steps)
    assert (s["SEB 서울 정류소 파일 중 배 선착장 — 뺌"], s["GGB 둘 다 있음 → 최신 값"], s["GGB   그중 이름 키가 바뀜"]) == (1, 1, 1)
    assert (s["GGB 최신 파일에만 있음 (새 정류소)"], s["GGB 전국 원본에만 → 폐지로 보고 뺌"]) == (1, 1)
    assert (s["SEB 전국 원본에만 · 서울 소재 → 폐지로 보고 뺌"],
            s["SEB 전국 원본에만 · 서울 밖 → 전국 원본 유지 (서울 파일이 담지 않는 범위)"]) == (1, 1)
    clean(out, stub_assign)  # 교체한 레코드가 정제 규칙을 그대로 통과한다


def test_refresh_coord_override():
    fix = pd.DataFrame([("SEB100000001", "서울역앞", "126.9010000", "37.5000000", "전국 원본 좌표", "")],
                       columns=["source_id", "name", "lon", "lat", "method", "checks"])
    out, _, moved = refresh(NATIONAL, SEOUL_STATIONS, GBIS_STATIONS, stub_assign, fix)
    r = out.set_index("정류장번호").loc["SEB100000001"]
    assert (r["경도"], r["위도"]) == ("126.9010000", "37.5000000")
    assert [m[0] for m in moved] == ["SEB100000001"] and 85 < moved[0][4] < 95
    with pytest.raises(ValueError, match="이름이 원천과 다릅니다"):
        refresh(NATIONAL, SEOUL_STATIONS, GBIS_STATIONS, stub_assign, fix.assign(name="다른이름"))
    with pytest.raises(ValueError, match="어느 레코드에도"):
        refresh(NATIONAL, SEOUL_STATIONS, GBIS_STATIONS, stub_assign, fix.assign(source_id="SEB100000002"))
