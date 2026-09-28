import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../frontend/js/hybrid.js', import.meta.url), 'utf8');
const context = vm.createContext({ fmtWon: value => `${value.toLocaleString('ko-KR')}원` });
vm.runInContext(source.slice(source.indexOf('function fareText('), source.indexOf('// key "h:')), context);

test('total fare range includes taxi and toll once', () => {
  const row = { fare: 12850, fare_info: { source: 'rules_range', value: 1850, min: 1750, max: 1850 } };
  assert.equal(context.fareText(row), '12,750원–12,850원');
  assert.match(context.fareMethod(row), /노선별 로컬 계산/);
  assert.match(context.fareMethod(row), /최대 요금/);
});

test('equal endpoints collapse to one fare and walking adds no taxi fare', () => {
  assert.equal(context.fareText({ fare: 1550,
    fare_info: { source: 'rules_range', value: 1550, min: 1550, max: 1550 } }), '1,550원');
  assert.equal(context.fareText({ fare: 1850,
    fare_info: { source: 'rules_range', value: 1850, min: 1750, max: 1850 } }), '1,750원–1,850원');
});

test('fallback, local estimate and API fares identify their methods', () => {
  const row = { fare: 12550, fare_info: { source: 'fallback', reason: '구간 거리 미확인' } };
  assert.equal(context.fareText(row), '12,550원');
  assert.match(context.fareMethod(row), /대체값 적용 · 구간 거리 미확인/);
  assert.match(context.fareMethod({ fare_info: { source: 'rules_estimate' } }), /로컬 거리·환승 계산/);
  assert.match(context.fareMethod({}), /API 제공값/);
});
