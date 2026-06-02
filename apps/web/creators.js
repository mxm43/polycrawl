/* ── TAG HELPERS ───────────────────────────────────────────────── */

function normalizeTag(input) {
  return String(input || "").trim().toLowerCase();
}

function getCreatorTags(creatorKey) {
  const creator = state.creators.find((c) => c.creator_key === creatorKey);
  return creator && Array.isArray(creator.tags) ? creator.tags : [];
}

async function _saveTags(creatorKey, tags) {
  try {
    const resp = await api(`/creators/${encodeURIComponent(creatorKey)}/tags`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tags }),
    });
    const creator = state.creators.find((c) => c.creator_key === creatorKey);
    if (creator) creator.tags = resp.tags;
  } catch { /* ignore */ }
  renderCreators();
}

function addTag(creatorKey, rawTag) {
  const tag = normalizeTag(rawTag);
  if (!creatorKey || !tag) return;
  const existing = getCreatorTags(creatorKey);
  if (existing.includes(tag)) return;
  _saveTags(creatorKey, [...existing, tag].sort());
}

function removeTag(creatorKey, tagToRemove) {
  if (!creatorKey) return;
  const next = getCreatorTags(creatorKey).filter((tag) => tag !== tagToRemove);
  _saveTags(creatorKey, next);
}

function creatorPlatformTags(creator) {
  return Array.from(new Set((creator.platforms || []).map((item) => normalizeTag(item)).filter(Boolean)));
}

function creatorAllTags(creator) {
  return Array.from(new Set([...creatorPlatformTags(creator), ...getCreatorTags(creator.creator_key)])).sort();
}

function creatorMatchesFilter(creator) {
  const sel = state.creatorTagFilter;
  if (sel === null) return true;
  if (sel.size === 0) return false;
  return creatorAllTags(creator).some((tag) => sel.has(tag));
}

function getAllFilterTags() {
  const all = new Set();
  for (const c of state.creators) for (const tag of creatorAllTags(c)) all.add(tag);
  return Array.from(all).sort();
}

function sortCreators(creators) {
  const key = state.creatorSort;
  if (key === "default") return [...creators];
  return [...creators].sort((a, b) => {
    if (key === "works_desc") return (b.works_count || 0) - (a.works_count || 0);
    if (key === "size_desc") return (b.total_bytes || 0) - (a.total_bytes || 0);
    if (key === "name_asc") return String(a.display_name || "").localeCompare(String(b.display_name || ""));
    return compareDateDesc(a.last_updated_at, b.last_updated_at);
  });
}

/* ── EXCEL-STYLE FILTER ────────────────────────────────────────── */

function closeExcelFilter() {
  document.getElementById("excel-filter-dropdown").classList.remove("is-open");
}

function renderExcelFilter() {
  const dict = t();
  const allTags = getAllFilterTags();
  const filterBtn = document.getElementById("excel-filter-btn");
  const filterLabel = document.getElementById("excel-filter-label");
  const dropdown = document.getElementById("excel-filter-dropdown");
  const body = document.getElementById("excel-filter-body");
  const searchInput = document.getElementById("excel-filter-search");

  const sel = state.creatorTagFilter;
  filterLabel.textContent = sel === null ? dict.labels.allTags : `${sel.size} 个已选`;
  filterBtn.classList.toggle("is-active", sel !== null);

  const searchText = searchInput.value.toLowerCase().trim();
  const allChecked = !sel || sel.size === allTags.length;

  let html = `<div class="excel-filter-item is-select-all">
    <input type="checkbox" class="excel-filter-select-all" id="excel-selall" ${allChecked ? "checked" : ""} />
    <label for="excel-selall">(Select All)</label>
  </div>`;

  for (const tag of allTags) {
    if (searchText && !tag.includes(searchText)) continue;
    const count = state.creators.filter((c) => creatorAllTags(c).includes(tag)).length;
    const checked = allChecked || (sel && sel.has(tag));
    const id = `excel-tag-${escapeHtml(tag).replace(/\s/g, "_")}`;
    html += `<div class="excel-filter-item">
      <input type="checkbox" class="excel-filter-tag" id="${id}" data-tag="${escapeHtml(tag)}" ${checked ? "checked" : ""} />
      <label for="${id}">${escapeHtml(tag)}</label>
      <span class="filter-tag-count">${count}</span>
    </div>`;
  }
  body.innerHTML = html;

  if (filterBtn._listenersAttached) return;
  filterBtn._listenersAttached = true;

  filterBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const isOpen = dropdown.classList.contains("is-open");
    if (!isOpen) {
      state.creatorTagFilterDraft = state.creatorTagFilter ? new Set(state.creatorTagFilter) : null;
      searchInput.value = "";
      renderExcelFilter();
    }
    dropdown.classList.toggle("is-open");
  });

  body.addEventListener("change", (e) => {
    const cb = e.target.closest("input[type='checkbox']");
    if (!cb) return;
    if (cb.classList.contains("excel-filter-select-all")) {
      body.querySelectorAll(".excel-filter-tag").forEach((c) => { c.checked = cb.checked; });
    } else {
      const all = body.querySelectorAll(".excel-filter-tag");
      const checked = body.querySelectorAll(".excel-filter-tag:checked");
      const selAll = body.querySelector(".excel-filter-select-all");
      if (selAll) selAll.checked = all.length > 0 && all.length === checked.length;
    }
    const checked = new Set();
    body.querySelectorAll(".excel-filter-tag:checked").forEach((c) => checked.add(c.dataset.tag));
    state.creatorTagFilter = checked.size === body.querySelectorAll(".excel-filter-tag").length ? null : checked;
    renderCreators();
  });

  searchInput.addEventListener("input", renderExcelFilter);
  document.addEventListener("click", (e) => { if (!document.getElementById("excel-filter").contains(e.target)) closeExcelFilter(); });
}

/* ── CREATOR RENDER ────────────────────────────────────────────── */

function renderCreators() {
  const dict = t();
  const container = document.getElementById("creators-cards");
  container.innerHTML = "";
  renderExcelFilter();

  if (!state.creators.length) {
    container.innerHTML = `<div class="hint">${dict.placeholders.noCreators}</div>`;
    renderStatCards(document.getElementById("creator-stats"), [
      { label: dict.stats.creatorsTotal, value: 0 },
      { label: dict.stats.worksTotal, value: 0 },
      { label: dict.stats.filesTotal, value: 0 },
      { label: dict.stats.capacityTotal, value: "0 B" },
    ]);
    return;
  }

  const visible = sortCreators(state.creators.filter((c) => creatorMatchesFilter(c)));
  const visibleKeys = new Set(visible.map((c) => c.creator_key));
  for (const key of state.selectedCreators) if (!visibleKeys.has(key)) state.selectedCreators.delete(key);

  if (state.reorderMode) {
    const bar = document.createElement("div");
    bar.className = "reorder-bar";
    bar.innerHTML = `<span>拖拽卡片调整顺序</span><div class="reorder-bar-actions">
      <button class="btn btn-ghost btn-sm" id="btn-cancel-order" type="button">取消</button>
      <button class="btn btn-primary btn-sm" id="btn-save-order" type="button">保存排序</button>
    </div>`;
    container.appendChild(bar);
  }

  const batchSelected = visible.filter((c) => state.selectedCreators.has(c.creator_key));
  if (state.batchMode) {
    const bar = document.createElement("div");
    bar.className = "batch-bar";
    bar.innerHTML = `<label class="batch-select-all">
      <input type="checkbox" id="batch-select-all" ${visible.length > 0 && batchSelected.length === visible.length ? "checked" : ""} />
      <span id="batch-label">${batchSelected.length > 0 ? `已选 ${batchSelected.length} / ${visible.length}` : "全选"}</span>
    </label>
    <div class="batch-tag-action" id="batch-tag-action" style="${batchSelected.length === 0 ? "display:none" : ""}">
      <div class="batch-tag-picker" id="batch-tag-picker">
        <div class="batch-tag-picker-header">
          <input type="text" id="batch-tag-input" placeholder="选择或输入标签..." maxlength="30" />
          <button class="btn btn-primary btn-sm" id="batch-tag-apply" type="button">应用</button>
        </div>
        <div class="batch-tag-picker-body" id="batch-tag-picker-body"></div>
      </div>
    </div>`;
    container.appendChild(bar);
  }

  if (!visible.length) {
    container.innerHTML = `<div class="hint">${dict.common.noData}</div>`;
    renderStatCards(document.getElementById("creator-stats"), [
      { label: dict.stats.creatorsTotal, value: 0 },
      { label: dict.stats.worksTotal, value: 0 },
      { label: dict.stats.filesTotal, value: 0 },
      { label: dict.stats.capacityTotal, value: "0 B" },
    ]);
    return;
  }

  let worksTotal = 0, filesTotal = 0, capacityTotal = 0;
  const customTagSet = new Set();

  for (const creator of visible) {
    worksTotal += Number(creator.works_count || 0);
    filesTotal += Number(creator.files_count || 0);
    capacityTotal += Number(creator.total_bytes || 0);
    for (const tag of getCreatorTags(creator.creator_key)) customTagSet.add(tag);

    const row = document.createElement("details");
    row.className = "creator-row";
    row.dataset.creatorKey = creator.creator_key;
    if (state.expandedCreators.has(creator.creator_key)) row.open = true;

    // Group links by platform::type
    const grouped = new Map();
    (creator.platform_groups || []).forEach((group) => {
      (group.links || []).forEach((link) => {
        const key = `${group.platform}::${link.account_type}`;
        if (!grouped.has(key)) grouped.set(key, { platform: group.platform, account_type: link.account_type, links: [] });
        grouped.get(key).links.push(link);
      });
    });

    const groupRows = Array.from(grouped.values())
      .sort((a, b) => `${a.platform}/${a.account_type}`.localeCompare(`${b.platform}/${b.account_type}`))
      .map((group) => {
        const linkRows = group.links.map((link) => {
          const label = formatLinkLabel(link);
          const targetUrl = safeExternalUrl(link.account_url);
          const match = state.accounts.find((a) => a.platform === group.platform && a.account_type === group.account_type && a.account_url === link.account_url);
          const checked = match && match.scheduled !== false;
          return `<div class="group-link-item">
            <div class="group-link-main">
              <a class="group-link-label" href="${targetUrl}" target="_blank" rel="noopener">${escapeHtml(label)}</a>
              <button class="btn-link-del" type="button" data-del-link data-creator-key="${escapeHtml(creator.creator_key)}" data-url="${escapeHtml(link.account_url)}" data-platform="${escapeHtml(group.platform)}" data-type="${escapeHtml(link.account_type)}" title="删除链接">✕</button>
            </div>
            <div class="group-link-bottom">
              <span class="group-link-meta">最后更新: ${toLocalTime(link.last_updated_at)}</span>
              ${match ? `<label class="group-link-toggle"><input type="checkbox" class="account-sched-toggle" data-account-id="${match.id}" ${checked ? "checked" : ""} /><span>${dict.labels.accountScheduled}</span></label>` : ""}
            </div>
          </div>`;
        }).join("");
        return `<div class="group-block">
          <div class="group-title">${escapeHtml(group.platform)} / ${escapeHtml(group.account_type)}</div>
          <div class="group-links">${linkRows || `<span class="hint">${dict.placeholders.noLinks}</span>`}</div>
        </div>`;
      }).join("");

    const platformTags = creatorPlatformTags(creator).map((tag) => `<span class="tag-chip tag-platform">${escapeHtml(tag)}</span>`).join("");
    const customTags = getCreatorTags(creator.creator_key).map((tag) => `<span class="tag-chip tag-custom-static">${escapeHtml(tag)}</span>`).join("");

    const batchCb = state.batchMode ? `<label class="creator-select-cb" onclick="event.stopPropagation()"><input type="checkbox" class="creator-select" data-creator-key="${escapeHtml(creator.creator_key)}" ${state.selectedCreators.has(creator.creator_key) ? "checked" : ""} /></label>` : "";
    const drag = state.reorderMode ? `<span class="drag-handle" draggable="true" data-creator-key="${escapeHtml(creator.creator_key)}">⠿</span>` : "";

    row.innerHTML = `<summary class="creator-summary${state.batchMode ? " is-batch" : ""}${state.reorderMode ? " is-reorder" : ""}">
      ${drag}${batchCb}
      <div class="creator-main">
        <div class="creator-name-line">
          <p class="creator-name">${escapeHtml(creator.display_name)}</p>
          <div class="creator-tags">${platformTags}${customTags}</div>
        </div>
      </div>
      <div class="creator-inline-metrics">
        <span class="inline-metric metric-works">${dict.labels.works}: <strong>${creator.works_count || 0}</strong></span>
        <span class="inline-metric metric-files">${dict.labels.files}: <strong>${creator.files_count || 0}</strong></span>
        <span class="inline-metric metric-capacity">${dict.labels.capacity}: <strong>${formatBytes(creator.total_bytes || 0)}</strong></span>
        <span class="inline-metric metric-updated">${dict.labels.updated}: <strong>${toLocalTime(creator.last_updated_at)}</strong></span>
      </div>
    </summary>
    <div class="creator-expand">
      <div class="group-list">${groupRows || `<span class="hint">${dict.placeholders.noLinks}</span>`}</div>
      <div class="creator-add-link">
        <select class="add-link-platform">${state.platforms.map(p => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`).join("")}</select>
        <select class="add-link-type"></select>
        <input type="text" class="add-link-input" placeholder="URL..." data-creator-key="${escapeHtml(creator.creator_key)}" />
        <button class="btn btn-ghost btn-sm add-link-btn" type="button" data-creator-key="${escapeHtml(creator.creator_key)}">添加</button>
      </div>
    </div>`;

    row.addEventListener("toggle", () => {
      if (state.reorderMode) { setTimeout(() => { row.open = false; }, 0); return; }
      const k = creator.creator_key;
      if (!k) return;
      if (row.open) state.expandedCreators.add(k);
      else state.expandedCreators.delete(k);
    });

    // Scheduled toggle
    row.querySelectorAll(".account-sched-toggle").forEach((toggle) => {
      toggle.addEventListener("change", async () => {
        const aid = Number(toggle.dataset.accountId);
        const sched = toggle.checked;
        try {
          await api(`/accounts/${aid}/scheduled`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scheduled: sched }) });
          const acct = state.accounts.find((a) => a.id === aid);
          if (acct) acct.scheduled = sched;
        } catch { toggle.checked = !sched; }
      });
    });

    // Batch select checkbox
    const selCb = row.querySelector(".creator-select");
    if (selCb) {
      selCb.addEventListener("change", () => {
        if (selCb.checked) state.selectedCreators.add(creator.creator_key);
        else state.selectedCreators.delete(creator.creator_key);
        renderCreators();
      });
    }

    // Delete link
    row.querySelectorAll("[data-del-link]").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const url = btn.dataset.url, key = btn.dataset.creatorKey;
        if (!url || !key) return;
        try {
          await api(`/creators/${encodeURIComponent(key)}/links?account_url=${encodeURIComponent(url)}`, { method: "DELETE" });
          await Promise.all([loadCreators(), loadAccounts()]);
        } catch (err) { alert("删除失败: " + err.message); }
      });
    });

    // Add link
    const addBtn = row.querySelector(".add-link-btn"), addInput = row.querySelector(".add-link-input");
    const addPlatform = row.querySelector(".add-link-platform"), addType = row.querySelector(".add-link-type");
    if (addBtn && addInput && addPlatform && addType) {
      const updateTypes = () => {
        const plat = state.platforms.find(p => p.name === addPlatform.value);
        addType.innerHTML = (plat ? plat.account_types : ["profile", "live"]).map(t => `<option value="${t}">${t}</option>`).join("");
      };
      addPlatform.addEventListener("change", updateTypes);
      updateTypes();
      const doAdd = async () => {
        const url = addInput.value.trim(), platform = addPlatform.value, accountType = addType.value;
        if (!url || !platform || !accountType) return;
        try {
          await api(`/creators/${encodeURIComponent(creator.creator_key)}/links`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ platform, account_type: accountType, account_url: url }) });
          addInput.value = "";
          await Promise.all([loadCreators(), loadAccounts()]);
        } catch (err) { alert("添加失败: " + err.message); }
      };
      addBtn.addEventListener("click", doAdd);
      addInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doAdd(); });
    }

    container.appendChild(row);
  }

  // Stats
  renderStatCards(document.getElementById("creator-stats"), [
    { label: dict.stats.creatorsTotal, value: visible.length },
    { label: dict.stats.worksTotal, value: worksTotal },
    { label: dict.stats.filesTotal, value: filesTotal },
    { label: dict.stats.capacityTotal, value: formatBytes(capacityTotal) },
  ]);

  // Batch: select all
  document.getElementById("batch-select-all")?.addEventListener("change", function () {
    if (this.checked) visible.forEach((c) => state.selectedCreators.add(c.creator_key));
    else state.selectedCreators.clear();
    renderCreators();
  });

  // Batch tag picker
  const pickerBody = document.getElementById("batch-tag-picker-body");
  const batchInput = document.getElementById("batch-tag-input");
  if (pickerBody && batchInput) {
    const allTags = getAllFilterTags();
    pickerBody.innerHTML = allTags.length
      ? allTags.map((tag) => `<label class="batch-tag-picker-item"><input type="checkbox" class="batch-tag-cb" data-tag="${escapeHtml(tag)}" /><span>${escapeHtml(tag)}</span></label>`).join("")
      : `<span class="hint" style="padding:6px 10px;display:block">暂无已有标签</span>`;
  }

  document.getElementById("batch-tag-apply")?.addEventListener("click", async () => {
    const tags = new Set();
    document.querySelectorAll(".batch-tag-cb:checked").forEach((cb) => tags.add(cb.dataset.tag));
    const custom = normalizeTag(batchInput?.value || "");
    if (custom) tags.add(custom);
    if (tags.size === 0) return;

    const btn = document.getElementById("batch-tag-apply");
    btn.disabled = true;
    btn.textContent = "保存中...";
    try {
      await Promise.all(Array.from(state.selectedCreators).map((key) => _saveTags(key, [...new Set([...getCreatorTags(key), ...tags])])));
      if (batchInput) batchInput.value = "";
      document.querySelectorAll(".batch-tag-cb").forEach((cb) => { cb.checked = false; });
      state.selectedCreators.clear();
    } catch { /* ignore */ }
    btn.disabled = false;
    btn.textContent = "应用";
    renderCreators();
  });

  // Drag reorder
  if (state.reorderMode) setupDragReorder(container);
}

/* ── DRAG-REORDER ──────────────────────────────────────────────── */

function setupDragReorder(container) {
  let dragState = null;

  container.addEventListener("mousedown", (e) => {
    const handle = e.target.closest(".drag-handle");
    if (!handle) return;
    const row = handle.closest(".creator-row");
    if (!row) return;
    e.preventDefault();

    const startY = e.clientY;
    const ghost = row.cloneNode(true);
    ghost.style.position = "fixed";
    ghost.style.pointerEvents = "none";
    ghost.style.width = row.offsetWidth + "px";
    ghost.style.zIndex = "1000";
    ghost.style.opacity = "0.8";
    ghost.style.boxShadow = "0 8px 24px rgba(0,0,0,0.15)";
    ghost.style.borderRadius = "12px";
    const r = row.getBoundingClientRect();
    ghost.style.top = (r.top + window.scrollY) + "px";
    ghost.style.left = r.left + "px";
    ghost.style.transition = "none";
    document.body.appendChild(ghost);
    row.style.opacity = "0.3";
    row.style.transition = "transform 0.2s";

    dragState = { el: row, startY, offsetY: 0, ghost };
  });

  document.addEventListener("mousemove", (e) => {
    if (!dragState) return;
    e.preventDefault();
    const dy = e.clientY - dragState.startY;
    dragState.offsetY = dy;
    dragState.ghost.style.transform = `translateY(${dy}px)`;

    const ghostCenter = e.clientY + dragState.ghost.offsetHeight / 2;
    const rows = Array.from(container.querySelectorAll(".creator-row"));
    const srcIdx = rows.indexOf(dragState.el);
    let targetIdx = srcIdx;
    for (let i = 0; i < rows.length; i++) {
      if (rows[i] === dragState.el) continue;
      const rc = rows[i].getBoundingClientRect();
      if (ghostCenter > rc.top + rc.height / 2 && i > targetIdx) targetIdx = i;
      if (ghostCenter < rc.top + rc.height / 2 && i < targetIdx) targetIdx = i;
    }
    rows.forEach((r, i) => {
      if (r === dragState.el) return;
      r.style.transition = "transform 0.2s";
      if (i < srcIdx && i >= targetIdx) r.style.transform = `translateY(${dragState.el.offsetHeight}px)`;
      else if (i > srcIdx && i <= targetIdx) r.style.transform = `translateY(${-dragState.el.offsetHeight}px)`;
      else r.style.transform = "";
    });
  });

  document.addEventListener("mouseup", () => {
    if (!dragState) return;
    document.body.removeChild(dragState.ghost);
    dragState.el.style.opacity = "";
    dragState.el.style.transition = "";
    document.querySelectorAll(".creator-row").forEach((r) => { r.style.transform = ""; r.style.transition = ""; });
    dragState = null;
  });
}
