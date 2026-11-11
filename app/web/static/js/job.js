import {
  el, setText, setClass, setStyle, setTitle, setHidden, setData, readStore, writeStore, reconcile, getJson, poll,
  refreshRelative, cssVar, initThemeToggle, fmtClock, fmtDateTime, fmtShort, fmtDuration, fmtBytes, fmtPercent,
  niceByteTicks,
} from "./ui.js";
import { agentLabel, bucketOf } from "./shared.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 5000;
const AGENT = $("job").dataset.agent;
const NAME = new URLSearchParams(location.search).get("name") ?? "";
const JOB_LABEL = NAME || "(unnamed)";
const QUERY = `agent=${encodeURIComponent(AGENT)}&name=${encodeURIComponent(NAME)}`;
const SIZES = [25, 50, 100];

const state = {
  tz: "UTC",
  clockOffset: 0,
  summaryEtag: null,
  targetSignature: "",
  trendSignature: "",
  runs: {
    offset: 0,
    limit: SIZES.includes(Number(readStore("runsPerPage"))) ? Number(readStore("runsPerPage")) : 25,
    total: 0,
    etag: null,
    status: "",
    target: "",
    since: "",
  },
  dialog: { id: null, etag: null, running: false },
};
let requestSeq = 0;
const nowEstimate = () => Date.now() / 1000 + state.clockOffset;
const syncClock = (data) => { state.tz = data.timezone || state.tz; if (data.now) state.clockOffset = data.now - Date.now() / 1000; };

document.title = `${JOB_LABEL} - ${AGENT} - JABS`;
setText($("j-name"), JOB_LABEL);
setText($("crumb-job"), JOB_LABEL);

function showError(message) {
  const box = $("banners");
  if (!message) { box.replaceChildren(); return; }
  if (!box.firstChild || box.firstChild.textContent !== message) {
    const node = el("div", "banner banner-crit", message);
    node.setAttribute("role", "alert");
    box.replaceChildren(node);
  }
}

// ---------------------------------------------------------------- summary and trend chart

let trend = null;

function buildTrend() {
  const text = cssVar("--muted");
  const grid = cssVar("--grid");
  let digits = 1;
  trend = new Chart($("c-trend"), {
    type: "line",
    data: {
      datasets: [
        { label: "Duration", yAxisID: "y", borderColor: cssVar("--c1"), backgroundColor: cssVar("--c1"), data: [], borderWidth: 1.6, tension: 0 },
        { label: "Size", yAxisID: "y1", borderColor: cssVar("--c2"), backgroundColor: cssVar("--c2"), data: [], borderWidth: 1.6, tension: 0 },
      ],
    },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false, parsing: false, normalized: true,
      interaction: { mode: "nearest", axis: "x", intersect: false },
      scales: {
        x: { type: "linear", ticks: { color: text, maxTicksLimit: 7, maxRotation: 0, callback: (v) => fmtShort(v, state.tz) }, grid: { color: grid } },
        y: { position: "left", beginAtZero: true, ticks: { color: text, callback: (v) => (v === 0 ? "0" : fmtDuration(v)) }, grid: { color: grid } },
        y1: {
          position: "right", beginAtZero: true, grid: { drawOnChartArea: false },
          ticks: { color: text, callback: (v) => (v === 0 ? "0" : fmtBytes(v, digits)) },
          afterBuildTicks(scale) {
            const nice = niceByteTicks(scale.min, scale.max, 4);
            if (nice.ticks.length >= 2) { digits = nice.digits; scale.ticks = nice.ticks.map((value) => ({ value })); }
          },
        },
      },
      plugins: {
        legend: { position: "bottom", labels: { color: text } },
        tooltip: {
          callbacks: {
            title: (items) => (items.length ? fmtDateTime(items[0].parsed.x, state.tz) : ""),
            label: (item) => (item.datasetIndex === 0 ? `Duration: ${fmtDuration(item.parsed.y)}` : `Size: ${fmtBytes(item.parsed.y)}`),
          },
        },
      },
    },
  });
}

function renderTrend(points, singleTarget) {
  const signature = JSON.stringify([points, singleTarget]);
  if (signature === state.trendSignature) return;
  state.trendSignature = signature;
  const colors = points.map((p) => cssVar("--st-" + bucketOf(p.status)));
  const [duration, size] = trend.data.datasets;
  // Runs of different targets interleave, so a line between them would only mislead.
  duration.showLine = size.showLine = singleTarget;
  duration.data = points.map((p) => ({ x: p.started_at, y: p.runtime_seconds }));
  duration.pointBackgroundColor = colors;
  duration.pointBorderColor = colors;
  duration.pointRadius = 3;
  size.data = points.filter((p) => p.bytes_processed !== null).map((p) => ({ x: p.started_at, y: p.bytes_processed }));
  size.pointRadius = 2;
  trend.update("none");
  setText($("trend-note"), points.length ? `Last ${points.length} finished runs; red points failed.` : "No finished runs yet.");
}

function setCard(id, value, note) {
  setText($("v-" + id), value);
  if (note !== null) setText($("n-" + id), note);
}

function renderSummary(data) {
  const rollup = data.rollup;
  const counts = { success: 0, failed: 0, stopped: 0, running: 0, other: 0 };
  Object.keys(rollup.status_counts).forEach((s) => { counts[bucketOf(s)] += rollup.status_counts[s]; });
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const judged = counts.success + counts.failed;

  setText($("j-pill"), rollup.last_status);
  setClass($("j-pill"), "pill pill-" + bucketOf(rollup.last_status));
  const where = [`host ${data.host}`];
  if (data.host !== AGENT) where.push(`agent ${AGENT}`);
  where.push(data.schedule ? `schedule ${data.schedule}` : "no schedule reported");
  setText($("j-meta"), where.join(" · "));
  setText($("crumb-agent"), agentLabel(data.host, AGENT));
  document.title = `${JOB_LABEL} - ${agentLabel(data.host, AGENT)} - JABS`;

  setCard("runs", String(total), `${counts.failed} failed`);
  setCard("rate", judged ? fmtPercent(counts.success / judged) : "–", null);
  setCard("avg", data.avg_runtime_seconds === null ? "–" : fmtDuration(data.avg_runtime_seconds), null);
  setCard("bytes", fmtBytes(data.bytes_processed || 0), null);
  setCard("last", fmtShort(rollup.last_started_at, state.tz), null);
  setData($("n-last"), "ts", String(rollup.last_started_at));
  if (rollup.next_event_at) {
    setCard("next", fmtShort(rollup.next_event_at, state.tz), null);
    setText($("l-next"), rollup.schedule_drifted ? "Next run ⚠" : "Next run");
    setData($("n-next"), "ts", String(rollup.next_event_at));
    setTitle($("card-next"), rollup.schedule_drifted ? "The last run did not match the reported schedule" : "");
  } else {
    setCard("next", "–", "no schedule");
    setText($("l-next"), "Next run");
    setTitle($("card-next"), "");
    if ($("n-next").dataset.ts !== undefined) delete $("n-next").dataset.ts;
  }

  const signature = JSON.stringify(data.targets);
  if (signature !== state.targetSignature) {
    state.targetSignature = signature;
    const select = $("f-target");
    const keep = select.value;
    select.replaceChildren(el("option", null, "All"), ...data.targets.map((t) => {
      const option = el("option", null, t.target_label && t.target_label !== t.target_id ? `${t.target_label} (${t.target_id})` : t.target_id || "(none)");
      option.value = t.target_id;
      return option;
    }));
    select.firstChild.value = "";
    select.value = keep;
    setHidden(select.closest("label"), data.targets.length < 2);
  }
  renderTrend(data.trend, data.targets.length < 2);
}

async function refreshSummary(force) {
  const mine = ++requestSeq;
  try {
    const result = await getJson(`/api/ui/job?${QUERY}`, force ? null : state.summaryEtag);
    if (mine !== requestSeq) return;
    showError("");
    if (!result.notModified) {
      syncClock(result.data);
      renderSummary(result.data);
      refreshRelative(document, nowEstimate());
      state.summaryEtag = result.etag; // only after a good render, so a failed one is retried
    }
    setText($("updated"), "Updated " + fmtClock(nowEstimate(), state.tz));
  } catch (e) {
    if (mine !== requestSeq) return;
    showError(e.message === "HTTP 404" ? `No runs have been reported for '${JOB_LABEL}' on ${AGENT}.` : "Cannot load data from the dashboard service.");
  }
}

// ---------------------------------------------------------------- runs table

const pad = (n) => n.toLocaleString("en-US");

function createRunRow() {
  const tr = el("tr", "clickable");
  const start = el("td");
  const link = el("a", "job-name tnum");
  const when = el("div", "sub");
  start.append(link, when);
  const target = el("td");
  const targetName = el("div");
  const targetId = el("div", "sub");
  target.append(targetName, targetId);
  const status = el("td");
  const pill = el("span", "pill");
  const progress = el("div", "progress");
  progress.append(el("div"));
  progress.hidden = true;
  status.append(pill, progress);
  const duration = el("td", "tnum");
  const files = el("td", "num tnum");
  const size = el("td", "num tnum");
  const rate = el("td", "num tnum");
  const message = el("td", "msg");
  tr.append(start, target, status, duration, files, size, rate, message);
  tr._refs = { link, when, targetName, targetId, pill, progress, bar: progress.firstChild, duration, files, size, rate, message };
  return tr;
}

function updateRunRow(tr, run) {
  const r = tr._refs;
  setText(r.link, fmtShort(run.started_at, state.tz));
  const href = `#run=${run.id}`;
  if (r.link.getAttribute("href") !== href) r.link.setAttribute("href", href);
  setTitle(r.link, fmtDateTime(run.started_at, state.tz));
  setData(r.when, "ts", String(run.started_at));
  const label = run.target_label || run.target_id;
  setText(r.targetName, label || "–");
  setText(r.targetId, run.target_label && run.target_id !== run.target_label ? run.target_id : "");
  setText(r.pill, run.status);
  setClass(r.pill, "pill pill-" + bucketOf(run.status));
  const running = bucketOf(run.status) === "running";
  setHidden(r.progress, !running);
  if (running) setStyle(r.bar, "width", run.percent_complete === null ? "100%" : `${Math.max(0, Math.min(100, run.percent_complete))}%`);
  setText(r.duration, run.runtime_seconds === null ? (running ? "…" : "–") : fmtDuration(run.runtime_seconds));
  setText(r.files, run.files_count === null ? "–" : pad(run.files_count));
  setText(r.size, run.bytes_processed === null ? "–" : fmtBytes(run.bytes_processed));
  setText(r.rate, run.bytes_processed && run.runtime_seconds ? fmtBytes(run.bytes_processed / run.runtime_seconds) + "/s" : "–");
  const text = run.error_message || run.message || "";
  setText(r.message, text);
  setTitle(r.message, text);
}

function renderPager() {
  const { offset, limit, total } = state.runs;
  const pages = Math.max(1, Math.ceil(total / limit));
  const page = Math.floor(offset / limit) + 1;
  setText($("pager-text"), `Page ${page} of ${pages} · ${total} run${total === 1 ? "" : "s"}${offset > 0 ? " · live updates paused" : ""}`);
  $("p-first").disabled = $("p-prev").disabled = offset === 0;
  $("p-next").disabled = $("p-last").disabled = offset + limit >= total;
}

async function refreshRuns(force) {
  const r = state.runs;
  if (r.offset > 0 && !force) return; // keep an older page from shifting under the reader
  const params = new URLSearchParams({ agent: AGENT, name: NAME, limit: String(r.limit), offset: String(r.offset) });
  ["status", "target", "since"].forEach((key) => { if (r[key]) params.set(key, r[key]); });
  try {
    const result = await getJson(`/api/ui/job/runs?${params}`, force ? null : r.etag);
    if (result.notModified) return;
    r.total = result.data.total;
    if (r.offset > 0 && r.offset >= r.total) { // the list shrank under us
      r.offset = Math.max(0, (Math.ceil(r.total / r.limit) - 1) * r.limit);
      await refreshRuns(true);
      return;
    }
    reconcile($("runs-body"), result.data.runs, { key: (run) => String(run.id), create: createRunRow, update: updateRunRow });
    setHidden($("runs-empty"), result.data.runs.length > 0);
    renderPager();
    refreshRelative(document, nowEstimate());
    r.etag = result.etag;
  } catch (e) {
    // The summary request reports connection problems; keep the rows already shown.
  }
}

function gotoOffset(offset) {
  state.runs.offset = Math.max(0, offset);
  refreshRuns(true);
}

// ---------------------------------------------------------------- run dialog

const FACTS = [
  ["host", "Host"], ["agent", "Agent"], ["status", "Status"], ["started", "Started"], ["finished", "Finished"], ["duration", "Duration"],
  ["type", "Type"], ["target", "Target"], ["source", "Source"], ["destination", "Destination"],
  ["files", "Files"], ["size", "Size"], ["compressed", "Compressed"], ["progress", "Progress"],
  ["error", "Error"], ["external", "Artifact ID"], ["runid", "Run ID"],
];
const factRefs = {};
FACTS.forEach(([key, label]) => {
  const dt = el("dt", null, label);
  const dd = el("dd");
  $("d-facts").append(dt, dd);
  factRefs[key] = { dt, dd };
});
const statusPill = el("span", "pill");
factRefs.status.dd.append(statusPill);

function setFact(key, value) {
  const f = factRefs[key];
  const empty = value === null || value === undefined || value === "";
  setHidden(f.dt, empty);
  setHidden(f.dd, empty);
  if (!empty && key !== "status") setText(f.dd, String(value));
}

function createEventRow() {
  const tr = el("tr");
  const time = el("td", "tnum");
  const stage = el("td");
  const type = el("td");
  const message = el("td", "msg-wrap");
  tr.append(time, stage, type, message);
  tr._refs = { time, stage, type, message };
  return tr;
}

function updateEventRow(tr, event) {
  const r = tr._refs;
  setText(r.time, fmtDateTime(event.timestamp, state.tz));
  setText(r.stage, event.stage || "");
  setText(r.type, event.event_type + (event.error_code !== null ? ` (code ${event.error_code})` : ""));
  setText(r.message, event.message);
}

function renderRun(detail) {
  const run = detail.run;
  const running = bucketOf(run.status) === "running";
  state.dialog.running = running;
  setText($("d-title"), `Run · ${fmtShort(run.started_at, state.tz)}`);
  setText(statusPill, run.status);
  setClass(statusPill, "pill pill-" + bucketOf(run.status));
  setFact("host", run.host);
  setFact("agent", run.agent !== run.host ? run.agent : "");
  setFact("started", fmtDateTime(run.started_at, state.tz));
  setFact("finished", run.completed_at ? fmtDateTime(run.completed_at, state.tz) : "");
  setFact("duration", run.runtime_seconds === null ? "" : fmtDuration(run.runtime_seconds));
  setFact("type", run.backup_type);
  setFact("target", run.target_label && run.target_label !== run.target_id ? `${run.target_label} (${run.target_id})` : run.target_id);
  setFact("source", run.source);
  setFact("destination", run.destination);
  setFact("files", run.files_count === null ? "" : pad(run.files_count));
  setFact("size", run.bytes_processed === null ? "" : fmtBytes(run.bytes_processed));
  setFact("compressed", run.bytes_compressed === null ? "" : fmtBytes(run.bytes_compressed));
  const progress = [];
  if (running && run.percent_complete !== null) progress.push(`${Math.round(run.percent_complete)}%`);
  if (running && run.eta_seconds) progress.push("ETA " + fmtDuration(run.eta_seconds));
  if (running && run.current_item) progress.push(run.current_item);
  setFact("progress", progress.join(" · "));
  setFact("error", run.error_message ? run.error_message + (run.error_code !== null ? ` (code ${run.error_code})` : "") : "");
  setFact("external", run.external_id);
  setFact("runid", run.run_id);
  reconcile($("d-events-body"), detail.events, { key: (e) => String(e.id), create: createEventRow, update: updateEventRow });
  setText($("d-note"), detail.events_truncated ? "Only the first events are shown." : "");
}

async function loadRun(force) {
  const id = state.dialog.id;
  if (id === null) return;
  try {
    const result = await getJson(`/api/ui/runs/${id}`, force ? null : state.dialog.etag);
    if (id !== state.dialog.id || result.notModified) return;
    renderRun(result.data);
    state.dialog.etag = result.etag;
  } catch (e) {
    if (id === state.dialog.id && e.message === "HTTP 404") {
      setText($("d-title"), "Run not found");
      setText($("d-note"), "It may have been removed by retention.");
    }
  }
}

function openRun(id) {
  if (state.dialog.id === id && $("run-dialog").open) return;
  state.dialog = { id, etag: null, running: false };
  setText($("d-note"), "");
  if (!$("run-dialog").open) $("run-dialog").showModal();
  loadRun(true);
}

function onHashChange() {
  const match = /^#run=(\d+)$/.exec(location.hash);
  if (match) openRun(Number(match[1]));
  else if ($("run-dialog").open) $("run-dialog").close();
}

$("run-dialog").addEventListener("close", () => {
  state.dialog.id = null;
  if (location.hash.startsWith("#run=")) history.replaceState(null, "", location.pathname + location.search);
});
$("run-dialog").addEventListener("click", (event) => { if (event.target === $("run-dialog")) $("run-dialog").close(); });
$("d-close").addEventListener("click", () => $("run-dialog").close());
window.addEventListener("hashchange", onHashChange);

// ---------------------------------------------------------------- wiring

$("runs-body").addEventListener("click", (event) => {
  if (event.target.closest("a")) return;
  const row = event.target.closest("tr");
  if (row) location.hash = "run=" + row.dataset.key;
});

[["f-status", "status"], ["f-target", "target"], ["f-since", "since"]].forEach(([id, key]) => {
  $(id).addEventListener("change", (event) => {
    state.runs[key] = event.target.value;
    gotoOffset(0);
  });
});
$("p-size").value = String(state.runs.limit);
$("p-size").addEventListener("change", (event) => {
  state.runs.limit = Number(event.target.value);
  writeStore("runsPerPage", String(state.runs.limit));
  gotoOffset(0);
});
$("p-first").addEventListener("click", () => gotoOffset(0));
$("p-prev").addEventListener("click", () => gotoOffset(state.runs.offset - state.runs.limit));
$("p-next").addEventListener("click", () => gotoOffset(state.runs.offset + state.runs.limit));
$("p-last").addEventListener("click", () => gotoOffset((Math.ceil(state.runs.total / state.runs.limit) - 1) * state.runs.limit));

initThemeToggle($("theme-toggle"));
document.addEventListener("themechange", () => {
  trend.destroy();
  buildTrend();
  state.trendSignature = "";
  refreshSummary(true);
});
buildTrend();
// The first summary sets the timezone, so a deep-linked run opens only after it.
refreshSummary(true).then(() => {
  onHashChange();
  poll(async () => {
    await refreshSummary(false);
    await refreshRuns(false);
    if (state.dialog.id !== null && state.dialog.running) await loadRun(false);
  }, POLL_MS);
});
setInterval(() => { if (!document.hidden) refreshRelative(document, nowEstimate()); }, 15000);
