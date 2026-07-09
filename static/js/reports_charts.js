// static/js/reports_charts.js
// Renders the Reports & Analytics charts. Depends on Chart.js (loaded in
// templates/reports/analytics.html) and the /reports/api/* routes in app.py.

(function () {
  const GREEN = "#2E7D32";
  const GREEN_LIGHT = "#66BB6A";
  const AMBER = "#F9A825";
  const GRID_COLOR = "rgba(255,255,255,0.08)";
  const TEXT_COLOR = "#E0E0E0";

  Chart.defaults.color = TEXT_COLOR;
  Chart.defaults.borderColor = GRID_COLOR;
  Chart.defaults.font.family =
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";

  const charts = {}; // keep references so filters can destroy + redraw

  async function getJSON(url, params) {
    const cleaned = Object.fromEntries(
      Object.entries(params || {}).filter(([, v]) => v !== undefined && v !== "")
    );
    const qs = new URLSearchParams(cleaned).toString();
    const res = await fetch(qs ? `${url}?${qs}` : url);
    if (!res.ok) {
      throw new Error(`Request to ${url} failed: ${res.status}`);
    }
    return res.json();
  }

  function destroyIfExists(key) {
    if (charts[key]) {
      charts[key].destroy();
    }
  }

  // 1. Stock value trend --------------------------------------------------
  async function renderStockValueTrend(location) {
    const data = await getJSON("/reports/api/stock-value-trend", { location });
    destroyIfExists("trend");
    const ctx = document.getElementById("chart-stock-value-trend");
    charts.trend = new Chart(ctx, {
      type: "line",
      data: {
        labels: data.labels,
        datasets: [
          {
            label: "Stock value (KES)",
            data: data.values,
            borderColor: GREEN_LIGHT,
            backgroundColor: "rgba(102,187,106,0.15)",
            fill: true,
            tension: 0.3,
            pointRadius: 2,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { color: GRID_COLOR } },
          y: { grid: { color: GRID_COLOR }, beginAtZero: true },
        },
      },
    });
  }

  // 2. Procurement vs. consumption ----------------------------------------
  async function renderProcurementVsConsumption(period) {
    const data = await getJSON("/reports/api/procurement-vs-consumption", { period });
    destroyIfExists("procurement");
    const ctx = document.getElementById("chart-procurement-vs-consumption");
    charts.procurement = new Chart(ctx, {
      type: "bar",
      data: {
        labels: data.labels,
        datasets: [
          { label: "Planned", data: data.planned, backgroundColor: GREEN },
          { label: "Actual", data: data.actual, backgroundColor: AMBER },
        ],
      },
      options: {
        responsive: true,
        scales: {
          x: { grid: { display: false } },
          y: { grid: { color: GRID_COLOR }, beginAtZero: true },
        },
      },
    });
  }

  // 3. Expiry timeline ------------------------------------------------------
  async function renderExpiryTimeline() {
    const data = await getJSON("/reports/api/expiry-timeline", {});
    destroyIfExists("expiry");
    const ctx = document.getElementById("chart-expiry-timeline");
    charts.expiry = new Chart(ctx, {
      type: "bar",
      data: {
        labels: data.labels,
        datasets: [
          {
            label: "Within 6-month alert window",
            data: data.within_alert,
            backgroundColor: "#C62828",
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
        scales: {
          x: { stacked: true, grid: { display: false } },
          y: { stacked: true, grid: { color: GRID_COLOR }, beginAtZero: true },
        },
      },
    });
  }

  // 4. Stock by location (donut) -------------------------------------------
  async function renderStockByLocation() {
    const data = await getJSON("/reports/api/stock-by-location", {});
    destroyIfExists("location");
    const ctx = document.getElementById("chart-stock-by-location");
    charts.location = new Chart(ctx, {
      type: "doughnut",
      data: {
        labels: data.labels,
        datasets: [
          {
            data: data.values,
            backgroundColor: [GREEN, GREEN_LIGHT, AMBER, "#8D6E63"],
            borderColor: "#1E1E1E",
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

  // 5. Top-moving items -----------------------------------------------------
  async function renderTopMovingItems(start, end) {
    const data = await getJSON("/reports/api/top-moving-items", { start, end });
    destroyIfExists("topMoving");
    const ctx = document.getElementById("chart-top-moving-items");
    charts.topMoving = new Chart(ctx, {
      type: "bar",
      data: {
        labels: data.labels,
        datasets: [
          { label: "Quantity issued", data: data.values, backgroundColor: GREEN },
        ],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { color: GRID_COLOR }, beginAtZero: true },
          y: { grid: { display: false } },
        },
      },
    });
  }

  async function renderAll() {
    const location = document.getElementById("filter-location").value;
    const period = document.getElementById("filter-period").value;
    const start = document.getElementById("filter-start").value;
    const end = document.getElementById("filter-end").value;

    await Promise.all([
      renderStockValueTrend(location),
      renderProcurementVsConsumption(period),
      renderExpiryTimeline(),
      renderStockByLocation(),
      renderTopMovingItems(start, end),
    ]);
  }

  document.addEventListener("DOMContentLoaded", () => {
    renderAll();
    document.getElementById("apply-filters").addEventListener("click", renderAll);
  });
})();