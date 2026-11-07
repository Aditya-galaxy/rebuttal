"""A case plan: what Rebuttal proposes to send for one dispute, and how it is sent.

The agent (agent.py) or the rules fallback below produces the plan; gate.py checks it; execute()
turns an approved plan into PayPal calls. A plan is plain JSON so it can be stored, shown, edited
by the seller, and replayed in tests.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from . import evidence_docs
from .playbook import Assessment

SHOP_NAME = "Ember & Clay"

ACTION_LINK = {"fight": "provide_evidence", "supporting_info": "provide_supporting_info",
               "partial_refund": "make_offer", "refund_with_return": "make_offer",
               "refund": "accept_claim", "message": "send_message"}
OFFER_TYPE = {"partial_refund": "REFUND", "refund_with_return": "REFUND_WITH_RETURN"}
DOC_FILES = {"delivery": "proof-of-delivery.pdf", "receipt": "order-receipt.pdf",
             "messages": "buyer-messages.pdf", "listing": "listing-and-policy.pdf",
             "photos": "photo-comparison.pdf"}


def rules_plan(dispute: dict, shop, a: Assessment, action: str | None = None) -> dict[str, Any]:
    """A plan written from templates. Used when no model is configured, as the agent's fallback,
    and when the seller picks a different action than the one recommended."""
    if action and action != a.recommended:
        a = replace(a, recommended=action)
    amount = float(dispute["dispute_amount"]["value"])
    buyer = (dispute.get("disputed_transactions") or [{}])[0].get("buyer", {}).get("name", "there")
    first = buyer.split()[0] if buyer else "there"
    order = shop.order(a.order_id) if a.order_id else None
    claims = [{"text": f, "cites": a.citations[:1] if i == 0 else a.citations} for i, f in enumerate(a.findings)]
    plan: dict[str, Any] = {"dispute_id": dispute["dispute_id"], "action": a.recommended, "author": "rules",
                            "claims": claims, "evidence": [], "offer_amount": None, "buyer_message": "",
                            "summary": ""}
    if a.recommended in ("fight", "supporting_info"):
        ship = shop.shipment(a.order_id) if a.order_id else None
        statement = " ".join(a.findings)
        for i, item in enumerate(a.play):
            doc = item.get("doc")
            if doc == "messages" and not (order and shop.messages(order["id"])):
                continue
            if doc == "photos" and not (order and shop.photos(order_id=order["id"], source="buyer")):
                continue
            ev: dict[str, Any] = {"evidence_type": item["type"], "doc": doc,
                                  "notes": statement if i == 0 else item["what"] + ".",
                                  "cites": a.citations}
            if item.get("tracking") and ship:
                ev["tracking"] = {"carrier_name": ship["carrier"].upper().replace(" ", "_"),
                                  "tracking_number": ship["tracking_number"]}
            plan["evidence"].append(ev)
        requested = {e["evidence_type"] for e in (dispute.get("evidences") or [])
                     if e.get("source") == "REQUESTED_FROM_SELLER"}
        for t in sorted(requested - {e["evidence_type"] for e in plan["evidence"]}):
            plan["evidence"].append({"evidence_type": t, "doc": "receipt", "notes": "Order receipt.",
                                     "cites": a.citations[:1]})
        plan["summary"] = (f"Your records support fighting this one. {a.findings[-1]} "
                           f"Rebuttal will send {len(plan['evidence'])} pieces of evidence to PayPal.")
        if a.recommended == "supporting_info":
            plan["buyer_message"] = ""
            plan["summary"] = (f"PayPal is already reviewing this claim, and you can still add to it. "
                               f"{a.findings[-1]} Rebuttal will send the delivery record and your shipping "
                               "notice as supporting information.")
        elif a.reason != "UNAUTHORISED":   # card chargebacks go through the bank; there's no buyer to message
            plan["buyer_message"] = (f"Hi {first}, our records show your order was delivered. We've shared "
                                     "the carrier's delivery record with PayPal. If something is wrong, please "
                                     "reply and we'll help.")
    elif a.recommended in OFFER_TYPE:
        opt = next((o for o in a.options if o.action == a.recommended), None)
        offer = opt.offer_amount if opt else amount
        plan["offer_amount"] = offer
        plan["summary"] = f"{opt.label}: {opt.detail}" if opt else "PayPal doesn't offer this option here."
        plan["buyer_message"] = (f"Hi {first}, I'm sorry the order wasn't what you hoped for. "
                                 + (f"I'd like to refund ${offer:.2f} and you keep the item."
                                    if a.recommended == "partial_refund" else
                                    "Please send the item back and I'll refund you in full when it arrives."))
    elif a.recommended == "message":
        ship = shop.shipment(a.order_id) if a.order_id else None
        eta = ship.get("expected_delivery") if ship else None
        when = f" and {ship['carrier']} expects to deliver it on {_date(eta)}" if eta else ""
        plan["summary"] = (f"The parcel is still on its way. {a.findings[-1] if a.findings else ''} "
                           "Rebuttal will send the buyer the tracking so they can follow it.").strip()
        plan["buyer_message"] = (
            f"Hi {first}, I'm sorry for the wait. Your order is on its way{when}. "
            + (f"You can follow it with {ship['carrier']} tracking number {ship['tracking_number']}. " if ship
               else "")
            + "If it hasn't arrived by then, reply here and I'll sort it out straight away.")
    elif a.recommended == "refund":
        plan["summary"] = (f"The records don't support fighting this one. {a.findings[-1] if a.findings else ''} "
                           f"Refunding ${amount:.2f} now avoids the dispute fee.").strip()
        plan["buyer_message"] = (f"Hi {first}, I'm sorry your order didn't reach you. "
                                 "I've refunded you in full through PayPal.")
    return plan


def _date(iso: str) -> str:
    from .playbook import parse_time
    return parse_time(iso).strftime("%-d %B")


def documents(plan: dict, shop, dispute_id: str, a: Assessment) -> dict[str, bytes]:
    """The PDF files a plan's evidence refers to, keyed by file name."""
    order = shop.order(a.order_id) if a.order_id else None
    if not order:
        return {}
    out: dict[str, bytes] = {}
    for ev in plan.get("evidence", []):
        doc = ev.get("doc")
        if not doc or DOC_FILES.get(doc) in out:
            continue
        if doc == "delivery" and (ship := shop.shipment(order["id"])):
            out[DOC_FILES[doc]] = evidence_docs.delivery(SHOP_NAME, order, ship)
        elif doc == "messages":
            out[DOC_FILES[doc]] = evidence_docs.messages(SHOP_NAME, order, shop.messages(order["id"]))
        elif doc in ("listing", "policy"):
            product = shop.product(order["items"][0]["sku"])
            if product:
                pics = shop.photos(sku=product["sku"], source="listing")
                out[DOC_FILES["listing"]] = evidence_docs.listing(SHOP_NAME, product, shop.policy("returns"),
                                                                  pics[0] if pics else None)
        elif doc == "photos":
            buyer = shop.photos(order_id=order["id"], source="buyer")
            listing = shop.photos(sku=order["items"][0]["sku"], source="listing") if order["items"] else []
            if buyer and listing:
                out[DOC_FILES["photos"]] = evidence_docs.photo_comparison(
                    SHOP_NAME, order, listing[0], buyer[-1], shop.photo_check(buyer[-1]["id"]))
        elif doc == "receipt":
            out[DOC_FILES[doc]] = evidence_docs.receipt(SHOP_NAME, order, dispute_id)
    return out


def paypal_evidences(plan: dict) -> list[dict]:
    """The plan's evidence in the shape provide-evidence expects."""
    out = []
    for ev in plan.get("evidence", []):
        item: dict[str, Any] = {"evidence_type": ev["evidence_type"], "notes": ev.get("notes", "")[:2000]}
        if ev.get("tracking"):
            item["evidence_info"] = {"tracking_info": [ev["tracking"]]}
        doc = ev.get("doc")
        if doc:
            item["documents"] = [{"name": DOC_FILES["listing" if doc == "policy" else doc]}]
        out.append(item)
    return out


def supporting_notes(plan: dict) -> str:
    """One statement for provide-supporting-info, which takes a single notes field (2,000 characters)."""
    parts = [e["notes"].strip() for e in plan.get("evidence", []) if e.get("notes")]
    for e in plan.get("evidence", []):
        t = e.get("tracking")
        if t and t["tracking_number"] not in " ".join(parts):
            parts.append(f"Carrier tracking: {t['carrier_name']} {t['tracking_number']}.")
    return "\n\n".join(parts)[:2000]


def execute(pp, plan: dict, files: dict[str, bytes]) -> list[str]:
    """Send an approved plan. Returns a log of what was sent."""
    did, action = plan["dispute_id"], plan["action"]
    log = []
    if action == "fight":
        # Message first: once evidence is in, the dispute goes to PayPal review and messaging closes.
        if plan.get("buyer_message"):
            try:
                pp.send_message(did, plan["buyer_message"])
                log.append("Messaged the buyer.")
            except Exception as e:  # the message is a courtesy; the evidence is what counts
                log.append(f"Couldn't message the buyer: {e}")
        evidences = paypal_evidences(plan)
        wanted = {d["name"] for e in evidences for d in e.get("documents", [])}
        parts = [(name, data, "application/pdf") for name, data in files.items() if name in wanted]
        pp.provide_evidence(did, evidences, parts)
        log.append(f"Sent {len(evidences)} evidence item{'' if len(evidences) == 1 else 's'} and "
                   f"{len(parts)} document{'' if len(parts) == 1 else 's'} to PayPal.")
    elif action == "supporting_info":
        notes = supporting_notes(plan)
        wanted = {d["name"] for e in paypal_evidences(plan) for d in e.get("documents", [])}
        parts = [(name, data, "application/pdf") for name, data in files.items() if name in wanted]
        pp.provide_supporting_info(did, notes, parts)
        log.append(f"Sent supporting information ({len(notes)} characters) and {len(parts)} "
                   f"document{'' if len(parts) == 1 else 's'} to PayPal.")
    elif action in OFFER_TYPE:
        amount = plan.get("offer_amount")
        money = {"currency_code": "USD", "value": f"{amount:.2f}"} if amount is not None else None
        pp.make_offer(did, OFFER_TYPE[action], money, plan.get("buyer_message") or "Offer from the seller.")
        log.append(f"Offered {OFFER_TYPE[action].replace('_', ' ').lower()}"
                   + (f" of ${amount:.2f}." if amount is not None else "."))
    elif action == "refund":
        pp.accept_claim(did, plan.get("buyer_message") or "Refunded in full.")
        log.append("Accepted the claim and refunded in full.")
    elif action == "message":
        pp.send_message(did, plan["buyer_message"])
        log.append("Messaged the buyer.")
    return log
