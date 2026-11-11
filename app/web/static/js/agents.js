import {
  el, setText, setClass, setStyle, setTitle, setHidden, setData, readStore, writeStore, reconcile, getJson, poll,
  initThemeToggle, refreshRelative, fmtClock, fmtDateTime, fmtRelative, fmtShort, fmtTick, fmtPercent,
} from "./ui.js";
import { AGENT_STATUS_LABEL as STATUS_LABEL, agentHref, bucketOf, groupByHost, groupKey, HOST_SOURCE_LABEL, syncTimeline } from "./shared.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 15000;
const RANGES = ["24h", "7d", "30d", "90d"];

const state = {
  range: RANGES.includes(readStore("agentRange")) ? readStore("agentRange") : "7d",
  group: readStore("agentGroup") === "none" ? "none" : "host",
  data: null,
  etag: null,
  tz: "UTC",
  clockOffset: 0,
  connectionError: false,
};
let requestSeq = 0;

const nowEstimate = () => Date.now() / 1000 + state.clockOffset;
const rangeIsIntraday = () => state.range === "24h";

// ---------------------------------------------------------------- cards

function fact(list, label) {
  const dt = el("dt", null, label);
  const dd = el("dd");
  list.append(dt, dd);
  return dd;
}

function createCard() {
  const card = el("article", "agent-card");

  const head = el("div", "agent-head");
  const name = el("h3", "agent-name");
  const nameLink = el("a");
  const pill = el("span", "pill");
  name.append(nameLink);
  head.append(name, pill);
  const meta = el("div", "sub");

  const uptimeRow = el("div", "uptime-row");
  const uptime = el("span", "uptime-value tnum");
  const uptimeLabel = el("span", "sub");
  uptimeRow.append(uptime, uptimeLabel);
  const timeline = el("div", "timeline");
  timeline.setAttribute("role", "img");
  const axis = el("div", "timeline-axis sub");
  const axisStart = el("span");
  const axisEnd = el("span", null, "now");
  axis.append(axisStart, axisEnd);

  const facts = el("dl", "facts");
  const host = fact(facts, "Host");
  const lastContact = fact(facts, "Last contact");
  const jobs = fact(facts, "Jobs");
  const lastRun = fact(facts, "Last run");
  const lastRunPill = el("span", "pill");
  const lastRunText = el("span");
  lastRun.append(lastRunPill, lastRunText);

  const details = el("details", "config");
  details.append(el("summary", null, "Configuration"));
  const cfg = el("dl", "facts");
  const refs = {
    enabled: fact(cfg, "Enabled"), grace: fact(cfg, "Grace period"), alerts: fact(cfg, "Offline alerts"),
    ip: fact(cfg, "Allowed IP"), notes: fact(cfg, "Notes"), schedules: fact(cfg, "Schedules"),
  };
  details.append(cfg);

  card.append(head, meta, uptimeRow, timeline, axis, facts, details);
  card._refs = { nameLink, pill, meta, uptime, uptimeLabel, timeline, axisStart, host, lastContact, jobs, lastRunPill, lastRunText, lastRun, ...refs };
  return card;
}

function updateCard(card, agent) {
  const r = card._refs;
  const data = state.data;
  setText(r.nameLink, agent.name);
  if (r.nameLink.getAttribute("href") !== agentHref(agent.name)) r.nameLink.href = agentHref(agent.name);
  setText(r.pill, STATUS_LABEL[agent.status]);
  setClass(r.pill, "pill pill-" + agent.status);
  const meta = [agent.agent_type, agent.version ? "v" + agent.version : null];
  if (agent.since && agent.status !== "up") meta.push(`${STATUS_LABEL[agent.status].toLowerCase()} since ${fmtShort(agent.since, state.tz)}`);
  setText(r.meta, meta.filter(Boolean).join(" · "));

  setText(r.host, agent.host);
  setTitle(r.host, "Host " + HOST_SOURCE_LABEL[agent.host_source]);
  setText(r.uptime, agent.uptime === null ? "–" : fmtPercent(agent.uptime));
  setText(r.uptimeLabel, agent.uptime === null ? "no up/down history in this range" : `up over the last ${data.range}`);
  syncTimeline(r.timeline, agent.spans, data.start, data.end, state.tz);
  setText(r.axisStart, fmtTick(data.start, state.tz, rangeIsIntraday()));

  if (agent.last_heartbeat) {
    setData(r.lastContact, "ts", String(agent.last_heartbeat));
    setTitle(r.lastContact, fmtDateTime(agent.last_heartbeat, state.tz));
  } else {
    if (r.lastContact.dataset.ts !== undefined) delete r.lastContact.dataset.ts;
    setText(r.lastContact, "never");
  }
  const failing = agent.jobs.failing ? ` · ${agent.jobs.failing} failing` : "";
  setText(r.jobs, agent.jobs.total ? `${agent.jobs.total} job${agent.jobs.total === 1 ? "" : "s"}${failing}` : "none yet");

  const run = agent.last_run;
  setHidden(r.lastRunPill, !run);
  if (run) {
    const bucket = bucketOf(run.status);
    setText(r.lastRunPill, run.status);
    setClass(r.lastRunPill, "pill pill-" + bucket);
    const prefix = ` ${run.job_name || "(unnamed)"} · `;
    setData(r.lastRunText, "ts", String(run.started_at));
    setData(r.lastRunText, "prefix", prefix);
    setText(r.lastRunText, prefix + fmtRelative(run.started_at, nowEstimate()));
  } else {
    setText(r.lastRunText, "–");
  }

  const cfg = agent.config;
  setText(r.enabled, cfg.enabled ? "yes" : "no");
  setText(r.grace, `${cfg.grace_minutes} min`);
  setText(r.alerts, cfg.offline_alerts ? "on" : "off");
  setText(r.ip, cfg.allowed_ip || "any");
  setText(r.notes, cfg.notes || "–");
  setText(r.schedules, agent.schedules.length ? agent.schedules.map((s) => `${s.job_name}: ${s.cron_schedule}`).join("\n") : "–");
}

// ---------------------------------------------------------------- render and fetch

function createGroupHead() {
  const head = el("div", "group-head");
  const name = el("span", "group-name");
  const summary = el("span", "sub");
  head.append(name, summary);
  head._refs = { name, summary };
  return head;
}

function updateGroupHead(head, group) {
  setText(head._refs.name, group.host);
  const bits = [`${group.count} agent${group.count === 1 ? "" : "s"}`];
  if (group.down) bits.push(`${group.down} down`);
  setText(head._refs.summary, bits.join(" · "));
}

function render(data) {
  const items = state.group === "host"
    ? groupByHost(data.agents, (members) => ({ down: members.filter((a) => a.status === "down").length }))
    : data.agents;
  reconcile($("agent-grid"), items, {
    key: (item) => (item.group ? groupKey(item) : item.name),
    create: (item) => (item.group ? createGroupHead() : createCard()),
    update: (node, item) => (item.group ? updateGroupHead(node, item) : updateCard(node, item)),
  });
  setHidden($("agents-empty"), data.agents.length > 0);
  const active = data.agents.filter((a) => a.status !== "disabled");
  const up = active.filter((a) => a.status === "up").length;
  setText($("agents-summary"), active.length ? `${up} of ${active.length} reporting` : "");
  refreshRelative($("agent-grid"), nowEstimate());
}

function renderBanner() {
  const box = $("banners");
  if (state.connectionError && !box.firstChild) {
    const node = el("div", "banner banner-crit", "Cannot load data from the dashboard service.");
    node.setAttribute("role", "alert");
    box.append(node);
  } else if (!state.connectionError) {
    box.replaceChildren();
  }
}

async function refresh(force) {
  const mine = ++requestSeq;
  try {
    const result = await getJson(`/api/ui/agents?range=${state.range}`, force ? null : state.etag);
    if (mine !== requestSeq) return;
    state.connectionError = false;
    if (!result.notModified) {
      state.data = result.data;
      state.tz = result.data.timezone;
      state.clockOffset = result.data.now - Date.now() / 1000;
      render(result.data);
      state.etag = result.etag; // only after a good render, so a failed one is retried
    }
    renderBanner();
    setText($("updated"), "Updated " + fmtClock(nowEstimate(), state.tz));
  } catch (e) {
    if (mine !== requestSeq) return;
    state.connectionError = true;
    renderBanner();
  }
}

function markRange() {
  document.querySelectorAll("#agent-ranges button").forEach((b) => {
    b.setAttribute("aria-pressed", b.dataset.range === state.range ? "true" : "false");
  });
}

initThemeToggle($("theme-toggle"));
markRange();
$("agent-group").value = state.group;
$("agent-group").addEventListener("change", (event) => {
  state.group = event.target.value === "none" ? "none" : "host";
  writeStore("agentGroup", state.group);
  if (state.data) render(state.data);
});
document.querySelectorAll("#agent-ranges button").forEach((b) => {
  b.addEventListener("click", () => {
    state.range = b.dataset.range;
    writeStore("agentRange", state.range);
    markRange();
    refresh(true);
  });
});
poll(() => refresh(false), POLL_MS);
setInterval(() => { if (!document.hidden) refreshRelative($("agent-grid"), nowEstimate()); }, 15000);
