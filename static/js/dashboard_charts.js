// static/js/dashboard_charts.js
// Renders the charts that replace the Expiry Alert / Low-Stock Alert tables
// on the dashboard for admin, store_officer, and pharmacist (doctor has its
// own layout and never loads this script). Reuses the same
// /reports/api/stock-by-location and /reports/api/expiry-timeline endpoints
// that power the admin Visual Analytics page, now with an optional
// ?locations= filter read from the .dash element's data-locations attribute
// (set server-side per role — empty for admin, meaning unscoped).
//
// Pharmacists additionally get a Prescription Fill Rate chart from
// /reports/api/fill-rate, which only that role (and admin) can query.
//
// No event listeners: this script tag sits in dashboard.html's extra_js
// block, which renders at the bottom of the page after all the canvas
// elements already exist, so charts can be drawn immediately on load.

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

  const dashRoot = document.querySelector(".dash");
  const locationsParam = dashRoot ? (dashRoot.dataset.locations || "") : "";

  function withLocations(url) {
    if (!locationsParam) return url;
    const separator = url.includes("?") ? "&" : "?";
    return `${url}${separator}locations=${encodeURIComponent(locationsParam)}`;
  }

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
    const data = await getJSON(withLocations("/reports/api/stock-by-location"));
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
    const data = await getJSON(withLocations("/reports/api/expiry-timeline?months_ahead=12"));
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

  async function renderFillRate() {
    const canvas = document.getElementById("chart-fill-rate");
    const kpiEl = document.getElementById("fill-rate-kpi");
    if (!canvas) return; // only present on the pharmacist dashboard

    const data = await getJSON("/reports/api/fill-rate?months=6");

    if (kpiEl) {
      kpiEl.textContent = `${data.overall_rate}%`;
    }

    new Chart(canvas, {
      type: "line",
      data: {
        labels: data.labels,
        datasets: [
          {
            label: "Fill rate (%)",
            data: data.values,
            borderColor: AMBER,
            backgroundColor: "rgba(249,168,37,0.15)",
            fill: true,
            tension: 0.3,
            pointRadius: 3,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          y: {
            min: 0,
            max: 100,
            grid: { color: GRID_COLOR },
            ticks: { callback: (value) => `${value}%` },
          },
          x: { grid: { display: false } },
        },
      },
    });
  }

  renderStockByLocation();
  renderExpiryTimeline();
  renderFillRate();
})();