"""Chat-completions client for Databricks model serving (OpenAI-compatible, with tool calling).

Auth, in order: DATABRICKS_TOKEN; Databricks Apps service-principal credentials (DATABRICKS_CLIENT_ID/SECRET, OAuth
client credentials); or the local CLI profile (`databricks auth token`, with a 20 s timeout). A fetched token is kept
until shortly before the expiry it comes with (the CLI may hand out a cached token with minutes left), and an HTTP
401 or 403 fetches a new one and retries once. Calls have a timeout and bounded retries on 429/5xx, network errors
and unreadable answers. Every failure, including a token that cannot be fetched, is raised as LLMError, so the agent
can fall back. Reasoning content returned by some models is dropped: it is never shown to the customer nor kept as an
audit artifact.
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime

import requests

DEFAULT_ENDPOINT = "databricks-gpt-oss-120b"  # best tool-use adherence in our smoke tests; override with APP_LLM_ENDPOINT
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
AUTH_STATUS = {401, 403}
CLI_TIMEOUT_S = 20
DEFAULT_TOKEN_TTL_S = 600  # when the token comes without an expiry


class LLMError(Exception):
    pass


def token_expiry(body, now):
    """Epoch seconds when a token expires: the CLI's 'expiry' (ISO 8601), else 'expires_in', else ten minutes."""
    if body.get("expiry"):
        try:
            return datetime.fromisoformat(str(body["expiry"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    try:
        return now + float(body["expires_in"])
    except (KeyError, TypeError, ValueError):
        return now + DEFAULT_TOKEN_TTL_S


class TokenProvider:
    def __init__(self, host, profile):
        self.host, self.profile = host, profile
        self._token, self._expires, self._refresh_at, self._lock = None, 0.0, 0.0, threading.Lock()

    def refreshable(self):
        """False for a fixed DATABRICKS_TOKEN, which asking again cannot change."""
        return not os.environ.get("DATABRICKS_TOKEN")

    def invalidate(self):
        with self._lock:
            self._token, self._expires, self._refresh_at = None, 0.0, 0.0

    def get(self):
        if os.environ.get("DATABRICKS_TOKEN"):
            return os.environ["DATABRICKS_TOKEN"]
        with self._lock:
            now = time.time()
            if self._token and now < self._refresh_at:
                return self._token
            if os.environ.get("DATABRICKS_CLIENT_ID") and os.environ.get("DATABRICKS_CLIENT_SECRET"):
                res = requests.post(self.host + "/oidc/v1/token", timeout=20,
                                    auth=(os.environ["DATABRICKS_CLIENT_ID"], os.environ["DATABRICKS_CLIENT_SECRET"]),
                                    data={"grant_type": "client_credentials", "scope": "all-apis"})
                res.raise_for_status()
                body = res.json()
            else:
                out = subprocess.run(["databricks", "auth", "token", "--profile", self.profile],
                                     capture_output=True, text=True, check=True, timeout=CLI_TIMEOUT_S).stdout
                body = json.loads(out)
            self._token, self._expires = body["access_token"], token_expiry(body, now)
            # Ask again two minutes before expiry; a token handed out with four minutes or less left is kept until ten
            # seconds before it expires, since the CLI only renews it when it is about to expire.
            self._refresh_at = self._expires - (120 if self._expires - now > 240 else 10)
            return self._token


class DatabricksChat:
    def __init__(self, endpoint=None, host=None, profile=None, timeout_s=45, max_retries=2):
        self.endpoint = endpoint or os.environ.get("APP_LLM_ENDPOINT", DEFAULT_ENDPOINT)
        self.host = (host or os.environ.get("DATABRICKS_HOST") or "").rstrip("/")
        if self.host and not self.host.startswith("http"):
            self.host = "https://" + self.host
        self.tokens = TokenProvider(self.host, profile or os.environ.get("DATABRICKS_CONFIG_PROFILE", "factored"))
        self.timeout_s, self.max_retries = timeout_s, max_retries

    def _token(self):
        try:
            return self.tokens.get()
        except Exception as exc:  # noqa: BLE001 - CLI missing or failing, OAuth error, unreadable token answer
            raise LLMError("auth_failed:" + type(exc).__name__) from exc

    def chat(self, messages, tools, temperature=0.1, max_tokens=900):
        url = self.host + "/serving-endpoints/" + self.endpoint + "/invocations"
        body = {"messages": messages, "tools": tools, "temperature": temperature, "max_tokens": max_tokens}
        last, attempts, retries, refreshed = None, 0, 0, False
        while True:
            attempts += 1
            started = time.monotonic()
            headers = {"Authorization": "Bearer " + self._token()}
            try:
                res = requests.post(url, json=body, timeout=self.timeout_s, headers=headers)
            except requests.RequestException as exc:
                last = LLMError(type(exc).__name__)
            else:
                if res.status_code in AUTH_STATUS and not refreshed and self._refreshable():
                    refreshed = True  # an expired or revoked token: fetch a new one and try once more
                    self.tokens.invalidate()
                    continue
                if res.status_code in RETRYABLE_STATUS:
                    last = LLMError("http_" + str(res.status_code))
                elif res.status_code >= 400:
                    raise LLMError("http_" + str(res.status_code) + ": " + res.text[:300])
                else:
                    out = _completion(res)
                    if out is not None:
                        return dict(out, latency_ms=int((time.monotonic() - started) * 1000), attempts=attempts,
                                    endpoint=self.endpoint)
                    last = LLMError("bad_response")
            if retries >= self.max_retries:
                raise last
            time.sleep(0.8 * (2 ** retries))
            retries += 1

    def _refreshable(self):
        check = getattr(self.tokens, "refreshable", None)
        return bool(check()) if callable(check) and hasattr(self.tokens, "invalidate") else False


def _completion(res):
    """{content, tool_calls, usage} of a chat-completions answer, or None when the body is not one."""
    try:
        data = res.json()
        msg = data["choices"][0]["message"]
        return {"content": _text(msg.get("content")), "tool_calls": msg.get("tool_calls") or [],
                "usage": data.get("usage") or {}}
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None


def _text(content):
    """Plain text of a message; list content keeps only text parts (reasoning parts are dropped)."""
    if content is None or isinstance(content, str):
        return content or ""
    parts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(part.get("text", ""))
    return "".join(parts)
