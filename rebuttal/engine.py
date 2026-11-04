"""Ties PayPal, the shop, the agent and the gate together: sync, investigate, approve, send."""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import gate
from . import plan as plans
from .agent import LLM, investigate
from .fake_paypal import FakePayPal
from .fake_paypal import seed_demo as seed_fake
from .paypal import LIVE, SANDBOX, PayPal, PayPalError
from .persist import Snapshotter, restore
from .playbook import assess
from .shop import Shop, seed_catalog
from .shop import seed_demo as seed_shop
from .state import CLOSED, FAILED, INVESTIGATING, READY, SENT, State

log = logging.getLogger("rebuttal")

ACTIVE = "REQUIRED_ACTION,REQUIRED_OTHER_PARTY_ACTION,UNDER_PAYPAL_REVIEW,RESOLVED"


class Engine:
    def __init__(self, pp, shop: Shop, state: State, llm: LLM | None, mode: str):
        self.pp, self.shop, self.state, self.llm, self.mode = pp, shop, state, llm, mode
        self.pool = ThreadPoolExecutor(max_workers=4)
        self._busy: set[str] = set()
        self.live: dict[str, list] = {}   # tool steps of investigations still running
        self._lock = threading.Lock()
        self.snapshots: Snapshotter | None = None
        self.generation = 0   # bumped by a demo reset; older investigations drop their results

    @classmethod
    def from_env(cls) -> Engine:
        from .cli import load_env
        load_env()
        mode = os.environ.get("PAYPAL_MODE", "mock")
        data = Path(os.environ.get("DATA_DIR", "data"))
        data.mkdir(parents=True, exist_ok=True)
        if mode == "mock":
            shop, state = Shop(), State()          # fresh every start: the demo always begins the same
            pp = FakePayPal()
            seed_shop(shop)
            seed_fake(pp)
        else:
            snap = os.environ.get("SNAPSHOT_DIR")
            if snap:
                restore(Path(snap), data, ["shop.db", "state.db"])
            shop, state = Shop(data / "shop.db"), State(data / "state.db")
            if shop.is_empty():
                seed_shop(shop)
            else:
                seed_catalog(shop)
            pp = PayPal(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"],
                        LIVE if mode == "live" else SANDBOX)
        engine = cls(pp, shop, state, LLM.from_env(), mode)
        if mode != "mock":
            engine.start_carrier()
        if mode != "mock" and os.environ.get("SNAPSHOT_DIR"):
            engine.snapshots = Snapshotter(Path(os.environ["SNAPSHOT_DIR"]),
                                           {"shop.db": shop.db, "state.db": state.db})
            engine.snapshots.start()
        return engine

    def carrier_tick(self) -> None:
        """Tell PayPal when a parcel on a real order has been delivered (its tracker said Shipped)."""
        for order, ship in self.shop.shipments_to_report():
            try:
                self.pp.update_tracker(order["transaction_id"], ship["tracking_number"],
                                       ship["carrier"].upper().replace(" ", "_"), "DELIVERED")
                self.shop.set_tracker_status(order["id"], "DELIVERED")
                log.info("tracker for %s marked delivered", order["id"])
            except PayPalError as e:
                log.warning("tracker update for %s failed: %s", order["id"], e)

    def start_carrier(self, every: float = 300) -> None:
        def loop():
            while True:
                try:
                    self.carrier_tick()
                except Exception:
                    log.exception("carrier tick")
                threading.Event().wait(every)
        threading.Thread(target=loop, daemon=True, name="carrier").start()

    def reset_demo(self) -> None:
        """Demo mode: put Ember & Clay's four disputes back as they were."""
        if self.mode != "mock":
            raise ValueError("Only the demo can be reset.")
        shop, state, pp = Shop(), State(), FakePayPal()
        seed_shop(shop)
        seed_fake(pp)
        with self._lock:
            self.generation += 1
            self.shop, self.state, self.pp = shop, state, pp
            self.live, self._busy = {}, set()
        self.sync()

    # --- sync and investigate -------------------------------------------------------------

    def sync(self, wait: bool = False) -> None:
        """Pull disputes from PayPal and investigate any that need a response."""
        for summary in self.pp.list_disputes(ACTIVE):
            self.refresh(self.pp.get_dispute(summary["dispute_id"]), wait=wait)

    def refresh(self, dispute: dict, wait: bool = False) -> None:
        did = dispute["dispute_id"]
        is_new = self.state.upsert_dispute(dispute)
        case = self.state.get(did)
        if dispute["status"] == "RESOLVED":
            if case["status"] != CLOSED:
                outcome = (dispute.get("dispute_outcome") or {}).get("outcome_code", "RESOLVED")
                self.state.set_status(did, CLOSED, f"PayPal closed the case: {_outcome(outcome)}.")
            return
        rels = {lk.get("rel") for lk in dispute.get("links", [])}
        waiting = dispute["status"] == "WAITING_FOR_SELLER_RESPONSE" or (
            "provide_supporting_info" in rels and "provide_evidence" not in rels and case["status"] != SENT)
        plan = case["plan"] or {}
        if case["status"] == SENT and plan.get("action") == "message":
            # We sent the tracking and are waiting on the parcel: look again once it moves on.
            if self._parcel_status(dispute) != plan.get("parcel_status"):
                self.state.set_status(did, INVESTIGATING, "The parcel's status changed.")
                self.investigate(did, wait=wait)
            return
        # New, never planned, or PayPal came back for more after we answered.
        if waiting and (is_new or not case["plan"] or case["status"] in (SENT, FAILED)):
            if case["status"] == SENT:
                self.state.set_status(did, INVESTIGATING, "PayPal asked for a new response.")
            self.investigate(did, wait=wait)

    def _parcel_status(self, dispute: dict) -> str | None:
        txn = ((dispute.get("disputed_transactions") or [{}])[0]).get("seller_transaction_id")
        order = self.shop.order_for_transaction(txn) if txn else None
        ship = self.shop.shipment(order["id"]) if order else None
        return ship["status"] if ship else None

    def investigate(self, dispute_id: str, wait: bool = False):
        with self._lock:
            if dispute_id in self._busy:
                return
            self._busy.add(dispute_id)
        fut = self.pool.submit(self._investigate, dispute_id, self.generation)
        if wait:
            fut.result()

    def _investigate(self, dispute_id: str, generation: int = 0):
        state, shop, live = self.state, self.shop, self.live
        try:
            case = state.get(dispute_id)
            dispute = case["dispute"]
            settings = state.settings()
            a = assess(dispute, shop, settings)
            live[dispute_id] = []
            plan = investigate(dispute, shop, a, self.llm, trace=live[dispute_id])
            plan["parcel_status"] = self._parcel_status(dispute)
            verdict = gate.run(plan, dispute, shop, a, settings)
            if generation != self.generation:
                return   # the demo was reset while this ran; its case no longer exists
            state.set_plan(dispute_id, plan, verdict.checks, verdict.autopilot, READY)
            state.set_status(dispute_id, READY, f"Investigated: recommends {_label(plan['action'])}"
                                  f" ({plan['author']}).")
            if verdict.autopilot and verdict.passed:
                self.send(dispute_id, approved_by="autopilot")
        except Exception as e:
            log.exception("investigate %s", dispute_id)
            if generation == self.generation:
                state.set_status(dispute_id, FAILED, f"Investigation failed: {e}")
        finally:
            with self._lock:
                if generation == self.generation:
                    self._busy.discard(dispute_id)
                live.pop(dispute_id, None)

    # --- seller actions -------------------------------------------------------------------

    def context(self, dispute_id: str):
        case = self.state.get(dispute_id)
        settings = self.state.settings()
        a = assess(case["dispute"], self.shop, settings)
        return case, a, settings

    def replan(self, dispute_id: str, action: str) -> None:
        """The seller picked a different action: redraft it from templates and re-run the gate."""
        case, a, settings = self.context(dispute_id)
        plan = plans.rules_plan(case["dispute"], self.shop, a, action=action)
        plan["trace"] = (case["plan"] or {}).get("trace", []) + [
            {"tool": "seller", "note": f"Seller switched the action to {_label(action)}."}]
        verdict = gate.run(plan, case["dispute"], self.shop, a, settings)
        self.state.set_plan(dispute_id, plan, verdict.checks, False, READY)

    def edit(self, dispute_id: str, notes: list[str], buyer_message: str, offer_amount: float | None) -> None:
        case, a, settings = self.context(dispute_id)
        plan = case["plan"]
        for ev, text in zip(plan.get("evidence", []), notes, strict=False):
            ev["notes"] = text
        plan["buyer_message"] = buyer_message
        if offer_amount is not None:
            plan["offer_amount"] = offer_amount
        verdict = gate.run(plan, case["dispute"], self.shop, a, settings)
        self.state.set_plan(dispute_id, plan, verdict.checks, False, READY)

    def documents(self, dispute_id: str) -> dict[str, bytes]:
        case, a, _ = self.context(dispute_id)
        return plans.documents(case["plan"] or {}, self.shop, dispute_id, a)

    def send(self, dispute_id: str, approved_by: str = "seller") -> list[str]:
        """Re-check against PayPal's latest copy of the dispute, then send."""
        dispute = self.pp.get_dispute(dispute_id)
        self.state.upsert_dispute(dispute)
        case, a, settings = self.context(dispute_id)
        verdict = gate.run(case["plan"], dispute, self.shop, a, settings)
        if not verdict.passed:
            self.state.set_plan(dispute_id, case["plan"], verdict.checks, False, READY)
            failed = [c.name for c in verdict.checks if c.blocking and not c.ok]
            raise ValueError("Blocked by checks: " + ", ".join(failed))
        try:
            lines = plans.execute(self.pp, case["plan"], plans.documents(case["plan"], self.shop, dispute_id, a))
        except PayPalError as e:
            self.state.set_status(dispute_id, READY, f"PayPal refused: {e}")
            raise
        for line in lines:
            self.state.set_status(dispute_id, SENT, f"{line} Approved by {approved_by}.")
        self.refresh(self.pp.get_dispute(dispute_id))
        return lines

    # --- demo controls (sandbox-only PayPal calls, or the fake) -----------------------------

    def simulate_decision(self, dispute_id: str, seller_wins: bool) -> None:
        self.pp.adjudicate(dispute_id, "SELLER_FAVOR" if seller_wins else "BUYER_FAVOR")
        self.refresh(self.pp.get_dispute(dispute_id))

    def simulate_buyer_accepts(self, dispute_id: str) -> None:
        if isinstance(self.pp, FakePayPal):
            self.pp.buyer_accepts_offer(dispute_id)
            self.refresh(self.pp.get_dispute(dispute_id))


def _label(action: str) -> str:
    return {"fight": "fight with evidence", "supporting_info": "send supporting information",
            "partial_refund": "partial refund", "refund": "refund in full",
            "refund_with_return": "refund after return", "message": "message the buyer"}.get(action, action)


def _outcome(code: str) -> str:
    return {"RESOLVED_SELLER_FAVOUR": "won by the seller", "RESOLVED_BUYER_FAVOUR": "refunded to the buyer",
            "RESOLVED_WITH_PAYOUT": "settled with the seller's offer"}.get(code, code.lower().replace("_", " "))
