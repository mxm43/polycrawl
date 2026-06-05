/* ── OVERVIEW / HEALTH ────────────────────────────────────────── */

async function renderCookieStatus() {
  const dict = t();
  const el = document.getElementById("cookie-status");
  if (!el) return;
  try {
    const statuses = await api("/login/status");
    const warnings = statuses.filter((s) => s.has_cookies && s.expired);
    const criticals = statuses.filter((s) => s.has_cookies && s.critical);
    const missing = statuses.filter((s) => !s.has_cookies);
    const verifyFailed = statuses.filter((s) => s.verified_ok === false);

    if (criticals.length > 0) {
      el.innerHTML = `<div class="health-card err" style="margin-bottom:0.75rem">
        <div style="display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap">
          <span class="status-dot err"></span>
          <strong>🚨 Cookies 过期警告</strong>
          ${criticals.map((s) => `<span class="hint" style="color:var(--danger)">${s.platform}</span>`).join("")}
          <a class="btn btn-ghost btn-sm" href="#" onclick="activateTab('login');return false">前往设置</a>
        </div>
      </div>`;
    } else if (warnings.length > 0 || missing.length > 0 || verifyFailed.length > 0) {
      el.innerHTML = `<div class="health-card warn" style="margin-bottom:0.75rem">
        <div style="display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap">
          <span class="status-dot warn"></span>
          <strong>⚠ Cookies 提醒</strong>
          ${warnings.map((s) => `<span class="hint">${s.platform} 即将过期</span>`).join("")}
          ${missing.map((s) => `<span class="hint">${s.platform} 未设置</span>`).join("")}
          ${verifyFailed.map((s) => `<span class="hint" style="color:var(--danger)">${s.platform} 验证失败</span>`).join("")}
          <a class="btn btn-ghost btn-sm" href="#" onclick="activateTab('login');return false">前往设置</a>
        </div>
      </div>`;
    } else {
      el.innerHTML = statuses.length > 0
        ? `<div class="health-card ok" style="margin-bottom:0.75rem">
            <div style="display:flex;align-items:center;gap:0.5rem">
              <span class="status-dot ok"></span>
              <span>Cookies 状态正常 (${statuses.length} 平台)</span>
            </div>
          </div>`
        : "";
    }
  } catch {
    el.innerHTML = "";
  }
}

function renderHealth() {
  const dict = t();
  const health = state.health;
  const grid = document.getElementById("health-grid");
  grid.innerHTML = health
    ? `<div class="health-card ${statusClass(health.status)}">
        <div class="health-status"><span class="status-dot ${statusClass(health.status)}"></span>${statusText(health.status)}</div>
        ${["config", "database"].map((k) => `<div class="health-row"><span>${dict.healthLabels[k]}</span><span class="status-dot ${statusClass(health[k])}"></span>${statusText(health[k])}</div>`).join("")}
      </div>`
    : `<div class="hint">${dict.common.noData}</div>`;

  const stats = [];
  stats.push({ label: dict.stats.creatorsTotal, value: (state.creators || []).length });
  const tasks = state.tasks || [];
  stats.push({ label: dict.stats.tasksTotal, value: tasks.length });
  stats.push({ label: dict.stats.tasksRunning, value: tasks.filter((t) => t.status === "running").length });
  stats.push({ label: dict.stats.tasksFailed, value: tasks.filter((t) => t.status === "failed").length });
  const live = state.live || [];
  stats.push({ label: dict.stats.liveOnline, value: live.filter((l) => l.status === "online").length });
  stats.push({ label: dict.stats.liveRecording, value: live.filter((l) => l.status === "recording").length });
  renderStatCards(document.getElementById("overview-stats"), stats);

  // Fetch cookie status asynchronously
  renderCookieStatus();
  // Fetch recent failures asynchronously
  renderRecentFailures();
}

/** Show last N failed tasks with error messages in the overview. */
async function renderRecentFailures() {
  const el = document.getElementById("recent-failures");
  if (!el) return;
  const tasks = state.tasks || [];
  const failed = tasks.filter((t) => t.status === "failed" && t.error_message).slice(0, 10);
  if (failed.length === 0) {
    el.innerHTML = "";
    return;
  }
  const dict = t();
  el.innerHTML = `<div class="health-card err" style="margin-bottom:0.75rem">
    <div style="margin-bottom:0.5rem">
      <span class="status-dot err"></span>
      <strong>最近失败任务 (${failed.length})</strong>
      <a class="btn btn-ghost btn-sm" href="#" onclick="activateTab('tasks');return false" style="float:right">查看全部</a>
    </div>
    ${failed.map((t) => `<div class="failure-item">
      <div class="failure-header">
        <span class="hint">${escapeHtml(t.task_type || "")} / ${escapeHtml(t.account_label || t.account_id || "")}</span>
        <span class="hint">${toLocalTime(t.created_at)}</span>
      </div>
      <div class="failure-msg">${escapeHtml(t.error_message)}</div>
    </div>`).join("")}
  </div>`;
}

function renderAccountsForTaskSelect() {
  const dict = t();
  const sel = document.getElementById("account-id");
  const accs = state.accounts || [];
  sel.innerHTML = accs.length
    ? `<option value="">-- ${dict.labels.account} --</option>` + accs.map((a) => `<option value="${a.id}">${escapeHtml(a.creator_display_name || a.account_alias || "?")} / ${escapeHtml(a.platform)} / ${escapeHtml(a.account_type)}</option>`).join("")
    : `<option value="">${dict.placeholders.noAccounts}</option>`;
}