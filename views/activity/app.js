/* Game activity sync view: single ES module, no dependencies, no build step.
 * Dynamic strings are written with textContent only; no HTML string injection. */

const BRIDGE = window.AstrBotPluginView || window.AstrBotPluginPage || null;
const BRIDGE_TIMEOUT_MS = 8000, REQUEST_TIMEOUT_MS = 15000, DEFAULT_POLL_SECONDS = 30;
const FAST_POLL_MS = 5000, FAST_POLL_MAX_MS = 180000, COOLDOWN_MS = 30000;
const SEARCH_DEBOUNCE_MS = 300, SEARCH_MAX_LENGTH = 64, TOAST_MS = 4000;
const BACKOFF_BASE_MS = 30000, BACKOFF_MAX_MS = 300000;
const BRIDGE_FATAL = {
  kind: "fatal", titleKey: "errors.bridgeTitle", titleFallback: "未检测到 AstrBot 视图桥接",
  bodyKey: "errors.bridgeBody", bodyFallback: "请在 AstrBot WebUI 的插件详情页中打开本视图；若问题持续，请重载插件后刷新。",
};
const TABS = ["overview", "activities", "sources", "reminders"];
const GAME_IDS = ["genshin", "starrail", "zenless", "arknights", "endfield", "wuthering_waves"];
const ACTIVITY_STATUSES = ["upcoming", "active", "ending_soon", "ended", "removed"];
const SOURCE_STATUSES = ["ok", "stale", "error", "disabled"];
const JOB_STATUSES = ["pending", "sent", "skipped", "failed"];
const STATUS_RANK = { ending_soon: 0, active: 1, upcoming: 2, ended: 3, removed: 4 };
const STATUS_TONE = { active: "ok", ending_soon: "warn", upcoming: "info", ended: "neutral", removed: "removed" };
const SOURCE_TONE = { ok: "ok", stale: "warn", error: "danger", disabled: "neutral" };
const JOB_TONE = { pending: "info", sent: "ok", skipped: "neutral", failed: "danger" };
const SYNC_TONE = { idle: "neutral", running: "accent", error: "danger" };
const LIMITS = [50, 100, 200];
const COLOR_PATTERN = /^(#[0-9a-f]{3}|#[0-9a-f]{6}|#[0-9a-f]{8}|rgba?\([^()]*\)|hsla?\([^()]*\))$/i;

const state = {
  boot: "loading", locale: "zh-CN", showTimezone: false, pageTitle: "", isDark: false, tab: "overview",
  paused: false, polling: false, failures: 0, lastError: null, games: null,
  overview: null, overviewAt: 0, lastGoodAt: 0, staleAt: 0,
  activities: { mounted: false, data: null, loading: false, generation: 0, limit: 50,
    filters: { game: "", status: "", q: "" }, listEl: null, countEl: null, activeKey: "" },
  sources: { data: null, loading: false, expanded: new Set(), pendingSource: "" },
  reminders: { data: null, loading: false, expanded: new Set() },
  sync: { inflight: false, fast: false, fastUntil: 0, cooldownUntil: 0 },
  banner: null, detailTrigger: null, detailKey: "", detailItem: null, confirm: null,
  refreshing: false, lastRefreshAt: 0, offContext: null,
  inflight: new Map(), chains: new Map(), warned: new Set(),
};

const timers = { poll: 0, countdown: 0, toast: 0, cooldown: 0, search: 0 };
const desktopQuery = typeof window.matchMedia === "function" ? window.matchMedia("(min-width: 1100px)") : { matches: false };

/* DOM helpers */
function $(id) { return document.getElementById(id); }
function panelEl(tab) { return $("panel-" + tab); }

function appendChildren(node, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) { appendChildren(node, child); continue; }
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "text") node.textContent = String(value);
      else if (key === "className") node.className = String(value);
      else if (key === "dataset") Object.assign(node.dataset, value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
  }
  appendChildren(node, children);
  return node;
}

function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

function svgIcon(name, className) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", className || "c-icon");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#" + name);
  svg.append(use);
  return svg;
}

function attrEsc(value) { return String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"'); }

function captureFocus(root) {
  const active = document.activeElement;
  if (!active || !root.contains(active)) return { key: "", scrollY: window.scrollY };
  const holder = active.closest("[data-focus-key]");
  return { key: holder ? holder.dataset.focusKey : "", scrollY: window.scrollY };
}

function restoreFocus(root, snapshot) {
  if (!snapshot) return;
  if (snapshot.key) {
    const node = root.querySelector('[data-focus-key="' + attrEsc(snapshot.key) + '"]');
    if (node) node.focus({ preventScroll: true });
  }
  if (snapshot.scrollY > 0) window.scrollTo(0, snapshot.scrollY);
}

/* i18n and formatting */
function t(key, fallback) {
  const text = fallback === undefined ? "" : fallback;
  if (!BRIDGE) return text;
  try {
    const value = BRIDGE.t("views.activity." + key, text);
    return typeof value === "string" && value ? value : text;
  } catch (error) {
    return text;
  }
}

function fmt(key, vars, fallback) {
  return t(key, fallback).replace(/\{(\w+)\}/g, (match, name) => {
    if (vars && Object.prototype.hasOwnProperty.call(vars, name)) return String(vars[name]);
    return match;
  });
}

let formatterCache = { key: "", formatter: null };

function dateFormatter() {
  const key = state.locale + "|" + state.showTimezone;
  if (formatterCache.key === key && formatterCache.formatter) return formatterCache.formatter;
  const options = { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false };
  if (state.showTimezone) options.timeZoneName = "short";
  let formatter;
  try { formatter = new Intl.DateTimeFormat(state.locale, options); }
  catch (error) { formatter = new Intl.DateTimeFormat("zh-CN", options); }
  formatterCache = { key, formatter };
  return formatter;
}

function formatDate(date) {
  try { return dateFormatter().format(date); }
  catch (error) { return date.toISOString(); }
}

function fromUnixSeconds(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const date = new Date(value * 1000);
  return Number.isNaN(date.getTime()) ? null : date;
}

function fromIso(value) {
  if (typeof value !== "string" || !value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function timeText(value) {
  const date = fromUnixSeconds(value);
  return date ? formatDate(date) : t("common.na", "—");
}

function timeTextOfMs(milliseconds) {
  if (typeof milliseconds !== "number" || !Number.isFinite(milliseconds) || milliseconds <= 0) return t("common.na", "—");
  return formatDate(new Date(milliseconds));
}

function isoText(value) {
  const date = fromIso(value);
  return date ? formatDate(date) : t("common.na", "—");
}

function timeNode(value) {
  const node = el("time");
  const date = fromUnixSeconds(value);
  if (date) { node.dateTime = date.toISOString(); node.textContent = formatDate(date); }
  else node.textContent = t("common.na", "—");
  return node;
}

function isoNode(value) {
  const date = fromIso(value);
  if (!date) return el("span", { text: t("common.na", "—") });
  const node = el("time");
  node.dateTime = date.toISOString();
  node.textContent = formatDate(date);
  return node;
}

function gameFromApi(id) {
  if (!Array.isArray(state.games)) return "";
  const found = state.games.find((game) => game.gameId === id);
  return found && found.name ? found.name : "";
}

function gameLabel(id) {
  if (!id) return t("common.na", "—");
  if (GAME_IDS.includes(id)) return t("games." + id, gameFromApi(id) || id);
  return gameFromApi(id) || String(id);
}

function warnUnknown(kind, value) {
  const key = kind + ":" + value;
  if (state.warned.has(key)) return;
  state.warned.add(key);
  console.warn("[game-activity-view] unexpected " + kind + " value", value);
}

function statusLabel(value) {
  if (ACTIVITY_STATUSES.includes(value)) return t("status." + value, value);
  warnUnknown("activity", value);
  return String(value || t("common.na", "—"));
}

function sourceStatusLabel(value) {
  if (SOURCE_STATUSES.includes(value)) return t("sourceStatus." + value, value);
  warnUnknown("source", value);
  return String(value || t("common.na", "—"));
}

function jobStatusLabel(value) {
  if (JOB_STATUSES.includes(value)) return t("jobStatus." + value, value);
  warnUnknown("job", value);
  return String(value || t("jobStatus.unknown", "未知状态"));
}

function syncStatusLabel(value) {
  if (value === "idle" || value === "running" || value === "error") return t("sync.chip." + value, value);
  warnUnknown("sync", value);
  return String(value || t("common.na", "—"));
}

function statusChip(tone, label) {
  const chip = el("span", { className: "c-status c-status--" + (tone || "neutral") });
  chip.append(el("span", { className: "c-status__dot", "aria-hidden": "true" }), el("span", { text: label }));
  return chip;
}

function syncStatusChip(status) { return statusChip(SYNC_TONE[status] || "neutral", syncStatusLabel(status)); }

/* Payload normalisation: the only place that reads raw response shapes. */
function str(value) {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  return String(value);
}

function num(value, fallback) {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback || 0;
}

function numOrNull(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function clampLimit(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 50;
  return Math.min(200, Math.max(1, Math.round(parsed)));
}

function normalizeGames(raw) {
  const payload = raw && typeof raw === "object" ? raw : {};
  const list = Array.isArray(payload.games) ? payload.games : [];
  return list.map((entry) => {
    if (typeof entry === "string") return { gameId: entry, name: "" };
    const game = entry && typeof entry === "object" ? entry : {};
    return { gameId: str(game.game_id), name: str(game.name) };
  }).filter((game) => Boolean(game.gameId));
}

function normalizeOverview(raw) {
  const payload = raw && typeof raw === "object" ? raw : {};
  const counts = payload.counts && typeof payload.counts === "object" ? payload.counts : {};
  const health = payload.source_health && typeof payload.source_health === "object" ? payload.source_health : {};
  const next = payload.next_reminder && typeof payload.next_reminder === "object" ? payload.next_reminder : null;
  return {
    schema: num(payload.schema_version, 1),
    counts: {
      activities: num(counts.activities), active: num(counts.active), endingSoon: num(counts.ending_soon),
      upcoming: num(counts.upcoming), ended: num(counts.ended), removed: num(counts.removed),
      games: num(counts.games), sources: num(counts.sources), remindersPending: num(counts.reminders_pending),
    },
    health: { ok: num(health.ok), stale: num(health.stale), error: num(health.error), disabled: num(health.disabled), total: num(health.total) },
    syncStatus: str(payload.sync_status) || "idle",
    lastSyncAt: numOrNull(payload.last_sync_at),
    syncError: str(payload.sync_error),
    removed24h: num(payload.removed_24h),
    nextReminder: next ? {
      activityName: str(next.activity_name), gameId: str(next.game_id),
      fireAtIso: str(next.fire_at_iso), thresholdDays: numOrNull(next.threshold_days),
    } : null,
    pollSeconds: Math.min(300, Math.max(10, num(payload.poll_seconds, DEFAULT_POLL_SECONDS))),
    showTimezone: payload.show_timezone === true,
  };
}

function normalizeActivity(raw) {
  const item = raw && typeof raw === "object" ? raw : {};
  const endDate = fromIso(str(item.end_iso));
  const endEpoch = numOrNull(item.end_epoch);
  return {
    key: str(item.key), rawId: str(item.raw_id), sourceId: str(item.source_id), gameId: str(item.game_id),
    name: str(item.name), description: str(item.description),
    kind: item.kind === null || item.kind === undefined ? "" : str(item.kind),
    startIso: str(item.start_iso), endIso: str(item.end_iso),
    startEpoch: numOrNull(item.start_epoch), endEpoch,
    coverUrl: str(item.cover_url), contentHash: str(item.content_hash),
    status: str(item.status) || "active", isPrimary: item.is_primary === true,
    mergedSources: Array.isArray(item.merged_sources) ? item.merged_sources.map(str) : [],
    firstSeenAt: numOrNull(item.first_seen_at), lastSeenAt: numOrNull(item.last_seen_at),
    removedAt: numOrNull(item.removed_at),
    endTs: endDate ? endDate.getTime() : endEpoch === null ? null : endEpoch * 1000,
  };
}

function compareActivities(left, right) {
  const rankLeft = STATUS_RANK[left.status] === undefined ? 9 : STATUS_RANK[left.status];
  const rankRight = STATUS_RANK[right.status] === undefined ? 9 : STATUS_RANK[right.status];
  if (rankLeft !== rankRight) return rankLeft - rankRight;
  const endLeft = left.endTs === null ? Number.MAX_SAFE_INTEGER : left.endTs;
  const endRight = right.endTs === null ? Number.MAX_SAFE_INTEGER : right.endTs;
  if (endLeft !== endRight) return endLeft - endRight;
  return left.key < right.key ? -1 : left.key > right.key ? 1 : 0;
}

function normalizeActivities(raw) {
  const payload = raw && typeof raw === "object" ? raw : {};
  const list = Array.isArray(payload.activities) ? payload.activities.map(normalizeActivity) : [];
  list.sort(compareActivities);
  const items = list.slice(0, 200);
  return { schema: num(payload.schema_version, 1), total: num(payload.total, items.length), items };
}

function normalizeSources(raw) {
  const payload = raw && typeof raw === "object" ? raw : {};
  const list = Array.isArray(payload.sources) ? payload.sources : [];
  return {
    schema: num(payload.schema_version, 1),
    items: list.map((entry) => {
      const item = entry && typeof entry === "object" ? entry : {};
      return {
        sourceId: str(item.source_id), gameId: str(item.game_id), url: str(item.url), adapter: str(item.adapter),
        priority: numOrNull(item.priority), enabled: item.enabled === true, status: str(item.status) || "ok",
        lastChecked: numOrNull(item.last_checked), lastSuccess: numOrNull(item.last_success),
        httpStatus: numOrNull(item.http_status), etagPresent: item.etag_present === true,
        lastModifiedPresent: item.last_modified_present === true,
        consecutiveFailures: num(item.consecutive_failures), lastError: str(item.last_error),
        activityCount: num(item.activity_count), nextRetryAt: numOrNull(item.next_retry_at),
      };
    }),
  };
}

function normalizeReminders(raw) {
  const payload = raw && typeof raw === "object" ? raw : {};
  const subscriptions = payload.subscriptions && typeof payload.subscriptions === "object" ? payload.subscriptions : {};
  const jobs = Array.isArray(payload.jobs) ? payload.jobs : [];
  return {
    schema: num(payload.schema_version, 1),
    subscriptionsCount: num(subscriptions.count),
    jobs: jobs.map((entry) => {
      const job = entry && typeof entry === "object" ? entry : {};
      return {
        key: str(job.key), gameId: str(job.game_id), sourceId: str(job.source_id), rawId: str(job.raw_id),
        activityName: str(job.activity_name), thresholdDays: numOrNull(job.threshold_days),
        fireAtIso: str(job.fire_at_iso), status: str(job.status), attempts: num(job.attempts),
        sentAt: numOrNull(job.sent_at), lastError: str(job.last_error),
      };
    }),
  };
}

/* Bridge requests */
function paramKey(params) {
  try { return JSON.stringify(params || {}); }
  catch (error) { return ""; }
}

function unwrapPayload(value) {
  if (value && typeof value === "object" && !Array.isArray(value) && value.data !== undefined) return value.data;
  return value;
}

function withTimeout(promise, milliseconds, kind) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      const error = new Error("timeout");
      error.kind = kind || "timeout";
      reject(error);
    }, milliseconds);
    promise.then((value) => { clearTimeout(timer); resolve(value); }, (error) => { clearTimeout(timer); reject(error); });
  });
}

async function apiGet(endpoint, params) {
  if (!BRIDGE) throw new Error("unauthorized: bridge unavailable");
  const key = paramKey(params);
  const running = state.inflight.get(endpoint);
  if (running && running.key === key && running.promise) return running.promise;
  const previous = state.chains.get(endpoint) || Promise.resolve();
  const run = previous.catch(() => undefined)
    .then(() => withTimeout(BRIDGE.apiGet(endpoint, params), REQUEST_TIMEOUT_MS))
    .then(unwrapPayload);
  state.chains.set(endpoint, run);
  state.inflight.set(endpoint, { key, promise: run });
  try { return await run; }
  finally {
    const current = state.inflight.get(endpoint);
    if (current && current.promise === run) state.inflight.delete(endpoint);
  }
}

async function apiPost(endpoint, body) {
  if (!BRIDGE) throw new Error("unauthorized: bridge unavailable");
  return unwrapPayload(await withTimeout(BRIDGE.apiPost(endpoint, body), REQUEST_TIMEOUT_MS));
}

function messageOf(error) {
  if (!error) return "";
  if (typeof error.message === "string" && error.message) return error.message;
  return String(error);
}

function classifyError(error) {
  const message = messageOf(error);
  if (error && error.kind === "timeout") return "timeout";
  if (!message) return "server";
  if (/^unauthorized:/.test(message) || /(401|403|unauthor|forbidden)/i.test(message) || /(登录|未登录)/.test(message)) return "auth";
  if (/^sync_rate_limited:/.test(message) || /(429|too many|rate.?limit|稍后|频率)/i.test(message)) return "rate";
  if (/^sync_already_running:/.test(message) || /(409|already|running|进行中)/i.test(message)) return "conflict";
  if (/^(invalid_parameter|unknown_game):/.test(message) || /(400|404|invalid|unknown.?game)/i.test(message)) return "invalid";
  if (/^internal_error:/.test(message) || /(5\d\d|server|internal|服务器)/i.test(message)) return "server";
  return "server";
}

/* Error banner, stale notice, toast */
function renderBanner() {
  const region = $("error-region");
  if (!region) return;
  clear(region);
  const banner = state.banner;
  if (!banner) { region.hidden = true; stopCountdown(); return; }
  region.hidden = false;
  const title = t(banner.titleKey, banner.titleFallback || "");
  const body = banner.bodyKey ? fmt(banner.bodyKey, banner.vars, banner.bodyFallback || "") : banner.message || "";
  const retry = el("button", { className: "c-btn c-btn--small", type: "button", text: t("errors.bannerRetry", "重试") });
  retry.addEventListener("click", () => { clearBanner(); void manualRefresh(); });
  const close = el("button", { className: "c-iconbtn", type: "button", "aria-label": t("common.close", "关闭") });
  close.append(svgIcon("i-close"));
  close.addEventListener("click", () => clearBanner());
  const box = el("div", { className: "c-error", role: "alert" });
  box.append(
    svgIcon("i-alert", "c-icon c-error__icon"),
    el("div", { className: "c-error__body" },
      el("p", { className: "c-error__title", text: title }),
      body ? el("p", { className: "c-error__text", text: body }) : null),
    el("div", { className: "c-error__actions" }, retry, el("span", { className: "c-error__count", dataset: { bannerCount: "1" } }), close),
  );
  region.append(box);
  startCountdown();
}

function setBanner(banner) { state.banner = banner || null; renderBanner(); }
function clearBanner() { if (state.banner) setBanner(null); }

function stopCountdown() {
  if (timers.countdown) { clearInterval(timers.countdown); timers.countdown = 0; }
}

function startCountdown() {
  stopCountdown();
  const banner = state.banner;
  if (!banner || !banner.retryAt) return;
  const tick = () => {
    if (!state.banner || !state.banner.retryAt) { stopCountdown(); return; }
    const region = $("error-region");
    const node = region ? region.querySelector("[data-banner-count]") : null;
    if (!node) { stopCountdown(); return; }
    const left = Math.max(0, Math.ceil((state.banner.retryAt - Date.now()) / 1000));
    node.textContent = fmt("errors.bannerNextRetry", { seconds: left }, "将在 {seconds} 秒后重试");
    if (left <= 0) stopCountdown();
  };
  tick();
  timers.countdown = setInterval(tick, 1000);
}

function renderStaleNotice() {
  const node = $("stale-notice");
  if (!node) return;
  if (!state.staleAt) { node.hidden = true; node.textContent = ""; return; }
  node.hidden = false;
  node.textContent = fmt("errors.staleData", { time: timeTextOfMs(state.staleAt) }, "显示的是 {time} 的缓存数据");
}

function showToast(message) {
  const region = $("toast-region");
  if (!region) return;
  if (timers.toast) { clearTimeout(timers.toast); timers.toast = 0; }
  clear(region);
  if (!message) return;
  const close = el("button", { className: "c-toast__close", type: "button", "aria-label": t("toast.dismiss", "关闭通知"), text: "×" });
  close.addEventListener("click", () => clear(region));
  region.append(el("div", { className: "c-toast" }, el("span", { className: "c-toast__text", text: message }), close));
  timers.toast = setTimeout(() => { clear(region); timers.toast = 0; }, TOAST_MS);
}

/* Header, theme, static text */
function applyThemeColors(colors) {
  const style = document.documentElement.style;
  const primary = colors && typeof colors.primary === "string" ? colors.primary.trim() : "";
  const secondary = colors && typeof colors.secondary === "string" ? colors.secondary.trim() : "";
  if (COLOR_PATTERN.test(primary)) style.setProperty("--accent", primary);
  else style.removeProperty("--accent");
  if (COLOR_PATTERN.test(secondary)) style.setProperty("--accent-secondary", secondary);
  else style.removeProperty("--accent-secondary");
}

function cooldownSeconds() { return Math.max(0, Math.ceil((state.sync.cooldownUntil - Date.now()) / 1000)); }
function syncIsRunning() { return Boolean(state.overview && state.overview.syncStatus === "running"); }

function renderSyncChip() {
  const chip = $("sync-chip");
  const labelNode = $("sync-chip-text");
  if (!chip || !labelNode) return;
  const running = state.sync.fast || syncIsRunning();
  const resolved = running ? "running" : state.overview && state.overview.syncStatus === "error" ? "error" : "idle";
  const label = syncStatusLabel(resolved);
  labelNode.textContent = label;
  chip.className = "c-chip c-chip--" + (SYNC_TONE[resolved] || "neutral") + (resolved === "running" ? " c-chip--running" : "");
  chip.setAttribute("aria-label", fmt("aria.syncStatus", { status: label }, "同步状态：{status}"));
}

function renderSyncButton() {
  const button = $("btn-sync");
  if (!button) return;
  const cooldown = cooldownSeconds();
  const busy = state.sync.inflight || state.sync.fast || syncIsRunning();
  const disabled = busy || cooldown > 0;
  const label = cooldown > 0
    ? fmt("sync.cooldown", { seconds: cooldown }, "{seconds} 秒后可再次同步")
    : busy ? t("header.syncing", "同步中…") : t("header.syncNow", "手动同步");
  button.textContent = label;
  button.setAttribute("aria-disabled", disabled ? "true" : "false");
  button.classList.toggle("is-busy", disabled);
}

function renderHeader() {
  const title = $("page-title");
  if (title) title.textContent = state.pageTitle || t("title", "游戏活动同步");
  const sub = $("page-sub");
  if (sub) {
    const parts = [];
    if (state.overview) {
      const lastSync = state.overview.lastSyncAt === null ? t("header.neverSynced", "尚未同步") : timeText(state.overview.lastSyncAt);
      parts.push(t("header.lastSyncLabel", "最后同步") + " " + lastSync);
      parts.push(fmt("overview.syncPanel.healthSummary", {
        ok: state.overview.health.ok, stale: state.overview.health.stale,
        error: state.overview.health.error, disabled: state.overview.health.disabled,
      }, "{ok} 正常 / {stale} 过期 / {error} 错误 / {disabled} 禁用"));
    } else if (state.lastError) {
      parts.push(t("header.loadFailed", "数据暂不可用"));
    } else {
      parts.push(t("header.lastSyncLabel", "最后同步") + " " + t("common.na", "—"));
    }
    sub.textContent = parts.join(" · ");
  }
  renderSyncChip();
  renderSyncButton();
  const refresh = $("btn-refresh");
  if (refresh) {
    refresh.classList.toggle("is-busy", state.refreshing);
    refresh.setAttribute("aria-busy", state.refreshing ? "true" : "false");
  }
}

function renderStaticTexts() {
  const tabbar = $("tabbar");
  if (tabbar) tabbar.setAttribute("aria-label", t("aria.tabs", "视图分区"));
  for (const tab of TABS) {
    const button = $("tab-" + tab);
    if (button) button.textContent = t("tabs." + tab, tab);
  }
  const refresh = $("btn-refresh");
  if (refresh) {
    const label = t("header.refreshTitle", "刷新当前数据（只读）");
    refresh.setAttribute("aria-label", label);
    refresh.setAttribute("title", label);
  }
  const detailClose = $("detail-close");
  if (detailClose) detailClose.setAttribute("aria-label", t("common.close", "关闭"));
  const detailTitle = $("detail-title");
  if (detailTitle) detailTitle.textContent = t("detail.title", "活动详情");
  const confirmCancel = $("confirm-cancel");
  if (confirmCancel) confirmCancel.textContent = t("confirm.cancel", "取消");
  const confirmOk = $("confirm-ok");
  if (confirmOk) confirmOk.textContent = t("confirm.ok", "确认");
  document.title = state.pageTitle || t("title", "游戏活动同步");
}

/* Panels: skeleton, empty, facts */
function setPanelBusy(tab, busy) {
  const panel = panelEl(tab);
  if (panel) panel.setAttribute("aria-busy", busy ? "true" : "false");
}

function skeletonBlock(rows) {
  const wrap = el("div", { className: "c-skeleton", "aria-hidden": "true" });
  for (let index = 0; index < rows; index += 1) wrap.append(el("span", { className: "c-sk c-sk--line" }));
  return wrap;
}

function renderSkeletonPanel(tab, rows) {
  const panel = panelEl(tab);
  if (!panel) return;
  clear(panel);
  panel.append(skeletonBlock(rows), el("span", { className: "sr-only", text: t("aria.loading", "数据加载中") }));
  setPanelBusy(tab, true);
}

function emptyState(options) {
  const wrap = el("div", { className: "c-empty", role: "status" });
  wrap.append(
    el("img", { className: "c-empty__img", src: "./assets/empty.svg", alt: "", width: 96, height: 96, loading: "lazy", decoding: "async" }),
    el("h2", { className: "c-empty__title", text: t(options.titleKey, options.titleFallback) }),
    el("p", { className: "c-empty__body", text: t(options.bodyKey, options.bodyFallback) }),
  );
  if (options.actionLabel && options.onAction) {
    const button = el("button", { className: "c-btn", type: "button", text: options.actionLabel });
    button.addEventListener("click", options.onAction);
    wrap.append(button);
  }
  return wrap;
}

function appendFact(dl, label, content, options) {
  const opts = options || {};
  dl.append(el("dt", { text: label }));
  const dd = el("dd", { className: opts.mono ? "c-mono" : "" });
  if (content instanceof Node) dd.append(content);
  else dd.textContent = content === null || content === undefined || content === "" ? t("common.na", "—") : String(content);
  if (opts.pre) dd.classList.add("c-pre");
  if (opts.copyText) {
    const button = el("button", { className: "c-copybtn", type: "button", "aria-label": t("common.copy", "复制") + " " + label });
    button.append(svgIcon("i-copy"), el("span", { text: t("common.copy", "复制") }));
    button.addEventListener("click", () => { void copyToClipboard(opts.copyText, dd); });
    dd.append(button);
  }
  dl.append(dd);
}

function selectNodeText(node) {
  if (!window.getSelection) return;
  try {
    const range = document.createRange();
    range.selectNodeContents(node);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  } catch (error) { /* selection is best effort */ }
}

async function copyToClipboard(text, node) {
  try {
    if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(text);
    showToast(t("common.copied", "已复制"));
  } catch (error) {
    selectNodeText(node);
    showToast(t("common.copyFailed", "复制失败：已选中文本，请按 Ctrl+C"));
  }
}

/* Covers */
const COVER_SIZES = { sm: 40, md: 56, lg: 72 };

function safeHttpUrl(value) {
  if (typeof value !== "string" || !value.trim()) return "";
  try {
    const url = new URL(value.trim(), window.location.href);
    if (url.protocol !== "http:" && url.protocol !== "https:") return "";
    return url.href;
  } catch (error) { return ""; }
}

function onCoverError(event) {
  const image = event.currentTarget;
  image.removeEventListener("error", onCoverError);
  image.src = "./assets/empty.svg";
}

function coverImage(url, sizeKey) {
  const size = COVER_SIZES[sizeKey] || COVER_SIZES.sm;
  const image = el("img", {
    className: "c-cover c-cover--" + sizeKey, alt: "", width: size, height: size,
    loading: "lazy", decoding: "async", referrerpolicy: "no-referrer",
  });
  const safe = safeHttpUrl(url);
  if (safe) { image.src = safe; image.addEventListener("error", onCoverError); }
  else image.src = "./assets/empty.svg";
  return image;
}

/* Overview tab */
function kpiButton(id, label, value, hint, onActivate) {
  const unit = t("overview.unit", "条");
  const button = el("button", {
    className: "c-kpi", type: "button", dataset: { kpi: id, focusKey: "kpi:" + id },
    "aria-label": label + ": " + value + " " + unit + " · " + t("overview.viewAll", "查看全部活动"),
  });
  button.append(
    el("span", { className: "c-kpi__label", text: label }),
    el("span", { className: "c-kpi__figure" },
      el("strong", { className: "c-kpi__value", text: String(value) }),
      el("span", { className: "c-kpi__unit", text: unit })),
    el("span", { className: "c-kpi__hint", text: hint }),
  );
  button.addEventListener("click", onActivate);
  return button;
}

function overviewSyncPanel(data) {
  const card = el("section", { className: "c-panelcard" });
  const dl = el("dl", { className: "c-facts" });
  appendFact(dl, t("overview.syncPanel.syncStatus", "同步状态"), syncStatusChip(data.syncStatus));
  appendFact(dl, t("overview.syncPanel.lastSync", "最后同步"), timeNode(data.lastSyncAt));
  appendFact(dl, t("overview.syncPanel.sourceHealth", "数据源健康"), fmt("overview.syncPanel.healthSummary",
    { ok: data.health.ok, stale: data.health.stale, error: data.health.error, disabled: data.health.disabled },
    "{ok} 正常 / {stale} 过期 / {error} 错误 / {disabled} 禁用"));
  card.append(el("h2", { className: "c-panelcard__title", text: t("overview.syncPanel.title", "同步与数据源") }), dl);
  card.append(el("p", { className: "c-small", text: fmt("overview.syncPanel.pollInterval", { seconds: data.pollSeconds }, "自动刷新间隔：{seconds} 秒") }));
  if (data.syncError) card.append(el("p", { className: "c-panelcard__error", text: data.syncError }));
  return card;
}

function overviewReminderPanel(data) {
  const card = el("section", { className: "c-panelcard" });
  card.append(el("h2", { className: "c-panelcard__title", text: t("overview.nextReminder.title", "下一提醒") }));
  if (!data.nextReminder) {
    card.append(el("p", { className: "c-small", text: t("overview.nextReminder.empty", "暂无待发送提醒") }));
    return card;
  }
  const next = data.nextReminder;
  const dl = el("dl", { className: "c-facts" });
  appendFact(dl, t("reminders.table.activity", "活动"), next.activityName);
  appendFact(dl, t("detail.fields.game", "游戏"), gameLabel(next.gameId));
  appendFact(dl, t("reminders.table.threshold", "提前"), next.thresholdDays === null ? null : fmt("reminders.thresholdDays", { days: next.thresholdDays }, "{days} 天"));
  appendFact(dl, t("reminders.table.fireAt", "触发时间"), isoNode(next.fireAtIso));
  card.append(dl);
  return card;
}

function goActivities(status) {
  state.activities.filters = { game: "", status: status || "", q: "" };
  switchTab("activities");
  if (state.tab === "activities") { syncFilterControls(); void requestActivities(); }
}

function renderOverview() {
  const panel = panelEl("overview");
  const data = state.overview;
  if (!panel || !data) return;
  const snapshot = captureFocus(panel);
  clear(panel);
  const kpis = el("div", { className: "c-kpis" });
  kpis.append(
    kpiButton("active", t("overview.kpi.active", "进行中"), data.counts.active, t("overview.kpiHint.active", "当前可见活动"), () => goActivities("active")),
    kpiButton("ending-soon", t("overview.kpi.endingSoon", "即将结束"), data.counts.endingSoon, t("overview.kpiHint.endingSoon", "临近结束"), () => goActivities("ending_soon")),
    kpiButton("upcoming", t("overview.kpi.upcoming", "即将开始"), data.counts.upcoming, t("overview.kpiHint.upcoming", "尚未开始"), () => goActivities("upcoming")),
    kpiButton("removed", t("overview.kpi.removed24h", "24 小时下架"), data.removed24h, t("overview.kpiHint.removed24h", "最近下架"), () => goActivities("removed")),
  );
  const updated = el("p", { className: "c-updated" });
  updated.append(el("span", { text: fmt("overview.updatedAt", { time: timeTextOfMs(state.overviewAt) }, "统计更新于 {time}") }));
  const viewAll = el("button", { className: "c-linkbtn", type: "button", text: t("overview.viewAll", "查看全部活动"), dataset: { focusKey: "overview:view-all" } });
  viewAll.addEventListener("click", () => goActivities(""));
  updated.append(viewAll);
  panel.append(kpis, el("div", { className: "c-overview" }, overviewSyncPanel(data), overviewReminderPanel(data)), updated);
  setPanelBusy("overview", false);
  restoreFocus(panel, snapshot);
}

/* Activities tab */
function activityCountText() {
  const data = state.activities.data;
  return data ? fmt("activities.resultCount", { count: data.total }, "共 {count} 条") : "";
}

function updateActivitiesCount() {
  if (state.activities.countEl) state.activities.countEl.textContent = activityCountText();
}

function fillGameOptions(select) {
  clear(select);
  select.append(el("option", { value: "", text: t("common.all", "全部") }));
  const games = state.games && state.games.length ? state.games : GAME_IDS.map((id) => ({ gameId: id, name: "" }));
  for (const game of games) select.append(el("option", { value: game.gameId, text: gameLabel(game.gameId) }));
}

function fillStatusOptions(select) {
  clear(select);
  select.append(el("option", { value: "", text: t("common.all", "全部") }));
  for (const status of ACTIVITY_STATUSES) select.append(el("option", { value: status, text: statusLabel(status) }));
}

function fillLimitOptions(select) {
  clear(select);
  for (const limit of LIMITS) select.append(el("option", { value: String(limit), text: String(limit) }));
}

function hasActivityFilters() {
  const filters = state.activities.filters;
  return Boolean(filters.game || filters.status || filters.q.trim());
}

function syncFilterControls() {
  const game = $("filter-game");
  if (game) game.value = state.activities.filters.game;
  const status = $("filter-status");
  if (status) status.value = state.activities.filters.status;
  const search = $("filter-search");
  if (search) search.value = state.activities.filters.q;
  const limit = $("filter-limit");
  if (limit) limit.value = String(state.activities.limit);
}

function refreshGameOptions() {
  const select = $("filter-game");
  if (!select) return;
  const current = state.activities.filters.game;
  fillGameOptions(select);
  select.value = current;
}

function clearActivityFilters() {
  state.activities.filters = { game: "", status: "", q: "" };
  syncFilterControls();
  void requestActivities();
}

function findActivity(key) {
  const data = state.activities.data;
  return data ? data.items.find((item) => item.key === key) || null : null;
}

function onActivitySearchInput(value) {
  state.activities.filters.q = value;
  if (timers.search) clearTimeout(timers.search);
  timers.search = setTimeout(() => { timers.search = 0; void requestActivities(); }, SEARCH_DEBOUNCE_MS);
}

function mountActivities() {
  const panel = panelEl("activities");
  if (!panel) return;
  clear(panel);
  state.activities.mounted = true;

  const gameSelect = el("select", { className: "c-select", id: "filter-game" });
  const gameField = el("div", { className: "c-field" });
  gameField.append(el("label", { className: "c-field__label", htmlFor: "filter-game", text: t("activities.filters.game", "游戏") }), gameSelect);

  const statusSelect = el("select", { className: "c-select", id: "filter-status" });
  const statusField = el("div", { className: "c-field" });
  statusField.append(el("label", { className: "c-field__label", htmlFor: "filter-status", text: t("activities.filters.status", "状态") }), statusSelect);

  const searchInput = el("input", {
    className: "c-input", id: "filter-search", type: "search", autocomplete: "off",
    placeholder: t("activities.filters.searchPlaceholder", "活动名称或描述"),
  });
  const searchField = el("div", { className: "c-field c-field--search" });
  searchField.append(el("label", { className: "c-field__label", htmlFor: "filter-search", text: t("activities.filters.search", "搜索") }), searchInput);

  const limitSelect = el("select", { className: "c-select", id: "filter-limit" });
  const limitField = el("div", { className: "c-field" });
  limitField.append(el("label", { className: "c-field__label", htmlFor: "filter-limit", text: t("activities.filters.limit", "每次加载") }), limitSelect);

  const count = el("p", { className: "c-filterbar__count", role: "status", "aria-live": "polite" });
  const clearButton = el("button", { className: "c-btn c-btn--small", type: "button", text: t("common.clear", "清除") });
  const bar = el("div", { className: "c-filterbar" }, gameField, statusField, searchField, limitField,
    el("div", { className: "c-filterbar__actions" }, count, clearButton));
  const list = el("div", { dataset: { role: "activity-list" } });
  panel.append(bar, list);
  state.activities.listEl = list;
  state.activities.countEl = count;

  fillGameOptions(gameSelect);
  fillStatusOptions(statusSelect);
  fillLimitOptions(limitSelect);
  syncFilterControls();

  gameSelect.addEventListener("change", () => { state.activities.filters.game = gameSelect.value; void requestActivities(); });
  statusSelect.addEventListener("change", () => { state.activities.filters.status = statusSelect.value; void requestActivities(); });
  limitSelect.addEventListener("change", () => { state.activities.limit = clampLimit(limitSelect.value); void requestActivities(); });
  searchInput.addEventListener("input", () => onActivitySearchInput(searchInput.value));
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      if (timers.search) { clearTimeout(timers.search); timers.search = 0; }
      void requestActivities();
    } else if (event.key === "Escape") {
      event.preventDefault();
      searchInput.value = "";
      state.activities.filters.q = "";
      void requestActivities();
    }
  });
  clearButton.addEventListener("click", clearActivityFilters);
  list.addEventListener("click", onActivityListClick);
  list.addEventListener("keydown", onActivityListKeyDown);
  list.addEventListener("focusin", (event) => {
    const trigger = event.target.closest("[data-activity-trigger]");
    if (trigger) state.activities.activeKey = trigger.dataset.key || "";
  });

  renderActivitiesList();
  if (!state.activities.data) {
    clear(list);
    list.append(skeletonBlock(5));
    setPanelBusy("activities", true);
  }
}

function onActivityListClick(event) {
  const trigger = event.target.closest("[data-activity-trigger]");
  if (trigger) {
    const item = findActivity(trigger.dataset.key);
    if (item) openDetail(item, trigger);
    return;
  }
  const row = event.target.closest("tr[data-key]");
  if (row) {
    const item = findActivity(row.dataset.key);
    if (item) openDetail(item, row.querySelector("[data-activity-trigger]") || row);
  }
}

function onActivityListKeyDown(event) {
  const wrap = event.currentTarget;
  const trigger = event.target.closest("[data-activity-trigger]");
  if (!trigger) return;
  const triggers = Array.from(wrap.querySelectorAll("[data-activity-trigger]"));
  const index = triggers.indexOf(trigger);
  if (index < 0) return;
  let next = -1;
  if (event.key === "ArrowDown") next = Math.min(index + 1, triggers.length - 1);
  else if (event.key === "ArrowUp") next = Math.max(index - 1, 0);
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = triggers.length - 1;
  else return;
  event.preventDefault();
  triggers.forEach((node, nodeIndex) => { node.tabIndex = nodeIndex === next ? 0 : -1; });
  triggers[next].focus();
}

function applyActivityRoving(list) {
  const triggers = Array.from(list.querySelectorAll("[data-activity-trigger]"));
  if (!triggers.length) return;
  let activeIndex = triggers.findIndex((node) => node.dataset.key === state.activities.activeKey);
  if (activeIndex < 0) activeIndex = 0;
  triggers.forEach((node, index) => { node.tabIndex = index === activeIndex ? 0 : -1; });
}

function activityRow(item) {
  const row = el("tr", { className: "c-row", dataset: { key: item.key } });
  row.setAttribute("aria-expanded", state.detailKey === item.key ? "true" : "false");
  const nameWrap = el("span", { className: "c-row__wrap" }, coverImage(item.coverUrl, "sm"));
  const trigger = el("button", {
    className: "c-rowlink" + (item.status === "removed" ? " is-removed" : ""), type: "button",
    dataset: { activityTrigger: "1", key: item.key, focusKey: "activity:" + item.key },
    "aria-label": fmt("activities.row.open", { name: item.name || item.key }, "查看 {name} 的详情"),
    text: item.name || t("common.na", "—"),
  });
  nameWrap.append(trigger);
  if (item.isPrimary && item.mergedSources.length > 1) nameWrap.append(el("span", { className: "c-badge", text: t("activities.row.primary", "主记录") }));
  row.append(
    el("td", { className: "c-table__name" }, nameWrap),
    el("td", { text: gameLabel(item.gameId) }),
    el("td", {}, statusChip(STATUS_TONE[item.status] || "neutral", statusLabel(item.status))),
    el("td", {}, isoNode(item.startIso)),
    el("td", {}, isoNode(item.endIso)),
    el("td", { className: "c-num", text: String(item.mergedSources.length || 1) }),
    el("td", {}, timeNode(item.lastSeenAt)),
  );
  return row;
}

function activitiesTable(items) {
  const table = el("table", { className: "c-table c-table--activities" });
  table.append(el("caption", { className: "sr-only", text: t("aria.activityTable", "活动列表") }));
  const headRow = el("tr");
  const columns = [["name", "活动"], ["game", "游戏"], ["status", "状态"], ["start", "开始"], ["end", "结束"], ["sources", "来源"], ["updated", "同步时间"]];
  for (const [key, fallback] of columns) headRow.append(el("th", { scope: "col", text: t("activities.table." + key, fallback) }));
  const body = el("tbody");
  for (const item of items) body.append(activityRow(item));
  table.append(el("thead", {}, headRow), body);
  return el("div", { className: "c-tablewrap" }, table);
}

function activityCard(item) {
  const hit = el("button", {
    className: "c-card__hit", type: "button",
    dataset: { activityTrigger: "1", key: item.key, focusKey: "activity:" + item.key },
    "aria-label": fmt("activities.row.open", { name: item.name || item.key }, "查看 {name} 的详情"),
  });
  const subParts = [t("activities.table.end", "结束") + " " + isoText(item.endIso)];
  if (item.mergedSources.length > 1) subParts.push(fmt("activities.row.merged", { count: item.mergedSources.length }, "{count} 个来源"));
  hit.append(
    coverImage(item.coverUrl, "md"),
    el("span", { className: "c-card__main" },
      el("span", { className: "c-card__title" + (item.status === "removed" ? " is-removed" : ""), text: item.name || t("common.na", "—") }),
      el("span", { className: "c-card__meta" },
        statusChip(STATUS_TONE[item.status] || "neutral", statusLabel(item.status)),
        el("span", { text: gameLabel(item.gameId) })),
      el("span", { className: "c-card__sub", text: subParts.join(" · ") })),
  );
  return el("li", { className: "c-card" }, hit);
}

function activitiesCards(items) {
  const list = el("ul", { className: "c-cards" });
  for (const item of items) list.append(activityCard(item));
  return list;
}

function renderActivitiesList() {
  const list = state.activities.listEl;
  if (!list) return;
  const snapshot = captureFocus(list);
  const data = state.activities.data;
  clear(list);
  if (!data) {
    list.append(skeletonBlock(5));
  } else if (!data.items.length) {
    list.append(hasActivityFilters()
      ? emptyState({
          titleKey: "activities.filteredEmpty.title", titleFallback: "没有匹配的活动",
          bodyKey: "activities.filteredEmpty.body", bodyFallback: "换个关键词，或清除筛选条件。",
          actionLabel: t("activities.filteredEmpty.action", "清除筛选"), onAction: clearActivityFilters,
        })
      : emptyState({
          titleKey: "activities.empty.title", titleFallback: "没有可显示的活动",
          bodyKey: "activities.empty.body", bodyFallback: "数据源同步完成后，活动会显示在这里。",
          actionLabel: t("activities.empty.action", "立即同步"), onAction: () => openSyncDialog(""),
        }));
  } else {
    list.append(desktopQuery.matches ? activitiesTable(data.items) : activitiesCards(data.items));
  }
  if (data && data.total > data.items.length) {
    list.append(el("p", { className: "c-small", text: fmt("activities.truncated", { count: data.items.length }, "仅显示前 {count} 条，请用筛选缩小范围") }));
  }
  applyActivityRoving(list);
  updateActivitiesCount();
  restoreFocus(list, snapshot);
}

async function requestActivities() {
  const generation = state.activities.generation + 1;
  state.activities.generation = generation;
  const filters = state.activities.filters;
  const params = { limit: clampLimit(state.activities.limit) };
  if (filters.game) params.game = filters.game;
  if (filters.status) params.status = filters.status;
  const query = filters.q.trim().slice(0, SEARCH_MAX_LENGTH);
  if (query) params.q = query;
  state.activities.loading = true;
  setPanelBusy("activities", true);
  try {
    const data = normalizeActivities(await apiGet("activities", params));
    if (generation !== state.activities.generation) return true;
    state.activities.data = data;
    maybeSchemaWarning(data.schema);
    renderActivitiesList();
    state.staleAt = 0;
    renderStaleNotice();
    return true;
  } catch (error) {
    if (generation !== state.activities.generation) return true;
    noteReadError(error);
    return false;
  } finally {
    if (generation === state.activities.generation) {
      state.activities.loading = false;
      setPanelBusy("activities", false);
      updateActivitiesCount();
    }
  }
}

/* Sources tab */
function sourceRetryButton(item) {
  const retrying = state.sync.inflight && state.sources.pendingSource === item.sourceId;
  if (!retrying && item.status !== "stale" && item.status !== "error") return null;
  const label = retrying ? t("sources.retrying", "重试中…") : t("sources.retry", "重试同步");
  const button = el("button", {
    className: "c-btn c-btn--small", type: "button", text: label,
    dataset: { retrySource: item.sourceId, focusKey: "source-retry:" + item.sourceId },
  });
  if (retrying) button.disabled = true;
  button.setAttribute("aria-label", label + " · " + item.sourceId);
  return button;
}

function sourceDiagnostics(item) {
  const dl = el("dl", { className: "c-facts" });
  appendFact(dl, t("sources.details.priority", "优先级"), item.priority === null ? null : String(item.priority));
  appendFact(dl, t("sources.details.enabled", "启用"), item.enabled ? t("sources.details.yes", "有") : t("sources.details.no", "无"));
  appendFact(dl, t("sources.details.lastChecked", "最近检查"), timeNode(item.lastChecked));
  if (item.httpStatus !== null && item.httpStatus > 0) {
    appendFact(dl, t("sources.details.httpStatus", "HTTP 状态"), String(item.httpStatus));
  }
  appendFact(dl, t("sources.details.etag", "ETag"), item.etagPresent ? t("sources.details.yes", "有") : t("sources.details.no", "无"));
  appendFact(dl, t("sources.details.lastModified", "Last-Modified"), item.lastModifiedPresent ? t("sources.details.yes", "有") : t("sources.details.no", "无"));
  if (item.nextRetryAt !== null && item.nextRetryAt > 0) {
    appendFact(dl, t("sources.details.nextRetryAt", "下次重试"), timeNode(item.nextRetryAt));
  }
  if (item.lastError) appendFact(dl, t("sources.details.lastError", "最后错误"), item.lastError, { pre: true });
  appendFact(dl, t("sources.table.adapter", "适配器"), item.adapter, { mono: true });
  appendFact(dl, t("sources.table.url", "地址"), item.url, { mono: true, pre: true });
  return dl;
}

function sourceDiagToggle(item) {
  const expanded = state.sources.expanded.has(item.sourceId);
  const button = el("button", {
    className: "c-linkbtn c-diag__toggle", type: "button",
    dataset: { diagToggle: item.sourceId, focusKey: "source-diag:" + item.sourceId },
    "aria-controls": "diag-" + item.sourceId,
    "aria-expanded": expanded ? "true" : "false",
  });
  button.append(
    el("span", { className: "c-diag__chev", "aria-hidden": "true" }),
    el("span", { text: t("sources.details.title", "详情") }),
  );
  return button;
}

function sourceDiagPanel(item, domId) {
  const attrs = { className: "c-diag__panel" };
  if (domId) attrs.id = domId;
  return el("div", attrs,
    el("p", { className: "c-diag__title", text: t("sources.details.title", "详情") + " · " + item.sourceId }),
    sourceDiagnostics(item));
}

function sourceActions(item) {
  const retry = sourceRetryButton(item);
  if (retry) return el("div", { className: "c-card__actions" }, retry);
  if (item.status === "disabled") {
    return el("div", { className: "c-card__actions" },
      el("p", { className: "c-small", text: t("sources.disabledNote", "该数据源已禁用，需在插件配置中启用。") }));
  }
  return el("div", { className: "c-card__actions" },
    el("span", { className: "c-small", text: t("common.na", "—") }));
}

function sourcesTable(items) {
  const table = el("table", { className: "c-table c-table--sources" });
  table.append(el("caption", { className: "sr-only", text: t("aria.sourcesTable", "数据源列表") }));
  const headRow = el("tr");
  const columns = [
    ["source", "数据源"], ["game", "游戏", "detail.fields.game"], ["status", "状态"],
    ["lastSuccess", "同步时间"], ["failures", "连续失败"], ["activityCount", "活动数"], ["actions", "操作"],
  ];
  for (const [key, fallback, keyPath] of columns) headRow.append(el("th", { scope: "col", text: t(keyPath || "sources.table." + key, fallback) }));
  table.append(el("thead", {}, headRow));
  for (const item of items) {
    const expanded = state.sources.expanded.has(item.sourceId);
    const body = el("tbody", { dataset: { sourceId: item.sourceId } });
    const row = el("tr", { className: "c-row" });
    row.append(
      el("th", { scope: "row", className: "c-sourcecell" },
        el("span", { className: "c-table__mono", text: item.sourceId }),
        sourceDiagToggle(item)),
      el("td", { text: gameLabel(item.gameId) }),
      el("td", {}, statusChip(SOURCE_TONE[item.status] || "neutral", sourceStatusLabel(item.status))),
      el("td", {}, timeNode(item.lastSuccess)),
      el("td", { className: "c-num" }, el("span", { className: item.consecutiveFailures > 0 ? "c-cell--error" : "", text: String(item.consecutiveFailures) })),
      el("td", { className: "c-num", text: String(item.activityCount) }),
      el("td", { className: "c-actions" }, sourceActions(item)),
    );
    const diagRow = el("tr", { className: "c-row c-row--diag", id: "diag-" + item.sourceId });
    if (!expanded) diagRow.hidden = true;
    diagRow.append(el("td", { colspan: "7" }, sourceDiagPanel(item, null)));
    body.append(row, diagRow);
    table.append(body);
  }
  return el("div", { className: "c-tablewrap" }, table);
}

function sourceCard(item) {
  const dl = el("dl", { className: "c-facts" });
  appendFact(dl, t("detail.fields.game", "游戏"), gameLabel(item.gameId));
  appendFact(dl, t("sources.table.lastSuccess", "同步时间"), timeNode(item.lastSuccess));
  appendFact(dl, t("sources.table.failures", "连续失败"), String(item.consecutiveFailures));
  appendFact(dl, t("sources.table.activityCount", "活动数"), String(item.activityCount));
  const panel = sourceDiagPanel(item, "diag-" + item.sourceId);
  if (!state.sources.expanded.has(item.sourceId)) panel.hidden = true;
  const block = el("div", { className: "c-card__body" },
    el("div", { className: "c-card__head" },
      el("span", { className: "c-card__headtitle", text: item.sourceId }),
      statusChip(SOURCE_TONE[item.status] || "neutral", sourceStatusLabel(item.status))),
    dl,
    el("div", { className: "c-card__diag" }, sourceDiagToggle(item), panel),
    sourceActions(item));
  return el("li", { className: "c-card" }, block);
}

function renderSources() {
  const panel = panelEl("sources");
  if (!panel) return;
  const data = state.sources.data;
  if (!data) { renderSkeletonPanel("sources", 5); return; }
  const snapshot = captureFocus(panel);
  clear(panel);
  if (!data.items.length) {
    panel.append(emptyState({
      titleKey: "sources.empty.title", titleFallback: "没有配置数据源",
      bodyKey: "sources.empty.body", bodyFallback: "在插件配置中添加数据源后，这里会显示其健康状态。",
    }));
  } else if (desktopQuery.matches) {
    panel.append(sourcesTable(data.items));
  } else {
    const list = el("ul", { className: "c-cards c-cards--single" });
    for (const item of data.items) list.append(sourceCard(item));
    panel.append(list);
  }
  setPanelBusy("sources", false);
  restoreFocus(panel, snapshot);
}

async function loadSources() {
  if (state.sources.loading) return true;
  state.sources.loading = true;
  if (!state.sources.data) setPanelBusy("sources", true);
  try {
    const data = normalizeSources(await apiGet("sources"));
    state.sources.data = data;
    maybeSchemaWarning(data.schema);
    renderSources();
    state.staleAt = 0;
    renderStaleNotice();
    return true;
  } catch (error) {
    noteReadError(error);
    return false;
  } finally {
    state.sources.loading = false;
    setPanelBusy("sources", false);
  }
}

function openSourceRetry(item) {
  if (item.status === "disabled") return;
  const cooldown = cooldownSeconds();
  if (cooldown > 0) { showToast(fmt("sync.cooldown", { seconds: cooldown }, "{seconds} 秒后可再次同步")); return; }
  if (state.sync.inflight || state.sync.fast || syncIsRunning()) {
    showToast(t("sync.toast.alreadyRunning", "同步已在运行，正在等待结果"));
    return;
  }
  openConfirm({
    title: t("sources.retryConfirmTitle", "重试该数据源？"),
    body: fmt("sources.retryConfirmBody", { source: item.sourceId, game: gameLabel(item.gameId) }, "将对 {source}（{game}）所属游戏触发一次同步。"),
    okLabel: t("sources.retry", "重试同步"),
    onConfirm() { void postSync(item.gameId, item.sourceId); },
  });
}

/* Reminders tab */
function summaryItem(label, value) {
  return el("li", { className: "c-summary__item" },
    el("span", { text: label }), el("strong", { className: "c-summary__value", text: String(value) }));
}

function isCatchUp(job) {
  if (!job || job.status !== "sent") return false;
  if (typeof job.sentAt !== "number" || !Number.isFinite(job.sentAt)) return false;
  const fireAt = fromIso(job.fireAtIso);
  if (!fireAt) return false;
  return job.sentAt * 1000 - fireAt.getTime() >= 60000;
}

function reminderStatus(job) {
  const group = el("span", { className: "c-statusgroup" },
    statusChip(JOB_TONE[job.status] || "neutral", jobStatusLabel(job.status)));
  if (!isCatchUp(job)) return group;
  const hint = t("reminders.catchUpHint", "该提醒晚于预定时间补发（超过 1 分钟）");
  group.append(el("span", {
    className: "c-badge c-badge--catchup", text: t("reminders.catchUp", "补发"),
    title: hint, "aria-label": t("reminders.catchUp", "补发") + ": " + hint,
  }));
  return group;
}

function reminderToggle(job, index) {
  const button = el("button", {
    className: "c-reminder__toggle", type: "button",
    dataset: { reminderToggle: String(index), focusKey: "reminder-toggle:" + job.key },
    "data-reminder-key": job.key,
    "aria-expanded": state.reminders.expanded.has(job.key) ? "true" : "false",
    "aria-controls": "reminder-details-" + index,
  });
  button.append(
    el("span", { className: "c-diag__chev", "aria-hidden": "true" }),
    el("span", { text: t("reminders.details.title", "详情") }),
  );
  return button;
}

function reminderDetails(job, domId) {
  const attrs = { className: "c-reminder__details" };
  if (domId) attrs.id = domId;
  const dl = el("dl", { className: "c-facts" });
  appendFact(dl, t("reminders.details.source", "数据源"), job.sourceId, { mono: true });
  appendFact(dl, t("reminders.details.rawId", "原始 ID"), job.rawId, { mono: true });
  appendFact(dl, t("reminders.details.attempts", "尝试次数"), String(job.attempts));
  if (job.lastError) appendFact(dl, t("sources.details.lastError", "最后错误"), job.lastError, { pre: true });
  return el("div", attrs, dl);
}

function reminderRow(job, index) {
  const name = el("span", { className: "c-reminder__name", text: job.activityName || t("common.na", "—") });
  const row = el("tr", { className: "c-row" });
  row.append(
    el("td", {}, el("span", { className: "c-reminder__activity" }, name, reminderToggle(job, index))),
    el("td", { text: gameLabel(job.gameId) }),
    el("td", { text: job.thresholdDays === null ? t("common.na", "—") : fmt("reminders.thresholdDays", { days: job.thresholdDays }, "{days} 天") }),
    el("td", {}, isoNode(job.fireAtIso)),
    el("td", {}, reminderStatus(job)),
    el("td", {}, timeNode(job.sentAt)),
    el("td", { className: job.lastError ? "c-cell--error" : "", text: job.lastError || t("common.na", "—") }),
  );
  return row;
}

function reminderDetailsRow(job, index) {
  const row = el("tr", { className: "c-row c-row--details", id: "reminder-details-" + index });
  if (!state.reminders.expanded.has(job.key)) row.hidden = true;
  row.append(el("td", { colspan: "7" }, reminderDetails(job, "")));
  return row;
}

function remindersTable(data) {
  const table = el("table", { className: "c-table c-table--reminders" });
  table.append(el("caption", { className: "sr-only", text: t("aria.remindersTable", "提醒任务列表") }));
  const headRow = el("tr");
  const columns = [
    ["activity", "活动"], ["game", "游戏"], ["threshold", "提前"], ["fireAt", "触发时间"],
    ["status", "状态"], ["sentAt", "发送时间"], ["lastError", "最后错误"],
  ];
  for (const [key, fallback] of columns) headRow.append(el("th", { scope: "col", text: t("reminders.table." + key, fallback) }));
  table.append(el("thead", {}, headRow));
  data.jobs.forEach((job, index) => {
    const body = el("tbody", { dataset: { jobKey: job.key } });
    body.append(reminderRow(job, index), reminderDetailsRow(job, index));
    table.append(body);
  });
  return el("div", { className: "c-tablewrap" }, table);
}

function reminderCard(job, index) {
  const dl = el("dl", { className: "c-facts" });
  appendFact(dl, t("reminders.table.game", "游戏"), gameLabel(job.gameId));
  appendFact(dl, t("reminders.table.threshold", "提前"), job.thresholdDays === null ? null : fmt("reminders.thresholdDays", { days: job.thresholdDays }, "{days} 天"));
  appendFact(dl, t("reminders.table.fireAt", "触发时间"), isoNode(job.fireAtIso));
  appendFact(dl, t("reminders.table.sentAt", "发送时间"), timeNode(job.sentAt));
  const panel = reminderDetails(job, "reminder-details-" + index);
  if (!state.reminders.expanded.has(job.key)) panel.hidden = true;
  const block = el("div", { className: "c-card__body" },
    el("div", { className: "c-card__head" },
      el("span", { className: "c-card__headtitle", text: job.activityName || t("common.na", "—") }),
      reminderStatus(job)),
    dl,
    el("div", { className: "c-reminder__togglebar" }, reminderToggle(job, index)),
    panel);
  if (job.lastError) block.append(el("p", { className: "c-cell--error c-small", text: job.lastError }));
  return el("li", { className: "c-card" }, block);
}

function renderReminders() {
  const panel = panelEl("reminders");
  if (!panel) return;
  const data = state.reminders.data;
  if (!data) { renderSkeletonPanel("reminders", 4); return; }
  const snapshot = captureFocus(panel);
  clear(panel);
  const counts = { pending: 0, sent: 0, skipped: 0, failed: 0 };
  for (const job of data.jobs) if (Object.prototype.hasOwnProperty.call(counts, job.status)) counts[job.status] += 1;
  panel.append(el("ul", { className: "c-summary" },
    summaryItem(t("reminders.summary.jobs", "提醒任务"), data.jobs.length),
    summaryItem(t("reminders.summary.pending", "待发送"), counts.pending),
    summaryItem(t("reminders.summary.sent", "已提交"), counts.sent),
    summaryItem(t("reminders.summary.skipped", "已跳过"), counts.skipped),
    summaryItem(t("reminders.summary.failed", "失败"), counts.failed),
    summaryItem(t("reminders.summary.subscriptions", "订阅会话数"), data.subscriptionsCount)));
  if (data.jobs.length) {
    panel.append(el("p", { className: "c-small c-reminder__note", text: t("reminders.sentNote", "“已提交”表示已交给平台适配器，不代表已送达；QQ 官方群聊冷群时可能被核心跳过。") }));
  }
  if (!data.jobs.length) {
    panel.append(emptyState({
      titleKey: "reminders.empty.title", titleFallback: "暂无提醒任务",
      bodyKey: "reminders.empty.body", bodyFallback: "为活动配置提醒后，任务会显示在这里。",
    }));
  } else if (desktopQuery.matches) {
    panel.append(remindersTable(data));
  } else {
    const list = el("ul", { className: "c-cards c-cards--single" });
    data.jobs.forEach((job, index) => list.append(reminderCard(job, index)));
    panel.append(list);
  }
  setPanelBusy("reminders", false);
  restoreFocus(panel, snapshot);
}

async function loadReminders() {
  if (state.reminders.loading) return true;
  state.reminders.loading = true;
  if (!state.reminders.data) setPanelBusy("reminders", true);
  try {
    const data = normalizeReminders(await apiGet("reminders", { limit: 50 }));
    state.reminders.data = data;
    maybeSchemaWarning(data.schema);
    renderReminders();
    state.staleAt = 0;
    renderStaleNotice();
    return true;
  } catch (error) {
    noteReadError(error);
    return false;
  } finally {
    state.reminders.loading = false;
    setPanelBusy("reminders", false);
  }
}

/* Details drawer */
function markRowExpanded(key, expanded) {
  const panel = panelEl("activities");
  if (!panel || !key) return;
  panel.querySelectorAll('[data-key="' + attrEsc(key) + '"]').forEach((node) => node.setAttribute("aria-expanded", expanded ? "true" : "false"));
}

function renderDetailBody(item) {
  const body = $("detail-body");
  if (!body) return;
  clear(body);
  body.append(el("div", { className: "c-detail__hero" },
    coverImage(item.coverUrl, "lg"),
    el("div", { className: "c-detail__heading" },
      el("p", { className: "c-detail__name", text: item.name || t("common.na", "—") }),
      statusChip(STATUS_TONE[item.status] || "neutral", statusLabel(item.status)))));

  const description = item.description.trim();
  body.append(el("section", { className: "c-detail__section" },
    el("h3", { className: "c-detail__sectiontitle", text: t("detail.description", "描述") }),
    description
      ? el("p", { className: "c-detail__desc", text: description })
      : el("p", { className: "c-detail__empty", text: t("detail.emptyDescription", "该活动没有描述。") })));

  const dl = el("dl", { className: "c-facts" });
  appendFact(dl, t("detail.fields.game", "游戏"), gameLabel(item.gameId));
  appendFact(dl, t("detail.fields.status", "状态"), statusChip(STATUS_TONE[item.status] || "neutral", statusLabel(item.status)));
  appendFact(dl, t("detail.fields.kind", "类型"), item.kind);
  appendFact(dl, t("detail.fields.timeRange", "活动时间"),
    isoText(item.startIso) + " " + t("time.rangeSeparator", "至") + " " + isoText(item.endIso));
  appendFact(dl, t("detail.fields.rawId", "原始 ID"), item.rawId, { mono: true, copyText: item.rawId });
  appendFact(dl, t("detail.fields.sourceId", "数据源"), item.sourceId, { mono: true });
  appendFact(dl, t("detail.fields.contentHash", "内容哈希"), item.contentHash, { mono: true, copyText: item.contentHash });
  appendFact(dl, t("detail.fields.mergedSources", "合并来源"), item.mergedSources.length ? item.mergedSources.join(", ") : null, { mono: true, pre: true });
  appendFact(dl, t("detail.fields.firstSeen", "首次发现"), timeNode(item.firstSeenAt));
  appendFact(dl, t("detail.fields.lastSeen", "同步时间"), timeNode(item.lastSeenAt));
  appendFact(dl, t("detail.fields.removedAt", "下架时间"), timeNode(item.removedAt));
  body.append(el("section", { className: "c-detail__section" }, dl));
}

function openDetail(item, trigger) {
  const dialog = $("detail-dialog");
  if (!dialog) return;
  if (state.detailKey) markRowExpanded(state.detailKey, false);
  state.detailKey = item.key;
  state.detailItem = item;
  state.detailTrigger = trigger || null;
  markRowExpanded(item.key, true);
  renderDetailBody(item);
  if (typeof dialog.showModal === "function") { if (!dialog.open) dialog.showModal(); }
  else dialog.setAttribute("open", "");
  const close = $("detail-close");
  if (close) close.focus();
}

function onDetailClosed() {
  if (state.detailKey) markRowExpanded(state.detailKey, false);
  const trigger = state.detailTrigger;
  state.detailKey = "";
  state.detailItem = null;
  state.detailTrigger = null;
  if (trigger && document.contains(trigger) && typeof trigger.focus === "function") trigger.focus();
}

/* Confirm dialog */
function openConfirm(options) {
  const dialog = $("confirm-dialog");
  if (!dialog) return;
  const title = $("confirm-title");
  if (title) title.textContent = options.title || "";
  const body = $("confirm-body");
  if (body) body.textContent = options.body || "";
  const extra = $("confirm-extra");
  if (extra) { clear(extra); if (options.buildExtra) options.buildExtra(extra); }
  const cancel = $("confirm-cancel");
  if (cancel) cancel.textContent = t("confirm.cancel", "取消");
  const ok = $("confirm-ok");
  if (ok) ok.textContent = options.okLabel || t("confirm.ok", "确认");
  state.confirm = options.onConfirm || null;
  dialog.returnValue = "cancel";
  if (typeof dialog.showModal === "function") { if (!dialog.open) dialog.showModal(); }
  else dialog.setAttribute("open", "");
  if (cancel) cancel.focus();
}

function closeConfirm(value) {
  const dialog = $("confirm-dialog");
  const action = state.confirm;
  state.confirm = null;
  if (!dialog) return;
  if (typeof dialog.close === "function") dialog.close(value);
  else dialog.removeAttribute("open");
  if (value === "ok" && action) void Promise.resolve().then(action);
}

function openSyncDialog(sourceGame) {
  const cooldown = cooldownSeconds();
  if (cooldown > 0) { showToast(fmt("sync.cooldown", { seconds: cooldown }, "{seconds} 秒后可再次同步")); return; }
  if (state.sync.inflight || state.sync.fast || syncIsRunning()) {
    showToast(t("sync.toast.alreadyRunning", "同步已在运行，正在等待结果"));
    if (!state.sync.fast && !state.sync.inflight) void loadOverview();
    return;
  }
  const initial = typeof sourceGame === "string" ? sourceGame : state.activities.filters.game || "";
  const games = state.games && state.games.length ? state.games : GAME_IDS.map((id) => ({ gameId: id, name: "" }));
  openConfirm({
    title: t("header.syncConfirmTitle", "立即同步？"),
    body: initial
      ? fmt("header.syncConfirmBody", { game: gameLabel(initial) }, "将对 {game} 触发一次数据同步，完成后活动列表会自动更新。")
      : t("header.syncConfirmBodyAll", "将触发一次全量同步（所有游戏），完成后列表会自动更新。"),
    okLabel: t("header.syncConfirmOk", "开始同步"),
    buildExtra(host) {
      const select = el("select", { className: "c-select", id: "sync-game" });
      select.append(el("option", { value: "", text: t("common.all", "全部") }));
      for (const game of games) select.append(el("option", { value: game.gameId, text: gameLabel(game.gameId) }));
      select.value = initial;
      select.addEventListener("change", () => {
        const body = $("confirm-body");
        if (!body) return;
        const next = select.value;
        body.textContent = next
          ? fmt("header.syncConfirmBody", { game: gameLabel(next) }, "将对 {game} 触发一次数据同步，完成后活动列表会自动更新。")
          : t("header.syncConfirmBodyAll", "将触发一次全量同步（所有游戏），完成后列表会自动更新。");
      });
      host.append(el("div", { className: "c-field" },
        el("label", { className: "c-field__label", htmlFor: "sync-game", text: t("activities.filters.game", "游戏") }), select));
    },
    onConfirm() {
      const select = $("sync-game");
      void postSync(select && select.value ? select.value : "", "");
    },
  });
}

/* Sync actions */
function startCooldown() {
  state.sync.cooldownUntil = Date.now() + COOLDOWN_MS;
  if (timers.cooldown) clearInterval(timers.cooldown);
  timers.cooldown = setInterval(() => {
    renderSyncButton();
    if (Date.now() >= state.sync.cooldownUntil) {
      clearInterval(timers.cooldown);
      timers.cooldown = 0;
      state.sync.cooldownUntil = 0;
      renderSyncButton();
    }
  }, 1000);
  renderSyncButton();
}

function handleSyncError(error) {
  const kind = classifyError(error);
  if (kind === "auth") { showAuthState(); return; }
  if (kind === "rate") {
    startCooldown();
    showToast(t("sync.toast.rateLimited", "刚刚已触发过，请 30 秒后再试"));
    return;
  }
  if (kind === "conflict") {
    showToast(t("sync.toast.alreadyRunning", "同步已在运行，正在等待结果"));
    void loadOverview();
    startFastPoll();
    return;
  }
  if (kind === "invalid") {
    setBanner({ kind: "error", titleKey: "errors.serverTitle", titleFallback: "服务暂时不可用", message: messageOf(error), retryAt: 0 });
    schedulePoll(pollDelay());
    return;
  }
  state.lastError = { kind, message: messageOf(error) };
  scheduleAfterFailure();
}

async function postSync(game, sourceId) {
  if (state.sync.inflight) return;
  state.sync.inflight = true;
  state.sources.pendingSource = sourceId || "";
  renderSyncButton();
  renderSyncChip();
  if (state.sources.data) renderSources();
  try {
    await apiPost("sync", game ? { game } : {});
    showToast(t("sync.toast.started", "已触发同步"));
    state.failures = 0;
    if (state.banner && state.banner.kind === "error") clearBanner();
    await loadOverview();
    startFastPoll();
  } catch (error) {
    handleSyncError(error);
  } finally {
    state.sync.inflight = false;
    state.sources.pendingSource = "";
    renderSyncButton();
    renderSyncChip();
    if (state.sources.data) renderSources();
  }
}

async function afterSync() {
  await refreshCore();
  schedulePoll(pollDelay());
}

function finishSync(reason) {
  state.sync.fast = false;
  renderSyncChip();
  renderSyncButton();
  showToast(reason === "timeout"
    ? fmt("errors.timeoutBody", { seconds: Math.round(FAST_POLL_MAX_MS / 1000) }, "服务器在 {seconds} 秒内没有响应，正在重试。")
    : t("sync.toast.finished", "同步完成，数据已更新"));
  void afterSync();
}

function startFastPoll() {
  state.sync.fast = true;
  state.sync.fastUntil = Date.now() + FAST_POLL_MAX_MS;
  state.failures = 0;
  clearPoll();
  schedulePoll(FAST_POLL_MS);
  renderSyncChip();
  renderSyncButton();
  if (state.sources.data) renderSources();
}

async function fastPollTick() {
  const ok = await loadOverview();
  if (!state.sync.fast) return;
  if (ok && state.overview && state.overview.syncStatus !== "running") { finishSync("done"); return; }
  if (!ok) showBannerFromLastError(0);
  if (Date.now() >= state.sync.fastUntil) { finishSync("timeout"); return; }
  schedulePoll(FAST_POLL_MS);
}

/* Loads, polling and backoff */
function pollDelay() {
  const seconds = state.overview && state.overview.pollSeconds ? state.overview.pollSeconds : DEFAULT_POLL_SECONDS;
  return Math.max(10, Math.min(300, seconds)) * 1000;
}

function backoffDelay() {
  const exponent = Math.min(Math.max(state.failures, 1) - 1, 4);
  return Math.min(BACKOFF_BASE_MS * 2 ** exponent, BACKOFF_MAX_MS);
}

function clearPoll() {
  if (timers.poll) { clearTimeout(timers.poll); timers.poll = 0; }
}

function schedulePoll(delay) {
  clearPoll();
  if (state.boot !== "ready" || state.paused) return;
  timers.poll = setTimeout(() => { timers.poll = 0; void pollOnce(); }, delay);
}

function maybeSchemaWarning(schema) {
  if (!Number.isFinite(schema) || schema === 1) return;
  if (state.banner && state.banner.kind === "error") return;
  setBanner({
    kind: "schema", titleKey: "errors.schemaMismatchTitle", titleFallback: "视图与后端版本不一致",
    bodyKey: "errors.schemaMismatchBody", bodyFallback: "响应 schema_version 不是 1，部分信息可能无法显示。", retryAt: 0,
  });
}

function noteReadError(error) {
  const kind = classifyError(error);
  if (kind === "auth") { showAuthState(); return; }
  state.lastError = { kind, message: messageOf(error) };
  if (state.lastGoodAt) { state.staleAt = state.lastGoodAt; renderStaleNotice(); }
}

function showBannerFromLastError(retryAt) {
  const info = state.lastError || { kind: "server", message: "" };
  const timeout = info.kind === "timeout";
  setBanner({
    kind: "error",
    titleKey: timeout ? "errors.timeoutTitle" : "errors.serverTitle",
    titleFallback: timeout ? "请求超时" : "服务暂时不可用",
    bodyKey: timeout ? "errors.timeoutBody" : "",
    bodyFallback: timeout ? "服务器在 {seconds} 秒内没有响应，正在重试。" : "",
    vars: { seconds: Math.round(REQUEST_TIMEOUT_MS / 1000) },
    message: info.message,
    retryAt: retryAt || 0,
  });
}

function scheduleAfterFailure() {
  if (state.boot !== "ready") return;
  state.failures += 1;
  const delay = backoffDelay();
  showBannerFromLastError(Date.now() + delay);
  schedulePoll(delay);
}

function onPollSuccess() {
  state.failures = 0;
  state.lastError = null;
  if (state.banner && state.banner.kind === "error") clearBanner();
  schedulePoll(pollDelay());
}

async function loadOverview() {
  try {
    const data = normalizeOverview(await apiGet("overview"));
    const timezoneChanged = data.showTimezone !== state.showTimezone;
    state.showTimezone = data.showTimezone;
    state.overview = data;
    state.overviewAt = Date.now();
    state.lastGoodAt = Date.now();
    state.staleAt = 0;
    renderStaleNotice();
    if (timezoneChanged) renderAll();
    else {
      renderHeader();
      renderOverview();
    }
    maybeSchemaWarning(data.schema);
    if (state.banner && state.banner.kind === "error") clearBanner();
    return true;
  } catch (error) {
    noteReadError(error);
    return false;
  }
}

async function loadGames() {
  try {
    state.games = normalizeGames(await apiGet("games"));
  } catch (error) {
    if (!state.games || !state.games.length) state.games = GAME_IDS.map((id) => ({ gameId: id, name: "" }));
  }
  refreshGameOptions();
}

function loadTab(tab) {
  if (tab === "activities") return requestActivities();
  if (tab === "sources") return loadSources();
  if (tab === "reminders") return loadReminders();
  return Promise.resolve(true);
}

async function refreshCore() {
  const tasks = [loadOverview()];
  if (state.tab !== "overview") tasks.push(loadTab(state.tab));
  const results = await Promise.all(tasks);
  return results.every(Boolean);
}

async function pollOnce() {
  if (state.boot !== "ready" || state.paused || state.polling) return;
  state.polling = true;
  try {
    if (state.sync.fast) { await fastPollTick(); return; }
    const ok = await refreshCore();
    if (ok) onPollSuccess();
    else scheduleAfterFailure();
  } finally {
    state.polling = false;
  }
}

async function manualRefresh() {
  if (state.refreshing) return;
  if (Date.now() - state.lastRefreshAt < 200) return;
  state.lastRefreshAt = Date.now();
  state.refreshing = true;
  renderHeader();
  try {
    const ok = await refreshCore();
    if (ok) onPollSuccess();
    else scheduleAfterFailure();
  } finally {
    state.refreshing = false;
    renderHeader();
  }
}

function onVisibilityChange() {
  if (document.hidden) {
    state.paused = true;
    clearPoll();
    stopCountdown();
    return;
  }
  if (state.boot !== "ready") return;
  state.paused = false;
  if (state.banner) renderBanner();
  if (state.sync.fast) { schedulePoll(0); return; }
  void pollOnce();
}

function clearAllTimers() {
  clearPoll();
  stopCountdown();
  if (timers.toast) { clearTimeout(timers.toast); timers.toast = 0; }
  if (timers.cooldown) { clearInterval(timers.cooldown); timers.cooldown = 0; }
  if (timers.search) { clearTimeout(timers.search); timers.search = 0; }
}

function onPageHide() {
  clearAllTimers();
  if (state.offContext) {
    try { state.offContext(); } catch (error) { /* ignore */ }
    state.offContext = null;
  }
  for (const id of ["detail-dialog", "confirm-dialog"]) {
    const dialog = $(id);
    if (dialog && dialog.open) { try { dialog.close(); } catch (error) { /* ignore */ } }
  }
  document.removeEventListener("visibilitychange", onVisibilityChange);
  window.removeEventListener("pagehide", onPageHide);
}

/* Tabs, routing, context */
function updateTabBar() {
  for (const tab of TABS) {
    const button = $("tab-" + tab);
    const panel = panelEl(tab);
    const active = tab === state.tab;
    if (button) {
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
      button.tabIndex = active ? 0 : -1;
    }
    if (panel) panel.hidden = !active;
  }
}

function mountActivitiesIfNeeded() {
  if (state.activities.mounted && state.activities.listEl) return;
  mountActivities();
}

function panelHasData(tab) {
  if (tab === "activities") return Boolean(state.activities.data);
  if (tab === "sources") return Boolean(state.sources.data);
  if (tab === "reminders") return Boolean(state.reminders.data);
  return Boolean(state.overview);
}

function loadTabNow(tab) {
  const task = tab === "overview" ? loadOverview() : loadTab(tab);
  void task.then((ok) => { if (ok) onPollSuccess(); else scheduleAfterFailure(); });
}

function applyTab(tab, skipLoad) {
  const target = TABS.includes(tab) ? tab : "overview";
  state.tab = target;
  updateTabBar();
  if (target === "overview") renderOverview();
  else if (target === "activities") mountActivitiesIfNeeded();
  else if (!panelHasData(target)) renderSkeletonPanel(target, target === "sources" ? 5 : 4);
  if (state.boot !== "ready" || skipLoad) return;
  loadTabNow(target);
}

function applyHash(skipLoad) {
  const raw = String(window.location.hash || "").replace(/^#\/?/, "").trim();
  if (!TABS.includes(raw)) {
    try { window.history.replaceState(null, "", "#/overview"); } catch (error) { /* opaque origin */ }
    applyTab("overview", skipLoad);
    return;
  }
  applyTab(raw, skipLoad);
}

function switchTab(tab) {
  if (!TABS.includes(tab)) return;
  const hash = "#/" + tab;
  if (window.location.hash === hash) { applyTab(tab, false); return; }
  window.location.hash = hash;
}

function onTabClick(event) {
  const button = event.target.closest("[data-tab]");
  if (button) switchTab(button.dataset.tab);
}

function onTabKeyDown(event) {
  const buttons = TABS.map((tab) => $("tab-" + tab)).filter(Boolean);
  const index = buttons.indexOf(document.activeElement);
  if (index < 0) return;
  let next = -1;
  if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (index + 1) % buttons.length;
  else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (index - 1 + buttons.length) % buttons.length;
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = buttons.length - 1;
  else return;
  event.preventDefault();
  const target = buttons[next];
  target.focus();
  switchTab(target.dataset.tab);
}

function applyContext(context) {
  if (!context || typeof context !== "object") return;
  const nextLocale = typeof context.locale === "string" && context.locale ? context.locale : state.locale;
  const nextTitle = typeof context.pageTitle === "string" && context.pageTitle ? context.pageTitle : state.pageTitle;
  const localeChanged = nextLocale !== state.locale;
  const titleChanged = nextTitle !== state.pageTitle;
  state.locale = nextLocale;
  state.pageTitle = nextTitle;
  if (typeof context.isDark === "boolean") state.isDark = context.isDark;
  applyThemeColors(context.themeColors);
  document.documentElement.lang = state.locale;
  if (state.boot === "ready" && (localeChanged || titleChanged)) { renderAll(); return; }
  renderStaticTexts();
  renderHeader();
}

function renderAll() {
  renderStaticTexts();
  renderHeader();
  state.activities.mounted = false;
  state.activities.listEl = null;
  state.activities.countEl = null;
  if (state.tab === "activities") mountActivitiesIfNeeded();
  renderSources();
  renderReminders();
  renderOverview();
  if (state.detailItem) renderDetailBody(state.detailItem);
  if (state.banner) renderBanner();
  renderStaleNotice();
}

/* Fatal and auth states */
function reloadView() {
  try { window.location.reload(); } catch (error) { /* ignore */ }
}

function showFatalState(options) {
  clearAllTimers();
  state.boot = options.kind;
  const app = $("app");
  if (app) app.hidden = true;
  const fatal = $("fatal-state");
  if (!fatal) return;
  fatal.hidden = false;
  const title = $("fatal-title");
  if (title) title.textContent = t(options.titleKey, options.titleFallback);
  const body = $("fatal-body");
  if (body) body.textContent = t(options.bodyKey, options.bodyFallback);
  const action = $("fatal-action");
  if (action) action.textContent = t("errors.reload", "重载页面");
}

function showAuthState() {
  if (state.boot === "auth") return;
  showFatalState({
    kind: "auth", titleKey: "errors.authTitle", titleFallback: "登录状态已失效",
    bodyKey: "errors.authBody", bodyFallback: "请重新登录 AstrBot WebUI 后刷新本页。",
  });
}

/* Event wiring */
function onSourcesPanelClick(event) {
  const retryButton = event.target.closest("[data-retry-source]");
  if (retryButton) {
    const data = state.sources.data;
    if (!data) return;
    const item = data.items.find((source) => source.sourceId === retryButton.dataset.retrySource);
    if (item) openSourceRetry(item);
    return;
  }
  const diagButton = event.target.closest("[data-diag-toggle]");
  if (!diagButton) return;
  const sourceId = diagButton.dataset.diagToggle;
  const open = !state.sources.expanded.has(sourceId);
  if (open) state.sources.expanded.add(sourceId);
  else state.sources.expanded.delete(sourceId);
  const panel = document.getElementById("diag-" + sourceId);
  if (panel) panel.hidden = !open;
  for (const button of document.querySelectorAll("[data-diag-toggle]")) {
    if (button.dataset.diagToggle === sourceId) button.setAttribute("aria-expanded", open ? "true" : "false");
  }
}

function onRemindersPanelClick(event) {
  const toggle = event.target.closest("[data-reminder-toggle]");
  if (!toggle) return;
  const key = toggle.dataset.reminderKey || "";
  if (!key) return;
  const open = !state.reminders.expanded.has(key);
  if (open) state.reminders.expanded.add(key);
  else state.reminders.expanded.delete(key);
  const panel = document.getElementById("reminder-details-" + (toggle.dataset.reminderToggle || ""));
  if (panel) panel.hidden = !open;
  const scope = panelEl("reminders");
  if (!scope) return;
  for (const node of scope.querySelectorAll("[data-reminder-toggle]")) {
    if ((node.dataset.reminderKey || "") === key) node.setAttribute("aria-expanded", open ? "true" : "false");
  }
}

function onSyncButtonClick() { openSyncDialog(state.activities.filters.game || ""); }

function onBreakpointChange() {
  renderActivitiesList();
  renderSources();
  renderReminders();
}

function bindStaticEvents() {
  const tabbar = $("tabbar");
  if (tabbar) {
    tabbar.addEventListener("click", onTabClick);
    tabbar.addEventListener("keydown", onTabKeyDown);
  }
  const refresh = $("btn-refresh");
  if (refresh) refresh.addEventListener("click", () => void manualRefresh());
  const sync = $("btn-sync");
  if (sync) sync.addEventListener("click", onSyncButtonClick);

  const detail = $("detail-dialog");
  if (detail) {
    const close = $("detail-close");
    if (close) close.addEventListener("click", () => detail.close());
    detail.addEventListener("close", onDetailClosed);
  }
  const confirm = $("confirm-dialog");
  if (confirm) {
    const cancel = $("confirm-cancel");
    if (cancel) cancel.addEventListener("click", () => closeConfirm("cancel"));
    const ok = $("confirm-ok");
    if (ok) ok.addEventListener("click", () => closeConfirm("ok"));
    confirm.addEventListener("close", () => { state.confirm = null; });
  }
  const fatalAction = $("fatal-action");
  if (fatalAction) fatalAction.addEventListener("click", reloadView);

  const sourcesPanel = panelEl("sources");
  if (sourcesPanel) sourcesPanel.addEventListener("click", onSourcesPanelClick);
  const remindersPanel = panelEl("reminders");
  if (remindersPanel) remindersPanel.addEventListener("click", onRemindersPanelClick);

  window.addEventListener("hashchange", () => applyHash(false));
  document.addEventListener("visibilitychange", onVisibilityChange);
  window.addEventListener("pagehide", onPageHide);
  if (typeof desktopQuery.addEventListener === "function") desktopQuery.addEventListener("change", onBreakpointChange);
  else if (typeof desktopQuery.addListener === "function") desktopQuery.addListener(onBreakpointChange);
}

/* Boot */
async function boot() {
  renderStaticTexts();
  bindStaticEvents();

  if (!BRIDGE) {
    showFatalState(BRIDGE_FATAL);
    return;
  }

  try {
    applyContext(await withTimeout(BRIDGE.ready(), BRIDGE_TIMEOUT_MS, "bridge"));
  } catch (error) {
    showFatalState(BRIDGE_FATAL);
    return;
  }

  state.offContext = BRIDGE.onContext(applyContext);
  state.boot = "ready";
  applyHash(true);
  renderHeader();
  void loadGames();
  const ok = await refreshCore();
  if (state.boot !== "ready") return;
  if (ok) { state.failures = 0; schedulePoll(pollDelay()); }
  else scheduleAfterFailure();
}

void boot().catch((error) => {
  console.error("[game-activity-view] boot failed", error);
});
