"""카카오 응답 정규화와 프로브 요약 (순수 함수 — 저장·로그 없음).

대중교통 실측 구조(method.md §2.2 · E2): `properties{total,bus,subway,busAndSubway}`,
`routes[]{properties{type,totalTime,totalDistance,transfers,fare{value}}, steps[]}`,
`steps[]{properties{guidance,type,distance,time,stops[{name}],vehicles[{type,name}]}, path{points}}`.
필드가 properties 밖에 있어도 읽는다(get_prop). `path.points` 의 원소 형식은 아직 실측 전이라
[x,y] 쌍 · {x,y} · 평탄 배열을 모두 받고, 프로브가 실제 형식을 보고한다.
"""
from collections import Counter

from geoutil import haversine_m
from textnorm import name_key

KNOWN_STEP_TYPES = ("BUS", "SUBWAY", "WALKING")
_KEY_KIND = {"BUS": "bus", "SUBWAY": "subway"}


def get_prop(obj, k):
    props = obj.get("properties")
    if isinstance(props, dict) and k in props:
        return props[k]
    return obj.get(k)


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_guidance(g) -> tuple[str | None, str | None]:
    """'수인분당선 (수원 > 정자)' → ('수원', '정자'). 역명 속 괄호(`판교(판교테크노밸리)`, `종로2가(중)`)를 허용.
    해석할 수 없으면(예: '정자정자역 환승') (None, None)."""
    if not isinstance(g, str) or " > " not in g or not g.endswith(")"):
        return None, None
    left, right = g[:-1].rsplit(" > ", 1)
    i = left.find(" (")
    if i < 0:
        return None, None
    return left[i + 2:].strip() or None, right.strip() or None


def _raw_points(path):
    return path.get("points") if isinstance(path, dict) else path


def _points(path):
    """path → [[lon, lat], ...]."""
    pts = _raw_points(path)
    if not isinstance(pts, list):
        return []
    if pts and all(isinstance(v, (int, float)) for v in pts):  # 평탄 [x1, y1, x2, y2, ...]
        pts = [pts[i:i + 2] for i in range(0, len(pts) - 1, 2)]
    out = []
    for p in pts:
        if isinstance(p, dict):
            p = (p.get("x"), p.get("y"))
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            try:
                out.append([float(p[0]), float(p[1])])
            except (TypeError, ValueError):
                pass
    return out


def _point_format(path):
    """프로브용: path.points 원소 형식 ('pair' | 'dict:x,y' | 'flat' | 'empty' | 그 밖의 타입 이름)."""
    pts = _raw_points(path)
    if not pts:
        return "empty"
    if not isinstance(pts, list):
        return type(pts).__name__
    p = pts[0]
    if isinstance(p, dict):
        return "dict:" + ",".join(sorted(p))
    if isinstance(p, (list, tuple)):
        return "pair"
    if isinstance(p, (int, float)):
        return "flat"
    return "list:" + type(p).__name__


def _step(idx, s):
    stops = [x.get("name") if isinstance(x, dict) else x for x in get_prop(s, "stops") or []]
    stops = [n for n in stops if isinstance(n, str) and n]
    vehicles = [{"type": v.get("type"), "name": v.get("name")}
                for v in get_prop(s, "vehicles") or [] if isinstance(v, dict)]
    guidance = get_prop(s, "guidance")
    g_from, g_to = parse_guidance(guidance)
    return {
        "idx": idx,
        "type": get_prop(s, "type"),
        "distance_m": _num(get_prop(s, "distance")),
        "time_s": _num(get_prop(s, "time")),
        "guidance": guidance,
        "vehicles": vehicles,
        "stops": stops,
        "path": _points(get_prop(s, "path")),
        "board_name": stops[0] if stops else g_from,
        "alight_name": stops[-1] if len(stops) >= 2 else g_to,
        "guidance_from": g_from,
        "guidance_to": g_to,
    }


def normalize_transit(raw: dict) -> dict:
    routes = []
    for i, r in enumerate(raw.get("routes") or []):
        fare = get_prop(r, "fare")
        fare = fare if isinstance(fare, dict) else {}
        routes.append({
            "idx": i,
            "type": get_prop(r, "type"),
            "total_time_s": _num(get_prop(r, "totalTime")),
            "total_distance_m": _num(get_prop(r, "totalDistance")),
            "transfers": _num(get_prop(r, "transfers")),
            "fare": {k: _num(fare.get(k)) for k in ("value", "min", "max")},
            "steps": [_step(j, s) for j, s in enumerate(get_prop(r, "steps") or [])],
        })
    return {
        "status": raw.get("status") or "",
        "summary": {k: _num(get_prop(raw, k)) for k in ("total", "bus", "subway", "busAndSubway")},
        "routes": routes,
    }


def _prop_keys(obj):
    props = obj.get("properties")
    return sorted(props) if isinstance(props, dict) else []


def _probe_route(r):
    steps, prev = [], None
    for s in r["steps"]:
        gap = None
        if prev and prev["path"] and s["path"]:
            gap = round(haversine_m(*prev["path"][-1], *s["path"][0]), 1)
        steps.append({"type": s["type"], "n_stops": len(s["stops"]), "n_vehicles": len(s["vehicles"]),
                      "n_path_points": len(s["path"]), "gap_prev_m": gap})
        prev = s
    return {"idx": r["idx"], "type": r["type"], "steps": steps}


def probe_summary(raw: dict, norm: dict) -> dict:
    """응답 구조 확인용 요약 (plan.md §13-2 확인 사항). 원문은 담지 않는다 — 키 이름·건수·차량 이름만."""
    raw_routes = raw.get("routes") or []
    first_route = raw_routes[0] if raw_routes else {}
    first_step = next((s for r in raw_routes for s in get_prop(r, "steps") or []), {})
    first_path = get_prop(first_step, "path")
    steps = [s for r in norm["routes"] for s in r["steps"]]
    types = Counter(s["type"] for s in steps)
    walk = [len(s["path"]) for s in steps if s["type"] == "WALKING"]
    checked = board_eq = alight_eq = 0
    for s in steps:
        kind = _KEY_KIND.get(s["type"])
        if kind and s["stops"] and s["guidance_from"] and s["guidance_to"]:
            checked += 1
            board_eq += name_key(s["stops"][0], kind) == name_key(s["guidance_from"], kind)
            alight_eq += name_key(s["stops"][-1], kind) == name_key(s["guidance_to"], kind)
    return {
        "top_keys": sorted(raw),
        "route_keys": sorted(first_route),
        "route_prop_keys": _prop_keys(first_route),
        "step_keys": sorted(first_step),
        "step_prop_keys": _prop_keys(first_step),
        "path_keys": sorted(first_path) if isinstance(first_path, dict) else [],
        "point_format": _point_format(first_path),
        "n_routes": len(norm["routes"]),
        "step_type_counts": dict(types),
        "unknown_step_types": sorted((t for t in types if t not in KNOWN_STEP_TYPES), key=str),
        "vehicle_names": {t: sorted({f"{v['type']}:{v['name']}" for s in steps if s["type"] == t
                                     for v in s["vehicles"]}) for t in ("BUS", "SUBWAY")},
        "walking_points": {"n": len(walk), "min": min(walk, default=None), "max": max(walk, default=None),
                           "share_2pt": sum(n == 2 for n in walk) / len(walk) if walk else None},
        "endpoints_vs_guidance": {"checked": checked, "board_eq": board_eq, "alight_eq": alight_eq},
        "routes": [_probe_route(r) for r in norm["routes"]],
    }


def normalize_car(raw: dict) -> dict:
    """카카오모빌리티 자동차 길찾기 → 요약 + 경로. `vertexes` 는 평탄 [x1, y1, x2, y2, ...]."""
    r0 = (raw.get("routes") or [{}])[0]
    code = r0.get("result_code")
    summ = r0.get("summary") or {}
    fare = summ.get("fare") or {}
    path = []
    if code == 0:
        for sec in r0.get("sections") or []:
            for road in sec.get("roads") or []:
                v = road.get("vertexes") or []
                for i in range(0, len(v) - 1, 2):
                    pt = [v[i], v[i + 1]]
                    if not path or path[-1] != pt:
                        path.append(pt)
    return {
        "result_code": code,
        "result_msg": r0.get("result_msg"),
        "distance_m": _num(summ.get("distance")),
        "duration_s": _num(summ.get("duration")),
        "fare": {"taxi": _num(fare.get("taxi")), "toll": _num(fare.get("toll"))},
        "path": path,
    }
