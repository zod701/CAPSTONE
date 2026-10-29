"""하이브리드(택시 ↔ 대중교통) 경로 — algo 의 후보를 온라인으로 확인해 화면에 줄 형태로 만든다.

모든 후보는 `O →[택시 →](대중교통·도보*) u →(택시) v →(대중교통·도보*) D` 한 꼴이다(algo/search.py, method.md §3.8).
u·v 는 탐색 하나로 만든다 — 시간을 아는 곳(출발지 · 기준 경로 위 정류소 · 택시로 간 정류소)에서 정적 노선표의 노선을 한 번 더 타 닿는
정류소까지가 u, 목적지 쪽에서 거꾸로 같은 것이 v 다. 그 모든 짝이 후보다(Tier 1, 쿼터 0).
확인(Tier 2)은 순차 검증 탐색(algo/lazy_pareto)이 고른다 — 검증 결과의 시간·요금 Pareto 를 보며 다음 질의를 정하고,
요청마다 카카오 고유 질의 SEARCH_BUDGET 회(자동차 + 대중교통, 기준 경로 1콜 제외)에서 멈춘다. 택시는 자동차 1콜,
노선을 더 탄 쪽 구간이 있으면 그 구간의 대중교통이 1콜씩 더 든다. 그래서 화면의 버튼을 눌렀을 때만 돈다 → method.md §2.4.
택시 시간에는 호출 대기가 들어가고(taxi_plan_s), 기준선보다 5분 이상 빠르지 않은 후보는 뺀다.

`/api/hybrid` 는 모든 승차 대기에 배차 추정을 쓴다. `plan` 에 `arrivals` 를 주면 첫 승차 대기를 실시간으로 바꾼 경로를
기준선과 앞부분(u 까지 — 출발지에서 걸어서 탄다)에 쓰고, 뒷부분(v 부터)은 원래 경로를 쓴다: 택시로 정류장에 닿아
'걸어서 닿는다' 는 전제의 실시간 대기가 맞지 않는다.

카카오 응답은 저장하지 않는다 — 만들어 돌려주고 버린다. 지도에 그릴 좌표도 이 응답 안에서만 산다.
기준 경로는 화면이 방금 받은 [경로 검색] 결과를 본문으로 돌려받아 다시 쓸 수 있다(POST /api/hybrid) — 서버에 두지 않고
이 요청 처리 중에만 쓴다. 그 결과에는 첫 승차 실시간 대기가 들어 있어 배차 추정으로 되돌린 뒤(static_copy) 이 시각으로 다시 매긴다.
"""
import asyncio
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from geoutil import haversine_m

from .config import KST
from .errors import ApiError
from .transit import normalize_car
from .wait import apply_wait_defaults
from .hybrid_trace import HybridTrace

_ROOT = Path(__file__).resolve().parents[3]   # 저장소 루트 — algo 패키지를 찾는다
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SEARCH_BUDGET = 30   # 요청 하나가 후보 확인에 쓰는 카카오 고유 질의 수 (자동차 + 대중교통, 기준 경로 1콜 제외)
SEARCH_PARALLEL = 3   # 한 번에 함께 보내는 질의 수 — 하나씩이면 응답이 4~9초 걸렸다


def _algo():
    """algo 패키지 — 없으면 ApiError. 실험 코드와 함께 두므로 없을 수도 있다."""
    try:
        from algo import anchors, compare
    except ImportError as err:
        raise ApiError("hybrid_unavailable", "하이브리드 경로 모듈(algo)을 불러오지 못했습니다.",
                       action="저장소의 algo 패키지를 확인하세요.", status=503) from err
    return anchors, compare


class AnchorTables:
    """후보 생성이 쓰는 추가 표 — 백엔드 DB 가 읽지 않는 열(역 실체 키 · 첫·막차)."""

    def __init__(self, phys, hours):
        self.phys, self.hours = phys, hours
        self.counts = {"phys": len(phys), "service_hours": len(hours)}

    @classmethod
    def load(cls, processed_dir):
        anchors, _ = _algo()
        return cls(anchors.load_phys(processed_dir), anchors.load_service_hours(processed_dir))


def static_copy(routes):
    """[경로 검색] 결과 → 첫 승차 대기를 배차 추정으로 되돌린 사본 (원본은 건드리지 않는다).

    그 결과는 받은 시각의 실시간 대기를 달고 있다(with_realtime_first_wait 가 static_wait_s 에 원래 값을 남긴다).
    뒷부분은 택시로 정류장에 닿아 원래 경로가 필요하고, 나머지는 이 요청 시각의 실시간으로 다시 매긴다. 경로 대기 합은 add_wait 와
    같은 규칙이다 — 대기를 모르는 승차 구간마다 기본 15분을 적용한다.
    """
    out = []
    for route in routes:
        steps = [dict(s) for s in route["steps"]]
        for step in steps:
            if "static_wait_s" in step:
                step["wait_s"] = step.pop("static_wait_s")
                source = step.pop("static_wait_source", None)
                step.pop("wait_source", None)
                if source is not None:
                    step["wait_source"] = source
                step.pop("walk_to_stop_s", None)
        restored = {**route, "steps": steps}
        apply_wait_defaults(restored)
        out.append(restored)
    return out


def _fastest(routes, o, d, compare):
    """기준선 — 대중교통 경로 중 가장 빠른 하나 → (재구성 값, 경로 객체). 택시를 섞는 사람은 돈보다 시간이 급하다.

    하이브리드와 같은 잣대(compare.reconstruct — 차내 + 첫·끝 도보 추정 + 대기)로 잰다. 카카오 시간에는 대기가 없어 그대로 견주면
    하이브리드가 부당하게 느려 보인다(method.md E15). 같으면 요금이 싼 쪽, 요금을 모르는 경로는 뒤다.
    """
    fare = lambda rec: rec["fare"] if rec["fare"] is not None else float("inf")  # noqa: E731
    scored = [(compare.reconstruct(r, o, d), r) for r in routes]
    return min(scored, key=lambda x: (x[0]["time_s"], fare(x[0])))


async def realtime_first_waits(st, arrivals, o, routes):
    """경로마다 첫 승차 지점의 실시간 도착으로 첫 대기를 바꾼 사본 → (경로들, 진단). 같은 정류장·역은 한 번만 부른다.

    도시철도는 행선 안내의 '방면' 으로 방향을 가리므로 승·하차역 쌍마다 그 방향 이웃 역 이름을 함께 넘기고,
    남은 역 수로 도착을 어림할 때 쓰도록 열차가 지나올 역 사이 누적 시간(`subway_upstream_s`)도 같은 키로 넘긴다 —
    넘기지 않으면 역당 공통 상수로 잡아 역 간격이 긴 구간에서 도착을 너무 이르게 본다(method.md E19).
    실시간이 없거나(no_realtime) 원천이 실패한 곳은 넘기지 않는다 — 그 경로는 정적 배차 대기로 남는다.
    [경로 검색] 의 대중교통 결과(api.transit_payload)도 이것을 쓴다. algo 가 없으면 ApiError.
    """
    anchors, _ = _algo()
    want, toward, upstream = {}, {}, {}
    for r in routes:
        i = anchors.first_ride(r["steps"])
        if i is None:
            continue
        step = r["steps"][i]
        board, alight = anchors._chosen(step, "board"), anchors._chosen(step, "alight")
        if not board:
            continue
        kind = "subway" if step["type"] == "SUBWAY" else "bus"
        want[board["id"]] = kind
        if kind == "subway" and alight:
            pair = (board["id"], alight["id"])
            toward[pair] = anchors.subway_toward(st.lines, st.live_stations, *pair)
            upstream[pair] = anchors.subway_upstream_s(st.lines, *pair)

    async def fetch(stop_id, kind):
        try:
            return stop_id, (await arrivals(st, kind, stop_id))["items"]
        except ApiError:
            return stop_id, None

    got = await asyncio.gather(*(fetch(sid, kind) for sid, kind in want.items()))
    return anchors.with_realtime_first_wait(routes, o, {sid: it for sid, it in got if it is not None}, toward, upstream)


class _Live:
    """카카오를 실제로 부른다 — 탐색이 다음 질의를 낼 때만 쓴다. 실패는 기록하고 탐색에는 '알 수 없음' 으로 넘긴다."""

    def __init__(self, st, transit_payload, unavailable):
        self.st, self.transit_payload, self.unavailable = st, transit_payload, unavailable
        self.failed = []

    async def car(self, start, end):
        try:
            return normalize_car(await self.st.kakao.car(*start, *end))
        except ApiError as err:
            self._fail("car", err)

    async def transit(self, start, end):
        try:
            return await self.transit_payload(self.st, start[0], start[1], end[0], end[1])
        except ApiError as err:
            self._fail("transit", err)

    def _fail(self, kind, err):
        self.failed.append({"query": kind, "code": err.code, "message": err.message})
        raise self.unavailable() from err


def _end(part):
    """택시 구간의 한쪽 끝 — 출발지·목적지 그대로면 None."""
    if part["source"] in ("origin", "destination"):
        return None
    return {k: part["point"].get(k) for k in ("id", "name", "lon", "lat", "kind", "line_group", "sgg_nm")}


SAME_CARD_M = 200.0   # 택시 양끝이 이만큼 가까우면 같은 정류소다 — 정류소 DB 와 경로 응답이 같은 역을 이름·좌표를 조금 달리 준다


def _same_card(a, b, o, d):
    """화면에서 같은 카드로 보이는 결과인가 — 시간(분)·요금이 같고 택시 양끝이 같은 곳."""
    ends = lambda r: (r["from"] or {"lon": o[0], "lat": o[1]}, r["to"] or {"lon": d[0], "lat": d[1]})  # noqa: E731
    return (round(a["time_s"] / 60) == round(b["time_s"] / 60) and a["fare"] == b["fare"]
            and all(haversine_m(p["lon"], p["lat"], q["lon"], q["lat"]) < SAME_CARD_M for p, q in zip(ends(a), ends(b))))


def _row(cand, result):
    """검증 결과 → 화면에 줄 항목. 화면은 대중교통 구간들(`transit.steps`)의 `taxi_at` 자리에 택시 구간을 끼운다.
    앞부분이 택시로 시작하면(`first_taxi`) 그 택시는 맨 앞 — 출발지에서 `first_taxi.to` 까지 — 에 하나 더 끼운다."""
    steps = result["head_steps"] + result["tail_steps"]
    first = None
    if result.get("first_car"):
        b = cand["head"]["taxi_from"]
        first = {"to": {k: b.get(k) for k in ("id", "name", "lon", "lat")}, "taxi": result["first_car"],
                 "counted_s": result["first_taxi_s"]}
    return {"from": _end(cand["head"]), "to": _end(cand["tail"]), "first_taxi": first,
            "head": cand["head"]["source"], "tail": cand["tail"]["source"], "taxi_at": len(result["head_steps"]),
            "time_s": result["time_s"], "fare": result["fare"], "fare_info": result["fare_info"],
            "transit_time_s": result["transit_time_s"],
            "taxi_counted_s": result["taxi_s"],   # 총 시간에 넣은 택시 몫 (호출 대기 · 연결 여유 포함)
            "taxi": result["car"], "transit": {"steps": steps}}


async def plan(st, transit_payload, o, d, *, arrivals=None, base_routes=None, t_max_s=1800, now=None, debug=False):
    """후보 생성 → 순차 검증 탐색으로 SEARCH_BUDGET 회 안에서 온라인 확인 → 기준 경로와 함께 견준 결과.

    `base_routes` 를 주면 기준 경로를 새로 부르지 않고 그것(같은 출발·도착의 [경로 검색] 결과)을 쓴다.

    선호는 시간 중시(VOT time)로 고정한다 — 기준선도 최소 시간 대중교통 경로다.

    `arrivals(st, kind, stop_id)` 를 주면 첫 승차 대기를 실시간으로 바꾼다. 기준 시각(`now`)을 준 요청은 '지금' 의
    도착정보가 그 시각과 맞지 않아 쓰지 않는다.
    """
    anchors, compare = _algo()
    from algo import search
    from algo.lazy_pareto import QueryUnavailable
    if st.anchor_tables is None or st.stops is None or st.routes is None or st.lines is None:
        raise ApiError("data_not_built", "하이브리드 경로에 필요한 정제 데이터가 없습니다.",
                       action="data 파이프라인을 먼저 실행하세요 (data/README.md).", status=503)
    pref = "time"
    vot = anchors.VOT[pref]
    at = (now or datetime.now(KST)).replace(tzinfo=None)

    if base_routes is None:
        base_norm = await transit_payload(st, o[0], o[1], d[0], d[1])   # 기준선 + 후보의 입력
        base_routes, base_source = base_norm["routes"], "called"
        if not base_routes:
            raise ApiError("no_route", base_norm["message"] or "대중교통 경로가 없어 기준을 세울 수 없습니다.",
                           action="출발지·도착지를 조금 옮겨 보세요.", status=404)
    else:
        base_routes, base_source = static_copy(base_routes), "reused"   # 화면이 돌려보낸 [경로 검색] 결과
    rt_routes, rt_diag = base_routes, None
    if arrivals is not None and now is None:
        rt_routes, rt_diag = await realtime_first_waits(st, arrivals, o, base_routes)
    base, base_route = _fastest(rt_routes, o, d, compare)

    cands, generation = await asyncio.to_thread(
        search.propose, st.stops, st.routes, st.lines, o, d, rt_routes, base_routes, vot=vot,
        baseline_s=base["time_s"], at=at, headway=st.headway, phys=st.anchor_tables.phys,
        hours=st.anchor_tables.hours, t_max_s=t_max_s)
    trace = HybridTrace(o, d, vot, base, cands, generation, group_color=st.stops.group_color) if debug else None

    # 순차 검증 — 확인한 결과(요금이 알려진 기준 경로 포함)의 Pareto 를 보며 다음 질의를 고른다 (algo/lazy_pareto.md)
    known = {"T%d" % i: search.Cost(rec["time_s"], rec["fare"])
             for i, rec in enumerate(compare.reconstruct(r, o, d) for r in rt_routes) if rec["fare"] is not None}
    live = _Live(st, transit_payload, QueryUnavailable)
    found, run = await search.run(cands, known, live, o, d, vot=vot, baseline_s=base["time_s"], at=at,
                                  budget=SEARCH_BUDGET, parallel=SEARCH_PARALLEL)

    rows = []
    for cand in cands:
        result, status = found.get(cand["id"]), run.status[cand["id"]]
        if result is not None:
            row = _row(cand, result)
            if any(_same_card(row, kept, o, d) for kept in rows):
                if trace:
                    trace.reject(cand, "경로 검증", "같은 곳에서 타고 내리는 같은 시간·요금의 결과가 이미 있습니다.")
                continue
            if trace:
                row["trace_id"] = trace.verified(cand, row)
            rows.append(row)
        elif trace and status == "pending":
            trace.reject(cand, "검증 순서·예산", "질의 %d회 안에 확인할 차례가 오지 않았습니다." % SEARCH_BUDGET)
        elif trace and status == "pruned":
            trace.reject(cand, "검증 순서·예산", "이미 확인한 경로가 이 후보가 낼 수 있는 가장 좋은 시간·요금보다 빠르고 싸서 확인하지 않았습니다.")
        elif trace and status == "unavailable":
            trace.reject(cand, "경로 검증", "API 검증 실패")
        elif trace:
            trace.reject(cand, "경로 검증", "연결 가능한 대중교통 경로나 택시 구간을 얻지 못했습니다.")
    if not rows and live.failed and not trace:
        raise ApiError(live.failed[0]["code"], live.failed[0]["message"],
                       action="잠시 뒤 다시 시도하세요.", status=503)

    verified = len(rows)
    if trace:
        for row in rows:
            if not compare.saves_enough(row["time_s"], base["time_s"]):
                trace.reject_id(row["trace_id"], "시간 절약 검사",
                                "기준 대비 %.1f분 절약. 택시 경로는 최소 5분을 절약해야 합니다."
                                % ((base["time_s"] - row["time_s"]) / 60))
    # 택시를 쓰는 최소 기준 — 같은 잣대의 기준선(최소 시간 경로)보다 5분 이상 빠르지 않은 후보는 보여 주지 않는다
    rows = [r for r in rows if compare.saves_enough(r["time_s"], base["time_s"])]

    baseline = {**base, "label": "최소 시간 경로", "route": base_route}
    for r in rows + [baseline]:
        r["gc_s"] = compare.gc_s(r["time_s"], r["fare"], vot)
    front = {id(r) for r in compare.pareto(rows + [baseline])}
    for r in rows:
        r["won_per_min"] = compare.saving_per_min(r, baseline)
        r["recommended"] = r["won_per_min"] is not None and r["won_per_min"] <= vot
        r["pareto"] = id(r) in front
    baseline["pareto"] = id(baseline) in front
    rows.sort(key=lambda r: r["gc_s"])
    return {**({"trace": trace.finish(rows)} if trace else {}),
            "pref": pref, "vot": vot, "at": at.isoformat(timespec="minutes"), "t_max_s": t_max_s,
            "baseline": baseline, "routes": rows, "failed": live.failed,
            "diag": {"heads": generation["heads"], "tails": generation["tails"], "pairs": generation["pairs"],
                     "search": {"budget": SEARCH_BUDGET, "queries": len(run.query_order),
                                "car": sum(q[0] == "car" for q in run.query_order),
                                "transit": sum(q[0] == "transit" for q in run.query_order),
                                "candidates": len(cands), "status": dict(Counter(run.status.values())),
                                "complete": run.complete},
                     "too_little_saving": verified - len(rows), "min_saving_s": anchors.TAXI_MIN_SAVING_S,
                     "realtime": rt_diag,   # 첫 승차 대기 — realtime · realtime+headway · no_arrivals … (안 썼으면 None)
                     "base_source": base_source,   # called = 기준 경로를 새로 부름 · reused = [경로 검색] 결과를 다시 씀
                     # 확인한 후보 수(성공·경로 없음·API 실패) · 그중 결과를 얻은 수
                     "picked": sum(v in ("verified", "infeasible", "unavailable") for v in run.status.values()),
                     "verified": verified}}
