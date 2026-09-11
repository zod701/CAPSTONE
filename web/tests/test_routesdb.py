"""버스 노선–정류소 순서표 색인 — 손으로 쓴 합성 픽스처(tiny_db/bus_route_stops.csv)."""
from pathlib import Path

import pytest

from app.routesdb import RoutesDB

TINY = Path(__file__).parent / "fixtures" / "tiny_db"


@pytest.fixture(scope="module")
def db():
    return RoutesDB.load(TINY)


def test_route_stops_in_order_without_virtual_or_repeats(db):
    r = db.route("204000901")
    assert (r["id"], r["name"], r["source"]) == ("204000901", "380", "gyeonggi")
    # 미정차(204000103)는 빼고, 회차해 되돌아오는 판교테크노·야탑역은 처음 한 번만
    assert [s["key"] for s in r["stops"]] == ["204000401", "204000101", "204000201", "204000301"]
    assert r["stops"][0] == {"key": "204000401", "name": "야탑역", "lat": 37.3645136, "lon": 127.1195658}


def test_route_keeps_file_order_and_source_only_stops(db):
    # 순번이 비어도(3 없음) 파일 순서 그대로, 정류소 DB 에 없는 원천 정류소(in_db=0)도 넣는다
    assert [s["key"] for s in db.route("100000901")["stops"]] == ["100000201", "100000202", "100000999"]


def test_routes_at_natural_name_order(db):
    routes = db.routes_at("204000101")
    assert [(r["name"], r["type"], r["n_stops"]) for r in routes] == [("9-1", "좌석", 2), ("10", "마을", 2), ("380", "일반", 4)]
    assert routes[2] == {"id": "204000901", "name": "380", "source": "gyeonggi", "type": "일반", "n_stops": 4}


def test_old_table_without_route_type(tmp_path):
    # route_type 열이 생기기 전 순서표도 읽는다 (유형은 빈칸)
    lines = (TINY / "bus_route_stops.csv").read_text(encoding="utf-8-sig").splitlines()
    old = [",".join(c for i, c in enumerate(line.split(",")) if i not in (2, 3)) for line in lines]
    (tmp_path / "bus_route_stops.csv").write_text("\n".join(old) + "\n", encoding="utf-8")
    route = RoutesDB.load(tmp_path).route("204000901")
    assert (route["type"], [s["key"] for s in route["stops"]]) == ("", ["204000401", "204000101", "204000201", "204000301"])


def test_unknown_or_virtual(db):
    assert db.routes_at("204000103") == []  # 미정차 — 타고 내릴 수 없다
    assert db.routes_at("nope") == []
    assert db.route("nope") is None


def test_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        RoutesDB.load(tmp_path)
