"""앵커 — 택시 시간·요금 근사. 합성 좌표와 공개 요금표만 쓴다."""
import math
from datetime import datetime

import pytest
from geoutil import EARTH_R

from algo.anchors import TAXI_BASE_S, fare_type, taxi_dist_km, taxi_fare, taxi_reach_m, taxi_time_s

# 기준점 하나를 두고 미터로 옮겨 가며 본다 — 실제 좌표일 필요는 없고 거리 관계만 쓴다
BASE = (127.0286, 37.2659)


def move(pt, east_m, north_m):
    """기준점에서 동·북으로 미터만큼 옮긴 좌표 (테스트가 거리 단위로 읽히게)."""
    lon, lat = pt
    return (lon + math.degrees(east_m / (EARTH_R * math.cos(math.radians(lat)))),
            lat + math.degrees(north_m / EARTH_R))


# --- 시간·거리 ---

def test_reach_is_inverse_of_time():
    # 15분에 닿는 직선거리를 되돌리면 다시 15분 — 계수를 고쳐도 둘이 어긋나면 안 된다
    t = 15 * 60
    far = move(BASE, taxi_reach_m(t), 0)
    assert taxi_time_s(*BASE, *far) == pytest.approx(t, rel=1e-6)


def test_same_point_costs_only_the_fixed_part():
    # 거리는 0 이어도 출발·신호 같은 고정 비용은 남는다
    assert (taxi_dist_km(*BASE, *BASE), taxi_time_s(*BASE, *BASE)) == (0.0, TAXI_BASE_S)


def test_reach_is_zero_below_the_fixed_part():
    # 고정 비용보다 짧은 시간으로는 어디에도 닿지 못한다 (반경이 음수가 되면 안 된다)
    assert taxi_reach_m(TAXI_BASE_S / 2) == 0.0


def test_time_grows_with_distance():
    near, far = move(BASE, 1000, 0), move(BASE, 3000, 0)
    assert taxi_time_s(*BASE, *near) < taxi_time_s(*BASE, *far)


def test_road_distance_exceeds_straight_line():
    # 우회계수가 붙으므로 직선 1 km 는 도로로 1 km 보다 길다
    assert taxi_dist_km(*BASE, *move(BASE, 1000, 0)) > 1.0


# --- 요금 유형 ---

def test_fare_type_by_city():
    # 표준형(수원·서울) / 가형(용인) / 나형(가평) — 공개 고시의 시군 배정
    assert (fare_type("수원시"), fare_type("용인시"),
            fare_type("가평군"), fare_type("강남구")) == ("표준", "가", "나", "표준")


def test_base_fare_within_free_distance():
    # 기본거리 안에서는 기본요금 그대로. 가형은 1.8 km 까지라 1.9 km 는 이미 넘는다
    assert (taxi_fare(1.0, "수원시"), taxi_fare(1.7, "용인시")) == (4800.0, 4800.0)
    assert taxi_fare(1.9, "용인시") > 4800.0


def test_distance_fare_beyond_free():
    # 표준형은 131 m 당 100원 — 기본거리에서 1.31 km 더 가면 정확히 1,000원이 붙는다
    assert taxi_fare(1.6 + 1.31, "수원시") == pytest.approx(5800.0)
    # 나형은 83 m 당 100원이라 같은 거리에서 더 비싸다
    assert taxi_fare(5.0, "가평군") > taxi_fare(5.0, "수원시")


# --- 할증 ---

def test_night_surcharge_splits_seoul_and_gyeonggi():
    # 서울은 22시부터 20%, 경기는 23시부터 30% — 22시 30분에 둘이 갈린다
    at = datetime(2026, 9, 12, 22, 30)
    assert taxi_fare(1.0, "강남구", "강남구", at) == pytest.approx(4800 * 1.2)
    assert taxi_fare(1.0, "수원시", "수원시", at) == 4800.0


def test_night_surcharge_wraps_past_midnight():
    # 새벽 3시는 둘 다 심야, 5시는 둘 다 아니다
    late, morning = datetime(2026, 9, 13, 3, 0), datetime(2026, 9, 13, 5, 0)
    assert taxi_fare(1.0, "수원시", "수원시", late) == pytest.approx(4800 * 1.3)
    assert taxi_fare(1.0, "수원시", "수원시", morning) == 4800.0


def test_outer_surcharge_only_across_business_zones():
    # 시 경계를 넘으면 20%. 서울 구끼리는 한 사업구역이고, 공동사업구역도 붙지 않는다
    assert taxi_fare(1.0, "수원시", "용인시") == pytest.approx(4800 * 1.2)
    assert taxi_fare(1.0, "강남구", "중랑구") == 4800.0
    assert taxi_fare(1.0, "화성시", "오산시") == 4800.0
    assert taxi_fare(1.0, "안양시", "의왕시") == 4800.0
    # 서울 ↔ 경기는 사업구역이 다르다
    assert taxi_fare(1.0, "강남구", "성남시") == pytest.approx(4800 * 1.2)


def test_surcharges_compound():
    # 심야와 시계외는 곱해서 붙는다
    at = datetime(2026, 9, 13, 1, 0)
    assert taxi_fare(1.0, "수원시", "용인시", at) == pytest.approx(4800 * 1.3 * 1.2)


# --- 연결 버퍼 ---

def test_taxi_plan_is_pickup_plus_eta_plus_buffer_when_connecting():
    from algo.anchors import TAXI_BUFFER_S, TAXI_PICKUP_S, taxi_connect_s, taxi_plan_s
    # 10분 ETA → 호출 대기 90초 + 10분 + (내려서 차를 타면) 60초. 택시로 목적지까지 가면 버퍼가 없다
    assert (taxi_connect_s(600), taxi_plan_s(600, connect=False)) == (90 + 600 + 60, 90 + 600)
    assert (TAXI_PICKUP_S, TAXI_BUFFER_S) == (90.0, 60.0)
    # ETA 를 못 받으면 고정분만 남는다 (0 으로 두면 환승이 성립하는 것처럼 보인다)
    assert taxi_connect_s(None) == TAXI_PICKUP_S + TAXI_BUFFER_S
    # ETA 에 비례하는 흔들림은 붙이지 않는다 — 이동 시간이 두 배여도 붙는 몫은 같다
    assert taxi_connect_s(1200) - 1200 == taxi_connect_s(600) - 600


def test_taxi_minimum_distance_is_the_base_fare_distance_in_straight_line():
    from algo.anchors import DETOUR, TAXI_MIN_M
    # 표준형 택시 기본거리 1.6 km(도로)를 우회계수로 나눈 직선거리 — 약 1.1 km
    assert TAXI_MIN_M == pytest.approx(1600 / DETOUR) and 1100 < TAXI_MIN_M < 1150
