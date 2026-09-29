"""요청 동안만 보관하는 개발자용 후보 처리 기록. 외부 API·파일 저장 없음."""
import math


STAGES = ["후보 생성", "노선별 제한", "병합·검증 예산", "경로 검증", "시간 절약 검사", "최종 결과"]
LIMIT = 500


def _itinerary(candidate, steps):
    """구간 이름만 요약한다. 좌표·시간·요금은 경로 설명에 복제하지 않는다."""
    kind = candidate["hybrid"]
    names = candidate.get("name", "앵커").split("→")
    mode = "도보" if candidate.get("link_mode") == "WALKING" else "택시"
    start = "출발지" if kind == "A" else names[0]
    end = "목적지" if kind == "B" or candidate.get("via") == "taxi_to_d" else names[-1]
    connection = f"{start}에서 {end}까지 {mode}"
    if not steps:
        if kind == "A":
            return ["출발지", connection, "대중교통 이용", "도보", "목적지 도착"]
        if kind == "B" or candidate.get("via") == "taxi_to_d":
            return ["출발지", "도보", f"대중교통으로 {start}까지 이동", connection, "목적지 도착"]
        return ["출발지", f"대중교통으로 {start}까지 이동", connection, "대중교통 이용", "도보", "목적지 도착"]
    parts = ["출발지"]
    if steps[0]["type"] in ("BUS", "SUBWAY"):
        parts.append("도보")
    for step in steps:
        if step["type"] == "TAXI":
            parts.append(connection)
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
    if steps[-1]["type"] in ("BUS", "SUBWAY"):
        parts.append("도보")
    return parts + ["목적지 도착"]


def _geometry(steps, connector, origin, destination, mode, group_color=None):
    segments = []
    previous = list(origin)
    for step in steps or []:
        kind = step["type"]
        if kind == "TAXI":
            path, kind = connector, mode
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
    if not segments:
        segments = [{"type": "TRANSIT", "path": [list(origin), connector[0]]},
                    {"type": mode, "path": connector},
                    {"type": "TRANSIT", "path": [connector[-1], list(destination)]}]
    elif previous != list(destination):
        segments.append({"type": "WALKING", "path": [previous, list(destination)]})
    return segments


class HybridTrace:
    def __init__(self, origin, destination, vot, baseline, *, group_color=None):
        self.origin, self.destination, self.vot = origin, destination, vot
        self.baseline = {k: baseline.get(k) for k in ("time_s", "fare")}
        self.buffers = {}
        self.group_color = group_color
        self.records, self.objects, self.generators = {}, {}, {}

    def add(self, candidates, source, diagnostics):
        self.generators[source] = diagnostics
        for candidate in candidates:
            key = id(candidate)
            if key in self.records:
                continue
            self.objects[key] = candidate  # id 재사용 방지
            anchor = [candidate["lon"], candidate["lat"]]
            kind = candidate["hybrid"]
            start = list(self.origin) if kind == "A" else (
                [candidate["from_lon"], candidate["from_lat"]] if kind in ("C", "D") else anchor)
            end = list(self.destination) if kind == "B" else anchor
            if kind in ("A", "B"):
                anchors = [{"id": candidate.get("anchor"), "name": candidate.get("name", ""),
                            "lon": anchor[0], "lat": anchor[1]}]
            else:
                ids = candidate.get("anchor", "").split(">")
                names = candidate.get("name", "").split("→")
                anchors = [{"id": ids[0], "name": names[0], "lon": start[0], "lat": start[1]}]
                if candidate.get("via") != "taxi_to_d":
                    anchors.append({"id": ids[-1], "name": names[-1], "lon": end[0], "lat": end[1]})
            score = candidate.get("score_s")
            steps = candidate.get("fare_steps")
            if steps is None and candidate.get("fare_step"):
                ride = candidate["fare_step"]
                steps = ([{"type": "TAXI"}, ride] if kind == "A" else [ride, {"type": "TAXI"}])
            self.records[key] = {
                "id": "c%d" % (len(self.records) + 1), "name": candidate.get("name", ""),
                "source": source, "type": kind, "mode": candidate.get("link_mode", "TAXI"),
                "estimated_score": score * self.vot / 60 if score is not None and math.isfinite(score) else None,
                "connector": [start, end], "anchors": anchors, "stage": len(STAGES) - 1,
                "reason": "최종 결과", "related": None, "verified": None,
                "itinerary": _itinerary(candidate, steps),
                "geometry": _geometry(steps, [start, end], self.origin, self.destination,
                                      candidate.get("link_mode", "TAXI"), self.group_color),
            }

    def reject(self, candidate, stage, reason, winner=None):
        row = self.records[id(candidate)]
        row.update(stage=STAGES.index(stage), reason=reason,
                   related=self.records[id(winner)]["id"] if winner is not None else None)

    def verified(self, candidate, result):
        row = self.records[id(candidate)]
        row["verified"] = {"time_s": result["time_s"], "fare": result["fare"],
                           "score": result["fare"] + result["time_s"] / 60 * self.vot}
        if result.get("transit") is not None:
            steps = list(result["transit"].get("steps", []))
            kind = candidate["hybrid"]
            at = (0 if kind == "A" else len(steps) if kind == "B" else
                  (result.get("detour") or {}).get("taxi_at") if kind == "C" else
                  (result.get("gap") or {}).get("at"))
            if at is not None:
                steps.insert(at, {"type": "TAXI"})
                row["verified"]["itinerary"] = _itinerary(candidate, steps)
                row["verified"]["geometry"] = _geometry(steps, row["connector"], self.origin,
                                                        self.destination, row["mode"], self.group_color)
        return row["id"]

    def finish(self, survivors):
        indices = {row["trace_id"]: i for i, row in enumerate(survivors)}
        for record in self.records.values():
            record["result_index"] = indices.get(record["id"])
            if record["result_index"] is not None:
                record["reason"] = "중복·시간 조건을 통과해 최종 결과에 포함됐습니다."
                record["pareto"] = survivors[record["result_index"]]["pareto"]
        records = list(self.records.values())
        # 표에는 전체 기록을 보내고 지도 표시만 제한한다.
        priority = sorted(records, key=lambda r: (r["stage"] < 3, r["estimated_score"] if r["estimated_score"] is not None else math.inf))
        map_limit = max(LIMIT, sum(r["stage"] >= 3 for r in records))
        return {"vot": self.vot, "baseline": self.baseline, "origin": self.origin, "destination": self.destination,
                "total": len(records), "omitted": max(0, len(records) - map_limit),
                "map_limit": map_limit, "candidates": priority,
                "generation": self.generators, "buffers": self.buffers,
                "stages": [{"name": name, "entered": sum(r["stage"] >= i for r in records),
                            "removed": sum(r["stage"] == i for r in records) if i < len(STAGES) - 1 else 0,
                            "remaining": sum(r["stage"] > i or (i == len(STAGES) - 1 and r["result_index"] is not None) for r in records)}
                           for i, name in enumerate(STAGES)]}
