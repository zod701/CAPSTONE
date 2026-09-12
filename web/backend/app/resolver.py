"""카카오 대중교통 구간의 승·하차 정류소 이름을 DB 행에 매칭한다 (1차: 진단용).

기준점은 구간 path 의 시작점(승차)·끝점(하차)이고, 오차 = 선택된 DB 좌표 ↔ 기준점 거리.
이름 수준은 key > alias > parts > prefix > route 순으로, 가장 높은 수준에서 가장 가까운 후보를 고른다.
`parts` 는 `.` 로 나눈 토막이 하나도 빠지지 않고 같고 순서만 다른 복합 이름(`A.B` ↔ `B.A`)이고,
`route` 는 이름을 아예 보지 않고 노선 순서표로 고른 것이다(아래 구제).
같은 이름의 정류소가 길 양쪽에 있어 거리만으로는 방향을 가릴 수 없으므로, 노선 순서표(RoutesDB)로
'운행 순서가 승차 → 하차' 인 노선만 남겨 그 노선이 서는 쪽을 고른다(_step_routes).
한쪽 끝에 **쓸 이름 후보가 없으면**(글자가 맞은 후보가 없고 접두 후보도 그 노선에 서지 않으면) 이름을 버리고
그 노선이 서는 정류장을 기준점 반경 안에서 후보로 삼는다 — 이름 근거가 없으니 운행 순서 간격이 정확히 맞는 짝만 받는다.
지도에 이름을 띄우려고 중간 정류소까지 포함한 stops[] 전부의 위치도 붙인다 — 노선과 승·하차가 정해지면
순서표에서 그 구간을 그대로 읽고(버스 _route_window · 도시철도 _line_window), 못 읽으면 이름·기하로 찾는다(locate_stops).
"""
import math
from collections import Counter

from geoutil import EARTH_R, haversine_m, local_xy
from textnorm import name_key, name_norm

from .stopsdb import HEAD_LEN

LEVELS = ("key", "alias", "parts", "prefix", "route")
# 이름 글자가 그대로 맞지 않은 수준 — 거리는 증거가 못 되고 운행 순서 간격만이 증거다
WEAK_LEVELS = ("parts", "route")
# 이름 글자가 맞은 수준 — 노선 순서 구제가 이것을 대신하지 않는다. `prefix` 는 앞 4글자만 같은 가장 느슨한
# 검사라('숙대입구' ↔ '숙대입구.서울시교육청역') 그 후보가 노선에 서지 않으면 순서표에 진다
NAME_LEVELS = ("key", "alias", "parts")
ROLES = ("board", "alight")
STEP_KINDS = {"BUS": "bus", "SUBWAY": "subway"}
PREFIX_MIN_LEN = HEAD_LEN  # 절단된 이름 대응(4). 짧은 키끼리의 접두 일치는 우연이 많다
PARTS_MIN_LEN = 2        # 토막 최소 길이 — 1글자 토막(`4.19민주묘지`)은 우연히 겹친다
NEAR_ORDER = 2           # 운행 순번이 이만큼 붙어 있는 경쟁 후보는 간격으로 가릴 수 없다(한 칸 어긋나면 뒤집힌다)
SAME_STATION_M = 300.0   # 같은 이름의 지하철 행이 이 안에 있으면 같은 역(환승 노선 행)
NEAREST_ANY_M = 200.0
# 중간 정류소 후보: 구간 선에서 이 거리 안. 버스 정류소는 도로 중심선에서 수십 m, 역 좌표는 선로에서 수백 m 까지 떨어진다
STOP_CORRIDOR_M = {"bus": 100.0, "subway": 500.0}
# 중간 정류소 점수에서 '노선이 서지 않는다' 에 매기는 감점 — 이름 수준 감점(100 × 수준)보다 반드시 커야
# 노선 경유가 이름 수준을 이긴다. LEVELS 가 늘어나도 그 관계가 유지되도록 길이에서 뽑는다
OFF_ROUTE_PENALTY = 100 * len(LEVELS)
ORDER_SLACK_M = 50.0     # 정류소 순서 판정 여유 — 길 건너편 정류소·역 좌표의 투영 차이만큼은 뒤로 가도 된다
WINDOW_NAME_MIN = 0.6    # 순서표 구간을 믿는 최소 이름 일치 비율 — 노선 변형·낡은 순서표를 막는다


def _parts_key(s, kind):
    """`.` 로 나눈 토막을 각각 키로 만들어 정렬한 것 — 토막 순서만 뒤바뀐 복합 이름(`A.B` ↔ `B.A`)을 맞추는 키.

    토막이 하나면 빈 값이다(맞출 것이 없다). 1글자 토막이 생기는 이름(`4.19민주묘지`)도 빈 값으로 둔다 —
    한 글자는 우연히 겹친다. 토막을 하나도 **버리지 않으므로** 부분 이름 일치와 다르다 — `A.B` 와 `B` 는
    여전히 안 맞고, 흔한 토막 하나로 붙는 오매칭이 생기지 않는다.
    """
    ps = sorted(name_key(p, kind) for p in name_norm(s).split(".") if p)
    return tuple(ps) if len(ps) >= 2 and all(len(p) >= PARTS_MIN_LEN for p in ps) else ()


def _row_parts(row, kind):
    """행 이름·별칭의 토막 키 집합 — 한 번 만들어 행에 담아 둔다(색인을 StopsDB 로 옮기면 그 값을 쓴다).

    별칭도 넣는다: 병합으로 모인 다른 원천 이름 쪽이 뒤바뀐 표기일 수 있다.
    """
    ps = row.get("parts_keys")
    if ps is None:
        names = [row["name"], *(a for a in (row.get("aliases") or "").split("|") if a)]
        ps = row["parts_keys"] = {p for p in (_parts_key(n, kind) for n in names) if p}
    return ps


def _level(kind, qk, row, pq=()):
    k = row["name_key"]
    if qk == k:
        return "key"
    if qk in row["alias_keys"]:
        return "alias"
    if pq and pq in _row_parts(row, kind):  # 토막이 하나도 빠지지 않고 다 같다 — 순서만 뒤바뀐 복합 이름
        return "parts"
    if min(len(qk), len(k)) >= PREFIX_MIN_LEN and (qk.startswith(k) or k.startswith(qk)):
        return "prefix"
    return None


def _row_id(kind, row):
    return row["stop_key"] if kind == "bus" else row["station_id"]


def _same_entity(kind, a, b):
    if kind == "bus":
        return a["stop_key"] == b["stop_key"]
    return (a["name_key"] == b["name_key"]
            and haversine_m(a["lon"], a["lat"], b["lon"], b["lat"]) <= SAME_STATION_M)


def _nearby_extras(db, e, kind, qk, lon, lat):
    """기준점에 가장 가까운 행 · 같은 이름 행까지의 거리 — 미매칭과 **이름이 다른 채 찾은** 항목에 붙인다.

    정류소 DB 가 낡았다(신설·개명·좌표 결함)는 신호가 구제에 삼켜지지 않게 남기는 값이다.
    """
    hits = db.near(kind, lon, lat, NEAREST_ANY_M)
    if hits:
        i, d = hits[0]
        row = db.row(kind, i)
        e["nearest_any"] = {"id": _row_id(kind, row), "name": row["name"], "dist_m": round(d, 1)}
    same = [db.row(kind, i) for i in db.by_key(kind, qk)] if qk else []
    if same:
        e["nearest_same_name_m"] = round(min(haversine_m(lon, lat, r["lon"], r["lat"]) for r in same), 1)


def _candidates(db, kind, qk, pq, lon, lat, radius_m, allow=None):
    """기준점 반경 안에서 이름이 맞는 DB 행 → [(수준 순위, 거리, 행)] (near() 가 거리순이라 이 목록도 거리순).

    allow(노선 순서로 가린 정류장 키)를 넘기면 그 밖의 행은 빼고, 이름으로는 안 맞는 행도 `route` 수준으로 받는다 —
    이름이 어긋난 승·하차를 노선 순서가 구제한 경우다(_step_routes). 이름이 맞는 행이 있으면 수준이 높아 먼저 잡히므로,
    이름 후보에서 나온 allow 에서는 `route` 승격이 실제로 일어나지 않는다.
    후보는 언제나 정류소 DB 행이다 — 순서표에만 있는 정류장은 chosen 을 채울 수 없어 애초에 후보가 아니다.
    """
    out = []
    if not (qk or allow):
        return out
    for i, d in db.near(kind, lon, lat, radius_m):
        row = db.row(kind, i)
        if allow is not None and _row_id(kind, row) not in allow:
            continue
        lv = _level(kind, qk, row, pq) if qk else None
        if lv is None and allow is None:
            continue
        out.append((LEVELS.index(lv or "route"), d, row))
    return out


def _end_cands(db, kind, name, anchor, radius_m):
    """한쪽 끝의 후보 (정류장 키, 이름 수준) — 노선을 가리기 전에 본다 (거리순)."""
    if anchor is None:
        return []
    cands = _candidates(db, kind, name_key(name or "", kind), _parts_key(name or "", kind),
                        anchor[0], anchor[1], radius_m)
    return [(_row_id(kind, row), LEVELS[lv]) for lv, _, row in cands]


def _route_stop_keys(db, rdb, rids, anchor, radius_m):
    """노선들이 서는 정류장 중 기준점 반경 안 · 정류소 DB 에 있는 것 (이름을 보지 않는다, 거리순).

    순서표에만 있는 정류장(in_db=0)·미정차는 DB 색인에 없어 저절로 빠진다.
    기준점이 없는 구간(마지막 구간의 하차)은 후보를 만들지 않는다 — 반경이 원거리 오매칭을 막는 유일한 방어선이다.
    """
    if anchor is None:
        return []
    on = {k for rid in rids for k in rdb.order_of(rid)}
    return [row["stop_key"] for row in (db.row("bus", i) for i, _ in db.near("bus", anchor[0], anchor[1], radius_m))
            if row["stop_key"] in on]


def _gap(ob, oa):
    """승차 → 하차 운행 순서 간격 중 가장 작은 양수. 회차해 다시 지나는 정류장은 순서가 둘이라 조합을 다 본다.
    양수가 없으면(이 노선에서 하차가 승차보다 앞) None — 탈 수 없는 짝이다."""
    diffs = [y - x for x in ob for y in oa if y > x]
    return min(diffs) if diffs else None


def _near_order_rivals(orders, keep, on, role, allowed, other):
    """남긴 후보와 운행 순번이 NEAR_ORDER 안으로 붙어 있는 탈락 후보 — 구간 정류소 개수가 한 칸 어긋나면 답이 뒤집히는 짝이다.

    이름 글자가 다른 수준에서는 간격이 유일한 증거인데, 붙어 있는 순번끼리는 그 증거가 약하다. 함께 남겨 모호로 보고한다.
    반대쪽 끝(other)과 '승차 → 하차' 로 이어지지 않는 후보는 뺀다 — 하차 뒤에서 타는 짝은 한 칸 어긋남으로 설명되지 않는다.
    간격이 맞는 후보(allowed)가 있으므로 경쟁 후보는 chosen 이 되지 않고 모호 근거로만 쓰인다(resolve_endpoint).
    """
    out = set()
    for rid in keep:
        order = orders[rid]
        kept = [o for k in allowed if k in order for o in order[k]]
        for k in on[rid][role]:
            if k in allowed or not any(abs(o - ko) <= NEAR_ORDER for o in order[k] for ko in kept):
                continue
            ends = [(order[k], order[o]) if role == "board" else (order[o], order[k])
                    for o in other if o in order]
            if any(_gap(*e) is not None for e in ends):
                out.add(k)
    return out


def _step_routes(rdb, vehicle_names, cand_levels, n_stops=0, db=None, anchors=None, radius_m=0.0):
    """버스 구간이 탄 노선을 차량 이름 + 승·하차 후보로 가린다
    → (노선 id 들, {역할: 허용 키 집합|None}, {역할: 경쟁 후보 키}, {역할: 진단 문구}, {역할: 구제 근거|None}).

    같은 번호의 노선이 시·군마다 있어(마을 '5' 21개) 이름만으로는 하나가 되지 않는다. 승·하차 후보를
    **운행 순서가 승차 → 하차** 로 잇는 노선만 남기면 길 건너 반대 방향 정류장이 떨어진다 — 이것이 진행 방향 판정이다.
    경기 순서표는 상행 뒤에 하행이 한 순번열로 이어져 '상행 승차 + 하행 하차' 짝도 이 조건을 지난다. 그래서
    **구간의 정류소 개수(n_stops)와 순서 간격이 가장 잘 맞는** 짝만 남긴다 — 반대 방향 짝은 간격이 크게 벗어난다
    (실측: 기대 2 인 곳에서 27·56).
    한쪽 끝의 후보가 없으면(기준점이 없는 마지막 구간 등) 순서를 볼 수 없으니 경유 여부만 본다.

    한쪽 끝의 이름 후보가 **쓸 것이 하나도 없으면**(개명·부분 이름·DB 에 없는 표기) 이름을 버리고 그 노선이 서는
    정류장을 기준점 반경 안에서 후보로 삼는다(구제) — 중간 정류소를 순서표에서 읽는 것과 같은 근거를 양끝에 쓰는 것이다.
    '쓸 것이 없다' 는 **글자가 맞은 후보(NAME_LEVELS)가 없고, 접두 후보도 그 노선에 서지 않는다** 는 뜻이다.
    글자가 맞는 행이 있는데 그 행이 순서표에 없는 것은 **순서표가 낡았다는 뜻**이므로 구제하지 않는다 — 이름 근거를
    버리고 순서표를 믿으면 글자가 그대로 맞는 0 m 후보를 두고 수백 m 떨어진 노선 정류장을 고른다(실데이터: 어느 노선에도
    없는 실차 정류소 136곳). 그때는 그 역할만 노선으로 가리지 않는다(line_filter mismatch).
    접두 후보는 그 노선에 설 때만 근거다 — 노선에 서지 않는 접두 일치는 앞 4글자가 겹친 다른 정류소인 경우가 많다
    (실측: '숙대입구.서울시교육청역' 이 232 m 짜리 '숙대입구' 에 붙었다. 순서표는 10.5 m 짜리 '숙대입구역' 을 준다).
    이름 근거가 없는 구제에는 제한을 둔다: 구간 정류소 개수를 모르면(n_stops < 2) 간격을 볼 수 없어 구제하지 않고,
    **간격이 정확히 맞는 짝만** 받는다(낡은 순서표는 후보가 사라져 미매칭이다).
    간격 제한은 역할마다 따로 본다 — 한쪽 끝이 약해도 다른 쪽 끝의 진행 방향 판정은 그대로 둔다(간격이 정확히 맞는 짝이
    없으면 오차가 가장 작은 짝을 쓴다).
    """
    rids = list(dict.fromkeys(r for n in vehicle_names for r in rdb.routes_by_name(n)))
    if not rids:
        return ((), {r: None for r in ROLES}, {r: frozenset() for r in ROLES},
                {r: "unmapped" for r in ROLES}, {r: None for r in ROLES})
    want = n_stops - 1 if n_stops >= 2 else None  # 구간 정류소 개수를 모르면 간격은 보지 않는다
    orders = {rid: rdb.order_of(rid) for rid in rids}
    keys = {role: dict(cand_levels[role]) for role in ROLES}   # 정류장 키 → 이름 수준 (거리순)
    saved = {role: () for role in ROLES}                       # 구제한 역할 → 반경 안 노선 정류장
    for role in ROLES:
        if (db is None or want is None
                or any(lv in NAME_LEVELS for lv in keys[role].values())        # 글자가 맞은 근거는 바꾸지 않는다
                or any(k in o for o in orders.values() for k in keys[role])):  # 접두 후보라도 그 노선에 서면 근거다
            continue
        rescued = _route_stop_keys(db, rdb, rids, (anchors or {}).get(role), radius_m)
        if rescued:
            keys[role], saved[role] = {k: "route" for k in rescued}, tuple(rescued)
    on = {rid: {role: [k for k in keys[role] if k in orders[rid]] for role in ROLES} for rid in rids}
    on_any = {role: list(dict.fromkeys(k for rid in rids for k in on[rid][role])) for role in ROLES}
    # 결정이 내려질 이름 수준(가장 높은 수준)이 이름 글자가 다른 수준이면 간격이 유일한 증거다 —
    # 같은 반경에 접두 후보가 섞여도 판정은 더 높은 수준에서 내려지므로 그 수준으로 본다
    weak = {role: bool(on_any[role])
            and min((keys[role][k] for k in on_any[role]), key=LEVELS.index) in WEAK_LEVELS
            for role in ROLES}
    pairs = []                                    # (간격 오차, 노선 id, 승차 키, 하차 키)
    on_route, seen_rids = {r: set() for r in ROLES}, []   # 순서를 볼 수 없을 때 쓸 경유 여부
    for rid in rids:
        order = orders[rid]
        if any(on[rid].values()):
            seen_rids.append(rid)
            for role in ROLES:
                if not saved[role]:  # 구제 후보는 '경유 여부만' 대체 경로에 넣지 않는다 — 간격이 유일한 근거다
                    on_route[role].update(on[rid][role])
        for b in on[rid]["board"]:
            for a in on[rid]["alight"]:
                gap = _gap(order[b], order[a])
                if gap is not None:
                    pairs.append((abs(gap - want) if want is not None else 0, rid, b, a))
    rivals = {role: frozenset() for role in ROLES}         # 간격이 어긋난 경쟁 후보 — 모호 근거로만 쓴다
    if pairs:
        best = min(p[0] for p in pairs)
        keep = list(dict.fromkeys(rid for err, rid, *_ in pairs if err == best))
        fit = {"board": {b for err, _, b, _ in pairs if err == best},   # 간격 오차가 가장 작은 짝의 키
               "alight": {a for err, _, _, a in pairs if err == best}}
        allow = {role: set(fit[role]) for role in ROLES}
        for role in ROLES:
            if not weak[role]:
                continue
            other = fit["alight" if role == "board" else "board"]
            if not best:      # 간격이 정확히 맞는 짝 — 한 칸 어긋나면 뒤집히는 순번 이웃을 경쟁 후보로 함께 남긴다
                rivals[role] = frozenset(_near_order_rivals(orders, keep, on, role, fit[role], other))
            elif saved[role]:  # 이름 근거가 없는 구제는 간격이 정확히 맞아야 받는다
                allow[role] = set()
            else:              # 이름은 맞지만 간격이 어긋났다 — 간격으로 가리지 않고 그 노선 위 후보를 다 남긴다
                rivals[role] = frozenset(k for k in on_any[role] if k not in fit[role])
            allow[role] |= rivals[role]
    else:
        keep, allow = seen_rids, {role: set(on_route[role]) for role in ROLES}
    notes, rescue = {}, {}
    for role in ROLES:
        got = bool(allow[role])
        notes[role] = ("route" if saved[role] else "applied") if got else ("route_gap" if saved[role] else "mismatch")
        rescue[role] = ({"route_ids": list(keep), "gap": want, "n_rivals": len(rivals[role]),
                         "n_in_radius": len(saved[role])} if saved[role] and got else None)
    return (tuple(keep), {role: (allow[role] or None) for role in ROLES}, rivals, notes, rescue)


def _unlinked(rdb, rids, res):
    """노선으로 가린 승·하차가 **한 노선으로 이어지지 않으면** True.

    같은 이름의 노선이 여럿이면(실데이터: 그대로 같은 이름인 노선 1,632개) 역할별로 따로 고르다 어느 노선도 잇지 못하는
    짝이 나온다 — 둘 중 하나는 반드시 오답이므로 둘 다 모호로 내린다(line_filter `unlinked`).
    한쪽이라도 노선으로 가리지 않았으면(mismatch·unmapped) 이을 근거가 애초에 없어 보지 않는다.
    """
    ids = [(res[role]["chosen"] or {}).get("id") for role in ROLES]
    if not (all(ids) and all(res[role]["line_filter"] in ("applied", "route") for role in ROLES)):
        return False
    b, a = ids
    return not any(b in o and a in o and _gap(o[b], o[a]) is not None for o in map(rdb.order_of, rids))


def _best_window(db, kind, best, want):
    """구간 검색이 고른 (이름 일치 수, [(id, 이름, lat, lon)…]) → 정류소마다 위치 항목, 못 믿으면 None.

    자리마다 이름이 맞는지 세어 WINDOW_NAME_MIN 을 넘을 때만 쓴다 — 노선 변형·낡은 순서표를 막는다.
    좌표는 정류소 DB 를 먼저 쓰고, DB 에 없는 정류소는 순서표 좌표를 쓴다.
    """
    if best is None or best[0] < max(2, WINDOW_NAME_MIN * len(want)):
        return None
    out = []
    for sid, _, lat, lon in best[1]:
        row = db.by_id(kind, sid)
        out.append({"lon": row["lon"] if row else lon, "lat": row["lat"] if row else lat,
                    "id": sid, "level": "route"})
    return out


def _hits(kind, win, want, wparts):
    """구간 자리마다 순서표 이름이 카카오 이름과 맞는지 센다 — 토막 순서만 뒤바뀐 이름도 맞은 것으로 센다.
    (순서표 이름은 정류소 DB 이름과 같아, 뒤바뀐 표기는 언제나 카카오 쪽이다)"""
    return sum(name_key(nm, kind) == w or (bool(p) and _parts_key(nm, kind) == p)
               for (_, nm, _, _), w, p in zip(win, want, wparts))


def _route_window(db, rdb, rids, stops, board_key, alight_key):
    """버스: 승차 → 하차가 구간 정류소 개수만큼 이어지는 순서표 구간 → 위치 항목, 못 찾으면 None.

    노선과 승·하차가 정해지면 그 사이 정류소는 순서표가 이미 알고 있다 — 이름·기하로 다시 고르지 않고 그대로 읽으면
    노선이 양쪽에 다 서는 정류소(길 건너편)와 DB 이름이 다른 복합 이름이 함께 풀린다.
    """
    want = [name_key(s, "bus") for s in stops]
    wparts = [_parts_key(s, "bus") for s in stops]
    n = len(want)
    best = None
    for rid in rids:
        seq = rdb.sequence(rid)
        for i in range(len(seq) - n + 1):
            if seq[i] != board_key or seq[i + n - 1] != alight_key:
                continue
            win = [(k, *rdb.stop(k)) for k in seq[i:i + n]]
            hits = _hits("bus", win, want, wparts)
            if best is None or hits > best[0]:
                best = (hits, win)
    return _best_window(db, "bus", best, want)


def _line_window(db, ldb, line_group, stops, board_id, alight_id):
    """도시철도: 노선 덩어리에서 승차 → 하차 구간을 뗀다 → 위치 항목, 못 찾으면 None.

    덩어리에는 방향이 없어 승·하차가 어디냐로 정한다(거꾸로 잡히면 뒤집는다). 순환선은 끝에서 처음으로 이어 붙인다.
    급행은 카카오가 정차역만 주므로 역 개수가 안 맞아 구간을 못 찾는다 — 그때는 이름·기하로 돌아간다.
    """
    want = [name_key(s, "subway") for s in stops]
    wparts = [_parts_key(s, "subway") for s in stops]
    n = len(want)
    best = None
    for cid in ldb.chains_for(line_group):
        seq = ldb.sequence(cid)
        ids = seq + seq[:n - 1] if ldb.is_loop(cid) and n <= len(seq) else seq
        for i in range(len(ids) - n + 1):
            win = ids[i:i + n]
            if win[-1] == board_id and win[0] == alight_id:
                win = win[::-1]
            elif not (win[0] == board_id and win[-1] == alight_id):
                continue
            win = [(sid, *ldb.station(sid)) for sid in win]
            hits = _hits("subway", win, want, wparts)
            if best is None or hits > best[0]:
                best = (hits, win)
    return _best_window(db, "subway", best, want)


def resolve_endpoint(db, kind, role, name, anchor, vehicle_names, radius_m, ambig_gap_m,
                     allow=None, line_filter=None, rescue=None, rivals=()):
    """승차 또는 하차 한 곳을 매칭해 진단 항목을 돌려준다.

    allow 는 노선으로 가린 정류장 키 집합(버스), line_filter 는 그 진단 문구,
    rescue 는 이름을 버리고 노선 순서로 후보를 만들었을 때의 근거(노선 id·간격)다.
    rivals 는 allow 안에서 **운행 순서 간격이 어긋난** 경쟁 후보다 — 모호 근거로만 쓰고 chosen 으로 고르지 않는다
    (chosen 은 지도 칩·실시간 도착에 그대로 쓰인다). 그래서 `second_gap_m` 은 경쟁 후보가 더 가까우면 음수다.
    이름 글자가 다른 수준(parts·route)은 거리로 순위를 가릴 수 없어, 경쟁 후보가 하나라도 있으면 그대로 모호로 남기고
    `nearest_any`·`nearest_same_name_m` 를 함께 채운다 — DB 가 낡았다는 신호를 구제가 삼키지 않게.
    """
    qk = name_key(name or "", kind)
    pq = _parts_key(name or "", kind)
    groups = set()
    line_filter = line_filter or "n/a"
    if kind == "subway":
        groups = {db.kakao_group(v) for v in vehicle_names or ()} - {None}
        line_filter = "applied" if groups else "unmapped"
    e = {
        "role": role, "kind": kind, "query_name": name, "query_key": qk,
        "anchor": [anchor[0], anchor[1]] if anchor is not None else None,
        "status": "unmatched", "match_level": None, "chosen": None, "error_m": None,
        "n_candidates": 0, "n_global_same_name": len(db.by_key(kind, qk)) if qk else 0,
        "second_gap_m": None, "line_filter": line_filter, "name_conflict": False,
        "nearest_any": None, "nearest_same_name_m": None,
        # 고른 행의 이름이 질의 이름과 글자까지 같지는 않다(토막 순서 뒤바뀜·절단·개명) / 구제 근거
        "name_mismatch": False, "rescue": rescue,
    }
    if anchor is None:
        if qk:  # 이름이 비었으면 skipped 가 아니라 unmatched
            e["status"] = "skipped"
        return e
    lon, lat = anchor[0], anchor[1]

    cands = _candidates(db, kind, qk, pq, lon, lat, radius_m, allow)
    if groups:
        same_line = [c for c in cands if c[2]["line_group"] in groups]
        if same_line:
            cands = same_line
        elif cands:  # 노선이 맞는 후보가 없어 필터 없이 고른다
            e["line_filter"] = "mismatch"
    e["n_candidates"] = len(cands)
    if not cands:
        _nearby_extras(db, e, kind, qk, lon, lat)
        return e

    # 순위: (간격이 어긋났나, 이름 수준, 거리). **간격을 이름 수준보다 먼저 본다** — 순서표가 말한 자리가
    # 표기보다 강한 증거다. 이름 수준으로만 고르면 간격이 한 칸 어긋난 높은 수준 후보가 간격이 맞는 낮은 수준
    # 후보를 조용히 밀어낸다(토막 일치가 접두 일치를 이기는 경로)
    off = lambda c: _row_id(kind, c[2]) in rivals   # noqa: E731
    best = min((off(c), c[0]) for c in cands)
    top = [c for c in cands if (off(c), c[0]) == best]
    _, d0, chosen = top[0]
    level = LEVELS[best[1]]
    # 모호 근거 — 같은 순위의 다른 실체 + 간격이 어긋나 뒤로 보낸 경쟁 후보(어느 쪽인지 말해 주지 못한다)
    peers = [d for _, d, r in top if not _same_entity(kind, chosen, r)]
    pushed = [] if best[0] else [d for _, d, r in cands
                                 if _row_id(kind, r) in rivals and not _same_entity(kind, chosen, r)]
    others = sorted(peers + pushed)
    gap = others[0] - d0 if others else None
    # 토막 일치·노선 구제는 거리 차가 커도 어느 쪽인지 말해 주지 못한다(길 양쪽 쌍의 중앙 거리 43 m > 모호 기준 10 m)
    ambiguous = bool(others) if level in WEAK_LEVELS else (gap is not None and gap < ambig_gap_m)
    e.update(
        status="ambiguous" if ambiguous else "matched",
        match_level=level,
        chosen={"id": _row_id(kind, chosen), "name": chosen["name"], "lat": chosen["lat"],
                "lon": chosen["lon"], "sgg_nm": chosen["sgg_nm"],
                "line_group": chosen["line_group"] if kind == "subway" else None},
        error_m=round(d0, 1),
        second_gap_m=None if gap is None else round(gap, 1),
        name_mismatch=bool(qk) and qk != chosen["name_key"] and qk not in chosen["alias_keys"],
    )
    if level in WEAK_LEVELS:
        _nearby_extras(db, e, kind, qk, lon, lat)
    return e


def _path_frame(path):
    """구간 선 → path[0] 기준 로컬 미터 좌표와 꼭짓점까지의 누적 거리."""
    lon0, lat0 = path[0]
    xy = [local_xy(p[0], p[1], lon0, lat0) for p in path]
    cum = [0.0]
    for (ax, ay), (bx, by) in zip(xy, xy[1:]):
        cum.append(cum[-1] + math.hypot(bx - ax, by - ay))
    return xy, cum


def _passes(xy, cum, px, py, corridor_m):
    """로컬 좌표의 점에 선이 corridor_m 안으로 다가가는 구간마다 (거리 m, 선을 따라간 거리 m) 하나씩, 선 순서대로.
    갔다가 돌아오는 노선은 같은 정류소 곁을 두 번 지나므로 여러 개가 나온다. 선이 꼭짓점에서 복도 밖에 있으면
    거기서 한 번의 통과가 끝난다(긴 선분 하나가 복도에 들어왔다 나가는 경우도 한 번으로 센다)."""
    if len(xy) == 1:
        d = math.hypot(px - xy[0][0], py - xy[0][1])
        return [(d, 0.0)] if d <= corridor_m else []
    out, best = [], None
    for i in range(len(xy) - 1):
        ax, ay = xy[i]
        bx, by = xy[i + 1]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
        d = math.hypot(px - ax - t * dx, py - ay - t * dy)
        if d <= corridor_m and (best is None or d < best[0]):
            best = (d, cum[i] + t * (cum[i + 1] - cum[i]))
        if best is not None and math.hypot(px - bx, py - by) > corridor_m:
            out.append(best)
            best = None
    if best is not None:
        out.append(best)
    return out


def _point_at(path, cum, s):
    """선을 따라 s m 간 곳의 [lon, lat] (꼭짓점 사이 선형 보간)."""
    for i in range(len(path) - 1):
        if s <= cum[i + 1] or i == len(path) - 2:
            seg = cum[i + 1] - cum[i]
            t = 0.0 if seg == 0 else max(0.0, min(1.0, (s - cum[i]) / seg))
            return [path[i][0] + t * (path[i + 1][0] - path[i][0]), path[i][1] + t * (path[i + 1][1] - path[i][1])]
    return [path[0][0], path[0][1]]


def _stop_options(db, kind, k, name, groups, frame, box, corridor_m, route_keys=None):
    """k 번째 정류소 이름이 맞는 DB 행이 구간 선 곁을 지나는 곳마다 후보 (k, 점수, 선 위 거리, 위치 항목).
    점수: 같은 노선(지하철 노선군 · 버스 노선 경유) > 이름 수준(key > alias > prefix) > 선까지 거리 —
    어느 후보든 0 보다 커서 하나라도 더 맞히는 조합이 이긴다. 노선이 서지 않는 쪽은 점수를 깎을 뿐 빼지는 않는다
    (순서표에 없는 정류소·노선 변형이 있어, 다른 후보가 없으면 그대로 쓴다)."""
    qk = name_key(name or "", kind)
    if not qk:
        return []
    idx = set(db.by_key(kind, qk)) | set(db.by_alias(kind, qk))
    if len(qk) >= PREFIX_MIN_LEN:
        idx |= set(db.by_head(kind, qk[:PREFIX_MIN_LEN]))
    (xy, cum), (lon0, lat0) = frame
    out = []
    for i in sorted(idx):
        row = db.row(kind, i)
        lv = _level(kind, qk, row)  # 중간 정류소는 순서표 구간(_route_window)이 이미 풀어 토막 수준을 쓰지 않는다
        if lv is None or not (box[0] <= row["lon"] <= box[2] and box[1] <= row["lat"] <= box[3]):
            continue
        mismatch = ((bool(groups) and row["line_group"] not in groups)
                    or (route_keys is not None and _row_id(kind, row) not in route_keys))
        loc = {"lon": row["lon"], "lat": row["lat"], "id": _row_id(kind, row), "level": lv}
        for d, s in _passes(xy, cum, *local_xy(row["lon"], row["lat"], lon0, lat0), corridor_m):
            out.append((k, 1000 - OFF_ROUTE_PENALTY * mismatch - 100 * LEVELS.index(lv)
                        - 100 * d / corridor_m, s, loc))
    return out


def _assign_in_order(options):
    """후보 중 정류소 순서대로 선 위 거리가 (ORDER_SLACK_M 여유 안에서) 줄지 않는 조합의 점수 합 최대 → {k: (선 위 거리, 위치)}.
    정류소마다 후보는 몇 개뿐이라 O(후보²) 동적 계획으로 충분하다."""
    options = sorted(options, key=lambda o: (o[0], o[2]))
    total, back = [], []
    for j, (k, score, s, _) in enumerate(options):
        best, prev = score, -1
        for i in range(j):
            ki, _, si, _ = options[i]
            if ki < k and si <= s + ORDER_SLACK_M and total[i] + score > best:
                best, prev = total[i] + score, i
        total.append(best)
        back.append(prev)
    out = {}
    j = max(range(len(options)), key=total.__getitem__, default=-1)
    while j >= 0:
        k, _, s, loc = options[j]
        out[k] = (s, loc)
        j = back[j]
    return out


def locate_stops(db, step, kind, groups, corridor_m, route_keys=None):
    """구간 stops[] 전부의 지도 위치 (이름 표시용) — stops 와 같은 길이, 원소는 {lon, lat, id, level} 또는 None.

    route_keys 를 넘기면(버스) 그 노선이 서는 정류장을 먼저 고른다 — 길 건너 반대 방향 정류장과 가리는 데 쓴다.
    승·하차는 매칭 결과(step["resolution"])를 그대로 쓰고, 중간 정류소는 구간 선 곁을 지나는 같은 이름 DB 행에서 찾되
    정류소 순서와 선 위 순서가 어긋나지 않는 조합으로 고른다(갔다 돌아오는 노선에서 같은 이름이 두 번 나와도 각자 제자리).
    못 찾은 정류소는 앞뒤로 위치가 정해진 정류소 사이를 순번 비율로 나눠 선 위에 놓는다(id·level = None: 추정 위치).
    선이 없는 구간은 매칭된 승·하차만 채운다.
    """
    stops = step.get("stops") or []
    path = step.get("path") or []
    n = len(stops)
    locs = [None] * n
    res = step.get("resolution") or {}
    for k, role in ((0, "board"), (n - 1, "alight")):
        e = res.get(role) if n and (role == "board" or n >= 2) else None
        if e and e.get("chosen"):
            c = e["chosen"]
            locs[k] = {"lon": c["lon"], "lat": c["lat"], "id": c["id"], "level": e["match_level"]}
    if not (n and path):
        return locs

    frame = (_path_frame(path), tuple(path[0]))
    cum = frame[0][1]
    pad_lat = math.degrees(corridor_m / EARTH_R)
    pad_lon = pad_lat / max(math.cos(math.radians(path[0][1])), 1e-6)
    lons, lats = [p[0] for p in path], [p[1] for p in path]
    box = (min(lons) - pad_lon, min(lats) - pad_lat, max(lons) + pad_lon, max(lats) + pad_lat)
    options = [o for k in range(1, n - 1)
               for o in _stop_options(db, kind, k, stops[k], groups, frame, box, corridor_m, route_keys)]

    # 선 위 거리: 구간 선은 승차에서 시작해 하차에서 끝나므로 양끝은 0 과 선 길이
    along = [None] * n
    along[0] = 0.0
    if n >= 2:
        along[-1] = cum[-1]
    for k, (s, loc) in _assign_in_order(options).items():
        locs[k], along[k] = loc, s
    # 추정: 위치가 정해진 앞뒤 정류소의 선 위 거리 사이를 순번으로 보간
    known = [k for k in range(n) if along[k] is not None]
    for k in range(n):
        if locs[k] is None:
            a = max(j for j in known if j <= k)
            b = min((j for j in known if j >= k), default=a)
            s = along[a] if b == a else along[a] + (along[b] - along[a]) * (k - a) / (b - a)
            lon, lat = _point_at(path, cum, s)
            locs[k] = {"lon": lon, "lat": lat, "id": None, "level": None}
    return locs


def resolve_route(db, route, radius_m, ambig_gap_m, memo=None, routes=None, lines=None):
    """경로의 BUS·SUBWAY 구간마다 승·하차를 매칭해 step 에 붙이고, 진단 항목 목록을 돌려준다.
    step["stop_locs"] 에 정류소 전부의 위치도 붙인다. memo(dict)를 넘기면 경로 사이에 똑같은 구간의 위치를 한 번만 계산한다.
    routes(RoutesDB)를 넘기면 버스 구간의 진행 방향을 노선 순서로 가리고, lines(LinesDB)를 넘기면 도시철도 구간의
    역을 노선 순서표에서 읽는다."""
    steps = route.get("steps") or []
    entries = []
    for i, step in enumerate(steps):
        kind = STEP_KINDS.get(step.get("type"))
        step["line_group"] = None
        step["color"] = None
        if kind is None:
            step["resolution"] = None
            step["stop_locs"] = None
            continue
        names = [v.get("name") for v in step.get("vehicles") or []]
        if kind == "subway":
            group = next((g for g in map(db.kakao_group, names) if g), None)
            step["line_group"] = group
            step["color"] = db.group_color(group) if group else None

        path = step.get("path") or []
        prev = (steps[i - 1].get("path") or []) if i > 0 else []
        nxt = (steps[i + 1].get("path") or []) if i + 1 < len(steps) else []
        stops = step.get("stops") or []
        ends = (
            ("board", step.get("board_name"), path[0] if path else (prev[-1] if prev else None),
             stops[0] if stops else None, step.get("guidance_from")),
            # 하차 이름은 stops 가 2개 이상일 때만 stops[-1] 에서 온다 (transit.normalize_transit)
            ("alight", step.get("alight_name"), path[-1] if path else (nxt[0] if nxt else None),
             stops[-1] if len(stops) >= 2 else None, step.get("guidance_to")),
        )
        rids = ()
        allow, rivals, filters, rescues = ({r: None for r in ROLES}, {r: () for r in ROLES},
                                           {r: None for r in ROLES}, {r: None for r in ROLES})
        if kind == "bus" and routes is not None:
            cand_levels = {role: _end_cands(db, kind, name, anchor, radius_m[kind])
                           for role, name, anchor, *_ in ends}
            anchors = {role: anchor for role, _, anchor, *_ in ends}
            rids, allow, rivals, filters, rescues = _step_routes(routes, names, cand_levels, len(stops), db=db,
                                                                 anchors=anchors, radius_m=radius_m[kind])
        res = {}
        for role, name, anchor, stop_name, guide_name in ends:
            e = resolve_endpoint(db, kind, role, name, anchor, names, radius_m[kind], ambig_gap_m[kind],
                                 allow=allow[role], line_filter=filters[role], rescue=rescues[role],
                                 rivals=rivals[role])
            e["name_conflict"] = bool(stop_name and guide_name
                                      and name_key(stop_name, kind) != name_key(guide_name, kind))
            res[role] = e
            entries.append(e)
        if kind == "bus" and rids and _unlinked(routes, rids, res):
            for e in res.values():
                e["status"], e["line_filter"] = "ambiguous", "unlinked"
        step["resolution"] = res
        step["route_ids"] = list(rids)   # 가려낸 버스 노선 (대기시간 추정이 배차를 찾는 키 — wait.py)

        groups = {db.kakao_group(n) for n in names} - {None} if kind == "subway" else set()
        # 중간 정류소도 가린 노선이 서는 쪽을 먼저 본다 (노선을 못 가렸으면 None — 이름·거리만으로 고른다)
        route_keys = {k for rid in rids for k in routes.order_of(rid)} if rids else None
        chosen_ids = tuple((res[role]["chosen"] or {}).get("id") for role in ROLES)
        key = (kind, tuple(stops), tuple(map(tuple, path)), frozenset(groups), rids, chosen_ids)
        locs = memo.get(key) if memo is not None else None
        if locs is None:
            window = None
            if len(stops) >= 2 and all(chosen_ids):
                if kind == "bus" and rids:
                    window = _route_window(db, routes, rids, stops, *chosen_ids)
                elif kind == "subway" and lines is not None:
                    window = _line_window(db, lines, step["line_group"], stops, *chosen_ids)
            locs = window or locate_stops(db, step, kind, groups, STOP_CORRIDOR_M[kind], route_keys)
            if memo is not None:
                memo[key] = locs
        step["stop_locs"] = [dict(x) if x else None for x in locs]
    return entries


def _quantile(xs, q):
    """정렬된 xs 의 선형 보간 분위수 (numpy 기본 방식)."""
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo), 1)


def _stats(entries):
    c = Counter(e["status"] for e in entries)
    base = len(entries) - c["skipped"]
    found = c["matched"] + c["ambiguous"]
    errs = sorted(e["error_m"] for e in entries
                  if e["status"] in ("matched", "ambiguous") and e["error_m"] is not None)
    return {
        "n": len(entries), "matched": c["matched"], "ambiguous": c["ambiguous"],
        "unmatched": c["unmatched"], "skipped": c["skipped"],
        "match_rate": c["matched"] / base if base else None,
        "found_rate": found / base if base else None,
        "error_m": {"median": _quantile(errs, 0.5), "p90": _quantile(errs, 0.9), "max": errs[-1]} if errs else None,
        "levels": {lv: sum(e["match_level"] == lv for e in entries) for lv in LEVELS},
    }


def summarize(entries, unmapped_vehicle_names=()):
    out = _stats(entries)
    out["by_kind"] = {k: _stats([e for e in entries if e["kind"] == k]) for k in STEP_KINDS.values()}
    out["unmapped_vehicle_names"] = sorted({n for n in unmapped_vehicle_names if n})
    return out
