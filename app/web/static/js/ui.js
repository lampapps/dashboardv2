// Shared helpers for every page: formatting, fetching, flicker-free DOM updates, theme.

// ---------------------------------------------------------------- formatting (pure)

const formatters = new Map();

function dtf(tz, key, options) {
  const id = tz + "|" + key;
  let f = formatters.get(id);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", Object.assign({ timeZone: tz, hourCycle: "h23" }, options));
    formatters.set(id, f);
  }
  return f;
}

function parts(ts, tz, key, options) {
  const out = {};
  dtf(tz, key, options).formatToParts(new Date(ts * 1000)).forEach((p) => { out[p.type] = p.value; });
  return out;
}

export function fmtDateTime(ts, tz) {
  const p = parts(ts, tz, "full", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", timeZoneName: "short",
  });
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second} ${p.timeZoneName}`;
}

export function fmtClock(ts, tz) {
  const p = parts(ts, tz, "clock", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return `${p.hour}:${p.minute}:${p.second}`;
}

export function fmtShort(ts, tz) {
  const p = parts(ts, tz, "short", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  return `${p.month} ${p.day}, ${p.hour}:${p.minute}`;
}

// "2026-11-09" -> "Nov 9" (a calendar label, so no timezone shift)
export function fmtDayLabel(isoDay) {
  const p = parts(Date.parse(isoDay + "T12:00:00Z") / 1000, "UTC", "day", { month: "short", day: "numeric" });
  return `${p.month} ${p.day}`;
}

export function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min ${sec} s`;
  return `${sec} s`;
}

// Coarse relative time: "just now", "5 min ago", "in 3 h".
export function fmtRelative(ts, now) {
  const delta = ts - now;
  const abs = Math.abs(delta);
  let text;
  if (abs < 45) return delta < 0 ? "just now" : "in a moment";
  if (abs < 3600) text = `${Math.round(abs / 60)} min`;
  else if (abs < 86400) text = `${Math.round(abs / 3600)} h`;
  else text = `${Math.round(abs / 86400)} d`;
  return delta < 0 ? `${text} ago` : `in ${text}`;
}

export function fmtPercent(fraction) {
  return fraction === null || fraction === undefined ? "–" : `${Math.round(fraction * 1000) / 10}%`;
}

// ---------------------------------------------------------------- DOM helpers

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// Writing identical text or classes still invalidates layout in some browsers, so skip no-ops.
export function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

export function setClass(node, className) {
  if (node.className !== className) node.className = className;
}

export function setStyle(node, property, value) {
  if (node.style[property] !== value) node.style[property] = value;
}

export function setTitle(node, text) {
  if (node.title !== text) node.title = text;
}

export function setHidden(node, hidden) {
  if (node.hidden !== hidden) node.hidden = hidden;
}

export function setData(node, key, value) {
  if (node.dataset[key] !== value) node.dataset[key] = value;
}

export function readStore(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}

export function writeStore(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* storage blocked */ }
}

// Brings `parent`'s children in line with `items` by key, touching only what changed.
// Rows keep their scroll position, hover and focus; moves happen only when the order differs.
export function reconcile(parent, items, { key, create, update }) {
  const existing = new Map();
  for (const child of parent.children) existing.set(child.dataset.key, child);
  const seen = new Set();
  items.forEach((item, index) => {
    const k = key(item);
    let node = existing.get(k);
    if (!node) {
      node = create(item);
      node.dataset.key = k;
    }
    update(node, item);
    seen.add(k);
    if (parent.children[index] !== node) parent.insertBefore(node, parent.children[index] || null);
  });
  for (const [k, node] of existing) if (!seen.has(k)) node.remove();
}

// ---------------------------------------------------------------- fetching and polling

// Returns {notModified: true} on 304, otherwise {data, etag}. Throws on network or HTTP errors.
export async function getJson(url, etag) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    const headers = etag ? { "If-None-Match": etag } : {};
    const resp = await fetch(url, { signal: controller.signal, cache: "no-store", headers });
    if (resp.status === 304) return { notModified: true, etag };
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return { data: await resp.json(), etag: resp.headers.get("ETag") };
  } finally {
    clearTimeout(timer);
  }
}

// Runs `fn` now and then every `intervalMs`, never overlapping and never while the tab is hidden.
export function poll(fn, intervalMs) {
  let timer = null;
  let busy = false;
  const run = async () => {
    if (busy) return;
    busy = true;
    try { await fn(); } finally { busy = false; }
  };
  const schedule = () => {
    timer = setTimeout(async () => {
      if (!document.hidden) await run();
      schedule();
    }, intervalMs);
  };
  document.addEventListener("visibilitychange", () => { if (!document.hidden) run(); });
  run();
  schedule();
  return { refresh: run, stop: () => clearTimeout(timer) };
}

// ---------------------------------------------------------------- theme and chart colors

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function initThemeToggle(button, onChange) {
  const label = () => {
    button.textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "Light mode" : "Dark mode";
  };
  label();
  button.addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    writeStore("theme", next);
    label();
    if (onChange) onChange(next);
  });
}
