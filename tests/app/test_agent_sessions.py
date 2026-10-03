"""Sign-in events in app/agent.py with the real bank service: the first sign-in, a new sign-in after an earlier
session (earlier confirmations are void), a sign-in as a different customer (the model context restarts), and a
restricted customer (the service restriction reaches the model)."""
import json

from tests.agent_eval.builders import last_result
from tests.app.helpers import create_prepared, find, prepare_found
from tests.bank_tools import fixture_data as fx

VERIFY = {"session_ref": "S-0000test", "expires_at": "2026-06-19T09:15:00"}


def event_payload(messages):
    content = [m for m in messages if m["role"] == "user"][-1]["content"]
    assert content.startswith('<app-event id="')
    return json.loads(content.split(">", 1)[1].rsplit("</app-event>", 1)[0])


def test_first_sign_in_resumes_the_request(bank):
    agent, llm = bank.agent([[("get_customer_overview", {})], "Para eso necesito que verifiques tu identidad.",
                             "Listo, ya puedo ver tus cuentas."])
    conv = agent.new_conversation("es")
    agent.customer_turn(conv, "No reconozco una compra en Super Ahorro")
    conv.session_token = bank.session(conv)
    out = agent.signed_in(conv, VERIFY)
    payload = event_payload(llm.seen[-1])
    assert payload["event"] == "customer_signed_in" and payload["country_code"] == "CO"
    assert payload["spanish_register"] == "usted"
    assert payload["service_restriction"] == {"handoff_required": False, "handoff_reason": None}
    assert "void" not in payload["instruction"] and "reset" not in payload["instruction"]
    assert out["reply"] == "Listo, ya puedo ver tus cuentas."
    assert conv.subject and conv.subject not in json.dumps(llm.seen[-1]) and conv.subject not in json.dumps(out)
    runtime = [r["tool"] for r in bank.audit.records if r.get("caller") == "runtime" and r["tool"][0] != "_"]
    assert runtime == ["get_customer_overview", "list_products"]


def test_a_new_sign_in_voids_earlier_confirmations(bank):
    steps = ["Hola, ¿en qué le ayudo?",
             [("get_customer_overview", {}), find()], prepare_found, "¿Confirma los datos del recuadro?",
             create_prepared, "Su sesión venció; verifique su identidad de nuevo.",
             lambda m: [("prepare_dispute_case", {"transaction_id": fx.T["super"],
                                                  "intent": "dispute_unrecognized_charge", "language": "es"})],
             "Revise el recuadro con los datos y confírmeme si son correctos."]
    agent, llm = bank.agent(steps)
    conv = agent.new_conversation("es")
    conv.session_token = bank.session(conv)
    agent.signed_in(conv, VERIFY)
    agent.customer_turn(conv, "No reconozco una compra en Super Ahorro")
    bank.clock.advance(16 * 60)  # past the 15-minute session
    out = agent.customer_turn(conv, "Sí, confirmo.")
    assert out["turn"]["tools"][0]["error"] == "SESSION_EXPIRED"
    before = len(conv.messages)
    conv.session_token = bank.session(conv)
    out = agent.signed_in(conv, VERIFY)
    payload = event_payload(llm.seen[-2])
    assert "confirmations and confirmation_ids from before it are void" in payload["instruction"]
    assert "call prepare_dispute_case again" in payload["instruction"]
    assert "reset" not in payload["instruction"] and len(conv.messages) > before  # same customer: context kept
    assert [b["type"] for b in out["blocks"]] == ["confirm"]


def test_a_different_customer_gets_a_fresh_context(bank):
    steps = ["Hola, ¿en qué le ayudo?", [("get_customer_overview", {}), ("list_recent_transactions", {})],
             "Estos son sus movimientos.", "Listo, ¿qué movimiento revisamos?"]
    agent, llm = bank.agent(steps)
    conv = agent.new_conversation("es")
    conv.session_token = bank.session(conv, fx.C1)
    agent.signed_in(conv, VERIFY)
    first_subject = conv.subject
    agent.customer_turn(conv, "Quiero ver mis movimientos recientes")
    assert any(m["role"] == "tool" for m in conv.messages) and conv.verified_ids == set()
    conv.session_token = bank.session(conv, fx.C2)
    agent.signed_in(conv, VERIFY)
    seen = llm.seen[-1]
    assert conv.subject != first_subject
    assert [m["role"] for m in seen] == ["system", "user", "user"]
    assert seen[1]["content"] == "Quiero ver mis movimientos recientes"
    payload = event_payload(seen)
    assert payload["country_code"] == "AR" and "context was reset" in payload["instruction"]
    assert "Estos son sus movimientos" not in json.dumps(seen) and fx.T["super"] not in json.dumps(seen)


def test_a_restricted_customer_gets_the_restriction_in_the_event(bank):
    handoff = [("handoff_to_human", {"reason_code": "customer_status_restricted", "language": "es", "package": {
        "request_summary": "Cliente con cuenta restringida pide ayuda.", "verified_facts": [], "actions_taken": [],
        "evidence": [], "open_questions": []}})]
    agent, llm = bank.agent([handoff, lambda m: "Le pasé con un especialista (ticket "
                             + last_result(m, "handoff_to_human")["data"]["ticket_id"] + ")."])
    conv = agent.new_conversation("es")
    conv.session_token = bank.session(conv, fx.C4)
    agent.signed_in(conv, VERIFY)
    payload = event_payload(llm.seen[0])
    assert payload["service_restriction"] == {"handoff_required": True, "handoff_reason": "customer_status_restricted"}
    assert "do not serve the request" in payload["instruction"]
    runtime = [r["tool"] for r in bank.audit.records if r.get("caller") == "runtime" and r["tool"][0] != "_"]
    assert runtime == ["get_customer_overview"]  # no product read for a restricted customer
    assert bank.rows("handoff_tickets")[0]["reason_code"] == "customer_status_restricted"
