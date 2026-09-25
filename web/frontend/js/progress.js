// GPS와 이미 받은 경로만 비교한다. 네트워크 요청은 하지 않는다.
export function remainingSeconds(route, progress = null) {
  const steps = route.steps || [];
  if (!steps.length) return null;
  let seconds = 0;
  for (let i = progress?.index ?? 0; i < steps.length; i++) {
    const step = steps[i];
    const duration = step.type === 'TAXI' ? step.counted_s ?? step.time_s : step.time_s;
    if (!Number.isFinite(duration) || duration < 0) return null;
    const fraction = progress && i === progress.index ? Math.max(0, Math.min(1, progress.fraction)) : 0;
    seconds += duration * (1 - fraction);
    if (['BUS', 'SUBWAY'].includes(step.type) && fraction === 0) {
      if (!Number.isFinite(step.wait_s) || step.wait_s < 0) return null;
      seconds += step.wait_s;
    }
  }
  return seconds;
}

export function stopProgress(step, fraction) {
  const count = step.stops?.length || 0;
  if (count < 2) return 0;
  const positions = (step.stop_locs || []).map(loc => loc && locateProgress([step], { lat: loc.lat, lng: loc.lon, accuracy: 100 })?.fraction);
  if (positions.length === count && positions.every((v, i) => Number.isFinite(v) && (i === 0 || v > positions[i - 1]))) {
    if (fraction <= positions[0]) return 0;
    for (let i = 1; i < count; i++) if (fraction <= positions[i]) return i - 1 + (fraction - positions[i - 1]) / (positions[i] - positions[i - 1]);
    return count - 1;
  }
  return Math.max(0, Math.min(1, fraction)) * (count - 1);
}

export function locateProgress(steps, fix) {
  if (!fix || !Number.isFinite(fix.accuracy) || fix.accuracy > 100) return null;
  const scaleX = 111320 * Math.cos(fix.lat * Math.PI / 180);
  const candidates = [];
  for (const [index, step] of steps.entries()) {
    const path = step.path || [];
    let length = 0, nearest = null;
    for (let i = 1; i < path.length; i++) {
      const a = path[i - 1], b = path[i];
      const ax = (a[0] - fix.lng) * scaleX, ay = (a[1] - fix.lat) * 111320;
      const dx = (b[0] - a[0]) * scaleX, dy = (b[1] - a[1]) * 111320;
      const squared = dx * dx + dy * dy;
      if (!squared) continue;
      const segment = Math.sqrt(squared);
      const t = Math.max(0, Math.min(1, -(ax * dx + ay * dy) / squared));
      const distance = Math.hypot(ax + t * dx, ay + t * dy);
      if (!nearest || distance < nearest.distance) nearest = { distance, along: length + t * segment };
      length += segment;
    }
    if (nearest && length) candidates.push({ index, fraction: nearest.along / length, distance: nearest.distance });
  }
  candidates.sort((a, b) => a.distance - b.distance);
  const best = candidates[0];
  if (!best || best.distance > Math.max(30, fix.accuracy)) return null;
  // 서로 떨어진 구간이 겹치는 곳에서는 진행 위치를 단정하지 않는다.
  if (candidates.some(other => Math.abs(other.index - best.index) > 1 && other.distance - best.distance < 15)) return null;
  return best;
}
