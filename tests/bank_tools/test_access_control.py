"""Ownership and access control in the service layer (CONTRACT §2.4, §3.0, §4): foreign ids look exactly like
unknown ids, probes are audited and revoke the session, restricted customers are gated, nothing crosses customers."""
import json

import pytest

from tests.bank_tools import fixture_data as fx
from tests.bank_tools import harness as hz
from tests.bank_tools.harness import expect_error, without_meta

UNREC, INCORRECT = "dispute_unrecognized_charge", "dispute_incorrect_charge_or_fee"
RANDOM = {"product": "PRD-ZZZZZZZZZZZZ", "transaction": "TRX-" + "Z" * 20, "case": "DSP-ZZZZZZZZZZZZ",
          "ticket": "HND-ZZZZZZZZZZZZ"}

PROBES = [
    ("get_balance", "product", lambda i: {"product_id": i}, fx.P["b_savings"]),
    ("list_recent_transactions", "product", lambda i: {"product_id": i}, fx.P["b_credit"]),
    ("explain_decline", "transaction", lambda i: {"transaction_id": i, "language": "es"}, fx.T["b_decl"]),
    ("check_dispute_eligibility", "transaction", lambda i: {"transaction_id": i, "intent": UNREC}, fx.T["b_super"]),
    ("prepare_dispute_case", "transaction", lambda i: {"transaction_id": i, "intent": UNREC, "language": "es"},
     fx.T["b_super"]),
    ("explain_decline", "transaction", lambda i: {"transaction_id": i, "language": "pt"}, fx.T["c_purchase"]),
]


def _probe_event(record):
    return "foreign_resource_probe" in (record.get("security_events") or [])


def _c2_case_and_ticket(bank):
    """C2 creates a dispute case and a handoff ticket through the normal flow."""
    conv = bank.customer(fx.C2)
    draft = conv.ok("prepare_dispute_case", {"transaction_id": fx.T["b_super"], "intent": UNREC, "language": "es"})
    conv.next_turn()
    case = conv.ok("create_dispute_case", {"confirmation_id": draft["confirmation_id"],
                                           "transaction_id": fx.T["b_super"], "customer_confirmed": True,
                                           "idempotency_key": hz.new_key()})
    ticket = conv.ok("handoff_to_human", hz.handoff_args("explicit_human_request"))
    assert case["verified"] is True and ticket["verified"] is True
    return case["case_id"], ticket["ticket_id"]


@pytest.mark.parametrize("tool,kind,args,foreign", PROBES, ids=[f"{p[0]}-{p[3][:9]}" for p in PROBES])
def test_foreign_ids_look_exactly_like_unknown_ids(bank, tool, kind, args, foreign):
    conv = bank.customer(fx.C1)
    env_foreign = conv.call(tool, args(foreign))
    env_random = conv.call(tool, args(RANDOM[kind]))
    expect_error(env_foreign, "NOT_FOUND", next_action="ask_customer")
    assert without_meta(env_foreign) == without_meta(env_random)
    assert _probe_event(bank.audit_for(env_foreign)), "a foreign id must be audited as a probe"
    assert bank.audit_for(env_foreign).get("internal_reason") == "forbidden_foreign_resource"
    assert not _probe_event(bank.audit_for(env_random))


def test_foreign_case_and_ticket_ids_look_like_unknown_ids(bank):
    case_id, ticket_id = _c2_case_and_ticket(bank)
    conv = bank.customer(fx.C1)
    for foreign, kind in ((case_id, "case"), (ticket_id, "ticket")):
        env_foreign = conv.call("get_case_status", {"case_id": foreign})
        env_random = conv.call("get_case_status", {"case_id": RANDOM[kind]})
        expect_error(env_foreign, "NOT_FOUND")
        assert without_meta(env_foreign) == without_meta(env_random)
    assert conv.ok("get_case_status") == {"cases": []}
    owner = bank.customer(fx.C2)
    assert owner.ok("get_case_status", {"case_id": case_id})["case"]["case_id"] == case_id
    assert owner.ok("get_case_status", {"case_id": ticket_id})["ticket"]["ticket_id"] == ticket_id


def test_create_with_a_foreign_transaction_reveals_nothing(bank):
    conv = bank.customer(fx.C1)
    draft = conv.ok("prepare_dispute_case", {"transaction_id": fx.T["super"], "intent": UNREC, "language": "es"})
    conv.next_turn()
    envs = [conv.call("create_dispute_case", {"confirmation_id": draft["confirmation_id"], "transaction_id": t,
                                              "customer_confirmed": True, "idempotency_key": hz.new_key()})
            for t in (fx.T["b_super"], RANDOM["transaction"])]
    expect_error(envs[0], "CONFIRMATION_REQUIRED", reason="transaction_mismatch")
    assert without_meta(envs[0]) == without_meta(envs[1])
    assert bank.cases(fx.C1) == [] and bank.cases(fx.C2) == []


def test_three_foreign_probes_revoke_the_session(bank):
    conv = bank.customer(fx.C1)
    probes = [conv.call("get_balance", {"product_id": fx.P["b_savings"]}),
              conv.call("explain_decline", {"transaction_id": fx.T["b_decl"], "language": "es"}),
              conv.call("get_balance", {"product_id": fx.P["c_credit"]})]
    for env in probes:
        expect_error(env, "NOT_FOUND")
        assert _probe_event(bank.audit_for(env))
    expect_error(conv.call("get_customer_overview", {}), "AUTH_REQUIRED")
    expect_error(conv.call("list_products", {}), "AUTH_REQUIRED")


def test_unknown_ids_are_not_probes(bank):
    conv = bank.customer(fx.C1)
    for _ in range(4):
        expect_error(conv.call("get_balance", {"product_id": RANDOM["product"]}), "NOT_FOUND")
    conv.ok("get_customer_overview")


def test_rows_with_a_mismatched_product_owner_are_never_served(bank):
    conv = bank.customer(fx.C1)
    listed = conv.ok("list_recent_transactions", {"limit": 20})["transactions"]
    assert fx.T["notowned"] not in {t["transaction_id"] for t in listed}
    found = conv.ok("find_candidate_transactions", {"purpose": "dispute", "hints": {"merchant": "Bazar Rojo"}})
    assert found["match_status"] == "none"
    expect_error(conv.call("check_dispute_eligibility", {"transaction_id": fx.T["notowned"], "intent": UNREC}),
                 "NOT_FOUND")


def test_no_other_customer_data_in_any_output(bank):
    conv = bank.customer(fx.C1)
    conv.ok("get_customer_overview")
    conv.ok("list_products")
    conv.ok("list_recent_transactions", {"limit": 20})
    conv.ok("list_recent_transactions", {"statuses": ["Declined"], "limit": 20})
    for hints in ({}, {"merchant": "Super Ahorro"}, {"merchant": "Kiosko Sur"}, {"amount": 15432.10}):
        conv.ok("find_candidate_transactions", {"purpose": "dispute", "hints": hints})
        conv.ok("find_candidate_transactions", {"purpose": "decline_inquiry", "hints": hints})
    conv.ok("get_case_status")
    foreign = fx.ids_of([fx.C2, fx.C3, fx.C4]) | {fx.T["notowned"]}
    text = json.dumps(bank.outputs)
    assert not [i for i in foreign if i in text], "an id of another customer reached the model"
    assert "15432.1" not in text and "Kiosko Sur" not in text


@pytest.mark.parametrize("bad", ["TRX-' OR 1=1 --", "TRX-AAAA'; DROP TABLE x;--", "trx-" + "a" * 21])
def test_injection_shaped_ids_fail_validation_without_echo(bank, bad):
    conv = bank.customer(fx.C1)
    env = conv.call("explain_decline", {"transaction_id": bad, "language": "es"})
    details = expect_error(env, "VALIDATION_ERROR", next_action="fix_arguments")
    assert any("transaction_id" in f.get("path", "") for f in details.get("fields", []))
    text = json.dumps(env)
    assert "OR 1=1" not in text and "DROP" not in text and bad.upper() not in text


@pytest.mark.parametrize("tool", hz.MODEL_TOOLS)
def test_a_customer_id_argument_is_rejected_by_every_tool(bank, tool):
    conv = bank.customer(fx.C1)
    if tool == "get_policy_info":
        args = {"topic": "scope", "language": "es"}
    elif tool == "handoff_to_human":
        args = hz.handoff_args("explicit_human_request")
    else:
        args = hz.valid_args(tool)
    details = expect_error(conv.call(tool, {**args, "customer_id": fx.C2}), "VALIDATION_ERROR")
    assert any(f.get("problem") == "unknown_field" for f in details.get("fields", []))


@pytest.mark.parametrize("customer", fx.RESTRICTED)
def test_restricted_customers_are_gated_to_a_handoff(bank, customer):
    conv = bank.customer(customer)
    overview = conv.ok("get_customer_overview")
    assert overview["customer_status"] == fx.CUSTOMERS[customer]["status"]
    assert overview["service_restriction"] == {"handoff_required": True, "handoff_reason": "customer_status_restricted"}
    for tool in hz.AUTH_TOOLS:
        if tool == "get_customer_overview":
            continue
        expect_error(conv.call(tool, hz.valid_args(tool, customer)), "POLICY_BLOCKED", reason="customer_status_restricted",
                     handoff_reason="customer_status_restricted", next_action="handoff")
    conv.ok("get_policy_info", {"topic": "human_agent", "language": "es"})
    ticket = conv.ok("handoff_to_human", hz.handoff_args("customer_status_restricted"))
    assert ticket["verified"] is True and ticket["identity_verified"] is True
    assert ticket["queue"] == "account_restrictions" and ticket["reason_check"] == "consistent"
    assert bank.cases(customer) == []


def test_active_customer_has_no_service_restriction(bank):
    overview = bank.customer(fx.C1).ok("get_customer_overview")
    assert overview["service_restriction"] == {"handoff_required": False, "handoff_reason": None}
