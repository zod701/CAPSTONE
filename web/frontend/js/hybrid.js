// 대중교통+택시(하이브리드) 결과 — 기준선 요약을 그리고, 카드는 대중교통 경로와 같은 목록에 넣는다(routes.js setExtraCards).
// 서버가 앵커마다 카카오를 부르므로(대중교통·자동차) 버튼을 눌렀을 때만 받아 온다.
// 탭·정렬·선택·펼치기는 목록이 한곳에서 한다 — 카드를 펼치면 대중교통 카드와 같은 구간 타임라인에 택시가 한 구간으로 들어간다.
import { esc } from "./api.js";
import { fmtMin, fmtWon, chips, setExtraCards, defaultWaitText, comparisonText, recommendationBadge } from "./routes.js";

// 최소 시간 대중교통 경로(서버 기준선)와 견준 값 — 절감 시간 · 추가 요금 · 원/분(시간을 줄이지 못하면 없음). 서버의 compare.saving_per_min 과 같은 식
function legText(r) {
  const leg = r.taxi;
  const distance = leg.distance_m == null ? "–" : `${(leg.distance_m / 1000).toFixed(1)} km`;
  return `대중교통 ${fmtMin(r.transit_time_s)} · 택시 ${fmtMin(leg.duration_s)} · ${distance}`;
}

// 대중교통 구간 사이에 택시를 한 구간(TAXI)으로 끼운 경로 — 타임라인·칩이 대중교통 카드와 같은 코드로 그린다.
// 서버가 준 자리(taxi_at)에 넣는다. from·to 가 없으면 출발지·목적지에서 바로 타고 내린다
function combinedRoute(r) {
  const leg = r.taxi;
  const taxi = { type: "TAXI", time_s: leg.duration_s, distance_m: leg.distance_m, path: leg.path, source: leg.source,
    fare: (leg.fare?.taxi ?? 0) + (leg.fare?.toll ?? 0), counted_s: r.taxi_counted_s,
    from_name: r.from?.name, to_name: r.to?.name };
  const steps = r.transit?.steps || [];
  return { steps: [...steps.slice(0, r.taxi_at), taxi, ...steps.slice(r.taxi_at)] };
}

function fareText(r) {
  const info = r.fare_info;
  if (info?.source !== "rules_range" || info.min === info.max) return fmtWon(r.fare);
  const other = r.fare - info.value;
  return `${fmtWon(other + info.min)}–${fmtWon(other + info.max)}`;
}

function fareMethod(r) {
  const info = r.fare_info;
  if (!info) return "대중교통 요금 · 경로 API 제공값";
  if (info.source === "fallback") return `대중교통 요금 대체값 적용 · ${info.reason}`;
  if (info.source === "rules_range") return "대중교통 요금 · 노선별 로컬 계산 · 비교는 최대 요금 기준";
  return "대중교통 요금 · 로컬 거리·환승 계산 · 성인 교통카드";
}

// key "h:번호" — 목록이 대중교통 카드("t:번호")와 같은 방식으로 고르고 펼친다
function cardHTML(r, i, base, vot) {
  const comparison = comparisonText(r, base);
  return `
    <div class="route-card hy-card" data-i="${i}" data-key="h:${i}">
      <button type="button" class="rc-head" aria-expanded="false">
        <span class="rc-top"><b class="rc-type">하이브리드</b>
          <span class="rc-times"><b class="rc-time">${fmtMin(r.time_s)}</b>
            <span class="rc-wait">${fareText(r)}</span></span>${recommendationBadge(`h:${i}`)}</span>
        ${defaultWaitText(r.transit)}
        <span class="rc-meta" title="${esc(r.fare_info?.reason || fareMethod(r))}">${esc(fareMethod(r))}</span>
        <span class="rc-meta">${legText(r)}</span>
        ${comparison ? `<span class="rc-meta hy-vs" title="최소시간 대중교통 대비">${comparison}</span>` : ""}
        <span class="rc-chips">${chips(combinedRoute(r))}</span>
      </button>
      <div class="rc-detail" hidden></div>
    </div>`;
}

// 첫 승차 대기를 실시간으로 바꾼 기준 경로 수 (서버 진단 — 실시간이 없거나 못 맞춘 경로는 정적 배차 대기로 남는다)
const liveFirstWaits = (data) => ["realtime", "realtime_stops", "realtime+headway"]
  .reduce((n, k) => n + (data.diag.realtime?.[k] ?? 0), 0);

// 택시를 쓰는 최소 기준(분) — 서버가 검증 뒤 이만큼 빠르지 않은 후보를 뺀다
const minSaving = (data) => Math.round((data.diag.min_saving_s ?? 300) / 60);

// el 에는 비교 대상 요약만 그리고, 카드는 결과 목록에 넣는다. onSelect(번호) — 카드를 고르면 지도에 그린다.
// 카드는 서버 기준선(대중교통 최소 시간 경로)과 견준다
export function renderHybrid(el, data, onSelect) {
  const base = data.baseline;
  // 한 줄에는 비교 대상만 — 나머지(추천 기준 · 첫 대기 실시간 · 확인한 앵커 · 뺀 후보)는 ⓘ 에 마우스를 올리면 한 줄씩 보인다
  const notes = [
    `추천: 전체 조회 결과 중 요금 + 시간 × 시간 가치(${Math.round(data.vot).toLocaleString("ko-KR")}원/분)가 가장 낮은 경로`,
    liveFirstWaits(data) ? `첫 대기 실시간 ${liveFirstWaits(data)}개 경로` : null,
    `후보 ${data.diag.search.candidates.toLocaleString("ko-KR")}개 중 ${data.diag.picked}개 확인 · 결과 ${data.diag.verified}개 (질의 ${data.diag.search.queries}/${data.diag.search.budget}회)`,
    data.diag.too_little_saving ? `시간 절약 기준 미달 ${data.diag.too_little_saving}개 제외 (택시 ${minSaving(data)}분 이상)` : null,
  ].filter(Boolean).join("\n");
  const head = `<p class="hy-base">비교 <b>최소 시간 경로 ${fmtMin(base.time_s)}</b> · ${fmtWon(base.fare)}`
    + ` ${defaultWaitText(base.route)} <span class="hy-info" tabindex="0" role="img" aria-label="${esc(notes)}" title="${esc(notes)}">ⓘ</span></p>`;
  if (!data.routes.length) {
    const why = data.diag.too_little_saving
      ? `시간 절약 기준을 충족하는 경로가 없습니다 (택시 ${minSaving(data)}분 이상).` : "조건에 맞는 하이브리드 경로를 찾지 못했습니다.";
    el.innerHTML = head + `<p class="note">${why}</p>`;
  } else {
    el.innerHTML = head + (data.failed.length ? `<p class="muted">실패한 조회 ${data.failed.length}건</p>` : "");
  }
  setExtraCards(data.routes.map((r, i) => ({ html: cardHTML(r, i, base, data.vot), route: combinedRoute(r),
    time_s: r.time_s, fare: r.fare, generalized_cost: (r.fare ?? 0) + r.time_s / 60 * data.vot,
    onSelect: () => onSelect(i) })));
}
