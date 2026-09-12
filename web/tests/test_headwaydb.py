"""배차간격 표 읽기 (app.headwaydb) — tiny 픽스처."""
from pathlib import Path

import pytest

from app.headwaydb import HeadwayDB

TINY = Path(__file__).parent / "fixtures" / "tiny_db"


@pytest.fixture
def db():
    return HeadwayDB.load(TINY)


def test_counts(db):
    assert db.counts == {"bus_routes": 4, "bus_rows": 7, "rail_stations": 3, "rail_rows": 4}


def test_bus_takes_the_middle_of_min_and_max(db):
    assert db.bus("204000901", "평일") == 15.0      # 10~20 분
    assert db.bus("204000902", "평일") == 30.0      # 서울식 대표 배차 하나 (최소 = 최대)
    assert db.bus("204000901", "토요일") == 15.0


def test_empty_and_zero_are_unknown(db):
    assert db.bus("204000903", "평일") is None      # 값이 빈 행
    assert db.bus("204000902", "토요일") is None    # 그 요일 행이 없다
    assert db.bus("없는노선", "평일") is None


def test_rail_averages_both_directions(db):
    assert db.rail("bundang-K222", "수인분당선", 8) == 5.5     # 상행 6 · 하행 5
    assert db.rail("bundang-K222", "수인분당선", 9) == 10.0
    assert db.rail("bundang-K222", "수인분당선", 7) is None    # 그 시간대 행이 없다
    assert db.rail("bundang-K222", "신분당선", 8) is None      # 그 역의 다른 노선군


def test_load_without_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        HeadwayDB.load(tmp_path)


def test_load_with_only_one_table(tmp_path):
    (tmp_path / "headway_rail.csv").write_bytes((TINY / "headway_rail.csv").read_bytes())
    db = HeadwayDB.load(tmp_path)
    assert db.counts["bus_rows"] == 0 and db.counts["rail_rows"] == 4
    assert db.bus("204000901", "평일") is None
