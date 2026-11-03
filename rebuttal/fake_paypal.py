"""An in-memory PayPal with the same methods as paypal.PayPal, for tests and the no-account demo.

It returns dispute JSON in PayPal's own shape and enforces the rules the real API enforces: an
action needs its HATEOAS link, an offer type must be in allowed_response_options, and each action
moves the dispute to the status the real API moves it to.
"""

from __future__ import annotations

import copy
import itertools
from datetime import UTC, datetime, timedelta

from .paypal import PayPalError, link

SELLER = {"merchant_id": "EMBERCLAY7Q2LM", "name": "Ember & Clay"}

ACTION_LINKS = ("provide_evidence", "accept_claim", "make_offer", "send_message")


class FakePayPal:
    is_sandbox = True

    def __init__(self):
        self.disputes: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []   # (method, dispute_id, payload) for assertions
        self.uploads: dict[str, list[tuple[str, bytes]]] = {}
        self._ids = itertools.count(1001)

    # --- the PayPal interface ---------------------------------------------------------------

    def list_disputes(self, states: str = "") -> list[dict]:
        wanted = set(filter(None, states.split(",")))
        return [_summary(d) for d in self.disputes.values() if not wanted or d["dispute_state"] in wanted]

    def get_dispute(self, dispute_id: str) -> dict:
        if dispute_id not in self.disputes:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND", "message": dispute_id}, dispute_id)
        return copy.deepcopy(self.disputes[dispute_id])

    def provide_evidence(self, dispute_id, evidences, files=()):
        d = self._need(dispute_id, "provide_evidence")
        names = {name for name, _, _ in files}
        for ev in evidences:
            for doc in ev.get("documents", []):
                if doc["name"] not in names:
                    raise PayPalError(400, {"name": "INVALID_REQUEST",
                                            "message": f"document {doc['name']} has no file part"}, dispute_id)
            if len(ev.get("notes", "")) > 2000:
                raise PayPalError(400, {"name": "INVALID_REQUEST", "message": "notes over 2000"}, dispute_id)
        self.calls.append(("provide_evidence", dispute_id, {"evidences": evidences}))
        self.uploads[dispute_id] = [(name, data) for name, data, _ in files]
        d["evidences"] = [e for e in (d.get("evidences") or []) if e.get("source") != "REQUESTED_FROM_SELLER"]
        d["evidences"] += [{**e, "source": "SUBMITTED_BY_SELLER", "date": _now()} for e in evidences]
        self._move(d, "UNDER_REVIEW", "UNDER_PAYPAL_REVIEW", ["adjudicate"])
        return {"links": [{"rel": "self", "href": f"/v1/customer/disputes/{dispute_id}"}]}

    def provide_supporting_info(self, dispute_id, notes, files=()):
        d = self._need(dispute_id, "provide_supporting_info")
        if len(notes) > 2000:
            raise PayPalError(400, {"name": "INVALID_REQUEST", "message": "notes over 2000"}, dispute_id)
        self.calls.append(("provide_supporting_info", dispute_id, {"notes": notes}))
        self.uploads[dispute_id] = [(name, data) for name, data, _ in files]
        d.setdefault("supporting_info", []).append(
            {"notes": notes, "source": "SUBMITTED_BY_SELLER", "provided_time": _now(),
             "documents": [{"name": name} for name, _, _ in files]})
        self._move(d, "UNDER_REVIEW", "UNDER_PAYPAL_REVIEW", ["adjudicate"])
        return {}

    def make_offer(self, dispute_id, offer_type, amount, note):
        d = self._need(dispute_id, "make_offer")
        allowed = (d.get("allowed_response_options") or {}).get("make_offer", {}).get("offer_types", [])
        if offer_type not in allowed:
            raise PayPalError(422, {"name": "UNPROCESSABLE_ENTITY",
                                    "message": f"offer type {offer_type} not allowed"}, dispute_id)
        if amount and float(amount["value"]) > float(d["dispute_amount"]["value"]):
            raise PayPalError(422, {"name": "UNPROCESSABLE_ENTITY",
                                    "message": "offer exceeds dispute amount"}, dispute_id)
        self.calls.append(("make_offer", dispute_id, {"offer_type": offer_type, "amount": amount, "note": note}))
        d["offer"] = {"seller_offered_amount": amount, "offer_type": offer_type,
                      "history": [{"offer_time": _now(), "actor": "SELLER", "event_type": "PROPOSED",
                                   "offer_type": offer_type}]}
        d.setdefault("messages", []).append({"posted_by": "SELLER", "time_posted": _now(), "content": note})
        self._move(d, "WAITING_FOR_BUYER_RESPONSE", "REQUIRED_OTHER_PARTY_ACTION", ["send_message"])
        return {}

    def accept_claim(self, dispute_id, note, claim_type="REFUND"):
        d = self._need(dispute_id, "accept_claim")
        self.calls.append(("accept_claim", dispute_id, {"note": note, "claim_type": claim_type}))
        d["dispute_outcome"] = {"outcome_code": "RESOLVED_BUYER_FAVOUR",
                                "amount_refunded": d["dispute_amount"]}
        self._move(d, "RESOLVED", "RESOLVED", [])
        return {}

    def send_message(self, dispute_id, message):
        d = self._need(dispute_id, "send_message")
        self.calls.append(("send_message", dispute_id, {"message": message}))
        d.setdefault("messages", []).append({"posted_by": "SELLER", "time_posted": _now(), "content": message})
        return {}

    def require_evidence(self, dispute_id, action="SELLER_EVIDENCE"):
        d = self.disputes[dispute_id]
        if action == "SELLER_EVIDENCE":
            self._move(d, "WAITING_FOR_SELLER_RESPONSE", "REQUIRED_ACTION", list(ACTION_LINKS))
        else:
            self._move(d, "WAITING_FOR_BUYER_RESPONSE", "REQUIRED_OTHER_PARTY_ACTION", [])
        return {}

    def adjudicate(self, dispute_id, outcome):
        d = self.disputes[dispute_id]
        if not link(d, "adjudicate"):
            raise PayPalError(422, {"name": "UNPROCESSABLE_ENTITY", "message": "not under review"}, dispute_id)
        won = outcome == "SELLER_FAVOR"
        d["dispute_outcome"] = {"outcome_code": "RESOLVED_SELLER_FAVOUR" if won else "RESOLVED_BUYER_FAVOUR",
                                **({} if won else {"amount_refunded": d["dispute_amount"]})}
        self._move(d, "RESOLVED", "RESOLVED", [])
        return {}

    def buyer_accepts_offer(self, dispute_id):
        """Demo helper: what the buyer clicking 'accept' in PayPal does."""
        d = self.disputes[dispute_id]
        amount = (d.get("offer") or {}).get("seller_offered_amount") or d["dispute_amount"]
        d["dispute_outcome"] = {"outcome_code": "RESOLVED_WITH_PAYOUT", "amount_refunded": amount}
        self._move(d, "RESOLVED", "RESOLVED", [])

    # --- building disputes ------------------------------------------------------------------

    def open_dispute(self, *, transaction_id: str, buyer_name: str, amount: str, reason: str,
                     stage: str = "INQUIRY", message: str = "", items: list[str] = (),
                     due_in: timedelta = timedelta(days=10), opened_ago: timedelta = timedelta(days=1),
                     requested: list[str] = (), offer_types: list[str] | None = None,
                     currency: str = "USD", dispute_id: str | None = None,
                     under_review: bool = False) -> dict:
        now = datetime.now(UTC)
        dispute_id = dispute_id or f"PP-D-{next(self._ids)}"
        if offer_types is None:
            offer_types = [] if reason == "UNAUTHORISED" else ["REFUND", "REFUND_WITH_RETURN"]
        money = {"currency_code": currency, "value": amount}
        d = {
            "dispute_id": dispute_id,
            "create_time": _iso(now - opened_ago),
            "update_time": _iso(now - opened_ago),
            "disputed_transactions": [{
                "seller_transaction_id": transaction_id,
                "buyer_transaction_id": "B" + transaction_id[1:],
                "create_time": _iso(now - opened_ago - timedelta(days=14)),
                "transaction_status": "COMPLETED",
                "gross_amount": money,
                "buyer": {"name": buyer_name},
                "seller": SELLER,
                "items": [{"item_name": name, "item_quantity": "1"} for name in items],
            }],
            "reason": reason,
            "status": "WAITING_FOR_SELLER_RESPONSE",
            "dispute_state": "REQUIRED_ACTION",
            "dispute_amount": money,
            "dispute_life_cycle_stage": stage,
            "dispute_channel": "EXTERNAL" if reason == "UNAUTHORISED" else "INTERNAL",
            "messages": [{"posted_by": "BUYER", "time_posted": _iso(now - opened_ago), "content": message}]
            if message else [],
            "evidences": [{"evidence_type": t, "source": "REQUESTED_FROM_SELLER"} for t in requested],
            "seller_response_due_date": _iso(now + due_in),
            "allowed_response_options": {
                "accept_claim": {"accept_claim_types": ["REFUND"]},
                **({"make_offer": {"offer_types": offer_types}} if offer_types else {}),
            },
            "links": [],
        }
        self.disputes[dispute_id] = d
        if under_review:   # what the sandbox does when a buyer files straight to a claim
            d.update(seller_response_due_date=None, allowed_response_options=None)
            self._move(d, "UNDER_REVIEW", "UNDER_PAYPAL_REVIEW", ["provide_supporting_info"])
        else:
            self._move(d, d["status"], d["dispute_state"],
                       [r for r in ACTION_LINKS if r != "make_offer" or offer_types])
        return copy.deepcopy(d)

    def _need(self, dispute_id: str, rel: str) -> dict:
        d = self.disputes.get(dispute_id)
        if d is None:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND", "message": dispute_id}, dispute_id)
        if not link(d, rel):
            raise PayPalError(422, {"name": "UNPROCESSABLE_ENTITY",
                                    "message": f"{rel} not allowed while {d['status']}"}, dispute_id)
        return d

    @staticmethod
    def _move(d: dict, status: str, state: str, rels: list[str]) -> None:
        d["status"], d["dispute_state"], d["update_time"] = status, state, _now()
        base = f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{d['dispute_id']}"
        d["links"] = [{"rel": "self", "href": base, "method": "GET"}] + [
            {"rel": r, "href": f"{base}/{r.replace('_', '-')}", "method": "POST"} for r in rels]


def seed_demo(pp: FakePayPal) -> None:
    """The four Ember & Clay disputes, matching shop.seed_demo's orders."""
    pp.open_dispute(dispute_id="PP-D-1001", transaction_id="7KD04291VX3301845", buyer_name="Jordan Miller",
                    amount="148.00", reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED", items=["Indigo Moon Vase"],
                    message="I never received my vase. Please refund me.",
                    due_in=timedelta(days=6, hours=4), opened_ago=timedelta(days=4),
                    requested=["PROOF_OF_FULFILLMENT"])
    pp.open_dispute(dispute_id="PP-D-1002", transaction_id="2MW71520LQ8846217", buyer_name="Priya Raman",
                    amount="62.00", reason="MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
                    items=["Chai Cups, set of 4 (Rust glaze)"],
                    message="The cups are a different colour from the photos. Brown, not rust.",
                    due_in=timedelta(days=2, hours=19), opened_ago=timedelta(days=7))
    pp.open_dispute(dispute_id="PP-D-1003", transaction_id="5RT38812GH1190274", buyer_name="Sam Okafor",
                    amount="35.00", reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                    items=["Block-print coasters, set of 6"],
                    message="Tracking hasn't updated in weeks. I want my money back.",
                    due_in=timedelta(days=9, hours=2), opened_ago=timedelta(days=1),
                    requested=["PROOF_OF_FULFILLMENT"])
    pp.open_dispute(dispute_id="PP-D-1004", transaction_id="9HB55107ZC2238461", buyer_name="Alex Chen",
                    amount="210.00", reason="UNAUTHORISED", stage="CHARGEBACK",
                    items=["Terracotta planter pair"],
                    due_in=timedelta(days=1, hours=7), opened_ago=timedelta(days=8),
                    requested=["PROOF_OF_FULFILLMENT", "OTHER"])


def _summary(d: dict) -> dict:
    keys = ("dispute_id", "create_time", "update_time", "reason", "status", "dispute_state",
            "dispute_amount", "dispute_life_cycle_stage", "dispute_channel", "seller_response_due_date", "links")
    return {k: copy.deepcopy(d[k]) for k in keys if k in d}


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _now() -> str:
    return _iso(datetime.now(UTC))
