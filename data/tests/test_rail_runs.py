"""도시철도 역 사이 운행 시간 — 합성 시각표만 쓴다."""
from build_rail_runs import parse_s, rail_runs, run_rows


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
    runs = rail_runs(p, {"RR_A": ("4호선", "하행"), "RR_X": ("4호선", "하행")})
    assert dict(runs) == {("4호선", "S1", "S2"): [120, 180], ("4호선", "S2", "S3"): [180], ("4호선", "S1", "S3"): [240]}

    # 우리 실체로 접어 중앙값. 못 붙인 역(S3)이 낀 쌍은 뺀다
    mapped = {("S1", "4호선"): {"station_id": "s1"}, ("S2", "4호선"): {"station_id": "s2"}}
    rows = run_rows(runs, mapped, {"s1": "p1", "s2": "p2"})
    assert rows == [["4호선", "p1", "p2", 150, 2]]
