"""실시간 지하철 역명 매핑표 — 합성 데이터만 쓴다."""
import pytest

from build_subway_live_map import build, file_date, live_index

DATE = "2026-09-02"
NO_LIVE = ("의정부경전철",)


def _live(subway_id, statn_id, name, group):
    return {"SUBWAY_ID": subway_id, "STATN_ID": statn_id, "STATN_NM": name, "호선이름": group}


def _station(sid, name, group, aliases=""):
    return {"station_id": sid, "name": name, "line_group": group, "aliases": aliases}


def _override(sid, our_name, live_name="", subway_id="", statn_id="", verified="yes", note="개명"):
    return {"station_id": sid, "our_name": our_name, "live_name": live_name,
            "live_subway_id": subway_id, "live_statn_id": statn_id, "verified": verified, "note": note}


LIVE = [_live("1004", "1004000426", "서울", "4호선"),
        _live("1007", "1007000736", "총신대입구(이수)", "7호선"),
        _live("1007", "1007000728", "뚝섬유원지", "7호선"),
        _live("1075", "1075075256", "신길온천", "수인분당선")]
LINE_GROUPS = [{"line_group": g} for g in ("4호선", "7호선", "수인분당선", "의정부경전철")]
STATIONS = [_station("s4-0426", "서울역", "4호선"),
            _station("s7-0736", "이수", "7호선", aliases="총신대입구(이수)"),
            _station("s7-0728", "자양(뚝섬한강공원)", "7호선"),
            _station("s4-0405", "진접역", "4호선"),
            _station("suin-1760", "능길역", "수인분당선"),
            _station("ui-1101", "회룡", "의정부경전철")]
OVERRIDES = [_override("s7-0728", "자양(뚝섬한강공원)", "뚝섬유원지", "1007", "1007000728",
                       note="실시간이 옛 이름을 쓴다"),
             _override("s4-0405", "진접역", note="진접선 — 역별 실시간 미제공")]


def _build(stations=None, overrides=None, line_groups=None, live=None):
    out, info = build(live or LIVE, stations or STATIONS, line_groups or LINE_GROUPS,
                      OVERRIDES if overrides is None else overrides, DATE, no_live_groups=NO_LIVE)
    return out.set_index("station_id"), info


# --- 매칭 ---

def test_match_kinds():
    out, info = _build()
    assert len(out) == len(STATIONS)
    assert out["match"].tolist() == ["exact", "alias", "override", "no_live_station",
                                     "no_live_station", "no_live_line"]
    assert not (out["match"] == "").any()
    assert dict(info["steps"])["실시간 조회 가능 행"] == 3
    assert out["data_date"].unique().tolist() == [DATE]


def test_exact_keeps_live_spelling():
    # 우리 이름은 `서울역`, 질의에 써야 하는 이름은 `서울` — 우리 이름으로 물으면 0 건이 온다
    r = _build()[0].loc["s4-0426"]
    assert (r["live_name"], r["live_subway_id"], r["live_statn_id"]) == ("서울", "1004", "1004000426")
    assert r["note"] == ""


def test_alias_match():
    r = _build()[0].loc["s7-0736"]
    assert (r["live_name"], r["live_statn_id"], r["match"]) == ("총신대입구(이수)", "1007000736", "alias")
    assert r["note"] == "별칭 총신대입구(이수)"


def test_override_applied_and_no_live_station():
    out, info = _build()
    r = out.loc["s7-0728"]
    assert (r["live_name"], r["live_statn_id"]) == ("뚝섬유원지", "1007000728")
    assert r["note"] == "실시간이 옛 이름을 쓴다"
    gap = out.loc["s4-0405"]      # 실시간 미제공 — 질의할 이름이 없다
    assert (gap["live_name"], gap["live_subway_id"], gap["live_statn_id"]) == ("", "", "")
    assert [r[0] for r in info["missing"]] == ["s4-0405", "suin-1760"]


def test_override_needed_for_renamed_station():
    # 원천에 옛 이름(`신길온천`)이 살아 있어도 자동으로는 붙지 않는다 → 보정표가 있어야 이어진다
    assert _build()[0].loc["suin-1760"]["match"] == "no_live_station"
    ov = [*OVERRIDES, _override("suin-1760", "능길역", "신길온천", "1075", "1075075256", verified="no")]
    r = _build(overrides=ov)[0].loc["suin-1760"]
    assert (r["match"], r["live_name"], r["live_statn_id"]) == ("override", "신길온천", "1075075256")


def test_no_live_line_is_excluded():
    out, info = _build()
    r = out.loc["ui-1101"]
    assert (r["live_subway_id"], r["live_statn_id"], r["live_name"]) == ("", "", "")
    assert info["no_live"] == [("의정부경전철", 1)]


def test_leftover_live_only_stations():
    # 우리 역 DB 에 없는 실시간 역(대상 밖·커버리지 경계)은 남는 것으로 센다
    info = _build()[1]
    assert dict(info["steps"])["실시간 역정보에만 있는 역"] == 1
    assert info["leftover"] == [("수인분당선", 1, "신길온천")]


# --- 보정표가 낡았을 때 ---

def test_stale_override_unknown_station():
    with pytest.raises(ValueError, match="역 DB 에 없습니다"):
        _build(overrides=[_override("없는-0001", "없는역", "서울", "1004", "1004000426")])


def test_stale_override_name_differs():
    with pytest.raises(ValueError, match="역 이름이 역 DB 와 다릅니다"):
        _build(overrides=[_override("s4-0405", "오남역", note="다른 역 이름")])


def test_stale_override_already_matches():
    # 자동으로 맞는 역에 보정이 남아 있으면 원천이 고쳐진 신호다 → 멈춘다
    with pytest.raises(ValueError, match="보정표가 필요 없습니다"):
        _build(overrides=[*OVERRIDES, _override("s4-0426", "서울역", "서울", "1004", "1004000426")])


def test_stale_override_live_name_gone():
    ov = [*OVERRIDES, _override("suin-1760", "능길역", "없는이름", "1075", "1075075256")]
    with pytest.raises(ValueError, match="실시간 역정보에 없습니다"):
        _build(overrides=ov)


def test_stale_override_wrong_subway_id():
    ov = [*OVERRIDES, _override("suin-1760", "능길역", "신길온천", "1004", "1004000454")]
    with pytest.raises(ValueError, match="SUBWAY_ID 가 그 역의 노선군과 다릅니다"):
        _build(overrides=ov)


def test_duplicate_override_row():
    with pytest.raises(ValueError, match="같은 역이 두 번"):
        _build(overrides=[*OVERRIDES, _override("s4-0405", "진접역")])


# --- 원천이 바뀌었을 때 ---

def test_no_live_groups_changed():
    # 실시간이 없는 노선군 목록이 기대와 다르면 멈춘다(제공 시작·노선군 추가 신호)
    with pytest.raises(ValueError, match="실시간이 없는 노선군이 기대와 다릅니다"):
        _build(line_groups=[*LINE_GROUPS, {"line_group": "김포골드라인"}])


def test_line_ids_reject_two_subway_ids():
    live = [*LIVE, _live("1104", "1104000001", "새역", "4호선")]
    with pytest.raises(ValueError, match="SUBWAY_ID 가 둘입니다"):
        _build(live=live)


def test_live_index_rejects_duplicate_name_key():
    live = [*LIVE, _live("1004", "1004000999", "서울역", "4호선")]   # 이름 키가 `서울` 로 겹친다
    with pytest.raises(ValueError, match="같은 노선·이름 키가 둘"):
        live_index(live)


def test_file_date():
    assert file_date("실시간도착_역정보(20260902).xlsx") == "2026-09-02"
    with pytest.raises(ValueError, match="기준일"):
        file_date("실시간도착_역정보.xlsx")
