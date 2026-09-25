import { test } from 'node:test';
import assert from 'node:assert/strict';
import { locateProgress, remainingSeconds, stopProgress } from '../frontend/js/progress.js';

const steps = [{ path: [[127, 37], [127.001, 37], [127.003, 37]] }];
test('strip position uses station coordinates instead of assuming equal stop distances', () => {
  const step = { ...steps[0], stops: ['A', 'B', 'C'], stop_locs: [{ lon: 127, lat: 37 }, { lon: 127.001, lat: 37 }, { lon: 127.003, lat: 37 }] };
  assert.ok(Math.abs(stopProgress(step, 0.5) - 1.25) < 0.001);
  assert.equal(stopProgress({ stops: ['A', 'B', 'C'] }, 0.5), 1);
  assert.equal(stopProgress(step, 1), 2);
});
test('remaining time removes completed travel and boarding wait but keeps later transfers', () => {
  const route = { steps: [
    { type: 'WALKING', time_s: 120 },
    { type: 'BUS', time_s: 600, wait_s: 180 },
    { type: 'SUBWAY', time_s: 900, wait_s: 240 },
    { type: 'WALKING', time_s: 60 },
  ] };
  assert.equal(remainingSeconds(route), 2100);
  assert.equal(remainingSeconds(route, { index: 1, fraction: 0.5 }), 1500);
  assert.equal(remainingSeconds(route, { index: 1, fraction: 0 }), 1980);
  assert.equal(remainingSeconds(route, { index: 3, fraction: 1 }), 0);
});
test('remaining time includes taxi buffer and refuses unknown times', () => {
  assert.equal(remainingSeconds({ steps: [{ type: 'TAXI', time_s: 100, counted_s: 160 }] }, { index: 0, fraction: 0.5 }), 80);
  assert.equal(remainingSeconds({ steps: [] }), null);
  assert.equal(remainingSeconds({ steps: [{ type: 'BUS', time_s: 600, wait_s: null }] }), null);
  assert.equal(remainingSeconds({ steps: [{ type: 'WALKING', time_s: null }] }), null);
});
test('progress uses distance along the selected step, not vertex count', () => {
  const result = locateProgress(steps, { lng: 127.0015, lat: 37, accuracy: 10 });
  assert.equal(result.index, 0);
  assert.ok(Math.abs(result.fraction - 0.5) < 0.001);
});
test('off-route, missing geometry and inaccurate GPS do not claim progress', () => {
  assert.equal(locateProgress(steps, { lng: 128, lat: 37, accuracy: 10 }), null);
  assert.equal(locateProgress(steps, { lng: 127, lat: 37, accuracy: 1500 }), null);
  assert.equal(locateProgress([], { lng: 127, lat: 37, accuracy: 10 }), null);
  assert.equal(locateProgress(steps, null), null);
});
test('switching routes recomputes progress and overlapping distant steps are uncertain', () => {
  const fix = { lng: 127.0015, lat: 37, accuracy: 10 };
  assert.equal(locateProgress([{ path: [[128, 37], [128.01, 37]] }], fix), null);
  assert.equal(locateProgress([steps[0], {}, steps[0]], fix), null);
  assert.equal(locateProgress([{}, steps[0]], fix).index, 1);
});
