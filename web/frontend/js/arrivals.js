import { getJSON, esc } from './api.js';

export function boardingPoints(route) {
  return (route.steps || []).flatMap((step, index) => {
    if (!['BUS', 'SUBWAY'].includes(step.type)) return [];
    const board = step.resolution?.board;
    const id = board?.status === 'matched' ? board.chosen?.id : null;
    return [{ step, index, name: step.stops?.[0] || step.board_name || '승차 지점',
      url: id ? `/api/arrivals/${step.type === 'BUS' ? 'stop' : 'station'}/${encodeURIComponent(id)}` : null }];
  });
}
export async function fetchBoardings(points, fetcher = getJSON) {
  const requests = new Map();
  for (const { url } of points) if (url && !requests.has(url)) {
    requests.set(url, fetcher(url).then(data => ({ data, at: Date.now() }), error => ({ error: error.message })));
  }
  await Promise.all(requests.values());
  return Promise.all(points.map(async point => {
    const result = point.url ? await requests.get(point.url) : { error: '승차 정류장이 정확하게 매칭되지 않아 조회할 수 없습니다.' };
    if (point.step.type !== 'SUBWAY' || !result.data) return result;
    // 기존 서버/캐시에 남은 모순된 0초 응답도 현재 역 도착으로 표시하지 않는다.
    return { ...result, data: { ...result.data, items: (result.data.items || []).map(item =>
      item.eta_s === 0 && item.n_stops_ahead > 0 ? { ...item, eta_s: null } : item) } };
  }));
}
export function matchingBusItems(point, items) {
  const ids = point.step.route_ids || [];
  return items.filter(item => item.route_id && ids.includes(item.route_id));
}
const snapshots = new WeakMap();
// 0초/문구만 있는 도착 상태는 정확한 출발 시각이 아니므로 잠깐 유지한다.
const statusOnlyArrival = item => item.eta_s === 0 || (item.eta_s == null && /곧\s*도착/.test(item.message || ''));
const minutes = seconds => seconds == null ? '시간 정보 없음' : seconds < 0 ? '-분' : seconds === 0 ? '곧 도착' : `${Math.ceil(seconds / 60)}분`;
export function elapsedItems(result, now = Date.now()) {
  const elapsed = result?.at == null ? 0 : Math.max(0, Math.floor((now - result.at) / 1000));
  return (result?.data?.items || []).map(item => ({ ...item,
    eta_s: statusOnlyArrival(item) ? (elapsed < 120 ? item.eta_s : -1) : item.eta_s == null ? null : item.eta_s - elapsed,
    expired: statusOnlyArrival(item) ? elapsed >= 120 : item.eta_s != null && item.eta_s - elapsed < 0,
  }));
}
const stationName = name => (name || '').replace(/\([^)]*\)|\s/g, '').replace(/역$/, '');
export function subwayDirection(point, item) {
  const next = stationName(point.step.stops?.[1]);
  const toward = stationName(item.headsign?.match(/-\s*(.+?)방면/)?.[1]);
  return !next || !toward ? null : next === toward;
}
const time = at => new Date(at).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
export function nextArrivalDelay(results, now = Date.now()) {
  let next = Infinity;
  for (const result of results) {
    if (result?.at == null) continue;
    const elapsed = Math.max(0, Math.floor((now - result.at) / 1000));
    for (const item of result.data?.items || []) {
      if (statusOnlyArrival(item)) {
        if (elapsed < 120) next = Math.min(next, result.at + 120000 - now);
        continue;
      }
      const soon = /곧\s*도착/.test(item.message || '');
      const eta = Number.isFinite(item.eta_s) ? item.eta_s : soon ? 0 : null;
      if (eta == null || eta - elapsed < 0) continue;
      const left = eta - elapsed;
      const boundary = soon || left <= 120 ? Math.floor(eta) + 1
        : elapsed + Math.ceil(left - (Math.ceil(left / 60) - 1) * 60);
      next = Math.min(next, result.at + boundary * 1000 - now);
    }
  }
  return Number.isFinite(next) ? Math.max(1, next) : null;
}
export function vehicleArrival(point, name, items) {
  const candidates = point.step.type === 'SUBWAY'
    ? items.filter(item => subwayDirection(point, item) === true)
    : matchingBusItems(point, items).filter(item => item.route_name === name);
  const known = candidates.filter(item => Number.isFinite(item.eta_s) || arrivingSoon(item) || item.expired);
  known.sort((a, b) => (a.eta_s ?? 0) - (b.eta_s ?? 0));
  if (known.length) return { text: known[0].expired ? '-분' : arrivingSoon(known[0]) ? '곧 도착' : minutes(known[0].eta_s), live: true };
  if (point.step.type === 'SUBWAY') {
    const wait = point.step.headway_m > 0 ? point.step.headway_m / 2 : null;
    return { text: wait == null ? '-' : `${Math.round(wait * 10) / 10}분`, live: false };
  }
  const headways = (point.step.bus_headways || []).filter(item => item.name === name && item.headway_m > 0);
  const wait = headways.length ? headways.reduce((sum, item) => sum + item.headway_m, 0) / headways.length / 2 : null;
  return { text: wait == null ? '-' : `${Math.round(wait * 10) / 10}분`, live: false };
}
export function arrivalGroups(point, items) {
  const matched = new Set(matchingBusItems(point, items));
  const groups = new Map();
  for (const item of items) {
    const name = point.step.type === 'BUS' ? item.route_name || '노선 정보 없음'
      : [item.direction, item.headsign || item.dest, item.train_type].filter(Boolean).join(' · ') || '방향 정보 없음';
    const key = `${item.source || ''}:${item.route_id || ''}:${name}`;
    if (!groups.has(key)) groups.set(key, { name, selected: point.step.type === 'BUS' ? matched.has(item) : subwayDirection(point, item), rows: [] });
    groups.get(key).rows.push(item);
  }
  for (const group of groups.values()) group.rows.sort((a, b) => (a.eta_s ?? Infinity) - (b.eta_s ?? Infinity));
  return [...groups.values()].sort((a, b) => Number(b.selected) - Number(a.selected));
}
export const arrivingSoon = item => !item.expired && !(item.eta_s < 0) && ((Number.isFinite(item.eta_s) && item.eta_s <= 120) || /곧\s*도착/.test(item.message || ''));
export function arrivalHeadway(point, item) {
  const value = item.headway_m ?? (point.step.bus_headways || []).find(row => row.id === item.route_id)?.headway_m;
  return Number.isFinite(value) && value > 0 ? `배차간격 약 ${Math.round(value * 10) / 10}분` : '배차간격 정보 없음';
}
export function vehicleOrder(point, names, items) {
  return names.map((name, index) => {
    const value = vehicleArrival(point, name, items);
    const eta = matchingBusItems(point, items).filter(item => item.route_name === name && !item.expired && item.eta_s != null && item.eta_s >= 0).map(item => item.eta_s);
    const seconds = value.live ? (eta.length ? Math.min(...eta) : value.text === '곧 도착' ? 0 : Infinity)
      : value.text === '-' ? Infinity : parseFloat(value.text) * 60;
    return { index, live: value.live, seconds };
  }).sort((a, b) => Number(b.live) - Number(a.live) || a.seconds - b.seconds || a.index - b.index).map(item => item.index);
}
let dialog;
function showArrivals(point, result, styles) {
  if (!dialog) {
    dialog = document.createElement('dialog');
    dialog.className = 'arrivals-dialog';
    document.body.append(dialog);
    dialog.addEventListener('click', e => { if (e.target === dialog) dialog.close(); });
    dialog.addEventListener('close', () => clearTimeout(dialog.countdown));
  }
  const items = elapsedItems(result);
  const groups = arrivalGroups(point, items);
  const renderGroup = group => {
    const item = group.rows[0];
    const color = styles.busColor?.(item.source === 'seoul' && item.route_type === '마을' ? '지선' : item.route_type);
    const badge = point.step.type === 'BUS' && color ? ` class="chip" style="${styles.chipStyle(color)}"` : '';
    const headway = point.step.type === 'BUS' ? `<small class="arrival-headway">${esc(arrivalHeadway(point, item))}</small>` : '';
    return `<section><strong${badge}>${esc(group.name)}</strong>${headway}${group.rows.slice(0, 2).map((item, i) =>
      `<p>${i ? '다음 차' : '이번 차'} · <span data-arrival-index="${items.indexOf(item)}"></span>${item.n_stops_ahead != null ? ` · ${esc(item.n_stops_ahead)}개 전` : ''}${item.seats != null ? ` · 잔여 ${esc(item.seats)}석` : ''}${item.message && !/^곧\s*도착$/.test(item.message) ? `<small>${esc(item.message)} (조회 당시)</small>` : ''}</p>`).join('')}${group.rows.length < 2 ? '<small>다음 차 정보 없음</small>' : ''}</section>`;
  };
  const rows = point.step.type === 'BUS' ? [true, false].map(selected => {
    const list = groups.filter(group => group.selected === selected);
    return `<div class="arrival-group${selected ? ' arrival-recommended' : ''}"><h3>${selected ? '선택 경로에서 탈 버스' : '선택 경로에 포함되지 않은 버스'}</h3>${list.map(renderGroup).join('') || '<p class="muted">도착정보 없음</p>'}</div>`;
  }).join('') : [true, false, null].map(selected => {
    const list = groups.filter(group => group.selected === selected);
    if (!list.length) return '';
    return `<div class="arrival-group${selected === true ? ' arrival-recommended' : ''}"><h3>${selected === true ? '선택 경로와 같은 방향' : selected === false ? '선택 경로와 다른 방향' : '방향 확인 필요'}</h3>${list.map(renderGroup).join('')}</div>`;
  }).join('');
  dialog.innerHTML = `<header><strong>${esc(point.name)}</strong><button type="button" aria-label="도착정보 닫기">×</button></header>
    <p class="muted">${result?.at ? `${time(result.at)} 조회 · 현재 정류장 도착정보` : '새로고침을 누르면 실시간 정보를 조회합니다.'}</p>
    <p class="muted">탑승 가능 여부나 환승 대기시간 예측이 아닙니다.</p>
    ${point.step.type === 'SUBWAY' ? `<p class="arrival-recommended">${esc(point.name)} → ${esc(point.step.stops?.[1] || '?')} 방면 · ${esc(point.step.stops?.at(-1) || point.step.alight_name || '?')} 하차<br><small>같은 방향도 종착역·급행 정차역을 확인하세요.</small></p>` : ''}
    ${result?.error ? `<p>${esc(result.error)}</p>` : rows || '<p>도착정보 없음</p>'}
    ${(result?.data?.failed || []).map(f => `<p>${esc(f.message)}</p>`).join('')}`;
  dialog.querySelector('button').addEventListener('click', () => dialog.close());
  if (!dialog.open) dialog.showModal();
  const update = () => {
    const current = elapsedItems(result);
    dialog.querySelectorAll('[data-arrival-index]').forEach(label => {
      const item = current[Number(label.dataset.arrivalIndex)];
      label.textContent = item.expired ? '-분' : arrivingSoon(item) ? '곧 도착' : minutes(item.eta_s);
      label.classList.toggle('arrival-soon', arrivingSoon(item));
    });
    const delay = nextArrivalDelay([result]);
    if (delay != null) dialog.countdown = setTimeout(() => { if (dialog.open) update(); }, delay);
  };
  clearTimeout(dialog.countdown);
  update();
}

export function mountArrivals(detail, route, styles = {}) {
  const points = boardingPoints(route);
  if (!points.length) return;
  const state = snapshots.get(route) || { results: [], busy: false, last: 0 };
  snapshots.set(route, state);
  const toolbar = document.createElement('div');
  toolbar.className = 'mobile-arrivals arrival-refresh';
  toolbar.innerHTML = '<span role="status"></span><button type="button" aria-label="실시간 새로고침" title="실시간 새로고침">↻</button>';
  detail.querySelector('.tl-node:first-child .tl-name').append(toolbar);
  const refresh = toolbar.querySelector('button'), status = toolbar.querySelector('span');
  const buttons = points.map((point, i) => {
    const segment = detail.querySelector(`[data-step="${point.index}"]`);
    if (!segment) return null;
    const node = segment.previousElementSibling;
    const old = node?.classList.contains('tl-node') ? node.querySelector('.tl-wait') : null;
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'mobile-arrivals arrival-link';
    button.textContent = '실시간';
    old?.classList.toggle('arrival-old-wait', i !== 0 || !!state.results[i]?.data);
    button.addEventListener('click', () => showArrivals(point, state.results[i], styles));
    (node?.querySelector('.tl-name') || segment.querySelector('.tl-info')).insertAdjacentElement('beforeend', button);
    const vehicles = [...segment.querySelectorAll('[data-vehicle]')].map(vehicle => {
      vehicle.querySelector('.tl-vtype')?.classList.add('arrival-old-wait');
      const label = document.createElement('span');
      label.className = 'mobile-arrivals vehicle-arrival';
      vehicle.append(label);
      return { label, vehicle, name: vehicle.dataset.vehicle };
    });
    return { button, old, point, vehicles };
  });
  const paint = () => {
    refresh.disabled = state.busy;
    status.textContent = state.busy ? '조회 중…' : state.last ? `${time(state.last)} 조회` : '버튼을 눌러 갱신';
    buttons.forEach((entry, i) => {
      if (!entry) return;
      const result = state.results[i];
      const items = elapsedItems(result);
      if (entry.point.step.type === 'BUS') {
        vehicleOrder(entry.point, entry.vehicles.map(v => v.name), items).forEach((index, rank) => {
          entry.vehicles[index].vehicle.style.setProperty('--arrival-order', rank);
        });
      }
      entry.old?.classList.toggle('arrival-old-wait', i !== 0 || !!result?.data);
      for (const { label, name } of entry.vehicles) {
        const value = vehicleArrival(entry.point, name, items);
        label.textContent = value.text;
        label.classList.toggle('live', value.live);
        label.classList.toggle('arrival-soon', value.text === '곧 도착');
        label.dataset.tip = value.live ? '' : value.text === '-' ? '배차 정보 없음' : '해당 노선 배차간격의 절반으로 추정';
      }
    });
  };
  clearTimeout(state.countdown);
  state.paint = () => {
    clearTimeout(state.countdown);
    if (!detail.isConnected || !toolbar.isConnected) return;
    paint();
    const delay = nextArrivalDelay(state.results);
    if (delay != null) {
      state.countdown = setTimeout(() => state.paint(), delay);
      state.countdown.unref?.();
    }
  };
  refresh.addEventListener('click', async () => {
    if (!detail.closest('.route-card.selected') || state.busy) return;
    if (Date.now() - state.last < 15000) { status.textContent = '15초 간격으로 갱신할 수 있습니다'; return; }
    state.busy = true; paint();
    try {
      state.results = await fetchBoardings(points);
      state.last = Date.now();
      for (const result of state.results) if (result.data?.quota) document.dispatchEvent(new CustomEvent('arrivals-quota', { detail: result.data.quota }));
    } finally { state.busy = false; state.paint(); }
  });
  state.paint();
}
