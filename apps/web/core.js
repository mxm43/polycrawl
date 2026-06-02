/* ── GLOBAL STATE ─────────────────────────────────────────────── */
const state = {
  lang: localStorage.getItem("spider.ui.lang") || "zh",
  creators: [],
  accounts: [],
  tasks: [],
  live: [],
  schedules: [],
  logs: [],
  health: null,
  logWs: null,
  logReconnectTimer: null,
  eventWs: null,
  expandedCreators: new Set(),
  creatorTagFilter: null,
  creatorTagFilterDraft: null,
  creatorSort: localStorage.getItem("spider.ui.sort") || "default",
  selectedCreators: new Set(),
  batchMode: false,
  reorderMode: false,
  platforms: [],
};

/* ── LANGUAGE & TAB MANAGEMENT ─────────────────────────────────── */

function applyLanguage() {
  const dict = t();
  document.documentElement.lang = state.lang === "zh" ? "zh-CN" : "en";
  document.title = `Spider ${dict.title}`;

  const $ = (id) => document.getElementById(id);
  $("title-main").textContent = dict.title;
  $("subtitle-main").textContent = dict.subtitle;
  $("label-lang").textContent = dict.langLabel;

  $("tab-creators").textContent = dict.tabs.creators;
  $("tab-overview").textContent = dict.tabs.overview;
  $("tab-tasks").textContent = dict.tabs.tasks;
  $("tab-live").textContent = dict.tabs.live;
  $("tab-schedules").textContent = dict.tabs.schedules;
  $("tab-logs").textContent = dict.tabs.logs;
  $("tab-login").textContent = dict.tabs.login;
  $("tab-docs").textContent = dict.tabs.docs;

  $("heading-creators").textContent = dict.headings.creators;
  $("heading-health").textContent = dict.headings.health;
  $("heading-create").textContent = dict.headings.create;
  $("heading-tasks").textContent = dict.headings.tasks;
  $("heading-live").textContent = dict.headings.live;
  $("heading-docs").textContent = dict.headings.docs;

  $("label-account").textContent = dict.labels.account;
  $("label-task-type").textContent = dict.labels.taskType;
  $("label-max-retries").textContent = dict.labels.maxRetries;
  $("label-creator-tag-filter").textContent = dict.labels.creatorTagFilter;
  $("label-creator-sort").textContent = dict.labels.creatorSort;
  $("btn-submit-task").textContent = dict.labels.submitTask;

  const creatorSort = $("creator-sort");
  creatorSort.innerHTML = "";
  Object.entries(dict.sorts).forEach(([value, label]) => {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    creatorSort.appendChild(opt);
  });
  creatorSort.value = state.creatorSort;

  $("th-task-id").textContent = dict.table.taskId;
  $("th-account").textContent = dict.table.account;
  $("th-type").textContent = dict.table.type;
  $("th-status").textContent = dict.table.status;
  $("th-retry").textContent = dict.table.retry;
  $("th-created").textContent = dict.table.created;
  $("clear-logs").textContent = dict.actions.clearLogs;
  $("docs-note").textContent = dict.docsNote;
  $("docs-tip").textContent = dict.docsTip;
  $("link-swagger").textContent = dict.docsLinks.swagger;
  $("link-redoc").textContent = dict.docsLinks.redoc;
  $("link-openapi").textContent = dict.docsLinks.openapi;

  renderCreators();
  renderHealth();
  renderTasks();
  renderLive();
}

function activateTab(tabKey) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("is-active", t.dataset.tab === tabKey));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("is-active", p.dataset.panel === tabKey));

  if (tabKey === "logs") {
    connectLogWebSocket();
    api("/logs?limit=200").then((fresh) => { if (Array.isArray(fresh)) state.logs = fresh; renderLogs(); }).catch(() => null);
  } else {
    disconnectLogWebSocket();
  }

  if (tabKey === "login" && typeof renderLoginPage === "function") {
    renderLoginPage();
  }
}

/* ── EVENT LISTENERS ───────────────────────────────────────────── */

document.getElementById("tabs").addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (tab) activateTab(tab.dataset.tab);
});

document.getElementById("clear-logs").addEventListener("click", async () => {
  await api("/logs", { method: "DELETE" });
  state.logs = [];
  renderLogs();
});

document.getElementById("btn-batch-toggle")?.addEventListener("click", () => {
  state.batchMode = !state.batchMode;
  if (!state.batchMode) state.selectedCreators.clear();
  renderCreators();
});

document.getElementById("btn-reorder-toggle")?.addEventListener("click", () => {
  state.reorderMode = !state.reorderMode;
  if (!state.reorderMode) renderCreators();
});

document.getElementById("log-level-filter")?.addEventListener("change", renderLogs);

document.getElementById("task-form")?.addEventListener("submit", createTask);

document.getElementById("creator-sort")?.addEventListener("change", (e) => {
  state.creatorSort = e.target.value;
  localStorage.setItem("spider.ui.sort", state.creatorSort);
  renderCreators();
});

document.getElementById("lang-select")?.addEventListener("change", (e) => {
  state.lang = e.target.value;
  localStorage.setItem("spider.ui.lang", state.lang);
  applyLanguage();
});

/* ── WEBSOCKET (events) ────────────────────────────────────────── */

function connectEventWebSocket() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${location.host}/ws/events`;
  state.eventWs = new WebSocket(wsUrl);
  state.eventWs.onmessage = (event) => {
    try { handleServerEvent(JSON.parse(event.data)); } catch { /* ignore */ }
  };
  state.eventWs.onclose = () => { state.eventWsReconnectTimer = setTimeout(connectEventWebSocket, 5000); };
  state.eventWs.onerror = () => { state.eventWs.close(); };
}

function handleServerEvent(msg) {
  const type = msg.type;
  const data = msg.data || {};
  switch (type) {
    case "task_created":
    case "task_updated":
      Promise.all([loadTasks(), loadLive(), loadCreators()]).catch(() => null);
      break;
    case "creators_updated":
      Promise.all([loadCreators(), loadAccounts()]).catch(() => null);
      break;
  }
}

/* ── DATA LOADING ──────────────────────────────────────────────── */

async function loadCreators() {
  state.creators = await api("/creators/summary");
  renderCreators();
  renderHealth();
}

async function loadHealth() {
  state.health = await api("/health");
  renderHealth();
}

async function loadAccounts() {
  state.accounts = await api("/accounts");
  renderAccountsForTaskSelect();
}

async function loadTasks() {
  state.tasks = await api("/tasks");
  renderTasks();
  renderHealth();
}

async function loadLive() {
  state.live = await api("/live/status");
  renderLive();
  renderHealth();
}

async function loadSchedules() {
  state.schedules = await api("/schedules");
  renderSchedules();
}

async function loadPlatforms() {
  state.platforms = await api("/platforms");
}

/* ── TASK CREATION ─────────────────────────────────────────────── */

async function createTask(event) {
  event.preventDefault();
  const dict = t();
  const result = document.getElementById("task-result");
  result.classList.remove("err");

  const accountId = Number(document.getElementById("account-id").value);
  const taskType = document.getElementById("task-type").value;
  const maxRetries = Number(document.getElementById("max-retries").value);

  if (!accountId || !taskType) return;

  result.textContent = dict.notices.submitting;
  try {
    const data = await api("/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account_id: accountId, task_type: taskType, max_retries: maxRetries, params: {} }),
    });
    result.textContent = fillPlaceholders(dict.notices.created, { id: data.id, status: statusText(data.status) });
    Promise.all([loadTasks(), loadLive(), loadCreators()]).catch(() => null);
  } catch (err) {
    result.classList.add("err");
    result.textContent = fillPlaceholders(dict.notices.failed, { message: err.message });
  }
}

/* ── INIT ──────────────────────────────────────────────────────── */

(async () => {
  await loadPlatforms();
  applyLanguage();
  Promise.all([loadCreators(), loadHealth(), loadAccounts(), loadTasks(), loadLive(), loadSchedules()])
    .catch((err) => console.error("Init error:", err));
  connectEventWebSocket();
})();
