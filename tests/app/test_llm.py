"""app/llm.py without a network: token expiry from the CLI, the 20 s CLI timeout, one token refresh after an HTTP
401 or 403, and every client failure (token, unreadable answer) raised as LLMError."""
import json
import subprocess
import time

import pytest

from app import llm as llm_mod
from app.llm import DatabricksChat, LLMError, TokenProvider, token_expiry

OK_BODY = {"choices": [{"message": {"content": "hola"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}


class FakeResponse:
    def __init__(self, status, body=None, raw=None):
        self.status_code, self._body, self._raw = status, body, raw
        self.text = raw if raw is not None else json.dumps(body or {})

    def json(self):
        if self._raw is not None:
            return json.loads(self._raw)
        return self._body


@pytest.fixture
def no_env_token(monkeypatch):
    for key in ("DATABRICKS_TOKEN", "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)


def cli(monkeypatch, bodies):
    """Fake `databricks auth token`: returns the bodies in order (the last one repeats); records each call."""
    calls = []

    class Done:
        def __init__(self, body):
            self.stdout = json.dumps(body)

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return Done(bodies[min(len(calls) - 1, len(bodies) - 1)])
    monkeypatch.setattr(llm_mod.subprocess, "run", run)
    return calls


def iso_in(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + seconds))


def test_token_expiry_reads_the_cli_fields():
    now = time.time()
    assert abs(token_expiry({"expiry": iso_in(300)}, now) - (now + 300)) < 2
    assert abs(token_expiry({"expiry": "2026-10-03T13:45:00.1234567-05:00"}, now)
               - token_expiry({"expiry": "2026-10-03T18:45:00Z"}, now)) < 1
    assert token_expiry({"expires_in": 120}, now) == now + 120
    assert token_expiry({"expiry": "not a date"}, now) == now + 600
    assert token_expiry({}, now) == now + 600


def test_cli_token_is_kept_until_its_own_expiry(monkeypatch, no_env_token):
    calls = cli(monkeypatch, [{"access_token": "tok-a", "expiry": iso_in(300)}])
    tokens = TokenProvider("https://example.invalid", "test")
    assert tokens.get() == "tok-a" and tokens.get() == "tok-a" and len(calls) == 1
    assert calls[0][1]["timeout"] == 20 and calls[0][1]["check"] is True
    assert abs(tokens._refresh_at - (tokens._expires - 120)) < 1  # asked again two minutes before expiry
    calls = cli(monkeypatch, [{"access_token": "tok-b", "expiry": iso_in(200)}])
    tokens = TokenProvider("https://example.invalid", "test")
    tokens.get()
    assert abs(tokens._refresh_at - (tokens._expires - 10)) < 1  # little time left: kept until ten seconds before
    calls = cli(monkeypatch, [{"access_token": "tok-c"}])
    tokens = TokenProvider("https://example.invalid", "test")
    tokens.get()
    assert abs(tokens._expires - (time.time() + 600)) < 2  # no expiry given: ten minutes


def http(monkeypatch, outcomes):
    seen = []

    def post(url, json=None, timeout=None, headers=None):
        out = outcomes[min(len(seen), len(outcomes) - 1)]
        seen.append(headers["Authorization"])
        if isinstance(out, Exception):
            raise out
        return out
    monkeypatch.setattr(llm_mod.requests, "post", post)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    return seen


@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_error_refreshes_the_token_once(monkeypatch, no_env_token, status):
    cli(monkeypatch, [{"access_token": "tok-old", "expiry": iso_in(3600)},
                      {"access_token": "tok-new", "expiry": iso_in(3600)}])
    seen = http(monkeypatch, [FakeResponse(status), FakeResponse(200, OK_BODY)])
    chat = DatabricksChat(endpoint="e", host="https://example.invalid", max_retries=0)
    res = chat.chat([{"role": "user", "content": "hola"}], [])
    assert res["content"] == "hola" and res["attempts"] == 2
    assert seen == ["Bearer tok-old", "Bearer tok-new"]


def test_a_second_auth_error_is_raised(monkeypatch, no_env_token):
    cli(monkeypatch, [{"access_token": "tok", "expiry": iso_in(3600)}])
    seen = http(monkeypatch, [FakeResponse(401, {"error_code": "UNAUTHENTICATED"})])
    chat = DatabricksChat(endpoint="e", host="https://example.invalid", max_retries=2)
    with pytest.raises(LLMError) as exc:
        chat.chat([], [])
    assert str(exc.value).startswith("http_401") and len(seen) == 2


def test_a_fixed_env_token_is_not_refreshed(monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-token-not-real")
    seen = http(monkeypatch, [FakeResponse(401)])
    chat = DatabricksChat(endpoint="e", host="https://example.invalid", max_retries=0)
    with pytest.raises(LLMError):
        chat.chat([], [])
    assert len(seen) == 1


@pytest.mark.parametrize("error", [FileNotFoundError("databricks"),
                                   subprocess.CalledProcessError(1, ["databricks"]),
                                   subprocess.TimeoutExpired(["databricks"], 20)])
def test_a_token_failure_is_an_llm_error(monkeypatch, no_env_token, error):
    def run(cmd, **kwargs):
        raise error
    monkeypatch.setattr(llm_mod.subprocess, "run", run)
    chat = DatabricksChat(endpoint="e", host="https://example.invalid")
    with pytest.raises(LLMError) as exc:
        chat.chat([], [])
    assert str(exc.value) == "auth_failed:" + type(error).__name__


@pytest.mark.parametrize("response", [FakeResponse(200, raw="<html>gateway</html>"), FakeResponse(200, {"x": 1}),
                                      FakeResponse(200, {"choices": []})])
def test_an_unreadable_answer_is_retried_then_raised(monkeypatch, response):
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-token-not-real")
    seen = http(monkeypatch, [response, response, FakeResponse(200, OK_BODY)])
    chat = DatabricksChat(endpoint="e", host="https://example.invalid", max_retries=1)
    with pytest.raises(LLMError) as exc:
        chat.chat([], [])
    assert str(exc.value) == "bad_response" and len(seen) == 2
    seen = http(monkeypatch, [response, FakeResponse(200, OK_BODY)])
    assert DatabricksChat(endpoint="e", host="https://example.invalid", max_retries=1).chat([], [])["attempts"] == 2
