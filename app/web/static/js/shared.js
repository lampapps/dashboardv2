// Page-level constants and helpers shared by the pages (status vocabulary, links, the status timeline).
import { setClass, setStyle, setTitle, el, fmtDateTime, fmtDuration } from "./ui.js";

// Run statuses as agents report them, folded into the five display buckets (mirrors the server).
export const BUCKET_OF = {
  success: "success", completed: "success", purged: "success",
  failed: "failed", error: "failed", stopped: "stopped", running: "running",
};
export const bucketOf = (status) => BUCKET_OF[status] || "other";

export const AGENT_STATUS_LABEL = { up: "Up", down: "Down", new: "New", disabled: "Disabled", unknown: "No data" };

export const agentHref = (name) => `/agents/${encodeURIComponent(name)}`;
export const jobHref = (agent, job) => `${agentHref(agent)}/job?name=${encodeURIComponent(job)}`;

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
