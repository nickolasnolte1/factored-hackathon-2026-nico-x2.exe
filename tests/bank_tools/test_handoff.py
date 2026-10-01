"""Handoff to a human agent (CONTRACT §3.15): a validated, PII-scrubbed package (never a transcript), evidence
from this conversation only, the verified draft attached, reason checks, routing, dedupe and verified read-back."""
import json

import pytest

from src.policy import dispute_policy as dp
from tests.bank_tools import fixture_data as fx
from tests.bank_tools import harness as hz
from tests.bank_tools.harness import expect_error, expect_ok

UNREC = dp.DISPUTE_INTENTS[0]
POL = hz.POLICY
QUEUES = {"suspected_card_compromise": "card_security", "customer_status_restricted": "account_restrictions",
          "complaint_routing": "complaints"}
PII = {
    "email": "ana.fixture@example.com",
    "customer": fx.C1,
    "phone": "+57 300 555 0101",
    "card": "4111 1111 1111 1111",
    "document": "CC 1000000001",
    "ip": "192.168.10.20",
    "bare_phone": "3005550101",
}


def _stored_text(bank, ticket_id, drop=("customer_id",)):
    row = bank.ticket(ticket_id)
    assert row is not None, "the ticket must be readable from the store"
    return json.dumps({k: v for k, v in dict(row).items() if k not in drop}, ensure_ascii=False, default=str), row


def _prepare(conv, key="super"):
    return conv.ok("prepare_dispute_case", {"transaction_id": fx.T[key], "intent": UNREC, "language": "es"})


@pytest.mark.parametrize("missing", ["request_summary", "verified_facts", "actions_taken", "evidence", "open_questions"])
def test_package_requires_every_field(bank, missing):
    pkg = hz.package()
    del pkg[missing]
    details = expect_error(bank.customer(fx.C1).call("handoff_to_human", hz.handoff_args("explicit_human_request", pkg)),
                           "VALIDATION_ERROR")
    assert any(missing in f.get("path", "") for f in details.get("fields", []))


@pytest.mark.parametrize("change", [
    {"request_summary": "corto"},
    {"verified_facts": "un hecho"},
    {"evidence": ["call-1"]},
    {"evidence": ["tc_0123456789abcdef", "tc_0123456789abcdef"]},
    {"transcript": "Cliente: hola\nAgente: hola"},
])
def test_package_shape_and_limits_are_enforced(bank, change):
    pkg = {**hz.package(), **change}
    expect_error(bank.customer(fx.C1).call("handoff_to_human", hz.handoff_args("explicit_human_request", pkg)),
                 "VALIDATION_ERROR")


@pytest.mark.parametrize("field,value", [("request_summary", "x" * 601), ("verified_facts", ["y" * 201]),
                                         ("open_questions", ["z" * 201])])
def test_texts_above_the_limits_are_never_stored_whole(bank, field, value):
    conv = bank.customer(fx.C1)
    env = conv.call("handoff_to_human", hz.handoff_args("explicit_human_request", {**hz.package(), field: value}))
    if env["ok"] is False:
        expect_error(env, "VALIDATION_ERROR")
        return
    data = env["data"]
    assert any(field in name for name in data["truncated_fields"]), data["truncated_fields"]
    stored, _ = _stored_text(bank, data["ticket_id"])
    assert (value if isinstance(value, str) else value[0]) not in stored


@pytest.mark.parametrize("field,limit", [("verified_facts", 12), ("actions_taken", 12), ("open_questions", 6),
                                         ("evidence", 30)])
def test_list_limits_are_enforced(bank, field, limit):
    """Over-long lists are rejected (schema maxItems) or cut to the limit and reported; never stored whole."""
    conv = bank.customer(fx.C1)
    if field == "evidence":  # real tool_call_ids of this conversation (under the 40-calls-per-session limit)
        items = [conv.call("list_products")["meta"]["tool_call_id"] for _ in range(limit + 1)]
    else:
        items = [f"item-{i:02d} del paquete" for i in range(limit + 1)]
    env = conv.call("handoff_to_human", hz.handoff_args("explicit_human_request", {**hz.package(), field: items}))
    if env["ok"] is False:
        expect_error(env, "VALIDATION_ERROR")
        return
    assert any(field in name for name in env["data"]["truncated_fields"]), env["data"]["truncated_fields"]
    stored, _ = _stored_text(bank, env["data"]["ticket_id"])
    assert sum(item in stored for item in items) <= limit


def test_reason_code_must_come_from_the_policy(bank):
    conv = bank.customer(fx.C1)
    for reason in ("refund_request", "other", "", "EXPLICIT_HUMAN_REQUEST"):
        expect_error(conv.call("handoff_to_human", hz.handoff_args(reason)), "VALIDATION_ERROR")
    expect_error(conv.call("handoff_to_human", hz.handoff_args("explicit_human_request", language="en")),
                 "VALIDATION_ERROR")


def test_pii_is_scrubbed_counted_and_service_ids_survive(bank):
    conv = bank.customer(fx.C1)
    bank.secrets |= set(PII.values()) - {fx.C1}
    summary = (f"Cliente {PII['customer']} escribe desde {PII['email']}, cel {PII['phone']}, tarjeta {PII['card']}, "
               f"{PII['document']}, IP {PII['ip']}; no reconoce un cobro.")
    fact = f"Movimiento {fx.T['super']} del producto {fx.P['credit']} por 250000.00 COP el 2026-06-15"
    pkg = hz.package(summary, facts=[fact], actions=["Se buscaron movimientos"],
                     questions=[f"Confirmar el telefono {PII['bare_phone']}"])
    data = conv.ok("handoff_to_human", hz.handoff_args("explicit_human_request", pkg))
    hz.check_output("handoff_to_human", data)
    assert data["verified"] is True and data["redactions"] >= 7
    stored, row = _stored_text(bank, data["ticket_id"])
    agent_text = json.dumps({k: v for k, v in dict(row).items() if "summary" in k or "agent_reported" in k},
                            ensure_ascii=False, default=str)
    assert agent_text != "{}", "the ticket stores the agent's package"
    for label, raw in PII.items():
        assert raw not in (agent_text if label == "customer" else stored), f"{label} stored unscrubbed"
    for token in ("[EMAIL]", "[CUSTOMER_ID]", "[IP]"):
        assert token in agent_text, token
    assert fx.T["super"] in stored and fx.P["credit"] in stored, "service ids are never scrubbed"
    assert row.get("customer_id") == fx.C1, "the ticket carries the verified customer internally"


@pytest.mark.parametrize("reason", hz.HANDOFF_REASONS)
def test_every_policy_reason_routes_to_its_queue(bank, reason):
    data = bank.customer(fx.C1).ok("handoff_to_human", hz.handoff_args(reason))
    assert data["verified"] is True and data["status"] == "queued" and data["replayed"] is False
    assert data["queue"] == QUEUES.get(reason, "disputes")
    assert data["identity_verified"] is True and data["case_draft_attached"] is False and data["draft_status"] is None
    if reason == "suspected_card_compromise":
        assert data["priority"] == "high"
        assert data["first_response_hours"] == POL["priority"]["first_response_hours"]["high"]
    else:
        assert data["priority"] is None and data["first_response_hours"] is None


def test_evidence_from_other_conversations_is_dropped(bank):
    token = bank.session(fx.C1)
    other, conv = bank.conv(token), bank.conv(token)
    foreign_tc = other.call("list_products")["meta"]["tool_call_id"]
    own_tc = conv.call("find_candidate_transactions", {"purpose": "dispute", "hints": {"merchant": "Super Ahorro"}})[
        "meta"]["tool_call_id"]
    unknown_tc = "tc_0123456789abcdef"
    env = conv.call("handoff_to_human", hz.handoff_args(
        "explicit_human_request", hz.package(evidence=[own_tc, foreign_tc, unknown_tc])))
    data = expect_ok(env)
    assert sorted(data["dropped_evidence"]) == sorted([foreign_tc, unknown_tc])
    assert env["warnings"], "dropped evidence is reported as a warning"
    stored, _ = _stored_text(bank, data["ticket_id"])
    assert own_tc in stored and "find_candidate_transactions" in stored, "kept evidence carries the service's record"
    assert "list_products" not in stored, "the other conversation's record must not be attached"


def test_confirmed_above_threshold_draft_reaches_the_human_pending_review(bank):
    conv = bank.customer(fx.C1)
    found = conv.call("find_candidate_transactions", {"purpose": "dispute", "intent": UNREC,
                                                      "hints": {"txn_type": "Transfer", "amount": 40000000.0}})
    draft_env = conv.call("prepare_dispute_case", {"transaction_id": fx.T["big"], "intent": UNREC, "language": "es"})
    draft = expect_ok(draft_env)
    assert draft["policy_decision"]["handoff_reason"] == "amount_above_threshold"
    conv.next_turn()
    evidence = [found["meta"]["tool_call_id"], draft_env["meta"]["tool_call_id"]]
    data = conv.ok("handoff_to_human", hz.handoff_args(
        "amount_above_threshold", hz.package(evidence=evidence), confirmation_id=draft["confirmation_id"]))
    exp = fx.expected_case("big", UNREC)
    assert data["case_draft_attached"] is True and data["draft_status"] == "pending_human_review"
    assert data["reason_check"] == "consistent" and data["queue"] == "disputes"
    assert data["priority"] == exp["priority"] and data["first_response_hours"] == exp["first_response_hours"]
    stored, _ = _stored_text(bank, data["ticket_id"])
    assert "pending_human_review" in stored and fx.T["big"] in stored
    assert bank.cases() == [], "a handoff never creates a case"


@pytest.mark.parametrize("reason,setup,expected", [
    ("customer_status_restricted", None, "inconsistent"),
    ("amount_above_threshold", "draft_super", "inconsistent"),
    ("outside_dispute_window", "draft_old", "consistent"),
    ("no_match_after_clarification", None, "inconsistent"),
    ("no_match_after_clarification", "two_failed_searches", "consistent"),
    ("tool_failure", None, "inconsistent"),
    ("explicit_human_request", None, "not_verifiable"),
    ("complaint_routing", None, "not_verifiable"),
    ("low_intent_confidence", None, "not_verifiable"),
])
def test_reason_check_never_blocks_the_handoff(bank, reason, setup, expected):
    conv = bank.customer(fx.C1)
    extra = {}
    if setup in ("draft_super", "draft_old"):
        extra["confirmation_id"] = _prepare(conv, setup.split("_")[1])["confirmation_id"]
    elif setup == "two_failed_searches":
        for merchant in ("Mercado Lejano", "Mercado Remoto"):
            conv.ok("find_candidate_transactions", {"purpose": "dispute", "hints": {"merchant": merchant}})
            conv.next_turn()
    data = conv.ok("handoff_to_human", hz.handoff_args(reason, **extra))
    assert data["verified"] is True and data["reason_check"] == expected


def test_tool_failure_reason_is_consistent_after_an_unavailable_result(make_bank):
    bank = make_bank(faults=[{"tool": "get_transactions", "type": "timeout", "failing_attempts": 99}])
    conv = bank.customer(fx.C1)
    env = conv.call("find_candidate_transactions", {"purpose": "dispute", "hints": {"merchant": "Super Ahorro"}})
    expect_error(env, "UNAVAILABLE", handoff_reason="tool_failure", next_action="handoff")
    data = conv.ok("handoff_to_human", hz.handoff_args("tool_failure", hz.package(evidence=[env["meta"]["tool_call_id"]])))
    assert data["reason_check"] == "consistent" and data["dropped_evidence"] == []


def test_unauthenticated_handoff_is_unbound(bank):
    owner = bank.customer(fx.C1)
    draft = _prepare(owner)
    conv = bank.conv(token=None)
    env = conv.call("handoff_to_human", hz.handoff_args("explicit_human_request", confirmation_id=draft["confirmation_id"],
                                                        candidate_transaction_ids=[fx.T["super"]]))
    data = expect_ok(env)
    assert data["verified"] is True and data["identity_verified"] is False
    assert data["case_draft_attached"] is False and data["draft_status"] is None
    assert env["warnings"], "ignored draft and candidates are reported"
    stored, row = _stored_text(bank, data["ticket_id"], drop=())
    assert row.get("customer_id") is None
    assert "250000" not in stored and fx.P["credit"] not in stored


def test_expired_session_handoff_is_unbound(bank):
    conv = bank.customer(fx.C1)
    draft = _prepare(conv)
    bank.advance(16 * 60)
    conv.next_turn()
    data = conv.ok("handoff_to_human", hz.handoff_args("explicit_human_request", confirmation_id=draft["confirmation_id"]))
    assert data["identity_verified"] is False and data["case_draft_attached"] is False
    _, row = _stored_text(bank, data["ticket_id"], drop=())
    assert row.get("customer_id") is None


def test_handoff_is_deduplicated(bank):
    conv = bank.customer(fx.C1)
    key = hz.new_key()
    first = conv.ok("handoff_to_human", hz.handoff_args("explicit_human_request", idempotency_key=key))
    again = conv.ok("handoff_to_human", hz.handoff_args("explicit_human_request", idempotency_key=key))
    assert again["ticket_id"] == first["ticket_id"] and again["replayed"] is True
    draft = _prepare(conv, "old")
    a = conv.ok("handoff_to_human", hz.handoff_args("outside_dispute_window", confirmation_id=draft["confirmation_id"]))
    b = conv.ok("handoff_to_human", hz.handoff_args("outside_dispute_window", confirmation_id=draft["confirmation_id"]))
    assert b["ticket_id"] == a["ticket_id"] and b["replayed"] is True
    c = conv.ok("handoff_to_human", hz.handoff_args("complaint_routing"))
    assert c["ticket_id"] not in (first["ticket_id"], a["ticket_id"])


def test_ticket_write_failure_falls_back_to_static_text(make_bank):
    bank = make_bank(faults=[{"tool": "create_ticket", "type": "error", "failing_attempts": 99}])
    env = bank.customer(fx.C1).call("handoff_to_human", hz.handoff_args("explicit_human_request"))
    expect_error(env, "UNAVAILABLE", next_action="static_fallback", attempts=1 + POL["handoff"]["tool_max_retries"])


def test_ticket_read_back_mismatch_is_never_verified(make_bank, snapshot_path):
    def corrupt(original, *a, **k):
        r = original(*a, **k)
        if not r:
            return r
        r = dict(r)
        r.update({"reason_code": "complaint_routing", "queue": "complaints", "status": "closed"})
        return r

    bank = make_bank(repo=hz.stub_repository(snapshot_path, {"get_ticket": corrupt}))
    env = bank.customer(fx.C1).call("handoff_to_human", hz.handoff_args("explicit_human_request"))
    assert env["ok"] is False and env["error"]["code"] in ("INTERNAL", "UNAVAILABLE")


def test_foreign_and_future_candidates_are_dropped_without_failing(bank):
    conv = bank.customer(fx.C1)
    env = conv.call("handoff_to_human", hz.handoff_args(
        "explicit_human_request", candidate_transaction_ids=[fx.T["super"], fx.T["b_super"], fx.T["future"]]))
    data = expect_ok(env)
    assert data["verified"] is True and env["warnings"]
    stored, _ = _stored_text(bank, data["ticket_id"])
    assert fx.T["super"] in stored
    assert "15432.1" not in stored and fx.P["b_credit"] not in stored, "another customer's data reached the ticket"
    assert "12000" not in stored and "Kiosko Luna" not in stored, "a movement after now reached the ticket"


def test_card_compromise_is_a_high_priority_security_ticket(bank):
    data = bank.customer(fx.C1).ok("handoff_to_human", hz.handoff_args("suspected_card_compromise", language="pt"))
    assert data["queue"] == "card_security" and data["priority"] == "high"
    assert data["case_draft_attached"] is False and data["reason_check"] == "not_verifiable"


def test_ticket_status_is_visible_to_its_owner(bank):
    conv = bank.customer(fx.C1)
    ticket = conv.ok("handoff_to_human", hz.handoff_args("complaint_routing"))
    view = conv.ok("get_case_status", {"case_id": ticket["ticket_id"]})
    hz.check_output("get_case_status", view)
    assert view["ticket"]["ticket_id"] == ticket["ticket_id"] and view["ticket"]["reason_code"] == "complaint_routing"
    assert view["ticket"]["queue"] == "complaints" and view["ticket"]["status"] == "queued"
