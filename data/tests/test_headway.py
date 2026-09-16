"""배차간격 표 — 합성 데이터만 쓴다."""
import pandas as pd
import pytest

from build_headway import (BUS_COLUMNS, bus_rows, gtfs_line_map, hhmm, parse_hm, rail_counts, rail_frame,
                           rail_routes, station_map)

GBIS_COLS = ["routeId", "routeName", "peekAlloc", "npeekAlloc", "upFirstTime", "upLastTime",
             "downFirstTime", "downLastTime", "satPeekAlloc", "satNpeekAlloc", "satUpFirstTime",
             "satUpLastTime", "satDownFirstTime", "satDownLastTime", "sunPeekAlloc", "sunNpeekAlloc",
             "sunUpFirstTime", "sunUpLastTime", "sunDownFirstTime", "sunDownLastTime",
             "wePeekAlloc", "weNpeekAlloc", "weUpFirstTime", "weUpLastTime", "weDownFirstTime", "weDownLastTime"]
SEOUL_COLS = ["busRouteId", "busRouteNm", "term", "firstBusTm", "lastBusTm"]


def _gbis(**over):
    row = dict.fromkeys(GBIS_COLS, "")
    row.update(routeId="200000006", routeName="300", peekAlloc="10", npeekAlloc="15",
               upFirstTime="04:20", upLastTime="21:30", downFirstTime="06:10", downLastTime="23:20")
    row.update(over)
    return pd.DataFrame([row], columns=GBIS_COLS)


def _seoul(**over):
    row = {"busRouteId": "100100022", "busRouteNm": "143", "term": "6",
           "firstBusTm": "20260911040000", "lastBusTm": "20260911221000"}
    row.update(over)
    return pd.DataFrame([row], columns=SEOUL_COLS)


# --- 시각 읽기 ---

@pytest.mark.parametrize("text, minutes", [
    ("04:20", 260), ("24:47:00", 1487), ("20260911225000", 1370), ("00:00", 0),
    ("", None), ("0", None), ("-", None), ("어제", None), (None, None),
])
def test_parse_hm(text, minutes):
    assert parse_hm(text) == minutes


def test_hhmm():
    assert (hhmm(260), hhmm(1487), hhmm(None)) == ("04:20", "24:47", "")


# --- 버스 ---

def test_bus_rows_gyeonggi_day_types():
    # 평일은 값이 있고, 토요일은 배차만, 일요일·공휴일은 아무 값도 없다 → 두 행
    g = _gbis(satPeekAlloc="12", satNpeekAlloc="20")
    out = bus_rows(g, _seoul().iloc[0:0], {"200000006"})
    assert out.columns.tolist() == BUS_COLUMNS
    assert out["day_type"].tolist() == ["평일", "토요일"]
    weekday = out.iloc[0]
    assert (weekday["headway_min_m"], weekday["headway_max_m"]) == (10, 15)
    assert (weekday["up_first"], weekday["up_last"]) == ("04:20", "21:30")
    assert (weekday["down_first"], weekday["down_last"]) == ("06:10", "23:20")
    sat = out.iloc[1]
    assert (sat["headway_min_m"], sat["up_first"]) == (12, "")   # 토요일 첫·막차는 원천에 없다


def test_bus_rows_zero_means_unknown():
    g = _gbis(peekAlloc="0", npeekAlloc="", upFirstTime="", upLastTime="", downFirstTime="", downLastTime="")
    assert bus_rows(g, _seoul().iloc[0:0], {"200000006"}).empty


def test_bus_rows_seoul_term_and_filter():
    out = bus_rows(_gbis().iloc[0:0], _seoul(), {"100100022"})
    assert len(out) == 1
    r = out.iloc[0]
    # 서울 원천은 요일 구분이 없고 term 하나뿐이다 → 최소·최대 같은 값, 하행 칸은 비운다
    assert (r["source"], r["day_type"], r["headway_min_m"], r["headway_max_m"]) == ("seoul", "평일", 6, 6)
    assert (r["up_first"], r["up_last"], r["down_first"], r["down_last"]) == ("04:00", "22:10", "", "")
    assert bus_rows(_gbis(), _seoul(), set()).empty   # 우리 순서표에 없는 노선은 뺀다


# --- 노선군 매핑 ---

def test_gtfs_line_map_uses_group_name_and_aliases():
    m = gtfs_line_map([{"line_group": "1호선", "gtfs_names": "서울1호선"},
                       {"line_group": "수인분당선", "gtfs_names": ""}])
    assert m == {"1호선": "1호선", "서울1호선": "1호선", "수인분당선": "수인분당선"}


def test_rail_routes_direction_and_skipped():
    rows = [{"route_id": "RR_A", "route_type": "1", "route_short_name": "서울4호선",
             "route_long_name": "서울4호선<하행>"},
            {"route_id": "RR_B", "route_type": "1", "route_short_name": "서울2호선",
             "route_long_name": "서울2호선(본선)<내선>"},
            {"route_id": "RR_C", "route_type": "1", "route_short_name": "부산1호선", "route_long_name": "부산1호선"},
            {"route_id": "BR_D", "route_type": "0", "route_short_name": "300", "route_long_name": "-"}]
    keep, skipped = rail_routes(rows, {"서울4호선": "4호선", "서울2호선": "2호선"})
    assert keep == {"RR_A": ("4호선", "하행"), "RR_B": ("2호선", "내선")}
    assert dict(skipped) == {"부산1호선": 1}     # 대상 밖 — 조용히 버리지 않고 센다


# --- 역 매칭 ---

def _station(sid, name, group, lat, lon, aliases=""):
    return {"station_id": sid, "name": name, "line_group": group,
            "lat": str(lat), "lon": str(lon), "aliases": aliases}


STATIONS = [
    _station("s4-1", "불암산", "4호선", 37.6, 127.1),
    _station("s7-1", "이수", "7호선", 37.48, 127.0, aliases="총신대입구(이수)"),
    _station("k1-1", "청량리역", "1호선", 37.58, 127.04),
    _station("far-1", "청량리역", "경강선", 37.30, 127.60),          # 이름은 같지만 멀다
]


def test_station_map_levels():
    stops = {"a": ("불암산", 37.6, 127.1),
             "b": ("총신대입구(7호선)", 37.48, 127.0),
             "c": ("당고개", 37.6, 127.1),
             "d": ("청량리(경의중앙선)", 37.581, 127.041),
             "e": ("없는역", 37.0, 127.0)}
    pairs = {("a", "4호선"), ("b", "7호선"), ("c", "4호선"), ("d", "경춘선"), ("e", "4호선")}
    over = [{"gtfs_name": "당고개", "line_group": "4호선", "station_id": "s4-1"}]
    mapped, missing, other = station_map(stops, pairs, STATIONS, over)
    assert mapped[("a", "4호선")]["station_id"] == "s4-1"          # 이름 키 + 노선군
    assert mapped[("b", "7호선")]["station_id"] == "s7-1"          # 별칭
    assert mapped[("c", "4호선")]["station_id"] == "s4-1"          # 보정표(개명)
    assert mapped[("d", "경춘선")]["station_id"] == "k1-1"          # 그 노선군 행이 없다 → 같은 실체(300 m)
    assert other == [("청량리(경의중앙선)", "경춘선", "k1-1", "1호선")]
    assert dict(missing) == {("없는역", "4호선"): 1}


def test_station_map_ignores_far_same_name():
    stops = {"a": ("청량리역", 37.58, 127.04)}
    mapped, missing, other = station_map(stops, {("a", "서해선")}, [STATIONS[3]])
    assert not mapped and other == [] and dict(missing) == {("청량리역", "서해선"): 1}


# --- 도시철도 배차 ---

def _stop_times(tmp_path, *rows):
    p = tmp_path / "stop_times.txt"
    lines = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type,timepoint"]
    lines += [",".join(r) for r in rows]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_rail_counts_and_headway(tmp_path):
    # 8시에 3회, 9시에 1회. 종착(pickup_type=1)과 대상 밖 노선은 세지 않는다
    p = _stop_times(
        tmp_path,
        ("RR_A_Ord001", "08:00:00", "08:00:00", "S1", "1", "0", "0", "1"),
        ("RR_A_Ord002", "08:20:00", "08:20:00", "S1", "1", "0", "0", "1"),
        ("RR_A_Ord003", "08:40:00", "08:40:00", "S1", "1", "0", "0", "1"),
        ("RR_A_Ord004", "09:10:00", "09:10:00", "S1", "1", "0", "0", "1"),
        ("RR_A_Ord004", "24:30:00", "24:30:00", "S2", "2", "1", "0", "1"),   # 종착 — 제외
        ("RR_B_Ord001", "08:05:00", "08:05:00", "S1", "1", "0", "0", "1"),   # 다른 노선(대상 밖)
    )
    counts, pairs, n, trips = rail_counts(p, {"RR_A": ("4호선", "하행")})
    assert n == 6 and pairs == {("S1", "4호선")}
    assert dict(counts[("S1", "4호선", "하행")]) == {8: 3, 9: 1}
    assert trips == {("4호선", "하행"): 4}   # 종착만 있는 회차는 세지 않는다

    mapped = {("S1", "4호선"): _station("s4-1", "불암산", "4호선", 37.6, 127.1)}
    df = rail_frame(counts, mapped)
    assert df["hour"].tolist() == [8, 9]
    assert df["n_trips"].tolist() == [3, 1]
    assert df["n_trips_3h"].tolist() == [4, 4]
    assert df["headway_m"].tolist() == [45.0, 45.0]              # 180 / 3시간 창의 정차 횟수
    assert df.iloc[0][["station_id", "name", "line_group", "direction"]].tolist() == ["s4-1", "불암산", "4호선", "하행"]


def test_rail_frame_window_keeps_hourly_service_and_smooths_boundary():
    mapped = {("S1", "4호선"): _station("s4-1", "불암산", "4호선", 37.6, 127.1),
              ("S2", "4호선"): _station("s4-2", "당고개", "4호선", 37.6, 127.1)}
    counts = {("S1", "4호선", "하행"): {10: 1, 11: 1, 12: 1},          # 실제로 한 시간에 한 대
              ("S2", "4호선", "하행"): {10: 2, 11: 1, 12: 2}}          # 40분 간격이 시 경계에 걸려 2·1·2
    df = rail_frame(counts, mapped).set_index(["station_id", "hour"])
    assert df.loc[("s4-1", 11), "headway_m"] == 60.0
    assert df.loc[("s4-1", 10), "headway_m"] == 90.0                  # 첫 시간대: 창이 운행 없는 9시를 품는다
    assert df.loc[("s4-2", 11), "headway_m"] == 36.0                  # 한 칸만 세면 60분


def test_rail_frame_keeps_after_midnight_hours(tmp_path):
    p = _stop_times(tmp_path,
                    ("RR_A_Ord001", "24:10:00", "24:10:00", "S1", "1", "0", "0", "1"),
                    ("RR_A_Ord002", "24:40:00", "24:40:00", "S1", "1", "0", "0", "1"))
    counts, *_ = rail_counts(p, {"RR_A": ("4호선", "하행")})
    assert dict(counts[("S1", "4호선", "하행")]) == {24: 2}
    mapped = {("S1", "4호선"): _station("s4-1", "불암산", "4호선", 37.6, 127.1)}
    assert rail_frame(counts, mapped)["hour"].tolist() == [24]


def test_rail_frame_drops_unmapped_station(tmp_path):
    p = _stop_times(tmp_path, ("RR_A_Ord001", "08:00:00", "08:00:00", "S9", "1", "0", "0", "1"))
    counts, *_ = rail_counts(p, {"RR_A": ("4호선", "하행")})
    assert rail_frame(counts, {}).empty
