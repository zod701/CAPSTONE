// 버스 정류장의 경유 노선: 정류장 메뉴에 노선 번호 칩, 칩을 누르면 그 노선이 지나는 정류장만 지도에 강조한다(선은 그리지 않는다).
// 노선 자료는 서버가 공공데이터 순서표(data/processed/bus_route_stops.csv)에서 준다 — 카카오 호출 없음.
import { getJSON, esc } from "./api.js";
import { busColor, chipStyle } from "./routes.js";

// 강조는 노선 유형 색(칩과 같은 색). 지도의 강조 점은 흰 채움 + 유형 색 테두리 — 지하철역 점(색 채움 + 흰 테두리)과 모양이
// 반대라 같은 계열 색(직행 빨강·신분당선 등)이어도 헷갈리지 않는다. 노랑(마을) 같은 밝은 색도 밝은 지도에서 보이게 아래에 반투명 검은 윤곽
const CASING = { color: "#000000", opacity: 0.45, weight: 5, fill: false };
const SOURCE = { seoul: "서울", gyeonggi: "경기" };
const kind = (r) => [SOURCE[r.source] || r.source, r.type].filter(Boolean).join(" "); // "경기 일반"
// 칩 색 = 경로 결과 칩과 같은 유형 색(routes.js BUS_COLOR). 서울 마을버스는 실제 초록이라 원천을 아는 여기서만 지선 초록으로
const routeColor = (r) => busColor(r.source === "seoul" && r.type === "마을" ? "지선" : r.type);

let v = null;      // createMap() 이 돌려준 지도 핸들
let shown = null;  // 지금 강조한 노선 id
let bar = null;    // 지도 왼쪽 아래 "[380] 경기 일반 버스 · 정류장 54곳 ×" (노선 번호는 유형 색 칩)

export function initStopRoutes(view) {
  v = view;
  const Bar = L.Control.extend({
    onAdd() {
      const d = L.DomUtil.create("div", "hl-bar");
      d.hidden = true;
      d.innerHTML = '<span class="hl-text"></span>'
        + '<button type="button" class="hl-close" title="노선 강조 끄기" aria-label="노선 강조 끄기">×</button>';
      d.querySelector("button").addEventListener("click", clearHighlight);
      L.DomEvent.disableClickPropagation(d); // 누른 것이 지도 클릭(출발/도착 메뉴)이 되지 않게
      L.DomEvent.disableScrollPropagation(d);
      return d;
    },
  });
  bar = new Bar({ position: "bottomleft" }).addTo(v.map).getContainer();
}

// 정류장 메뉴에 붙일 "경유 노선" 칸. 칸을 먼저 돌려주고 노선은 받아 오는 대로 채운다. at = 누른 정류장 위치
export function stopRoutesSection(busKey, at) {
  const sec = document.createElement("div");
  sec.className = "odm-routes";
  const say = (text) => { sec.innerHTML = `<p class="odm-routes-msg">${esc(text)}</p>`; };
  say("경유 노선 불러오는 중…");
  getJSON(`/api/stops/${encodeURIComponent(busKey)}/routes`)
    .then(({ routes = [] }) => {
      if (!routes.length) return say("경유 노선 정보 없음");
      sec.innerHTML = `<p class="odm-routes-msg">경유 노선 ${routes.length}</p><div class="odm-chips">`
        + routes.map((r) => `<button type="button" class="route-chip" data-route="${esc(r.id)}" aria-pressed="${r.id === shown}"`
          + ` style="${chipStyle(routeColor(r))};--c:${routeColor(r)}" title="${esc(kind(r))} 버스 · 정류장 ${r.n_stops}곳">`
          + `${esc(r.name)}</button>`).join("")
        + "</div>";
      for (const b of sec.querySelectorAll(".route-chip")) {
        b.addEventListener("click", () => (b.dataset.route === shown ? clearHighlight() : highlight(b.dataset.route, at, sec)));
      }
    })
    .catch((err) => say(err.message));
  return sec;
}

// 노선이 지나는 정류장을 강조 점으로(누른 정류장도 같은 점). 지도는 노선 전체가 보이게 맞추되,
// 누른 정류장의 메뉴가 가리지 않게 메뉴 크기만큼 위·옆에 여유를 둔다(메뉴를 열어 둔 채 다른 노선과 견줄 수 있게)
async function highlight(id, at, sec) {
  shown = id;
  syncChips();
  let route;
  try {
    route = await getJSON(`/api/routes/${encodeURIComponent(id)}`);
  } catch (err) {
    if (shown !== id) return;
    clearHighlight();
    showBar(esc(err.message));
    return;
  }
  if (shown !== id) return; // 그사이 다른 노선을 눌렀거나 껐다
  const g = v.hlLayer;
  const renderer = v.renderers.hl;
  const c = routeColor(route);
  g.clearLayers();
  const pts = route.stops.map((s) => L.latLng(s.lat, s.lon));
  const dot = (p, radius, style) => L.circleMarker(p, { radius, renderer, interactive: false, ...style }).addTo(g);
  // 윤곽을 먼저 다 깔고 색 점을 위에 — 가까운 정류장끼리 뒤 점의 윤곽이 앞 점을 덮지 않게
  for (const p of pts) dot(p, 5, CASING);
  for (const p of pts) dot(p, 5, { color: c, weight: 3, fillColor: "#FFFFFF", fillOpacity: 1 });
  g.addTo(v.map);
  showBar(`<span class="chip" style="${chipStyle(c)}">${esc(route.name)}</span> ${esc(kind(route))} 버스 · 정류장 ${pts.length}곳`, c);
  const pop = sec.closest(".leaflet-popup");
  const w = pop ? Math.round(pop.offsetWidth / 2) + 10 : 40;
  v.map.fitBounds(L.latLngBounds([...pts, at]), {
    paddingTopLeft: [w, pop ? pop.offsetHeight + 20 : 40], paddingBottomRight: [w, 40], maxZoom: 16,
  });
}

function clearHighlight() {
  shown = null;
  v.hlLayer.clearLayers();
  v.hlLayer.remove();
  bar.hidden = true;
  syncChips();
}

// color = 노선 유형 색 (막대 왼쪽 띠). 오류 문구면 없음
function showBar(html, color = null) {
  bar.querySelector(".hl-text").innerHTML = html;
  if (color) bar.style.setProperty("--c", color);
  else bar.style.removeProperty("--c");
  bar.hidden = false;
}

// 열린 메뉴의 칩에 지금 강조한 노선 표시
function syncChips() {
  for (const b of document.querySelectorAll(".route-chip")) b.setAttribute("aria-pressed", String(b.dataset.route === shown));
}
