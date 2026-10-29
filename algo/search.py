"""하이브리드 후보 — 하나의 규칙과 순차 검증 (method.md §3.8).

모든 하이브리드는 `O →[택시 →](대중교통·도보*) u →(택시) v →(대중교통·도보*) D` 꼴이고, 후보는 U × V 의 모든 짝이다.
U·V 는 **탐색 하나**로 만든다(`propose`):

- 시작 표지 — 시간을 아는 곳: 출발지(도보로 닿는 정류장) · 카카오 기준 경로를 타고 가다 내리는 정류장(응답 시간) ·
  출발지에서 택시로 간 정류장 · 택시로 기준 경로에 올라타 내리는 정류장. 카카오 지점에서는 걸어서 옆 정류장으로 갈아탈 수 있다
- 확장 — 거기서 정적 노선표의 노선을 최대 `LOCAL_RIDES` 번 더 탄다(`reach`). 시작 표지와 닿는 정류장이 모두 u 다
- V 는 목적지에서 거꾸로 같다 — 시작 표지는 목적지(도보) · 기준 경로의 승차 지점(그 경로를 끝까지 탄다)

기준 경로에서 자른 부분은 응답에 이미 있어 쿼터가 들지 않고, 노선을 더 탄 부분은 검증할 때 그 구간의 대중교통을 한 번 부른다.
택시는 자동차 경로 1콜이다. 확인할 후보는 순차 검증 탐색(`lazy_pareto`)이 결과를 보며 고른다.

카카오는 호출자가 넘기는 `api`(`car(출발, 도착)` · `transit(출발, 도착)`)로만 부른다 — 실패하면 `QueryUnavailable` 을 던진다.
"""
import math
from bisect import bisect_right
from datetime import timedelta

from geoutil import haversine_m

from algo import anchors, compare, reach
from algo.fares import estimate
from algo.lazy_pareto import Candidate, Cost, LazyParetoSearch

SGG_RADII = (2000.0, 10000.0)   # 좌표가 속한 시군을 가장 가까운 정류소로 본다 (택시 요금 유형·시계외)
LOCAL_RIDES = 1   # 시작 표지에서 정적 노선표로 더 타는 횟수 — 환승이 늘수록 추정 오차가 커진다(method.md E29)
# 남은 길의 하한 — 택시(그 시각의 추정)와 전철(일반 표정속도 + 역 연결) 중 빠른 쪽. 직선거리로 잰다.
# 전철 연결(역까지 · 개찰 · 대기 · 나오기)은 실측 몇 건에서 4–5분이었는데, 하한이라 낮게 둔다
REST_RAIL_KMH = anchors.RAIL_KMH_DEFAULT
REST_RAIL_CONNECT_S = 120.0
# 추정 Pareto 띠 — 다른 후보가 추정으로 이만큼 넘게 빠르고 이만큼 이상 싼 후보는 뒤 차례로 미룬다. 추정 오차(노선 부분 2–5분 · 택시 요금
# 약 1,000원, method.md E18 · E24 · E29)보다 크게 밀리는 후보는 실제로도 Pareto 에 오르기 어렵다. 버리지는 않는다 — 추정이 낙관적인
# 후보 하나가 이웃을 밀어낼 수 있어서, 띠 안을 다 확인하고 예산이 남으면 띠 밖도 확인한다(method.md E32 · E33).
# 확인을 건너뛰는 근거(하한)와는 따로다
BAND_TIME_S = 300.0
BAND_FARE = 2000.0


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


def route_taxi_heads(routes, o, stops, *, sgg_o, vot, at=None, t_max_s=anchors.T_MAX_S):
    """출발지에서 택시로 기준 경로의 승차 지점 b 까지 가서 그 경로를 타고 가다 내리는 정류소마다 앞부분.

    짝지으면 택시가 두 번(출발지 → b, u → v)인 후보가 된다 — 꼴은 같고 앞부분이 택시로 시작할 뿐이다. 택시는 b 까지 원래
    경로(도보·앞 구간)보다 빨라야 하고, 걷기보다 빠르며 30분 안이어야 한다. 같은 정류소는 하나만 둔다. 첫 택시는 검증 때
    자동차 경로를 한 번 더 부르고, 가운데 대중교통은 응답에서 자른 그대로라 다시 부르지 않는다.
    """
    best = {}
    for ri, route in enumerate(routes):
        steps = route["steps"]
        first = anchors._edge_stop(steps, "board")
        fallback = compare.fare_of(route.get("fare") or {})
        if first is None or fallback is None:
            continue
        arrive, t = {}, anchors.walk_s(haversine_m(*o, *xy(first)))
        for i, s in enumerate(steps):          # 원래 경로로 각 승차 지점에 닿는 시각(대기 전)
            if s["type"] in anchors.RIDE_TYPES:
                arrive[i] = t
                t += (s.get("wait_s") or 0) + (s.get("time_s") or 0)
            else:
                t += s.get("time_s") or 0
        points = anchors.route_points(route, o)
        for si, t_board in arrive.items():
            b = anchors._chosen(steps[si], "board")
            if not b:
                continue
            straight = haversine_m(*o, *xy(b))
            eta = anchors.taxi_s_for(straight, at)
            taxi_s = anchors.taxi_plan_s(eta, True)
            if straight < anchors.TAXI_MIN_M or eta > t_max_s or taxi_s >= t_board:
                continue
            taxi_fare = anchors.taxi_fare(anchors.road_km(straight), sgg_o, b.get("sgg_nm"), at)
            for p in points:
                if p["step"] < si or (p["step"] == si and p["fraction"] <= 0):
                    continue
                seg = anchors.prefix_steps(route, p)[si:]
                seg_s = _duration(seg)
                row = stops.by_id(p["kind"], p["id"]) or {}
                point = {"id": p["id"], "name": p["name"], "lon": p["lon"], "lat": p["lat"], "kind": p["kind"],
                         "sgg_nm": row.get("sgg_nm"), "line_group": row.get("line_group")}
                part = _part(point, seg, taxi_s + seg_s, fallback, source="taxi_route", at=at, route_index=ri,
                             taxi_from={"id": b["id"], "name": b["name"], "lon": b["lon"], "lat": b["lat"]},
                             seg_s=seg_s, lower_s=anchors.taxi_plan_s(0, True) + seg_s)
                part["fare"] += taxi_fare
                part["taxi_fare"] = taxi_fare
                _keep(best, part, vot)
    return list(best.values())


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


def _node(part):
    return part["point"].get("kind") or "bus", part["point"]["id"]


def _stop_point(net, stops, node):
    kind, sid = node
    lon, lat, name = net.coord[node]
    row = stops.by_id(kind, sid) or {}
    return {"id": sid, "name": row.get("name") or name, "lon": lon, "lat": lat, "kind": kind,
            "sgg_nm": row.get("sgg_nm"), "line_group": row.get("line_group")}


def _ride_steps(net, waits, path):
    """탐색이 더 탄 구간 → 구간들(요금 추정·화면용) · 대체 요금. 차내 시간은 탐색과 같은 잣대다."""
    steps, fallback = [], 0.0
    for leg in path:
        if leg[0] == "walk":
            steps.append({"type": "WALKING", "time_s": anchors.walk_s(leg[1]), "distance_m": leg[1]})
            continue
        _, p, i, j = leg
        kind, rid, brief, nodes, cum = net.patterns[p]
        a, b = net.coord[nodes[i]], net.coord[nodes[j]]
        dist = sum(haversine_m(*net.coord[x][:2], *net.coord[y][:2]) for x, y in zip(nodes[i:j], nodes[i + 1:j + 1]))
        step = {"type": "SUBWAY" if kind == "subway" else "BUS", "time_s": cum[j] - cum[i], "distance_m": dist,
                "wait_s": waits.wait(p, nodes[i]), "board_name": a[2], "alight_name": b[2],
                "stop_locs": [{"lon": a[0], "lat": a[1]}, {"lon": b[0], "lat": b[1]}], "line_group": brief.get("line_group")}
        if kind == "bus":
            step["fare_routes"] = [{"id": rid, "name": brief["name"], "type": brief["type"], "source": brief.get("source")}]
        steps.append(step)
        fallback += anchors.transit_fare(brief["type"], kind=kind)
    return steps, fallback


def _starts(net, parts, access):
    """시작 표지 {정류장: (초, 어떻게 왔나)}. 카카오 부분의 끝에서는 걸어서 옆 정류장으로 갈아탈 수 있다. 같은 정류장은 빠른 것."""
    out = {}

    def put(node, t, how):
        if node in net.at and (node not in out or t < out[node][0]):
            out[node] = (t, how)

    for node, (t, how) in access.items():
        put(node, t, how)
    for part in parts:
        node = _node(part)
        if node not in net.coord:
            continue
        put(node, part["time_s"], {"part": part})
        for m, dist in net.neighbors(node):
            put(m, part["time_s"] + anchors.walk_s(dist), {"part": part, "walk_m": dist})
    return out


def rest_s(straight_m, at=None):
    """직선 straight_m 를 남긴 곳에서 끝까지 가는 시간의 하한(초) — 택시 이동 추정과 전철(직선 ÷ 표정속도 + 역 연결) 중 짧은 쪽.
    택시 호출 대기는 호출자의 시간 상한에 이미 들어 있다."""
    return min(anchors.taxi_s_for(straight_m, at), straight_m / 1000.0 / REST_RAIL_KMH * 3600.0 + REST_RAIL_CONNECT_S)


def _extend(net, waits, starts, *, limit_s, far, at=None, backward=False):
    """시작 표지에서 노선을 더 타 닿는 정류장 → [(정류장, 초, 어떻게 시작했나, 더 탄 구간)].

    `far` 는 남은 길의 반대쪽 끝(정방향이면 목적지, 역방향이면 출발지) — 거기까지의 하한을 더해 시간 상한을 넘는 정류장은 펼치지 않는다.
    목적지에서 멀어지는 쪽으로 뻗는 것이 여기서 잘린다.
    """
    def rest(node):
        lon, lat, _ = net.coord[node]
        return rest_s(haversine_m(lon, lat, *far), at)

    tau, par = reach.search(net, waits, {n: t for n, (t, _) in starts.items()}, limit_s=limit_s,
                            max_rides=LOCAL_RIDES, backward=backward, rest=rest)
    out = []
    for node, t in tau.items():
        if par[node][0] == "ride":
            start, path = reach.legs(net, par, node, backward)
            out.append((node, t, starts[start][1], path))
    return out


def _walk(how):
    m = how.get("walk_m")
    return [{"type": "WALKING", "time_s": anchors.walk_s(m), "distance_m": m}] if m else []


def _walk_access(stops, pt, used):
    out = {}
    for kind in ("bus", "subway"):
        for sid, (row, dist) in anchors._near(stops, kind, pt, anchors.WALK_RADIUS_M, radius_used=used).items():
            out[(kind, sid)] = (anchors.walk_s(dist), {"walk_s": anchors.walk_s(dist)})
    return out


def pair(heads, tails, *, vot, baseline_s, at=None, t_max_s=anchors.T_MAX_S):
    """앞부분 × 뒷부분 → (후보, 진단). 택시 구간이 걷기보다 느리거나(직선 약 150 m 미만) 30분을 넘거나, ETA 가 0 이어도 기준선보다
    5분 빨라질 수 없는 짝은 뺀다. 택시 요금 유형·심야는 택시를 타는 곳과 그 시각으로 정한다.

    짝이 수만–수십만이라 일찍 거른다 — 뒷부분을 택시 도달 거리만 한 격자 칸에 나눠 담고, 칸 안은 '뒷부분 + 택시 고정분' 순으로 둔다.
    앞부분마다 주변 3×3 칸만 보고(그 밖은 직선으로도 30분 밖), 각 칸에서 시간 예산을 넘는 뒷부분은 이분 탐색으로 한꺼번에 뺀다.
    진단의 사유는 먼저 걸린 것 하나로 센다(칸 밖 = too_far, 예산 밖 = no_saving).
    """
    diag, out = {"too_close": 0, "too_far": 0, "no_saving": 0}, []
    limit = baseline_s - anchors.TAXI_MIN_SAVING_S
    reach = anchors.taxi_reach_m(t_max_s, at)   # 택시 시간 배수는 요청 시각의 시간대로 고른다
    lat_max = max([abs(p["point"]["lat"]) for p in heads + tails] or [0.0])
    dlat = math.degrees(reach / 6371000.0)
    # 폭이 가장 넓어지는 위도로 잡아 빠뜨리지 않는다
    dlon = math.degrees(reach / (6371000.0 * math.cos(math.radians(min(89.0, lat_max + dlat)))))
    cell = (max(dlon, 1e-9), max(dlat, 1e-9))
    grid = {}
    for t in tails:
        fixed = t["time_s"] + anchors.taxi_plan_s(0, bool(t["steps"]))
        k = (math.floor(t["point"]["lon"] / cell[0]), math.floor(t["point"]["lat"] / cell[1]))
        grid.setdefault(k, []).append((fixed, t))
    for k, items in grid.items():
        items.sort(key=lambda x: x[0])
        grid[k] = (items, [f for f, _ in items])
    for h in heads:
        u = xy(h["point"])
        cx, cy = math.floor(u[0] / cell[0]), math.floor(u[1] / cell[1])
        budget = limit - h["time_s"]
        seen = 0
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                bucket = grid.get((gx, gy))
                if bucket is None:
                    continue
                items, fixed = bucket
                n = bisect_right(fixed, budget)
                seen += len(items)
                diag["no_saving"] += len(items) - n
                for _, t in items[:n]:
                    if h["source"] == "origin" and t["source"] == "destination":
                        continue   # 전 구간 택시는 하이브리드가 아니다
                    v = xy(t["point"])
                    if abs(v[0] - u[0]) > dlon or abs(v[1] - u[1]) > dlat:
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
                    taxi_s = anchors.taxi_plan_s(eta, connect)
                    at_u = at + timedelta(seconds=h["time_s"]) if at is not None else None
                    taxi_fare = anchors.taxi_fare(anchors.road_km(straight), h["point"].get("sgg_nm"),
                                                  t["point"].get("sgg_nm"), at_u)
                    time_s = h["time_s"] + taxi_s + t["time_s"]
                    fare = taxi_fare + h["fare"] + t["fare"]
                    # 시간 하한 — 기준 경로에서 자른 부분과 택시 호출 대기는 응답과 무관하게 고정이다. 다시 부르는 부분은 0 으로 둔다
                    lower = ((h.get("lower_s", h["time_s"]) if h["query"] is None else 0) + anchors.taxi_plan_s(0, connect)
                             + (t["time_s"] if t["query"] is None else 0))
                    out.append({"head": h, "tail": t, "eta_s": eta, "taxi_s": taxi_s, "taxi_fare": taxi_fare,
                                "time_s": time_s, "fare": fare, "score_s": time_s + fare / vot * 60.0, "lower_s": lower})
        diag["too_far"] += len(tails) - seen
    inner, diag["off_band"] = _band(out)
    inner = {id(c) for c in inner}
    for c in out:
        c["tier"] = 0 if id(c) in inner else 1
    out.sort(key=lambda c: (c["score_s"], c["head"]["point"]["id"], c["tail"]["point"]["id"]))
    for n, c in enumerate(out):
        c["id"] = "c%d" % n
    diag["candidates"] = len(out)
    return out, diag


def _band(cands, time_s=None, fare=None):
    """추정 Pareto 띠 안의 후보 → (띠 안 후보, 띠 밖 수). 다른 후보가 `BAND_TIME_S` 넘게 빠르면서 `BAND_FARE` 이상 싸면 띠 밖이다.
    시간 순으로 한 번 훑는다 — 지금 후보보다 `BAND_TIME_S` 이상 빠른 후보들의 최소 요금과 견준다."""
    time_s = BAND_TIME_S if time_s is None else time_s
    fare = BAND_FARE if fare is None else fare
    order = sorted(cands, key=lambda c: c["time_s"])
    kept, cheapest, j = [], math.inf, 0
    for c in order:
        while j < len(order) and order[j]["time_s"] < c["time_s"] - time_s:
            cheapest = min(cheapest, order[j]["fare"])
            j += 1
        if cheapest <= c["fare"] - fare:
            continue
        kept.append(c)
    return kept, len(cands) - len(kept)


def propose(stops, routes, lines, o, d, head_routes, tail_routes, *, vot, baseline_s, at=None, headway=None,
            phys=None, hours=None, t_max_s=anchors.T_MAX_S):
    """후보 전체 → (후보, 진단). 쿼터를 쓰지 않는다.

    앞부분은 `head_routes`, 뒷부분은 `tail_routes` 에서 자른다 — 출발지에서 걸어서 타는 앞부분은 첫 대기를 실시간으로 바꾼
    사본이, 택시로 닿아 타는 뒷부분은 배차 추정 경로가 맞다. 둘은 같은 경로를 같은 순서로 담는다.
    """
    net = reach.network(stops, routes, lines)
    waits = reach._Waits(net, headway, hours, at)
    limit = baseline_s - anchors.TAXI_MIN_SAVING_S - anchors.TAXI_PICKUP_S   # 이보다 늦게 닿는 곳은 짝이 될 수 없다
    sgg_o, sgg_d = sgg_at(stops, o), sgg_at(stops, d)
    used_o, used_d = {}, {}

    # 카카오 기준 경로에서 자른 부분 — 시간이 정확한 시작 표지이자 그 자체로 u·v 다
    by_route, skipped = route_heads(head_routes, o, stops, vot=vot, at=at)
    taxi_route = route_taxi_heads(tail_routes, o, stops, sgg_o=sgg_o, vot=vot, at=at, t_max_s=t_max_s)
    route_v = route_tails(tail_routes, d, stops, vot=vot, at=at)

    heads = [_part({"id": "O", "name": "출발지", "lon": o[0], "lat": o[1], "sgg_nm": sgg_o}, [], 0.0, 0.0, source="origin")]
    heads += by_route + taxi_route
    walked = _starts(net, by_route, _walk_access(stops, o, used_o))
    best = {n: t for n, (t, _) in walked.items()}
    for node, t, how, path in _extend(net, waits, walked, limit_s=limit, far=d, at=at):
        steps, fallback = _ride_steps(net, waits, path)
        part = how.get("part")
        point = _stop_point(net, stops, node)
        prefix = (part["steps"] + _walk(how)) if part else []
        heads.append(_part(point, prefix + steps, t, fallback + (part["fallback"] if part else 0.0), source="line", at=at,
                           initial_s=part["initial_s"] if part else how["walk_s"], query=key("transit", o, xy(point))))
        best[node] = min(best.get(node, math.inf), t)

    # 택시로 시작 — 출발지에서 택시로 정류장에 가거나(택시 도달 거리 안) 택시로 기준 경로에 올라탄 뒤 노선을 더 탄다
    reach_m, taxi_access = anchors.taxi_reach_m(t_max_s, at), {}
    for kind in ("bus", "subway"):
        for sid, (row, dist) in anchors._near_any(stops, kind, [o], reach_m).items():
            if dist >= anchors.TAXI_MIN_M:
                taxi_s = anchors.taxi_plan_s(anchors.taxi_s_for(dist, at), True)
                taxi_access[(kind, sid)] = (taxi_s, {"dist": dist})
    by_taxi = _starts(net, taxi_route, taxi_access)
    for node, t, how, path in _extend(net, waits, by_taxi, limit_s=limit, far=d, at=at):
        if t >= best.get(node, math.inf):
            continue   # 걸어서 시작한 것보다 빠르지 않으면 택시를 한 번 더 낼 이유가 없다
        steps, fallback = _ride_steps(net, waits, path)
        part = how.get("part")
        point = _stop_point(net, stops, node)
        if part:
            b, prefix, taxi_fare = part["taxi_from"], part["steps"] + _walk(how), part["taxi_fare"]
            fallback += part["fallback"]
        else:
            _, p, i, _ = path[0]   # 택시에서 내려 바로 타는 정류장
            b = _stop_point(net, stops, net.patterns[p][3][i])
            prefix = []
            taxi_fare = anchors.taxi_fare(anchors.road_km(how["dist"]), sgg_o, b.get("sgg_nm"), at)
        new = _part(point, prefix + steps, t, fallback, source="taxi_line", at=at,
                    query=key("transit", xy(b), xy(point)), taxi_from=b, taxi_fare=taxi_fare)
        new["fare"] += taxi_fare
        heads.append(new)

    tails = [_part({"id": "D", "name": "목적지", "lon": d[0], "lat": d[1], "sgg_nm": sgg_d}, [], 0.0, 0.0,
                   source="destination")] + route_v
    for node, t, how, path in _extend(net, waits, _starts(net, route_v, _walk_access(stops, d, used_d)),
                                      limit_s=limit, far=o, at=at, backward=True):
        steps, fallback = _ride_steps(net, waits, path)
        part = how.get("part")
        point = _stop_point(net, stops, node)
        suffix = (_walk(how) + part["steps"]) if part else []
        tails.append(_part(point, steps + suffix, t, fallback + (part["fallback"] if part else 0.0), source="line", at=at,
                           query=key("transit", xy(point), d)))

    cands, pair_diag = pair(heads, tails, vot=vot, baseline_s=baseline_s, at=at, t_max_s=t_max_s)

    def count(parts):
        return {src: sum(p["source"] == src for p in parts) for src in dict.fromkeys(p["source"] for p in parts)}

    def walk_radius(pt, used):   # 개발자 기록이 그리는 도보 반경 — 종류별로 실제 넓혀 쓴 마지막 반경
        return {"buffers": {k: [{"center": pt, "radius_m": used.get(k), "mode": "walk"}] for k in ("bus", "subway")}}

    return cands, {"heads": {**count(heads), "skipped_routes": skipped}, "tails": count(tails), "pairs": pair_diag,
                   "line_head": walk_radius(o, used_o), "line_tail": walk_radius(d, used_d)}


def queries(cand, o, d):
    """후보 하나를 확인하는 질의 — `verify` 가 부르는 것과 같아야 한다."""
    h, t = cand["head"], cand["tail"]
    first = key("car", o, xy(h["taxi_from"])) if h.get("taxi_from") else None
    return tuple(q for q in (first, key("car", xy(h["point"]), xy(t["point"])), h["query"], t["query"]) if q is not None)


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

    기준 경로에서 자른 부분은 그대로 쓰고, 노선을 더 탄 부분은 그 구간의 대중교통 대안 중 일반화 비용이 가장 낮은 것을 쓴다.
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
    if not _car_ok(car):
        return None
    first, lead = None, []
    if h.get("taxi_from"):                     # 출발지에서 택시로 정류장에 가서 타는 앞부분
        first = await api.car(o, xy(h["taxi_from"]))
        if not _car_ok(first):
            return None
        first_s = anchors.taxi_plan_s(first["duration_s"], True)
        leg = ((h["steps"], h["seg_s"], h["fallback"], 0.0) if h["query"] is None   # 기준 경로에서 자른 그대로
               else await _leg(api, h, xy(h["taxi_from"]), u, vot))                # 노선을 더 탄 부분은 다시 부른다
        head = None if leg is None else (leg[0], first_s + leg[1], leg[2], 0.0)
        lead = [{"type": "TAXI", "time_s": first_s}]
    else:
        head = await _leg(api, h, o, u, vot)
    tail = await _leg(api, t, v, d, vot)
    if head is None or tail is None:
        return None
    (head_steps, head_s, head_fb, initial), (tail_steps, tail_s, tail_fb, _) = head, tail
    taxi_s = anchors.taxi_plan_s(car["duration_s"], any(s["type"] in anchors.RIDE_TYPES for s in tail_steps))
    info = estimate(lead + head_steps + [{"type": "TAXI", "time_s": taxi_s}] + tail_steps, head_fb + tail_fb,
                    at=at, initial_walk_s=initial)
    taxi_fares = sum(c["fare"]["taxi"] + (c["fare"].get("toll") or 0) for c in (first, car) if c)
    return {"time_s": head_s + taxi_s + tail_s, "fare": taxi_fares + info["value"],
            "fare_info": info, "car": car, "first_car": first, "first_taxi_s": lead[0]["time_s"] if lead else None,
            "taxi_s": taxi_s, "head_steps": head_steps,
            "tail_steps": tail_steps, "transit_time_s": head_s + tail_s - (lead[0]["time_s"] if lead else 0)}


def _car_ok(car):
    return bool(car.get("path")) and car.get("duration_s") is not None and (car.get("fare") or {}).get("taxi") is not None


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

    models = [Candidate(c["id"], Cost(c["time_s"], c["fare"]), queries(c, o, d), Cost(c["lower_s"], 0), c.get("tier", 0))
              for c in cands]
    search = LazyParetoSearch(models, known, fetch, evaluate, vot=vot)
    await search.advance(budget, parallel=parallel)
    return found, search
