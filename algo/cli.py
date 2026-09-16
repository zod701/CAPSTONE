r"""앵커 선택 실험 진입점 — plan(후보 생성·검증) / calibrate(택시 근사 보정).

카카오 응답은 저장하지 않는다. 화면에 남기는 것은 **파생 수치**(시간·비용·원/분)뿐이다 → method.md §2.2.

카카오는 **데모 웹 백엔드를 경유해서** 부른다. `QuotaGuard` 의 잠금은 프로세스 안에서만 유효해서
(web/README.md §3) CLI 가 직접 부르면 서버와 같은 `quota.json` 을 동시에 써 카운트가 덮어써진다.
백엔드를 지나가면 쿼터가 한 프로세스에서만 관리되고, 덤으로 접지(`resolve_route`)와 대기(`add_wait`)까지
끝난 경로를 받는다. 그래서 실험 중에도 데모 웹을 띄워 둔 채로 쓸 수 있다.

    .\.venv\Scripts\python.exe -m algo.cli plan --od dongtan --offline
    .\.venv\Scripts\python.exe -m algo.cli plan --od suji --top 5
    .\.venv\Scripts\python.exe -m algo.cli calibrate --samples 150
"""
import argparse
import asyncio
import random
from datetime import datetime, timedelta

import httpx

import algo  # noqa: F401  — sys.path 훅 (web/backend, data/scripts)
from app.config import KST, load_settings
from app.headwaydb import HeadwayDB
from app.linesdb import LinesDB
from app.livestationsdb import LiveStationsDB
from app.routesdb import RoutesDB
from app.stopsdb import StopsDB

from algo import anchors, compare

# 접지 실측(E7·E10·E13)과 같은 공통 표본이다 — 여기서 나온 수치가 그 실험들과 곧바로 이어진다
OD_PRESETS = {
    "dongtan": ("동탄역 → 경기도청(광교)", (127.09638, 37.20009), (127.05627, 37.28773)),
    "suji": ("수지구청 → 서현역", (127.09558, 37.32301), (127.12325, 37.38493)),
    "unjeong": ("운정역 → 대화역", (126.76708, 37.72544), (126.74776, 37.67588)),
    "dasan": ("다산역 → 구리시청", (127.14980, 37.62437), (127.13125, 37.59497)),
    "baegot": ("배곧신도시 → 안산 중앙역", (126.72900, 37.37300), (126.83853, 37.31605)),
}
DEFAULT_API = "http://127.0.0.1:8000"


class Backend:
    """데모 웹 백엔드의 얇은 클라이언트 — 카카오를 직접 부르지 않는다."""

    def __init__(self, http, base):
        self.http, self.base = http, base.rstrip("/")

    async def _get(self, path, **params):
        r = await self.http.get(self.base + path, params=params)
        if r.status_code != 200:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            raise RuntimeError("%s %s: %s" % (path, r.status_code, body.get("message") or r.text[:120]))
        return r.json()

    async def transit(self, a, b):
        """접지·대기까지 끝난 대중교통 경로 (`/api/transit`)."""
        return await self._get("/api/transit", sx=a[0], sy=a[1], ex=b[0], ey=b[1])

    async def car(self, a, b):
        """자동차 경로 — 시간·거리·택시 예상요금·통행료 (`/api/car`)."""
        return await self._get("/api/car", sx=a[0], sy=a[1], ex=b[0], ey=b[1])

    async def quota(self):
        return await self._get("/api/quota")


def load_dbs(st):
    """정적 표를 한 번에 올린다 — 1초쯤 걸리고 그 뒤는 전부 메모리에서 돈다."""
    return {"stops": StopsDB.load(st.processed_dir, st.ref_dir),
            "routes": RoutesDB.load(st.processed_dir),
            "lines": LinesDB.load(st.processed_dir),
            "headway": HeadwayDB.load(st.processed_dir),
            "phys": anchors.load_phys(st.processed_dir),
            "hours": anchors.load_service_hours(st.processed_dir),
            "live": LiveStationsDB.load(st.processed_dir)}


def _point(text):
    """'lon,lat' → (lon, lat)."""
    lon, lat = (float(v) for v in text.split(","))
    return lon, lat


def _od(args):
    """--od 프리셋 또는 --from/--to → (제목, O, D)."""
    if args.od:
        return OD_PRESETS[args.od]
    return "%s → %s" % (args.frm, args.to), _point(args.frm), _point(args.to)


def _sgg(dbs, pt):
    """좌표가 속한 시군 — 가장 가까운 정류소의 값으로 본다 (택시 요금 유형·시계외 판정)."""
    return anchors._sgg_at(anchors._near(dbs["stops"], "bus", pt, (2000.0, 10000.0)))


def print_candidates(cands, title):
    """후보 표 — 파생 수치만 찍는다."""
    print("\n%s  (%d개)" % (title, len(cands)))
    print("  %-4s %-18s %-9s %6s %8s %6s %6s %6s %8s" %
          ("유형", "앵커", "노선", "택시분", "택시요금", "대기분", "승차분", "도보분", "점수분"))
    for c in cands:
        print("  %-4s %-18s %-9s %6.1f %8.0f %6s %6.1f %6.1f %8.1f" % (
            c["strategy"] + c["hybrid"], (c["name"] or "")[:16], str(c.get("ride_name") or "")[:9],
            c["taxi_s"] / 60, c["fare"],
            "?" if c["wait_s"] is None else "%.1f" % (c["wait_s"] / 60),
            c["ride_s"] / 60, c["walk_s"] / 60, c["score_s"] / 60))


def cmd_plan(args):
    st = load_settings()
    dbs = load_dbs(st)
    title, o, d = _od(args)
    at = datetime.now(KST).replace(tzinfo=None) if args.at is None else datetime.fromisoformat(args.at)
    vot = anchors.VOT[args.pref]
    print("%s  선호=%s(VOT %.0f원/분)  기준시각=%s" % (title, args.pref, vot, at.strftime("%m-%d %H:%M")))

    sel = []
    for hybrid in ("A", "B"):   # A = 택시로 타러 간다 · B = 걸어서 타고 내려서 택시 (§3.2 의 대칭판)
        cands, diag = anchors.propose_backtrack(
            dbs["stops"], dbs["routes"], dbs["lines"], o, d, vot=vot, hybrid=hybrid, at=at,
            headway=dbs["headway"], phys=dbs["phys"], hours=dbs["hours"], t_max_s=args.t_max * 60)
        print("(b) %s 방향:" % hybrid, diag)
        picked = anchors.top_n(cands, args.top)
        print_candidates(picked, "(b) %s 상위 %d" % (hybrid, args.top))
        sel += picked

    if args.offline:
        print("\n--offline — 쿼터를 쓰지 않았다. 온라인 검증은 이 플래그를 빼고 실행한다.")
        return
    asyncio.run(_run_plan(dbs, title, o, d, sel, vot, at, args))


async def _run_plan(dbs, title, o, d, sel, vot, at, args):
    """온라인 검증 — 기준 경로 1콜 + (a) 후보 생성 + 앵커마다 검증 콜."""
    async with httpx.AsyncClient(timeout=args.timeout) as http:
        api = Backend(http, args.api)
        try:
            before = await api.quota()
        except httpx.ConnectError:
            print("\n백엔드에 닿지 못했다 (%s). 데모 웹을 먼저 띄우거나 --api 로 주소를 지정한다." % args.api)
            return
        print("\n쿼터 잔량: transit %s / car %s" % (before["transit"]["remaining"], before["car"]["remaining"]))

        base_resp = await api.transit(o, d)
        routes = base_resp.get("routes") or []
        if not routes:
            print("기준 경로 없음:", base_resp.get("message") or base_resp.get("status"))
            return
        # 첫 승차 대기를 실시간으로 — 기준선과 D 가 같은 값을 써야 둘을 견줄 수 있다. 기준 시각을 지정했으면 '지금' 의
        # 도착정보가 그 시각과 맞지 않아 쓰지 않는다. (a) 의 B 도 걸어서 첫 정류장에 닿으므로 사본을 쓰고, A 만 원래 경로다
        rt_routes = routes
        if args.at is None and not args.no_realtime:
            before_rt = await api.quota()
            rt_routes, rt_diag = await _realtime_first_waits(api, dbs, o, routes)
            after_rt = await api.quota()
            print("첫 승차 실시간 대기:", rt_diag, "· 실시간 쿼터 %s" % {
                k: after_rt[k]["used"] - before_rt[k]["used"] for k in ("gyeonggi_bus", "seoul_bus", "seoul_subway")})
            _print_first_waits(routes, rt_routes)
        base = min((compare.reconstruct(r, o, d) for r in rt_routes),
                   key=lambda b: compare.gc_s(b["time_s"], b["fare"], vot))
        print("기준선(전 구간 대중교통): %.1f분 %s원 도보%.0fm  대안 %d개"
              % (base["time_s"] / 60, base["fare"], base["walk_m"], len(routes)))

        perturb = dict(vot=vot, sgg_o=_sgg(dbs, o), sgg_d=_sgg(dbs, d), at=at, t_max_s=args.t_max * 60)
        a_only, a_diag = anchors.propose_perturb(o, d, routes, hybrids=("A",), **perturb)        # 택시로 정류장에 닿는다
        b_only, b_diag = anchors.propose_perturb(o, d, rt_routes, hybrids=("B",), **perturb)     # 걸어서 첫 정류장에 닿는다
        a_cands = sorted(a_only + b_only, key=lambda c: c["score_s"])
        print("(a) 기준 경로 섭동: A", a_diag, "· B", b_diag)
        print_candidates(anchors.top_n(a_cands, args.top, max_per_route=args.top), "(a) 상위 %d" % args.top)
        d_cands, d_diag = anchors.propose_gap(o, d, rt_routes, vot=vot, at=at, t_max_s=args.t_max * 60)
        print("(a) D 공백 메우기:", d_diag)
        print_candidates(d_cands[:args.top], "(a) D 상위 %d" % args.top)
        c_cands, c_diag = anchors.propose_detour(
            dbs["stops"], dbs["routes"], dbs["lines"], o, d, rt_routes, vot=vot, at=at, headway=dbs["headway"],
            phys=dbs["phys"], hours=dbs["hours"], t_max_s=args.t_max * 60,
            n_points=args.c_points, n_anchors=args.c_anchors)
        print("(C) 돌아가기 시작하는 정류장에서 다시 찾기:", c_diag)
        print_candidates(c_cands, "(C) 후보")

        picks = anchors.merge_candidates(sel, a_cands, d_cands, top=args.top)
        # C 는 예산을 따로 둔다 — 정류장 수 × (택시로 목적지 1 + 앵커 수) 만큼만 만들어 전부 확인한다
        picks += anchors.merge_candidates(c_cands, top=len(c_cands)) if c_cands else []
        n_transit = sum(1 for c in picks
                        if c["hybrid"] == "A" or c["strategy"] == "b" or c.get("via") == "reanchor")
        print("\n검증할 앵커 %d개 (예상 transit %d콜 · car %d콜 — (a)의 B·D 는 나머지 구간이 기준 경로에 있어 공짜다)"
              % (len(picks), n_transit, len(picks)))
        results = await asyncio.gather(*(_verify_one(api, o, d, c, vot, at) for c in picks),
                                       return_exceptions=True)
        rows = [r for r in results if isinstance(r, dict)]
        for err in (r for r in results if not isinstance(r, dict)):
            print("  검증 실패:", err)
        _report(rows, base, vot)
        after = await api.quota()
    print("쿼터 소모: transit %d · car %d"
          % (after["transit"]["used"] - before["transit"]["used"],
             after["car"]["used"] - before["car"]["used"]))


async def _realtime_first_waits(api, dbs, o, routes):
    """경로마다 첫 승차 지점의 실시간 도착을 받아 첫 대기를 바꾼 사본 — 같은 정류장·역은 한 번만 부른다."""
    want, toward, upstream = {}, {}, {}
    for r in routes:
        i = anchors.first_ride(r["steps"])
        if i is None:
            continue
        s = r["steps"][i]
        b, a = anchors._chosen(s, "board"), anchors._chosen(s, "alight")
        if not b:
            continue
        kind = "subway" if s["type"] == "SUBWAY" else "bus"
        want[b["id"]] = kind
        if kind == "subway" and a:
            toward[(b["id"], a["id"])] = anchors.subway_toward(dbs["lines"], dbs["live"], b["id"], a["id"])
            upstream[(b["id"], a["id"])] = anchors.subway_upstream_s(dbs["lines"], b["id"], a["id"])

    async def fetch(sid, kind):
        path = ("/api/arrivals/station/%s" if kind == "subway" else "/api/arrivals/stop/%s") % sid
        try:
            return sid, (await api._get(path)).get("items") or []
        except (RuntimeError, httpx.HTTPError):   # 실시간이 없거나(503) 원천이 응답하지 않으면 정적 배차로 남긴다
            return sid, None

    got = await asyncio.gather(*(fetch(s, k) for s, k in want.items()))
    return anchors.with_realtime_first_wait(routes, o, {sid: it for sid, it in got if it is not None}, toward, upstream)


def _print_first_waits(routes, rt_routes):
    """경로별 첫 승차 대기 — 정적 배차와 실시간(도보 뺀 값)을 나란히."""
    shown = set()
    for r in rt_routes:
        i = anchors.first_ride(r["steps"])
        s = r["steps"][i] if i is not None else None
        if not s or not s.get("wait_source"):
            continue
        b = anchors._chosen(s, "board")
        key = (b["id"], s["wait_s"])
        if key in shown:
            continue
        shown.add(key)
        print("  %-18s %-10s 도보 %4.1f분 · 정적 %5s분 → 실시간 %4.1f분 (%s)" % (
            b["name"][:18], str((s.get("vehicles") or [{}])[0].get("name"))[:10], s["walk_to_stop_s"] / 60,
            "?" if s["static_wait_s"] is None else "%.1f" % (s["static_wait_s"] / 60), s["wait_s"] / 60, s["wait_source"]))


async def _verify_detour(api, d, cand, vot, at=None):
    """C 후보 확인 — 돌아가기 시작하는 정류장까지는 기준 경로 시간(`head_s`), 거기서부터 온라인으로 잰다.

    택시로 목적지까지는 자동차 1콜, 직행 노선 앵커는 자동차 + 대중교통 2콜이다. 환승이 이어지면 뒤 노선은 기본요금을 빼고
    거리 비례분만 더하고, 끊기면 뒤 노선 요금을 통째로 더한다(환승 판정은 응답 ETA 로 다시 한다).
    """
    frm, pt = (cand["from_lon"], cand["from_lat"]), (cand["lon"], cand["lat"])
    cap = cand.get("transit_fare_cap") or 0
    if cand["via"] == "taxi_to_d":
        car = await api.car(frm, d)
        time_s = cand["head_s"] + anchors.taxi_plan_s(car["duration_s"], connect=False)
        fare = (car["fare"]["taxi"] or 0) + (car["fare"]["toll"] or 0) + cap
        return {"cand": cand, "time_s": time_s, "fare": fare, "note": ""}
    car, leg = await asyncio.gather(api.car(frm, pt), api.transit(pt, d))
    legs = leg.get("routes") or []
    if not legs:
        return {"cand": cand, "time_s": None, "fare": None, "note": leg.get("status")}
    best = min((compare.reconstruct(r, pt, d) for r in legs), key=lambda b: compare.gc_s(b["time_s"], b["fare"], vot))
    plan = anchors.taxi_plan_s(car["duration_s"], connect=True)
    board_at = at + timedelta(seconds=cand["head_s"] + plan + cand["link_s"]) if at is not None else None
    _, kept = anchors.transfer_fare(cap, True, True, cand["link_s"], plan, board_at)
    tail = best["fare"] or 0
    transit = cap + (max(0.0, tail - anchors.TRANSFER_REBASE_FARE) if kept else tail)
    fare = (car["fare"]["taxi"] or 0) + (car["fare"]["toll"] or 0) + transit
    return {"cand": cand, "time_s": cand["head_s"] + plan + best["time_s"], "fare": fare, "note": ""}


async def _verify_one(api, o, d, cand, vot, at=None):
    """후보 하나를 온라인으로 확인.

    A 유형은 `car(O→앵커)` + `대중교통(앵커→D)` 2콜, (b)의 B 는 `대중교통(O→앵커)` + `car(앵커→D)` 2콜,
    (a)의 B 는 앞 구간이 기준 경로에 있어 `car(앵커→D)` 1콜, D 는 공백 한 구간만 바꿔 `car(공백 시작→끝)` 1콜이다.
    """
    pt = (cand["lon"], cand["lat"])
    if cand["hybrid"] == "C":
        return await _verify_detour(api, d, cand, vot, at)
    if cand["hybrid"] == "A":
        car, leg = await asyncio.gather(api.car(o, pt), api.transit(pt, d))
        legs = leg.get("routes") or []
        if not legs:
            return {"cand": cand, "time_s": None, "fare": None, "note": leg.get("status")}
        best = min((compare.reconstruct(r, pt, d) for r in legs),
                   key=lambda b: compare.gc_s(b["time_s"], b["fare"], vot))
        time_s = anchors.taxi_connect_s(car["duration_s"]) + best["time_s"]
        fare = (car["fare"]["taxi"] or 0) + (car["fare"]["toll"] or 0) + (best["fare"] or 0)
    elif cand["strategy"] == "b":
        # (b)의 B 는 기준 경로에 없는 노선을 찾아낸 것이라 앞 구간도 확인해야 한다
        leg, car = await asyncio.gather(api.transit(o, pt), api.car(pt, d))
        legs = leg.get("routes") or []
        if not legs:
            return {"cand": cand, "time_s": None, "fare": None, "note": leg.get("status")}
        best = min((compare.reconstruct(r, o, pt) for r in legs),
                   key=lambda b: compare.gc_s(b["time_s"], b["fare"], vot))
        time_s = best["time_s"] + anchors.taxi_plan_s(car["duration_s"], connect=False)
        fare = (car["fare"]["taxi"] or 0) + (car["fare"]["toll"] or 0) + (best["fare"] or 0)
    elif cand["hybrid"] == "D":
        # 공백 한 구간만 택시로 — 나머지는 기준 경로에 그대로 있다. 내려서 다시 차를 타면 연결 버퍼를 씌운다
        car = await api.car((cand["from_lon"], cand["from_lat"]), pt)
        eta = car["duration_s"] or 0
        time_s = (cand["base_time_s"] - cand["gap_s"]
                  + anchors.taxi_plan_s(eta, connect=cand["resume_ride"]))
        # 환승이 이어지는지는 응답 ETA 로 다시 판정한다 — 오프라인 근사로 이어진다고 봤어도 실제 택시가 길면 끊긴다
        plan = anchors.taxi_connect_s(eta)
        board_at = at + timedelta(seconds=cand["board_offset_s"] + plan) if at is not None else None
        transit, _ = anchors.transfer_fare(cand.get("transit_fare_cap"), cand["pre_ride"], cand["resume_ride"],
                                           cand["link_s"], plan, board_at)
        fare = (car["fare"]["taxi"] or 0) + (car["fare"]["toll"] or 0) + (transit or 0)
    else:
        # (a)의 B 는 앞 구간이 기준 경로에 그대로 있다 — 대중교통 콜이 들지 않는다
        car = await api.car(pt, d)
        time_s = (cand["ride_s"] + (cand["wait_s"] or 0) + cand["walk_s"]
                  + anchors.taxi_plan_s(car["duration_s"], connect=False))
        fare = (car["fare"]["taxi"] or 0) + (car["fare"]["toll"] or 0) + (cand.get("transit_fare_cap") or 0)
    return {"cand": cand, "time_s": time_s, "fare": fare, "note": ""}


def _report(rows, base, vot):
    """검증 결과 → Pareto · 원/분 · 구조 다양성."""
    ok = [r for r in rows if r["time_s"] is not None]
    # 택시를 쓰는 최소 기준 — 같은 잣대의 기준선보다 5분 이상 빠르지 않은 후보는 보여 주지 않는다
    little = [r for r in ok if not compare.saves_enough(r["time_s"], base["time_s"])]
    if little:
        print("  기준선보다 %d분 이상 빠르지 않아 뺀 후보 %d개" % (anchors.TAXI_MIN_SAVING_S // 60, len(little)))
    ok = [r for r in ok if r not in little]
    for r in ok:
        r["hybrid"] = r["cand"]["hybrid"]
        r["gc_s"] = compare.gc_s(r["time_s"], r["fare"], vot)
        r["won_per_min"] = compare.saving_per_min(r, base)
    baseline = dict(base, cand=None, hybrid="E",
                    gc_s=compare.gc_s(base["time_s"], base["fare"], vot), won_per_min=None)
    front = compare.pareto(ok + [baseline])
    print("\n검증 결과 — 비지배해 %d개 / 후보 %d개" % (len(front), len(ok)))
    for r in compare.diversify(sorted(front, key=lambda x: x["gc_s"])):
        name = "전 구간 대중교통(기준선)" if r["cand"] is None else "%s %s" % (r["hybrid"], r["cand"]["name"])
        eff = "" if r.get("won_per_min") is None else "  %.0f원/분 %s" % (
            r["won_per_min"], "비추천" if r["won_per_min"] > vot else "추천")
        print("  %-28s %6.1f분 %8.0f원  GC %6.1f분%s"
              % (name[:28], r["time_s"] / 60, r["fare"], r["gc_s"] / 60, eff))


def cmd_calibrate(args):
    """자동차 경로 응답 표본으로 우회계수·평균속도·요금 계수를 잰다 — 파생 수치만 남긴다."""
    st = load_settings()
    stops = load_dbs(st)["stops"]
    rng = random.Random(args.seed)
    n = stops.counts["bus"]
    pairs = []
    while len(pairs) < args.samples:
        a, b = stops.row("bus", rng.randrange(n)), stops.row("bus", rng.randrange(n))
        if a["is_virtual"] or b["is_virtual"]:
            continue
        straight = anchors.haversine_m(a["lon"], a["lat"], b["lon"], b["lat"])
        if args.min_km * 1000 <= straight <= args.max_km * 1000:
            pairs.append((a, b, straight))
    asyncio.run(_calibrate(pairs, args))


async def _calibrate(pairs, args):
    now = datetime.now(KST).replace(tzinfo=None)   # 응답 요금은 지금 시각 기준이다 — 심야 할증을 같은 조건으로 맞춘다
    rows = []
    async with httpx.AsyncClient(timeout=args.timeout) as http:
        api = Backend(http, args.api)
        for a, b, straight in pairs:
            try:
                car = await api.car((a["lon"], a["lat"]), (b["lon"], b["lat"]))
            except Exception as e:      # 한 건 실패가 표본 전체를 버리게 두지 않는다
                print("  건너뜀:", e)
                continue
            if not car.get("distance_m") or not car.get("duration_s"):
                continue
            rows.append({"straight_m": straight, "road_m": car["distance_m"], "dur_s": car["duration_s"],
                         "taxi": car["fare"]["taxi"], "sgg": a.get("sgg_nm"), "sgg_to": b.get("sgg_nm")})
    if not rows:
        print("표본 없음")
        return
    _calibration_report(rows, now)


def _quantile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def _spearman(xs, ys):
    """순위 상관 — 가지치기는 순위만 맞으면 되므로 절대 오차보다 이 값이 직접적인 기준이다."""
    def ranks(v):
        order = sorted(range(len(v)), key=v.__getitem__)
        r = [0.0] * len(v)
        for rank, i in enumerate(order):
            r[i] = float(rank)
        return r
    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    var = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return cov / var if var else float("nan")


def _time_errors(rows, detour, speed):
    """근사 시간 − 응답 시간 (분). 양수면 근사가 느리게 본다."""
    return [(r["straight_m"] * detour / 1000 / speed * 3600 - r["dur_s"]) / 60 for r in rows]


def _model_errors(rows):
    """현재 `anchors` 택시 시간 모형의 오차 (분)."""
    return [(anchors.TAXI_BASE_S + r["straight_m"] / 1000 * anchors.TAXI_S_PER_KM - r["dur_s"]) / 60 for r in rows]


def _line(label, errs):
    ab = [abs(e) for e in errs]
    return "  %-20s 편향(중앙) %+5.1f분 · 절대오차 중앙 %4.1f분 · p90 %4.1f분" % (
        label, _quantile(errs, 0.5), _quantile(ab, 0.5), _quantile(ab, 0.9))


def _calibration_report(rows, now):
    """자동차 경로 표본 → 제안 계수와 현재 계수의 오차 비교. 파생 수치만 찍는다.

    시간에 실제로 물리는 것은 우회계수와 속도의 **비**(직선 1 km 당 초)라, 둘을 따로 중앙값으로 잡으면 곱의 중앙값이
    어긋난다. 우회계수는 도로 거리/직선의 중앙값으로 두고, 속도는 '직선 1 km 당 초'의 중앙값에 맞춰 역산한다.
    """
    n = len(rows)
    detour = _quantile([r["road_m"] / r["straight_m"] for r in rows], 0.5)
    sec_per_km = _quantile([r["dur_s"] / (r["straight_m"] / 1000) for r in rows], 0.5)
    speed = detour * 3600 / sec_per_km
    road_speed = _quantile([r["road_m"] / 1000 / (r["dur_s"] / 3600) for r in rows], 0.5)
    print("\n표본 %d개 · 기준 시각 %s" % (n, now.strftime("%m-%d %H:%M")))
    print("  직선거리 중앙 %.1f km (%.1f–%.1f)" % (
        _quantile([r["straight_m"] / 1000 for r in rows], 0.5),
        min(r["straight_m"] for r in rows) / 1000, max(r["straight_m"] for r in rows) / 1000))
    print("  우회계수(도로/직선) 중앙 %.3f · 도로 평균속도 중앙 %.1f km/h · 직선 1 km 당 %.0f초"
          % (detour, road_speed, sec_per_km))

    print("\n택시 시간 — 현재 %.0f초 + %.0f초/km  vs  원점 모형 DETOUR=%.2f · SPEED=%.1f km/h"
          % (anchors.TAXI_BASE_S, anchors.TAXI_S_PER_KM, detour, speed))
    print(_line("현재 모형", _model_errors(rows)))
    print(_line("원점 모형", _time_errors(rows, detour, speed)))
    # 절편 모형 — 짧은 구간에서 근사가 빠르고 긴 구간에서 느리면 출발·신호 같은 고정 비용이 있다는 뜻이다
    xs = [r["straight_m"] / 1000 for r in rows]
    ys = [r["dur_s"] for r in rows]
    mx, my = sum(xs) / n, sum(ys) / n
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    icpt = my - slope * mx
    print(_line("절편 재적합 %.0f+%.0f/km" % (icpt, slope),
                [(icpt + slope * x - y) / 60 for x, y in zip(xs, ys)]))
    approx = [r["straight_m"] for r in rows]   # 계수는 곱이라 순위는 직선거리 순위와 같다
    print("  순위 상관(스피어만, 계수와 무관) %.3f" % _spearman(approx, [r["dur_s"] for r in rows]))
    for lo, hi in ((1, 3), (3, 6), (6, 12)):
        part = [r for r in rows if lo * 1000 <= r["straight_m"] < hi * 1000]
        if len(part) >= 5:
            print(_line("  직선 %d–%d km (%d)" % (lo, hi, len(part)), _model_errors(part)))

    fared = [r for r in rows if r["taxi"]]
    if fared:
        formula = [anchors.taxi_fare(r["road_m"] / 1000, r["sgg"], r["sgg_to"], now) - r["taxi"] for r in fared]
        chain = [anchors.taxi_fare(r["straight_m"] * detour / 1000, r["sgg"], r["sgg_to"], now) - r["taxi"]
                 for r in fared]
        rel = [abs(e) / r["taxi"] for e, r in zip(formula, fared)]
        print("\n택시 요금 (%d개, 응답 중앙 %.0f원)" % (len(fared), _quantile([r["taxi"] for r in fared], 0.5)))
        for label, errs in (("산식만(응답 도로거리)", formula), ("근사 전체(직선×우회)", chain)):
            ab = [abs(e) for e in errs]
            print("  %-20s 편향(중앙) %+6.0f원 · 절대오차 중앙 %5.0f원 · p90 %5.0f원"
                  % (label, _quantile(errs, 0.5), _quantile(ab, 0.5), _quantile(ab, 0.9)))
        print("  산식 상대오차 중앙 %.1f%% · 순위 상관 %.3f"
              % (_quantile(rel, 0.5) * 100,
                 _spearman([anchors.taxi_fare(r["road_m"] / 1000, r["sgg"], r["sgg_to"], now) for r in fared],
                           [r["taxi"] for r in fared])))


def main(argv=None):
    p = argparse.ArgumentParser(prog="algo.cli", description="앵커 선택 실험")
    p.add_argument("--api", default=DEFAULT_API, help="데모 웹 백엔드 주소 (쿼터를 한 프로세스에서 관리한다)")
    p.add_argument("--timeout", type=float, default=30.0)
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("plan", help="앵커 후보 생성과 검증")
    q.add_argument("--od", choices=sorted(OD_PRESETS), help="경기 외곽 공통 표본 OD")
    q.add_argument("--from", dest="frm", help="출발 'lon,lat'")
    q.add_argument("--to", help="도착 'lon,lat'")
    q.add_argument("--pref", choices=sorted(anchors.VOT), default="balance")
    q.add_argument("--top", type=int, default=8, help="온라인으로 확인할 앵커 수")
    q.add_argument("--t-max", type=int, default=15, help="택시 구간 상한(분)")
    q.add_argument("--at", help="기준 시각 ISO (기본: 지금)")
    q.add_argument("--offline", action="store_true", help="쿼터를 쓰지 않고 후보 생성까지만")
    q.add_argument("--no-realtime", action="store_true", help="첫 승차 대기를 실시간 없이 정적 배차로만")
    q.add_argument("--c-points", type=int, default=anchors.C_POINTS, help="C: 다시 찾을 정류장 수")
    q.add_argument("--c-anchors", type=int, default=anchors.C_ANCHORS, help="C: 정류장마다 확인할 앵커 수")
    q.set_defaults(func=cmd_plan)

    c = sub.add_parser("calibrate", help="택시 시간·요금 근사 보정")
    c.add_argument("--samples", type=int, default=150)
    c.add_argument("--min-km", type=float, default=1.0)
    c.add_argument("--max-km", type=float, default=12.0)
    c.add_argument("--seed", type=int, default=20260912)
    c.set_defaults(func=cmd_calibrate)

    args = p.parse_args(argv)
    if args.cmd == "plan" and not args.od and not (args.frm and args.to):
        p.error("--od 또는 --from/--to 가 필요하다")
    args.func(args)


if __name__ == "__main__":
    main()
