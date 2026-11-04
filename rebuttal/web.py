"""The seller's web app: the dispute board, the case review, settings, and the demo storefront."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import insights, photos, responses_bridge, vertex
from .engine import Engine
from .fake_paypal import FakePayPal
from .paypal import PayPalError
from .plan import DOC_FILES
from .playbook import REASON_LABELS, Settings, assess, hours_left, parse_time, urgency
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
    e = eng()
    settings = e.state.settings()
    rows = []
    for c in e.state.all():
        r = _row(c)
        r["strength"] = assess(c["dispute"], e.shop, settings).strength
        rows.append(r)
    rows.sort(key=lambda r: (r["status"] == CLOSED, r["status"] == SENT,
                             r["hours"] if r["hours"] is not None else 1e9))
    for i, r in enumerate(rows):
        r["rank"] = i   # the grid's default order: open cases by deadline, then sent, then closed
    return templates.TemplateResponse(request, "board.html", _ctx(
        request, rows=rows, s=_summary(rows), busy=any(r["status"] == INVESTIGATING for r in rows)))


@app.post("/demo/reset")
def reset_demo():
    eng().reset_demo()
    return RedirectResponse("/", 303)


@app.on_event("startup")
def _logging():
    """uvicorn's logging setup disables loggers created before it ran, which includes ours."""
    logger = logging.getLogger("rebuttal")
    logger.disabled = False
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     rebuttal: %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False


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
        error=request.query_params.get("error"), **_photo_context(e, order)))


def _photo_context(e: Engine, order: dict | None) -> dict:
    """The listing photo and any photo the buyer sent, with Gemini's comparison if it has run."""
    if not order or not order["items"]:
        return {"listing_photo": None, "buyer_photo": None, "photo_check": None, "can_check_photos": False}
    listing = e.shop.photos(sku=order["items"][0]["sku"], source="listing")
    buyer = e.shop.photos(order_id=order["id"], source="buyer")
    return {"listing_photo": listing[0] if listing else None, "buyer_photo": buyer[-1] if buyer else None,
            "photo_check": e.shop.photo_check(buyer[-1]["id"]) if buyer else None,
            "can_check_photos": bool(buyer and listing and vertex.project())}


@app.post("/case/{dispute_id}/photos")
def check_photos(dispute_id: str):
    e = eng()
    case, a, _ = e.context(dispute_id)
    if a.order_id:
        words = " ".join(m.get("content", "") for m in case["dispute"].get("messages") or []
                         if m.get("posted_by") == "BUYER")
        if not photos.check(e.shop, a.order_id, words):
            return RedirectResponse(f"/case/{dispute_id}?error=The photo check couldn't run right now.", 303)
    return RedirectResponse(f"/case/{dispute_id}", 303)


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
        is_fake=isinstance(e.pp, FakePayPal), done=request.query_params.get("done"),
        orders=[] if isinstance(e.pp, FakePayPal) else e.shop.recent_orders()))


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
    arrives = _ship_demo(e, order_id, cap["id"], body.get("scenario", "delivered"), name or "Sandbox buyer",
                         addr.get("admin_area_2", ""), addr.get("admin_area_1", ""), realtime=True)
    return {"order_id": order_id, "transaction_id": cap["id"], "arrives": arrives}


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


# FedEx International Priority from Jaipur, hours after payment. Scaled by SHIP_HOURS_SCALE so a
# test can run the journey in minutes; the demo uses real time.
JOURNEY = [(20, "Jaipur, IN", "Picked up"),
           (32, "New Delhi, IN", "Departed FedEx hub"),
           (54, "Memphis, TN, US", "Arrived at FedEx hub"),
           (60, "Memphis, TN, US", "International shipment release - Import"),
           (86, "{where}, US", "On FedEx vehicle for delivery"),
           (92, "{where}, US", "Delivered, signed for by {signed}")]


def _ship_demo(e: Engine, order_id: str, transaction_id: str, scenario: str, buyer: str, city: str,
               state: str, realtime: bool = False) -> str | None:
    """Record a shipment for a demo order (a real studio would sync this from its carrier account).

    The journey starts at payment (now, for a real sandbox order; the backdated order date in demo
    mode), and the shop only reveals each scan once its time has passed, so a parcel arrives days
    after payment as it would for real. Returns the planned delivery time.
    """
    now = datetime.now(UTC)
    first, _, last = buyer.partition(" ")
    signed = f"{first[:1]}. {last or first}".upper()
    where = ", ".join(filter(None, [city, state])) or "Destination"
    start = now if realtime else parse_time(e.shop.order(order_id)["created_at"])
    scale = float(os.environ.get("SHIP_HOURS_SCALE", "1")) if realtime else 1.0
    at = lambda h: (start + timedelta(hours=h * scale)).isoformat(timespec="seconds")  # noqa: E731
    journey = JOURNEY[:2] if scenario == "lost" else JOURNEY
    events = [{"at": at(h), "where": w.format(where=where), "event": ev.format(signed=signed)}
              for h, w, ev in journey]
    number = "77" + now.strftime("%d%H%M%S%f")[:10]
    if scenario == "lost":
        e.shop.add_shipment(order_id, "FedEx", number, "FedEx International Economy", "IN_TRANSIT", None, events)
    else:
        e.shop.add_shipment(order_id, "FedEx", number, "FedEx International Priority", "DELIVERED", signed,
                            events)
    if not isinstance(e.pp, FakePayPal):
        try:
            e.pp.add_tracker(transaction_id, number, "FEDEX", "SHIPPED")
            e.shop.set_tracker_status(order_id, "SHIPPED")
        except PayPalError as err:   # tracking upload helps the demo but mustn't break checkout
            log.warning("tracking upload for %s failed: %s", order_id, err)
    return events[-1]["at"] if scenario != "lost" else None


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


# --- Insights: AG Studio over the dispute history, with an AI assistant ---------------------

AG_STUDIO_CDN = "https://cdn.jsdelivr.net/npm/ag-studio@3.0.0/dist/umd/ag-studio.min.js"
# AG Studio's AI assistant runs several sub-agents built around OpenAI's strict-schema tool calls. Its data
# agent's query tool has a recursive schema that other providers can't take as-is: with Nebius models
# (DeepSeek V4 Pro, Qwen3 235B, Kimi K2.6, MiniMax M3) it stalled, and Vertex AI's Gemini endpoints reject
# the schema, so through the simplified one its queries fail. Off by default; INSIGHTS_AI=1 with
# INSIGHTS_LLM_* (OpenAI) turns it on, INSIGHTS_AI=vertex tries Gemini (experimental).
INSIGHTS_MODEL = "gpt-5.4-mini"


@app.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request):
    e = eng()
    config = {"dataUrl": "/api/insights", "aiEnabled": _insights_ai_on(),
              "licenseKey": os.environ.get("AG_STUDIO_LICENSE_KEY", "")}
    return templates.TemplateResponse(request, "insights.html", _ctx(
        request, config=config, studio_js=AG_STUDIO_CDN, generated=e.mode == "mock"))


@app.get("/api/insights")
def insights_data():
    e = eng()
    return insights.build(e, demo_history=e.mode == "mock")


@app.post("/api/ai/responses")
async def ai_responses(request: Request):
    """The AG Studio assistant's model calls, forwarded with the server's key (never sent to the browser).

    The demo is public, so calls are capped per visitor and per day, the model is fixed, and the
    output length is bounded.
    """
    if not _insights_ai_on():
        return JSONResponse({"error": {"message": "The assistant isn't configured on this server."}}, 503)
    who = request.client.host if request.client else "?"
    if not ai_limiter.allow(who):
        return JSONResponse({"error": {"message": "That's a lot of questions; try again in a little while."}}, 429)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": {"message": "Request body must be JSON."}}, 400)
    body["max_output_tokens"] = min(int(body.get("max_output_tokens") or 8000), 8000)
    if _insights_provider() == "vertex":
        return await _ai_via_vertex(body)
    model = os.environ.get("INSIGHTS_LLM_MODEL", INSIGHTS_MODEL)
    body["model"] = model
    _give_ids(body.get("input"))
    base = os.environ.get("INSIGHTS_LLM_BASE_URL", "https://api.openai.com/v1/")
    key = os.environ.get("INSIGHTS_LLM_API_KEY", "")
    client = httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(180, connect=15))
    upstream = await client.send(client.build_request(
        "POST", "responses", json=body, headers={"Authorization": f"Bearer {key}"}), stream=True)


    async def relay():
        try:
            async for chunk in upstream.aiter_bytes():   # decoded: upstream compresses
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(relay(), status_code=upstream.status_code, headers={"Cache-Control": "no-store"},
                             media_type=upstream.headers.get("content-type", "application/json"))


def _insights_provider() -> str | None:
    """INSIGHTS_AI=1 with an OpenAI key, or INSIGHTS_AI=vertex (experimental: Gemini runs the
    assistant's top level, but its data agent's queries fail on the simplified schemas). Off otherwise."""
    if os.environ.get("INSIGHTS_AI") == "1" and os.environ.get("INSIGHTS_LLM_API_KEY"):
        return "openai"
    if os.environ.get("INSIGHTS_AI") == "vertex" and vertex.project():
        return "vertex"
    return None


def _insights_ai_on() -> bool:
    return _insights_provider() is not None


async def _ai_via_vertex(body: dict):
    """Answer a Responses API request with Gemini's Chat Completions on Vertex AI (see responses_bridge)."""
    model = os.environ.get("INSIGHTS_VERTEX_MODEL", vertex.INSIGHTS_MODEL)
    chat = responses_bridge.to_chat(body, model)
    # A 429 here means the location's shared capacity is busy, so move to the next location before
    # waiting; AG Studio's sub-agents send bursts of calls.
    locations = vertex.FALLBACK_LOCATIONS if "preview" not in model else ["global"]
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
        for attempt in range(8):
            token = await run_in_threadpool(vertex.token)
            try:
                r = await client.post(vertex.openai_base(locations[attempt % len(locations)]) + "chat/completions",
                                      json=chat, headers={"Authorization": f"Bearer {token}"})
            except httpx.TimeoutException:
                log.warning("insights assistant: %s timed out, trying the next location",
                            locations[attempt % len(locations)])
                continue
            if r.status_code not in (429, 500, 503):
                break
            await asyncio.sleep(1 if attempt < len(locations) - 1 else 4)
    if r.status_code >= 400:
        detail = r.text[:300]
        log.warning("insights assistant: Vertex returned %s: %s", r.status_code, detail)
        return JSONResponse({"error": {"message": f"The model returned an error ({r.status_code})."}}, 502)
    response = responses_bridge.to_response(r.json(), model)
    return StreamingResponse(responses_bridge.events(response), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})


def _give_ids(items) -> None:
    """Earlier output items sent back as input: OpenAI accepts them without an id, Nebius doesn't."""
    prefix = {"message": "msg", "function_call": "fc", "reasoning": "rs"}
    for item in items if isinstance(items, list) else []:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind in prefix and not item.get("id") and (kind != "message" or item.get("role") == "assistant"):
            item["id"] = f"{prefix[kind]}_{uuid.uuid4().hex}"


class RateLimit:
    """At most `per_visitor` calls per visitor per hour, and `per_day` in all."""

    def __init__(self, per_visitor: int = 40, per_day: int = 600):
        self.per_visitor, self.per_day = per_visitor, per_day
        self.calls: dict[str, list[float]] = {}
        self.day: list[float] = []

    def allow(self, who: str) -> bool:
        now = time.time()
        mine = [t for t in self.calls.get(who, []) if now - t < 3600]
        self.day = [t for t in self.day if now - t < 86400]
        if len(mine) >= self.per_visitor or len(self.day) >= self.per_day:
            self.calls[who] = mine
            return False
        self.calls[who] = [*mine, now]
        self.day.append(now)
        return True


ai_limiter = RateLimit()


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
