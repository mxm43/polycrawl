/* ── Cookie Management (manual configuration only) ──────────── */

/* global state, t, $, api */

const LOGIN_TAB_KEY = "login";
const COOKIE_EXPIRY_THRESHOLD = 86400; // 24h in seconds
const COOKIE_CRITICAL_THRESHOLD = 604800; // 7d in seconds

/** Platform cookie templates: key → description */
const COOKIE_TEMPLATES = {
  xiaohongshu: {
    a1: "基础身份令牌（必需）",
    web_session: "会话令牌（登录后获取）",
    webId: "设备 ID",
    gid: "设备指纹",
    websectiga: "风控安全令牌",
    sec_poison_id: "风控标识",
    id_token: "身份令牌",
    acw_tc: "阿里云 WAF 令牌",
    xsecappid: "签名应用 ID",
    abRequestId: "请求 ID",
  },
  douyin: {
    ttwid: "设备标识（必需）",
    odin_tt: "用户身份标识",
    passport_csrf_token: "CSRF 令牌",
    sid_guard: "会话令牌（登录后获取）",
    passport_assist_user: "辅助用户信息",
    sid_ucp_v1: "会话 v1",
    s_v_web_id: "设备指纹",
  },
  weibo: {
    SUB: "微博登录令牌（必需）",
    SUBP: "登录子令牌",
    SCF: "安全验证",
    SSOLoginState: "登录状态时间戳",
    _T_WM: "设备标识",
    ALF: "自动登录标记",
    MLOGIN: "移动端登录标记",
    XSRF_TOKEN: "CSRF 令牌",
    gdxidpyhxdE: "设备指纹",
    mweibo_short_token: "短令牌",
  },
};

/* ── Smart Cookie Parser ──────────────────────────────────── */

/**
 * Parse raw cookie text (browser-pasted) into a key-value dict.
 * Supports multiple formats:
 *   1. JSON object: {"key": "val", ...}
 *   2. JSON array:  [{"name": "key", "value": "val"}, ...]
 *   3. Semicolon-separated: key=val; key2=val2
 *   4. Newline-separated: key=val\nkey2=val2
 *   5. Cookie header line: cookie: key=val; key2=val2
 *   6. Full cookie lines with attrs: key=val; Path=/; Domain=...; Expires=...
 *   7. Mixed newlines + semicolons
 *   8. Tab-separated (Chrome DevTools export): Name\tValue\tDomain\tPath\t...
 */
function parseCookieText(raw) {
  let text = String(raw ?? "").trim();
  if (!text) return null;

  // ── Try JSON first ──────────────
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === "object") {
      if (Array.isArray(parsed)) {
        // [{"name":"key","value":"val"}] or [{"key":"val"}]
        const result = {};
        for (const item of parsed) {
          if (item && typeof item === "object") {
            if (item.name != null && item.value != null) {
              result[String(item.name).trim()] = String(item.value).trim();
            } else {
              for (const [k, v] of Object.entries(item)) {
                if (k && v != null) result[k.trim()] = String(v).trim();
              }
            }
          }
        }
        return Object.keys(result).length ? result : null;
      }
      // Plain object
      const result = {};
      for (const [k, v] of Object.entries(parsed)) {
        if (k && v != null) result[k.trim()] = String(v).trim();
      }
      return Object.keys(result).length ? result : null;
    }
  } catch { /* not JSON, fall through */ }

  // ── Strip common prefixes ──────
  text = text.replace(/^(?:cookie|set-cookie):\s*/i, "");

  // ── Tab-separated format (Chrome DevTools export) ──────
  // Lines contain \t, first field is cookie name, second is value.
  // Heuristic: at least 2 lines with tabs, or 1 line with ≥2 tabs.
  const lines = text.split("\n").map(s => s.trim()).filter(Boolean);
  const tabLines = lines.filter(l => l.includes("\t"));
  if (tabLines.length >= Math.max(2, lines.length / 2)) {
    const result = {};
    for (const line of tabLines) {
      const parts = line.split("\t");
      const key = (parts[0] || "").trim();
      const val = (parts[1] || "").trim();
      if (key && val) result[key] = val;
    }
    if (Object.keys(result).length) return result;
  }

  // ── Split into segments (; or \n delimited) ────────────
  let segments = text.split(/[;\n]+/).map(s => s.trim()).filter(Boolean);

  const result = {};
  const KNOWN_ATTRS = new Set([
    "path", "domain", "expires", "max-age", "secure", "httponly",
    "samesite", "priority", "samesite", "comment", "version",
  ]);

  for (const seg of segments) {
    // If segment contains sub-attributes (e.g. "key=val; Path=/"),
    // split on first semicolon that follows a value
    // But we already split by ; so each segment should be atomic.
    // However, the original text may have been separated by newlines only,
    // so a segment may still contain "; Path=/" style sub-attrs.
    // Handle this by extracting only the first key=value pair.
    const eqIdx = seg.indexOf("=");
    if (eqIdx <= 0) continue;

    const key = seg.substring(0, eqIdx).trim();
    let value = seg.substring(eqIdx + 1).trim();

    // Skip known cookie attribute keys
    if (KNOWN_ATTRS.has(key.toLowerCase())) continue;

    // If value contains a semicolon, strip everything after it
    // (e.g., "value; Path=/" → "value")
    const semiIdx = value.indexOf(";");
    if (semiIdx >= 0) value = value.substring(0, semiIdx).trim();

    // Strip surrounding quotes from value
    value = value.replace(/^["']|["']$/g, "");

    if (key && value) result[key] = value;
  }

  return Object.keys(result).length ? result : null;
}

function _templatePlaceholder(platform) {
  const tpl = COOKIE_TEMPLATES[platform];
  if (!tpl) return '{"key1": "value1", "key2": "value2"}';
  const keys = Object.keys(tpl);
  const sample = {};
  keys.forEach(k => { sample[k] = ""; });
  return JSON.stringify(sample, null, 2);
}

function _templateKeysHint(platform) {
  const tpl = COOKIE_TEMPLATES[platform];
  if (!tpl) return "";
  return "必需字段: " + Object.entries(tpl)
    .filter(([k, v]) => v.includes("必需"))
    .map(([k]) => k)
    .join(", ") + " — 其他可选字段见模板";
}

function _timeAgo(seconds) {
  if (seconds == null) return "-";
  if (seconds < 60) return seconds + "秒前";
  if (seconds < 3600) return Math.floor(seconds / 60) + "分钟前";
  if (seconds < 86400) return Math.floor(seconds / 3600) + "小时前";
  return Math.floor(seconds / 86400) + "天前";
}

function _cookieStatusBadge(status) {
  if (!status.has_cookies) return `<span class="badge badge-err">未设置</span>`;
  // Explicit verify result takes precedence over age-based heuristics.
  if (status.verified_ok === true) return `<span class="badge badge-ok">已验证</span>`;
  if (status.verified_ok === false) return `<span class="badge badge-err">已验证失败</span>`;
  if (status.critical) return `<span class="badge badge-err">已过期(>7天)</span>`;
  if (status.expired) return `<span class="badge badge-warn">即将过期(>24h)</span>`;
  return `<span class="badge badge-warn">未验证</span>`;
}

/** Refresh a single platform card's badge & status details without full re-render. */
async function _refreshPlatformCard(platform) {
  let statuses;
  try {
    statuses = await api("/login/status");
  } catch {
    return;
  }
  const st = statuses.find(s => s.platform === platform);
  if (!st) return;

  const card = document.querySelector(`.login-platform-card[data-platform="${platform}"]`);
  if (!card) return;

  // ── Update badge ──
  const platId = card.querySelector(".plat-id");
  if (platId) {
    const h3 = platId.querySelector("h3");
    if (h3) {
      platId.innerHTML = h3.outerHTML + _cookieStatusBadge(st);
    }
  }

  // ── Update status detail line ──
  const statusLine = [
    `${st.cookie_count} 个 cookie`,
    st.verified_ok === true ? "✓ 已验证通过" : "",
    st.verified_ok === false ? "✗ 验证失败" : "",
    st.saved_at_iso ? `保存于 ${_timeAgo(st.elapsed_seconds)}` : "",
  ].filter(Boolean).join(" · ");
  const detailEl = card.querySelector(".status-detail");
  if (detailEl) detailEl.textContent = statusLine;

  // ── Clear transient test-result ──
  const testResult = document.getElementById(`test-result-${platform}`);
  if (testResult) {
    testResult.textContent = "";
    testResult.style.color = "";
  }
}

async function renderLoginPage() {
  const dict = t();
  const container = document.getElementById("login-content");
  if (!container) return;

  container.innerHTML = `<p class="hint" id="login-loading">加载平台状态...</p>`;

  let statuses = [];
  try {
    statuses = await api("/login/status");
  } catch (e) {
    container.innerHTML = `<p class="hint" style="color:var(--danger)">加载失败: ${escapeHtml(e.message)}</p>`;
    return;
  }

  let html = `<div class="login-platforms">`;

  for (const st of statuses) {
    const plat = st.platform;
    const platLabel = plat === "xiaohongshu" ? "小红书" : plat === "douyin" ? "抖音" : plat === "weibo" ? "微博" : plat;

    const statusLine = [
      `${st.cookie_count} 个 cookie`,
      st.verified_ok === true ? "✓ 已验证通过" : "",
      st.verified_ok === false ? "✗ 验证失败" : "",
      st.saved_at_iso ? `保存于 ${_timeAgo(st.elapsed_seconds)}` : "",
    ].filter(Boolean).join(" · ");

    html += `
      <div class="login-platform-card" data-platform="${escapeHtml(plat)}">
        <div class="login-platform-header">
          <div class="plat-id">
            <h3>${escapeHtml(platLabel)}</h3>
            ${_cookieStatusBadge(st)}
          </div>
          <span class="hint status-detail">${escapeHtml(statusLine)}</span>
          <span id="test-result-${escapeHtml(plat)}" class="hint test-result"></span>
          <div class="header-actions">
            <button class="btn btn-ghost btn-sm btn-test-cookies" type="button" data-platform="${escapeHtml(plat)}">🔍 测试</button>
            <button class="btn btn-primary btn-sm btn-save-cookies" type="button" data-platform="${escapeHtml(plat)}">💾 保存</button>
          </div>
        </div>
        ${st.expired ? `<div class="hint cookie-warn">⚠ cookies 上次更新已超过24小时，可能已过期</div>` : ""}
        ${st.critical ? `<div class="hint cookie-critical">🚨 cookies 已过期超过7天，需要重新设置</div>` : ""}
        ${st.verified_ok === false ? `<div class="hint cookie-critical">✗ 上次验证失败，cookies 可能已失效</div>` : ""}

        <div class="cookie-input-row">
          <textarea id="cookie-input-${escapeHtml(plat)}" class="cookies-textarea" rows="5"
            placeholder='在此粘贴 Cookie（JSON / key=value / 浏览器导出格式）'>${escapeHtml(_templatePlaceholder(plat))}</textarea>
          <div class="cookie-input-tools">
            <label class="smart-paste-toggle">
              <input type="checkbox" id="smart-paste-${escapeHtml(plat)}" checked>
              <span>智能粘贴</span>
              <span class="toggle-desc">自动解析为 JSON</span>
            </label>
            <div id="parse-result-${escapeHtml(plat)}" class="parse-result"></div>
          </div>
          <div id="cookie-result-${escapeHtml(plat)}" class="cookie-result"></div>
        </div>
      </div>`;
  }

  html += `</div>

    <style>
      .login-platforms { display: flex; flex-direction: column; gap: 0.75rem; }
      .login-platform-card { border: 1px solid var(--border); border-radius: 6px; padding: 0.6rem 0.8rem; }
      .login-platform-header { display: flex; align-items: center; gap: 0.5rem; }
      .plat-id { display: flex; align-items: center; gap: 0.4rem; width: 150px; flex-shrink: 0; }
      .plat-id h3 { margin: 0; font-size: 1rem; white-space: nowrap; width: 4em; }
      .status-detail { font-size: 0.78rem; color: var(--text-muted, #999); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .test-result { font-size: 0.78rem; min-width: 4em; flex-shrink: 0; }
      .header-actions { display: flex; align-items: center; gap: 0.35rem; flex-shrink: 0; margin-left: auto; }
      .cookies-textarea { width: 100%; font-family: var(--mono); font-size: 0.82rem; padding: 0.35rem 0.5rem; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); color: var(--text); resize: vertical; box-sizing: border-box; }
      .cookie-input-row { margin-top: 0.4rem; }
      .cookie-input-tools { display: flex; align-items: center; gap: 0.5rem; margin-top: 0.3rem; }
      .smart-paste-toggle { display: inline-flex; align-items: center; gap: 0.3rem; cursor: pointer; font-size: 0.78rem; user-select: none; color: var(--text-muted, #999); white-space: nowrap; flex-shrink: 0; }
      .smart-paste-toggle input[type="checkbox"] { margin: 0; cursor: pointer; accent-color: var(--primary, #409eff); }
      .toggle-desc { color: var(--text-muted, #bbb); margin-left: 0.1rem; }

      .cookie-result { font-size: 0.78rem; margin: 0; line-height: 1.4; min-height: 0; }
      .cookie-warn { color: var(--warning, #e6a23c); }
      .cookie-critical { color: var(--danger, #f56c6c); }
      .cookie-keys-hint { font-size: 0.8rem; color: var(--text-muted, #999); margin: 0; }
      .badge { display: inline-block; padding: 0.15em 0.5em; border-radius: 4px; font-size: 0.8rem; font-weight: 600; white-space: nowrap; }
      .badge-ok { background: var(--success, #67c23a); color: #fff; }
      .badge-warn { background: var(--warning, #e6a23c); color: #fff; }
      .badge-err { background: var(--danger, #f56c6c); color: #fff; }
      .parse-result { display: none; margin: 0.35rem 0; padding: 0.35rem 0.6rem; border-radius: 4px; font-size: 0.8rem; }
      .parse-ok { color: var(--success, #67c23a); }
      .parse-err { color: var(--danger, #f56c6c); }
      .parse-result code { background: var(--border, #ddd); padding: 0.1em 0.35em; border-radius: 3px; font-size: 0.75rem; color: var(--text); }
    </style>`;

  container.innerHTML = html;

  // ── Attach save handlers ────────────────────────────────
  container.querySelectorAll(".btn-save-cookies").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const platform = btn.dataset.platform;
      const input = document.getElementById("cookie-input-" + platform);
      const result = document.getElementById("cookie-result-" + platform);
      if (!input || !result) return;

      let cookies;
      try {
        cookies = JSON.parse(input.value);
      } catch {
        result.textContent = "JSON 格式错误，请检查";
        result.style.color = "var(--danger)";
        return;
      }
      if (Object.keys(cookies).length === 0) {
        result.textContent = "cookies 不能为空";
        result.style.color = "var(--danger)";
        return;
      }

      try {
        await api("/login/cookies", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ platform, cookies }),
        });
        result.textContent = "Cookies 已保存！";
        result.style.color = "var(--success)";
        setTimeout(() => _refreshPlatformCard(platform), 1000);
      } catch (e) {
        result.textContent = "保存失败: " + e.message;
        result.style.color = "var(--danger)";
      }
    });
  });

  // ── Attach smart paste handlers ──────────────────────────
  container.querySelectorAll(".cookies-textarea").forEach((ta) => {
    const platform = ta.id.replace("cookie-input-", "");
    const parseResult = document.getElementById("parse-result-" + platform);
    const toggle = document.getElementById("smart-paste-" + platform);

    ta.addEventListener("paste", (e) => {
      // Only intercept if smart paste is enabled
      if (toggle && !toggle.checked) return;

      // Let the paste happen naturally first, then we'll process
      // Use setTimeout to read the pasted value after the event
      setTimeout(() => {
        const raw = ta.value;
        if (!raw) return;

        const parsed = parseCookieText(raw);
        if (parsed) {
          const keys = Object.keys(parsed);
          ta.value = JSON.stringify(parsed, null, 2);
          if (parseResult) {
            parseResult.innerHTML =
              `<span class="parse-ok">✓ 已解析 <strong>${keys.length}</strong> 个 Cookie 字段：` +
              keys.map(k => `<code>${escapeHtml(k)}</code>`).join(", ") +
              `</span>`;
            parseResult.style.display = "block";
          }
        } else {
          if (parseResult) {
            parseResult.innerHTML = `<span class="parse-err">✗ 未能自动解析，请检查格式或关闭智能粘贴手动输入 JSON</span>`;
            parseResult.style.display = "block";
          }
        }
      }, 10);
    });

    // Clear parse result when user manually edits
    ta.addEventListener("input", () => {
      if (parseResult) parseResult.style.display = "none";
    });
  });

  // ── Attach test handlers ────────────────────────────────
  container.querySelectorAll(".btn-test-cookies").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const platform = btn.dataset.platform;
      const resultEl = document.getElementById("test-result-" + platform);
      if (!resultEl) return;

      btn.disabled = true;
      resultEl.textContent = "测试中...";
      resultEl.style.color = "";

      try {
        const res = await api("/login/verify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ platform }),
        });
        if (res.valid) {
          resultEl.textContent = "✓ " + (res.detail || "有效");
          resultEl.style.color = "var(--success)";
        } else {
          resultEl.textContent = "✗ " + (res.detail || "无效");
          resultEl.style.color = "var(--danger)";
        }
        // Refresh card badges after verification
        setTimeout(() => _refreshPlatformCard(platform), 1000);
      } catch (e) {
        resultEl.textContent = "请求失败: " + e.message;
        resultEl.style.color = "var(--danger)";
      }
      btn.disabled = false;
    });
  });
}
