"""짧은 택시 연결의 도보 대체: 시간·요금·A/B/C/D 생성 경계."""
from copy import deepcopy

import pytest

from algo import anchors as a
from algo.tests import test_backtrack as bt
from algo.tests import test_detour as dt


@pytest.mark.parametrize('distance,walking', [(0, True), (800, True), (a.TAXI_MIN_M, False), (1500, False)])
def test_distance_boundary_and_connection_overheads(distance, walking):
    replacement = a.walking_connection(distance)
    assert bool(replacement) == walking
    if walking:
        assert replacement['fare'] == replacement['taxi_s'] == replacement['taxi_km'] == 0
        for connect in (True, False):
            assert a.connection_plan_s(replacement, 999, connect=connect) == a.walk_s(distance)


@pytest.mark.parametrize('hybrid', ['A', 'B'])
@pytest.mark.parametrize('kind', ['bus', 'subway'])
def test_backtrack_walks_the_correct_end(hybrid, kind):
    p = bt.move(bt.O, 800 if hybrid == 'A' else 200, 0)
    q = bt.move(bt.D, 200 if hybrid == 'A' else 800, 0)
    if kind == 'bus':
        args = dict(bus=[bt.bstop('p', p), bt.bstop('q', q)],
                    routes=[bt.rrow('r1', 1, 'p', p), bt.rrow('r1', 2, 'q', q)])
    else:
        args = dict(sub=[bt.sstop('p', p), bt.sstop('q', q)],
                    lines=[bt.lrow('r1', 1, 'p', p), bt.lrow('r1', 2, 'q', q)])
    candidates, _ = bt.run(hybrid=hybrid, **args)
    c, = candidates
    assert c['anchor'] == ('p' if hybrid == 'A' else 'q')
    assert c['link_walk_s'] == pytest.approx(a.walk_s(800), rel=1e-4)
    assert c['score_s'] == pytest.approx(c['link_walk_s'] + a.WAIT_UNKNOWN_S + c['ride_s']
                                        + c['walk_s'] + c['transit_fare'] / 300 * 60)


@pytest.mark.parametrize('hybrid', ['A', 'B'])
def test_perturb_includes_walk_time_and_no_taxi_fare(hybrid):
    p, q = bt.move(bt.O, 800, 0), bt.move(bt.D, 800, 0)
    route = bt.base_route([bt.ride_step(bt.chosen('p', p), bt.chosen('q', q))])
    original = deepcopy(route)
    candidates, _ = a.propose_perturb(bt.O, bt.D, [route], vot=300, hybrids=(hybrid,), at=bt.DAY)
    c, = candidates
    assert c['link_mode'] == 'WALKING' and c['fare'] == 0
    assert c['score_s'] == pytest.approx(c['link_walk_s'] + c['wait_s'] + c['ride_s'] + c['walk_s']
                                        + c['fare_info']['value'] / 300 * 60)
    assert route == original


def test_wait_gap_becomes_walk_and_recalculates_transit_fare():
    route = bt.gap_route(wait2=900)
    step = route['steps'][2]
    point = bt.move(bt.M2, 800, 0)
    step['resolution']['alight']['chosen'] = bt.chosen('close', point)
    candidates, _ = a.propose_gap(bt.O, bt.D, [route], vot=300, at=bt.DAY)
    c = next(c for c in candidates if c['anchor'] == 'm2>close')
    assert c['link_mode'] == 'WALKING' and c['fare'] == 0
    assert c['score_s'] == pytest.approx(c['base_time_s'] - c['gap_s'] + c['link_walk_s']
                                        + c['transit_fare'] / 300 * 60)
    expected = deepcopy(c['fare_steps'])
    connector = next(s for s in expected if s['type'] == 'TAXI')
    connector.update(type='WALKING', time_s=c['link_walk_s'])
    assert c['transit_fare'] == a.estimate_fare(expected, c['transit_fare_cap'], at=bt.DAY,
                                               initial_walk_s=c['fare_initial_walk_s'])['value']


def test_detour_reanchors_on_foot_and_carries_the_mode():
    p, q = dt.move(dt.LOOP[2], 800, 0), dt.move(dt.D, 100, 0)
    stops = dt.StopsDB([dt.bstop('s2', dt.LOOP[2]), dt.bstop('p', p), dt.bstop('q', q)], [], dt.LINE_GROUPS)
    routes = dt.RoutesDB([dt.rrow('r1', 1, 'p', p), dt.rrow('r1', 2, 'q', q)])
    candidates, _ = a.propose_detour(stops, routes, dt.LinesDB([]), dt.O, dt.D, [dt.loop_route()], vot=300, at=dt.DAY)
    c = next(c for c in candidates if c['via'] == 'reanchor')
    assert c['link_mode'] == 'WALKING' and c['fare'] == 0
    wait = c['wait_s'] if c['wait_s'] is not None else a.WAIT_UNKNOWN_S
    assert c['time_s'] == pytest.approx(c['head_s'] + c['link_walk_s'] + wait + c['ride_s'] + c['walk_s'])


def test_detour_walks_to_nearby_destination():
    destination = dt.move(dt.LOOP[2], 0, 800)
    stops = dt.StopsDB([dt.bstop('s2', dt.LOOP[2])], [], dt.LINE_GROUPS)
    candidates, _ = a.propose_detour(stops, dt.RoutesDB([]), dt.LinesDB([]), dt.O, destination,
                                    [dt.loop_route()], vot=300, at=dt.DAY)
    c = next(c for c in candidates if c['via'] == 'taxi_to_d')
    assert c['link_mode'] == 'WALKING' and c['fare'] == 0
    assert c['time_s'] == pytest.approx(c['head_s'] + a.walk_s(800), rel=1e-4)


def test_nearby_walking_and_taxi_candidates_are_not_merged():
    taxi = bt.cand_at('A', 'p', bt.P, 10)
    walking = dict(bt.cand_at('A', 'q', bt.move(bt.P, 20, 0), 11), link_mode='WALKING')
    assert len(a.merge_candidates([taxi, walking], top=5)) == 2
