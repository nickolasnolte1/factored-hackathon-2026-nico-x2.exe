"""Grounded writes (CONTRACT §3.11-§3.13): prepare -> customer confirmation in a later turn -> create, with
signed confirmations bound to transaction and session, idempotency, policy blocks and verified read-back."""
import json
from datetime import datetime, timedelta

import pytest

from src.policy import dispute_policy as dp
from tests.bank_tools import fixture_data as fx
from tests.bank_tools import harness as hz
from tests.bank_tools.harness import expect_error, expect_ok

UNREC, INCORRECT = dp.DISPUTE_INTENTS
POL = hz.POLICY
CONFIRMATION_TTL = timedelta(seconds=600)


def _prepare(conv, key="super", intent=UNREC, language="es", compromise=None):
    args = {"transaction_id": fx.T[key], "intent": intent, "language": language}
    if compromise is not None:
        args["suspected_card_compromise"] = compromise
    return conv.ok("prepare_dispute_case", args)


def _create(conv, confirmation_id, key="super", confirmed=True, idem=None):
    return conv.call("create_dispute_case", {"confirmation_id": confirmation_id, "transaction_id": fx.T[key],
                                             "customer_confirmed": confirmed, "idempotency_key": idem or hz.new_key()})


def _confirmed_case(bank, conv, key="super", intent=UNREC, idem=None):
    draft = _prepare(conv, key, intent)
    conv.next_turn()
    bank.advance(82)
    return draft, expect_ok(_create(conv, draft["confirmation_id"], key, idem=idem))


@pytest.mark.parametrize("key,intent,language", [("super", UNREC, "es"), ("fee", INCORRECT, "pt"),
                                                 ("intl", UNREC, "es"), ("implausible", UNREC, "pt")])
def test_prepare_returns_facts_from_the_record_and_writes_nothing(bank, key, intent, language):
    conv = bank.customer(fx.C1)
    data = _prepare(conv, key, intent, language)
    hz.check_output("prepare_dispute_case", data)
    src, exp = fx.row(key), fx.expected_case(key, intent, language)
    product = fx.PRODUCT_BY_ID[src["product_id"]]
    facts = data["verified_facts"]
    assert facts["transaction_id"] == src["transaction_id"] and facts["product_id"] == src["product_id"]
    assert facts["product_type_en"] == product["product_type_en"]
    assert facts["number_last4"] == product["product_number_last4"]
    assert facts["transaction_type"] == src["transaction_type"] and facts["event_date"] == src["event_date"]
    assert facts["event_ts"] == src["event_ts"][:16]
    assert facts["amount"] == exp["amount"] and facts["currency"] == exp["currency"]
    assert facts["channel"] == dp.narratable_channel(src) and facts["is_international"] is src["is_international"]
    if src["merchant_name"] is None:
        assert facts["merchant"] is None
    else:
        assert facts["merchant"]["untrusted_text"] == src["merchant_name"]
    assert data["case_preview"] == {k: exp[k] for k in ("case_type", "dispute_type", "category", "subcategory",
                                                        "priority", "first_response_hours")}
    assert data["policy_decision"] == {"eligible": True, "handoff_required": False, "handoff_reason": None,
                                       "handoff_reasons_all": [], "next_action": "ask_customer_to_confirm_then_create"}
    assert data["customer_must_confirm"] is True
    assert data["confirmation_expires_at"] == hz.iso(fx.NOW + CONFIRMATION_TTL)
    assert _prepare(conv, key, intent, language)["confirmation_id"] == data["confirmation_id"], "same draft reused"
    assert bank.cases() == [] and conv.ok("get_case_status") == {"cases": []}


def test_case_is_created_after_confirmation_in_a_later_turn_and_read_back(bank):
    conv = bank.customer(fx.C1)
    found = conv.ok("find_candidate_transactions", {"purpose": "dispute", "intent": UNREC,
                                                    "hints": {"merchant": "Super Ahorro", "amount": 250000}})
    assert [c["transaction_id"] for c in found["candidates"]] == [fx.T["super"]]
    draft, case = _confirmed_case(bank, conv)
    hz.check_output("create_dispute_case", case)
    exp = fx.expected_case("super", UNREC)
    created = bank.now()
    assert case["verified"] is True and case["status"] == POL["case"]["initial_status"]
    assert case["replayed"] is False and case["already_existed"] is False
    assert case["created_at"] == hz.iso(created) and case["priority"] == exp["priority"]
    assert case["first_response_hours"] == exp["first_response_hours"]
    assert case["first_response_due_at"] == hz.iso(created + timedelta(hours=exp["first_response_hours"]))
    assert case["case"] == {k: exp[k] for k in ("case_type", "dispute_type", "category", "subcategory",
                                                "transaction_id", "product_id", "amount", "currency", "event_date")}
    stored = bank.repo.get_case(fx.C1, case["case_id"])
    assert {f: stored[f] for f in POL["case"]["required_fields"]} == {f: exp[f] for f in POL["case"]["required_fields"]}
    assert stored["status"] == "Open" and stored["created_via"] == POL["case"]["created_via"]
    assert stored["language"] == "es"
    view = conv.ok("get_case_status", {"case_id": case["case_id"]})
    hz.check_output("get_case_status", view)
    assert view["case"]["case_id"] == case["case_id"] and view["case"]["amount"] == exp["amount"]
    assert [c["case_id"] for c in conv.ok("get_case_status")["cases"]] == [case["case_id"]]
    assert bank.customer(fx.C2).ok("get_case_status") == {"cases": []}


def _tamper(confirmation_id):
    return confirmation_id[:-1] + ("A" if confirmation_id[-1] != "A" else "B")


@pytest.mark.parametrize("case", ["not_confirmed", "same_turn", "never_prepared", "tampered_signature",
                                  "other_transaction", "swapped_draft"])
def test_no_case_without_a_valid_confirmation(bank, case):
    conv = bank.customer(fx.C1)
    draft = _prepare(conv)
    other = _prepare(conv, "uber_a")
    if case != "same_turn":
        conv.next_turn()
    conf, key, confirmed, reason = draft["confirmation_id"], "super", True, "invalid_or_expired"
    if case == "not_confirmed":
        confirmed, reason = False, "not_confirmed"
    elif case == "same_turn":
        reason = "same_turn"
    elif case == "never_prepared":
        conf = "CNF-AAAAAAAAAAAA." + "A" * 22
    elif case == "tampered_signature":
        conf = _tamper(conf)
    elif case == "other_transaction":
        key, reason = "uber_a", "transaction_mismatch"
    elif case == "swapped_draft":
        conf = draft["confirmation_id"].split(".")[0] + "." + other["confirmation_id"].split(".")[1]
    expect_error(_create(conv, conf, key, confirmed), "CONFIRMATION_REQUIRED", reason=reason,
                 next_action="prepare_and_confirm")
    assert bank.cases() == []


def test_confirmation_is_bound_to_the_session_and_the_customer(bank):
    conv = bank.customer(fx.C1)
    draft = _prepare(conv)
    conv.next_turn()
    conv.token = bank.session(fx.C1)  # re-authenticated: a new session id
    expect_error(_create(conv, draft["confirmation_id"]), "CONFIRMATION_REQUIRED", reason="invalid_or_expired")
    intruder = bank.customer(fx.C2, conversation_id=conv.conversation_id, turn=conv.turn + 1)
    expect_error(_create(intruder, draft["confirmation_id"]), "CONFIRMATION_REQUIRED", reason="invalid_or_expired")
    assert bank.cases(fx.C1) == [] and bank.cases(fx.C2) == []


def test_confirmation_expires_and_a_new_one_works(bank):
    conv = bank.customer(fx.C1)
    draft = _prepare(conv)
    bank.advance(CONFIRMATION_TTL.seconds + 1)
    conv.next_turn()
    expect_error(_create(conv, draft["confirmation_id"]), "CONFIRMATION_REQUIRED", reason="invalid_or_expired")
    fresh = _prepare(conv)
    assert fresh["confirmation_id"] != draft["confirmation_id"]
    conv.next_turn()
    assert expect_ok(_create(conv, fresh["confirmation_id"]))["verified"] is True


def test_confirmation_expiry_is_capped_by_the_session(bank):
    token = bank.session(fx.C1, authenticated_at=fx.NOW - timedelta(minutes=14))
    draft = _prepare(bank.conv(token))
    assert draft["confirmation_expires_at"] == hz.iso(fx.NOW + timedelta(minutes=1))


def test_expired_session_creates_nothing_and_kills_its_drafts(bank):
    conv = bank.customer(fx.C1)
    draft = _prepare(conv)
    bank.advance(1020)  # the e2e expired_session offset
    conv.next_turn()
    expect_error(_create(conv, draft["confirmation_id"]), "SESSION_EXPIRED", next_action="reauthenticate")
    assert bank.cases() == []
    conv.token = bank.session(fx.C1)
    conv.next_turn()
    expect_error(_create(conv, draft["confirmation_id"]), "CONFIRMATION_REQUIRED", reason="invalid_or_expired")
    assert bank.cases() == []


def test_same_idempotency_key_replays_the_same_case(bank):
    conv = bank.customer(fx.C1)
    key = hz.new_key()
    draft, case = _confirmed_case(bank, conv, idem=key)
    replay = expect_ok(_create(conv, draft["confirmation_id"], idem=key))
    assert replay["replayed"] is True and replay["verified"] is True and replay["case_id"] == case["case_id"]
    assert len(bank.cases()) == 1


def test_idempotency_key_reused_for_another_request_is_rejected(bank):
    conv = bank.customer(fx.C1)
    key = hz.new_key()
    _confirmed_case(bank, conv, idem=key)
    other = _prepare(conv, "uber_a")
    conv.next_turn()
    expect_error(_create(conv, other["confirmation_id"], "uber_a", idem=key), "VALIDATION_ERROR",
                 reason="idempotency_key_reused")
    assert len(bank.cases()) == 1


def test_second_request_for_the_same_movement_returns_the_existing_case(bank):
    conv = bank.customer(fx.C1)
    first_draft, case = _confirmed_case(bank, conv)
    again = _prepare(conv)
    assert again["confirmation_id"] != first_draft["confirmation_id"], "a used draft is never reused"
    conv.next_turn()
    second = expect_ok(_create(conv, again["confirmation_id"]))
    assert second["already_existed"] is True and second["verified"] is True and second["case_id"] == case["case_id"]
    assert len(bank.cases()) == 1


@pytest.mark.parametrize("key,compromise,reason", [("big", None, "amount_above_threshold"),
                                                   ("old", None, "outside_dispute_window"),
                                                   ("super", True, "suspected_card_compromise")])
def test_policy_handoffs_block_case_creation(bank, key, compromise, reason):
    conv = bank.customer(fx.C1)
    data = _prepare(conv, key, compromise=compromise)
    src = fx.row(key)
    exp = fx.expected_case(key, UNREC, compromise=bool(compromise))
    all_reasons = dp.handoff_reasons({"customer_status": "Active", "suspected_card_compromise": bool(compromise),
                                      "transaction": src, "now": fx.NOW}, POL)
    decision = data["policy_decision"]
    assert decision["handoff_required"] is True and decision["handoff_reason"] == reason == all_reasons[0]
    assert decision["handoff_reasons_all"] == all_reasons
    assert decision["next_action"] == "ask_customer_to_confirm_then_handoff"
    assert data["case_preview"]["priority"] == exp["priority"]
    conv.next_turn()
    expect_error(_create(conv, data["confirmation_id"], key), "POLICY_BLOCKED", reason="handoff_required",
                 handoff_reason=reason, next_action="handoff", confirmation_id_usable_for_handoff=True)
    assert bank.cases() == []


def test_card_compromise_flag_makes_a_separate_draft(bank):
    conv = bank.customer(fx.C1)
    plain, flagged = _prepare(conv), _prepare(conv, compromise=True)
    assert plain["confirmation_id"] != flagged["confirmation_id"]
    assert flagged["case_preview"]["priority"] == "high"
    assert flagged["case_preview"]["first_response_hours"] == POL["priority"]["first_response_hours"]["high"]


def test_window_crossing_between_prepare_and_create_creates_nothing(make_bank):
    bank = make_bank(now=datetime(2026, 6, 19, 23, 55, 0))
    conv = bank.customer(fx.C1)
    data = _prepare(conv, "boundary")
    assert data["policy_decision"]["handoff_required"] is False
    bank.advance(420)  # 2026-06-20T00:02: the movement is now 91 days old (session and confirmation still valid)
    conv.next_turn()
    env = _create(conv, data["confirmation_id"], "boundary")
    assert env["ok"] is False and env["error"]["code"] in ("CONFIRMATION_REQUIRED", "POLICY_BLOCKED")
    assert bank.cases() == []


def test_changed_facts_at_write_time_are_rejected(make_bank, snapshot_path):
    state = {"stale": False}

    def alter(r):
        if r and state["stale"] and r.get("transaction_id") == fx.T["super"]:
            return {**r, "amount": round(r["amount"] + 1000.0, 2)}
        return r

    repo = hz.stub_repository(snapshot_path, {
        "get_transaction": lambda original, *a, **k: alter(original(*a, **k)),
        "list_transactions": lambda original, *a, **k: [alter(r) for r in original(*a, **k)]})
    bank = make_bank(repo=repo)
    conv = bank.customer(fx.C1)
    draft = _prepare(conv)
    state["stale"] = True
    conv.next_turn()
    expect_error(_create(conv, draft["confirmation_id"]), "CONFIRMATION_REQUIRED", reason="stale_facts")
    assert bank.cases() == []


@pytest.mark.parametrize("key,intent,reason,action", [
    ("decl54", UNREC, "status_declined", "explain_decline"), ("pending", UNREC, "status_pending", "offer_human"),
    ("reversed", UNREC, "status_reversed", "offer_human"), ("deposit", INCORRECT, "type_deposit", "offer_human"),
    ("fee", UNREC, "type_adjustment", "offer_human")])
def test_ineligible_movements_cannot_be_prepared(bank, key, intent, reason, action):
    conv = bank.customer(fx.C1)
    env = conv.call("prepare_dispute_case", {"transaction_id": fx.T[key], "intent": intent, "language": "es"})
    expect_error(env, "POLICY_BLOCKED", reason="not_eligible", eligibility_reason=reason, next_action=action)
    assert dp.is_eligible(fx.row(key), fx.C1, intent, POL) == (False, reason)


def test_failed_write_is_never_reported_as_created(make_bank):
    bank = make_bank(faults=[{"tool": "create_case", "type": "error", "failing_attempts": 99}])
    conv = bank.customer(fx.C1)
    draft = _prepare(conv)
    conv.next_turn()
    env = _create(conv, draft["confirmation_id"])
    expect_error(env, "UNAVAILABLE", write_state="not_written", handoff_reason="tool_failure", next_action="handoff",
                 attempts=1 + POL["handoff"]["tool_max_retries"])
    assert bank.audit_for(env)["attempts"].get("create_case") == 1 + POL["handoff"]["tool_max_retries"]
    assert bank.cases() == [] and conv.ok("get_case_status") == {"cases": []}
    ticket = conv.ok("handoff_to_human", hz.handoff_args(
        "tool_failure", hz.package(evidence=[env["meta"]["tool_call_id"]]), confirmation_id=draft["confirmation_id"]))
    assert ticket["verified"] is True and ticket["case_draft_attached"] is True
    assert ticket["draft_status"] == "pending_human_review" and ticket["reason_check"] == "consistent"
    assert ticket["dropped_evidence"] == []


def test_lost_write_is_never_verified(make_bank, snapshot_path):
    repo = hz.stub_repository(snapshot_path, {"insert_case": lambda original, *a, **k: None})
    bank = make_bank(repo=repo)
    conv = bank.customer(fx.C1)
    draft = _prepare(conv)
    conv.next_turn()
    env = _create(conv, draft["confirmation_id"])
    assert env["ok"] is False and env["error"]["code"] in ("INTERNAL", "UNAVAILABLE")
    assert env["error"]["details"].get("handoff_reason") == "tool_failure"
    assert bank.cases() == []


def test_read_back_mismatch_gives_internal_and_is_audited(make_bank, snapshot_path):
    def corrupt(original, *a, **k):
        r = original(*a, **k)
        return {**r, "amount": round(r["amount"] + 1.0, 2)} if r else r

    bank = make_bank(repo=hz.stub_repository(snapshot_path, {"get_case": corrupt}))
    conv = bank.customer(fx.C1)
    draft = _prepare(conv)
    conv.next_turn()
    env = _create(conv, draft["confirmation_id"])
    expect_error(env, "INTERNAL", reason="read_back_mismatch", handoff_reason="tool_failure", next_action="handoff")
    assert "read_back_mismatch" in json.dumps(bank.audit_for(env))


def test_create_takes_no_amount_currency_or_date(bank):
    conv = bank.customer(fx.C1)
    draft = _prepare(conv)
    conv.next_turn()
    for extra in ({"amount": 1.0}, {"currency": "USD"}, {"event_date": "2026-06-15"}):
        args = {"confirmation_id": draft["confirmation_id"], "transaction_id": fx.T["super"],
                "customer_confirmed": True, "idempotency_key": hz.new_key(), **extra}
        details = expect_error(conv.call("create_dispute_case", args), "VALIDATION_ERROR")
        assert any(f.get("problem") == "unknown_field" for f in details.get("fields", []))
    assert bank.cases() == []
