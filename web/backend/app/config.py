"""서버 설정: 저장소 루트 `.env` 와 경로·한도.

`.env` 는 `KEY ="value"` 형식이다. python-dotenv 가 따옴표를 벗기지만 남은 따옴표·공백을 한 번 더
걷어 낸다 — 따옴표째 보내면 카카오가 형식 오류와 함께 키를 에코한다.
"""
import os
from dataclasses import dataclass, field, replace
from datetime import timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]
KST = timezone(timedelta(hours=9))  # 고정 오프셋 — venv 에 tzdata 가 없어 zoneinfo 를 쓰지 않는다


@dataclass
class Settings:
    repo_root: Path
    processed_dir: Path
    ref_dir: Path
    frontend_dir: Path
    quota_path: Path
    kakao_rest_key: str | None = field(repr=False)  # repr·오류 출력에 키가 찍히지 않게
    vworld_key: str | None = field(repr=False)
    transit_daily_limit: int = 1000
    car_daily_limit: int = 10000
    keyword_daily_limit: int = 100000   # 카카오 로컬 키워드로 장소 검색 — 무료 쿼터와 같게 두면 초과 과금이 없다
    address_daily_limit: int = 100000   # 카카오 로컬 주소 검색
    radius_m: dict = field(default_factory=lambda: {"bus": 300.0, "subway": 1000.0})
    ambig_gap_m: dict = field(default_factory=lambda: {"bus": 10.0, "subway": 150.0})
    http_timeout_s: float = 10.0
    # 공공데이터 실시간 도착 — 세 원천 모두 개발계정 1,000건/일
    gyeonggi_bus_daily_limit: int = 1000
    seoul_bus_daily_limit: int = 1000
    seoul_subway_daily_limit: int = 1000
    arrivals_timeout_s: float = 5.0      # 늦게 온 실시간 값은 쓸모가 없다 — 카카오(10초)보다 짧게
    arrivals_cache_ttl_s: float = 15.0   # 같은 정류장을 여러 화면이 함께 볼 때 쿼터를 아낀다 (공공데이터라 허용)
    data_go_kr_key: str | None = field(default=None, repr=False)          # 경기·서울 버스 도착정보 (같은 키)
    seoul_subway_live_key: str | None = field(default=None, repr=False)   # 서울 열린데이터광장 실시간 지하철


def _env(name):
    v = (os.environ.get(name) or "").strip().strip('"').strip("'").strip()
    return v or None


def load_settings(env_file: Path | None = None, **overrides) -> Settings:
    """`.env`(기본: 저장소 루트)를 읽어 설정을 만든다. 이미 있는 환경변수가 우선한다.
    overrides 는 필드를 그대로 덮어쓴다(테스트용)."""
    load_dotenv(env_file or REPO_ROOT / ".env", override=False)
    s = Settings(
        repo_root=REPO_ROOT,
        processed_dir=REPO_ROOT / "data" / "processed",
        ref_dir=REPO_ROOT / "data" / "ref",
        frontend_dir=REPO_ROOT / "web" / "frontend",
        quota_path=REPO_ROOT / "web" / "backend" / "var" / "quota.json",
        kakao_rest_key=_env("KAKAO_REST_API_KEY"),
        vworld_key=_env("VWORLD_API_KEY"),
        transit_daily_limit=int(_env("KAKAO_TRANSIT_DAILY_LIMIT") or 1000),
        car_daily_limit=int(_env("KAKAO_CAR_DAILY_LIMIT") or 10000),
        keyword_daily_limit=int(_env("KAKAO_KEYWORD_DAILY_LIMIT") or 100000),
        address_daily_limit=int(_env("KAKAO_ADDRESS_DAILY_LIMIT") or 100000),
        data_go_kr_key=_env("DATA_GO_KR_API_KEY"),
        seoul_subway_live_key=_env("SEOUL_SUBWAY_LIVE_API"),
        gyeonggi_bus_daily_limit=int(_env("GYEONGGI_BUS_DAILY_LIMIT") or 1000),
        seoul_bus_daily_limit=int(_env("SEOUL_BUS_DAILY_LIMIT") or 1000),
        seoul_subway_daily_limit=int(_env("SEOUL_SUBWAY_DAILY_LIMIT") or 1000),
    )
    return replace(s, **overrides)
