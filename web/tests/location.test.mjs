import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../frontend/js/location.js', import.meta.url), 'utf8').replace('export function', 'function');
function setup({ secure = true, supported = true } = {}) {
  const elements = [], layers = [], watches = [], cleared = [], picks = [], moves = [], positions = [];
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
  context.initLocation(map, p => picks.push(p), p => positions.push(p));
  return { elements, layers, watches, cleared, picks, moves, positions, document, window,
    fix: (lat = 37.5) => watches.at(-1).ok({ coords: { latitude: lat, longitude: 127, accuracy: 15 } }) };
}

test('startup tracks and centers once without opening a popup or creating a GPS button', () => {
  const s = setup();
  assert.equal(s.watches.length, 1);
  assert.equal(s.elements.length, 3);
  s.fix(); s.fix(37.6);
  assert.equal(s.moves.length, 1);
  assert.equal(s.moves[0][0].lat, 37.5);
  assert.equal(s.picks.length, 0);
  s.layers[1].events.click();
  assert.equal(s.picks[0].lat, 37.6);
  assert.equal(s.positions[1].lat, 37.6);
  assert.equal(s.elements[1].hidden, true);
});

test('my location button zooms to latest fix without opening a popup or duplicating watches', () => {
  const s = setup();
  const click = () => s.elements[2].events.click();
  click(); click();
  assert.equal(s.watches.length, 1);
  s.fix(); s.fix(37.7); click();
  assert.equal(s.moves.at(-1)[0].lat, 37.7);
  assert.equal(s.moves.at(-1)[1], 16);
  assert.equal(s.picks.length, 0);
  s.watches[0].fail({ code: 3 });
  click();
  assert.equal(s.watches.length, 2);
  s.fix(37.8);
  assert.equal(s.moves.at(-1)[0].lat, 37.8);
});

test('background pauses and resumes tracking without recentering, ignoring late fixes', () => {
  const s = setup(); s.fix();
  const old = s.watches[0];
  s.document.hidden = true; s.document.events.visibilitychange();
  assert.deepEqual(s.cleared, [0]);
  s.document.hidden = false; s.document.events.visibilitychange();
  assert.equal(s.watches.length, 2);
  old.ok({ coords: { latitude: 1, longitude: 1, accuracy: 1 } });
  assert.equal(s.positions.at(-1), null);
  s.fix(37.7);
  assert.equal(s.positions.at(-1).lat, 37.7);
  assert.equal(s.moves.length, 1);
});

test('errors keep the default map and explain recovery without a removed GPS button', () => {
  for (const code of [1, 2, 3]) {
    const s = setup(); s.watches[0].fail({ code });
    assert.equal(s.moves.length, 0);
    assert.ok(s.elements[1].textContent.length);
    assert.ok(!s.elements[1].textContent.includes('GPS'));
  }
  for (const options of [{ secure: false }, { supported: false }]) {
    const s = setup(options);
    assert.equal(s.watches.length, 0);
    assert.ok(s.elements[1].textContent.length);
  }
});
