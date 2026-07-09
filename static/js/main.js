/* =====================================================================
   SupplyLink — main.js
   Global utilities called from inline HTML attributes (onclick, oninput,
   onsubmit). Deliberately has no addEventListener calls — every function
   here is triggered directly by a markup attribute, e.g.:

     <button onclick="SupplyLink.addLineItemRow('po-lines-body','po-line-template')">
     <button onclick="SupplyLink.removeLineItemRow(this)">
     <input oninput="SupplyLink.updateLineTotal(this)">
     <button onclick="SupplyLink.toggleSidebar()">
     <form onsubmit="return SupplyLink.confirmAction('Delete this item?')">
     <button onclick="SupplyLink.printPage()">

   The one exception is init(), which runs once as the script itself
   is parsed — it is a plain function call, not an event registration.
   ===================================================================== */

const SupplyLink = (function () {

  const SIDEBAR_STORAGE_KEY = "supplylink_sidebar_collapsed";

  /* -------------------------------------------------------------------
     Confirmation guard for destructive actions.
     Usage: <form onsubmit="return SupplyLink.confirmAction('Delete?')">
     ------------------------------------------------------------------- */
  function confirmAction(message) {
    return window.confirm(message || "Are you sure?");
  }

  /* -------------------------------------------------------------------
     Dynamic line-item rows (Purchase Orders, Requisitions,
     Outpatient/Inpatient dispensing — anywhere the form posts
     item_id[], quantity[], etc. as repeating arrays).

     Expects a <table><tbody id="tableBodyId"> and a
     <template id="templateId"><tr>...</tr></template> sitting
     alongside it in the same page.
     ------------------------------------------------------------------- */
  function addLineItemRow(tableBodyId, templateId) {
    const tbody = document.getElementById(tableBodyId);
    const template = document.getElementById(templateId);
    if (!tbody || !template || !("content" in template)) return;

    const row = template.content.cloneNode(true);
    tbody.appendChild(row);
  }

  function removeLineItemRow(triggerEl) {
    const row = triggerEl.closest("tr");
    if (!row) return;

    const tbody = row.parentElement;
    row.remove();

    // keep at least one row so the form always posts something
    if (tbody && tbody.children.length === 0) {
      const template = document.getElementById(tbody.dataset.emptyTemplate || "");
      if (template && "content" in template) {
        tbody.appendChild(template.content.cloneNode(true));
      }
    }
  }

  /* -------------------------------------------------------------------
     Live line-total calculation for quantity x unit cost rows.
     Call from either the quantity or unit-cost input's oninput.

     Expected markup inside the row:
       <tr data-line-row>
         <input data-role="qty" ...>
         <input data-role="cost" ...>
         <span data-role="line-total"></span>
       </tr>

     Optionally pass a grandTotalId to also sum every row's line-total
     into a page-level total element.
     ------------------------------------------------------------------- */
  function updateLineTotal(triggerEl, grandTotalId) {
    const row = triggerEl.closest("[data-line-row]");
    if (!row) return;

    const qtyEl = row.querySelector('[data-role="qty"]');
    const costEl = row.querySelector('[data-role="cost"]');
    const totalEl = row.querySelector('[data-role="line-total"]');

    const qty = parseFloat(qtyEl && qtyEl.value) || 0;
    const cost = parseFloat(costEl && costEl.value) || 0;
    const lineTotal = qty * cost;

    if (totalEl) {
      totalEl.textContent = formatKES(lineTotal);
    }

    if (grandTotalId) {
      recalculateGrandTotal(grandTotalId);
    }
  }

  function recalculateGrandTotal(grandTotalId) {
    const grandTotalEl = document.getElementById(grandTotalId);
    if (!grandTotalEl) return;

    const rows = document.querySelectorAll("[data-line-row]");
    let sum = 0;

    rows.forEach(function (row) {
      const qtyEl = row.querySelector('[data-role="qty"]');
      const costEl = row.querySelector('[data-role="cost"]');
      const qty = parseFloat(qtyEl && qtyEl.value) || 0;
      const cost = parseFloat(costEl && costEl.value) || 0;
      sum += qty * cost;
    });

    grandTotalEl.textContent = formatKES(sum);
  }

  /* -------------------------------------------------------------------
     Fixed icon+label sidebar collapse toggle, persisted across page
     loads via localStorage.
     ------------------------------------------------------------------- */
  function toggleSidebar() {
    document.body.classList.toggle("sidebar-collapsed");

    try {
      const collapsed = document.body.classList.contains("sidebar-collapsed");
      window.localStorage.setItem(SIDEBAR_STORAGE_KEY, collapsed ? "1" : "0");
    } catch (e) {
      /* localStorage unavailable (private browsing, etc.) — ignore */
    }
  }

  /* -------------------------------------------------------------------
     KES currency formatting for on-the-fly totals (server-rendered
     values already come formatted; this is only for live JS math).
     ------------------------------------------------------------------- */
  function formatKES(value) {
    const n = Number(value) || 0;
    return "KES " + n.toLocaleString("en-KE", {
      minimumFractionDigits: 0,
      maximumFractionDigits: 2
    });
  }

  /* -------------------------------------------------------------------
     Print the current view (requisition/PO detail slips, etc.).
     ------------------------------------------------------------------- */
  function printPage() {
    window.print();
  }

  /* -------------------------------------------------------------------
     One-time setup, run as the script itself is parsed — not bound
     to any event.
     ------------------------------------------------------------------- */
  function init() {
    try {
      if (window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === "1") {
        document.body.classList.add("sidebar-collapsed");
      }
    } catch (e) {
      /* localStorage unavailable — ignore, sidebar stays expanded */
    }
  }

  init();

  return {
    confirmAction: confirmAction,
    addLineItemRow: addLineItemRow,
    removeLineItemRow: removeLineItemRow,
    updateLineTotal: updateLineTotal,
    toggleSidebar: toggleSidebar,
    formatKES: formatKES,
    printPage: printPage
  };

})();