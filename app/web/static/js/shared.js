// Page-level constants and helpers shared by the pages (status vocabulary, links, the status timeline).
import { setClass, setStyle, setTitle, el, fmtDateTime, fmtDuration } from "./ui.js";

// Run statuses as agents report them, folded into the six display buckets (mirrors the server).
export const BUCKET_OF = {
  success: "success", completed: "success", purged: "purged",
  failed: "failed", error: "failed", stopped: "stopped", running: "running",
};
export const bucketOf = (status) => BUCKET_OF[status] || "other";

export const AGENT_STATUS_LABEL = { up: "Up", down: "Down", new: "New", disabled: "Disabled", unknown: "No data" };

export const agentHref = (name) => `/agents/${encodeURIComponent(name)}`;
export const jobHref = (agent, job) => `${agentHref(agent)}/job?name=${encodeURIComponent(job)}`;

export const HOST_SOURCE_LABEL = {
  reported: "reported by the agent", configured: "set in agents.yaml", agent: "the agent name; none reported or configured",
};

// "host / agent", or just the one name when they are the same.
export const agentLabel = (host, agent) => (host === agent ? agent : `${host} / ${agent}`);

export const groupKey = (item) => "\u0001group\u0000" + item.host;

// Inserts a {group: true, host, count, ...summarize(members)} header before each host's rows.
// Hosts come out alphabetically; rows keep their incoming order within a host.
export function groupByHost(rows, summarize = () => ({})) {
  const byHost = new Map();
  rows.forEach((row) => {
    if (!byHost.has(row.host)) byHost.set(row.host, []);
    byHost.get(row.host).push(row);
  });
  const items = [];
  [...byHost.keys()].sort((a, b) => a.localeCompare(b)).forEach((host) => {
    const members = byHost.get(host);
    items.push({ group: true, host, count: members.length, ...summarize(members) }, ...members);
  });
  return items;
}

// Segments are positioned by percentage of the range and reused in place on every update.
export function syncTimeline(container, spans, start, end, tz) {
  const total = end - start;
  while (container.children.length > spans.length) container.lastElementChild.remove();
  while (container.children.length < spans.length) container.append(el("span", "seg"));
  spans.forEach(([begin, finish, status], i) => {
    const seg = container.children[i];
    setClass(seg, "seg seg-" + status);
    setStyle(seg, "left", `${((begin - start) / total) * 100}%`);
    setStyle(seg, "width", `${((finish - begin) / total) * 100}%`);
    setTitle(seg, `${AGENT_STATUS_LABEL[status]} · ${fmtDateTime(begin, tz)} to ${fmtDateTime(finish, tz)} (${fmtDuration(finish - begin)})`);
  });
}
