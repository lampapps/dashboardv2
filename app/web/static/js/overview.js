import {
  el, setText, setClass, readStore, writeStore, getJson, poll, refreshRelative,
  initThemeToggle, fmtClock, fmtDuration, fmtPercent,
} from "./ui.js";
import { bucketOf } from "./shared.js";
import { createActivityChart } from "./activity-chart.js";
import { createJobsTable } from "./jobs-table.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 5000;
const BADGE_REFRESH_MS = 60000;
const RANGES = [7, 14, 30, 90, 365];
const TICK_STALE_S = 900;

const state = {
  data: null,
  etag: null,
  tz: "UTC",
  clockOffset: 0,
  connectionError: null,
  days: Number(readStore("days")) || 30,
  bannerSignature: "",
};
if (RANGES.indexOf(state.days) < 0) state.days = 30;
let requestSeq = 0;

const nowEstimate = () => Date.now() / 1000 + state.clockOffset;

const activityChart = createActivityChart($("c-activity"), $("activity-note"));
const jobsTable = createJobsTable({
  table: $("jobs"), body: $("jobs-body"), empty: $("jobs-empty"), count: $("jobs-count"),
  getTz: () => state.tz, showHost: true,
});

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
  setCard("success", String(stats.success_7d), null, "");
  setCard("failed", String(stats.failed_7d), null, stats.failed_7d ? "crit" : "");
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

// ---------------------------------------------------------------- badges, wiring

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
    activityChart.update(data.activity);
    jobsTable.render(data.jobs);
    refreshRelative(document, nowEstimate());
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
      state.tz = result.data.timezone;
      state.clockOffset = result.data.now - Date.now() / 1000;
      renderAll();
      state.etag = result.etag; // only after a good render, so a failed one is retried
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
  initThemeToggle($("theme-toggle"), activityChart.rebuild);
  document.querySelectorAll("#ranges button").forEach((b) => {
    b.addEventListener("click", () => {
      state.days = Number(b.dataset.days);
      writeStore("days", String(state.days));
      markRange();
      refresh(true);
    });
  });
  const problems = $("problems-only");
  problems.checked = readStore("problemsOnly") === "1";
  jobsTable.setProblemsOnly(problems.checked);
  problems.addEventListener("change", (event) => {
    writeStore("problemsOnly", event.target.checked ? "1" : "0");
    jobsTable.setProblemsOnly(event.target.checked);
  });
  $("job-search").addEventListener("input", (event) => jobsTable.setFilter(event.target.value));
  const groupBy = $("group-by");
  groupBy.value = readStore("jobGroup") === "host" ? "host" : "none";
  jobsTable.setGroup(groupBy.value);
  groupBy.addEventListener("change", (event) => {
    writeStore("jobGroup", event.target.value);
    jobsTable.setGroup(event.target.value);
  });
}

markRange();
wire();
poll(() => refresh(false), POLL_MS);
setInterval(() => { if (!document.hidden) refreshRelative(document, nowEstimate()); }, 15000);
if (document.querySelector("img[data-badge]")) setInterval(() => { if (!document.hidden) refreshBadges(); }, BADGE_REFRESH_MS);
