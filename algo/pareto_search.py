"""작은 정적 교통망의 정확한 파레토 탐색 — 택시 한 구간까지 허용한다.

서비스의 앵커 생성기와 독립적인 연구용 기준 구현이다. 모든 연결의 시간·요금은
고정된 비음수 값이어야 한다. 대기·호출·연결 시간은 입력 연결에 포함한다.
시간표나 환승운임 이력이 다르면 별도 노드로 표현해야 하며, 이 모듈은 그 상태를
자동 생성하지 않는다. 사용법과 보장 범위는 pareto_search.md 를 참고한다.
"""
from collections import defaultdict
from dataclasses import dataclass
from heapq import heappop, heappush
from itertools import count
from math import isfinite
from typing import Iterable, Literal


@dataclass(frozen=True)
class Edge:
    """방향이 있는 연결. taxi 는 도로 링크가 아닌 한 번의 전체 택시 이용이다."""

    start: str
    end: str
    mode: Literal["walk", "transit", "taxi"]
    time_s: float
    fare: float = 0

    def __post_init__(self):
        if self.mode not in ("walk", "transit", "taxi"):
            raise ValueError("mode must be walk, transit, or taxi")
        if not isfinite(self.time_s) or self.time_s < 0:
            raise ValueError("time_s must be finite and nonnegative")
        if not isfinite(self.fare) or self.fare < 0:
            raise ValueError("fare must be finite and nonnegative")


@dataclass(frozen=True)
class Journey:
    """시간·요금과 실제 선택된 연결 순서. 같은 비용쌍은 대표 경로 하나만 반환한다."""

    time_s: float
    fare: float
    edges: tuple[Edge, ...]


@dataclass(eq=False)
class _Label:
    node: str
    taxi_used: bool
    time_s: float
    fare: float
    previous: "_Label | None" = None
    edge: Edge | None = None


def _journey(label):
    edges = []
    last = label
    while label.edge is not None:
        edges.append(label.edge)
        label = label.previous
    return Journey(last.time_s, last.fare, tuple(reversed(edges)))


def pareto_journeys(edges: Iterable[Edge], origin: str, destination: str) -> list[Journey]:
    """택시 0/1회 경로의 파레토 비용쌍과 대표 경로를 시간·요금 순으로 반환한다.

    노드는 같은 후속 선택·비용 규칙을 갖는 상태여야 한다. 같은 노드에서도 택시
    사용 여부가 다르면 별도 라벨 집합을 유지한다. 동일 비용은 먼저 발견한 라벨을
    남기며, 목적지에서 0/1회 비용까지 같으면 0회 경로를 남긴다.
    도달할 수 없으면 [], 출발=도착이면 비용 0의 빈 경로를 반환한다.
    """
    outgoing = defaultdict(list)
    for edge in edges:
        outgoing[edge.start].append(edge)

    start = _Label(origin, False, 0, 0)
    labels = defaultdict(list)
    labels[origin, False].append(start)
    serial = count()  # 비용이 같아도 노드나 라벨 자체를 비교하지 않는다.
    queue = [(0, 0, next(serial), start)]
    while queue:
        _, _, _, current = heappop(queue)
        if current not in labels[current.node, current.taxi_used]:
            continue  # 더 나은 라벨이 나와 이미 제거되었다.
        if current.node == destination:
            continue
        for edge in outgoing[current.node]:
            if edge.mode == "taxi" and current.taxi_used:
                continue
            used = current.taxi_used or edge.mode == "taxi"
            time_s, fare = current.time_s + edge.time_s, current.fare + edge.fare
            if not isfinite(time_s) or not isfinite(fare):
                raise ValueError("accumulated time and fare must remain finite")
            bag = labels[edge.end, used]
            # 같은 비용도 합친다. 이 조건으로 비용 0의 순환 역시 끝난다.
            if any(old.time_s <= time_s and old.fare <= fare for old in bag):
                continue
            candidate = _Label(edge.end, used, time_s, fare, current, edge)
            labels[edge.end, used] = [old for old in bag
                                     if not (time_s <= old.time_s and fare <= old.fare)] + [candidate]
            heappush(queue, (time_s, fare, next(serial), candidate))

    # 목적지에서는 앞으로 택시를 쓸 가능성이 없으므로 0/1회 집합을 함께 비교한다.
    reached = sorted(labels[destination, False] + labels[destination, True],
                     key=lambda label: (label.time_s, label.fare))
    result, cheapest = [], float("inf")
    for label in reached:
        if label.fare < cheapest:
            result.append(_journey(label))
            cheapest = label.fare
    return result
