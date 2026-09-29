"""요청 동안만 보관하는 개발자용 후보 처리 기록. 외부 API·파일 저장 없음."""
import math


STAGES = ["후보 생성", "노선별 제한", "병합·검증 예산", "경로 검증", "중복 제외", "시간 절약 검사", "최종 결과"]
LIMIT = 500


class HybridTrace:
    def __init__(self, origin, destination, vot, baseline):
        self.origin, self.destination, self.vot = origin, destination, vot
        self.baseline = {k: baseline.get(k) for k in ("time_s", "fare")}
        self.buffers = {}
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
            self.records[key] = {
                "id": "c%d" % (len(self.records) + 1), "name": candidate.get("name", ""),
                "source": source, "type": kind, "mode": candidate.get("link_mode", "TAXI"),
                "estimated_score": score * self.vot / 60 if score is not None and math.isfinite(score) else None,
                "connector": [start, end], "anchors": anchors, "stage": len(STAGES) - 1,
                "reason": "최종 결과", "related": None, "verified": None,
            }

    def reject(self, candidate, stage, reason, winner=None):
        row = self.records[id(candidate)]
        row.update(stage=STAGES.index(stage), reason=reason,
                   related=self.records[id(winner)]["id"] if winner is not None else None)

    def verified(self, candidate, result):
        row = self.records[id(candidate)]
        row["verified"] = {"time_s": result["time_s"], "fare": result["fare"],
                           "score": result["fare"] + result["time_s"] / 60 * self.vot}
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
