"""The data behind the Insights page: every dispute with its outcome, and the monthly dispute rate.

AG Studio (in the browser) builds the report and the AI assistant's answers from these tables.
Live cases come from Rebuttal's own state. In demo mode, Ember & Clay also gets six months of
generated history so the report has something to show; the page labels it as generated, and
sandbox mode never includes it.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import UTC, date, datetime, timedelta

from .playbook import REASON_LABELS, Settings, assess, parse_time

ACTION_LABELS = {"fight": "Fight", "supporting_info": "Supporting info", "partial_refund": "Partial refund",
                 "refund_with_return": "Refund after return", "refund": "Refund", "message": "Send tracking"}
OUTCOMES = {"RESOLVED_SELLER_FAVOUR": "Won", "RESOLVED_BUYER_FAVOUR": "Refunded",
            "RESOLVED_WITH_PAYOUT": "Settled"}


def build(engine, demo_history: bool) -> dict:
    settings = engine.state.settings()
    live = [_live_row(c, engine.shop, settings) for c in engine.state.all()]
    disputes = live + (_history(datetime.now(UTC)) if demo_history else [])
    orders_by_month = Counter()
    for o in engine.shop.db.execute("select created_at from orders"):
        orders_by_month[o["created_at"][:7]] += 1
    if demo_history:
        orders_by_month.update(_demo_orders(datetime.now(UTC)))
    disputes_by_month = Counter(d["opened"][:7] for d in disputes)
    months = sorted(set(orders_by_month) | set(disputes_by_month))
    monthly = [{"month": f"{m}-01", "orders": orders_by_month[m], "disputes": disputes_by_month[m]}
               for m in months]
    return {"asOf": datetime.now(UTC).isoformat(timespec="seconds"), "generatedHistory": demo_history,
            "disputes": disputes, "monthly": monthly}


def _live_row(case: dict, shop, settings: Settings) -> dict:
    d = case["dispute"]
    a = assess(d, shop, settings)
    plan = case.get("plan") or {}
    amount = float(d["dispute_amount"]["value"])
    code = (d.get("dispute_outcome") or {}).get("outcome_code", "")
    outcome = OUTCOMES.get(code, "Open")
    refunded = float(((d.get("dispute_outcome") or {}).get("amount_refunded") or {}).get("value") or 0)
    if outcome == "Refunded" and not refunded:
        refunded = amount
    sent = next((entry["at"] for entry in case.get("log", []) if entry["text"].startswith(("Sent", "Offered",
                 "Accepted", "Messaged"))), None)
    opened = d.get("create_time") or case.get("updated_at")
    return _row(
        id=case["dispute_id"], opened=opened, closed=d.get("update_time") if outcome != "Open" else None,
        reason=REASON_LABELS.get(d.get("reason"), d.get("reason") or "Other"), amount=amount,
        strength=a.strength, recommended=ACTION_LABELS.get(a.recommended, a.recommended),
        chosen=ACTION_LABELS.get(plan.get("action"), "") if case["status"] in ("sent", "closed") else "",
        outcome=outcome, refunded=refunded, fee=settings.dispute_fee if outcome == "Refunded" else 0.0,
        responded=sent, due=d.get("seller_response_due_date"), handled_by="Rebuttal",
        stage=(d.get("dispute_life_cycle_stage") or "").title(), source="Live")


def _row(*, id, opened, closed, reason, amount, strength, recommended, chosen, outcome, refunded, fee,
         responded, due, handled_by, stage, source) -> dict:
    """One dispute, flat and numeric, so Studio can sum and divide it."""
    resp_hours = None
    if responded and opened:
        resp_hours = round((parse_time(responded) - parse_time(opened)).total_seconds() / 3600, 1)
    on_time = None
    if responded and due:
        on_time = parse_time(responded) <= parse_time(due)
    closed_flag = outcome != "Open"
    return {
        "id": id, "opened": opened[:10], "closed": closed[:10] if closed else None, "reason": reason,
        "stage": stage, "amount": round(amount, 2), "strength": strength.title(), "recommended": recommended,
        "chosen": chosen or "Not yet", "followedAdvice": bool(chosen) and chosen == recommended,
        "outcome": outcome, "isClosed": int(closed_flag), "isWon": int(outcome == "Won"),
        "recovered": round(amount - refunded, 2) if closed_flag else 0.0,
        "lost": round(refunded + fee, 2), "fee": round(fee, 2), "responseHours": resp_hours,
        "onTime": int(bool(on_time)) if on_time is not None else None,
        "answered": int(responded is not None), "handledBy": handled_by, "source": source, "count": 1,
    }


# --- generated demo history ------------------------------------------------------------------

def _history(now: datetime) -> list[dict]:
    """Six months of closed disputes for the demo studio, seeded so every visit looks the same.

    The last two months are handled by Rebuttal; before that the owner answered by hand, often
    late. The numbers are illustrative, not measured.
    """
    rng = random.Random(2041)
    reasons = [("Item not received", 0.48), ("Not as described", 0.30), ("Unauthorised payment", 0.14),
               ("Refund not processed", 0.08)]
    prices = [35.0, 48.0, 62.0, 75.0, 96.0, 120.0, 148.0, 185.0, 210.0, 260.0]
    rows = []
    start = (now - timedelta(days=182)).date()
    for i in range(64):
        opened = start + timedelta(days=min(168, i * 168 // 64 + rng.randint(0, 2)))   # spread evenly
        rebuttal = (now.date() - opened).days <= 61
        reason = _pick(rng, reasons)
        amount = rng.choice(prices)
        strength = _pick(rng, [("Strong", 0.45), ("Medium", 0.35), ("Weak", 0.20)])
        recommended = {"Strong": "Fight", "Weak": "Refund"}.get(
            strength, "Partial refund" if reason == "Not as described" else "Fight")
        due = opened + timedelta(days=10 if reason != "Unauthorised payment" else 7)
        if rebuttal:
            chosen = recommended if rng.random() < 0.9 else "Fight"
            answer_day = opened + timedelta(days=rng.randint(0, 2))
        else:
            chosen = "Fight" if rng.random() < 0.7 else "Refund"
            late = rng.random() < 0.35
            answer_day = due + timedelta(days=rng.randint(1, 4)) if late else opened + timedelta(
                days=rng.randint(3, 9))
        outcome, refunded = _outcome(rng, chosen, strength, amount, answer_day <= due)
        closed = answer_day + timedelta(days=rng.randint(4, 20))
        if closed > now.date():
            closed = now.date() - timedelta(days=1)
        fee = 15.0 if outcome == "Refunded" and chosen in ("Fight", "Supporting info") else 0.0
        rows.append(_row(
            id=f"PP-D-{8100 + i}", opened=_iso(opened), closed=_iso(closed), reason=reason, amount=amount,
            strength=strength, recommended=recommended, chosen=chosen, outcome=outcome, refunded=refunded,
            fee=fee, responded=_iso(answer_day), due=_iso(due), handled_by="Rebuttal" if rebuttal else "Manual",
            stage="Inquiry", source="Generated demo history"))
    return rows


def _outcome(rng, chosen, strength, amount, on_time) -> tuple[str, float]:
    if chosen == "Refund":
        return "Refunded", amount
    if chosen == "Partial refund":
        return ("Settled", round(amount * 0.3, 2)) if rng.random() < 0.8 else ("Refunded", amount)
    if not on_time:
        return "Refunded", amount           # a missed deadline loses the case
    odds = {"Strong": 0.85, "Medium": 0.5, "Weak": 0.1}[strength]
    return ("Won", 0.0) if rng.random() < odds else ("Refunded", amount)


def _demo_orders(now: datetime) -> Counter:
    rng = random.Random(7)
    out = Counter()
    for back in range(6, -1, -1):
        month = (now.replace(day=1) - timedelta(days=30 * back)).strftime("%Y-%m")
        orders = rng.randint(520, 680)
        out[month] += orders * now.day // 30 if back == 0 else orders   # this month so far
    return out


def _pick(rng, weighted):
    r, acc = rng.random(), 0.0
    for value, w in weighted:
        acc += w
        if r <= acc:
            return value
    return weighted[-1][0]


def _iso(d: date) -> str:
    return datetime(d.year, d.month, d.day, 12, tzinfo=UTC).isoformat()
