"""도시철도 역 사이 운행 시간 · 급행 정차 패턴 — 합성 시각표만 쓴다."""
from build_rail_runs import express_rows, parse_s, rail_runs, rail_trips, run_rows


def _stop_times(tmp_path, *rows):
    p = tmp_path / "stop_times.txt"
    lines = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type,timepoint"]
    lines += [",".join(r) for r in rows]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_parse_s_keeps_after_midnight():
    assert (parse_s("08:01:30"), parse_s("24:10:00"), parse_s("8:01"), parse_s("")) == (28890, 87000, None, None)


def test_runs_are_arrival_gaps_of_neighbouring_stops_per_trip(tmp_path):
    # 완행은 S1 → S2 → S3, 급행은 S2 를 건너뛴다. 순서가 뒤섞여 적혀도 정차 순번으로 잇는다
    p = _stop_times(
        tmp_path,
        ("RR_A_Ord001", "08:02:00", "08:02:30", "S2", "2", "0", "0", "1"),
        ("RR_A_Ord001", "08:00:00", "08:00:30", "S1", "1", "0", "0", "1"),
        ("RR_A_Ord001", "08:05:00", "08:05:00", "S3", "3", "1", "0", "1"),
        ("RR_A_Ord002", "08:10:00", "08:10:30", "S1", "1", "0", "0", "1"),
        ("RR_A_Ord002", "08:13:00", "08:13:30", "S2", "2", "0", "0", "1"),
        ("RR_X_Ord001", "08:20:00", "08:20:00", "S1", "1", "0", "0", "1"),   # 급행
        ("RR_X_Ord001", "08:24:00", "08:24:00", "S3", "2", "1", "0", "1"),
        ("RR_B_Ord001", "08:00:00", "08:00:00", "S1", "1", "0", "0", "1"),   # 대상 밖 노선
        ("RR_B_Ord001", "08:01:00", "08:01:00", "S2", "2", "0", "0", "1"),
    )
    runs = rail_runs(rail_trips(p, {"RR_A": ("4호선", "하행"), "RR_X": ("4호선", "하행")}))
    assert dict(runs) == {("4호선", "S1", "S2"): [120, 180], ("4호선", "S2", "S3"): [180], ("4호선", "S1", "S3"): [240]}

    # 우리 실체로 접어 중앙값. 못 붙인 역(S3)이 낀 쌍은 뺀다
    mapped = {("S1", "4호선"): {"station_id": "s1"}, ("S2", "4호선"): {"station_id": "s2"}}
    rows = run_rows(runs, mapped, {"s1": "p1", "s2": "p2"})
    assert rows == [["4호선", "p1", "p2", 150, 2]]


def _seq(group, chain, sids, loop="0"):
    return [{"chain_id": chain, "line_group": group, "loop": loop, "seq": str(i), "station_id": sid, "phys_id": sid.upper()}
            for i, sid in enumerate(sids, 1)]


def test_express_patterns_are_trips_that_skip_neighbouring_stations():
    # 순서표 s1 - s2 - s3 - s4. 완행은 다 서고, 급행 둘(같은 정차)은 s2 를 건너뛴다. s9 는 대상 밖 역이라 잘라 낸다
    trips = {
        "L1": ("1호선", [(1, 8 * 3600, "G1"), (2, 8 * 3600 + 120, "G2"), (3, 8 * 3600 + 240, "G3")]),
        "X1": ("1호선", [(1, 8 * 3600, "G1"), (2, 8 * 3600 + 180, "G3"), (3, 8 * 3600 + 300, "G4"), (4, 8 * 3600 + 900, "G9")]),
        "X2": ("1호선", [(1, 9 * 3600, "G1"), (2, 9 * 3600 + 200, "G3"), (3, 9 * 3600 + 320, "G4")]),
    }
    mapped = {(f"G{i}", "1호선"): {"station_id": f"s{i}"} for i in (1, 2, 3, 4)}
    rows = express_rows(trips, mapped, _seq("1호선", "c1", ["s1", "s2", "s3", "s4"]))
    assert [r[3] for r in rows] == ["s1", "s3", "s4"]
    assert [r[4] for r in rows] == [0, 190, 310]   # 첫 역부터 누적 초, 열차별 중앙값
    assert rows[0][5] == 2 and rows[0][6] == "8:1|9:1" and rows[-1][6] == ""   # 끝 역은 출발이 없다


def test_loop_seam_is_not_a_skip():
    # 순환선은 끝(s3)과 처음(s1)이 이웃이다 — 이음매를 넘는 열차는 급행이 아니다
    trips = {"T": ("2호선", [(1, 0, "G3"), (2, 60, "G1"), (3, 120, "G2")])}
    mapped = {(f"G{i}", "2호선"): {"station_id": f"s{i}"} for i in (1, 2, 3)}
    assert express_rows(trips, mapped, _seq("2호선", "c2", ["s1", "s2", "s3"], loop="1")) == []
