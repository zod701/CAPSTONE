// 대중교통 경로·택시·매칭 진단: 지도 그리기 + 사이드바 카드/표.
import { esc, safeColor } from "./api.js";

const COLOR = { BUS: "#1E88E5", SUBWAY: "#8E24AA", WALKING: "#757575", TAXI: "#FFC107", OTHER: "#9E9E9E" };
// 버스 유형(카카오 vehicles[].type, 실측 · 노선 순서표 route_type) → 실제 차체 색. 서울: 간선 파랑 · 지선 초록 · 순환 노랑 · 공항 (서울 BIS 노선색),
// 경기: G버스 표준 도색 (일반 초록 · 마을 노랑 · 좌석 파랑 · 직행좌석 빨강 · 시외일반 초록). 카카오는 경기 직행좌석을 "광역"·"직행" 둘 다로,
// M버스를 "광역"으로 준다 → 둘 다 빨강. 서울 마을버스는 초록이지만 카카오 유형만으로는 경기와 구분되지 않아 경기 노랑
// (원천을 아는 정류장 경유 노선 칩은 서울 마을을 초록으로 — stoproutes.js). 표준 도색이 없는 심야·관광·수요응답(순서표에만 있음)은
// 기타 회색 하나. 없는 유형은 COLOR.BUS
const OTHER_BUS = "#607D8B";
const BUS_COLOR = {
  간선: "#3D5BAB", 지선: "#5BB025", 순환: "#F99D1C", 광역: "#EE2737", 직행: "#EE2737",
  일반: "#18BF74", 마을: "#FED70F", 좌석: "#3C96D5", 시외: "#009775", 공항: "#AA9872",
  심야: OTHER_BUS, 관광: OTHER_BUS, 수요응답: OTHER_BUS,
};
export const busColor = (type) => BUS_COLOR[type] || COLOR.BUS;
const TYPE_LABEL = { BUS: "버스", SUBWAY: "지하철", WALKING: "도보" };
const ROLE_LABEL = { board: "승차", alight: "하차" };
const STATUS_COLOR = { matched: "#2E7D32", ambiguous: "#EF6C00", unmatched: "#C62828" };
const KAKAO_ATTR = "경로 © Kakao, Kakao Mobility";
const FIT = { padding: [40, 40] };

let v = null; // createMap() 이 돌려준 지도 핸들
let attrOn = false;
let stopLabels = []; // 선택 경로의 정류장 이름표 [{marker, end}]
let stopsOpen = false; // 진단의 "승·하차 정류소" 접힘 상태 (경로를 바꿔도 유지)

export function initRoutes(view) {
  v = view;
  v.map.on("zoomend overlayadd", declutterLabels);
}

const num = (x) => Math.round(x).toLocaleString("ko-KR");
// 시간·거리·요금 표기는 하이브리드 목록(hybrid.js)도 같은 것을 쓴다 — 한 화면에서 잣대가 달라 보이지 않게
export const fmtMin = (s) => {
  if (s == null) return "–";
  const m = Math.round(s / 60);
  return m >= 60 ? `${Math.floor(m / 60)}시간 ${m % 60}분` : `${m}분`;
};
const fmtKm = (m) => (m == null ? "–" : `${(m / 1000).toFixed(1)} km`);
const fmtM = (d) => (d == null ? "–" : `${num(d)} m`);
export const fmtDist = (m) => (m == null ? "–" : m < 1000 ? `${num(m)} m` : fmtKm(m));
export const fmtWon = (w) => (w == null ? "–" : `${num(w)}원`);
const pct = (r) => (r == null ? "–" : `${(r * 100).toFixed(1)}%`);
const ll = (p) => [p[1], p[0]]; // [lon, lat] → Leaflet [lat, lon]

function routeLabel(route) {
  const types = new Set((route.steps || []).map((s) => s.type).filter((t) => t && t !== "WALKING"));
  const parts = [];
  if (types.delete("BUS")) parts.push("버스");
  if (types.delete("SUBWAY")) parts.push("지하철");
  parts.push(...types);
  return parts.join("+") || route.type || "경로";
}

// 첫 승차 대기를 실시간 도착정보로 잡은 방식(서버 wait_source) — 셋 다 화면에는 초록 '실시간' 으로 나오고, 마우스를 올리면
// 무엇으로 잡았는지 밝힌다. realtime = 탈 차의 도착 예측 초 · realtime_stops = 남은 정류장·역 수로 어림 ·
// realtime+headway = 알려진 차를 걸어가는 동안 다 놓쳐 마지막 차 뒤로 배차간격을 이음
const LIVE_WAIT = {
  realtime: "실시간 도착 예측",
  realtime_stops: "실시간 위치로 어림 — 도착 예측 초가 없어 탈 차의 남은 정류장·역 수로 도착 시간을 잡았습니다",
  "realtime+headway": "실시간 + 배차 — 알려진 차는 정류장까지 걸어가는 동안 모두 떠나, 마지막 차 뒤로 배차간격을 이어 다음 차를 잡았습니다",
};
const isLiveWait = (source) => Object.hasOwn(LIVE_WAIT, source || "");

// 실시간 대기 표시에 올리는 설명 — 방식 · 걸어가는 시간 · 배차로만 추정했을 때의 값
function liveWaitTitle(s) {
  const lines = [LIVE_WAIT[s.wait_source]];
  if (s.walk_to_stop_s != null) lines.push(`정류장까지 걸어가는 ${fmtMin(s.walk_to_stop_s)}을 빼고 그 뒤 첫 차까지`);
  if (s.wait_source === "realtime+headway" && s.headway_m != null) lines.push(`배차간격 ${Math.round(s.headway_m)}분`);
  if (s.static_wait_s != null) lines.push(`배차로만 추정하면 약 ${fmtMin(s.static_wait_s)}`);
  return lines.join("\n");
}

// 카카오 소요 시간에는 차를 기다리는 시간이 없다(실측) — 배차로 추정한 대기를 더한 값을 옆에 작게.
// 한 구간이라도 배차를 모르면 서버가 합을 주지 않는다(total_with_wait_s = null) → 그 경로는 지금까지처럼 시간만 보인다
function waitText(r) {
  if (r.total_with_wait_s == null) return "";
  const day = cards?.transit?.wait_basis?.day_type;
  const rides = (r.steps || []).filter((s) => s.headway_m != null);
  const per = rides.map((s) => `${s.type === "SUBWAY" ? "지하철" : "버스"} ${Math.round(s.headway_m)}분`).join(" · ");
  // 도시철도 배차는 평일 시각표(GTFS)에서 센 값이라 주말에도 평일 값이다 — 무엇을 근거로 삼았는지 밝힌다
  const basis = [day && rides.some((s) => s.type !== "SUBWAY") ? `버스 ${day} 배차` : "",
    rides.some((s) => s.type === "SUBWAY") ? "지하철 평일 시각표" : ""].filter(Boolean).join(" · ");
  const live = (r.steps || []).some((s) => isLiveWait(s.wait_source));
  const why = `기다리는 시간 ${fmtMin(r.wait_s)}을 더한 값 — `
    + (live ? "첫 승차는 실시간 도착정보, 나머지는 구간 배차의 절반으로 추정" : "구간 배차의 절반으로 추정")
    + (per ? `
구간 배차: ${per}` : "") + (basis ? `
기준: ${basis}` : "");
  return `<span class="rc-wait" title="${esc(why)}">대기 포함 ${fmtMin(r.total_with_wait_s)}</span>`;
}

function fareText(f) {
  if (f?.value != null) return fmtWon(f.value);
  if (f?.min != null || f?.max != null) {
    return `${f.min != null ? num(f.min) : "?"}–${f.max != null ? num(f.max) : "?"}원`;
  }
  return "요금 정보 없음";
}

// 한 구간에 탈 수 있는 차량을 전부 (카카오가 같은 차량을 두 번 주기도 해서 겹친 것만 뺀다)
function vehicles(s) {
  const seen = new Set();
  return (s.vehicles || []).filter((x) => {
    const k = `${x.type}|${x.name}`;
    if (!x.name || seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

function stepName(s) {
  const names = [...new Set(vehicles(s).map((x) => x.name))];
  return names.length ? names.join(" · ") : TYPE_LABEL[s.type] || s.type || "?";
}

// 한 구간에 유형이 다른 버스가 섞이면 구간 색(지도 선 · 타임라인 세로선 · 역 목록 선)은 가장 많은 색 — 같으면 먼저 나온 쪽.
// 색으로 세므로 같은 빨강인 광역·직행은 함께 센다
function majorBusColor(s) {
  const n = new Map();
  for (const x of vehicles(s)) {
    const c = busColor(x.type);
    n.set(c, (n.get(c) || 0) + 1);
  }
  let best = COLOR.BUS;
  let max = 0;
  for (const [c, k] of n) if (k > max) [best, max] = [c, k];
  return best;
}

function stepStyle(s) {
  if (s.type === "BUS") return { color: majorBusColor(s), weight: 8 };
  if (s.type === "SUBWAY") return { color: safeColor(s.color, COLOR.SUBWAY), weight: 8 };
  if (s.type === "WALKING") return { color: COLOR.WALKING, weight: 5, dashArray: "1 9", lineCap: "round", className: "route-walk" };
  return { color: COLOR.OTHER, weight: 6 };
}

function matchBadge(s) {
  if (!s) return "";
  const cls = s.unmatched ? "bad" : s.ambiguous ? "warn" : "ok";
  const title = `모호 ${s.ambiguous} · 미매칭 ${s.unmatched} · 건너뜀 ${s.skipped}`;
  return `<span class="badge ${cls}" title="${title}">매칭 ${s.matched}/${s.n - s.skipped}</span>`;
}

// 칩 바탕 · 글자색. 아주 밝은 바탕(마을버스 노랑)은 흰 글씨가 안 보여 어두운 글씨로 (상대 휘도 > 0.6 — 지하철 노선색은 해당 없음)
export function chipStyle(c) {
  const lin = (i) => {
    const x = parseInt(c.slice(i, i + 2), 16) / 255;
    return x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4;
  };
  const light = 0.2126 * lin(1) + 0.7152 * lin(3) + 0.0722 * lin(5) > 0.6;
  return `background:${c}${light ? ";color:#1b1e23;text-shadow:none" : ""}`;
}

const chip = (text, color) => `<span class="chip" style="${chipStyle(color)}">${esc(text)}</span>`;

// 버스 구간은 번호마다 제 유형 색 칩(같은 번호는 한 번), 그 밖은 구간 칩 하나
function stepChips(s) {
  const vs = vehicles(s);
  if (s.type !== "BUS" || !vs.length) return chip(stepName(s), stepStyle(s).color);
  const seen = new Set();
  return vs.filter((x) => !seen.has(x.name) && seen.add(x.name))
    .map((x) => chip(x.name, busColor(x.type))).join(" ");
}

// 경로 칩 줄 — 하이브리드 카드도 같은 줄을 쓴다(택시 구간은 TAXI 한 칸)
export function chips(route) {
  return (route.steps || [])
    .filter((s) => s.type !== "WALKING")
    .map((s) => (s.type === "TAXI" ? chip("택시", COLOR.TAXI) : stepChips(s)))
    .join('<span class="chip-sep">›</span>');
}

// --- 대중교통 카드: 종류별 탭(전체 · 버스 · 지하철 · 버스+지하철) + 정렬 ---

const SORTS = [ // [값, 이름, 기준(경로, 번호)] — 기준이 없으면 맨 뒤, 같으면 소요 시간 → 카카오 순서
  ["time", "최소 시간순", (r) => r.total_time_s],
  ["fare", "최소 비용순", (r) => r.fare?.value ?? r.fare?.min], // 요금이 범위(min–max)로 오면 낮은 쪽
  ["distance", "최소 거리순", (r) => r.total_distance_m],
  ["transfers", "최소 환승순", (r) => r.transfers],
  ["walk", "최소 도보순", (r, i) => cards.walk[i]],
];
// 결과 목록의 보기 상태 {el, transit, onSelect, walk, group, sort, expanded, selected, extras}.
// 대중교통 경로(키 "t:번호")와 하이브리드 경로(키 "h:번호", setExtraCards)를 한 목록에 그린다 — selected·expanded 는 그 키
let cards = null;

// 탭·정렬 버튼은 결과 머리(app.js)가 그린다 — 정렬 선택지와 종류별 경로 수만 내준다
export const SORT_OPTIONS = SORTS.map(([k, label]) => [k, label]);
export function routeGroupCounts(transit) {
  const counts = new Map();
  for (const r of transit?.routes || []) counts.set(routeLabel(r), (counts.get(routeLabel(r)) || 0) + 1);
  return counts;
}

// 결과를 지울 때 목록 상태도 버린다 — 남겨 두면 다음 탭 전환이 지운 경로를 다시 그린다
export function resetRouteCards() {
  cards = null;
}

// 결과 머리의 탭(group: null = 전체 · "hybrid" · 대중교통 종류 이름)·정렬로 목록을 다시 그린다
// → 보이는 대중교통 경로 번호(정렬 순서). 목록이 없으면 []
export function setRouteView({ group, sort } = {}) {
  if (!cards) return [];
  if (group !== undefined) cards.group = group;
  if (sort !== undefined) cards.sort = sort;
  drawCards();
  return visibleEntries().filter((e) => e.kind === "t").map((e) => e.i);
}

// 하이브리드 카드를 같은 목록에 넣는다 — items: [{html, route(펼칠 때의 타임라인), time_s, fare, onSelect}] (hybrid.js 가 만든다)
export function setExtraCards(items) {
  if (!cards) return;
  cards.extras = items;
  drawCards();
}

// 걷는 거리(m) = 카카오 도보 구간 거리 + 응답에 없는 첫·끝 도보의 직선거리(지도에 점선 직선으로 그리는 그 구간)
function walkMeters(route, origin, dest) {
  const steps = route.steps || [];
  let m = steps.reduce((a, s) => a + (s.type === "WALKING" && typeof s.distance_m === "number" ? s.distance_m : 0), 0);
  const withPath = steps.filter((s) => s.path?.length);
  if (withPath.length) {
    if (origin) m += v.map.distance(origin, ll(withPath[0].path[0]));
    if (dest) m += v.map.distance(ll(withPath.at(-1).path.at(-1)), dest);
  }
  return m;
}

const numOr = (x) => (typeof x === "number" && Number.isFinite(x) ? x : Infinity);

// 경로 목록을 그리고, 보이는 첫 경로를 고른다(onSelect). 카드의 data-i 는 transit.routes 의 원래 번호.
// group·sort 는 결과 머리의 탭·정렬(탭 전환은 setRouteView). origin·dest(검색한 출발·도착)는 첫·끝 도보 거리 추정에 쓴다.
export function renderRouteCards(el, transit, onSelect, { origin = null, dest = null, group = null, sort = SORTS[0][0] } = {}) {
  cards = { el, transit, onSelect, walk: transit.routes.map((r) => walkMeters(r, origin, dest)),
    group, sort, expanded: null, selected: null, extras: [] };
  el.innerHTML = '<div class="rt-list"></div>';
  drawCards();
  const first = visibleEntries().find((e) => e.kind === "t");
  if (first) onSelect(first.i);
}

// 하이브리드 경로에 있는 정렬 기준 — 거리·환승·도보는 값이 없어 맨 뒤로 간다(기준이 없으면 맨 뒤)
const EXTRA_SORT = { time: (x) => x.time_s, fare: (x) => x.fare };

// 지금 탭의 경로를 정렬 순서대로 → [{kind "t"|"h", i, key}].
// 전체 탭에서 하이브리드와 섞일 때 시간순은 대중교통도 **대기 포함 시간**(없으면 카카오 시간)으로 견준다 —
// 하이브리드 시간에는 대기가 들어 있고 카카오 시간에는 없어(method.md E15) 그대로 섞으면 대중교통이 부당하게 앞선다.
// 대중교통 종류 탭은 섞이지 않으니 카카오 시간 그대로다
function visibleEntries() {
  const { transit, group, sort, extras } = cards;
  const mixed = group === null && extras.length > 0;
  const tKey = mixed && sort === "time" ? (r) => r.total_with_wait_s ?? r.total_time_s : SORTS.find(([k]) => k === sort)[2];
  const out = [];
  if (group !== "hybrid") {
    transit.routes.forEach((r, i) => {
      if (group === null || routeLabel(r) === group) out.push({ kind: "t", i, key: `t:${i}`, v: tKey(r, i), tie: r.total_time_s });
    });
  }
  if (group === null || group === "hybrid") {
    extras.forEach((x, i) => out.push({ kind: "h", i, key: `h:${i}`, v: EXTRA_SORT[sort]?.(x), tie: x.time_s }));
  }
  return out.sort((a, b) => numOr(a.v) - numOr(b.v) || numOr(a.tie) - numOr(b.tie)
    || (a.kind === b.kind ? a.i - b.i : a.kind === "t" ? -1 : 1));
}

function drawCards() {
  const { el, transit, extras } = cards;
  const list = el.querySelector(".rt-list");
  const shown = visibleEntries();
  list.innerHTML = shown.map((e) => (e.kind === "t" ? routeCardHTML(transit.routes[e.i], e.i) : extras[e.i].html)).join("");
  for (const card of list.querySelectorAll(".route-card")) {
    const key = card.dataset.key;
    card.querySelector(".rc-head").addEventListener("click", () => {
      const wasOpen = card.classList.contains("expanded");
      if (!card.classList.contains("selected")) selectEntry(key); // 지도에 그리기 (이미 선택된 카드면 다시 맞추지 않음)
      expandCard(wasOpen ? null : key);
    });
  }
  paintSelected();
  if (shown.some((e) => e.key === cards.expanded)) expandCard(cards.expanded);
  else cards.expanded = null;
}

const entryOf = (key) => {
  const [kind, n] = key.split(":");
  return { kind, i: Number(n) };
};

function selectEntry(key) {
  const { kind, i } = entryOf(key);
  if (kind === "t") cards.onSelect(i);
  else cards.extras[i].onSelect();
}

function routeCardHTML(r, i) {
  return `
    <div class="route-card" data-i="${i}" data-key="t:${i}">
      <button type="button" class="rc-head" aria-expanded="false">
        <span class="rc-top"><b class="rc-type">${esc(routeLabel(r))}</b>
          <span class="rc-times"><b class="rc-time">${fmtMin(r.total_time_s)}</b>${waitText(r)}</span>${matchBadge(r.diag_summary)}</span>
        <span class="rc-meta">${esc(fareText(r.fare))} · 환승 ${esc(r.transfers ?? "–")}회 · ${fmtKm(r.total_distance_m)} · 도보 약 ${fmtDist(cards.walk[i])}</span>
        <span class="rc-chips">${chips(r)}</span>
      </button>
      <div class="rc-detail" hidden></div>
    </div>`;
}

// 카드 하나만 펼친다(key, 없으면 null). 펼칠 때 처음 한 번만 구간 타임라인을 만든다 — 하이브리드는 택시를 끼운 경로로.
function expandCard(key) {
  cards.expanded = key;
  for (const card of cards.el.querySelectorAll(".route-card")) {
    const open = card.dataset.key === key;
    const detail = card.querySelector(".rc-detail");
    if (open && !detail.childElementCount) {
      const { kind, i } = entryOf(key);
      fillTimeline(detail, kind === "t" ? cards.transit.routes[i] : cards.extras[i].route);
    }
    card.classList.toggle("expanded", open);
    card.querySelector(".rc-head").setAttribute("aria-expanded", String(open));
    detail.hidden = !open;
  }
}

// --- 구간 타임라인: 출발지 → (구간 · 정류장)… → 도착지, 세로로 ---

// 펼친 카드의 구간 타임라인 — 승차 구간을 누르면 정류장·역 목록이 열린다 (하이브리드 카드도 같은 것을 쓴다)
export function fillTimeline(detail, route) {
  detail.innerHTML = routeTimelineHTML(route);
  for (const b of detail.querySelectorAll(".tl-toggle")) {
    b.addEventListener("click", () => {
      const strip = b.nextElementSibling;
      strip.hidden = !strip.hidden;
      b.setAttribute("aria-expanded", String(!strip.hidden));
    });
  }
}

// 구간 타임라인. 하이브리드는 택시를 TAXI 구간 하나로 끼워 넣는다 — {time_s, distance_m, fare, counted_s, from_name, to_name}.
// from_name·to_name 이 없으면 그 끝이 출발지·도착지다
export function routeTimelineHTML(route) {
  const steps = route.steps || [];
  const items = [];
  // wait = 그 지점에서 차를 기다리는 시간 — 타고 오르는 지점에만 붙는다. 보통은 배차의 절반 추정이고,
  // liveTitle 이 있으면 실시간 도착정보로 잡은 값이다(첫 승차 — 정류장까지 걷는 동안 떠나는 차는 뺀 뒤 첫 차)
  const node = (name, end = false, wait = null, liveTitle = null) => {
    const prev = items.at(-1);
    if (!end && prev?.kind === "node" && prev.name === name) { // 도보 없이 이어 탈 때 같은 역은 한 번만
      if (wait != null) Object.assign(prev, { wait, liveTitle });
      return;
    }
    items.push({ kind: "node", name, end, wait, liveTitle });
  };
  const isRide = (s) => s?.type === "BUS" || s?.type === "SUBWAY";
  // 카카오가 주지 않는 첫·끝 도보. 택시가 출발지에서 떠나거나 도착지에 닿으면 그 끝에는 도보가 없다
  const edgeWalk = (s, end) => s && s.type !== "WALKING" && (s.type !== "TAXI" || s[end]);
  node("출발지", true);
  if (edgeWalk(steps[0], "from_name")) items.push({ kind: "missing" });
  for (const [k, s] of steps.entries()) {
    if (s.type === "WALKING") {
      items.push({ kind: "walk", s });
      continue;
    }
    if (s.type === "TAXI") {
      // 택시 양끝 이름은 이웃 구간이 이름을 주지 않을 때만 — 같은 정류장이 이름만 조금 달라 두 번 나오지 않게
      if (s.from_name && items.at(-1)?.kind !== "node") node(s.from_name);
      items.push({ kind: "taxi", s });
      if (s.to_name && !isRide(steps[k + 1])) node(s.to_name);
      continue;
    }
    const stops = s.stops || [];
    node(stops[0] ?? s.board_name ?? "?", false, s.wait_s ?? null, isLiveWait(s.wait_source) ? liveWaitTitle(s) : null);
    items.push({ kind: "ride", s });
    node(stops.length >= 2 ? stops.at(-1) : s.alight_name ?? "?");
  }
  if (edgeWalk(steps.at(-1), "to_name")) items.push({ kind: "missing" });
  node("도착지", true);
  return `<ol class="tl">${items.map(timelineItem).join("")}</ol>`;
}

const rail = (line = "") => `<span class="tl-rail">${line}</span>`;

function timelineItem(it) {
  if (it.kind === "node") {
    const wait = it.wait == null ? ""
      : it.liveTitle ? `<span class="tl-wait live" title="${esc(it.liveTitle)}">대기 ${fmtMin(it.wait)} · 실시간</span>`
      : `<span class="tl-wait" title="배차의 절반으로 추정한 기다리는 시간">대기 약 ${fmtMin(it.wait)}</span>`;
    return `<li class="tl-node${it.end ? " tl-end" : ""}">${rail('<span class="tl-dot"></span>')}`
      + `<span class="tl-name">${esc(it.name)}${wait}</span></li>`;   // 레일 오른쪽 칸은 하나다 — 이름 안에 붙인다
  }
  if (it.kind === "missing") {
    return `<li class="tl-seg tl-walk tl-missing">${rail('<span class="tl-line"></span>')}`
      + '<span class="tl-info"><span class="tl-meta">도보</span></span></li>';
  }
  if (it.kind === "taxi") {
    const t = it.s;
    // 택시 뒤에 차를 잡아야 하면 도착 추정에 여유를 둔다 — 그 여유까지 총 시간에 들어갔음을 밝힌다
    const buffer = t.counted_s != null && t.counted_s - (t.time_s ?? 0) >= 30
      ? ` · 연결 여유 포함 ${fmtMin(t.counted_s)}` : "";
    return `<li class="tl-seg tl-ride tl-taxi">${rail(`<span class="tl-line" style="background:${COLOR.TAXI}"></span>`)}`
      + `<div class="tl-info"><span class="tl-vehs">${chip("택시", COLOR.TAXI)}</span>`
      + `<span class="tl-meta">${fmtMin(t.time_s)} · ${fmtDist(t.distance_m)} · 요금 ${fmtWon(t.fare)}${buffer}</span></div></li>`;
  }
  const s = it.s;
  if (it.kind === "walk") {
    return `<li class="tl-seg tl-walk">${rail('<span class="tl-line"></span>')}`
      + `<span class="tl-info"><span class="tl-meta">도보 ${fmtMin(s.time_s)} · ${fmtDist(s.distance_m)}</span></span></li>`;
  }
  const color = stepStyle(s).color;
  const n = (s.stops || []).length;
  const vs = vehicles(s);
  const vehHTML = vs.length
    ? vs.map((x) => `<span class="tl-veh">${chip(x.name, s.type === "BUS" ? busColor(x.type) : color)}`
      + (x.type ? `<span class="tl-vtype">${esc(x.type)}</span>` : "") + "</span>").join("")
    : chip(TYPE_LABEL[s.type] || s.type || "?", color);
  const count = n ? ` · ${n}개 ${s.type === "SUBWAY" ? "역" : "정류장"}` : "";
  return `<li class="tl-seg tl-ride">${rail(`<span class="tl-line" style="background:${color}"></span>`)}`
    + `<div class="tl-info"><button type="button" class="tl-toggle" aria-expanded="false"${n ? ' title="역 목록 보기"' : " disabled"}>`
    + `<span class="tl-vehs">${vehHTML}</span>`
    + `<span class="tl-meta">${fmtMin(s.time_s)} · ${fmtDist(s.distance_m)}${count}</span></button>`
    + `<div class="strip" hidden>${stopStripHTML(s.stops || [], color)}</div></div></li>`;
}

// 한 구간의 정류장·역을 가로로 (길면 가로 스크롤)
// 한 줄에 STRIP_COLS 개씩 ㄹ자로: 짝수 줄은 왼쪽 → 오른쪽, 홀수 줄은 오른쪽 → 왼쪽(CSS row-reverse — 문서 순서는 늘 운행 순서).
// 같은 줄의 선은 칸마다 필요한 반쪽(l = 왼쪽 반, r = 오른쪽 반)만, 줄 끝은 바깥으로 꺾이는 선(.turn)이 다음 줄 같은 자리의 점으로 잇는다.
const STRIP_COLS = 4;

function stopStripHTML(stops, color) {
  const n = stops.length;
  const rows = [];
  for (let r = 0; r * STRIP_COLS < n; r++) {
    const rtl = r % 2 === 1;
    const cells = stops.slice(r * STRIP_COLS, (r + 1) * STRIP_COLS).map((name, i) => {
      const k = r * STRIP_COLS + i;
      const prev = i > 0; // 같은 줄에서 앞 역과 이어진다
      const next = i < STRIP_COLS - 1 && k < n - 1; // 같은 줄에서 다음 역과 이어진다
      const cls = [(rtl ? next : prev) && "l", (rtl ? prev : next) && "r", (k === 0 || k === n - 1) && "strip-end"]
        .filter(Boolean).join(" ");
      return `<li class="${cls}"><span class="strip-dot"></span><span class="strip-name">${esc(name)}</span></li>`;
    });
    const turn = (r + 1) * STRIP_COLS < n; // 다음 줄이 있으면 줄 끝에서 꺾는다
    rows.push(`<ol class="strip-row${rtl ? " rtl" : ""}${turn ? " turn" : ""}">${cells.join("")}</ol>`);
  }
  return `<div class="strip-snake" style="--c:${color}">${rows.join("")}</div>`;
}

// 지도에 그린 경로 표시 — key "t:번호"(대중교통) · "h:번호"(하이브리드) · null. 탭·정렬로 다시 그려도 유지한다
export function markSelectedKey(key) {
  if (!cards) return;
  cards.selected = key;
  paintSelected();
}

// 대중교통 경로 번호로 (없으면 null)
export function markSelected(el, idx) {
  markSelectedKey(idx == null ? null : `t:${idx}`);
}

function paintSelected() {
  for (const b of cards.el.querySelectorAll(".route-card")) b.classList.toggle("selected", b.dataset.key === cards.selected);
}

// --- 지도: 대중교통 경로 ---

export function drawRoute(route, origin, dest) {
  const g = v.transitGroup;
  const renderer = v.renderers.route;
  g.clearLayers();
  const pts = [];
  const withPath = (route.steps || []).filter((s) => s.path?.length);
  // 테두리를 먼저 다 깔고 색 선을 위에 — 구간이 이어지는 곳에서 뒤 구간 테두리가 앞 구간 선을 덮지 않게.
  // 테두리 색은 CSS(.route-casing)가 테마에 맞춰 정한다: 밝은 화면 흰색, 다크 모드 검정에 가까운 색
  for (const s of withPath) {
    L.polyline(s.path.map(ll), { color: "#FFFFFF", weight: stepStyle(s).weight + 4, opacity: 0.9, renderer, interactive: false,
      className: "route-casing" }).addTo(g);
  }
  for (const s of withPath) {
    const latlngs = s.path.map(ll);
    pts.push(...latlngs);
    L.polyline(latlngs, { ...stepStyle(s), renderer, interactive: false }).addTo(g);
  }
  // 응답에 없는 첫·끝 도보: 출발지 → 첫 구간 시작점, 끝 구간 끝점 → 도착지를 옅은 점선 직선으로
  if (withPath.length) {
    const ends = [[origin, ll(withPath[0].path[0])], [ll(withPath.at(-1).path.at(-1)), dest]];
    for (const [a, b] of ends) {
      if (!a || !b) continue;
      L.polyline([a, b], { color: "#9E9E9E", weight: 5, dashArray: "1 9", lineCap: "round", opacity: 0.9, renderer })
        .bindTooltip("도보 (직선 표시)", { sticky: true })
        .addTo(g);
    }
  }
  drawStops(route, g, renderer);
  if (origin) pts.push(origin);
  if (dest) pts.push(dest);
  if (pts.length) v.map.fitBounds(extendWith(L.latLngBounds(pts), v.carGroup), FIT); // 켜진 택시 경로도 함께
  syncAttribution();
  declutterLabels();
}

// 구간의 정류장·역 위치. 서버가 DB 에서 찾은 stop_locs(중간 정류소 포함)를 쓰고, 정제 데이터가 없어 그게 없으면
// 승·하차만 구간 선 양끝에 둔다. level 이 없으면 선 위 추정 위치다.
function stopPoints(s) {
  const stops = s.stops || [];
  const n = stops.length;
  const at = (k) => {
    const loc = s.stop_locs?.[k];
    if (loc) return { ll: [loc.lat, loc.lon], approx: !loc.level };
    if (s.stop_locs || !s.path?.length || (k !== 0 && k !== n - 1)) return null;
    return { ll: ll(k === 0 ? s.path[0] : s.path.at(-1)), approx: true };
  };
  return stops.map((name, k) => ({ name, end: k === 0 || k === n - 1, ...at(k) })).filter((p) => p.ll);
}

// 선택 경로의 정류장·역마다 점 + 이름표. 환승으로 겹치는 같은 이름(내린 역 = 다시 탄 역)은 한 번만.
function drawStops(route, g, renderer) {
  stopLabels = [];
  for (const s of route.steps || []) {
    if (s.type === "WALKING") continue;
    const color = stepStyle(s).color;
    for (const p of stopPoints(s)) {
      if (stopLabels.some((q) => q.name === p.name && v.map.distance(q.ll, p.ll) < 300)) continue;
      const marker = L.circleMarker(p.ll, {
        radius: p.end ? 6 : 4, color, weight: p.end ? 3 : 2, fillColor: "#FFFFFF", fillOpacity: 1,
        dashArray: p.approx ? "2 3" : null, renderer, interactive: false,
      }).bindTooltip(esc(p.name), {
        permanent: true, direction: "right", offset: [p.end ? 8 : 6, 0],
        className: `stop-label${p.end ? " end" : ""}${p.approx ? " approx" : ""}`,
      }).addTo(g);
      stopLabels.push({ name: p.name, ll: p.ll, end: p.end, marker });
    }
  }
}

// 이름표가 겹치면 뒤의 것을 숨긴다(확대하면 다시 보인다). 승·하차(환승) 이름은 항상 보인다.
function declutterLabels() {
  const items = stopLabels
    .map((it) => ({ end: it.end, el: it.marker.getTooltip()?.getElement() }))
    .filter((it) => it.el?.isConnected)
    .sort((a, b) => b.end - a.end);
  for (const it of items) it.el.style.visibility = "";
  const rects = items.map((it) => it.el.getBoundingClientRect());
  const shown = [];
  const hide = items.map((it, i) => {
    const r = rects[i];
    const hit = !it.end && shown.some((q) => r.left < q.right && r.right > q.left && r.top < q.bottom && r.bottom > q.top);
    if (!hit) shown.push(r);
    return hit;
  });
  items.forEach((it, i) => { if (hide[i]) it.el.style.visibility = "hidden"; });
}

export function clearTransit() {
  v.transitGroup.clearLayers();
  v.diagLayer.clearLayers();
  stopLabels = [];
  syncAttribution();
}

// --- 택시 ---

// fit: 택시 경로와 지도에 켜진 대중교통 경로가 함께 보이게 맞춘다
export function drawCar(car, { fit = false } = {}) {
  const g = v.carGroup;
  const renderer = v.renderers.car;
  g.clearLayers();
  if (car?.path?.length >= 2) {
    const latlngs = car.path.map(ll);
    L.polyline(latlngs, { color: "#000000", weight: 7, opacity: 0.85, renderer, interactive: false }).addTo(g);
    L.polyline(latlngs, { color: COLOR.TAXI, weight: 4, opacity: 1, renderer, interactive: false }).addTo(g);
    if (fit) v.map.fitBounds(extendWith(L.latLngBounds(latlngs), v.transitGroup), FIT);
  }
  syncAttribution();
}

// 무리 안 선들의 범위를 더한다 (점 표지는 선 위에 있으므로 선만)
function extendWith(bounds, group) {
  group.eachLayer((l) => { if (l instanceof L.Polyline) bounds.extend(l.getBounds()); });
  return bounds;
}

export function clearCar() {
  v.carGroup.clearLayers();
  syncAttribution();
}

// 택시 카드: 대중교통 카드처럼 누르면 지도에 경로를 켜고, 다시 누르면 끈다(대중교통 선택과 따로). 처음엔 꺼짐.
export function renderCarCard(el, car, onToggle) {
  if (car.result_code == null || Number(car.result_code) !== 0) {
    el.innerHTML = `<p class="note">자동차 경로를 찾지 못했습니다: ${esc(car.result_msg || "사유 미상")}`
      + ` <small>(result_code ${esc(car.result_code)})</small></p>`;
    return;
  }
  el.innerHTML = `<div class="route-card taxi-card">
    <button type="button" class="rc-head" aria-pressed="false" title="누르면 지도에 택시 경로 표시 · 다시 누르면 숨김">
      <span class="rc-top"><b class="rc-type">택시</b><b class="rc-time">${fmtMin(car.duration_s)}</b></span>
      <span class="rc-meta">거리 ${fmtKm(car.distance_m)} · 택시요금 ${fmtWon(car.fare?.taxi)} · 통행료 ${fmtWon(car.fare?.toll)}</span>
      <span class="muted">카카오모빌리티 제공</span>
    </button>
  </div>`;
  el.querySelector(".rc-head").addEventListener("click", onToggle);
}

export function markCarSelected(el, on) {
  const card = el.querySelector(".taxi-card");
  if (!card) return;
  card.classList.toggle("selected", on);
  card.querySelector(".rc-head").setAttribute("aria-pressed", String(on));
}

function syncAttribution() {
  const on = v.transitGroup.getLayers().length > 0 || v.carGroup.getLayers().length > 0;
  if (on === attrOn) return;
  attrOn = on;
  if (on) v.map.attributionControl.addAttribution(KAKAO_ATTR);
  else v.map.attributionControl.removeAttribution(KAKAO_ATTR);
}

// --- 매칭 진단 ---

function routeEntries(route) {
  const out = [];
  for (const s of route?.steps || []) {
    for (const role of ["board", "alight"]) {
      const e = s.resolution?.[role];
      if (e) out.push([e, s]);
    }
  }
  return out;
}

// 예: [승차] 판교역동편 → DB 판교역동편 (204000022) · 오차 12 m · key · 동명 3곳
function entryText(e) {
  const head = `[${ROLE_LABEL[e.role] || e.role}] ${e.query_name || "(이름 없음)"}`;
  const noun = e.kind === "subway" ? "역" : "정류장";
  const parts = [];
  if (e.chosen) {
    parts.push(`${head} → DB ${e.chosen.name} (${e.chosen.id})`, `오차 ${fmtM(e.error_m)}`);
    if (e.match_level) parts.push(e.match_level);
    parts.push(`동명 ${e.n_global_same_name ?? 0}곳`);
    if (e.status === "ambiguous") parts.push(`모호: 2위와 ${fmtM(e.second_gap_m)} 차`);
  } else if (e.status === "skipped") {
    parts.push(`${head} → 기준점 없음 (건너뜀)`);
  } else {
    parts.push(`${head} → 매칭 실패`);
    parts.push(e.nearest_any
      ? `가장 가까운 ${noun} ${e.nearest_any.name} (${fmtM(e.nearest_any.dist_m)})`
      : `200 m 안에 ${noun} 없음`);
    parts.push(e.nearest_same_name_m != null ? `동명 최근접 ${fmtM(e.nearest_same_name_m)}` : "동명 없음");
  }
  if (e.line_filter && e.line_filter !== "n/a") parts.push(`노선 필터 ${e.line_filter}`);
  if (e.name_conflict) parts.push("이름 불일치 (stops ≠ guidance)");
  return parts.join(" · ");
}

export function drawDiagnostics(route) {
  const g = v.diagLayer;
  const renderer = v.renderers.diag;
  g.clearLayers();
  const entries = routeEntries(route).map(([e]) => e);
  // 오차선(기준점 → DB 좌표)을 먼저 깔고 원을 위에
  for (const e of entries) {
    if (e.chosen && e.anchor) {
      L.polyline([ll(e.anchor), [e.chosen.lat, e.chosen.lon]],
        { color: "#616161", weight: 1.5, opacity: 0.9, renderer, interactive: false }).addTo(g);
    }
  }
  // 진단 원은 승·하차 정류장을 덮으므로, 누르면 정류장처럼 [출발][도착] 메뉴 — 매칭 진단은 ⓘ 카드로.
  // bubblingMouseEvents: false — 클릭이 지도로 번져 일반 메뉴가 따로 뜨지 않게
  for (const [e, s] of routeEntries(route)) {
    const info = `<p>${esc(entryText(e))}</p>`;
    // 지하철역이면 메뉴에서 역 이름 앞에 노선 이름 칩(구간의 노선 색) — 지도에서 역을 누를 때와 같게
    const line = e.kind === "subway" ? e.chosen?.line_group || s.line_group || null : null;
    // 매칭된 버스 정류장이면 정류장 점을 누를 때처럼 경유 노선 칩도(busKey)
    const busKey = e.kind === "bus" && e.chosen ? e.chosen.id : null;
    const menu = (lat, lon, name, color) => () => v.onPlaceClick({ latlng: L.latLng(lat, lon), name, info, line, busKey,
      color: line ? safeColor(s.color, COLOR.SUBWAY) : color });
    if (e.chosen && (e.status === "matched" || e.status === "ambiguous")) {
      const c = STATUS_COLOR[e.status];
      L.circleMarker([e.chosen.lat, e.chosen.lon], { radius: 6, color: c, weight: 2, fillColor: c, fillOpacity: 0.85,
        renderer, bubblingMouseEvents: false }).on("click", menu(e.chosen.lat, e.chosen.lon, e.chosen.name, c)).addTo(g);
    } else if (e.status === "unmatched" && e.anchor) {
      L.circleMarker(ll(e.anchor), { radius: 8, color: STATUS_COLOR.unmatched, weight: 2.5, fillOpacity: 0,
        renderer, bubblingMouseEvents: false })
        .on("click", menu(e.anchor[1], e.anchor[0], e.query_name || "(이름 없음)", STATUS_COLOR.unmatched)).addTo(g);
    }
  }
}

const kindCell = (k) => (k && k.n ? `${pct(k.match_rate)} (${k.matched}/${k.n - k.skipped})` : "–");

const SUMMARY_ROWS = [
  ["판정 대상 (건너뜀)", (s) => `${s.n} (${s.skipped})`],
  ["매칭 / 모호 / 미매칭", (s) => `${s.matched} / ${s.ambiguous} / ${s.unmatched}`],
  ["매칭률", (s) => pct(s.match_rate)],
  ["발견률 (매칭+모호)", (s) => pct(s.found_rate)],
  ["오차 중앙값", (s) => fmtM(s.error_m?.median)],
  ["오차 p90", (s) => fmtM(s.error_m?.p90)],
  ["오차 최대", (s) => fmtM(s.error_m?.max)],
  ["수준 key / alias / prefix", (s) => `${s.levels?.key ?? 0} / ${s.levels?.alias ?? 0} / ${s.levels?.prefix ?? 0}`],
  ["버스 매칭률", (s) => kindCell(s.by_kind?.bus)],
  ["지하철 매칭률", (s) => kindCell(s.by_kind?.subway)],
  ["미매핑 차량 이름", (s) => (s.unmapped_vehicle_names || []).join(", ") || "없음"],
];

export function renderDiagnostics(el, transit, route) {
  if (!transit) {
    el.innerHTML = '<p class="muted">경로를 선택하면 정류소 매칭 결과가 표시됩니다.</p>';
    return;
  }
  if (!route) {
    el.innerHTML = '<p class="muted">진단할 경로가 없습니다.</p>';
    return;
  }
  if (!transit.diag_summary) {
    el.innerHTML = '<p class="muted">정제 데이터가 없어 정류소 매칭 진단을 건너뛰었습니다 (data/README.md).</p>';
    return;
  }
  const sel = route.diag_summary;
  const rows = SUMMARY_ROWS.map(([label, f]) =>
    `<tr><th>${label}</th><td>${sel ? esc(f(sel)) : "–"}</td><td>${esc(f(transit.diag_summary))}</td></tr>`).join("");
  const items = routeEntries(route).map(([e, s]) =>
    `<li class="st st-${esc(e.status)}"><span class="st-step">${esc(TYPE_LABEL[s.type] || s.type)} ${esc(stepName(s))}</span>`
    + ` ${esc(entryText(e))}</li>`).join("");
  el.innerHTML = `<table class="diag"><thead><tr><th></th><th>선택 경로</th><th>전체 (중복 제거)</th></tr></thead>`
    + `<tbody>${rows}</tbody></table>`
    + `<details class="fold"${stopsOpen ? " open" : ""}><summary>승·하차 정류소 (선택 경로)</summary>`
    + `<ol class="stops">${items || '<li class="muted">매칭 대상 구간이 없습니다.</li>'}</ol></details>`;
  el.querySelector("details.fold").addEventListener("toggle", (e) => { stopsOpen = e.currentTarget.open; });
}
