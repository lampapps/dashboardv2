import assert from "node:assert/strict";
import { test } from "node:test";

import {
  fmtDateTime, fmtClock, fmtShort, fmtDayLabel, fmtDuration, fmtRelative, fmtPercent, fmtBytes, fmtTick,
  niceByteTicks, tickValues, reconcile, singlePointRange, jobConfigRows,
} from "../../app/web/static/js/ui.js";
import { bucketOf, agentHref, jobHref, agentLabel, groupByHost, groupKey } from "../../app/web/static/js/shared.js";
import { totalsText } from "../../app/web/static/js/jobs-table.js";

// 2026-01-15 12:00:00 UTC = 17:30 in Asia/Kolkata
const TS = Date.UTC(2026, 0, 15, 12, 0, 0) / 1000;

test("date and time formatting uses the given timezone", () => {
  assert.equal(fmtClock(TS, "Asia/Kolkata"), "17:30:00");
  assert.equal(fmtDateTime(TS, "UTC").startsWith("2026-01-15 12:00:00"), true);
  assert.equal(fmtShort(TS, "Asia/Kolkata"), "Jan 15, 17:30");
});

test("day labels are calendar days and do not shift with the viewer's timezone", () => {
  assert.equal(fmtDayLabel("2026-11-09"), "Nov 9");
  assert.equal(fmtDayLabel("2026-01-01"), "Jan 1");
});

test("a job's card rows list its schedule, source and destination", () => {
  const single = jobConfigRows({
    job_name: "jim-home", cron_schedule: "0 0 * * *",
    targets: [{ name: "jim-home", source: "/home/jim", destination: "s3:bucket/jim" }],
  });
  assert.deepEqual(single, [
    { label: "Schedule", text: "0 0 * * *" }, { label: "Source", text: "/home/jim" },
    { label: "Destination", text: "s3:bucket/jim" },
  ]);
});

test("a job with several targets gets a heading per target, and missing data shows a dash", () => {
  const rows = jobConfigRows({
    job_name: "sync", cron_schedule: null,
    targets: [{ name: "a", source: "/a", destination: "nas:a" }, { name: "b", source: "nas:b", destination: "" }],
  });
  assert.deepEqual(rows.map((r) => r.heading ?? `${r.label}=${r.text}`), [
    "Schedule=–", "a", "Source=/a", "Destination=nas:a", "b", "Source=nas:b", "Destination=–",
  ]);
  assert.deepEqual(jobConfigRows({ job_name: "x", cron_schedule: "* * * * *", targets: [] }).map((r) => r.text),
    ["* * * * *", "–", "–"]);
});

test("each reported schedule is marked enabled or disabled", () => {
  const text = (schedules, cron = null) => jobConfigRows({ job_name: "j", cron_schedule: cron, schedules, targets: [] })[0].text;
  assert.equal(text([{ cron: "0 2 * * *", enabled: true }, { cron: "0 9 * * 1", enabled: false }]),
    "0 2 * * * (enabled)\n0 9 * * 1 (disabled)");
  assert.equal(text([{ cron: "0 2 * * *", enabled: false }], "0 2 * * *"), "0 2 * * * (disabled)");
  assert.equal(text([], "0 2 * * *"), "none");   // the agent says it has none; a stale heartbeat cron is ignored
  assert.equal(text(null, "0 2 * * *"), "0 2 * * *");   // an agent that does not report schedules
  assert.equal(text(undefined), "–");
});

test("durations", () => {
  assert.equal(fmtDuration(5), "5 s");
  assert.equal(fmtDuration(125), "2 min 5 s");
  assert.equal(fmtDuration(3725), "1 h 2 min");
  assert.equal(fmtDuration(-3), "0 s");
});

test("relative times", () => {
  assert.equal(fmtRelative(1000, 1010), "just now");
  assert.equal(fmtRelative(1000 + 20, 1000), "in a moment");
  assert.equal(fmtRelative(0, 300), "5 min ago");
  assert.equal(fmtRelative(10800, 0), "in 3 h");
  assert.equal(fmtRelative(0, 2 * 86400), "2 d ago");
});

test("bytes use binary units and keep a readable number of digits", () => {
  assert.equal(fmtBytes(0), "0 B");
  assert.equal(fmtBytes(1023), "1023 B");
  assert.equal(fmtBytes(1536), "1.5 KiB");
  assert.equal(fmtBytes(5 * 1024 ** 3), "5.0 GiB");
  assert.equal(fmtBytes(250 * 1024 ** 4), "250 TiB");
  assert.equal(fmtBytes(3.2 * 1024 ** 4), "3.2 TiB");
  assert.equal(fmtBytes(null), "–");
  assert.equal(fmtBytes(-1536), "-1.5 KiB");
});

test("tick values fall on local midnights of the given timezone", () => {
  const min = Date.UTC(2026, 0, 10, 3, 0, 0) / 1000;
  const max = Date.UTC(2026, 0, 14, 3, 0, 0) / 1000;
  const ticks = tickValues(min, max, 86400, "Asia/Kolkata");
  assert.equal(ticks.length, 4);
  ticks.forEach((t) => assert.equal(fmtClock(t, "Asia/Kolkata"), "00:00:00"));
  assert.ok(ticks.every((t) => t >= min && t <= max));
});

test("tick labels switch between clock times and calendar days", () => {
  assert.equal(fmtTick(TS, "Asia/Kolkata", true), "17:30");
  assert.equal(fmtTick(TS, "Asia/Kolkata", false), "Jan 15");
});

test("nice byte ticks are evenly spaced round numbers of the largest unit", () => {
  const GiB = 1024 ** 3;
  const TiB = 1024 ** 4;
  const a = niceByteTicks(0, 10 * GiB);
  assert.deepEqual(a.ticks.map((t) => t / GiB), [0, 2, 4, 6, 8, 10]);
  assert.equal(a.digits, 0);

  const b = niceByteTicks(3.0 * TiB, 3.6 * TiB);
  const labels = b.ticks.map((t) => fmtBytes(t, b.digits));
  assert.ok(b.ticks.length >= 3 && b.ticks.length <= 10);
  assert.equal(new Set(labels).size, labels.length, "neighbouring ticks must read differently");
  assert.ok(b.ticks.every((t) => t >= 3.0 * TiB * 0.999 && t <= 3.6 * TiB * 1.001));

  const gaps = b.ticks.slice(1).map((t, i) => t - b.ticks[i]);
  assert.ok(gaps.every((g) => Math.abs(g - gaps[0]) < 1));

  assert.deepEqual(niceByteTicks(5, 5).ticks, []);
});

test("explicit digits override the automatic precision", () => {
  assert.equal(fmtBytes(3.25 * 1024 ** 4, 2), "3.25 TiB");
  assert.equal(fmtBytes(512, 2), "512 B");
});

test("run statuses fold into display buckets like the server does", () => {
  assert.equal(bucketOf("completed"), "success");
  assert.equal(bucketOf("purged"), "purged");
  assert.equal(bucketOf("error"), "failed");
  assert.equal(bucketOf("running"), "running");
  assert.equal(bucketOf("skipped"), "other");
  assert.equal(bucketOf(undefined), "other");
});

test("links escape agent and job names", () => {
  assert.equal(agentHref("nas 1"), "/agents/nas%201");
  assert.equal(jobHref("nas1", "photos & docs/2026"), "/agents/nas1/job?name=photos%20%26%20docs%2F2026");
  assert.equal(jobHref("nas1", ""), "/agents/nas1/job?name=");
});

test("status totals text groups statuses and omits zeros", () => {
  assert.equal(totalsText({ success: 5, completed: 1, failed: 1, error: 1 }), "6 ok · 2 failed");
  assert.equal(totalsText({ running: 1 }), "1 running");
  assert.equal(totalsText({ success: 2, purged: 3 }), "2 ok · 3 purged");
  assert.equal(totalsText({}), "–");
});

test("percent", () => {
  assert.equal(fmtPercent(null), "–");
  assert.equal(fmtPercent(0.9667), "96.7%");
  assert.equal(fmtPercent(1), "100%");
});

// A tiny stand-in for a DOM parent: enough of the API that reconcile() uses.
function fakeParent() {
  const parent = {
    nodes: [],
    moves: 0,
    get children() { return this.nodes; },
    insertBefore(node, ref) {
      const at = this.nodes.indexOf(node);
      if (at >= 0) this.nodes.splice(at, 1);
      const to = ref ? this.nodes.indexOf(ref) : this.nodes.length;
      this.nodes.splice(to, 0, node);
      this.moves += 1;
    },
  };
  return parent;
}

function fakeNode(key) {
  const node = { dataset: { key }, removed: false, updates: 0 };
  node.remove = function remove() { this.removed = true; parent.nodes.splice(parent.nodes.indexOf(this), 1); };
  return node;
}

let parent;
const hooks = (created) => ({
  key: (item) => item.id,
  create: (item) => { created.push(item.id); return fakeNode(item.id); },
  update: (node) => { node.updates += 1; },
});

test("reconcile creates, reuses, reorders and removes by key", () => {
  parent = fakeParent();
  const created = [];
  reconcile(parent, [{ id: "a" }, { id: "b" }, { id: "c" }], hooks(created));
  assert.deepEqual(parent.nodes.map((n) => n.dataset.key), ["a", "b", "c"]);
  const first = parent.nodes[0];

  parent.moves = 0;
  reconcile(parent, [{ id: "a" }, { id: "b" }, { id: "c" }], hooks(created));
  assert.equal(parent.moves, 0, "unchanged order must not touch the DOM");
  assert.equal(parent.nodes[0], first, "existing nodes are reused");
  assert.deepEqual(created, ["a", "b", "c"]);

  reconcile(parent, [{ id: "c" }, { id: "a" }, { id: "d" }], hooks(created));
  assert.deepEqual(parent.nodes.map((n) => n.dataset.key), ["c", "a", "d"]);
  assert.deepEqual(created, ["a", "b", "c", "d"]);
  assert.equal(parent.nodes[1], first);
});

test("agentLabel shows host and agent, or one name when they match", () => {
  assert.equal(agentLabel("P3Tiny", "snapshot"), "P3Tiny / snapshot");
  assert.equal(agentLabel("snapshot", "snapshot"), "snapshot");
});

test("groupByHost puts a header before each host's rows, hosts alphabetical, row order kept", () => {
  const rows = [
    { host: "P3Tiny", job: "b", bad: 1 }, { host: "NAS1", job: "a", bad: 0 }, { host: "P3Tiny", job: "a", bad: 0 },
  ];
  const items = groupByHost(rows, (members) => ({ bad: members.reduce((n, r) => n + r.bad, 0) }));
  assert.deepEqual(items.map((i) => (i.group ? `# ${i.host} ${i.count} ${i.bad}` : `${i.host}/${i.job}`)),
    ["# NAS1 1 0", "NAS1/a", "# P3Tiny 2 1", "P3Tiny/b", "P3Tiny/a"]);
  assert.deepEqual(groupByHost([]), []);
});

test("group headers get keys that cannot collide with job rows", () => {
  assert.notEqual(groupKey({ host: "a" }), "a\u0000job");
  assert.notEqual(groupKey({ host: "a" }), groupKey({ host: "b" }));
});

test("singlePointRange gives a lone run a window and leaves real spans to the chart", () => {
  const day = 86400;
  assert.deepEqual(singlePointRange([1000]), { min: 1000 - day / 2, max: 1000 + day / 2 });
  assert.deepEqual(singlePointRange([1000, 1030]), { min: 1015 - day / 2, max: 1015 + day / 2 });
  assert.deepEqual(singlePointRange([1000, 1180]), { min: undefined, max: undefined });
  assert.deepEqual(singlePointRange([]), { min: undefined, max: undefined });
  assert.deepEqual(singlePointRange([5], 60, 600), { min: -295, max: 305 });
});
