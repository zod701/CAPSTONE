import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { stageState, comparisonCandidates, createHybridDebug, paretoFront, chartPoints } from '../frontend/js/hybrid-debug.js';

test('shared route style distinguishes trunk buses and Bundang rail', () => {
  const source = readFileSync(new URL('../frontend/js/routes.js', import.meta.url), 'utf8');
  const constants = source.slice(source.indexOf('const COLOR ='), source.indexOf('const TYPE_LABEL ='));
  const functions = ['vehicles', 'majorBusColor', 'stepStyle'].map(name =>
    source.match(new RegExp(`(?:export )?function ${name}\\([^]*?\\n\\}`))[0]).join('\n');
  const context = vm.createContext({ safeColor: (c, fallback) => c || fallback });
  vm.runInContext((constants + functions).replaceAll('export ', ''), context);
  assert.equal(context.stepStyle({ type: 'BUS', vehicles: [{ name: '140', type: '간선' }] }).color, '#3D5BAB');
  assert.equal(context.stepStyle({ type: 'BUS', vehicles: [{ name: '4212', type: '지선' }] }).color, '#5BB025');
  assert.equal(context.stepStyle({ type: 'SUBWAY', color: '#FABE00' }).color, '#FABE00');
});

test('failed developer verification can retry without discarding trace or unlocking successful searches', async () => {
  const source = readFileSync(new URL('../frontend/js/app.js', import.meta.url), 'utf8');
  const elements = { 'developer-mode': { checked: true }, 'btn-search': {}, 'btn-hybrid': {} };
  const result = { routes: [], failed: [{ code: 'upstream_error' }], diag: { verified: 0 }, trace: {} };
  const state = { hybrid: result, origin: {}, dest: {}, busy: false, hybridBusy: false };
  const context = vm.createContext({ state, $: id => elements[id], panel: {
    showResults() { throw new Error('passed search guard'); },
  } });
  vm.runInContext(source.slice(source.indexOf('function hybridSearchComplete()'), source.indexOf('// keepHybrid')), context);
  vm.runInContext(source.slice(source.indexOf('async function runHybrid()'), source.indexOf('// 택시 구간과 대중교통 구간을 함께 그린다')), context);
  context.update();
  assert.equal(elements['btn-hybrid'].disabled, false);
  assert.equal(state.hybrid, result);
  await assert.rejects(context.runHybrid(), /passed search guard/);
  state.hybridBusy = true;
  context.update();
  assert.equal(elements['btn-hybrid'].disabled, true);
  await context.runHybrid();
  state.hybridBusy = false;
  result.failed = [];
  context.update();
  assert.equal(elements['btn-hybrid'].disabled, true); // no candidates is a completed search
  await context.runHybrid();
  result.routes = [{ time_s: 1000 }];
  result.diag.verified = 1;
  result.failed = [{ code: 'upstream_error' }];
  context.update();
  assert.equal(elements['btn-hybrid'].disabled, true); // partial success stays locked
});

test('stage playback distinguishes current removals from prior removals and survivors', () => {
  const removed = { stage: 2 };
  assert.equal(stageState(removed, 1, 6), 'kept');
  assert.equal(stageState(removed, 2, 6), 'removed');
  assert.equal(stageState(removed, 3, 6), 'past');
  assert.equal(stageState({ stage: 6 }, 6, 6), 'kept');
});

test('anchor markers open stop information without bubbling into address lookup', () => {
  const shapes = [];
  const shape = (points, options) => {
    const item = { points, options, handlers: {}, bindTooltip() { return this; },
      on(event, handler) { this.handlers[event] = handler; return this; }, addTo() { return this; },
      getBounds() { return { getSouthWest: () => [37, 127], getNorthEast: () => [38, 128] }; } };
    shapes.push(item);
    return item;
  };
  const previous = globalThis.L;
  globalThis.L = { svg: () => ({}), polyline: shape, circleMarker: shape, circle: shape,
    layerGroup: () => ({ clearLayers() { shapes.length = 0; }, remove() {}, addTo() {} }) };
  try {
    const controls = {};
    const next = { dataset: { nav: '1' }, addEventListener(_, fn) { this.click = fn; } };
    const el = { querySelectorAll: s => s === '[data-nav]' ? [next] : [],
      querySelector: s => controls[s] ||= { addEventListener(_, fn) { this.change = fn; } } };
    const map = { createPane: () => ({ style: {} }), fitBounds() {} };
    const anchor = { id: 'bus-stop', name: '정류장', lat: 37.1, lon: 127.1 };
    let opened, cleared = 0;
    const view = createHybridDebug(el, map, () => {}, value => { opened = value; }, () => { cleared++; },
      () => ({ color: '#3D5BAB' }));
    view.enable(true);
    view.setTrace({ vot: 500, baseline: {}, generation: {}, origin: [127, 37], destination: [128, 38],
      buffers: { head: { bus: [{ center: [127, 37], radius_m: 400, mode: 'walk' }] },
        tail: { bus: [{ center: [128, 38], radius_m: 800, mode: 'walk' }] } }, verify_stage: 1,
      stages: [{ name: '후보 생성', entered: 1, removed: 0, remaining: 1 }, { name: '다음', remaining: 1 }],
      candidates: [{ id: 'c1', name: '정류장', stage: 1, shape: '기준 경로 → 목적지',
        connector: [[127.1, 37.1], [128, 38]], anchors: [anchor], geometry: [
          { type: 'WALKING', path: [[127, 37], [127.05, 37.05]] },
          { type: 'BUS', path: [[127.05, 37.05], [127.1, 37.1]] },
          { type: 'TAXI', path: [[127.1, 37.1], [128, 38]] },
        ] }] });
    const marker = shapes.find(s => s.options.radius === 6);
    assert.deepEqual(marker.points, [37.1, 127.1]);
    assert.equal(marker.options.bubblingMouseEvents, false);
    assert.equal(shapes.find(s => s.options.weight === 3).options.bubblingMouseEvents, false);
    assert.deepEqual(shapes.filter(s => s.options.radius > 10).map(s => s.options.radius), [400]);
    marker.handlers.click();
    assert.equal(opened, anchor);
    assert.equal(cleared, 1);
    assert.equal(shapes.filter(s => s.options.weight === 5)[1].options.color, '#3D5BAB');
    assert.deepEqual(shapes.filter(s => s.options.weight === 5).map(s => s.points), [
      [[37, 127], [37.05, 127.05]], [[37.05, 127.05], [37.1, 127.1]], [[37.1, 127.1], [38, 128]],
    ]);
    controls['[data-side]'].change({ target: { value: 'tail' } });
    assert.deepEqual(shapes.filter(s => s.options.radius > 10).map(s => s.options.radius), [800]);
    next.click();
    assert.equal(shapes.filter(s => s.options.radius > 10).length, 0);
  } finally { globalThis.L = previous; }
});

test('table includes all survivors, excludes current removals, and sorts by stage scores', () => {
  const items = Array.from({ length: 8 }, (_, i) => ({ id: `c${i}`, stage: 6,
    estimated_score: i, verified: { score: 10 - i } }));
  items.push({ id: 'removed', stage: 1, estimated_score: -1 });
  items.push({ id: 'removed-now', stage: 2, estimated_score: -2 });
  // 검증 단계(3) 전에는 추정 점수, 그 뒤로는 검증 점수로 줄을 세운다
  assert.deepEqual(comparisonCandidates(items, 2, 6, 3).map(c => c.id), ['c0', 'c1', 'c2', 'c3', 'c4', 'c5', 'c6', 'c7']);
  assert.deepEqual(comparisonCandidates(items, 3, 6, 3).map(c => c.id), ['c7', 'c6', 'c5', 'c4', 'c3', 'c2', 'c1', 'c0']);
  assert.equal(comparisonCandidates([], 6, 6, 3).length, 0);
});

test('chart plots estimates before verification, verified values after, and skips past candidates', () => {
  const items = [
    { id: 'a', stage: 4, estimated: { time_s: 600, fare: 9000 }, verified: { time_s: 660, fare: 9500 } },
    { id: 'b', stage: 2, estimated: { time_s: 700, fare: 8000 }, verified: null },
    { id: 'c', stage: 0, estimated: { time_s: 800, fare: 7000 } },
  ];
  assert.deepEqual(chartPoints(items, 1, 4, 2).map(p => [p.id, p.status, p.time_s]), [['a', 'kept', 600], ['b', 'kept', 700]]);
  // 검증 단계에서 제외된 b 는 확인값이 없어 점이 없다
  assert.deepEqual(chartPoints(items, 2, 4, 2).map(p => [p.id, p.time_s]), [['a', 660]]);
});

test('pareto front keeps only points no other point beats on both time and fare', () => {
  const points = [{ id: 'slow-cheap', time_s: 900, fare: 3000 }, { id: 'fast', time_s: 600, fare: 9000 },
    { id: 'worse', time_s: 800, fare: 9500 }, { id: 'middle', time_s: 700, fare: 6000 }, { id: 'tie', time_s: 700, fare: 6500 }];
  assert.deepEqual(paretoFront(points).map(p => p.id), ['fast', 'middle', 'slow-cheap']);
});

test('hybrid card puts a first taxi before the transit it boards', () => {
  const source = readFileSync(new URL('../frontend/js/hybrid.js', import.meta.url), 'utf8');
  const code = source.slice(source.indexOf('function taxiStep'), source.indexOf('function fareText')).replaceAll('export ', '');
  const context = vm.createContext({});
  vm.runInContext(code + '; this.combinedRoute = combinedRoute;', context);
  const r = { taxi: { duration_s: 600, fare: { taxi: 9000, toll: 0 } }, taxi_counted_s: 690, taxi_at: 1,
    from: { name: 'u' }, to: null, transit: { steps: [{ type: 'SUBWAY' }] },
    first_taxi: { taxi: { duration_s: 300, fare: { taxi: 5000, toll: 0 } }, counted_s: 450, to: { name: 'b' } } };
  const steps = context.combinedRoute(r).steps;
  // vm 안에서 만든 배열이라 JSON 으로 견준다 (undefined 는 null 이 된다)
  assert.equal(JSON.stringify(steps.map(s => [s.type, s.from_name, s.to_name, s.counted_s])),
    JSON.stringify([['TAXI', null, 'b', 450], ['SUBWAY', null, null, null], ['TAXI', 'u', null, 690]]));
  assert.equal(context.combinedRoute({ ...r, first_taxi: null }).steps.length, 2);
});
