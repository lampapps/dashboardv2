// The jobs table used on the Overview and the agent page: keyed rows, sorting, filtering.
import {
  el, setText, setClass, setStyle, setTitle, setHidden, setData, readStore, writeStore, reconcile,
  fmtDateTime, fmtShort, fmtDuration,
} from "./ui.js";
import { bucketOf, agentHref, jobHref } from "./shared.js";

// Lower rank sorts first when ordering by status, so problems rise to the top.
const ATTENTION_RANK = { failed: 0, running: 1, stopped: 2, other: 3, success: 4 };

const SORTERS = {
  job: (a, b) => a.job_name.localeCompare(b.job_name) || a.agent.localeCompare(b.agent),
  last: (a, b) => a.last_started_at - b.last_started_at,
  next: (a, b) => (a.next_event_at ?? Infinity) - (b.next_event_at ?? Infinity),
  status: (a, b) => ATTENTION_RANK[bucketOf(a.last_status)] - ATTENTION_RANK[bucketOf(b.last_status)]
    || b.last_started_at - a.last_started_at,
};

export function totalsText(counts) {
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

function createRow() {
  const tr = el("tr");
  const job = el("td");
  const jobLink = el("a", "job-name");
  const jobAgent = el("a", "sub block-link");
  job.append(jobLink, jobAgent);

  const last = el("td");
  const lastTop = el("div");
  const pill = el("span", "pill");
  const when = el("span", "when");
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
  next.append(nextTime, nextRel);

  const recent = el("td");
  const pips = el("div", "pips");
  recent.append(pips);

  const totals = el("td", "tnum");
  tr.append(job, last, next, recent, totals);
  tr._refs = { jobLink, jobAgent, pill, when, lastDetail, lastError, progress, bar, nextTime, nextRel, pips, totals, pipSig: "" };
  return tr;
}

function updateRow(tr, job, tz, showAgent) {
  const r = tr._refs;
  const bucket = bucketOf(job.last_status);
  setText(r.jobLink, job.job_name || "(unnamed)");
  if (r.jobLink.getAttribute("href") !== jobHref(job.agent, job.job_name)) r.jobLink.href = jobHref(job.agent, job.job_name);
  setHidden(r.jobAgent, !showAgent);
  setText(r.jobAgent, job.agent);
  if (r.jobAgent.getAttribute("href") !== agentHref(job.agent)) r.jobAgent.href = agentHref(job.agent);
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
    setText(r.nextTime, fmtShort(job.next_event_at, tz) + (job.schedule_drifted ? " ⚠" : ""));
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
      pip.title = `${fmtDateTime(run.started_at, tz)} · ${run.status}`;
      return pip;
    }));
  }
  setText(r.totals, totalsText(job.status_counts));
}

// Wires sorting on the table's [data-sort] buttons and renders rows into `body`.
export function createJobsTable({ table, body, empty, count, getTz, showAgent = true }) {
  const state = {
    jobs: [],
    filter: "",
    problemsOnly: false,
    sort: { key: readStore("sortKey") || "last", dir: readStore("sortDir") || "desc" },
  };

  function visible() {
    const needle = state.filter.trim().toLowerCase();
    const rows = state.jobs.filter((j) => {
      if (needle && !(j.job_name + " " + j.agent).toLowerCase().includes(needle)) return false;
      if (state.problemsOnly && ["success", "running"].includes(bucketOf(j.last_status))) return false;
      return true;
    });
    const compare = SORTERS[state.sort.key] || SORTERS.last;
    const sign = state.sort.dir === "asc" ? 1 : -1;
    return rows.sort((a, b) => sign * compare(a, b) || a.job_name.localeCompare(b.job_name));
  }

  function render(jobs) {
    if (jobs) state.jobs = jobs;
    const rows = visible();
    reconcile(body, rows, {
      key: (j) => j.agent + "\u0000" + j.job_name,
      create: createRow,
      update: (tr, job) => updateRow(tr, job, getTz(), showAgent),
    });
    setHidden(empty, rows.length > 0);
    setText(empty, state.jobs.length ? "No jobs match the filter." : "No runs have been reported yet.");
    const total = state.jobs.length;
    setText(count, rows.length === total ? `${total} job${total === 1 ? "" : "s"}` : `${rows.length} of ${total} jobs`);
    table.querySelectorAll("th[aria-sort]").forEach((th) => {
      const button = th.querySelector("[data-sort]");
      const active = button && button.dataset.sort === state.sort.key;
      th.setAttribute("aria-sort", active ? (state.sort.dir === "asc" ? "ascending" : "descending") : "none");
    });
  }

  table.querySelectorAll("[data-sort]").forEach((button) => {
    button.addEventListener("click", () => {
      const key = button.dataset.sort;
      const dir = state.sort.key === key ? (state.sort.dir === "asc" ? "desc" : "asc") : (key === "last" ? "desc" : "asc");
      state.sort = { key, dir };
      writeStore("sortKey", key);
      writeStore("sortDir", dir);
      render();
    });
  });

  return {
    render,
    setFilter(text) { state.filter = text; render(); },
    setProblemsOnly(value) { state.problemsOnly = value; render(); },
  };
}
