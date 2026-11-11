import {
  el, setText, setHidden, readStore, writeStore, getJson, poll, initThemeToggle,
  fmtClock, fmtDateTime, fmtRelative, splitMatches,
} from "./ui.js";
import { agentLabel } from "./shared.js";

const $ = (id) => document.getElementById(id);
const LEVELS = ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "other"];
const PAGE = 500;
const DOM_CAP = 3000;
const NEAR_BOTTOM_PX = 40;
const SEARCH_DEBOUNCE_MS = 250;

const urlParams = new URLSearchParams(location.search);
const state = {
  logs: [],
  etag: null,
  tz: "UTC",
  clockOffset: 0,
  agent: urlParams.get("agent") || "",
  log: urlParams.get("log") || "",
  levels: new Set((urlParams.get("levels") || "").split(",").filter((l) => LEVELS.includes(l))),
  q: urlParams.get("q") || "",
  follow: true,
  loadedKey: null,
  firstId: null,
  lastId: null,
  hasMore: false,
  seq: 0,
};
const view = $("log-view");
const nowEstimate = () => Date.now() / 1000 + state.clockOffset;

// ---------------------------------------------------------------- URL and requests

function syncUrl() {
  const p = new URLSearchParams();
  if (state.agent) p.set("agent", state.agent);
  if (state.log) p.set("log", state.log);
  if (state.levels.size) p.set("levels", [...state.levels].join(","));
  if (state.q) p.set("q", state.q);
  const qs = p.toString();
  history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
}

function linesUrl(extra) {
  const p = new URLSearchParams({ agent: state.agent, log: state.log, limit: String(PAGE), ...extra });
  if (state.levels.size) p.set("levels", [...state.levels].join(","));
  if (state.q) p.set("q", state.q);
  return `/api/ui/logs/lines?${p}`;
}

// ---------------------------------------------------------------- line rendering

function fillText(node, text) {
  node.replaceChildren(...splitMatches(text, state.q).map((s) => (s.match ? el("mark", null, s.text) : document.createTextNode(s.text))));
}

function lineNode(line) {
  const node = el("div", "ll" + (line.level ? " ll-" + line.level.toLowerCase() : ""));
  node.dataset.id = String(line.id);
  node.title = fmtDateTime(line.ts, state.tz);
  fillText(node, line.text);
  return node;
}

const nearBottom = () => view.scrollHeight - view.scrollTop - view.clientHeight < NEAR_BOTTOM_PX;
const scrollToBottom = () => { view.scrollTop = view.scrollHeight; };

function updateMore() {
  setHidden($("l-more"), !state.hasMore);
}

function appendLines(lines) {
  if (!lines.length) return;
  const fragment = document.createDocumentFragment();
  lines.forEach((line) => fragment.append(lineNode(line)));
  view.append(fragment);
  state.lastId = lines[lines.length - 1].id;
  if (state.firstId === null) state.firstId = lines[0].id;
  // Keep the page light during long follow sessions; trimmed lines stay reachable with "Load earlier".
  while (view.childElementCount > DOM_CAP) {
    view.firstElementChild.remove();
    state.hasMore = true;
  }
  state.firstId = Number(view.firstElementChild.dataset.id);
  updateMore();
}

// ---------------------------------------------------------------- loading

const streamKey = () => `${state.agent}\u0000${state.log}`;

async function selectStream() {
  const mine = ++state.seq;
  state.loadedKey = streamKey();
  view.replaceChildren();
  state.firstId = state.lastId = null;
  state.hasMore = false;
  updateMore();
  $("l-download").href = `/api/ui/logs/download?agent=${encodeURIComponent(state.agent)}&log=${encodeURIComponent(state.log)}`;
  syncUrl();
  if (!state.agent || !state.log) return;
  try {
    const { data } = await getJson(linesUrl({}));
    if (mine !== state.seq) return;
    appendLines(data.lines);
    state.hasMore = data.has_more_before;
    updateMore();
    setText($("log-empty"), data.lines.length ? "" : "No lines match.");
    setHidden($("log-empty"), data.lines.length > 0);
    setFollow(true, false);
    scrollToBottom();
  } catch (e) {
    setText($("log-empty"), "Cannot load log lines.");
    setHidden($("log-empty"), false);
  }
}

async function fetchNew() {
  if (!state.agent || !state.log) return false;
  const mine = state.seq;
  try {
    const { data } = await getJson(linesUrl({ after: String(state.lastId ?? 0) }));
    if (mine !== state.seq || !data.lines.length) return false;
    appendLines(data.lines);
    setHidden($("log-empty"), true);
    return true;
  } catch (e) {
    return false;
  }
}

async function loadEarlier() {
  const mine = state.seq;
  const { data } = await getJson(linesUrl({ before: String(state.firstId) }));
  if (mine !== state.seq || !data.lines.length) { state.hasMore = false; updateMore(); return; }
  const heightBefore = view.scrollHeight;
  const fragment = document.createDocumentFragment();
  data.lines.forEach((line) => fragment.append(lineNode(line)));
  view.prepend(fragment);
  view.scrollTop += view.scrollHeight - heightBefore; // keep the reader where they were
  state.firstId = data.lines[0].id;
  state.hasMore = data.has_more_before;
  updateMore();
}

async function followTick() {
  if (state.follow && (await fetchNew())) scrollToBottom();
}

// ---------------------------------------------------------------- follow, chips, selects

function setFollow(value, fetchNow = true) {
  state.follow = value;
  $("l-follow").setAttribute("aria-pressed", value ? "true" : "false");
  if (value && fetchNow) fetchNew().then(scrollToBottom);
}

function buildChips() {
  $("l-levels").replaceChildren(...LEVELS.map((level) => {
    const button = el("button", "btn chip");
    button.type = "button";
    button.dataset.level = level;
    button.append(el("span", "chip-name", level), el("span", "chip-count"));
    button.addEventListener("click", () => {
      if (state.levels.has(level)) state.levels.delete(level); else state.levels.add(level);
      renderChips();
      selectStream();
    });
    return button;
  }));
}

function currentEntry() {
  return state.logs.find((l) => l.agent === state.agent && l.log === state.log) || null;
}

function renderChips() {
  const counts = (currentEntry() || {}).counts || {};
  $("l-levels").querySelectorAll(".chip").forEach((chip) => {
    const level = chip.dataset.level;
    chip.setAttribute("aria-pressed", state.levels.has(level) ? "true" : "false");
    setText(chip.querySelector(".chip-count"), String(counts[level] || 0));
    chip.classList.toggle("chip-empty", !counts[level]);
  });
}

// `options` is a list of [value, label]; the list is rebuilt only when it changed.
function fillSelect(select, options, selected) {
  const key = (value, label) => value + "\u0000" + label;
  const existing = [...select.options].map((o) => key(o.value, o.text));
  if (existing.length !== options.length || existing.some((k, i) => k !== key(...options[i]))) {
    select.replaceChildren(...options.map(([value, label]) => { const o = el("option", null, label); o.value = value; return o; }));
  }
  select.value = selected;
}

function renderList() {
  const empty = state.logs.length === 0;
  setHidden($("log-empty"), !empty);
  if (empty) setText($("log-empty"), "No agent has shipped logs yet. Agents push log tails to POST /api/v2/logs.");
  document.querySelector(".log-controls").hidden = empty;
  document.querySelector(".log-wrap").hidden = empty;
  if (empty) { setText($("log-summary"), ""); return false; }

  const hosts = new Map(state.logs.map((l) => [l.agent, l.host]));
  const agents = [...hosts.keys()].sort((a, b) => hosts.get(a).localeCompare(hosts.get(b)) || a.localeCompare(b));
  if (!agents.includes(state.agent)) state.agent = agents[0];
  const logsOfAgent = state.logs.filter((l) => l.agent === state.agent).map((l) => l.log);
  if (!logsOfAgent.includes(state.log)) state.log = logsOfAgent[0];
  fillSelect($("l-agent"), agents.map((a) => [a, agentLabel(hosts.get(a), a)]), state.agent);
  fillSelect($("l-log"), logsOfAgent.map((name) => [name, name]), state.log);
  renderChips();
  const entry = currentEntry();
  setText($("log-summary"), `${entry.lines.toLocaleString("en-US")} lines kept · last line ${fmtRelative(entry.last_ts, nowEstimate())}`);
  return true;
}

async function refreshList() {
  try {
    const result = await getJson("/api/ui/logs", state.etag);
    if (result.notModified) { renderList(); return; }
    state.logs = result.data.logs;
    state.tz = result.data.timezone;
    state.clockOffset = result.data.now - Date.now() / 1000;
    const ok = renderList();
    state.etag = result.etag;
    if (ok && streamKey() !== state.loadedKey) await selectStream();
    setText($("updated"), "Updated " + fmtClock(nowEstimate(), state.tz));
  } catch (e) {
    setText($("log-summary"), "Cannot load the log list.");
  }
}

// ---------------------------------------------------------------- wiring

initThemeToggle($("theme-toggle"));
buildChips();
$("l-search").value = state.q;
$("l-wrap").checked = readStore("logWrap") === "1";
view.classList.toggle("wrap", $("l-wrap").checked);

$("l-agent").addEventListener("change", (event) => {
  state.agent = event.target.value;
  state.log = "";
  renderList();
  selectStream();
});
$("l-log").addEventListener("change", (event) => {
  state.log = event.target.value;
  renderList();
  selectStream();
});

let searchTimer = null;
$("l-search").addEventListener("input", (event) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = event.target.value.trim(); selectStream(); }, SEARCH_DEBOUNCE_MS);
});
$("l-wrap").addEventListener("change", (event) => {
  writeStore("logWrap", event.target.checked ? "1" : "0");
  view.classList.toggle("wrap", event.target.checked);
});
$("l-follow").addEventListener("click", () => setFollow(!state.follow));
$("l-more").addEventListener("click", () => loadEarlier());
// Scrolling up pauses following; scrolling back to the bottom resumes it.
view.addEventListener("scroll", () => {
  const near = nearBottom();
  if (!near && state.follow) setFollow(false, false);
  else if (near && !state.follow) setFollow(true, false);
});

poll(refreshList, 15000);
poll(followTick, 3000);
