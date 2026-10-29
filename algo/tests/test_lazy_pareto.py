"""Paid observations, resumable budgets, and independent finite-pool oracles."""

import asyncio
from itertools import permutations
import random

import pytest

from algo.lazy_pareto import Candidate, Cost, LazyParetoSearch, QueryUnavailable


def candidate(cid, time_s, fare, queries, lower_bound=Cost(0, 0)):
    return Candidate(cid, Cost(time_s, fare), tuple(queries), lower_bound)


def make_search(candidates, outcomes, baseline=None, failed=()):
    calls, evaluations = [], []

    async def fetch(key):
        calls.append(key)
        if key in failed:
            raise QueryUnavailable("missing observation")
        return ("response", key)

    async def evaluate(cand, paid):
        # Other candidates' already-paid responses must remain hidden too.
        assert set(paid) == set(cand.queries)
        assert all(value == ("response", key) for key, value in paid.items())
        evaluations.append(cand.id)
        outcome = outcomes[cand.id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    search = LazyParetoSearch(candidates, baseline or {}, fetch, evaluate, vot=456)
    return search, calls, evaluations


def pairs(front):
    return {(value.time_s, value.fare) for value in front.values()}


def exhaustive_front(costs):
    """Independent all-pairs comparison; does not call production dominance."""
    unique = {(cost.time_s, cost.fare) for cost in costs}
    return {pair for pair in unique if not any(
        other != pair and other[0] <= pair[0] and other[1] <= pair[1]
        for other in unique)}


def test_estimated_dominated_candidate_is_eventually_the_actual_winner():
    rows = [candidate("promising", 10, 10, ["a"]),
            candidate("misestimated", 1000, 1000, ["b"])]
    search, calls, _ = make_search(rows, {
        "promising": Cost(90, 90), "misestimated": Cost(1, 1)},
        baseline={"transit": Cost(100, 100)})

    assert pairs(asyncio.run(search.advance(1))) == {(90, 90)}
    assert search.status["misestimated"] == "pending"
    assert pairs(asyncio.run(search.advance(2))) == {(1, 1)}
    assert calls == ["a", "b"]
    assert search.complete


def test_one_paid_shared_query_evaluates_every_unlocked_candidate():
    rows = [candidate("fast", 10, 200, ["shared"]),
            candidate("cheap", 100, 10, ["shared"]),
            candidate("duplicate", 10, 200, ["shared", "shared"])]
    outcomes = {"fast": Cost(10, 200), "cheap": Cost(100, 10),
                "duplicate": Cost(10, 200)}
    search, calls, evaluations = make_search(rows, outcomes)

    front = asyncio.run(search.advance(1))
    assert pairs(front) == {(10, 200), (100, 10)}
    assert len(front) == 2
    assert set(evaluations) == set(outcomes)
    assert calls == search.query_order == ["shared"]
    assert search.complete


def test_budget_zero_still_evaluates_query_free_journeys():
    rows = [candidate("free", 100, 100, []),
            candidate("paid", 1, 1, ["query"])]
    search, calls, evaluations = make_search(rows, {
        "free": Cost(20, 5), "paid": Cost(1, 1)})

    assert pairs(asyncio.run(search.advance(0))) == {(20, 5)}
    assert calls == []
    assert evaluations == ["free"]
    assert search.status["paid"] == "pending"
    assert not search.complete


def test_paid_mapping_is_read_only_and_contains_only_candidate_queries():
    seen = []

    async def fetch(query):
        return query

    async def evaluate(cand, paid):
        seen.append((cand.id, dict(paid)))
        assert set(paid) == set(cand.queries)
        with pytest.raises(TypeError):
            paid["unpaid"] = "injected"
        return Cost(10, 10)

    rows = [candidate("a", 1, 1, ["a"]), candidate("b", 2, 2, ["b"])]
    search = LazyParetoSearch(rows, {}, fetch, evaluate, vot=456)
    asyncio.run(search.advance(2))
    assert seen == [("a", {"a": "a"}), ("b", {"b": "b"})]


def test_failed_shared_query_is_charged_once_and_never_retried():
    rows = [candidate("a", 1, 1, ["bad", "unnecessary"]),
            candidate("b", 2, 2, ["bad"]),
            candidate("valid", 3, 3, ["good"])]
    search, calls, evaluations = make_search(rows, {
        "valid": Cost(30, 30)}, failed={"bad"})

    assert asyncio.run(search.advance(1)) == {}
    assert search.status["a"] == search.status["b"] == "unavailable"
    assert pairs(asyncio.run(search.advance(2))) == {(30, 30)}
    asyncio.run(search.advance(20))
    assert calls == search.query_order == ["bad", "good"]
    assert evaluations == ["valid"]
    assert not search.complete


def test_unknown_evaluation_differs_from_known_infeasibility():
    rows = [candidate("unknown", 1, 1, ["a"]),
            candidate("infeasible", 2, 2, ["b"])]
    search, calls, evaluations = make_search(rows, {
        "unknown": QueryUnavailable("unknown fare"), "infeasible": None})

    assert asyncio.run(search.advance(10)) == {}
    assert search.status == {"unknown": "unavailable", "infeasible": "infeasible"}
    asyncio.run(search.advance(10))
    assert calls == ["a", "b"]
    assert evaluations == ["unknown", "infeasible"]
    assert not search.complete


def test_known_infeasible_candidates_allow_completion():
    rows = [candidate("infeasible", 1, 1, ["q"])]
    search, _, _ = make_search(rows, {"infeasible": None},
                               baseline={"transit": Cost(100, 10)})
    assert pairs(asyncio.run(search.advance(1))) == {(100, 10)}
    assert search.complete


def test_two_query_bundle_pauses_instead_of_filling_budget_with_other_candidate():
    rows = [candidate("a", 1, 1, ["second", "first"]),
            candidate("b", 2, 2, ["other"])]
    outcomes = {"a": Cost(10, 10), "b": Cost(20, 5)}
    resumed, calls, evaluations = make_search(rows, outcomes)
    one_shot, _, _ = make_search(rows, outcomes)

    assert asyncio.run(resumed.advance(1)) == {}
    assert calls == ["first"]
    assert evaluations == []
    assert resumed.status["a"] == "pending"
    asyncio.run(resumed.advance(2))
    assert calls == ["first", "second"]
    assert evaluations == ["a"]
    result = asyncio.run(resumed.advance(3))
    assert result == asyncio.run(one_shot.advance(3))
    assert resumed.query_order == one_shot.query_order == ["first", "second", "other"]


def test_prior_verified_point_disappears_only_when_dominated_by_new_result():
    rows = [candidate("a", 1, 1, ["a"]), candidate("b", 2, 2, ["b"]),
            candidate("c", 3, 3, ["c"])]
    search, _, _ = make_search(rows, {
        "a": Cost(80, 120), "b": Cost(120, 80), "c": Cost(70, 110)},
        baseline={"transit": Cost(100, 100)})
    previous = search.front

    for budget in range(1, 4):
        current = asyncio.run(search.advance(budget))
        assert all(any(new.time_s <= old.time_s and new.fare <= old.fare
                       for new in current.values()) for old in previous.values())
        previous = current
    assert pairs(previous) == {(70, 110), (100, 100), (120, 80)}
    assert set(search.verified) == {"transit", "a", "b", "c"}


def test_input_permutation_preserves_query_order_front_and_status():
    rows = [candidate("c", 20, 1, ["z", "y"]),
            candidate("a", 20, 1, ["b", "a"]),
            candidate("b", 20, 1, ["x", "y"])]
    outcomes = {"a": Cost(20, 50), "b": Cost(50, 20), "c": Cost(30, 30)}
    expected = None
    for shuffled in permutations(rows):
        search, _, _ = make_search(list(shuffled), outcomes)
        front = asyncio.run(search.advance(10))
        result = (search.query_order, front, search.status)
        if expected is None:
            expected = result
        assert result == expected


def test_certified_lower_bound_prunes_but_equal_ordinary_estimate_does_not():
    rows = [candidate("certified", 500, 500, ["skip"], Cost(100, 100)),
            candidate("estimate_only", 500, 500, ["fetch"])]
    search, calls, _ = make_search(rows, {"estimate_only": Cost(50, 50)},
                                  baseline={"transit": Cost(100, 100)})

    assert pairs(asyncio.run(search.advance(10))) == {(50, 50)}
    assert calls == ["fetch"]
    assert search.status["certified"] == "pruned"
    assert search.complete


def test_new_result_can_certify_pruning_of_remaining_queries():
    rows = [candidate("winner", 1, 1, ["query"]),
            candidate("bounded", 2, 2, ["unnecessary"], Cost(30, 30))]
    search, calls, _ = make_search(rows, {"winner": Cost(20, 20)})

    assert pairs(asyncio.run(search.advance(1))) == {(20, 20)}
    assert search.status["bounded"] == "pruned"
    assert calls == ["query"]
    assert search.complete
    previous_status = dict(search.status)
    assert pairs(asyncio.run(search.advance(1))) == {(20, 20)}
    assert search.status == previous_status
    assert calls == ["query"]


def test_actual_seed_response_changes_the_next_choice_without_unpaid_access():
    rows = [candidate("seed", 1, 1, ["seed"]),
            candidate("a", 100, 100, ["a1", "a2"]),
            candidate("b", 200, 200, ["b"])]
    slow, _, _ = make_search(rows, {
        "seed": Cost(900, 900), "a": Cost(100, 100), "b": Cost(200, 200)},
        baseline={"transit": Cost(1000, 1000)})
    fast, _, _ = make_search(rows, {
        "seed": Cost(10, 1), "a": Cost(100, 100), "b": Cost(200, 200)},
        baseline={"transit": Cost(1000, 1000)})

    asyncio.run(slow.advance(2))
    asyncio.run(fast.advance(2))
    # With a poor incumbent, the one-query recommendation improvement wins.
    # Once the seed dominates all estimates, score ordering remains a fallback.
    assert slow.query_order == ["seed", "b"]
    assert fast.query_order == ["seed", "a1"]


def test_later_tier_waits_until_earlier_tier_is_done_then_uses_the_rest():
    rows = [candidate("near", 500, 500, ["n"]),
            Candidate("deferred", Cost(1, 1), ("d",), tier=1)]   # 추정이 훨씬 좋아도 차례가 뒤다
    search, _, _ = make_search(rows, {"near": Cost(500, 500), "deferred": Cost(10, 10)},
                               baseline={"transit": Cost(1000, 1000)})

    asyncio.run(search.advance(1))
    assert search.query_order == ["n"] and search.status["deferred"] == "pending"
    assert pairs(asyncio.run(search.advance(2))) == {(10, 10)}


def test_no_recommendation_gain_still_explores_tradeoffs_per_query_cost():
    rows = [candidate("seed", 0, 1, ["seed"]),
            candidate("fast", 100, 10000, ["f1", "f2"]),
            candidate("middle", 400, 6000, ["m"]),
            candidate("cheap", 2000, 0, ["c"])]
    search, _, _ = make_search(rows, {
        "seed": Cost(1000, 1000), "fast": Cost(100, 10000),
        "middle": Cost(400, 6000), "cheap": Cost(2000, 0)},
        baseline={"transit": Cost(1000, 1000)})

    asyncio.run(search.advance(2))
    assert search.query_order == ["seed", "m"]


def test_zero_cost_result_deduplicates_and_certifies_remaining_lower_bounds():
    rows = [candidate("a", 1, 1, ["shared"]),
            candidate("b", 1, 1, ["shared"]),
            candidate("c", 2, 2, ["unnecessary"])]
    search, calls, _ = make_search(rows, {"a": Cost(0, 0), "b": Cost(0, 0)})

    front = asyncio.run(search.advance(10))
    assert pairs(front) == {(0, 0)}
    assert len(front) == 1
    assert calls == ["shared"]
    assert search.complete


@pytest.mark.parametrize("seed", range(100))
def test_random_finite_pool_matches_exhaustive_oracle_and_every_budget_prefix(seed):
    rng = random.Random(seed)
    baseline = {"transit": Cost(rng.randint(0, 100), rng.randint(0, 100))}
    rows, outcomes = [], {}
    for index in range(rng.randint(2, 20)):
        cid = f"candidate{index:02}"
        actual = Cost(rng.randint(0, 100), rng.randint(0, 100))
        outcomes[cid] = actual if rng.random() > .15 else None
        lower = Cost(rng.randint(0, int(actual.time_s)),
                     rng.randint(0, int(actual.fare))) if rng.random() < .4 else Cost(0, 0)
        rows.append(candidate(cid, rng.randint(0, 200), rng.randint(0, 200),
                              [rng.randrange(12) for _ in range(rng.randrange(4))], lower))
    rng.shuffle(rows)
    resumed, calls, _ = make_search(rows, outcomes, baseline)
    one_shot, _, _ = make_search(rows, outcomes, baseline)
    total = len({q for row in rows for q in row.queries})
    full = asyncio.run(one_shot.advance(total))
    expected = exhaustive_front(list(baseline.values()) + [
        result for result in outcomes.values() if result is not None])
    assert pairs(full) == expected
    assert one_shot.complete

    previous = resumed.front
    for budget in range(total + 1):
        current = asyncio.run(resumed.advance(budget))
        assert len(calls) <= budget
        assert calls == one_shot.query_order[:len(calls)]
        assert all(any(new.time_s <= old.time_s and new.fare <= old.fare
                       for new in current.values()) for old in previous.values())
        previous = current
    assert pairs(previous) == expected
    assert calls == one_shot.query_order
    assert resumed.status == one_shot.status
    assert resumed.complete


@pytest.mark.parametrize("stage", ["fetch", "evaluate"])
def test_unexpected_programming_error_propagates(stage):
    async def fetch(query):
        if stage == "fetch":
            raise RuntimeError("programming error")
        return query

    async def evaluate(cand, paid):
        raise RuntimeError("programming error")

    search = LazyParetoSearch([candidate("a", 1, 1, ["q"])], {},
                              fetch, evaluate, vot=456)
    with pytest.raises(RuntimeError, match="programming error"):
        asyncio.run(search.advance(1))


def test_observed_cost_contradicting_certified_bound_is_an_error():
    search, _, _ = make_search([
        candidate("a", 20, 20, ["q"], Cost(10, 10))], {"a": Cost(9, 10)})
    with pytest.raises(ValueError, match="certified lower bound"):
        asyncio.run(search.advance(1))


def test_budget_cannot_rewind_paid_queries():
    search, calls, _ = make_search([candidate("a", 1, 1, ["q"])], {"a": Cost(1, 1)})
    asyncio.run(search.advance(1))
    with pytest.raises(ValueError, match="budget"):
        asyncio.run(search.advance(0))
    assert calls == ["q"]


def test_parallel_batches_take_next_ranked_distinct_queries_within_budget():
    rows = [candidate(f"c{i}", 10 + i, 10 + i, [f"q{i}", "shared"]) for i in range(5)]
    outcomes = {f"c{i}": Cost(50 + i, 50) for i in range(5)}
    one, one_calls, _ = make_search(rows, outcomes, baseline={"transit": Cost(100, 100)})
    asyncio.run(one.advance(4))
    batch, batch_calls, _ = make_search(rows, outcomes, baseline={"transit": Cost(100, 100)})
    asyncio.run(batch.advance(4, parallel=3))
    assert len(batch_calls) == len(set(batch_calls)) == 4   # 예산을 넘지 않고 같은 질의를 두 번 내지 않는다
    assert batch_calls[0] == one_calls[0]                    # 묶음의 첫 질의는 한 번에 하나일 때와 같다
    assert batch.query_order == batch_calls


def test_parallel_one_keeps_the_sequential_order():
    rows = [candidate(f"c{i}", 10 + i, 10 + 2 * i, [f"q{i}"]) for i in range(6)]
    outcomes = {f"c{i}": Cost(40 - i, 40 + i) for i in range(6)}
    a, a_calls, _ = make_search(rows, outcomes, baseline={"transit": Cost(100, 100)})
    b, b_calls, _ = make_search(rows, outcomes, baseline={"transit": Cost(100, 100)})
    asyncio.run(a.advance(6))
    asyncio.run(b.advance(6, parallel=1))
    assert a_calls == b_calls and pairs(a.front) == pairs(b.front)
