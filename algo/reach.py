"""U·V 를 한 탐색으로 — 정확한 시간을 아는 시작 표지에서 정적 노선표의 대중교통을 더 타 닿는 정류장(RAPTOR 류). 카카오를 부르지 않는다.

시작 표지(출발지 도보 · 카카오 기준 경로 위 지점 · 택시로 간 정류장 …)는 호출자(`search.propose`)가 준다. 여기서는 노선을
최대 `max_rides` 번 더 탄다 — 정방향은 그때까지 걸린 시간, 역방향(`backward`)은 목적지까지 남은 시간이다.

차내 = 순서표 거리 ÷ 유형별 표정속도(버스 유형별, 도시철도 노선군별), 대기 = 배차의 절반(모르면 기본 대기),
갈아탈 때는 `TRANSFER_M` 안 정류장으로 걷는다. 한 정류장의 표지는 시간 하나다(요금은 경로를 다시 짜 낸다).
기준선보다 5분 빨라질 수 없는 시각을 넘는 표지는 펼치지 않는다(`limit_s`).
"""
from math import inf

from geoutil import haversine_m

from app.stopsdb import ID_COL
from app.wait import WEEKDAY, _route_headway, day_type, rail_hour
from algo import anchors

TRANSFER_M = 300.0          # 갈아탈 때 걸어서 옮겨 갈 수 있는 정류장 거리


class Network:
    """노선 패턴(정류장 순서와 누적 차내 시간)과 정류장 → 패턴 색인. 정적 표가 같으면 한 번만 만든다."""

    def __init__(self, stops, routes, lines):
        self.stops = stops
        self.patterns = []   # (종류, 노선·덩어리 id, 노선 정보, 정류장들, 누적 차내 초)
        self.at = {}         # 정류장 → [(패턴 번호, 위치)]
        self.coord = {}      # 정류장 → (lon, lat, 이름)
        for rid, seq in routes._seq.items():
            brief = routes._brief(rid)
            if brief["type"] in anchors.EXCLUDED_ROUTE_TYPES or len(seq) < 2:
                continue
            kmh = anchors.BUS_KMH.get(brief["type"] or "", anchors.BUS_KMH_DEFAULT)
            nodes = tuple(("bus", k) for k in seq)
            for k in seq:
                name, lat, lon = routes.stop(k)
                self.coord[("bus", k)] = (lon, lat, name)
            self._add("bus", rid, brief, nodes, kmh)
        group_of = {cid: g for g, cids in lines._by_group.items() for cid in cids}
        for cid in lines.chains_for(None):
            seq = lines.sequence(cid)
            if len(seq) < 2:
                continue
            for sid in seq:
                name, lat, lon = lines.station(sid)
                self.coord[("subway", sid)] = (lon, lat, name)
            brief = {"id": cid, "name": group_of.get(cid), "type": "지하철", "line_group": group_of.get(cid)}
            kmh = anchors.RAIL_KMH_BY_GROUP.get(group_of.get(cid), anchors.RAIL_KMH_DEFAULT)
            for nodes in (tuple(("subway", s) for s in seq), tuple(("subway", s) for s in reversed(seq))):
                self._add("subway", cid, brief, nodes, kmh)
        self._near = {}

    def _add(self, kind, rid, brief, nodes, kmh):
        cum, total = [0.0], 0.0
        for a, b in zip(nodes, nodes[1:]):
            (x1, y1, _), (x2, y2, _) = self.coord[a], self.coord[b]
            total += haversine_m(x1, y1, x2, y2) / 1000.0 / kmh * 3600.0
            cum.append(total)
        p = len(self.patterns)
        self.patterns.append((kind, rid, brief, nodes, cum))
        for i, n in enumerate(nodes):
            self.at.setdefault(n, []).append((p, i))

    def neighbors(self, node):
        """걸어서 갈아탈 수 있는 다른 정류장 → [(정류장, 거리 m)]. 노선이 서는 정류장만."""
        if node not in self._near:
            lon, lat, _ = self.coord[node]
            out = []
            for kind in ("bus", "subway"):
                for i, dist in self.stops.near(kind, lon, lat, TRANSFER_M):
                    other = (kind, self.stops.row(kind, i)[ID_COL[kind]])
                    if other != node and other in self.at:
                        out.append((other, dist))
            self._near[node] = out
        return self._near[node]


_CACHE = {}


def network(stops, routes, lines):
    key = (id(stops), id(routes), id(lines))
    if key not in _CACHE:
        _CACHE.clear()
        _CACHE[key] = Network(stops, routes, lines)
    return _CACHE[key]


class _Waits:
    """패턴·정류장의 대기(초)와 운행 여부 — 요청 시각마다 다르니 요청마다 새로 만든다."""

    def __init__(self, net, headway, hours, at):
        self.net, self.headway, self.hours, self.at = net, headway, hours, at
        self.day = day_type(at) if at is not None else WEEKDAY
        self.hour = rail_hour(at) if at is not None else None
        self._bus, self._rail, self._on = {}, {}, {}

    def running(self, p):
        if p not in self._on:
            kind, rid, *_ = self.net.patterns[p]
            self._on[p] = kind != "bus" or anchors.in_service(self.hours, rid, self.day, self.at)
        return self._on[p]

    def wait(self, p, node):
        kind, rid, brief, *_ = self.net.patterns[p]
        if kind == "bus":
            if rid not in self._bus:
                h = _route_headway(self.headway, rid, self.day) if self.headway is not None else None
                self._bus[rid] = h * 30.0 if h is not None else anchors.WAIT_UNKNOWN_S
            return self._bus[rid]
        k = (node[1], brief["line_group"])
        if k not in self._rail:
            h = (self.headway.rail(node[1], brief["line_group"], self.hour)
                 if self.headway is not None and self.hour is not None else None)
            self._rail[k] = h * 30.0 if h is not None else anchors.WAIT_UNKNOWN_S
        return self._rail[k]


def search(net, waits, access, *, limit_s, max_rides, backward=False, rest=None):
    """시작 표지 {정류장: 초} → (표지 {정류장: 초}, 부모 {정류장: …}). backward 면 '목적지까지 남은 시간' 을 거꾸로 편다.

    부모: ("ride", 패턴, 탄 위치, 내린 위치, 회차) · ("walk", 앞 정류장, 거리) · ("access",)
    `rest(정류장)` 을 주면 그 정류장에서 나머지 길(정방향은 목적지까지, 역방향은 출발지에서)의 하한을 더해 `limit_s` 와 견준다.
    """
    rest = rest or (lambda node: 0.0)
    tau = dict(access)
    par = {n: ("access",) for n in access}
    marked = set(access)
    for k in range(1, max_rides + 1):
        snap = {n: tau[n] for n in marked}
        start = {}
        for n in marked:
            for p, i in net.at.get(n, ()):
                if waits.running(p):
                    start[p] = (min if not backward else max)(start.get(p, i), i)
        improved = set()
        for p, s in start.items():
            nodes, cum = net.patterns[p][3], net.patterns[p][4]
            best, held = inf, None
            idx = range(s, len(nodes)) if not backward else range(s, -1, -1)
            for i in idx:
                n = nodes[i]
                if held is not None:
                    # 정방향: held 에서 타서 i 에 내림. 역방향: i 에서 타서 held 에 내림(대기는 타는 i 에서)
                    t = best + cum[i] if not backward else waits.wait(p, n) + best - cum[i]
                    if t < tau.get(n, inf) and t + rest(n) <= limit_s:
                        tau[n] = t
                        par[n] = ("ride", p, held, i, k) if not backward else ("ride", p, i, held, k)
                        improved.add(n)
                if n in snap:
                    cand = snap[n] + waits.wait(p, n) - cum[i] if not backward else snap[n] + cum[i]
                    if cand < best:
                        best, held = cand, i
        marked = set(improved)
        rode = {n: tau[n] for n in improved}
        for n, t_n in rode.items():   # 내린 정류장에서 걸어서 옮겨 타기 — 내린 시각에서 한 번만(걸은 표지에서 다시 걷지 않는다)
            for m, dist in net.neighbors(n):
                t = t_n + anchors.walk_s(dist)
                if t < tau.get(m, inf) and t + rest(m) <= limit_s:
                    tau[m] = t
                    par[m] = ("walk", n, dist)
                    marked.add(m)
        if not marked:
            break
    return tau, par


def legs(net, par, node, backward=False):
    """표지의 부모를 따라 → (시작 정류장, [("ride", 패턴, 탄 위치, 내린 위치) | ("walk", 거리)…]) — 이동 순서대로."""
    out, n = [], node
    while par[n][0] != "access":
        e = par[n]
        if e[0] == "ride":
            _, p, i, j, _ = e
            out.append(("ride", p, i, j))
            n = net.patterns[p][3][i] if not backward else net.patterns[p][3][j]
        else:
            out.append(("walk", e[2]))
            n = e[1]
    return n, (out[::-1] if not backward else out)
