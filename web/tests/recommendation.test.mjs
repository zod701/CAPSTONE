import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../frontend/js/routes.js', import.meta.url), 'utf8');
const context = vm.createContext({ fmtWon: n => `${n}원` });
vm.runInContext(source.slice(source.indexOf('function optimalCost('), source.indexOf('const EXTRA_SORT'))
  .replaceAll('export ', ''), context);
test('only the global minimum is recommended across all modes as VOT changes', () => {
  const entries = [
    { key: 't:0', time_s: 3600, fare: 2000 },
    { key: 'h:0', time_s: 3000, fare: 6000 },
    { key: 'car', time_s: 1800, fare: 20000 },
  ];
  assert.equal(context.bestRecommendation(entries, 0), 't:0');
  assert.equal(context.bestRecommendation(entries, 350), 't:0');
  assert.equal(context.bestRecommendation(entries, 500), 'h:0');
  assert.equal(context.bestRecommendation(entries, 1500), 'car');
  assert.equal(context.bestRecommendation(entries, 400), 'h:0');
  assert.equal(context.bestRecommendation([], 500), null);
  assert.equal(context.bestRecommendation([{ key: 'bad', time_s: 1, fare: null }], 500), null);
});
