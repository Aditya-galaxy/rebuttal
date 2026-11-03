"""The checks every plan must pass before anything reaches PayPal.

The model drafts; the gate decides whether the draft may be sent. All checks are deterministic,
run on PayPal's live dispute object and the seller's records, and are shown to the seller.
"""

from __future__ import annotations

from dataclasses import dataclass

from .paypal import link
from .plan import ACTION_LINK, OFFER_TYPE
from .playbook import Assessment, Settings, hours_left


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


@dataclass
class Verdict:
    checks: list[Check]
    autopilot: bool       # the seller's policy lets this go without a click

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks if c.blocking)


def run(plan: dict, dispute: dict, shop, a: Assessment, settings: Settings) -> Verdict:
    checks: list[Check] = []
    action = plan.get("action")
    amount = float(dispute["dispute_amount"]["value"])

    h = hours_left(dispute)
    checks.append(Check("Deadline still open", h is None or h > 0,
                        "PayPal hasn't set a seller deadline; it's reviewing now." if h is None else
                        f"{h:.0f} hours left." if h > 0 else f"Deadline passed {-h:.0f} hours ago."))

    rel = ACTION_LINK.get(action)
    allowed = bool(rel and link(dispute, rel))
    detail = f"PayPal currently allows {rel.replace('_', ' ')}." if allowed else \
        f"PayPal doesn't allow '{action}' on this dispute right now."
    if allowed and action in OFFER_TYPE:
        types = (dispute.get("allowed_response_options") or {}).get("make_offer", {}).get("offer_types", [])
        allowed = OFFER_TYPE[action] in types
        if not allowed:
            detail = f"{OFFER_TYPE[action]} isn't among the offers PayPal allows ({', '.join(types) or 'none'})."
    checks.append(Check("Action allowed by PayPal", allowed, detail))

    if action in ("fight", "supporting_info"):
        requested = {e["evidence_type"] for e in (dispute.get("evidences") or [])
                     if e.get("source") == "REQUESTED_FROM_SELLER"}
        given = {e["evidence_type"] for e in plan.get("evidence", [])}
        missing = requested - given if action == "fight" else set()
        checks.append(Check("Covers every document PayPal asked for", not missing,
                            "PayPal asked for: " + (", ".join(sorted(requested)) or "nothing specific")
                            + (f". Missing: {', '.join(sorted(missing))}." if missing else ". All included.")))
        checks.append(Check("Has evidence", bool(plan.get("evidence")),
                            f"{len(plan.get('evidence', []))} evidence items."))

        ship = shop.shipment(a.order_id) if a.order_id else None
        bad = [e["tracking"]["tracking_number"] for e in plan.get("evidence", []) if e.get("tracking")
               and (not ship or e["tracking"]["tracking_number"] != ship["tracking_number"])]
        checks.append(Check("Tracking numbers match the shipment", not bad,
                            f"Not in the shop's records: {', '.join(bad)}." if bad else
                            "Every tracking number matches the recorded shipment."))
        long_notes = [e["evidence_type"] for e in plan.get("evidence", []) if len(e.get("notes", "")) > 2000]
        joined = sum(len(e.get("notes", "")) + 2 for e in plan.get("evidence", []))
        if action == "supporting_info" and joined > 2000:
            long_notes.append(f"the combined statement ({joined} characters)")
        checks.append(Check("Notes within PayPal's 2,000 characters", not long_notes,
                            f"Too long: {', '.join(long_notes)}." if long_notes else "All notes fit."))
        checks.append(Check("Case is winnable", a.strength != "weak",
                            f"Evidence strength is {a.strength}." + (
                                " Fighting would likely lose the money and the fee." if a.strength == "weak"
                                else "")))

    cites = [c for cl in plan.get("claims", []) for c in cl.get("cites", [])]
    cites += [c for e in plan.get("evidence", []) for c in e.get("cites", [])]
    fake = sorted({c for c in cites if not shop.record_exists(c)})
    uncited = [cl["text"][:60] for cl in plan.get("claims", []) if not cl.get("cites")]
    checks.append(Check("Every fact cites a real record", not fake and not uncited,
                        (f"Unknown records: {', '.join(fake)}. " if fake else "")
                        + (f"Uncited: {'; '.join(uncited)}." if uncited else "")
                        or f"{len(set(cites))} records cited, all found."))

    if action in OFFER_TYPE:
        offer = plan.get("offer_amount")
        ok = offer is not None and 0 < offer <= amount
        checks.append(Check("Offer amount is sensible", ok,
                            f"${offer} of ${amount:.2f} disputed." if offer is not None else "No amount set."))

    msg = plan.get("buyer_message", "")
    checks.append(Check("Buyer message length", len(msg) <= 2000, f"{len(msg)} characters."))

    if action != a.recommended:
        checks.append(Check("Matches the numbers", False,
                            f"The plan says '{action}', the cost comparison says '{a.recommended}'.",
                            blocking=False))

    auto = settings.autopilot_limit > 0 and amount <= settings.autopilot_limit and action == a.recommended
    return Verdict(checks, auto)
