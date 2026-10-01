"""BankService: the 14 bank tools behind one pipeline (CONTRACT.md sections 1.3 and 3 to 7).

    result = service.call_tool(name, args, session_token, ToolContext(conversation_id, turn_index, trace_id))
    result.for_model()   # the envelope sent to the model
    result.runtime       # runtime-only data (the session token after verify_otp)

Pipeline: resolve tool and exposure -> normalize and validate args -> session -> rate limit -> restricted-customer
gate -> handler -> output allow-list check -> typed errors -> one audit record. Every business rule comes from
src/policy/dispute_policy.py; thresholds are never re-typed here.
"""
import copy
import hashlib
import hmac
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.policy import dispute_policy as dp

from .clock import RealSleeper, RecordingSleeper, epoch, iso, wall_utc
from .config import FAULT_ENVS, SERVICE_VERSION, Config
from .errors import MalformedRecord, RepositoryError, ToolError, Unavailable
from .faults import NullFaultInjector
from .identity import IdentityService, Keys, TestOutbox
from .ids import IdFactory, b64url
from .redaction import redact_args, reference_text_ok, scrub_pii, short_hash, wrap_untrusted
from .repository.base import GuardedRepository
from .schemas import ToolSchemas, normalize, truncate_to_limits
from .state import RateLimiter, StateStore

SNIPPETS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy_snippets.json")
GATE_ALLOWED = ("get_customer_overview", "get_policy_info", "handoff_to_human")
WRITE_TOOLS = ("create_dispute_case",)
CREDIT_TYPES = ("Credit Card", "Personal Loan", "Mortgage")
BALANCE_KIND = {"Savings Account": "funds", "Checking Account": "funds", "Debit Card": "funds", "Investment": "funds",
                "Credit Card": "outstanding_debt", "Personal Loan": "outstanding_debt", "Mortgage": "outstanding_debt",
                "Insurance": "unspecified"}
QUEUES = {"suspected_card_compromise": "card_security", "customer_status_restricted": "account_restrictions",
          "complaint_routing": "complaints"}
TRANSACTION_LEVEL_REASONS = ("outside_dispute_window", "amount_above_threshold")
INTERNAL_POLICY_PATHS = ("handoff.amount_usd_threshold", "handoff.threshold_calibration", "priority.high_amount_usd",
                         "priority.medium_amount_usd", "priority.calibration", "transaction_matching",
                         "handoff.min_intent_confidence", "handoff.max_clarifications_before_handoff",
                         "handoff.tool_max_retries", "handoff.tool_retry_backoff_seconds", "handoff.triggers_in_order",
                         "must_not_vocabulary")
CONFIRMATION = re.compile(r"^CNF-([A-Z0-9]{12})\.([A-Za-z0-9_-]{22})$")
LAST4 = re.compile(r"^[A-Z0-9]{4}$")
TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
PLACEHOLDER = re.compile(r"\{policy:([a-z_.]+)(?:\|([a-z]+))?\}")
MAX_ARG_DEPTH = 6  # the deepest input is args -> package -> list -> string (3 containers)
HUMAN_REVIEW_RETENTION = timedelta(days=1)  # like drafts: a movement handed to a human stays out of automation


def _too_deep(value, limit=MAX_ARG_DEPTH):
    """True when containers nest deeper than `limit` (checked iteratively: hostile input cannot exhaust the stack)."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        if depth > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def _check_date(value, path):
    """The schema pattern admits impossible dates such as 2026-02-31: refuse them as a validation error."""
    if value is None:
        return
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ToolError("VALIDATION_ERROR", {"reason": "schema", "fields": [{"path": path, "problem": "date"}]},
                        internal_reason="bad_date") from None


def _ordered(schema, data):
    """Keys in output-schema order (stored replays come back sorted)."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(data, dict) or not props:
        return data
    out = {k: data[k] for k in props if k in data}
    out.update({k: v for k, v in data.items() if k not in out})
    for key, value in out.items():
        if isinstance(value, dict):
            out[key] = _ordered(props.get(key, {}), value)
    return out


@dataclass
class ToolContext:
    """Supplied by the runtime, never by the model."""
    conversation_id: str
    turn_index: int
    trace_id: str
    caller: str = "model"

    def check(self):
        if not isinstance(self.conversation_id, str) or not 1 <= len(self.conversation_id) <= 64:
            raise ValueError("ToolContext.conversation_id must be 1 to 64 characters")
        if isinstance(self.turn_index, bool) or not isinstance(self.turn_index, int) or self.turn_index < 1:
            raise ValueError("ToolContext.turn_index must be an integer >= 1")
        if not isinstance(self.trace_id, str) or not TRACE_ID.match(self.trace_id):
            raise ValueError("ToolContext.trace_id must be 32 hex characters")
        if self.caller not in ("model", "runtime"):
            raise ValueError("ToolContext.caller must be model or runtime")


class ToolResult:
    def __init__(self, tool, ok, data=None, error=None, warnings=None, meta=None, runtime=None):
        self.tool = tool
        self.ok = ok
        self.data = data
        self.error = error
        self.warnings = warnings or []
        self.meta = meta or {}
        self.runtime = runtime or {}

    @property
    def code(self):
        return None if self.ok else self.error["code"]

    def for_model(self):
        env = {"ok": self.ok, "tool": self.tool}
        if self.ok:
            env["data"] = copy.deepcopy(self.data)
        else:
            env["error"] = copy.deepcopy(self.error)
        env["warnings"] = list(self.warnings)
        env["meta"] = dict(self.meta)
        return env

    def __repr__(self):
        return f"ToolResult({self.tool}, ok={self.ok}, code={self.code})"


class _Call:
    """Scratch state of one tool call; feeds the audit record."""

    def __init__(self, tool, ctx, now, tool_call_id):
        self.tool, self.ctx, self.now, self.id = tool, ctx, now, tool_call_id
        self.session = None
        self.args = None
        self.warnings = []
        self.security_events = []
        self.internal_reason = None
        self.result_summary = {}
        self.policy_decision = {"eligible": None, "handoff_required": None, "handoff_reason": None,
                                "next_action": None, "match_status": None, "clarifications_used": None}
        self.truncated = []
        self.extra = {}

    @property
    def now_iso(self):
        return iso(self.now)


class BankService:
    def __init__(self, repository, clock, audit, faults=None, config=None, *, state=None, outbox=None, sleeper=None,
                 ids=None, schemas=None, policy=None):
        self.config = (config or Config.from_env()).checked()
        if faults is not None and getattr(faults, "active", False) and self.config.env not in FAULT_ENVS:
            raise RuntimeError("a FaultInjector is accepted only when BANK_TOOLS_ENV is test or eval")
        self.repository = repository
        self.clock = clock
        self.audit = audit
        self.faults = faults or NullFaultInjector()
        self.pol = policy or dp.load_policy()
        self.schemas = schemas or ToolSchemas()
        self.keys = Keys(self.config.session_key, self.config.otp_key, self.config.session_key_previous)
        seeded = self.config.env in FAULT_ENVS
        self.ids = ids or IdFactory(self.config.id_seed if seeded else None)
        self.sleeper = sleeper or (RecordingSleeper() if seeded else RealSleeper())
        self.state = state or StateStore(self.config.path(self.config.state_path) or None)
        self.limiter = RateLimiter(self.state)
        self.outbox = outbox or TestOutbox(self.config.path(self.config.otp_outbox_file) or None)
        self.guarded = GuardedRepository(repository, self.pol, self.faults, self.sleeper,
                                         self.config.attempt_deadline_s, self.config.call_deadline_s)
        self.identity = IdentityService(self.config, self.keys, self.state, self.guarded, repository, self.ids,
                                        self.outbox, self.pol, self.limiter, audit_hook=self)
        self.reason_codes = [t["reason"] for t in self.pol["handoff"]["triggers_in_order"]] + ["complaint_routing"]
        schema_enum = self.schemas.tool("handoff_to_human")["input_schema"]["properties"]["reason_code"]["enum"]
        if schema_enum != self.reason_codes:
            raise RuntimeError("handoff reason_code enum differs from the policy triggers plus complaint_routing")
        self.snippets = self._load_snippets()
        self._handlers = {name: getattr(self, "_h_" + name) for name in self.schemas.tools}

    # =========================================================================================================
    # Public API
    # =========================================================================================================

    def call_tool(self, name, args, session_token, context):
        if not isinstance(context, ToolContext):
            raise TypeError("call_tool requires a ToolContext supplied by the runtime")
        context.check()
        started = time.perf_counter()
        now = self.clock.now().replace(microsecond=0)
        call = _Call(name if isinstance(name, str) else "<invalid>", context, now, self.ids.tool_call_id())
        stats = self.guarded.begin_call()
        data, error, runtime = None, None, None
        try:
            spec = self.schemas.tool(name)
            if spec is None:
                call.tool = "<unknown>"
                raise ToolError("VALIDATION_ERROR", {"reason": "unknown_tool", "fields": []},
                                internal_reason="unknown_tool")
            if spec["exposure"] == "runtime" and context.caller == "model" and not self.config.model_auth:
                raise ToolError("FORBIDDEN", internal_reason="runtime_tool_from_model")
            call.args = self._prepare_args(spec, args, call)
            call.session = self._session_for(spec, session_token, now, call)
            self._rate_limit(name, call)
            restricted = self.pol["handoff"]["restricted_customer_statuses"]
            if (spec["auth"] == "required" and name not in GATE_ALLOWED and call.session is not None
                    and call.session.customer_status in restricted):
                call.policy_decision.update(handoff_required=True, handoff_reason="customer_status_restricted",
                                            next_action="handoff")
                raise ToolError("POLICY_BLOCKED", {"reason": "customer_status_restricted",
                                                   "handoff_reason": "customer_status_restricted",
                                                   "next_action": "handoff"},
                                internal_reason="customer_status_restricted")
            if self.config.read_only and name in WRITE_TOOLS:
                raise ToolError("FORBIDDEN", internal_reason="read_only_mode")
            out = self._handlers[name](call.args, call)
            data, runtime = out if isinstance(out, tuple) else (out, None)
            data = _ordered(spec["output_schema"], data)
            problems = self.schemas.validate_output(name, data)
            if problems:  # the allow-list is the second privacy barrier: fail closed
                data, runtime = None, None
                raise ToolError("INTERNAL", {"reason": "unexpected", "handoff_reason": "tool_failure"},
                                internal_reason="output_schema:" + ",".join(p["path"] for p in problems[:5]))
        except ToolError as exc:
            error = exc
        except Unavailable as exc:
            details = {"attempts": exc.attempts, "write_state": exc.write_state, "handoff_reason": "tool_failure",
                       "next_action": "static_fallback" if name == "handoff_to_human" else "handoff"}
            error = ToolError("UNAVAILABLE", details, internal_reason=exc.op + "_unavailable")
        except MalformedRecord as exc:
            error = ToolError("INTERNAL", {"reason": "malformed_record", "handoff_reason": "tool_failure"},
                              internal_reason="malformed_record:" + exc.op)
        except RepositoryError as exc:
            error = ToolError("INTERNAL", {"reason": "unexpected", "handoff_reason": "tool_failure"},
                              internal_reason="repository_error:" + str(exc)[:60])
        except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the model
            error = ToolError("INTERNAL", {"reason": "unexpected", "handoff_reason": "tool_failure"},
                              internal_reason="unexpected:" + type(exc).__name__)
        if error is not None and call.internal_reason is None:
            call.internal_reason = error.internal_reason
        warnings = list(call.warnings)
        if stats.malformed:
            warnings.append("malformed_rows_excluded:" + str(stats.malformed))
        if call.truncated:
            warnings.append("package_truncated:" + str(len(call.truncated)))
        meta = {"tool_call_id": call.id, "now": iso(now), "policy_version": self.pol["version"]}
        result = ToolResult(call.tool, error is None, data=data, error=error.to_dict() if error else None,
                            warnings=[w[:80] for w in warnings], meta=meta, runtime=runtime)
        self._audit(call, result, stats, started)
        return result

    def model_tools(self, parameters_key="input_schema"):
        """Tool definitions to offer the model (runtime tools only with BANK_TOOLS_MODEL_AUTH=true)."""
        return self.schemas.model_tools(include_runtime=self.config.model_auth, parameters_key=parameters_key)

    def health(self):
        out = {"repository": self.repository.health(), "env": self.config.env, "service_version": SERVICE_VERSION,
               "policy_version": self.pol["version"]}
        if hasattr(self.audit, "health"):
            out["audit"] = self.audit.health()
        return out

    def end_conversation(self, conversation_id):
        """Flush buffered audit records and drop the conversation's counters and tool-call index (retention)."""
        if hasattr(self.audit, "end_conversation"):
            self.audit.end_conversation(conversation_id)
        elif hasattr(self.audit, "flush"):
            self.audit.flush()
        self.state.delete("tool_calls", conversation_id)
        self.state.purge(self.clock.now())

    # One public method per tool, same pipeline as call_tool.
    def start_authentication(self, session_token, ctx, **kwargs):
        return self.call_tool("start_authentication", kwargs, session_token, ctx)

    def verify_otp(self, session_token, ctx, **kwargs):
        return self.call_tool("verify_otp", kwargs, session_token, ctx)

    def get_customer_overview(self, session_token, ctx, **kwargs):
        return self.call_tool("get_customer_overview", kwargs, session_token, ctx)

    def list_products(self, session_token, ctx, **kwargs):
        return self.call_tool("list_products", kwargs, session_token, ctx)

    def get_balance(self, session_token, ctx, **kwargs):
        return self.call_tool("get_balance", kwargs, session_token, ctx)

    def list_recent_transactions(self, session_token, ctx, **kwargs):
        return self.call_tool("list_recent_transactions", kwargs, session_token, ctx)

    def find_candidate_transactions(self, session_token, ctx, **kwargs):
        return self.call_tool("find_candidate_transactions", kwargs, session_token, ctx)

    def explain_decline(self, session_token, ctx, **kwargs):
        return self.call_tool("explain_decline", kwargs, session_token, ctx)

    def check_dispute_eligibility(self, session_token, ctx, **kwargs):
        return self.call_tool("check_dispute_eligibility", kwargs, session_token, ctx)

    def prepare_dispute_case(self, session_token, ctx, **kwargs):
        return self.call_tool("prepare_dispute_case", kwargs, session_token, ctx)

    def create_dispute_case(self, session_token, ctx, **kwargs):
        return self.call_tool("create_dispute_case", kwargs, session_token, ctx)

    def get_case_status(self, session_token, ctx, **kwargs):
        return self.call_tool("get_case_status", kwargs, session_token, ctx)

    def get_policy_info(self, session_token, ctx, **kwargs):
        return self.call_tool("get_policy_info", kwargs, session_token, ctx)

    def handoff_to_human(self, session_token, ctx, **kwargs):
        return self.call_tool("handoff_to_human", kwargs, session_token, ctx)

    # =========================================================================================================
    # Pipeline steps
    # =========================================================================================================

    def _prepare_args(self, spec, args, call):
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ToolError("VALIDATION_ERROR", {"reason": "schema", "fields": [{"path": "$", "problem": "type"}]},
                            internal_reason="args_not_object")
        if _too_deep(args):
            raise ToolError("VALIDATION_ERROR", {"reason": "schema", "fields": [{"path": "$", "problem": "depth"}]},
                            internal_reason="schema:depth")
        schema = spec["input_schema"]
        clean = normalize(schema, args)
        if spec["name"] == "handoff_to_human" and isinstance(clean.get("package"), dict):
            # Section 3.15: texts above the limits are cut (and reported), never a reason to refuse a handoff.
            call.truncated = truncate_to_limits(schema["properties"]["package"], clean["package"], "package")
        problems = self.schemas.validate_input(spec["name"], clean)
        if problems:
            raise ToolError("VALIDATION_ERROR", {"reason": "schema", "fields": problems[:20]},
                            internal_reason="schema:" + ",".join(p["problem"] for p in problems[:5]))
        return clean

    def _session_for(self, spec, token, now, call):
        conversation_id = call.ctx.conversation_id
        if spec["auth"] == "required":
            return self.identity.validate(token, now, conversation_id)
        if spec["auth"] == "optional" and token:
            try:
                return self.identity.validate(token, now, conversation_id)
            except ToolError as exc:  # a handoff is the safe fallback: proceed unbound
                call.warnings.append("identity_not_verified")
                call.extra["unbound_reason"] = exc.internal_reason
                return None
        return None

    def _rate_limit(self, name, call):
        cfg, now_e, conv = self.config, epoch(call.now), call.ctx.conversation_id
        buckets = [("conv:" + conv, cfg.conversation_calls, None)]
        if call.session is not None:
            buckets.append(("sess:" + call.session.sid, cfg.session_calls_per_window, cfg.session_window_s))
        if name == "get_policy_info":
            buckets.append(("policy:" + conv, cfg.policy_info_calls, None))
        for bucket, limit, window in buckets:
            if self.limiter.peek(bucket, limit, window, now_e):
                wait = self.limiter.hit(bucket, limit, window, now_e)
                raise ToolError("RATE_LIMITED", {"retry_after_s": wait}, internal_reason="rate_limit:" + bucket.split(":")[0])
        for bucket, limit, window in buckets:
            self.limiter.hit(bucket, limit, window, now_e)

    def _not_found(self, kind, resource_id, call):
        """Unknown, foreign and future ids get the same NOT_FOUND; a foreign id is labeled in the audit only."""
        session = call.session
        call.internal_reason = "not_found"
        foreign = False
        if session is not None:
            try:
                foreign = self.repository.resource_exists(kind, resource_id, exclude_customer_id=session.customer_id)
            except Exception:  # noqa: BLE001 - labeling must never change the response
                foreign = False
        if foreign:
            call.internal_reason = "forbidden_foreign_resource"
            call.security_events.append("foreign_resource_probe")
            key = session.sid
            probes = int(self.state.get("probes", key, 0)) + 1
            self.state.put("probes", key, probes, expires_at=session.expires_at)
            if probes >= self.config.foreign_probe_limit:
                self.identity.revoke(session.sid)
                call.security_events.append("session_revoked")
        raise ToolError("NOT_FOUND", {"resource": kind})

    # =========================================================================================================
    # Views (allow-listed fields only)
    # =========================================================================================================

    @staticmethod
    def _transaction_view(row):
        return {"transaction_id": row["transaction_id"], "product_id": row["product_id"],
                "product_type_en": row["product_type_en"], "event_ts": row["event_ts"][:16],
                "event_date": row["event_date"], "transaction_type": row["transaction_type"],
                "transaction_status": row["transaction_status"], "amount": round(float(row["amount"]), 2),
                "currency": row["currency"], "merchant": wrap_untrusted(row.get("merchant_name")),
                "channel": dp.narratable_channel(row), "is_international": bool(row["is_international"]),
                "transaction_country_code": row["transaction_country_code"]}

    @staticmethod
    def _last4(product):
        value = (product or {}).get("product_number_last4")
        return value if isinstance(value, str) and LAST4.match(value) else None

    def _product_view(self, row):
        return {"product_id": row["product_id"], "product_type_en": row["product_type_en"], "is_card": row["is_card"],
                "currency": row["currency"], "effective_status": row["effective_status"],
                "status_as_of": row.get("balance_as_of") or self.pol["as_of_date"], "number_last4": self._last4(row)}

    def _verified_facts(self, txn, product):
        view = self._transaction_view(txn)
        return {"transaction_id": view["transaction_id"], "product_id": view["product_id"],
                "product_type_en": view["product_type_en"], "number_last4": self._last4(product),
                "transaction_type": view["transaction_type"], "event_date": view["event_date"],
                "event_ts": view["event_ts"], "amount": view["amount"], "currency": view["currency"],
                "merchant": view["merchant"], "channel": view["channel"],
                "is_international": view["is_international"]}

    @staticmethod
    def _case_view(row):
        keys = ("case_id", "status", "created_at", "priority", "first_response_hours", "first_response_due_at",
                "case_type", "dispute_type", "category", "subcategory", "transaction_id", "product_id", "amount",
                "currency", "event_date")
        return {k: row.get(k) for k in keys}

    # =========================================================================================================
    # Handlers: identity
    # =========================================================================================================

    def _h_start_authentication(self, args, call):
        data, info = self.identity.start(call.ctx.conversation_id, args["document_type"], args["document_number"],
                                         args.get("preferred_channel") or "sms", call.now)
        call.result_summary = {"challenge_id": data["challenge_id"], "doc_key": info["doc_key"], "decoy": info["decoy"]}
        return data

    def _h_verify_otp(self, args, call):
        data, token, session = self.identity.verify(call.ctx.conversation_id, args["challenge_id"], args["code"],
                                                    call.now)
        call.session = session
        call.result_summary = {"authenticated": True, "session_ref": data["session_ref"]}
        return data, {"session_token": token}

    # =========================================================================================================
    # Handlers: account
    # =========================================================================================================

    def _h_get_customer_overview(self, args, call):
        s = call.session
        profile = self.guarded.get_customer(s.customer_id)
        if profile is None:
            raise ToolError("AUTH_REQUIRED", internal_reason="unknown_subject")
        products = self.guarded.list_products(s.customer_id)
        counts = Counter(p["product_type_en"] for p in products)
        need, why = dp.requires_handoff({"customer_status": profile["customer_status"]}, self.pol)
        restricted = need and why == "customer_status_restricted"
        call.policy_decision.update(handoff_required=restricted, handoff_reason=why if restricted else None,
                                    next_action="handoff" if restricted else None)
        call.result_summary = {"customer_status": profile["customer_status"], "handoff_required": restricted,
                               "products": len(products)}
        minutes_left = max(0, int((s.expires_at - call.now).total_seconds() // 60))
        return {"customer_status": profile["customer_status"],
                "service_restriction": {"handoff_required": restricted,
                                        "handoff_reason": "customer_status_restricted" if restricted else None},
                "country_code": profile["country_code"],
                "products_summary": [{"product_type_en": t, "count": n} for t, n in sorted(counts.items())],
                "session": {"session_ref": self.identity.session_ref(s.sid), "authenticated_at": iso(s.authenticated_at),
                            "expires_at": iso(s.expires_at), "minutes_left": minutes_left},
                "now": call.now_iso, "data_as_of": self.pol["as_of_date"], "languages": ["es", "pt"]}

    def _h_list_products(self, args, call):
        types = set(args.get("product_types") or [])
        only_active = bool(args.get("only_active"))
        rows = self.guarded.list_products(call.session.customer_id)
        views = [self._product_view(r) for r in rows
                 if (not types or r["product_type_en"] in types) and (not only_active or r["effective_status"] == "Active")]
        call.result_summary = {"count": len(views)}
        return {"products": views, "count": len(views)}

    def _h_get_balance(self, args, call):
        row = self.guarded.get_product(call.session.customer_id, args["product_id"])
        if row is None:
            self._not_found("product", args["product_id"], call)
        if row.get("current_balance") is None:
            raise MalformedRecord("get_products")
        kind = row["product_type_en"]
        call.result_summary = {"product_id": row["product_id"]}
        return {"product_id": row["product_id"], "product_type_en": kind, "currency": row["currency"],
                "current_balance": round(float(row["current_balance"]), 2), "balance_kind": BALANCE_KIND[kind],
                "credit_limit": (round(float(row["credit_limit"]), 2)
                                 if kind in CREDIT_TYPES and row.get("credit_limit") is not None else None),
                "effective_status": row["effective_status"],
                "balance_as_of": row.get("balance_as_of") or self.pol["as_of_date"]}

    def _h_list_recent_transactions(self, args, call):
        cid = call.session.customer_id
        product_id = args.get("product_id")
        _check_date(args.get("date_from"), "$.date_from")
        _check_date(args.get("date_to"), "$.date_to")
        today = call.now.date().isoformat()
        date_from, date_to = args.get("date_from"), args.get("date_to")
        if date_to and date_to > today:
            date_to = today
        if date_from and date_to and date_from > date_to:
            raise ToolError("VALIDATION_ERROR", {"reason": "date_range",
                                                 "fields": [{"path": "$.date_from", "problem": "after_date_to"}]},
                            internal_reason="date_range")
        if product_id and self.guarded.get_product(cid, product_id) is None:
            self._not_found("product", product_id, call)
        rows = self.guarded.list_transactions(cid, call.now_iso)
        statuses, types = set(args.get("statuses") or []), set(args.get("transaction_types") or [])
        kept = [r for r in rows
                if (not product_id or r["product_id"] == product_id)
                and (not statuses or r["transaction_status"] in statuses)
                and (not types or r["transaction_type"] in types)
                and (not date_from or r["event_date"] >= date_from) and (not date_to or r["event_date"] <= date_to)]
        ordered = dp.recent_movements(kept, cid, call.now, n=max(1, len(kept)), pol=self.pol) if kept else []
        limit = args.get("limit") or self.pol["narration"]["recent_movements_count"]
        shown = ordered[:limit]
        call.result_summary = {"returned": len(shown), "has_more": len(ordered) > limit,
                               "transaction_ids": [t["transaction_id"] for t in shown]}
        return {"transactions": [self._transaction_view(t) for t in shown], "returned": len(shown),
                "has_more": len(ordered) > limit, "now": call.now_iso}

    # =========================================================================================================
    # Handlers: disputes
    # =========================================================================================================

    def _h_find_candidate_transactions(self, args, call):
        s, ctx = call.session, call.ctx
        purpose = args["purpose"]
        intent = args.get("intent") if purpose == "dispute" else None
        hints = args.get("hints") or {}
        claim = {k: hints[k] for k in ("amount", "currency", "date", "merchant", "txn_type", "channel") if k in hints}
        _check_date(claim.get("date"), "$.hints.date")
        rows = self.guarded.list_transactions(s.customer_id, call.now_iso)
        if purpose == "dispute":
            cands = dp.candidate_transactions(rows, s.customer_id, call.now, intent, self.pol)
        else:
            cands = dp.candidate_transactions(rows, s.customer_id, call.now, None, self.pol, statuses=("Declined",))
        match = dp.match_transactions(claim, cands, self.pol)
        status = match["status"]
        key = ctx.conversation_id + "|" + s.customer_id + "|" + purpose
        counter = self.state.get("counter", key, {"turns": {}, "last": None})
        used = sum(1 for turn, st in counter["turns"].items() if int(turn) < ctx.turn_index and st != "unique")
        max_clar = self.pol["handoff"]["max_clarifications_before_handoff"]
        handoff_reason = None
        if status == "unique":
            next_action = "confirm_candidate"
        elif status in ("multiple", "no_hints"):
            next_action = "ask_customer_to_pick"
        else:
            need, why = dp.requires_handoff({"match_status": "none", "match_clarifications": used}, self.pol)
            next_action, handoff_reason = ("handoff", why) if need else ("ask_one_clarifying_question", None)
        counter["turns"][str(ctx.turn_index)] = status
        counter["last"] = {"turn": ctx.turn_index, "match_status": status, "clarifications_used": used,
                           "next_action": next_action}
        self.state.put("counter", key, counter)
        by_id = {t["transaction_id"]: t for t in cands}
        shown = match["matches"][:self.pol["transaction_matching"]["max_candidates_shown"]]
        call.policy_decision.update(match_status=status, clarifications_used=used, next_action=next_action,
                                    handoff_required=next_action == "handoff", handoff_reason=handoff_reason)
        call.result_summary = {"purpose": purpose, "match_status": status, "total_matches": len(match["matches"]),
                               "candidate_ids": shown}
        return {"match_status": status, "candidates": [self._transaction_view(by_id[t]) for t in shown],
                "total_matches": len(match["matches"]),
                "policy": {"clarifications_used": used, "max_clarifications": max_clar, "next_action": next_action,
                           "handoff_reason": handoff_reason}}

    def _h_explain_decline(self, args, call):
        s, lang = call.session, args["language"]
        txn = self.guarded.get_transaction(s.customer_id, args["transaction_id"], call.now_iso)
        if txn is None:
            self._not_found("transaction", args["transaction_id"], call)
        product = self.guarded.get_product(s.customer_id, txn["product_id"])
        if product is None:  # Gold guarantees an owner-consistent product for every movement
            raise MalformedRecord("get_products")
        product_type = txn["product_type_en"]
        expiration = product.get("expiration_date")
        product_view = {"product_id": txn["product_id"], "product_type_en": product_type,
                        "effective_status": product["effective_status"],
                        "card_expiration_month": expiration[:7] if expiration and product.get("is_card") else None,
                        "card_expired_at_transaction": (expiration < txn["event_date"]) if expiration else None}
        out = {"transaction_id": txn["transaction_id"], "transaction_status": txn["transaction_status"],
               "applicable": txn["transaction_status"] == "Declined", "response_code": None, "reason": None,
               "customer_message_key": None, "customer_message": None, "inconsistent_code": False,
               "offer_human": False, "product": product_view}
        if out["applicable"]:
            exp = dp.decline_explanation(txn.get("response_code"), self.pol, product_type)
            unknown = exp["reason"] == self.pol["decline_codes"]["null"]["reason"]
            key = txn.get("decline_code_key") if not (unknown and not exp["inconsistent_code"]) else "missing"
            code_row = self.guarded.get_decline_code(key or "missing")
            if code_row is None:
                raise MalformedRecord("get_decline_codes")
            field = ("non_card_explanation_" if exp["inconsistent_code"] else "explanation_") + lang
            message = code_row.get(field)
            if not message or not reference_text_ok(message):  # quoted as trusted text: it must look like it
                raise MalformedRecord("get_decline_codes")
            enum = self.schemas.tool("explain_decline")["output_schema"]["properties"]["response_code"]["enum"]
            out.update({"response_code": exp["response_code"] if exp["response_code"] in enum else None,
                        "reason": exp["reason"], "customer_message_key": exp["customer_message_key"],
                        "customer_message": message[:400], "inconsistent_code": exp["inconsistent_code"],
                        "offer_human": unknown})
        call.result_summary = {"transaction_id": txn["transaction_id"], "applicable": out["applicable"],
                               "reason": out["reason"], "offer_human": out["offer_human"]}
        return out

    def _h_check_dispute_eligibility(self, args, call):
        s = call.session
        txn = self.guarded.get_transaction(s.customer_id, args["transaction_id"], call.now_iso)
        if txn is None:
            self._not_found("transaction", args["transaction_id"], call)
        ok, reason = dp.is_eligible(txn, s.customer_id, args["intent"], self.pol)
        reasons = dp.handoff_reasons({"transaction": txn, "now": call.now, "customer_status": s.customer_status},
                                     self.pol)
        first = next((r for r in reasons if r in TRANSACTION_LEVEL_REASONS), None)
        call.policy_decision.update(eligible=ok, handoff_required=first is not None, handoff_reason=first)
        call.result_summary = {"transaction_id": txn["transaction_id"], "eligible": ok,
                               "handoff_required": first is not None}
        return {"transaction_id": txn["transaction_id"], "eligible": ok, "ineligible_reason": None if ok else reason,
                "within_dispute_window": dp.in_dispute_window(txn["event_date"], call.now, self.pol),
                "days_since_transaction": dp.days_since(txn["event_date"], call.now),
                "dispute_window_days": self.pol["dispute_window"]["days_since_transaction"],
                "handoff_required": first is not None, "handoff_reason": first}

    # -- drafts and confirmations --------------------------------------------------------------------------------

    @staticmethod
    def _facts_hash(case):
        return hashlib.sha256(json.dumps(case, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _confirmation_sig(self, draft):
        message = "|".join([draft["draft_id"], draft["sid"], draft["transaction_id"], draft["intent"],
                            draft["expires_at"], draft["facts_hash"]])
        return b64url(self.keys.mac(self.keys.confirm, message))[:22]

    def _load_draft(self, confirmation_id, session):
        """(draft, reason). Reason is None when the signature verifies and the draft belongs to the session."""
        m = CONFIRMATION.match(confirmation_id or "")
        if not m:
            return None, "invalid_or_expired"
        draft = self.state.get("draft", m.group(1))
        if draft is None or draft["sid"] != session.sid:
            return None, "invalid_or_expired"
        if not hmac.compare_digest(self._confirmation_sig(draft), m.group(2)):
            return None, "invalid_or_expired"
        return draft, None

    def _save_draft(self, draft, session):
        self.state.put("draft", draft["draft_id"], draft, expires_at=session.expires_at + timedelta(days=1))

    def _h_prepare_dispute_case(self, args, call):
        s, ctx = call.session, call.ctx
        tid, intent, lang = args["transaction_id"], args["intent"], args["language"]
        flag = bool(args.get("suspected_card_compromise")) or self._compromise_reported(s)
        txn = self.guarded.get_transaction(s.customer_id, tid, call.now_iso)
        if txn is None:
            self._not_found("transaction", tid, call)
        ok, why = dp.is_eligible(txn, s.customer_id, intent, self.pol)
        if not ok:
            call.policy_decision.update(eligible=False)
            raise ToolError("POLICY_BLOCKED", {"reason": "not_eligible", "eligibility_reason": why,
                                               "next_action": "explain_decline" if why == "status_declined"
                                               else "offer_human"}, internal_reason="not_eligible:" + why)
        index_key = "|".join([s.sid, tid, intent, str(int(flag))])
        existing_id = self.state.get("draft_index", index_key)
        existing = self.state.get("draft", existing_id) if existing_id else None
        if existing and existing["status"] == "prepared" and call.now_iso < existing["expires_at"]:
            decision = existing["response"]["policy_decision"]
            call.policy_decision.update(eligible=True, handoff_required=decision["handoff_required"],
                                        handoff_reason=decision["handoff_reason"], next_action=decision["next_action"])
            call.result_summary = {"transaction_id": tid, "draft_id": existing["draft_id"], "reused": True,
                                   "handoff_required": decision["handoff_required"],
                                   "priority": existing["case"]["priority"]}
            return existing["response"]
        case = dp.build_case(intent, txn, s.customer_id, self.pol, lang, flag)
        reasons = dp.handoff_reasons({"customer_status": s.customer_status, "suspected_card_compromise": flag,
                                      "transaction": txn, "now": call.now}, self.pol)
        handoff_required = bool(reasons)
        decision = {"eligible": True, "handoff_required": handoff_required,
                    "handoff_reason": reasons[0] if reasons else None, "handoff_reasons_all": reasons,
                    "next_action": "ask_customer_to_confirm_then_handoff" if handoff_required
                    else "ask_customer_to_confirm_then_create"}
        product = None
        try:
            product = self.guarded.get_product(s.customer_id, txn["product_id"])
        except (Unavailable, MalformedRecord):  # the last-4 hint is optional
            call.warnings.append("product_hint_unavailable")
        draft_id = self.ids.code("")
        expires_at = min(call.now + timedelta(seconds=self.config.confirmation_ttl_s), s.expires_at)
        draft = {"draft_id": draft_id, "sid": s.sid, "customer_id": s.customer_id, "transaction_id": tid,
                 "intent": intent, "language": lang, "suspected_card_compromise": flag, "case": case,
                 "facts_hash": self._facts_hash(case), "decision": decision, "prepared_turn": ctx.turn_index,
                 "expires_at": iso(expires_at), "case_id": self.ids.code("DSP-"), "status": "prepared",
                 "conversation_id": ctx.conversation_id, "blocked_reasons": []}
        confirmation_id = "CNF-" + draft_id + "." + self._confirmation_sig(draft)
        draft["response"] = {
            "confirmation_id": confirmation_id, "confirmation_expires_at": iso(expires_at),
            "verified_facts": self._verified_facts(txn, product),
            "case_preview": {k: case[k] for k in ("case_type", "dispute_type", "category", "subcategory", "priority",
                                                  "first_response_hours")},
            "policy_decision": decision, "customer_must_confirm": True}
        self._save_draft(draft, s)
        self.state.put("draft_index", index_key, draft_id, expires_at=expires_at)
        call.policy_decision.update(eligible=True, handoff_required=handoff_required,
                                    handoff_reason=decision["handoff_reason"], next_action=decision["next_action"])
        call.result_summary = {"transaction_id": tid, "draft_id": draft_id, "reused": False,
                               "handoff_required": handoff_required, "priority": case["priority"]}
        return draft["response"]

    def _case_matches(self, stored, expected):
        for field in list(self.pol["case"]["required_fields"]) + ["status"]:
            a, b = stored.get(field), expected.get(field)
            if field == "amount" and a is not None and b is not None:
                a, b = round(float(a), 2), round(float(b), 2)
            if a != b:
                return False
        return True

    def _create_output(self, row, replayed, already_existed):
        return {"case_id": row["case_id"], "status": row["status"], "verified": True, "replayed": replayed,
                "already_existed": already_existed, "created_at": row["created_at"], "priority": row["priority"],
                "first_response_hours": row["first_response_hours"],
                "first_response_due_at": row["first_response_due_at"],
                "case": {k: row[k] for k in ("case_type", "dispute_type", "category", "subcategory", "transaction_id",
                                             "product_id", "amount", "currency", "event_date")}}

    def _h_create_dispute_case(self, args, call):
        s, ctx = call.session, call.ctx
        cid, tid, key = s.customer_id, args["transaction_id"], args["idempotency_key"]
        idem_key = s.sid + "|" + short_hash(key, 32)
        stored = self.state.get("idem_case", idem_key)
        if stored is not None:
            if stored["confirmation_id"] != args["confirmation_id"]:
                raise ToolError("VALIDATION_ERROR", {"reason": "idempotency_key_reused", "fields": []},
                                internal_reason="idempotency_key_reused")
            out = dict(stored["data"], replayed=True)
            call.result_summary = {"case_id": out["case_id"], "verified": True, "replayed": True,
                                   "priority": out["priority"]}
            return out

        def confirm_required(reason):
            raise ToolError("CONFIRMATION_REQUIRED", {"reason": reason}, internal_reason="confirmation:" + reason)

        if args["customer_confirmed"] is not True:
            confirm_required("not_confirmed")
        draft, reason = self._load_draft(args["confirmation_id"], s)
        if draft is None:
            confirm_required(reason)
        if draft["transaction_id"] != tid:
            confirm_required("transaction_mismatch")
        if call.now_iso >= draft["expires_at"]:
            confirm_required("invalid_or_expired")
        if ctx.turn_index <= draft["prepared_turn"]:
            confirm_required("same_turn")
        # Re-verify at write time: same facts, same policy decision at the current clock.
        txn = self.guarded.get_transaction(cid, tid, call.now_iso)
        if txn is None or not dp.is_eligible(txn, cid, draft["intent"], self.pol)[0]:
            confirm_required("stale_facts")
        case = dp.build_case(draft["intent"], txn, cid, self.pol, draft["language"], draft["suspected_card_compromise"])
        if self._facts_hash(case) != draft["facts_hash"]:
            confirm_required("stale_facts")
        compromise = draft["suspected_card_compromise"] or self._compromise_reported(s)
        reasons = dp.handoff_reasons({"customer_status": s.customer_status, "suspected_card_compromise": compromise,
                                      "transaction": txn, "now": call.now}, self.pol)
        with_human = self.state.get("human_review", cid + "|" + tid)  # the movement was handed to a human
        blocked_by = reasons[0] if reasons else (with_human or {}).get("reason")
        call.policy_decision.update(eligible=True, handoff_required=blocked_by is not None, handoff_reason=blocked_by)
        if blocked_by is not None:
            if reasons:
                draft["blocked_reasons"] = sorted(set(draft.get("blocked_reasons", [])) | set(reasons))
                self._save_draft(draft, s)
            call.policy_decision["next_action"] = "handoff"
            raise ToolError("POLICY_BLOCKED", {"reason": "handoff_required", "handoff_reason": blocked_by,
                                               "confirmation_id_usable_for_handoff": True, "next_action": "handoff"},
                            internal_reason=("handoff_required:" if reasons else "with_human:") + blocked_by)
        existing = self.guarded.find_open_case(cid, tid, case["dispute_type"])
        already = existing is not None
        if already:
            case_id = existing["case_id"]
        else:
            case_id = draft["case_id"]
            hours = case["first_response_hours"]
            row = dict(case)
            row.update({"case_id": case_id, "created_at": call.now_iso, "recorded_at": wall_utc(),
                        "first_response_due_at": iso(call.now + timedelta(hours=hours)),
                        "conversation_id": ctx.conversation_id, "session_id_hash": s.sid_hash,
                        "draft_id": draft["draft_id"], "idempotency_key_hash": short_hash(key),
                        "policy_version": self.pol["version"], "service_version": SERVICE_VERSION,
                        "env": self.config.env})
            self.guarded.insert_case(row)
        try:
            back = self.guarded.get_case(cid, case_id)
        except Unavailable as exc:
            raise Unavailable(exc.op, exc.attempts, "unknown") from None
        except MalformedRecord:
            back = None
        if back is None or (not already and not self._case_matches(back, case)):
            call.security_events.append("read_back_mismatch")
            raise ToolError("INTERNAL", {"reason": "read_back_mismatch", "handoff_reason": "tool_failure"},
                            internal_reason="read_back_mismatch:" + case_id)
        draft.update(status="case_created", created_case_id=back["case_id"])
        self._save_draft(draft, s)
        out = self._create_output(back, False, already)
        self.state.put("idem_case", idem_key, {"confirmation_id": args["confirmation_id"], "data": out},
                       expires_at=call.now + timedelta(days=1))
        call.result_summary = {"case_id": back["case_id"], "verified": True, "replayed": False,
                               "already_existed": already, "priority": back["priority"]}
        return out

    def _h_get_case_status(self, args, call):
        cid = call.session.customer_id
        ref = args.get("case_id")
        if ref is None:
            rows = self.guarded.list_cases(cid, 5)
            call.result_summary = {"kind": "list", "count": len(rows)}
            return {"cases": [self._case_view(r) for r in rows[:5]]}
        if ref.startswith("DSP-"):
            row = self.guarded.get_case(cid, ref)
            if row is None:
                self._not_found("case", ref, call)
            call.result_summary = {"kind": "case", "case_id": ref}
            return {"case": self._case_view(row)}
        row = self.guarded.get_ticket(ref, cid)
        if row is None:
            self._not_found("ticket", ref, call)
        call.result_summary = {"kind": "ticket", "ticket_id": ref}
        return {"ticket": {k: row[k] for k in ("ticket_id", "status", "queue", "created_at", "reason_code")}}

    # =========================================================================================================
    # Handlers: policy text and handoff
    # =========================================================================================================

    def _load_snippets(self):
        with open(SNIPPETS_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
        for topic, spec in doc["topics"].items():
            paths = list(spec["values"])
            for lang in ("es", "pt"):
                paths += [m.group(1) for s in spec[lang] for m in PLACEHOLDER.finditer(s["text"])]
            for path in paths:
                if any(path == p or path.startswith(p + ".") for p in INTERNAL_POLICY_PATHS):
                    raise RuntimeError(f"policy_snippets.json references an internal policy path ({topic})")
                self._policy_value(path)  # fails loudly on a typo
        return doc

    def _policy_value(self, path):
        value = self.pol
        for part in path.split("."):
            value = value[part]
        return value

    def _render(self, text, lang):
        labels = self.snippets["labels"][lang]
        joiner = " y " if lang == "es" else " e "

        def fill(m):
            value, kind = self._policy_value(m.group(1)), m.group(2)
            if kind == "list":
                items = [labels.get(str(v), str(v)) for v in value]
                return items[0] if len(items) == 1 else ", ".join(items[:-1]) + joiner + items[-1]
            if kind == "label":
                return labels.get(str(value), str(value))
            if kind == "minutes":
                return str(int(value) // 60)
            return str(value)

        return PLACEHOLDER.sub(fill, text)

    def _h_get_policy_info(self, args, call):
        topic, lang = args["topic"], args["language"]
        spec = self.snippets["topics"][topic]
        snippets = [{"id": s["id"], "text": self._render(s["text"], lang)[:600]} for s in spec[lang]]
        call.result_summary = {"topic": topic, "language": lang, "snippets": len(snippets)}
        return {"topic": topic, "language": lang, "snippets": snippets,
                "values": {p: copy.deepcopy(self._policy_value(p)) for p in spec["values"]},
                "policy_id": self.pol["policy_id"], "policy_version": self.pol["version"], "synthetic": True}

    def _reason_check(self, reason, session, draft, conversation_id):
        index = self.state.get("tool_calls", conversation_id, [])
        if reason == "customer_status_restricted":
            if session is None:
                return "not_verifiable"
            restricted = session.customer_status in self.pol["handoff"]["restricted_customer_statuses"]
            return "consistent" if restricted else "inconsistent"
        if reason in TRANSACTION_LEVEL_REASONS:
            if draft is None:
                return "inconsistent"
            fired = set(draft["decision"]["handoff_reasons_all"]) | set(draft.get("blocked_reasons", []))
            return "consistent" if reason in fired else "inconsistent"
        if reason == "no_match_after_clarification":
            if session is None:
                return "not_verifiable"
            for purpose in ("dispute", "decline_inquiry"):
                last = (self.state.get("counter", conversation_id + "|" + session.customer_id + "|" + purpose)
                        or {}).get("last")
                if last and last["match_status"] == "none" and last["clarifications_used"] >= 1:
                    return "consistent"
            return "inconsistent"
        if reason == "tool_failure":
            failed = any(e.get("error_code") in ("UNAVAILABLE", "INTERNAL") for e in index)
            return "consistent" if failed else "inconsistent"
        return "not_verifiable"

    def _h_handoff_to_human(self, args, call):
        s, ctx = call.session, call.ctx
        reason, lang, pkg = args["reason_code"], args["language"], args["package"]
        truncated, redactions = list(call.truncated), 0
        clean = {}
        text, n = scrub_pii(pkg["request_summary"])
        redactions += n
        limits = self.schemas.tool("handoff_to_human")["input_schema"]["properties"]["package"]["properties"]
        if len(text) > limits["request_summary"]["maxLength"]:
            text = text[:limits["request_summary"]["maxLength"]]
            truncated.append("package.request_summary")
        clean["request_summary"] = text
        for key in ("verified_facts", "actions_taken", "open_questions"):
            limit, items = limits[key]["items"]["maxLength"], []
            for i, item in enumerate(pkg.get(key) or []):
                text, n = scrub_pii(item)
                redactions += n
                if len(text) > limit:
                    text = text[:limit]
                    truncated.append(f"package.{key}[{i}]")
                items.append(text)
            clean[key] = items
        index = {e["tool_call_id"]: e for e in self.state.get("tool_calls", ctx.conversation_id, [])}
        own_key = self.identity.customer_key(s.customer_id) if s else None
        evidence, dropped = [], []
        for tcid in pkg.get("evidence") or []:
            entry = index.get(tcid)
            # Only calls made without a customer, or for this ticket's customer: never another customer's data,
            # and never a verified session's data on an identity-unverified ticket.
            if entry is not None and entry.get("customer_key") in (None, own_key):
                evidence.append({k: v for k, v in entry.items() if k != "customer_key"})
            else:
                dropped.append(tcid)
        if dropped:
            call.warnings.append("evidence_dropped:" + str(len(dropped)))
        draft = None
        if args.get("confirmation_id"):
            if s is None:
                call.warnings.append("confirmation_ignored_unverified")
            else:
                draft, _ = self._load_draft(args["confirmation_id"], s)  # expiry does not matter here
                if draft is None:
                    call.warnings.append("confirmation_not_attached")
        candidates = []
        if args.get("candidate_transaction_ids"):
            if s is None:
                call.warnings.append("candidates_ignored_unverified")
            else:
                for tid in args["candidate_transaction_ids"]:
                    try:
                        row = self.guarded.get_transaction(s.customer_id, tid, call.now_iso)
                    except (Unavailable, MalformedRecord):
                        row = None
                    if row is None:
                        call.warnings.append("candidate_dropped")
                        if s is not None and self._is_foreign("transaction", tid, s):
                            call.security_events.append("foreign_resource_probe")
                        continue
                    candidates.append({"transaction_id": row["transaction_id"], "product_id": row["product_id"],
                                       "event_date": row["event_date"], "transaction_type": row["transaction_type"],
                                       "transaction_status": row["transaction_status"], "amount": row["amount"],
                                       "currency": row["currency"]})
        check = self._reason_check(reason, s, draft, ctx.conversation_id)
        draft_status, priority = None, None
        if draft is not None:
            draft_status = "case_created" if draft["status"] == "case_created" else "pending_human_review"
            priority = draft["case"]["priority"]
        elif reason == "suspected_card_compromise":
            priority = dp.priority({"suspected_card_compromise": True}, self.pol)
        hours = self.pol["priority"]["first_response_hours"][priority] if priority else None
        queue = QUEUES.get(reason, "disputes")
        call.policy_decision.update(handoff_required=True, handoff_reason=reason)
        # Dedupe: the same idempotency key, or the same (conversation, reason, draft), returns the existing ticket.
        dedupe_keys = ["route|" + ctx.conversation_id + "|" + reason + "|" + (draft["draft_id"] if draft else "-")
                       + "|" + (s.sid_hash if s else "-")]
        if args.get("idempotency_key"):  # scoped to the session too: a key never replays another customer's ticket
            dedupe_keys.insert(0, "key|" + ctx.conversation_id + "|" + (s.sid_hash if s else "-") + "|"
                               + short_hash(args["idempotency_key"], 32))
        for k in dedupe_keys:
            hit = self.state.get("ticket_dedupe", k)
            if hit is not None:
                out = dict(hit, replayed=True)
                call.result_summary = {"ticket_id": out["ticket_id"], "verified": True, "replayed": True,
                                       "queue": out["queue"], "reason_check": out["reason_check"]}
                return out
        ticket_id = self.ids.code("HND-")
        draft_record = None
        if draft is not None:
            fields = dict(draft["case"])
            merchant = fields.pop("merchant_name", None)
            fields["merchant"] = wrap_untrusted(merchant)
            fields["status"] = draft_status
            draft_record = {"draft_id": draft["draft_id"], "case_fields": fields,
                            "case_id": draft.get("created_case_id"), "decision": draft["decision"],
                            "blocked_reasons": draft.get("blocked_reasons", [])}
        service_verified = {
            "draft": draft_record, "evidence": evidence, "candidates": candidates, "dropped_evidence": dropped,
            "session": ({"auth_method": s.method, "authenticated_at": iso(s.authenticated_at),
                         "expires_at": iso(s.expires_at), "customer_status": s.customer_status} if s else None),
        }
        row = {"ticket_id": ticket_id, "created_at": call.now_iso, "recorded_at": wall_utc(),
               "conversation_id": ctx.conversation_id, "session_id_hash": s.sid_hash if s else None,
               "identity_verified": s is not None, "customer_id": s.customer_id if s else None, "status": "queued",
               "reason_code": reason, "reason_check": check, "queue": queue, "priority": priority,
               "first_response_hours": hours, "language": lang, "request_summary": clean["request_summary"],
               "agent_reported_json": json.dumps(clean, ensure_ascii=False, sort_keys=True),
               "service_verified_json": json.dumps(service_verified, ensure_ascii=False, sort_keys=True, default=str),
               "redactions": redactions, "policy_version": self.pol["version"], "service_version": SERVICE_VERSION,
               "env": self.config.env}
        self.guarded.insert_ticket(row)
        try:
            back = self.guarded.get_ticket(ticket_id, s.customer_id if s else None)
        except Unavailable as exc:
            raise Unavailable(exc.op, exc.attempts, "unknown") from None
        except MalformedRecord:
            back = None
        expected = ("ticket_id", "reason_code", "queue", "status", "customer_id", "identity_verified", "priority")
        if back is None or any(back.get(k) != row[k] for k in expected):
            call.security_events.append("read_back_mismatch")
            raise ToolError("INTERNAL", {"reason": "read_back_mismatch", "handoff_reason": "tool_failure"},
                            internal_reason="read_back_mismatch:" + ticket_id)
        if draft is not None and draft["status"] != "case_created":
            draft["status"] = "pending_human_review"
            self._save_draft(draft, s)
            self.state.put("human_review", s.customer_id + "|" + draft["transaction_id"],
                           {"reason": reason, "ticket_id": ticket_id}, expires_at=call.now + HUMAN_REVIEW_RETENTION)
        if s is not None and reason == "suspected_card_compromise":
            self.state.put("compromise", s.customer_id, {"ticket_id": ticket_id},
                           expires_at=call.now + HUMAN_REVIEW_RETENTION)
        out = {"ticket_id": ticket_id, "status": back["status"], "verified": True, "replayed": False,
               "queue": back["queue"], "priority": back["priority"], "first_response_hours": back["first_response_hours"],
               "identity_verified": bool(back["identity_verified"]), "case_draft_attached": draft is not None,
               "draft_status": draft_status, "reason_check": check, "redactions": redactions,
               "truncated_fields": truncated, "dropped_evidence": dropped}
        for k in dedupe_keys:
            self.state.put("ticket_dedupe", k, out, expires_at=call.now + timedelta(days=1))
        call.result_summary = {"ticket_id": ticket_id, "verified": True, "replayed": False, "queue": queue,
                               "reason_check": check, "draft_attached": draft is not None,
                               "identity_verified": s is not None, "redactions": redactions}
        return out

    def _is_foreign(self, kind, resource_id, session):
        try:
            return self.repository.resource_exists(kind, resource_id, exclude_customer_id=session.customer_id)
        except Exception:  # noqa: BLE001
            return False

    def _compromise_reported(self, session):
        """A verified suspected_card_compromise handoff of this customer (last 24 h): the policy sends every
        dispute of the customer to a human, whatever flag a later call sends."""
        return self.state.get("compromise", session.customer_id) is not None

    # =========================================================================================================
    # Audit and tool-call index
    # =========================================================================================================

    def now(self):
        return self.clock.now().replace(microsecond=0)

    def _audit(self, call, result, stats, started):
        s = call.session
        args = redact_args(call.args) if call.args is not None else {"_args_not_validated": True}
        decision = dict(call.policy_decision)
        record = {
            "ts": call.now_iso, "recorded_at": wall_utc(), "trace_id": call.ctx.trace_id,
            "conversation_id": call.ctx.conversation_id, "turn_index": call.ctx.turn_index, "tool_call_id": call.id,
            "tool": call.tool, "caller": call.ctx.caller, "args_redacted": args,
            "outcome": "ok" if result.ok else "error", "error_code": result.code,
            "internal_reason": None if result.ok else call.internal_reason,
            "retryable": None if result.ok else result.error["retryable"],
            "attempts": dict(stats.attempts), "backoff_s": stats.backoff_s,
            "latency_ms": int((time.perf_counter() - started) * 1000) + stats.simulated_latency_ms,
            "policy_decision": decision, "result_summary": call.result_summary,
            "session_id_hash": s.sid_hash if s else None,
            "customer_key": self.identity.customer_key(s.customer_id) if s else None,
            "auth_method": s.method if s else None, "security_events": list(call.security_events),
            "faults_injected": list(stats.faults_injected), "warnings": list(result.warnings),
            "env": self.config.env, "repository": getattr(self.repository, "source", "unknown"),
            "service_version": SERVICE_VERSION, "policy_version": self.pol["version"],
        }
        if call.truncated:
            record["truncated_fields"] = list(call.truncated)
        self.audit.write(record)
        entry = {"tool_call_id": call.id, "tool": call.tool, "turn_index": call.ctx.turn_index,
                 "outcome": record["outcome"], "error_code": result.code, "result_summary": call.result_summary,
                 "policy_decision": decision, "customer_key": record["customer_key"]}
        items = self.state.get("tool_calls", call.ctx.conversation_id, [])
        items.append(entry)
        self.state.put("tool_calls", call.ctx.conversation_id, items[-500:])

    def test_session_issued(self, session, conversation_id):
        """Audit hook of identity.issue_test_session (tool '_test_issue_session', caller runtime)."""
        self.audit.write({
            "ts": iso(session.authenticated_at), "recorded_at": wall_utc(), "trace_id": None,
            "conversation_id": conversation_id, "turn_index": None, "tool_call_id": self.ids.tool_call_id(),
            "tool": "_test_issue_session", "caller": "runtime", "args_redacted": {}, "outcome": "ok",
            "error_code": None, "internal_reason": None, "retryable": None, "attempts": {}, "backoff_s": 0.0,
            "latency_ms": 0, "policy_decision": {}, "result_summary": {"session_ref": self.identity.session_ref(session.sid)},
            "session_id_hash": session.sid_hash, "customer_key": self.identity.customer_key(session.customer_id),
            "auth_method": session.method, "security_events": [], "faults_injected": [], "warnings": [],
            "env": self.config.env, "repository": getattr(self.repository, "source", "unknown"),
            "service_version": SERVICE_VERSION, "policy_version": self.pol["version"]})
