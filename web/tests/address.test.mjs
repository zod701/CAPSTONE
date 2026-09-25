import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

test('address lookup ignores stale responses and keeps coordinates on empty or failed lookup', async () => {
  const source = readFileSync(new URL('../frontend/js/app.js', import.meta.url), 'utf8');
  const pending = [];
  let label = 'coordinates';
  const context = vm.createContext({
    getJSON: () => new Promise((resolve, reject) => pending.push({ resolve, reject })),
    renderQuota() {}, showLabel: (_, text) => label = text,
  });
  vm.runInContext(source.slice(source.indexOf('const addressRequests ='), source.indexOf('function setPoint(')), context);
  vm.runInContext('addressRequests.origin = 1', context);
  const old = context.resolvePointAddress('origin', { lat: 37, lng: 127 }, 1);
  vm.runInContext('addressRequests.origin = 2', context);
  const current = context.resolvePointAddress('origin', { lat: 38, lng: 127 }, 2);
  pending[1].resolve({ address: '새 주소' }); await current;
  pending[0].resolve({ address: '이전 주소' }); await old;
  assert.equal(label, '새 주소');
  const empty = context.resolvePointAddress('origin', {}, 2);
  pending[2].resolve({ address: '' }); await empty;
  assert.equal(label, '새 주소');
  const failed = context.resolvePointAddress('origin', {}, 2);
  pending[3].reject(new Error('offline')); await failed;
  assert.equal(label, '새 주소');
});
