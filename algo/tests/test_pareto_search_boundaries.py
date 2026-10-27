"""정적 모델의 경계 — 시간표·운임 이력을 노드로 펼친 예. 실제 OD/API 검증이 아니다."""
import unittest

from algo.pareto_search import Edge, pareto_journeys


class ExpandedStateTest(unittest.TestCase):
    """시각·운임을 자동 계산하지 않고, 다른 후속 조건을 별도 노드로 표현한 예."""

    def test_departure_events_keep_early_expensive_arrival(self):
        # u에 10분 도착하면 12분 출발편, 15분 도착하면 30분 출발편을 탄다.
        # 각 차량의 차내 시간은 10분이다.
        edges = [Edge("O", "u@10", "transit", 10 * 60, 1000),
                 Edge("O", "u@15", "transit", 15 * 60, 500),
                 Edge("u@10", "departure@12", "walk", 2 * 60),
                 Edge("u@15", "departure@30", "walk", 15 * 60),
                 Edge("departure@12", "D", "transit", 10 * 60),
                 Edge("departure@30", "D", "transit", 10 * 60)]
        journeys = pareto_journeys(edges, "O", "D")
        self.assertEqual([(j.time_s, j.fare) for j in journeys], [(22 * 60, 1000), (40 * 60, 500)])
        self.assertEqual([[e.end for e in j.edges] for j in journeys],
                         [["u@10", "departure@12", "D"], ["u@15", "departure@30", "D"]])

    def test_transfer_expiry_keeps_initially_slower_costlier_arrival(self):
        # 동일 u라도 환승 만료가 다르다. 20분 출발·30분 도착편의 추가 운임은
        # 12분에 만료된 앞 경로는 2000원, 25분까지 유효한 뒤 경로는 0원이다.
        expired = "u@10:expires12"
        valid = "u@11:expires25"
        edges = [Edge("O", expired, "transit", 10 * 60, 1000),
                 Edge("O", valid, "transit", 11 * 60, 1200),
                 Edge(expired, "D", "transit", 20 * 60, 2000),
                 Edge(valid, "D", "transit", 19 * 60)]
        journeys = pareto_journeys(edges, "O", "D")
        self.assertEqual([(j.time_s, j.fare) for j in journeys], [(30 * 60, 1200)])
        self.assertEqual(journeys[0].edges[0].end, valid)


if __name__ == "__main__":
    unittest.main()
