"""The harness and the command line without a network: endpoint failures (real requests exceptions and HTTP answers
through DatabricksChat), the token refresh, setup checks, transcripts, resume by fingerprint, --fresh, mixed
fingerprints, the stop after repeated endpoint failures, oracle results kept out of eval/results, and how unscored
scenarios show in the results."""
import json
import time

import pytest
import requests

from src.agent_eval import harness, report, run as cli
from tests.agent_eval import builders as b

SAY_HELLO = "Hola, ¿en qué te puedo ayudar?"


def two_turns(sid="t-es-0101"):
    return b.scenario(sid=sid, outcome="abstain", intent="out_of_scope",
                      turns=[{"turn": 1, "offset_s": 0, "after": "start", "text": "Hola", "script": {}},
                             {"turn": 2, "offset_s": 30, "after": "clarifying_question", "text": "Gracias",
                              "script": {}}])


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body or {}, json.dumps(body or {})

    def json(self):
        return self._body


OK_BODY = {"choices": [{"message": {"content": SAY_HELLO}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}


def real_client(monkeypatch, outcomes):
    """PatientChat whose HTTP calls return or raise the given outcomes in order (the last one repeats)."""
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-token-not-real")
    seen = []

    def post(url, json=None, timeout=None, headers=None):
        out = outcomes[min(len(seen), len(outcomes) - 1)]
        seen.append(headers.get("Authorization"))
        if isinstance(out, Exception):
            raise out
        return out
    monkeypatch.setattr("app.llm.requests.post", post)
    llm = harness.PatientChat(endpoint="test-endpoint", host="https://example.invalid", max_retries=0, waits=(),
                              sleeper=lambda s: None)
    return llm, seen


@pytest.mark.parametrize("outcome,status", [
    (requests.exceptions.ReadTimeout("read timed out"), "infra_error"),
    (requests.exceptions.ConnectTimeout("connect timed out"), "infra_error"),
    (requests.exceptions.ConnectionError("refused"), "infra_error"),
    (requests.exceptions.SSLError("bad handshake"), "infra_error"),
    (FakeResponse(401, {"error_code": "UNAUTHENTICATED"}), "infra_error"),
    (FakeResponse(403, {"error_code": "PERMISSION_DENIED"}), "infra_error"),
    (FakeResponse(404, {"error_code": "NOT_FOUND"}), "infra_error"),
    (FakeResponse(503), "infra_error"),
    (FakeResponse(429), "rate_limited"),
    (FakeResponse(400, {"error_code": "BAD_REQUEST"}), "ok"),
])
def test_endpoint_failures_are_not_scored_and_stop_the_scenario(env, monkeypatch, outcome, status):
    llm, seen = real_client(monkeypatch, [outcome])
    tr = harness.run_agent_scenario(two_turns(), env["snapshot"], env["cfg"], llm, None, env["schemas"], env["pol"])
    assert tr["status"] == status, tr.get("error")
    if status == "ok":  # a 400 is the system's own behavior: both turns run and are scored
        assert len(tr["turns"]) == 2 and tr["turns"][0]["trace"]["fallback"].startswith("llm_error:http_400")
    else:
        assert len(tr["turns"]) == 1  # no more model calls after the endpoint failed


class CrashingChat:
    endpoint = "fake"

    def chat(self, messages, tools):
        raise KeyError("choices")


def test_a_client_exception_is_a_harness_error_with_its_detail(env):
    tr = harness.run_agent_scenario(two_turns(), env["snapshot"], env["cfg"], CrashingChat(), None, env["schemas"],
                                    env["pol"])
    assert tr["status"] == "harness_error" and len(tr["turns"]) == 1
    assert tr["error"].startswith("agent fallback llm_error:KeyError: ") and "choices" in tr["error"]
    assert "choices" in tr["turns"][0]["fallback_detail"]


@pytest.mark.parametrize("reason,status", [
    ("static_fallback", "ok"), ("max_model_calls:repeated_tool_call", "ok"),
    ("llm_error:auth_failed:OSError", "infra_error"), ("llm_error:bad_response", "infra_error"),
    ("llm_error:ChunkedEncodingError", "infra_error"), ("llm_error:TypeError", "harness_error"),
])
def test_fallback_reasons_map_to_a_status(reason, status):
    assert harness.fallback_status(reason) == status


def test_token_is_refreshed_once_after_a_403(env, monkeypatch):
    llm, seen = real_client(monkeypatch, [FakeResponse(403), FakeResponse(200, OK_BODY)])
    llm.tokens = harness.CliTokenProvider("https://example.invalid", "test")
    res = llm.chat([{"role": "user", "content": "hola"}], [])
    assert res["content"] == SAY_HELLO and llm.token_refreshes == 1 and len(seen) == 2
    llm, seen = real_client(monkeypatch, [FakeResponse(403)])
    llm.tokens = harness.CliTokenProvider("https://example.invalid", "test")
    with pytest.raises(harness.LLMError):
        llm.chat([{"role": "user", "content": "hola"}], [])
    assert llm.token_refreshes == 1 and len(seen) == 2  # one refresh, then the error goes up


def test_cli_token_provider_keeps_the_reported_expiry(monkeypatch):
    for key in ("DATABRICKS_TOKEN", "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    calls = []
    expiry = time.time() + 600

    class Done:
        stdout = json.dumps({"access_token": "tok-test",
                             "expiry": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(expiry))})

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Done()
    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    tokens = harness.CliTokenProvider("https://example.invalid", "test")
    assert tokens.get() == "tok-test" and tokens.get() == "tok-test" and len(calls) == 1
    assert abs(tokens._expires - expiry) < 2
    assert abs(tokens._refresh_at - (expiry - 120)) < 2  # asked again two minutes before expiry
    tokens.invalidate()
    tokens.get()
    assert len(calls) == 2
    expiry = time.time() + 200  # a cached token with little time left: kept until ten seconds before expiry
    Done.stdout = json.dumps({"access_token": "tok-short",
                              "expiry": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(expiry))})
    tokens.invalidate()
    assert tokens.get() == "tok-short" and tokens.get() == "tok-short" and len(calls) == 3
    assert abs(tokens._refresh_at - (expiry - 10)) < 2
    assert harness.parse_expiry("2026-10-03T13:45:00.1234567-05:00") is not None
    assert harness.parse_expiry("not a date") is None


def test_session_setup_mismatch_is_a_harness_error(env):
    sc = two_turns()
    sc["session"]["expires_at"] = "2030-01-01T00:00:00"
    tr = harness.run_agent_scenario(sc, env["snapshot"], env["cfg"], b.FakeLLM([]), None, env["schemas"], env["pol"])
    assert tr["status"] == "harness_error" and "test session setup failed" in tr["error"]


def test_transcript_records_what_produced_it(env, tmp_path):
    fp = harness.fingerprint("fake", env["snapshot"], env["cfg"])
    assert fp["id"] and fp["system_prompt"] and fp["app_agent"] and fp["snapshot"]
    assert fp["service"] == {"env": "eval", "model_auth": False, "read_only": False, "confirmation_ttl_s": 600}
    tr = harness.run_agent_scenario(two_turns(), env["snapshot"], env["cfg"], b.FakeLLM([SAY_HELLO, SAY_HELLO]),
                                    None, env["schemas"], env["pol"], fingerprint=fp)
    assert tr["fingerprint"]["id"] == fp["id"] and harness.fingerprint_id(tr) == fp["id"]
    assert tr["system_prompt"].startswith("You are the customer-service assistant")
    assert tr["messages"][0]["role"] == "system" and "content_sha256" in tr["messages"][0]
    assert "customer-service assistant" not in json.dumps(tr["messages"][0])
    assert tr["messages"][0]["runtime_facts"].startswith("Runtime facts:")
    assert [m["role"] for m in tr["messages"][1:]] == ["user", "assistant", "user", "assistant"]
    path = tmp_path / "t.jsonl"
    harness.TranscriptWriter(str(path)).write(tr)
    assert b"\r\n" not in path.read_bytes()


def test_eval_config_ignores_the_shell(monkeypatch):
    monkeypatch.setenv("BANK_TOOLS_MODEL_AUTH", "true")
    monkeypatch.setenv("BANK_TOOLS_READ_ONLY", "true")
    monkeypatch.setenv("BANK_TOOLS_CONFIRMATION_TTL_S", "5")
    cfg = harness.eval_config()
    assert (cfg.model_auth, cfg.read_only, cfg.confirmation_ttl_s) == (False, False, 600)


# -- command line ----------------------------------------------------------------------------------------------------
class FakePatient:
    """Stands in for harness.PatientChat in the CLI; behavior by the scenario's first customer text."""
    modes, calls = {}, {}

    def __init__(self, endpoint=None, host=None, profile=None):
        self.endpoint, self.rate_limit_waits, self.token_refreshes, self.tokens = endpoint, 0, 0, None

    def chat(self, messages, tools):
        first = next(m["content"] for m in messages if m["role"] == "user")
        FakePatient.calls[first] = FakePatient.calls.get(first, 0) + 1
        if FakePatient.modes.get(first) == "timeout":
            raise harness.LLMError("ReadTimeout")
        return {"content": SAY_HELLO, "tool_calls": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "latency_ms": 1, "attempts": 1, "endpoint": self.endpoint}


@pytest.fixture
def cli_env(env, tmp_path, monkeypatch):
    scenarios = []
    for i in range(1, 5):
        sc = b.scenario(sid=f"t-es-{i:04d}", outcome="abstain", intent="out_of_scope",
                        turns=[{"turn": 1, "offset_s": 0, "after": "start", "text": f"Mensaje {i}", "script": {}}])
        scenarios.append(sc)
    path = tmp_path / "scenarios.jsonl"
    path.write_text("".join(json.dumps(sc, ensure_ascii=False) + "\n" for sc in scenarios), encoding="utf-8")
    monkeypatch.setattr(cli, "TRANSCRIPTS_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(cli, "RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(harness, "PatientChat", FakePatient)
    monkeypatch.setattr(cli, "load_classifier", lambda: None)
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.invalid")
    FakePatient.modes, FakePatient.calls = {}, {}
    base = ["--scenarios", str(path), "--snapshot", env["snapshot"], "--endpoint", "fake-endpoint", "--workers", "1"]
    return {"argv": base, "tmp": tmp_path, "transcripts": tmp_path / "data" / "dev_fake-endpoint" / "transcripts.jsonl",
            "results": tmp_path / "results" / "agent_e2e_dev_fake-endpoint.json"}


def statuses(path):
    return {k: v["status"] for k, v in harness.read_transcripts(str(path)).items()}


def test_resume_reruns_failed_and_stale_scenarios_only(cli_env, monkeypatch):
    FakePatient.modes = {"Mensaje 2": "timeout"}
    assert cli.main(cli_env["argv"]) == 1
    assert statuses(cli_env["transcripts"])["t-es-0002"] == "infra_error"
    res = json.loads(cli_env["results"].read_text(encoding="utf-8"))
    assert res["counts"]["skipped"] == {"infra_error": ["t-es-0002"]} and res["counts"]["complete"] is False
    assert all(t["incomplete"] for t in res["targets"]) and res["conservative_success"]["n"] == 4

    FakePatient.modes, FakePatient.calls = {}, {}
    assert cli.main(cli_env["argv"]) == 0
    assert FakePatient.calls == {"Mensaje 2": 1}  # only the failed one ran again
    assert set(statuses(cli_env["transcripts"]).values()) == {"ok"}

    real = harness.fingerprint
    monkeypatch.setattr(harness, "fingerprint", lambda *a, **k: dict(real(*a, **k), id="changed-prompt"))
    FakePatient.calls = {}
    assert cli.main(cli_env["argv"]) == 0
    assert len(FakePatient.calls) == 4  # a new fingerprint re-runs everything


def test_mixed_fingerprints_are_refused_unless_allowed(cli_env, monkeypatch):
    assert cli.main(cli_env["argv"] + ["--only", "t-es-0001,t-es-0002"]) == 0
    real = harness.fingerprint
    monkeypatch.setattr(harness, "fingerprint", lambda *a, **k: dict(real(*a, **k), id="other"))
    assert cli.main(cli_env["argv"] + ["--only", "t-es-0003"]) == 0
    assert cli.main(cli_env["argv"] + ["--rescore"]) == 2
    assert cli.main(cli_env["argv"] + ["--rescore", "--allow-mixed"]) == 0
    res = json.loads(cli_env["results"].read_text(encoding="utf-8"))
    assert sorted(f["n"] for f in res["inputs"]["fingerprints"]) == [1, 2]
    assert res["counts"]["missing"] == ["t-es-0004"]


def test_fresh_sets_old_transcripts_aside(cli_env):
    assert cli.main(cli_env["argv"] + ["--only", "t-es-0001"]) == 0
    FakePatient.calls = {}
    assert cli.main(cli_env["argv"] + ["--only", "t-es-0001", "--fresh"]) == 0
    assert FakePatient.calls == {"Mensaje 1": 1}
    kept = [p.name for p in cli_env["transcripts"].parent.iterdir() if p.name != "transcripts.jsonl"]
    assert len(kept) == 1 and kept[0].startswith("transcripts.") and kept[0].endswith(".jsonl")


def test_run_stops_after_repeated_endpoint_failures(cli_env):
    FakePatient.modes = {f"Mensaje {i}": "timeout" for i in range(1, 5)}
    assert cli.main(cli_env["argv"] + ["--max-consecutive-failures", "2"]) == 1
    ran = len(harness.read_transcripts(str(cli_env["transcripts"])))
    assert 2 <= ran < 4  # the scenario already running when the run stops still finishes; the rest never start


def test_oracle_results_stay_in_the_ignored_data_folder(cli_env):
    assert cli.main(["--oracle", "--split", "all"] + cli_env["argv"][:4]) == 0
    folder = cli_env["tmp"] / "data" / "all_oracle"
    assert (folder / "agent_e2e_all_oracle.json").exists() and (folder / "agent_e2e_all_oracle.md").exists()
    assert not (cli_env["tmp"] / "results").exists()


def test_final_run_must_be_complete(cli_env, monkeypatch):
    FakePatient.modes = {"Mensaje 1": "timeout"}
    path = cli_env["argv"][1]
    with open(path, encoding="utf-8") as fh:
        rows = [dict(json.loads(line), split="test") for line in fh]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))
    assert cli.main(cli_env["argv"] + ["--split", "test", "--final", "--max-consecutive-failures", "0"]) == 1


def test_resolve_host_reads_the_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    cfg = tmp_path / "databrickscfg"
    cfg.write_text("[factored]\nhost = https://adb-0000.azuredatabricks.net\n", encoding="utf-8")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
    assert cli.resolve_host("factored") == "https://adb-0000.azuredatabricks.net"
    with pytest.raises(SystemExit):
        cli.resolve_host("missing")


# -- results -------------------------------------------------------------------------------------------------------
def test_unscored_scenarios_in_the_results(env):
    scs = {sid: b.scenario(sid=sid, outcome="abstain", intent="out_of_scope") for sid in ("a", "b", "c", "d")}
    for sid, sc in scs.items():
        sc["turns"] = [{"turn": 1, "offset_s": 0, "after": "start", "text": "Hola", "script": {}}]
    ok = harness.run_agent_scenario(scs["a"], env["snapshot"], env["cfg"], b.FakeLLM([SAY_HELLO]), None,
                                    env["schemas"], env["pol"])
    transcripts = {"a": ok, "b": {"scenario_id": "b", "status": "harness_error", "error": "KeyError: 'x'"},
                   "c": {"scenario_id": "c", "status": "infra_error"}}
    r = report.build_results(scs, transcripts, env["lookup"], "dev", "fake")
    assert r["counts"]["scored"] == 2 and r["counts"]["harness_errors"] == 1
    assert r["counts"]["skipped"] == {"infra_error": ["c"]} and r["counts"]["missing"] == ["d"]
    assert r["overall"]["success"]["k"] == 1 and r["overall"]["success"]["n"] == 2  # the harness error is a failure
    assert r["conservative_success"] == {"k": 1, "n": 4, "rate": 0.25,
                                         "note": "scenarios not scored count as failures"}
    assert r["counts"]["complete"] is False and "**Incomplete:**" in report.to_markdown(r)


@pytest.mark.parametrize("op,target,value,ci,want", [
    (">=", 0.8, 0.85, [0.82, 0.9], "met"),
    (">=", 0.8, 0.85, [0.7, 0.95], "inconclusive"),
    (">=", 0.8, 0.6, [0.5, 0.75], "not met"),
    (">=", 1.0, 0.98, [0.95, 1.0], "not met"),
    (">=", 1.0, 1.0, [1.0, 1.0], "met"),
    ("==", 0, 0, None, "met"),
    ("==", 0, 2, None, "not met"),
    ("<=", 0.05, 0.03, [-0.152, 0.212], "inconclusive"),  # the review's example: 28/33 vs 27/33
    ("<=", 0.05, 0.0, [-0.04, 0.04], "met"),
    ("<=", 0.05, -0.2, [-0.3, -0.1], "not met"),
    (">=", 0.8, None, None, "n/a"),
])
def test_target_verdicts(op, target, value, ci, want):
    assert report.target_verdict(op, target, value, ci) == want
