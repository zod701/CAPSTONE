import json
import math

import pytest

from build_boundaries import sgg_code, sgg_name
from regions import RegionIndex

# 위도 37°에서 경도 0.001° ≈ 88.8 m, 위도 0.001° ≈ 111.2 m
#   인천(대상 밖) 126.98–126.99 | 빈틈 | 수원 127.00–127.01 | 성남 127.01–127.02   (위도 37.00–37.01)


def _feat(sido_cd, sido_nm, sgg_cd, sgg_nm, in_scope, minlon, maxlon):
    ring = [[minlon, 37.0], [maxlon, 37.0], [maxlon, 37.01], [minlon, 37.01], [minlon, 37.0]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {"sido_cd": sido_cd, "sido_nm": sido_nm, "sgg_cd": sgg_cd, "sgg_nm": sgg_nm,
                           "in_scope": in_scope, "n_dong": 1}}


@pytest.fixture
def geojson(tmp_path):
    fc = {"type": "FeatureCollection", "features": [
        # 파일 순서를 일부러 섞는다 — 동률 판정은 파일 순서가 아니라 sgg_cd 순이어야 한다
        _feat("41", "경기도", "41130", "성남시", True, 127.01, 127.02),
        _feat("41", "경기도", "41110", "수원시", True, 127.00, 127.01),
        _feat("28", "인천광역시", "28000", "인천광역시", False, 126.98, 126.99),
    ]}
    p = tmp_path / "admin_sgg.geojson"
    p.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture
def idx(geojson):
    return RegionIndex(geojson)


def test_contain(idx):
    [r] = idx.assign([127.005], [37.005])
    assert r == {"sido_cd": "41", "sido_nm": "경기도", "sgg_cd": "41110", "sgg_nm": "수원시",
                 "region_method": "contain", "snap_dist_m": None, "drop_reason": "", "detail": ""}
    assert idx.n_ties == 0


def test_neighbour_dropped(idx):
    [r] = idx.assign([126.985], [37.005])
    assert r["drop_reason"] == "outside_region"
    assert r["detail"] == "contained:인천광역시"
    assert r["region_method"] == "" and r["sgg_cd"] == "" and r["sido_cd"] == ""


def test_snap_100m(idx, geojson):
    # 수원 북쪽 변에서 위도 0.0009° ≈ 100 m
    [r] = idx.assign([127.005], [37.0109])
    assert (r["region_method"], r["sgg_cd"], r["drop_reason"]) == ("snap", "41110", "")
    assert r["snap_dist_m"] == pytest.approx(100.1, abs=0.5)
    # snap 반경을 줄이면 같은 점이 제외된다
    [r] = RegionIndex(geojson, snap_max_m=50).assign([127.005], [37.0109])
    assert (r["drop_reason"], r["detail"]) == ("outside_region", "nearest:경기도 100 m")


def test_far_and_nearest(idx):
    # 수원 북쪽 5 km(후보 상자 밖) / 2.5 km(상자 안이지만 snap 반경 밖)
    far, near = idx.assign([127.005, 127.005], [37.055, 37.0325])
    assert (far["drop_reason"], far["detail"]) == ("outside_region", "far")
    assert (near["drop_reason"], near["detail"]) == ("outside_region", "nearest:경기도 2502 m")
    assert far["region_method"] == near["region_method"] == ""


def test_nearer_neighbour_blocks_snap(idx):
    # 인천 동쪽 변에서 ≈44 m, 수원까지 ≈843 m — 인천 쪽 점이므로 경기로 snap 하지 않는다
    [r] = idx.assign([126.9905], [37.005])
    assert (r["drop_reason"], r["detail"]) == ("outside_region", "nearest:인천광역시 44 m")


def test_invalid_coord(idx):
    lons = [math.nan, 0.0, 127.0, None, "", "x", 127.005, 123.9]
    lats = [37.0, 0.0, 45.0, 37.0, 37.0, 37.0, math.inf, 37.0]
    rows = idx.assign(lons, lats)
    assert [r["drop_reason"] for r in rows] == ["invalid_coord"] * len(lons)
    assert all(r["region_method"] == "" and r["snap_dist_m"] is None for r in rows)
    rows[0]["detail"] = "x"
    assert rows[1]["detail"] == ""  # 행마다 별개 dict


def test_numeric_strings_accepted(idx):
    [r] = idx.assign(["127.005"], ["37.005"])
    assert r["region_method"] == "contain"


def test_tie_on_shared_edge_picks_lowest_sgg_cd(idx):
    rows = idx.assign([127.01, 127.015], [37.005, 37.005])
    assert (rows[0]["region_method"], rows[0]["sgg_cd"]) == ("contain", "41110")
    assert rows[0]["detail"] == "tie:41110,41130"
    assert (rows[1]["sgg_cd"], rows[1]["detail"]) == ("41130", "")
    assert idx.n_ties == 1


def test_result_aligned_with_input(idx):
    rows = idx.assign([127.015, math.nan, 126.985, 127.005], [37.005, 37.0, 37.005, 37.005])
    assert [r["sgg_cd"] or r["drop_reason"] for r in rows] == \
        ["41130", "invalid_coord", "outside_region", "41110"]


@pytest.mark.parametrize("adm_cd2, want", [
    ("4111100000", "41110"),   # 수원시장안구 → 수원시
    ("4159700000", "41590"),   # 화성시동탄구 → 화성시
    ("4115000000", "41150"),   # 일반구 없는 시
    ("1111053000", "11110"),   # 서울 종로구
])
def test_sgg_code(adm_cd2, want):
    assert sgg_code(adm_cd2) == want


def test_sgg_code_rejects_other_sido():
    with pytest.raises(ValueError):
        sgg_code("2811000000")


@pytest.mark.parametrize("sggnm, sido, want", [
    ("수원시장안구", "41", "수원시"),
    ("화성시동탄구", "41", "화성시"),
    ("성남시", "41", "성남시"),
    ("가평군", "41", "가평군"),
    ("종로구", "11", "종로구"),
])
def test_sgg_name(sggnm, sido, want):
    assert sgg_name(sggnm, sido) == want
