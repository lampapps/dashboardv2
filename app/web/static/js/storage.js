// Overview storage panel: current usage plus usage over time for local drives and cloud buckets.
import {
  el, setText, setClass, setStyle, setHidden, setData, readStore, writeStore, reconcile, getJson, poll,
  cssVar, fmtBytes, fmtDateTime, fmtRelative, fmtTick, niceByteTicks, tickValues,
} from "./ui.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 30000;
const RANGES = ["24h", "7d", "30d", "90d", "1y"];
// Seconds between x-axis ticks per range.
const TICK_STEPS = { "24h": 3 * 3600, "7d": 86400, "30d": 5 * 86400, "90d": 14 * 86400, "1y": 30 * 86400 };
const PALETTE = ["--c1", "--c2", "--c3", "--c4", "--c5", "--c6"];

const state = {
  range: RANGES.includes(readStore("storageRange")) ? readStore("storageRange") : "30d",
  data: null,
  etag: null,
  tz: "UTC",
  charts: { local: null, cloud: null },
  signatures: { local: "", cloud: "" },
};
let requestSeq = 0;

// ---------------------------------------------------------------- labels and grouping

const isLocalKind = (entry) => entry.kind !== "cloud";

function seriesLabel(entry) {
  return entry.series ? `${entry.name} · ${entry.series}` : entry.name;
}

// Cloud entries of one bucket are summed; each storage class or prefix becomes a detail item.
function cloudGroups(current) {
  const groups = new Map();
  current.filter((c) => c.kind === "cloud").forEach((c) => {
    const key = c.source + "\u0000" + c.name;
    const group = groups.get(key) || { key, source: c.source, name: c.name, used: 0, ts: 0, parts: [] };
    group.used += c.used_bytes;
    group.ts = Math.max(group.ts, c.ts);
    if (c.series) group.parts.push([c.series, c.used_bytes]);
    groups.set(key, group);
  });
  return [...groups.values()];
}

// ---------------------------------------------------------------- current usage rows

function createUsageRow() {
  const row = el("div", "usage-row");
  const head = el("div", "usage-head");
  const name = el("span", "usage-name");
  const text = el("span", "usage-text tnum");
  head.append(name, text);
  const meter = el("div", "meter");
  const fill = el("div", "meter-fill");
  meter.append(fill);
  const foot = el("div", "usage-foot sub");
  const detail = el("span");
  const when = el("span", "when");
  foot.append(detail, when);
  row.append(head, meter, foot);
  row._refs = { name, text, meter, fill, detail, when };
  return row;
}

function updateUsageRow(row, item) {
  const r = row._refs;
  setText(r.name, item.name);
  setText(r.text, item.text);
  setHidden(r.meter, item.percent === null);
  if (item.percent !== null) {
    setStyle(r.fill, "width", `${Math.min(100, item.percent)}%`);
    setClass(r.fill, "meter-fill" + (item.percent >= 90 ? " is-crit" : item.percent >= 80 ? " is-warn" : ""));
  }
  setText(r.detail, item.detail);
  setData(r.when, "ts", String(item.ts));
  setText(r.when, item.rel);
}

function localItems(current, now) {
  return current.filter(isLocalKind).map((c) => {
    const percent = c.total_bytes ? (c.used_bytes / c.total_bytes) * 100 : null;
    const text = c.total_bytes
      ? `${fmtBytes(c.used_bytes)} of ${fmtBytes(c.total_bytes)} · ${Math.round(percent)}%`
      : fmtBytes(c.used_bytes);
    const detail = c.series || "";
    return { key: `${c.source}\u0000${c.kind}\u0000${c.name}\u0000${c.series}`, name: c.name, text, percent, detail, ts: c.ts, rel: fmtRelative(c.ts, now) };
  });
}

function cloudItems(current, now) {
  return cloudGroups(current).map((g) => ({
    key: g.key,
    name: g.name,
    text: fmtBytes(g.used),
    percent: null,
    detail: g.parts.map(([series, bytes]) => `${series} ${fmtBytes(bytes)}`).join(" · "),
    ts: g.ts,
    rel: fmtRelative(g.ts, now),
  }));
}

function renderUsage(container, items) {
  reconcile(container, items, { key: (i) => i.key, create: createUsageRow, update: updateUsageRow });
}

// ---------------------------------------------------------------- charts

function buildChart(kind) {
  const canvas = $(kind === "local" ? "c-local" : "c-cloud");
  const stacked = kind === "cloud";
  const text = cssVar("--muted");
  const grid = cssVar("--grid");
  const tz = () => state.tz;
  let digits = 1;
  const chart = new Chart(canvas, {
    type: "line",
    data: { datasets: [] },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false, parsing: false, normalized: true,
      interaction: { mode: "nearest", axis: "x", intersect: false },
      scales: {
        x: {
          type: "linear",
          ticks: { color: text, maxRotation: 0, callback: (v) => fmtTick(v, tz(), state.range === "24h") },
          grid: { color: grid },
          afterBuildTicks(scale) {
            const ticks = tickValues(scale.min, scale.max, TICK_STEPS[state.range], tz());
            if (ticks.length >= 2 && ticks.length <= 40) scale.ticks = ticks.map((value) => ({ value }));
          },
        },
        y: {
          stacked, beginAtZero: stacked, grace: stacked ? 0 : "8%",
          ticks: { color: text, callback: (v) => (v === 0 ? "0" : fmtBytes(v, digits)) },
          grid: { color: grid },
          afterBuildTicks(scale) {
            const nice = niceByteTicks(scale.min, scale.max);
            if (nice.ticks.length >= 2) {
              digits = nice.digits;
              scale.ticks = nice.ticks.map((value) => ({ value }));
            }
          },
        },
      },
      plugins: {
        legend: { position: "bottom", labels: { color: text } },
        tooltip: {
          callbacks: {
            title: (items) => (items.length ? fmtDateTime(items[0].parsed.x, tz()) : ""),
            label: (item) => `${item.dataset.label}: ${fmtBytes(item.parsed.y)}`,
          },
        },
      },
    },
  });
  state.charts[kind] = chart;
}

function dataset(label, color, points, stacked) {
  return {
    label, data: points, borderColor: color, backgroundColor: stacked ? color + "99" : color,
    borderWidth: 1.8, pointRadius: points.length < 40 ? 2 : 0, pointHoverRadius: 3,
    fill: stacked, tension: 0, spanGaps: true,
  };
}

// Cloud series are stacked, so every series needs a value at every x; a missing one is zero.
function alignToGrid(seriesList) {
  const xs = [...new Set(seriesList.flatMap((s) => s.points.map((p) => p[0])))].sort((a, b) => a - b);
  return seriesList.map((s) => {
    const byX = new Map(s.points.map((p) => [p[0], p[1]]));
    return { ...s, points: xs.map((x) => [x, byX.get(x) ?? 0]) };
  });
}

function renderChart(kind, seriesList, data) {
  const chart = state.charts[kind];
  const stacked = kind === "cloud";
  const list = stacked ? alignToGrid(seriesList) : seriesList;
  const signature = JSON.stringify([data.start, data.end, list]);
  if (signature === state.signatures[kind]) return;
  state.signatures[kind] = signature;

  const labels = list.map(seriesLabel);
  const same = chart.data.datasets.length === labels.length && chart.data.datasets.every((d, i) => d.label === labels[i]);
  const points = list.map((s) => s.points.map(([x, y]) => ({ x, y })));
  if (same) {
    chart.data.datasets.forEach((d, i) => { d.data = points[i]; });
  } else {
    chart.data.datasets = list.map((s, i) => dataset(labels[i], cssVar(PALETTE[i % PALETTE.length]), points[i], stacked));
  }
  chart.options.scales.x.min = data.start;
  chart.options.scales.x.max = data.end;
  chart.update("none");
}

// Chart.js keeps colors it already resolved, so a theme switch rebuilds the charts.
function rebuildCharts() {
  ["local", "cloud"].forEach((kind) => {
    if (state.charts[kind]) state.charts[kind].destroy();
    buildChart(kind);
    state.signatures[kind] = "";
  });
  if (state.data) render(state.data);
}

// ---------------------------------------------------------------- render and fetch

function render(data) {
  const localSeries = data.series.filter(isLocalKind);
  const cloudSeries = data.series.filter((s) => s.kind === "cloud");
  const hasLocal = data.configured.local > 0 || localSeries.length > 0 || data.current.some(isLocalKind);
  const hasCloud = data.configured.cloud > 0 || cloudSeries.length > 0 || data.current.some((c) => c.kind === "cloud");

  setHidden($("storage-panel"), !hasLocal && !hasCloud);
  setHidden($("local-box"), !hasLocal);
  setHidden($("cloud-box"), !hasCloud);

  renderUsage($("local-usage"), localItems(data.current, data.now));
  renderUsage($("cloud-usage"), cloudItems(data.current, data.now));
  renderChart("local", localSeries, data);
  renderChart("cloud", cloudSeries, data);

  const empty = data.current.length === 0 && data.series.length === 0;
  const notes = [];
  if (empty) notes.push("Storage samples appear after the first timer run.");
  else {
    if (hasLocal && localSeries.length === 0) notes.push("No local samples in this range.");
    if (hasCloud && cloudSeries.length === 0) notes.push("No cloud samples in this range (S3 sizes are reported once a day).");
  }
  setText($("storage-note"), notes.join(" "));
}

async function refresh(force) {
  const mine = ++requestSeq;
  try {
    const result = await getJson(`/api/ui/storage?range=${state.range}`, force ? null : state.etag);
    if (mine !== requestSeq || result.notModified) return;
    state.data = result.data;
    state.etag = result.etag;
    state.tz = result.data.timezone || state.tz;
    render(result.data);
  } catch (e) {
    // The Overview's connection banner already reports a failing service; keep the last charts.
  }
}

function markRange() {
  document.querySelectorAll("#storage-ranges button").forEach((b) => {
    b.setAttribute("aria-pressed", b.dataset.range === state.range ? "true" : "false");
  });
}

function init() {
  markRange();
  buildChart("local");
  buildChart("cloud");
  document.querySelectorAll("#storage-ranges button").forEach((b) => {
    b.addEventListener("click", () => {
      state.range = b.dataset.range;
      writeStore("storageRange", state.range);
      markRange();
      refresh(true);
    });
  });
  document.addEventListener("themechange", rebuildCharts);
  poll(() => refresh(false), POLL_MS);
}

init();
