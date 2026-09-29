"""시간·요금 추정 차이와 무관하게 동일한 승차 여정인 도보 대체 결과를 제외한다."""


def _point(value):
    return tuple(round(x, 6) for x in value) if value and len(value) == 2 else None


def _rides(route):
    result = []
    for step in (route or {}).get("steps", []):
        if step["type"] == "WALKING":
            continue
        if step["type"] not in ("BUS", "SUBWAY"):
            return None
        path = step.get("path") or []
        stops = step.get("stops") or []
        lines = tuple(sorted(set(step.get("route_ids") or []))) if step["type"] == "BUS" else step.get("line_group")
        if not lines or len(stops) < 2 or len(path) < 2:
            return None   # 식별 정보가 부족하면 중복이라고 단정하지 않는다
        result.append((step["type"], lines, tuple(stops), tuple(_point(p) for p in path)))
    return tuple(result) if result else None


def duplicate_walk_route(row, routes, origin, destination):
    if not row.get("walking"):
        return False
    rides = _rides(row.get("transit"))
    if not rides or not any(_rides(route) == rides for route in routes):
        return False
    # 새 도보가 기존 승차 여정의 양 끝/환승 사이를 연결할 때만 같다.
    # 다른 지점을 경유하는 도보까지 같은 경로로 취급하지 않는다.
    gaps = [(_point(origin), rides[0][3][0]), (rides[-1][3][-1], _point(destination))]
    gaps += [(a[3][-1], b[3][0]) for a, b in zip(rides, rides[1:])]
    path = row["walking"].get("path") or []
    return len(path) == 2 and (_point(path[0]), _point(path[-1])) in gaps
