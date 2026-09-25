// 툴팁 — 모든 title 을 브라우저 기본 툴팁 대신 같은 모양의 말풍선으로 띄운다(문서 전체에 한 번 건다).
// 동적으로 추가·변경되는 title 도 data-tip 으로 옮기고 복원하지 않아 기본 툴팁과 겹치지 않게 한다.
const DELAY_MS = 250; // 스쳐 지나가는 마우스에는 띄우지 않는다
const GAP = 8;        // 요소와 말풍선 사이
const EDGE = 8;       // 화면 가장자리 여백
const ARROW_IN = 16;  // 말풍선 왼쪽 끝에서 화살표까지 (첨부 모양 — 화살표가 왼쪽에 붙는다)

let tip = null;
let text = null;
let target = null;
let timer = null;

export function initTooltips() {
  tip = document.createElement("div");
  tip.className = "tip";
  tip.setAttribute("role", "tooltip");
  tip.hidden = true;
  tip.innerHTML = '<span class="tip-text"></span><span class="tip-arrow"></span>';
  text = tip.querySelector(".tip-text");
  document.body.append(tip);

  const migrate = el => {
    if (el.hasAttribute?.("title")) {
      el.dataset.tip = el.getAttribute("title");
      el.removeAttribute("title");
    }
    el.querySelectorAll?.("[title]").forEach(migrate);
  };
  migrate(document.body);
  new MutationObserver(records => {
    for (const record of records) {
      if (record.type === "childList") record.addedNodes.forEach(migrate);
      else if (record.attributeName === "title") migrate(record.target);
    }
    if (target) {
      if (!target.isConnected || !target.dataset.tip) leave();
      else if (!tip.hidden && text.textContent !== target.dataset.tip) show(target, target.dataset.tip);
    }
  }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["title", "data-tip"] });

  document.addEventListener("mouseover", (e) => enter(e.target.closest?.("[data-tip], [title]")));
  document.addEventListener("mouseout", (e) => {
    if (target && !target.contains(e.relatedTarget)) leave();
  });
  document.addEventListener("focusin", (e) => enter(e.target.closest?.("[data-tip], [title]"), 0));
  document.addEventListener("focusout", () => leave());
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") leave();
  });
  // 스크롤·크기 변경·클릭이면 말풍선이 요소에서 떨어진다 — 닫는다
  for (const ev of ["scroll", "resize", "pointerdown"]) window.addEventListener(ev, () => leave(), true);
}

function enter(el, delay = DELAY_MS) {
  if (!el || el === target) return;
  const msg = el.getAttribute("title") ?? el.dataset.tip;
  if (!msg) return;
  leave();
  target = el;
  el.dataset.tip = msg;
  el.removeAttribute("title"); // 기본 툴팁이 겹쳐 뜨지 않게
  timer = setTimeout(() => { if (el.dataset.tip) show(el, el.dataset.tip); else leave(); }, delay);
}

function leave() {
  clearTimeout(timer);
  timer = null;
  if (target) {
    target = null;
  }
  if (tip) tip.hidden = true;
}

function show(el, msg) {
  if (el !== target || !el.isConnected) return leave();
  text.textContent = msg;
  tip.hidden = false;
  tip.classList.remove("above");
  const r = el.getBoundingClientRect();
  const w = tip.offsetWidth;
  const h = tip.offsetHeight;
  const cx = r.left + r.width / 2;
  const x = Math.max(EDGE, Math.min(cx - ARROW_IN, innerWidth - EDGE - w));
  let y = r.bottom + GAP;
  if (y + h > innerHeight - EDGE && r.top - GAP - h >= EDGE) { // 아래가 모자라면 위로
    y = r.top - GAP - h;
    tip.classList.add("above");
  }
  tip.style.left = `${Math.round(x)}px`;
  tip.style.top = `${Math.round(y)}px`;
  tip.style.setProperty("--ax", `${Math.round(Math.max(10, Math.min(cx - x, w - 10)))}px`); // 화살표가 요소 가운데를 가리킨다
}
