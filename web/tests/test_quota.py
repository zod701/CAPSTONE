import json
from datetime import datetime, timezone

import pytest

from app.config import KST
from app.errors import ApiError
from app.quota import QuotaGuard

LIMITS = {"transit": 3, "car": 5}


class Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


def kst(s):
    return datetime.fromisoformat(s).replace(tzinfo=KST)


def test_reserve_persists_and_reloads(tmp_path):
    path = tmp_path / "var" / "quota.json"  # 부모 폴더가 없어도 만든다
    clock = Clock(kst("2026-09-11T10:00"))
    q = QuotaGuard(path, LIMITS, clock)
    assert q.reserve("transit") == "2026-09-11"
    q.reserve("transit")
    q.reserve("car")
    assert json.loads(path.read_text(encoding="utf-8")) == {"date": "2026-09-11", "used": {"transit": 2, "car": 1}}
    assert [p.name for p in path.parent.iterdir()] == ["quota.json"]  # 임시 파일이 남지 않는다

    q2 = QuotaGuard(path, LIMITS, clock)
    assert q2.snapshot() == {
        "date": "2026-09-11",
        "transit": {"used": 2, "limit": 3, "remaining": 1},
        "car": {"used": 1, "limit": 5, "remaining": 4},
    }


def test_kst_midnight_rollover(tmp_path):
    path = tmp_path / "quota.json"
    clock = Clock(datetime(2026, 9, 11, 14, 59, 59, tzinfo=timezone.utc))  # KST 23:59:59
    q = QuotaGuard(path, LIMITS, clock)
    for _ in range(3):
        q.reserve("transit")
    with pytest.raises(ApiError):
        q.reserve("transit")

    clock.dt = datetime(2026, 9, 11, 15, 0, 0, tzinfo=timezone.utc)  # KST 9/12 00:00
    snap = q.snapshot()
    assert snap["date"] == "2026-09-12"
    assert snap["transit"] == {"used": 0, "limit": 3, "remaining": 3}
    q.reserve("car")
    assert json.loads(path.read_text(encoding="utf-8")) == {"date": "2026-09-12", "used": {"car": 1}}


def test_stale_file_resets_on_load(tmp_path):
    path = tmp_path / "quota.json"
    path.write_text(json.dumps({"date": "2026-09-10", "used": {"transit": 3}}), encoding="utf-8")
    q = QuotaGuard(path, LIMITS, Clock(kst("2026-09-11T00:00")))
    assert q.snapshot()["transit"]["used"] == 0  # 어제 소진은 오늘을 막지 않는다
    q.reserve("transit")


def test_limit_blocks_before_call(tmp_path):
    q = QuotaGuard(tmp_path / "quota.json", {"transit": 2, "car": 5}, Clock(kst("2026-09-11T10:00")))
    q.reserve("transit")
    q.reserve("transit")
    with pytest.raises(ApiError) as ei:
        q.reserve("transit")
    e = ei.value
    assert (e.code, e.status) == ("quota_exceeded", 429)
    assert e.message == "오늘의 대중교통 경로 조회 호출 한도(2건)를 모두 사용했습니다."
    assert e.action == "한국 시간 자정(00:00)에 초기화됩니다."
    assert q.snapshot()["transit"]["used"] == 2  # 막힌 예약은 세지 않는다
    q.reserve("car")  # 다른 종류는 영향 없음


def test_release_same_day_only(tmp_path):
    path = tmp_path / "quota.json"
    clock = Clock(datetime(2026, 9, 11, 14, 59, 59, tzinfo=timezone.utc))  # KST 23:59:59
    q = QuotaGuard(path, LIMITS, clock)
    day = q.reserve("transit")
    q.release("transit", day)
    assert q.snapshot()["transit"]["used"] == 0
    day = q.reserve("transit")
    clock.dt = datetime(2026, 9, 11, 15, 0, 1, tzinfo=timezone.utc)  # 자정을 넘겨 되돌림
    q.reserve("transit")
    q.release("transit", day)  # 어제 예약 — 오늘 건수를 건드리지 않는다
    assert q.snapshot()["transit"]["used"] == 1


def test_mark_exhausted(tmp_path):
    path = tmp_path / "quota.json"
    clock = Clock(kst("2026-09-11T10:00"))
    q = QuotaGuard(path, LIMITS, clock)
    day = q.reserve("car")
    q.mark_exhausted("car", day)
    assert q.snapshot()["car"] == {"used": 5, "limit": 5, "remaining": 0}
    with pytest.raises(ApiError) as ei:
        q.reserve("car")
    assert "자동차 길찾기" in ei.value.message and "(5건)" in ei.value.message
    assert QuotaGuard(path, LIMITS, clock).snapshot()["car"]["used"] == 5  # 재시작 후에도 유지


def test_mark_exhausted_same_day_only(tmp_path):
    clock = Clock(datetime(2026, 9, 11, 14, 59, 59, tzinfo=timezone.utc))  # KST 23:59:59
    q = QuotaGuard(tmp_path / "quota.json", LIMITS, clock)
    day = q.reserve("transit")
    clock.dt = datetime(2026, 9, 11, 15, 0, 1, tzinfo=timezone.utc)  # 자정을 넘겨 -10 이 옴
    q.mark_exhausted("transit", day)  # 어제 호출의 한도 초과 — 새 날을 막지 않는다
    assert q.snapshot()["transit"] == {"used": 0, "limit": 3, "remaining": 3}
    q.reserve("transit")


def test_snapshot_without_file(tmp_path):
    q = QuotaGuard(tmp_path / "quota.json", LIMITS, Clock(kst("2026-09-11T10:00")))
    assert q.snapshot() == {
        "date": "2026-09-11",
        "transit": {"used": 0, "limit": 3, "remaining": 3},
        "car": {"used": 0, "limit": 5, "remaining": 5},
    }
    assert not (tmp_path / "quota.json").exists()  # 조회만으로는 파일을 만들지 않는다
