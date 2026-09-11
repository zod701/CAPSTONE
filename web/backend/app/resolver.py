"""카카오 대중교통 구간의 승·하차 정류소 이름을 DB 행에 매칭한다 (1차: 진단용).

기준점은 구간 path 의 시작점(승차)·끝점(하차)이고, 오차 = 선택된 DB 좌표 ↔ 기준점 거리.
이름 수준은 key > alias > prefix 순으로, 가장 높은 수준에서 가장 가까운 후보를 고른다.
지도에 이름을 띄우려고 중간 정류소까지 포함한 stops[] 전부의 위치도 붙인다(locate_stops).
"""
import math
from collections import Counter

from geoutil import EARTH_R, haversine_m, local_xy
from textnorm import name_key

from .stopsdb import HEAD_LEN

LEVELS = ("key", "alias", "prefix")
STEP_KINDS = {"BUS": "bus", "SUBWAY": "subway"}
PREFIX_MIN_LEN = HEAD_LEN  # 절단된 이름 대응(4). 짧은 키끼리의 접두 일치는 우연이 많다
SAME_STATION_M = 300.0   # 같은 이름의 지하철 행이 이 안에 있으면 같은 역(환승 노선 행)
NEAREST_ANY_M = 200.0
# 중간 정류소 후보: 구간 선에서 이 거리 안. 버스 정류소는 도로 중심선에서 수십 m, 역 좌표는 선로에서 수백 m 까지 떨어진다
STOP_CORRIDOR_M = {"bus": 100.0, "subway": 500.0}
ORDER_SLACK_M = 50.0     # 정류소 순서 판정 여유 — 길 건너편 정류소·역 좌표의 투영 차이만큼은 뒤로 가도 된다


def _level(qk, row):
    k = row["name_key"]
    if qk == k:
        return "key"
    if qk in row["alias_keys"]:
        return "alias"
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


def _unmatched_extras(db, e, kind, qk, lon, lat):
    hits = db.near(kind, lon, lat, NEAREST_ANY_M)
    if hits:
        i, d = hits[0]
        row = db.row(kind, i)
        e["nearest_any"] = {"id": _row_id(kind, row), "name": row["name"], "dist_m": round(d, 1)}
    same = [db.row(kind, i) for i in db.by_key(kind, qk)] if qk else []
    if same:
        e["nearest_same_name_m"] = round(min(haversine_m(lon, lat, r["lon"], r["lat"]) for r in same), 1)


def resolve_endpoint(db, kind, role, name, anchor, vehicle_names, radius_m, ambig_gap_m):
    """승차 또는 하차 한 곳을 매칭해 진단 항목을 돌려준다."""
    qk = name_key(name or "", kind)
    groups = set()
    line_filter = "n/a"
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
    }
    if anchor is None:
        if qk:  # 이름이 비었으면 skipped 가 아니라 unmatched
            e["status"] = "skipped"
        return e
    lon, lat = anchor[0], anchor[1]

    cands = []  # (수준 순위, 거리, 행) — near() 가 거리순이므로 이 목록도 거리순
    if qk:
        for i, d in db.near(kind, lon, lat, radius_m):
            row = db.row(kind, i)
            lv = _level(qk, row)
            if lv:
                cands.append((LEVELS.index(lv), d, row))
    if groups:
        same_line = [c for c in cands if c[2]["line_group"] in groups]
        if same_line:
            cands = same_line
        elif cands:  # 노선이 맞는 후보가 없어 필터 없이 고른다
            e["line_filter"] = "mismatch"
    e["n_candidates"] = len(cands)
    if not cands:
        _unmatched_extras(db, e, kind, qk, lon, lat)
        return e

    best = min(c[0] for c in cands)
    top = [c for c in cands if c[0] == best]
    _, d0, chosen = top[0]
    rivals = [d for _, d, r in top if not _same_entity(kind, chosen, r)]
    gap = rivals[0] - d0 if rivals else None
    e.update(
        status="ambiguous" if gap is not None and gap < ambig_gap_m else "matched",
        match_level=LEVELS[best],
        chosen={"id": _row_id(kind, chosen), "name": chosen["name"], "lat": chosen["lat"],
                "lon": chosen["lon"], "sgg_nm": chosen["sgg_nm"],
                "line_group": chosen["line_group"] if kind == "subway" else None},
        error_m=round(d0, 1),
        second_gap_m=None if gap is None else round(gap, 1),
    )
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


def _stop_options(db, kind, k, name, groups, frame, box, corridor_m):
    """k 번째 정류소 이름이 맞는 DB 행이 구간 선 곁을 지나는 곳마다 후보 (k, 점수, 선 위 거리, 위치 항목).
    점수: 같은 노선(지하철) > 이름 수준(key > alias > prefix) > 선까지 거리 — 어느 후보든 0 보다 커서 하나라도 더 맞히는 조합이 이긴다."""
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
        lv = _level(qk, row)
        if lv is None or not (box[0] <= row["lon"] <= box[2] and box[1] <= row["lat"] <= box[3]):
            continue
        mismatch = bool(groups) and row["line_group"] not in groups
        loc = {"lon": row["lon"], "lat": row["lat"], "id": _row_id(kind, row), "level": lv}
        for d, s in _passes(xy, cum, *local_xy(row["lon"], row["lat"], lon0, lat0), corridor_m):
            out.append((k, 1000 - 300 * mismatch - 100 * LEVELS.index(lv) - 100 * d / corridor_m, s, loc))
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


def locate_stops(db, step, kind, groups, corridor_m):
    """구간 stops[] 전부의 지도 위치 (이름 표시용) — stops 와 같은 길이, 원소는 {lon, lat, id, level} 또는 None.

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
    options = [o for k in range(1, n - 1) for o in _stop_options(db, kind, k, stops[k], groups, frame, box, corridor_m)]

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


def resolve_route(db, route, radius_m, ambig_gap_m, memo=None):
    """경로의 BUS·SUBWAY 구간마다 승·하차를 매칭해 step 에 붙이고, 진단 항목 목록을 돌려준다.
    step["stop_locs"] 에 정류소 전부의 위치도 붙인다. memo(dict)를 넘기면 경로 사이에 똑같은 구간의 위치를 한 번만 계산한다."""
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
        res = {}
        for role, name, anchor, stop_name, guide_name in ends:
            e = resolve_endpoint(db, kind, role, name, anchor, names, radius_m[kind], ambig_gap_m[kind])
            e["name_conflict"] = bool(stop_name and guide_name
                                      and name_key(stop_name, kind) != name_key(guide_name, kind))
            res[role] = e
            entries.append(e)
        step["resolution"] = res

        groups = {db.kakao_group(n) for n in names} - {None} if kind == "subway" else set()
        key = (kind, tuple(stops), tuple(map(tuple, path)), frozenset(groups),
               tuple((e["chosen"] or {}).get("id") for e in res.values()))
        locs = memo.get(key) if memo is not None else None
        if locs is None:
            locs = locate_stops(db, step, kind, groups, STOP_CORRIDOR_M[kind])
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
