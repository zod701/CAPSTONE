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


def test_routes_by_name(db):
    assert db.routes_by_name("380") == ("204000901",)
    assert db.routes_by_name(" 9-1 ") == ("204000903",)   # 카카오 차량 이름의 공백은 무시한다
    assert db.routes_by_name("9") == ("100000901",)       # '9-1' 과 다른 노선
    assert db.routes_by_name("없는노선") == () and db.routes_by_name(None) == ()


def test_routes_by_name_falls_back_to_loose_name(db):
    # 카카오는 요일별 운행을 '11-A(평일 출퇴근)' 처럼 갈라 부르지만 순서표에는 노선 하나뿐이다
    assert db.routes_by_name("380(주말)") == ("204000901",)
    assert db.routes_by_name("10-A(평일 출퇴근)") == ("204000902",)
    assert db.routes_by_name("9-1") == ("204000903",)   # 그대로 맞는 이름이 먼저다
    assert db.routes_by_name("380-Z") == ("204000901",)


def test_sequence_and_stop(db):
    assert db.sequence("204000901") == ("204000401", "204000101", "204000201",
                                        "204000301", "204000201", "204000401")
    assert db.sequence("nope") == ()
    assert db.stop("100000999") == ("새정류소", 37.573, 126.97)   # 정류소 DB 에 없는 정류소도 있다
    assert db.stop("nope") is None


def test_order_of_marks_first_and_last_pass(db):
    o = db.order_of("204000901")
    assert o["204000401"] == (0, 5)   # 회차해 되돌아오는 정류장 — 처음과 마지막 순서가 다르다
    assert (o["204000101"], o["204000201"], o["204000301"]) == ((1, 1), (2, 4), (3, 3))
    assert "204000103" not in o       # 미정차
    assert db.order_of("nope") == {}


def test_unknown_or_virtual(db):
    assert db.routes_at("204000103") == []  # 미정차 — 타고 내릴 수 없다
    assert db.routes_at("nope") == []
    assert db.route("nope") is None


def test_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        RoutesDB.load(tmp_path)
