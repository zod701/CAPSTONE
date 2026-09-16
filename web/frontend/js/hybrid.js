// 대중교통+택시(하이브리드) 결과 — 기준선 요약을 그리고, 카드는 대중교통 경로와 같은 목록에 넣는다(routes.js setExtraCards).
// 서버가 앵커마다 카카오를 부르므로(대중교통·자동차) 버튼을 눌렀을 때만 받아 온다.
// 탭·정렬·선택·펼치기는 목록이 한곳에서 한다 — 카드를 펼치면 대중교통 카드와 같은 구간 타임라인에 택시가 한 구간으로 들어간다.
import { esc } from "./api.js";
import { fmtMin, fmtWon, fmtDist, chips, setExtraCards } from "./routes.js";

// 최소 시간 대중교통 경로(서버 기준선)와 견준 값 — 절감 시간 · 추가 요금 · 원/분(시간을 줄이지 못하면 없음). 서버의 compare.saving_per_min 과 같은 식
function versus(r, base) {
  const saved = base.time_s - r.time_s;
  const extra = (r.fare ?? 0) - (base.fare ?? 0);
  return { saved, extra, wonPerMin: saved > 0 ? extra / (saved / 60) : null };
}

function versusText({ saved, extra, wonPerMin }) {
  const parts = [saved > 0 ? `${fmtMin(saved)} 절약` : `${fmtMin(-saved)} 더 걸림`,
    extra > 0 ? `${fmtWon(extra)} 추가` : `${fmtWon(-extra)} 절약`];
  if (wonPerMin != null) parts.push(`${Math.round(wonPerMin).toLocaleString("ko-KR")}원/분`);
  return parts.join(" · ");
}

// 추천 — 아낀 1분에 드는 돈(원/분)이 시간가치(시간 중시로 고정) 이하면 추천(method.md §7.4)
function badge({ wonPerMin }, vot) {
  if (wonPerMin == null) return '<span class="badge mute">시간 이득 없음</span>';
  return wonPerMin <= vot ? '<span class="badge ok">추천</span>' : '<span class="badge warn">비추천</span>';
}

// 어디서 갈아타고 무엇을 타는지 — A 는 택시로 앵커까지, B 는 앵커까지 대중교통,
// D 는 기준 경로 한가운데의 공백(긴 대기·긴 환승 도보) 한 구간만 택시로 건너뛴다(anchor.name = "타는 곳→내리는 곳")
function legText(r) {
  const taxi = `택시 ${fmtMin(r.taxi.duration_s)} · ${fmtDist(r.taxi.distance_m)}`;
  if (r.hybrid === "D") {
    const [from, to] = String(r.anchor.name || "→").split("→");
    const skip = r.gap?.kind === "walk" ? `환승 도보 ${fmtMin(r.gap.removed_s)} 대신`
      : `${r.ride_name ? `${esc(String(r.ride_name))} ` : ""}대기·승차 ${fmtMin(r.gap?.removed_s)} 대신`;
    return `${esc(from)} → ${esc(to)} ${taxi} (${skip})`;
  }
  // C 는 기준 경로가 목적지에서 멀어지기 시작하는 정류장에서 내린다 — 택시로 목적지까지, 또는 곧장 가는 노선을 다시 탄다
  if (r.hybrid === "C") {
    const at = esc(r.detour?.point_name || "정류장");
    const loop = r.detour?.loop_s ? ` (돌아가는 ${fmtMin(r.detour.loop_s)} 대신)` : "";
    if (r.detour?.via === "taxi_to_d") return `${at}에서 내려 ${taxi} → 목적지${loop}`;
    const to = String(r.anchor.name || "→").split("→")[1] || "앵커";
    return `${at}에서 내려 ${taxi} → ${esc(to)}에서 다시 대중교통${loop}`;
  }
  const transit = `대중교통 ${fmtMin(r.transit_time_s)}${r.ride_name ? ` · ${esc(String(r.ride_name))}` : ""}`;
  const at = `${esc(r.anchor.name || "앵커")}에서 갈아탐`;
  return r.hybrid === "A" ? `${taxi} → ${at} → ${transit}` : `${transit} → ${at} → ${taxi}`;
}

// 대중교통 구간 사이에 택시를 한 구간(TAXI)으로 끼운 경로 — 타임라인·칩이 대중교통 카드와 같은 코드로 그린다.
// A 는 맨 앞, B 는 맨 뒤, D 는 서버가 뺀 공백 자리(gap.at), C 는 돌아가기 시작하는 정류장 뒤(detour.taxi_at)에 넣는다
function combinedRoute(r) {
  const taxi = { type: "TAXI", time_s: r.taxi.duration_s, distance_m: r.taxi.distance_m,
    fare: (r.taxi.fare?.taxi ?? 0) + (r.taxi.fare?.toll ?? 0), counted_s: r.taxi_counted_s };
  const steps = r.transit?.steps || [];
  if (r.hybrid === "A") return { steps: [{ ...taxi, to_name: r.anchor.name }, ...steps] };
  if (r.hybrid === "B") return { steps: [...steps, { ...taxi, from_name: r.anchor.name }] };
  if (r.hybrid === "C") {
    const cut = r.detour?.taxi_at ?? steps.length;
    const to = r.detour?.via === "taxi_to_d" ? null : String(r.anchor.name || "→").split("→")[1];
    return { steps: [...steps.slice(0, cut), { ...taxi, from_name: r.detour?.point_name, to_name: to }, ...steps.slice(cut)] };
  }
  const [from, to] = String(r.anchor.name || "→").split("→");
  const at = r.gap?.at ?? steps.length;
  return { steps: [...steps.slice(0, at), { ...taxi, from_name: from, to_name: to }, ...steps.slice(at)] };
}

// key "h:번호" — 목록이 대중교통 카드("t:번호")와 같은 방식으로 고르고 펼친다
function cardHTML(r, i, base, vot) {
  const v = versus(r, base);
  return `
    <div class="route-card hy-card" data-i="${i}" data-key="h:${i}">
      <button type="button" class="rc-head" aria-expanded="false">
        <span class="rc-top"><b class="rc-type">하이브리드</b>
          <span class="rc-times"><b class="rc-time">${fmtMin(r.time_s)}</b>
            <span class="rc-wait">${fmtWon(r.fare)}</span></span>${badge(v, vot)}</span>
        <span class="rc-meta">${legText(r)}</span>
        <span class="rc-meta hy-vs">${versusText(v)}</span>
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
    base.wait_s == null ? "비교 대상 시간에 대기 미포함" : null,
    `추천: 아낀 1분에 ${Math.round(data.vot).toLocaleString("ko-KR")}원 이하`,
    liveFirstWaits(data) ? `첫 대기 실시간 ${liveFirstWaits(data)}개 경로` : null,
    `앵커 ${data.diag.verified}/${data.diag.picked}개 확인`,
    data.diag.too_little_saving ? `${minSaving(data)}분 이상 빠르지 않은 ${data.diag.too_little_saving}개 뺌` : null,
  ].filter(Boolean).join("\n");
  const head = `<p class="hy-base">비교 <b>최소 시간 경로 ${fmtMin(base.time_s)}</b> · ${fmtWon(base.fare)}`
    + ` <span class="hy-info" tabindex="0" role="img" aria-label="${esc(notes)}" title="${esc(notes)}">ⓘ</span></p>`;
  if (!data.routes.length) {
    const why = data.diag.too_little_saving
      ? `최소 시간 경로보다 ${minSaving(data)}분 이상 빠른 하이브리드 경로가 없습니다.` : "조건에 맞는 하이브리드 경로를 찾지 못했습니다.";
    el.innerHTML = head + `<p class="note">${why}</p>`;
  } else {
    el.innerHTML = head + (data.failed.length ? `<p class="muted">확인하지 못한 앵커 ${data.failed.length}개</p>` : "");
  }
  setExtraCards(data.routes.map((r, i) => ({ html: cardHTML(r, i, base, data.vot), route: combinedRoute(r),
    time_s: r.time_s, fare: r.fare, onSelect: () => onSelect(i) })));
}
