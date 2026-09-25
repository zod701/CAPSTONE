const KEY = "baroga-search-history";
const ENABLED_KEY = "baroga-search-history-enabled";
export function isSearchHistoryEnabled() {
  try { return localStorage.getItem(ENABLED_KEY) !== "false"; } catch { return true; }
}
export function setSearchHistoryEnabled(enabled) {
  try { localStorage.setItem(ENABLED_KEY, String(enabled)); } catch { /* 저장소 차단 */ }
}
export function readSearchHistory() {
  try {
    const values = JSON.parse(localStorage.getItem(KEY) || "[]");
    return Array.isArray(values) ? [...new Set(values.filter(q => typeof q === "string" && q.trim() && q.length <= 100))].slice(0, 10) : [];
  } catch { return []; }
}
export function rememberSearch(query) {
  if (!isSearchHistoryEnabled()) return;
  const q = query.trim();
  if (!q || q.length > 100) return;
  try { localStorage.setItem(KEY, JSON.stringify([q, ...readSearchHistory().filter(old => old !== q)].slice(0, 10))); }
  catch { /* 저장소가 차단되어도 검색은 계속한다. */ }
}
export function clearSearchHistory() {
  try { localStorage.removeItem(KEY); } catch { /* 저장소 차단 */ }
}
