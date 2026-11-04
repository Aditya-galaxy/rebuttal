"""What wins each kind of dispute, and the deterministic read on a case.

The agent writes the response; this module decides the facts that the response may lean on:
how strong the seller's position is, what each option costs, and how long is left. Keeping these
out of the model means a confident-sounding draft can't turn a lost case into a "fight".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

REASON_LABELS = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "Item not received",
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "Not as described",
    "UNAUTHORISED": "Unauthorised payment",
    "CREDIT_NOT_PROCESSED": "Refund not processed",
    "DUPLICATE_TRANSACTION": "Duplicate payment",
    "INCORRECT_AMOUNT": "Incorrect amount",
    "PAYMENT_BY_OTHER_MEANS": "Paid by other means",
    "CANCELED_RECURRING_BILLING": "Cancelled subscription billed",
    "PROBLEM_WITH_REMITTANCE": "Remittance problem",
    "OTHER": "Other",
}

# Evidence that wins each reason, in PayPal's evidence_type names. "tracking" means the evidence
# goes in evidence_info.tracking_info rather than (only) as a document.
PLAYBOOK: dict[str, list[dict]] = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": [
        {"type": "PROOF_OF_FULFILLMENT", "what": "Carrier tracking showing delivery to the address on the "
         "transaction, plus the carrier's event history", "tracking": True, "doc": "delivery"},
        {"type": "OTHER", "what": "Shipping notice sent to the buyer with the tracking number", "doc": "messages"},
    ],
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": [
        {"type": "ITEM_DESCRIPTION", "what": "The listing as the buyer saw it, including any disclosure "
         "that matches the complaint", "doc": "listing"},
        {"type": "RETURN_POLICY", "what": "The published return policy", "doc": "policy"},
        {"type": "OTHER", "what": "The listing photo beside the photo the buyer sent", "doc": "photos"},
        {"type": "OTHER", "what": "Messages with the buyer about the issue", "doc": "messages"},
    ],
    "UNAUTHORISED": [
        {"type": "PROOF_OF_FULFILLMENT", "what": "Delivery to the address on the transaction, signed for",
         "tracking": True, "doc": "delivery"},
        {"type": "OTHER", "what": "Messages from the account holder about this order", "doc": "messages"},
    ],
    "CREDIT_NOT_PROCESSED": [
        {"type": "RETURN_POLICY", "what": "The return policy and whether the return arrived", "doc": "policy"},
        {"type": "OTHER", "what": "Messages about the return", "doc": "messages"},
    ],
}
DEFAULT_PLAY = [{"type": "OTHER", "what": "Order receipt and messages with the buyer", "doc": "receipt"}]

WIN_ODDS = {"strong": 0.85, "medium": 0.5, "weak": 0.1}
SIGNATURE_THRESHOLD_USD = 750.0   # PayPal Seller Protection needs signature confirmation at or above this
STALLED_DAYS = 15                 # no carrier scan for this long: treat the parcel as lost
RESALE_SHARE = 0.6                # a returned item can usually be resold for about this share of its price


@dataclass
class Settings:
    dispute_fee: float = 15.0            # the seller's PayPal dispute fee, charged when a case is lost
    return_shipping_cost: float = 38.0   # what a buyer pays to send a parcel back to the studio
    partial_refund_pct: int = 30         # share of the price offered when the buyer keeps the item
    autopilot_limit: float = 0.0         # act without approval up to this amount (0 = always ask)


@dataclass
class Option:
    action: str          # fight | partial_refund | refund_with_return | refund | message
    label: str
    expected_cost: float
    detail: str
    offer_amount: float | None = None


@dataclass
class Assessment:
    reason: str
    reason_label: str
    strength: str                     # strong | medium | weak
    findings: list[str]               # plain-English facts, each tied to a record
    citations: list[str]              # records the findings rest on, e.g. "shipment:EC-2041"
    play: list[dict]
    options: list[Option]
    recommended: str
    order_id: str | None = None
    warnings: list[str] = field(default_factory=list)


def hours_left(dispute: dict, now: datetime | None = None) -> float | None:
    due = dispute.get("seller_response_due_date")
    if not due:
        return None
    now = now or datetime.now(UTC)
    return (parse_time(due) - now).total_seconds() / 3600


def urgency(hours: float | None) -> str:
    if hours is None:
        return "none"
    if hours < 0:
        return "overdue"
    if hours < 24:
        return "critical"
    if hours < 72:
        return "soon"
    return "ok"


def parse_time(s: str) -> datetime:
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def assess(dispute: dict, shop, settings: Settings | None = None, now: datetime | None = None) -> Assessment:
    settings = settings or Settings()
    now = now or datetime.now(UTC)
    reason = dispute.get("reason", "OTHER")
    amount = float(dispute["dispute_amount"]["value"])
    txn = (dispute.get("disputed_transactions") or [{}])[0].get("seller_transaction_id", "")
    order = shop.order_for_transaction(txn) if txn else None
    findings, cites, warnings = [], [], []
    in_transit = False

    if not order:
        warnings.append(f"No order in the shop matches transaction {txn or '(none)'}.")
        return _finish(dispute, reason, "weak", findings, cites, warnings, None, amount, settings)

    oid = order["id"]
    cites.append(f"order:{oid}")
    findings.append(f"Order {oid}: {', '.join(i['title'] for i in order['items'])} for "
                    f"${order['total']}, shipping to {order['ship_to']}.")
    ship = shop.shipment(oid)
    msgs = shop.messages(oid)
    strength = "medium"

    if reason in ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "UNAUTHORISED"):
        if not ship:
            strength = "weak"
            findings.append("No shipment is recorded for this order.")
        else:
            cites.append(f"shipment:{oid}")
            last = ship["events"][-1] if ship["events"] else None
            if ship["status"] == "DELIVERED":
                sig = ship.get("signature")
                findings.append(f"{ship['carrier']} {ship['tracking_number']} shows delivered "
                                f"{_day(last['at']) if last else ''}" + (f", signed for by {sig}." if sig else
                                                                        ", no signature."))
                if sig or amount < SIGNATURE_THRESHOLD_USD:
                    strength = "strong"
                else:
                    strength = "medium"
                    warnings.append(f"Over ${SIGNATURE_THRESHOLD_USD:.0f} without a signature: "
                                    "Seller Protection needs signature confirmation.")
            elif ship.get("expected_delivery") and (
                    not last or (now - parse_time(last["at"])).days < STALLED_DAYS):
                in_transit = True
                seen = f"last scan \"{last['event']}\" in {last['where']} {_day(last['at'])}" if last \
                    else "label created, not yet picked up"
                findings.append(f"{ship['carrier']} {ship['tracking_number']} is on its way ({seen}); "
                                f"delivery expected {_day(ship['expected_delivery'])}.")
            else:
                idle = (now - parse_time(last["at"])).days if last else 999
                findings.append(f"{ship['carrier']} {ship['tracking_number']} last scanned {idle} days ago: "
                                f"\"{last['event'] if last else 'no events'}\".")
                strength = "weak" if idle >= STALLED_DAYS else "medium"
                if idle >= STALLED_DAYS:
                    findings.append(f"No scan for {STALLED_DAYS}+ days: the parcel is most likely lost.")
        if reason == "UNAUTHORISED":
            from_buyer = [m for m in msgs if m["sender"] == "buyer"]
            if from_buyer:
                cites += [f"message:{m['id']}" for m in from_buyer]
                findings.append(f"The account holder wrote to the shop about this order "
                                f"{len(from_buyer)} time(s), from {order['buyer_email']}.")
            elif strength == "strong":
                strength = "medium"

    elif reason == "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED":
        product = shop.product(order["items"][0]["sku"]) if order["items"] else None
        if product:
            cites.append(f"product:{product['sku']}")
            findings.append(f"Listing says: \"{product['description']}\"")
        policy = shop.policy("returns")
        if policy:
            cites.append("policy:returns")
        if ship and ship["status"] == "DELIVERED":
            cites.append(f"shipment:{oid}")
            findings.append(f"Delivered by {ship['carrier']} {_day(ship['events'][-1]['at'])}.")
        warnings.append("PayPal Seller Protection doesn't cover 'not as described' claims; PayPal usually "
                        "asks the buyer to return the item.")
        strength = "medium"

    for m in msgs:
        if m["sender"] == "buyer" and f"message:{m['id']}" not in cites:
            cites.append(f"message:{m['id']}")
    # Keep-it and send-it-back offers only make sense when the buyer agrees they have the item.
    has_item = reason == "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED" and bool(ship and ship["status"] == "DELIVERED")
    return _finish(dispute, reason, strength, findings, cites, warnings, oid, amount, settings, has_item,
                   in_transit)


def _finish(dispute, reason, strength, findings, cites, warnings, oid, amount, settings,
            has_item=False, in_transit=False) -> Assessment:
    offer_types = ((dispute.get("allowed_response_options") or {}).get("make_offer", {}).get("offer_types") or [])
    can_accept = bool((dispute.get("allowed_response_options") or {}).get("accept_claim"))
    p = WIN_ODDS[strength]
    options = [Option("fight", "Fight with evidence", round((1 - p) * (amount + settings.dispute_fee), 2),
                      f"About {int(p * 100)}% chance to win; a loss costs ${amount:.2f} plus the "
                      f"${settings.dispute_fee:.0f} fee.")]
    if "REFUND" in offer_types and has_item:
        partial = round(amount * settings.partial_refund_pct / 100, 2)
        options.append(Option("partial_refund", f"Offer {settings.partial_refund_pct}% back, buyer keeps it",
                              partial, f"Offer ${partial:.2f}; if the buyer accepts, the case closes.",
                              offer_amount=partial))
    if "REFUND_WITH_RETURN" in offer_types and has_item:
        options.append(Option("refund_with_return", "Full refund after the item comes back",
                              round(amount * (1 - RESALE_SHARE), 2),
                              f"The buyer pays about ${settings.return_shipping_cost:.0f} to post it back; "
                              "items often come back damaged or not at all.", offer_amount=amount))
    if can_accept:
        options.append(Option("refund", "Refund in full now", amount,
                              f"Closes the case today for ${amount:.2f}. No fee."))

    can_message = any(lk.get("rel") == "send_message" for lk in dispute.get("links", []))
    if in_transit and can_message:
        options.insert(0, Option("message", "Send the tracking and wait",
                                 round(0.1 * (amount + settings.dispute_fee), 2),
                                 "The parcel is on schedule. Most buyers close the case once they can see "
                                 "it moving; if it doesn't arrive, you can still refund."))
        return Assessment(reason, REASON_LABELS.get(reason, reason), strength, findings, cites,
                          PLAYBOOK.get(reason, DEFAULT_PLAY), options, "message", oid, warnings)
    if _reviewing(dispute):
        fight = options[0]
        fight.action, fight.label = "supporting_info", "Send supporting information"
        fight.detail = ("PayPal is already reviewing this claim. " + fight.detail +
                        " Refunds and offers aren't available at this stage.")
        return Assessment(reason, REASON_LABELS.get(reason, reason), strength, findings, cites,
                          PLAYBOOK.get(reason, DEFAULT_PLAY), [fight], "supporting_info", oid, warnings)
    if strength == "strong":
        recommended = "fight"
    elif strength == "weak":
        recommended = "refund" if can_accept else "fight"
    else:
        cheap = sorted((o for o in options if o.action != "fight"), key=lambda o: o.expected_cost)
        recommended = cheap[0].action if cheap and cheap[0].expected_cost < options[0].expected_cost else "fight"
        if recommended == "refund_with_return" and settings.return_shipping_cost >= amount * 0.5:
            alt = next((o for o in options if o.action == "partial_refund"), None)
            if alt:
                recommended = "partial_refund"
    return Assessment(reason, REASON_LABELS.get(reason, reason), strength, findings, cites,
                      PLAYBOOK.get(reason, DEFAULT_PLAY), options, recommended, oid, warnings)


def _reviewing(dispute: dict) -> bool:
    """A claim under PayPal review where the seller may only add supporting information."""
    rels = {lk.get("rel") for lk in dispute.get("links", [])}
    return "provide_supporting_info" in rels and "provide_evidence" not in rels


def _day(iso: str) -> str:
    return parse_time(iso).strftime("%-d %b")
