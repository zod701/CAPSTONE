import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

test('map long press opens once; taps, movement, cancellation and multitouch do not open', () => {
  const events = {};
  let timer, calls = 0;
  const context = vm.createContext({
    setTimeout: (fn, delay) => { assert.equal(delay, 550); timer = fn; return 1; },
    clearTimeout: () => timer = null,
  });
  vm.runInContext(readFileSync(new URL('../frontend/js/longpress.js', import.meta.url), 'utf8').replace('export function', 'function'), context);
  context.initMapLongPress({ addEventListener: (name, fn) => events[name] = fn }, () => calls++);
  const emit = (type, extra = {}) => events[type]({ isPrimary: true, button: 0, pointerId: 1, clientX: 10, clientY: 10, target: { closest: () => null }, ...extra });
  emit('pointerdown'); emit('pointerup'); assert.equal(timer, null);
  emit('pointerdown'); emit('pointermove', { clientX: 25 }); assert.equal(timer, null);
  emit('pointerdown'); emit('pointercancel'); assert.equal(timer, null);
  emit('pointerdown'); emit('pointerdown', { isPrimary: false, pointerId: 2 }); assert.equal(timer, null);
  emit('pointerdown', { target: { closest: () => ({}) } }); assert.equal(timer, null);
  assert.equal(calls, 0);
  emit('pointerdown'); timer(); assert.equal(calls, 1); assert.equal(timer, null);
  emit('pointerup');
  let prevented = false, stopped = false;
  emit('click', { preventDefault: () => prevented = true, stopImmediatePropagation: () => stopped = true });
  assert.equal(prevented && stopped, true);
  // 다음 일반 클릭과 팝업 버튼 조작은 막지 않는다.
  emit('pointerdown', { target: { closest: () => ({}) } });
  emit('pointerup');
  emit('click', { preventDefault: () => assert.fail('next click blocked'), stopImmediatePropagation() {} });
});
