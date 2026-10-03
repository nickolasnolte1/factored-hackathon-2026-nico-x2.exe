"""A scripted fake model and a small bank wrapper for the app runtime tests."""
import json

from src.bank_tools import BankService, FaultInjector, FixedClock, ListAuditSink, LocalRepository
from src.bank_tools.clock import iso
from tests.agent_eval.builders import last_result
from tests.bank_tools import fixture_data as fx

SUPER_HINTS = {"merchant": "Super Ahorro", "amount": 250000, "currency": "COP", "date": "2026-06-15"}
UNRECOGNIZED = "dispute_unrecognized_charge"


class ScriptedLLM:
    """Plays a script, one step per model call: a reply text, a list of (tool, args) calls (args may be a raw
    string), an exception to raise, or a function of the messages that returns one of those."""
    endpoint = "fake"

    def __init__(self, steps):
        self.steps, self.calls, self.seen = list(steps), 0, []

    def chat(self, messages, tools):
        self.seen.append(json.loads(json.dumps(messages)))
        step = self.steps.pop(0)
        if callable(step):
            step = step(messages)
        if isinstance(step, BaseException):
            raise step
        self.calls += 1
        base = {"usage": {"prompt_tokens": 100, "completion_tokens": 10}, "latency_ms": 3, "attempts": 1,
                "endpoint": "fake"}
        if isinstance(step, str):
            return dict(base, content=step, tool_calls=[])
        calls = [{"id": f"call_{self.calls}_{i}", "type": "function",
                  "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}
                 for i, (name, args) in enumerate(step)]
        return dict(base, content="", tool_calls=calls)


class Bank:
    """The real service in env eval over the fixture snapshot, with a fresh in-memory store."""

    def __init__(self, env, faults=()):
        self.env = env
        self.repo = LocalRepository(env["snapshot"])
        self.clock = FixedClock(fx.NOW)
        self.audit = ListAuditSink()
        self.svc = BankService(self.repo, self.clock, self.audit, FaultInjector(list(faults), env=env["cfg"].env),
                               env["cfg"], schemas=env["schemas"], policy=env["pol"])

    def agent(self, steps, classifier=None):
        from app.agent import Agent
        llm = ScriptedLLM(steps)
        return Agent(self.svc, llm, classifier=classifier), llm

    def session(self, conv, customer=fx.C1):
        return self.svc.identity.issue_test_session(customer, authenticated_at=iso(self.clock.now()),
                                                    conversation_id=conv.id)

    def rows(self, table):
        return self.repo.store_rows(table)

    def model_records(self, tool=None):
        return [r for r in self.audit.records if r.get("caller") == "model" and (tool is None or r["tool"] == tool)]


def find(hints=None, intent=UNRECOGNIZED):
    return ("find_candidate_transactions", {"purpose": "dispute", "intent": intent, "hints": hints or SUPER_HINTS})


def prepare_found(messages, language="es"):
    tid = last_result(messages, "find_candidate_transactions")["data"]["candidates"][0]["transaction_id"]
    return [("prepare_dispute_case", {"transaction_id": tid, "intent": UNRECOGNIZED, "language": language})]


def create_prepared(messages, key="key-000001"):
    data = last_result(messages, "prepare_dispute_case")["data"]
    return [("create_dispute_case", {"confirmation_id": data["confirmation_id"],
                                     "transaction_id": data["verified_facts"]["transaction_id"],
                                     "customer_confirmed": True, "idempotency_key": key})]


def tool_messages(messages, tool):
    """The tool results (as the model saw them) of one tool, in order."""
    out = []
    for m in messages:
        if m["role"] == "tool":
            env = json.loads(m["content"])
            if env.get("tool") == tool:
                out.append(env)
    return out


def prepared_turn(bank, extra_steps=(), classifier=None, text="Hola, no reconozco una compra en Super Ahorro."):
    """An agent and conversation (signed in as C1) where turn 1 found and prepared the Super Ahorro movement."""
    steps = [[("get_customer_overview", {}), find()], prepare_found,
             "Revisa el recuadro con los datos y confírmame si es correcto."] + list(extra_steps)
    agent, llm = bank.agent(steps, classifier=classifier)
    conv = agent.new_conversation("es")
    conv.session_token = bank.session(conv)
    conv.country_code = "CO"
    out = agent.customer_turn(conv, text)
    return agent, llm, conv, out
