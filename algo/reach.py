"""U·V 를 한 탐색으로 — 정확한 시간을 아는 시작 표지에서 정적 노선표의 대중교통을 더 타 닿는 정류장(RAPTOR 류). 카카오를 부르지 않는다.

시작 표지(출발지 도보 · 카카오 기준 경로 위 지점 · 택시로 간 정류장 …)는 호출자(`search.propose`)가 준다. 여기서는 노선을
최대 `max_rides` 번 더 탄다 — 정방향은 그때까지 걸린 시간, 역방향(`backward`)은 목적지까지 남은 시간이다.

차내 = 버스는 정류장 칸마다 정차 고정분 + 거리 ÷ 주행속도(지역 · 노선 부류별, `anchors.bus_hop_s`), 도시철도는 이웃한 역 사이
시각표 운행 시간(없는 칸만 거리 ÷ 노선군 표정속도)이고, 급행은 시각표의 정차 패턴을 노선 하나로 탄다(`lines.express`).
대기 = 배차의 절반(모르면 기본 대기, 버스와 급행은 타고 내리는 두 정류장을 함께 지나는 노선 · 급행의 빈도를 더한다),
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

    def __init__(self, stops, routes, lines, exclude=()):
        """exclude: 더 뺄 노선 유형 — 공간 분석은 통합요금 밖 노선(공항 · 시외 · 관광)을 뺀다(analysis/grid_cbd.py)."""
        self.stops = stops
        self.lines = lines
        self.patterns = []   # (종류, 노선·덩어리 id, 노선 정보, 정류장들, 누적 차내 초)
        self.at = {}         # 정류장 → [(패턴 번호, 위치)]
        self.pos = {}        # 버스 정류장 · 급행 역 → {패턴 번호: 처음 서는 위치} — 두 정류장을 차례로 지나는 노선 찾기
        self.express_hours = {}   # 급행 패턴 번호 → 위치마다 {시: 출발 횟수}
        self.coord = {}      # 정류장 → (lon, lat, 이름)
        for rid, seq in routes._seq.items():
            brief = routes._brief(rid)
            if brief["type"] in anchors.EXCLUDED_ROUTE_TYPES or brief["type"] in exclude or len(seq) < 2:
                continue
            nodes = tuple(("bus", k) for k in seq)
            for k in seq:
                name, lat, lon = routes.stop(k)
                self.coord[("bus", k)] = (lon, lat, name)

            def hop(a, b, dist, rtype=brief["type"]):
                row = stops.by_id("bus", a[1]) or {}
                return anchors.bus_hop_s(dist, row.get("sido_nm"), rtype, row.get("sgg_nm"))
            self._add("bus", rid, brief, nodes, hop)
        group_of = {cid: g for g, cids in lines._by_group.items() for cid in cids}
        for cid in lines.chains_for(None):
            seq = lines.sequence(cid)
            if len(seq) < 2:
                continue
            for sid in seq:
                name, lat, lon = lines.station(sid)
                self.coord[("subway", sid)] = (lon, lat, name)
            group = group_of.get(cid)
            brief = {"id": cid, "name": group, "type": "지하철", "line_group": group}
            kmh = anchors.RAIL_KMH_BY_GROUP.get(group, anchors.RAIL_KMH_DEFAULT)

            def hop(a, b, dist, group=group, kmh=kmh):
                # 시각표의 실제 운행 시간, 없으면 거리 ÷ 노선군 표정속도(method.md E39)
                run = lines.run_s(group, a[1], b[1])
                return run if run is not None else dist / 1000.0 / kmh * 3600.0
            if lines.is_loop(cid):   # 순환선은 이음매를 넘어 한 바퀴 안에서 탈 수 있게 한 바퀴를 더 잇는다
                seq = seq + seq[:-1]
            for nodes in (tuple(("subway", s) for s in seq), tuple(("subway", s) for s in reversed(seq))):
                self._add("subway", cid, brief, nodes, hop)
        for pid, (group, sids, cum, hours) in getattr(lines, "express", {}).items():   # 급행 — 시각표의 누적 운행 시간 그대로
            nodes = tuple(("subway", s) for s in sids)
            if len(nodes) >= 2 and all(n in self.coord for n in nodes):
                brief = {"id": pid, "name": group, "type": "지하철", "line_group": group, "express": True}
                self.express_hours[self._add("subway", pid, brief, nodes, None, list(cum))] = hours
        self._near = {}

    def _add(self, kind, rid, brief, nodes, hop, cum=None):
        """hop(앞 정류장, 뒤 정류장, 칸 거리 m) → 그 칸의 차내 초. 누적 차내 초(cum)를 주면 그대로 쓴다. → 패턴 번호."""
        if cum is None:
            cum, total = [0.0], 0.0
            for a, b in zip(nodes, nodes[1:]):
                (x1, y1, _), (x2, y2, _) = self.coord[a], self.coord[b]
                total += hop(a, b, haversine_m(x1, y1, x2, y2))
                cum.append(total)
        p = len(self.patterns)
        self.patterns.append((kind, rid, brief, nodes, cum))
        for i, n in enumerate(nodes):
            self.at.setdefault(n, []).append((p, i))
            if kind == "bus" or brief.get("express"):
                self.pos.setdefault(n, {}).setdefault(p, i)
        return p

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


def network(stops, routes, lines, exclude=()):
    key = (id(stops), id(routes), id(lines), frozenset(exclude))
    if key not in _CACHE:
        _CACHE.clear()
        _CACHE[key] = Network(stops, routes, lines, exclude)
    return _CACHE[key]


class _Waits:
    """패턴·정류장의 대기(초)와 운행 여부 — 요청 시각마다 다르니 요청마다 새로 만든다."""

    def __init__(self, net, headway, hours, at):
        self.net, self.headway, self.hours, self.at = net, headway, hours, at
        self.day = day_type(at) if at is not None else WEEKDAY
        self.hour = rail_hour(at) if at is not None else None
        self._bus, self._pair, self._rail, self._on = {}, {}, {}, {}
        self._unknown = set()   # 배차를 못 찾아 기본 대기를 쓴 (두 정류장) · (역, 노선군)

    def running(self, p):
        if p not in self._on:
            kind, rid, brief, nodes, _ = self.net.patterns[p]
            if brief.get("express"):   # 그 시간대에 한 역에서라도 출발하는 급행이 있어야 한다
                self._on[p] = any(self._express_trips(p, i) for i in range(len(nodes) - 1))
            else:
                self._on[p] = kind != "bus" or anchors.in_service(self.hours, rid, self.day, self.at)
        return self._on[p]

    def _express_trips(self, p, i):
        """급행 패턴 p 의 위치 i 에서 앞뒤 1시간을 합친 3시간 창의 출발 횟수 — 도시철도 배차표와 같은 창(배차 = 180분 ÷ 횟수)."""
        if self.hour is None:
            return 0
        c = self.net.express_hours[p][i]
        return c.get(self.hour - 1, 0) + c.get(self.hour, 0) + c.get(self.hour + 1, 0)

    def _bus_headway(self, rid):
        """버스 노선의 배차(분) — 모르면 None."""
        if rid not in self._bus:
            self._bus[rid] = _route_headway(self.headway, rid, self.day) if self.headway is not None else None
        return self._bus[rid]

    def wait(self, p, node):
        """패턴 p 하나만 기다릴 때의 대기(초) — 내릴 곳을 모르는 곳(탈 정류장 고르기)에서 쓴다."""
        kind, rid, brief, *_ = self.net.patterns[p]
        if kind == "bus":
            h = self._bus_headway(rid)
            return h * 30.0 if h is not None else anchors.WAIT_UNKNOWN_S
        if brief.get("express"):
            n = self._express_trips(p, self.net.pos[node][p])
            return 180.0 / n * 30.0 if n else inf
        k = (node[1], brief["line_group"])
        if k not in self._rail:
            h = None
            if self.headway is not None and self.hour is not None:
                # 배차가 같은 역의 다른 운영사 행에만 붙어 있으면 그 값을 쓴다(청량리 1호선 · 지축 3호선)
                for sid in (node[1], *self.net.lines.same_station(node[1])):
                    h = self.headway.rail(sid, brief["line_group"], self.hour)
                    if h is not None:
                        break
            self._rail[k] = h * 30.0 if h is not None else anchors.WAIT_UNKNOWN_S
            if h is None:
                self._unknown.add(k)
        return self._rail[k]

    def ride_wait(self, p, i, j):
        """패턴 p 를 위치 i 에서 타 j 에서 내릴 때의 대기(초). 버스는 두 정류장을 이 차례로 지나는 운행 중 노선 중 **먼저 오면 탈 노선**
        (매력 노선 — 차내가 짧은 노선부터, 그 노선의 차내가 지금까지 묶은 노선의 기대 대기 + 기대 차내보다 짧을 때만 더한다)의
        빈도를 더한다(method.md §7.5, E37). 크게 돌아가는 노선은 먼저 와도 타지 않는다. 배차를 모르는 노선은 빼고, 아는 노선이
        없으면 기본 대기다. 도시철도 배차는 이미 역의 '아무 열차나' 값이라 그대로 쓴다."""
        kind, _, brief, nodes, _ = self.net.patterns[p]
        a, b = nodes[min(i, j)], nodes[max(i, j)]
        if brief.get("express"):   # 같은 노선군의 급행 중 두 역에 차례로 서는 것의 빈도 합
            key = (a, b, brief["line_group"])
            if key not in self._pair:
                later, freq = self.net.pos.get(b, {}), 0.0
                for q, qa in self.net.pos[a].items():
                    qb = later.get(q)
                    if (qb is not None and qb > qa and self.net.patterns[q][2].get("line_group") == brief["line_group"]
                            and self.net.patterns[q][2].get("express")):
                        freq += self._express_trips(q, qa) / 180.0   # 분당 빈도
                self._pair[key] = 30.0 / freq if freq > 0 else inf
            return self._pair[key]
        if kind != "bus":
            return self.wait(p, a)
        if (a, b) not in self._pair:
            later, lines = self.net.pos.get(b, {}), []
            for q, qa in self.net.pos[a].items():
                qb = later.get(q)
                if qb is not None and qb > qa and self.running(q):
                    h = self._bus_headway(self.net.patterns[q][1])
                    if h is not None and h > 0:
                        cum = self.net.patterns[q][4]
                        lines.append((cum[qb] - cum[qa], 1.0 / (h * 60.0)))   # (차내 초, 초당 빈도)
            freq = ride = 0.0
            for r, f in sorted(lines):
                if freq and r >= (0.5 + ride) / freq:   # 기대 대기(0.5 / 빈도) + 기대 차내(Σ 빈도 × 차내 / 빈도)
                    break
                freq, ride = freq + f, ride + f * r
            self._pair[(a, b)] = 0.5 / freq if freq > 0 else anchors.WAIT_UNKNOWN_S
            if freq <= 0:
                self._unknown.add((a, b))
        return self._pair[(a, b)]

    def known(self, p, i, j):
        """그 승차의 대기가 배차에서 나왔나 — 못 찾아 기본 대기를 썼으면 False. 값(900초)으로 가르면 배차 30분 노선과 섞인다."""
        kind, _, brief, nodes, _ = self.net.patterns[p]
        self.ride_wait(p, i, j)
        a, b = nodes[min(i, j)], nodes[max(i, j)]
        if brief.get("express"):
            return True
        return ((a, b) if kind == "bus" else (a[1], brief["line_group"])) not in self._unknown


def search(net, waits, access, *, limit_s, max_rides, backward=False, rest=None, on_round=None):
    """시작 표지 {정류장: 초} → (표지 {정류장: 초}, 부모 {정류장: …}). backward 면 '목적지까지 남은 시간' 을 거꾸로 편다.

    부모: ("ride", 패턴, 탄 위치, 내린 위치, 회차, 탄 정류장의 부모) · ("walk", 앞 정류장, 거리, 앞 정류장의 부모) · ("access",)
    부모는 그 표지를 만든 앞 부모를 직접 가리킨다 — 뒤 회차에 앞 정류장의 표지가 바뀌어도 이미 만든 경로는 그대로다.
    `rest(정류장)` 을 주면 그 정류장에서 나머지 길(정방향은 목적지까지, 역방향은 출발지에서)의 하한을 더해 `limit_s` 와 견준다.
    `on_round(k, tau, par)` 를 주면 회차 k 가 끝날 때마다 부른다 — 그때의 표지는 'k 번까지 타고' 갈 수 있는 최소 시간이다.
    더 나아질 정류장이 없어 일찍 끝나면 남은 회차는 부르지 않는다(그 뒤 회차의 값은 마지막 값과 같다).
    """
    rest = rest or (lambda node: 0.0)
    tau = dict(access)
    par = {n: ("access",) for n in access}
    marked = set(access)
    for k in range(1, max_rides + 1):
        snap = {n: tau[n] for n in marked}
        snap_par = {n: par[n] for n in marked}
        start = {}
        for n in marked:
            for p, i in net.at.get(n, ()):
                if waits.running(p):
                    start[p] = (min if not backward else max)(start.get(p, i), i)
        improved = set()
        for p, s in start.items():
            kind, nodes, cum = net.patterns[p][0], net.patterns[p][3], net.patterns[p][4]
            best, base, held, held_par = inf, inf, None, None
            idx = range(s, len(nodes)) if not backward else range(s, -1, -1)
            for i in idx:
                n = nodes[i]
                if held is not None:
                    # 정방향: held 에서 타서 i 에 내림. 역방향: i 에서 타서 held 에 내림. 대기는 타고 내리는 두 정류장으로 정한다
                    t = base + cum[i] if not backward else base - cum[i]
                    if t < tau.get(n, inf):   # 대기 없이도 못 줄이면 대기를 셀 필요가 없다
                        t += waits.ride_wait(p, held, i) + (anchors.RAIL_STATION_S if kind == "subway" else 0.0)
                        if t < tau.get(n, inf) and t + rest(n) <= limit_s:
                            tau[n] = t
                            par[n] = (("ride", p, held, i, k, held_par) if not backward
                                      else ("ride", p, i, held, k, held_par))
                            improved.add(n)
                if n in snap:
                    # 탈 정류장은 노선 하나의 대기로 고른다 — 내릴 곳을 아직 모른다
                    cand = snap[n] + waits.wait(p, n) - cum[i] if not backward else snap[n] + cum[i]
                    if cand < best:
                        best, held, held_par = cand, i, snap_par[n]
                        base = snap[n] - cum[i] if not backward else snap[n] + cum[i]
        marked = set(improved)
        rode = {n: tau[n] for n in improved}
        for n, t_n in rode.items():   # 내린 정류장에서 걸어서 옮겨 타기 — 내린 시각에서 한 번만(걸은 표지에서 다시 걷지 않는다)
            for m, dist in net.neighbors(n):
                t = t_n + anchors.walk_s(dist)
                if t < tau.get(m, inf) and t + rest(m) <= limit_s:
                    tau[m] = t
                    par[m] = ("walk", n, dist, par[n])
                    marked.add(m)
        if on_round:
            on_round(k, tau, par)
        if not marked:
            break
    return tau, par


def legs(net, par, node, backward=False):
    """표지의 부모를 따라 → (시작 정류장, [("ride", 패턴, 탄 위치, 내린 위치) | ("walk", 거리)…]) — 이동 순서대로."""
    out, n, e = [], node, par[node]
    while e[0] != "access":
        if e[0] == "ride":
            _, p, i, j, _, e = e
            out.append(("ride", p, i, j))
            n = net.patterns[p][3][i] if not backward else net.patterns[p][3][j]
        else:
            _, n, dist, e = e
            out.append(("walk", dist))
    return n, (out[::-1] if not backward else out)
