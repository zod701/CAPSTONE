"""성인 교통카드 요금 추정. 공식 출처·지원 범위는 fare_rules.md.

거리에는 API 구간 거리(중간 하차는 거리 비율)를 쓴다. 운임 최단거리 DB가 아니므로
정산 확정값이 아니다. 식별 불가·별도운임은 원래 경로 요금으로 대체하고 사유를 남긴다.
"""
import math
from itertools import product
from datetime import timedelta

from app.wait import DEFAULT_WAIT_S


RAIL_LINES = {f"{n}호선" for n in range(1, 10)} | {
    "수인분당선", "경의중앙선", "경춘선", "경강선", "서해선", "우이신설선", "신림선",
    "인천1호선", "인천2호선", "김포골드라인",
}
TRANSFER_WINDOW_S = 1800
TRANSFER_WINDOW_NIGHT_S = 3600


def transfer_window_s(board_at):
    return TRANSFER_WINDOW_NIGHT_S if board_at and (board_at.hour >= 21 or board_at.hour < 7) else TRANSFER_WINDOW_S


# 기본요금, 통합 기본거리(m), 단독 거리요금 종류, 조조 할인액
BUS_RULES = {
    "seoul": {"간선": (1500, 10000, "flat", 300), "지선": (1500, 10000, "flat", 300),
              "순환": (1400, 10000, "flat", 280), "마을": (1200, 10000, "flat", 240),
              "광역": (3000, 30000, "flat", 600)},
    "gyeonggi": {"일반": (1650, 10000, "bus", 200), "좌석": (2650, 30000, "flat", 200),
                 "직행": (3200, 30000, "flat", 400), "광역": (3200, 30000, "bus", 400),
                 "순환": (3450, 30000, "bus", 400)},
}


def _leg(step):
    distance = step.get("distance_m")
    if distance is None or not math.isfinite(distance) or distance < 0:
        return None, "구간 거리 미확인"
    if step["type"] == "SUBWAY":
        line = step.get("line_group")
        if line not in RAIL_LINES:
            return None, "철도 별도운임 또는 노선 미확인"
        # 평택~신창, 가평~춘천의 별도 거리 규칙은 운임 구간 DB가 있어야 계산할 수 있다.
        if line in ("1호선", "경춘선"):
            names = set(step.get("stops") or []) | {step.get("board_name"), step.get("alight_name")}
            outside = {"평택", "성환", "직산", "두정", "천안", "봉명", "쌍용", "아산", "탕정", "배방", "온양온천", "신창",
                       "가평", "굴봉산", "백양리", "강촌", "김유정", "남춘천", "춘천"}
            if not names - {None} or any((n or "").removesuffix("역") in outside for n in names):
                return None, "수도권 외 철도 운임거리 미확인"
        rule, ids = (1550, 10000, "rail", 310), {"rail"}
    else:
        routes = step.get("fare_routes") or []
        if not routes:
            return None, "버스 관할·노선 유형 미확인"
        rules = []
        for route in routes:
            source, kind = route.get("source"), route.get("type")
            rule = BUS_RULES.get(source, {}).get(kind)
            if source == "seoul" and str(route.get("name", "")).startswith("N"):
                rule = (2500, 10000, "flat", 500)
            if rule is None:
                return None, "마을버스 관할 요금 또는 별도 버스 운임 미확인"
            rules.append(rule)
        if len(set(rules)) != 1:
            return None, "승차 가능 노선의 요금이 서로 다름"
        rule = rules[0]
        ids = {r["id"] for r in routes}
    base, free, mode, early = rule
    return {"base": base, "free": free, "mode": mode, "early": early,
            "distance": distance, "ids": ids, "rail": step["type"] == "SUBWAY"}, None


def _extra(distance, free, interval=5000):
    return 100 * math.ceil(max(0, distance - free) / interval)


def _single(leg):
    distance, free, mode = leg["distance"], leg["free"], leg["mode"]
    if mode == "rail":
        extra = _extra(min(distance, 50000), 10000) + _extra(distance, 50000, 8000)
    elif mode == "bus":
        extra = min(700, _extra(distance, free))
    else:
        extra = 0
    return leg["base"] + extra


def estimate(steps, fallback, *, at=None, initial_walk_s=0):
    """승차 노선 조합별 전체 요금을 계산한다. 미지원 조합이 있으면 기존 대체 방식을 유지한다."""
    choices = [[{**step, "fare_routes": [route]} for route in step["fare_routes"]]
               if step["type"] == "BUS" and len(step.get("fare_routes") or []) > 1 else [step]
               for step in steps]
    if all(len(options) == 1 for options in choices):
        return _estimate(steps, fallback, at=at, initial_walk_s=initial_walk_s)
    low, high = math.inf, -math.inf
    transfer_states = set()
    for selected in product(*choices):
        result = _estimate(selected, fallback, at=at, initial_walk_s=initial_walk_s)
        if result["source"] == "fallback":
            return _estimate(steps, fallback, at=at, initial_walk_s=initial_walk_s)
        low, high = min(low, result["value"]), max(high, result["value"])
        transfer_states.add(result["transfer_kept"])
    return {"value": high, "min": low, "max": high, "source": "rules_range",
            "transfer_kept": next(iter(transfer_states)) if len(transfer_states) == 1 else None,
            "reason": "성인 교통카드 · 노선 조합별 거리·환승·조조 요금 계산 · 택시 별도 결제 가정 · 비교는 최대 요금 기준"}


def _estimate(steps, fallback, *, at=None, initial_walk_s=0):
    """보존/절단한 steps + TAXI(time_s) → 요금과 출처. 택시 요금·거리는 합산하지 않는다.

    TAXI 시간은 호출 대기·연결 버퍼까지 포함한다. 도보·대기·택시 시간은 환승 시한에만
    넣고, 대중교통 거리만 누적한다. 같은 카드 승하차 태그 및 택시 별도 결제를 가정한다.
    """
    groups, group = [], []
    elapsed, gap = initial_walk_s, 0.0
    taxi, previous_rail = False, False
    discount, paid, taxi_kept = 0, 0, None
    for step in steps:
        if step["type"] not in ("BUS", "SUBWAY"):
            gap += step.get("time_s") or 0
            elapsed += step.get("time_s") or 0
            taxi = taxi or step["type"] == "TAXI"
            continue
        leg, reason = _leg(step)
        if reason:
            return {"value": fallback, "source": "fallback", "reason": reason}
        wait = step.get("wait_s")
        wait = DEFAULT_WAIT_S if wait is None else wait
        gap += wait
        elapsed += wait
        board_at = at + timedelta(seconds=elapsed) if at is not None else None
        window = transfer_window_s(board_at)
        rail_connection = bool(group) and previous_rail and leg["rail"] and not taxi
        same_bus = bool(group) and not leg["rail"] and bool(group[-1]["ids"] & leg["ids"])
        # 역 밖으로 나와 택시를 탄 철도→철도는 구내 환승으로 보지 않는다.
        keep = bool(group) and (rail_connection or (
            gap <= window and len(group) < 5 and not same_bus and not (previous_rail and leg["rail"])))
        if taxi and group:
            taxi_kept = keep
        old_base = max((x["base"] for x in group), default=0)
        if not keep:
            if group:
                groups.append(paid)
            group, paid = [], 0
            # 조조는 최초 승차만. 시간 정보가 없으면 일반 요금을 쓴다.
            discount = leg["early"] if not groups and board_at and board_at.hour * 60 + board_at.minute < 390 else 0
            old_base = 0
        if rail_connection:
            group[-1] = {**group[-1], "distance": group[-1]["distance"] + leg["distance"]}
        else:
            group.append(leg)
        base = max(x["base"] for x in group)
        if len(group) == 1:
            total = _single(group[0]) - discount
        else:
            distance = sum(x["distance"] for x in group)
            total = min(base + _extra(distance, max(x["free"] for x in group)),
                        sum(_single(x) for x in group)) - discount
        # 이미 낸 거리요금을 뒤에서 기본거리가 커졌다는 이유로 환급하지 않는다.
        paid = max(paid + max(0, base - old_base) if keep else 0, total)
        elapsed += step.get("time_s") or 0
        gap, taxi, previous_rail = 0, False, leg["rail"]
    reason = "성인 교통카드·구간 거리 기준 추정"
    if taxi_kept:
        reason += " · 택시 별도 결제와 대중교통 환승 유지 가정"
    return {"value": sum(groups) + paid, "source": "rules_estimate", "reason": reason,
            "transfer_kept": taxi_kept}
