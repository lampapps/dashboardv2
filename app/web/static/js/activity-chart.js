// Stacked bar chart of runs per day by outcome, shared by the Overview and the agent page.
import { cssVar, fmtDayLabel, setText } from "./ui.js";

const BUCKETS = ["success", "purged", "failed", "stopped", "running", "other"];
const BUCKET_LABEL = { success: "Success", purged: "Purged", failed: "Failed", stopped: "Stopped", running: "Running", other: "Other" };

export function createActivityChart(canvas, noteEl) {
  let chart = null;
  let signature = "";
  let last = null;

  function build() {
    const text = cssVar("--muted");
    const grid = cssVar("--grid");
    chart = new Chart(canvas, {
      type: "bar",
      data: {
        labels: [],
        datasets: BUCKETS.map((b) => ({ label: BUCKET_LABEL[b], data: [], backgroundColor: cssVar("--st-" + b), borderWidth: 0, stack: "runs" })),
      },
      options: {
        animation: false, responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { stacked: true, ticks: { color: text, maxRotation: 0, autoSkipPadding: 12 }, grid: { display: false } },
          y: { stacked: true, beginAtZero: true, ticks: { color: text, precision: 0 }, grid: { color: grid } },
        },
        plugins: {
          legend: {
            position: "bottom",
            labels: {
              color: text,
              // Hide legend entries for outcomes that never occur in the range.
              filter: (item, data) => data.datasets[item.datasetIndex].data.some((n) => n > 0),
            },
          },
        },
      },
    });
  }

  function update(activity) {
    last = activity;
    const next = JSON.stringify(activity);
    if (next === signature) return;
    signature = next;
    chart.data.labels = activity.days.map(fmtDayLabel);
    chart.data.datasets.forEach((ds, i) => { ds.data = activity.series[BUCKETS[i]]; });
    chart.update("none");
    if (noteEl) {
      const total = BUCKETS.reduce((sum, b) => sum + activity.series[b].reduce((a, n) => a + n, 0), 0);
      setText(noteEl, total ? `${total} runs started in the last ${activity.days.length} days.` : "No runs in this range.");
    }
  }

  // Chart.js keeps colors it already resolved, so a theme switch rebuilds the chart (a rare, user-driven event).
  function rebuild() {
    chart.destroy();
    build();
    signature = "";
    if (last) update(last);
  }

  build();
  return { update, rebuild };
}
