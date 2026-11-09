import assert from "node:assert/strict";
import { test } from "node:test";

import {
  fmtDateTime, fmtClock, fmtShort, fmtDayLabel, fmtDuration, fmtRelative, fmtPercent, reconcile,
} from "../../app/web/static/js/ui.js";

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
