"""카카오 호출 일일 쿼터 — KST 날짜별 건수를 `var/quota.json` 에 둔다.

한도에 닿으면 카카오를 부르기 전에 막는다. 파일에는 건수만 쓴다(응답은 저장하지 않는다).
잠금은 프로세스 안에서만 유효하므로 uvicorn 은 단일 워커로 띄운다.
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from .config import KST
from .errors import ApiError

LABELS = {"transit": "대중교통 경로 조회", "car": "자동차 길찾기", "keyword": "장소 검색", "address": "주소 검색",
          "gyeonggi_bus": "경기 버스 도착정보", "seoul_bus": "서울 버스 도착정보",
          "seoul_subway": "서울 도시철도 실시간 도착"}


def exceeded_error(kind, limit=None, upstream_status=None):
    cap = f"({limit}건)" if limit is not None else ""
    return ApiError("quota_exceeded", f"오늘의 {LABELS.get(kind, kind)} 호출 한도{cap}를 모두 사용했습니다.",
                    action="한국 시간 자정(00:00)에 초기화됩니다.", status=429, upstream_status=upstream_status)


class QuotaGuard:
    def __init__(self, path: Path, limits: dict[str, int], clock=lambda: datetime.now(KST)):
        self.path = Path(path)
        self.limits = dict(limits)
        self.clock = clock
        self._lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        self._date = data.get("date")
        self._used = dict(data.get("used") or {})

    def _roll(self):
        """KST 날짜가 바뀌었으면 건수를 비운다."""
        today = self.clock().astimezone(KST).date().isoformat()
        if self._date != today:
            self._date, self._used = today, {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"date": self._date, "used": self._used}), encoding="utf-8")
        os.replace(tmp, self.path)  # 원자적 교체

    def reserve(self, kind):
        """한도 확인과 차감을 한 잠금 안에서 한다 — 확인과 기록이 따로면 동시 요청이 모두 확인을
        통과해 한도를 넘긴다. → 예약한 KST 날짜 (release 에 넘긴다)"""
        with self._lock:
            self._roll()
            limit = self.limits[kind]
            used = self._used.get(kind, 0)
            if used >= limit:
                raise exceeded_error(kind, limit)
            self._used[kind] = used + 1
            self._save()
            return self._date

    def release(self, kind, date):
        """예약했지만 카카오 쿼터를 쓰지 않은 호출(401·403·연결 실패)을 되돌린다.
        그사이 자정을 넘겼으면 새 날의 건수이므로 건드리지 않는다."""
        with self._lock:
            self._roll()
            if self._date == date and self._used.get(kind, 0) > 0:
                self._used[kind] -= 1
                self._save()

    def mark_exhausted(self, kind, date):
        """카카오가 한도 초과(-10)를 알려 오면 그날 남은 호출을 0 으로 만든다. date = reserve 가 돌려준 예약 날짜.
        그사이 자정을 넘겼으면 어제 호출에 대한 답이므로 새 날을 막지 않는다 (release 와 같다)."""
        with self._lock:
            self._roll()
            if self._date != date:
                return
            self._used[kind] = max(self._used.get(kind, 0), self.limits[kind])
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            self._roll()
            out = {"date": self._date}
            for kind, limit in self.limits.items():
                used = self._used.get(kind, 0)
                out[kind] = {"used": used, "limit": limit, "remaining": max(0, limit - used)}
            return out
