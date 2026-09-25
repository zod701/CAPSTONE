import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

test('touching search history survives input blur and outside touch dismisses it', () => {
  const inputEvents = {}, listEvents = {}, documentEvents = {};
  const item = { closest: selector => selector === '.ac-item' ? item : null, dataset: { i: '0' } };
  const input = { value: '', setAttribute() {}, removeAttribute() {}, select() {},
    addEventListener: (name, fn) => { (inputEvents[name] ||= []).push(fn); } };
  const list = { id: 'results', hidden: true, contains: target => target === item,
    after() {}, replaceChildren() {}, querySelectorAll: () => [],
    addEventListener: (name, fn) => listEvents[name] = fn };
  const queries = [];
  const context = vm.createContext({
    document: { createElement: () => ({ setAttribute() {} }), addEventListener: (name, fn) => documentEvents[name] = fn },
    clearTimeout() {}, setTimeout() {}, AbortController,
    esc: s => s, readSearchHistory: () => ['서울역'], isSearchHistoryEnabled: () => true,
    rememberSearch() {}, getJSON: (_, params) => { queries.push(params.q); return new Promise(() => {}); },
  });
  const source = readFileSync(new URL('../frontend/js/search.js', import.meta.url), 'utf8')
    .replace(/^import .*;\r?\n/gm, '').replace('export function', 'function');
  vm.runInContext(source, context);
  context.createPlaceSearch(input, list, { onPick() {} });
  inputEvents.focus.forEach(fn => fn());
  assert.equal(list.hidden, false);
  documentEvents.pointerdown({ target: item });
  inputEvents.blur.forEach(fn => fn({ relatedTarget: null }));
  assert.equal(list.hidden, false);
  listEvents.click({ target: item });
  assert.deepEqual(queries, ['서울역']);
  documentEvents.pointerdown({ target: {} });
  assert.equal(list.hidden, true);
});
