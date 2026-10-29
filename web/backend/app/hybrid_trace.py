"""요청 동안만 보관하는 개발자용 후보 처리 기록. 외부 API·파일 저장 없음.

후보는 모두 `O → u →(택시) v → D` 꼴이다(algo/search.py). 앞부분이 택시로 시작하면(출발지 → 기준 경로 승차 지점) 택시가 둘이다. 후보가 수만 개일 수 있어 단계 집계는 전체로 내고,
경로 설명·지도 좌표는 보낼 후보(확인을 시도한 것 전부 + 추정 점수 상위 LIMIT 개)에만 만든다.
"""
import math


STAGES = ["후보 생성", "검증 순서·예산", "경로 검증", "시간 절약 검사", "최종 결과"]
VERIFY = STAGES.index("경로 검증")   # 이 단계까지 온 후보는 카카오로 확인한 것이다
LIMIT = 500
SOURCES = {"origin": "출발지", "route": "기준 경로", "line": "노선 더 탐", "taxi_route": "택시 → 기준 경로",
           "taxi_line": "택시 → 노선 더 탐",
           "destination": "목적지"}


def _shape(cand):
    """후보가 앞뒤 부분을 어디서 가져왔는지 — '기준 경로 → 노선 더 탐' 처럼."""
    return "%s → %s" % (SOURCES[cand["head"]["source"]], SOURCES[cand["tail"]["source"]])


def _itinerary(steps, start, end):
    """구간 이름만 요약한다. 좌표·시간·요금은 경로 설명에 복제하지 않는다."""
    parts = ["출발지"]
    if steps and steps[0]["type"] in ("BUS", "SUBWAY"):
        parts.append("도보")
    for step in steps:
        if step["type"] == "TAXI":   # 첫 택시(출발지 → 기준 경로 승차 지점)는 자기 양끝 이름을 들고 온다
            parts.append(f"{step.get('from_name', start)}에서 {step.get('to_name', end)}까지 택시")
        elif step["type"] == "WALKING":
            if parts[-1] != "도보":
                parts.append("도보")
        elif step["type"] in ("BUS", "SUBWAY"):
            stops = step.get("stops") or []
            board = step.get("board_name") or (stops[0] if stops else "승차 지점")
            alight = step.get("alight_name") or (stops[-1] if stops else "하차 지점")
            vehicles = step.get("vehicles") or step.get("fare_routes") or []
            routes = list(dict.fromkeys(v["name"] for v in vehicles if v.get("name")))
            service = " / ".join(routes) or step.get("line_group") or ""
            transit = "버스" if step["type"] == "BUS" else "지하철"
            parts.extend([f"{board}에서 {service + ' ' if service else ''}{transit} 탑승", f"{alight} 하차"])
    if steps and steps[-1]["type"] in ("BUS", "SUBWAY"):
        parts.append("도보")
    return parts + ["목적지 도착"]


def _geometry(steps, connector, origin, destination, group_color=None):
    segments = []
    previous = list(origin)
    for step in steps or []:
        kind = step["type"]
        if kind == "TAXI":
            path = step.get("path") or connector
        else:
            path = [[p["lon"], p["lat"]] for p in step.get("stop_locs") or []
                    if p and p.get("lon") is not None and p.get("lat") is not None]
            if len(path) < 2:
                path = step.get("path") or []
        if len(path) < 2:
            continue
        if previous != list(path[0]):
            segments.append({"type": "WALKING", "path": [previous, path[0]]})
        segment = {"type": kind, "path": path}
        if kind == "BUS":
            segment["vehicles"] = [{"name": v.get("name"), "type": v.get("type")}
                                   for v in step.get("vehicles") or step.get("fare_routes") or []]
        elif kind == "SUBWAY":
            segment["color"] = step.get("color") or (group_color(step.get("line_group")) if group_color else None)
        segments.append(segment)
        previous = list(path[-1])
    if previous != list(destination):
        segments.append({"type": "WALKING", "path": [previous, list(destination)]})
    return segments


class HybridTrace:
    def __init__(self, origin, destination, vot, baseline, candidates, generation, *, group_color=None):
        self.origin, self.destination, self.vot = origin, destination, vot
        self.baseline = {k: baseline.get(k) for k in ("time_s", "fare")}
        self.group_color = group_color
        self.generation = generation
        # 출발지·목적지에서 걸어서 닿는 정류소를 찾은 도보 반경 — 출발 쪽(head)과 도착 쪽(tail)
        self.buffers = {side: generation["line_" + side].get("buffers", {}) for side in ("head", "tail")}
        self.candidates = candidates   # 추정 점수 오름차순
        self.state = {c["id"]: {"stage": len(STAGES) - 1, "reason": "최종 결과", "verified": None} for c in candidates}
        self.by_id = {c["id"]: c for c in candidates}

    def reject(self, candidate, stage, reason):
        self.reject_id(candidate["id"], stage, reason)

    def reject_id(self, cid, stage, reason):
        self.state[cid].update(stage=STAGES.index(stage), reason=reason)

    def _lead(self, head):
        """앞부분이 택시로 시작하면 그 택시 구간 — 출발지에서 기준 경로 승차 지점까지 직선."""
        b = head.get("taxi_from")
        return [{"type": "TAXI", "from_name": "출발지", "to_name": b["name"],
                 "path": [list(self.origin), [b["lon"], b["lat"]]]}] if b else []

    def verified(self, candidate, row):
        steps = list(row["transit"]["steps"])
        steps.insert(row["taxi_at"], {"type": "TAXI"})
        steps = self._lead(candidate["head"]) + steps
        self.state[candidate["id"]]["verified"] = {
            "time_s": row["time_s"], "fare": row["fare"], "score": row["fare"] + row["time_s"] / 60 * self.vot,
            "steps": steps}
        return candidate["id"]

    def _record(self, cand):
        h, t, st = cand["head"], cand["tail"], self.state[cand["id"]]
        u, v = h["point"], t["point"]
        connector = [[u["lon"], u["lat"]], [v["lon"], v["lat"]]]
        steps = self._lead(h) + h["steps"] + [{"type": "TAXI"}] + t["steps"]
        verified = st["verified"]
        if verified is not None:
            vsteps = verified.pop("steps")
            verified.update(itinerary=_itinerary(vsteps, u["name"], v["name"]),
                            geometry=_geometry(vsteps, connector, self.origin, self.destination, self.group_color))
        return {"id": cand["id"], "name": "%s → %s" % (u["name"], v["name"]), "shape": _shape(cand),
                "estimated_score": cand["fare"] + cand["time_s"] / 60 * self.vot,
                "estimated": {"time_s": cand["time_s"], "fare": cand["fare"]},
                "connector": connector,
                "anchors": [{"id": p["id"], "name": p["name"], "lon": p["lon"], "lat": p["lat"]}
                            for p in ([h["taxi_from"]] if h.get("taxi_from") else [])
                            + [x["point"] for x in (h, t) if x["source"] not in ("origin", "destination")]],
                "stage": st["stage"], "reason": st["reason"], "verified": verified,
                "itinerary": _itinerary(steps, u["name"], v["name"]),
                "geometry": _geometry(steps, connector, self.origin, self.destination, self.group_color)}

    def finish(self, survivors):
        indices = {row["trace_id"]: i for i, row in enumerate(survivors)}
        tried = [c for c in self.candidates if self.state[c["id"]]["stage"] >= VERIFY]
        rest = [c for c in self.candidates if self.state[c["id"]]["stage"] < VERIFY][:LIMIT]
        records = [self._record(c) for c in tried + rest]
        for record in records:
            record["result_index"] = indices.get(record["id"])
            if record["result_index"] is not None:
                record["reason"] = "시간 조건을 통과해 최종 결과에 포함됐습니다."
                record["pareto"] = survivors[record["result_index"]]["pareto"]
        records.sort(key=lambda r: (r["stage"] < VERIFY,
                                    r["estimated_score"] if math.isfinite(r["estimated_score"]) else math.inf))
        stages = [s["stage"] for s in self.state.values()]
        final = len(survivors)
        return {"vot": self.vot, "baseline": self.baseline, "origin": self.origin, "destination": self.destination,
                "total": len(stages), "omitted": len(stages) - len(records), "limit": LIMIT, "verify_stage": VERIFY,
                "candidates": records, "generation": self.generation, "buffers": self.buffers,
                "stages": [{"name": name, "entered": sum(s >= i for s in stages),
                            "removed": sum(s == i for s in stages) if i < len(STAGES) - 1 else 0,
                            "remaining": final if i == len(STAGES) - 1 else sum(s > i for s in stages)}
                           for i, name in enumerate(STAGES)]}
