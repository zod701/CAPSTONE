import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

test('custom tooltips never restore native titles, including dynamic updates', () => {
  const events = {};
  let observe;
  const attrs = new Map([['title', '설명']]);
  const target = { dataset: {}, isConnected: true,
    hasAttribute: key => attrs.has(key), getAttribute: key => attrs.get(key),
    removeAttribute: key => attrs.delete(key), querySelectorAll: () => [],
    contains: () => false, closest() { return this; } };
  const tip = { setAttribute() {}, querySelector: () => ({}), hidden: true };
  const context = vm.createContext({
    document: { createElement: () => tip, body: { append() {}, querySelectorAll: () => attrs.has('title') ? [target] : [] },
      addEventListener: (event, callback) => events[event] = callback },
    window: { addEventListener() {} }, setTimeout: () => 1, clearTimeout() {},
    MutationObserver: class { constructor(fn) { observe = fn; } observe() {} },
  });
  vm.runInContext(readFileSync(new URL('../frontend/js/tooltip.js', import.meta.url), 'utf8').replace('export function', 'function'), context);
  context.initTooltips();
  assert.equal(attrs.has('title'), false);
  events.mouseover({ target });
  events.mouseout({ relatedTarget: null });
  assert.equal(attrs.has('title'), false);
  assert.equal(target.dataset.tip, '설명');
  attrs.set('title', '변경');
  observe([{ type: 'attributes', attributeName: 'title', target }]);
  assert.equal(attrs.has('title'), false);
  assert.equal(target.dataset.tip, '변경');
});
