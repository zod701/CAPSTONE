// 지도·배경지도·정류장/역·시군구 경계.
import { getJSON, esc, safeColor } from "./api.js";

const DATA_ATTR = "정류장: 국토교통부 · 노선: 서울 열린데이터광장·경기도 버스정보 · 역: 전국도시철도역사정보표준데이터";
const BOUNDARY_ATTR = "행정경계: 통계청 SGIS(공공누리 1유형), 가공 vuski/admdongkor (CC BY 4.0)";
const BUS_MIN_ZOOM = 15;
const SUBWAY_MIN_ZOOM = 10;
const SPLIT_COLOR = "#D32F2F";
// 캔버스·GeoJSON 은 CSS 변수를 못 쓰므로 테마별 색을 여기 둔다 (다크: 어두운 배경지도에서 보이게 밝게)
const isDark = () => document.documentElement.dataset.theme === "dark";
// 버스 정류장 점: 다크 모드의 야간 지도가 남색이라 파랑 계열은 묻힌다 → 반대색인 호박색 + 얇은 어두운 테두리(흰 글씨 위에서도 윤곽)
const busStyle = () => (isDark() ? { fill: "#FFCA28", line: "#1b1e23" } : { fill: "#1565C0", line: "#1565C0" });
const boundaryColor = () => (isDark() ? "#B0BEC5" : "#37474F");

const MERGE_LABEL = {
  single: "단독",
  merged_near: "근접 병합 (≤ 50 m)",
  merged_far: "원거리 병합 (이름 호환)",
  split: "분리 (같은 번호, 다른 위치)",
  override_merge: "수동 병합",
  override_split: "수동 분리",
};

// onPlaceClick({latlng, name, color, info, line?, busKey?}) — 정류장·역을 누르면 (info = 정류장 정보 HTML, 이미 이스케이프됨.
// line = 지하철 노선군, busKey = 버스 정류장 키 — 경유 노선 칩용, 미정차는 없음)
export function createMap(el, { onError = () => {}, onPlaceClick = () => {} } = {}) {
  const map = L.map(el, {
    preferCanvas: true,
    renderer: L.canvas({ tolerance: 3 }), // 반지름 3 px 점도 클릭되게
    maxBounds: [[33, 124], [39.5, 132]],
  }).setView([37.40, 127.10], 11);

  const vworld = { minZoom: 6, maxZoom: 19, attribution: "© VWorld (국토교통부)" };
  const vw = (layer, opts = {}) => L.tileLayer(`/tiles/vworld/${layer}/{z}/{y}/{x}`, { ...vworld, ...opts });
  // 목록의 한 항목 = 밝은·어두운 타일 한 쌍 — 모드가 바뀌면 같은 항목 안에서 타일만 바뀐다.
  // 어두운 판이 있는 것은 VWorld 기본(→ midnight)뿐: OSM 은 공식 어두운 스타일이 없다
  // (다른 업체의 OSM 기반 어두운 지도는 키·호출 제한이 있어 넣지 않았다). 백지도는 사용자가 뺐다.
  const bases = {
    // 야간은 z18 까지만 제공된다 — z19 에서는 z18 타일을 확대해 쓴다 (없으면 배경이 빈다)
    "VWorld 기본": themedBase(vw("Base"), vw("midnight", { maxNativeZoom: 18 })),
    "OpenStreetMap": themedBase(L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    })),
  };
  bases["VWorld 기본"].addTo(map);

  // 정류장·역·경계는 기본 캔버스 하나에 둔다(캔버스를 겹치면 맨 위 것만 클릭을 받음).
  // 경로·노선 강조·진단은 그 위의 SVG 창에 두고 창 자체는 클릭을 통과시킨다 — 선·원만 이벤트를 받는다.
  const renderers = {};
  for (const [name, z] of [["car", 440], ["route", 450], ["hl", 455], ["diag", 460]]) {
    const pane = map.createPane(`${name}Pane`);
    pane.style.zIndex = String(z);
    pane.style.pointerEvents = "none";
    renderers[name] = L.svg({ pane: `${name}Pane` });
  }

  const busLayer = L.layerGroup([], { attribution: DATA_ATTR }).addTo(map);
  const subwayMarkers = L.layerGroup();
  const subwayLayer = L.layerGroup([], { attribution: DATA_ATTR }).addTo(map);
  const boundaryLayer = L.layerGroup([], { attribution: BOUNDARY_ATTR });
  const carGroup = L.layerGroup();
  const transitGroup = L.layerGroup();
  const routeLayer = L.layerGroup([carGroup, transitGroup]).addTo(map);
  const diagLayer = L.layerGroup().addTo(map);
  const hlLayer = L.layerGroup([], { attribution: DATA_ATTR }); // 노선 강조 — 켤 때만 지도에 (노선 출처 표기 포함)

  // 레이어 목록의 하위 항목(들여 씀): 미정차 표시 ⊂ 버스 정류장, 매칭 진단 ⊂ 경로.
  // 미정차 표시는 레이어가 아니라 버스 점 거르개라 우리 체크박스를 버스 정류장 바로 아래 끼운다. Leaflet 이 목록을 다시 그려도
  // 항목마다 _addItem 을 거치므로 여기서 끼우고 들여 쓰면 유지된다. 켜고 끄기는 서로 따로(하위 항목은 모양만 묶음).
  const virtualLabel = L.DomUtil.create("label", "layers-sub");
  virtualLabel.innerHTML = '<span><input type="checkbox" class="virtual-toggle"><span> 미정차 표시</span></span>';
  const virtualBox = virtualLabel.querySelector("input");
  const LayersTree = L.Control.Layers.extend({
    _addItem(obj) {
      const label = L.Control.Layers.prototype._addItem.call(this, obj);
      if (obj.layer === diagLayer) label.classList.add("layers-sub");
      if (obj.layer === busLayer) this._overlaysList.appendChild(virtualLabel);
      return label;
    },
  });
  new LayersTree(bases, {
    "버스 정류장": busLayer,
    "지하철역": subwayLayer,
    "시군구 경계": boundaryLayer, // 서울 구 + 경기 시·군 (경기 일반구는 시로 합친 그대로)
    "경로": routeLayer,
    "매칭 진단": diagLayer,
  }, { collapsed: false }).addTo(map);

  const hint = addControl(map, "bottomleft", "map-hint", "정류장을 보려면 확대하세요");

  // --- 버스 정류장: z ≥ 15 에서 화면 범위만 ---
  let busTimer = null;
  let busAbort = null;
  const scheduleBus = () => {
    clearTimeout(busTimer);
    busTimer = setTimeout(loadBus, 250);
  };

  async function loadBus() {
    if (busAbort) busAbort.abort();
    busAbort = null;
    const on = map.hasLayer(busLayer);
    if (!on || map.getZoom() < BUS_MIN_ZOOM) {
      busLayer.clearLayers();
      hint.hidden = !on;
      return;
    }
    const ctrl = (busAbort = new AbortController());
    const b = map.getBounds();
    const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map((x) => x.toFixed(6)).join(",");
    try {
      const data = await getJSON("/api/stops",
        { bbox, kinds: "bus", virtual: virtualBox.checked ? 1 : 0 }, { signal: ctrl.signal });
      if (ctrl !== busAbort) return;
      busLayer.clearLayers();
      for (const s of data.bus || []) busLayer.addLayer(busMarker(s));
      hint.hidden = !data.truncated;
      // 새로 그린 버스 점이 역 위를 덮지 않게
      if (map.hasLayer(subwayMarkers)) subwayMarkers.eachLayer((m) => m.bringToFront());
    } catch (err) {
      if (err.name !== "AbortError") onError(err);
    }
  }

  // 정류장·역 클릭은 지도로 번지지 않게(bubblingMouseEvents: false) — 지도 클릭 메뉴가 따로 뜨지 않도록.
  // 지하철역은 메뉴에서 역 이름 앞에 노선 이름(노선군)을 붙이고(line), 버스 정류장은 경유 노선을 보인다(busKey)
  const place = (s, color, info, extra = {}) => () =>
    onPlaceClick({ latlng: L.latLng(s.lat, s.lon), name: s.name, color, info: info(s), ...extra });

  function busMarker(s) {
    const split = (s.merge || "").endsWith("split");
    const { fill, line } = busStyle();
    return L.circleMarker([s.lat, s.lon], {
      radius: 3,
      color: split ? SPLIT_COLOR : line,
      weight: split ? 2 : 1,
      fillColor: fill,
      fillOpacity: s.virtual ? 0.3 : 0.9,
      bubblingMouseEvents: false,
    }).on("click", place(s, fill, busInfo, s.virtual ? {} : { busKey: s.id })); // 미정차는 타고 내릴 노선이 없다
  }

  map.on("moveend", scheduleBus);
  virtualBox.addEventListener("change", scheduleBus);
  scheduleBus();

  // --- 지하철역: 한 번만 받고 z ≥ 10 에서 표시 ---
  const syncSubway = () => {
    const show = map.getZoom() >= SUBWAY_MIN_ZOOM;
    if (show && !subwayLayer.hasLayer(subwayMarkers)) subwayLayer.addLayer(subwayMarkers);
    else if (!show && subwayLayer.hasLayer(subwayMarkers)) subwayLayer.removeLayer(subwayMarkers);
  };
  map.on("zoomend", syncSubway);
  getJSON("/api/stops", { kinds: "subway" })
    .then((data) => {
      for (const s of data.subway || []) {
        L.circleMarker([s.lat, s.lon], {
          radius: 5,
          color: "#FFFFFF",
          weight: 1.5,
          fillColor: safeColor(s.color),
          fillOpacity: 1,
          bubblingMouseEvents: false,
        }).on("click", place(s, safeColor(s.color), subwayInfo, { line: s.group || s.line })).addTo(subwayMarkers);
      }
      syncSubway();
    })
    .catch(onError);

  // --- 시군구 경계: 처음 켤 때 받는다. 항상 맨 뒤(클릭이 정류장에 가도록) ---
  let boundary = null;
  let boundaryLoading = false;
  map.on("overlayadd", async (e) => {
    if (e.layer === busLayer) scheduleBus();
    if (e.layer !== boundaryLayer) return;
    if (boundary) {
      boundary.bringToBack();
      return;
    }
    if (boundaryLoading) return;
    boundaryLoading = true;
    try {
      const gj = await getJSON("/api/boundaries");
      boundary = L.geoJSON(gj, {
        style: { color: boundaryColor(), weight: 1, opacity: 0.8, fill: false },
        onEachFeature: (f, layer) => layer.bindTooltip(esc(f.properties?.sgg_nm), { sticky: true }),
      });
      boundaryLayer.addLayer(boundary);
      boundary.bringToBack();
    } catch (err) {
      onError(err);
    } finally {
      boundaryLoading = false;
    }
  });
  map.on("overlayremove", (e) => {
    if (e.layer === busLayer) scheduleBus();
  });

  // 테마가 바뀌면: 배경지도마다 밝은 ↔ 어두운 타일(고른 항목은 그대로), 버스 점·시군구 경계 색 다시
  function setTheme(dark) {
    for (const b of Object.values(bases)) b.setTheme(dark);
    boundary?.setStyle({ color: boundaryColor() });
    scheduleBus();
  }

  return { map, renderers, carGroup, transitGroup, diagLayer, hlLayer, onPlaceClick, setTheme };
}

// 배경지도 목록의 한 항목: 밝은·어두운 타일 레이어를 담는 무리. 테마에 맞는 쪽 하나만 무리에 넣는다
// (목록의 라디오는 무리를 가리키므로 테마가 바뀌어도 고른 항목이 유지된다). 어두운 판이 없으면 dark = light.
function themedBase(light, dark = light) {
  const group = L.layerGroup([isDark() ? dark : light]);
  group.setTheme = (on) => {
    const want = on ? dark : light;
    if (!group.hasLayer(want)) {
      group.clearLayers();
      group.addLayer(want);
    }
  };
  return group;
}

function addControl(map, position, className, html) {
  const Box = L.Control.extend({
    onAdd() {
      const div = L.DomUtil.create("div", className);
      div.innerHTML = html;
      L.DomEvent.disableClickPropagation(div);
      L.DomEvent.disableScrollPropagation(div);
      return div;
    },
  });
  return new Box({ position }).addTo(map).getContainer();
}

// 원 표기(도시명)와 좌표 판정 시군이 같은 곳을 가리키는지. 서울 원 표기는 "서울특별시" 뿐이다.
function sameRegion(label, sgg) {
  if (!label || !sgg) return true;
  return label.endsWith(sgg) || (label === "서울특별시" && sgg.endsWith("구"));
}

function row(dt, dd, cls = "") {
  return `<dt>${dt}</dt><dd${cls ? ` class="${cls}"` : ""}>${dd}</dd>`;
}

const list = (s) => (s || "").split("|").filter(Boolean).map(esc).join(", ");

function busInfo(s) {
  const rows = [row("정류장 키", esc(s.id)), row("ARS", list(s.ars) || "없음")];
  if (sameRegion(s.label, s.sgg)) {
    rows.push(row("판정 시군", esc(s.sgg)), row("원 표기", esc(s.label) || "—"));
  } else {
    rows.push(row("지역", `원 표기 ${esc(s.label)} → 판정 ${esc(s.sgg)}`, "diff"));
  }
  rows.push(row("원천 ID", list(s.src)), row("병합", esc(MERGE_LABEL[s.merge] || s.merge)));
  if (s.aliases) rows.push(row("다른 이름", list(s.aliases)));
  if (s.snap_m != null) {
    rows.push(row("경계 보정", `경계 밖 ${Math.round(s.snap_m)} m → 가장 가까운 시군`, "warn"));
  }
  if (s.virtual) rows.push(row("미정차", "경로 표현용 통과 노드 (승하차 불가)", "warn"));
  return `<dl>${rows.join("")}</dl>`;
}

function subwayInfo(s) {
  const stale = s.date && s.date < "2024-01-01";
  const date = s.date
    ? esc(s.date) + (stale ? ' <span class="warn">기준일 오래됨</span>' : "")
    : '<span class="warn">기준일 없음 (원천 값 무효)</span>';
  const rows = [
    row("노선", `${esc(s.group)} / ${esc(s.line)}`),
    row("운영", esc(s.operator)),
    row("기준일", date),
    row("환승역", s.transfer ? "예" : "아니오"),
    row("시군", esc(s.sgg)),
    row("역 ID", esc(s.id)),
  ];
  return `<dl>${rows.join("")}</dl>`;
}
