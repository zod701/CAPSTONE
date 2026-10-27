import { esc } from './api.js';

const SIDES = { head: '출발지에서 걸어 타는 직행 노선', tail: '목적지로 곧장 가는 직행 노선' };
const EXPLANATIONS = [
  '모든 후보는 출발지 → (대중교통) → u → 택시 1회 → v → (대중교통) → 목적지 꼴입니다. u는 출발지·기준 경로에서 내리는 정류소·걸어 타는 직행 노선의 하차 지점, v는 목적지·기준 경로의 승차 지점·목적지로 가는 직행 노선의 승차 지점이며 그 모든 짝을 만듭니다. 택시 직선 1.1 km 미만, 30분 초과, 택시가 0분이어도 5분을 줄일 수 없는 짝은 만들지 않습니다.',
  '확인한 결과의 시간·요금 Pareto를 보며 다음에 확인할 후보를 고릅니다. 추천 점수를 낮출 것으로 추정되는 후보, 없으면 아직 없는 절충안을 채울 후보가 먼저입니다. 질의 예산을 다 쓰면 나머지는 확인하지 않습니다. 이미 확인한 경로가 후보의 하한보다 빠르고 싸면 확인하지 않습니다.',
  '선택된 후보의 실제 자동차·대중교통 경로를 카카오맵 API로 조회합니다. 같은 질의를 쓰는 후보는 응답을 나눠 씁니다.',
  '최소시간 대중교통보다 5분 이상 빨라야 합니다. 요금이 저렴해도 이 조건을 통과하지 못하면 제외합니다.',
  '남은 후보를 검증 점수순으로 제시합니다. Pareto는 다른 경로가 시간·요금 모두 유리한지 표시할 뿐, 이 단계에서 후보를 제거하지 않습니다. 화면의 추천 배지는 별도로 사용자 VOT를 적용합니다.',
];
const num = n => Number.isFinite(n) ? Math.round(n).toLocaleString('ko-KR') : '–';
const minutes = n => Number.isFinite(n) ? `${(n / 60).toFixed(1)}분` : '미검증';

export function stageState(candidate, step, last) {
  if (candidate.stage < step) return 'past';
  if (candidate.stage === step && step < last) return 'removed';
  return 'kept';
}

export function comparisonCandidates(candidates, step, last, verify) {
  const score = c => step >= verify ? c.verified?.score ?? Infinity : c.estimated_score ?? Infinity;
  return candidates.filter(c => stageState(c, step, last) === 'kept')
    .sort((a, b) => score(a) - score(b));
}

export function createHybridDebug(el, map, onSelectResult, onAnchorClick, onSelectCandidate, transitStyle) {
  let trace = null, enabled = false, step = 0, selected = null, showMap = true;
  let side = 'head', bufferKind = 'bus';
  const layer = L.layerGroup();
  const colors = { kept: '#16884a', removed: '#df553d', past: '#888' };
  const pane = map.createPane('hybridDebugPane');
  pane.style.zIndex = '470';
  const renderer = L.svg({ pane: 'hybridDebugPane' });

  function selectCandidate(id) {
    selected = id || null;
    showMap = true;
    onSelectCandidate?.();
    render();
    paintMap(true);
  }

  function paintMap(fit = false) {
    layer.clearLayers();
    if (!enabled || !trace || !showMap) { layer.remove(); return; }
    layer.addTo(map);
    const bounds = [];
    const last = trace.stages.length - 1;
    const verify = trace.verify_stage;
    if (step === 0) {
      for (const buffer of trace.buffers?.[side]?.[bufferKind] || []) {
        const circle = L.circle([buffer.center[1], buffer.center[0]], {
          renderer, radius: buffer.radius_m, color: buffer.mode === 'taxi' ? '#2474db' : '#9455c6',
          weight: 2, fillOpacity: 0.06, dashArray: '6 5', interactive: false,
        }).addTo(layer);
        const box = circle.getBounds();
        if (!selected) bounds.push(box.getSouthWest(), box.getNorthEast());
      }
    }
    for (const c of trace.candidates) {
      if (selected && c.id !== selected) continue;
      const status = stageState(c, step, last);
      if (status === 'past' && c.id !== selected) continue;
      const points = c.connector.map(([lon, lat]) => [lat, lon]);
      bounds.push(...points);
      const choose = () => selectCandidate(c.id);
      const geometry = selected ? (step >= verify && c.verified?.geometry || c.geometry) : null;
      if (geometry?.length) {
        const routeColors = { TAXI: '#e5a000', WALKING: '#888', BUS: '#16884a', SUBWAY: '#2474db', TRANSIT: '#2474db' };
        for (const segment of geometry) {
          const path = segment.path.map(([lon, lat]) => [lat, lon]);
          bounds.push(...path);
          const color = ['BUS', 'SUBWAY'].includes(segment.type) ? transitStyle?.(segment).color : null;
          L.polyline(path, { renderer, bubblingMouseEvents: false, color: color || routeColors[segment.type],
            weight: 5, opacity: 0.95, dashArray: segment.type === 'WALKING' ? '3 5' : null })
            .addTo(layer);
        }
      } else {
        L.polyline(points, { renderer, bubblingMouseEvents: false, color: colors[status], weight: c.id === selected ? 7 : 3,
          opacity: c.id === selected ? 1 : 0.5, dashArray: '8 5' })
          .bindTooltip(`${esc(c.id)} · ${esc(c.name)}`).on('click', choose).addTo(layer);
      }
      for (const anchor of c.anchors || []) {
        L.circleMarker([anchor.lat, anchor.lon], { renderer, radius: 6, weight: 2,
          bubblingMouseEvents: false, color: colors[status], fillColor: colors[status], fillOpacity: 0.9 })
          .bindTooltip(`${esc(c.id)} · ${esc(anchor.name)}`)
          .on('click', () => { choose(); onAnchorClick?.(anchor); }).addTo(layer);
      }
    }
    for (const [label, point] of [['출발', trace.origin], ['도착', trace.destination]]) {
      const ll = [point[1], point[0]];
      bounds.push(ll);
      L.circleMarker(ll, { renderer, radius: 7, color: '#2474db', fillOpacity: 1, interactive: false })
        .bindTooltip(label).addTo(layer);
    }
    if (fit && bounds.length) map.fitBounds(bounds, { padding: [35, 35], maxZoom: 14 });
  }

  function render() {
    el.hidden = !enabled;
    if (!enabled) { layer.remove(); return; }
    if (!trace) {
      layer.remove();
      el.innerHTML = '<p class="muted">개발자 모드를 켠 채 ‘하이브리드 경로 탐색’을 누르면 처리 과정을 기록합니다.</p>';
      return;
    }
    const last = trace.stages.length - 1;
    const verify = trace.verify_stage;
    const stage = trace.stages[step];
    const chosen = trace.candidates.find(c => c.id === selected);
    const rows = comparisonCandidates(trace.candidates, step, last, verify);
    const metrics = trace.stages.map((s, i) => `<button type="button" data-stage="${i}" aria-pressed="${i === step}">${i + 1}. ${esc(s.name)}<small>${s.remaining}개 남음</small></button>`).join('');
    el.innerHTML = `<h3>하이브리드가 남는 과정</h3>
      <p>탐색 기준 ${num(trace.vot)}원/분 · 기준 대중교통 ${minutes(trace.baseline.time_s)}, ${num(trace.baseline.fare)}원</p>
      <div class="debug-stages">${metrics}</div>
      <div class="debug-controls"><button type="button" data-nav="-1" ${step === 0 ? 'disabled' : ''}>이전</button>
        <b>${step + 1} / ${trace.stages.length}</b><button type="button" data-nav="1" ${step === last ? 'disabled' : ''}>다음</button>
        <label><input type="checkbox" data-map ${showMap ? 'checked' : ''}>지도에 표시</label><button type="button" data-fit>전체 보기</button></div>
      <p>${EXPLANATIONS[step]}</p>
      ${step === 0 ? `<div class="debug-controls"><label>탐색 반경 <select data-side>${Object.entries(SIDES).map(([k, name]) => `<option value="${k}" ${side === k ? 'selected' : ''}>${name}</option>`).join('')}</select></label><label>정류장 종류 <select data-buffer-kind><option value="bus" ${bufferKind === 'bus' ? 'selected' : ''}>버스</option><option value="subway" ${bufferKind === 'subway' ? 'selected' : ''}>지하철</option></select></label></div>` : ''}
      <p class="debug-count">들어온 후보 ${stage.entered} → 제외 ${stage.removed} → 남은 후보 ${stage.remaining}</p>
      ${trace.omitted ? `<p class="debug-warning">후보 ${num(trace.total)}개 중 확인을 시도한 후보와 추정 점수 상위 ${trace.limit}개만 표·지도에 표시합니다. 단계 집계에는 전체 후보가 포함됩니다.</p>` : ''}
      <label>후보 선택 <select data-candidate><option value="">점수 상위 후보 보기</option>${trace.candidates.map(c => `<option value="${c.id}" ${selected === c.id ? 'selected' : ''}>${c.id} · ${esc(c.shape)} · ${esc(c.name)}${c.result_index != null ? ' · 최종 생존' : ''}</option>`).join('')}</select></label>
      ${chosen ? `<div class="debug-detail"><b>${esc(chosen.id)} · ${esc(chosen.name)}</b><p>${esc(chosen.shape)} · 택시 1회</p>
        <p>${step >= verify && chosen.verified?.itinerary ? '검증 경로' : '예상 경로'}: ${(step >= verify && chosen.verified?.itinerary || chosen.itinerary || []).map(esc).join(' → ')}</p>
        <p>${step < chosen.stage ? '현재 단계까지 유지 중' : esc(chosen.reason)}</p>
        ${step === last && chosen.result_index != null ? `<p>Pareto: ${chosen.pareto ? '시간·요금 모두 더 나은 다른 경로 없음' : '다른 경로가 시간·요금에서 우세 — 결과에는 유지'}</p><button type="button" data-result="${chosen.result_index}">최종 경로 카드·지도 보기</button>` : ''}</div>` : ''}
      <p>현재 단계의 생존 후보 전체 ${rows.length}개 · 점수순. 작은 점수가 유리합니다.</p>
      <div class="debug-table-wrap"><table><thead><tr><th>후보</th><th>추정 점수</th><th>검증 시간</th><th>요금</th><th>검증 점수</th></tr></thead><tbody>${rows.map(c => `<tr class="${selected === c.id ? 'debug-selected' : ''}"><td><button type="button" data-related="${c.id}">${c.id} ${esc(c.shape)}</button><small>${esc(c.name)}</small></td><td>${num(c.estimated_score)}</td><td>${step >= verify ? minutes(c.verified?.time_s) : '검증 전'}</td><td>${step >= verify ? num(c.verified?.fare) : '–'}</td><td>${step >= verify ? num(c.verified?.score) : '–'}</td></tr>`).join('')}</tbody></table></div>
      <p class="muted">점수 = 요금 + 시간(분) × ${num(trace.vot)}. 추정 점수는 검증 전 근삿값입니다. 슬라이더는 최종 카드 정렬·추천만 바꾸며 이 탐색 기록을 바꾸지 않습니다.</p>`;
    el.querySelectorAll('[data-stage]').forEach(b => b.addEventListener('click', () => { step = Number(b.dataset.stage); render(); }));
    el.querySelectorAll('[data-nav]').forEach(b => b.addEventListener('click', () => { step += Number(b.dataset.nav); render(); }));
    el.querySelector('[data-map]').addEventListener('change', e => { showMap = e.target.checked; paintMap(); });
    el.querySelector('[data-fit]').addEventListener('click', () => { showMap = true; render(); paintMap(true); });
    el.querySelector('[data-candidate]').addEventListener('change', e => selectCandidate(e.target.value));
    el.querySelector('[data-side]')?.addEventListener('change', e => { side = e.target.value; render(); });
    el.querySelector('[data-buffer-kind]')?.addEventListener('change', e => { bufferKind = e.target.value; render(); });
    el.querySelectorAll('[data-related]').forEach(b => b.addEventListener('click', () => {
      const scrollTop = el.querySelector('.debug-table-wrap').scrollTop;
      selectCandidate(b.dataset.related);
      el.querySelector('.debug-table-wrap').scrollTop = scrollTop;
    }));
    el.querySelector('[data-result]')?.addEventListener('click', e => { showMap = false; render(); onSelectResult(Number(e.target.dataset.result)); });
    paintMap();
  }
  return {
    enable(value) { enabled = value; render(); },
    setTrace(value) { trace = value || null; step = 0; selected = null; showMap = true; render(); },
    clear() { trace = null; selected = null; layer.clearLayers(); render(); },
    hideMap() { showMap = false; render(); },
  };
}
