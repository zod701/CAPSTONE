"""후보를 같은 잣대로 재구성하고 견준다 — 일반화 비용 · Pareto · 원/분 (순수 함수, 저장 없음).

**잣대가 하나여야 한다.** 블랙박스의 `total_time_s` 를 그대로 쓰면 안 된다 — 거기에는 응답에 없는
첫·끝 도보가 이미 들어 있고 대기는 들어 있지 않다(method.md E15). 하이브리드 후보는 잘라 붙인 구간의
합으로 만들 수밖에 없으므로, 기준 경로도 같은 식(구간 시간 합 + 추정 도보 + 대기)으로 다시 만든다.
섞어 쓰면 하이브리드가 부당하게 빨라 보이고, 배차가 긴 노선이 체계적으로 유리해진다(§7.4).
"""
from geoutil import haversine_m

from algo.anchors import RIDE_TYPES, TAXI_MIN_SAVING_S, _chosen, walk_s

# 대중교통 요금이 범위로 올 때는 낮은 쪽을 쓴다 — 정렬에서도 택시와 견줄 때도 같은 규칙이다 (method.md §7.2, E11)
def fare_of(fare):
    """경로 요금 — 값 하나면 그 값, 범위면 낮은 쪽. 둘 다 없으면 None."""
    return fare.get("value") if fare.get("value") is not None else fare.get("min")


def edge_walk_m(steps, o, d):
    """첫 승차점까지와 마지막 하차점부터의 직선 도보 (m).

    블랙박스는 환승 도보만 실제 경로로 주고 첫·끝 도보는 주지 않는다. 빼고 재면 첫 정류장이 1 km 떨어진
    경로도 도보 0 이 되어 비교가 왜곡된다 → method.md §7.3.
    """
    rides = [s for s in steps if s["type"] in RIDE_TYPES]
    if not rides:
        return 0.0
    head, tail = _chosen(rides[0], "board"), _chosen(rides[-1], "alight")
    m = 0.0
    if head:
        m += haversine_m(o[0], o[1], head["lon"], head["lat"])
    if tail:
        m += haversine_m(tail["lon"], tail["lat"], d[0], d[1])
    return m


def reconstruct(route, o, d):
    """대중교통 경로 하나를 우리 잣대로 → {time_s, fare, walk_m, transfers, wait_s}.

    대기가 한 구간이라도 비면 `wait_s` 는 None 이고 시간에도 넣지 않는다 — 빠진 구간만큼 짧은 값은
    없는 것보다 나쁘다(§7.5). 그때는 대기를 뺀 시간이라는 것을 `wait_s is None` 으로 알린다.
    """
    steps = route["steps"]
    ride = sum(s.get("time_s") or 0 for s in steps)
    walk_m = edge_walk_m(steps, o, d)
    wait = route.get("wait_s")
    return {"time_s": ride + walk_s(walk_m) + (wait or 0), "fare": fare_of(route["fare"]),
            "walk_m": walk_m, "transfers": route.get("transfers"), "wait_s": wait}


def gc_s(time_s, fare, vot):
    """일반화 비용(초) — 시간 + 비용/VOT. VOT 는 원/분이라 60 을 곱해 초로 맞춘다."""
    return time_s + (fare or 0) / vot * 60.0


def pareto(items, time_key="time_s", fare_key="fare"):
    """(시간, 비용) 최소 기준 비지배 집합 — 다른 해가 두 축 모두에서 같거나 낫고 하나는 더 나으면 뺀다."""
    out = []
    for a in items:
        ta, fa = a[time_key], a[fare_key] or 0
        if any((b[time_key] <= ta and (b[fare_key] or 0) <= fa
                and (b[time_key] < ta or (b[fare_key] or 0) < fa)) for b in items if b is not a):
            continue
        out.append(a)
    return out


def saving_per_min(cand, base):
    """절감 효율 — 추가 비용을 절감 시간으로 나눈 원/분. 시간을 못 줄이면 None(비교 대상이 아니다).

    고른 선호 단계의 VOT 보다 비싸면 "비추천" 으로 표시한다 → method.md §7.4.
    """
    saved_min = (base["time_s"] - cand["time_s"]) / 60.0
    if saved_min <= 0:
        return None
    return ((cand["fare"] or 0) - (base["fare"] or 0)) / saved_min


def diversify(items, n=4, key="hybrid"):
    """구조가 서로 다른 것부터 n 개 — 전 구간 대중교통 · first-mile · last-mile · 전 구간 택시.

    한 유형이 상위를 다 차지하면 사용자가 고를 것이 없다. 유형마다 하나씩 먼저 채우고 남는 자리를 점수순으로 준다.
    """
    out, seen = [], set()
    for it in items:
        if it.get(key) not in seen:
            seen.add(it.get(key))
            out.append(it)
    for it in items:
        if len(out) >= n:
            break
        if it not in out:
            out.append(it)
    return out[:n]


def saves_enough(time_s, base_time_s):
    """택시를 쓰는 최소 기준 — 같은 잣대의 기준선보다 `TAXI_MIN_SAVING_S`(5분) 이상 빠른가.

    택시가 대신한 부분을 아는 후보(D)는 후보 단계에서 이미 거르고, 모르는 후보((b)·(a)·C)는 검증한 시간으로 여기서 견준다.
    """
    return base_time_s - time_s >= TAXI_MIN_SAVING_S

