/* ── LOGS TAB ────────────────────────────────────────────────── */

function renderLogs() {
  const dict = t();
  const LEVELS = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3 };
  const minLevel = LEVELS[(document.getElementById("log-level-filter")?.value || "").toUpperCase()] ?? -1;
  const container = document.getElementById("log-entries");
  const entries = state.logs || [];
  const filtered = minLevel >= 0 ? entries.filter((e) => (LEVELS[(e.level || "").toUpperCase()] ?? 0) >= minLevel) : entries;
  container.innerHTML = filtered.length
    ? filtered.map((e) => `<div class="log-entry log-${escapeHtml((e.level || "info").toLowerCase())}">
        <span class="log-time">${toLocalTime(e.timestamp)}</span>
        <span class="log-level">${escapeHtml((e.level || "").toUpperCase())}</span>
        <span class="log-msg">${escapeHtml((e.message || e.msg || ""))}</span>
      </div>`).join("\n")
    : `<div class="hint">${dict.labels.logNoEntries}</div>`;
  container.scrollTop = container.scrollHeight;
}

function connectLogWebSocket() {
  // ── wire up filter dropdown change ─────
  const filterEl = document.getElementById("log-level-filter");
  if (filterEl) {
    filterEl.addEventListener("change", renderLogs);
  }

  if (state.logWs && state.logWs.readyState === WebSocket.OPEN) return;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${protocol}//${location.host}/ws/logs`;
  state.logWs = new WebSocket(url);
  state.logWs.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      if (Array.isArray(data)) { state.logs = data; } else { state.logs.push(data); }
      renderLogs();
    } catch { /* ignore */ }
  };
  state.logWs.onclose = () => {
    state.logWs = null;
    state.logReconnectTimer = setTimeout(connectLogWebSocket, 3000);
  };
  state.logWs.onerror = () => { state.logWs.close(); };
  const statusEl = document.getElementById("log-live-status");
  if (statusEl) statusEl.textContent = t().labels.logConnected;
}

function disconnectLogWebSocket() {
  if (state.logWs) {
    state.logWs.onclose = null;
    state.logWs.close();
    state.logWs = null;
  }
  if (state.logReconnectTimer) {
    clearTimeout(state.logReconnectTimer);
    state.logReconnectTimer = null;
  }
  const statusEl = document.getElementById("log-live-status");
  if (statusEl) statusEl.textContent = t().labels.logDisconnected;
}