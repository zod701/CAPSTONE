"""앵커 후보 생성과 가지치기 — Tier 1 오프라인 (쿼터를 쓰지 않는다).

앵커 = 택시와 대중교통이 갈아타는 정류소·역이다. 수도권 정류소가 5만 곳이고 대중교통 경로 쿼터는 하루
1,000콜이라 전수 검증이 불가능하다. 여기서는 정적 데이터(정류소 좌표·노선 순서표·배차)만으로 후보를
수십 개까지 줄이고, 확정 값은 온라인 검증(Tier 2)이 준다 → method.md §2.4, §3.

**택시 근사에 요금이 들어가는 이유**는 method.md R23 이다. 점수를 시간만으로 매기면, 정차까지 넣은 버스
실효 속도(~17 km/h)가 택시(~30 km/h)보다 느려 **택시를 오래 탈수록 점수가 단조 감소**한다. 그래서 상위 N 이
전부 택시 도달 반경 경계의 같은 한 지점으로 붕괴한다(배곧→안산 실측: 상위 6개가 모두 택시 21.1분인 같은
정류소). 비용 축이 비면 Pareto 도 선호 3단계도 후보 단계에서 죽는다.

요금은 공개 요금 체계를 그대로 옮긴 근사다. method.md R13 이 기각한 것은 *최종 요금*을 산식으로 계산하는
것이고(심야·시계외 결합, 읍면 복합할증이 공개 자료로 확정되지 않는다), 여기서 쓰는 것은 **순위용 근사**다.
확정 요금은 Tier 2 의 자동차 경로 응답이 실시간 교통·할증까지 반영해 준다 → method.md §7.1.
"""
import csv
import math
from collections import Counter
from datetime import timedelta
from pathlib import Path

from geoutil import haversine_m

from app.stopsdb import ID_COL
# 대기 규칙은 백엔드와 한 벌만 둔다 — 빈도 합과 '같은 이름 노선은 한 노선' (method.md §7.5, R21) 을
# 여기서 다시 쓰면 반드시 어긋난다. 비공개 이름이지만 규칙 자체가 공유 계약이라 그대로 가져다 쓴다.
from app.wait import WEEKDAY, _bus_headway, day_type, rail_hour

# --- 택시 시간 근사 ---
# 시간 = 고정 비용 + 직선 1 km 당 초. 가지치기는 순위만 맞으면 되고 절대값은 Tier 2 가 준다 — 그런데 순위에 물리는 것은
# **기울기**다(고정 비용은 모든 택시 후보에 똑같이 붙는다). 원점을 지나는 '직선 × 우회계수 ÷ 속도' 는 출발·신호 같은
# 고정 비용을 기울기에 떠안겨 택시 1 km 를 실제보다 비싸게 매기고, 구간에 따라 편향이 뒤집힌다(짧으면 빠르게, 길면 느리게).
# 값은 2026-09-14(월) 19:37 비혼잡·비할증 시각, 서울·경기 정류소 쌍 150개 — 직선 1–6 km, 택시 구간이 실제로 쓰이는
# 범위 — 의 자동차 경로 응답에 맞췄다. 절대오차 중앙 2.4분 · p90 6.6분 (원점 모형 2.9 · 7.7분, 처음 둔 상식값
# 1.35 · 30 km/h 는 편향 −5.0분으로 택시를 빠르게 봐 택시를 오래 타는 앵커가 부당하게 유리했다) → method.md E18.
# `cli.py calibrate` 로 다시 잴 수 있다.
TAXI_BASE_S = 236.0
TAXI_S_PER_KM = 178.0
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
    return haversine_m(lon1, lat1, lon2, lat2) * DETOUR / 1000.0


def taxi_time_s(lon1, lat1, lon2, lat2):
    """택시 소요 시간 근사 (초) — 고정 비용 + 직선거리 비례."""
    return TAXI_BASE_S + haversine_m(lon1, lat1, lon2, lat2) / 1000.0 * TAXI_S_PER_KM


def taxi_reach_m(t_max_s):
    """t_max_s 안에 닿는 **직선거리** 상한 (m) — `taxi_time_s` 의 역함수.

    반경 검색에 쓴다. 시간과 반경을 각각 상수로 두면 계수를 고칠 때 반드시 어긋나므로 한 곳에서만 계산한다.
    """
    return max(0.0, t_max_s - TAXI_BASE_S) / TAXI_S_PER_KM * 1000.0


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
# - 호출 대기: 택시가 오기까지의 정보가 없어 1–2분의 가운데 상수로 둔다. 택시를 타는 모든 곳(A 는 출발지, B·C·D 는 경로 중간)에 붙인다
# - 연결 버퍼: 내려서 정류장·승강장까지 가는 몫만 둔다. 처음에는 계획서 §8 대로 'ETA × 1.2 + 3분' 이었다 — ETA 에 비례하는 흔들림은
#   대중교통 구간에는 붙이지 않으므로 택시에만 붙이면 택시가 한쪽으로 불리해진다. 비례분을 빼고 3분을 60초로 줄였다
#   (1.2배·3분 모두 실측값이 아니었고, 오프라인 근사를 넣으면 버퍼는 사실상 3.8분 + 직선 1 km 당 36초인 상수였다)
TAXI_PICKUP_S = 90.0
TAXI_BUFFER_S = 60.0
# 최소 기준 — 같은 정류장·옆 정류장으로 가는 택시처럼 너무 짧은 거리를 택시로 바꾸는 후보가 섞였다.
# 거리: 표준형 택시 기본거리 1.6 km(도로)를 직선으로 — 이보다 짧으면 기본요금을 다 내고 걷거나 버스로 갈 거리를 간다.
# 절약: 택시가 대신한 부분보다 이만큼은 빨라야 한다(호출 대기·버퍼 포함). 대신한 부분을 모르는 후보는 검증 뒤 기준선과 견준다.
TAXI_MIN_M = FARE_TYPES["표준"][0] * 1000.0 / DETOUR
TAXI_MIN_SAVING_S = 300.0


def taxi_plan_s(eta_s, connect=True):
    """택시 구간에 잡는 시간(초) = 호출 대기 + ETA + (내려서 차를 타면) 연결 버퍼. ETA 가 없으면 고정분만 남는다."""
    return TAXI_PICKUP_S + (eta_s or 0) + (TAXI_BUFFER_S if connect else 0.0)


def taxi_connect_s(eta_s):
    """내려서 대중교통을 타는 택시 구간(A, 뒤에 탈 차가 남은 C·D)에 잡는 시간 — `taxi_plan_s(eta, connect=True)`.

    택시로 목적지까지 가는 구간(B, 마지막 구간 D)은 `taxi_plan_s(eta, connect=False)` 를 쓴다 — 놓칠 차가 없다.
    """
    return taxi_plan_s(eta_s, connect=True)


# --- 승차 시간 근사 ---
# 정차를 포함한 표정속도(km/h)다 — 정차 시간을 따로 더하지 않는다. 노선 유형은 순서표의 `route_type`
# (카카오 버스 유형 이름에 맞춘 값). 기준 경로의 실제 구간 시간과 견주어 고칠 수 있다.
BUS_KMH = {"마을": 15.0, "순환": 15.0, "지선": 18.0, "일반": 18.0, "간선": 20.0,
           "광역": 32.0, "직행": 32.0, "시외": 40.0, "공항": 40.0}
BUS_KMH_DEFAULT = 18.0
# 후보로 쓰지 않는 노선 유형. 수요응답형(똑버스·DRT)은 앱 호출·예약으로 구역 안을 달리는 버스라 정해진 노선도 배차도 없다 —
# 순서표 64노선 중 39개는 구역 끝 두 정류장만 적힌 행이고(두 정류장 사이 중앙 190 m), 배차 값은 232행 중 19행뿐이다.
# 카카오 대중교통 경로에는 나오지 않고(화성똑버스06-2 구역 안 OD 실측) 우리 순서표로만 들어오므로 노선을 모으는 단계에서 뺀다.
# 순서표 자체에서는 지우지 않는다 — 데모 웹이 정류장 경유 노선으로 보여 준다.
EXCLUDED_ROUTE_TYPES = frozenset({"수요응답"})
RAIL_KMH = 32.0          # 급행도 일반 기준으로 둔다 — 과대평가하지 않는 쪽(도시철도 구간의 43%가 급행, E17)
RIDE_DETOUR_MAX = 1.6    # 승차 구간이 직선 대비 이보다 돌면 반대 방향 짝이다(경기 순서표는 상·하행이 한 순번열)

# 도보는 블랙박스 자신의 값에서 역산한 계수를 쓴다 → method.md E15
WALK_MPS = 0.83
WALK_DETOUR = 1.39

# 걷는 쪽 끝의 반경 — A 면 D 쪽, B 면 O 쪽이다. 비면 단계적으로 넓힌다
# (외곽에서는 400 m 안에 정류소가 하나도 없는 곳이 있다 — 실측).
WALK_RADIUS_M = (400.0, 800.0, 1500.0)
T_MAX_S = 15 * 60        # 택시로 갈아타러 가는 시간의 상한

# 시간가치(원/분) — **잠정값이다**(plan.md §12 에서 확정한다). 후보 점수와 일반화 비용에 같은 값을 쓴다.
VOT = {"time": 500.0, "balance": 300.0, "cost": 150.0}


def walk_s(dist_m):
    """직선거리를 도보 시간(초)으로 — 우회율을 곱하고 속도로 나눈다."""
    return dist_m * WALK_DETOUR / WALK_MPS


def ride_s(km, route_type=None, kind="bus"):
    """노선을 타고 가는 시간(초) 근사 — 운행 거리를 표정속도로 나눈다."""
    kmh = RAIL_KMH if kind == "subway" else BUS_KMH.get(route_type or "", BUS_KMH_DEFAULT)
    return km / kmh * 3600.0


def transit_fare(route_type=None, kind="bus"):
    """대중교통 요금 근사(원) — 노선 유형만 보는 값이다. 거리비례·환승할인은 검증이 준다."""
    if kind == "subway":
        return TRANSIT_FARE_DEFAULT
    return TRANSIT_FARE.get(route_type or "", TRANSIT_FARE_DEFAULT)


# 노선 유형별 대중교통 요금 근사(원) — 순위용이다. 확정 요금은 검증 단계의 응답이 준다.
# 넣지 않으면 요금이 몇 배인 노선이 공짜처럼 보인다 — 배곧 → 안산 중앙역의 B 방향에서 공항버스(N7000)가
# 상위를 차지했다. 택시 요금이 지배적인 A 방향에서는 가려져 있던 결함이다.
TRANSIT_FARE = {"공항": 8000.0, "시외": 4000.0, "광역": 2800.0, "직행": 2800.0}
TRANSIT_FARE_DEFAULT = 1450.0

# 배차를 모르는 노선의 대기 — 0 으로 두면 배차를 모르는 노선이 아는 노선보다 유리해진다.
# 버스 배차표(headway_bus.csv) 평일 행의 대표 배차(최소·최대의 중간값) 중앙 30분의 절반이다 — 값이 있는 3,764행, 2026-09-15 계산
# (경기 40분 · 서울 18분). 처음 둔 450초는 근거 없이 적은 값으로 평일 하위 25%(15분)의 절반이었다 — 모르는 대기를 짧게 잡아
# 배차를 모르는 노선을 유리하게 만들었다. 노선 단위 중앙이라 경로에 자주 나오는 간선보다 긴 쪽으로 치우칠 수 있다.
WAIT_UNKNOWN_S = 900.0


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
            if wins:
                out.setdefault(r["route_id"], {})[r["day_type"]] = wins
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


# --- 승차 구간 ---

def _span(ob, oa):
    """승차 → 하차 운행 순서 구간 중 가장 짧은 (승차 순서, 하차 순서). 없으면 None.

    `resolver._gap` 과 같은 규칙(최소 양수 간격)이되, 거리를 재려면 간격이 아니라 양끝이 필요하다.
    회차해 다시 지나는 정류장은 순서가 둘이라 조합을 다 본다.
    """
    spans = [(x, y) for x in ob for y in oa if y > x]
    return min(spans, key=lambda s: s[1] - s[0]) if spans else None


def _path_km(points):
    """좌표 목록 [(lat, lon)…] 을 이은 길이 (km)."""
    return sum(haversine_m(a[1], a[0], b[1], b[0])
               for a, b in zip(points, points[1:])) / 1000.0


def _ride_km(routes, rid, i, j):
    """버스 노선을 타고 운행 순서 i → j 로 가는 거리 (km) — 순서표 좌표를 이어 잰다."""
    seq = routes.sequence(rid)
    return _path_km([routes.stop(k)[1:] for k in seq[i:j + 1]])


def _direct_enough(ride_km, straight_km):
    """승차 구간이 직선 대비 지나치게 돌지 않는가 — 반대 방향 짝을 거른다.

    경기 순서표는 상행 뒤에 하행이 한 순번열로 이어져(E6) 길 건너 반대 방향 정류장 쌍도 순서 간격이 양수다.
    그 짝은 노선을 거의 한 바퀴 돌아가므로 거리로 걸린다. 아주 가까운 짝은 비율이 의미가 없어 통과시킨다.
    """
    return straight_km < 0.5 or ride_km <= straight_km * RIDE_DETOUR_MAX


def _chain_km(lines, cid, i, j):
    """도시철도 덩어리의 순번 i ↔ j 구간 거리 (km). 방향은 가리지 않는다."""
    seq = lines.sequence(cid)
    a, b = (i, j) if i <= j else (j, i)
    return _path_km([lines.station(s)[1:] for s in seq[a:b + 1]])


# --- 후보 생성 ---

def _near(stops, kind, pt, radii):
    """기준점 반경 안의 정류소·역 → {id: (행, 거리 m)}. 비면 다음 반경으로 넓힌다.

    미정차(가상) 정류소는 `StopsDB.near` 가 기본으로 뺀다. 환승역은 운영 노선별로 행이 여럿이라
    같은 실체가 여러 id 로 들어온다 — 짝을 만들 때 phys_id 로 접는다.
    """
    for r in radii:
        found = {}
        for i, dist in stops.near(kind, pt[0], pt[1], r):
            row = stops.row(kind, i)
            found[row[ID_COL[kind]]] = (row, dist)
        if found:
            return found
    return {}


def _sgg_at(*near_maps):
    """기준점에서 가장 가까운 정류소의 시군 — 택시 요금 유형과 시계외 판정에 쓴다."""
    best = None
    for m in near_maps:
        for row, dist in m.values():
            if best is None or dist < best[0]:
                best = (dist, row.get("sgg_nm"))
    return best[1] if best else None


def _bus_pairs(routes, near_o, near_d):
    """O 근처와 D 근처를 모두 지나는 노선의 (노선 정보, 승차 키, 하차 키, 승차 순서, 하차 순서).

    교집합을 먼저 잡아 노선을 줄인다 — 경기 외곽 OD 에서 O 근처 노선 수백 개가 10–42개로 떨어진다.
    반대 방향 짝도 순서 간격이 양수로 나오므로(경기 순서표는 상행 뒤 하행이 한 순번열) 거르는 것은 호출자가 한다.
    """
    at_o, at_d = {}, {}
    for keys, out in ((near_o, at_o), (near_d, at_d)):
        for k in keys:
            for r in routes.routes_at(k):
                if r["type"] in EXCLUDED_ROUTE_TYPES:      # 수요응답형 — 정해진 노선·배차가 없다
                    continue
                out.setdefault(r["id"], (r, []))[1].append(k)
    pairs = []
    for rid in at_o.keys() & at_d.keys():
        order = routes.order_of(rid)
        brief, o_keys = at_o[rid]
        for p in o_keys:
            for q in at_d[rid][1]:
                span = _span(order[p], order[q])
                if span:
                    pairs.append((brief, p, q, *span))
    return pairs


def _rail_pairs(lines, near_o, near_d, phys):
    """같은 덩어리에 O 근처 역과 D 근처 역이 함께 있으면 짝 → (덩어리, 승차 역, 하차 역, 역 수).

    **덩어리에는 방향이 없다**(method.md §6.6) — 어느 쪽 순번이 앞인지로 거르지 않는다. 기재 방향이 임의라
    그렇게 거르면 노선군에 따라 후보가 통째로 사라진다(수지구청 → 서현이 `bundang` 에서 탈락한다).
    원본과 이어붙임 덩어리를 모두 보되(이어붙임은 끝끼리만 이어 경인선처럼 빠지는 구간이 있다)
    같은 실체(phys_id) 짝은 역 수가 가장 적은 것 하나만 남긴다.
    """
    phys = phys or {}
    best = {}
    for cid in lines.chains_for(None):
        seq = lines.sequence(cid)
        idx = {sid: i for i, sid in enumerate(seq)}
        on_o = [s for s in near_o if s in idx]
        on_d = [s for s in near_d if s in idx]
        for p in on_o:
            for q in on_d:
                if phys.get(p, p) == phys.get(q, q):
                    continue
                n = abs(idx[q] - idx[p])
                if lines.is_loop(cid):
                    n = min(n, len(seq) - n)
                key = (phys.get(p, p), phys.get(q, q))
                if key not in best or n < best[key][3]:
                    best[key] = (cid, p, q, n)
    return list(best.values())


def _wait_s(cand, best, headway, routes, day, at):
    """승차 지점에서 차를 기다리는 시간(초)과 유효 배차(분). 배차를 모르면 (None, None).

    같은 승차 지점에서 탈 수 있는 노선이 여럿이면 먼저 오는 차를 타므로 빈도를 더한다 — 규칙은 `wait.py` 와 한 벌이다.
    **B 는 앵커가 하차 지점이라** 한 앵커에 승차 지점이 여럿 딸린다. 승차 지점이 다르면 함께 기다릴 수 없으므로
    고른 노선과 **같은 승차 지점**의 것만 센다.
    """
    if headway is None:
        return None, None
    if cand["kind"] == "bus":
        rides = [r["id"] for r in cand["rides"] if r["board"] == best["board"]]
        h = _bus_headway(headway, rides, day, routes)
    else:
        hour = rail_hour(at) if at is not None else None
        h = (headway.rail(best["board"], best["board_group"], hour)
             if hour is not None and best.get("board_group") else None)
    return (h * 60 / 2 if h is not None else None), h


def _new_cand(kind, hybrid, anchor, row, far, sgg_far, at):
    """앵커 하나의 빈 후보 — 택시 구간은 앵커가 정해지면 바로 정해진다.

    A 는 O 에서 앵커(승차 지점)까지, B 는 앵커(하차 지점)에서 D 까지 택시를 탄다.
    요금 유형과 심야 할증은 **택시를 타는 곳** 기준이다(§7.1).
    """
    pt = (row["lon"], row["lat"])
    a, b = (far, pt) if hybrid == "A" else (pt, far)
    sgg_from, sgg_to = ((sgg_far, row.get("sgg_nm")) if hybrid == "A"
                        else (row.get("sgg_nm"), sgg_far))
    km = taxi_dist_km(a[0], a[1], b[0], b[1])
    return {"strategy": "b", "hybrid": hybrid, "kind": kind, "anchor": anchor, "name": row["name"],
            "lat": row["lat"], "lon": row["lon"], "sgg_nm": row.get("sgg_nm"),
            "line_group": row.get("line_group"),
            "taxi_km": km, "taxi_s": taxi_time_s(a[0], a[1], b[0], b[1]),
            "fare": taxi_fare(km, sgg_from, sgg_to, at), "rides": []}


def _ride(brief_id, name, rtype, p, p_row, q, q_row, n_stops, km, walk_m, kind="bus"):
    """앵커에 딸리는 승차 한 건 — 어디서 타고 어디서 내리는지, 그 구간이 얼마나 걸리는지."""
    return {"id": brief_id, "name": name, "type": rtype,
            "board": p, "board_name": p_row["name"], "board_group": p_row.get("line_group"),
            "alight": q, "alight_name": q_row["name"], "n_stops": n_stops,
            "ride_km": km, "ride_s": ride_s(km, rtype, kind=kind), "walk_s": walk_s(walk_m),
            "fare": transit_fare(rtype, kind=kind)}


def propose_backtrack(stops, routes, lines, o, d, *, vot, hybrid="A", at=None, headway=None,
                      phys=None, hours=None, t_max_s=T_MAX_S, walk_radius_m=WALK_RADIUS_M):
    """(b) 직행 노선 역추적 → (점수 오름차순 후보, 진단). method.md §3.2·§3.7.

    - `hybrid="A"` — O 에서 **택시로** 승차 지점까지 가고, 거기서 D 근처로 곧장 가는 노선을 탄다(first-mile).
    - `hybrid="B"` — O 근처에서 **걸어서** 타고, D 근처 하차 지점에서 택시로 마무리한다(last-mile).
      §3.2 의 대칭판이다. 기준 경로에 나오지 않은 노선으로 D 쪽까지 가는 경우는 (a) 섭동이 볼 수 없다 —
      목적지가 대중교통 사각지대일 때가 바로 그 경우다.

    두 방향은 **반경을 맞바꾼 같은 알고리즘**이다. 택시를 타는 쪽 끝에 택시 도달 반경을, 걷는 쪽 끝에 도보 반경을 준다.
    후보는 **앵커 단위로 묶는다** — 검증 콜이 앵커마다 하나씩 들기 때문이다(A 는 승차 지점, B 는 하차 지점).
    전부 오프라인이라 쿼터를 쓰지 않는다.
    """
    day = day_type(at) if at is not None else WEEKDAY
    reach = (taxi_reach_m(t_max_s),)
    r_o, r_d = (reach, walk_radius_m) if hybrid == "A" else (walk_radius_m, reach)
    near_o = {k: _near(stops, k, o, r_o) for k in ("bus", "subway")}
    near_d = {k: _near(stops, k, d, r_d) for k in ("bus", "subway")}
    far = o if hybrid == "A" else d            # 택시의 반대쪽 끝 (앵커가 아닌 쪽)
    sgg_far = (_sgg_at(near_o["bus"], near_o["subway"]) if hybrid == "A"
               else _sgg_at(near_d["bus"], near_d["subway"]))
    diag, by_anchor = Counter(), {}

    for brief, p, q, i, j in _bus_pairs(routes, near_o["bus"], near_d["bus"]):
        diag["bus_pairs"] += 1
        if not in_service(hours, brief["id"], day, at):
            diag["dropped_service"] += 1
            continue
        km = _ride_km(routes, brief["id"], i, j)
        p_row, q_row = near_o["bus"][p][0], near_d["bus"][q][0]
        straight = haversine_m(p_row["lon"], p_row["lat"], q_row["lon"], q_row["lat"]) / 1000.0
        if not _direct_enough(km, straight):
            diag["dropped_detour"] += 1
            continue
        akey, arow = (p, p_row) if hybrid == "A" else (q, q_row)
        if (near_o["bus"][p][1] if hybrid == "A" else near_d["bus"][q][1]) < TAXI_MIN_M:
            diag["too_short"] += 1
            continue
        walk_m = near_d["bus"][q][1] if hybrid == "A" else near_o["bus"][p][1]
        cand = by_anchor.get(("bus", akey)) or _new_cand("bus", hybrid, akey, arow, far, sgg_far, at)
        by_anchor[("bus", akey)] = cand
        cand["rides"].append(_ride(brief["id"], brief["name"], brief["type"],
                                   p, p_row, q, q_row, j - i, km, walk_m))

    for cid, p, q, n in _rail_pairs(lines, near_o["subway"], near_d["subway"], phys):
        diag["rail_pairs"] += 1
        p_row, q_row = near_o["subway"][p][0], near_d["subway"][q][0]
        seq = lines.sequence(cid)
        km = _chain_km(lines, cid, seq.index(p), seq.index(q))
        akey, arow = (p, p_row) if hybrid == "A" else (q, q_row)
        if (near_o["subway"][p][1] if hybrid == "A" else near_d["subway"][q][1]) < TAXI_MIN_M:
            diag["too_short"] += 1
            continue
        walk_m = near_d["subway"][q][1] if hybrid == "A" else near_o["subway"][p][1]
        cand = by_anchor.get(("subway", akey)) or _new_cand("subway", hybrid, akey, arow, far, sgg_far, at)
        by_anchor[("subway", akey)] = cand
        cand["rides"].append(_ride(cid, p_row.get("line_group"), "지하철",
                                   p, p_row, q, q_row, n, km, walk_m, kind="subway"))

    out = []
    for cand in by_anchor.values():
        # 최선 노선도 점수와 같은 잣대(일반화 비용)로 고른다 — 시간만 보면 비싼 노선이 뽑힌다
        best = min(cand["rides"], key=lambda r: r["ride_s"] + r["walk_s"] + r["fare"] / vot * 60.0)
        wait, headway_m = _wait_s(cand, best, headway, routes, day, at)
        cand["wait_s"], cand["headway_m"] = wait, (round(headway_m, 1) if headway_m else None)
        cand["ride_s"], cand["ride_km"], cand["walk_s"] = best["ride_s"], best["ride_km"], best["walk_s"]
        cand["board"], cand["alight"] = best["board"], best["alight"]
        cand["ride_id"], cand["ride_name"] = best["id"], best["name"]
        cand["transit_fare"] = best["fare"]
        cand["score_s"] = (taxi_plan_s(cand["taxi_s"], connect=hybrid == "A") + (wait if wait is not None else WAIT_UNKNOWN_S)
                           + best["ride_s"] + best["walk_s"]
                           + (cand["fare"] + best["fare"]) / vot * 60.0)
        out.append(cand)
    out.sort(key=lambda c: c["score_s"])
    info = dict(diag)   # Counter.update 는 값을 더한다 — 여기서는 덮어써야 한다
    info.update({"hybrid": hybrid, "anchors": len(out),
                 "near_o_bus": len(near_o["bus"]), "near_d_bus": len(near_d["bus"]),
                 "near_o_rail": len(near_o["subway"]), "near_d_rail": len(near_d["subway"])})
    return out, info


def top_n(cands, n, max_per_route=2):
    """점수 상위 n 개 — 같은 노선만 뽑히지 않게 노선마다 max_per_route 개까지 받는다.

    한 노선이 지나는 정류소가 여럿이면 상위가 그 노선 하나로 채워진다(동탄 → 경기도청 실측: 2–6위가
    모두 `7200` 한 노선이었다). 검증 콜은 앵커마다 하나씩 드는데 같은 노선을 타는 후보끼리는 알려 주는 것이
    겹친다 — 쿼터를 쓰는 만큼 서로 다른 노선을 보는 편이 낫다.
    """
    seen, out = Counter(), []
    for cand in cands:
        rid = cand["ride_id"]
        if seen[rid] >= max_per_route:
            continue
        seen[rid] += 1
        out.append(cand)
        if len(out) >= n:
            break
    return out


# --- (a) 기준 경로 섭동 ---

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


def propose_perturb(o, d, transit_routes, *, vot, sgg_o=None, sgg_d=None, at=None, t_max_s=T_MAX_S,
                    hybrids=("A", "B")):
    """(a) 기준 경로 섭동 — 기준 경로의 승·하차점을 택시로 잇는다 → (점수 오름차순 후보, 진단).

    구간 경계에서 자르면 **나머지 구간의 시간이 기준 경로 응답에 이미 있다** — 후보를 매기는 데 대중교통 콜을
    더 쓰지 않는다. 다만 그 값은 O→D 최적화의 부산물이지 P→D 의 최적 경로가 아니라 **비관적 상한**이다.
    요금은 경로 단위로만 와서 자른 경로의 요금을 알 수 없다(구간별 요금이 없고 택시→버스는 환승할인도 아니다).
    B 유형은 기준 경로의 진부분집합이라 기준 요금이 상한이 되지만, A 유형은 앞을 버리는 만큼 상한이 헐겁다 —
    어느 쪽이든 확정 요금은 온라인 검증이 준다.

    `hybrids` 로 만들 유형을 고른다 — **A 와 B 는 넘길 경로가 다르다.** B 는 출발지에서 걸어서 첫 정류장에 닿으므로
    첫 승차 대기를 실시간으로 바꾼 사본(`with_realtime_first_wait`)을 넘기고, A 는 택시로 정류장에 닿아 걸어서 닿는다는
    그 사본의 전제가 맞지 않으므로 원래 경로를 넘긴다. 같은 경로를 한 번에 넘기면 둘 중 하나가 틀린 대기를 쓴다.

    점수는 **일반화 비용**이다 — 택시 요금에 기준 경로의 대중교통 요금(상한)을 더해 (b)와 같은 잣대로 맞춘다.
    한쪽만 대중교통 요금을 세면 합쳐 정렬할 때 그쪽이 부당하게 불리해진다. 실제 요금 비교는 `compare` 가 한다.
    """
    diag, best = Counter(), {}
    for route in transit_routes:
        steps = route["steps"]
        fare_cap = route["fare"].get("value") or route["fare"].get("min")
        head, tail = _edge_stop(steps, "board"), _edge_stop(steps, "alight")
        for i, step in enumerate(steps):
            if step["type"] not in RIDE_TYPES:
                continue
            for role, hybrid in (("board", "A"), ("alight", "B")):
                if hybrid not in hybrids:
                    continue
                chosen = _chosen(step, role)
                if not chosen:
                    diag["no_anchor"] += 1
                    continue
                pt = (chosen["lon"], chosen["lat"])
                if hybrid == "A":
                    taxi_s, rest, edge, sgg_from, sgg_to = (
                        taxi_time_s(o[0], o[1], *pt), steps[i:], tail, sgg_o, chosen.get("sgg_nm"))
                else:
                    taxi_s, rest, edge, sgg_from, sgg_to = (
                        taxi_time_s(pt[0], pt[1], d[0], d[1]), steps[:i + 1], head,
                        chosen.get("sgg_nm"), sgg_d)
                if taxi_s > t_max_s:
                    diag["too_far"] += 1
                    continue
                straight = (haversine_m(o[0], o[1], *pt) if hybrid == "A"
                            else haversine_m(pt[0], pt[1], d[0], d[1]))
                if straight < TAXI_MIN_M:
                    diag["too_short"] += 1
                    continue
                km = (taxi_dist_km(o[0], o[1], *pt) if hybrid == "A"
                      else taxi_dist_km(pt[0], pt[1], d[0], d[1]))
                # 택시가 대신한 쪽 끝의 도보는 사라지고 반대쪽 끝 도보만 남는다 (응답에 없어 직선으로 추정한다)
                far = d if hybrid == "A" else o
                walk = walk_s(haversine_m(edge["lon"], edge["lat"], *far)) if edge else 0.0
                fare = taxi_fare(km, sgg_from, sgg_to, at)
                cand = {"strategy": "a", "hybrid": hybrid, "kind": chosen["id"] and
                        ("subway" if step["type"] == "SUBWAY" else "bus"),
                        "anchor": chosen["id"], "name": chosen["name"],
                        "lat": chosen["lat"], "lon": chosen["lon"], "sgg_nm": chosen.get("sgg_nm"),
                        "line_group": chosen.get("line_group"),
                        "taxi_km": km, "taxi_s": taxi_s, "fare": fare,
                        "wait_s": sum(s.get("wait_s") or 0 for s in rest) or None,
                        "ride_s": sum(s.get("time_s") or 0 for s in rest), "walk_s": walk,
                        "ride_id": None, "ride_name": (step.get("vehicles") or [{}])[0].get("name"),
                        "transit_fare_cap": fare_cap, "rides": []}
                cand["score_s"] = (taxi_plan_s(taxi_s, connect=hybrid == "A") + (cand["wait_s"] or WAIT_UNKNOWN_S) + cand["ride_s"]
                                   + walk + (fare + (fare_cap or 0)) / vot * 60.0)
                key = (cand["anchor"], hybrid)
                diag["pairs"] += 1
                if key not in best or cand["score_s"] < best[key]["score_s"]:
                    best[key] = cand
    out = sorted(best.values(), key=lambda c: c["score_s"])
    diag["anchors"] = len(out)
    return out, dict(diag)


# --- (a) 의 D 유형 — 공백 메우기 ---
# 공백이 이보다 짧으면 택시의 호출 대기·고정 비용(출발·신호)·연결 버퍼만으로 이미 진다 — 거리 0 인 택시도 이만큼은 든다.
GAP_MIN_S = TAXI_PICKUP_S + TAXI_BASE_S + TAXI_BUFFER_S

# 수도권 통합환승 인정 시간 — 앞 차 하차 뒤 이 시간 안에 다음 차에 타야 요금이 이어진다. **다음 차에 타는 시각** 기준으로
# 평시 30분, 21시–익일 07시 60분이다. 넘기면 다음 차에서 기본요금을 새로 낸다 — 거리 비례분은 앞뒤로 나뉘어 합이 크게
# 달라지지 않는다고 보고, 끊긴 대가를 기본요금 한 번으로 둔다.
TRANSFER_WINDOW_S = 30 * 60
TRANSFER_WINDOW_NIGHT_S = 60 * 60
TRANSFER_NIGHT = (21, 7)
TRANSFER_REBASE_FARE = TRANSIT_FARE_DEFAULT


def transfer_window_s(board_at):
    """다음 차에 타는 시각의 환승 인정 시간(초). 시각을 모르면 짧은 쪽(평시)으로 본다."""
    if board_at is None:
        return TRANSFER_WINDOW_S
    start, end = TRANSFER_NIGHT
    return TRANSFER_WINDOW_NIGHT_S if board_at.hour >= start or board_at.hour < end else TRANSFER_WINDOW_S


def transfer_fare(fare_cap, pre_ride, resume_ride, link_s, taxi_plan_s, board_at=None):
    """D 경로의 대중교통 요금과 환승이 이어지는지 → (요금, 이어짐 True/False, 이을 환승이 없으면 None).

    앞뒤로 모두 차를 탈 때만 따진다 — 첫 구간이나 마지막 구간을 메우면 끊길 환승이 없다.
    판정 시간은 앞 차 하차 → 다음 차 승차 사이(환승 도보 + 택시 + 다음 차 대기)다. 택시는 **연결 버퍼를 씌운 계획 시간**으로
    본다 — 경로를 짤 때 잡는 시간과 같은 값이고, 경계에서 끊기는 쪽으로 잡아 요금을 낮게 약속하지 않는다.
    """
    if not (pre_ride and resume_ride):
        return fare_cap, None
    kept = link_s + taxi_plan_s <= transfer_window_s(board_at)
    return (fare_cap or 0) + (0 if kept else TRANSFER_REBASE_FARE), kept


def _edge_walk_s(steps, o, d):
    """첫 승차점까지와 마지막 하차점부터의 도보 시간(초) — 응답에 없어 직선으로 추정한다(`compare.edge_walk_m` 과 같은 규칙)."""
    head, tail = _edge_stop(steps, "board"), _edge_stop(steps, "alight")
    m = 0.0
    if head:
        m += haversine_m(o[0], o[1], head["lon"], head["lat"])
    if tail:
        m += haversine_m(tail["lon"], tail["lat"], d[0], d[1])
    return walk_s(m)


def _gaps(steps):
    """경로의 공백 → [{gap, i, gap_s, removed_wait, removed_time, x, y, resume_ride, vehicle, kind}].

    - **대기**: 승차 구간의 기대 대기가 길면 그 정류장에서 기다리지 않고 택시로 그 구간의 하차 지점까지 간다.
      택시가 대기와 차내 시간을 함께 대신하므로 빠지는 시간은 둘의 합이다. 공백 판정은 대기만 본다.
    - **도보**: 두 승차 구간 사이의 환승 도보가 길면 앞 하차 지점에서 뒤 승차 지점까지 택시로 간다.
    """
    rides = [i for i, s in enumerate(steps) if s["type"] in RIDE_TYPES]

    def walks(a, b, skip=None):
        return sum(steps[k].get("time_s") or 0 for k in range(a + 1, b)
                   if k != skip and steps[k]["type"] == "WALKING")

    def until(k):
        """경로 첫 구간부터 k 번 구간 끝까지 (대기 포함) — 앞 차에서 내리는 시각을 잡는다."""
        return sum((steps[t].get("time_s") or 0) + (steps[t].get("wait_s") or 0) for t in range(k + 1))

    out = []
    for n, i in enumerate(rides):
        s = steps[i]
        prev = rides[n - 1] if n > 0 else None
        nxt = rides[n + 1] if n < len(rides) - 1 else None
        link = ((walks(prev, i) if prev is not None else 0) + (walks(i, nxt) if nxt is not None else 0)
                + ((steps[nxt].get("wait_s") or 0) if nxt is not None else 0))
        out.append({"gap": "wait", "i": i, "gap_s": s.get("wait_s") or 0, "link_s": link, "pre_ride": n > 0,
                    "head_s": until(prev) if prev is not None else 0,
                    "removed_wait": s.get("wait_s") or 0, "removed_time": s.get("time_s") or 0,
                    "x": _chosen(s, "board"), "y": _chosen(s, "alight"), "resume_ride": n < len(rides) - 1,
                    "vehicle": (s.get("vehicles") or [{}])[0].get("name"),
                    "kind": "subway" if s["type"] == "SUBWAY" else "bus"})
    for prev, nxt in zip(rides, rides[1:]):
        for j in range(prev + 1, nxt):
            w = steps[j]
            if w["type"] != "WALKING":
                continue
            out.append({"gap": "walk", "i": j, "gap_s": w.get("time_s") or 0, "pre_ride": True,
                        "link_s": walks(prev, nxt, skip=j) + (steps[nxt].get("wait_s") or 0), "head_s": until(prev),
                        "removed_wait": 0, "removed_time": w.get("time_s") or 0,
                        "x": _chosen(steps[prev], "alight"), "y": _chosen(steps[nxt], "board"), "resume_ride": True,
                        "vehicle": "도보", "kind": "subway" if steps[nxt]["type"] == "SUBWAY" else "bus"})
    return out


def propose_gap(o, d, transit_routes, *, vot, at=None, t_max_s=T_MAX_S, gap_min_s=GAP_MIN_S):
    """(a) 의 D 유형 — 기준 경로에서 공백(긴 대기·긴 환승 도보)이 생기는 한 구간을 택시로 메운다 → (점수 오름차순 후보, 진단).

    A·B 가 경로의 **끝**(출발·도착 쪽)을 택시로 바꾸는 것과 달리 D 는 **경로 한가운데의 한 구간**만 바꾼다 — 택시를 타는 곳도
    내리는 곳도 경로 위 정류소다. "다음 버스가 14분 뒤" 인 정류장에서 기다리지 않고 택시로 그 구간을 건너뛰는 판단이다.
    나머지 구간은 기준 경로에 그대로 있어 대중교통 콜이 들지 않는다 — 검증은 자동차 1콜이다.

    시간 = 기준 경로 시간(구간 합 + 첫·끝 도보 + 대기) − 공백 구간 + 택시. 연결 버퍼는 검증 단계에서 붙인다.
    **대기를 모르는 구간이 하나라도 있는 경로는 건너뛴다** — 기준 시간의 대기 합이 없어 무엇을 빼는지 정의되지 않는다(§7.5).
    요금은 기준 경로 요금에서 출발한다 — 한 구간을 빼면 거리 비례분이 줄어 상한이 된다. 다만 앞 차 하차 → 다음 차 승차가
    **환승 인정 시간**(다음 차 승차 시각 기준 30분, 21–07시 60분)을 넘으면 다음 차에서 기본요금을 새로 낸다(`transfer_fare`).
    """
    diag, best = Counter(), {}
    for route in transit_routes:
        steps = route["steps"]
        if route.get("wait_s") is None:
            diag["route_wait_unknown"] += 1
            continue
        fare_cap = route["fare"].get("value") or route["fare"].get("min")
        total = sum(s.get("time_s") or 0 for s in steps)
        edge = _edge_walk_s(steps, o, d)
        first = _edge_stop(steps, "board")
        head_walk = walk_s(haversine_m(o[0], o[1], first["lon"], first["lat"])) if first else 0.0
        base = total + edge + route["wait_s"]
        for g in _gaps(steps):
            if g["gap_s"] < gap_min_s:
                diag["below_min"] += 1
                continue
            x, y = g["x"], g["y"]
            if not x or not y or x["id"] == y["id"]:
                diag["no_anchor"] += 1
                continue
            taxi_s = taxi_time_s(x["lon"], x["lat"], y["lon"], y["lat"])
            if taxi_s > t_max_s:
                diag["too_far"] += 1
                continue
            if haversine_m(x["lon"], x["lat"], y["lon"], y["lat"]) < TAXI_MIN_M:
                diag["too_short"] += 1
                continue
            km = taxi_dist_km(x["lon"], x["lat"], y["lon"], y["lat"])
            fare = taxi_fare(km, x.get("sgg_nm"), y.get("sgg_nm"), at)
            removed = g["removed_wait"] + g["removed_time"]
            # 다음 차에 타는 시각 = 출발 + 앞 차 하차까지 + 사이(도보·대기) + 택시 — 환승 인정 시간이 이 시각으로 갈린다
            offset = head_walk + g["head_s"] + g["link_s"]
            taxi_plan = taxi_plan_s(taxi_s, connect=g["resume_ride"])
            if removed - taxi_plan < TAXI_MIN_SAVING_S:     # 대신한 부분을 알므로 후보 단계에서 거른다
                diag["saves_little"] += 1
                continue
            board_at = at + timedelta(seconds=offset + taxi_plan) if at is not None else None
            transit, kept = transfer_fare(fare_cap, g["pre_ride"], g["resume_ride"], g["link_s"], taxi_plan, board_at)
            cand = {"strategy": "a", "hybrid": "D", "gap": g["gap"], "gap_s": removed, "kind": g["kind"],
                    "anchor": "%s>%s" % (x["id"], y["id"]), "name": "%s→%s" % (x["name"], y["name"]),
                    "from_lon": x["lon"], "from_lat": x["lat"], "lon": y["lon"], "lat": y["lat"],
                    "sgg_nm": x.get("sgg_nm"), "line_group": y.get("line_group"),
                    "taxi_km": km, "taxi_s": taxi_s, "fare": fare,
                    "base_time_s": base, "resume_ride": g["resume_ride"],
                    "wait_s": (route["wait_s"] - g["removed_wait"]) or None,
                    "ride_s": total - g["removed_time"], "walk_s": edge,
                    "ride_id": g["vehicle"], "ride_name": g["vehicle"],
                    "transit_fare_cap": fare_cap, "transit_fare": transit, "transfer_kept": kept,
                    "pre_ride": g["pre_ride"], "link_s": g["link_s"], "board_offset_s": offset, "rides": []}
            cand["score_s"] = base - removed + taxi_plan + (fare + (transit or 0)) / vot * 60.0
            diag["gaps_" + g["gap"]] += 1
            if cand["anchor"] not in best or cand["score_s"] < best[cand["anchor"]]["score_s"]:
                best[cand["anchor"]] = cand
    out = sorted(best.values(), key=lambda c: c["score_s"])
    info = dict(diag)
    info["anchors"] = len(out)
    return out, info


# --- 검증할 후보 고르기 ---
# 이만큼 가까운 같은 유형 앵커는 한 지점으로 본다 — 역과 그 앞 버스 정류장, 환승역의 노선별 행은 id 가 달라도
# 검증 쿼리가 사실상 같다(오이도역 실측: (a)와 (b)가 4호선·수인분당선 두 id 로 같은 역을 내 쿼터를 두 번 썼다).
# **이 규칙은 실험(algo)과 데모 웹(app.hybrid)이 함께 쓴다.**
NEAR_ANCHOR_M = 150.0


def _same_spot(a, b, near_m):
    """두 후보가 같은 검증 쿼리인가 — 유형이 같고 앵커가 같거나 가깝다.

    C·D 는 택시의 **양끝이 모두 앵커**다. 도착점만 가깝고 출발점이 멀면 서로 다른 후보라 합치지 않는다.
    """
    if a["hybrid"] != b["hybrid"]:
        return False
    if a["anchor"] == b["anchor"]:
        return True
    near = haversine_m(a["lon"], a["lat"], b["lon"], b["lat"]) < near_m
    if near and a["hybrid"] in ("C", "D"):
        near = haversine_m(a["from_lon"], a["from_lat"], b["from_lon"], b["from_lat"]) < near_m
    return near


def merge_candidates(*groups, top, near_m=NEAR_ANCHOR_M):
    """여러 전략의 후보를 점수순으로 합쳐 검증할 상위 top 개 — 같은 검증 쿼리는 한 번만 남긴다(점수가 낮은 쪽)."""
    out = []
    for cand in sorted((c for g in groups for c in g), key=lambda c: c["score_s"]):
        if any(_same_spot(c, cand, near_m) for c in out):
            continue
        out.append(cand)
        if len(out) >= top:
            break
    return out


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
    **원본은 건드리지 않는다** — A 유형처럼 택시로 정류장에 닿는 후보에는 걸어서 닿는다는 전제의 이 값이 맞지 않는다.
    """
    out, diag = [], Counter()
    for route in routes:
        r = dict(route, steps=[dict(s) for s in route["steps"]])
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
        step["static_wait_s"], step["wait_s"] = step.get("wait_s"), round(wait)
        step["wait_source"], step["walk_to_stop_s"] = source, round(walk)
        rides = [s for s in r["steps"] if s["type"] in RIDE_TYPES]
        r["wait_s"] = sum(s["wait_s"] for s in rides) if all(s.get("wait_s") is not None for s in rides) else None
        r["total_with_wait_s"] = (r["total_time_s"] + r["wait_s"]
                                  if r["wait_s"] is not None and r.get("total_time_s") is not None else None)
    return out, dict(diag)


# --- C 유형 — 돌아가기 시작하는 정류장에서 내려 경로를 다시 찾는다 ---
# 목적지까지 직선거리가 이만큼 넘게 다시 늘어야 '돌아간다' 로 본다 — 도로를 따라가며 생기는 흔들림은 무시한다.
DETOUR_RISE_M = 300.0
# 요청당 다시 찾을 정류장 수와, 정류장마다 확인할 직행 노선 앵커 수 — 검증 쿼리 예산이다.
C_POINTS = 2
C_ANCHORS = 3


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
                            "last": i == rides[-1] and n == len(pts) - 1})
        t += ride
    return out


def detour_points(route, o, d, rise_m=DETOUR_RISE_M):
    """경로가 목적지에서 멀어지기 시작하는 정류장 → [{point, loop_s, rise_m}].

    경로를 따라 목적지까지 직선거리가 줄다가 `rise_m` 넘게 다시 늘어나는 곳의 직전 정류장이다. 돌아가는 비용(`loop_s`)은
    그 정류장을 떠나 목적지에 다시 그만큼 가까워질 때까지 기준 경로가 쓰는 시간이고, 끝내 가까워지지 않으면 도착까지다.
    합류할 곳을 정하지 않고 기준 경로 응답만으로 잰다 — C 는 이 정류장에서 경로를 다시 찾는다.
    """
    pts = route_points(route, o)
    dist = [haversine_m(p["lon"], p["lat"], d[0], d[1]) for p in pts]
    out = []
    for k in range(len(pts) - 1):
        if pts[k]["last"] or dist[k + 1] <= dist[k] or (k > 0 and dist[k - 1] < dist[k]):
            continue
        back = next((m for m in range(k + 1, len(pts)) if dist[m] <= dist[k]), len(pts) - 1)
        rise = max(dist[k + 1:back + 1]) - dist[k]
        if rise >= rise_m:
            out.append({"point": pts[k], "loop_s": pts[back]["t"] - pts[k]["t"], "rise_m": rise})
    return out


def propose_detour(stops, routes, lines, o, d, transit_routes, *, vot, at=None, headway=None, phys=None,
                   hours=None, t_max_s=T_MAX_S, n_points=C_POINTS, n_anchors=C_ANCHORS):
    """C 유형 — 경로가 돌아가기 시작하는 정류장에서 내려 경로를 다시 찾는다 → (점수 오름차순 후보, 진단).

    기준 경로에 다시 합류하면 '그 노선을 계속 탄다' 는 전제가 남아 그 정류장에서 더 좋은 길이 있어도 못 본다.
    그래서 돌아가는 비용이 큰 정류장 `n_points` 곳을 새 출발지로 두고 두 가지를 다시 찾는다.
    - 택시로 목적지까지 (`via=taxi_to_d`) — 검증 자동차 1콜
    - (b) 직행 노선 역추적의 A 방향으로 앵커 `n_anchors` 개 (`via=reanchor`) — 검증 앵커마다 자동차 1 + 대중교통 1콜
    정류장까지는 기준 경로의 시간(`head_s`)을 쓰고, 그 정류장 시각(`at + head_s`)으로 첫·막차·배차·심야 할증을 읽는다.
    요금은 내린 곳까지 기준 요금이 상한이고, 택시를 사이에 둔 환승이 인정 시간을 넘기면 다음 차에서 기본요금을 새로 낸다.
    """
    points = {}
    for route in transit_routes:
        cap = route["fare"].get("value") or route["fare"].get("min") or 0
        for dp in detour_points(route, o, d):
            pid = dp["point"]["id"]
            if pid not in points or dp["loop_s"] > points[pid]["loop_s"]:
                points[pid] = dict(dp, fare_cap=cap)
    chosen = []
    for dp in sorted(points.values(), key=lambda x: -x["loop_s"]):
        p = dp["point"]
        if any(haversine_m(p["lon"], p["lat"], c["point"]["lon"], c["point"]["lat"]) < NEAR_ANCHOR_M for c in chosen):
            continue
        chosen.append(dp)
        if len(chosen) >= n_points:
            break

    sgg_d = _sgg_at(_near(stops, "bus", d, (2000.0, 10000.0)))
    diag, out = Counter(), []
    for dp in chosen:
        p, head, cap = dp["point"], dp["point"]["t"], dp["fare_cap"]
        at_p = at + timedelta(seconds=head) if at is not None else None
        sgg_p = (stops.by_id(p["kind"], p["id"]) or {}).get("sgg_nm")
        # 전략 이름은 're'(재탐색)다 — 계획서의 (c) 허브 타원 전략과 이름이 겹치지 않게 한다
        base = {"strategy": "re", "hybrid": "C", "point_id": p["id"], "point_name": p["name"], "loop_s": dp["loop_s"],
                "from_lon": p["lon"], "from_lat": p["lat"], "head_s": head, "transit_fare_cap": cap, "pre_ride": True}

        # 1) 택시로 목적지까지
        taxi_s = taxi_time_s(p["lon"], p["lat"], d[0], d[1])
        if haversine_m(p["lon"], p["lat"], d[0], d[1]) < TAXI_MIN_M:
            diag["too_short"] += 1
        elif taxi_s > t_max_s:
            diag["too_far"] += 1
        else:
            km = taxi_dist_km(p["lon"], p["lat"], d[0], d[1])
            fare = taxi_fare(km, sgg_p, sgg_d, at_p)
            time = head + taxi_plan_s(taxi_s, connect=False)
            out.append(dict(base, via="taxi_to_d", anchor="%s>D" % p["id"], name="%s→목적지" % p["name"],
                            kind=p["kind"], lon=d[0], lat=d[1], sgg_nm=sgg_p, taxi_s=taxi_s, taxi_km=km, fare=fare,
                            transit_fare=cap, transfer_kept=None, resume_ride=False, link_s=0.0,
                            wait_s=None, ride_s=0.0, walk_s=0.0, ride_id="taxi:%s" % p["id"], ride_name="택시",
                            rides=[], time_s=time, score_s=time + (fare + cap) / vot * 60.0))
            diag["taxi_to_d"] += 1

        # 2) 그 정류장에서 목적지로 곧장 가는 노선 — 최소 거리·첫막차·요금은 (b) 가 그대로 따진다
        found, _ = propose_backtrack(stops, routes, lines, (p["lon"], p["lat"]), d, vot=vot, hybrid="A", at=at_p,
                                     headway=headway, phys=phys, hours=hours, t_max_s=t_max_s)
        for c in top_n(found, n_anchors):
            plan = taxi_plan_s(c["taxi_s"], connect=True)
            wait = c["wait_s"] if c["wait_s"] is not None else WAIT_UNKNOWN_S
            time = head + plan + wait + c["ride_s"] + c["walk_s"]
            board_at = at_p + timedelta(seconds=plan + wait) if at_p is not None else None
            transit, kept = transfer_fare(cap, True, True, wait, plan, board_at)
            cand = dict(c)
            cand.update(base)
            cand.update(via="reanchor", anchor="%s>%s" % (p["id"], c["anchor"]), anchor_id=c["anchor"],
                        name="%s→%s" % (p["name"], c["name"]), transit_fare=transit, transfer_kept=kept,
                        resume_ride=True, link_s=wait, time_s=time,
                        score_s=time + (c["fare"] + (transit or 0)) / vot * 60.0)
            out.append(cand)
            diag["reanchor"] += 1
    out.sort(key=lambda c: c["score_s"])
    info = dict(diag)
    info.update(points_found=len(points), points_used=len(chosen), anchors=len(out))
    return out, info
