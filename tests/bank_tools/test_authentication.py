"""Identity and sessions (CONTRACT §2): document + one-time code, decoys, locking, token integrity, expiry."""
import json
import logging
from datetime import timedelta

import pytest

from tests.bank_tools import fixture_data as fx
from tests.bank_tools import harness as hz
from tests.bank_tools.harness import expect_error, expect_ok

TTL = timedelta(minutes=hz.POLICY["authentication"]["session_ttl_minutes"])
OTP = hz.POLICY["authentication"]["second_factor"]


def _start(conv, document=None, channel=None):
    doc_type, number = document or fx.CUSTOMERS[fx.C1]["document"]
    args = {"document_type": doc_type, "document_number": number}
    if channel:
        args["preferred_channel"] = channel
    return conv.call("start_authentication", args, caller="runtime")


def _wrong(code):
    return f"{(int(code) + 1) % 1000000:06d}"


@pytest.mark.parametrize("tool", hz.AUTH_TOOLS)
def test_no_session_means_no_data(bank, tool):
    """A customer number (or anything that is not a valid session) never authenticates: every data tool refuses."""
    conv = bank.conv(token=None)
    expect_error(conv.call(tool, hz.valid_args(tool)), "AUTH_REQUIRED", next_action="reauthenticate")
    for fake in (fx.C1, fx.CUSTOMERS[fx.C1]["document"][1], "S-1a2b3c4d", "bts1.e30.e30"):
        expect_error(conv.call(tool, hz.valid_args(tool), token=fake), "AUTH_REQUIRED")


def test_document_alone_does_not_authenticate(bank):
    conv = bank.conv()
    env, result = conv.call_full("start_authentication",
                                 {"document_type": "CC", "document_number": fx.CUSTOMERS[fx.C1]["document"][1]},
                                 caller="runtime")
    data = expect_ok(env)
    assert hz.runtime_token(result) is None, "a document alone must not yield a session"
    assert data["code_length"] == OTP["digits"] and data["max_attempts"] == OTP["max_attempts"]
    assert data["expires_at"] == hz.iso(fx.NOW + timedelta(seconds=OTP["ttl_seconds"]))
    expect_error(conv.call("get_customer_overview", {}, token=data["challenge_id"]), "AUTH_REQUIRED")
    expect_error(conv.call("get_customer_overview", {}), "AUTH_REQUIRED")


def test_document_and_code_authenticate(bank):
    conv = bank.conv()
    data = expect_ok(conv.call("start_authentication", {"document_type": "CC", "document_number": "1.000.000.001",
                                                        "preferred_channel": "app"}, caller="runtime"))
    assert data["delivery"] == {"channel": "app", "status": "sent"}
    code = bank.otp_code(data["challenge_id"])
    assert code, "the normalized document (dots removed, Gold document_hash twin) must reach the customer"
    env, result = conv.call_full("verify_otp", {"challenge_id": data["challenge_id"], "code": code}, caller="runtime")
    model_view = expect_ok(env)
    token = hz.runtime_token(result)
    bank.secrets |= {code, token}
    assert set(model_view) == {"authenticated", "session_ref", "expires_at", "ttl_minutes"}
    assert model_view["authenticated"] is True and model_view["ttl_minutes"] == TTL.seconds // 60
    assert model_view["expires_at"] == hz.iso(fx.NOW + TTL)
    assert token.startswith("bts1.") and token not in json.dumps(env)
    assert hz.token_payload(token)["amr"] == ["document", "otp"]
    conv.token = token
    overview = conv.ok("get_customer_overview")
    assert overview["session"]["session_ref"] == model_view["session_ref"]
    assert overview["session"]["expires_at"] == model_view["expires_at"]


def test_unknown_document_gets_an_identical_decoy(bank):
    real = _start(bank.conv())
    decoy = _start(bank.conv(), document=fx.UNKNOWN_DOCUMENT)
    a, b = expect_ok(real), expect_ok(decoy)
    assert set(a) == set(b) and a["delivery"] == b["delivery"]
    assert {k: v for k, v in a.items() if k != "challenge_id"} == {k: v for k, v in b.items() if k != "challenge_id"}
    assert bank.otp_code(b["challenge_id"]) is None, "nothing may be delivered for an unknown document"
    conv = bank.conv()
    env = conv.call("verify_otp", {"challenge_id": b["challenge_id"], "code": "123456"}, caller="runtime")
    expect_error(env, "AUTH_FAILED")


def test_three_wrong_codes_lock_the_challenge(bank):
    conv = bank.conv()
    challenge = expect_ok(_start(conv))["challenge_id"]
    code = bank.otp_code(challenge)
    bank.secrets.add(code)
    remaining = []
    for _ in range(OTP["max_attempts"]):
        env = conv.call("verify_otp", {"challenge_id": challenge, "code": _wrong(code)}, caller="runtime")
        details = expect_error(env, "AUTH_FAILED")
        remaining.append(details.get("attempts_remaining"))
    assert remaining[:-1] == list(range(OTP["max_attempts"] - 1, 0, -1))
    assert details.get("challenge_locked") is True and details["next_action"] == "restart_authentication"
    env, result = conv.call_full("verify_otp", {"challenge_id": challenge, "code": code}, caller="runtime")
    expect_error(env, "AUTH_FAILED", next_action="restart_authentication")
    assert hz.runtime_token(result) is None, "a locked challenge never issues a session, even with the right code"
    assert bank.login_otp(bank.conv()), "a new challenge works"


def test_code_is_bound_to_its_conversation_and_expires(bank):
    conv = bank.conv()
    challenge = expect_ok(_start(conv))["challenge_id"]
    code = bank.otp_code(challenge)
    bank.secrets.add(code)
    expect_error(bank.conv().call("verify_otp", {"challenge_id": challenge, "code": code}, caller="runtime"),
                 "AUTH_FAILED")
    bank.advance(OTP["ttl_seconds"] + 1)
    expect_error(conv.call("verify_otp", {"challenge_id": challenge, "code": code}, caller="runtime"),
                 "AUTH_FAILED", reason="challenge_expired", next_action="restart_authentication")


def test_code_and_document_never_reach_the_model_audit_or_logs(bank, caplog):
    caplog.set_level(logging.DEBUG)
    conv = bank.conv()
    bank.login_otp(conv)
    conv.ok("get_customer_overview")
    records = bank.audit_records()
    start = next(r for r in records if r.get("tool") == "start_authentication")
    verify = next(r for r in records if r.get("tool") == "verify_otp")
    assert start["args_redacted"]["document_number"] == "[REDACTED]"
    assert verify["args_redacted"]["code"] == "[REDACTED]"
    for secret in bank.secrets:
        assert not hz.contains_token(caplog.text, secret), "a secret reached the logs"
    # bank.assert_no_leaks() (run after the test) scans every output and audit record for the same values


@pytest.mark.parametrize("tool", ["start_authentication", "verify_otp"])
def test_runtime_auth_tools_are_forbidden_to_the_model(bank, tool):
    args = ({"document_type": "CC", "document_number": "1000000001"} if tool == "start_authentication"
            else {"challenge_id": "CHL-AAAAAAAAAAAA", "code": "123456"})
    expect_error(bank.conv().call(tool, args, caller="model"), "FORBIDDEN", next_action="refuse")


def test_model_auth_mode_still_keeps_the_token_in_the_runtime(make_bank):
    bank = make_bank(env={"BANK_TOOLS_MODEL_AUTH": "true"})
    conv = bank.conv()
    data = expect_ok(conv.call("start_authentication", {"document_type": "CC", "document_number": "1000000001"},
                               caller="model"))
    code = bank.otp_code(data["challenge_id"])
    env, result = conv.call_full("verify_otp", {"challenge_id": data["challenge_id"], "code": code}, caller="model")
    token = hz.runtime_token(result)
    bank.secrets |= {code, token}
    assert set(expect_ok(env)) == {"authenticated", "session_ref", "expires_at", "ttl_minutes"}


def test_tampered_tokens_are_rejected(bank):
    token = bank.session(fx.C1)
    prefix, payload, signature = token.split(".")
    flip = lambda s, i: s[:i] + ("A" if s[i] != "A" else "B") + s[i + 1:]
    tampered = [flip(token, len(prefix) + 3), token[:-1] + ("A" if token[-1] != "A" else "B"),
                f"{prefix}.{payload}.", f"bts2.{payload}.{signature}", f"{prefix}.{payload}", token + "x",
                f"{prefix}.{hz._b64e(json.dumps({**hz.token_payload(token), 'sub': fx.C2}).encode(), False)}.{signature}"]
    conv = bank.conv()
    for bad in tampered:
        expect_error(conv.call("get_customer_overview", {}, token=bad), "AUTH_REQUIRED")
    conv.token = token
    conv.ok("get_customer_overview")


def test_validly_signed_tokens_with_bad_claims_are_rejected(bank):
    token = bank.session(fx.C1)
    claims = hz.token_payload(token)
    control = hz.resign_token(token)  # same claims, re-encoded and re-signed: must be accepted
    if control is None:
        pytest.skip("cannot reproduce the token signature from the contract's key derivation")
    bank.secrets.add(control)
    conv = bank.conv()
    if conv.call("get_customer_overview", {}, token=control)["ok"] is not True:
        pytest.skip("re-encoded tokens are not accepted, so forged claims cannot be isolated")
    now = hz.epoch(fx.NOW)
    cases = [{"kid": "k9"}, {"v": 2}, {"amr": ["customer_number"]}, {"amr": ["document"]}, {"amr": []},
             {"iat": now + 120, "exp": now + 120 + TTL.seconds}]
    for change in cases:
        forged = hz.resign_token(token, **change)
        bank.secrets.add(forged)
        expect_error(conv.call("get_customer_overview", {}, token=forged), "AUTH_REQUIRED")
    expired = hz.resign_token(token, exp=now - 1, iat=claims["iat"])
    bank.secrets.add(expired)
    env = conv.call("get_customer_overview", {}, token=expired)
    assert env["ok"] is False and env["error"]["code"] in ("SESSION_EXPIRED", "AUTH_REQUIRED")


def test_session_expires_after_the_policy_ttl(bank):
    conv = bank.customer(fx.C1)
    conv.ok("get_customer_overview")
    bank.advance(TTL.seconds - 1)
    conv.ok("get_customer_overview")
    bank.advance(1)
    for tool in hz.AUTH_TOOLS:
        expect_error(conv.call(tool, hz.valid_args(tool)), "SESSION_EXPIRED", next_action="reauthenticate",
                     expired_at=hz.iso(fx.NOW + TTL))
    conv.ok("get_policy_info", {"topic": "session_expiry", "language": "es"})


def test_reauthentication_revokes_the_previous_session(bank):
    conv = bank.conv()
    first = bank.login_otp(conv)
    conv.ok("get_customer_overview")
    second = bank.login_otp(conv)
    assert second != first
    expect_error(conv.call("get_customer_overview", {}, token=first), "AUTH_REQUIRED")
    conv.ok("get_customer_overview", token=second)


def test_challenges_are_rate_limited_and_decoys_count(bank):
    conv = bank.conv()
    expect_ok(_start(conv))
    expect_ok(_start(conv, document=fx.UNKNOWN_DOCUMENT))
    expect_ok(_start(conv))
    details = expect_error(_start(conv, document=fx.UNKNOWN_DOCUMENT), "RATE_LIMITED", next_action="wait")
    assert details.get("retry_after_s", 1) > 0


def test_test_session_issuer_exists_only_in_test_environments(make_bank):
    bank = make_bank(env={"BANK_TOOLS_ENV": "dev"})
    with pytest.raises(Exception):
        bank.service.identity.issue_test_session(fx.C1)


def test_test_session_is_bound_to_the_customer(bank):
    """The trusted test session reproduces the scenario session: authenticated at `now`, 15-minute absolute TTL."""
    token = bank.session(fx.C1, authenticated_at=fx.NOW - timedelta(minutes=5))
    session = bank.conv(token).ok("get_customer_overview")["session"]
    assert session["authenticated_at"] == hz.iso(fx.NOW - timedelta(minutes=5))
    assert session["expires_at"] == hz.iso(fx.NOW + TTL - timedelta(minutes=5))
    assert session["minutes_left"] == 10
    issued = [r for r in bank.audit_records() if r.get("tool") == "_test_issue_session"]
    assert issued and issued[-1].get("caller") == "runtime"
