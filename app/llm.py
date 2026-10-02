"""Chat-completions client for Databricks model serving (OpenAI-compatible, with tool calling).

Auth, in order: DATABRICKS_TOKEN; Databricks Apps service-principal credentials (DATABRICKS_CLIENT_ID/SECRET, OAuth
client credentials); or the local CLI profile (`databricks auth token`). Calls have a timeout and bounded retries
on 429/5xx and network errors. Reasoning content returned by some models is dropped: it is never shown to the
customer nor kept as an audit artifact.
"""
import json
import os
import subprocess
import threading
import time

import requests

DEFAULT_ENDPOINT = "databricks-gpt-oss-120b"  # best tool-use adherence in our smoke tests; override with APP_LLM_ENDPOINT
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    pass


class TokenProvider:
    def __init__(self, host, profile):
        self.host, self.profile = host, profile
        self._token, self._expires, self._lock = None, 0.0, threading.Lock()

    def get(self):
        if os.environ.get("DATABRICKS_TOKEN"):
            return os.environ["DATABRICKS_TOKEN"]
        with self._lock:
            if self._token and time.time() < self._expires - 120:
                return self._token
            if os.environ.get("DATABRICKS_CLIENT_ID") and os.environ.get("DATABRICKS_CLIENT_SECRET"):
                res = requests.post(self.host + "/oidc/v1/token", timeout=20,
                                    auth=(os.environ["DATABRICKS_CLIENT_ID"], os.environ["DATABRICKS_CLIENT_SECRET"]),
                                    data={"grant_type": "client_credentials", "scope": "all-apis"})
                res.raise_for_status()
                body = res.json()
                self._token, self._expires = body["access_token"], time.time() + int(body.get("expires_in", 3600))
            else:
                out = subprocess.run(["databricks", "auth", "token", "--profile", self.profile],
                                     capture_output=True, text=True, check=True).stdout
                body = json.loads(out)
                self._token, self._expires = body["access_token"], time.time() + 1800
            return self._token


class DatabricksChat:
    def __init__(self, endpoint=None, host=None, profile=None, timeout_s=45, max_retries=2):
        self.endpoint = endpoint or os.environ.get("APP_LLM_ENDPOINT", DEFAULT_ENDPOINT)
        self.host = (host or os.environ.get("DATABRICKS_HOST") or "").rstrip("/")
        if self.host and not self.host.startswith("http"):
            self.host = "https://" + self.host
        self.tokens = TokenProvider(self.host, profile or os.environ.get("DATABRICKS_CONFIG_PROFILE", "factored"))
        self.timeout_s, self.max_retries = timeout_s, max_retries

    def chat(self, messages, tools, temperature=0.1, max_tokens=900):
        url = self.host + "/serving-endpoints/" + self.endpoint + "/invocations"
        body = {"messages": messages, "tools": tools, "temperature": temperature, "max_tokens": max_tokens}
        last = None
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            try:
                res = requests.post(url, json=body, timeout=self.timeout_s,
                                    headers={"Authorization": "Bearer " + self.tokens.get()})
                if res.status_code in RETRYABLE_STATUS:
                    last = LLMError("http_" + str(res.status_code))
                elif res.status_code >= 400:
                    raise LLMError("http_" + str(res.status_code) + ": " + res.text[:300])
                else:
                    data = res.json()
                    msg = data["choices"][0]["message"]
                    return {
                        "content": _text(msg.get("content")),
                        "tool_calls": msg.get("tool_calls") or [],
                        "usage": data.get("usage") or {},
                        "latency_ms": int((time.monotonic() - started) * 1000),
                        "attempts": attempt + 1,
                        "endpoint": self.endpoint,
                    }
            except (requests.Timeout, requests.ConnectionError) as exc:
                last = LLMError(type(exc).__name__)
            if attempt < self.max_retries:
                time.sleep(0.8 * (2 ** attempt))
        raise last or LLMError("unknown")


def _text(content):
    """Plain text of a message; list content keeps only text parts (reasoning parts are dropped)."""
    if content is None or isinstance(content, str):
        return content or ""
    parts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(part.get("text", ""))
    return "".join(parts)
