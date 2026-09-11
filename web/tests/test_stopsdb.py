import shutil
from datetime import datetime
from pathlib import Path

import pytest

from app.stopsdb import StopsDB

TINY = Path(__file__).parent / "fixtures" / "tiny_db"


@pytest.fixture
def dirs(tmp_path):
    processed, ref = tmp_path / "processed", tmp_path / "ref"
    processed.mkdir()
    ref.mkdir()
    for f in ("bus_stops.csv", "subway_stations.csv"):
        shutil.copy(TINY / f, processed / f)
    shutil.copy(TINY / "line_groups.csv", ref / "line_groups.csv")
    return processed, ref


@pytest.fixture
def db(dirs):
    return StopsDB.load(*dirs)


def idx(db, kind, id_):
    col = "stop_key" if kind == "bus" else "station_id"
    return next(i for i in range(db.counts[kind]) if db.row(kind, i)[col] == id_)


def ids(db, kind, indices):
    col = "stop_key" if kind == "bus" else "station_id"
    return sorted(db.row(kind, i)[col] for i in indices)


def test_load_counts_and_row_types(db):
    assert db.counts == {"bus": 9, "subway": 4}
    r = db.row("bus", idx(db, "bus", "204000301"))
    assert isinstance(r["lat"], float) and isinstance(r["lon"], float)
    assert r["is_virtual"] is False
    assert r["alias_keys"] == {"삼평동행정복지센터"}
    assert r["ars_nos"] == "07301"  # 나머지 열은 문자열 그대로 (0 채움 유지)
    assert db.row("bus", idx(db, "bus", "204000103"))["is_virtual"] is True
    s = db.row("subway", idx(db, "subway", "shinbundang-D13"))
    assert s["is_transfer"] is True and s["is_virtual"] is False
    assert s["alias_keys"] == set()
    assert datetime.fromisoformat(db.data_mtime).utcoffset().total_seconds() == 9 * 3600


def test_init_from_rows_does_not_mutate_input():
    bus = [{"stop_key": "1", "name": "가", "name_key": "가", "aliases": "", "lat": "37.5", "lon": "127.0",
            "is_virtual": "0"}]
    db = StopsDB(bus, [], [])
    assert db.counts == {"bus": 1, "subway": 0}
    assert bus[0]["lat"] == "37.5"
    assert db.bbox("subway", 124, 33, 132, 39) == []


def test_bbox_excludes_virtual_unless_asked(db):
    box = (127.118, 37.382, 127.120, 37.383)
    assert ids(db, "bus", db.bbox("bus", *box)) == ["204000101", "204000102", "204000104"]
    assert "204000103" in ids(db, "bus", db.bbox("bus", *box, include_virtual=True))
    assert ids(db, "subway", db.bbox("subway", 127.10, 37.36, 127.12, 37.40)) == [
        "bundang-K222", "shinbundang-D12", "shinbundang-D13"]


def test_bbox_wide_range_scans_all(db):
    # 세계 전체 범위도 셀을 돌지 않고 바로 답한다
    assert len(db.bbox("bus", -180, -90, 180, 90)) == 8
    assert len(db.bbox("bus", -180, -90, 180, 90, include_virtual=True)) == 9


def test_near_sorted_and_excludes_virtual(db):
    a = db.row("bus", idx(db, "bus", "204000101"))
    hits = db.near("bus", a["lon"], a["lat"], 50)
    assert ids(db, "bus", [i for i, _ in hits]) == ["204000101", "204000102", "204000104"]
    assert [d for _, d in hits] == sorted(d for _, d in hits)
    assert hits[0][1] == pytest.approx(0.0, abs=0.01)
    with_virtual = db.near("bus", a["lon"], a["lat"], 50, include_virtual=True)
    assert db.row("bus", with_virtual[1][0])["stop_key"] == "204000103"  # 3 m


def test_by_key_and_alias_non_virtual(db):
    assert ids(db, "bus", db.by_key("bus", "분당구청")) == ["204000101", "204000102"]
    assert ids(db, "bus", db.by_alias("bus", "삼평동행정복지센터")) == ["204000301"]
    assert db.by_key("bus", "없는정류장") == []
    assert ids(db, "subway", db.by_key("subway", "정자")) == ["bundang-K222", "shinbundang-D12"]
    assert ids(db, "subway", db.by_key("subway", "판교")) == ["shinbundang-D13"]


@pytest.mark.parametrize("name, group", [
    ("수도권 1호선", "1호선"),
    ("1호선", "1호선"),
    ("수도권1호선", "1호선"),
    ("신분당선", "신분당선"),
    (" 수인분당선 ", "수인분당선"),
    ("수도권광역급행철도A", "GTX-A"),
    ("gtx-a", "GTX-A"),
    ("자기부상철도", None),
    ("", None),
    (None, None),
])
def test_kakao_group_normalization(db, name, group):
    assert db.kakao_group(name) == group


def test_group_color(db):
    assert db.group_color("신분당선") == "#D4003B"
    assert db.group_color("없는노선") is None
    assert db.group_color(None) is None


@pytest.mark.parametrize("missing", ["processed/bus_stops.csv", "processed/subway_stations.csv",
                                     "ref/line_groups.csv"])
def test_missing_csv_raises(dirs, missing):
    processed, ref = dirs
    (processed.parent / missing).unlink()
    with pytest.raises(FileNotFoundError):
        StopsDB.load(processed, ref)
