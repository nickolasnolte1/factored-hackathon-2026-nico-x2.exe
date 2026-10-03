"""Runs one e2e scenario against the real agent (or the scripted oracle) and returns its transcript.

The environment of every scenario follows CONTRACT.md section 10 and src/bank_tools/replay.py: LocalRepository over
the panel snapshot with a fresh in-memory store, FixedClock at the scenario's `now`, FaultInjector(tool_faults),
ListAuditSink, env `eval` (seeded ids, no-op sleeper). An authenticated scenario gets the trusted test session
(identity.issue_test_session, bound to the conversation); the runtime then reads the customer's country with a
runtime-caller get_customer_overview, as app/agent.py signed_in does, without sending any app event to the model.
If that read fails or the session does not match the scenario's expiry, the scenario is a harness_error. A
signed-out scenario starts with no token. The clock moves to now + offset_s before each customer turn.

Agent mode drives app.agent.Agent (its prompt, tools and loop, unchanged) with every customer turn in order, whatever
the agent did; turns[].script and `expected` never reach it. The conversation starts in Spanish, as the app's UI does.
When a turn falls back because the endpoint failed (an HTTP error other than 400, 413 or 422, a network error, a
token failure or an unreadable answer), the scenario stops there with status infra_error (rate_limited for HTTP 429):
it is re-run on resume and never scored. Any other client exception is a bug, not an outage: the scenario stops with
status harness_error and the fallback detail as its error, and it counts as a failure. Oracle
mode runs the scripted oracle of replay.py instead (no model, no probes) so its store and audit can be scored by the
same tool-level scorer.

A transcript (one JSON line) keeps, per turn, the customer text, the reply, the UI blocks, the app's trace entry
(public_turn) and the full tool events; per scenario, the store rows, the audit records, the messages the model saw
(the system message replaced by its hash and its runtime facts), the system prompt, and the fingerprint of what
produced it (fingerprint() below), so a resumed run never mixes prompt versions.
"""
import copy
import hashlib
import json
import os
import random
import subprocess
import threading
import time
import traceback
from datetime import datetime, timedelta

import requests

from app.agent import SYSTEM_PROMPT, Agent
from app.llm import DatabricksChat, LLMError, TokenProvider
from src.bank_tools import BankService, Config, FaultInjector, FixedClock, ListAuditSink, LocalRepository
from src.bank_tools.clock import iso, parse_dt
from src.bank_tools.config import REPO_ROOT
from src.bank_tools.replay import SESSION, Replay

HARNESS_VERSION = "1.2.0"
START_LANGUAGE = "es"  # the app's UI opens every conversation in Spanish
RATE_LIMIT_WAITS_S = (10, 20, 40, 60, 90)  # extra waits after DatabricksChat's own retries give up on HTTP 429
EVAL_CONFIG = {"model_auth": False, "read_only": False, "confirmation_ttl_s": 600}  # pinned, whatever the shell says
# Fallbacks that are the system's own behavior (scored). Endpoint failures are re-run and never scored; any other
# client exception (llm_error:<ExceptionName>) is a harness_error.
SYSTEM_FALLBACKS = ("max_model_calls", "empty_reply", "static_fallback", "llm_error:http_400", "llm_error:http_413",
                    "llm_error:http_422")
ENDPOINT_FALLBACKS = ("llm_error:http_", "llm_error:auth_failed", "llm_error:bad_response")
NETWORK_ERRORS = frozenset(name for name, cls in vars(requests.exceptions).items()
                           if isinstance(cls, type) and issubclass(cls, requests.RequestException))
FINGERPRINT_FILES = {"app_agent": "app/agent.py", "app_llm": "app/llm.py",
                     "tool_schemas": "src/bank_tools/tool_schemas.json", "policy": "src/policy/dispute_policy.json"}


class HarnessSetupError(RuntimeError):
    pass


def eval_config():
    return Config.from_env(env="eval", repository="local", state_path="", otp_outbox_file="", **EVAL_CONFIG)


# -- fingerprint -----------------------------------------------------------------------------------------------------
def _sha(data):
    return hashlib.sha256(data).hexdigest()[:16]


def file_sha(path, text=True):
    """sha256 (16 hex) of a file; text files with LF line endings, so a checkout's line endings do not matter."""
    if not path or not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        data = fh.read()
    return _sha(data.replace(b"\r\n", b"\n") if text else data)


def git_state():
    """HEAD and whether app/, src/bank_tools or src/policy have uncommitted changes (recorded, not compared)."""
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True,
                              timeout=10).stdout.strip() or None
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "app", "src/bank_tools", "src/policy"],
                               cwd=REPO_ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
        return {"head": head, "app_or_service_dirty": bool(dirty)}
    except (OSError, subprocess.SubprocessError):
        return {"head": None, "app_or_service_dirty": None}


def fingerprint(endpoint, snapshot, cfg, classifier=None):
    """What produced a transcript: hashes of the agent, the LLM client, the tool schemas, the policy, the system prompt
    and the snapshot, the endpoint, the classifier version, the harness version and the pinned service settings.
    `id` covers all of them; `git` is recorded for reference only (a commit elsewhere does not change behavior)."""
    parts = {"harness_version": HARNESS_VERSION, "endpoint": endpoint,
             **{k: file_sha(os.path.join(REPO_ROOT, p)) for k, p in FINGERPRINT_FILES.items()},
             "system_prompt": _sha(SYSTEM_PROMPT.encode("utf-8")),
             "snapshot": file_sha(snapshot, text=False),
             "classifier": getattr(classifier, "model_version", None),
             "service": {"env": cfg.env, "model_auth": cfg.model_auth, "read_only": cfg.read_only,
                         "confirmation_ttl_s": cfg.confirmation_ttl_s}}
    parts["id"] = _sha(json.dumps(parts, sort_keys=True).encode("utf-8"))
    parts["git"] = git_state()
    return parts


# -- model client ----------------------------------------------------------------------------------------------------
def parse_expiry(value):
    """Epoch seconds of the CLI's token expiry (ISO 8601), or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class CliTokenProvider(TokenProvider):
    """TokenProvider that keeps a CLI token until the expiry the CLI reports (TokenProvider assumes 30 minutes, but the
    CLI may hand out a cached token with minutes left), and that a client can invalidate after an HTTP 401 or 403.
    The token and client-credential paths are unchanged."""

    _refresh_at = 0.0

    def get(self):
        if os.environ.get("DATABRICKS_TOKEN") or (os.environ.get("DATABRICKS_CLIENT_ID")
                                                  and os.environ.get("DATABRICKS_CLIENT_SECRET")):
            return super().get()
        with self._lock:
            now = time.time()
            if self._token and now < self._refresh_at:
                return self._token
            out = subprocess.run(["databricks", "auth", "token", "--profile", self.profile],
                                 capture_output=True, text=True, check=True).stdout
            body = json.loads(out)
            self._token = body["access_token"]
            self._expires = parse_expiry(body.get("expiry")) or now + 1800
            # Ask again two minutes before expiry; a token handed out with less than four minutes left is kept until
            # ten seconds before it expires, since the CLI only renews it then.
            left = self._expires - now
            self._refresh_at = self._expires - (120 if left > 240 else 10)
            return self._token

    def invalidate(self):
        with self._lock:
            self._token, self._expires, self._refresh_at = None, 0.0, 0.0


class PatientChat(DatabricksChat):
    """DatabricksChat with longer waits on HTTP 429 (rate limit), for parallel evaluation runs, and one token refresh
    after an HTTP 401 or 403.

    The request, the payload and the response parsing are DatabricksChat's own; only a call that still fails with
    http_429 after its bounded retries is repeated, after RATE_LIMIT_WAITS_S (with jitter)."""

    def __init__(self, *args, waits=RATE_LIMIT_WAITS_S, sleeper=time.sleep, **kwargs):
        super().__init__(*args, **kwargs)
        self.waits, self.sleeper = tuple(waits), sleeper
        self.rate_limit_waits = 0
        self.token_refreshes = 0

    def chat(self, messages, tools, **kwargs):
        rounds, refreshed = 0, False
        waits = list(self.waits)
        while True:
            try:
                res = super().chat(messages, tools, **kwargs)
                res["attempts"] = res.get("attempts", 1) + rounds * (self.max_retries + 1) + int(refreshed)
                return res
            except LLMError as exc:
                code = str(exc)
                if code.startswith(("http_401", "http_403")) and not refreshed and hasattr(self.tokens, "invalidate"):
                    refreshed = True
                    self.token_refreshes += 1
                    self.tokens.invalidate()
                    continue
                if code != "http_429" or not waits:
                    raise
                rounds += 1
                self.rate_limit_waits += 1
                self.sleeper(waits.pop(0) * (1 + 0.25 * random.random()))


# -- scenarios -------------------------------------------------------------------------------------------------------
def _service(sc, snapshot, cfg, schemas, pol):
    repo = LocalRepository(snapshot)
    clock = FixedClock(parse_dt(sc["now"]))
    audit = ListAuditSink()
    faults = FaultInjector(sc.get("tool_faults") or [], env=cfg.env)
    return BankService(repo, clock, audit, faults, cfg, schemas=schemas, policy=pol), repo, clock, audit


def _store(repo):
    return {"dispute_cases": repo.store_rows("dispute_cases"), "handoff_tickets": repo.store_rows("handoff_tickets")}


def _base(sc, mode, endpoint, fp=None):
    return {"scenario_id": sc["scenario_id"], "split": sc.get("split"), "category": sc["category"],
            "subtype": sc["subtype"], "language": sc["language"], "mode": mode, "endpoint": endpoint,
            "harness_version": HARNESS_VERSION, "fingerprint": fp, "status": "ok", "error": None}


def fallback_status(reason):
    """ok for no fallback or a fallback of the system itself; rate_limited for HTTP 429; infra_error when the
    endpoint failed (HTTP, network, token, unreadable answer); harness_error for any other client exception."""
    reason = str(reason or "")
    if not reason or reason.startswith(SYSTEM_FALLBACKS):
        return "ok"
    if reason.startswith("llm_error:http_429"):
        return "rate_limited"
    if reason.startswith(ENDPOINT_FALLBACKS) or reason.split(":", 1)[-1] in NETWORK_ERRORS:
        return "infra_error"
    return "harness_error"


def _status(turns):
    """ok, or rate_limited / infra_error when a turn fell back because the endpoint was rate-limited, down,
    unreachable or refused the credentials (those scenarios are re-run on resume and never scored)."""
    found = {fallback_status((t.get("trace") or {}).get("fallback")) for t in turns}
    for status in ("rate_limited", "infra_error", "harness_error"):
        if status in found:
            return status
    return "ok"


def _messages(conv):
    """The conversation as the model saw it, with the system message replaced by its hash and runtime facts."""
    system = conv.messages[0]["content"]
    base = SYSTEM_PROMPT.replace("{nonce}", conv.nonce)
    facts = system[len(base):].strip() if system.startswith(base) else None
    head = {"role": "system", "content_sha256": _sha(system.encode("utf-8")), "runtime_facts": facts}
    return [head] + copy.deepcopy(conv.messages[1:])


def run_agent_scenario(sc, snapshot, cfg, llm, classifier=None, schemas=None, pol=None, endpoint=None,
                       fingerprint=None):
    """Drive the real agent through the scenario's customer turns; returns the transcript dict."""
    out = _base(sc, "agent", endpoint or getattr(llm, "endpoint", None), fingerprint)
    out["system_prompt"] = SYSTEM_PROMPT
    out["classifier_version"] = getattr(classifier, "model_version", None)
    started = time.monotonic()
    repo = None
    try:
        svc, repo, clock, audit = _service(sc, snapshot, cfg, schemas, pol)
        agent = Agent(svc, llm, classifier=classifier)
        conv = agent.new_conversation(START_LANGUAGE)
        now0 = parse_dt(sc["now"])
        out.update({"conversation_id": conv.id, "nonce": conv.nonce, "country_code": None, "session": None})
        if sc["session"]["authenticated"]:
            conv.session_token = svc.identity.issue_test_session(sc["customer_id"], authenticated_at=iso(now0),
                                                                 conversation_id=conv.id)
            overview = svc.call_tool("get_customer_overview", {}, conv.session_token, agent._ctx(conv, "runtime"))
            data = overview.data if overview.ok else {}
            conv.country_code = data.get("country_code")
            out["country_code"] = conv.country_code
            expires = (data.get("session") or {}).get("expires_at")
            out["session"] = {"expires_at": expires, "matches_scenario": expires == sc["session"]["expires_at"]}
            if not overview.ok or not out["session"]["matches_scenario"]:
                raise HarnessSetupError("test session setup failed: overview "
                                        + ("ok" if overview.ok else str(overview.code))
                                        + f", expires_at {expires} vs scenario {sc['session']['expires_at']}")
        turns = []
        for st in sc["turns"]:
            clock.set(now0 + timedelta(seconds=st.get("offset_s", 0)))
            res = agent.customer_turn(conv, st["text"])
            full = conv.trace[-1]
            turns.append({"turn": st["turn"], "after": st["after"], "offset_s": st.get("offset_s", 0),
                          "clock": iso(clock.now()), "text": st["text"], "reply": res["reply"],
                          "blocks": res["blocks"], "trace": res["turn"], "events": full["events"],
                          **({"fallback_detail": full["fallback_detail"]} if full.get("fallback_detail") else {})})
            status = fallback_status(res["turn"].get("fallback"))
            if status == "harness_error":
                out["error"] = ("agent fallback " + str(res["turn"].get("fallback")) + ": "
                                + str(full.get("fallback_detail") or "")[:300])
            if status != "ok":
                break  # the endpoint failed (re-run later) or the client crashed: later turns would only cost calls
        out["turns"] = turns
        out["messages"] = _messages(conv)
        out["store"] = _store(repo)
        out["audit"] = list(audit.records)
        out["status"] = _status(turns)
    except Exception as exc:  # noqa: BLE001 - one scenario's crash must not stop the run
        out["status"] = "harness_error"
        out["error"] = type(exc).__name__ + ": " + str(exc)[:300]
        out["traceback"] = traceback.format_exc()[-2000:]
    finally:
        if repo is not None:
            repo.close()
    out["rate_limit_waits"] = getattr(llm, "rate_limit_waits", 0)
    out["token_refreshes"] = getattr(llm, "token_refreshes", 0)
    out["wall_s"] = round(time.monotonic() - started, 2)
    return out


class _OracleReplay(Replay):
    """replay.py's oracle, recording each call's arguments (the oracle itself is unchanged)."""

    def call(self, tool, args=None, probe=False, token=SESSION, caller="model"):
        res = super().call(tool, args, probe, token, caller)
        self.calls[-1]["args"] = copy.deepcopy(args or {})
        return res


def run_oracle_scenario(sc, snapshot, cfg, schemas=None, pol=None, fingerprint=None):
    """Drive the scripted oracle (no model). Its own probe calls are left out of the scored audit."""
    out = _base(sc, "oracle", "oracle", fingerprint)
    started = time.monotonic()
    rp = None
    try:
        rp = _OracleReplay(sc, snapshot, cfg, schemas, pol, None, None)
        if sc["session"]["authenticated"]:
            rp.token = rp.issue(rp.now0)
        turns, stopped = [], False
        for i, st in enumerate(sc["turns"]):
            before = len(rp.calls)
            if not stopped:
                rp.turn = st["turn"]
                rp.clock.set(rp.now0 + timedelta(seconds=st.get("offset_s", 0)))
                stopped = rp.step(st.get("script") or {}, first=i == 0)
            events = [{"tool": c["tool"], "args": c.get("args"), "envelope": c["result"].for_model(),
                       "latency_ms": (c["audit"] or {}).get("latency_ms")}
                      for c in rp.calls[before:] if not c["probe"]]
            turns.append({"turn": st["turn"], "after": st["after"], "offset_s": st.get("offset_s", 0),
                          "clock": iso(rp.clock.now()), "text": st["text"], "reply": None, "blocks": None,
                          "trace": None, "events": events})
        probe_ids = {c["result"].meta.get("tool_call_id") for c in rp.calls if c["probe"]}
        out.update({"conversation_id": rp.conv, "nonce": None, "turns": turns, "store": _store(rp.repo),
                    "audit": [r for r in rp.audit.records if r.get("tool_call_id") not in probe_ids],
                    "oracle_reached": rp.reached()})
    except Exception as exc:  # noqa: BLE001
        out["status"] = "harness_error"
        out["error"] = type(exc).__name__ + ": " + str(exc)[:300]
        out["traceback"] = traceback.format_exc()[-2000:]
    finally:
        if rp is not None:
            rp.repo.close()
    out["wall_s"] = round(time.monotonic() - started, 2)
    return out


# -- transcripts ---------------------------------------------------------------------------------------------------
class TranscriptWriter:
    """Appends one JSON line per finished scenario (thread-safe), flushed at once so a crash loses nothing."""

    def __init__(self, path, truncate=False):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        if truncate:
            open(path, "w", encoding="utf-8", newline="\n").close()

    def write(self, record):
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._lock, open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()


def read_transcripts(path):
    """{scenario_id: last record} of a transcripts file (later lines win, so a re-run replaces a failed one)."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line cut by a crash
            out[rec["scenario_id"]] = rec
    return out


def fingerprint_id(record):
    return (record.get("fingerprint") or {}).get("id")
