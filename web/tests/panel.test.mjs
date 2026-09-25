import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
const source = readFileSync(new URL('../frontend/js/panel.js', import.meta.url), 'utf8').replace('export function', 'function');
function setup(mobile = false, { animated = false, reduced = false } = {}) {
  const classes = new Set(['panel-collapsed']);
  const events = {}, attrs = {};
  const handle = { addEventListener: (k, v) => events[k] = v,
    getBoundingClientRect: () => ({ bottom: dataset.panelSize === 'full' ? 112 : classes.has('panel-collapsed') ? 800 : 400 }),
    setAttribute: (k, v) => attrs[k] = v, setPointerCapture() {}, releasePointerCapture() {},
    classList: { add() {}, remove() {} } };
  const pending = [];
  const dataset = {};
  const styles = {};
  const frames = new Map();
  let clock = 0, frameId = 0;
  const flush = () => {
    while (frames.size) {
      clock += 50;
      const batch = [...frames.values()]; frames.clear();
      batch.forEach(fn => fn(clock));
    }
  };
  const media = { matches: mobile, addEventListener() {} };
  let skipped = 0;
  const context = vm.createContext({ matchMedia: query => query.includes('reduced-motion') ? { matches: reduced } : media,
    window: { innerHeight: 800 },
    performance: { now: () => clock },
    requestAnimationFrame: fn => { frames.set(++frameId, fn); return frameId; },
    cancelAnimationFrame: id => frames.delete(id),
    document: { getElementById: () => ({ getBoundingClientRect: () => ({ bottom: 64, height: classes.has('panel-collapsed') ? 0 : 400 }) }),
      body: { clientHeight: 800, dataset, style: { setProperty: (k, v) => styles[k] = v, removeProperty: k => delete styles[k] }, classList: {
      add: k => classes.add(k), remove: k => classes.delete(k), contains: k => classes.has(k),
      toggle(k, on) { if (on) classes.add(k); else classes.delete(k); } } } } });
  if (animated) context.document.startViewTransition = update => {
    pending.push(update);
    return { ready: Promise.resolve(), skipTransition() { skipped++; } };
  };
  vm.runInContext(source, context);
  const panel = context.initPanelHandle(handle);
  return { attrs, pending, panel, dataset, handle, styles, skipped: () => skipped, open: () => !classes.has('panel-collapsed'),
    frames, flush,
    emit: (type, extra = {}) => events[type]({ isPrimary: true, button: 0, pointerId: 1, clientX: 100, clientY: 100, detail: 1, ...extra }) };
}
test('click and keyboard toggle the panel and accessibility state', () => {
  const s = setup();
  s.emit('click'); assert.equal(s.open(), true); assert.equal(s.attrs['aria-expanded'], 'true');
  s.emit('click', { detail: 0 }); assert.equal(s.open(), false);
});

test('rapid toggles during a pending transition keep the latest requested state', () => {
  const s = setup(false, { animated: true });
  s.emit('click'); s.emit('click');
  assert.equal(s.skipped(), 1);
  for (const update of s.pending) update();
  assert.equal(s.open(), false);
  assert.equal(s.attrs['aria-expanded'], 'false');
});

test('reduced motion changes the panel immediately without a transition', () => {
  const s = setup(false, { animated: true, reduced: true });
  s.emit('click');
  assert.equal(s.open(), true);
  assert.equal(s.pending.length, 0);
});
test('inward/outward drags open/close and the following click is ignored', () => {
  for (const mobile of [false, true]) {
    const s = setup(mobile);
    s.emit('pointerdown');
    s.emit('pointerup', mobile ? { clientY: 50 } : { clientX: 150 });
    s.emit('click'); assert.equal(s.open(), true);
    s.emit('pointerdown');
    s.emit('pointerup', mobile ? { clientY: 150 } : { clientX: 50 });
    s.emit('click'); assert.equal(s.open(), false);
  }
});
test('cancelled, perpendicular and short drags do not open the panel', () => {
  const s = setup();
  s.emit('pointerdown'); s.emit('pointercancel'); s.emit('pointerup', { clientX: 150 });
  assert.equal(s.open(), false);
  for (const point of [{ clientY: 150 }, { clientX: 115 }]) {
    s.emit('pointerdown'); s.emit('pointerup', point); s.emit('click');
    assert.equal(s.open(), false);
  }
});

const swipe = (s, up) => {
  s.emit('pointerdown');
  s.emit('pointerup', { clientY: up ? 50 : 150 });
  s.emit('click');
};

test('mobile drag follows the pointer, clamps height and clears preview on release or cancellation', () => {
  const s = setup(true);
  s.emit('pointerdown');
  s.emit('pointermove', { clientY: 40 });
  assert.equal(s.styles['--panel-drag-height'], '60px');
  s.emit('pointermove', { clientY: -600 });
  assert.equal(s.styles['--panel-drag-height'], '400px');
  s.emit('pointerup', { clientY: 40 });
  s.flush();
  assert.equal(s.dataset.panelSize, 'compact');
  assert.equal(s.styles['--panel-drag-height'], undefined);
  s.panel.showSelection();
  s.emit('pointerdown');
  s.emit('pointermove', { clientY: 0 });
  assert.equal(s.styles['--panel-drag-height'], '500px');
  s.emit('pointercancel');
  assert.equal(s.styles['--panel-drag-height'], undefined);
  assert.equal(s.dataset.panelSize, 'half');
});

test('initial results resize during pointer movement', () => {
  const s = setup(true);
  s.panel.showResults();
  s.emit('pointerdown');
  s.emit('pointermove', { clientY: 200 });
  assert.equal(s.styles['--panel-drag-height'], '588px');
});

test('upward selection drag stops below the floating search and handle space', () => {
  const s = setup(true);
  s.panel.showSelection();
  s.emit('pointerdown');
  s.emit('pointermove', { clientY: -700 });
  assert.equal(s.styles['--panel-drag-height'], '688px');
  s.emit('pointerup', { clientY: -700 });
  assert.equal(s.dataset.panelSize, 'full');
  s.emit('pointerdown');
  s.emit('pointermove', { clientY: 0 });
  assert.equal(s.styles['--panel-drag-height'], '688px');
});

test('dragging from full screen keeps the grabbed handle position under the pointer', () => {
  const s = setup(true);
  s.panel.showSelection();
  swipe(s, true);
  assert.equal(s.dataset.panelSize, 'full');
  // Full-screen handle occupies y=68..112; grab 12px above its bottom.
  s.emit('pointerdown', { clientY: 100 });
  for (const y of [110, 200, 350]) {
    s.emit('pointermove', { clientY: y });
    const handleBottom = 800 - parseFloat(s.styles['--panel-drag-height']);
    assert.equal(handleBottom - 12, y);
  }
  s.emit('pointerup', { clientY: 350 });
  assert.equal(s.dataset.panelSize, 'half');
  assert.equal(s.styles['--panel-drag-height'], undefined);
});

test('map layout preserves center on resize and compensates desktop panel width without accumulating shifts', () => {
  const app = readFileSync(new URL('../frontend/js/app.js', import.meta.url), 'utf8');
  const layout = app.slice(app.indexOf('let panelMapOffset = 0;'), app.indexOf('const panel = initPanelHandle'));
  let closed = true, mobile = false;
  const pans = [], invalidations = [];
  const context = vm.createContext({
    view: { map: { invalidateSize: options => invalidations.push(options), panBy: point => pans.push(Array.from(point)) } },
    matchMedia: () => ({ matches: mobile }),
    document: { body: { classList: { contains: () => closed } } },
    $: () => ({ getBoundingClientRect: () => ({ right: 436 }) }),
  });
  vm.runInContext(layout, context);
  context.updateMapLayout();
  closed = false;
  context.updateMapLayout(); context.updateMapLayout();
  closed = true;
  context.updateMapLayout();
  mobile = true; closed = false;
  context.updateMapLayout();
  assert.deepEqual(pans, [[-218, 0], [218, 0]]);
  assert.ok(invalidations.every(options => options.pan && !options.animate));
});

test('mobile starts closed; first point opens compact, upward drag expands only to half', () => {
  const s = setup(true);
  s.panel.reset();
  assert.equal(s.open(), false);
  s.panel.reset({ pointChanged: true });
  assert.equal(s.dataset.panelSize, 'compact');
  assert.equal(s.open(), true);
  s.emit('click'); assert.equal(s.open(), false);
  s.emit('click'); assert.equal(s.dataset.panelSize, 'compact');
  swipe(s, true); assert.equal(s.dataset.panelSize, 'half');
  swipe(s, true); assert.equal(s.dataset.panelSize, 'half');
});

test('endpoint changes reopen the closed desktop panel', () => {
  const s = setup();
  s.panel.reset(); assert.equal(s.open(), false);
  s.panel.reset({ pointChanged: true }); assert.equal(s.open(), true);
  s.emit('click');
  s.panel.reset({ pointChanged: true }); assert.equal(s.open(), true);
});

test('editing endpoints keeps an open setup panel still without another animation', () => {
  const s = setup(true, { animated: true });
  s.panel.reset({ pointChanged: true });
  s.pending.shift()();
  s.panel.reset({ pointChanged: true });
  assert.equal(s.pending.length, 0);
  assert.equal(s.dataset.panelSize, 'compact');
  swipe(s, true);
  assert.equal(s.pending.length, 0);
  s.panel.reset({ pointChanged: true });
  assert.equal(s.pending.length, 0);
  assert.equal(s.dataset.panelSize, 'half');
});

test('endpoint changes reopen a closed mobile panel at compact size every time', () => {
  const s = setup(true);
  for (let i = 0; i < 3; i++) {
    s.panel.reset();
    assert.equal(s.open(), false);
    s.panel.reset({ pointChanged: true });
    assert.equal(s.open(), true);
    assert.equal(s.dataset.panelSize, 'compact');
    s.emit('click');
  }
  s.panel.showSelection();
  s.emit('click');
  s.panel.reset({ pointChanged: true });
  assert.equal(s.dataset.panelSize, 'compact');
  assert.equal(s.open(), true);
});

test('mobile results support full, half, compact and closed before selection', () => {
  const s = setup(true);
  s.panel.showResults();
  assert.equal(s.dataset.panelSize, 'full');
  assert.equal(s.handle.hidden, false);
  swipe(s, false);
  assert.equal(s.dataset.panelSize, 'half');
  swipe(s, false);
  assert.equal(s.dataset.panelSize, 'compact');
  swipe(s, false);
  assert.equal(s.dataset.panelSize, 'closed');
  swipe(s, true);
  assert.equal(s.dataset.panelSize, 'compact');
  swipe(s, true);
  assert.equal(s.dataset.panelSize, 'half');
  swipe(s, true);
  assert.equal(s.dataset.panelSize, 'full');
  s.panel.reset();
  assert.equal(s.dataset.panelSize, 'compact');
  assert.equal(s.handle.hidden, false);
});

test('selection draws after half layout and supports full, half, closed stages', () => {
  const s = setup(true);
  s.panel.showResults();
  let drawn = 0;
  s.panel.showSelection(() => { assert.equal(s.dataset.panelSize, 'half'); drawn++; });
  assert.equal(drawn, 1);
  swipe(s, true); assert.equal(s.dataset.panelSize, 'full');
  swipe(s, false); assert.equal(s.dataset.panelSize, 'half');
  swipe(s, false); assert.equal(s.dataset.panelSize, 'compact');
  swipe(s, false); assert.equal(s.dataset.panelSize, 'closed');
  swipe(s, true); assert.equal(s.dataset.panelSize, 'compact');
  swipe(s, true); assert.equal(s.dataset.panelSize, 'half');
  s.emit('click'); assert.equal(s.dataset.panelSize, 'closed');
  s.emit('click'); assert.equal(s.dataset.panelSize, 'half');
});

test('reset cancels a queued selection drawing after coordinates change', () => {
  const s = setup(true, { animated: true });
  let drawn = false;
  s.panel.showSelection(() => drawn = true);
  s.panel.reset();
  for (const update of s.pending) update();
  assert.equal(drawn, false);
  assert.equal(s.dataset.panelPhase, 'setup');
});

test('switching selected routes preserves panel size without starting a transition', () => {
  const s = setup(true, { animated: true });
  s.panel.showSelection(() => {});
  s.pending.shift()();
  swipe(s, true);
  assert.equal(s.pending.length, 0);
  assert.equal(s.dataset.panelSize, 'full');
  let drawn = false;
  s.panel.showSelection(() => drawn = true);
  assert.equal(drawn, true);
  assert.equal(s.dataset.panelSize, 'full');
  assert.equal(s.pending.length, 0);
});

test('mobile drag release snaps without replaying the panel entrance animation', () => {
  const s = setup(true, { animated: true });
  s.panel.showResults();
  s.pending.shift()();
  for (const y of [200, 200, 200, 0, 0, 0, 85]) {
    s.emit('pointerdown');
    s.emit('pointermove', { clientY: y });
    s.emit('pointerup', { clientY: y });
    s.flush();
    s.emit('click');
    assert.equal(s.pending.length, 0);
    assert.equal(s.styles['--panel-drag-height'], undefined);
  }
  assert.equal(s.dataset.panelSize, 'full');
});

test('settling animates height and respects reduced motion', () => {
  for (const reduced of [false, true]) {
    const s = setup(true, { reduced });
    s.emit('pointerdown');
    s.emit('pointermove', { clientY: 40 });
    s.emit('pointerup', { clientY: 40 });
    assert.equal(s.frames.size, reduced ? 0 : 1);
    s.flush();
    assert.equal(s.styles['--panel-drag-height'], undefined);
    assert.equal(s.dataset.panelSize, 'compact');
  }
});
