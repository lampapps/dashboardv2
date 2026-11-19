import {
  el, setText, setClass, setTitle, readStore, writeStore, getJson, poll, refreshRelative, initThemeToggle,
  fmtClock, fmtDateTime, fmtDuration, fmtShort, fmtTick, fmtPercent, fmtBytes,
} from "./ui.js";
import { AGENT_STATUS_LABEL, HOST_SOURCE_LABEL, syncTimeline } from "./shared.js";
import { createActivityChart } from "./activity-chart.js";
import { createJobsTable } from "./jobs-table.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 15000;
const RANGES = ["7d", "30d", "90d", "1y"];
const AGENT = $("agent").dataset.agent;

const state = {
  range: RANGES.includes(readStore("agentDetailRange")) ? readStore("agentDetailRange") : "30d",
  data: null,
  etag: null,
  tz: "UTC",
  clockOffset: 0,
};
let requestSeq = 0;
const nowEstimate = () => Date.now() / 1000 + state.clockOffset;

const activityChart = createActivityChart($("c-activity"), $("activity-note"));
const jobsTable = createJobsTable({
  table: $("jobs"), body: $("jobs-body"), empty: $("jobs-empty"), count: $("jobs-count"),
  getTz: () => state.tz, showHost: false,
});

function setCard(id, value, note, tone) {
  setText($("v-" + id), value);
  if (note !== null) setText($("n-" + id), note);
  setClass($("card-" + id), tone ? "card is-" + tone : "card");
}

function render(data) {
  setText($("a-pill"), AGENT_STATUS_LABEL[data.status]);
  setClass($("a-pill"), "pill pill-" + data.status);
  const meta = [`host ${data.host}`, data.agent_type, data.version ? "v" + data.version : null];
  if (data.since && data.status !== "up") meta.push(`${AGENT_STATUS_LABEL[data.status].toLowerCase()} since ${fmtShort(data.since, state.tz)}`);
  setText($("a-meta"), meta.filter(Boolean).join(" · "));

  setText($("a-uptime"), data.uptime === null ? "–" : fmtPercent(data.uptime));
  setText($("a-uptime-note"), data.uptime === null ? "no up/down history in this range" : `up over the last ${data.range}`);
  syncTimeline($("a-timeline"), data.spans, data.start, data.end, state.tz);
  setText($("a-axis-start"), fmtTick(data.start, state.tz, false));

  const contact = $("a-contact");
  if (data.last_heartbeat) {
    contact.dataset.ts = String(data.last_heartbeat);
    setTitle(contact, fmtDateTime(data.last_heartbeat, state.tz));
  } else {
    setText(contact, "never");
  }
  const cfg = data.config;
  setText($("a-host"), `${data.host} (${HOST_SOURCE_LABEL[data.host_source]})`);
  setText($("a-grace"), `${cfg.grace_minutes} min`);
  setText($("a-alerts"), cfg.offline_alerts ? "on" : "off");
  setText($("a-ip"), cfg.allowed_ip || "any");
  setText($("a-notes"), cfg.notes || "–");
  const scheduled = data.jobs_config.flatMap((j) => j.schedules.filter((s) => s.enabled).map((s) => `${j.job_name}: ${s.cron}`));
  setText($("a-schedules"), scheduled.length ? scheduled.join("\n") : "–");

  const counts = data.counts;
  setCard("runs", String(counts.total), `last ${data.activity.days.length} days`, "");
  setCard("success", String(counts.success), counts.purged ? `${counts.purged} purged` : "", "");
  setCard("failed", String(counts.failed), null, counts.failed ? "crit" : "");
  setCard("running", String(counts.runs_now), null, "");
  setCard("bytes", fmtBytes(data.totals.bytes), `last ${data.totals.days} days`, "");
  setText($("v-avg"), data.totals.avg_runtime_seconds === null ? "–" : fmtDuration(data.totals.avg_runtime_seconds));

  activityChart.update(data.activity);
  jobsTable.render(data.job_rows);
  refreshRelative(document, nowEstimate());
}

function showError(message) {
  const box = $("banners");
  if (!message) { box.replaceChildren(); return; }
  if (!box.firstChild || box.firstChild.textContent !== message) {
    const node = el("div", "banner banner-crit", message);
    node.setAttribute("role", "alert");
    box.replaceChildren(node);
  }
}

async function refresh(force) {
  const mine = ++requestSeq;
  try {
    const result = await getJson(`/api/ui/agents/${encodeURIComponent(AGENT)}?range=${state.range}`, force ? null : state.etag);
    if (mine !== requestSeq) return;
    showError("");
    if (!result.notModified) {
      state.data = result.data;
      state.tz = result.data.timezone;
      state.clockOffset = result.data.now - Date.now() / 1000;
      render(result.data);
      state.etag = result.etag; // only after a good render, so a failed one is retried
    }
    setText($("updated"), "Updated " + fmtClock(nowEstimate(), state.tz));
  } catch (e) {
    if (mine !== requestSeq) return;
    showError(e.message === "HTTP 404" ? "This agent is not registered." : "Cannot load data from the dashboard service.");
  }
}

function markRange() {
  document.querySelectorAll("#agent-ranges button").forEach((b) => {
    b.setAttribute("aria-pressed", b.dataset.range === state.range ? "true" : "false");
  });
}

initThemeToggle($("theme-toggle"), activityChart.rebuild);
markRange();
document.querySelectorAll("#agent-ranges button").forEach((b) => {
  b.addEventListener("click", () => {
    state.range = b.dataset.range;
    writeStore("agentDetailRange", state.range);
    markRange();
    refresh(true);
  });
});
$("job-search").addEventListener("input", (event) => jobsTable.setFilter(event.target.value));
poll(() => refresh(false), POLL_MS);
setInterval(() => { if (!document.hidden) refreshRelative(document, nowEstimate()); }, 15000);
