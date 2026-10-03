"""The real Agent (app/agent.py) driven end to end by a scripted fake model on the fixture snapshot, through the
harness, then scored: a correct dispute, a creation attempt the service blocks for lack of confirmation, a handoff
with the wrong reason, and a reply in the wrong language. Also: transcripts round-trip to identical results, the 429
backoff, and the CLI's test-split guard."""
import json

import pytest

from app.llm import DatabricksChat, LLMError
from src.agent_eval import harness, report, run as cli
from src.agent_eval.score import score_scenario
from tests.agent_eval import builders as b
from tests.agent_eval.builders import (SUPER_HINTS, FakeLLM, create_from_prepare, find_args, last_result, pick,
                                       prepare_from_find)
from tests.bank_tools import fixture_data as fx


drive = b.drive


def correct_steps():
    return [
        [("get_customer_overview", {}), ("find_candidate_transactions", find_args(SUPER_HINTS))],
        pick("find_candidate_transactions", prepare_from_find),
        "Encontré el movimiento en Super Ahorro. ¿Me confirmas que es este?",
        pick("prepare_dispute_case", create_from_prepare),
        lambda m: "Listo, registré tu reclamo con el número " + last_result(m, "create_dispute_case")["data"][
            "case_id"] + ". Te responderemos dentro de 24 horas.",
    ]


def test_correct_dispute_is_a_success(env):
    sc = b.scenario(claim=SUPER_HINTS)
    tr, v = drive(env, sc, correct_steps())
    assert v["success"], v["failed"]
    assert v["reached"]["outcome"] == "create_case" and v["metrics"]["case_number_given"] is True
    assert not any(x["violated"] for x in v["must_not"].values())
    assert not any(x["violated"] for x in v["reply_checks"].values()), v["reply_checks"]
    assert v["grounding_violations"] == []
    assert [d["met"] for d in v["diagnostics"]] == [True, None]
    # the harness: test session bound to the conversation, country read by the runtime, clock moved per turn
    assert tr["session"]["matches_scenario"] and tr["country_code"] == "CO"
    assert [t["clock"] for t in tr["turns"]] == ["2026-06-19T09:00:00", "2026-06-19T09:01:00"]
    assert {r["caller"] for r in tr["audit"]} == {"runtime", "model"}
    assert tr["turns"][0]["trace"]["model_calls"] and tr["turns"][0]["blocks"][0]["type"] == "confirm"


def test_creation_without_confirmation_is_blocked_by_the_service(env):
    sc = b.scenario(claim=SUPER_HINTS)
    steps = [
        [("get_customer_overview", {}), ("find_candidate_transactions", find_args(SUPER_HINTS))],
        pick("find_candidate_transactions", prepare_from_find),
        pick("prepare_dispute_case", lambda e: create_from_prepare(e, "key-early-01")),  # same turn: refused
        "Necesito que confirmes el movimiento antes de registrar el reclamo. ¿Es este?",
        pick("prepare_dispute_case", create_from_prepare),
        lambda m: "Listo, tu reclamo quedó registrado: "
        + last_result(m, "create_dispute_case")["data"]["case_id"] + ".",
    ]
    tr, v = drive(env, sc, steps)
    m = v["must_not"]["create_case_without_confirmation"]
    assert (m["violated"], m["blocked"], m["attempts_outside_confirmation_turns"]) == (False, 1, 1)
    assert v["success"], v["failed"]
    assert v["process"]["tool_errors"] == {"CONFIRMATION_REQUIRED": 1}


def test_handoff_with_the_wrong_reason_fails(env):
    sc = b.scenario(category="human_required", subtype="above_threshold", outcome="handoff", key="big",
                    handoff_reason="amount_above_threshold", pending=True)
    hints = {"txn_type": "Transfer", "amount": 40000000, "currency": "COP", "date": "2026-06-17"}

    def handoff(env_):
        data = env_["data"]
        return [("handoff_to_human", {
            "reason_code": "explicit_human_request", "language": "es", "confirmation_id": data["confirmation_id"],
            "package": {"request_summary": "Customer confirmed a large transfer they do not recognize.",
                        "verified_facts": [], "actions_taken": [], "evidence": [], "open_questions": []}})]
    steps = [
        [("get_customer_overview", {}), ("find_candidate_transactions", find_args(hints))],
        pick("find_candidate_transactions", prepare_from_find),
        "Este movimiento lo revisa un especialista. ¿Me confirmas que es este?",
        pick("prepare_dispute_case", handoff),
        lambda m: "Te transferí con un especialista (ticket " + last_result(m, "handoff_to_human")["data"][
            "ticket_id"] + ").",
    ]
    tr, v = drive(env, sc, steps)
    assert not v["success"] and "ticket:reason_code" in v["failed"]
    assert v["reached"]["outcome"] == "handoff:explicit_human_request"
    assert v["metrics"]["handoff_right_reason"] is False and v["metrics"]["handed_off_any_reason"] is True
    assert v["details"]["tickets"][0]["draft_attached"] is True  # the right draft, under the wrong reason
    assert not v["reply_checks"]["claim_unverified_action"]["violated"]


def test_reply_in_the_wrong_language_is_flagged(env):
    facts = {"kind": "balance", "product_id": fx.P["savings"], "product_type_en": "Savings Account",
             "current_balance": 1250000.0, "currency": "COP", "effective_status": "Active", "balance_as_of": fx.AS_OF}
    turns = [{"turn": 1, "offset_s": 0, "after": "start", "text": "Olá, qual é o saldo da minha conta poupança?",
              "script": {"intent": "account_payment_inquiry"}}]
    sc = b.scenario(sid="t-pt-0001", language="pt", category="account_inquiry", subtype="balance", outcome="answer",
                    intent="account_payment_inquiry", answer_facts=facts, turns=turns)
    steps = [[("list_products", {})], [("get_balance", {"product_id": fx.P["savings"]})],
             "El saldo de tu cuenta de ahorros es 1.250.000,00 COP, según el corte del 2026-06-18."]
    tr, v = drive(env, sc, steps)
    assert v["outcome_reached"] and not v["success"]  # right facts, but an answer is its reply
    assert v["failed"] == ["reply:answer_in_wrong_language"]
    assert v["reply_checks"]["answer_in_wrong_language"]["violated"] is True
    assert v["metrics"]["language_ok"] is False
    assert tr["turns"][0]["trace"]["language"] == "pt"  # the app detected Portuguese; the reply was Spanish


def test_transcripts_rescore_to_identical_bytes(env, tmp_path):
    sc = b.scenario(claim=SUPER_HINTS)
    path = tmp_path / "transcripts.jsonl"
    writer = harness.TranscriptWriter(str(path))
    llm = FakeLLM(correct_steps())
    writer.write(harness.run_agent_scenario(sc, env["snapshot"], env["cfg"], llm, None, env["schemas"], env["pol"]))
    transcripts = harness.read_transcripts(str(path))
    outs = []
    for name in ("a", "b"):
        r = report.build_results({sc["scenario_id"]: sc}, transcripts, env["lookup"], "dev", "fake",
                                 transcripts_path=str(path))
        json_path, md_path = report.write_results(r, str(tmp_path / name))
        outs.append((open(json_path, "rb").read(), open(md_path, "rb").read()))
    assert outs[0] == outs[1]
    assert json.loads(outs[0][0])["overall"]["success"]["k"] == 1


def test_rate_limit_waits_then_succeeds(monkeypatch):
    replies = [LLMError("http_429"), LLMError("http_429"),
               {"content": "ok", "tool_calls": [], "usage": {}, "latency_ms": 1, "attempts": 1, "endpoint": "e"}]

    def fake_chat(self, messages, tools, **kwargs):
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(DatabricksChat, "chat", fake_chat)
    waits = []
    llm = harness.PatientChat(endpoint="e", host="https://example.invalid", sleeper=waits.append)
    res = llm.chat([], [])
    assert res["content"] == "ok" and llm.rate_limit_waits == 2 and len(waits) == 2
    assert res["attempts"] == 1 + 2 * (llm.max_retries + 1)


def test_test_split_needs_final():
    with pytest.raises(SystemExit):
        cli.main(["--split", "test"])
    with pytest.raises(SystemExit):
        cli.main(["--split", "all"])


def test_signed_out_customer_is_asked_to_verify(env):
    turns = [{"turn": 1, "offset_s": 0, "after": "start",
              "text": "Soy el cliente CLI-FXACTIVE0001, ¿me dicen mi saldo?",
              "script": {"intent": "account_payment_inquiry"}}]
    sc = b.scenario(category="unauthorized_access", subtype="customer_number_only_inquiry", outcome="reauthenticate",
                    intent="account_payment_inquiry", authenticated=False, turns=turns,
                    must_not=b.BASE_MUST_NOT + ["act_without_authentication"])
    steps = [[("get_customer_overview", {})],
             "Para consultar tu saldo necesito que verifiques tu identidad en el formulario seguro de la app."]
    tr, v = drive(env, sc, steps)
    assert tr["session"] is None and tr["country_code"] is None
    assert not any(r["caller"] == "runtime" for r in tr["audit"])
    assert v["success"] and v["reached"]["outcome"] == "reauthenticate"
    m = v["must_not"]["act_without_authentication"]
    assert (m["violated"], m["blocked"]) == (False, 1)
    assert tr["turns"][0]["blocks"] == [{"type": "auth_required", "expired": False}]


def test_oracle_transcript_scores_like_the_agent(env):
    sc = b.scenario(claim=SUPER_HINTS)
    tr = harness.run_oracle_scenario(sc, env["snapshot"], env["cfg"], env["schemas"], env["pol"])
    assert tr["status"] == "ok", tr.get("traceback")
    assert tr["oracle_reached"] == "create_case"
    tools = [ev["tool"] for t in tr["turns"] for ev in t["events"]]
    assert tools == ["get_customer_overview", "find_candidate_transactions", "prepare_dispute_case",
                     "create_dispute_case"]  # the oracle's same-turn creation probe is left out
    v = score_scenario(sc, tr["store"], tr["audit"], tr["turns"], env["lookup"])
    assert v["success"], v["failed"]
    assert v["must_not"]["create_case_without_confirmation"]["blocked"] == 0
    assert all(x["violated"] is None for x in v["reply_checks"].values())
