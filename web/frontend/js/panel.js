// 가장자리 핸들: 클릭/키보드로 토글, 안쪽 드래그로 열고 바깥쪽으로 닫는다.
export function initPanelHandle(handle, onLayout = () => {}) {
  const mobile = matchMedia("(max-width: 720px)");
  let phase = "setup";
  let size = "closed";
  let drag = null;
  let suppressClick = false;
  let transition = null;
  let desiredOpen = !document.body.classList.contains("panel-collapsed");
  let afterLayout = null;
  let snapFrame = null;
  function stopSnap() {
    if (snapFrame !== null) cancelAnimationFrame(snapFrame);
    snapFrame = null;
  }
  function snapHeight(from, to) {
    if (from === to || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const start = performance.now();
    const paint = (height) => {
      document.body.style.setProperty("--panel-drag-height", `${height}px`);
      document.body.classList.add("panel-dragging");
      onLayout();
    };
    paint(from);
    const tick = (now) => {
      const progress = Math.min(1, (now - start) / 220);
      if (progress < 1) {
        paint(from + (to - from) * (1 - (1 - progress) ** 3));
        snapFrame = requestAnimationFrame(tick);
      } else {
        snapFrame = null;
        clearDragSize();
        onLayout();
      }
    };
    snapFrame = requestAnimationFrame(tick);
  }
  function clearDragSize() {
    document.body.classList.remove("panel-dragging");
    document.body.style.removeProperty("--panel-drag-height");
  }
  function render(animate = true) {
    stopSnap();
    const update = () => {
      clearDragSize();
      const open = mobile.matches ? size !== "closed" : desiredOpen;
      document.body.dataset.panelPhase = phase;
      document.body.dataset.panelSize = size;
      document.body.classList.toggle("panel-collapsed", !open);
      handle.hidden = false;
      handle.setAttribute("aria-expanded", String(open));
      handle.setAttribute("aria-label", mobile.matches && open
        ? "패널 접기 · 위아래로 드래그하여 크기 변경"
        : `검색·결과 패널 ${open ? "닫기" : "열기"}`);
      onLayout();
      const callback = afterLayout;
      afterLayout = null;
      callback?.();
    };
    transition?.skipTransition();
    if (!animate || !document.startViewTransition || matchMedia("(prefers-reduced-motion: reduce)").matches) {
      update();
      return;
    }
    // DOM을 복제하지 않고 전환 전후 화면을 연결해 검색값·스크롤·지도 상태를 유지한다.
    transition = document.startViewTransition(update);
    transition.ready.catch(() => {}); // 연속 조작으로 생략한 전환은 정상적인 취소다.
  }
  function resize(direction) {
    if (!mobile.matches) desiredOpen = direction > 0;
    else {
      const stages = phase === "setup" ? ["closed", "compact", "half"] : ["closed", "compact", "half", "full"];
      const index = stages.indexOf(size) + (direction > 0 ? 1 : -1);
      size = stages[Math.max(0, Math.min(stages.length - 1, index))];
    }
    render(!mobile.matches);
  }
  handle.addEventListener("click", (event) => {
    if (suppressClick && event.detail !== 0) {
      suppressClick = false;
      return;
    }
    if (!mobile.matches) desiredOpen = !desiredOpen;
    else size = size === "closed" ? (phase === "setup" ? "compact" : "half") : "closed";
    render();
  });
  handle.addEventListener("pointerdown", (event) => {
    if (!event.isPrimary || event.button !== 0) return;
    stopSnap();
    suppressClick = false;
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY,
      mobile: mobile.matches, height: window.innerHeight - handle.getBoundingClientRect().bottom };
    handle.setPointerCapture(event.pointerId);
    handle.classList.add("dragging");
  });
  handle.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.id || !drag.mobile) return;
    const dy = drag.y - event.clientY;
    if (Math.abs(dy) < 8 && !drag.moved) return;
    drag.moved = true;
    transition?.skipTransition();
    const panelTop = document.getElementById("floating-search").getBoundingClientRect().bottom + 48;
    const maximum = phase === "setup" ? window.innerHeight / 2 : Math.max(0, document.body.clientHeight - panelTop);
    const height = Math.max(0, Math.min(maximum, drag.height + dy));
    document.body.style.setProperty("--panel-drag-height", `${height}px`);
    document.body.classList.add("panel-dragging");
    onLayout();
  });
  handle.addEventListener("pointerup", (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    const distance = drag.mobile ? -dy : dx;
    const sidebar = document.getElementById("sidebar");
    const from = sidebar.getBoundingClientRect().height;
    const settle = drag.mobile && drag.moved;
    suppressClick = Math.hypot(dx, dy) > 8;
    if (Math.abs(distance) >= 24) resize(distance);
    else if (drag.moved) render(false);
    if (settle) snapHeight(from, sidebar.getBoundingClientRect().height);
    drag = null;
    handle.classList.remove("dragging");
    handle.releasePointerCapture(event.pointerId);
  });
  for (const type of ["pointercancel", "lostpointercapture"]) {
    handle.addEventListener(type, () => {
      if (drag) { clearDragSize(); onLayout(); }
      drag = null;
      handle.classList.remove("dragging");
    });
  }
  mobile.addEventListener("change", () => {
    size = phase === "setup" ? (size === "closed" ? "closed" : "compact") : phase === "results" ? "full" : "half";
    render(false);
  });
  render(false);
  return {
    reset({ pointChanged = false } = {}) {
      const reveal = pointChanged && (mobile.matches ? size === "closed" : !desiredOpen);
      if (reveal) {
        desiredOpen = true;
      }
      if (reveal || (phase !== "setup" && size !== "closed")) size = "compact";
      phase = "setup";
      afterLayout = null;
      render(reveal);
    },
    showResults() {
      phase = "results";
      size = "full";
      afterLayout = null;
      render();
    },
    showSelection(draw) {
      if (phase === "selected") {
        if (afterLayout) afterLayout = draw;
        else draw?.();
        return;
      }
      phase = "selected";
      size = "half";
      afterLayout = draw;
      render();
    },
  };
}
