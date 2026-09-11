"""도시철도 역 순서표 — 합성 데이터만 쓴다."""
import pandas as pd
import pytest

from build_subway_seq import (_tok, apply_fixes, build, components, edges, gtxa_chains, norm_no, parse_composition,
                              select_lines)

LINE_MAP = pd.DataFrame([("가선", "ga", "가"), ("나선", "na", "나"), ("가지선", "gaji", "가"), ("GTX-A", "gtxa", "GTX-A")],
                        columns=["raw_line_name", "line_id", "line_group"], dtype=str)
SEOUL, INCHEON = "서울특별시 중구 어딘가 1", "인천광역시 중구 어딘가 1"


def _db(*rows):
    """(station_id, name, line_raw, line_group, lon, lat)"""
    return pd.DataFrame(list(rows), columns=["station_id", "name", "line_raw", "line_group", "lon", "lat"], dtype=str)


def _raw(*rows):
    return pd.DataFrame(list(rows), columns=["역사명", "역사도로명주소"], dtype=str)


def _src(*rows):
    """(노선번호, 노선명, 정거장구성)"""
    return pd.DataFrame([(*r, "2026-02-28") for r in rows], columns=["노선번호", "노선명", "정거장구성", "데이터기준일자"],
                        dtype=str)


def _lines(*rows):
    return pd.DataFrame([(*r, "") for r in rows], columns=["src_line_no", "src_line_name", "line_raw", "chain_id", "note"],
                        dtype=str)


def _fixes(*rows):
    return pd.DataFrame(list(rows), columns=["chain_id", "action", "target", "value", "branch", "note"], dtype=str)


NO_FIX = _fixes()


# --- 정거장구성 해석 ---

@pytest.mark.parametrize("s, want", [
    ('"A01-서울,A02-공덕,\nA03-홍대입구"', [("A01", "서울"), ("A02", "공덕"), ("A03", "홍대입구")]),  # 따옴표·줄바꿈
    ("D04-신사+D05-논현", [("D04", "신사"), ("D05", "논현")]),                                          # + 구분자
    ("211-1-용답, 211-2-신답", [("211-1", "용답"), ("211-2", "신답")]),                                  # 지선 번호
    ("S112-4.19민주묘지,S113-가오리", [("S112", "4.19민주묘지"), ("S113", "가오리")]),                   # 이름 속 숫자·점
    ("1001-서울역, ,1002-남영역,", [("1001", "서울역"), ("1002", "남영역")]),                           # 빈 토큰
])
def test_parse_composition(s, want):
    assert parse_composition(s) == (want, [])


def test_parse_composition_bad_token():
    assert parse_composition("1-가,이름만") == ([("1", "가")], ["이름만"])


@pytest.mark.parametrize("a, b", [("0405", "405"), ("D004", "D04"), ("211-1", "2111"), ("G100", "g100")])
def test_norm_no(a, b):
    assert norm_no(a) == norm_no(b)


# --- 보정 ---

def _chains(*names, cid="ga"):
    return {cid: {"meta": {"chain_id": cid, "chain_name": "가선", "line_raw": "가선", "line_group": "가", "loop": 0},
                  "tokens": [_tok(n, "comp", str(i), n) for i, n in enumerate(names, 1)]}}


def _names(chains, cid="ga"):
    return [(t["name"], t["source"]) for t in chains[cid]["tokens"]]


def test_fix_rename_insert_move_loop():
    ch = _chains("가산역", "옛이름", "라", "다")
    log = apply_fixes(ch, [
        {"chain_id": "ga", "action": "rename", "target": "옛이름", "value": "나", "branch": ""},
        {"chain_id": "ga", "action": "move_before", "target": "다", "value": "라", "branch": ""},
        {"chain_id": "ga", "action": "insert_before", "target": "가산", "value": "시작", "branch": ""},  # 이름 키로 (가산역 = 가산)
        {"chain_id": "ga", "action": "insert_after", "target": "라", "value": "마|바", "branch": ""},
        {"chain_id": "ga", "action": "loop", "target": "", "value": "", "branch": ""},
    ])
    assert _names(ch) == [("시작", "added"), ("가산역", "comp"), ("나", "renamed"), ("다", "moved"), ("라", "comp"),
                          ("마", "added"), ("바", "added")]
    assert ch["ga"]["meta"]["loop"] == 1 and len(log) == 5


def test_fix_branch_takes_listed_stations_and_adds_missing():
    ch = _chains("가", "지선1", "나", "다", "지선2")
    apply_fixes(ch, [{"chain_id": "ga", "action": "branch", "target": "나", "value": "지선1|지선2|새역", "branch": "끝"}])
    assert _names(ch) == [("가", "comp"), ("나", "comp"), ("다", "comp")]
    assert _names(ch, "ga:끝") == [("나", "junction"), ("지선1", "comp"), ("지선2", "comp"), ("새역", "added")]
    assert ch["ga:끝"]["meta"]["chain_name"] == "가선 · 끝 지선" and ch["ga:끝"]["meta"]["loop"] == 0


@pytest.mark.parametrize("fix, msg", [
    ({"chain_id": "ga", "action": "rename", "target": "없는역", "value": "나", "branch": ""}, "목록에 없습니다"),
    ({"chain_id": "zz", "action": "loop", "target": "", "value": "", "branch": ""}, "chain_id"),
    ({"chain_id": "ga", "action": "swap", "target": "가", "value": "나", "branch": ""}, "알 수 없는 보정"),
    ({"chain_id": "ga", "action": "move_before", "target": "가", "value": "없는역", "branch": ""}, "기준 역"),
    ({"chain_id": "ga", "action": "branch", "target": "가", "value": "나", "branch": ""}, "지선 이름"),
    ({"chain_id": "ga", "action": "insert_after", "target": "가", "value": "", "branch": ""}, "value"),
])
def test_fix_errors(fix, msg):
    with pytest.raises(ValueError, match=msg):
        apply_fixes(_chains("가", "나"), [fix])


# --- 노선정보 행 고르기 ---

def test_select_lines_by_number_and_collapsed_name():
    src = _src(("X1", "수도권  가선", "1-가"), ("X1", "가지선", "9-지"), ("Y2", "나선", "1-나"))
    picked = select_lines(src, _lines(("X1", "수도권 가선", "가선", "ga"), ("X1", "가지선", "가지선", "gaji")))
    assert [s["정거장구성"] for _, s in picked] == ["1-가", "9-지"]
    with pytest.raises(ValueError, match="0개"):
        select_lines(src, _lines(("Z9", "없는선", "가선", "ga")))


# --- 전체 ---

# 가선: 가(126.90) 나(126.91) 다(126.92) — 나선: 라(126.925, 다 옆 다른 노선), 멀리(128.5, 가선 '마'와 같은 이름 · 동명이역)
DB = _db(("ga-1", "가", "가선", "가", "126.90", "37.50"), ("ga-2", "나루역", "가선", "가", "126.91", "37.50"),
         ("ga-3", "다", "가선", "가", "126.92", "37.50"), ("na-1", "라", "나선", "나", "126.925", "37.50"),
         ("na-2", "다", "나선", "나", "126.9201", "37.5001"), ("na-9", "마", "나선", "나", "128.50", "36.00"),
         ("ga-9", "외딴역", "가선", "가", "127.30", "37.30"))
RAW = _raw(("바깥", INCHEON), ("양원", SEOUL), ("마", SEOUL))


def test_build_resolution_order_drop_missing_and_phys():
    src = _src(("X1", "가선", "1-가,2-나루,3-다,4-마,5-양원,6-바깥"))
    fixes = _fixes(("ga", "insert_after", "다", "라", "", ""))
    out, info = build(src, _lines(("X1", "가선", "가선", "ga")), fixes, DB, RAW, LINE_MAP)
    rows = out[["seq", "station_id", "name", "status", "source"]].values.tolist()
    assert rows == [
        [1, "ga-1", "가", "db", "comp"], [2, "ga-2", "나루역", "db", "comp"], [3, "ga-3", "다", "db", "comp"],
        [4, "na-1", "라", "db", "added"],         # 가선에 없는 역 → 이웃 8 km 안의 다른 노선 역
        [5, "", "마", "missing", "comp"],         # 같은 이름 na-9 는 200 km 밖 (동명이역) → 원천 주소가 서울 → 이름만
        [6, "", "양원", "missing", "comp"],       # 역 DB 에 없고 원천 주소가 서울 (좌표 결함)
    ]                                             # 바깥(인천)은 끝에서 뺐다
    assert info["dropped"] == {"ga": ["바깥"]} and info["interior"] == []
    assert info["how"] == {"line": 3, "other": 1, "missing": 2}
    # 같은 이름 + 300 m 안 = 한 물리 역: 가선 다(ga-3)와 나선 다(na-2) → 대표 id 는 둘 중 작은 것
    assert out.loc[out["station_id"] == "ga-3", "phys_id"].item() == "ga-3"
    assert out.loc[out["name"] == "양원", "phys_id"].item() == "missing:양원"
    assert sorted(info["uncovered"]["station_id"]) == ["ga-9", "na-2", "na-9"]


def test_build_interior_drop_is_reported():
    src = _src(("X1", "가선", "1-가,2-바깥,3-나루"))
    _, info = build(src, _lines(("X1", "가선", "가선", "ga")), NO_FIX, DB, RAW, LINE_MAP)
    assert info["interior"] == [("ga", "바깥")]


def test_build_unresolved_name_fails():
    src = _src(("X1", "가선", "1-가,2-옛이름"))
    with pytest.raises(ValueError, match="rename"):
        build(src, _lines(("X1", "가선", "가선", "ga")), NO_FIX, DB, RAW, LINE_MAP)


def test_build_rejects_bad_ref():
    src = _src(("X1", "가선", "1-가"))
    with pytest.raises(ValueError, match="chain_id 가 겹칩니다"):
        build(src, _lines(("X1", "가선", "가선", "ga"), ("X1", "가선", "가선", "ga")), NO_FIX, DB, RAW, LINE_MAP)
    with pytest.raises(ValueError, match="subway_line_map"):
        build(src, _lines(("X1", "가선", "없는선", "ga")), NO_FIX, DB, RAW, LINE_MAP)
    with pytest.raises(ValueError, match="해석하지 못한"):
        build(_src(("X1", "가선", "1-가,이름만")), _lines(("X1", "가선", "가선", "ga")), NO_FIX, DB, RAW, LINE_MAP)


def test_edges_loop_and_components():
    src = _src(("X1", "가선", "1-가,2-나루,3-다"), ("X2", "가지선", "9-다,8-외딴역"))
    lines = _lines(("X1", "가선", "가선", "ga"), ("X2", "가지선", "가지선", "gaji"))
    out, _ = build(src, lines, _fixes(("ga", "loop", "", "", "", "")), DB, RAW, LINE_MAP)
    pairs = [(c, a["name"], b["name"]) for c, a, b in edges(out)]
    assert pairs == [("ga", "가", "나루역"), ("ga", "나루역", "다"), ("ga", "다", "가"), ("gaji", "다", "외딴역")]
    # 가지선의 '다' 는 같은 노선군(가) 의 ga-3 로 찾아져 두 순서가 한 덩어리
    assert [len(c) for c in components(out)["가"]] == [4]


def test_gtxa_segments_are_separate_chains():
    # 북부·남부는 이어지지 않았다 — 구간마다 따로, seq 순
    g = pd.DataFrame([("X109", "성남", "south", "2"), ("X108", "수서", "south", "1"), ("N02", "킨텍스", "north", "2"),
                      ("N01", "운정중앙", "north", "1")], columns=["station_no", "name", "segment", "seq"], dtype=str)
    segs = gtxa_chains(g)
    assert {k: [t["name"] for t in v] for k, v in segs.items()} == {"north": ["운정중앙", "킨텍스"], "south": ["수서", "성남"]}
    db = _db(("gtxa-N01", "운정중앙", "GTX-A", "GTX-A", "126.73", "37.72"), ("gtxa-N02", "킨텍스", "GTX-A", "GTX-A", "126.75", "37.67"),
             ("gtxa-X108", "수서", "GTX-A", "GTX-A", "127.10", "37.49"), ("gtxa-X109", "성남", "GTX-A", "GTX-A", "127.12", "37.39"))
    out, _ = build(_src(("X1", "가선", "1-가")), _lines(), NO_FIX, pd.concat([DB, db]), RAW, LINE_MAP, gtxa=g)
    assert sorted(set(out["chain_id"])) == ["gtxa:남부", "gtxa:북부"]
    assert [len(c) for c in components(out)["GTX-A"]] == [2, 2]
