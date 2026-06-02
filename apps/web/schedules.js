/* ── SCHEDULES TAB ───────────────────────────────────────────── */

function renderSchedules() {
  const dict = t();
  const schedules = state.schedules || { tasks: [] };
  const container = document.getElementById("schedules-form");
  if (!container) return;
  container.innerHTML = schedules.tasks.map((entry, i) => `<div class="schedule-entry" data-idx="${i}">
      <h3>${escapeHtml(entry.type || "")}</h3>
      <label>${dict.labels.scheduleEnabled} <input type="checkbox" class="sched-enabled" ${entry.enabled !== false ? "checked" : ""} /></label>
      <span class="schedule-strategy">${dict.labels.scheduleStrategy || "策略"}: ${escapeHtml(entry.strategy?.use || "incremental")}</span>
      <label>${dict.labels.scheduleStartAt} <input type="text" class="sched-start" value="${escapeHtml(entry.start_at || "")}" placeholder="HH:MM" /></label>
      <label>${dict.labels.scheduleInterval} <input type="text" class="sched-interval" value="${escapeHtml(entry.interval || "")}" placeholder="6h / 1d" /></label>
    </div>`).join("");
  container.insertAdjacentHTML("beforeend", `<p class="hint" style="margin-top:1rem">${dict.labels.schedulesTip}</p>`);
  container.insertAdjacentHTML("beforeend", `<button id="btn-save-schedules" class="btn btn-primary" type="button" style="margin-top:0.5rem">${dict.labels.scheduleSave || "保存调度"}</button>`);

  document.getElementById("btn-save-schedules")?.addEventListener("click", saveSchedules);
}

async function saveSchedules() {
  const dict = t();
  const result = document.getElementById("schedules-result");
  result.classList.remove("err");
  try {
    const entries = Array.from(document.querySelectorAll(".schedule-entry"));
    const originals = state.schedules?.tasks || [];
    const tasks = entries.map((el, i) => {
      const orig = originals[i] || {};
      const strat = orig.strategy || { use: "incremental" };
      return {
        type: el.querySelector("h3").textContent,
        enabled: el.querySelector(".sched-enabled").checked,
        strategy: strat,
        tag_filter: orig.tag_filter || null,
        start_at: el.querySelector(".sched-start").value || null,
        interval: el.querySelector(".sched-interval").value || null,
      };
    });
    state.schedules = await api("/schedules", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tasks }),
    });
    result.textContent = "Schedules saved.";
    renderSchedules();
  } catch (err) {
    result.classList.add("err");
    result.textContent = `Save failed: ${err.message}`;
  }
}