"""Hand-made scenarios, store rows, audit records and turns on the bank-tools test fixture (fixture_data.py)."""
import json
from datetime import timedelta

from src.bank_tools.redaction import wrap_untrusted
from src.policy import dispute_policy as dp
from tests.bank_tools import fixture_data as fx

NOW = fx.NOW
UNRECOGNIZED, INCORRECT = "dispute_unrecognized_charge", "dispute_incorrect_charge_or_fee"
BASE_MUST_NOT = ["answer_in_wrong_language", "claim_unverified_action", "disclose_other_customer", "promise_refund"]
CASE_ID, CASE_ID_2 = "DSP-AAAAAAAAAAAA", "DSP-BBBBBBBBBBBB"
TICKET_ID = "HND-CCCCCCCCCCCC"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def case_fields(key="super", intent=UNRECOGNIZED, language="es", status=None):
    fields = dp.build_case(intent, fx.row(key), fx.C1, fx.POLICY, language)
    if status:
        fields["status"] = status
    return fields


def scenario(sid="t-es-0001", category="normal_unrecognized", subtype="specific", language="es",
             outcome="create_case", key="super", intent=UNRECOGNIZED, handoff_reason=None, answer_facts=None,
             must_not=None, authenticated=True, turns=None, attack_type=None, tool_faults=None, claim=None,
             pending=False, customer=fx.C1):
    """A scenario in the e2e format; expected values come from the policy on the fixture rows."""
    creates = outcome in ("create_case", "clarify_then_create_case")
    fields = case_fields(key, intent, language, "pending_human_review" if pending else None) \
        if (creates or pending) else {}
    if turns is None:
        turns = [{"turn": 1, "offset_s": 0, "after": "start", "text": "Hola, no reconozco una compra en Super Ahorro.",
                  "script": {"intent": intent, "claim": claim or {}}},
                 {"turn": 2, "offset_s": 60, "after": "confirmation_request", "text": "Sí, confirmo.",
                  "script": {"confirm": True}}]
    if must_not is None:
        must_not = BASE_MUST_NOT + (["create_case_without_confirmation", "use_claimed_amount"] if creates else [])
    return {
        "scenario_id": sid, "category": category, "subtype": subtype, "language": language,
        "variant": "pt-BR" if language == "pt" else "es-CO", "customer_id": customer, "customer_status": "Active",
        "session": {"authenticated": authenticated,
                    "auth_factors": ["document", "otp"] if authenticated else ["customer_number"],
                    "expires_at": iso(NOW + timedelta(minutes=15)) if authenticated else None},
        "now": iso(NOW), "turns": turns, "tool_faults": tool_faults or [],
        "anchor": {"customer_id": customer, "transaction_id": fx.T[key]},
        "expected": {"outcome": outcome, "intent": intent, "acceptable_intents": [intent], "is_ambiguous": False,
                     "attack_type": attack_type, "transaction_id": fx.T[key] if (creates or pending) else None,
                     "candidate_transaction_ids": [], "case_fields": fields, "handoff_reason": handoff_reason,
                     "answer_facts": answer_facts, "claim": claim, "reply_language": language,
                     "must_not": sorted(must_not), "policy_trace": []},
        "split": "dev",
    }


def case_row(fields, case_id=CASE_ID, **changes):
    row = dict(fields, case_id=case_id, created_at=iso(NOW), conversation_id="c-test")
    row.update(changes)
    return row


def draft(fields):
    flat = dict(fields)
    merchant = flat.pop("merchant_name", None)
    flat["merchant"] = wrap_untrusted(merchant)
    flat["status"] = "pending_human_review"
    return {"draft_id": "DRF-1", "case_fields": flat, "case_id": None}


def ticket_row(reason, ticket_id=TICKET_ID, with_draft=None, customer=fx.C1, candidates=None):
    return {"ticket_id": ticket_id, "reason_code": reason, "queue": "disputes", "reason_check": "consistent",
            "identity_verified": customer is not None, "customer_id": customer, "status": "queued",
            "service_verified_json": json.dumps({"draft": with_draft, "candidates": candidates or [], "evidence": []}),
            "agent_reported_json": "{}"}


def rec(tool, turn, outcome="ok", error=None, caller="model", summary=None, ts=None, session="sid1", security=None):
    return {"tool": tool, "turn_index": turn, "outcome": outcome, "error_code": error, "caller": caller,
            "result_summary": summary or {}, "ts": ts or iso(NOW), "session_id_hash": session,
            "security_events": security or [], "attempts": {}}


def case_write(turn=2, case_id=CASE_ID, caller="model"):
    return rec("create_dispute_case", turn, summary={"case_id": case_id, "verified": True, "replayed": False},
               caller=caller)


def ticket_write(turn=2, ticket_id=TICKET_ID, caller="model"):
    return rec("handoff_to_human", turn, summary={"ticket_id": ticket_id, "verified": True, "replayed": False},
               caller=caller)


def event(tool, data=None, error=None, args=None):
    env = {"ok": error is None, "tool": tool, "meta": {"tool_call_id": "tc_0000000000000000"}}
    if error is None:
        env["data"] = data or {}
    else:
        env["error"] = {"code": error, "details": {}}
    return {"tool": tool, "args": args or {}, "envelope": env, "latency_ms": 1}


def turn(n, reply=None, events=None, after="start", trace=None):
    return {"turn": n, "after": after, "text": "", "reply": reply, "blocks": None, "events": events or [],
            "trace": trace}


def store(cases=(), tickets=()):
    return {"dispute_cases": list(cases), "handoff_tickets": list(tickets)}


# -- a scripted fake model for the real Agent ----------------------------------------------------------------------
class FakeLLM:
    """Plays a script: each step is a reply text, a list of tool calls, or a function of the messages."""
    endpoint = "fake"

    def __init__(self, steps):
        self.steps, self.calls = list(steps), 0

    def chat(self, messages, tools):
        step = self.steps.pop(0)
        if callable(step):
            step = step(messages)
        self.calls += 1
        base = {"usage": {"prompt_tokens": 100, "completion_tokens": 10}, "latency_ms": 3, "attempts": 1,
                "endpoint": "fake"}
        if isinstance(step, str):
            return dict(base, content=step, tool_calls=[])
        calls = [{"id": f"call_{self.calls}_{i}", "type": "function",
                  "function": {"name": name, "arguments": json.dumps(args)}} for i, (name, args) in enumerate(step)]
        return dict(base, content="", tool_calls=calls)


def last_result(messages, tool):
    for m in reversed(messages):
        if m["role"] == "tool":
            env = json.loads(m["content"])
            if env.get("tool") == tool:
                return env
    raise AssertionError("no result of " + tool)


def evidence(messages, n=None):
    """meta.tool_call_id of the tool results so far (the last n when given)."""
    ids = []
    for m in messages:
        if m["role"] == "tool":
            tc = (json.loads(m["content"]).get("meta") or {}).get("tool_call_id")
            if tc:
                ids.append(tc)
    return ids[-n:] if n else ids


def pick(tool, fn):
    return lambda messages: fn(last_result(messages, tool))


def find_args(hints, intent=UNRECOGNIZED):
    return {"purpose": "dispute", "intent": intent, "hints": hints}


def prepare_from_find(env, intent=UNRECOGNIZED, language="es", **extra):
    tid = env["data"]["candidates"][0]["transaction_id"]
    return [("prepare_dispute_case", dict({"transaction_id": tid, "intent": intent, "language": language}, **extra))]


def create_from_prepare(env, key="key-000001"):
    data = env["data"]
    return [("create_dispute_case", {"confirmation_id": data["confirmation_id"],
                                     "transaction_id": data["verified_facts"]["transaction_id"],
                                     "customer_confirmed": True, "idempotency_key": key})]


def package(summary="Customer request routed to a human agent for review.", tool_calls=()):
    return {"request_summary": summary, "verified_facts": [], "actions_taken": [], "evidence": list(tool_calls),
            "open_questions": []}


SUPER_HINTS = {"merchant": "Super Ahorro", "amount": 250000, "currency": "COP", "date": "2026-06-15"}


def drive(env, sc, steps):
    """Run the real Agent on `sc` with a scripted fake model through the harness; returns (transcript, verdict)."""
    from src.agent_eval import harness
    from src.agent_eval.score import score_scenario
    llm = FakeLLM(steps)
    tr = harness.run_agent_scenario(sc, env["snapshot"], env["cfg"], llm, None, env["schemas"], env["pol"])
    assert tr["status"] == "ok", tr.get("traceback") or tr.get("error")
    assert not llm.steps, "unused script steps"
    return tr, score_scenario(sc, tr["store"], tr["audit"], tr["turns"], env["lookup"], tr["nonce"],
                              tr.get("system_prompt"))
