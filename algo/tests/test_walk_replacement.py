"""짧은 연결은 도보 대체로 만들지 않고 A/B/C/D 후보 생성에서 제외한다."""
from copy import deepcopy

import pytest

from algo import anchors as a
from algo.tests import test_backtrack as bt
from algo.tests import test_detour as dt


@pytest.mark.parametrize('hybrid', ['A', 'B'])
@pytest.mark.parametrize('kind', ['bus', 'subway'])
def test_backtrack_excludes_short_connection(hybrid, kind):
    p = bt.move(bt.O, 800 if hybrid == 'A' else 200, 0)
    q = bt.move(bt.D, 200 if hybrid == 'A' else 800, 0)
    if kind == 'bus':
        args = dict(bus=[bt.bstop('p', p), bt.bstop('q', q)],
                    routes=[bt.rrow('r1', 1, 'p', p), bt.rrow('r1', 2, 'q', q)])
    else:
        args = dict(sub=[bt.sstop('p', p), bt.sstop('q', q)],
                    lines=[bt.lrow('r1', 1, 'p', p), bt.lrow('r1', 2, 'q', q)])
    candidates, diag = bt.run(hybrid=hybrid, **args)
    assert candidates == [] and diag['too_close'] == 1


@pytest.mark.parametrize('hybrid', ['A', 'B'])
def test_perturb_excludes_short_connection_without_changing_original(hybrid):
    p, q = bt.move(bt.O, 800, 0), bt.move(bt.D, 800, 0)
    route = bt.base_route([bt.ride_step(bt.chosen('p', p), bt.chosen('q', q))])
    original = deepcopy(route)
    candidates, diag = a.propose_perturb(bt.O, bt.D, [route], vot=300, hybrids=(hybrid,), at=bt.DAY)
    assert candidates == [] and diag['too_close'] == 1
    assert route == original


def test_wait_gap_does_not_create_walk_candidate():
    route = bt.gap_route(wait2=900)
    point = bt.move(bt.M2, 800, 0)
    route['steps'][2]['resolution']['alight']['chosen'] = bt.chosen('close', point)
    candidates, _ = a.propose_gap(bt.O, bt.D, [route], vot=300, at=bt.DAY)
    assert all(c['anchor'] != 'm2>close' and c.get('link_mode') != 'WALKING' for c in candidates)


def test_detour_does_not_reanchor_on_foot():
    p, q = dt.move(dt.LOOP[2], 800, 0), dt.move(dt.D, 100, 0)
    stops = dt.StopsDB([dt.bstop('s2', dt.LOOP[2]), dt.bstop('p', p), dt.bstop('q', q)], [], dt.LINE_GROUPS)
    routes = dt.RoutesDB([dt.rrow('r1', 1, 'p', p), dt.rrow('r1', 2, 'q', q)])
    candidates, _ = a.propose_detour(stops, routes, dt.LinesDB([]), dt.O, dt.D, [dt.loop_route()], vot=300, at=dt.DAY)
    assert not any(c['via'] == 'reanchor' for c in candidates)
    assert all(c.get('link_mode') != 'WALKING' for c in candidates)


def test_detour_does_not_walk_to_nearby_destination():
    destination = dt.move(dt.LOOP[2], 0, 800)
    stops = dt.StopsDB([dt.bstop('s2', dt.LOOP[2])], [], dt.LINE_GROUPS)
    candidates, diag = a.propose_detour(stops, dt.RoutesDB([]), dt.LinesDB([]), dt.O, destination,
                                     [dt.loop_route()], vot=300, at=dt.DAY)
    assert not any(c['via'] == 'taxi_to_d' and c['point_id'] == 's2' for c in candidates)
    assert all(c.get('link_mode') != 'WALKING' for c in candidates)
    assert diag['too_close'] > 0
