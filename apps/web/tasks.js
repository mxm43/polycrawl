/* ── TASKS TAB ───────────────────────────────────────────────── */

function renderTasks() {
  const dict = t();
  const list = state.tasks || [];
  const tbody = document.getElementById("tasks-body");
  tbody.innerHTML = list.length
    ? list.slice(0, 100).map((task) => {
        const hasError = task.status === "failed" && task.error_message;
        return `<tr class="${hasError ? "task-row-error" : ""}" ${hasError ? `onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'table-row':'none'"` : ""}>
        <td class="cell-mono">${escapeHtml(task.id || "").slice(0, 8)}</td>
        <td>${escapeHtml(task.account_label || task.account_id || "")}</td>
        <td>${escapeHtml(task.task_type || "")}</td>
        <td><span class="status-badge ${statusClass(task.status)}">${statusText(task.status)}</span></td>
        <td>${task.retry_count ?? "-"}</td>
        <td>${toLocalTime(task.created_at)}</td>
      </tr>${hasError ? `<tr class="task-error-detail" style="display:none"><td colspan="6"><div class="task-error-msg"><strong>${dict.table.error}:</strong> ${escapeHtml(task.error_message)}</div></td></tr>` : ""}`;
      }).join("")
    : `<tr><td colspan="6" class="hint">${dict.common.noData}</td></tr>`;

  const stats = [];
  stats.push({ label: dict.stats.tasksTotal, value: list.length });
  stats.push({ label: dict.stats.tasksSuccess, value: list.filter((t) => t.status === "success").length });
  stats.push({ label: dict.stats.tasksFailed, value: list.filter((t) => t.status === "failed").length });
  stats.push({ label: dict.stats.tasksRunning, value: list.filter((t) => t.status === "running").length });
  renderStatCards(document.getElementById("task-stats"), stats);
}