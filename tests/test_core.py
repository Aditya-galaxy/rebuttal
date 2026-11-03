"""The decision engine, the gate and the PayPal calls, on the Ember & Clay demo data."""

import json

import pytest

from rebuttal import gate
from rebuttal import plan as plans
from rebuttal.agent import investigate
from rebuttal.fake_paypal import FakePayPal
from rebuttal.fake_paypal import seed_demo as seed_pp
from rebuttal.paypal import PayPalError
from rebuttal.playbook import Settings, assess
from rebuttal.shop import Shop
from rebuttal.shop import seed_demo as seed_shop


@pytest.fixture
def world():
    shop, pp = Shop(), FakePayPal()
    seed_shop(shop)
    seed_pp(pp)
    return shop, pp


def case(world, did):
    shop, pp = world
    d = pp.get_dispute(did)
    return d, assess(d, shop)


@pytest.mark.parametrize("did,strength,action", [
    ("PP-D-1001", "strong", "fight"),           # signed DHL delivery
    ("PP-D-1002", "medium", "partial_refund"),  # disclosed glaze variation; returning costs more than it's worth
    ("PP-D-1003", "weak", "refund"),            # India Post stalled 25 days
    ("PP-D-1004", "strong", "fight"),           # signed delivery + the "stranger" emailed about gift wrap
])
def test_assessment_picks_the_cheapest_good_outcome(world, did, strength, action):
    _, a = case(world, did)
    assert (a.strength, a.recommended) == (strength, action)


def test_not_received_cases_never_offer_keep_it_options(world):
    _, a = case(world, "PP-D-1001")
    assert {o.action for o in a.options} == {"fight", "refund"}


def test_signature_threshold_downgrades_big_unsigned_deliveries(world):
    shop, pp = world
    shop.add_order("EC-3000", "TXNBIG", "Big Buyer", "b@example.com", "Somewhere, US",
                   [{"sku": "VASE-INDIGO", "title": "Vase", "qty": 6, "price": "148.00"}], "888.00")
    shop.add_shipment("EC-3000", "FedEx", "1", "Priority", "DELIVERED", None,
                      [{"at": "2026-10-01T00:00:00+00:00", "where": "X", "event": "Delivered"}])
    pp.open_dispute(dispute_id="PP-D-9", transaction_id="TXNBIG", buyer_name="Big Buyer", amount="888.00",
                    reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED")
    a = assess(pp.get_dispute("PP-D-9"), shop)
    assert a.strength == "medium"
    assert any("signature" in w for w in a.warnings)


@pytest.mark.parametrize("did", ["PP-D-1001", "PP-D-1002", "PP-D-1003", "PP-D-1004"])
def test_rules_plans_pass_the_gate_and_send(world, did):
    shop, pp = world
    d, a = case(world, did)
    p = plans.rules_plan(d, shop, a)
    v = gate.run(p, d, shop, a, Settings())
    assert v.passed, [c for c in v.checks if not c.ok]
    log = plans.execute(pp, p, plans.documents(p, shop, did, a))
    assert log
    assert pp.get_dispute(did)["status"] in ("UNDER_REVIEW", "WAITING_FOR_BUYER_RESPONSE", "RESOLVED")


def test_fight_sends_tracking_and_pdfs(world):
    shop, pp = world
    d, a = case(world, "PP-D-1001")
    p = plans.rules_plan(d, shop, a)
    plans.execute(pp, p, plans.documents(p, shop, "PP-D-1001", a))
    sent = next(c for c in pp.calls if c[0] == "provide_evidence")[2]["evidences"]
    pof = next(e for e in sent if e["evidence_type"] == "PROOF_OF_FULFILLMENT")
    assert pof["evidence_info"]["tracking_info"][0]["tracking_number"] == "4589203716"
    names = [n for n, _ in pp.uploads["PP-D-1001"]]
    assert "proof-of-delivery.pdf" in names
    assert all(data.startswith(b"%PDF") for _, data in pp.uploads["PP-D-1001"])


def test_gate_blocks_invented_records_and_tracking(world):
    shop, _ = world
    d, a = case(world, "PP-D-1001")
    p = plans.rules_plan(d, shop, a)
    p["claims"].append({"text": "Buyer admitted receiving it.", "cites": ["message:999"]})
    p["evidence"][0]["tracking"]["tracking_number"] = "0000000000"
    v = gate.run(p, d, shop, a, Settings())
    failed = {c.name for c in v.checks if not c.ok}
    assert "Every fact cites a real record" in failed
    assert "Tracking numbers match the shipment" in failed
    assert not v.passed


def test_gate_blocks_missing_requested_evidence(world):
    shop, _ = world
    d, a = case(world, "PP-D-1004")
    p = plans.rules_plan(d, shop, a)
    p["evidence"] = [e for e in p["evidence"] if e["evidence_type"] != "OTHER"]
    v = gate.run(p, d, shop, a, Settings())
    assert not v.passed


def test_gate_blocks_fighting_a_lost_case(world):
    shop, _ = world
    d, a = case(world, "PP-D-1003")
    p = plans.rules_plan(d, shop, a, action="fight")
    v = gate.run(p, d, shop, a, Settings())
    assert any(c.name == "Case is winnable" and not c.ok for c in v.checks)


def test_gate_blocks_offers_paypal_does_not_allow(world):
    shop, _ = world
    d, a = case(world, "PP-D-1004")          # unauthorised: no make_offer link
    p = plans.rules_plan(d, shop, a, action="partial_refund")
    p["offer_amount"] = 50.0
    v = gate.run(p, d, shop, a, Settings())
    assert any(c.name == "Action allowed by PayPal" and not c.ok for c in v.checks)


def test_gate_blocks_past_deadline(world):
    shop, pp = world
    pp.disputes["PP-D-1001"]["seller_response_due_date"] = "2020-01-01T00:00:00.000Z"
    d, a = case(world, "PP-D-1001")
    v = gate.run(plans.rules_plan(d, shop, a), d, shop, a, Settings())
    assert any(c.name == "Deadline still open" and not c.ok for c in v.checks)


def test_autopilot_only_when_under_limit_and_matching(world):
    shop, _ = world
    d, a = case(world, "PP-D-1003")
    p = plans.rules_plan(d, shop, a)
    assert gate.run(p, d, shop, a, Settings(autopilot_limit=50)).autopilot
    assert not gate.run(p, d, shop, a, Settings(autopilot_limit=20)).autopilot
    assert not gate.run(p, d, shop, a, Settings()).autopilot


def test_fake_enforces_paypal_rules(world):
    _, pp = world
    with pytest.raises(PayPalError):
        pp.make_offer("PP-D-1004", "REFUND", {"currency_code": "USD", "value": "10.00"}, "x")
    with pytest.raises(PayPalError):
        pp.make_offer("PP-D-1002", "REFUND", {"currency_code": "USD", "value": "99.00"}, "x")
    with pytest.raises(PayPalError):
        pp.adjudicate("PP-D-1001", "SELLER_FAVOR")   # not under review yet


class ScriptedLLM:
    """Plays back tool calls, so the agent loop is tested without a model."""
    model = "scripted"

    def __init__(self, steps):
        self.steps = list(steps)

    def chat(self, messages, tools):
        name, args = self.steps.pop(0)
        return {"content": "", "tool_calls": [{"id": f"c{len(self.steps)}", "type": "function",
                                               "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_agent_loop_builds_a_plan_from_tool_calls(world):
    shop, _ = world
    d, a = case(world, "PP-D-1001")
    llm = ScriptedLLM([
        ("get_dispute", {}), ("get_assessment", {}), ("get_shipment", {"order_id": "EC-2041"}),
        ("submit_plan", {"action": "fight", "summary": "Delivered and signed.",
                         "claims": [{"text": "DHL delivered it.", "cites": ["shipment:EC-2041"]}],
                         "evidence": [{"evidence_type": "PROOF_OF_FULFILLMENT", "doc": "delivery",
                                       "notes": "Signed for.", "cites": ["shipment:EC-2041"],
                                       "tracking_number": "4589203716", "carrier": "DHL"}],
                         "buyer_message": "Hi Jordan"}),
    ])
    p = investigate(d, shop, a, llm)
    assert p["author"] == "scripted"
    assert [t["tool"] for t in p["trace"]] == ["get_dispute", "get_assessment", "get_shipment", "submit_plan"]
    assert gate.run(p, d, shop, a, Settings()).passed


def test_agent_failure_falls_back_to_rules_visibly(world):
    shop, _ = world
    d, a = case(world, "PP-D-1001")

    class Broken:
        model = "broken"

        def chat(self, *_):
            raise RuntimeError("model down")

    p = investigate(d, shop, a, Broken())
    assert p["author"] == "rules"
    assert p["trace"][-1]["tool"] == "fallback"


def test_buyer_text_reaches_the_model_marked_untrusted(world):
    shop, _ = world
    d, a = case(world, "PP-D-1001")
    seen = []

    class Spy(ScriptedLLM):
        def chat(self, messages, tools):
            seen.append(messages[-1].get("content", ""))
            return super().chat(messages, tools)

    investigate(d, shop, a, Spy([("get_dispute", {}), ("get_messages", {"order_id": "EC-2002"}),
                                ("submit_plan", {"action": "fight", "summary": "", "claims": [], "evidence": [],
                                                 "buyer_message": ""})]))
    assert "buyer_messages_untrusted" in seen[1]


def test_claim_already_under_review_gets_supporting_info(world):
    """What the sandbox did with a real 'not received' claim: straight to review, one action allowed."""
    shop, pp = world
    pp.open_dispute(dispute_id="PP-R-1", transaction_id="7KD04291VX3301845", buyer_name="Jordan Miller",
                    amount="148.00", reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED", stage="CHARGEBACK",
                    under_review=True)
    d, a = case(world, "PP-R-1")
    assert d["seller_response_due_date"] is None
    assert [o.action for o in a.options] == ["supporting_info"] and a.recommended == "supporting_info"
    p = plans.rules_plan(d, shop, a)
    v = gate.run(p, d, shop, a, Settings())
    assert v.passed, [c for c in v.checks if not c.ok]
    plans.execute(pp, p, plans.documents(p, shop, "PP-R-1", a))
    sent = next(c for c in pp.calls if c[0] == "provide_supporting_info")[2]["notes"]
    assert "4589203716" in sent and len(sent) <= 2000
    assert "proof-of-delivery.pdf" in [n for n, _ in pp.uploads["PP-R-1"]]
