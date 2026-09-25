// 지도 이동과 핀치 확대는 취소하고, 같은 지점을 550ms 누를 때만 선택한다.
export function initMapLongPress(element, onPress) {
  let press = null;
  let timer = null;
  let suppressClick = false;
  function cancel() {
    clearTimeout(timer);
    timer = null;
    press = null;
  }
  element.addEventListener("pointerdown", (event) => {
    cancel();
    suppressClick = false;
    if (!event.isPrimary || event.button !== 0 || event.target.closest(".leaflet-control, .leaflet-popup, .leaflet-marker-icon, .leaflet-interactive")) return;
    press = { id: event.pointerId, x: event.clientX, y: event.clientY };
    timer = setTimeout(() => {
      cancel();
      suppressClick = true;
      onPress(event);
    }, 550);
  });
  element.addEventListener("pointermove", (event) => {
    if (press && event.pointerId === press.id && Math.hypot(event.clientX - press.x, event.clientY - press.y) > 10) cancel();
  });
  for (const type of ["pointerup", "pointercancel", "pointerleave", "lostpointercapture"]) element.addEventListener(type, cancel);
  // 손을 뗀 뒤 생성되는 click이 Leaflet의 팝업 닫기로 전달되지 않게 한다.
  element.addEventListener("click", (event) => {
    if (!suppressClick) return;
    suppressClick = false;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
  element.addEventListener("contextmenu", (event) => {
    if (!event.target.closest(".leaflet-control, .leaflet-popup")) event.preventDefault();
  });
  return cancel;
}
