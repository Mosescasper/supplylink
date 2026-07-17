// static/js/dashboard_charts.js
// Renders the two charts that replace the Expiry Alert / Low-Stock Alert
// tables on the dashboard for admin, store_officer, and pharmacist (not
// shown to doctor, which has its own dashboard layout). Reuses the same
// /reports/api/stock-by-location and /reports/api/expiry-timeline endpoints
// that power the admin Visual Analytics page — no new backend routes.
//
// No event listeners: this script tag sits in dashboard.html's extra_js
// block, which renders at the bottom of the page after the canvas elements
// already exist, so the charts can be drawn immediately on script load.

(function () {
  const GREEN = "#2E7D32";
  const GREEN_LIGHT = "#66BB6A";
  const AMBER = "#F9A825";
  const RED = "#C62828";
  const BROWN = "#8D6E63";
  const GRID_COLOR = "rgba(255,255,255,0.08)";
  const TEXT_COLOR = "#78899b";

  Chart.defaults.color = TEXT_COLOR;
  Chart.defaults.borderColor = GRID_COLOR;
  Chart.defaults.font.family =
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";

  async function getJSON(url) {
    const res = await fetch(url);
    if (!res.ok) {
      throw new Error(`Request to ${url} failed: ${res.status}`);
    }
    return res.json();
  }

  async function renderStockByLocation() {
    const canvas = document.getElementById("chart-stock-by-location");
    if (!canvas) return;
    const data = await getJSON("/reports/api/stock-by-location");
    new Chart(canvas, {
      type: "doughnut",
      data: {
        labels: data.labels,
        datasets: [
          {
            data: data.values,
            backgroundColor: [GREEN, GREEN_LIGHT, AMBER, BROWN],
            borderColor: "#10161d",
            borderWidth: 2,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { position: "bottom" } },
      },
    });
  }

  async function renderExpiryTimeline() {
    const canvas = document.getElementById("chart-expiry-timeline");
    if (!canvas) return;
    const data = await getJSON("/reports/api/expiry-timeline?months_ahead=12");
    new Chart(canvas, {
      type: "bar",
      data: {
        labels: data.labels,
        datasets: [
          {
            label: "Within 6-month alert window",
            data: data.within_alert,
            backgroundColor: RED,
          },
          {
            label: "Later",
            data: data.later,
            backgroundColor: GREEN,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { position: "bottom" } },
        scales: {
          x: { stacked: true, grid: { display: false } },
          y: { stacked: true, grid: { color: GRID_COLOR }, beginAtZero: true },
        },
      },
    });
  }

  renderStockByLocation();
  renderExpiryTimeline();
})();