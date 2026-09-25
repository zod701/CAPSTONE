import { test } from 'node:test';
import assert from 'node:assert/strict';
import { boardingPoints, fetchBoardings, matchingBusItems, mountArrivals, vehicleArrival, arrivalGroups, arrivingSoon, arrivalHeadway, vehicleOrder, elapsedItems, subwayDirection, nextArrivalDelay } from '../frontend/js/arrivals.js';

const bus = { type: 'BUS', stops: ['승차역'], route_ids: ['42'], resolution: { board: { status: 'matched', chosen: { id: 'stop-1' } } } };
test('subway arrivals several stations away cannot become zero-second arrivals', async () => {
  const points = boardingPoints({ steps: [{ ...bus, type: 'SUBWAY' }] });
  const [result] = await fetchBoardings(points, async () => ({ items: [
    { eta_s: 0, n_stops_ahead: 6, message: '[6]번째 전역' },
    { eta_s: 0, n_stops_ahead: null, message: '잠실 도착' },
    { eta_s: 100, n_stops_ahead: 1 },
  ] }));
  const items = elapsedItems(result, result.at);
  assert.equal(items[0].eta_s, null);
  assert.equal(arrivingSoon(items[0]), false);
  assert.equal(arrivingSoon(items[1]), true);
  assert.equal(arrivingSoon(items[2]), true);
});
test('vehicle order puts realtime before estimates and sorts by seconds within each group', () => {
  const point = { step: { type: 'BUS', route_ids: ['a', 'b'], bus_headways: [{ name: 'estimate', headway_m: 2 }] } };
  const items = [{ route_id: 'a', route_name: 'slow', eta_s: 500 }, { route_id: 'b', route_name: 'soon', eta_s: 90 }];
  assert.deepEqual(vehicleOrder(point, ['estimate', 'slow', 'unknown', 'soon'], items), [3, 1, 0, 2]);
});
test('zero-second and message-only arrival statuses remain visible for two minutes', () => {
  for (const item of [{ eta_s: 0 }, { eta_s: null, message: '곧 도착' }]) {
    const result = { at: 1000, data: { items: [item] } };
    assert.equal(arrivingSoon(elapsedItems(result, 2000)[0]), true);
    assert.equal(arrivingSoon(elapsedItems(result, 120000)[0]), true);
    assert.equal(nextArrivalDelay([result], 2000), 119000);
    assert.equal(elapsedItems(result, 121000)[0].expired, true);
    assert.equal(nextArrivalDelay([result], 121000), null);
  }
});
test('countdown schedules only minute changes and arrival expiry using absolute time', () => {
  const results = [{ at: 1000, data: { items: [{ eta_s: 90 }] } }];
  assert.equal(nextArrivalDelay(results, 1000), 91000);
  assert.equal(nextArrivalDelay(results, 31000), 61000);
  assert.equal(nextArrivalDelay(results, 91000), 1000);
  assert.equal(nextArrivalDelay(results, 92000), null);
  assert.equal(nextArrivalDelay(results, 81500), 10500);
  assert.equal(nextArrivalDelay([{ at: 1000, data: { items: [{ eta_s: 90, message: '곧 도착' }] } }], 1000), 91000);
  assert.equal(nextArrivalDelay([{ at: 1000, data: { items: [{ eta_s: null }] } }], 1000), null);
});
test('every boarding is included without querying on selection and duplicate stops share one request', async () => {
  const points = boardingPoints({ steps: [bus, { type: 'WALKING' }, bus, { type: 'SUBWAY', resolution: { board: { status: 'matched', chosen: { id: 'rail-1' } } } }] });
  assert.equal(points.length, 3);
  const calls = [];
  const results = await fetchBoardings(points, async url => { calls.push(url); return { items: [{ eta_s: 240 }, { eta_s: 1140 }] }; });
  assert.deepEqual(calls, ['/api/arrivals/stop/stop-1', '/api/arrivals/station/rail-1']);
  assert.equal(results[0], results[1]);
  assert.equal(results[0].data.items.length, 2);
});
test('unmatched stops and partial failure remain explicit; route numbers cannot match another route ID', async () => {
  const points = boardingPoints({ steps: [bus, { type: 'BUS', resolution: { board: { status: 'ambiguous', chosen: { id: 'wrong' } } } }] });
  const results = await fetchBoardings(points, async () => { throw new Error('quota exceeded'); });
  assert.equal(results[0].error, 'quota exceeded');
  assert.ok(results[1].error);
  assert.equal(points[1].url, null);
  assert.deepEqual(matchingBusItems(points[0], [{ route_id: '42', eta_s: 0 }, { route_id: 'other', eta_s: 60 }]), [{ route_id: '42', eta_s: 0 }]);
});

for (const mobile of [false, true]) test(`selected refresh works with cooldown on ${mobile ? 'mobile' : 'desktop'} without automatic fetches`, async () => {
  const previous = { document: globalThis.document, matchMedia: globalThis.matchMedia, fetch: globalThis.fetch, setTimeout: globalThis.setTimeout };
  let tick;
  let selected = false, calls = 0;
  const element = () => ({ events: {}, dataset: {}, classList: { add() {}, toggle() {} }, addEventListener(name, fn) { this.events[name] = fn; }, insertAdjacentElement() {} });
  const refresh = element(), status = element();
  const toolbar = element(); toolbar.isConnected = true; toolbar.querySelector = selector => selector === 'button' ? refresh : status;
  let first = true;
  const info = element();
  let oldWaitHidden;
  const oldWait = { classList: { toggle: (name, hidden) => { oldWaitHidden = hidden; } } };
  const boardingNode = { classList: { contains: () => true }, querySelector: selector => selector === '.tl-wait' ? oldWait : info };
  let etaLabel;
  const vehicle = { style: { setProperty() {} }, dataset: { vehicle: '7727' }, querySelector: () => null, append: label => { etaLabel = label; } };
  let refreshParent;
  const detail = { isConnected: true, closest: () => selected ? {} : null,
    querySelector: selector => selector === '.tl-node:first-child .tl-name'
      ? { append: child => { refreshParent = child; } }
      : ({ previousElementSibling: boardingNode, querySelector: () => info, querySelectorAll: () => [vehicle] }) };
  try {
    globalThis.setTimeout = fn => { tick = fn; return 1; };
    globalThis.document = { createElement: () => { if (first) { first = false; return toolbar; } return element(); } };
    globalThis.matchMedia = () => ({ matches: mobile });
    globalThis.fetch = async () => { calls++; return { ok: true, json: async () => ({ items: [{ route_id: '42', route_name: '7727', eta_s: 120 }] }) }; };
    mountArrivals(detail, { steps: [bus] });
    assert.equal(refreshParent, toolbar);
    assert.match(toolbar.innerHTML, /aria-label="실시간 새로고침"/);
    assert.ok(toolbar.innerHTML.indexOf('<span') < toolbar.innerHTML.indexOf('<button'));
    assert.equal(calls, 0);
    assert.equal(etaLabel.textContent, '-');
    assert.equal(oldWaitHidden, false);
    await refresh.events.click(); assert.equal(calls, 0);
    await refresh.events.click(); assert.equal(calls, 0);
    selected = true;
    await refresh.events.click(); assert.equal(calls, 1);
    assert.equal(etaLabel.textContent, '곧 도착');
    assert.equal(oldWaitHidden, true);
    tick(); assert.equal(calls, 1);
    await refresh.events.click(); assert.equal(calls, 1);
    assert.match(status.textContent, /15초/);
  } finally {
    Object.assign(globalThis, previous);
  }
});

test('local countdown ages snapshots without mutation and expires soon messages', () => {
  const result = { at: 1000, data: { items: [{ route_id: '42', route_name: '7727', eta_s: 120, message: '곧 도착' }] } };
  assert.equal(elapsedItems(result, 61000)[0].eta_s, 60);
  assert.equal(result.data.items[0].eta_s, 120);
  const point = { step: bus };
  assert.equal(vehicleArrival(point, '7727', elapsedItems(result, 1000)).text, '곧 도착');
  const expired = elapsedItems(result, 122000);
  assert.equal(vehicleArrival(point, '7727', expired).text, '-분');
  assert.equal(arrivingSoon(expired[0]), false);
});

test('subway next-station direction is grouped without guessing unknown headsigns', () => {
  const point = { step: { type: 'SUBWAY', stops: ['정자', '판교', '강남'] } };
  const same = { headsign: '운정중앙행 - 판교방면', eta_s: 120 };
  const opposite = { headsign: '동탄행 - 미금방면', eta_s: 60 };
  assert.equal(subwayDirection(point, same), true);
  assert.equal(subwayDirection(point, opposite), false);
  assert.equal(subwayDirection(point, { headsign: '강남행' }), null);
  assert.equal(arrivalGroups(point, [opposite, same])[0].selected, true);
});

test('vehicle ETA uses matching IDs and each route headway, never the combined headway', () => {
  const point = { step: { ...bus, headway_m: 6, bus_headways: [{ id: '42', name: '7727', headway_m: 15 }] } };
  assert.deepEqual(vehicleArrival(point, '7727', [{ route_id: '42', route_name: '7727', eta_s: 120 }]), { text: '곧 도착', live: true });
  assert.deepEqual(vehicleArrival(point, '7727', [{ route_id: 'wrong', route_name: '7727', eta_s: 0 }]), { text: '7.5분', live: false });
  assert.deepEqual(vehicleArrival(point, '7728', []), { text: '-', live: false });
  assert.deepEqual(vehicleArrival(point, '7727', [{ route_id: '42', route_name: '7727', eta_s: 0 }]), { text: '곧 도착', live: true });
});

test('arrival groups put the selected route first and retain next vehicles and bus colors', () => {
  const groups = arrivalGroups({ step: bus }, [
    { route_id: 'other', route_name: '42', eta_s: 0 },
    { route_id: '42', route_name: '7727', route_type: '지선', eta_s: 600 },
    { route_id: '42', route_name: '7727', route_type: '지선', eta_s: 120 },
  ]);
  assert.equal(groups[0].selected, true);
  assert.deepEqual(groups[0].rows.map(item => item.eta_s), [120, 600]);
  assert.equal(groups[0].rows[0].route_type, '지선');
  assert.equal(groups[1].selected, false);
  assert.equal(arrivingSoon({ eta_s: 45, message: '곧 도착' }), true);
  assert.equal(arrivingSoon({ eta_s: null }), false);
});

test('subway labels prefer verified direction realtime and fall back to headway', () => {
  const point = { step: { type: 'SUBWAY', headway_m: 7, stops: ['정자', '판교'], route_ids: ['2'] } };
  assert.deepEqual(vehicleArrival(point, '2호선', [{ headsign: '운정중앙행 - 판교방면', eta_s: 240 }]), { text: '4분', live: true });
  assert.deepEqual(vehicleArrival(point, '2호선', [{ headsign: '운정중앙행 - 판교방면', eta_s: 120 }]), { text: '곧 도착', live: true });
  assert.deepEqual(vehicleArrival(point, '2호선', [{ headsign: '동탄행 - 미금방면', eta_s: 60 }]), { text: '3.5분', live: false });
  assert.deepEqual(vehicleArrival(point, '2호선', [{ route_id: '2', route_name: '2호선', eta_s: 60 }]), { text: '3.5분', live: false });
  point.step.headway_m = null;
  assert.deepEqual(vehicleArrival(point, '2호선', []), { text: '-', live: false });
});

test('soon starts at 120 seconds and schedules the transition without polling', () => {
  assert.equal(arrivingSoon({ eta_s: 121 }), false);
  assert.equal(arrivingSoon({ eta_s: 120 }), true);
  assert.equal(arrivingSoon({ eta_s: 0 }), true);
  assert.equal(arrivingSoon({ eta_s: -1 }), false);
  const results = [{ at: 1000, data: { items: [{ eta_s: 150 }] } }];
  assert.equal(nextArrivalDelay(results, 1000), 30000);
  assert.equal(nextArrivalDelay(results, 31000), 121000);
});

test('arrival dialog headway uses full per-route intervals and matches fallback by ID', () => {
  const point = { step: { bus_headways: [{ id: '42', name: '7727', headway_m: 15 }] } };
  assert.equal(arrivalHeadway(point, { route_id: '42' }), '배차간격 약 15분');
  assert.equal(arrivalHeadway(point, { route_id: 'other', headway_m: 10 }), '배차간격 약 10분');
  assert.equal(arrivalHeadway(point, { route_id: 'other', route_name: '7727' }), '배차간격 정보 없음');
});
