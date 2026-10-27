"""Exact static-search checks, with an independent exhaustive-path oracle."""

import math
import random
import unittest

from algo.pareto_search import Edge, pareto_journeys


def exhaustive_costs(edges, origin, destination):
    """Enumerate simple expanded-state paths, without production search helpers.

    A repeated (node, taxi_used) state encloses a nonnegative-cost cycle, so
    removing that cycle cannot lose a nondominated cost pair.
    """
    outgoing = {}
    for edge in edges:
        outgoing.setdefault(edge.start, []).append(edge)
    costs = []

    def visit(node, used, time_s, fare, visited):
        if node == destination:
            costs.append((time_s, fare))
            return
        for edge in outgoing.get(node, []):
            if used and edge.mode == "taxi":
                continue
            next_used = used or edge.mode == "taxi"
            state = (edge.end, next_used)
            if state not in visited:
                visit(edge.end, next_used, time_s + edge.time_s,
                      fare + edge.fare, visited | {state})

    visit(origin, False, 0, 0, {(origin, False)})
    return costs


def nondominated(costs):
    unique = set(costs)
    return {pair for pair in unique if not any(
        other != pair and other[0] <= pair[0] and other[1] <= pair[1]
        for other in unique
    )}


def random_graphs(seed, count, cyclic=False):
    rng = random.Random(seed)
    for _ in range(count):
        nodes = [str(i) for i in range(rng.randint(3, 5 if cyclic else 8))]
        edges = []
        for i, start in enumerate(nodes):
            for j, end in enumerate(nodes):
                if (i == j if cyclic else i >= j) or rng.random() > 0.55:
                    continue
                for _ in range(rng.randint(1, 2)):
                    edges.append(Edge(start, end,
                                      rng.choice(("walk", "transit", "taxi")),
                                      rng.randint(0, 12), rng.randint(0, 12)))
        yield edges, nodes[0], nodes[-1]


class ParetoSearchTests(unittest.TestCase):
    def assert_matches_oracle(self, edges, origin="O", destination="D"):
        journeys = pareto_journeys(edges, origin, destination)
        actual = {(journey.time_s, journey.fare) for journey in journeys}
        self.assertEqual(actual, nondominated(exhaustive_costs(
            edges, origin, destination)))
        self.assertEqual(len(journeys), len(actual), "one path per cost pair")
        for journey in journeys:
            self.assertIsInstance(journey.edges, tuple)
            node = origin
            used = 0
            for edge in journey.edges:
                self.assertIn(edge, edges)
                self.assertEqual(edge.start, node)
                node = edge.end
                used += edge.mode == "taxi"
            self.assertEqual(node, destination)
            self.assertLessEqual(used, 1)
            self.assertEqual(journey.time_s, sum(e.time_s for e in journey.edges))
            self.assertEqual(journey.fare, sum(e.fare for e in journey.edges))
        return actual

    def test_anchor_shapes_and_candidates_without_trigger_thresholds(self):
        scenarios = {
            "A first leg": (
                [Edge("O", "A", "transit", 30, 2),
                 Edge("A", "D", "transit", 10),
                 Edge("O", "A", "taxi", 5, 10)],
                {(40, 2), (15, 10)}),
            "B final leg": (
                [Edge("O", "A", "transit", 10, 2),
                 Edge("A", "D", "transit", 30),
                 Edge("A", "D", "taxi", 5, 10)],
                {(40, 2), (15, 12)}),
            "C bypass detour and find another suffix": (
                [Edge("O", "U", "transit", 5, 2),
                 Edge("U", "X", "transit", 40),
                 Edge("X", "D", "transit", 5),
                 Edge("U", "V", "taxi", 3, 8),
                 Edge("V", "D", "transit", 7, 2)],
                {(50, 2), (15, 12)}),
            "D replace long wait and transfer": (
                [Edge("O", "U", "transit", 5, 2),
                 Edge("U", "V", "transit", 40, 1),
                 Edge("V", "D", "transit", 5),
                 Edge("U", "V", "taxi", 10, 10)],
                {(50, 3), (20, 12)}),
            "short wait and long ride": (
                [Edge("O", "U", "transit", 300, 2),
                 # Two minutes waiting plus sixty minutes riding.
                 Edge("U", "V", "transit", 120 + 3600, 1),
                 Edge("V", "D", "transit", 300),
                 Edge("U", "V", "taxi", 984, 10)],
                {(4320, 3), (1584, 12)}),
            "skip several transfers": (
                [Edge("O", "U", "transit", 5, 2),
                 Edge("U", "M", "transit", 20, 1),
                 Edge("M", "N", "transit", 20, 1),
                 Edge("N", "V", "transit", 20, 1),
                 Edge("V", "D", "transit", 5),
                 Edge("U", "V", "taxi", 10, 10)],
                {(70, 5), (20, 12)}),
            "connect routes outside the best transit baseline": (
                [Edge("O", "A", "transit", 10, 1),
                 Edge("A", "D", "transit", 90),
                 Edge("O", "B", "transit", 80, 1),
                 Edge("B", "D", "transit", 10),
                 Edge("A", "B", "taxi", 5, 8)],
                {(90, 1), (25, 9)}),
            "empty prefix and suffix": (
                [Edge("O", "D", "taxi", 5, 10),
                 Edge("O", "D", "transit", 30, 2)],
                {(5, 10), (30, 2)}),
        }
        for name, (edges, expected) in scenarios.items():
            with self.subTest(name=name):
                self.assertEqual(self.assert_matches_oracle(edges), expected)

    def test_keeps_tradeoffs_that_no_positive_weighted_sum_selects(self):
        edges = [Edge("O", "D", "transit", 1, 10),
                 Edge("O", "D", "transit", 6, 6),
                 Edge("O", "D", "transit", 10, 1),
                 Edge("O", "D", "transit", 11, 11)]
        self.assertEqual(self.assert_matches_oracle(edges),
                         {(1, 10), (6, 6), (10, 1)})

    def test_rejects_second_taxi_even_when_it_would_be_faster(self):
        edges = [Edge("O", "A", "taxi", 1, 1),
                 Edge("A", "D", "taxi", 1, 1),
                 Edge("O", "A", "transit", 10),
                 Edge("A", "D", "transit", 10)]
        self.assertEqual(self.assert_matches_oracle(edges), {(11, 1), (20, 0)})

    def test_labels_with_different_taxi_availability_are_not_merged(self):
        edges = [Edge("O", "A", "taxi", 1),
                 Edge("O", "A", "transit", 5, 1),
                 Edge("A", "D", "taxi", 1),
                 Edge("A", "D", "transit", 20)]
        self.assertEqual(self.assert_matches_oracle(edges), {(6, 1), (21, 0)})

    def test_zero_cost_cycles_and_taxi_self_loop_terminate(self):
        edges = [Edge("O", "A", "walk", 0),
                 Edge("A", "O", "walk", 0),
                 Edge("A", "B", "transit", 2, 1),
                 Edge("B", "A", "transit", 0),
                 Edge("B", "D", "transit", 2),
                 Edge("A", "A", "taxi", 0),
                 Edge("A", "D", "taxi", 1, 2)]
        self.assertEqual(self.assert_matches_oracle(edges), {(4, 1), (1, 2)})

    def test_equal_parallel_edges_and_cost_pairs_have_one_representative(self):
        edges = [Edge("O", "A", "walk", 1),
                 Edge("O", "A", "walk", 1),
                 Edge("O", "A", "transit", 1),
                 Edge("A", "D", "transit", 2, 2),
                 Edge("O", "D", "taxi", 3, 2)]
        self.assertEqual(self.assert_matches_oracle(edges), {(3, 2)})
        self.assertEqual(pareto_journeys(edges, "O", "D")[0].edges, (edges[0], edges[3]))

    def test_unreachable_and_empty_graphs(self):
        for edges in ([], [Edge("O", "A", "walk", 1)]):
            with self.subTest(edges=edges):
                self.assertEqual(self.assert_matches_oracle(edges), set())
        self.assertEqual(self.assert_matches_oracle([], "O", "O"), {(0, 0)})
        self.assertEqual(self.assert_matches_oracle(
            [Edge("O", "O", "taxi", 0)], "O", "O"), {(0, 0)})
        self.assertEqual(pareto_journeys([], "O", "O")[0].edges, ())

    def test_fractional_costs(self):
        edges = [Edge("O", "A", "walk", 0.5, 0.25),
                 Edge("A", "D", "transit", 1.25, 0.5),
                 Edge("O", "D", "taxi", 0.75, 1.5)]
        self.assertEqual(self.assert_matches_oracle(edges),
                         {(1.75, 0.75), (0.75, 1.5)})

    def test_fixed_seed_200_dags_against_complete_enumeration(self):
        for index, (edges, origin, destination) in enumerate(
                random_graphs(20261010, 200)):
            with self.subTest(graph=index):
                self.assert_matches_oracle(edges, origin, destination)

    def test_fixed_seed_small_cyclic_graphs_against_complete_enumeration(self):
        for index, (edges, origin, destination) in enumerate(
                random_graphs(20261011, 40, cyclic=True)):
            with self.subTest(graph=index):
                self.assert_matches_oracle(edges, origin, destination)

    def test_invalid_costs_are_rejected(self):
        for field in ("time_s", "fare"):
            for value in (-1, math.inf, -math.inf, math.nan):
                with self.subTest(field=field, value=value):
                    costs = {"time_s": 1, "fare": 1}
                    costs[field] = value
                    with self.assertRaises(ValueError):
                        edge = Edge("O", "D", "transit", **costs)
                        pareto_journeys([edge], "O", "D")

    def test_unknown_modes_are_rejected(self):
        for mode in ("car", "Taxi", "", None):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    edge = Edge("O", "D", mode, 1)
                    pareto_journeys([edge], "O", "D")

    def test_accumulated_float_overflow_is_rejected(self):
        for costs in ({"time_s": 1e308}, {"time_s": 1, "fare": 1e308}):
            with self.subTest(costs=costs):
                edges = [Edge("O", "A", "transit", **costs),
                         Edge("A", "D", "transit", **costs)]
                with self.assertRaises(ValueError):
                    pareto_journeys(edges, "O", "D")


if __name__ == "__main__":
    unittest.main()
