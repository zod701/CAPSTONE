import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../frontend/js/routes.js', import.meta.url), 'utf8');
const context = vm.createContext({ fmtMin: seconds => `${Math.round(seconds / 60)}분`,
  fmtWon: value => `${value.toLocaleString('ko-KR')}원` });
vm.runInContext(source.slice(source.indexOf('export function comparisonText'),
  source.indexOf('export function renderCarCard')).replaceAll('export ', ''), context);
const car = { duration_s: 2400, fare: { taxi: 12000, toll: 1000 } };
const base = { time_s: 3600, fare: 2000 };

test('taxi compares additional fare including toll against minutes saved', () => {
  assert.equal(context.taxiComparisonText(car, base),
    '20분 단축 · 11,000원 추가 · 단축 1분당 550원 추가');
});
test('equal or slower journeys never divide by zero or show negative time efficiency', () => {
  for (const time_s of [2400, 1800]) {
    const text = context.taxiComparisonText(car, { ...base, time_s });
    if (time_s === 2400) assert.match(text, /소요시간 동일/);
    else assert.equal(text, '');
    assert.doesNotMatch(text, /Infinity|NaN/);
  }
});
test('unavailable transit or fares do not fabricate a comparison', () => {
  assert.equal(context.taxiComparisonText(car, null), '');
  assert.equal(context.taxiComparisonText(car, { ...base, fare: null }), '');
  assert.equal(context.taxiComparisonText({ ...car, fare: { taxi: null, toll: 0 } }, base), '');
});
test('cheaper taxi keeps the same signed cost convention as hybrid cards', () => {
  assert.match(context.taxiComparisonText(car, { ...base, fare: 15000 }), /2,000원 절약 · 단축 1분당 100원 절약/);
});

test('slower cheaper transit shows savings per additional minute', () => {
  assert.equal(context.comparisonText({ time_s: 4200, fare: 1000 }, base),
    '10분 더 걸림 · 1,000원 절약 · 추가 1분당 100원 절약');
  assert.match(context.comparisonText({ time_s: 4200, fare: 2000 }, base), /요금 동일$/);
});
