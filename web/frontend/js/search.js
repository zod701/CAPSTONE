// 주소·장소 검색창 (사이드바에 하나). 입력하면 잠시 뒤 /api/search → 칸 아래 목록, 고르면 onPick(item) —
// 출발/도착은 부르는 쪽이 정한다. 결과는 목록을 그리는 동안만 메모리에 둔다 — 캐시·저장하지 않는다(카카오 로컬 약관: 짧은 캐시도 금지).
import { getJSON, esc } from "./api.js";
import { readSearchHistory, rememberSearch, clearSearchHistory, isSearchHistoryEnabled, setSearchHistoryEnabled } from "./search-history.js";

const DEBOUNCE_MS = 300;
const MIN_AUTO = 2; // 이 글자 수부터 입력하는 동안 자동으로 찾는다 (Enter 는 1글자도)

// onQuota(quota) — 검색 응답의 쿼터. 검색이 실패하면 응답에 쿼터가 없어 null (다시 불러오라는 뜻)
export function createPlaceSearch(input, list, { onPick, onQuota = () => {} }) {
  let items = [];   // 지금 목록의 결과
  let shown = null; // 지금 목록이 어떤 검색어의 결과인지 (Enter 로 고를 수 있는지 판단)
  let active = -1;
  let timer = null;
  let ctrl = null;
  let pending = null; // 서버에 보내 답을 기다리는 검색어 — 같은 검색어는 끊고 다시 보내지 않는다(끊어도 서버는 카카오를 이미 불렀다)
  let seq = 0;      // 새 입력마다 증가 — 늦게 온 이전 검색어의 결과는 버린다
  let selectOnUp = false;
  let touchingList = false;

  // 목록 안 문구(role=presentation)는 화면 낭독기가 읽지 않아 목록 밖 status 영역에도 넣는다
  const status = document.createElement("p");
  status.className = "sr-only";
  status.setAttribute("role", "status");
  list.after(status);

  function cancel() {
    clearTimeout(timer);
    timer = null;
    ctrl?.abort();
    ctrl = null;
    pending = null;
    seq++;
  }

  function close() {
    cancel();
    items = [];
    shown = null;
    active = -1;
    list.hidden = true;
    list.replaceChildren();
    status.textContent = "";
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  // 목록을 연다: 결과 항목 HTML + 문구 [[글, 클래스], …]. 옛 항목을 가리키던 선택 표시는 지운다(결과가 있으면 render 가 다시 고른다)
  function open(itemsHTML, msgs = []) {
    list.innerHTML = itemsHTML
      + msgs.map(([text, cls = ""]) => `<li class="ac-msg ${cls}" role="presentation">${esc(text)}</li>`).join("");
    list.hidden = false;
    active = -1;
    input.setAttribute("aria-expanded", "true");
    input.removeAttribute("aria-activedescendant");
    status.textContent = msgs.map(([text]) => text).join(" ");
  }

  function setActive(i) {
    active = i;
    for (const li of list.querySelectorAll(".ac-item")) {
      const on = Number(li.dataset.i) === i;
      li.setAttribute("aria-selected", String(on));
      if (on) {
        input.setAttribute("aria-activedescendant", li.id);
        li.scrollIntoView({ block: "nearest" });
      }
    }
  }

  function showHistory() {
    if (input.value.trim()) return;
    close();
    const enabled = isSearchHistoryEnabled();
    items = enabled ? readSearchHistory().map(query => ({ historyQuery: query })) : [];
    shown = "";
    open(items.map((it, i) => `<li id="${list.id}-${i}" class="ac-item" role="option" aria-selected="false" data-i="${i}"><span class="ac-name">${esc(it.historyQuery)}</span></li>`).join("")
      + `<li class="ac-history-actions" role="presentation"><span class="ac-history-title">최근 검색어</span><button type="button" data-toggle-history>검색 기록 ${enabled ? "끄기" : "켜기"}</button><button type="button" data-clear-history>검색 기록 지우기</button></li>`,
    enabled && items.length ? [] : [[enabled ? "검색 기록이 없습니다" : "검색 기록 저장이 꺼져 있습니다"]]);
  }

  function render(q, data) {
    items = data.items || [];
    shown = q;
    const warns = (data.failed || []).map((f) => [f.message, "ac-warn"]); // 한쪽 검색만 실패
    if (!items.length) {
      open("", [["결과가 없습니다"], ...warns]);
      return;
    }
    open(items.map((it, i) =>
      `<li id="${list.id}-${i}" class="ac-item" role="option" aria-selected="false" data-i="${i}">`
      + `<span class="ac-name">${esc(it.name)}</span>`
      + (it.detail ? `<span class="ac-detail">${esc(it.detail)}</span>` : "")
      + (it.address ? `<span class="ac-addr">${esc(it.address)}</span>` : "")
      + "</li>").join(""), warns);
    setActive(0); // Enter 는 첫 결과
  }

  async function run(q) {
    cancel();
    items = [];
    shown = null;
    const my = seq;
    const c = (ctrl = new AbortController());
    pending = q;
    open("", [["찾는 중…"]]);
    try {
      const data = await getJSON("/api/search", { q }, { signal: c.signal });
      if (my !== seq) return;
      ctrl = null;
      pending = null;
      render(q, data);
      if (data.quota) onQuota(data.quota);
    } catch (err) {
      if (err.name === "AbortError" || my !== seq) return;
      ctrl = null;
      pending = null; // 실패한 검색어는 Enter 로 다시 보낼 수 있다
      items = [];
      shown = null;
      open("", [[err.message, "ac-err"], ...(err.action ? [[err.action, "ac-err"]] : [])]);
      onQuota(null);
    }
  }

  function pick(i) {
    const it = items[i];
    if (!it) return;
    if (it.historyQuery) {
      input.value = it.historyQuery;
      rememberSearch(it.historyQuery);
      run(it.historyQuery);
      return;
    }
    if (shown) rememberSearch(shown);
    input.value = it.name;
    close();
    onPick(it);
  }

  input.addEventListener("input", () => {
    const q = input.value.trim();
    if (q === pending) return; // 끝에 공백만 친 경우 등 — 도는 검색을 끊고 다시 보내지 않는다
    cancel();                  // 기다리던 다른 검색어는 멈춘다
    if (q === shown) return;   // 보이는 목록이 이미 이 검색어의 결과
    if (q.length < MIN_AUTO) {
      close();
      if (!q) showHistory();
      return;
    }
    timer = setTimeout(() => run(q), DEBOUNCE_MS);
  });

  input.addEventListener("keydown", (e) => {
    selectOnUp = false;
    if (e.isComposing || e.keyCode === 229) return; // 한글 조합 중의 Enter·화살표는 조합을 끝내는 키다
    const q = input.value.trim();
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!items.length) {
        if (q && shown !== q && pending !== q) run(q);
        return;
      }
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((active + step + items.length) % items.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (q) rememberSearch(q);
      if (items.length && shown === q) pick(active); // 목록이 지금 검색어의 결과일 때만 고른다
      else if (q && pending !== q) run(q);          // 같은 검색어가 도는 중이면 답을 기다린다
    } else if (e.key === "Escape") {
      if (!list.hidden) close();
      else cancel(); // 치고 바로 Esc — 기다리던 검색이 뒤늦게 목록을 다시 열지 않게
    }
  });

  // 목록을 누르는 동안 칸이 포커스를 잃지 않게 (잃으면 목록이 먼저 닫힌다)
  list.addEventListener("mousedown", (e) => e.preventDefault());
  document.addEventListener("pointerdown", (e) => {
    touchingList = list.contains(e.target);
    if (!touchingList && e.target !== input) close();
  }, true);
  list.addEventListener("focusout", (e) => {
    if (!list.contains(e.relatedTarget) && e.relatedTarget !== input) close();
  });
  input.addEventListener("keydown", (e) => { if (e.key === "Tab") touchingList = false; });
  list.addEventListener("click", (e) => {
    if (e.target.closest("[data-toggle-history]")) {
      setSearchHistoryEnabled(!isSearchHistoryEnabled());
      showHistory();
      return;
    }
    if (e.target.closest("[data-clear-history]")) {
      clearSearchHistory();
      showHistory();
      return;
    }
    const li = e.target.closest(".ac-item");
    if (li) pick(Number(li.dataset.i));
  });
  list.addEventListener("mousemove", (e) => {
    const li = e.target.closest(".ac-item");
    if (li && Number(li.dataset.i) !== active) setActive(Number(li.dataset.i));
  });

  // 들어오면 전체 선택 — 바로 치면 앞 검색어가 지워진다. 마우스로 들어온 경우 뒤따르는 mouseup 이 선택을 풀지 않게 한 번 막는다
  input.addEventListener("focus", () => {
    input.select();
    selectOnUp = true;
    showHistory();
  });
  input.addEventListener("click", () => { if (list.hidden) showHistory(); });
  input.addEventListener("mouseup", (e) => {
    if (selectOnUp) e.preventDefault();
    selectOnUp = false;
  });
  input.addEventListener("blur", (e) => {
    selectOnUp = false;
    if (!touchingList && !list.contains(e.relatedTarget)) close();
  });

  return { focus: () => input.focus() };
}
