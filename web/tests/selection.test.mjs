import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const app = readFileSync(new URL('../frontend/js/app.js', import.meta.url), 'utf8');
const names = ['selectRoute', 'selectHybrid', 'dropHybrid', 'toggleCar'];
const source = names.map(name => app.match(new RegExp(`function ${name}\\([^]*?\\n\\}`))[0]).join('\n');

test('taxi, transit and hybrid selections replace each other on cards and map', () => {
  const state = { transit: { routes: [{}] }, hybrid: { routes: [{ anchor: {}, taxi: {}, transit: {} }] }, car: {}, carOn: false };
  let key = null, taxiSelected = false, transitDrawn = false, taxiDrawn = false;
  const context = vm.createContext({ state, $: id => id, revealSelectedCard() {},
    panel: { showSelection: draw => draw() }, L: { latLng: () => ({}) },
    R: {
      markSelected: (_, i) => key = `t:${i}`, markSelectedKey: k => key = k,
      markCarSelected: (_, on) => taxiSelected = on,
      clearCar: () => taxiDrawn = false, clearTransit: () => transitDrawn = false,
      drawCar: () => taxiDrawn = true, drawRoute: () => transitDrawn = true,
      drawDiagnostics() {}, renderDiagnostics() {},
    },
  });
  vm.runInContext(source, context);
  context.selectRoute(0);
  context.toggleCar();
  assert.equal(key, null);
  assert.equal(state.selectedIdx, null);
  assert.equal(taxiSelected && taxiDrawn && !transitDrawn, true);
  context.selectRoute(0);
  assert.equal(state.carOn, false);
  assert.equal(key, 't:0');
  assert.equal(!taxiSelected && !taxiDrawn && transitDrawn, true);
  context.toggleCar();
  context.selectHybrid(0);
  assert.equal(state.carOn, false);
  assert.equal(taxiSelected, false);
  assert.equal(key, 'h:0');
  context.toggleCar();
  assert.equal(state.hybridIdx, null);
  assert.equal(key, null);
  assert.equal(taxiSelected && taxiDrawn && !transitDrawn, true);
  context.toggleCar();
  assert.equal(taxiSelected || taxiDrawn || transitDrawn, false);
});
