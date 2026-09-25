import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../frontend/js/location.js', import.meta.url), 'utf8').replace('export function', 'function');
function setup({ secure = true, supported = true } = {}) {
  const elements = [], layers = [], watches = [], cleared = [], picks = [], moves = [];
  const element = () => ({ attrs: {}, events: {}, setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(k, v) { this.events[k] = v; } });
  const document = element(), window = element();
  window.isSecureContext = secure;
  const map = { on() {}, getZoom: () => 11, setView: (...args) => moves.push(args) };
  const layer = (ll) => {
    const item = { ll, events: {}, addTo() { return this; }, on(k, v) { this.events[k] = v; return this; },
      getLatLng() { return this.ll; }, setLatLng(p) { this.ll = p; return this; },
      setRadius(r) { this.radius = r; return this; }, remove() { this.removed = true; } };
    layers.push(item);
    return item;
  };
  const context = vm.createContext({ document, window,
    navigator: { geolocation: supported ? {
      watchPosition(ok, fail, options) { watches.push({ ok, fail, options }); return watches.length - 1; },
      clearWatch: id => cleared.push(id),
    } : undefined },
    L: { Control: { extend: spec => class { addTo() { spec.onAdd(); return this; } } },
      DomUtil: { create() { const el = element(); elements.push(el); return el; } },
      DomEvent: { disableClickPropagation() {}, disableScrollPropagation() {} },
      latLng: (lat, lng) => ({ lat, lng }), divIcon: v => v, circle: layer, marker: layer },
  });
  vm.runInContext(source, context);
  context.initLocation(map, p => picks.push(p));
  return { elements, layers, watches, cleared, picks, moves, document, window,
    click: () => elements[1].events.click(),
    fix: (lat = 37.5) => watches.at(-1).ok({ coords: { latitude: lat, longitude: 127, accuracy: 15 } }) };
}

test('GPS is opt-in; updates marker without moving map or reopening menu', () => {
  const s = setup();
  assert.equal(s.watches.length, 0);
  s.click(); s.fix(); s.fix(37.6);
  assert.equal(s.watches.length, 1);
  assert.equal(s.moves.length, 1);
  assert.equal(s.picks.length, 1);
  assert.equal(s.picks[0].lat, 37.5);
  s.layers[1].events.click();
  assert.equal(s.picks[1].lat, 37.6);
  assert.equal(s.layers[0].radius, 15);
  assert.equal(s.elements[2].hidden, true);
});

test('repeated GPS clicks recenter to the latest fix and reopen the menu without stopping', () => {
  const s = setup();
  s.click(); s.click();
  assert.equal(s.watches.length, 1);
  assert.equal(s.moves.length, 0);
  s.fix(); s.fix(37.6);
  s.click(); s.click();
  assert.equal(s.watches.length, 1);
  assert.deepEqual(s.cleared, []);
  assert.equal(s.moves.length, 3);
  assert.equal(s.moves[2][0].lat, 37.6);
  assert.equal(s.moves[2][1], 16);
  assert.equal(s.picks.length, 3);
  assert.equal(s.picks[2].lat, 37.6);
  assert.ok(s.layers.every(l => !l.removed));
});

test('background cleanup clears watch ID zero and ignores late fixes after restart', () => {
  const s = setup();
  s.click(); s.fix();
  const old = s.watches[0];
  s.document.hidden = true; s.document.events.visibilitychange();
  assert.deepEqual(s.cleared, [0]);
  assert.ok(s.layers.every(l => l.removed));
  s.document.hidden = false; s.document.events.visibilitychange();
  s.click();
  old.ok({ coords: { latitude: 1, longitude: 1, accuracy: 1 } });
  assert.equal(s.picks.length, 1);
  s.fix();
  assert.equal(s.picks.length, 2);
});

test('permission, unavailable and timeout errors reset GPS for retry', () => {
  for (const code of [1, 2, 3]) {
    const s = setup();
    s.click(); s.watches[0].fail({ code });
    assert.equal(s.elements[1].attrs['aria-pressed'], 'false');
    assert.ok(s.elements[2].textContent.length);
    s.click();
    assert.equal(s.watches.length, 2);
  }
});

test('hidden page stops tracking and does not automatically restart', () => {
  const s = setup();
  s.click(); s.fix();
  s.document.hidden = true; s.document.events.visibilitychange();
  s.document.hidden = false; s.document.events.visibilitychange();
  assert.deepEqual(s.cleared, [0]);
  assert.equal(s.watches.length, 1);
  assert.equal(s.elements[2].hidden, true);
});

test('insecure and unsupported browsers explain why GPS is unavailable', () => {
  for (const options of [{ secure: false }, { supported: false }]) {
    const s = setup(options);
    s.click();
    assert.equal(s.watches.length, 0);
    assert.ok(s.elements[2].textContent.length);
  }
});
