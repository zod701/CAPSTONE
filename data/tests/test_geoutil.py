import math

import pytest

from geoutil import GridIndex, haversine_m, point_polyline_m


def test_haversine_known_distance():
    # 위도 0.001도 ≈ 111.2 m
    assert haversine_m(127.0, 37.5, 127.0, 37.501) == pytest.approx(111.19, abs=0.5)
    assert haversine_m(127.0, 37.5, 127.0, 37.5) == 0


def test_haversine_nan_propagates():
    # 반지구 거리(20,015 km)가 아니라 nan 이어야 호출자가 무효 좌표를 알아챈다
    assert math.isnan(haversine_m(float("nan"), float("nan"), 127.0, 37.5))


def test_point_polyline():
    line = [[127.0, 37.5], [127.001, 37.5]]
    # 선분 중앙에서 북쪽 0.0001도 ≈ 11.1 m
    assert point_polyline_m(127.0005, 37.5001, line) == pytest.approx(11.1, abs=0.3)
    # 선분 끝 너머는 끝점까지 거리
    assert point_polyline_m(127.002, 37.5, line) == pytest.approx(88.3, abs=1.0)
    assert point_polyline_m(127.0, 37.5, []) == math.inf
    assert point_polyline_m(127.0, 37.5, [[127.0, 37.5]]) == 0


def test_grid_bbox_and_near():
    lons = [127.0, 127.0003, 127.02, 126.99]
    lats = [37.5, 37.5, 37.5, 37.49]
    g = GridIndex(lons, lats)
    assert sorted(g.bbox(126.999, 37.499, 127.001, 37.501)) == [0, 1]
    hits = g.near(127.0, 37.5, 50)
    assert [i for i, _ in hits] == [0, 1]
    assert hits[1][1] == pytest.approx(26.5, abs=1.0)
    assert g.near(127.0, 37.5, 5) == [(0, 0.0)]


def test_grid_bbox_huge_flat_box_scans_points():
    # 면적 0 이어도 셀이 수십억 개인 박스 — 셀을 돌지 않고 점을 훑어야 즉시 끝난다
    g = GridIndex([127.0, 127.0003, 127.02, 126.99], [37.5, 37.5, 37.5, 37.49])
    assert sorted(g.bbox(0.0, 37.5, 1e9, 37.5)) == [0, 1, 2]
    assert g.bbox(-180.0, -90.0, 180.0, 90.0) == [0, 1, 2, 3]


def test_grid_cell_boundary():
    # 셀 경계를 사이에 둔 두 점도 반경 질의에 함께 잡혀야 한다
    g = GridIndex([126.99999, 127.00001], [37.5, 37.5])
    assert len(g.near(127.0, 37.5, 10)) == 2


def test_grid_length_mismatch():
    with pytest.raises(ValueError):
        GridIndex([1.0], [])
