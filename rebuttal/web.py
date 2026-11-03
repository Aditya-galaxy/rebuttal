"""The seller's web app: the dispute board, the case review, settings, and the demo storefront."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .engine import Engine
from .fake_paypal import FakePayPal
from .paypal import PayPalError
from .plan import DOC_FILES
from .playbook import REASON_LABELS, Settings, hours_left, parse_time, urgency
from .state import CLOSED, FAILED, INVESTIGATING, READY, SENT

HERE = Path(__file__).parent
log = logging.getLogger("rebuttal")
templates = Jinja2Templates(directory=HERE / "templates")
app = FastAPI(title="Rebuttal")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

engine: Engine | None = None


def eng() -> Engine:
    global engine
    if engine is None:
        engine = Engine.from_env()
        engine.sync()
    return engine


ACTION_LABELS = {"fight": "Fight with evidence", "supporting_info": "Send supporting information",
                 "partial_refund": "Partial refund",
                 "refund_with_return": "Refund after return", "refund": "Refund in full",
                 "message": "Message the buyer"}
STATUS_LABELS = {INVESTIGATING: "Investigating", READY: "Ready for you", SENT: "Sent to PayPal",
                 CLOSED: "Closed", FAILED: "Needs attention"}


def _row(case: dict) -> dict:
    d = case["dispute"]
    txn = (d.get("disputed_transactions") or [{}])[0]
    h = hours_left(d)
    plan = case.get("plan") or {}
    outcome = (d.get("dispute_outcome") or {}).get("outcome_code", "")
    return {
        "id": case["dispute_id"], "buyer": txn.get("buyer", {}).get("name", "Buyer"),
        "item": ", ".join(i.get("item_name", "") for i in txn.get("items", [])) or "Order",
        "reason": REASON_LABELS.get(d.get("reason"), d.get("reason")),
        "amount": float(d["dispute_amount"]["value"]), "currency": d["dispute_amount"]["currency_code"],
        "stage": (d.get("dispute_life_cycle_stage") or "").title(),
        "paypal_status": d.get("status", "").replace("_", " ").title(),
        "due": d.get("seller_response_due_date"), "hours": h,
        "urgency": urgency(h) if case["status"] in (READY, INVESTIGATING, FAILED) else "done",
        "status": case["status"], "status_label": STATUS_LABELS.get(case["status"], case["status"]),
        "action": plan.get("action"), "action_label": ACTION_LABELS.get(plan.get("action"), ""),
        "outcome": outcome,
        "won": outcome == "RESOLVED_SELLER_FAVOUR",
    }


def _summary(rows: list[dict]) -> dict:
    open_rows = [r for r in rows if r["status"] != CLOSED]
    closed = [r for r in rows if r["status"] == CLOSED]
    next_due = min((r for r in open_rows if r["status"] in (READY, INVESTIGATING) and r["hours"] is not None),
                   key=lambda r: r["hours"], default=None)
    return {"at_risk": sum(r["amount"] for r in open_rows), "open": len(open_rows),
            "waiting_on_you": sum(1 for r in open_rows if r["status"] == READY),
            "next_due": next_due, "won": sum(r["amount"] for r in closed if r["won"]),
            "closed": len(closed)}


def _ctx(request: Request, **kw) -> dict:
    e = eng()
    return {"request": request, "mode": e.mode, "model": e.llm.model if e.llm else None, **kw}


@app.get("/", response_class=HTMLResponse)
def board(request: Request):
    rows = sorted((_row(c) for c in eng().state.all()),
                  key=lambda r: (r["status"] == CLOSED, r["status"] == SENT,
                                 r["hours"] if r["hours"] is not None else 1e9))
    return templates.TemplateResponse(request, "board.html", _ctx(request, rows=rows, s=_summary(rows),
                                                         busy=any(r["status"] == INVESTIGATING for r in rows)))


@app.post("/demo/reset")
def reset_demo():
    eng().reset_demo()
    return RedirectResponse("/", 303)


@app.on_event("shutdown")
def _flush():
    if engine and engine.snapshots:
        engine.snapshots.stop()


@app.post("/sync")
def sync():
    eng().sync()
    return RedirectResponse("/", 303)


@app.get("/case/{dispute_id}", response_class=HTMLResponse)
def case_page(request: Request, dispute_id: str):
    e = eng()
    case = e.state.get(dispute_id)
    if not case:
        raise HTTPException(404)
    _, a, settings = e.context(dispute_id)
    d = case["dispute"]
    order = e.shop.order(a.order_id) if a.order_id else None
    plan = case.get("plan") or {}
    docs = sorted({DOC_FILES["listing" if ev.get("doc") == "policy" else ev["doc"]]
                   for ev in plan.get("evidence", []) if ev.get("doc")})
    checks = case.get("checks") or []
    return templates.TemplateResponse(request, "case.html", _ctx(
        request, c=case, r=_row(case), d=d, a=a, order=order, plan=plan, docs=docs, checks=checks,
        passed=all(ch["ok"] for ch in checks if ch["blocking"]) if checks else False,
        shipment=e.shop.shipment(a.order_id) if a.order_id else None,
        messages=e.shop.messages(a.order_id) if a.order_id else [],
        buyer_msgs=[m for m in (d.get("messages") or []) if m.get("posted_by") == "BUYER"]
        + [{"content": ev["notes"], "time_posted": ev.get("date")} for ev in (d.get("evidences") or [])
           if ev.get("source") == "SUBMITTED_BY_BUYER" and ev.get("notes")],
        requested=[ev["evidence_type"] for ev in (d.get("evidences") or [])
                   if ev.get("source") == "REQUESTED_FROM_SELLER"],
        submitted=[ev for ev in (d.get("evidences") or []) if ev.get("source") == "SUBMITTED_BY_SELLER"],
        actions=ACTION_LABELS, settings=settings, is_fake=isinstance(e.pp, FakePayPal),
        can_adjudicate=any(lk.get("rel") == "adjudicate" for lk in d.get("links", [])),
        error=request.query_params.get("error")))


@app.get("/case/{dispute_id}/status")
def case_status(dispute_id: str):
    case = eng().state.get(dispute_id)
    if not case:
        raise HTTPException(404)
    live = eng().live.get(dispute_id)
    trace = live if live is not None else (case.get("plan") or {}).get("trace", [])
    return {"status": case["status"], "trace": [{"tool": t.get("tool")} for t in trace]}


@app.post("/case/{dispute_id}/action")
def choose_action(dispute_id: str, action: str = Form(...)):
    eng().replan(dispute_id, action)
    return RedirectResponse(f"/case/{dispute_id}", 303)


@app.post("/case/{dispute_id}/reinvestigate")
def reinvestigate(dispute_id: str):
    e = eng()
    e.state.set_status(dispute_id, INVESTIGATING, "Seller asked for a fresh investigation.")
    e.investigate(dispute_id)
    return RedirectResponse(f"/case/{dispute_id}", 303)


@app.post("/case/{dispute_id}/edit")
async def edit_plan(request: Request, dispute_id: str):
    form = await request.form()
    notes = form.getlist("notes")
    offer = form.get("offer_amount")
    eng().edit(dispute_id, [str(n) for n in notes], str(form.get("buyer_message", "")),
               float(offer) if offer not in (None, "") else None)
    return RedirectResponse(f"/case/{dispute_id}", 303)


@app.post("/case/{dispute_id}/send")
def send(dispute_id: str):
    try:
        eng().send(dispute_id)
    except (ValueError, PayPalError) as err:
        return RedirectResponse(f"/case/{dispute_id}?error={str(err)[:300]}", 303)
    return RedirectResponse(f"/case/{dispute_id}", 303)


@app.post("/case/{dispute_id}/simulate")
def simulate(dispute_id: str, outcome: str = Form(...)):
    e = eng()
    try:
        if outcome == "buyer_accepts":
            e.simulate_buyer_accepts(dispute_id)
        else:
            e.simulate_decision(dispute_id, outcome == "seller")
    except PayPalError as err:
        return RedirectResponse(f"/case/{dispute_id}?error={str(err)[:300]}", 303)
    return RedirectResponse(f"/case/{dispute_id}", 303)


@app.get("/case/{dispute_id}/doc/{name}")
def document(dispute_id: str, name: str):
    files = eng().documents(dispute_id)
    if name not in files:
        raise HTTPException(404)
    return Response(files[name], media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{dispute_id}-{name}"'})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", _ctx(request, st=eng().state.settings(),
                                                            saved=request.query_params.get("saved")))


@app.post("/settings")
def save_settings(dispute_fee: float = Form(...), return_shipping_cost: float = Form(...),
                  partial_refund_pct: int = Form(...), autopilot_limit: float = Form(...)):
    eng().state.save_settings(Settings(dispute_fee, return_shipping_cost, max(5, min(90, partial_refund_pct)),
                                       max(0.0, autopilot_limit)))
    return RedirectResponse("/settings?saved=1", 303)


# --- the demo storefront: real sandbox checkout, or a simulated one in mock mode ---------------

@app.get("/shop", response_class=HTMLResponse)
def storefront(request: Request):
    e = eng()
    return templates.TemplateResponse(request, "shop.html", _ctx(
        request, products=e.shop.products(), client_id=os.environ.get("PAYPAL_CLIENT_ID", ""),
        is_fake=isinstance(e.pp, FakePayPal), done=request.query_params.get("done")))


@app.post("/shop/orders")
async def create_order(request: Request):
    body = await request.json()
    e = eng()
    product = e.shop.product(body["sku"])
    if not product:
        raise HTTPException(404)
    order = e.pp.create_order(product["price"], "USD", product["title"], body["sku"])
    return {"id": order["id"]}


@app.post("/shop/orders/{paypal_order_id}/capture")
async def capture_order(request: Request, paypal_order_id: str):
    body = await request.json()
    e = eng()
    captured = e.pp.capture_order(paypal_order_id)
    unit = captured["purchase_units"][0]
    cap = unit["payments"]["captures"][0]
    payer = captured.get("payer", {})
    name = " ".join(filter(None, [payer.get("name", {}).get("given_name"), payer.get("name", {}).get("surname")]))
    ship = unit.get("shipping", {})
    addr = ship.get("address", {})
    ship_to = ", ".join(filter(None, [ship.get("name", {}).get("full_name") or name, addr.get("address_line_1"),
                                      addr.get("admin_area_2"), addr.get("admin_area_1"), addr.get("postal_code"),
                                      addr.get("country_code")]))
    product = e.shop.product(body["sku"])
    order_id = e.shop.next_order_id()
    e.shop.add_order(order_id, cap["id"], name or "Sandbox buyer", payer.get("email_address", ""), ship_to,
                     [{"sku": product["sku"], "title": product["title"], "qty": 1, "price": product["price"]}],
                     cap["amount"]["value"], cap["amount"]["currency_code"], paypal_order_id=paypal_order_id)
    _ship_demo(e, order_id, cap["id"], body.get("scenario", "delivered"), name or "Sandbox buyer",
               addr.get("admin_area_2", ""), addr.get("admin_area_1", ""))
    return {"order_id": order_id, "transaction_id": cap["id"]}


@app.post("/shop/simulate")
def simulate_purchase(sku: str = Form(...), scenario: str = Form("delivered"), reason: str = Form(...)):
    """Mock mode: a buyer pays, the studio ships, and the buyer opens a dispute."""
    e = eng()
    if not isinstance(e.pp, FakePayPal):
        raise HTTPException(400, "Only in mock mode; use the PayPal buttons in sandbox mode.")
    product = e.shop.product(sku)
    order_id = e.shop.next_order_id()
    txn = "SIM" + datetime.now(UTC).strftime("%m%d%H%M%S") + order_id[-3:]
    created = (datetime.now(UTC) - timedelta(days=20)).isoformat(timespec="seconds")
    e.shop.add_order(order_id, txn, "Casey Rivera", "casey.rivera@example.com",
                     "Casey Rivera, 742 Evergreen Ter, Springfield, OR 97477, US",
                     [{"sku": sku, "title": product["title"], "qty": 1, "price": product["price"]}],
                     product["price"], created_at=created)
    _ship_demo(e, order_id, txn, scenario, "Casey Rivera", "Springfield", "OR")
    reason_code = {"inr": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "snad": "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
                   "unauth": "UNAUTHORISED"}[reason]
    msg = {"inr": "My order never arrived.", "snad": "This is not what the photos showed.", "unauth": ""}[reason]
    d = e.pp.open_dispute(transaction_id=txn, buyer_name="Casey Rivera", amount=product["price"],
                          reason=reason_code, items=[product["title"]], message=msg,
                          stage="CHARGEBACK" if reason == "unauth" else "INQUIRY",
                          requested=["PROOF_OF_FULFILLMENT"] if reason != "snad" else [])
    e.refresh(e.pp.get_dispute(d["dispute_id"]))
    return RedirectResponse(f"/case/{d['dispute_id']}", 303)


def _ship_demo(e: Engine, order_id: str, transaction_id: str, scenario: str, buyer: str, city: str,
               state: str) -> None:
    """Record a shipment for a demo order (a real studio would sync this from its carrier account).

    Carrier events fall between the payment and now, so a sandbox order paid a minute ago shows a
    delivery a few seconds ago rather than one dated before the payment.
    """
    now = datetime.now(UTC)
    start = parse_time(e.shop.order(order_id)["created_at"])
    at = lambda f: (start + (now - start) * f).isoformat(timespec="seconds")  # noqa: E731
    number = "77" + now.strftime("%d%H%M%S%f")[:10]
    if scenario == "lost":
        events = [{"at": at(0.05), "where": "Jaipur, IN", "event": "Picked up"},
                  {"at": at(0.1), "where": "Mumbai, IN", "event": "Departed origin country"}]
        e.shop.add_shipment(order_id, "FedEx", number, "FedEx International Economy", "IN_TRANSIT", None, events)
    else:
        first, _, last = buyer.partition(" ")
        signed = f"{first[:1]}. {last or first}".upper()
        where = ", ".join(filter(None, [city, state])) or "Destination"
        events = [{"at": at(0.1), "where": "Jaipur, IN", "event": "Picked up"},
                  {"at": at(0.5), "where": "Memphis, TN, US", "event": "International shipment release"},
                  {"at": at(0.9), "where": f"{where}, US", "event": f"Delivered, signed for by {signed}"}]
        e.shop.add_shipment(order_id, "FedEx", number, "FedEx International Priority", "DELIVERED", signed,
                            events)
    if not isinstance(e.pp, FakePayPal):
        try:
            e.pp.add_tracker(transaction_id, number, "FEDEX", "DELIVERED" if scenario != "lost" else "SHIPPED")
        except PayPalError as err:   # tracking upload helps the demo but mustn't break checkout
            log.warning("tracking upload for %s failed: %s", order_id, err)


# --- PayPal webhooks ---------------------------------------------------------------------------

@app.post("/webhooks/paypal")
async def webhook(request: Request):
    e = eng()
    raw = await request.body()
    event = json.loads(raw or b"{}")
    webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID")
    if webhook_id and not isinstance(e.pp, FakePayPal):
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            verified = await run_in_threadpool(e.pp.verify_webhook, headers, event, webhook_id)
        except PayPalError:
            verified = False
        if not verified:
            raise HTTPException(400, "signature check failed")
    elif not isinstance(e.pp, FakePayPal):
        raise HTTPException(503, "PAYPAL_WEBHOOK_ID not set")
    if event.get("event_type", "").startswith("CUSTOMER.DISPUTE."):
        did = (event.get("resource") or {}).get("dispute_id")
        if did:
            try:
                dispute = await run_in_threadpool(e.pp.get_dispute, did)
            except PayPalError as err:   # e.g. a simulator event for a dispute that doesn't exist
                log.warning("webhook for %s: %s", did, err)
                return JSONResponse({"ok": True, "ignored": "unknown dispute"})
            await run_in_threadpool(e.refresh, dispute)
    return JSONResponse({"ok": True})


@app.get("/health")
def health():
    return {"ok": True, "mode": eng().mode}


def _fmt_time(iso: str | None) -> str:
    return parse_time(iso).strftime("%-d %b, %H:%M UTC") if iso else ""


def _money(v) -> str:
    return f"${float(v):,.2f}"


def _clock(hours: float | None) -> str:
    if hours is None:
        return "-"
    if hours < 0:
        return "overdue"
    d, h = divmod(int(hours), 24)
    return f"{d}d {h}h" if d else f"{h}h {int((hours % 1) * 60)}m"


templates.env.filters.update(when=_fmt_time, money=_money, clock=_clock,
                             evlabel=lambda t: t.replace("_", " ").capitalize())
