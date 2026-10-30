"""하이브리드 후보의 공통 부품 — 택시 시간·요금 근사, 승차 시간 근사, 경로 위 정류소, 첫 승차 실시간 대기 (쿼터를 쓰지 않는다).

앵커 = 택시와 대중교통이 갈아타는 정류소·역이다. 수도권 정류소가 5만 곳이고 대중교통 경로 쿼터는 하루
1,000콜이라 전수 검증이 불가능하다. 후보는 정적 데이터(정류소 좌표·노선 순서표·배차)만으로 만들어 순위를 매기고,
확정 값은 온라인 검증(Tier 2)이 준다 → method.md §2.4, §3.8. 후보를 짝짓고 검증하는 규칙은 `algo.search` 에 있다.

**택시 근사에 요금이 들어가는 이유**는 method.md R23 이다. 점수를 시간만으로 매기면, 정차까지 넣은 버스
실효 속도(~17 km/h)가 택시(~30 km/h)보다 느려 **택시를 오래 탈수록 점수가 단조 감소**한다. 그래서 상위 후보가
전부 택시 도달 반경 경계의 같은 한 지점으로 붕괴한다(배곧→안산 실측: 상위 6개가 모두 택시 21.1분인 같은
정류소). 비용 축이 비면 Pareto 도 선호 3단계도 후보 단계에서 죽는다.

요금은 공개 요금 체계를 그대로 옮긴 근사다. method.md R13 이 기각한 것은 *최종 요금*을 산식으로 계산하는
것이고(심야·시계외 결합, 읍면 복합할증이 공개 자료로 확정되지 않는다), 여기서 쓰는 것은 **순위용 근사**다.
확정 요금은 Tier 2 의 자동차 경로 응답이 실시간 교통·할증까지 반영해 준다 → method.md §7.1.
"""
import csv
import math
from collections import Counter
from pathlib import Path

from geoutil import haversine_m

from app.stopsdb import ID_COL
# 대기 규칙은 백엔드와 한 벌만 둔다 — 빈도 합과 '같은 이름 노선은 한 노선' (method.md §7.5, R21) 을
# 여기서 다시 쓰면 반드시 어긋난다.
from app.wait import DEFAULT_WAIT_S, apply_wait_defaults, WEEKDAY, day_type

# --- 택시 시간 근사 ---
# 시간 = TAXI_K_S × (직선 km)^TAXI_P × 시간대 배수. 멀리 갈수록 큰길 · 고속화도로를 타 1 km 당 시간이 준다 —
# 2026-10-29 11:23(평시) 자동차 경로 응답 450개(직선 0.3–1 · 1–6 · 6–12 km 각 150)에서 직선 1 km 당 중앙 406 · 245 · 188초였다.
# 그 전의 '고정 236초 + 178초/km' 는 1–6 km 에서만 맞고 짧은 쪽을 1.3분, 긴 쪽을 2.6분 느리게 봤다 → method.md E31.
# 세 구간 중앙값을 로그로 맞춘 곡선은 0.7 · 4.2 · 9.1 km 에서 0.5분 안으로 맞는다. `cli.py calibrate` 로 다시 잴 수 있다.
# 시간대 배수는 출근(08:18)·낮·퇴근(18:43)·심야(23:47)에 같은 표본을 잰 편향(E23)을 직선 4.2 km 에서 같은 비율로 옮긴 값이다.
TAXI_K_S = 370.0
TAXI_P = 0.70
TAXI_BAND = {"peak": 1.20, "normal": 1.0, "night": 0.84}
PEAK_HOURS = ((7.0, 10.0), (17.0, 19.5))   # 평일 출퇴근 — 19:37(월)은 평시 기울기와 맞았다
NIGHT_HOURS = (22.0, 6.0)                   # 22시부터 다음 날 6시 전까지
# 요금 거리(도로 km)를 직선에서 추정하는 우회계수 — 같은 표본의 도로/직선 중앙값
DETOUR = 1.42

# --- 택시 요금 근사 (2026-09 고시) ---
# 기본요금은 서울·경기 모두 4,800원이고, 경기는 기본거리와 거리단가가 시군 유형별로 갈린다.
# 유형 → (기본거리 km, 거리단가 원/km). 단가는 "N m 당 100원" 을 km 로 환산한 값이다.
BASE_FARE = 4800.0
FARE_TYPES = {"표준": (1.6, 100 / 0.131), "가": (1.8, 100 / 0.104), "나": (2.0, 100 / 0.083)}
FARE_GA = frozenset("동두천시 양주시 오산시 용인시 화성시 광주시 평택시 하남시".split())
FARE_NA = frozenset("이천시 포천시 가평군 안성시 여주시 양평군 연천군".split())
# 그 밖(서울 25구 + 경기 표준형 15시)은 표준형이다. 광명시는 공개 목록에서 유형을 확인하지 못했다 —
# 서울에 인접한 도시형이라 표준형으로 둔다. 기본거리 0.2 km 차이라 순위에는 거의 영향이 없다.

NIGHT_SEOUL = (22, 4)   # 서울 심야 할증 22–04시 ×1.2
NIGHT_GG = (23, 4)      # 경기 심야 할증 23–04시 ×1.3
NIGHT_MULT_SEOUL = 1.2
NIGHT_MULT_GG = 1.3
OUTER_MULT = 1.2        # 시계외 할증 — 사업구역(시·군) 경계를 넘으면 붙는다
# 공동사업구역은 시 경계를 넘어도 시계외를 받지 않는다. 서울 25개 구는 전체가 한 사업구역이다.
JOINT_ZONES = (frozenset({"화성시", "오산시"}),
               frozenset({"안양시", "과천시", "군포시", "의왕시"}),
               frozenset({"구리시", "남양주시"}))


def taxi_dist_km(lon1, lat1, lon2, lat2):
    """직선거리에 우회계수를 곱한 도로 거리 근사 (km)."""
    return road_km(haversine_m(lon1, lat1, lon2, lat2))


def taxi_time_s(lon1, lat1, lon2, lat2, at=None):
    """택시 소요 시간 근사 (초) — 고정 비용 + 직선거리 비례."""
    return taxi_s_for(haversine_m(lon1, lat1, lon2, lat2), at)


def road_km(straight_m):
    """직선거리(m) → 도로 거리 근사(km). 직선거리를 이미 잰 곳(후보 짝 짓기)이 다시 재지 않게 따로 둔다."""
    return straight_m * DETOUR / 1000.0


def taxi_band(at):
    """택시 시간 기울기의 시간대 — 'peak'(평일 출퇴근) · 'night'(22–6시) · 'normal'. 시각을 모르면 'normal'."""
    if at is None:
        return "normal"
    h = at.hour + at.minute / 60.0
    if h >= NIGHT_HOURS[0] or h < NIGHT_HOURS[1]:
        return "night"
    if day_type(at) == WEEKDAY and any(start <= h < end for start, end in PEAK_HOURS):
        return "peak"
    return "normal"


def taxi_s_for(straight_m, at=None):
    """직선거리(m) → 택시 소요 시간 근사(초). 배수는 택시를 타는 시각의 시간대로 고른다."""
    return TAXI_K_S * (max(0.0, straight_m) / 1000.0) ** TAXI_P * TAXI_BAND[taxi_band(at)]


def taxi_reach_m(t_max_s, at=None):
    """t_max_s 안에 닿는 **직선거리** 상한 (m) — `taxi_s_for` 의 역함수.

    반경 검색에 쓴다. 시간과 반경을 각각 상수로 두면 계수를 고칠 때 반드시 어긋나므로 한 곳에서만 계산한다.
    """
    return 1000.0 * (max(0.0, t_max_s) / (TAXI_K_S * TAXI_BAND[taxi_band(at)])) ** (1.0 / TAXI_P)


def _is_seoul(sgg_nm):
    """서울 자치구인가 — 정류소 DB 의 시군 이름은 서울 25개가 모두 '구' 로 끝나고 경기 31개는 '시·군' 이다."""
    return bool(sgg_nm) and sgg_nm.endswith("구")


def fare_type(sgg_nm):
    """시군의 택시 요금 유형. 서울과 경기 표준형 시는 '표준'."""
    if sgg_nm in FARE_GA:
        return "가"
    if sgg_nm in FARE_NA:
        return "나"
    return "표준"


def _night_mult(sgg_nm, at):
    """심야 할증 배수 — 승차 시각과 승차 지점의 사업구역으로 정한다."""
    if at is None:
        return 1.0
    start, end = NIGHT_SEOUL if _is_seoul(sgg_nm) else NIGHT_GG
    if at.hour >= start or at.hour < end:
        return NIGHT_MULT_SEOUL if _is_seoul(sgg_nm) else NIGHT_MULT_GG
    return 1.0


def _outer_mult(sgg_from, sgg_to):
    """시계외 할증 배수 — 사업구역을 벗어나면 붙는다.

    서울 안(구 → 구)은 25개 구가 한 사업구역이라 붙지 않는다. 경기는 시·군이 각각 사업구역이되
    공동사업구역 협약을 맺은 곳끼리는 붙지 않는다.
    """
    if not sgg_from or not sgg_to or sgg_from == sgg_to:
        return 1.0
    if _is_seoul(sgg_from) and _is_seoul(sgg_to):
        return 1.0
    pair = {sgg_from, sgg_to}
    if any(pair <= zone for zone in JOINT_ZONES):
        return 1.0
    return OUTER_MULT


def taxi_fare(km, sgg_from, sgg_to=None, at=None):
    """택시 요금 근사 (원). 요금 유형·심야는 **승차 지점**(sgg_from)과 승차 시각으로 정한다.

    저속 시간요금(표준형 15.72 km/h 미만에서 30초당 100원)은 넣지 않는다 — 실시간 교통을 모르는 Tier 1 에서는
    계산할 수 없고, 빼면 과소 추정 쪽이라 후보를 부당하게 죽이지 않는다.
    """
    free_km, per_km = FARE_TYPES[fare_type(sgg_from)]
    fare = BASE_FARE + max(0.0, km - free_km) * per_km
    return fare * _night_mult(sgg_from, at) * _outer_mult(sgg_from, sgg_to)


# --- 택시 구간에 잡는 시간과 택시를 쓰는 최소 기준 ---
# 택시 구간 = 호출 대기 + 이동(ETA) + (내려서 차를 타면) 연결 버퍼. **이 규칙은 실험(algo)과 데모 웹(app.hybrid)이 함께 쓴다.**
# - 호출 대기: 택시가 오기까지의 정보가 없어 1–2분의 가운데 상수로 둔다. 택시를 타는 모든 곳(출발지든 경로 중간이든)에 붙인다
# - 연결 버퍼: 내려서 정류장·승강장까지 가는 몫만 둔다. 처음에는 계획서 §8 대로 'ETA × 1.2 + 3분' 이었다 — ETA 에 비례하는 흔들림은
#   대중교통 구간에는 붙이지 않으므로 택시에만 붙이면 택시가 한쪽으로 불리해진다. 비례분을 빼고 3분을 60초로 줄였다
#   (1.2배·3분 모두 실측값이 아니었고, 오프라인 근사를 넣으면 버퍼는 사실상 3.8분 + 직선 1 km 당 36초인 상수였다)
TAXI_PICKUP_S = 90.0
TAXI_BUFFER_S = 60.0
# 최소 기준 — 같은 정류장·옆 정류장으로 가는 택시처럼 너무 짧은 거리를 택시로 바꾸는 후보가 섞였다.
# 거리(`TAXI_MIN_M`)는 도보 계수 아래에 둔다. 절약: 기준선보다 이만큼은 빨라야 한다(호출 대기·버퍼 포함).
TAXI_MIN_SAVING_S = 300.0


def taxi_plan_s(eta_s, connect=True):
    """택시 구간에 잡는 시간(초) = 호출 대기 + ETA + (내려서 차를 타면) 연결 버퍼. ETA 가 없으면 고정분만 남는다."""
    return TAXI_PICKUP_S + (eta_s or 0) + (TAXI_BUFFER_S if connect else 0.0)


def taxi_connect_s(eta_s):
    """내려서 대중교통을 타는 택시 구간(뒤에 탈 차가 남은 것)에 잡는 시간 — `taxi_plan_s(eta, connect=True)`.

    택시로 목적지까지 가는 구간은 `taxi_plan_s(eta, connect=False)` 를 쓴다 — 놓칠 차가 없다.
    """
    return taxi_plan_s(eta_s, connect=True)


# --- 승차 시간 근사 ---
# 버스는 정류장 한 칸마다 정차 고정분(초) + 칸 거리 ÷ 주행속도(km/h)다. 둘 다 칸이 시작하는 정류장의 지역(서울 / 인천 / 그 밖)과
# 노선 부류(급행 = `BUS_EXPRESS_TYPES`, 광역, 나머지 완행)로 가른다. 노선 유형은 순서표의 `route_type`. 거리는 순서표 정류장 좌표를 이은
# 직선(`reach`)이라 도로보다 짧고, 속도도 그 잣대다. 값은 무작위 OD 의 카카오 경로 버스 구간에서 짝수 OD 로 맞추고
# 홀수 OD 로 확인했다 → method.md E35(서울 · 그 밖), E44(인천 · 광역). 유형별 속도 하나(E24)는 짧은 도심 구간에 맞춰 경기 외곽 장거리를
# 1.4–1.7배 느리게 봤다. 정류장이 드문 외곽의 정차 고정분을 따로 낮춰 봤지만 나아지지 않았다(E40).
# 광역은 지역을 가르지 않는다 — 고속도로 정체와 긴 정차로 좌석 · 직행보다 느렸다(서울 · 경기 · 인천 표본 모두 0.83–0.89배).
BUS_EXPRESS_TYPES = frozenset({"직행", "좌석", "시외", "공항", "급행간선"})
BUS_HOP = {("서울", "완행"): (40.0, 18.0), ("서울", "급행"): (60.0, 36.0),
           ("인천", "완행"): (65.0, 39.0), ("인천", "급행"): (75.0, 42.0),
           ("경기", "완행"): (45.0, 40.0), ("경기", "급행"): (60.0, 42.0),
           "광역": (105.0, 39.0)}
SIDO_REGION = {"서울특별시": "서울", "인천광역시": "인천"}   # 그 밖(경기 · 모름)은 "경기"
RURAL_SGG = frozenset({"강화군", "옹진군"})   # 인천 계수는 도심 노선에 맞췄다 — 군 지역은 "경기"(서울 밖 외곽) 계수가 맞았다(E44)


def bus_hop_s(dist_m, sido_nm, route_type, sgg_nm=None):
    """버스 정류장 한 칸의 차내 시간(초) — 칸이 시작하는 정류장의 시도 · 시군구, 노선 유형으로 고른 정차 고정분 + 거리 ÷ 주행속도."""
    if route_type == "광역":
        stop_s, kmh = BUS_HOP["광역"]
    else:
        region = "경기" if sgg_nm in RURAL_SGG else SIDO_REGION.get(sido_nm, "경기")
        stop_s, kmh = BUS_HOP[(region, "급행" if route_type in BUS_EXPRESS_TYPES else "완행")]
    return stop_s + dist_m / 1000.0 / kmh * 3600.0



# 후보로 쓰지 않는 노선 유형. 수요응답형(똑버스·DRT)은 앱 호출·예약으로 구역 안을 달리는 버스라 정해진 노선도 배차도 없다 —
# 순서표 64노선 중 39개는 구역 끝 두 정류장만 적힌 행이고(두 정류장 사이 중앙 190 m), 배차 값은 232행 중 19행뿐이다.
# 카카오 대중교통 경로에는 나오지 않고(화성똑버스06-2 구역 안 OD 실측) 우리 순서표로만 들어오므로 노선을 모으는 단계에서 뺀다.
# 순서표 자체에서는 지우지 않는다 — 데모 웹이 정류장 경유 노선으로 보여 준다.
EXCLUDED_ROUTE_TYPES = frozenset({"수요응답"})
RAIL_KMH = 32.0          # 급행도 일반 기준으로 둔다 — 과대평가하지 않는 쪽(도시철도 구간의 43%가 급행, E17)
# 노선군별 표정속도(km/h) — 역 사이 시각표 운행 시간(`processed/rail_runs.csv`, 이웃한 역 쌍의 98%)이 없는 칸에만 쓴다(E39).
# 값은 기준 경로 응답의 지하철 구간 102개(2026-10-29, 20 OD)에서 카카오 차내 시간과 덩어리 역 사이 직선 합으로 잰
# 중앙값이다(method.md E29). 표본 3개 이상인 노선군과 설계 속도가 뚜렷한 GTX-A 만 두고, 나머지는 전체 중앙 34 km/h 를 쓴다.
# 경춘선 · 경강선 · 서해선은 무작위 OD 의 구간(짝수 OD 5–10개)으로 더했다(E35). 1호선은 같은 구간 29개의 37.4 km/h 로 낮췄다 —
# 46 km/h(구간 10개)는 서울 구간에서 빨라 로컬이 1호선 경로를 낙관했다(E36)
RAIL_KMH_BY_GROUP = {"GTX-A": 90.0, "경강선": 69.0, "경춘선": 49.0, "경의중앙선": 44.0, "서해선": 42.0,
                     "1호선": 38.0, "신분당선": 38.0, "8호선": 38.0, "수인분당선": 36.0,
                     "4호선": 34.0, "3호선": 34.0, "7호선": 33.0, "2호선": 31.0, "5호선": 30.0, "6호선": 30.0, "9호선": 30.0}
RAIL_KMH_DEFAULT = 34.0
# 도시철도 승차마다 더하는 역 안 이동(초) — 개찰구 · 승강장까지 내려가고 나오는 시간. 정류장 좌표 사이 직선 도보에는 없다.
# 카카오는 이것을 도보에 넣어 철도 경로의 도보가 로컬보다 길었다(E37). 시각표 운행 시간이 탄 역의 정차를 품어 150 → 90초로 낮췄다(E39)
RAIL_STATION_S = 90.0

# 도보는 블랙박스 자신의 값에서 역산한 계수를 쓴다 → method.md E15
WALK_MPS = 0.83
WALK_DETOUR = 1.39

# 출발지·목적지에서 걸어서 닿는 정류소를 찾는 반경 — 비면 800 m 로 넓힌다
# (외곽에서는 400 m 안에 정류소가 하나도 없는 곳이 있다 — 실측). 더 멀면 걷지 않는다 — 1,500 m 는 도보 모형으로 약 42분이라
# 시간을 줄이려는 사람은 그만큼 걷기보다 택시를 탄다(출발지에서 바로 택시 · 택시로 정류소에 가서 타기가 이미 후보다)
WALK_RADIUS_M = (400.0, 800.0)
T_MAX_S = 30 * 60        # 택시로 갈아타러 가는 시간의 상한

# 시간가치(원/분) — 시간 중시는 업무통행 22,502원/시 × 물가 보정 1.2165 ÷ 60을 반올림한다.
# 균형·비용 중시는 잠정값이다. 후보 점수와 일반화 비용에 같은 값을 쓴다.
VOT = {"time": 456.0, "balance": 300.0, "cost": 150.0}


def walk_s(dist_m):
    """직선거리를 도보 시간(초)으로 — 우회율을 곱하고 속도로 나눈다."""
    return dist_m * WALK_DETOUR / WALK_MPS


def _walk_equal_m():
    """택시(호출 대기 + 이동 + 연결 버퍼, 평시)와 걷기가 같아지는 직선거리(m) — 이분법."""
    lo, hi = 1.0, 5000.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if taxi_plan_s(taxi_s_for(mid)) < walk_s(mid):
            hi = mid
        else:
            lo = mid
    return hi


# 택시 최소 거리 — 택시가 걷기보다 빨라지는 직선거리(약 150 m). 이보다 짧으면 걷는 편이 빠르다.
# 표준형 기본거리 1.6 km 를 직선으로 바꾼 1.1 km 로 두었을 때는 마지막 1 km 남짓을 택시로 끝내는 좋은 경로를 만들지 못했다(method.md E22)
TAXI_MIN_M = _walk_equal_m()


def transit_fare(route_type=None, kind="bus"):
    """노선을 더 탄 구간의 관할·별도운임을 식별하지 못했을 때 순위용 대체값(원)."""
    if kind == "subway":
        return 1550.0
    return TRANSIT_FARE.get(route_type or "", TRANSIT_FARE_DEFAULT)


# 공개 규칙을 적용하지 못한 후보의 대체값(원) — 순위용이다. 완전한 API 경로는 응답 요금을 쓴다.
# 넣지 않으면 요금이 몇 배인 노선이 공짜처럼 보인다 — 배곧 → 안산 중앙역에서 걸어서 타는 쪽 공항버스(N7000)가
# 상위를 차지했다. 택시 요금이 지배적인 택시로 타러 가는 쪽에서는 가려져 있던 결함이다.
TRANSIT_FARE = {"공항": 8000.0, "시외": 4000.0, "광역": 3200.0, "직행": 3200.0, "좌석": 2650.0}
TRANSIT_FARE_DEFAULT = 1650.0

# 배차를 모르는 노선의 대기 — 0 으로 두면 배차를 모르는 노선이 아는 노선보다 유리해진다.
# 버스 배차표(headway_bus.csv) 평일 행의 대표 배차(최소·최대의 중간값) 중앙 30분의 절반이다 — 값이 있는 3,764행, 2026-09-15 계산
# (경기 40분 · 서울 18분). 처음 둔 450초는 근거 없이 적은 값으로 평일 하위 25%(15분)의 절반이었다 — 모르는 대기를 짧게 잡아
# 배차를 모르는 노선을 유리하게 만들었다. 노선 단위 중앙이라 경로에 자주 나오는 간선보다 긴 쪽으로 치우칠 수 있다.
WAIT_UNKNOWN_S = DEFAULT_WAIT_S


# --- 정적 표 추가 로더 (백엔드 DB 가 읽지 않는 열) ---

def load_phys(processed_dir):
    """station_id → phys_id — 같은 역의 노선별 행(환승)을 한 실체로 묶은 키.

    `LinesDB` 는 덩어리를 이을 때만 쓰고 버린다. 청량리는 3행(1호선 2 + 경의중앙선 1), 서울역·김포공항은
    5행이라 이 키가 없으면 같은 역이 여러 후보가 되어 검증 콜을 중복해서 쓴다.
    """
    with open(Path(processed_dir) / "subway_line_seq.csv", encoding="utf-8-sig", newline="") as f:
        return {r["station_id"]: r["phys_id"] for r in csv.DictReader(f)}


def load_service_hours(processed_dir):
    """route_id → {요일 유형: [(첫차, 막차)…]} (자정부터의 분) — headway_bus.csv 의 첫·막차 열.

    `HeadwayDB` 가 이 열을 읽지 않아 따로 읽는다. 배차 분값 없이 첫·막차만 있는 행이 1,763개다.
    상·하행을 **각각 창으로 담아 하나라도 걸리면 운행**으로 본다 — 우리가 아는 방향과 원천의 상·하행이
    맞물린다는 보장이 없어, 잘못 거르는 쪽보다 덜 거르는 쪽을 택한다.
    """
    out = {}
    with open(Path(processed_dir) / "headway_bus.csv", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            wins = [w for w in ((_hm(r["up_first"]), _hm(r["up_last"])),
                                (_hm(r["down_first"]), _hm(r["down_last"])))
                    if w[0] is not None and w[1] is not None]
            if wins:   # 같은 노선 · 요일 유형이 여러 행이면 창을 모두 담는다 — 덮어쓰면 낮에 다니는 노선이 빠진다(E35)
                out.setdefault(r["route_id"], {}).setdefault(r["day_type"], []).extend(wins)
    return out


def _hm(s):
    """'HH:MM' → 자정부터의 분. 빈칸·형식 오류면 None."""
    parts = (s or "").strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]) * 60 + int(parts[1])


def in_service(hours, route_id, day, at):
    """그 시각에 운행하는 노선인가. 표에 없으면 True — 모르는 것을 거르지 않는다.

    막차가 첫차보다 이르면 자정을 넘긴 것이다(04:30 → 01:00, 182 노선). 그날 요일 유형 값이 없으면
    평일 값으로 돌아간다(경기 원천은 주말·공휴일 행이 절반 넘게 비어 있다).
    """
    if hours is None or at is None:
        return True
    days = hours.get(route_id)
    wins = (days or {}).get(day) or (days or {}).get(WEEKDAY)
    if not wins:
        return True
    m = at.hour * 60 + at.minute
    return any(f <= m <= l if f <= l else (m >= f or m <= l) for f, l in wins)


# --- 정류소 찾기 ---

def _near(stops, kind, pt, radii, *, radius_used=None):
    """기준점 반경 안의 정류소·역 → {id: (행, 거리 m)}. 비면 다음 반경으로 넓힌다.

    미정차(가상) 정류소는 `StopsDB.near` 가 기본으로 뺀다. 환승역은 운영 노선별로 행이 여럿이라
    같은 실체가 여러 id 로 들어온다 — 짝을 만들 때 phys_id 로 접는다.
    """
    for r in radii:
        if radius_used is not None:
            radius_used[kind] = r
        found = {}
        for i, dist in stops.near(kind, pt[0], pt[1], r):
            row = stops.row(kind, i)
            found[row[ID_COL[kind]]] = (row, dist)
        if found:
            return found
    return {}


def _near_any(stops, kind, points, radius_m):
    """여러 중심 중 어느 하나의 반경 안 정류소·역 → {id: (행, 가장 가까운 중심까지 거리 m)}."""
    found = {}
    for pt in points:
        for i, dist in stops.near(kind, pt[0], pt[1], radius_m):
            row = stops.row(kind, i)
            key = row[ID_COL[kind]]
            if key not in found or dist < found[key][1]:
                found[key] = (row, dist)
    return found


def _sgg_at(*near_maps):
    """기준점에서 가장 가까운 정류소의 시군 — 택시 요금 유형과 시계외 판정에 쓴다."""
    best = None
    for m in near_maps:
        for row, dist in m.values():
            if best is None or dist < best[0]:
                best = (dist, row.get("sgg_nm"))
    return best[1] if best else None


# --- 기준 경로 읽기 ---

RIDE_TYPES = ("BUS", "SUBWAY")


def _chosen(step, role):
    """구간의 승·하차 지점 — 접지가 정하지 못했으면 None."""
    return ((step.get("resolution") or {}).get(role) or {}).get("chosen")


def _edge_stop(steps, role):
    """경로 전체의 첫 승차점(board) 또는 마지막 하차점(alight). 없으면 None."""
    rides = [s for s in steps if s["type"] in RIDE_TYPES]
    if not rides:
        return None
    return _chosen(rides[0] if role == "board" else rides[-1], role)


# --- 첫 승차 대기 — 실시간 도착 ---
# 실시간으로 알 수 있는 것은 **지금** 첫 승차 정류장에 오는 차뿐이다 — 뒤 구간의 도착은 지금 알 수 없어 정적 배차로 남긴다.
# 예측 초 없이 남은 정류소·역 수만 오는 행이 흔하다(E14: 도시철도 실호출 13건이 전부 예측 0). 도시철도는 **그 열차가 지나올
# 역 사이 실제 거리**를 순서표에서 합해 표정속도로 나눈다(`subway_upstream_s`) — 역 간 거리가 노선마다 크게 달라(서울 6호선
# 846 m · 분당선 1,386 m · GTX-A 9,801 m) 전 노선 공통값으로는 외곽에서 도착을 이르게 본다(E19: 분당선 실측 편향 −75초 → +11초).
# 아래 상수는 순서표로 구간을 못 읽을 때의 대체값이다 — 전체 인접 거리 중앙값 ÷ 표정속도(도시철도 1,195 m ÷ 32 km/h,
# 버스 362 m ÷ 18 km/h). 버스는 원천이 대개 예측 초를 줘 이 값으로 떨어지는 일이 드물고, 실측으로 확인하지 않았다.
RAIL_S_PER_STATION = 134.0
BUS_S_PER_STOP = 72.0


def first_ride(steps):
    """경로의 첫 승차 구간 번호 — 없으면 None."""
    return next((i for i, s in enumerate(steps) if s["type"] in RIDE_TYPES), None)


def _headsign_toward(item):
    """도시철도 행선 안내 '왕십리행 - 이매방면' → 다음 역 이름 '이매'. 없으면 None."""
    text = item.get("headsign") or ""
    if "-" not in text:
        return None
    tail = text.rsplit("-", 1)[1].strip()
    return tail[:-2].strip() if tail.endswith("방면") else None


def subway_toward(lines, live, board_id, alight_id):
    """승차역에서 하차역 쪽으로 가는 열차가 달고 오는 '○○방면' 이름들 — 승차역의 그 방향 이웃 역 실시간 역명.

    순서표 덩어리에는 방향이 없어(§6.6) 상·하행 표기로는 어느 열차를 기다리는지 가를 수 없다. 대신 행선 안내의 '방면' 이
    다음 역이라, 하차역 쪽 이웃 역 이름과 맞추면 방향이 정해진다(서현역 실측: 왕십리행 - 이매방면 / 인천행 - 수내방면).
    두 역을 함께 담은 덩어리가 없으면 빈 집합이다 — 방향을 모르는 것이지 열차가 없는 것이 아니다.
    """
    names = set()
    for cid in lines.chains_for(None):
        seq = lines.sequence(cid)
        if board_id not in seq or alight_id not in seq:
            continue
        i, j = seq.index(board_id), seq.index(alight_id)
        if i == j:
            continue
        step = 1 if j > i else -1
        if lines.is_loop(cid) and abs(j - i) > len(seq) / 2:   # 순환선은 짧은 쪽으로 돈다
            step = -step
        nb = seq[(i + step) % len(seq)]
        got = live.live_for(nb) if live is not None else None
        names.add(got[0] if got else lines.station(nb)[0].removesuffix("역"))
    return names


def subway_upstream_s(lines, board_id, alight_id, k_max=12):
    """승차역으로 오는 열차가 지나올 역 사이 누적 시간 → [1역 전, 2역 전, …] 초. 못 읽으면 빈 목록.

    하차역 쪽이 열차가 갈 방향이므로 그 반대쪽이 열차가 오는 쪽이다. 구간 거리는 순서표 좌표의 직선이고 표정속도(`RAIL_KMH`)로
    시간으로 바꾼다 — 분당선 3역 실측 역당 중앙 159초, 순서표 거리로 잡은 값 156초(E19). 원본·이어붙임 덩어리가 함께 담으면
    더 멀리까지 읽히는 쪽을 쓴다(덩어리 끝에서 끊기지 않게).
    """
    best = []
    for cid in lines.chains_for(None):
        seq = lines.sequence(cid)
        if board_id not in seq or alight_id not in seq:
            continue
        i, j = seq.index(board_id), seq.index(alight_id)
        if i == j:
            continue
        step = 1 if j > i else -1
        loop = lines.is_loop(cid)
        if loop and abs(j - i) > len(seq) / 2:
            step = -step
        total, prev, out = 0.0, i, []
        for k in range(1, k_max + 1):
            nxt = i - step * k
            if loop:
                nxt %= len(seq)
            elif not 0 <= nxt < len(seq):
                break
            a, b = lines.station(seq[prev]), lines.station(seq[nxt])
            total += haversine_m(a[2], a[1], b[2], b[1])
            out.append(total / 1000.0 / RAIL_KMH * 3600.0)
            prev = nxt
        if len(out) > len(best):
            best = out
    return best


def arrival_s(item, kind):
    """실시간 항목 → 차가 정류장에 닿기까지 초 (근거는 `arrival` 이 함께 준다)."""
    return arrival(item, kind)[0]


def arrival(item, kind, upstream_s=None):
    """실시간 항목 → (차가 정류장에 닿기까지 초, 근거 'prediction'|'stops') — 매기지 못하면 (None, None).

    근거를 함께 넘기는 이유: 원천의 도착 예측 초와 남은 정류소·역 수로 잡은 근사는 오차가 전혀 다르다(도시철도 실호출은
    대부분 남은 역 수만 왔다, E14). 화면이 둘을 가려 '실시간' 과 '추정' 으로 따로 보여 줄 수 있어야 한다. 예측 초가 없으면 남은 정류소·역 수로 근사하고, 둘 다 없으면 None
    (운행종료·출발대기 같은 문구뿐인 행 — 0초 뒤가 아니다).

    남은 수는 **원천이 그 값을 받은 시각의 위치**다 — 수신 경과(`age_s`)만큼 차가 이미 움직였으므로 빼고 쓴다
    (E14 실측 경과 41–281초라 '2역 전' 이 실제로는 지금 들어오는 열차일 수 있다). 빼서 음수면 이미 지나갔거나
    들어오는 중이라 모름으로 둔다 — 예측 초의 규칙과 같다. 예측 초는 파서(`realtime.subway`)가 이미 경과를 뺐다.
    수신 시각을 주지 않는 원천(버스)은 경과를 0 으로 본다."""
    if item.get("eta_s") is not None:
        return float(item["eta_s"]), "prediction"
    n = item.get("n_stops_ahead")
    if n is None:
        return None, None
    per = RAIL_S_PER_STATION if kind == "subway" else BUS_S_PER_STOP
    # 지나올 구간 거리(`subway_upstream_s`)가 있으면 그것으로, 목록보다 멀거나 없으면 공통 상수로
    travel = upstream_s[n - 1] if upstream_s and 1 <= n <= len(upstream_s) else n * per
    left = travel - (item.get("age_s") or 0)
    return (left, "stops") if left >= 0 else (None, None)


def realtime_first_wait(step, items, walk_to_stop_s, toward=None, upstream_s=None):
    """첫 승차 정류장의 실시간 도착 → (대기 초, 출처) — 매기지 못하면 (None, 사유).

    **걸어서 정류장에 닿기 전에 오는 차는 탈 수 없다.** 도착까지 남은 시간이 도보 시간보다 짧은 차는 빼고, 그 뒤 첫 차까지를
    대기로 본다. 경로가 이 구간에 준 차만 센다 — 버스는 노선 id(실측: 실시간 항목의 노선 id 가 접지의 노선 id 와 같다),
    도시철도는 하차역 쪽 '방면'. 여럿이면 먼저 오는 차를 탄다. 알려진 차를 모두 놓치면 마지막으로 알려진 차 뒤로
    배차간격만큼씩 이어 다음 차를 잡는다(실시간 + 정적 배차). 배차도 모르면 매기지 않는다.

    출처: `realtime` = 탈 차의 도착 예측 초 · `realtime_stops` = 탈 차의 남은 정류소·역 수로 잡은 근사 ·
    `realtime+headway` = 알려진 차를 모두 놓쳐 배차간격으로 이은 추정. 앞의 것만 '실시간' 이라 부를 수 있다.
    """
    kind = "subway" if step["type"] == "SUBWAY" else "bus"
    if kind == "bus":
        want = set(step.get("route_ids") or ())
        mine = [it for it in items if it.get("route_id") in want]
    else:
        if not toward:
            return None, "direction_unknown"
        mine = [it for it in items if _headsign_toward(it) in toward]
    arrivals = sorted(a for a in (arrival(it, kind, upstream_s) for it in mine) if a[0] is not None)
    if not arrivals:
        return None, "no_prediction" if mine else "no_match"
    catch = [a for a in arrivals if a[0] >= walk_to_stop_s]
    if catch:
        at, basis = catch[0]
        return at - walk_to_stop_s, "realtime" if basis == "prediction" else "realtime_stops"
    headway_s = (step.get("headway_m") or 0) * 60
    if not headway_s:
        return None, "missed_all"
    last = arrivals[-1][0]
    later = last + math.ceil((walk_to_stop_s - last) / headway_s) * headway_s
    return later - walk_to_stop_s, "realtime+headway"


def with_realtime_first_wait(routes, o, arrivals, toward=None, upstream=None):
    """기준 경로 사본 — 첫 승차 구간의 대기를 실시간으로 바꾸고 경로 대기 합을 다시 낸다 → (경로들, 진단).

    `arrivals` 는 {첫 승차 지점 id: 실시간 항목 목록}, `toward` 는 {(승차역 id, 하차역 id): 방면 이름 집합},
    `upstream` 은 같은 키로 `subway_upstream_s` 결과(없으면 남은 역 수를 공통 상수로 바꾼다).
    정류장까지의 도보는 첫·끝 도보와 같은 추정(직선 × 우회율 ÷ 속도, E15)이다. 매기지 못한 경로는 정적 배차 대기를 둔다.
    **원본은 건드리지 않는다** — 택시로 정류장에 닿는 뒷부분에는 걸어서 닿는다는 전제의 이 값이 맞지 않는다.
    """
    out, diag = [], Counter()
    for route in routes:
        r = dict(route, steps=[dict(s) for s in route["steps"]])
        apply_wait_defaults(r)
        out.append(r)
        i = first_ride(r["steps"])
        step = r["steps"][i] if i is not None else None
        board = _chosen(step, "board") if step else None
        if board is None or board["id"] not in arrivals:
            diag["no_arrivals"] += 1
            continue
        alight = _chosen(step, "alight")
        walk = walk_s(haversine_m(o[0], o[1], board["lon"], board["lat"]))
        pair = (board["id"], alight["id"] if alight else None)
        wait, source = realtime_first_wait(step, arrivals[board["id"]], walk, (toward or {}).get(pair),
                                           (upstream or {}).get(pair))
        diag[source] += 1
        if wait is None:
            continue
        step["static_wait_source"] = step.get("wait_source")
        step["static_wait_s"], step["wait_s"] = step.get("wait_s"), round(wait)
        step["wait_source"], step["walk_to_stop_s"] = source, round(walk)
        rides = [s for s in r["steps"] if s["type"] in RIDE_TYPES]
        r["wait_s"] = sum(s["wait_s"] for s in rides) if all(s.get("wait_s") is not None for s in rides) else None
        r["total_with_wait_s"] = (r["total_time_s"] + r["wait_s"]
                                  if r["wait_s"] is not None and r.get("total_time_s") is not None else None)
    return out, dict(diag)


# --- 경로 위 정류소 ---

def route_points(route, o):
    """경로를 따라 내릴 수 있는 정류장 → [{t, lon, lat, id, name, kind, step, last}] (t = 출발부터 그 정류장에 닿는 초).

    블랙박스는 구간 시간만 주므로, 구간 안 중간 정류장에 닿는 시각은 구간 시간을 정류장 사이 직선거리 비율로 나눠 잡는다.
    출발 → 첫 승차 도보와 대기(첫 승차는 실시간 사본이면 실시간 값)를 앞에 더한다. 위치를 추정만 한 정류장
    (`stop_locs` 의 id 가 빈 것)은 거리 비율에는 쓰고 내릴 곳으로는 내놓지 않는다.
    """
    steps = route["steps"]
    rides = [i for i, s in enumerate(steps) if s["type"] in RIDE_TYPES]
    first = _edge_stop(steps, "board")
    t = walk_s(haversine_m(o[0], o[1], first["lon"], first["lat"])) if first else 0.0
    out = []
    for i, s in enumerate(steps):
        if s["type"] not in RIDE_TYPES:
            t += s.get("time_s") or 0
            continue
        t += s.get("wait_s") or 0
        locs, names = s.get("stop_locs") or [], s.get("stops") or []
        if not any(locs):                         # 위치 복원이 없으면 승·하차만
            ends = [_chosen(s, "board"), _chosen(s, "alight")]
            locs, names = ends, [e["name"] if e else "" for e in ends]
        pts = [(k, loc) for k, loc in enumerate(locs) if loc]
        cum = [0.0]
        for (_, a), (_, b) in zip(pts, pts[1:]):
            cum.append(cum[-1] + haversine_m(a["lon"], a["lat"], b["lon"], b["lat"]))
        ride, total = s.get("time_s") or 0, cum[-1] or 1.0
        for n, ((k, loc), c) in enumerate(zip(pts, cum)):
            if loc.get("id"):
                out.append({"t": t + ride * c / total, "lon": loc["lon"], "lat": loc["lat"], "id": loc["id"],
                            "name": names[k] if k < len(names) and names[k] else loc["id"],
                            "kind": "subway" if s["type"] == "SUBWAY" else "bus", "step": i,
                            "fraction": c / total, "stop_index": k,
                            "last": i == rides[-1] and n == len(pts) - 1})
        t += ride
    return out


def prefix_steps(route, point):
    """경로 위 정류소(`route_points` 의 한 점)에서 내릴 때까지의 구간 사본. 중간 하차면 그 구간의 거리·시간을
    `route_points` 와 같은 거리 비율로 자르고, 정류장 목록·경로선도 그 정류소에서 끊는다."""
    i, fraction = point["step"], point["fraction"]
    steps = route["steps"][:i]
    if fraction <= 0:
        return steps
    step = dict(route["steps"][i])
    step["time_s"] = (step.get("time_s") or 0) * fraction
    if step.get("distance_m") is not None:
        step["distance_m"] *= fraction
    step["alight_name"] = point["name"]
    for key in ("stops", "stop_locs"):
        if step.get(key):
            step[key] = step[key][:point["stop_index"] + 1]
    if step.get("path"):
        near = min(range(len(step["path"])),
                   key=lambda n: haversine_m(step["path"][n][0], step["path"][n][1], point["lon"], point["lat"]))
        step["path"] = step["path"][:near + 1]
    return steps + [step]
