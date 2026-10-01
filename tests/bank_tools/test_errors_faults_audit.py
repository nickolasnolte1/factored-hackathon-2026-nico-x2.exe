"""Typed errors, bounded retries and fault injection, rate limits, configuration switches and the audit trail
(CONTRACT §4, §7, §8, §11)."""
import json
import logging
import re
import time
from pathlib import Path

import pytest

from src.policy import dispute_policy as dp
from tests.bank_tools import fixture_data as fx
from tests.bank_tools import harness as hz
from tests.bank_tools.harness import expect_error, expect_ok, without_meta

UNREC = dp.DISPUTE_INTENTS[0]
POL = hz.POLICY
MAX_ATTEMPTS = 1 + POL["handoff"]["tool_max_retries"]
BACKOFF = POL["handoff"]["tool_retry_backoff_seconds"]
AUDIT_KEYS = {"ts", "recorded_at", "trace_id", "conversation_id", "turn_index", "tool_call_id", "tool", "caller",
              "args_redacted", "outcome", "error_code", "internal_reason", "retryable", "attempts", "backoff_s",
              "latency_ms", "policy_decision", "result_summary", "session_id_hash", "customer_key", "auth_method",
              "security_events", "faults_injected", "warnings", "env", "repository", "service_version",
              "policy_version"}


# ---------------------------------------------------------------- validation and typed errors


def test_unknown_tool_is_a_validation_error(bank):
    expect_error(bank.customer(fx.C1).call("transfer_money", {"amount": 100}), "VALIDATION_ERROR")


def test_arguments_are_normalized_before_validation(bank):
    conv = bank.customer(fx.C1)
    data = conv.ok("get_balance", {"product_id": "  " + fx.P["savings"].lower() + " "})
    assert data["product_id"] == fx.P["savings"]
    assert conv.ok("explain_decline", {"transaction_id": fx.T["decl54"].lower(), "language": "es"})["applicable"]
    nulls = conv.ok("list_recent_transactions", {"product_id": None, "limit": None, "statuses": None,
                                                 "transaction_types": None, "date_from": None, "date_to": None})
    assert nulls["returned"] == POL["narration"]["recent_movements_count"]


@pytest.mark.parametrize("tool,args,problem", [
    ("get_balance", {}, "required"),
    ("get_balance", {"product_id": 123}, "type"),
    ("list_recent_transactions", {"limit": "five"}, "type"),
    ("list_recent_transactions", {"statuses": ["Refunded"]}, "enum"),
    ("find_candidate_transactions", {"purpose": "dispute", "hints": {"amount": "100"}}, "type"),
    ("find_candidate_transactions", {"purpose": "dispute"}, "required"),
    ("create_dispute_case", {"confirmation_id": "CNF-AAAAAAAAAAAA." + "A" * 22, "transaction_id": fx.T["super"],
                             "customer_confirmed": "yes", "idempotency_key": "idem-0001"}, "type"),
    ("create_dispute_case", {"confirmation_id": "CNF-AAAAAAAAAAAA." + "A" * 22, "transaction_id": fx.T["super"],
                             "customer_confirmed": True, "idempotency_key": "short"}, "pattern"),
    ("get_policy_info", {"topic": "loans", "language": "es"}, "enum"),
])
def test_invalid_arguments_are_typed_validation_errors(bank, tool, args, problem):
    details = expect_error(bank.customer(fx.C1).call(tool, args), "VALIDATION_ERROR", next_action="fix_arguments")
    fields = details.get("fields") or []
    assert fields and all(set(f) >= {"path", "problem"} for f in fields)
    assert problem in {f["problem"] for f in fields}


# ---------------------------------------------------------------- rate limits


def test_policy_info_is_rate_limited_per_conversation(bank):
    conv = bank.conv(token=None)
    for _ in range(20):
        conv.ok("get_policy_info", {"topic": "scope", "language": "es"})
    details = expect_error(conv.call("get_policy_info", {"topic": "scope", "language": "es"}), "RATE_LIMITED",
                           next_action="wait")
    assert details.get("retry_after_s", 1) > 0
    bank.conv(token=None).ok("get_policy_info", {"topic": "scope", "language": "es"})


def test_session_calls_are_rate_limited_on_the_service_clock(bank):
    conv = bank.customer(fx.C1)
    for _ in range(40):
        conv.ok("list_products")
    expect_error(conv.call("list_products", {}), "RATE_LIMITED")
    bank.advance(301)
    conv.ok("list_products")


# ---------------------------------------------------------------- retries and fault injection


@pytest.mark.parametrize("failing", [1, 2, 99])
def test_transient_failures_are_retried_within_the_policy_budget(make_bank, failing):
    faults = [{"tool": "get_transactions", "type": "timeout", "failing_attempts": failing}]
    bank = make_bank(faults=faults)
    conv = bank.customer(fx.C1)
    started = time.monotonic()
    env = conv.call("list_recent_transactions", {})
    assert time.monotonic() - started < 2.0, "test and eval environments never sleep for real"
    ok, attempts, _ = dp.tool_call("get_transactions", faults, POL)
    record = bank.audit_for(env)
    assert record["attempts"].get("get_transactions") == attempts
    assert record.get("faults_injected"), "every injected fault is listed in the audit"
    assert record.get("backoff_s") == sum(BACKOFF[:attempts - 1])
    if ok:
        expect_ok(env)
    else:
        expect_error(env, "UNAVAILABLE", attempts=MAX_ATTEMPTS, handoff_reason="tool_failure", next_action="handoff")


def test_unavailable_hits_exactly_the_nth_call(make_bank):
    bank = make_bank(faults=[{"tool": "get_products", "type": "unavailable", "on_attempts": [2]}])
    conv = bank.customer(fx.C1)
    assert bank.audit_for(conv.call("list_products"))["attempts"].get("get_products") == 1
    env = conv.call("get_balance", {"product_id": fx.P["savings"]})
    expect_ok(env)
    assert bank.audit_for(env)["attempts"].get("get_products") == 2
    assert bank.audit_for(conv.call("list_products"))["attempts"].get("get_products") == 1

    bank = make_bank(faults=[{"tool": "get_products", "type": "unavailable", "on_attempts": [2, 3, 4]}])
    conv = bank.customer(fx.C1)
    conv.ok("list_products")
    expect_error(conv.call("get_balance", {"product_id": fx.P["savings"]}), "UNAVAILABLE", attempts=MAX_ATTEMPTS,
                 next_action="handoff")
    conv.ok("list_products")


@pytest.mark.parametrize("tool,op", [
    ("get_customer_overview", "get_products"), ("list_products", "get_products"), ("get_balance", "get_products"),
    ("list_recent_transactions", "get_transactions"), ("find_candidate_transactions", "get_transactions"),
    ("explain_decline", "get_transactions"), ("check_dispute_eligibility", "get_transactions"),
    ("prepare_dispute_case", "get_transactions"), ("get_case_status", "get_case")])
def test_every_read_tool_fails_safe_after_retries(make_bank, tool, op):
    bank = make_bank(faults=[{"tool": op, "type": "error", "failing_attempts": 99}])
    env = bank.customer(fx.C1).call(tool, hz.valid_args(tool))
    expect_error(env, "UNAVAILABLE", attempts=MAX_ATTEMPTS, handoff_reason="tool_failure", next_action="handoff")
    assert bank.audit_for(env)["attempts"].get(op) == MAX_ATTEMPTS


def test_malformed_rows_are_excluded_from_lists_and_fail_direct_fetches(make_bank):
    bank = make_bank(faults=[{"tool": "get_transactions", "type": "malformed", "transaction_id": fx.T["super"],
                              "field": "amount", "value": None}])
    conv = bank.customer(fx.C1)
    env = conv.call("list_recent_transactions", {"limit": 20})
    ids = [t["transaction_id"] for t in expect_ok(env)["transactions"]]
    assert fx.T["super"] not in ids and fx.T["uber_a"] in ids
    assert "malformed_rows_excluded:1" in env["warnings"]
    assert "malformed_rows_excluded:1" in (bank.audit_for(env).get("warnings") or [])
    expect_error(conv.call("prepare_dispute_case", {"transaction_id": fx.T["super"], "intent": UNREC, "language": "es"}),
                 "INTERNAL", reason="malformed_record", handoff_reason="tool_failure", next_action="handoff")


def test_fault_injection_is_refused_outside_test_and_eval(make_bank):
    faults = [{"tool": "get_transactions", "type": "timeout", "failing_attempts": 1}]
    make_bank(faults=faults, env={"BANK_TOOLS_ENV": "eval"})
    with pytest.raises(Exception):
        make_bank(faults=faults, env={"BANK_TOOLS_ENV": "dev"})


# ---------------------------------------------------------------- configuration switches


def test_read_only_mode_forbids_case_creation_but_keeps_handoff(make_bank):
    bank = make_bank(env={"BANK_TOOLS_READ_ONLY": "true"})
    conv = bank.customer(fx.C1)
    draft = conv.ok("prepare_dispute_case", {"transaction_id": fx.T["super"], "intent": UNREC, "language": "es"})
    conv.next_turn()
    expect_error(conv.call("create_dispute_case", {"confirmation_id": draft["confirmation_id"],
                                                   "transaction_id": fx.T["super"], "customer_confirmed": True,
                                                   "idempotency_key": hz.new_key()}), "FORBIDDEN", next_action="refuse")
    assert bank.cases() == []
    assert conv.ok("handoff_to_human", hz.handoff_args("explicit_human_request",
                                                       confirmation_id=draft["confirmation_id"]))["verified"] is True


def test_direct_methods_follow_the_same_pipeline(bank):
    token = bank.session(fx.C1)
    conv = bank.conv(token)
    via_dispatch = conv.call("get_balance", {"product_id": fx.P["credit"]})
    before = len(bank.audit_records())
    direct = bank.service.get_balance(token, conv.ctx(), product_id=fx.P["credit"]).for_model()
    assert without_meta(direct) == without_meta(via_dispatch)
    invalid = bank.service.get_balance(token, conv.ctx(), product_id="PRD-bad").for_model()
    expect_error(invalid, "VALIDATION_ERROR")
    unauthenticated = bank.service.get_balance(None, conv.ctx(), product_id=fx.P["credit"]).for_model()
    expect_error(unauthenticated, "AUTH_REQUIRED")
    for env in (direct, invalid, unauthenticated):
        hz.check_envelope(env, "get_balance", bank.now())
    bank.outputs += [direct, invalid, unauthenticated]
    assert len(bank.audit_records()) == before + 3, "direct calls are audited like call_tool"


# ---------------------------------------------------------------- audit


def test_exactly_one_audit_record_per_call_including_rejections(bank):
    token = bank.session(fx.C1)
    conv = bank.conv(token)
    calls = [
        ("get_customer_overview", {}, {}),
        ("get_balance", {"product_id": "nope"}, {}),
        ("no_such_tool", {}, {}),
        ("get_customer_overview", {}, {"token": None}),
        ("start_authentication", {"document_type": "CC", "document_number": "1000000001"}, {}),
        ("get_balance", {"product_id": "PRD-ZZZZZZZZZZZZ"}, {}),
        ("prepare_dispute_case", {"transaction_id": fx.T["decl54"], "intent": UNREC, "language": "es"}, {}),
        ("get_policy_info", {"topic": "privacy", "language": "pt"}, {"token": None}),
        ("handoff_to_human", hz.handoff_args("explicit_human_request"), {}),
    ]
    ids = set()
    for tool, args, kw in calls:
        before = len(bank.audit_records())
        env = conv.call(tool, args, **kw)
        records = bank.audit_records()
        assert len(records) == before + 1, f"{tool}: expected exactly one audit record"
        record = records[-1]
        assert record["tool_call_id"] == env["meta"]["tool_call_id"]
        assert re.fullmatch(r"tc_[0-9a-f]{16}", record["tool_call_id"])
        assert (record["outcome"] == "ok") is env["ok"]
        assert record.get("error_code") == (None if env["ok"] else env["error"]["code"])
        ids.add(record["tool_call_id"])
    assert len(ids) == len(calls), "tool_call_id is unique per call"


def test_audit_record_fields_link_calls_without_storing_identities(bank):
    conv_a = bank.customer(fx.C1)
    a1 = bank.audit_for(conv_a.call("list_products"))
    a2 = bank.audit_for(conv_a.call("find_candidate_transactions", {"purpose": "dispute",
                                                                    "hints": {"merchant": "Super Ahorro"}}))
    b1 = bank.audit_for(bank.customer(fx.C2).call("list_products"))
    for record in (a1, a2, b1):
        missing = AUDIT_KEYS - set(record)
        assert not missing, f"audit record lacks {missing}"
        assert record["policy_version"] == POL["version"] and record["env"] == "test"
        assert record["repository"] == "local" and record["caller"] == "model"
        assert record["ts"] == hz.iso(fx.NOW) and isinstance(record["latency_ms"], (int, float))
        assert re.fullmatch(r"[0-9a-f]{16}", record["session_id_hash"])
        assert re.fullmatch(r"[0-9a-f]{16}", record["customer_key"])
        assert record["auth_method"] == "test_issuer"
    assert a1["trace_id"] == conv_a.trace_id and a1["conversation_id"] == conv_a.conversation_id
    assert a1["turn_index"] == conv_a.turn
    assert a1["customer_key"] == a2["customer_key"] != b1["customer_key"]
    assert a1["session_id_hash"] == a2["session_id_hash"] != b1["session_id_hash"]
    summary = json.dumps(a2["result_summary"])
    assert "Super Ahorro" not in summary and "250000" not in summary, "result_summary holds ids, counts and flags"
    assert a2["policy_decision"].get("match_status") == "unique"


def test_audit_redacts_arguments(bank, caplog):
    caplog.set_level(logging.DEBUG)
    conv = bank.conv()
    bank.login_otp(conv)
    draft = conv.ok("prepare_dispute_case", {"transaction_id": fx.T["super"], "intent": UNREC, "language": "es"})
    conv.next_turn()
    key = hz.new_key()
    create_env = conv.call("create_dispute_case", {"confirmation_id": draft["confirmation_id"],
                                                   "transaction_id": fx.T["super"], "customer_confirmed": True,
                                                   "idempotency_key": key})
    email = "maria.prueba@example.com"
    bank.secrets |= {email, key}
    long_note = "Detalle " * 60
    handoff_env = conv.call("handoff_to_human", hz.handoff_args("explicit_human_request", hz.package(
        f"Pide asesor y deja su correo {email}.", questions=[long_note[:200]])))
    expect_ok(handoff_env)
    create_args = bank.audit_for(create_env)["args_redacted"]
    assert create_args["idempotency_key"] != key
    signature = draft["confirmation_id"].split(".")[1]
    assert signature not in json.dumps(create_args), "confirmation signatures are cut in the audit"
    handoff_args = bank.audit_for(handoff_env)["args_redacted"]
    for _, text in hz.iter_strings(handoff_args):
        assert len(text) <= 200 and email not in text
    for secret in bank.secrets:
        assert not hz.contains_token(caplog.text, secret), "a secret reached the logs"


def test_jsonl_audit_lines_are_valid_and_redacted(make_bank, tmp_path):
    impl = hz.impl()
    if impl.JsonlAuditSink is None:
        pytest.skip("JsonlAuditSink is not implemented")
    audit_dir = tmp_path / "jsonl_audit"
    sink = None
    for make in (lambda: impl.JsonlAuditSink(str(audit_dir)), lambda: impl.JsonlAuditSink(directory=str(audit_dir)),
                 lambda: impl.JsonlAuditSink(path=str(audit_dir)), lambda: impl.JsonlAuditSink()):
        try:
            sink = make()  # the last shape reads BANK_TOOLS_AUDIT_DIR (tmp_path/audit)
            break
        except TypeError:
            continue
    if sink is None:
        pytest.skip("JsonlAuditSink constructor shape not recognized")
    bank = make_bank(audit_sink=sink)
    conv = bank.conv()
    bank.login_otp(conv)
    conv.ok("get_customer_overview")
    expect_error(conv.call("get_balance", {"product_id": "nope"}), "VALIDATION_ERROR")
    files = sorted(Path(audit_dir).rglob("*.jsonl")) or sorted(Path(tmp_path).rglob("tool_audit_*.jsonl"))
    assert files, "the JSONL sink writes tool_audit_<YYYYMMDD>.jsonl"
    lines = [line for f in files for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) >= 4
    text = "\n".join(lines)
    for line in lines:
        record = json.loads(line)
        assert record.get("trace_id") and record.get("policy_version") == POL["version"] and "latency_ms" in record
    assert "CLI-" not in text
    for secret in bank.secrets:
        assert not hz.contains_token(text, secret)
