"""Finite-candidate Pareto search with resumable, query-counted verification.

Candidate estimates guide query order only. Permanent pruning requires a
caller-certified lower bound, never a taxi ETA estimate. This research module
does not generate transit paths or model timetables/fare histories itself.
"""
import asyncio
import heapq
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Hashable


@dataclass(frozen=True)
class Cost:
    time_s: float
    fare: float

    def __post_init__(self):
        if any(not isfinite(v) or v < 0 for v in (self.time_s, self.fare)):
            raise ValueError("costs must be finite and nonnegative")

    def dominates(self, other):
        return self.time_s <= other.time_s and self.fare <= other.fare


@dataclass(frozen=True)
class Candidate:
    id: str
    estimate: Cost
    queries: tuple[Hashable, ...]
    lower_bound: Cost = Cost(0, 0)
    tier: int = 0   # lower tiers are ranked first; a later tier waits until no earlier one is pending

    def __post_init__(self):
        # Canonical query order gives the same prefix regardless of input order.
        object.__setattr__(self, "queries", tuple(sorted(set(self.queries), key=repr)))


class QueryUnavailable(Exception):
    """A paid query failed or cannot support a known-cost comparison."""


class LazyParetoSearch:
    """Verify candidates incrementally without looking at unpaid responses.

    fetch(query) and evaluate(candidate, responses) are async callables.
    evaluate receives ONLY the candidate's paid responses and returns Cost, or
    None for a known infeasible/policy-excluded journey. It must not fetch data.
    Raise QueryUnavailable for unknown observations, rather than returning None.

    advance(n) sets a cumulative query cap. Calls may be paused within a
    multi-query candidate and resumed. Budgets never influence priority order.
    A complete result certifies the frontier ONLY over these finite candidates,
    conditional on valid lower bounds, stable responses, and correct evaluators.
    """
    def __init__(self, candidates, baseline, fetch, evaluate, *, vot):
        candidates = list(candidates)
        self.candidates = {c.id: c for c in candidates}
        if len(self.candidates) != len(candidates):
            raise ValueError("candidate ids must be unique")
        if set(self.candidates) & set(baseline):
            raise ValueError("baseline and candidate ids must be distinct")
        if not isfinite(vot) or vot <= 0:
            raise ValueError("vot must be finite and positive")
        self.vot, self.fetch, self.evaluate = vot, fetch, evaluate
        self.verified = dict(baseline)
        self.responses, self.failed_queries = {}, set()
        self.status = {cid: "pending" for cid in self.candidates}
        self._ids = sorted(self.candidates)
        self._estimate_score = {c.id: self._score(c.estimate) for c in candidates}
        self._checked_front = None
        self.query_order = []
        self._pending = None
        self._seeded = False

    @property
    def front(self):
        result, cheapest = {}, float("inf")
        for cid, cost in sorted(self.verified.items(), key=lambda item: (
                item[1].time_s, item[1].fare, item[0])):
            if cost.fare < cheapest:
                result[cid] = cost
                cheapest = cost.fare
        return result

    @property
    def complete(self):
        return all(s in ("verified", "infeasible", "pruned") for s in self.status.values())

    def _score(self, cost):
        return cost.fare + cost.time_s / 60 * self.vot

    async def _settle(self):
        # Evaluate all combinations unlocked by paid shared queries, even if a
        # different candidate caused the query. No extra logical charge is due.
        # Pending candidates were already checked against the front as it was
        # at the end of the last settle; verified costs change only here.
        front = tuple(self.front.values())
        check = front != self._checked_front
        for cid in self._ids:
            if self.status[cid] != "pending":
                continue
            cand = self.candidates[cid]
            if check and any(p.dominates(cand.lower_bound) for p in front):
                self.status[cid] = "pruned"
            elif any(q in self.failed_queries for q in cand.queries):
                self.status[cid] = "unavailable"
            elif all(q in self.responses for q in cand.queries):
                available = MappingProxyType({q: self.responses[q] for q in cand.queries})
                try:
                    value = await self.evaluate(cand, available)
                except QueryUnavailable:
                    self.status[cid] = "unavailable"
                    continue
                if value is not None and not cand.lower_bound.dominates(value):
                    raise ValueError("verified cost violates certified lower bound")
                self.status[cid] = "infeasible" if value is None else "verified"
                if value is not None:
                    self.verified[cid] = value
        # Close the certificate at this checkpoint too: the final paid query
        # may prove all remaining candidates irrelevant without another call.
        front = tuple(self.front.values())
        fresh = front != self._checked_front
        for cid, cand in self.candidates.items():
            status = self.status[cid]
            if (status == "unavailable" or (status == "pending" and fresh)) and any(
                    p.dominates(cand.lower_bound) for p in front):
                self.status[cid] = "pruned"
        self._checked_front = front

    def _ranked(self, n):
        """The first n pending candidates in priority order."""
        remaining = [c for c in self.candidates.values() if self.status[c.id] == "pending"]
        if not remaining:
            return []
        tier = min(c.tier for c in remaining)
        remaining = [c for c in remaining if c.tier == tier]
        if not self._seeded or not self.front:
            # Start with the same requested-VOT objective that worked at low
            # budgets, then retain that observation for every later checkpoint.
            self._seeded = True
            return heapq.nsmallest(n, remaining, key=lambda c: (self._estimate_score[c.id], c.id))
        front = tuple(self.front.values())
        best_score = min(map(self._score, front))
        points = [(p.time_s, p.fare) for p in front]
        vot, responses, scores = self.vot, self.responses, self._estimate_score

        def priority(cand):
            cost = cand.estimate
            time_s, fare, score = cost.time_s, cost.fare, scores[cand.id]
            marginal = sum(q not in responses for q in cand.queries)
            gain = max(0, best_score - score)
            # Minimum improvement needed to dominate the estimate, in won.
            # It is zero if a verified point already dominates the estimate.
            gap = max(0, min(max((pt - time_s) / 60 * vot, pf - fare) for pt, pf in points))
            return (gain > 0, gain / marginal, gap / marginal, -score)

        return heapq.nsmallest(n, remaining, key=lambda c: (tuple(-v for v in priority(c)), c.id))

    async def advance(self, budget, parallel=1):
        """parallel > 1 fetches the chosen query plus the next-ranked
        candidates' unpaid queries together, without seeing each other's
        responses. parallel=1 is the one-at-a-time order described above."""
        if not isinstance(budget, int) or budget < len(self.query_order):
            raise ValueError("budget must be an integer at least the spent query count")
        while True:
            await self._settle()
            if len(self.query_order) >= budget:
                break
            width = min(parallel, budget - len(self.query_order))
            # One ranking serves both the next choice and the rest of the batch.
            # Candidates sharing an already-chosen query add nothing; a few times
            # the width is enough to fill the batch in practice.
            ranked = None
            if self._pending is None or self.status[self._pending] != "pending":
                ranked = self._ranked(4 * width)
                self._pending = ranked[0].id if ranked else None
            if self._pending is None:
                break
            cand = self.candidates[self._pending]
            keys = [next(q for q in cand.queries if q not in self.responses)]
            if width > 1 and ranked is None:
                ranked = self._ranked(4 * width)
            for other in (ranked if width > 1 else ()):
                if len(keys) >= width:
                    break
                key = next((q for q in other.queries if q not in self.responses and q not in keys), None)
                if key is not None:
                    keys.append(key)
            self.query_order.extend(keys)  # Failed attempts are charged too.
            got = await asyncio.gather(*map(self.fetch, keys), return_exceptions=True)
            for key, value in zip(keys, got):
                if isinstance(value, QueryUnavailable):
                    self.failed_queries.add(key)
                elif isinstance(value, BaseException):
                    raise value
                else:
                    self.responses[key] = value
        return self.front
