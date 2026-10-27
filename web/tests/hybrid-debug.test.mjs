import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { stageState, comparisonCandidates, createHybridDebug } from '../frontend/js/hybrid-debug.js';

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
      buffers: { head: { bus: [{ center: [127, 37], radius_m: 400, mode: 'walk' },
        { center: [128, 38], radius_m: 8790, mode: 'taxi' }] },
        tail: { bus: [{ center: [127, 37], radius_m: 8790, mode: 'taxi' },
          { center: [128, 38], radius_m: 800, mode: 'walk' }] } }, verify_stage: 1,
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
    assert.deepEqual(shapes.filter(s => s.options.radius > 10).map(s => s.options.radius), [400, 8790]);
    marker.handlers.click();
    assert.equal(opened, anchor);
    assert.equal(cleared, 1);
    assert.equal(shapes.filter(s => s.options.weight === 5)[1].options.color, '#3D5BAB');
    assert.deepEqual(shapes.filter(s => s.options.weight === 5).map(s => s.points), [
      [[37, 127], [37.05, 127.05]], [[37.05, 127.05], [37.1, 127.1]], [[37.1, 127.1], [38, 128]],
    ]);
    controls['[data-side]'].change({ target: { value: 'tail' } });
    assert.deepEqual(shapes.filter(s => s.options.radius > 10).map(s => s.options.radius), [8790, 800]);
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
