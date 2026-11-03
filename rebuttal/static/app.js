// Live countdowns, and polling while an investigation runs.
(function () {
  function fmt(ms) {
    if (ms < 0) return "overdue";
    const m = Math.floor(ms / 60000), d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m % 60}m` : `${m % 60}m ${Math.floor(ms / 1000) % 60}s`;
  }
  function tick() {
    document.querySelectorAll("[data-due]").forEach(el => {
      const due = Date.parse(el.dataset.due);
      if (!isNaN(due)) el.textContent = fmt(due - Date.now());
    });
  }
  tick();
  setInterval(tick, 1000);

  const tr = document.querySelector("[data-trace]");
  const poll = document.querySelector("[data-poll]");
  if (tr) {
    const names = {get_dispute: "Read the PayPal dispute", get_assessment: "Weighed the evidence and the costs",
      find_order: "Found the order", get_shipment: "Read the carrier history", get_messages: "Read your emails",
      get_product: "Read the listing", get_policy: "Read your policies", submit_plan: "Wrote the plan",
      fallback: "Fell back to templates", rules: "Wrote the plan from templates"};
    setInterval(async () => {
      const r = await fetch(tr.dataset.trace).then(r => r.json()).catch(() => null);
      if (!r) return;
      tr.innerHTML = r.trace.map(t => `<li>${names[t.tool] || t.tool}</li>`).join("");
      if (r.status !== "investigating") location.reload();
    }, 1500);
  } else if (poll) {
    setTimeout(() => location.reload(), 3000);
  }
})();
