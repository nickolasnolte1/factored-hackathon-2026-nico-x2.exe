"""Demo app server (app/server.py) with FastAPI's TestClient, a scripted model and the bank tools' fixture snapshot.
No network: the model is a fake and the clock's wall time is a counter the tests move.

    python -m pytest tests/app -q
"""
import datetime
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from src.bank_tools import Config, LocalRepository  # noqa: E402
from tests.bank_tools import fixture_data as fx  # noqa: E402

import app.server as srv  # noqa: E402

C1_DOC = fx.CUSTOMERS[fx.C1]["document"]      # a test customer (in the personas file)
C2_DOC = fx.CUSTOMERS[fx.C2]["document"]      # a real customer who is not a test customer
FULL_CUSTOMER_ID = re.compile(r"CLI-[A-Z0-9]{12}")


class FakeWall:
    """Monotonic wall time the test moves by hand."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += minutes * 60


class FakeLLM:
    """Scripted chat model. Each step is ("text", reply), ("tools", [(name, args), ...]) or a function of the
    messages that returns one of those. With no script left it answers with a short text."""
    endpoint = "fake-llm"

    def __init__(self):
        self.script = []
        self.calls = 0
        self.fail_with = None

    def chat(self, messages, tools, **kwargs):
        self.calls += 1
        if self.fail_with:
            raise self.fail_with
        step = self.script.pop(0) if self.script else ("text", "Listo.")
        if callable(step):
            step = step(messages)
        kind, payload = step
        base = {"usage": {"prompt_tokens": 10, "completion_tokens": 5}, "latency_ms": 1, "attempts": 1,
                "endpoint": self.endpoint}
        if kind == "text":
            return {**base, "content": payload, "tool_calls": []}
        calls = [{"id": "call_%d_%d" % (self.calls, i), "type": "function",
                  "function": {"name": name, "arguments": json.dumps(args)}} for i, (name, args) in enumerate(payload)]
        return {**base, "content": "", "tool_calls": calls}


def last_result(messages, tool):
    for m in reversed(messages):
        if m["role"] == "tool":
            body = json.loads(m["content"])
            if body.get("tool") == tool:
                return body
    raise AssertionError("no result of " + tool)


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory):
    return fx.write_snapshot(tmp_path_factory.mktemp("app_fixture") / "snapshot.sqlite")


@pytest.fixture
def personas_file(tmp_path):
    path = tmp_path / "personas.json"
    persona = {"persona_id": fx.C1[-4:], "first_name": "Rocío", "country_code": "CO", "customer_status": "Active",
               "segment": "Premium", "document_type": C1_DOC[0], "document_number": C1_DOC[1],
               "story": {"es": "Cliente regular", "pt": "Cliente comum"}}
    path.write_text(json.dumps({"synthetic": True, "personas": [persona]}, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def make(snapshot, personas_file, tmp_path):
    """make(**create_app overrides) -> Env with the app, a client, the fake model and the fake wall."""

    class Env:
        pass

    def factory(**overrides):
        env = Env()
        env.llm, env.wall = FakeLLM(), FakeWall()
        cfg = Config(env="demo", session_key="test-only-app-session-key-" + "0" * 20,
                     otp_key="test-only-app-otp-key-" + "0" * 24, audit_dir=str(tmp_path / "audit")).checked()
        args = {"llm": env.llm, "config": cfg, "repository": LocalRepository(str(snapshot), store_path=":memory:"),
                "classifier": None, "personas_path": personas_file, "demo_controls": True, "wall": env.wall}
        args.update(overrides)
        env.app = srv.create_app(**args)
        env.demo = env.app.state.demo
        env.client = TestClient(env.app)
        return env

    return factory


def new_conv(env, **body):
    out = env.client.post("/api/conversations", json=body).json()
    return out["conversation_id"], {"X-Conversation-Key": out["conversation_key"]}, out


def sign_in(env, cid, headers, doc=C1_DOC, reply="Hola, ya te identifiqué."):
    start = env.client.post(f"/api/conversations/{cid}/auth/start", headers=headers,
                            json={"document_type": doc[0], "document_number": doc[1]}).json()
    assert start["ok"], start
    code = env.client.get(f"/api/conversations/{cid}/phone", headers=headers).json()["code"]
    assert code
    env.llm.script = [("text", reply)]
    out = env.client.post(f"/api/conversations/{cid}/auth/verify", headers=headers, json={"code": code}).json()
    assert out["ok"] and out["signed_in"], out
    return out


def say(env, cid, headers, text, script=()):
    env.llm.script = list(script)
    return env.client.post(f"/api/conversations/{cid}/messages", headers=headers, json={"text": text})


def conv_now(env, cid, headers):
    return env.client.get(f"/api/conversations/{cid}/clock", headers=headers).json()["now"]


OVERVIEW = [("tools", [("get_customer_overview", {})]), ("text", "Aquí está tu resumen.")]


def create_case_script():
    """Turn 1: find and prepare the Super Ahorro purchase. Turn 2 (after the customer's yes): create the case."""
    find = ("tools", [("find_candidate_transactions", {"purpose": "dispute", "intent": "dispute_unrecognized_charge",
                                                       "hints": {}})])
    prepare = ("tools", [("prepare_dispute_case", {"transaction_id": fx.T["super"],
                                                   "intent": "dispute_unrecognized_charge", "language": "es"})])
    create = lambda ms: ("tools", [("create_dispute_case", {  # noqa: E731
        "confirmation_id": last_result(ms, "prepare_dispute_case")["data"]["confirmation_id"],
        "transaction_id": fx.T["super"], "customer_confirmed": True, "idempotency_key": "idem-" + str(len(ms)) * 4})])
    return [find, prepare, ("text", "Revisa los datos.")], [create, ("text", "Tu reclamo quedó registrado.")]


# -- conversation binding --------------------------------------------------------------------------------------------
def test_every_conversation_endpoint_needs_the_conversation_key(make):
    env = make()
    cid, headers, _ = new_conv(env)
    other_cid, other_headers, _ = new_conv(env)
    calls = [("post", f"/api/conversations/{cid}/messages", {"text": "hola"}),
             ("post", f"/api/conversations/{cid}/auth/start", {"document_type": "CC", "document_number": "1000000001"}),
             ("post", f"/api/conversations/{cid}/auth/verify", {"code": "123456"}),
             ("get", f"/api/conversations/{cid}/phone", None),
             ("get", f"/api/conversations/{cid}/trace", None),
             ("get", f"/api/conversations/{cid}/clock", None),
             ("post", "/api/demo/clock", {"conversation_id": cid, "minutes": 16})]
    for keys in ({}, {"X-Conversation-Key": "wrong"}, other_headers):
        for method, path, body in calls:
            res = env.client.request(method, path, headers=keys, json=body)
            assert res.status_code == 404, (path, keys, res.status_code)
    assert env.client.get(f"/api/conversations/{cid}/trace", headers=headers).status_code == 200
    assert say(env, cid, headers, "hola").status_code == 200
    assert env.client.get(f"/api/conversations/{other_cid}/trace", headers=other_headers).status_code == 200


def test_console_masks_customer_ids_and_hides_conversation_ids(make):
    env = make()
    cid, headers, opened = new_conv(env)
    sign_in(env, cid, headers)
    prepare = ("tools", [("prepare_dispute_case", {"transaction_id": fx.T["super"],
                                                   "intent": "dispute_unrecognized_charge", "language": "es"})])
    handoff = lambda ms: ("tools", [("handoff_to_human", {  # noqa: E731
        "reason_code": "explicit_human_request", "language": "es",
        "confirmation_id": last_result(ms, "prepare_dispute_case")["data"]["confirmation_id"],
        "package": {"request_summary": "Pide una persona para revisar " + fx.C2 + ".",
                    "verified_facts": ["Compra en Super Ahorro"], "actions_taken": [],
                    "evidence": [last_result(ms, "prepare_dispute_case")["meta"]["tool_call_id"]],
                    "open_questions": []}})])
    find = ("tools", [("find_candidate_transactions", {"purpose": "dispute", "intent": "dispute_unrecognized_charge",
                                                       "hints": {}})])
    out = say(env, cid, headers, "No reconozco la compra de Super Ahorro, quiero una persona",
              [find, prepare, handoff, ("text", "Te paso con un especialista.")]).json()
    assert [b["type"] for b in out["blocks"]][-1] == "ticket"

    console = env.client.get("/api/console").json()
    text = json.dumps(console, ensure_ascii=False)
    assert console["tickets"], console
    assert not FULL_CUSTOMER_ID.search(text)
    assert cid not in text and "conversation_id" not in text and "amount_usd" not in text
    ticket = console["tickets"][0]
    assert ticket["turn_label"] == opened["label"]
    assert ticket["customer_ref"] == "CLI-…" + fx.C1[-4:]
    assert ticket["service_verified"]["draft"]["case_fields"]["customer_ref"] == "CLI-…" + fx.C1[-4:]


def test_masked_masks_ids_inside_text_and_drops_internal_keys():
    row = {"customer_id": fx.C1, "conversation_id": "c-1", "notes": ["see " + fx.C2], "amount_usd": 3.0,
           "nested": {"customer_id": fx.C2, "session_id_hash": "x"}}
    out = srv.masked(row)
    assert out == {"customer_ref": "CLI-…" + fx.C1[-4:], "notes": ["see CLI-…" + fx.C2[-4:]],
                   "nested": {"customer_ref": "CLI-…" + fx.C2[-4:]}}


# -- demo clock ------------------------------------------------------------------------------------------------------
def test_clock_moves_one_conversation_only_and_follows_wall_time(make):
    env = make()
    a, ha, _ = new_conv(env)
    b, hb, _ = new_conv(env)
    start = conv_now(env, a, ha)
    assert start == srv.DEMO_NOW
    moved = env.client.post("/api/demo/clock", headers=ha, json={"conversation_id": a, "minutes": 16}).json()
    assert moved["now"] == "2026-06-19T09:16:00" and moved["added_minutes"] == 16
    assert conv_now(env, b, hb) == "2026-06-19T09:00:00"
    c, hc, opened = new_conv(env)  # a new conversation never moves another one
    assert opened["now"] == "2026-06-19T09:00:00"
    assert conv_now(env, a, ha) == "2026-06-19T09:16:00"
    env.wall.advance(5)
    assert conv_now(env, a, ha) == "2026-06-19T09:21:00"
    assert conv_now(env, b, hb) == "2026-06-19T09:05:00"
    assert env.client.get("/api/clock").json()["now"] == "2026-06-19T09:05:00"
    for _ in range(10):
        last = env.client.post("/api/demo/clock", headers=ha, json={"conversation_id": a, "minutes": 16}).json()
    assert last["offset_minutes"] == srv.MAX_OFFSET_MIN and last["added_minutes"] == 0
    assert conv_now(env, c, hc) == "2026-06-19T09:05:00"


def test_session_expiry_follows_each_conversation_clock(make):
    env = make()
    a, ha, _ = new_conv(env)
    b, hb, _ = new_conv(env)
    sign_in(env, a, ha)
    sign_in(env, b, hb)
    env.client.post("/api/demo/clock", headers=ha, json={"conversation_id": a, "minutes": 16})
    expired = say(env, a, ha, "mi saldo", OVERVIEW).json()
    assert expired["turn"]["tools"][0]["error"] == "SESSION_EXPIRED"
    assert {"type": "auth_required", "expired": True} in expired["blocks"]
    assert expired["signed_in"] is True  # the runtime still holds the (expired) token; the form asks again
    still = say(env, b, hb, "mi saldo", OVERVIEW).json()
    assert still["turn"]["tools"][0]["ok"] is True


def test_moving_one_clock_keeps_other_conversations_challenges(make):
    env = make()
    a, ha, _ = new_conv(env)
    b, hb, _ = new_conv(env)
    start = env.client.post(f"/api/conversations/{b}/auth/start", headers=hb,
                            json={"document_type": C1_DOC[0], "document_number": C1_DOC[1]}).json()
    code = env.client.get(f"/api/conversations/{b}/phone", headers=hb).json()["code"]
    assert start["ok"] and code
    env.client.post("/api/demo/clock", headers=ha, json={"conversation_id": a, "minutes": 30})
    env.client.post(f"/api/conversations/{a}/auth/start", headers=ha,  # purges state at a's later time
                    json={"document_type": C1_DOC[0], "document_number": C1_DOC[1]})
    env.llm.script = [("text", "Hola.")]
    out = env.client.post(f"/api/conversations/{b}/auth/verify", headers=hb, json={"code": code}).json()
    assert out["ok"], out


# -- rate limits -----------------------------------------------------------------------------------------------------
def test_same_test_customer_signs_in_many_times(make):
    env = make()
    for _ in range(12):  # the bank tool default allows 5 challenges per document per hour
        cid, headers, _ = new_conv(env)
        sign_in(env, cid, headers)
        env.wall.advance(1)


def test_rate_limited_sign_in_reports_the_wait_and_the_window_slides(make):
    env = make()
    cid, headers, _ = new_conv(env)
    body = {"document_type": C1_DOC[0], "document_number": C1_DOC[1]}
    limit = srv.APP_LIMITS["challenges_per_conversation"]
    for _ in range(limit):
        assert env.client.post(f"/api/conversations/{cid}/auth/start", headers=headers, json=body).json()["ok"]
    refused = env.client.post(f"/api/conversations/{cid}/auth/start", headers=headers, json=body).json()
    assert refused["ok"] is False and refused["error"] == "RATE_LIMITED" and refused["retry_after_s"] > 0
    env.wall.advance(16)  # the per-conversation window is 15 minutes of service time, which now passes
    assert env.client.post(f"/api/conversations/{cid}/auth/start", headers=headers, json=body).json()["ok"]


# -- demo controls ---------------------------------------------------------------------------------------------------
def test_demo_controls_off_removes_phone_personas_and_clock(make):
    env = make(demo_controls=False)
    cfg = env.client.get("/api/config").json()
    assert cfg["demo_controls"] is False and cfg["personas"] == []
    cid, headers, _ = new_conv(env)
    assert env.client.get(f"/api/conversations/{cid}/phone", headers=headers).status_code == 404
    res = env.client.post("/api/demo/clock", headers=headers, json={"conversation_id": cid, "minutes": 16})
    assert res.status_code == 404
    assert env.client.get(f"/api/conversations/{cid}/clock", headers=headers).status_code == 200


def test_demo_controls_on_serves_personas_without_internal_fields(make):
    env = make()
    cfg = env.client.get("/api/config").json()
    assert cfg["demo_controls"] is True
    (persona,) = cfg["personas"]
    assert persona["first_name"] == "Rocío"  # read as UTF-8 on every platform
    assert persona["persona_id"] == "P1" and "segment" not in persona


def test_phone_shows_codes_only_for_test_customers(make):
    env = make()
    for doc, shown in ((C2_DOC, False), (fx.UNKNOWN_DOCUMENT, False), (C1_DOC, True)):
        cid, headers, _ = new_conv(env)
        start = env.client.post(f"/api/conversations/{cid}/auth/start", headers=headers,
                                json={"document_type": doc[0], "document_number": doc[1]}).json()
        assert start["ok"]
        code = env.client.get(f"/api/conversations/{cid}/phone", headers=headers).json()["code"]
        assert bool(code) is shown, doc


# -- input limits and conversation lifetime --------------------------------------------------------------------------
def test_input_limits(make):
    env = make(max_turns=3)
    cid, headers, _ = new_conv(env)
    assert say(env, cid, headers, "x" * (srv.MAX_TEXT + 1)).status_code == 422
    big = json.dumps({"text": "y" * (srv.MAX_BODY_BYTES + 10)})
    res = env.client.post(f"/api/conversations/{cid}/messages", headers={**headers, "Content-Type": "application/json"},
                          content=big)
    assert res.status_code == 413
    res = env.client.post(f"/api/conversations/{cid}/auth/start", headers=headers,
                          json={"document_type": "CC", "document_number": "9" * 40})
    assert res.status_code == 422
    assert say(env, cid, headers, "   ").status_code == 400
    for _ in range(3):
        assert say(env, cid, headers, "hola").status_code == 200
    res = say(env, cid, headers, "hola otra vez")
    assert res.status_code == 429 and res.json()["detail"]["code"] == "turn_limit"


def test_idle_conversations_end_and_the_service_forgets_them(make):
    env = make(idle_minutes=60)
    cid, headers, opened = new_conv(env)
    sign_in(env, cid, headers)
    ended = []
    inner = env.demo.service._inner
    original = inner.end_conversation
    inner.end_conversation = lambda conv_id: (ended.append(conv_id), original(conv_id))[1]
    env.wall.advance(30)
    assert say(env, cid, headers, "sigo aquí").status_code == 200
    env.wall.advance(40)
    assert conv_now(env, cid, headers)  # the page's clock reads do not keep a conversation alive
    env.wall.advance(21)
    assert env.client.get(f"/api/conversations/{cid}/trace", headers=headers).status_code == 404
    assert ended == [cid]
    assert env.demo.agent.get(cid) is None
    assert env.demo.labels[cid] == opened["label"]  # the console still names the turn


def test_new_conversation_ends_the_previous_one_only_with_its_key(make):
    env = make()
    a, ha, _ = new_conv(env)
    b, hb, _ = new_conv(env)
    env.client.post("/api/conversations", json={"previous_id": a})  # no key: nothing ends
    assert env.client.get(f"/api/conversations/{a}/trace", headers=ha).status_code == 200
    env.client.post("/api/conversations", headers=ha, json={"previous_id": a})
    assert env.client.get(f"/api/conversations/{a}/trace", headers=ha).status_code == 404
    assert env.client.get(f"/api/conversations/{b}/trace", headers=hb).status_code == 200


def test_busy_conversation_gets_409_while_others_keep_working(make):
    env = make()
    a, ha, _ = new_conv(env)
    b, hb, _ = new_conv(env)
    handle = env.demo.handles[a]
    handle.lock.acquire()
    try:
        res = say(env, a, ha, "hola")
        assert res.status_code == 409 and res.json()["detail"]["code"] == "busy"
        moved = env.client.post("/api/demo/clock", headers=ha, json={"conversation_id": a, "minutes": 16})
        assert moved.status_code == 200
        sign_in(env, b, hb)
    finally:
        handle.lock.release()
    assert say(env, a, ha, "hola").status_code == 200


# -- sign-in ---------------------------------------------------------------------------------------------------------
def test_another_customer_cannot_sign_in_inside_the_same_conversation(make):
    env = make()
    cid, headers, _ = new_conv(env)
    sign_in(env, cid, headers)
    other = env.client.post(f"/api/conversations/{cid}/auth/start", headers=headers,
                            json={"document_type": C2_DOC[0], "document_number": C2_DOC[1]}).json()
    assert other == {"ok": False, "error": "OTHER_CUSTOMER"}
    same = "1.000.000.001"  # the same document, typed with separators (re-authentication after an expiry)
    assert same.replace(".", "") == C1_DOC[1]
    again = env.client.post(f"/api/conversations/{cid}/auth/start", headers=headers,
                            json={"document_type": C1_DOC[0], "document_number": same}).json()
    assert again["ok"]


def test_sign_in_stands_when_the_model_fails(make):
    env = make()
    cid, headers, _ = new_conv(env)
    env.client.post(f"/api/conversations/{cid}/auth/start", headers=headers,
                    json={"document_type": C1_DOC[0], "document_number": C1_DOC[1]})
    code = env.client.get(f"/api/conversations/{cid}/phone", headers=headers).json()["code"]
    env.llm.fail_with = RuntimeError("endpoint down")
    out = env.client.post(f"/api/conversations/{cid}/auth/verify", headers=headers, json={"code": code}).json()
    assert out["ok"] and out["signed_in"] and out["reply"]
    assert out["session"]["expires_at"] == "2026-06-19T09:15:00"


# -- cards -----------------------------------------------------------------------------------------------------------
def test_case_card_says_when_the_case_already_existed(make):
    env = make()
    first_turn, second_turn = create_case_script()
    flags = []
    for _ in range(2):
        cid, headers, _ = new_conv(env)
        sign_in(env, cid, headers)
        say(env, cid, headers, "No reconozco una compra", first_turn)
        out = say(env, cid, headers, "Sí, confirmo", second_turn).json()
        (case,) = [b for b in out["blocks"] if b["type"] == "case"]
        flags.append(case["already_existed"])
    assert flags == [False, True]
    console = env.client.get("/api/console").json()
    assert len(console["cases"]) == 1 and console["cases"][0]["turn_label"] == "R-201"
    assert not FULL_CUSTOMER_ID.search(json.dumps(console))


def test_static_page_is_served(make):
    env = make()
    page = env.client.get("/")
    assert page.status_code == 200 and "/static/app.js" in page.text
    assert env.client.get("/static/app.js").status_code == 200


# -- shared limits and the demo clock ----------------------------------------------------------------------------------
def start_sign_in(env, cid, headers, doc):
    return env.client.post(f"/api/conversations/{cid}/auth/start", headers=headers,
                           json={"document_type": doc[0], "document_number": doc[1]}).json()


def test_a_moved_clock_cannot_reset_the_per_document_sign_in_limit(make):
    env = make()
    limit = srv.Config.challenges_per_document  # a document that is not a test customer keeps the default
    for _ in range(limit):
        cid, headers, _ = new_conv(env)
        assert start_sign_in(env, cid, headers, C2_DOC)["ok"]
    cid, headers, _ = new_conv(env)
    assert start_sign_in(env, cid, headers, C2_DOC)["error"] == "RATE_LIMITED"
    moved, moved_headers, _ = new_conv(env)  # +61 min used to empty the shared bucket for everyone
    env.client.post("/api/demo/clock", headers=moved_headers, json={"conversation_id": moved, "minutes": 61})
    refused = start_sign_in(env, moved, moved_headers, C2_DOC)
    assert refused["error"] == "RATE_LIMITED" and refused["retry_after_s"] > 3000
    cid, headers, _ = new_conv(env)
    assert start_sign_in(env, cid, headers, C2_DOC)["error"] == "RATE_LIMITED"
    env.wall.advance(61)  # the window slides with wall time
    cid, headers, _ = new_conv(env)
    assert start_sign_in(env, cid, headers, C2_DOC)["ok"]


def test_a_moved_clock_cannot_lock_a_test_customer_out_for_longer(make):
    env = make()
    limit = srv.DEMO_LIMITS["challenges_per_document"]
    while limit > 0:  # every sign-in start of a test customer's document, from conversations moved +120 min
        cid, headers, _ = new_conv(env)
        env.client.post("/api/demo/clock", headers=headers, json={"conversation_id": cid, "minutes": 120})
        for _ in range(min(limit, srv.APP_LIMITS["challenges_per_conversation"])):
            assert start_sign_in(env, cid, headers, C1_DOC)["ok"]
            limit -= 1
    cid, headers, _ = new_conv(env)
    refused = start_sign_in(env, cid, headers, C1_DOC)
    assert refused["error"] == "RATE_LIMITED" and refused["retry_after_s"] <= 3600
    env.wall.advance(61)  # before, the stamps of +120 min conversations kept it locked for 3 hours
    cid, headers, _ = new_conv(env)
    assert start_sign_in(env, cid, headers, C1_DOC)["ok"]


# -- conversation registry -------------------------------------------------------------------------------------------
def test_a_full_registry_never_ends_active_conversations(make, monkeypatch):
    monkeypatch.setattr(srv, "MAX_CONVERSATIONS", 3)
    env = make()
    convs = [new_conv(env)[:2] for _ in range(3)]
    res = env.client.post("/api/conversations", json={"language": "es"})
    assert res.status_code == 503 and res.json()["detail"]["code"] == "server_busy"
    for cid, headers in convs:
        assert env.client.get(f"/api/conversations/{cid}/trace", headers=headers).status_code == 200
    env.wall.advance(6)
    for cid, headers in convs[1:]:  # still in use
        assert say(env, cid, headers, "hola").status_code == 200
    assert env.client.post("/api/conversations", json={"language": "es"}).status_code == 200
    first, first_headers = convs[0]  # idle longest, beyond FULL_IDLE_S: it made room
    assert env.client.get(f"/api/conversations/{first}/trace", headers=first_headers).status_code == 404


def test_each_client_opens_a_bounded_number_of_conversations(make, monkeypatch):
    monkeypatch.setattr(srv, "NEW_CONVERSATIONS", (2, 600))
    env = make()
    for _ in range(2):
        assert env.client.post("/api/conversations", json={"language": "es"}).status_code == 200
    res = env.client.post("/api/conversations", json={"language": "es"})
    assert res.status_code == 429 and res.json()["detail"]["code"] == "too_many_conversations"
    other = {"X-Forwarded-Email": "otra.persona@example.com"}
    assert env.client.post("/api/conversations", headers=other, json={"language": "es"}).status_code == 200
    env.wall.advance(11)
    assert env.client.post("/api/conversations", json={"language": "es"}).status_code == 200


def test_turns_wait_for_a_free_model_slot(make, monkeypatch):
    monkeypatch.setattr(srv, "TURN_SLOT_WAIT_S", 0.05)
    env = make(max_concurrent=1)
    cid, headers, _ = new_conv(env)
    env.demo.turn_slots.acquire()
    try:
        res = say(env, cid, headers, "hola")
        assert res.status_code == 503 and res.json()["detail"]["code"] == "server_busy"
    finally:
        env.demo.turn_slots.release()
    out = say(env, cid, headers, "hola")
    assert out.status_code == 200 and out.json()["turns_left"] == env.demo.max_turns - 1  # the refused one not counted


def test_api_posts_must_be_json_and_responses_carry_security_headers(make):
    env = make()
    res = env.client.post("/api/conversations", content=b'{"language": "es"}', headers={"Content-Type": "text/plain"})
    assert res.status_code == 415
    res = env.client.post("/api/conversations", content=b'{"language": "es"}')
    assert res.status_code == 415
    page = env.client.get("/")
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    assert env.client.get("/api/config").headers["x-frame-options"] == "DENY"


def test_a_non_ascii_conversation_key_is_just_a_wrong_key(make):
    env = make()
    cid, headers, _ = new_conv(env)
    res = env.client.get(f"/api/conversations/{cid}/trace", headers={"X-Conversation-Key": b"\xe9" * 10})
    assert res.status_code == 404
    res = env.client.post("/api/conversations", headers={"X-Conversation-Key": b"\xe9" * 10},
                          json={"language": "es", "previous_id": cid})
    assert res.status_code == 200
    assert env.client.get(f"/api/conversations/{cid}/trace", headers=headers).status_code == 200


# -- console access ----------------------------------------------------------------------------------------------------
def test_console_is_off_without_demo_controls_or_an_allow_list(make):
    env = make(demo_controls=False, console_users=[])
    assert env.client.get("/api/config").json()["console"] is False
    assert env.client.get("/api/console").status_code == 404
    assert env.client.get("/api/console/count").status_code == 404
    env = make(demo_controls=False, console_users=["especialista@banco.example"])
    assert env.client.get("/api/console").status_code == 403
    allowed = {"X-Forwarded-Email": "Especialista@banco.example"}
    assert env.client.get("/api/console", headers=allowed).status_code == 200


def test_the_badge_reads_ticket_ids_only(make):
    env = make()
    cid, headers, _ = new_conv(env)
    handoff = ("tools", [("handoff_to_human", {"reason_code": "explicit_human_request", "language": "es",
                                               "package": {"request_summary": "Pide una persona.",
                                                           "verified_facts": [], "actions_taken": [], "evidence": [],
                                                           "open_questions": []}})])
    say(env, cid, headers, "Quiero hablar con una persona", [handoff, ("text", "Te paso con un especialista.")])
    count = env.client.get("/api/console/count").json()
    console = env.client.get("/api/console").json()
    assert count == {"ticket_ids": [t["ticket_id"] for t in console["tickets"]]} and len(count["ticket_ids"]) == 1
    assert set(console["tickets"][0]) == set(srv.CONSOLE_TICKET_FIELDS)


# -- public demo (Hugging Face Space) ------------------------------------------------------------------------------------
PUBLIC_VARS = ("APP_DAILY_MODEL_TURNS", "APP_TRUST_FORWARDED_FOR", "APP_PUBLIC_DEMO", "APP_FRAME_ANCESTORS",
               "APP_MAX_CONCURRENT_TURNS")
DAY = datetime.date(2026, 10, 4)


def test_public_demo_settings_are_off_by_default(make, monkeypatch):
    for var in PUBLIC_VARS:
        monkeypatch.delenv(var, raising=False)
    env = make()
    cfg = env.client.get("/api/config").json()
    assert cfg["public_demo"] is False and cfg["daily_limit_reached"] is False
    assert cfg["limits"]["daily_model_turns"] == 0
    assert env.demo.daily.limit == 0 and env.demo.trust_forwarded_for is False and env.demo.max_concurrent == 8
    page = env.client.get("/")
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    for _ in range(3):
        assert say(env, *new_conv(env)[:2], "hola").status_code == 200  # no daily cap


def test_public_demo_settings_come_from_the_environment(make, monkeypatch):
    for var, value in (("APP_DAILY_MODEL_TURNS", "400"), ("APP_TRUST_FORWARDED_FOR", "1"), ("APP_PUBLIC_DEMO", "1"),
                       ("APP_FRAME_ANCESTORS", "https://huggingface.co"), ("APP_MAX_CONCURRENT_TURNS", "3")):
        monkeypatch.setenv(var, value)
    env = make()
    cfg = env.client.get("/api/config").json()
    assert cfg["public_demo"] is True and cfg["limits"]["daily_model_turns"] == 400
    assert env.demo.trust_forwarded_for is True and env.demo.max_concurrent == 3
    page = env.client.get("/")
    assert "frame-ancestors https://huggingface.co" in page.headers["content-security-policy"]
    assert "x-frame-options" not in page.headers and page.headers["x-content-type-options"] == "nosniff"


def test_frame_ancestors_takes_https_origins_only(make):
    for bad in ("http://huggingface.co", "https://huggingface.co; script-src *", "'self'", "*"):
        with pytest.raises(ValueError):
            make(frame_ancestors=bad)
    env = make(frame_ancestors="https://huggingface.co https://*.hf.space")
    assert "frame-ancestors https://huggingface.co https://*.hf.space" in \
        env.client.get("/").headers["content-security-policy"]


def test_daily_cap_counts_customer_and_sign_in_turns_and_resets_at_utc_midnight(make):
    env = make(daily_turns=2)
    env.demo.daily.today = lambda: DAY
    cid, headers, opened = new_conv(env)
    assert opened["daily_limit_reached"] is False
    sign_in(env, cid, headers)                                      # 1: the sign-in turn
    assert say(env, cid, headers, "hola").status_code == 200        # 2
    calls = env.llm.calls
    res = say(env, cid, headers, "hola otra vez")
    detail = res.json()["detail"]
    assert res.status_code == 429 and detail == {"code": "daily_limit", "limit": 2, "resets_at": "2026-10-05T00:00:00Z"}
    assert env.llm.calls == calls  # the refused turn never reached the model
    other, other_headers, opened = new_conv(env)  # the cap is global: every conversation sees it
    assert opened["daily_limit_reached"] is True and env.client.get("/api/config").json()["daily_limit_reached"]
    res = env.client.post(f"/api/conversations/{other}/auth/start", headers=other_headers,
                          json={"document_type": C1_DOC[0], "document_number": C1_DOC[1]})
    assert res.status_code == 429 and res.json()["detail"]["code"] == "daily_limit"
    env.demo.daily.today = lambda: DAY + datetime.timedelta(days=1)  # 00:00 UTC
    out = say(env, cid, headers, "hola otra vez")
    assert out.status_code == 200 and out.json()["turns_left"] == env.demo.max_turns - 2  # the refused one not counted


def test_a_sign_in_with_a_wrong_code_does_not_use_the_daily_cap(make):
    env = make(daily_turns=1)
    env.demo.daily.today = lambda: DAY
    cid, headers, _ = new_conv(env)
    assert start_sign_in(env, cid, headers, C1_DOC)["ok"]
    code = env.client.get(f"/api/conversations/{cid}/phone", headers=headers).json()["code"]
    wrong = str((int(code) + 1) % 10 ** 6).zfill(6)
    out = env.client.post(f"/api/conversations/{cid}/auth/verify", headers=headers, json={"code": wrong}).json()
    assert out["ok"] is False and out["reason"] == "invalid_code"
    env.llm.script = [("text", "Hola, ya te identifiqué.")]
    out = env.client.post(f"/api/conversations/{cid}/auth/verify", headers=headers, json={"code": code}).json()
    assert out["ok"] and out["signed_in"]  # the only turn of the day
    res = say(env, cid, headers, "hola")
    assert res.status_code == 429 and res.json()["detail"]["code"] == "daily_limit"


def test_daily_cap_takes_and_gives_back_on_the_same_day_only():
    day = [DAY]
    cap = srv.DailyCap(2, today=lambda: day[0])
    first = cap.take()
    cap.take()
    assert cap.reached()
    with pytest.raises(srv.HTTPException):
        cap.take()
    cap.give_back(first)
    assert not cap.reached()
    cap.take()
    day[0] = DAY + datetime.timedelta(days=1)
    cap.give_back(first)  # yesterday's turn: today's count stays at 0
    assert cap.take() == day[0] and not cap.reached()
    off = srv.DailyCap(0)
    assert off.take() is None and not off.reached()


def test_conversation_limit_keys_on_the_first_forwarded_for_address_when_trusted(make, monkeypatch):
    monkeypatch.setattr(srv, "NEW_CONVERSATIONS", (2, 600))
    first = {"X-Forwarded-For": "203.0.113.7, 10.0.0.2"}
    second = {"X-Forwarded-For": "198.51.100.9, 10.0.0.2"}

    def opens(env, headers):
        return env.client.post("/api/conversations", headers=headers, json={"language": "es"}).status_code

    env = make(trust_forwarded_for=True)
    assert [opens(env, first) for _ in range(3)] == [200, 200, 429]
    assert [opens(env, second) for _ in range(3)] == [200, 200, 429]  # another visitor behind the same proxy
    assert [opens(env, {}) for _ in range(3)] == [200, 200, 429]      # no header: the client address, as before
    env = make()  # not trusted (the default): the header is ignored and every request is the proxy's address
    assert [opens(env, first), opens(env, second), opens(env, {})] == [200, 200, 429]
