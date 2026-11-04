// The disputes board as an AG Grid: live countdowns, sort, filter and search across every case.
// If the grid's script didn't load, the plain list rendered by the server stays in place.
(function () {
  const el = document.getElementById("grid");
  const data = document.getElementById("grid-data");
  if (!el || !data || !window.agGrid) return;
  const rows = JSON.parse(data.textContent);

  const money = v => "$" + Number(v).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const clock = ms => {
    if (ms < 0) return "overdue";
    const m = Math.floor(ms / 60000), d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m % 60}m` : `${m % 60}m ${Math.floor(ms / 1000) % 60}s`;
  };
  const answering = r => ["ready", "investigating", "failed"].includes(r.status);

  // Sort order for the clock column: open cases by time left, then in review, sent, and closed.
  const clockKey = r => {
    if (r.total) return Infinity;
    if (answering(r) && r.due) return Date.parse(r.due) - Date.now();
    if (answering(r)) return 1e13;
    return r.status === "sent" ? 2e13 : 3e13;
  };

  class ClockCell {
    init(p) {
      this.r = p.data;
      this.e = document.createElement("div");
      this.e.className = "gc-clock";
      this.draw();
      if (answering(this.r) && this.r.due) this.t = setInterval(() => this.draw(), 1000);
    }
    draw() {
      const r = this.r;
      if (r.total) { this.e.innerHTML = ""; return; }
      let big, small, tone = "done";
      if (answering(r) && r.due) {
        const ms = Date.parse(r.due) - Date.now(), h = ms / 3600000;
        big = clock(ms); small = "to respond";
        tone = h < 0 ? "overdue" : h < 24 ? "critical" : h < 72 ? "soon" : "ok";
      } else if (answering(r)) { big = "In review"; small = "add info any time"; }
      else if (r.status === "closed") { big = r.won ? "Won" : "Closed"; small = r.won ? "kept " + money(r.amount) : "refunded"; }
      else { big = "Sent"; small = (r.paypal_status || "").toLowerCase(); }
      this.e.dataset.tone = tone;
      this.e.innerHTML = `<b>${esc(big)}</b><span>${esc(small)}</span>`;
    }
    getGui() { return this.e; }
    refresh() { return false; }
    destroy() { clearInterval(this.t); }
  }

  const strengthLabel = {strong: "Strong", medium: "Medium", weak: "Weak"};
  const columnDefs = [
    {colId: "clock", headerName: "Time left", width: 132, pinned: "left", sort: "asc", filter: false,
     valueGetter: p => clockKey(p.data), cellRenderer: ClockCell, comparator: (a, b) => a - b},
    {field: "buyer", headerName: "Buyer and item", flex: 2.2, minWidth: 230, filter: "agTextColumnFilter",
     getQuickFilterText: p => `${p.data.buyer} ${p.data.item} ${p.data.id}`,
     cellRenderer: p => p.data.total ? `<b class="gc-total">${esc(p.value)}</b>`
       : `<div class="gc-two"><b>${esc(p.value)}</b><span>${esc(p.data.item)} · ${esc(p.data.id)}</span></div>`},
    {field: "reason", headerName: "Reason", flex: 1.2, minWidth: 150, filter: "agTextColumnFilter",
     cellRenderer: p => p.value ? `<span class="tag">${esc(p.value)}</span>` : ""},
    {field: "amount", headerName: "Amount", width: 112, type: "numericColumn", filter: "agNumberColumnFilter",
     valueFormatter: p => p.value == null ? "" : money(p.value), cellClass: "gc-amount"},
    {field: "strength", headerName: "Evidence", width: 112, filter: "agTextColumnFilter",
     comparator: (a, b) => ["weak", "medium", "strong"].indexOf(a) - ["weak", "medium", "strong"].indexOf(b),
     cellRenderer: p => p.value ? `<span class="strength st-${esc(p.value)}">${strengthLabel[p.value] || esc(p.value)}</span>` : ""},
    {field: "action_label", headerName: "Recommended", flex: 1.4, minWidth: 170, filter: "agTextColumnFilter",
     cellRenderer: p => p.data.total ? "" : p.data.status === "investigating"
       ? `<span class="chip chip-busy"><span class="spin"></span> Investigating</span>`
       : p.value ? `<span class="chip chip-${esc(p.data.action)}">${esc(p.value)}</span>` : ""},
    {field: "status_label", headerName: "Status", flex: 1, minWidth: 120, filter: "agTextColumnFilter",
     cellRenderer: p => p.value ? `<span class="state s-${esc(p.data.status)}">${esc(p.value)}</span>` : ""},
  ];

  const open = rows.filter(r => r.status !== "closed");
  const api = agGrid.createGrid(el, {
    columnDefs, rowData: rows,
    defaultColDef: {sortable: true, resizable: true, suppressHeaderMenuButton: false},
    domLayout: "autoHeight", rowHeight: 68, headerHeight: 46, animateRows: true,
    getRowId: p => p.data.id || "total",
    rowClass: "gc-row",
    pinnedBottomRowData: [{total: true, buyer: `In dispute across ${open.length} open case${open.length === 1 ? "" : "s"}`,
                           amount: open.reduce((s, r) => s + r.amount, 0)}],
    onRowClicked: e => { if (e.data && !e.data.total) location.href = "/case/" + encodeURIComponent(e.data.id); },
    overlayNoRowsTemplate: "No disputes match.",
  });

  // Narrow screens keep the columns that decide what to do next.
  const narrow = window.matchMedia("(max-width: 760px)");
  const fit = () => api.setColumnsVisible(["reason", "strength", "status_label"], !narrow.matches);
  narrow.addEventListener("change", fit);
  fit();

  document.getElementById("grid-search").addEventListener("input", e => api.setGridOption("quickFilterText", e.target.value));
  el.hidden = false;
  document.querySelector(".grid-tools").hidden = false;
  document.getElementById("case-list").hidden = true;
})();
