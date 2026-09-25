// 위치 추적은 브라우저에서만 수행한다. 경로 검색/역지오코딩을 자동 호출하지 않는다.
export function initLocation(map, onPick) {
  let watchId = null;
  let generation = 0;
  let marker = null;
  let accuracy = null;
  let button;
  let status;
  const Control = L.Control.extend({
    onAdd() {
      const box = L.DomUtil.create("div", "location-control");
      button = L.DomUtil.create("button", "location-btn", box);
      button.type = "button";
      button.id = "gps-btn";
      status = L.DomUtil.create("div", "location-status", box);
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      L.DomEvent.disableClickPropagation(box);
      L.DomEvent.disableScrollPropagation(box);
      button.addEventListener("click", () => watchId === null ? start() : showLocation());
      return box;
    },
  });
  new Control({ position: "topleft" }).addTo(map);

  function setStatus(active, message = "") {
    button.textContent = "GPS";
    button.setAttribute("aria-pressed", String(active));
    button.setAttribute("aria-label", active ? "현재 위치로 이동하고 출발·도착 선택" : "현재 위치 추적 시작");
    status.textContent = message;
    status.hidden = !message;
  }

  function showLocation() {
    if (!marker) return; // 첫 위치를 기다리는 동안 다시 눌러도 추적을 중복 등록하지 않는다.
    const latlng = marker.getLatLng();
    map.setView(latlng, Math.max(map.getZoom(), 16));
    onPick(latlng);
  }

  function stop(message = "위치 추적을 중지했습니다.") {
    generation++;
    if (watchId !== null) navigator.geolocation.clearWatch(watchId);
    watchId = null;
    marker?.remove();
    accuracy?.remove();
    marker = accuracy = null;
    setStatus(false, message);
  }

  function start() {
    if (!window.isSecureContext) {
      setStatus(false, "현재 위치는 HTTPS 연결에서 사용할 수 있습니다.");
      return;
    }
    if (!navigator.geolocation) {
      setStatus(false, "이 브라우저는 현재 위치를 지원하지 않습니다.");
      return;
    }
    const current = ++generation;
    setStatus(true, "위치 확인 중… 위치 권한을 허용해 주세요.");
    watchId = navigator.geolocation.watchPosition(({ coords }) => {
      if (current !== generation) return;
      const latlng = L.latLng(coords.latitude, coords.longitude);
      const first = !marker;
      if (first) {
        accuracy = L.circle(latlng, {
          radius: coords.accuracy, color: "#1976D2", weight: 1,
          fillOpacity: 0.1, interactive: false,
        }).addTo(map);
        marker = L.marker(latlng, {
          icon: L.divIcon({ className: "location-marker", iconSize: [44, 44], iconAnchor: [22, 22] }),
          title: "현재 위치", alt: "현재 위치", zIndexOffset: 1000,
        }).addTo(map);
        marker.on("click", () => onPick(marker.getLatLng()));
      } else {
        marker.setLatLng(latlng);
        accuracy.setLatLng(latlng).setRadius(coords.accuracy);
      }
      setStatus(true);
      // 이후에는 지도를 강제로 이동하거나 열어 둔 메뉴를 다시 열지 않는다.
      if (first) showLocation();
    }, (error) => {
      if (current !== generation) return;
      const messages = {
        1: "위치 권한이 거부되었습니다. 브라우저 설정에서 위치를 허용한 뒤 GPS를 눌러 주세요.",
        2: "위치를 확인할 수 없습니다. 기기의 위치 설정을 확인한 뒤 다시 시도해 주세요.",
        3: "위치 확인 시간이 초과되었습니다. GPS를 눌러 다시 시도해 주세요.",
      };
      stop(messages[error.code] || "위치를 확인하지 못했습니다. 다시 시도해 주세요.");
    }, { enableHighAccuracy: true, maximumAge: 5000, timeout: 20000 });
  }

  // 백그라운드에서 배터리를 사용하지 않는다. 돌아오면 버튼으로 재시작한다.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && watchId !== null) stop("");
  });
  window.addEventListener("pagehide", () => stop());
  map.on("unload", () => stop());
  setStatus(false);
}
