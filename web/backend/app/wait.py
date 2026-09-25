"""배차간격으로 차를 기다리는 시간을 추정한다 (순수 함수 — 저장·로그 없음).

카카오 대중교통 응답의 소요 시간에는 **대기가 전혀 들어 있지 않다**(실측: 차내 시간 + 환승 도보 + 양 끝 도보만).
그래서 승차 구간마다 그 노선의 배차로 대기를 추정해 더한다. 도착 시각을 모르고 승객이 아무 때나 온다고 보면
대기의 기댓값은 배차의 절반이다. 한 구간에서 탈 수 있는 노선이 여럿이면(카카오 `vehicles` 가 여럿) 먼저 오는
차를 타므로 **빈도를 더한다** — 유효 배차 = 1 / Σ(1/hᵢ), 10분·15분이면 6분(대기 3분)이다.

배차를 모르는 승차 구간은 기본 대기 15분을 적용하고 `wait_source=default` 로 표시한다.
지하철 배차는 시간대별이라 각 구간을 **탈 시각**(출발 시각 + 앞 구간들의 이동·대기)으로 조회한다.
"""
from datetime import timedelta
from statistics import fmean

DAY_TYPE = ("월", "화", "수", "목", "금", "토요일", "일요일")   # weekday() 순서 — 공휴일은 가리지 않는다
WEEKDAY = "평일"
RAIL_HOURS = range(5, 26)   # 표의 시간대 (25 = 다음날 1시). 그 밖(새벽 2~4시)은 값이 없다


DEFAULT_WAIT_S = 900.0   # 배차를 모르는 승차 구간마다 적용하는 기본 대기 15분


def apply_wait_defaults(route):
    """미확인 승차 대기에 기본값과 출처를 채우고 경로 합계를 갱신한다."""
    rides = [s for s in route["steps"] if s["type"] in ("BUS", "SUBWAY")]
    for step in rides:
        if step.get("wait_s") is None:
            step["wait_s"] = DEFAULT_WAIT_S
            step["wait_source"] = "default"
    route["wait_s"] = sum(s["wait_s"] for s in rides)
    total = route.get("total_time_s")
    route["total_with_wait_s"] = total + route["wait_s"] if total is not None else None


def day_type(now):
    """그날의 요일 유형 — 배차표의 `day_type` 값. 공휴일은 달력이 없어 가리지 않는다(평일로 본다)."""
    d = DAY_TYPE[now.weekday()]
    return d if d.endswith("요일") else WEEKDAY


def rail_hour(at):
    """도시철도 표의 시간대 — 자정 넘긴 0·1시는 전날의 24·25시다. 표에 없는 시간대면 None."""
    h = at.hour + 24 if at.hour < 2 else at.hour
    return h if h in RAIL_HOURS else None


def _effective_min(headways):
    """여러 노선을 함께 기다릴 때의 유효 배차(분) — 빈도의 합. 하나면 그 값 그대로."""
    return 1 / sum(1 / h for h in headways)


def _route_headway(db, rid, day):
    """노선 하나의 배차(분). 그날 요일 유형 값이 없으면 평일 값으로 돌아간다
    (경기 원천은 주말·공휴일 행이 절반 넘게 비어 있다)."""
    h = db.bus(rid, day)
    return h if h is not None or day == WEEKDAY else db.bus(rid, WEEKDAY)


def _bus_headway(db, route_ids, day, routes=None):
    """구간에서 탈 수 있는 노선들의 유효 배차(분). 배차를 아는 노선이 하나도 없으면 None.

    **이름이 같은 후보는 한 노선으로 본다** — 같은 번호가 시·군마다 있어(마을 '5' 21개) 매칭이 하나로
    좁히지 못하고 남긴 것이지 함께 기다릴 수 있는 다른 노선이 아니다. 어느 쪽인지 모르므로 후보들의 평균을 쓴다.
    빈도를 더하는 것은 카카오가 한 구간에 여러 노선을 준 경우(이름이 서로 다르다)뿐이다.
    배차를 모르는 노선은 빼고 센다 — 그만큼 대기를 길게 잡는 쪽이라 과소 추정이 되지 않는다.
    """
    by_name = {}
    for rid in route_ids:
        h = _route_headway(db, rid, day)
        if h is not None:
            by_name.setdefault(routes.name_of(rid) if routes else rid, []).append(h)
    hs = [fmean(v) for v in by_name.values()]
    return _effective_min(hs) if hs else None


def _board_station(step):
    """지하철 구간에서 탄 역의 id — 매칭이 승차역을 정하지 못했으면 None."""
    res = step.get("resolution") or {}
    chosen = (res.get("board") or {}).get("chosen")
    return chosen.get("id") if chosen else None


def _step_headway(step, db, day, at, routes):
    """구간의 배차(분) — 버스는 노선들의 유효 배차, 지하철은 탄 역·노선군의 그 시간대 배차."""
    if db is None:
        return None
    if step["type"] == "BUS":
        return _bus_headway(db, step.get("route_ids") or (), day, routes)
    if step["type"] == "SUBWAY":
        hour = rail_hour(at)
        station = _board_station(step)
        if hour is None or station is None or not step.get("line_group"):
            return None
        return db.rail(station, step["line_group"], hour)
    return None


def add_wait(transit_routes, db, now, routes=None):
    """경로마다 구간 대기(step.wait_s·step.headway_m)와 합(route.wait_s·route.total_with_wait_s)을 채운다.
    `routes`(RoutesDB)를 주면 같은 이름의 노선 후보를 한 노선으로 묶는다.

    → 기준 {day_type, hour} (화면에 "무슨 요일 · 몇 시 기준" 으로 보이는 값).
    """
    day = day_type(now)
    for route in transit_routes:
        at = now              # 이 구간을 탈 시각 (앞 구간들의 이동·대기를 더해 온 값)
        for step in route["steps"]:
            step["wait_s"] = None
            step["headway_m"] = None
            step.pop("wait_source", None)
            ride = step["type"] in ("BUS", "SUBWAY")
            if ride:
                h = _step_headway(step, db, day, at, routes)
                if h is None:
                    step["wait_s"] = DEFAULT_WAIT_S
                    step["wait_source"] = "default"
                else:
                    step["headway_m"] = round(h, 1)
                    step["wait_s"] = round(h * 60 / 2)
                at += timedelta(seconds=step["wait_s"])
            at += timedelta(seconds=step.get("time_s") or 0)
        apply_wait_defaults(route)
    return {"day_type": day, "hour": now.hour}
