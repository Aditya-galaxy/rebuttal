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


def test_snapshots_survive_a_restart(tmp_path):
    from rebuttal.persist import Snapshotter, restore
    data, snap, data2 = tmp_path / "data", tmp_path / "snap", tmp_path / "data2"
    for d in (data, snap, data2):
        d.mkdir()
    shop = Shop(data / "shop.db")
    seed_shop(shop)
    s = Snapshotter(snap, {"shop.db": shop.db})
    s.flush()
    shop.add_order("EC-9999", "TXN9", "New Buyer", "n@example.com", "X", [], "1.00")
    s.stop()                                   # what shutdown does
    restore(snap, data2, ["shop.db"])
    assert Shop(data2 / "shop.db").order("EC-9999")["buyer_name"] == "New Buyer"


def test_webhook_refreshes_known_disputes_and_ignores_unknown(monkeypatch):
    from fastapi.testclient import TestClient

    from rebuttal import web
    from rebuttal.engine import Engine
    from rebuttal.state import State
    shop, pp = Shop(), FakePayPal()
    seed_shop(shop)
    seed_pp(pp)
    monkeypatch.setattr(web, "engine", Engine(pp, shop, State(), None, "mock"))
    client = TestClient(web.app)
    ok = client.post("/webhooks/paypal", json={"event_type": "CUSTOMER.DISPUTE.CREATED",
                                               "resource": {"dispute_id": "PP-D-1001"}})
    assert ok.status_code == 200 and web.engine.state.get("PP-D-1001")
    unknown = client.post("/webhooks/paypal", json={"event_type": "CUSTOMER.DISPUTE.CREATED",
                                                    "resource": {"dispute_id": "PP-D-0000"}})
    assert unknown.status_code == 200 and unknown.json()["ignored"]


def test_carrier_scans_appear_only_once_they_happen():
    from datetime import UTC, datetime, timedelta
    shop = Shop()
    t0 = datetime(2026, 11, 4, tzinfo=UTC)
    shop.add_order("EC-1", "TX1", "Jo Lee", "j@example.com", "Austin, TX", [], "10.00", paypal_order_id="PO1")
    shop.add_shipment("EC-1", "FedEx", "77", "Priority", "DELIVERED", "J. LEE", [
        {"at": (t0 + timedelta(hours=20)).isoformat(), "where": "Jaipur, IN", "event": "Picked up"},
        {"at": (t0 + timedelta(hours=92)).isoformat(), "where": "Austin, TX, US", "event": "Delivered"}])
    early = shop.shipment("EC-1", now=t0 + timedelta(hours=1))
    assert (early["status"], early["events"], early["signature"]) == ("LABEL_CREATED", [], None)
    mid = shop.shipment("EC-1", now=t0 + timedelta(hours=30))
    assert mid["status"] == "IN_TRANSIT" and len(mid["events"]) == 1 and mid["expected_delivery"]
    done = shop.shipment("EC-1", now=t0 + timedelta(hours=93))
    assert (done["status"], done["signature"], len(done["events"])) == ("DELIVERED", "J. LEE", 2)


def test_not_received_before_delivery_is_not_a_strong_fight():
    """A buyer who disputes while the parcel is still moving: no proof of delivery yet."""
    shop, pp = Shop(), FakePayPal()
    seed_shop(shop)
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    shop.add_order("EC-5", "TX5", "Jo Lee", "j@example.com", "Austin, TX",
                   [{"sku": "VASE-INDIGO", "title": "Vase", "qty": 1, "price": "148.00"}], "148.00")
    shop.add_shipment("EC-5", "FedEx", "78", "Priority", "DELIVERED", "J. LEE", [
        {"at": (now - timedelta(hours=10)).isoformat(), "where": "Jaipur, IN", "event": "Picked up"},
        {"at": (now + timedelta(hours=60)).isoformat(), "where": "Austin, TX, US", "event": "Delivered"}])
    pp.open_dispute(dispute_id="PP-D-5", transaction_id="TX5", buyer_name="Jo Lee", amount="148.00",
                    reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED")
    a = assess(pp.get_dispute("PP-D-5"), shop)
    assert a.strength == "medium" and a.recommended != "fight"


def test_in_transit_case_messages_then_fights_once_delivered():
    from datetime import UTC, datetime, timedelta

    from rebuttal.engine import Engine
    from rebuttal.state import State
    shop, pp = Shop(), FakePayPal()
    seed_shop(shop)
    now = datetime.now(UTC)
    shop.add_order("EC-7", "TX7", "Jo Lee", "j@example.com", "Austin, TX",
                   [{"sku": "VASE-INDIGO", "title": "Vase", "qty": 1, "price": "148.00"}], "148.00")
    journey = [{"at": (now - timedelta(hours=10)).isoformat(), "where": "Jaipur, IN", "event": "Picked up"},
               {"at": (now + timedelta(hours=60)).isoformat(), "where": "Austin, TX, US", "event": "Delivered"}]
    shop.add_shipment("EC-7", "FedEx", "79", "Priority", "DELIVERED", "J. LEE", journey)
    pp.open_dispute(dispute_id="PP-D-7", transaction_id="TX7", buyer_name="Jo Lee", amount="148.00",
                    reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED", requested=["PROOF_OF_FULFILLMENT"])
    e = Engine(pp, shop, State(), None, "mock")
    e.sync(wait=True)
    assert e.state.get("PP-D-7")["plan"]["action"] == "message"
    e.send("PP-D-7")
    assert "79" in pp.get_dispute("PP-D-7")["messages"][-1]["content"]
    e.sync(wait=True)                                      # nothing changed: no new investigation
    assert e.state.get("PP-D-7")["status"] == "sent"
    journey[-1]["at"] = (now - timedelta(minutes=5)).isoformat()   # the parcel arrives
    shop.add_shipment("EC-7", "FedEx", "79", "Priority", "DELIVERED", "J. LEE", journey)
    e.sync(wait=True)
    case = e.state.get("PP-D-7")
    assert case["status"] == "ready" and case["plan"]["action"] == "fight"


def test_shop_reads_stay_correct_under_parallel_investigations():
    """Four investigations share one connection; each must still see its own rows."""
    from concurrent.futures import ThreadPoolExecutor
    shop = Shop()
    seed_shop(shop)
    want = {oid: shop.shipment(oid)["tracking_number"] for oid in ("EC-2041", "EC-2055", "EC-2063", "EC-2070")}

    def read(oid):
        return all(shop.shipment(oid)["tracking_number"] == want[oid] and shop.order(oid)["id"] == oid
                   and isinstance(shop.messages(oid), list) for _ in range(300))

    with ThreadPoolExecutor(8) as pool:
        assert all(pool.map(read, list(want) * 4))


def test_demo_reset_while_investigating_leaves_every_case_planned():
    import time

    from rebuttal.engine import Engine
    from rebuttal.state import State

    class Slow:
        model = "slow"

        def chat(self, *_):
            time.sleep(0.3)
            raise RuntimeError("slow model gives up")   # falls back to the rules plan

    shop, pp = Shop(), FakePayPal()
    seed_shop(shop)
    seed_pp(pp)
    e = Engine(pp, shop, State(), Slow(), "mock")
    e.sync()                      # four investigations start
    e.reset_demo()                # reset before they finish
    e.pool.shutdown(wait=True)
    cases = {c["dispute_id"]: c for c in e.state.all()}
    assert set(cases) == {"PP-D-1001", "PP-D-1002", "PP-D-1003", "PP-D-1004"}
    assert all(c["status"] == "ready" and c["plan"] for c in cases.values())


def test_insights_data_labels_generated_history_and_keeps_sandbox_real(world):
    from rebuttal import insights
    from rebuttal.engine import Engine
    from rebuttal.state import State
    shop, pp = world
    e = Engine(pp, shop, State(), None, "mock")
    e.sync(wait=True)
    demo = insights.build(e, demo_history=True)
    live = insights.build(e, demo_history=False)
    assert {d["source"] for d in live["disputes"]} == {"Live"} and len(live["disputes"]) == 4
    generated = [d for d in demo["disputes"] if d["source"] == "Generated demo history"]
    assert len(generated) == 64 and demo["generatedHistory"]
    for d in demo["disputes"]:   # Studio divides these, so they must be numbers that add up
        assert d["isWon"] <= d["isClosed"] and d["recovered"] + d["lost"] >= 0
    assert all(m["orders"] >= m["disputes"] for m in demo["monthly"])


def test_insights_assistant_is_off_unless_configured(monkeypatch):
    from fastapi.testclient import TestClient

    from rebuttal import web
    monkeypatch.delenv("INSIGHTS_AI", raising=False)
    monkeypatch.delenv("VERTEX_PROJECT", raising=False)
    r = TestClient(web.app).post("/api/ai/responses", json={"input": "hi"})
    assert r.status_code == 503


def test_ai_proxy_gives_earlier_output_items_ids():
    from rebuttal.web import _give_ids
    items = [{"type": "message", "role": "user", "content": []},
             {"type": "message", "role": "assistant", "content": []},
             {"type": "function_call", "call_id": "c1", "name": "x", "arguments": "{}"},
             {"type": "function_call_output", "call_id": "c1", "output": "{}"},
             {"type": "reasoning", "id": "rs_keep", "summary": []}]
    _give_ids(items)
    assert "id" not in items[0] and items[1]["id"].startswith("msg_") and items[2]["id"].startswith("fc_")
    assert "id" not in items[3] and items[4]["id"] == "rs_keep"


def test_ai_rate_limit_per_visitor_and_per_day():
    from rebuttal.web import RateLimit
    rl = RateLimit(per_visitor=2, per_day=3)
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a")
    assert rl.allow("b") and not rl.allow("c")


def test_agent_ignores_extra_tool_arguments_and_reports_missing_ones(world):
    shop, _ = world
    d, a = case(world, "PP-D-1001")
    llm = ScriptedLLM([
        ("get_dispute", {"dispute_id": "PP-D-1001"}),            # extra argument: dropped
        ("get_shipment", {}),                                       # missing argument: reported
        ("submit_plan", {"action": "fight", "summary": "", "claims": [], "evidence": [], "buyer_message": ""}),
    ])
    p = investigate(d, shop, a, llm)
    assert p["author"] == "scripted"
    assert "PP-D-1001" in p["trace"][0]["result"] and "missing argument" in p["trace"][1]["result"]


def test_gate_accepts_the_dispute_itself_as_a_citation_but_not_other_disputes(world):
    shop, _ = world
    d, a = case(world, "PP-D-1003")
    p = plans.rules_plan(d, shop, a)
    p["claims"].append({"text": "The buyer says tracking stopped.", "cites": ["dispute:PP-D-1003"]})
    assert gate.run(p, d, shop, a, Settings()).passed
    p["claims"].append({"text": "Another case.", "cites": ["dispute:PP-D-1001"]})
    assert not gate.run(p, d, shop, a, Settings()).passed


def test_bridge_schema_simplifier_stays_small_on_recursive_schemas():
    import json

    from rebuttal.responses_bridge import simplify
    # An expression type that refers to itself several times per level, like AG Studio's query schema.
    schema = {"$defs": {"expr": {"type": "object", "properties": {
        "op": {"type": "string"}, "left": {"$ref": "#/$defs/expr"}, "right": {"$ref": "#/$defs/expr"},
        "args": {"type": "array", "items": {"$ref": "#/$defs/expr"}},
        "maybe": {"anyOf": [{"$ref": "#/$defs/expr"}, {"type": "null"}]}}}},
        "type": "object", "properties": {"query": {"$ref": "#/$defs/expr"}}, "additionalProperties": False}
    out = simplify(schema)
    assert len(json.dumps(out)) < 5000
    assert "additionalProperties" not in json.dumps(out)
    assert out["properties"]["query"]["properties"]["left"]["type"] == "object"


def test_bridge_round_trip_tool_calls():
    from rebuttal import responses_bridge as rb
    body = {"instructions": "be brief", "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "checking"}]},
        {"type": "function_call", "call_id": "c1", "name": "view_page", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "{\"ok\":true}"}],
        "tools": [{"type": "function", "name": "view_page", "parameters": {"type": "object", "properties": {}}}],
        "tool_choice": "auto"}
    chat = rb.to_chat(body, "google/gemini-2.5-flash")
    roles = [m["role"] for m in chat["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert chat["messages"][2]["tool_calls"][0]["id"] == "c1"
    reply = {"choices": [{"message": {"content": "done", "tool_calls": [
        {"id": "c2", "function": {"name": "view_page", "arguments": "{}"}}]}}]}
    resp = rb.to_response(reply, "m")
    kinds = [o["type"] for o in resp["output"]]
    assert kinds == ["message", "function_call"] and resp["output"][1]["call_id"] == "c2"
    stream = b"".join(rb.events(resp)).decode()
    assert stream.count("event: response.output_item.done") == 2 and "event: response.completed" in stream


def test_llm_moves_to_the_next_endpoint_when_one_is_busy():
    import httpx

    from rebuttal.agent import LLM
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "busy" in str(request.url):
            return httpx.Response(429, json={"error": {"message": "Resource exhausted"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://busy.example/v1/")
    llm = LLM(None, "m", "https://busy.example/v1/", http=client, token=lambda: "t",
              fallback_bases=["https://spare.example/v1/"])
    assert llm.chat([{"role": "user", "content": "hi"}], [])["content"] == "ok"
    assert seen == ["https://busy.example/v1/chat/completions", "https://spare.example/v1/chat/completions"]


def test_agent_is_sent_back_once_when_it_picks_another_action(world):
    shop, _ = world
    d, a = case(world, "PP-D-1002")                  # the cost comparison says partial_refund
    plan = {"summary": "", "claims": [], "evidence": [], "buyer_message": "Hi"}
    llm = ScriptedLLM([("submit_plan", {**plan, "action": "fight"}),
                       ("submit_plan", {**plan, "action": "partial_refund", "offer_amount": 18.6})])
    p = investigate(d, shop, a, llm)
    assert p["action"] == "partial_refund" and "Sent back" in p["trace"][0]["note"]


def test_catalogue_refresh_fixes_old_saved_photos(tmp_path):
    from rebuttal.shop import seed_catalog
    shop = Shop(tmp_path / "shop.db")
    shop.add_product("CHAI-RUST", "Chai Cups", "old", "62.00", "x", ["cups-set.jpg"])   # an old database
    seed_catalog(shop)
    seed_catalog(shop)                                                                 # and again: no duplicates
    assert shop.product("CHAI-RUST")["photos"] == ["chai-rust.jpg"]
    assert len(shop.photos(sku="CHAI-RUST", source="listing")) == 1


def test_agent_drops_an_empty_email_thread_it_attached(world):
    shop, _ = world
    d, a = case(world, "PP-D-1001")
    shop.db.execute("delete from messages where order_id = ?", (a.order_id,))
    llm = ScriptedLLM([
        ("get_assessment", {}),
        ("submit_plan", {"action": "fight", "summary": "Delivered and signed.",
                         "claims": [{"text": "DHL delivered it.", "cites": ["shipment:EC-2041"]}],
                         "evidence": [{"evidence_type": "PROOF_OF_FULFILLMENT", "doc": "delivery",
                                       "notes": "Signed for.", "cites": ["shipment:EC-2041"],
                                       "tracking_number": "4589203716", "carrier": "DHL"},
                                      {"evidence_type": "OTHER", "doc": "messages",
                                       "notes": "The emails.", "cites": ["message:EC-2041"]}],
                         "buyer_message": "Hi Jordan"}),
    ])
    p = investigate(d, shop, a, llm)
    assert [e["doc"] for e in p["evidence"]] == ["delivery"]
    assert gate.run(p, d, shop, a, Settings()).passed
