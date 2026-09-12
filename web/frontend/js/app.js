// 상태·흐름: 주소·장소 검색 또는 지도·정류장 클릭 → 출발/도착, [경로 검색] → 대중교통·택시 동시 요청, 오류 배너, 쿼터.
// 카카오 응답은 이 페이지의 메모리(state)에만 둔다 — localStorage/sessionStorage 에 두지 않는다(저장소엔 화면 테마 설정만).
import { getJSON, esc } from "./api.js";
import { createMap } from "./map.js";
import * as R from "./routes.js";
import { createPlaceSearch } from "./search.js";
import { initStopRoutes, stopRoutesSection } from "./stoproutes.js";

const $ = (id) => document.getElementById(id);
const CONSOLE_URL = "https://developers.kakao.com/console/app";
const OD_LABEL = { origin: "출발", dest: "도착" };

const state = {
  origin: null,
  dest: null,
  labels: { origin: "", dest: "" }, // 출발·도착 옆에 보이는 이름
  markers: { origin: null, dest: null },
  transit: null,
  selectedIdx: null,
  car: null,
  carOn: false, // 택시 경로를 지도에 그렸는지 (택시 카드로 켜고 끈다)
  busy: false,
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

// label = 출발·도착 옆에 보일 이름 (검색 결과·정류장 이름). 없으면 좌표
function setPoint(role, latlng, label = null) {
  state[role] = latlng;
  showLabel(role, latlng ? label || fmtLL(latlng) : "");
  const m = state.markers[role];
  if (!latlng) {
    if (m) m.remove();
    state.markers[role] = null;
  } else if (m) {
    m.setLatLng(latlng);
  } else {
    const mk = L.marker(latlng, { draggable: true, icon: odIcon(role), title: OD_LABEL[role] }).addTo(view.map);
    mk.on("dragend", () => {
      state[role] = mk.getLatLng();
      showLabel(role, fmtLL(state[role])); // 옮긴 자리는 이름이 없다
      invalidate();
    });
    // 리스너가 없으면 클릭이 지도로 가서 아이콘(점 위쪽) 자리에 메뉴가 뜬다 — 마커 점에 띄운다(끌기 뒤 클릭은 Leaflet 이 무시)
    mk.on("click", () => openOdMenu(mk.getLatLng(), null, state.labels[role]));
    state.markers[role] = mk;
  }
}

// 검색 결과를 고르면: 지도를 그 자리로 옮기고 지도를 누른 것처럼 [출발] [도착] 메뉴(이름 달고).
// 키보드로 골랐어도 이어서 정하게 [출발]에 포커스. 정하고 나서 다른 쪽이 비었으면 검색창으로 돌아와 이어서 찾는다
function pickPlace(item) {
  const ll = L.latLng(item.lat, item.lon);
  view.map.setView(ll, Math.max(view.map.getZoom(), 16));
  const btns = openOdMenu(ll, { name: item.name }).querySelectorAll("[data-role]");
  btns[0].focus({ preventScroll: true });
  for (const b of btns) {
    b.addEventListener("click", () => {
      if (!state.origin || !state.dest) placeSearch.focus();
    });
  }
}

function update() {
  const btn = $("btn-search");
  btn.disabled = state.busy || !(state.origin && state.dest);
  btn.textContent = state.busy ? "경로 검색 중…" : "경로 검색";
}

function clearResults() {
  state.transit = null;
  state.selectedIdx = null;
  state.car = null;
  state.carOn = false;
  R.clearTransit();
  R.clearCar();
  setMsg("transit-body", "출발지와 도착지를 지정한 뒤 [경로 검색]을 누르세요.");
  setMsg("car-body", "출발지와 도착지를 지정한 뒤 [경로 검색]을 누르세요.");
  R.renderDiagnostics($("diag-body"), null, null);
  renderProbe(null);
}

// 좌표가 바뀌면 이전 결과는 모두 무효
function invalidate() {
  seq++;
  state.busy = false;
  clearResults();
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
    : "")
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
    for (const [ev, on] of [["mouseenter", true], ["mouseleave", false], ["focus", true], ["blur", false]]) {
      info.addEventListener(ev, () => show(on));
    }
  }
  // 열 때는 지도를 밀지 않는다 — 가장자리 첫 클릭에 지도가 밀리면 더블클릭의 둘째 클릭이 메뉴 버튼을 누를 수 있다.
  // 더블클릭이 끝날 시간이 지나도 열려 있으면 그때 메뉴가 다 보이게 민다(가장자리에서 잘리지 않게).
  const popup = L.popup({ className: "odm-popup", maxWidth: 240, autoPan: false, autoPanPadding: [16, 16] })
    .setLatLng(latlng).setContent(box).openOn(view.map);
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

view.map.on("click", (e) => openOdMenu(e.latlng));
view.map.on("dblclick", () => view.map.closePopup()); // 더블클릭 확대의 첫 클릭이 연 메뉴를 남기지 않는다

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

// --- 검색 ---

async function search() {
  if (state.busy || !state.origin || !state.dest) return;
  const my = ++seq;
  clearResults();
  state.busy = true;
  update();
  setMsg("transit-body", "조회 중…");
  setMsg("car-body", "조회 중…");

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
    showTransit(t);
    showCar(c);
  }
  refreshQuota();
}

function showTransit(res) {
  const body = $("transit-body");
  if (res.status === "rejected") {
    body.replaceChildren(errorBox(res.reason));
    return;
  }
  const t = (state.transit = res.value);
  if (t.quota) renderQuota(t.quota);
  renderProbe(t.probe);
  if (!t.routes?.length) {
    body.innerHTML = `<p class="note">${esc(t.message || "대중교통 경로가 없습니다.")} <small>(${esc(t.status)})</small></p>`;
    R.renderDiagnostics($("diag-body"), t, null);
    return;
  }
  // 탭·정렬 기본값(전체 · 최소 시간순)의 첫 경로를 고른다. 출발·도착은 첫·끝 도보 거리 추정용
  R.renderRouteCards(body, t, selectRoute, { origin: state.origin, dest: state.dest });
}

function selectRoute(i) {
  const t = state.transit;
  const route = t?.routes?.[i];
  if (!route) return;
  state.selectedIdx = i;
  R.markSelected($("transit-body"), i);
  R.drawRoute(route, state.origin, state.dest);
  R.drawDiagnostics(route);
  R.renderDiagnostics($("diag-body"), t, route);
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

// 택시 경로 켜기/끄기 — 대중교통 경로 선택과 따로라 둘 다 켤 수 있다
function toggleCar() {
  state.carOn = !state.carOn;
  R.markCarSelected($("car-body"), state.carOn);
  if (state.carOn) R.drawCar(state.car, { fit: true });
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
  return new Ctl({ position: "bottomright" }).addTo(view.map).getContainer();
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
