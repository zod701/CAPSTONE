"""하이브리드 후보 — 택시 1회 경로 하나의 규칙과 순차 검증 (method.md §3.8).

모든 하이브리드는 `O →(대중교통·도보*) u →(택시 1회) v →(대중교통·도보*) D` 꼴이다(pareto_search.md).
후보는 u 쪽 앞부분과 v 쪽 뒷부분의 모든 짝이다.

- 앞부분(u): 출발지 그대로 · 기준 경로를 타고 가다 내리는 모든 정류소(중간 정류소 포함) ·
  출발지 근처에서 걸어 타는 직행 노선의 하차 지점(`anchors.direct_ends(side="head")`)
- 뒷부분(v): 목적지 그대로 · 기준 경로의 승차 지점 · 목적지 근처로 곧장 가는 직행 노선의 승차 지점(`side="tail"`)

기준 경로에서 자른 부분은 응답에 이미 있어 쿼터가 들지 않고, 직행 노선 부분은 검증할 때 그 구간의 대중교통을 한 번 부른다.
택시는 자동차 경로 1콜이다. 확인할 후보는 순차 검증 탐색(`lazy_pareto`)이 결과를 보며 고른다.

카카오는 호출자가 넘기는 `api`(`car(출발, 도착)` · `transit(출발, 도착)`)로만 부른다 — 실패하면 `QueryUnavailable` 을 던진다.
"""
import math
from bisect import bisect_left, bisect_right
from datetime import timedelta

from geoutil import haversine_m

from algo import anchors, compare
from algo.fares import estimate
from algo.lazy_pareto import Candidate, Cost, LazyParetoSearch

SGG_RADII = (2000.0, 10000.0)   # 좌표가 속한 시군을 가장 가까운 정류소로 본다 (택시 요금 유형·시계외)
# 직행 노선 끝점을 찾는 택시 창의 중심 간격. 반경에 같은 만큼 더하므로 솎아 낸 정류소에서 닿는 곳도 빠지지 않는다
CORRIDOR_STEP_M = 2000.0


def xy(point):
    return point["lon"], point["lat"]


def key(kind, start, end):
    """카카오 질의 하나의 이름 — ("car"|"transit", 출발 lon, lat, 도착 lon, lat). 후보끼리 같은 질의를 알아본다."""
    return (kind, *(round(v, 7) for v in (*start, *end)))


def sgg_at(stops, pt):
    return anchors._sgg_at(anchors._near(stops, "bus", pt, SGG_RADII))


def _duration(steps):
    """구간 시간 + 승차 대기(모르면 기본 대기)."""
    return sum((s.get("time_s") or 0) + ((s.get("wait_s") if s.get("wait_s") is not None else anchors.WAIT_UNKNOWN_S)
                                         if s["type"] in anchors.RIDE_TYPES else 0) for s in steps)


def _part(point, steps, time_s, fallback, *, source, initial_s=0.0, query=None, at=None, **extra):
    """앞부분·뒷부분 하나. `fare` 는 그 부분만의 대중교통 요금 추정(순위용)이고, 확정 요금은 검증에서 앞뒤를 이어 다시 낸다."""
    fare = estimate(steps, fallback, at=at, initial_walk_s=initial_s)["value"] if steps else 0.0
    return {"point": point, "steps": steps, "time_s": time_s, "fallback": fallback, "fare": fare,
            "initial_s": initial_s, "query": query, "source": source, **extra}


def _keep(best, part, vot):
    """같은 정류소의 부분은 하나만 — 시간·요금 근사의 일반화 비용이 낮은 쪽."""
    k = part["point"]["id"]
    if k not in best or part["time_s"] + part["fare"] / vot * 60 < best[k]["time_s"] + best[k]["fare"] / vot * 60:
        best[k] = part


def route_heads(routes, o, stops, *, vot, at=None):
    """기준 경로를 타고 가다 내리는 정류소마다 앞부분. 탄 정류장에서 곧바로 내리는 점은 뺀다 — 앞 하차 지점(또는 출발지)과 같은 곳이다."""
    best, skipped = {}, 0
    for ri, route in enumerate(routes):
        first = anchors._edge_stop(route["steps"], "board")
        fallback = compare.fare_of(route.get("fare") or {})
        if first is None or fallback is None:
            skipped += 1
            continue
        initial = anchors.walk_s(haversine_m(*o, *xy(first)))
        for p in anchors.route_points(route, o):
            if p["fraction"] <= 0:
                continue
            row = stops.by_id(p["kind"], p["id"]) or {}
            point = {"id": p["id"], "name": p["name"], "lon": p["lon"], "lat": p["lat"], "kind": p["kind"],
                     "sgg_nm": row.get("sgg_nm"), "line_group": row.get("line_group")}
            _keep(best, _part(point, anchors.prefix_steps(route, p), p["t"], fallback, source="route",
                              initial_s=initial, at=at, route_index=ri), vot)
    return list(best.values()), skipped


def route_tails(routes, d, stops, *, vot, at=None):
    """기준 경로의 승차 지점마다 뒷부분 — 그 지점부터 기준 경로를 그대로 탄다. 중간 정류소에서 다시 타지는 않는다(R28)."""
    best = {}
    for ri, route in enumerate(routes):
        steps = route["steps"]
        last = anchors._edge_stop(steps, "alight")
        fallback = compare.fare_of(route.get("fare") or {})
        if last is None or fallback is None:
            continue
        last_walk = anchors.walk_s(haversine_m(*xy(last), *d))
        for si, step in enumerate(steps):
            board = anchors._chosen(step, "board") if step["type"] in anchors.RIDE_TYPES else None
            if not board:
                continue
            kind = "subway" if step["type"] == "SUBWAY" else "bus"
            row = stops.by_id(kind, board["id"]) or {}
            point = {"id": board["id"], "name": board["name"], "lon": board["lon"], "lat": board["lat"], "kind": kind,
                     "sgg_nm": board.get("sgg_nm") or row.get("sgg_nm"), "line_group": board.get("line_group")}
            tail = steps[si:]
            _keep(best, _part(point, tail, _duration(tail) + last_walk, fallback, source="route", at=at,
                              route_index=ri), vot)
    return list(best.values())


def corridor(o, d, routes):
    """택시 창의 중심 — 출발지 · 목적지 · 기준 경로 위 모든 정류소를 CORRIDOR_STEP_M 간격으로 솎은 점들.

    앞·뒤 부분의 끝(u·v)은 거의 이 위에 있다. 직행 노선 끝점은 여기서 택시 30분 안이어야 짝이 될 수 있다.
    """
    kept = []
    for pt in [o, d] + [(p["lon"], p["lat"]) for r in routes for p in anchors.route_points(r, o)]:
        if all(haversine_m(*pt, *k) >= CORRIDOR_STEP_M for k in kept):
            kept.append(pt)
    return kept


def line_parts(ends, o, d, side, *, at=None):
    """`direct_ends` 의 끝점 → 앞부분(side="head") 또는 뒷부분(side="tail"). 검증할 때 그 구간의 대중교통을 부른다."""
    out = []
    for e in ends:
        point = {k: e[k] for k in ("name", "lon", "lat", "kind", "sgg_nm", "line_group")} | {"id": e["anchor"]}
        query = key("transit", o, xy(point)) if side == "head" else key("transit", xy(point), d)
        out.append(_part(point, [e["fare_step"]], e["time_s"], e["transit_fare"], source="line", query=query, at=at,
                         initial_s=e["walk_s"] if side == "head" else 0.0,
                         ride_name=e["ride_name"], headway_m=e["headway_m"]))
    return out


def pair(heads, tails, *, vot, baseline_s, at=None, t_max_s=anchors.T_MAX_S):
    """앞부분 × 뒷부분 → (후보, 진단). 택시 구간이 걷기보다 느리거나(직선 약 258 m 미만) 30분을 넘거나, ETA 가 0 이어도 기준선보다
    5분 빨라질 수 없는 짝은 뺀다. 택시 요금 유형·심야는 택시를 타는 곳과 그 시각으로 정한다."""
    diag, out = {"too_close": 0, "too_far": 0, "no_saving": 0}, []
    limit = baseline_s - anchors.TAXI_MIN_SAVING_S
    # 뒷부분을 경도순으로 두고, 택시 도달 거리 안에 들 수 있는 경도 폭만 본다 — 그 밖은 직선으로도 30분 밖이다
    reach = anchors.taxi_reach_m(t_max_s, at)   # 택시 시간 기울기는 요청 시각의 시간대로 고른다
    tails = sorted(tails, key=lambda t: t["point"]["lon"])
    lons = [t["point"]["lon"] for t in tails]
    for h in heads:
        u = xy(h["point"])
        lat = min(89.0, abs(u[1]) + math.degrees(reach / 6371000.0))   # 폭이 가장 넓어지는 위도로 잡아 빠뜨리지 않는다
        dlon = math.degrees(reach / (6371000.0 * math.cos(math.radians(lat))))
        dlat = math.degrees(reach / 6371000.0)
        lo, hi = bisect_left(lons, u[0] - dlon), bisect_right(lons, u[0] + dlon)
        diag["too_far"] += len(tails) - (hi - lo)
        for t in tails[lo:hi]:
            if h["source"] == "origin" and t["source"] == "destination":
                continue   # 전 구간 택시는 하이브리드가 아니다
            v = xy(t["point"])
            if abs(v[1] - u[1]) > dlat:
                diag["too_far"] += 1
                continue
            straight = haversine_m(*u, *v)
            if straight < anchors.TAXI_MIN_M:
                diag["too_close"] += 1
                continue
            eta = anchors.taxi_s_for(straight, at)
            if eta > t_max_s:
                diag["too_far"] += 1
                continue
            connect = bool(t["steps"])
            if h["time_s"] + anchors.taxi_plan_s(0, connect) + t["time_s"] > limit:
                diag["no_saving"] += 1
                continue
            taxi_s = anchors.taxi_plan_s(eta, connect)
            at_u = at + timedelta(seconds=h["time_s"]) if at is not None else None
            taxi_fare = anchors.taxi_fare(anchors.road_km(straight), h["point"].get("sgg_nm"),
                                          t["point"].get("sgg_nm"), at_u)
            time_s = h["time_s"] + taxi_s + t["time_s"]
            fare = taxi_fare + h["fare"] + t["fare"]
            # 시간 하한 — 기준 경로에서 자른 부분과 택시 호출 대기는 응답과 무관하게 고정이다. 다시 부르는 부분은 0 으로 둔다
            lower = ((h["time_s"] if h["query"] is None else 0) + anchors.taxi_plan_s(0, connect)
                     + (t["time_s"] if t["query"] is None else 0))
            out.append({"head": h, "tail": t, "eta_s": eta, "taxi_s": taxi_s, "taxi_fare": taxi_fare,
                        "time_s": time_s, "fare": fare, "score_s": time_s + fare / vot * 60.0, "lower_s": lower})
    out.sort(key=lambda c: (c["score_s"], c["head"]["point"]["id"], c["tail"]["point"]["id"]))
    for n, c in enumerate(out):
        c["id"] = "c%d" % n
    diag["candidates"] = len(out)
    return out, diag


def propose(stops, routes, lines, o, d, head_routes, tail_routes, *, vot, baseline_s, at=None, headway=None,
            phys=None, hours=None, t_max_s=anchors.T_MAX_S):
    """후보 전체 → (후보, 진단). 쿼터를 쓰지 않는다.

    앞부분은 `head_routes`, 뒷부분은 `tail_routes` 에서 자른다 — 출발지에서 걸어서 타는 앞부분은 첫 대기를 실시간으로 바꾼
    사본이, 택시로 닿아 타는 뒷부분은 배차 추정 경로가 맞다. 둘은 같은 경로를 같은 순서로 담는다.
    """
    sgg_o, sgg_d = sgg_at(stops, o), sgg_at(stops, d)
    window = corridor(o, d, head_routes)
    common = dict(vot=vot, at=at, headway=headway, phys=phys, hours=hours, t_max_s=t_max_s,
                  taxi_points=window, taxi_slack_m=CORRIDOR_STEP_M)
    head_ends, head_diag = anchors.direct_ends(stops, routes, lines, o, d, side="head", **common)
    tail_ends, tail_diag = anchors.direct_ends(stops, routes, lines, o, d, side="tail", **common)
    by_route, skipped = route_heads(head_routes, o, stops, vot=vot, at=at)
    heads = ([_part({"id": "O", "name": "출발지", "lon": o[0], "lat": o[1], "sgg_nm": sgg_o}, [], 0.0, 0.0,
                    source="origin")]
             + by_route + line_parts(head_ends, o, d, "head", at=at))
    tails = ([_part({"id": "D", "name": "목적지", "lon": d[0], "lat": d[1], "sgg_nm": sgg_d}, [], 0.0, 0.0,
                    source="destination")]
             + route_tails(tail_routes, d, stops, vot=vot, at=at) + line_parts(tail_ends, o, d, "tail", at=at))
    cands, pair_diag = pair(heads, tails, vot=vot, baseline_s=baseline_s, at=at, t_max_s=t_max_s)
    count = lambda parts, src: sum(p["source"] == src for p in parts)   # noqa: E731
    return cands, {"corridor": len(window),
                   "heads": {"route": count(heads, "route"), "line": count(heads, "line"), "skipped_routes": skipped},
                   "tails": {"route": count(tails, "route"), "line": count(tails, "line")},
                   "pairs": pair_diag, "line_head": head_diag, "line_tail": tail_diag}


def queries(cand, o, d):
    """후보 하나를 확인하는 질의 — `verify` 가 부르는 것과 같아야 한다."""
    h, t = cand["head"], cand["tail"]
    return tuple(q for q in (key("car", xy(h["point"]), xy(t["point"])), h["query"], t["query"]) if q is not None)


class Paid:
    """검증 한 번이 읽는 응답 — 탐색이 이미 낸 질의만 돌려준다. 검증 중에 새로 부르지 않는다(없는 질의는 KeyError)."""

    def __init__(self, responses):
        self.responses = responses

    async def car(self, start, end):
        return self.responses[key("car", start, end)]

    async def transit(self, start, end):
        return self.responses[key("transit", start, end)]


async def _leg(api, part, a, b, vot):
    """부분 하나의 확정 값 → (구간들, 시간, 요금 대체값, 첫 도보 초). 다시 부른 구간이 없으면 None.

    기준 경로에서 자른 부분은 그대로 쓰고, 직행 노선 부분은 그 구간의 대중교통 대안 중 일반화 비용이 가장 낮은 것을 쓴다.
    """
    if part["query"] is None:
        return part["steps"], part["time_s"], part["fallback"], part["initial_s"]
    routes = (await api.transit(a, b)).get("routes") or []
    if not routes:
        return None
    rec, route = min(((compare.reconstruct(r, a, b), r) for r in routes),
                     key=lambda x: compare.gc_s(x[0]["time_s"], x[0]["fare"], vot))
    first = anchors._edge_stop(route["steps"], "board")
    initial = anchors.walk_s(haversine_m(*a, *xy(first))) if first else 0.0
    return route["steps"], rec["time_s"], rec["fare"] or 0, initial


async def verify(api, cand, o, d, vot, at=None):
    """후보 하나를 응답으로 확인 → {time_s, fare, fare_info, car, taxi_s, head_steps, tail_steps, …}. 구간을 못 얻으면 None.

    시간 = 앞부분 + 택시(호출 대기 + ETA + 뒤에 탈 차가 있으면 연결 버퍼) + 뒷부분.
    대중교통 요금은 앞뒤 구간을 택시로 이어 붙여 거리·환승 이력으로 다시 계산한다(`fares.estimate`). 못 하면 두 부분 요금의 합이다.
    """
    h, t = cand["head"], cand["tail"]
    u, v = xy(h["point"]), xy(t["point"])
    car = await api.car(u, v)
    if not car.get("path") or car.get("duration_s") is None or (car.get("fare") or {}).get("taxi") is None:
        return None
    head, tail = await _leg(api, h, o, u, vot), await _leg(api, t, v, d, vot)
    if head is None or tail is None:
        return None
    (head_steps, head_s, head_fb, initial), (tail_steps, tail_s, tail_fb, _) = head, tail
    taxi_s = anchors.taxi_plan_s(car["duration_s"], any(s["type"] in anchors.RIDE_TYPES for s in tail_steps))
    info = estimate(head_steps + [{"type": "TAXI", "time_s": taxi_s}] + tail_steps, head_fb + tail_fb,
                    at=at, initial_walk_s=initial)
    return {"time_s": head_s + taxi_s + tail_s, "fare": car["fare"]["taxi"] + (car["fare"].get("toll") or 0) + info["value"],
            "fare_info": info, "car": car, "taxi_s": taxi_s, "head_steps": head_steps, "tail_steps": tail_steps,
            "transit_time_s": head_s + tail_s}


async def run(cands, known, api, o, d, *, vot, baseline_s, at=None, budget, parallel=1):
    """순차 검증 — 확인한 결과(요금이 알려진 기준 경로 `known` 포함)의 Pareto 를 보며 다음 질의를 고른다 (lazy_pareto.md).

    → (검증 결과 {후보 id: verify 결과}, 탐색 객체). 기준선보다 5분 이상 빠르지 않은 결과도 돌려주되 Pareto 에는 넣지 않는다.
    """
    by_id, found = {c["id"]: c for c in cands}, {}

    async def fetch(q):
        return await getattr(api, q[0])(q[1:3], q[3:5])

    async def evaluate(model, responses):
        r = await verify(Paid(responses), by_id[model.id], o, d, vot, at)
        if r is None:
            return None
        found[model.id] = r
        return Cost(r["time_s"], r["fare"]) if compare.saves_enough(r["time_s"], baseline_s) else None

    models = [Candidate(c["id"], Cost(c["time_s"], c["fare"]), queries(c, o, d), Cost(c["lower_s"], 0)) for c in cands]
    search = LazyParetoSearch(models, known, fetch, evaluate, vot=vot)
    await search.advance(budget, parallel=parallel)
    return found, search
