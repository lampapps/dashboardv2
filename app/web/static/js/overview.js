import {
  el, setText, setClass, setStyle, setTitle, setHidden, setData, readStore, writeStore, reconcile, getJson, poll,
  cssVar, initThemeToggle, fmtDateTime, fmtClock, fmtShort, fmtDayLabel, fmtDuration, fmtRelative, fmtPercent,
} from "./ui.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 5000;
const BADGE_REFRESH_MS = 60000;
const RANGES = [7, 14, 30, 90, 365];
const BUCKETS = ["success", "failed", "stopped", "running", "other"];
const BUCKET_LABEL = { success: "Success", failed: "Failed", stopped: "Stopped", running: "Running", other: "Other" };
const BUCKET_OF = {
  success: "success", completed: "success", purged: "success",
  failed: "failed", error: "failed", stopped: "stopped", running: "running",
};
// Lower rank sorts first when ordering by status, so problems rise to the top.
const ATTENTION_RANK = { failed: 0, running: 1, stopped: 2, other: 3, success: 4 };
const TICK_STALE_S = 900;

const bucketOf = (status) => BUCKET_OF[status] || "other";

const state = {
  data: null,
  etag: null,
  tz: "UTC",
  clockOffset: 0,
  connectionError: null,
  days: Number(readStore("days")) || 30,
  sort: { key: readStore("sortKey") || "last", dir: readStore("sortDir") || "desc" },
  filter: "",
  problemsOnly: readStore("problemsOnly") === "1",
  chartSignature: "",
  bannerSignature: "",
};
if (RANGES.indexOf(state.days) < 0) state.days = 30;

const nowEstimate = () => Date.now() / 1000 + state.clockOffset;
let requestSeq = 0;

// ---------------------------------------------------------------- summary cards and banners

function setCard(id, value, note, tone) {
  setText($("v-" + id), value);
  if (note !== null) setText($("n-" + id), note);
  setClass($("card-" + id), tone ? "card is-" + tone : "card");
}

function renderCards(stats) {
  const down = stats.agents_total - stats.agents_up;
  setCard("agents", stats.agents_total ? `${stats.agents_up} / ${stats.agents_total}` : "0",
    stats.agents_total === 0 ? "none registered" : down ? `${down} not reporting` : "all reporting",
    stats.agents_total === 0 ? "" : down ? "warn" : "ok");
  setCard("running", String(stats.running), stats.running ? "in progress" : "idle", "");
  setCard("success", String(stats.success_24h), null, "");
  setCard("failed", String(stats.failed_24h), null, stats.failed_24h ? "crit" : "");
  setCard("failing", String(stats.jobs_failing), `of ${stats.jobs_total} jobs`, stats.jobs_failing ? "crit" : stats.jobs_total ? "ok" : "");
  setCard("rate", fmtPercent(stats.success_rate_7d), null, "");
}

function banner(level, text) {
  const node = el("div", "banner banner-" + level, text);
  node.setAttribute("role", level === "crit" ? "alert" : "status");
  return node;
}

function bannerList(data) {
  const list = [];
  if (state.connectionError) {
    list.push(["crit", "Cannot load data from the dashboard service: " + state.connectionError]);
  }
  if (data) {
    const down = data.agents.filter((a) => a.status === "down").map((a) => a.name);
    if (down.length) list.push(["warn", "Not reporting: " + down.join(", ")]);
    const failing = data.jobs.filter((j) => bucketOf(j.last_status) === "failed");
    if (failing.length) {
      const names = failing.slice(0, 5).map((j) => j.job_name || "(unnamed)").join(", ");
      list.push(["crit", `${failing.length} job${failing.length === 1 ? "" : "s"} failed on the last run: ${names}${failing.length > 5 ? ", …" : ""}`]);
    }
    if (data.tick_age_s === null) {
      list.push(["info", "Periodic tasks have not run yet. Alerts and rollups start after the first timer run."]);
    } else if (data.tick_age_s > TICK_STALE_S) {
      list.push(["warn", `Periodic tasks last ran ${fmtDuration(data.tick_age_s)} ago; check the timer.`]);
    }
  }
  return list;
}

function renderBanners(data) {
  const list = bannerList(data);
  const signature = JSON.stringify(list);
  if (signature === state.bannerSignature) return;
  state.bannerSignature = signature;
  $("banners").replaceChildren(...list.map(([level, text]) => banner(level, text)));
}

// ---------------------------------------------------------------- activity chart

let chart = null;

function chartColors() {
  return { text: cssVar("--muted"), grid: cssVar("--grid"), bucket: (b) => cssVar("--st-" + b) };
}

function buildChart() {
  const c = chartColors();
  chart = new Chart($("c-activity"), {
    type: "bar",
    data: {
      labels: [],
      datasets: BUCKETS.map((b) => ({ label: BUCKET_LABEL[b], data: [], backgroundColor: c.bucket(b), borderWidth: 0, stack: "runs" })),
    },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { stacked: true, ticks: { color: c.text, maxRotation: 0, autoSkipPadding: 12 }, grid: { display: false } },
        y: { stacked: true, beginAtZero: true, ticks: { color: c.text, precision: 0 }, grid: { color: c.grid } },
      },
      plugins: {
        legend: {
          position: "bottom",
          labels: {
            color: c.text,
            // Hide legend entries for outcomes that never occur in the range.
            filter: (item, data) => data.datasets[item.datasetIndex].data.some((n) => n > 0),
          },
        },
      },
    },
  });
}

// Chart.js keeps colors it already resolved, so a theme switch rebuilds the chart (a rare, user-driven event).
function applyChartTheme() {
  if (!chart) return;
  chart.destroy();
  buildChart();
  state.chartSignature = "";
  if (state.data) renderActivity(state.data.activity);
}

function renderActivity(activity) {
  const signature = JSON.stringify(activity);
  if (signature === state.chartSignature) return;
  state.chartSignature = signature;
  chart.data.labels = activity.days.map(fmtDayLabel);
  chart.data.datasets.forEach((ds, i) => { ds.data = activity.series[BUCKETS[i]]; });
  chart.update("none");
  const total = BUCKETS.reduce((sum, b) => sum + activity.series[b].reduce((a, n) => a + n, 0), 0);
  setText($("activity-note"), total ? `${total} runs started in the last ${activity.days.length} days.` : "No runs in this range.");
}

// ---------------------------------------------------------------- jobs table

function createRow() {
  const tr = el("tr");
  const job = el("td");
  const jobName = el("div", "job-name");
  const jobAgent = el("div", "sub");
  job.append(jobName, jobAgent);

  const last = el("td");
  const lastTop = el("div");
  const pill = el("span", "pill");
  const when = el("span", "when");
  when.dataset.rel = "ago";
  lastTop.append(pill, when);
  const lastDetail = el("div", "sub");
  const lastError = el("div", "sub err");
  const progress = el("div", "progress");
  const bar = el("div");
  progress.append(bar);
  progress.hidden = true;
  last.append(lastTop, lastDetail, progress, lastError);

  const next = el("td");
  const nextTime = el("div", "tnum");
  const nextRel = el("div", "sub");
  nextRel.dataset.rel = "in";
  next.append(nextTime, nextRel);

  const recent = el("td");
  const pips = el("div", "pips");
  recent.append(pips);

  const totals = el("td", "tnum");
  tr.append(job, last, next, recent, totals);
  tr._refs = { jobName, jobAgent, pill, when, lastDetail, lastError, progress, bar, nextTime, nextRel, pips, totals, pipSig: "" };
  return tr;
}

function totalsText(counts) {
  const sums = { success: 0, failed: 0, stopped: 0, running: 0, other: 0 };
  Object.keys(counts).forEach((status) => { sums[bucketOf(status)] += counts[status]; });
  const text = [];
  if (sums.success) text.push(`${sums.success} ok`);
  if (sums.failed) text.push(`${sums.failed} failed`);
  if (sums.stopped) text.push(`${sums.stopped} stopped`);
  if (sums.running) text.push(`${sums.running} running`);
  if (sums.other) text.push(`${sums.other} other`);
  return text.join(" · ") || "–";
}

function updateRow(tr, job) {
  const r = tr._refs;
  const bucket = bucketOf(job.last_status);
  setText(r.jobName, job.job_name || "(unnamed)");
  setText(r.jobAgent, job.agent);
  setText(r.pill, job.last_status);
  setClass(r.pill, "pill pill-" + bucket);
  setData(r.when, "ts", String(job.last_started_at));
  setText(r.lastError, bucket === "failed" && job.last_error ? job.last_error : "");

  const progress = job.last_progress;
  setHidden(r.progress, !progress);
  let detail = bucket === "running" || job.last_runtime_seconds === null ? "" : "took " + fmtDuration(job.last_runtime_seconds);
  if (progress) {
    const percent = progress.percent_complete;
    setStyle(r.bar, "width", percent === null ? "100%" : `${Math.max(0, Math.min(100, percent))}%`);
    const bits = [percent === null ? "running" : `${Math.round(percent)}%`];
    if (progress.eta_seconds) bits.push("ETA " + fmtDuration(progress.eta_seconds));
    if (progress.current_item) bits.push(progress.current_item);
    detail = bits.join(" · ");
  }
  setText(r.lastDetail, detail);

  if (job.next_event_at) {
    setText(r.nextTime, fmtShort(job.next_event_at, state.tz) + (job.schedule_drifted ? " ⚠" : ""));
    setTitle(r.nextTime, job.schedule_drifted ? "The last run did not match the reported schedule" : "");
    setData(r.nextRel, "ts", String(job.next_event_at));
  } else {
    setText(r.nextTime, "–");
    setTitle(r.nextTime, "");
    if (r.nextRel.dataset.ts !== undefined) delete r.nextRel.dataset.ts;
    setText(r.nextRel, "no schedule");
  }

  const signature = job.recent.map((run) => run.status + run.started_at).join();
  if (signature !== r.pipSig) {
    r.pipSig = signature;
    r.pips.replaceChildren(...job.recent.map((run) => {
      const pip = el("span", "pip pip-" + bucketOf(run.status));
      pip.title = `${fmtDateTime(run.started_at, state.tz)} · ${run.status}`;
      return pip;
    }));
  }
  setText(r.totals, totalsText(job.status_counts));
}

const SORTERS = {
  job: (a, b) => a.job_name.localeCompare(b.job_name) || a.agent.localeCompare(b.agent),
  last: (a, b) => a.last_started_at - b.last_started_at,
  next: (a, b) => (a.next_event_at ?? Infinity) - (b.next_event_at ?? Infinity),
  status: (a, b) => ATTENTION_RANK[bucketOf(a.last_status)] - ATTENTION_RANK[bucketOf(b.last_status)]
    || b.last_started_at - a.last_started_at,
};

function visibleJobs(jobs) {
  const needle = state.filter.trim().toLowerCase();
  const rows = jobs.filter((j) => {
    if (needle && !(j.job_name + " " + j.agent).toLowerCase().includes(needle)) return false;
    if (state.problemsOnly && ["success", "running"].includes(bucketOf(j.last_status))) return false;
    return true;
  });
  const compare = SORTERS[state.sort.key] || SORTERS.last;
  const sign = state.sort.dir === "asc" ? 1 : -1;
  return rows.sort((a, b) => sign * compare(a, b) || a.job_name.localeCompare(b.job_name));
}

function renderJobs(jobs) {
  const rows = visibleJobs(jobs);
  reconcile($("jobs-body"), rows, {
    key: (j) => j.agent + "\u0000" + j.job_name,
    create: createRow,
    update: updateRow,
  });
  const empty = $("jobs-empty");
  setHidden(empty, rows.length > 0);
  setText(empty, jobs.length ? "No jobs match the filter." : "No runs have been reported yet.");
  setText($("jobs-count"), rows.length === jobs.length ? `${jobs.length} job${jobs.length === 1 ? "" : "s"}` : `${rows.length} of ${jobs.length} jobs`);
  document.querySelectorAll("#jobs th[aria-sort]").forEach((th) => {
    const button = th.querySelector("[data-sort]");
    const active = button && button.dataset.sort === state.sort.key;
    th.setAttribute("aria-sort", active ? (state.sort.dir === "asc" ? "ascending" : "descending") : "none");
  });
}

// ---------------------------------------------------------------- relative times, badges, wiring

function refreshRelative() {
  const now = nowEstimate();
  document.querySelectorAll("[data-ts]").forEach((node) => {
    setText(node, fmtRelative(Number(node.dataset.ts), now));
  });
}

// Preload a fresh copy and swap it in, so a badge never blanks while reloading.
function refreshBadges() {
  document.querySelectorAll("img[data-badge]").forEach((img) => {
    if (!img.dataset.src) img.dataset.src = img.getAttribute("src");
    let url;
    try { url = new URL(img.dataset.src, location.href); } catch (e) { return; }
    url.searchParams.set("_", String(Date.now()));
    const probe = new Image();
    probe.onload = () => { img.src = probe.src; };
    probe.src = url.toString();
  });
}

function renderAll() {
  const data = state.data;
  if (data) {
    renderCards(data.stats);
    renderActivity(data.activity);
    renderJobs(data.jobs);
    refreshRelative();
  }
  renderBanners(data);
  $("cards").classList.toggle("stale", Boolean(state.connectionError));
}

async function refresh(force) {
  const mine = ++requestSeq;
  try {
    const result = await getJson(`/api/ui/overview?days=${state.days}`, force ? null : state.etag);
    if (mine !== requestSeq) return; // a newer request (e.g. a range change) superseded this one
    state.connectionError = null;
    if (!result.notModified) {
      state.data = result.data;
      state.etag = result.etag;
      state.tz = result.data.timezone;
      state.clockOffset = result.data.now - Date.now() / 1000;
      renderAll();
    } else {
      renderBanners(state.data);
      $("cards").classList.remove("stale");
    }
    setText($("updated"), "Updated " + fmtClock(nowEstimate(), state.tz));
  } catch (e) {
    if (mine !== requestSeq) return;
    state.connectionError = e.message;
    renderBanners(state.data);
    $("cards").classList.add("stale");
  }
}

function markRange() {
  document.querySelectorAll("#ranges button").forEach((b) => {
    b.setAttribute("aria-pressed", Number(b.dataset.days) === state.days ? "true" : "false");
  });
}

function wire() {
  initThemeToggle($("theme-toggle"), applyChartTheme);
  document.querySelectorAll("#ranges button").forEach((b) => {
    b.addEventListener("click", () => {
      state.days = Number(b.dataset.days);
      writeStore("days", String(state.days));
      markRange();
      refresh(true);
    });
  });
  $("problems-only").checked = state.problemsOnly;
  $("problems-only").addEventListener("change", (event) => {
    state.problemsOnly = event.target.checked;
    writeStore("problemsOnly", state.problemsOnly ? "1" : "0");
    if (state.data) renderJobs(state.data.jobs);
  });
  $("job-search").addEventListener("input", (event) => {
    state.filter = event.target.value;
    if (state.data) renderJobs(state.data.jobs);
  });
  document.querySelectorAll("#jobs [data-sort]").forEach((button) => {
    button.addEventListener("click", () => {
      const key = button.dataset.sort;
      const dir = state.sort.key === key ? (state.sort.dir === "asc" ? "desc" : "asc") : (key === "last" ? "desc" : "asc");
      state.sort = { key, dir };
      writeStore("sortKey", state.sort.key);
      writeStore("sortDir", state.sort.dir);
      if (state.data) renderJobs(state.data.jobs);
    });
  });
}

markRange();
buildChart();
wire();
poll(() => refresh(false), POLL_MS);
setInterval(() => { if (!document.hidden) refreshRelative(); }, 15000);
if (document.querySelector("img[data-badge]")) setInterval(() => { if (!document.hidden) refreshBadges(); }, BADGE_REFRESH_MS);
