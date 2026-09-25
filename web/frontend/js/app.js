// 상태·흐름: 주소·장소 검색 또는 지도·정류장 클릭 → 출발/도착, [경로 검색] → 대중교통·택시 동시 요청, 오류 배너, 쿼터.
// 카카오 응답은 이 페이지의 메모리(state)에만 둔다 — localStorage/sessionStorage 에 두지 않는다(저장소엔 화면 테마 설정만).
import { getJSON, postJSON, esc } from "./api.js";
import { createMap } from "./map.js";
import * as R from "./routes.js";
import { renderHybrid } from "./hybrid.js";
import { createPlaceSearch } from "./search.js";
import { initStopRoutes, stopRoutesSection } from "./stoproutes.js";
import { initTooltips } from "./tooltip.js";
import { initLocation } from "./location.js";
import { initPanelHandle } from "./panel.js";
import { initMapLongPress } from "./longpress.js";

const $ = (id) => document.getElementById(id);
const CONSOLE_URL = "https://developers.kakao.com/console/app";
const OD_LABEL = { origin: "출발", dest: "도착" };
// 하이브리드가 [경로 검색] 결과를 기준 경로로 다시 쓰는 시간 한도 — 넘으면 새로 부른다(카카오: 저장하지 않고 실시간 호출로만)
const REUSE_MS = 5 * 60 * 1000;

const state = {
  origin: null,
  dest: null,
  labels: { origin: "", dest: "" }, // 출발·도착 옆에 보이는 이름
  markers: { origin: null, dest: null },
  transit: null,
  selectedIdx: null,
  car: null,
  carOn: false, // 택시 경로를 지도에 그렸는지 (택시 카드로 켜고 끈다)
  hybrid: null,
  hybridIdx: null, // 지도에 그린 하이브리드 경로 번호 (대중교통·택시 선택과 배타적이다)
  busy: false,
  hybridBusy: false,
  picked: null, // 검색해서 고른 장소 {latlng, name} — 검색창 옆 [출발][도착]이 쓴다
  tab: "all",   // 결과 탭: all · 버스 · 지하철 · 버스+지하철 · hybrid
  sort: "time",
  transitAt: 0, // [경로 검색] 결과를 받은 시각 (하이브리드가 다시 쓸 수 있는지)
};
let seq = 0; // 좌표가 바뀔 때마다 증가 — 늦게 도착한 이전 검색 결과는 버린다
const banners = new Map();

// --- 오류 표시 ---

function errorBox(err) {
  const div = document.createElement("div");
  const variant = err.code === "quota_exceeded" ? "warn" : err.code === "kakao_map_disabled" ? "boxed" : "plain";
  div.className = `err err-${variant}`;
  const meta = [
    err.code,
    err.status ? `HTTP ${err.status}` : "",
    err.upstreamStatus ? `카카오 응답 ${err.upstreamStatus}` : "",
  ].filter(Boolean).join(" · ");
  div.innerHTML = `<strong>${esc(err.message || "알 수 없는 오류가 발생했습니다.")}</strong>`
    + (err.action ? `<p>${esc(err.action)}</p>` : "")
    + (err.code === "kakao_map_disabled"
      ? `<p><a href="${CONSOLE_URL}" target="_blank" rel="noopener noreferrer">카카오 개발자 콘솔 열기 ↗</a></p>`
      : "")
    + (meta ? `<small>${esc(meta)}</small>` : "");
  return div;
}

// 전역 상태(서버·데이터·키) 배너. 같은 key 는 하나만.
function showBanner(err, key = err.code || "error") {
  banners.get(key)?.remove();
  const box = errorBox(err);
  const close = document.createElement("button");
  close.type = "button";
  close.className = "err-close";
  close.title = "닫기";
  close.textContent = "×";
  close.addEventListener("click", () => {
    box.remove();
    banners.delete(key);
  });
  box.prepend(close);
  banners.set(key, box);
  $("banners").append(box);
}

function setMsg(id, text, cls = "muted") {
  $(id).innerHTML = `<p class="${cls}">${esc(text)}</p>`;
}

// --- 지도와 출발/도착 ---

const view = createMap($("map"), { onError: (err) => showBanner(err), onPlaceClick: (p) => openOdMenu(p.latlng, p) });
R.initRoutes(view);
initStopRoutes(view);
initLocation(view.map, (latlng) => {
  document.activeElement?.blur();
  openOdMenu(latlng, { name: "현재 위치" });
});

let panelMapOffset = 0;
function updateMapLayout() {
  view.map.invalidateSize({ pan: true, animate: false });
  const offset = !matchMedia("(max-width: 720px)").matches && !document.body.classList.contains("panel-collapsed")
    ? $("sidebar").getBoundingClientRect().right / 2 : 0;
  if (offset !== panelMapOffset) view.map.panBy([panelMapOffset - offset, 0], { animate: false });
  panelMapOffset = offset;
}
const panel = initPanelHandle($("panel-handle"), updateMapLayout);
new ResizeObserver(() => {
  document.body.style.setProperty("--mobile-panel-height", `${$("sidebar").getBoundingClientRect().height}px`);
}).observe($("sidebar"));
new ResizeObserver(updateMapLayout).observe($("map"));

function odIcon(role) {
  return L.divIcon({ className: `od-marker od-${role}`, html: OD_LABEL[role], iconSize: [40, 24], iconAnchor: [20, 30] });
}

// 주소·장소 검색창 (하나): 결과를 고르면 지도를 그 자리로 옮기고 [출발] [도착] 메뉴
const placeSearch = createPlaceSearch($("place-q"), $("place-list"), {
  onPick: pickPlace,
  onQuota: (q) => (q ? renderQuota(q) : refreshQuota()), // 검색이 실패하면 응답에 쿼터가 없다
});

const fmtLL = (p) => `${p.lat.toFixed(5)}, ${p.lng.toFixed(5)}`;

// 출발·도착 옆 이름 (길면 말줄임 — 마우스를 올리면 전체)
function showLabel(role, text) {
  state.labels[role] = text;
  const el = $(`${role}-name`);
  el.textContent = text;
  el.title = text;
}

const addressRequests = { origin: 0, dest: 0 };
async function resolvePointAddress(role, latlng, requestId) {
  try {
    const result = await getJSON("/api/reverse-address", { lon: latlng.lng, lat: latlng.lat });
    if (result.quota) renderQuota(result.quota);
    if (addressRequests[role] === requestId && result.address) showLabel(role, result.address);
  } catch {
    // 주소가 없는 지점이나 조회 실패 시에도 좌표로 경로 검색은 계속할 수 있다.
  }
}

// 검색 결과·정류장 이름은 유지하고, 임의의 좌표는 주소로 변환한다.
function setPoint(role, latlng, label = null) {
  const requestId = ++addressRequests[role];
  state[role] = latlng;
  showLabel(role, latlng ? label || fmtLL(latlng) : "");
  if (latlng && (!label || label === fmtLL(latlng))) resolvePointAddress(role, latlng, requestId);
  const m = state.markers[role];
  if (!latlng) {
    if (m) m.remove();
    state.markers[role] = null;
  } else if (m) {
    m.setLatLng(latlng);
  } else {
    const mk = L.marker(latlng, { draggable: true, icon: odIcon(role), title: OD_LABEL[role] }).addTo(view.map);
    mk.on("dragend", () => {
      setPoint(role, mk.getLatLng());
      invalidate();
    });
    // 리스너가 없으면 클릭이 지도로 가서 아이콘(점 위쪽) 자리에 메뉴가 뜬다 — 마커 점에 띄운다(끌기 뒤 클릭은 Leaflet 이 무시)
    mk.on("click", () => openOdMenu(mk.getLatLng(), null, state.labels[role]));
    state.markers[role] = mk;
  }
}

// 검색 결과를 고르면: 검색창 옆 [출발][도착]이 풀려 그 자리에서 곧바로 정한다(키보드로 골랐어도 [출발]에 포커스).
// 어디인지 보이게 지도도 그 자리로 옮기고 지도를 누른 것처럼 메뉴를 연다 — 지도에서 골라도 된다.
// 정하고 나서 다른 쪽이 비었으면 검색창으로 돌아와 이어서 찾는다
function pickPlace(item) {
  const ll = L.latLng(item.lat, item.lon);
  view.map.setView(ll, Math.max(view.map.getZoom(), 16));
  state.picked = { latlng: ll, name: item.name };
  syncPickButtons();
  for (const b of openOdMenu(ll, { name: item.name }).querySelectorAll("[data-role]")) {
    b.addEventListener("click", afterPick);
  }
  $("pick-origin").focus({ preventScroll: true });
}

function syncPickButtons() {
  for (const role of ["origin", "dest"]) $(`pick-${role}`).disabled = !state.picked;
}

// 고른 장소를 정한 뒤 — 검색창을 비우고, 다른 쪽이 비었으면 이어서 찾게 검색창으로
function afterPick() {
  state.picked = null;
  $("place-q").value = "";
  syncPickButtons();
  if (!state.origin || !state.dest) placeSearch.focus();
}

for (const role of ["origin", "dest"]) {
  $(`pick-${role}`).addEventListener("click", () => {
    const p = state.picked;
    if (!p) return;
    view.map.closePopup();
    setPoint(role, p.latlng, p.name);
    invalidate();
    afterPick();
  });
}
// 검색어를 고치면 고른 장소는 더 이상 검색창의 것이 아니다
$("place-q").addEventListener("input", () => {
  if (!state.picked) return;
  state.picked = null;
  syncPickButtons();
});

function update() {
  const btn = $("btn-search");
  btn.disabled = state.busy || !(state.origin && state.dest);
  btn.textContent = state.busy ? "경로 검색 중…" : "경로 검색";
  const hy = $("btn-hybrid");
  // 한 번 찾으면 출발·도착이 바뀔 때까지 잠근다 — 같은 조건으로 다시 찾으면 쿼터만 쓴다(실패했으면 다시 시도할 수 있게 둔다)
  hy.disabled = state.busy || state.hybridBusy || !!state.hybrid || !(state.origin && state.dest);
  hy.textContent = state.hybridBusy ? "앵커 확인 중…"
    : state.hybrid ? `하이브리드 경로 검색 완료 (${state.hybrid.routes.length}개)` : "하이브리드 경로 검색";
}

// keepHybrid — 같은 출발·도착으로 다시 검색할 때 찾은 하이브리드는 남긴다(버튼도 잠근 채)
function clearResults({ keepHybrid = false, pointChanged = false } = {}) {
  $("btn-hybrid").hidden = true;
  panel.reset({ pointChanged });
  state.transit = null;
  state.selectedIdx = null;
  state.car = null;
  state.carOn = false;
  if (!keepHybrid) state.hybrid = null;
  state.hybridIdx = null;
  state.tab = "all";
  R.clearTransit();
  R.clearCar();
  R.resetRouteCards();
  $("car-body").replaceChildren();
  $("route-bar").hidden = true;
  $("hybrid").hidden = true;
  setMsg("transit-body", "출발지와 도착지를 지정한 뒤 [경로 검색]을 누르세요.");
  if (!keepHybrid) setMsg("hybrid-body", "[하이브리드 경로 검색]을 누르면 택시로 갈아탈 지점을 찾습니다.");
  R.renderDiagnostics($("diag-body"), null, null);
  renderProbe(null);
}

// 좌표가 바뀌면 이전 결과는 모두 무효
function invalidate() {
  seq++;
  state.busy = false;
  clearResults({ pointChanged: true });
  update();
}

// 출발·도착 옆에 보일 정류장·역 이름 (역은 '역'을 붙인다)
const placeLabel = (p) => (p.line && !p.name.endsWith("역") ? `${p.name}역` : p.name);

// 누른 곳(또는 정류장·역·검색 결과)에 [출발] [도착] 메뉴 → 메뉴 요소. 정류장이면 이름 옆 ⓘ 에 마우스를 올려 정류장 정보를 보고,
// 버스 정류장이면 아래에 경유 노선 칩이 붙는다(누르면 그 노선의 정류장을 지도에 강조 — stoproutes.js).
// 경유는 넣지 않는다 — 카카오 대중교통 API 에 경유지 파라미터가 없다 (method.md E9).
// label = 고른 자리를 출발·도착 옆에 보일 이름 (출발·도착 표지를 눌렀을 때 그 이름을 이어받는다)
function openOdMenu(latlng, place = null, label = null) {
  const box = document.createElement("div");
  box.className = "odm";
  // 지하철역이면 이름 앞에 노선 이름 칩(노선 색), 정류장이면 색 점. 검색 결과는 이름만
  const mark = place?.line
    ? `<span class="chip odm-line" style="background:${place.color}">${esc(place.line)}</span>`
    : place?.color ? `<span class="swatch" style="background:${place.color}"></span>` : "";
  box.innerHTML = (place
    ? `<div class="odm-head">${mark}<b>${esc(place.name)}</b>`
      + (place.info ? '<button type="button" class="odm-i" aria-label="정류장 정보">ⓘ</button>' : "") + "</div>"
      + (place.info ? `<div class="odm-card pop" hidden>${place.info}</div>` : "")
    : `<div class="odm-head"><b class="odm-address" aria-live="polite">${esc(label || "주소 확인 중…")}</b></div>`)
    + `<div class="odm-btns">${["origin", "dest"].map((r) =>
      `<button type="button" class="pin od-${r}" data-role="${r}">${OD_LABEL[r]}</button>`).join("")}</div>`;
  if (place?.busKey) box.append(stopRoutesSection(place.busKey, latlng)); // 버스 정류장: 경유 노선 칩
  for (const b of box.querySelectorAll("[data-role]")) {
    b.addEventListener("click", () => {
      view.map.closePopup();
      setPoint(b.dataset.role, latlng, place ? placeLabel(place) : label);
      invalidate(); // 좌표가 바뀌었으니 이전 결과는 무효
    });
  }
  const info = box.querySelector(".odm-i");
  if (info) {
    const card = box.querySelector(".odm-card");
    const show = (on) => {
      card.hidden = !on;
      info.setAttribute("aria-expanded", String(on));
      if (!on) return;
      // 메뉴 오른쪽에, 지도 오른쪽 끝을 넘으면 왼쪽에 — 그래도 넘치면 지도 안으로 밀어 넣는다.
      // 카드는 pointer-events: none 이라 ⓘ 를 덮어도 마우스가 ⓘ 에서 떨어지지 않는다(깜빡임 없음)
      const m = view.map.getContainer().getBoundingClientRect();
      const o = box.getBoundingClientRect();
      const c = card.getBoundingClientRect();
      let x = o.right + 18;
      if (x + c.width > m.right - 4) x = o.left - 18 - c.width;
      x = Math.max(m.left + 4, Math.min(x, m.right - 4 - c.width));
      const y = Math.max(m.top + 4, Math.min(o.top - 8, m.bottom - 4 - c.height));
      card.style.left = `${Math.round(x - o.left)}px`;
      card.style.top = `${Math.round(y - o.top)}px`;
    };
    info.setAttribute("aria-expanded", "false");
    info.addEventListener("pointerenter", (e) => { if (e.pointerType === "mouse") show(true); });
    info.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") show(false); });
    info.addEventListener("click", () => show(card.hidden));
    info.addEventListener("keydown", (e) => { if (e.key === "Escape") show(false); });
    info.addEventListener("blur", () => show(false));
  }
  // 열 때는 지도를 밀지 않는다 — 가장자리 첫 클릭에 지도가 밀리면 더블클릭의 둘째 클릭이 메뉴 버튼을 누를 수 있다.
  // 더블클릭이 끝날 시간이 지나도 열려 있으면 그때 메뉴가 다 보이게 민다(가장자리에서 잘리지 않게).
  const popup = L.popup({ className: "odm-popup", maxWidth: 240, autoPan: false, autoPanPadding: [16, 16] })
    .setLatLng(latlng).setContent(box).openOn(view.map);
  if (info) {
    const closeInfoOutside = (event) => {
      if (!info.contains(event.target)) {
        box.querySelector(".odm-card").hidden = true;
        info.setAttribute("aria-expanded", "false");
      }
    };
    document.addEventListener("pointerdown", closeInfoOutside, true);
    popup.once("remove", () => document.removeEventListener("pointerdown", closeInfoOutside, true));
  }
  if (!place && (!label || label === fmtLL(latlng))) {
    const address = box.querySelector(".odm-address");
    getJSON("/api/reverse-address", { lon: latlng.lng, lat: latlng.lat }).then((result) => {
      if (result.quota) renderQuota(result.quota);
      if (!popup.isOpen()) return;
      label = result.address || null;
      address.textContent = label || `주소 없음 · ${fmtLL(latlng)}`;
      const focused = box.contains(document.activeElement) ? document.activeElement : null;
      popup.update();
      focused?.focus({ preventScroll: true });
    }).catch(() => {
      if (!popup.isOpen()) return;
      address.textContent = `주소 확인 실패 · ${fmtLL(latlng)}`;
      popup.update();
    });
  }
  // update 는 내용을 떼었다 다시 붙여 메뉴 안의 포커스가 풀린다 — 되살린다(검색 결과로 연 메뉴는 [출발]에 포커스가 있다)
  setTimeout(() => {
    if (!popup.isOpen()) return;
    const focused = box.contains(document.activeElement) ? document.activeElement : null;
    popup.options.autoPan = true;
    popup.update();
    focused?.focus({ preventScroll: true });
  }, 400);
  return box;
}

const cancelMapPress = initMapLongPress($("map"), (event) => openOdMenu(view.map.mouseEventToLatLng(event)));
view.map.on("movestart zoomstart", cancelMapPress);

$("btn-swap").addEventListener("click", () => {
  const { origin, dest } = state;
  const { origin: lo, dest: ld } = state.labels;
  setPoint("origin", dest, ld);
  setPoint("dest", origin, lo);
  invalidate();
});

// 출발·도착 옆 × — 그쪽만 지우고 검색창으로 (바로 새로 찾게)
for (const role of ["origin", "dest"]) {
  $(`${role}-clear`).addEventListener("click", () => {
    setPoint(role, null);
    invalidate();
    placeSearch.focus();
  });
}

$("btn-search").addEventListener("click", search);
$("btn-hybrid").addEventListener("click", runHybrid);

function drawHybrid() {
  renderHybrid($("hybrid-body"), state.hybrid, selectHybrid);
}

// --- 검색 ---

async function search() {
  if (state.busy || !state.origin || !state.dest) return;
  const my = ++seq;
  clearResults({ keepHybrid: true }); // 출발·도착이 그대로라 찾은 하이브리드는 유효하다
  state.busy = true;
  update();
  setMsg("transit-body", "조회 중…");
  setMsg("car-body", "조회 중…");
  panel.showResults();

  const o = state.origin;
  const d = state.dest;
  const p = { sx: o.lng.toFixed(6), sy: o.lat.toFixed(6), ex: d.lng.toFixed(6), ey: d.lat.toFixed(6) };
  const [t, c] = await Promise.allSettled([
    getJSON("/api/transit", { ...p, probe: $("chk-probe").checked ? 1 : 0 }),
    getJSON("/api/car", p),
  ]);
  if (my === seq) {
    state.busy = false;
    update();
    showCar(c);
    showTransit(t);
    if (state.hybrid) drawHybrid(); // 목록에 다시 섞는다
    showResultsBar();
    $("btn-hybrid").hidden = false;
  }
  refreshQuota();
}

// --- 결과 머리: 택시 카드 아래의 탭(전체 · [버스 · 지하철 · 버스+지하철] · 하이브리드)과 정렬 ---
// 전체 = 대중교통과 하이브리드를 한 목록에 섞는다(시간순이면 대기 포함 시간으로 견준다 — routes.js visibleEntries).
// 하이브리드 영역(버튼 · 기준선 요약)은 전체 · 하이브리드 탭에서 목록 위에 보인다
const TRANSIT_TABS = ["버스", "지하철", "버스+지하철"];

function showResultsBar() {
  const sel = $("route-sort");
  if (!sel.options.length) {
    sel.innerHTML = R.SORT_OPTIONS.map(([k, label]) => `<option value="${k}">${esc(label)}</option>`).join("");
    sel.addEventListener("change", () => {
      state.sort = sel.value; // 정렬만 바꾸고 선택한 경로는 그대로
      R.setRouteView({ sort: state.sort });
    });
  }
  sel.value = state.sort;
  $("route-bar").hidden = false;
  applyTab();
}

function tabCounts() {
  const groups = R.routeGroupCounts(state.transit);
  const transitN = state.transit?.routes?.length || 0;
  const hybridN = state.hybrid?.routes?.length; // 아직 찾지 않았으면 비워 둔다
  return { all: transitN + (hybridN || 0), hybrid: hybridN,
    ...Object.fromEntries(TRANSIT_TABS.map((g) => [g, groups.get(g) || 0])) };
}

function applyTab() {
  const n = tabCounts();
  for (const b of document.querySelectorAll("#route-bar .rt-tab")) {
    const t = b.dataset.tab;
    b.setAttribute("aria-selected", String(t === state.tab));
    b.querySelector(".rt-n").textContent = n[t] ?? "";
    b.disabled = TRANSIT_TABS.includes(t) && !n[t]; // 그 종류의 경로가 없으면 잠근다
  }
  $("hybrid").hidden = !(state.tab === "hybrid" || state.tab === "all");
  const shown = R.setRouteView({ group: state.tab === "all" ? null : state.tab });
  if (state.tab === "hybrid") return; // 하이브리드 탭은 카드를 누르기 전까지 지도를 바꾸지 않는다
  // 지도의 경로가 이 탭에 없으면 탭의 첫 경로로 — 전체 탭에서는 고른 하이브리드를 그대로 둔다
  const keepHybrid = state.tab === "all" && state.hybridIdx != null;
  if (!matchMedia("(max-width: 720px)").matches && !keepHybrid && shown.length && !shown.includes(state.selectedIdx)) selectRoute(shown[0]);
}

for (const b of document.querySelectorAll("#route-bar .rt-tab")) {
  b.addEventListener("click", () => {
    state.tab = b.dataset.tab;
    applyTab();
    if (state.tab === "hybrid" && !state.hybrid) runHybrid();
  });
}

function showTransit(res) {
  const body = $("transit-body");
  if (res.status === "rejected") {
    body.replaceChildren(errorBox(res.reason));
    return;
  }
  const t = (state.transit = res.value);
  state.transitAt = Date.now();
  if (t.quota) renderQuota(t.quota);
  renderProbe(t.probe);
  if (!t.routes?.length) {
    body.innerHTML = `<p class="note">${esc(t.message || "대중교통 경로가 없습니다.")} <small>(${esc(t.status)})</small></p>`;
    R.renderDiagnostics($("diag-body"), t, null);
    return;
  }
  // 결과 머리의 탭·정렬로 그리고 보이는 첫 경로를 고른다. 출발·도착은 첫·끝 도보 거리 추정용
  R.renderRouteCards(body, t, selectRoute, { origin: state.origin, dest: state.dest,
    group: state.tab === "all" ? null : state.tab, sort: state.sort,
    autoSelect: !matchMedia("(max-width: 720px)").matches });
}

function selectRoute(i) {
  const t = state.transit;
  const route = t?.routes?.[i];
  if (!route) return;
  state.selectedIdx = i;
  state.carOn = false;
  R.markCarSelected($("car-body"), false);
  R.clearCar();
  dropHybrid(); // 하이브리드는 지도의 두 층(대중교통·택시)을 함께 쓴다 — 한 번에 하나만 그린다
  R.markSelected($("transit-body"), i);
  panel.showSelection(() => {
    R.drawRoute(route, state.origin, state.dest);
    R.drawDiagnostics(route);
    R.renderDiagnostics($("diag-body"), t, route);
    revealSelectedCard();
  });
}

function revealSelectedCard() {
  if (!matchMedia("(max-width: 720px)").matches) return;
  const card = $("route-cards").querySelector(".route-card.selected");
  if (card) $("route-cards").scrollTop += card.getBoundingClientRect().top - $("route-cards").getBoundingClientRect().top;
}

// --- 대중교통+택시 (하이브리드) ---

// 버튼을 눌렀을 때만 부른다 — 서버가 앵커마다 카카오를 부르므로 쿼터를 크게 쓴다
async function runHybrid() {
  if (state.busy || state.hybridBusy || state.hybrid || !state.origin || !state.dest) return;
  state.tab = "hybrid";
  panel.showResults();
  showResultsBar();
  const my = seq;
  state.hybridBusy = true;
  update();
  setMsg("hybrid-body", "택시로 갈아탈 지점을 찾고 확인하는 중…");
  const o = state.origin;
  const d = state.dest;
  const params = { sx: o.lng.toFixed(6), sy: o.lat.toFixed(6), ex: d.lng.toFixed(6), ey: d.lat.toFixed(6) };
  // [경로 검색] 결과를 받은 지 REUSE_MS 안이면 기준 경로로 돌려보내 다시 쓴다 — 서버는 이 요청을 처리하는 동안에만 쓴다
  // (대중교통 1콜 절약). 출발·도착이 바뀌면 결과가 지워지므로 여기 남은 것은 늘 같은 출발·도착의 결과다
  const reuse = state.transit?.routes?.length > 0 && Date.now() - state.transitAt < REUSE_MS;
  try {
    const data = reuse
      ? await postJSON("/api/hybrid", params, { routes: state.transit.routes })
      : await getJSON("/api/hybrid", params);
    if (my !== seq) return; // 그사이 출발·도착이 바뀌었다
    state.hybrid = data;
    if (data.quota) renderQuota(data.quota);
    drawHybrid();
    applyTab(); // 탭 옆 경로 수에 하이브리드를 더한다
  } catch (err) {
    if (my === seq) $("hybrid-body").replaceChildren(errorBox(err));
  } finally {
    state.hybridBusy = false;
    update();
    refreshQuota();
  }
}

// 앵커를 사이에 두고 택시 구간과 대중교통 구간을 함께 그린다.
// A 는 앵커 → 도착지가 대중교통이고, B 는 출발지 → 앵커가 대중교통이다(나머지 끝이 택시).
// D 는 출발지 → 도착지 기준 경로에서 택시가 대신한 한 구간만 빠진 경로다 — 빈 자리를 택시 선이 메운다
function selectHybrid(i) {
  const r = state.hybrid?.routes?.[i];
  if (!r) return;
  state.hybridIdx = i;
  state.selectedIdx = null;
  state.carOn = false;
  R.markSelectedKey(`h:${i}`);
  R.markCarSelected($("car-body"), false);
  R.clearTransit();
  const at = L.latLng(r.anchor.lat, r.anchor.lon);
  const [from, to] = r.hybrid === "A" ? [at, state.dest] : r.hybrid === "B" ? [state.origin, at] : [state.origin, state.dest];
  panel.showSelection(() => {
    if (r.transit) R.drawRoute(r.transit, from, to);
    R.drawCar(r.taxi, { fit: true });
    revealSelectedCard();
  });
}

// 대중교통·택시를 고르면 하이브리드 표시를 거둔다 (지도에 한 경로만 남게)
function dropHybrid() {
  if (state.hybridIdx == null) return;
  state.hybridIdx = null;
  R.markSelectedKey(state.selectedIdx == null ? null : `t:${state.selectedIdx}`);
  R.clearCar();
}

function showCar(res) {
  const body = $("car-body");
  if (res.status === "rejected") {
    body.replaceChildren(errorBox(res.reason));
    return;
  }
  const car = (state.car = res.value);
  if (car.quota) renderQuota(car.quota);
  R.renderCarCard(body, car, toggleCar); // 처음엔 꺼짐 — 카드를 눌러야 지도에 그린다
}

// 택시도 다른 경로와 하나만 선택한다. 다시 누르면 선택을 해제한다.
function toggleCar() {
  dropHybrid();
  state.selectedIdx = null;
  R.markSelectedKey(null);
  R.clearTransit();
  state.carOn = !state.carOn;
  R.markCarSelected($("car-body"), state.carOn);
  if (state.carOn) panel.showSelection(() => {
    R.drawCar(state.car, { fit: true });
    revealSelectedCard();
  });
  else R.clearCar();
}

function renderProbe(probe) {
  $("probe-body").innerHTML = probe
    ? `<pre>${esc(JSON.stringify(probe, null, 2))}</pre>`
    : '<p class="muted">위 상자를 켜고 검색하면 카카오 응답의 구조 요약(키·구간 수·차량 이름 등)이 표시됩니다.</p>';
}

// --- 서버 상태·쿼터 ---

const num = (x) => Number(x ?? 0).toLocaleString("ko-KR");

function renderQuota(q) {
  const rows = [["transit", "대중교통"], ["car", "자동차"], ["keyword", "장소 검색"], ["address", "주소 검색"]].map(([k, label]) => {
    const u = q[k];
    if (!u) return "";
    const full = u.remaining <= 0;
    const w = u.limit > 0 ? Math.min(100, (u.used / u.limit) * 100) : 100;
    return `<div class="quota-row${full ? " full" : ""}"><span class="q-label">${label}</span>`
      + `<span class="q-bar"><span class="q-fill" style="width:${w.toFixed(1)}%"></span></span>`
      + `<span class="q-num">${num(u.used)}/${num(u.limit)}</span></div>`;
  }).join("");
  $("quota-body").innerHTML = rows + `<p class="muted">${esc(q.date)} 기준 · 한국 시간 자정(00:00)에 초기화</p>`;
}

async function refreshQuota() {
  try {
    renderQuota(await getJSON("/api/quota"));
  } catch (err) {
    showBanner(err);
  }
}

function renderHealth(h) {
  const parts = h.ready ? [`정류장 ${num(h.bus_stops)}`, `역 ${num(h.subway_stations)}`] : ["정제 데이터 없음"];
  if (h.data_mtime) parts.push(`데이터 ${String(h.data_mtime).slice(0, 16).replace("T", " ")}`);
  $("health-line").textContent = parts.join(" · ");
  if (!h.ready) {
    showBanner({ code: "data_not_built", message: "정제 데이터가 없습니다.",
      action: "data 파이프라인을 먼저 실행하세요 (data/README.md)." });
  }
  if (!h.keys?.kakao_rest) {
    showBanner({ code: "missing_key", message: "카카오 REST API 키가 설정되지 않았습니다.",
      action: ".env 에 KAKAO_REST_API_KEY 를 추가하고 서버를 다시 시작하세요." }, "missing_key:kakao");
  }
  if (!h.keys?.vworld) {
    showBanner({ code: "missing_key", message: "VWorld API 키가 설정되지 않아 VWorld 배경지도가 표시되지 않습니다.",
      action: ".env 에 VWORLD_API_KEY 를 추가하고 서버를 다시 시작하세요. 그동안은 지도 오른쪽 위에서 OpenStreetMap 을 고르세요." },
    "missing_key:vworld");
  }
}

// --- 화면 테마 (다크 모드) — 처음 값은 index.html 이 그리기 전에 정한다(저장한 선택 → 운영체제 설정) ---

const THEME_KEY = "baroga-theme";
// 어두운 원 안의 흰 아이콘: 밝은 화면에선 달(누르면 다크), 다크에선 해(누르면 밝게)
const ICON = {
  moon: '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">'
    + '<path fill="currentColor" d="M20.6 14.2A8.5 8.5 0 0 1 9.8 3.4 8.5 8.5 0 1 0 20.6 14.2z"/></svg>',
  sun: '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor"'
    + ' stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="4" fill="currentColor" stroke="none"/>'
    + '<path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/></svg>',
};

// 지도 오른쪽 아래(출처 표기 위) 컨트롤. 두 아이콘을 다 넣어 두고 CSS 가 테마에 맞는 하나만 보인다 —
// 누를 때 아이콘을 갈아 끼우면 클릭 대상이 문서에서 떨어져 Leaflet 이 지도 클릭으로 알고 출발/도착 메뉴를 띄운다
const themeBtn = (() => {
  const Ctl = L.Control.extend({
    onAdd() {
      const b = L.DomUtil.create("button", "theme-btn");
      b.type = "button";
      b.id = "theme-btn";
      b.innerHTML = `<span class="ti-moon">${ICON.moon}</span><span class="ti-sun">${ICON.sun}</span>`;
      L.DomEvent.disableClickPropagation(b); // 누른 것이 지도 클릭(출발/도착 메뉴)이 되지 않게
      b.addEventListener("click", toggleTheme);
      return b;
    },
  });
  const button = new Ctl({ position: "topright" }).addTo(view.map).getContainer();
  button.parentElement.prepend(button);
  return button;
})();

function showThemeButton(dark) {
  themeBtn.dataset.icon = dark ? "sun" : "moon";
  themeBtn.title = dark ? "밝은 화면으로" : "다크 모드로";
  themeBtn.setAttribute("aria-label", themeBtn.title);
  themeBtn.setAttribute("aria-pressed", String(dark));
}

function toggleTheme() {
  const dark = document.documentElement.dataset.theme !== "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  try {
    localStorage.setItem(THEME_KEY, dark ? "dark" : "light"); // 화면 테마 설정만 저장한다
  } catch { /* 저장소를 못 써도 이번 화면에는 적용 */ }
  showThemeButton(dark);
  view.setTheme(dark); // 배경지도 기본 ↔ 야간, 버스 점·시군구 경계 색
}

async function init() {
  clearResults();
  update();
  showThemeButton(document.documentElement.dataset.theme === "dark");
  initTooltips(); // 모든 title 을 테마에 맞춘 말풍선으로
  const info = $("info-dialog"); // 데이터 출처·API 표 (제목 옆 ⓘ). 닫기는 × · Esc · 바깥 클릭
  $("info-btn").addEventListener("click", () => info.showModal());
  info.addEventListener("click", (e) => { if (e.target === info) info.close(); });
  const [h, q] = await Promise.allSettled([getJSON("/api/health"), getJSON("/api/quota")]);
  if (h.status === "fulfilled") {
    renderHealth(h.value);
  } else {
    $("health-line").textContent = "서버 상태를 확인할 수 없습니다.";
    showBanner(h.reason);
  }
  if (q.status === "fulfilled") {
    renderQuota(q.value);
  } else {
    setMsg("quota-body", "쿼터를 불러오지 못했습니다.");
    showBanner(q.reason);
  }
}

init();
