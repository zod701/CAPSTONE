import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

test('route labels hide when zoomed out, prioritize boarding stops and never overlap', () => {
  const source = readFileSync(new URL('../frontend/js/routes.js', import.meta.url), 'utf8');
  const fn = source.slice(source.indexOf('function declutterLabels()'), source.indexOf('export function clearTransit()'));
  let zoom = 15;
  const elements = [0, 10, 100].map(left => ({ isConnected: true, style: {},
    getBoundingClientRect: () => ({ left, right: left + 40, top: 0, bottom: 20 }) }));
  const context = vm.createContext({ v: { map: { getZoom: () => zoom } },
    stopLabels: elements.map((el, i) => ({ end: i < 2, marker: { getTooltip: () => ({ getElement: () => el }) } })) });
  vm.runInContext(fn, context);
  context.declutterLabels();
  assert.ok(elements.every(el => el.style.visibility === 'hidden'));
  zoom = 16; context.declutterLabels();
  assert.deepEqual(elements.map(el => el.style.visibility), ['', 'hidden', '']);
  elements[1].getBoundingClientRect = () => ({ left: 200, right: 240, top: 0, bottom: 20 });
  context.declutterLabels();
  assert.ok(elements.every(el => el.style.visibility === ''));
  zoom = 14; context.declutterLabels();
  assert.ok(elements.every(el => el.style.visibility === 'hidden'));
});
