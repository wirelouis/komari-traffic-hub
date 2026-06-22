const SIDEBAR_STORAGE_KEY = "komari.sidebarCollapsed";
const THEME_STORAGE_KEY = "komari.themeMode";
const THEME_MODES = ["auto", "light", "dark"];
const DISPLAY_LIMITS = {
  overviewNodes: 8,
  analyticsNodes: 10,
  analyticsGroups: 18,
  taskRuns: 6,
};

// Data is treated as fresh for this long. Within the window, navigating back to
// a route shows its cached content instantly with no network call; past it, the
// route is revalidated silently in the background (stale-while-revalidate).
const ROUTE_REVALIDATE_MS = 30000;

const routeConfig = {
  "/": {
    title: "流量分析工作台",
    subtitle: "查看探针流量、节点排行和服务状态。",
  },
  "/nodes": {
    title: "节点流量分析",
    subtitle: "按时间窗口比较节点上下行、合计流量和 Komari 机器绑定。",
  },
  "/alerts": {
    title: "告警控制",
    subtitle: "查看告警状态、规则、检查历史，并执行检查或推送。",
  },
  "/telegram": {
    title: "推送控制",
    subtitle: "预览周期报表、测试 Telegram，并查看计划任务。",
  },
  "/ai": {
    title: "数据问答",
    subtitle: "刷新数据包、使用快捷问题，快速定位流量异常。",
  },
  "/analytics": {
    title: "流量分析",
    subtitle: "按日期范围查看 SQLite 长期统计、节点贡献和分组趋势。",
  },
  "/system": {
    title: "系统健康",
    subtitle: "查看配置状态、运行记录、低敏配置和数据维护动作。",
  },
};

const state = {
  authenticated: false,
  overview: null,
  route: "/",
  nodesHours: 24,
  nodes: [],
  machines: [],
  schedules: [],
  selectedNodeUuid: "",
  sidebarCollapsed: false,
  themeMode: "auto",
  aiAsking: false,
  aiAutoRefreshed: false,
  system: null,
  routeToken: 0,
  requestToken: 0,
  nodeDetailToken: 0,
  routeLoaded: {},
  routeLoadedAt: {},
  nodeDetail: null,
  exportSuccessTimer: null,
};

const $ = (id) => document.getElementById(id);

function setVisible(id, visible) {
  $(id).classList.toggle("hidden", !visible);
}

function loginFields() {
  return [$("login-username"), $("login-password")].filter(Boolean);
}

function loginRememberEnabled() {
  return Boolean($("login-remember")?.checked);
}

function setLoginFieldMode({ remember = false, clear = false } = {}) {
  const form = $("login-form");
  const username = $("login-username");
  const password = $("login-password");
  if (!form || !username || !password) return;
  form.setAttribute("autocomplete", remember ? "on" : "off");
  if (remember) {
    form.removeAttribute("data-lpignore");
  } else {
    form.setAttribute("data-lpignore", "true");
  }
  username.setAttribute("name", remember ? "username" : "komari-user-field");
  password.setAttribute("name", remember ? "password" : "komari-pass-field");
  username.setAttribute("autocomplete", remember ? "username" : "new-password");
  password.setAttribute("autocomplete", remember ? "current-password" : "new-password");
  loginFields().forEach((input) => {
    if (clear) input.value = "";
    if (remember) {
      input.removeAttribute("readonly");
      input.removeAttribute("data-lpignore");
    } else {
      input.setAttribute("readonly", "readonly");
      input.setAttribute("data-lpignore", "true");
    }
  });
}

function lockAndClearLoginFields() {
  setLoginFieldMode({ remember: loginRememberEnabled(), clear: true });
}

function handleLoginRememberChange() {
  const remember = loginRememberEnabled();
  setLoginFieldMode({ remember, clear: !remember });
}

function unlockLoginFields() {
  if (loginRememberEnabled()) return;
  loginFields().forEach((input) => {
    input.removeAttribute("readonly");
  });
}

function showLoginView(message = "") {
  state.authenticated = false;
  state.overview = null;
  if ($("login-remember")) $("login-remember").checked = false;
  lockAndClearLoginFields();
  $("login-error").textContent = message;
  setVisible("login-view", true);
  setVisible("app-view", false);
  window.setTimeout(lockAndClearLoginFields, 80);
  window.setTimeout(lockAndClearLoginFields, 360);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function stripHtml(value) {
  return String(value ?? "").replace(/<[^>]+>/g, "");
}

function friendlyError(value) {
  const text = String(value || "").trim();
  if (!text) return "请求失败";
  if (/Invalid URL|No scheme supplied|MissingSchema|KOMARI_BASE_URL/i.test(text)) {
    return "Komari API 未配置或不可达，暂时没有可展示的数据。";
  }
  return text;
}

function normalizeThemeMode(value) {
  return THEME_MODES.includes(value) ? value : "auto";
}

function loadThemePreference() {
  try {
    state.themeMode = normalizeThemeMode(window.localStorage.getItem(THEME_STORAGE_KEY));
  } catch (_error) {
    state.themeMode = "auto";
  }
}

function saveThemePreference() {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, state.themeMode);
  } catch (_error) {
    // Ignore storage failures in private browsing or restricted WebViews.
  }
}

function applyThemeMode() {
  document.documentElement.dataset.theme = state.themeMode;
  document.documentElement.style.colorScheme = state.themeMode === "dark" ? "dark" : (state.themeMode === "light" ? "light" : "light dark");
  document.querySelectorAll("#theme-switch [data-theme]").forEach((button) => {
    const isActive = button.dataset.theme === state.themeMode;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-pressed", String(isActive));
  });
}

// --- Motion helpers --------------------------------------------------------
const prefersReducedMotion = () =>
  Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

let progressHideTimer = null;
let progressStartTimer = null;
function cancelRouteProgress() {
  const bar = $("route-progress");
  if (!bar) return;
  window.clearTimeout(progressHideTimer);
  window.clearTimeout(progressStartTimer);
  bar.classList.remove("loading", "done");
}
function startRouteProgress({ delay = 160 } = {}) {
  const bar = $("route-progress");
  if (!bar) return;
  window.clearTimeout(progressHideTimer);
  window.clearTimeout(progressStartTimer);
  const show = () => {
    bar.classList.remove("done");
    void bar.offsetWidth; // restart the slide cleanly on rapid navigation
    bar.classList.add("loading");
  };
  if (delay > 0) {
    progressStartTimer = window.setTimeout(show, delay);
  } else {
    show();
  }
}
function stopRouteProgress() {
  const bar = $("route-progress");
  if (!bar) return;
  window.clearTimeout(progressStartTimer);
  if (!bar.classList.contains("loading")) {
    bar.classList.remove("done");
    return;
  }
  bar.classList.remove("loading");
  bar.classList.add("done");
  progressHideTimer = window.setTimeout(() => bar.classList.remove("done"), 360);
}

function retriggerPop(el) {
  if (!el) return;
  el.classList.remove("kt-pop");
  void el.offsetWidth;
  el.classList.add("kt-pop");
}

// Animate a metric value: count up the numeric part (keeping its unit suffix),
// otherwise just set the text with a subtle pop. Honors reduced-motion.
function setMetricValue(el, finalText) {
  if (!el) return;
  const text = String(finalText ?? "--");
  const match = text.match(/^(-?\d[\d,]*(?:\.\d+)?)(\s*\D[\s\S]*)?$/);
  if (!match || prefersReducedMotion()) {
    el.textContent = text;
    if (!match && !prefersReducedMotion()) retriggerPop(el);
    return;
  }
  const target = parseFloat(match[1].replace(/,/g, ""));
  const suffix = match[2] || "";
  const decimals = (match[1].split(".")[1] || "").length;
  if (!Number.isFinite(target)) {
    el.textContent = text;
    return;
  }
  // Glide from the value already on screen rather than always from 0. On the
  // first paint there's nothing to parse, so it counts up from 0 as before; on a
  // silent refresh it tweens from the current number, so an unchanged or barely
  // changed metric stays put instead of snapping down to 0 and back.
  // Only glide from the previous number when its unit is the same; if the suffix
  // changed (MiB -> GiB) the old number is on a different scale, so start from 0.
  const prev = el.dataset.metricValue;
  const sameUnit = el.dataset.metricSuffix === suffix;
  const start = sameUnit && prev !== undefined && Number.isFinite(parseFloat(prev)) ? parseFloat(prev) : 0;
  el.dataset.metricValue = String(target);
  el.dataset.metricSuffix = suffix;
  if (start === target) {
    el.textContent = `${target.toFixed(decimals)}${suffix}`;
    return;
  }
  const duration = 650;
  const startTime = performance.now();
  function step(now) {
    const t = Math.min(1, (now - startTime) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = `${(start + (target - start) * eased).toFixed(decimals)}${suffix}`;
    if (t < 1) requestAnimationFrame(step);
    else el.textContent = `${target.toFixed(decimals)}${suffix}`;
  }
  requestAnimationFrame(step);
}

// --- Skeleton placeholders -------------------------------------------------
function skelCards(n) {
  return Array.from({ length: n }, () => `<div class="kt-skeleton kt-skel-card"></div>`).join("");
}
function skelRows(n) {
  return `<div class="kt-skel-stack">${Array.from({ length: n }, () => `<div class="kt-skeleton kt-skel-row"></div>`).join("")}</div>`;
}
function skelTableRows(rows, cols) {
  const cells = Array.from({ length: cols }, () => `<td><div class="kt-skeleton kt-skel-line lg"></div></td>`).join("");
  return Array.from({ length: rows }, () => `<tr>${cells}</tr>`).join("");
}
function setSkel(id, html) {
  const el = $(id);
  // Only show a skeleton when the container has no real content yet (first paint).
  // Avoids flashing skeletons over existing data on refresh / navigating back.
  if (el && el.children.length === 0) {
    el.classList.remove("hidden");
    el.innerHTML = html;
  }
}
function showRouteSkeleton(route) {
  switch (route) {
    case "/":
      setSkel("overview-health", skelCards(4));
      setSkel("top-list", skelRows(4));
      setSkel("trend-chart", `<div class="kt-skeleton kt-skel-chart"></div>`);
      break;
    case "/nodes":
      setSkel("nodes-table", skelTableRows(6, 5));
      break;
    case "/alerts":
      setSkel("alerts-summary", skelCards(4));
      setSkel("alerts-body", skelRows(2));
      setSkel("alert-history-list", skelRows(4));
      setSkel("alert-thresholds", skelRows(5));
      break;
    case "/telegram":
      setSkel("telegram-summary", skelCards(4));
      setSkel("telegram-schedules", skelRows(2));
      setSkel("telegram-task-runs", skelRows(3));
      break;
    case "/ai":
      setSkel("ai-summary", skelCards(4));
      setSkel("ai-sources", skelRows(3));
      break;
    case "/system":
      setSkel("system-summary", skelCards(4));
      setSkel("system-services", skelRows(3));
      setSkel("system-data", skelRows(2));
      setSkel("system-task-runs", skelRows(3));
      break;
    default:
      break;
  }
}

// On a failed load, replace any skeleton placeholders still showing in the
// active view with a friendly empty state so nothing shimmers forever.
function clearRouteSkeleton() {
  const view = document.querySelector(".route-view:not(.hidden)");
  if (!view) return;
  const containers = new Set();
  view.querySelectorAll(".kt-skeleton").forEach((el) => {
    const holder = el.closest("[id]");
    if (holder && holder !== view) containers.add(holder);
  });
  containers.forEach((c) => {
    c.innerHTML = `<div class="empty-state">暂时无法加载，请稍后重试。</div>`;
  });
}

async function api(path, options = {}) {
  const controller = new AbortController();
  // AI 相关接口需要更长的超时时间，默认 15 秒适用于普通接口
  const timeout = options.timeout !== undefined ? options.timeout : 15000;
  const timeoutId = setTimeout(() => controller.abort(), timeout);
  const callToken = options.token;

  try {
    const response = await fetch(path, {
      credentials: "same-origin",
      signal: controller.signal,
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
    });

    if (callToken !== undefined && callToken !== state.requestToken) {
      return null;
    }

    const data = await response.json().catch(() => ({ ok: false, error: { message: "响应无法解析" } }));
    if (!response.ok || data.ok === false) {
      const error = new Error(friendlyError(data.error?.message || "请求失败"));
      error.payload = data;
      error.status = response.status;
      if (error.status === 401) {
        showLoginView();
      }
      throw error;
    }
    return data.data;
  } catch (error) {
    if (error.name === "AbortError") {
      const timeoutError = new Error("请求超时，请检查网络连接或稍后重试。");
      timeoutError.isTimeout = true;
      throw timeoutError;
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
}

async function postJson(path, body = {}) {
  return api(path, { method: "POST", body: JSON.stringify(body) });
}

function trafficRangeQuery(extra = {}) {
  const query = new URLSearchParams({
    from: $("traffic-range-from").value,
    to: $("traffic-range-to").value,
    group: $("traffic-range-group").value,
  });
  Object.entries(extra).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
  });
  return query;
}

function filenameFromDisposition(header, fallback) {
  const text = String(header || "");
  const utf8 = text.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8) return decodeURIComponent(utf8[1]);
  const plain = text.match(/filename="?([^";]+)"?/i);
  return plain ? plain[1] : fallback;
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function runAction({ resultId, busyText, button, fn }) {
  const resultEl = resultId ? $(resultId) : null;
  const originalDisabled = button?.disabled;
  if (button) button.classList.add("loading");
  try {
    if (resultEl) resultEl.textContent = busyText || "处理中...";
    if (button) button.disabled = true;
    const result = await fn();
    return result;
  } catch (error) {
    if (resultEl) resultEl.textContent = friendlyError(error.message);
    updateStatus(friendlyError(error.message), false);
    throw error;
  } finally {
    if (button) {
      button.classList.remove("loading");
      button.disabled = originalDisabled || false;
    }
  }
}

function showToast(message, type = "info", duration = 3000) {
  const container = $("toast-container") || document.body.appendChild(Object.assign(document.createElement("div"), { id: "toast-container" }));
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.classList.add("toast-exit");
    setTimeout(() => toast.remove(), 250);
  }, duration);
}

function normalizeRoute(value) {
  let path = "/";
  try {
    path = new URL(value, window.location.origin).pathname;
  } catch (_error) {
    path = "/";
  }
  if (path.length > 1 && path.endsWith("/")) path = path.slice(0, -1);
  return routeConfig[path] ? path : "/";
}

function routeSearch() {
  return new URLSearchParams(window.location.search);
}

function routeUrl(target) {
  const url = new URL(target, window.location.origin);
  const route = normalizeRoute(url.pathname);
  return `${route}${url.search}`;
}

function updateTopbar(route) {
  const config = routeConfig[route] || routeConfig["/"];
  $("topbar-title").textContent = config.title;
  $("topbar-subtitle").textContent = config.subtitle;
  document.title = `${config.title} - Komari Traffic Console`;
}

// Mark a route-view as having completed its first successful load. The CSS
// gates the staggered entrance / bar-grow animations behind
// `.route-view[data-loaded]`, so they play once on first paint and never replay
// on later background refreshes — that replay was the "闪一下" flash.
//
// We set the flag only AFTER the first-paint entrance animations have had time
// to finish (longest is ~310ms stagger delay + 360ms, plus 680ms bars). Setting
// it synchronously would hit the still-running first reveal with
// `animation: none` and cut it off. Reduced-motion users have no animation to
// wait for, so flag immediately.
function markRouteLoaded(route) {
  const view = document.querySelector(`.route-view[data-route="${CSS.escape(route)}"]`);
  if (!view || view.dataset.loaded) return;
  if (prefersReducedMotion()) {
    view.dataset.loaded = "true";
    return;
  }
  window.setTimeout(() => {
    view.dataset.loaded = "true";
  }, 850);
}

function showRoute(route) {
  const nextRoute = normalizeRoute(route);
  state.route = nextRoute;
  state.aiAutoRefreshed = false; // Reset auto-refresh flag on route change
  document.querySelectorAll(".route-view").forEach((view) => {
    view.classList.toggle("hidden", view.dataset.route !== nextRoute);
  });
  document.querySelectorAll(".nav-link[data-route], .brand[data-route]").forEach((link) => {
    const isActive = link.dataset.route === nextRoute;
    link.classList.toggle("active", isActive);
    if (isActive) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  });
  updateTopbar(nextRoute);
}

async function navigateRoute(target, options = {}) {
  const nextUrl = routeUrl(target);
  const nextRoute = normalizeRoute(nextUrl);
  const currentUrl = `${window.location.pathname}${window.location.search}`;
  if (options.replace || (!routeConfig[window.location.pathname] && currentUrl !== nextUrl)) {
    window.history.replaceState({ route: nextRoute }, "", nextUrl);
  } else if (options.push && currentUrl !== nextUrl) {
    window.history.pushState({ route: nextRoute }, "", nextUrl);
  }
  showRoute(nextRoute);
  if (options.scroll !== false) window.scrollTo({ top: 0, behavior: "instant" });
  if (options.load !== false && state.authenticated) {
    await loadCurrentRoute({ includeOverview: Boolean(options.forceOverview), manual: Boolean(options.manual) });
  }
}

function updateStatus(message, good = true) {
  const pill = $("status-pill");
  pill.textContent = message;
  pill.classList.toggle("good", good);
  pill.classList.toggle("bad", !good);
}

function setInlineBadge(id, text = "", level = "") {
  const el = $(id);
  if (!el) return;
  const value = String(text || "").trim();
  el.classList.toggle("hidden", !value);
  el.classList.remove("good", "bad", "warn");
  if (!value) {
    el.textContent = "";
    el.removeAttribute("title");
    const toolbar = el.closest(".route-toolbar");
    if (toolbar) {
      const hasVisibleChild = Array.from(toolbar.children).some((child) => !child.classList.contains("hidden"));
      toolbar.classList.toggle("hidden", !hasVisibleChild);
    }
    return;
  }
  el.textContent = value;
  if (level) el.classList.add(level);
  const toolbar = el.closest(".route-toolbar");
  if (toolbar) {
    const hasVisibleChild = Array.from(toolbar.children).some((child) => !child.classList.contains("hidden"));
    toolbar.classList.toggle("hidden", !hasVisibleChild);
  }
}

function actionableLevel(level) {
  return level === "bad" || level === "warn";
}

function renderNoticeRows(items, emptyText = "没有需要处理的项目。") {
  const rows = (items || []).filter((item) => actionableLevel(item.level));
  if (!rows.length) {
    return `<div class="quiet-state">${escapeHtml(emptyText)}</div>`;
  }
  return rows.map((item) => {
    const cls = item.level === "bad" ? "bad" : "warn";
    const label = item.level === "bad" ? "需处理" : "提醒";
    const detail = [item.message, item.detail].filter(Boolean).join(" ");
    return `
      <div class="notice-row ${cls}">
        <span class="notice-dot" aria-hidden="true"></span>
        <span>
          <strong>${escapeHtml(item.label || item.title || "提醒")}</strong><br>
          <span class="tiny">${escapeHtml(detail || "需要确认当前状态。")}</span>
          ${item.fix ? `<br><span class="tiny fix-text">${escapeHtml(item.fix)}</span>` : ""}
        </span>
        <span class="notice-badge ${cls}">${escapeHtml(label)}</span>
      </div>`;
  }).join("");
}

function serviceMetricText(data) {
  const services = data.services || {};
  if (!services.komari?.configured) {
    return { value: "需配置", note: "还没有可读取的 Komari 探针地址" };
  }
  const notes = ["探针可读"];
  if (services.telegram?.configured) notes.push("推送可用");
  else notes.push("推送未配置");
  if (services.alerts?.enabled) notes.push("告警启用");
  else notes.push("告警关闭");
  if (services.ai?.configured) notes.push("AI 可用");
  return {
    value: "运行中",
    note: notes.join("，"),
  };
}

function noteText(period) {
  if (!period?.ok) return friendlyError(period?.error?.message || "不可用");
  const data = period.data;
  if (data.note === "insufficient_snapshots") return "采样不足";
  const count = data.node_count ?? data.nodes?.length ?? 0;
  return data.hidden_node_count ? `${count} 个节点 · 展示摘要` : `${count} 个节点`;
}

function totalText(period) {
  if (!period?.ok) return "--";
  return period.data?.total?.total_human || "--";
}

function formatBytes(value) {
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let n = Math.max(0, Number(value || 0));
  for (let i = 0; i < units.length; i += 1) {
    if (n < 1024 || i === units.length - 1) {
      return units[i] === "B" ? `${Math.round(n)} B` : `${n.toFixed(2)} ${units[i]}`;
    }
    n /= 1024;
  }
  return "0 B";
}

function percentOf(value, max) {
  const n = finiteNumber(value);
  const m = Math.max(1, finiteNumber(max));
  return Math.max(0, Math.min(100, (n / m) * 100));
}

function finiteNumber(value) {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? n : 0;
}

function byteValue(row, key = "total") {
  if (!row || typeof row !== "object") return 0;
  const aliases = {
    total: ["total", "total_bytes", "bytes", "value"],
    up: ["up", "up_bytes", "upload", "upload_bytes"],
    down: ["down", "down_bytes", "download", "download_bytes"],
  }[key] || [key, `${key}_bytes`];
  for (const alias of aliases) {
    const value = row[alias];
    if (value && typeof value === "object") {
      const nested = byteValue(value, key);
      if (nested > 0) return nested;
    } else {
      const n = finiteNumber(value);
      if (n > 0) return n;
    }
  }
  return 0;
}

function byteText(row, key = "total") {
  if (!row || typeof row !== "object") return "--";
  const aliases = {
    total: ["total_human", "totalHuman", "bytes_human", "value_human"],
    up: ["up_human", "upHuman", "upload_human"],
    down: ["down_human", "downHuman", "download_human"],
  }[key] || [`${key}_human`];
  for (const alias of aliases) {
    if (row[alias]) return String(row[alias]);
  }
  return formatBytes(byteValue(row, key));
}

function barPercent(value, max) {
  const pct = percentOf(value, max);
  return (finiteNumber(value) > 0 && pct < 0.4 ? 0.4 : pct).toFixed(2);
}

function barEnd(percent) {
  return (2 + (Number(percent) / 100) * 96).toFixed(2);
}

function svgBar(className, value, max, height = 8) {
  const end = barEnd(barPercent(value, max));
  const y = height / 2;
  const fillLine = finiteNumber(value) > 0
    ? `<line class="bar-fill-line ${escapeHtml(className)}" x1="2" y1="${y}" x2="${end}" y2="${y}"></line>`
    : "";
  return `
    <svg class="bar-svg bar-svg-single" viewBox="0 0 100 ${height}" preserveAspectRatio="none" aria-hidden="true">
      <line class="bar-track-line" x1="2" y1="${y}" x2="98" y2="${y}"></line>
      ${fillLine}
    </svg>`;
}

function svgStackedBar(down, up, max) {
  const downEnd = Number(barEnd(barPercent(down, max)));
  const upEnd = Number((downEnd + (Number(barPercent(up, max)) / 100) * 96).toFixed(2));
  const downLine = finiteNumber(down) > 0
    ? `<line class="bar-fill-line down stacked-segment" x1="2" y1="6" x2="${downEnd.toFixed(2)}" y2="6"></line>`
    : "";
  const upLine = finiteNumber(up) > 0
    ? `<line class="bar-fill-line up stacked-segment" x1="${downEnd.toFixed(2)}" y1="6" x2="${Math.min(98, upEnd).toFixed(2)}" y2="6"></line>`
    : "";
  return `
    <svg class="bar-svg bar-svg-stacked" viewBox="0 0 100 12" preserveAspectRatio="none" aria-hidden="true">
      <line class="bar-track-line" x1="2" y1="6" x2="98" y2="6"></line>
      ${downLine}
      ${upLine}
    </svg>`;
}

function sumBy(rows, key) {
  return (rows || []).reduce((total, row) => total + byteValue(row, key), 0);
}

function compactTrafficRows(rows, limit, label = "其他节点") {
  const items = Array.isArray(rows) ? rows : [];
  const maxItems = Math.max(1, Number(limit || items.length || 1));
  if (items.length <= maxItems) {
    return { rows: items, hidden: 0, hiddenTotal: 0 };
  }
  const visible = items.slice(0, maxItems);
  const rest = items.slice(maxItems);
  const up = sumBy(rest, "up");
  const down = sumBy(rest, "down");
  const total = up + down;
  return {
    rows: [
      ...visible,
      {
        uuid: "__other__",
        name: `${rest.length} 个${label}`,
        up,
        down,
        total,
        up_human: formatBytes(up),
        down_human: formatBytes(down),
        total_human: formatBytes(total),
        compact_other: true,
      },
    ],
    hidden: rest.length,
    hiddenTotal: total,
  };
}

function compactCompareRows(rows, limit) {
  const items = Array.isArray(rows) ? rows : [];
  const maxItems = Math.max(1, Number(limit || items.length || 1));
  if (items.length <= maxItems) {
    return { rows: items, hidden: 0 };
  }
  const visible = items.slice(0, maxItems);
  const rest = items.slice(maxItems);
  const last24 = sumBy(rest, "last24");
  const last7d = sumBy(rest, "last7d");
  return {
    rows: [
      ...visible,
      {
        uuid: "__other__",
        name: `${rest.length} 个其他节点`,
        last24,
        last7d,
        last24_human: formatBytes(last24),
        last7d_human: formatBytes(last7d),
        compact_other: true,
      },
    ],
    hidden: rest.length,
  };
}

function metric(stat, key) {
  const value = stat?.[key];
  return value === null || value === undefined || Number.isNaN(Number(value)) ? "--" : `${value}%`;
}

function hoursLabel(hours) {
  const value = Number(hours || 0);
  if (value === 1) return "最近 1 小时";
  if (value === 6) return "最近 6 小时";
  if (value === 24) return "最近 24 小时";
  if (value === 168) return "最近 7 天";
  if (value === 720) return "最近 30 天";
  return `${value} 小时窗口`;
}

function miniCard(label, value, note = "", status = "") {
  const severity = status === "bad" || status === "warn" ? status : "";
  const statusText = severity === "bad" ? "需处理" : "提醒";
  return `
    <article class="mini-card ${escapeHtml(severity)}">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value || "--")}</strong>
      ${note ? `<small class="tiny">${escapeHtml(note)}</small>` : ""}
      ${severity ? `<span class="notice-badge ${escapeHtml(severity)}">${escapeHtml(statusText)}</span>` : ""}
    </article>`;
}

function isoDay(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function setDefaultRangeDates() {
  const to = new Date();
  const from = new Date(to.getTime() - 29 * 24 * 3600 * 1000);
  if (!$("traffic-range-to").value) $("traffic-range-to").value = isoDay(to);
  if (!$("traffic-range-from").value) $("traffic-range-from").value = isoDay(from);
}

function runTypeLabel(type) {
  const map = { report: "报表", alert: "告警", ai: "AI", sample: "采样", maintenance: "维护" };
  return map[type] || type || "任务";
}

function aiSourceLabel(key) {
  const map = {
    today: "今日流量",
    last_1h: "最近 1 小时",
    last_1h_by_node: "最近 1 小时节点明细",
    last_24h: "最近 24 小时",
    last_24h_hourly: "最近 24 小时趋势",
    today_hourly_by_node: "今日小时趋势",
    yesterday_hourly_by_node: "昨日小时趋势",
    last_7d: "最近 7 天",
    last_30d: "最近 30 天",
    history: "历史趋势",
    alerts: "告警状态",
  };
  return map[key] || key || "数据源";
}

function runStatusClass(status) {
  if (status === "success") return "good";
  if (status === "failed") return "bad";
  return "";
}

function runStatusText(status) {
  if (status === "success") return "成功";
  if (status === "failed") return "失败";
  return status || "未知";
}

function statusLevelClass(level, ok = false) {
  if (level === "bad") return "bad";
  if (level === "warn" || level === "muted") return "warn";
  return ok ? "good" : "";
}

function statusLevelText(level, ok = false) {
  if (level === "bad") return "需处理";
  if (level === "warn") return "提醒";
  if (level === "muted") return "已关闭";
  return ok || level === "ok" ? "正常" : "待确认";
}

function taskRunNote(run) {
  if (run.error) return run.error;
  if (run.summary) return run.summary;
  if (run.status === "success") return "任务已完成。";
  if (run.status === "failed") return "任务执行失败，请查看错误。";
  return "任务状态未记录完整。";
}

function renderTaskRuns(targetId, runs) {
  const target = $(targetId);
  const rawItems = runs || [];
  const items = rawItems.slice(0, DISPLAY_LIMITS.taskRuns);
  const hidden = Math.max(0, rawItems.length - items.length);
  target.innerHTML = items.length
    ? `${items.map((run) => `
      <div class="task-run-row ${run.status === "failed" ? "failed" : ""}">
        <span>
          <strong>${escapeHtml(runTypeLabel(run.type))}</strong><br>
          <span class="tiny">${escapeHtml(taskRunNote(run))}</span><br>
          <span class="tiny">${escapeHtml(run.started_at_text || "未记录时间")} · 耗时 ${escapeHtml(run.duration_text || "--")}</span>
        </span>
        <span class="pill ${runStatusClass(run.status)}">${escapeHtml(runStatusText(run.status))}</span>
      </div>`).join("")}${hidden ? `<div class="compact-note">还有 ${hidden} 条较早记录未展开。</div>` : ""}`
    : `<div class="empty-state">最近没有需要展示的任务记录。</div>`;
}

async function loadTaskRuns(targetId, type = "", limit = DISPLAY_LIMITS.taskRuns) {
  const query = new URLSearchParams({ limit: String(limit) });
  if (type) query.set("type", type);
  const data = await api(`/api/tasks/runs?${query.toString()}`);
  renderTaskRuns(targetId, data.runs || []);
  return data.runs || [];
}

function nodeByUuid(uuid) {
  const target = String(uuid || "");
  return state.nodes.find((node) => String(node.uuid) === target)
    || (String(state.nodeDetail?.uuid || "") === target ? state.nodeDetail : null);
}

function mergeNodeDetail(node) {
  if (!node?.uuid) return;
  const index = state.nodes.findIndex((item) => String(item.uuid) === String(node.uuid));
  if (index >= 0) {
    state.nodes[index] = { ...state.nodes[index], ...node };
  } else {
    state.nodes.push(node);
  }
  state.nodeDetail = node;
}

function bindingLabel(binding) {
  if (!binding) return "未绑定";
  if (binding.mode === "manual") return binding.stale ? "手动失效" : "手动";
  if (binding.mode === "auto") return "自动";
  return "未绑定";
}

function bindingPillClass(binding) {
  if (!binding || binding.stale || binding.mode === "missing") return "bad";
  if (binding.mode === "manual") return "warn";
  return "good";
}

function machineByUuid(uuid) {
  const target = String(uuid || "");
  return state.machines.find((machine) => String(machine.uuid) === target) || null;
}

function bindingSelectValue(node) {
  return node?.binding?.mode === "manual" ? String(node.binding.komari_uuid || "") : "";
}

function machineOptionText(machine) {
  const note = [machine?.region, machine?.group].filter(Boolean).join(" · ");
  return `${machine?.name || machine?.uuid || "未命名机器"}${note ? ` · ${note}` : ""} · ${machine?.uuid || ""}`;
}

function renderBindingOptions(node) {
  const current = bindingSelectValue(node);
  const options = [`<option value="">自动绑定（节点 uuid）</option>`];
  if (current && !machineByUuid(current)) {
    options.push(`<option value="${escapeHtml(current)}">当前手动绑定不可用 · ${escapeHtml(current)}</option>`);
  }
  state.machines.forEach((machine) => {
    options.push(`<option value="${escapeHtml(machine.uuid)}">${escapeHtml(machineOptionText(machine))}</option>`);
  });
  return options.join("");
}

function applyBindingResult(sourceId, payload = {}) {
  if (Array.isArray(payload.machines)) state.machines = payload.machines;
  const node = nodeByUuid(sourceId);
  if (!node) return null;
  node.binding = payload.binding || node.binding || {};
  node.komari = {
    ...(node.komari || {}),
    machine: payload.machine || null,
    web_url: payload.web_url || "",
  };
  if (String(state.nodeDetail?.uuid || "") === String(sourceId)) {
    state.nodeDetail = { ...state.nodeDetail, binding: node.binding, komari: node.komari };
  }
  return node;
}

function nodeWebUrl(node) {
  return node?.komari?.web_url || "";
}

function defaultNodeDetailText() {
  return "选择节点查看详情；点击打开按钮进入 Komari 机器。";
}

function updateNodeSelectionUi(uuid = state.selectedNodeUuid) {
  const activeUuid = String(uuid || "");
  document.querySelectorAll("#nodes-table tr[data-uuid]").forEach((row) => {
    row.classList.toggle("selected", activeUuid && row.dataset.uuid === activeUuid);
  });
  document.querySelectorAll('[data-node-action="detail"]').forEach((button) => {
    const selected = activeUuid && button.dataset.uuid === activeUuid;
    button.textContent = selected ? "收起" : "详情";
    button.setAttribute("aria-expanded", String(Boolean(selected)));
  });
}

function clearNodeRouteParam() {
  if (state.route !== "/nodes" || !routeSearch().has("node")) return;
  const url = new URL(window.location.href);
  url.searchParams.delete("node");
  const nextUrl = `${url.pathname}${url.search}`;
  window.history.replaceState({ route: "/nodes" }, "", nextUrl || "/nodes");
}

function collapseNodeDetail({ updateUrl = true } = {}) {
  state.selectedNodeUuid = "";
  state.nodeDetail = null;
  state.nodeDetailToken += 1;
  updateNodeSelectionUi("");
  $("node-detail").textContent = defaultNodeDetailText();
  if (updateUrl) clearNodeRouteParam();
}

async function jumpToNode(uuid) {
  if (!uuid) return;
  await navigateRoute(`/nodes?node=${encodeURIComponent(uuid)}`, { push: true });
}


function renderOverviewHealth(system) {
  const target = $("overview-health");
  if (!target) return;
  if (!system) {
    target.classList.remove("hidden");
    target.innerHTML = [
      miniCard("状态同步", "暂不可用", "系统健康信息稍后重试", "warn"),
    ].join("");
    return;
  }
  const summary = system.summary || {};
  const latestReport = system.latest_runs?.report;
  const db = system.data?.sqlite || {};
  const schedules = system.runtime?.schedules || {};
  const cards = [];
  if ((summary.issues || []).length) {
    cards.push(miniCard("需要处理", `${summary.issues.length} 项`, summary.issues.join("、"), "bad"));
  }
  if ((summary.warnings || []).length) {
    cards.push(miniCard("需要确认", `${summary.warnings.length} 项`, summary.warnings.join("、"), "warn"));
  }
  if (latestReport?.status === "failed") {
    cards.push(miniCard("最近任务失败", "报表", latestReport.started_at_text || "未记录时间", "bad"));
  }
  if (db.ok === false) {
    cards.push(miniCard("长期统计异常", "无法保存", "检查 data 目录权限和容器日志", "bad"));
  }
  if (Number(schedules.total || 0) > 0 && Number(schedules.enabled || 0) === 0) {
    cards.push(miniCard("推送计划停用", "0 启用", `${schedules.total} 条计划均未启用`, "warn"));
  }
  target.classList.toggle("hidden", cards.length === 0);
  target.innerHTML = cards.join("");
}

function renderOverview(data) {
  state.overview = data;
  const periods = data.periods || {};
  setMetricValue($("metric-today"), totalText(periods.today));
  setMetricValue($("metric-week"), totalText(periods.week));
  setMetricValue($("metric-month"), totalText(periods.month));
  $("metric-today-note").textContent = noteText(periods.today);
  $("metric-week-note").textContent = noteText(periods.week);
  $("metric-month-note").textContent = noteText(periods.month);
  const serviceText = serviceMetricText(data);
  $("metric-services").textContent = serviceText.value;
  $("metric-time").textContent = serviceText.note || data.now || "--";
  $("range-label").textContent = "24h / 7d";

  const topNodes = periods.today?.data?.top_nodes || periods.today?.data?.nodes?.slice(0, 5) || [];
  $("top-list").innerHTML = topNodes.length
    ? topNodes.map((n, index) => `
      <li>
        <button class="top-node-button" data-jump-node="${escapeHtml(n.uuid)}" title="跳到 ${escapeHtml(n.name)}">
          <span class="rank">${index + 1}.</span>
          <span>
            <span class="node-name">${escapeHtml(n.name)}</span><br>
            <span class="tiny">${escapeHtml(n.total_human)} · 下行 ${escapeHtml(n.down_human)} / 上行 ${escapeHtml(n.up_human)}</span>
          </span>
        </button>
      </li>`).join("")
    : `<li class="muted">暂无 Top 数据</li>`;

  renderChart(data);
}

function renderChart(data) {
  const byId = new Map();
  const mergeNodes = (nodes, key) => {
    (nodes || []).forEach((node) => {
      const uuid = String(node.uuid || node.name || "");
      if (!uuid) return;
      const row = byId.get(uuid) || { uuid, name: node.name || uuid, last24: 0, last7d: 0, compact_other: Boolean(node.compact_other) };
      row.name = node.name || row.name;
      row.compact_other = row.compact_other || Boolean(node.compact_other);
      row[key] = byteValue(node, "total");
      row[`${key}_human`] = byteText(node, "total");
      byId.set(uuid, row);
    });
  };
  mergeNodes(data.records?.last_24h?.data?.nodes || [], "last24");
  mergeNodes(data.records?.last_7d?.data?.nodes || [], "last7d");
  const allNodes = [...byId.values()].sort((a, b) => {
    if (a.compact_other !== b.compact_other) return a.compact_other ? 1 : -1;
    return (b.last7d + b.last24) - (a.last7d + a.last24);
  });
  const chart = $("trend-chart");
  if (!allNodes.length) {
    const msg = data.records?.last_24h?.error?.message || data.records?.last_7d?.error?.message || "暂无 records 数据";
    chart.innerHTML = `<div class="empty-state">${escapeHtml(friendlyError(msg))}</div>`;
    return;
  }
  const hiddenFromApi = Math.max(
    Number(data.records?.last_24h?.data?.hidden_node_count || 0),
    Number(data.records?.last_7d?.data?.hidden_node_count || 0),
  );
  const alreadyCompact = allNodes.some((node) => node.compact_other);
  const compact = alreadyCompact ? { rows: allNodes, hidden: hiddenFromApi } : compactCompareRows(allNodes, DISPLAY_LIMITS.overviewNodes);
  const nodes = compact.rows;
  const totalNodeCount = Math.max(
    Number(data.records?.last_24h?.data?.node_count || 0),
    Number(data.records?.last_7d?.data?.node_count || 0),
    allNodes.length,
  );
  const max = Math.max(...nodes.flatMap((n) => [n.last24, n.last7d]), 1);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((ratio) => ({ ratio, label: formatBytes(max * ratio) }));
  chart.innerHTML = `
    <div class="traffic-chart">
      <div class="traffic-chart-legend">
        <span><i class="legend-dot day"></i>最近 24h</span>
        <span><i class="legend-dot week"></i>最近 7d</span>
        <span>展示 ${Math.min(totalNodeCount, DISPLAY_LIMITS.overviewNodes)} / ${totalNodeCount} 个节点</span>
        ${compact.hidden ? `<span>${compact.hidden} 个节点已汇总</span>` : ""}
      </div>
      <div class="traffic-axis">
        <span></span>
        <div class="axis-ticks">${ticks.map((tick) => `<span>${escapeHtml(tick.label)}</span>`).join("")}</div>
      </div>
      <div class="traffic-chart-rows">
        ${nodes.map((n) => `
          <${n.compact_other ? "div" : "button"} class="traffic-chart-row ${n.compact_other ? "compact-other" : ""}" ${n.compact_other ? "" : `data-jump-node="${escapeHtml(n.uuid)}" title="${escapeHtml(n.name)}"`}>
            <span class="traffic-y-label">${escapeHtml(n.name)}</span>
            <span class="traffic-bars">
              <span class="traffic-bar-line">
                ${svgBar("day", n.last24, max)}
                <span class="traffic-value">${escapeHtml(n.last24_human || formatBytes(n.last24))}</span>
              </span>
              <span class="traffic-bar-line">
                ${svgBar("week", n.last7d, max)}
                <span class="traffic-value">${escapeHtml(n.last7d_human || formatBytes(n.last7d))}</span>
              </span>
            </span>
          </${n.compact_other ? "div" : "button"}>`).join("")}
      </div>
    </div>`;
  bindNodeJumpButtons();
}

function bindNodeJumpButtons() {
  document.querySelectorAll("[data-jump-node]").forEach((button) => {
    button.style.cursor = "pointer";
  });
}

async function loadOverview() {
  const [overviewResult, systemResult] = await Promise.allSettled([
    api("/api/overview"),
    api("/api/system/status")
  ]);
  if (overviewResult.status === "fulfilled") {
    renderOverview(overviewResult.value);
  }
  renderOverviewHealth(systemResult.status === "fulfilled" ? systemResult.value : null);
}

function renderNodesTable() {
  $("nodes-table").innerHTML = state.nodes.length
    ? state.nodes.map((n) => {
      const machine = n.komari?.machine;
      const binding = n.binding || {};
      const stale = Boolean(binding.stale);
      const url = nodeWebUrl(n);
      const selected = String(n.uuid) === String(state.selectedNodeUuid);
      return `
        <tr data-uuid="${escapeHtml(n.uuid)}">
          <td><span class="node-name">${escapeHtml(n.name)}</span><div class="tiny">${escapeHtml(n.uuid)}</div></td>
          <td>
            <div class="traffic-cell">
              <strong>${escapeHtml(n.total_human)}</strong>
              <span class="tiny">下行 ${escapeHtml(n.down_human)} · 上行 ${escapeHtml(n.up_human)}</span>
            </div>
          </td>
          <td>
            <div class="health-cell">
              <span>CPU ${metric(n.cpu, "avg")}</span>
              <span class="tiny">RAM ${metric(n.ram, "avg")} · Disk ${metric(n.disk, "avg")}</span>
            </div>
          </td>
          <td>
            <div class="machine-cell">
              <span>${escapeHtml(machine?.name || (stale ? "绑定异常" : "未绑定"))}</span>
              <span class="tiny">${escapeHtml(bindingLabel(binding))}${machine?.region ? ` · ${escapeHtml(machine.region)}` : ""}</span>
            </div>
          </td>
          <td>
            <span class="node-actions">
              <button class="text-btn" data-node-action="detail" data-uuid="${escapeHtml(n.uuid)}" aria-expanded="${selected ? "true" : "false"}">${selected ? "收起" : "详情"}</button>
              <button class="text-btn" data-node-action="open" data-uuid="${escapeHtml(n.uuid)}" ${url ? "" : "disabled"}>打开</button>
            </span>
          </td>
        </tr>`;
    }).join("")
    : `<tr><td colspan="5">暂无节点数据</td></tr>`;
  updateNodeSelectionUi();
}

// Open the node detail named by the ?node= URL param (or keep the current
// selection), or collapse if nothing matches. Shared by loadNodes and by the
// fresh-cache navigation path so jumping to a node still works without a refetch.
async function syncNodeSelectionFromUrl() {
  const target = routeSearch().get("node") || state.selectedNodeUuid;
  if (target && nodeByUuid(target)) {
    await selectNode(target, { scroll: Boolean(routeSearch().get("node")) });
  } else {
    collapseNodeDetail();
  }
}

async function loadNodes(hours = state.nodesHours) {
  state.nodesHours = hours;
  const token = ++state.requestToken;
  setSkel("nodes-table", skelTableRows(6, 5));
  try {
    const data = await api(`/api/nodes?hours=${hours}`, { token });
    if (token !== state.requestToken) return;
    if (!data) return;
    state.nodes = data.nodes || [];
    state.machines = data.machines || state.machines || [];
    renderNodesTable();
    await syncNodeSelectionFromUrl();
  } catch (error) {
    if (token !== state.requestToken) return;
    $("nodes-table").innerHTML = `<tr><td colspan="5">${escapeHtml(friendlyError(error.message))}</td></tr>`;
  }
}

async function selectNode(uuid, options = {}) {
  if (!nodeByUuid(uuid)) return;
  state.selectedNodeUuid = uuid;
  updateNodeSelectionUi(uuid);
  const row = document.querySelector(`#nodes-table tr[data-uuid="${CSS.escape(uuid)}"]`);
  if (options.scroll && row) row.scrollIntoView({ block: "center", behavior: "smooth" });
  await loadNodeDetail(uuid);
}

function renderNodeDetail(node) {
  const machine = node.komari?.machine;
  const binding = node.binding || {};
  const machineNote = machine
    ? [machine.uuid, machine.region, machine.group].filter(Boolean).join(" · ")
    : (binding.stale ? "绑定目标不存在或 Komari 暂不可达" : "暂无可打开的 Komari 机器");
  const currentMachineText = machine?.name || (binding.komari_uuid ? binding.komari_uuid : "未绑定");
  const bindSelectDisabled = state.machines.length ? "" : "disabled";
  const saveDisabled = state.machines.length ? "" : "disabled";
  const autoDisabled = binding.mode === "manual" ? "" : "disabled";
  $("node-detail").innerHTML = `
      <div class="detail-drawer">
      <div class="detail-main">
        <div class="detail-title">
          <h3>${escapeHtml(node.name)}</h3>
          <p class="tiny">${escapeHtml(node.uuid)}</p>
        </div>
        <div class="detail-actions">
          <span class="pill ${bindingPillClass(binding)}">${escapeHtml(bindingLabel(binding))}</span>
          <span class="soft-label">${escapeHtml(hoursLabel(state.nodesHours))}</span>
          <button class="primary-btn" id="detail-open-node" data-detail-action="open" data-uuid="${escapeHtml(node.uuid)}" ${nodeWebUrl(node) ? "" : "disabled"}>打开 Komari 机器</button>
        </div>
      </div>
      <div class="detail-grid">
        <div class="detail-item"><span>合计</span><strong>${escapeHtml(node.total_human)}</strong></div>
        <div class="detail-item"><span>下行</span><strong>${escapeHtml(node.down_human)}</strong></div>
        <div class="detail-item"><span>上行</span><strong>${escapeHtml(node.up_human)}</strong></div>
        <div class="detail-item"><span>Komari 机器</span><strong>${escapeHtml(machine?.name || "未绑定")}</strong><small>${escapeHtml(machineNote)}</small></div>
        <div class="detail-item"><span>CPU 平均</span><strong>${metric(node.cpu, "avg")}</strong><small>峰值 ${metric(node.cpu, "max")}</small></div>
        <div class="detail-item"><span>RAM 平均</span><strong>${metric(node.ram, "avg")}</strong><small>峰值 ${metric(node.ram, "max")}</small></div>
        <div class="detail-item"><span>Disk 平均</span><strong>${metric(node.disk, "avg")}</strong><small>峰值 ${metric(node.disk, "max")}</small></div>
        <div class="detail-item"><span>记录数</span><strong>${escapeHtml(node.record_count || "--")}</strong><small>${escapeHtml(node.from || "")} ${node.to ? `→ ${escapeHtml(node.to)}` : ""}</small></div>
      </div>
      <div class="node-binding-editor">
        <div class="binding-current">
          <span>机器绑定</span>
          <strong>${escapeHtml(currentMachineText)}</strong>
          <small>${escapeHtml(bindingLabel(binding))}${machineNote ? ` · ${escapeHtml(machineNote)}` : ""}</small>
        </div>
        <label class="binding-select">
          <span>绑定到</span>
          <select id="node-binding-select" data-uuid="${escapeHtml(node.uuid)}" ${bindSelectDisabled}>
            ${renderBindingOptions(node)}
          </select>
        </label>
        <div class="binding-editor-actions">
          <button class="primary-btn" data-binding-action="save" data-uuid="${escapeHtml(node.uuid)}" ${saveDisabled}>保存绑定</button>
          <button class="ghost-btn" data-binding-action="auto" data-uuid="${escapeHtml(node.uuid)}" ${autoDisabled}>恢复自动</button>
        </div>
      </div>
    </div>`;
  const select = $("node-binding-select");
  if (select) select.value = bindingSelectValue(node);
}

async function loadNodeDetail(uuid) {
  const token = ++state.nodeDetailToken;
  $("node-detail").textContent = "加载节点详情...";
  try {
    const data = await api(`/api/nodes/${encodeURIComponent(uuid)}?hours=${state.nodesHours}`);
    if (token !== state.nodeDetailToken || String(state.selectedNodeUuid) !== String(uuid)) return;
    mergeNodeDetail(data.node);
    renderNodeDetail(data.node);
  } catch (error) {
    if (token !== state.nodeDetailToken || String(state.selectedNodeUuid) !== String(uuid)) return;
    $("node-detail").textContent = friendlyError(error.message);
  }
}

function openKomariNode(uuid) {
  const node = nodeByUuid(uuid);
  const url = nodeWebUrl(node);
  if (!url) {
    updateStatus("节点未绑定 Komari 机器", false);
    selectNode(uuid).catch(() => null);
    return;
  }
  window.open(url, "_blank", "noopener");
}

async function saveNodeBinding(uuid, { clear = false, button = null } = {}) {
  const node = nodeByUuid(uuid);
  if (!node) {
    updateStatus("节点数据还未同步，请稍后重试", false);
    return;
  }
  const select = $("node-binding-select");
  const komariUuid = clear ? "" : (select?.value || "");
  await runAction({
    button,
    fn: async () => {
      const payload = await postJson("/api/node-bindings", {
        source_id: uuid,
        komari_uuid: komariUuid,
      });
      const updatedNode = applyBindingResult(uuid, payload);
      if (updatedNode) {
        renderNodesTable();
        renderNodeDetail(updatedNode);
      }
      updateStatus(clear ? "已恢复自动绑定" : "绑定已保存", true);
      return payload;
    },
  }).catch(() => null);
}

function renderAlerts(data) {
  const activeCount = Number(data.active_count || 0);
  if (!data.enabled) {
    setInlineBadge("alert-count", "告警关闭", "bad");
  } else if (activeCount > 0) {
    setInlineBadge("alert-count", `${activeCount} 个告警`, "warn");
  } else if (data.muted_until || data.in_silence_window) {
    setInlineBadge("alert-count", "静默中", "warn");
  } else {
    setInlineBadge("alert-count");
  }
  const notices = [];
  if (!data.enabled) {
    notices.push({
      level: "bad",
      label: "告警已关闭",
      message: "当前不会产生新的告警事件。",
      fix: "需要监控流量时，在右侧开启告警并保存。",
    });
  }
  if (activeCount > 0) {
    notices.push({
      level: "warn",
      label: "当前有未恢复告警",
      message: `${activeCount} 个事件仍未恢复。`,
      fix: "先查看左侧事件，再决定是否静默或推送。",
    });
  }
  if (data.muted_until) {
    notices.push({
      level: "warn",
      label: "告警已静默",
      message: `静默至 ${data.muted_until}。`,
      fix: "需要恢复通知时点击下方解除静默。",
    });
  } else if (data.in_silence_window) {
    notices.push({
      level: "warn",
      label: "处于静默时段",
      message: "当前命中静默窗口，通知会被抑制。",
    });
  }
  const chatMissing = !String(data.alert_chat || "").trim() || String(data.alert_chat || "").includes("未配置");
  if (chatMissing) {
    notices.push({
      level: "warn",
      label: "告警 Chat 未配置",
      message: "告警通知会回落到默认 Telegram Chat，或无法推送。",
      fix: "在右侧配置告警 Chat 后保存。",
    });
  }
  const summaryTarget = $("alerts-summary");
  summaryTarget.classList.toggle("hidden", notices.length === 0);
  summaryTarget.innerHTML = renderNoticeRows(notices);

  const active = data.active || [];
  $("alerts-body").innerHTML = active.length
    ? active.map((item) => `
      <div class="alert-row">
        <span><strong>${escapeHtml(item.title)}</strong><br><span class="tiny">${escapeHtml(item.type || item.key)}</span></span>
        <span class="tiny">${escapeHtml(item.last_seen_text || "未记录")}</span>
      </div>`).join("")
    : `<div class="empty-state">当前没有未恢复的告警。</div>`;

  const thresholds = data.thresholds || {};
  const thresholdRows = [
    ["窗口总流量", thresholds.total_window],
    ["窗口单节点", thresholds.node_window],
    ["日总流量", thresholds.daily_total],
    ["日单节点", thresholds.daily_node],
    ["静默窗口", data.silence_windows || "未配置"],
  ];
  $("alert-thresholds").innerHTML = thresholdRows
    .map(([label, value]) => `<div class="rule-chip"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "未设置")}</strong></div>`)
    .join("");
}

async function loadAlerts() {
  setSkel("alerts-summary", skelCards(4));
  setSkel("alerts-body", skelRows(2));
  setSkel("alert-history-list", skelRows(4));
  setSkel("alert-thresholds", skelRows(5));
  try {
    const [alerts, config, history] = await Promise.all([
      api("/api/alerts"),
      api("/api/system/config"),
      api("/api/alerts/history?limit=50").catch(() => ({ runs: [] })),
    ]);
    renderAlerts(alerts);
    renderAlertConfig(config);
    renderAlertHistory(history);
  } catch (error) {
    $("alerts-body").innerHTML = `<div class="empty-state">${escapeHtml(friendlyError(error.message))}</div>`;
    $("alert-config-form").innerHTML = `<div class="empty-state">${escapeHtml(friendlyError(error.message))}</div>`;
  }
}

async function runAlertCheck(notify) {
  const button = event?.target;
  await runAction({
    resultId: "alert-result",
    busyText: "检查中...",
    button,
    fn: async () => {
      const data = await postJson("/api/alerts/check", { notify });
      renderAlertCheckResult(data);
      updateStatus(notify ? "告警已检查并推送" : "告警已检查", true);
      await loadAlerts();
    },
  });
}

function renderAlertCheckResult(data) {
  const summary = data.summary || {};
  const level = summary.level === "warn" ? "bad" : "good";
  const title = summary.title || (data.events?.length ? "检查完成，发现事件" : "检查完成，暂无异常");
  const message = summary.message || `当前未恢复告警 ${data.active_count || 0} 个。`;
  const items = summary.items || [];
  $("alert-result").innerHTML = `
    <div class="alert-check-card ${level}">
      <div>
        <strong>${escapeHtml(title)}</strong>
        <p>${escapeHtml(message)}</p>
      </div>
      <div class="alert-check-meta">
        <span class="pill ${level}">事件 ${escapeHtml(summary.events_count ?? (data.events || []).length)}</span>
        <span class="pill">未恢复 ${escapeHtml(summary.active_count ?? data.active_count ?? 0)}</span>
        <span class="pill">${summary.notified ? "已推送" : "未推送"}</span>
      </div>
      ${items.length ? `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}
    </div>`;
}

async function muteAlerts(hours) {
  $("alert-result").textContent = "设置静默中...";
  try {
    const data = await postJson("/api/alerts/mute", { hours: Number(hours || 1) });
    $("alert-result").textContent = `已静默至 ${data.muted_until}`;
    await loadAlerts();
  } catch (error) {
    $("alert-result").textContent = friendlyError(error.message);
  }
}

function renderAlertHistory(data) {
  const runs = data.runs || [];
  const target = $("alert-history-list");
  if (!runs.length) {
    target.innerHTML = `<div class="empty-state">暂无告警历史记录。</div>`;
    return;
  }
  const PAGE_SIZE = 10;
  const currentPage = Number(target.dataset.historyPage || 1);
  const totalPages = Math.ceil(runs.length / PAGE_SIZE);
  const page = Math.max(1, Math.min(currentPage, totalPages));
  const start = (page - 1) * PAGE_SIZE;
  const end = start + PAGE_SIZE;
  const visible = runs.slice(start, end);

  target.dataset.historyPage = String(page);
  target.innerHTML = visible.map((run) => {
    const meta = run.metadata || {};
    const events = meta.events || 0;
    const activeCount = meta.active_count || 0;
    const notified = meta.notify ? "已推送" : "未推送";
    return `
      <div class="task-run-row ${run.status === "failed" ? "failed" : ""}">
        <span>
          <strong>${escapeHtml(run.started_at_text || "未记录时间")}</strong>
          <span class="pill ${runStatusClass(run.status)}">${escapeHtml(runStatusText(run.status))}</span>
          <br>
          <span class="tiny">事件 ${events} 个 · 未恢复 ${activeCount} 个 · ${notified}</span>
          ${run.summary ? `<br><span class="tiny">${escapeHtml(run.summary)}</span>` : ""}
          ${run.error ? `<br><span class="tiny error-text">${escapeHtml(run.error)}</span>` : ""}
        </span>
        <span class="tiny">${escapeHtml(run.duration_text || "--")}</span>
      </div>`;
  }).join("") + (totalPages > 1 ? `
    <div class="pagination-row">
      <button class="text-btn" id="history-prev-btn" ${page === 1 ? "disabled" : ""}>上一页</button>
      <span class="pagination-info">${page} / ${totalPages}</span>
      <button class="text-btn" id="history-next-btn" ${page === totalPages ? "disabled" : ""}>下一页</button>
    </div>` : "");
}

async function scheduleBody() {
  return {
    enabled: $("schedule-enabled").checked,
    scope: $("schedule-scope").value,
    mode: $("schedule-mode").value,
    time: $("schedule-time").value || "09:00",
    weekday: Number($("schedule-weekday").value || 0),
    month_day: Number($("schedule-month-day").value || 1),
    chat: "",
  };
}

function updateScheduleFormVisibility() {
  const scope = $("schedule-scope").value;
  $("schedule-weekday-wrap").classList.toggle("hidden", scope !== "weekly");
  $("schedule-month-day-wrap").classList.toggle("hidden", scope !== "monthly");
}

function resetScheduleForm() {
  $("schedule-id").value = "";
  $("schedule-scope").value = "daily";
  $("schedule-time").value = "09:00";
  $("schedule-weekday").value = "0";
  $("schedule-month-day").value = "1";
  $("schedule-mode").value = "full";
  $("schedule-enabled").checked = true;
  $("save-schedule-btn").textContent = "保存计划";
  updateScheduleFormVisibility();
}

function editSchedule(id) {
  const item = state.schedules.find((schedule) => schedule.id === id);
  if (!item) return;
  $("schedule-id").value = item.id;
  $("schedule-scope").value = item.scope || "daily";
  $("schedule-time").value = item.time || "09:00";
  $("schedule-weekday").value = String(item.weekday ?? 0);
  $("schedule-month-day").value = String(item.month_day ?? 1);
  $("schedule-mode").value = item.mode || "full";
  $("schedule-enabled").checked = Boolean(item.enabled);
  $("save-schedule-btn").textContent = "更新计划";
  updateScheduleFormVisibility();
  $("schedule-scope").focus();
}

async function saveSchedule() {
  const id = $("schedule-id").value;
  const options = {
    method: id ? "PATCH" : "POST",
    body: JSON.stringify(scheduleBody()),
  };
  $("telegram-result").textContent = id ? "更新计划中..." : "保存计划中...";
  try {
    await api(id ? `/api/schedules/${encodeURIComponent(id)}` : "/api/schedules", options);
    $("telegram-result").textContent = id ? "计划已更新。" : "计划已保存。";
    resetScheduleForm();
    await loadTelegramStatus();
  } catch (error) {
    $("telegram-result").textContent = friendlyError(error.message);
  }
}

async function deleteSchedule(id) {
  if (!id || !window.confirm("删除这条推送计划？")) return;
  $("telegram-result").textContent = "删除计划中...";
  try {
    await api(`/api/schedules/${encodeURIComponent(id)}`, { method: "DELETE" });
    $("telegram-result").textContent = "计划已删除。";
    resetScheduleForm();
    await loadTelegramStatus();
  } catch (error) {
    $("telegram-result").textContent = friendlyError(error.message);
  }
}

async function runScheduleNow(id) {
  if (!id) return;
  $("telegram-result").textContent = "立即发送中...";
  try {
    const data = await postJson(`/api/schedules/${encodeURIComponent(id)}/run-now`, {});
    $("telegram-result").textContent = `已发送：${data.label || "计划任务"}，目标 ${data.chat || "默认 Chat"}；运行记录已写入。`;
    await loadTelegramStatus();
  } catch (error) {
    $("telegram-result").textContent = friendlyError(error.message);
  }
}

function renderTelegramStatus(data) {
  setInlineBadge("telegram-status-pill", data.configured ? "" : "未配置", data.configured ? "" : "bad");
  const schedules = data.schedules || [];
  state.schedules = schedules;
  $("telegram-summary").innerHTML = [
    miniCard("发送状态", data.configured ? "可发送" : "不可发送", data.bot_token_configured ? "Bot Token 已配置" : "缺少 Bot Token", data.configured ? "" : "bad"),
    miniCard("默认 Chat", data.chat || "未配置"),
    miniCard("告警 Chat", data.alert_chat || "未配置"),
    miniCard("应用内计划", `${schedules.length} 条`, "面板可编辑"),
  ].join("");
  const appRows = schedules.length
    ? schedules.map((item) => `
      <div class="schedule-row ${item.enabled ? "" : "muted-row"}">
        <span>
          <strong>${escapeHtml(item.label || "推送计划")}</strong><br>
          <span class="tiny">${item.enabled ? "已启用" : "已停用"} · ${item.mode === "top" ? "Top 报表" : "完整报表"} · ${item.chat_masked ? `Chat ${escapeHtml(item.chat_masked)}` : "默认 Chat"}</span><br>
          <span class="tiny">上次：${escapeHtml(item.last_run?.started_at_text || "暂无")} ${item.last_status ? `· ${escapeHtml(runStatusText(item.last_status))}` : ""}</span><br>
          <span class="tiny">下次：${escapeHtml(item.next_run_text || (item.enabled ? "等待计算" : "已停用"))}</span>
        </span>
        <span class="schedule-actions">
          <span class="pill ${item.enabled ? "good" : ""}">${item.enabled ? "启用" : "停用"}</span>
          <button class="text-btn" data-schedule-action="edit" data-schedule-id="${escapeHtml(item.id)}">编辑</button>
          <button class="text-btn" data-schedule-action="run" data-schedule-id="${escapeHtml(item.id)}">立即发送</button>
          <button class="text-btn danger" data-schedule-action="delete" data-schedule-id="${escapeHtml(item.id)}">删除</button>
        </span>
      </div>`).join("")
    : `<div class="empty-state">还没有应用内计划，可用下方表单新增每日、每周或每月推送。</div>`;
  $("telegram-schedules").innerHTML = appRows;
}

async function loadTelegramStatus() {
  setSkel("telegram-summary", skelCards(4));
  setSkel("telegram-schedules", skelRows(2));
  try {
    const [statusResult, schedulesResult] = await Promise.allSettled([
      api("/api/telegram/status"),
      api("/api/schedules")
    ]);
    const status = statusResult.status === "fulfilled" ? statusResult.value : {};
    const schedules = schedulesResult.status === "fulfilled" ? schedulesResult.value : {};
    renderTelegramStatus({
      ...status,
      schedules: schedules.schedules || [],
      schedule_path: schedules.path,
    });
    await loadTaskRuns("telegram-task-runs", "report", DISPLAY_LIMITS.taskRuns);
  } catch (error) {
    $("telegram-summary").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function reportRequestBody() {
  return {
    scope: $("report-scope").value,
    mode: $("report-mode").value,
  };
}

async function previewReport() {
  $("telegram-preview").textContent = "生成预览中...";
  try {
    const data = await postJson("/api/telegram/preview", reportRequestBody());
    $("telegram-preview").textContent = stripHtml(data.message || "");
    $("telegram-result").textContent = `预览已生成，目标 chat：${data.chat || "未配置"}`;
  } catch (error) {
    $("telegram-preview").textContent = friendlyError(error.message);
  }
}

async function sendReport() {
  $("telegram-result").textContent = "发送中...";
  try {
    const data = await postJson("/api/telegram/report", reportRequestBody());
    $("telegram-result").textContent = `已发送到 ${data.chat}；运行记录已写入。`;
    await loadTelegramStatus();
  } catch (error) {
    $("telegram-result").textContent = friendlyError(error.message);
  }
}

function renderAiStatus(data) {
  const sources = data.data_sources || [];
  const failedSources = sources.filter((item) => item.status !== "ok");
  setInlineBadge(
    "ai-status",
    !data.configured ? "未配置" : (!data.cache_valid ? "待刷新" : (failedSources.length ? `${failedSources.length} 项异常` : "")),
    !data.configured ? "bad" : "warn",
  );
  $("ai-summary").innerHTML = [
    miniCard("AI 状态", data.configured ? "可用" : "不可用", data.model ? `模型 ${data.model}` : "未配置模型", data.configured ? "" : "bad"),
    miniCard("数据包", data.cache_valid ? "可用" : "需要刷新", data.cache_created_at_text || "尚未生成", data.cache_valid ? "" : "warn"),
    miniCard("数据覆盖", sources.length ? `${sources.length} 个窗口` : "未生成", failedSources.length ? `${failedSources.length} 个窗口失败` : "默认隐藏正常窗口", failedSources.length ? "warn" : ""),
  ].join("");
  if (!sources.length) {
    $("ai-sources").innerHTML = `<div class="quiet-state">还没有生成数据包，点击左侧"刷新数据包"后再查看。</div>`;
    return;
  }
  $("ai-sources").innerHTML = failedSources.length
    ? failedSources.map((item) => `
      <div class="source-row bad">
        <span><strong>${escapeHtml(aiSourceLabel(item.key))}</strong><br><span class="tiny">这个分析窗口暂时不可用。</span></span>
        <span class="notice-badge bad">异常</span>
      </div>`).join("")
    : `<div class="quiet-state">数据包已覆盖 ${sources.length} 个常用分析窗口；正常窗口默认不展开。</div>`;
}

async function loadAiStatus() {
  setSkel("ai-summary", skelCards(4));
  setSkel("ai-sources", skelRows(3));
  try {
    const status = await api("/api/ai/status");
    renderAiStatus(status);
    // Auto-refresh the data pack if it's expired and we haven't already refreshed
    // this turn (avoid double-refresh on manual refresh button click).
    if (!status.cache_valid && !state.aiAutoRefreshed) {
      state.aiAutoRefreshed = true;
      $("ai-answer").textContent = "数据包已过期,自动刷新中...";
      try {
        const refreshed = await api("/api/ai/refresh", {
          method: "POST",
          body: JSON.stringify({}),
          timeout: 90000, // AI 自动刷新需要 90 秒超时
        });
        renderAiStatus(refreshed);
        $("ai-answer").textContent = "数据包已自动刷新。";
      } catch (error) {
        $("ai-answer").textContent = `自动刷新失败: ${friendlyError(error.message)}`;
      }
    }
  } catch (error) {
    $("ai-sources").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
  await loadAiHistory();
}

async function loadAiHistory() {
  const historyList = $("ai-history-list");
  if (!historyList) return;

  setSkel("ai-history-list", skelRows(3));
  try {
    const data = await api("/api/ai/history");
    const items = data.items || [];

    if (!items.length) {
      historyList.innerHTML = `<div class="quiet-state">暂无问答历史。</div>`;
      return;
    }

    historyList.innerHTML = items.map((item, index) => {
      const answerId = `ai-history-answer-${index}`;
      const answerText = stripHtml(item.answer || "");
      const isLong = answerText.length > 200;
      const shortAnswer = isLong ? answerText.substring(0, 200) + "..." : answerText;

      return `
        <div class="history-row">
          <div class="history-question">
            <strong>${escapeHtml(item.question || "")}</strong>
            <span class="history-time">${escapeHtml(item.created_at_text || "")}</span>
          </div>
          <div class="history-answer ${isLong ? 'collapsible' : ''}" id="${answerId}">
            <div class="history-answer-text">${escapeHtml(shortAnswer)}</div>
            ${isLong ? `
              <button class="history-expand-btn ghost-btn" data-target="${answerId}" data-full="${escapeHtml(answerText)}" data-short="${escapeHtml(shortAnswer)}">
                展开完整答案
              </button>
            ` : ''}
          </div>
          <button class="ghost-btn history-reask-btn" data-question="${escapeHtml(item.question || '')}">
            再次提问
          </button>
        </div>
      `;
    }).join("");

    // Attach event listeners for expand buttons
    historyList.querySelectorAll(".history-expand-btn").forEach((btn) => {
      btn.addEventListener("click", toggleHistoryAnswer);
    });

    // Attach event listeners for reask buttons
    historyList.querySelectorAll(".history-reask-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const question = btn.dataset.question || "";
        $("ai-question").value = question;
        $("ai-answer").textContent = "已填入历史问题，确认后点击\"提问\"。";
        $("ai-question").focus();
      });
    });
  } catch (error) {
    historyList.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function toggleHistoryAnswer(event) {
  const btn = event.currentTarget;
  const targetId = btn.dataset.target;
  const fullText = btn.dataset.full || "";
  const shortText = btn.dataset.short || "";
  const textEl = btn.previousElementSibling;

  if (btn.textContent.trim() === "展开完整答案") {
    textEl.textContent = fullText;
    btn.textContent = "收起";
  } else {
    textEl.textContent = shortText;
    btn.textContent = "展开完整答案";
  }
}

async function refreshAiPack() {
  if (state.aiAsking) return;
  state.aiAutoRefreshed = true; // Mark as refreshed to avoid auto-refresh after manual refresh
  $("ai-answer").textContent = "刷新数据包中...";
  try {
    const data = await api("/api/ai/refresh", {
      method: "POST",
      body: JSON.stringify({}),
      timeout: 90000, // AI 刷新需要 90 秒超时
    });
    renderAiStatus(data);
    $("ai-answer").textContent = "AI 数据包已刷新。";
  } catch (error) {
    $("ai-answer").textContent = friendlyError(error.message);
  }
}

function setAiBusy(busy) {
  state.aiAsking = Boolean(busy);
  $("ai-ask-btn").disabled = state.aiAsking;
  document.querySelectorAll(".ai-prompt").forEach((button) => {
    button.disabled = state.aiAsking;
  });
}

function fillAiPrompt(button) {
  if (state.aiAsking) return;
  $("ai-question").value = button.dataset.question || "";
  document.querySelectorAll(".ai-prompt").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  $("ai-answer").textContent = "已填入快捷问题，确认后点击\"提问\"。";
  $("ai-question").focus();
}

async function askAi() {
  if (state.aiAsking) return;
  const value = String($("ai-question").value || "").trim();
  if (!value) {
    $("ai-answer").textContent = "请输入问题。";
    return;
  }
  $("ai-question").value = value;
  setAiBusy(true);

  try {
    // Check if data pack is expired before asking
    $("ai-answer").textContent = "检查数据包状态...";
    const status = await api("/api/ai/status");

    if (!status.cache_valid) {
      // Auto-refresh expired data pack
      $("ai-answer").textContent = "数据包已过期，正在更新数据...";
      try {
        await api("/api/ai/refresh", {
          method: "POST",
          body: JSON.stringify({}),
          timeout: 90000,
        });
        renderAiStatus(await api("/api/ai/status"));
      } catch (error) {
        $("ai-answer").textContent = `数据包刷新失败: ${friendlyError(error.message)}`;
        setAiBusy(false);
        return;
      }
    }

    // Now ask the question
    $("ai-answer").textContent = "分析中...";
    const data = await api("/api/ai/ask", {
      method: "POST",
      body: JSON.stringify({ question: value }),
      timeout: 90000,
    });
    $("ai-answer").textContent = stripHtml(data.answer || "");
    await loadAiStatus();
    await loadAiHistory();
  } catch (error) {
    $("ai-answer").textContent = friendlyError(error.message);
  } finally {
    setAiBusy(false);
  }
}

function renderSystemStatus(data) {
  state.system = data;
  const summary = data.summary || {};
  const issues = summary.issues || [];
  const warnings = summary.warnings || [];
  const dbOk = data.data?.sqlite?.ok !== false;
  const schedules = data.runtime?.schedules || {};
  const overall = issues.length ? "需处理" : (warnings.length ? "有提醒" : "正常");
  const overallClass = issues.length ? "bad" : (warnings.length ? "warn" : "good");
  setInlineBadge("system-status-pill", overallClass === "good" ? "" : overall, overallClass);
  const summaryCards = [];
  if (issues.length) summaryCards.push(miniCard("需要处理", `${issues.length} 项`, issues.join("、"), "bad"));
  if (warnings.length) summaryCards.push(miniCard("需要确认", `${warnings.length} 项`, warnings.join("、"), "warn"));
  if (summary.recent_failures) summaryCards.push(miniCard("最近失败", String(summary.recent_failures), "失败记录会出现在下方", "bad"));
  if (!dbOk) summaryCards.push(miniCard("长期统计", "异常", "统计数据可能无法保存", "bad"));
  if (Number(schedules.total || 0) > 0 && Number(schedules.enabled || 0) === 0) {
    summaryCards.push(miniCard("推送计划", "全部停用", `${schedules.total} 条计划未启用`, "warn"));
  }
  $("system-summary").innerHTML = summaryCards.length
    ? summaryCards.join("")
    : `<div class="quiet-state wide-state">没有需要处理的系统问题。</div>`;

  const services = data.health_items || data.services || [];
  $("system-services").innerHTML = services.length
    ? renderNoticeRows(services, "配置健康，没有需要处理的项目。")
    : `<div class="quiet-state">还没有系统状态。</div>`;

  const dataStatus = data.data_status || [];
  $("system-data").innerHTML = dataStatus.length
    ? renderNoticeRows(dataStatus, "数据状态稳定，没有需要处理的项目。")
    : `<div class="quiet-state">还没有数据状态。</div>`;
  renderMaintenanceStatus(data.data?.maintenance || {});
}

function renderMaintenanceStatus(status) {
  const ok = status.ok !== false;
  const oldRuns = Number(status.old_task_runs || 0);
  const hasCleanup = ok && oldRuns > 0;
  setInlineBadge("maintenance-status-pill", !ok ? "异常" : (hasCleanup ? "可清理" : ""), !ok ? "bad" : "warn");
  if (!ok) {
    $("maintenance-summary").innerHTML = [
      miniCard("维护状态", "异常", status.error || "请检查容器日志", "bad"),
    ].join("");
    return;
  }
  if (hasCleanup) {
    $("maintenance-summary").innerHTML = [
      miniCard("运行记录", `${oldRuns} 条偏旧`, "可点击下方按钮清理", "warn"),
      miniCard("保留策略", status.retention_enabled ? `${status.retention_days || 0} 天` : "关闭", "只清理任务运行记录"),
    ].join("");
    return;
  }
  $("maintenance-summary").innerHTML = `<div class="quiet-state wide-state">暂无需要维护的数据项。</div>`;
}

function configFieldsByGroup(fields) {
  const groups = new Map();
  fields.forEach((field) => {
    const group = field.group || "基础";
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(field);
  });
  return groups;
}

function configFieldHtml(field, scope) {
  const key = escapeHtml(field.key);
  const id = `runtime-config-${escapeHtml(scope)}-${key}`;
  if (field.type === "boolean") {
    return `
      <label class="config-field config-toggle" for="${id}">
        <span>
          <strong>${escapeHtml(field.label || field.key)}</strong>
          <small>${escapeHtml(field.note || "")}</small>
        </span>
        <input id="${id}" data-config-key="${key}" type="checkbox" ${field.value ? "checked" : ""}>
      </label>`;
  }
  const type = field.type === "number" ? "number" : "text";
  const attrs = [
    `id="${id}"`,
    `data-config-key="${key}"`,
    `type="${type}"`,
    field.type === "bytes" ? `inputmode="text"` : "",
    field.min !== undefined ? `min="${escapeHtml(field.min)}"` : "",
    field.max !== undefined ? `max="${escapeHtml(field.max)}"` : "",
    `value="${escapeHtml(field.value ?? "")}"`,
  ].filter(Boolean).join(" ");
  return `
    <label class="config-field" for="${id}">
      <span>${escapeHtml(field.label || field.key)}</span>
      <input ${attrs}>
      <small>${escapeHtml(field.note || "")}</small>
    </label>`;
}

function renderConfigEditor(targetId, fields, emptyText) {
  const target = $(targetId);
  if (!fields.length) {
    target.innerHTML = `<div class="empty-state">${escapeHtml(emptyText)}</div>`;
    return;
  }
  const groups = configFieldsByGroup(fields);
  target.innerHTML = Array.from(groups.entries()).map(([group, items]) => `
    <section class="config-group">
      <h4 class="config-group-title">${escapeHtml(group)}</h4>
      <div class="config-group-grid">
        ${items.map((field) => configFieldHtml(field, targetId)).join("")}
      </div>
    </section>`).join("");
}

function configPayloadFrom(rootId) {
  const payload = {};
  const root = $(rootId);
  if (!root) return payload;
  root.querySelectorAll("[data-config-key]").forEach((input) => {
    const key = input.dataset.configKey;
    if (!key) return;
    if (input.type === "checkbox") payload[key] = input.checked;
    else if (input.type === "number") payload[key] = Number(input.value);
    else payload[key] = input.value;
  });
  return payload;
}

function systemConfigFields(config) {
  return (config.editable || []).filter((field) => field.group !== "告警");
}

function alertConfigFields(config) {
  return (config.editable || []).filter((field) => field.group === "告警");
}

function renderSystemConfig(config) {
  renderConfigEditor("editable-config-form", systemConfigFields(config), "暂无可编辑配置。");
  $("config-result").textContent = "这些配置不包含 token、密码或 API Key，保存后立即生效。";
}

function renderAlertConfig(config) {
  renderConfigEditor("alert-config-form", alertConfigFields(config), "暂无可编辑的告警配置。");
  $("alert-config-result").textContent = "阈值留空或填 0 表示关闭；静默时段格式如 23:00-07:00。";
}

async function saveConfigFromForm({ formId, resultId, buttonId, afterSave }) {
  $(resultId).textContent = "保存配置中...";
  $(buttonId).disabled = true;
  try {
    const data = await api("/api/system/config", {
      method: "POST",
      body: JSON.stringify(configPayloadFrom(formId)),
    });
    if (afterSave) await afterSave(data);
    $(resultId).textContent = "配置已保存并立即生效。";
  } catch (error) {
    $(resultId).textContent = friendlyError(error.message);
  } finally {
    $(buttonId).disabled = false;
  }
}

async function saveSystemConfig() {
  await saveConfigFromForm({
    formId: "editable-config-form",
    resultId: "config-result",
    buttonId: "save-config-btn",
    afterSave: async (data) => {
      renderSystemConfig(data.config || {});
      await loadTaskRuns("system-task-runs", $("task-run-filter").value, DISPLAY_LIMITS.taskRuns);
    },
  });
}

async function saveAlertConfig() {
  await saveConfigFromForm({
    formId: "alert-config-form",
    resultId: "alert-config-result",
    buttonId: "save-alert-config-btn",
    afterSave: async (data) => {
      renderAlertConfig(data.config || {});
      await loadAlerts();
    },
  });
}

function setMaintenanceBusy(busy) {
  $("prune-task-runs-btn").disabled = busy;
  $("vacuum-db-btn").disabled = busy;
}

async function pruneTaskRuns() {
  $("maintenance-result").textContent = "清理旧运行记录中...";
  setMaintenanceBusy(true);
  try {
    const data = await postJson("/api/system/maintenance/prune-task-runs", {});
    renderMaintenanceStatus(data.maintenance || {});
    $("maintenance-result").textContent = `已清理 ${data.result?.deleted || 0} 条旧运行记录，当前剩余 ${data.result?.remaining || 0} 条。`;
    await loadTaskRuns("system-task-runs", $("task-run-filter").value, DISPLAY_LIMITS.taskRuns);
  } catch (error) {
    $("maintenance-result").textContent = friendlyError(error.message);
  } finally {
    setMaintenanceBusy(false);
  }
}

async function vacuumDb() {
  $("maintenance-result").textContent = "压缩 SQLite 中...";
  setMaintenanceBusy(true);
  try {
    const data = await postJson("/api/system/maintenance/vacuum", {});
    renderMaintenanceStatus(data.maintenance || {});
    $("maintenance-result").textContent = "数据维护完成，长期统计库状态正常。";
    await loadTaskRuns("system-task-runs", $("task-run-filter").value, DISPLAY_LIMITS.taskRuns);
  } catch (error) {
    $("maintenance-result").textContent = friendlyError(error.message);
  } finally {
    setMaintenanceBusy(false);
  }
}

function renderTrafficRange(data) {
  const nodes = data.nodes || [];
  const groups = data.groups || [];
  const visibleGroups = groups.length > DISPLAY_LIMITS.analyticsGroups ? groups.slice(-DISPLAY_LIMITS.analyticsGroups) : groups;
  const compactNodes = data.compact || nodes.some((node) => node.compact_other)
    ? { rows: nodes, hidden: Number(data.hidden_node_count || 0), hiddenTotal: 0 }
    : compactTrafficRows(nodes, DISPLAY_LIMITS.analyticsNodes);
  const topNodes = compactNodes.rows;
  const maxNode = Math.max(...topNodes.map((node) => byteValue(node, "total")), 1);
  const maxGroup = Math.max(...visibleGroups.map((group) => byteValue(group.total, "total")), 1);
  setInlineBadge("analytics-status-pill");
  $("traffic-range-result").innerHTML = `
    <div class="range-summary">
      ${miniCard("区间合计", byteText(data.total, "total"), `${escapeHtml(data.from)} -> ${escapeHtml(data.to)}`)}
      ${miniCard("下行 / 上行", `${byteText(data.total, "down")} / ${byteText(data.total, "up")}`, "区间累计")}
      ${miniCard("节点展示", `${Math.min(data.node_count ?? nodes.length, DISPLAY_LIMITS.analyticsNodes)}/${data.node_count ?? nodes.length}`, compactNodes.hidden ? `其余 ${compactNodes.hidden} 个已汇总` : "全部展示")}
      ${miniCard("趋势分组", `${visibleGroups.length}/${groups.length}`, `${data.day_count || 0} 天 · ${data.group || "daily"}`)}
    </div>
    <div class="analytics-grid">
      <article class="analytics-panel analytics-wide">
        <div class="panel-head compact-head">
          <div>
            <h3>分组趋势</h3>
          </div>
          <span class="soft-label">${escapeHtml(visibleGroups.length === groups.length ? (data.group || "daily") : `最近 ${visibleGroups.length} 组`)}</span>
        </div>
        <div class="analytics-axis">
          <span>0</span>
          <span>${escapeHtml(formatBytes(maxGroup / 2))}</span>
          <span>${escapeHtml(formatBytes(maxGroup))}</span>
        </div>
        <div class="analytics-group-list">
          ${visibleGroups.length ? visibleGroups.map((group) => `
            <div class="analytics-group-row">
              <span class="analytics-label" title="${escapeHtml(group.label)}">${escapeHtml(group.label)}</span>
              <span class="analytics-track">
                ${svgBar("total", byteValue(group.total, "total"), maxGroup, 12)}
              </span>
              <span class="analytics-value">${escapeHtml(byteText(group.total, "total"))}</span>
            </div>`).join("") : `<div class="empty-state">暂无分组数据。</div>`}
          ${groups.length > visibleGroups.length ? `<div class="compact-note">较早的 ${groups.length - visibleGroups.length} 组已隐藏，调整日期范围可查看。</div>` : ""}
        </div>
      </article>
      <article class="analytics-panel">
        <div class="panel-head compact-head">
          <div>
            <h3>节点贡献</h3>
          </div>
          <span class="soft-label">${compactNodes.hidden ? "Top + 其余" : `Top ${escapeHtml(String(topNodes.length || 0))}`}</span>
        </div>
        <div class="analytics-node-bars">
          ${topNodes.length ? topNodes.map((node, index) => `
            <${node.compact_other ? "div" : "button"} class="analytics-node-row ${node.compact_other ? "compact-other" : ""}" ${node.compact_other ? "" : `data-jump-node="${escapeHtml(node.uuid)}" title="跳到 ${escapeHtml(node.name)}"`}>
              <span class="analytics-rank">${node.compact_other ? "..." : index + 1}</span>
              <span class="analytics-node-main">
                <span class="analytics-node-title">${escapeHtml(node.name)}</span>
                <span class="analytics-track">
                  ${svgStackedBar(byteValue(node, "down"), byteValue(node, "up"), maxNode)}
                </span>
                <span class="tiny">下行 ${escapeHtml(byteText(node, "down"))} / 上行 ${escapeHtml(byteText(node, "up"))}</span>
              </span>
              <span class="analytics-value">${escapeHtml(byteText(node, "total"))}</span>
            </${node.compact_other ? "div" : "button"}>`).join("") : `<div class="empty-state">这个区间暂无节点汇总。</div>`}
        </div>
      </article>
    </div>`;
}

async function loadTrafficRange() {
  setDefaultRangeDates();
  $("traffic-range-result").innerHTML = `<div class="range-summary">${skelCards(4)}</div><div class="kt-skeleton kt-skel-chart"></div>`;
  setInlineBadge("analytics-status-pill", "查询中");
  const token = ++state.requestToken;
  try {
    const query = trafficRangeQuery();
    const data = await api(`/api/traffic/range?${query.toString()}`, { token });
    if (token !== state.requestToken || !data) return;
    renderTrafficRange(data);
  } catch (error) {
    if (token !== state.requestToken) return;
    setInlineBadge("analytics-status-pill", "异常", "bad");
    $("traffic-range-result").innerHTML = `<div class="empty-state">${escapeHtml(friendlyError(error.message))}</div>`;
  }
}

async function exportTrafficRangeCsv() {
  setDefaultRangeDates();
  const button = $("export-traffic-range-btn");
  button.disabled = true;
  setInlineBadge("analytics-status-pill", "导出中");
  try {
    const query = trafficRangeQuery();
    const response = await fetch(`/api/traffic/range/export.csv?${query.toString()}`, {
      credentials: "same-origin",
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new Error(friendlyError(payload?.error?.message || "导出失败"));
    }
    const blob = await response.blob();
    const filename = filenameFromDisposition(
      response.headers.get("content-disposition"),
      `komari-traffic-${$("traffic-range-from").value}-${$("traffic-range-to").value}.csv`,
    );
    downloadBlob(blob, filename);
    setInlineBadge("analytics-status-pill", "已导出");
    window.clearTimeout(state.exportSuccessTimer);
    state.exportSuccessTimer = window.setTimeout(() => setInlineBadge("analytics-status-pill"), 1800);
  } catch (error) {
    const message = friendlyError(error.message);
    setInlineBadge("analytics-status-pill", "导出失败", "bad");
    $("analytics-status-pill").title = message;
    console.warn(message);
  } finally {
    button.disabled = false;
  }
}

async function loadAnalyticsPage() {
  setDefaultRangeDates();
  await loadTrafficRange();
}

async function loadSystemPage() {
  setSkel("system-summary", skelCards(4));
  setSkel("system-services", skelRows(3));
  try {
    const [system, config] = await Promise.all([
      api("/api/system/status"),
      api("/api/system/config"),
    ]);
    renderSystemStatus(system);
    renderSystemConfig(config);
    await loadTaskRuns("system-task-runs", $("task-run-filter").value, DISPLAY_LIMITS.taskRuns);
  } catch (error) {
    $("system-summary").innerHTML = `<div class="empty-state">${escapeHtml(friendlyError(error.message))}</div>`;
  }
}

function normalizeLoadOptions(options = {}) {
  if (typeof options === "boolean") return { includeOverview: options };
  return options || {};
}

async function loadCurrentRoute(options = {}) {
  const loadOptions = normalizeLoadOptions(options);
  const route = state.route;
  const routeToken = ++state.routeToken;
  const hasLoaded = Boolean(state.routeLoaded[route]);
  const manual = Boolean(loadOptions.manual);
  const includeOverview = Boolean(loadOptions.includeOverview);
  const age = Date.now() - (state.routeLoadedAt[route] || 0);
  const isFresh = hasLoaded && age < ROUTE_REVALIDATE_MS;

  // Stale-while-revalidate: a route that's already loaded and still fresh is
  // shown instantly from its existing DOM — no network call, no skeleton, no
  // re-render flash. We only fetch when the data is missing, gone stale, or the
  // user explicitly refreshed (manual) / asked to include the overview.
  if (hasLoaded && isFresh && !manual && !includeOverview) {
    updateStatus("已同步", true);
    cancelRouteProgress();
    // The cached DOM is reused as-is, but node selection is driven by the URL
    // (?node=) and may differ from when the route was last rendered — e.g. a
    // "jump to node" from the overview. Re-sync it without any refetch.
    if (route === "/nodes") syncNodeSelectionFromUrl();
    return;
  }

  // A background revalidation (loaded before, just stale) stays silent: keep the
  // cached content on screen and swap it seamlessly when fresh data arrives.
  const showProgress = manual || !hasLoaded;
  updateStatus(hasLoaded ? "同步中" : "加载中", true);
  if (showProgress) {
    startRouteProgress({ delay: manual ? 80 : 160 });
  } else {
    cancelRouteProgress();
  }
  if (!hasLoaded) showRouteSkeleton(route);
  try {
    if (route === "/" || includeOverview) await loadOverview();
    if (route === "/nodes") await loadNodes(state.nodesHours);
    if (route === "/alerts") await loadAlerts();
    if (route === "/telegram") await loadTelegramStatus();
    if (route === "/ai") await loadAiStatus();
    if (route === "/analytics") await loadAnalyticsPage();
    if (route === "/system") await loadSystemPage();
    if (routeToken !== state.routeToken) return;
    state.routeLoaded[route] = true;
    state.routeLoadedAt[route] = Date.now();
    markRouteLoaded(route);
    updateStatus("已同步", true);
  } catch (error) {
    if (routeToken !== state.routeToken) return;
    if (error.status === 401) {
      showLoginView();
      return;
    }
    updateStatus(friendlyError(error.message), false);
  } finally {
    if (routeToken === state.routeToken) {
      clearRouteSkeleton();
      if (showProgress) stopRouteProgress();
    }
  }
}

async function checkSession() {
  const data = await api("/api/auth/session");
  state.authenticated = Boolean(data.authenticated);
  if (state.authenticated) {
    setVisible("login-view", false);
    setVisible("app-view", true);
    await loadCurrentRoute();
  } else {
    showLoginView();
  }
}

async function doLogin(event) {
  event.preventDefault();
  const button = event.target.querySelector('button[type="submit"]');
  if (button) button.disabled = true;
  unlockLoginFields();
  $("login-error").textContent = "";
  try {
    await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: $("login-username").value,
        password: $("login-password").value,
        remember: loginRememberEnabled(),
      }),
    });
    state.authenticated = true;
    lockAndClearLoginFields();
    setVisible("login-view", false);
    setVisible("app-view", true);
    await loadCurrentRoute();
  } catch (error) {
    $("login-error").textContent = friendlyError(error.message);
  } finally {
    if (button) button.disabled = false;
  }
}

function canUseDesktopSidebar() {
  return window.matchMedia("(min-width: 921px)").matches;
}

function loadSidebarPreference() {
  try {
    state.sidebarCollapsed = window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === "true";
  } catch (_error) {
    state.sidebarCollapsed = false;
  }
}

function saveSidebarPreference() {
  try {
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, state.sidebarCollapsed ? "true" : "false");
  } catch (_error) {
    // Ignore storage failures in private browsing or restricted WebViews.
  }
}

function applySidebarState() {
  const collapsed = state.sidebarCollapsed && canUseDesktopSidebar();
  $("app-view").classList.toggle("sidebar-collapsed", collapsed);
  const toggle = $("sidebar-toggle");
  toggle.setAttribute("aria-expanded", String(!collapsed));
  toggle.setAttribute("aria-label", collapsed ? "展开导航" : "收起导航");
  toggle.setAttribute("title", collapsed ? "展开导航" : "收起导航");
}

function bindIconFallbacks() {
  document.querySelectorAll(".brand-icon").forEach((img) => {
    img.addEventListener("error", () => {
      img.hidden = true;
      const fallback = img.nextElementSibling;
      if (fallback) fallback.hidden = false;
    });
  });
}

function bindEvents() {
  // Global event delegation
  document.addEventListener("click", (e) => {
    const jumpBtn = e.target.closest("[data-jump-node]");
    if (jumpBtn) {
      jumpToNode(jumpBtn.dataset.jumpNode);
      return;
    }

    const detailActionBtn = e.target.closest("[data-detail-action]");
    if (detailActionBtn) {
      e.stopPropagation();
      const uuid = detailActionBtn.dataset.uuid;
      const action = detailActionBtn.dataset.detailAction;
      if (action === "open") openKomariNode(uuid);
      return;
    }

    const bindingActionBtn = e.target.closest("[data-binding-action]");
    if (bindingActionBtn) {
      e.stopPropagation();
      const uuid = bindingActionBtn.dataset.uuid;
      const action = bindingActionBtn.dataset.bindingAction;
      saveNodeBinding(uuid, { clear: action === "auto", button: bindingActionBtn });
      return;
    }

    // Node table row clicks
    const nodeRow = e.target.closest("#nodes-table tr[data-uuid]");
    if (nodeRow && !e.target.closest("button,a,select")) {
      if (String(state.selectedNodeUuid) === String(nodeRow.dataset.uuid)) {
        collapseNodeDetail();
        return;
      }
      selectNode(nodeRow.dataset.uuid, { scroll: false });
      return;
    }

    // Node action buttons
    const nodeActionBtn = e.target.closest("[data-node-action]");
    if (nodeActionBtn) {
      e.stopPropagation();
      const uuid = nodeActionBtn.dataset.uuid;
      const action = nodeActionBtn.dataset.nodeAction;
      if (action === "open") openKomariNode(uuid);
      if (action === "detail") {
        if (String(state.selectedNodeUuid) === String(uuid)) {
          collapseNodeDetail();
        } else {
          selectNode(uuid, { scroll: true });
        }
      }
      return;
    }

    // Schedule action buttons
    const scheduleActionBtn = e.target.closest("[data-schedule-action]");
    if (scheduleActionBtn) {
      const id = scheduleActionBtn.dataset.scheduleId;
      const action = scheduleActionBtn.dataset.scheduleAction;
      if (action === "edit") editSchedule(id);
      if (action === "delete") deleteSchedule(id);
      if (action === "run") runScheduleNow(id);
      return;
    }

    // Alert history pagination buttons
    const historyPrevBtn = e.target.closest("#history-prev-btn");
    const historyNextBtn = e.target.closest("#history-next-btn");
    if (historyPrevBtn || historyNextBtn) {
      const target = $("alert-history-list");
      const currentPage = Number(target.dataset.historyPage || 1);
      if (historyPrevBtn) target.dataset.historyPage = String(currentPage - 1);
      if (historyNextBtn) target.dataset.historyPage = String(currentPage + 1);
      loadAlerts();
      return;
    }
  });

  $("login-form").addEventListener("submit", doLogin);
  $("login-remember").addEventListener("change", handleLoginRememberChange);
  loginFields().forEach((input) => {
    input.addEventListener("pointerdown", unlockLoginFields);
    input.addEventListener("focus", unlockLoginFields);
    input.addEventListener("keydown", unlockLoginFields);
  });
  $("refresh-btn").addEventListener("click", () => loadCurrentRoute({ manual: true }));
  $("logout-btn").addEventListener("click", async () => {
    await postJson("/api/auth/logout", {}).catch(() => null);
    showLoginView();
  });
  $("sidebar-toggle").addEventListener("click", () => {
    state.sidebarCollapsed = !state.sidebarCollapsed;
    saveSidebarPreference();
    applySidebarState();
  });
  document.querySelectorAll("#theme-switch [data-theme]").forEach((button) => {
    button.addEventListener("click", () => {
      state.themeMode = normalizeThemeMode(button.dataset.theme);
      saveThemePreference();
      applyThemeMode();
    });
  });
  window.addEventListener("resize", applySidebarState);
  window.addEventListener("popstate", () => navigateRoute(`${window.location.pathname}${window.location.search}`, { load: true, scroll: false }));
  document.querySelectorAll(".nav-link[data-route], .brand[data-route]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      navigateRoute(link.dataset.route, { push: true });
    });
  });
  document.querySelectorAll("#range-tabs button").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("#range-tabs button").forEach((b) => b.classList.remove("active"));
      button.classList.add("active");
      loadNodes(Number(button.dataset.hours));
    });
  });
  $("check-alerts-btn").addEventListener("click", () => runAlertCheck(false));
  $("notify-alerts-btn").addEventListener("click", () => runAlertCheck(true));
  $("mute-btn").addEventListener("click", () => muteAlerts(Number($("mute-hours").value || 1)));
  document.querySelectorAll(".quick-mute").forEach((button) => {
    button.addEventListener("click", () => muteAlerts(Number(button.dataset.hours || 1)));
  });
  $("unmute-btn").addEventListener("click", async () => {
    $("alert-result").textContent = "解除静默中...";
    try {
      await postJson("/api/alerts/unmute", {});
      $("alert-result").textContent = "已解除告警静默。";
      await loadAlerts();
    } catch (error) {
      $("alert-result").textContent = friendlyError(error.message);
    }
  });
  $("preview-report-btn").addEventListener("click", previewReport);
  $("send-report-btn").addEventListener("click", sendReport);
  $("save-schedule-btn").addEventListener("click", saveSchedule);
  $("reset-schedule-btn").addEventListener("click", resetScheduleForm);
  $("schedule-scope").addEventListener("change", updateScheduleFormVisibility);
  $("tg-test-btn").addEventListener("click", async () => {
    $("telegram-result").textContent = "测试发送中...";
    try {
      const data = await postJson("/api/telegram/test", {});
      $("telegram-result").textContent = `测试消息已发送到 ${data.chat}`;
    } catch (error) {
      $("telegram-result").textContent = friendlyError(error.message);
    }
  });
  $("ai-ask-btn").addEventListener("click", () => askAi());
  document.querySelectorAll(".ai-prompt").forEach((button) => {
    button.addEventListener("click", () => fillAiPrompt(button));
  });
  $("ai-history-toggle").addEventListener("click", () => {
    const toggle = $("ai-history-toggle");
    const body = $("ai-history-body");
    const expanded = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", !expanded);
    body.hidden = expanded;
  });
  $("load-traffic-range-btn").addEventListener("click", loadTrafficRange);
  $("export-traffic-range-btn").addEventListener("click", exportTrafficRangeCsv);
  $("traffic-range-group").addEventListener("change", loadTrafficRange);
  $("task-run-filter").addEventListener("change", () => loadTaskRuns("system-task-runs", $("task-run-filter").value, DISPLAY_LIMITS.taskRuns));
  $("save-config-btn").addEventListener("click", saveSystemConfig);
  $("save-alert-config-btn").addEventListener("click", saveAlertConfig);
  $("prune-task-runs-btn").addEventListener("click", pruneTaskRuns);
  $("vacuum-db-btn").addEventListener("click", vacuumDb);
  resetScheduleForm();
  setDefaultRangeDates();
}

function initRoute() {
  const initialRoute = normalizeRoute(window.location.pathname);
  const initialUrl = routeUrl(`${window.location.pathname}${window.location.search}`);
  if (initialUrl !== `${window.location.pathname}${window.location.search}`) {
    window.history.replaceState({ route: initialRoute }, "", initialUrl);
  }
  showRoute(initialRoute);
}

loadThemePreference();
loadSidebarPreference();
bindIconFallbacks();
bindEvents();
initRoute();
applyThemeMode();
applySidebarState();
checkSession().catch((error) => {
  showLoginView(friendlyError(error.message));
});
