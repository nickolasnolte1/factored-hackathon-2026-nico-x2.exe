"""Harness for the bank tool service tests, written from `src/bank_tools/CONTRACT.md` (v1.0.0) only.

The contract fixes the surface used here: `BankService(repository, clock, audit, faults, config)`,
`call_tool(name, args, session_token, context)`, `ToolResult.for_model()` / `.runtime`, `ToolContext`,
`LocalRepository(snapshot_path, store_path)`, `FixedClock`, `ListAuditSink`, `FaultInjector` / `NullFaultInjector`,
`service.identity.issue_test_session` and the test outbox (`BANK_TOOLS_OTP_OUTBOX_FILE`).

Where the contract leaves a construction detail open (the config object, the module that defines ToolContext, how
the outbox and the list sink expose their contents), it is resolved here, in one place, with documented fallbacks.
Test modules use only this harness and the tool interface, so they stay an independent check of the contract.
"""
import base64
import hashlib
import hmac
import importlib
import json
import os
import re
import secrets
import types
from datetime import datetime
from pathlib import Path

import pytest

from src.gold import gold_lib
from src.policy import dispute_policy as dp
from tests.bank_tools import fixture_data as fx

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = json.loads((REPO_ROOT / "src" / "bank_tools" / "tool_schemas.json").read_text(encoding="utf-8"))
POLICY = dp.load_policy()
TOOLS = {t["name"]: t for t in SCHEMAS["tools"]}
ERRORS = SCHEMAS["errors"]
MODEL_TOOLS = [n for n, t in TOOLS.items() if t["exposure"] == "model"]
AUTH_TOOLS = [n for n, t in TOOLS.items() if t["auth"] == "required"]
HANDOFF_REASONS = [t["reason"] for t in POLICY["handoff"]["triggers_in_order"]] + ["complaint_routing"]
SERVICE_FILE = REPO_ROOT / "src" / "bank_tools" / "service.py"

# TEST ONLY keys (never real secrets). The session key is also used to check token signatures in the forgery tests.
TEST_SESSION_KEY = "test-only-bank-tools-session-key-0123456789abcdef"
TEST_OTP_KEY = "test-only-bank-tools-otp-hash-key-0123456789abcdef"

# Error `details` whitelist per code (CONTRACT §4).
ERROR_DETAIL_KEYS = {
    "AUTH_REQUIRED": {"next_action"},
    "AUTH_FAILED": {"reason", "attempts_remaining", "challenge_locked", "next_action"},
    "SESSION_EXPIRED": {"expired_at", "next_action"},
    "FORBIDDEN": {"next_action"},
    "NOT_FOUND": {"resource", "next_action"},
    "VALIDATION_ERROR": {"fields", "reason", "next_action"},
    "CONFIRMATION_REQUIRED": {"reason", "next_action"},
    "POLICY_BLOCKED": {"reason", "handoff_reason", "eligibility_reason", "confirmation_id_usable_for_handoff",
                       "next_action"},
    "RATE_LIMITED": {"retry_after_s", "next_action"},
    "UNAVAILABLE": {"attempts", "write_state", "handoff_reason", "next_action"},
    "INTERNAL": {"reason", "handoff_reason", "next_action"},
}

# Strings the model may legitimately see that look like numbers or codes: service ids (CONTRACT §3.1).
SERVICE_ID = re.compile(r"^(TRX-[A-Z0-9]{20}|PRD-[A-Z0-9]{12}|DSP-[A-Z0-9]{12}|HND-[A-Z0-9]{12}|CHL-[A-Z0-9]{12}"
                        r"|CNF-[A-Z0-9]{12}\.[A-Za-z0-9_-]{22}|tc_[0-9a-f]{16}|S-[0-9a-f]{8})$")
PII_VALUE_PATTERNS = {k: re.compile(v) for k, v in gold_lib.load_spec()["privacy"]["value_patterns"].items()}


def service_env(tmp_path, snapshot_path):
    """Environment of a test service (CONTRACT §11)."""
    return {
        "BANK_TOOLS_ENV": "test",
        "BANK_TOOLS_SESSION_KEY": TEST_SESSION_KEY,
        "BANK_TOOLS_SESSION_KEY_PREVIOUS": "",
        "BANK_TOOLS_OTP_KEY": TEST_OTP_KEY,
        "BANK_TOOLS_REPOSITORY": "local",
        "BANK_TOOLS_SNAPSHOT": str(snapshot_path),
        "BANK_TOOLS_STATE_PATH": "",
        "BANK_TOOLS_CLOCK": fx.NOW.isoformat(),
        "BANK_TOOLS_CONFIRMATION_TTL_S": "600",
        "BANK_TOOLS_MODEL_AUTH": "false",
        "BANK_TOOLS_READ_ONLY": "false",
        "BANK_TOOLS_AUDIT_DIR": str(Path(tmp_path) / "audit"),
        "BANK_TOOLS_OTP_OUTBOX_FILE": str(Path(tmp_path) / "otp_outbox.jsonl"),
    }


def require_impl():
    if not SERVICE_FILE.exists():
        pytest.skip("bank tool service not implemented yet (src/bank_tools/service.py is missing)")


def iso(dt):
    return dt.isoformat(timespec="seconds")


# --------------------------------------------------------------------------------------------------------------
# Implementation lookup (the only place that knows module paths beyond the contract's layout table)


def _module(name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name and name.startswith(exc.name):
            return None
        raise


def _lookup(attr, modules):
    for name in modules:
        mod = _module(name)
        if mod is not None and hasattr(mod, attr):
            return getattr(mod, attr)
    return None


class Impl:
    """Classes named in CONTRACT §1.2, imported from the documented module layout."""

    def __init__(self):
        self.BankService = importlib.import_module("src.bank_tools.service").BankService
        self.LocalRepository = importlib.import_module("src.bank_tools.repository.local").LocalRepository
        self.FixedClock = importlib.import_module("src.bank_tools.clock").FixedClock
        audit = importlib.import_module("src.bank_tools.audit")
        self.ListAuditSink = audit.ListAuditSink
        self.JsonlAuditSink = getattr(audit, "JsonlAuditSink", None)
        faults = importlib.import_module("src.bank_tools.faults")
        self.FaultInjector, self.NullFaultInjector = faults.FaultInjector, faults.NullFaultInjector
        self.ToolContext = _lookup("ToolContext", ["src.bank_tools.service", "src.bank_tools",
                                                   "src.bank_tools.context", "src.bank_tools.types"])
        self.build_service = _lookup("build_service", ["src.bank_tools"])

    def config(self):
        """The config object for BankService, built from the environment (CONTRACT §11)."""
        modules = ["src.bank_tools.config", "src.bank_tools", "src.bank_tools.service", "src.bank_tools.settings"]
        for name in ("load_config", "config_from_env", "read_config", "get_config"):
            fn = _lookup(name, modules)
            if callable(fn) and not isinstance(fn, type):
                try:
                    return fn()
                except TypeError:
                    pass
        for name in ("ServiceConfig", "Config", "BankToolsConfig", "Settings"):
            cls = _lookup(name, modules)
            if isinstance(cls, type):
                for ctor in ("from_env", "from_environment", "load"):
                    if callable(getattr(cls, ctor, None)):
                        return getattr(cls, ctor)()
                try:
                    return cls()
                except TypeError:
                    pass
        if callable(self.build_service):
            svc = self.build_service()
            for attr in ("config", "_config", "cfg", "settings"):
                if getattr(svc, attr, None) is not None:
                    return getattr(svc, attr)
        return None

    def ctx(self, conversation_id, turn_index, trace_id, caller):
        kw = {"conversation_id": conversation_id, "turn_index": turn_index, "trace_id": trace_id, "caller": caller}
        return self.ToolContext(**kw) if self.ToolContext is not None else types.SimpleNamespace(**kw)


_IMPL = None


def impl():
    global _IMPL
    require_impl()
    if _IMPL is None:
        _IMPL = Impl()
    return _IMPL


def stub_repository(snapshot_path, hooks):
    """A LocalRepository subclass whose methods in `hooks` go through hook(call_original, *args, **kwargs).
    This is the "test-only repository stub" of CONTRACT §12.7."""
    base = impl().LocalRepository
    attrs = {}
    for name, hook in hooks.items():
        def method(self, *args, _name=name, _hook=hook, **kwargs):
            original = getattr(super(stub_cls, self), _name)
            return _hook(original, *args, **kwargs)
        attrs[name] = method
    stub_cls = type("StubRepository", (base,), attrs)
    return stub_cls(str(snapshot_path), store_path=":memory:")


# --------------------------------------------------------------------------------------------------------------
# A service under test and a conversation driving it


class Bank:
    """One service instance over a fixture snapshot, with its clock, audit sink and fault injector."""

    def __init__(self, snapshot_path, now=fx.NOW, faults=None, repo=None, audit_sink=None):
        self.impl = impl()
        self.snapshot_path = snapshot_path
        self.clock = self.impl.FixedClock(now)
        self.repo = repo if repo is not None else self.impl.LocalRepository(str(snapshot_path), store_path=":memory:")
        self.audit = audit_sink if audit_sink is not None else self.impl.ListAuditSink()
        self.faults = self.impl.FaultInjector(faults) if faults is not None else self.impl.NullFaultInjector()
        self.service = self.impl.BankService(self.repo, self.clock, self.audit, self.faults, self.impl.config())
        self.outputs = []  # every for_model() envelope returned in this test
        self.secrets = set()  # values that must never reach the model or the audit
        for c in fx.CUSTOMERS.values():
            doc_type, number = c["document"]
            self.secrets |= {number, gold_lib.document_hash(doc_type, number)}
        self.secrets |= {fx.UNKNOWN_DOCUMENT[1], gold_lib.document_hash(*fx.UNKNOWN_DOCUMENT)}

    # clock
    def now(self):
        return self.clock.now()

    def advance(self, seconds):
        self.clock.advance(seconds)

    def set_now(self, dt):
        self.clock.set(dt)

    # sessions
    def session(self, customer_id=fx.C1, authenticated_at=None):
        token = self.service.identity.issue_test_session(customer_id, authenticated_at=authenticated_at)
        assert isinstance(token, str) and token.startswith("bts1."), "issue_test_session must return a bts1 token"
        self.secrets.add(token)
        return token

    def conv(self, token=None, conversation_id=None, turn=1):
        return Conversation(self, token, conversation_id, turn)

    def customer(self, customer_id=fx.C1, **kw):
        """A conversation with a fresh test session for the customer (the e2e harness's trusted session)."""
        return self.conv(self.session(customer_id), **kw)

    def login_otp(self, conv, customer_id=fx.C1, document=None):
        """Document + one-time code through the runtime tools; sets and returns the runtime's session token."""
        doc_type, number = document or fx.CUSTOMERS[customer_id]["document"]
        data = expect_ok(conv.call("start_authentication", {"document_type": doc_type, "document_number": number},
                                   caller="runtime"))
        code = self.otp_code(data["challenge_id"])
        assert code, "the test outbox must hold the code of a real challenge"
        env, res = conv.call_full("verify_otp", {"challenge_id": data["challenge_id"], "code": code}, caller="runtime")
        expect_ok(env)
        token = runtime_token(res)
        assert token, "verify_otp must hand the session token to the runtime"
        self.secrets |= {code, token}
        conv.token = token
        return token

    # test outbox (CONTRACT §2.2: codes go only to the TestOutbox, optionally mirrored to BANK_TOOLS_OTP_OUTBOX_FILE)
    def otp_code(self, challenge_id):
        path = os.environ.get("BANK_TOOLS_OTP_OUTBOX_FILE")
        if path and os.path.exists(path):
            code = _code_from_text(Path(path).read_text(encoding="utf-8"), challenge_id)
            if code:
                return code
        return _search_code(self.service, challenge_id, 0, set())

    # audit
    def audit_records(self):
        sink = self.audit
        for attr in ("records", "items", "entries", "rows", "lines"):
            value = getattr(sink, attr, None)
            if value is not None:
                records = value() if callable(value) else value
                break
        else:
            try:
                records = list(sink)
            except TypeError:
                return []
        return [_as_dict(r) for r in records]

    def last_audit(self):
        records = self.audit_records()
        assert records, "the audit sink holds no record"
        return records[-1]

    def audit_for(self, env):
        tc = env["meta"]["tool_call_id"]
        found = [r for r in self.audit_records() if r.get("tool_call_id") == tc]
        assert len(found) == 1, f"expected one audit record for {tc}, found {len(found)}"
        return found[0]

    # store (read through the public Repository interface, CONTRACT §1.4)
    def cases(self, customer_id=fx.C1):
        return list(self.repo.list_cases(customer_id, 50) or [])

    def ticket(self, ticket_id):
        return self.repo.get_ticket(ticket_id)

    # leak guard (run after every test that built a Bank)
    def assert_no_leaks(self):
        model_text = json.dumps(self.outputs, ensure_ascii=False, default=str)
        audit_text = json.dumps(self.audit_records(), ensure_ascii=False, default=str)
        for label, text in (("model output", model_text), ("audit", audit_text)):
            assert "CLI-" not in text, f"a customer id reached the {label}"
            for value in self.secrets:
                assert not contains_token(text, value), f"a secret value reached the {label}"


class Conversation:
    """A chat conversation as the runtime sees it: session token, conversation id, turn index and trace id."""

    def __init__(self, bank, token=None, conversation_id=None, turn=1):
        self.bank = bank
        self.token = token
        self.conversation_id = conversation_id or "c-" + secrets.token_hex(6)
        self.turn = turn
        self.trace_id = secrets.token_hex(16)

    def next_turn(self, n=1):
        self.turn += n
        self.trace_id = secrets.token_hex(16)
        return self

    def ctx(self, caller="model"):
        return self.bank.impl.ctx(self.conversation_id, self.turn, self.trace_id, caller)

    def call_full(self, tool, args=None, caller="model", token=...):
        result = self.bank.service.call_tool(tool, dict(args or {}), self.token if token is ... else token,
                                             self.ctx(caller))
        env = result.for_model()
        check_envelope(env, tool, self.bank.now())
        self.bank.outputs.append(env)
        return env, result

    def call(self, tool, args=None, caller="model", token=...):
        return self.call_full(tool, args, caller, token)[0]

    def ok(self, tool, args=None, **kw):
        return expect_ok(self.call(tool, args, **kw))


def runtime_token(result):
    runtime = getattr(result, "runtime", None)
    if isinstance(runtime, dict):
        return runtime.get("session_token")
    return getattr(runtime, "session_token", None)


def _as_dict(record):
    if isinstance(record, dict):
        return record
    if isinstance(record, str):
        return json.loads(record)
    if hasattr(record, "to_dict"):
        return record.to_dict()
    if hasattr(record, "_asdict"):
        return record._asdict()
    return dict(vars(record))


_CODE = re.compile(r"(?<![0-9])[0-9]{6}(?![0-9])")


def _code_from_text(text, challenge_id):
    for line in text.splitlines():
        if challenge_id not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            for key in ("code", "otp", "one_time_code"):
                if isinstance(obj.get(key), str) and _CODE.fullmatch(obj[key]):
                    return obj[key]
        match = _CODE.search(line.replace(challenge_id, ""))
        if match:
            return match.group(0)
    return None


def _six_digits(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9]{6}", value))


def _search_code(obj, challenge_id, depth, seen):
    """Find the code delivered for `challenge_id` inside the service's in-memory TestOutbox (fallback when the outbox
    file is not written). Only objects of the bank_tools package and plain containers are walked."""
    if depth > 6 or id(obj) in seen:
        return None
    seen.add(id(obj))
    if isinstance(obj, dict):
        if challenge_id in obj:
            value = obj[challenge_id]
            if _six_digits(value):
                return value
            found = _search_code(value, challenge_id, depth + 1, seen) if not isinstance(value, str) else None
            if found:
                return found
            if isinstance(value, dict):
                return next((v for v in value.values() if _six_digits(v)), None)
        if challenge_id in obj.values():
            if _six_digits(obj.get("code")):
                return obj["code"]
            return next((v for v in obj.values() if _six_digits(v)), None)
        items = obj.values()
    elif isinstance(obj, (list, tuple, set)):
        if challenge_id in obj:
            return next((v for v in obj if _six_digits(v)), None)
        items = obj
    elif type(obj).__module__.startswith("src.bank_tools"):
        attrs = getattr(obj, "__dict__", None) or {s: getattr(obj, s, None) for s in getattr(obj, "__slots__", ())}
        if attrs.get("challenge_id") == challenge_id and _six_digits(attrs.get("code")):
            return attrs["code"]
        items = attrs.values()
    else:
        return None
    for item in list(items):
        found = _search_code(item, challenge_id, depth + 1, seen)
        if found:
            return found
    return None


# --------------------------------------------------------------------------------------------------------------
# Envelope, errors and output schemas


def check_envelope(env, tool, now):
    errors = validate(env, SCHEMAS["envelope"])
    assert not errors, f"{tool}: envelope violates tool_schemas.json: {errors[:5]}"
    assert env["ok"] is ("data" in env) and env["ok"] is not ("error" in env), f"{tool}: data/error mismatch"
    assert env["meta"]["now"] == iso(now), f"{tool}: meta.now is not the service clock"
    assert env["meta"]["policy_version"] == POLICY["version"]
    json.dumps(env)


def expect_ok(env):
    assert env["ok"] is True, f"{env.get('tool')} failed: {env.get('error')}"
    return env["data"]


def expect_error(env, code, **details):
    """Assert a typed error: fixed message and retryable flag from the catalog, whitelisted details, next_action."""
    assert env["ok"] is False, f"{env.get('tool')}: expected {code}, got ok with {str(env.get('data'))[:300]}"
    err = env["error"]
    assert err["code"] == code, f"{env.get('tool')}: expected {code}, got {err['code']} {err.get('details')}"
    assert err["message"] == ERRORS[code]["message"], f"{code}: message is not the fixed template"
    assert err["retryable"] is ERRORS[code]["retryable"], f"{code}: wrong retryable flag"
    assert err["details"]["next_action"] in ERRORS[code]["next_action"], f"{code}: next_action not allowed"
    extra = set(err["details"]) - ERROR_DETAIL_KEYS[code]
    assert not extra, f"{code}: details carry keys outside the whitelist: {extra}"
    for key, value in details.items():
        assert err["details"].get(key) == value, f"{code}: details.{key} = {err['details'].get(key)!r}, want {value!r}"
    return err["details"]


def without_meta(env):
    return {k: v for k, v in env.items() if k != "meta"}


def check_output(tool, data):
    """The data object of a successful call against the tool's output schema (an allow-list: additionalProperties
    is false at every level)."""
    errors = validate(data, TOOLS[tool]["output_schema"])
    assert not errors, f"{tool}: output violates its schema: {errors[:8]}"


def _is_type(value, name):
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, float) and value.is_integer())
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    raise ValueError(name)


def validate(value, schema, path="$"):
    """Validator for the JSON Schema subset of tool_schemas.json (outputs included: $ref, anyOf, min/maxProperties).
    Returns a list of 'path: problem' strings that never include the value itself."""
    errors = []
    if "$ref" in schema:
        schema = SCHEMAS["$defs"][schema["$ref"].rsplit("/", 1)[-1]]
    if "anyOf" in schema:
        if not any(not validate(value, sub, path) for sub in schema["anyOf"]):
            errors.append(f"{path}: matches no anyOf branch")
        return errors
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, n) for n in names):
            return [f"{path}: type is {type(value).__name__}, want {names}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: not in enum")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: not the const")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: pattern {schema['pattern']}")
        if len(value) > schema.get("maxLength", 10 ** 9) or len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: length {len(value)}")
    if _is_type(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: not above exclusiveMinimum")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errors += [f"{path}: missing {k}" for k in schema.get("required", []) if k not in value]
        if schema.get("additionalProperties") is False:
            errors += [f"{path}: unexpected key {k}" for k in value if k not in props]
        if not schema.get("minProperties", 0) <= len(value) <= schema.get("maxProperties", 10 ** 9):
            errors.append(f"{path}: {len(value)} properties")
        for k, v in value.items():
            if k in props:
                errors += validate(v, props[k], f"{path}.{k}")
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10 ** 9):
            errors.append(f"{path}: {len(value)} items")
        if schema.get("uniqueItems"):
            dumped = [json.dumps(v, sort_keys=True) for v in value]
            if len(set(dumped)) != len(dumped):
                errors.append(f"{path}: items not unique")
        if "items" in schema:
            for i, v in enumerate(value):
                errors += validate(v, schema["items"], f"{path}[{i}]")
    return errors


# --------------------------------------------------------------------------------------------------------------
# Scanners


def iter_strings(obj, path="$"):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from iter_strings(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from iter_strings(v, f"{path}[{i}]")


def iter_numbers(obj):
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iter_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_numbers(v)


def pii_hits(obj):
    """String values (service ids excepted) that match the Gold privacy value patterns (CONTRACT §12.9)."""
    hits = []
    for path, text in iter_strings(obj):
        if SERVICE_ID.match(text):
            continue
        hits += [f"{path}: {name}" for name, pattern in PII_VALUE_PATTERNS.items() if pattern.search(text)]
    return hits


def contains_token(text, value):
    """`value` occurs in `text` as a whole token (digit-only values are not matched inside longer numbers)."""
    if not value:
        return False
    if value.isdigit():
        return re.search(rf"(?<![0-9A-Za-z.]){re.escape(value)}(?![0-9A-Za-z]|\.[0-9])", text) is not None
    return value in text


# --------------------------------------------------------------------------------------------------------------
# Session-token forgery with the TEST key (CONTRACT §2.3 format), for the tampering tests


def _b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b64e(raw, padded):
    text = base64.urlsafe_b64encode(raw).decode("ascii")
    return text if padded else text.rstrip("=")


def _key_candidates(secret, label):
    s, l = secret.encode(), label.encode()
    return [hmac.new(s, l, hashlib.sha256).digest(), hmac.new(l, s, hashlib.sha256).digest()]


def token_payload(token):
    return json.loads(_b64d(token.split(".")[1]))


def resign_token(token, **claims):
    """Re-sign `token` with modified claims using the TEST session key. Returns None when this harness cannot
    reproduce the genuine signature (then a forged-claims test would prove nothing and is skipped)."""
    prefix, payload_b64, signature = token.split(".")
    padded = "=" in payload_b64 or "=" in signature
    for key in _key_candidates(TEST_SESSION_KEY, "bank-tools/session/v1"):
        sign = lambda p64, k=key: _b64e(hmac.new(k, f"{prefix}.{p64}".encode(), hashlib.sha256).digest(), padded)
        if hmac.compare_digest(sign(payload_b64), signature):
            payload = json.loads(_b64d(payload_b64))
            payload.update(claims)
            new_b64 = _b64e(json.dumps(payload, separators=(",", ":")).encode(), padded)
            return f"{prefix}.{new_b64}.{sign(new_b64)}"
    return None


def epoch(dt):
    """Naive service time as a UTC epoch (CONTRACT §2.3)."""
    return int((dt - datetime(1970, 1, 1)).total_seconds())


# --------------------------------------------------------------------------------------------------------------
# Argument builders


def valid_args(tool, customer_id=fx.C1):
    """Schema-valid arguments for each auth-required tool, pointing at the customer's own records."""
    own = {fx.C1: ("savings", "super", "decl54"), fx.C2: ("b_savings", "b_super", "b_decl"),
           fx.C3: ("c_credit", "c_purchase", "c_purchase"), fx.C4: ("d_savings", "d_transfer", "d_transfer")}
    product, txn, declined = own[customer_id]
    return {
        "get_customer_overview": {},
        "list_products": {},
        "get_balance": {"product_id": fx.P[product]},
        "list_recent_transactions": {},
        "find_candidate_transactions": {"purpose": "dispute", "intent": None, "hints": {"merchant": "Super Ahorro"}},
        "explain_decline": {"transaction_id": fx.T[declined], "language": "es"},
        "check_dispute_eligibility": {"transaction_id": fx.T[txn], "intent": "dispute_unrecognized_charge"},
        "prepare_dispute_case": {"transaction_id": fx.T[txn], "intent": "dispute_unrecognized_charge", "language": "es"},
        "create_dispute_case": {"confirmation_id": "CNF-AAAAAAAAAAAA." + "A" * 22, "transaction_id": fx.T[txn],
                                "customer_confirmed": True, "idempotency_key": "idem-" + secrets.token_hex(6)},
        "get_case_status": {},
    }[tool]


def package(summary="El cliente no reconoce un cargo en su tarjeta y pide que lo revise un asesor.",
            facts=(), actions=(), evidence=(), questions=()):
    return {"request_summary": summary, "verified_facts": list(facts), "actions_taken": list(actions),
            "evidence": list(evidence), "open_questions": list(questions)}


def handoff_args(reason, pkg=None, language="es", **extra):
    args = {"reason_code": reason, "language": language, "package": pkg or package()}
    args.update(extra)
    return args


def new_key():
    return "idem-" + secrets.token_hex(8)
