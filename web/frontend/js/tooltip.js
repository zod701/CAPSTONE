// 툴팁 — 모든 title 을 브라우저 기본 툴팁 대신 같은 모양의 말풍선으로 띄운다(문서 전체에 한 번 건다).
// 마우스가 올라가거나 키보드 포커스가 들어온 동안만 title 을 data-tip 으로 옮겨 기본 툴팁을 막고, 떠나면 되돌린다 —
// 화면 낭독기와 다른 코드는 title 을 그대로 읽는다. 여러 줄(\n)은 줄을 바꿔 보인다(CSS white-space: pre-line).
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

  document.addEventListener("mouseover", (e) => enter(e.target.closest?.("[title]")));
  document.addEventListener("mouseout", (e) => {
    if (target && !target.contains(e.relatedTarget)) leave();
  });
  document.addEventListener("focusin", (e) => enter(e.target.closest?.("[title]"), 0));
  document.addEventListener("focusout", () => leave());
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") leave();
  });
  // 스크롤·크기 변경·클릭이면 말풍선이 요소에서 떨어진다 — 닫는다
  for (const ev of ["scroll", "resize", "pointerdown"]) window.addEventListener(ev, () => leave(), true);
}

function enter(el, delay = DELAY_MS) {
  if (!el || el === target) return;
  const msg = el.getAttribute("title");
  if (!msg) return;
  leave();
  target = el;
  el.dataset.tip = msg;
  el.removeAttribute("title"); // 기본 툴팁이 겹쳐 뜨지 않게
  timer = setTimeout(() => show(el, msg), delay);
}

function leave() {
  clearTimeout(timer);
  timer = null;
  if (target) {
    // 그사이 코드가 title 을 새로 달았으면 그 값을 둔다
    if (!target.hasAttribute("title") && target.dataset.tip != null) target.setAttribute("title", target.dataset.tip);
    delete target.dataset.tip;
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
