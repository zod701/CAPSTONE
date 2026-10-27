r"""하이브리드 실험 진입점 — plan(후보 생성·순차 검증) / calibrate(택시 근사 보정).

카카오 응답은 저장하지 않는다. 화면에 남기는 것은 **파생 수치**(시간·비용·원/분)뿐이다 → method.md §2.2.

카카오는 **데모 웹 백엔드를 경유해서** 부른다. `QuotaGuard` 의 잠금은 프로세스 안에서만 유효해서
(web/README.md §3) CLI 가 직접 부르면 서버와 같은 `quota.json` 을 동시에 써 카운트가 덮어써진다.
백엔드를 지나가면 쿼터가 한 프로세스에서만 관리되고, 덤으로 접지(`resolve_route`)와 대기(`add_wait`)까지
끝난 경로를 받는다. 그래서 실험 중에도 데모 웹을 띄워 둔 채로 쓸 수 있다.

    .\.venv\Scripts\python.exe -m algo.cli plan --od dongtan --offline
    .\.venv\Scripts\python.exe -m algo.cli plan --od suji --budget 20
    .\.venv\Scripts\python.exe -m algo.cli calibrate --samples 150
"""
import argparse
import asyncio
import random
from collections import Counter
from datetime import datetime

import httpx

import algo  # noqa: F401  — sys.path 훅 (web/backend, data/scripts)
from app.config import KST, load_settings
from app.headwaydb import HeadwayDB
from app.linesdb import LinesDB
from app.livestationsdb import LiveStationsDB
from app.routesdb import RoutesDB
from app.stopsdb import StopsDB

from algo import anchors, compare, search
from algo.lazy_pareto import QueryUnavailable

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


def print_candidates(cands, title):
    """후보 표 — 파생 수치만 찍는다."""
    print("\n%s  (%d개)" % (title, len(cands)))
    print("  %-14s %-16s %-16s %6s %8s %7s %8s %8s" %
          ("앞 → 뒤", "택시 타는 곳", "택시 내리는 곳", "택시분", "택시요금", "추정분", "추정요금", "점수분"))
    for c in cands:
        print("  %-14s %-16s %-16s %6.1f %8.0f %7.1f %8.0f %8.1f" % (
            "%s→%s" % (c["head"]["source"], c["tail"]["source"]), c["head"]["point"]["name"][:16],
            c["tail"]["point"]["name"][:16], c["taxi_s"] / 60, c["taxi_fare"], c["time_s"] / 60, c["fare"],
            c["score_s"] / 60))


class _Search:
    """탐색이 부르는 api — 백엔드 실패는 '알 수 없음'(QueryUnavailable)으로 넘긴다."""

    def __init__(self, api):
        self.api, self.failed = api, []

    async def _call(self, fn, a, b):
        try:
            return await fn(a, b)
        except (RuntimeError, httpx.HTTPError) as err:
            self.failed.append(str(err))
            raise QueryUnavailable() from err

    async def car(self, a, b):
        return await self._call(self.api.car, a, b)

    async def transit(self, a, b):
        return await self._call(self.api.transit, a, b)


def cmd_plan(args):
    st = load_settings()
    dbs = load_dbs(st)
    title, o, d = _od(args)
    at = datetime.now(KST).replace(tzinfo=None) if args.at is None else datetime.fromisoformat(args.at)
    vot = anchors.VOT[args.pref]
    print("%s  선호=%s(VOT %.0f원/분)  기준시각=%s" % (title, args.pref, vot, at.strftime("%m-%d %H:%M")))
    asyncio.run(_run_plan(dbs, o, d, vot, at, args))


async def _run_plan(dbs, o, d, vot, at, args):
    """기준 경로 1콜 → 후보 생성(쿼터 0) → 순차 검증(고유 질의 --budget 회)."""
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
        # 첫 승차 대기를 실시간으로 — 기준선과 앞부분(걸어서 탄다)이 같은 값을 써야 견줄 수 있다. 기준 시각을 지정했으면
        # '지금' 의 도착정보가 그 시각과 맞지 않아 쓰지 않는다. 뒷부분은 택시로 닿아 타므로 원래 경로다
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

        cands, diag = search.propose(dbs["stops"], dbs["routes"], dbs["lines"], o, d, rt_routes, routes, vot=vot,
                                     baseline_s=base["time_s"], at=at, headway=dbs["headway"], phys=dbs["phys"],
                                     hours=dbs["hours"], t_max_s=args.t_max * 60)
        print("앞부분:", diag["heads"], "· 뒷부분:", diag["tails"], "· 짝:", diag["pairs"])
        print_candidates(cands[:args.show], "추정 점수 상위 %d" % args.show)
        if args.offline:
            print("\n--offline — 기준 경로 1콜 뒤 검증하지 않았다.")
            return

        known = {"T%d" % i: search.Cost(r["time_s"], r["fare"])
                 for i, r in enumerate(compare.reconstruct(x, o, d) for x in rt_routes) if r["fare"] is not None}
        wrapped = _Search(api)
        found, run = await search.run(cands, known, wrapped, o, d, vot=vot, baseline_s=base["time_s"], at=at,
                                      budget=args.budget, parallel=args.parallel)
        for err in wrapped.failed:
            print("  조회 실패:", err)
        print("\n순차 검증: 질의 %d회 · 후보 상태 %s" % (len(run.query_order), dict(Counter(run.status.values()))))
        _report(cands, found, base, vot)
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


def _report(cands, found, base, vot):
    """검증 결과 → 5분 기준 · Pareto · 원/분. 비지배해를 일반화 비용 순으로 모두 찍는다."""
    by_id = {c["id"]: c for c in cands}
    rows = [dict(r, cand=by_id[cid]) for cid, r in found.items()]
    # 택시를 쓰는 최소 기준 — 같은 잣대의 기준선보다 5분 이상 빠르지 않은 후보는 보여 주지 않는다
    ok = [r for r in rows if compare.saves_enough(r["time_s"], base["time_s"])]
    if len(ok) < len(rows):
        print("  시간 절약 기준 미달로 뺀 후보 %d개 (택시 %d분 이상)" % (len(rows) - len(ok), anchors.TAXI_MIN_SAVING_S // 60))
    for r in ok:
        r["gc_s"] = compare.gc_s(r["time_s"], r["fare"], vot)
        r["won_per_min"] = compare.saving_per_min(r, base)
    baseline = dict(base, cand=None, gc_s=compare.gc_s(base["time_s"], base["fare"], vot), won_per_min=None)
    front = compare.pareto(ok + [baseline])
    print("\n검증 결과 — 비지배해 %d개 / 결과 %d개" % (len(front), len(ok)))
    for r in sorted(front, key=lambda x: x["gc_s"]):
        c = r["cand"]
        name = ("전 구간 대중교통(기준선)" if c is None
                else "%s → %s" % (c["head"]["point"]["name"], c["tail"]["point"]["name"]))
        eff = "" if r.get("won_per_min") is None else "  %.0f원/분 %s" % (
            r["won_per_min"], "비추천" if r["won_per_min"] > vot else "추천")
        print("  %-30s %6.1f분 %8.0f원  GC %6.1f분%s"
              % (name[:30], r["time_s"] / 60, r["fare"], r["gc_s"] / 60, eff))


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
    p = argparse.ArgumentParser(prog="algo.cli", description="하이브리드 실험")
    p.add_argument("--api", default=DEFAULT_API, help="데모 웹 백엔드 주소 (쿼터를 한 프로세스에서 관리한다)")
    p.add_argument("--timeout", type=float, default=30.0)
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("plan", help="후보 생성과 순차 검증")
    q.add_argument("--od", choices=sorted(OD_PRESETS), help="경기 외곽 공통 표본 OD")
    q.add_argument("--from", dest="frm", help="출발 'lon,lat'")
    q.add_argument("--to", help="도착 'lon,lat'")
    q.add_argument("--pref", choices=sorted(anchors.VOT), default="balance")
    q.add_argument("--budget", type=int, default=30, help="후보 확인에 쓸 카카오 고유 질의 수(자동차 + 대중교통)")
    q.add_argument("--parallel", type=int, default=3, help="한 번에 함께 보낼 질의 수")
    q.add_argument("--show", type=int, default=15, help="찍을 추정 점수 상위 후보 수")
    q.add_argument("--t-max", type=int, default=30, help="택시 구간 상한(분)")
    q.add_argument("--at", help="기준 시각 ISO (기본: 지금)")
    q.add_argument("--offline", action="store_true", help="기준 경로 1콜 뒤 후보 생성까지만")
    q.add_argument("--no-realtime", action="store_true", help="첫 승차 대기를 실시간 없이 정적 배차로만")
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
