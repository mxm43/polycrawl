/* ── UTILITIES ────────────────────────────────────────────────── */

/** Generic fetch wrapper with JSON parsing and error handling. */
async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

/** Get current language dictionary. */
function t() {
  return TRANSLATIONS[state.lang] || TRANSLATIONS.zh;
}

/** Sanitize HTML special characters. */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** Validate and return safe external URLs (http/https only). */
function safeExternalUrl(raw) {
  try {
    const parsed = new URL(raw);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") return parsed.toString();
  } catch { /* fall through */ }
  return "#";
}

/** Format ISO timestamp to localized string. */
function toLocalTime(isoTime) {
  if (!isoTime) return "-";
  const date = new Date(isoTime);
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString();
}

/** Format bytes to human-readable (B/KB/MB/GB/TB). */
function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let idx = 0;
  let size = bytes;
  while (size >= 1024 && idx < units.length - 1) { size /= 1024; idx += 1; }
  return `${size.toFixed(size >= 10 || idx === 0 ? 0 : 1)} ${units[idx]}`;
}

/** Map status value to CSS class: ok / warn / err. */
function statusClass(value) {
  const text = String(value || "").toLowerCase();
  if (text === "ok" || text === "success" || text === "running" || text === "recording") return "ok";
  if (text === "queued" || text === "retrying" || text === "degraded" || text === "probing") return "warn";
  return "err";
}

/** Localized status display text. */
function statusText(value) {
  const key = String(value || "unknown").toLowerCase();
  return t().statuses[key] || String(value || t().statuses.unknown);
}

/** Template string substitution: {key} → value. */
function fillPlaceholders(template, data) {
  return template.replace(/\{(\w+)\}/g, (_, key) => data[key] ?? "");
}

/** Sort helper: newest-first by ISO date. */
function compareDateDesc(a, b) {
  const ta = a ? new Date(a).getTime() : 0;
  const tb = b ? new Date(b).getTime() : 0;
  return tb - ta;
}

/** Generic stat card renderer. */
function renderStatCards(container, stats) {
  container.innerHTML = "";
  for (const item of stats) {
    const card = document.createElement("div");
    card.className = "stat-card";
    card.innerHTML = `<div class="stat-label">${item.label}</div><div class="stat-value">${item.value}</div>`;
    container.appendChild(card);
  }
}

/** Format account link display label. */
function formatLinkLabel(link) {
  return link.account_alias || link.account_url || "-";
}
