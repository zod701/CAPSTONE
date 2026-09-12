"""도시철도 역 순서표 색인 — 손으로 쓴 합성 픽스처(tiny_db/subway_line_seq.csv)와 합성 행."""
from pathlib import Path

import pytest

from app.linesdb import LinesDB

TINY = Path(__file__).parent / "fixtures" / "tiny_db"


@pytest.fixture(scope="module")
def db():
    return LinesDB.load(TINY)


def make(*chains):
    """(덩어리, 노선군, 순환, [(역 id, 실체 id)…]) → LinesDB. 이름은 역 id 를 그대로 쓴다."""
    rows = []
    for cid, group, loop, stations in chains:
        for i, (sid, phys) in enumerate(stations, 1):
            rows.append({"chain_id": cid, "line_group": group, "loop": str(int(loop)), "seq": str(i),
                         "station_id": sid, "phys_id": phys, "name": sid, "lat": "37.5", "lon": "127.0"})
    return LinesDB(rows)


def chain(cid, group, loop, *sids):
    return (cid, group, loop, [(s, s) for s in sids])


def test_load(db):
    assert db.counts == {"chains": 2, "joined": 0, "stations": 5}
    assert db.chains_for("수인분당선") == ("bundang",)
    assert db.chains_for("없는노선군") == ()
    assert set(db.chains_for()) == {"bundang", "shinbundang"}
    assert db.sequence("bundang") == ("bundang-K245", "bundang-K240", "bundang-K222")
    assert db.sequence("nope") == ()
    assert db.is_loop("bundang") is False
    assert db.station("bundang-K240") == ("능길", 37.31, 127.05)
    assert db.station("nope") is None


def test_no_join_across_line_groups(db):
    # 정자(수인분당선 K222)와 정자(신분당선 D12)는 같은 실체지만 노선군이 다르다 — 잇지 않는다
    assert db.counts["joined"] == 0


def test_joins_chains_that_continue_at_an_end():
    # 운영이 갈려 두 덩어리로 적힌 노선(8호선 별내선 ↔ 서울 8호선) — 열차는 그대로 지나간다
    db = make(chain("metro8", "8호선", False, "동구릉", "다산", "별내"),
              chain("seoul8", "8호선", False, "동구릉", "구리", "암사", "모란"))
    joined = [c for c in db.chains_for("8호선") if c not in ("metro8", "seoul8")]
    assert len(joined) == 1 and db.counts["joined"] == 1
    assert db.sequence(joined[0]) == ("별내", "다산", "동구릉", "구리", "암사", "모란")
    assert db.sequence("metro8") == ("동구릉", "다산", "별내")   # 원래 덩어리는 그대로 둔다


def test_joins_when_the_shared_station_has_two_rows():
    # 3호선 지축은 서울 3호선·일산선에 따로 적혀 있다 — 같은 실체(phys)면 잇고 한 번만 담는다
    db = make(("seoul3", "3호선", False, [("지축-s", "지축"), ("구파발", "구파발"), ("오금", "오금")]),
              ("ilsan", "3호선", False, [("지축-i", "지축"), ("삼송", "삼송"), ("대화", "대화")]))
    joined = [c for c in db.chains_for("3호선") if c not in ("seoul3", "ilsan")]
    assert db.sequence(joined[0]) == ("오금", "구파발", "지축-s", "삼송", "대화")


def test_branch_and_disconnected_chains_are_not_joined():
    db = make(chain("seoul5", "5호선", False, "방화", "강동", "하남검단산"),
              chain("seoul5:마천", "5호선", False, "강동", "마천"),          # 분기역이 본선 한가운데
              chain("gtxa:북부", "GTX-A", False, "운정중앙", "서울역"),
              chain("gtxa:남부", "GTX-A", False, "수서", "동탄"))           # 아직 안 이어진 두 노선
    assert db.counts["joined"] == 0
    assert len(db.chains_for("5호선")) == 2 and len(db.chains_for("GTX-A")) == 2


def test_loop_chain_is_never_joined():
    db = make(("seoul2", "2호선", True, [(s, s) for s in ("시청", "성수", "신도림", "충정로")]),
              chain("seoul2:성수지선", "2호선", False, "성수", "신설동"))
    assert db.counts["joined"] == 0
    assert db.is_loop("seoul2") is True


def test_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        LinesDB.load(tmp_path)
