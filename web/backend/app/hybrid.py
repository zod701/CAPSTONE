"""하이브리드(택시 ↔ 대중교통) 경로 — algo 의 앵커 후보를 온라인으로 확인해 화면에 줄 형태로 만든다.

후보 생성(Tier 1)은 algo 가 정적 표만으로 한다(쿼터 0). 여기서는 상위 몇 개만 카카오로 확인한다(Tier 2) —
앵커마다 자동차 1콜, A 유형은 대중교통 1콜이 더 든다. 기준 경로까지 더하면 top=5 에 대중교통 최대 6 · 자동차 5콜이라
화면의 버튼을 눌렀을 때만 돈다 → method.md §2.4, §3.

**유형**: A = O →(택시) 앵커 →(대중교통) D · B = O →(대중교통) 앵커 →(택시) D ·
D = 기준 경로 한가운데의 공백(긴 대기·긴 환승 도보) 한 구간만 택시로 — 택시의 양끝이 모두 경로 위 정류소다.
A 의 뒷구간은 앵커에서 다시 부른 대중교통 경로다. B 의 앞구간은 어디서 나온 후보냐에 따라 다르다 —
(a) 섭동이 낸 B 는 기준 경로를 앵커에서 자른 것이고(콜이 들지 않는다), (b) 역추적이 낸 B 는 기준 경로에 없는
노선이라 O → 앵커를 한 번 더 부른다. D 는 나머지 구간이 기준 경로에 그대로 있어 자동차 1콜뿐이다.
C = 기준 경로가 돌아가기 시작하는 정류장에서 내려 경로를 다시 찾는다 — 택시로 목적지까지(자동차 1콜) 또는 그 정류장에서
곧장 가는 노선의 앵커로(자동차 1 + 대중교통 1콜). C 는 예산을 따로 둬 정류장 2곳 × (1 + 앵커 3) 을 전부 확인한다.
그래서 top=5 의 콜은 대중교통 1(기준) + 최대 5 + C 최대 6, 자동차 최대 5 + C 최대 8 이다.
택시 시간에는 호출 대기가 들어가고(taxi_plan_s), 검증 뒤 기준선보다 5분 이상 빠르지 않은 후보는 뺀다(algo/cli 와 같다).

**첫 승차 대기는 실시간 도착정보로** 바꾼 기준 경로를 기준선 · D · (a) 섭동의 B 에 쓴다 — 같은 값을 써야 견줄 수 있다.
(a) 섭동의 A 만 원래 경로를 쓴다: 택시로 정류장에 닿아 '걸어서 닿는다' 는 전제의 실시간 대기가 맞지 않는다.
그래서 (a) 는 A·B 를 나눠 두 번 부른다(algo/cli 와 같은 규칙).
도착정보는 첫 승차 정류장·역마다 한 번(공공데이터 쿼터, 15초 캐시)이고, 없거나 실패하면 정적 배차 대기로 남는다.

카카오 응답은 저장하지 않는다 — 만들어 돌려주고 버린다. 지도에 그릴 좌표도 이 응답 안에서만 산다.
기준 경로는 화면이 방금 받은 [경로 검색] 결과를 본문으로 돌려받아 다시 쓸 수 있다(POST /api/hybrid) — 서버에 두지 않고
이 요청 처리 중에만 쓴다. 그 결과에는 첫 승차 실시간 대기가 들어 있어 배차 추정으로 되돌린 뒤(static_copy) 이 시각으로 다시 매긴다.
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from geoutil import haversine_m

from .config import KST
from .errors import ApiError
from .transit import normalize_car
from .wait import apply_wait_defaults

_ROOT = Path(__file__).resolve().parents[3]   # 저장소 루트 — algo 패키지를 찾는다
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SGG_RADII = (2000.0, 10000.0)   # 좌표가 속한 시군을 가장 가까운 정류소로 본다 (택시 요금 유형·시계외)
MAX_TOP = 8
RIDE_TYPES = ("BUS", "SUBWAY")


def _algo():
    """algo 패키지 — 없으면 ApiError. 실험 코드와 함께 두므로 없을 수도 있다."""
    try:
        from algo import anchors, compare
    except ImportError as err:
        raise ApiError("hybrid_unavailable", "하이브리드 경로 모듈(algo)을 불러오지 못했습니다.",
                       action="저장소의 algo 패키지를 확인하세요.", status=503) from err
    return anchors, compare


class AnchorTables:
    """앵커 후보 생성이 쓰는 추가 표 — 백엔드 DB 가 읽지 않는 열(역 실체 키 · 첫·막차)."""

    def __init__(self, phys, hours):
        self.phys, self.hours = phys, hours
        self.counts = {"phys": len(phys), "service_hours": len(hours)}

    @classmethod
    def load(cls, processed_dir):
        anchors, _ = _algo()
        return cls(anchors.load_phys(processed_dir), anchors.load_service_hours(processed_dir))


def _sgg(stops, anchors, pt):
    """좌표가 속한 시군 — algo 의 실험(cli)과 같은 규칙으로 본다."""
    return anchors._sgg_at(anchors._near(stops, "bus", pt, SGG_RADII))


def _cut_route(routes, cand):
    """B 유형의 앞구간 — 후보가 계산에 쓴 원본 경로를 같은 하차 구간까지 자른 사본.

    같은 앵커를 지나는 경로·구간이 여럿이어도 후보에 보존한 위치로 정확히 찾는다.
    """
    route = routes[cand["route_index"]]
    return {**route, "steps": route["steps"][:cand["step_index"] + 1]}


def _gap_route(routes, cand):
    """D 유형의 대중교통 부분 — 후보가 계산에 쓴 원본 경로에서 공백 구간만 뺀 사본.

    대기 공백은 승차 구간을, 도보 공백은 환승 도보를 뺀다.
    → (경로, 뺀 자리) — 화면은 같은 자리에 택시 구간을 끼운다.
    """
    route = routes[cand["route_index"]]
    steps, i = route["steps"], cand["step_index"]
    return {**route, "steps": steps[:i] + steps[i + 1:]}, i


def _detour_route(routes, cand, anchors, o):
    """C 유형의 앞구간 — 기준 경로를 돌아가기 시작하는 정류장에서 자른 사본(지도·구간 표시용) → (경로, 택시 끼울 자리).

    그 정류장이 승차 구간 중간이면 구간도 그 정류장에서 자른다. 잘린 구간의 시간은 algo 가 그 정류장 시각을 잡은 방식
    (구간 시간을 정류장 사이 직선거리 비율로 나눔, anchors.route_points)을 그대로 따라 후보의 head_s 와 맞는다.
    어느 경로에서 나온 정류장인지는 후보가 들고 있지 않아 (정류장 id, 닿는 시각) 으로 찾는다. 못 찾으면 (None, None).
    """
    for route in routes:
        pts = anchors.route_points(route, o)
        hit = next((p for p in pts if p["id"] == cand["point_id"] and abs(p["t"] - cand["head_s"]) < 1), None)
        if hit is None:
            continue
        i = hit["step"]
        start = min(p["t"] for p in pts if p["step"] == i)   # 그 구간에서 차에 오른 시각 (대기 뒤)
        if hit["t"] - start < 1:                            # 탄 정류장에서 곧바로 내리면 그 구간은 없다
            return {**route, "steps": route["steps"][:i]}, i
        step = dict(route["steps"][i])
        locs = step.get("stop_locs") or []
        k = next((n for n, loc in enumerate(locs) if loc and loc.get("id") == cand["point_id"]), None)
        if k is not None:
            step["stops"], step["stop_locs"] = (step.get("stops") or [])[:k + 1], locs[:k + 1]
        if step.get("path"):
            near = min(range(len(step["path"])),
                       key=lambda n: haversine_m(step["path"][n][0], step["path"][n][1], hit["lon"], hit["lat"]))
            step["path"] = step["path"][:near + 1]
        step["time_s"], step["alight_name"] = hit["t"] - start, hit["name"]
        if step.get("distance_m") is not None:
            step["distance_m"] *= hit["fraction"]
        return {**route, "steps": route["steps"][:i] + [step]}, i + 1
    return None, None


def static_copy(routes):
    """[경로 검색] 결과 → 첫 승차 대기를 배차 추정으로 되돌린 사본 (원본은 건드리지 않는다).

    그 결과는 받은 시각의 실시간 대기를 달고 있다(with_realtime_first_wait 가 static_wait_s 에 원래 값을 남긴다).
    A 는 택시로 정류장에 닿아 원래 경로가 필요하고, 나머지는 이 요청 시각의 실시간으로 다시 매긴다. 경로 대기 합은 add_wait 와
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


def _pick_leg(legs, o, d, vot, compare):
    """대안 중 일반화 비용이 가장 낮은 하나 → (재구성 값, 경로 객체)."""
    scored = [(compare.reconstruct(r, o, d), r) for r in legs]
    return min(scored, key=lambda x: compare.gc_s(x[0]["time_s"], x[0]["fare"], vot))


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


async def _verify(st, transit_payload, o, d, cand, vot, anchors, compare, base_routes, live_routes=None, at=None):
    """후보 하나를 카카오로 확인 → 화면에 줄 항목. 구간을 못 얻으면 None(쿼터는 이미 썼다).

    콜 수가 유형마다 다르다 — A 는 자동차 + 대중교통, (a) 의 B 는 앞구간이 기준 경로에 있어 자동차만,
    (b) 의 B 는 기준 경로에 없는 노선이라 대중교통(O → 앵커) 1콜을 더 쓴다. D 는 공백 한 구간만 바꿔 자동차 1콜이다.
    D 도 (a) 섭동이 낸 후보라 strategy 가 "a" 다 — 유형으로 먼저 갈라야 (a) 의 B 로 잘못 떨어지지 않는다.
    `live_routes` 는 D · C · (a) 의 B 가 만들어진 경로(첫 대기를 실시간으로 바꾼 사본) — 화면의 구간 대기도 그 값이어야
    시간과 맞는다. C 는 algo/cli 의 _verify_detour 와 같은 식이다. `at` 은 환승 인정 시간 판정(심야 등)의 기준 시각.
    """
    pt = (cand["lon"], cand["lat"])
    anchor = {"id": cand["anchor"], "name": cand["name"], "lat": cand["lat"], "lon": cand["lon"],
              "kind": cand["kind"], "line_group": cand.get("line_group"), "sgg_nm": cand.get("sgg_nm")}
    detour = None
    fare_info = None
    if cand["hybrid"] == "C":
        frm = (cand["from_lon"], cand["from_lat"])
        head, taxi_at = _detour_route(live_routes or base_routes, cand, anchors, o)
        anchor.update(from_lat=cand["from_lat"], from_lon=cand["from_lon"])
        if cand["via"] == "taxi_to_d":
            car = normalize_car(await st.kakao.car(frm[0], frm[1], d[0], d[1]))
            taxi_counted_s = anchors.taxi_plan_s(car["duration_s"], connect=False)
            time_s = cand["head_s"] + taxi_counted_s
            fare_info = anchors.candidate_fare(cand, taxi_counted_s, at=at)
            fare = (car["fare"]["taxi"] or 0) + (car["fare"]["toll"] or 0) + fare_info["value"]
            route, wait_s, transit_time_s = head, None, cand["head_s"]
        else:
            car_raw, leg = await asyncio.gather(st.kakao.car(frm[0], frm[1], pt[0], pt[1]),
                                                transit_payload(st, pt[0], pt[1], d[0], d[1]))
            car = normalize_car(car_raw)
            if not leg["routes"]:
                return None
            taxi_counted_s = anchors.taxi_plan_s(car["duration_s"], connect=True)
            options = [(compare.reconstruct(r, pt, d), r,
                        anchors.candidate_fare(cand, taxi_counted_s, tail=r, at=at)) for r in leg["routes"]]
            best, leg_route, fare_info = min(options, key=lambda x: compare.gc_s(x[0]["time_s"], x[2]["value"], vot))
            fare = (car["fare"]["taxi"] or 0) + (car["fare"]["toll"] or 0) + fare_info["value"]
            time_s = cand["head_s"] + taxi_counted_s + best["time_s"]
            route = {**leg_route, "steps": (head["steps"] if head else []) + leg_route["steps"]}
            taxi_at = len(head["steps"]) if head else 0
            wait_s, transit_time_s = best["wait_s"], cand["head_s"] + best["time_s"]
        detour = {"via": cand["via"], "point_name": cand["point_name"], "loop_s": cand["loop_s"], "taxi_at": taxi_at}
    elif cand["hybrid"] == "A":
        car_raw, leg = await asyncio.gather(st.kakao.car(o[0], o[1], pt[0], pt[1]),
                                            transit_payload(st, pt[0], pt[1], d[0], d[1]))
        car = normalize_car(car_raw)
        if not leg["routes"]:
            return None
        best, route = _pick_leg(leg["routes"], pt, d, vot, compare)
        # 택시 도착 시각은 추정치다 — 버퍼를 넘겨야 환승이 성립한다 (method.md §7.5, algo 와 한 벌)
        taxi_counted_s = anchors.taxi_connect_s(car["duration_s"])
        time_s = taxi_counted_s + best["time_s"]
        fare = (car["fare"]["taxi"] or 0) + (car["fare"]["toll"] or 0) + (best["fare"] or 0)
        wait_s, transit_time_s = best["wait_s"], best["time_s"]
    elif cand["hybrid"] == "D":
        car = normalize_car(await st.kakao.car(cand["from_lon"], cand["from_lat"], pt[0], pt[1]))
        eta = car["duration_s"] or 0
        # 공백을 빼고 택시(호출 대기 포함)를 넣는다. 내린 뒤 다시 차를 타면(resume_ride) 연결 버퍼도 씌운다 (algo 와 한 벌)
        taxi_counted_s = anchors.taxi_plan_s(eta, connect=cand["resume_ride"])
        time_s = cand["base_time_s"] - cand["gap_s"] + taxi_counted_s
        fare_info = anchors.candidate_fare(cand, taxi_counted_s, at=at)
        fare = (car["fare"]["taxi"] or 0) + (car["fare"]["toll"] or 0) + fare_info["value"]
        route, gap_at = _gap_route(live_routes or base_routes, cand)
        wait_s = cand["wait_s"]
        transit_time_s = cand["ride_s"] + (wait_s or 0) + cand["walk_s"]
        anchor.update(from_lat=cand["from_lat"], from_lon=cand["from_lon"])
    elif cand["strategy"] == "b":
        car_raw, leg = await asyncio.gather(st.kakao.car(pt[0], pt[1], d[0], d[1]),
                                            transit_payload(st, o[0], o[1], pt[0], pt[1]))
        car = normalize_car(car_raw)
        if not leg["routes"]:
            return None
        best, route = _pick_leg(leg["routes"], o, pt, vot, compare)
        transit_time_s = best["time_s"]
        taxi_counted_s = anchors.taxi_plan_s(car["duration_s"], connect=False)   # 호출 대기는 있고 연결 버퍼는 없다
        time_s = transit_time_s + taxi_counted_s
        fare = (car["fare"]["taxi"] or 0) + (car["fare"]["toll"] or 0) + (best["fare"] or 0)
        wait_s = best["wait_s"]
    else:
        car = normalize_car(await st.kakao.car(pt[0], pt[1], d[0], d[1]))
        route = _cut_route(live_routes or base_routes, cand)
        wait_s = cand["wait_s"]
        transit_time_s = cand["ride_s"] + (wait_s or 0) + cand["walk_s"]
        taxi_counted_s = anchors.taxi_plan_s(car["duration_s"], connect=False)
        time_s = transit_time_s + taxi_counted_s
        fare_info = anchors.candidate_fare(cand, taxi_counted_s, at=at)
        fare = (car["fare"]["taxi"] or 0) + (car["fare"]["toll"] or 0) + fare_info["value"]
    if not car["path"]:
        return None   # 택시 구간을 못 얻으면 시간도 지도도 반쪽이다
    return {"hybrid": cand["hybrid"], "strategy": cand["strategy"], "anchor": anchor,
            "ride_name": cand.get("ride_name"), "headway_m": cand.get("headway_m"),
            "time_s": time_s, "fare": fare, "fare_info": fare_info, "wait_s": wait_s, "transit_time_s": transit_time_s,
            "taxi_counted_s": taxi_counted_s,   # 총 시간에 넣은 택시 몫 (호출 대기 · 연결 여유 포함)
            "detour": detour,
            "gap": ({"kind": cand["gap"], "removed_s": cand["gap_s"], "resume_ride": cand["resume_ride"], "at": gap_at}
                    if cand["hybrid"] == "D" else None),
            "taxi": car, "transit": route}


async def plan(st, transit_payload, o, d, *, arrivals=None, base_routes=None, top=5, t_max_s=1800, now=None):
    """앵커 후보 생성 → 상위 top 개 온라인 확인 → 기준 경로와 함께 견준 결과.

    `base_routes` 를 주면 기준 경로를 새로 부르지 않고 그것(같은 출발·도착의 [경로 검색] 결과)을 쓴다.

    선호는 시간 중시(VOT time)로 고정한다 — 기준선도 최소 시간 대중교통 경로다.

    `arrivals(st, kind, stop_id)` 를 주면 첫 승차 대기를 실시간으로 바꾼다. 기준 시각(`now`)을 준 요청은 '지금' 의
    도착정보가 그 시각과 맞지 않아 쓰지 않는다.
    """
    anchors, compare = _algo()
    if st.anchor_tables is None or st.stops is None or st.routes is None or st.lines is None:
        raise ApiError("data_not_built", "하이브리드 경로에 필요한 정제 데이터가 없습니다.",
                       action="data 파이프라인을 먼저 실행하세요 (data/README.md).", status=503)
    pref = "time"
    vot = anchors.VOT[pref]
    at = (now or datetime.now(KST)).replace(tzinfo=None)
    top = max(1, min(int(top), MAX_TOP))

    if base_routes is None:
        base_norm = await transit_payload(st, o[0], o[1], d[0], d[1])   # 기준선 + (a) 후보의 입력
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

    # (b) 는 두 방향을 따로 낸다 — A 는 택시로 타러 가는 쪽, B 는 내려서 택시로 마무리하는 쪽(§3.2 대칭판)
    (b_a, diag_a), (b_b, diag_b) = await asyncio.gather(*(
        asyncio.to_thread(anchors.propose_backtrack, st.stops, st.routes, st.lines, o, d,
                          vot=vot, hybrid=h, at=at, headway=st.headway, phys=st.anchor_tables.phys,
                          hours=st.anchor_tables.hours, t_max_s=t_max_s)
        for h in ("A", "B")))
    # (a) 섭동은 A·B 에 넘길 경로가 다르다 — A 는 택시로 정류장에 닿아 원래 경로, B 는 출발지에서 걸어서 첫 정류장에
    # 닿아 실시간 사본. 실시간 사본 하나만 넘기면 첫 구간에서 타는 A 까지 걸어서 닿는다고 보고 대기를 매긴다
    perturb = dict(vot=vot, sgg_o=_sgg(st.stops, anchors, o), sgg_d=_sgg(st.stops, anchors, d), at=at, t_max_s=t_max_s)
    a_only, perturb_a = anchors.propose_perturb(o, d, base_routes, hybrids=("A",), **perturb)
    b_only, perturb_b = anchors.propose_perturb(o, d, rt_routes, hybrids=("B",), **perturb)
    a_cands = a_only + b_only
    d_cands, d_diag = anchors.propose_gap(o, d, rt_routes, vot=vot, at=at, t_max_s=t_max_s)
    # 병합은 algo 규칙을 그대로 쓴다 — D 는 택시 양끝이 모두 앵커라 도착점만 보면 서로 다른 공백을 하나로 합친다
    picks = anchors.merge_candidates(anchors.top_n(b_a, top), anchors.top_n(b_b, top), a_cands, d_cands, top=top)
    # C — 돌아가기 시작하는 정류장에서 다시 찾는다. 예산을 따로 둬 만든 만큼(정류장 수 × (택시로 목적지 1 + 앵커 수)) 전부 확인한다
    c_cands, c_diag = await asyncio.to_thread(
        anchors.propose_detour, st.stops, st.routes, st.lines, o, d, rt_routes, vot=vot, at=at,
        headway=st.headway, phys=st.anchor_tables.phys, hours=st.anchor_tables.hours, t_max_s=t_max_s)
    if c_cands:
        picks += anchors.merge_candidates(c_cands, top=len(c_cands))

    done = await asyncio.gather(*(_verify(st, transit_payload, o, d, c, vot, anchors, compare, base_routes, rt_routes, at)
                                 for c in picks), return_exceptions=True)
    rows, failed = [], []
    for cand, r in zip(picks, done):
        if isinstance(r, ApiError):
            failed.append({"anchor": cand["name"], "code": r.code, "message": r.message})
        elif isinstance(r, BaseException):
            raise r
        elif r is not None:
            rows.append(r)
    if not rows and failed:
        raise ApiError(failed[0]["code"], failed[0]["message"],
                       action="잠시 뒤 다시 시도하거나 확인할 앵커 수를 줄이세요.", status=503)

    verified = len(rows)
    # 택시를 쓰는 최소 기준 — 같은 잣대의 기준선(최소 시간 경로)보다 5분 이상 빠르지 않은 후보는 보여 주지 않는다 (algo/cli 와 같다)
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
    return {"pref": pref, "vot": vot, "at": at.isoformat(timespec="minutes"), "t_max_s": t_max_s,
            "baseline": baseline, "routes": rows, "failed": failed,
            "diag": {"backtrack_a": diag_a, "backtrack_b": diag_b, "perturb_a": perturb_a, "perturb_b": perturb_b,
                     "gap": d_diag, "detour": c_diag,
                     "too_little_saving": verified - len(rows), "min_saving_s": anchors.TAXI_MIN_SAVING_S,
                     "realtime": rt_diag,   # 첫 승차 대기 — realtime · realtime+headway · no_arrivals … (안 썼으면 None)
                     "base_source": base_source,   # called = 기준 경로를 새로 부름 · reused = [경로 검색] 결과를 다시 씀
                     "picked": len(picks), "verified": verified}}
