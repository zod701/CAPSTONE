import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../frontend/js/routes.js', import.meta.url), 'utf8');
const code = source.slice(source.indexOf('const SORTS ='), source.indexOf('function drawCards()'))
  .replaceAll('export ', '');

test('optimal sorts transit and hybrid by generalized cost in mixed and filtered tabs', () => {
  const context = vm.createContext({ routeLabel: r => r.group });
  vm.runInContext(code, context);
  vm.runInContext(`cards = { sort: 'optimal', vot: 500, group: null, transit: { routes: [
    { group: 'bus', fare: { value: 15000 }, total_with_wait_s: 600 },
    { group: 'bus', fare: { min: 8000 }, total_with_wait_s: 1200 },
    { group: 'rail', total_time_s: 300 }
  ] }, extras: [{ fare: 11500, time_s: 900 }, { fare: 4500, time_s: 1500 }] };`, context);
  const keys = () => Array.from(context.visibleEntries(), row => row.key);
  assert.deepEqual(keys(), ['h:1', 't:1', 'h:0', 't:0', 't:2']);
  vm.runInContext("cards.group = 'hybrid'", context);
  assert.deepEqual(keys(), ['h:1', 'h:0']);
  vm.runInContext("cards.group = 'bus'", context);
  assert.deepEqual(keys(), ['t:1', 't:0']);
  vm.runInContext("cards.group = null; cards.vot = 0", context);
  assert.deepEqual(keys(), ['h:1', 't:1', 'h:0', 't:0', 't:2']);
  vm.runInContext("cards.vot = 1500", context);
  assert.deepEqual(keys(), ['t:0', 'h:0', 't:1', 'h:1', 't:2']);
});
