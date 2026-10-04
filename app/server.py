"""Demo app: customer chat with the secure sign-in form, the agent's trace, and the human agent console.

    python -m uvicorn app.server:app --port 8000        # from the repository root

Local mode reads the Gold snapshot (python -m src.bank_tools.snapshot ...) and keeps cases and tickets in a local
SQLite store. The secure form calls the runtime-only tools directly, so the document number and the one-time code
never reach the model.

Each conversation belongs to the browser that opened it: POST /api/conversations returns a random key that every
conversation endpoint checks (X-Conversation-Key header), and the console never shows conversation ids.
Conversations run in parallel, one lock each, with at most APP_MAX_CONCURRENT_TURNS model turns at a time; a
conversation ends after APP_IDLE_MINUTES without activity and takes at most APP_MAX_TURNS customer messages. Each
client opens a bounded number of conversations, and a full registry ends only long-idle ones to make room.

The service clock is a demo clock: it starts at the data's demo time and advances with wall time, and each
conversation can be moved forward on its own. The simulated phone, the test customers and the clock button exist
only while APP_DEMO_CONTROLS is on.

Public demo settings, all off by default (the Hugging Face Space turns them on in app/space/Dockerfile):
APP_DAILY_MODEL_TURNS caps the model turns of the whole app per UTC day, APP_TRUST_FORWARDED_FOR keys the
per-client conversation limit on the first X-Forwarded-For address, APP_PUBLIC_DEMO shows a note in the sidebar, and
APP_FRAME_ANCESTORS lets the listed origins frame the page.
"""
import contextvars
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.bank_tools import LocalRepository, ToolContext, build_service, demo_config
from src.bank_tools.clock import DEMO_NOW, epoch, iso, parse_dt
from src.bank_tools.config import DEMO_LIMITS, Config
from src.bank_tools.state import RateLimiter, StateStore
from src.gold.gold_lib import document_hash, normalize_document

from .agent import Agent, public_turn
from .llm import DatabricksChat

log = logging.getLogger("app")

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/bank_tools"
STATIC = Path(__file__).resolve().parent / "static"
PERSONAS = DATA / "demo_personas.json"
DEFAULT_HOST = "https://dbc-779a8237-d9dd.cloud.databricks.com"

MAX_TEXT = 1000              # characters in one customer message
MAX_BODY_BYTES = 16 * 1024   # any request body
MAX_OFFSET_MIN = 120         # how far one conversation's clock can be moved forward
MAX_CONVERSATIONS = 1000     # live conversations in memory
FULL_IDLE_S = 300            # when the registry is full, only conversations idle this long end to make room
NEW_CONVERSATIONS = (30, 600)  # conversations one client may open per window (seconds)
TURN_SLOT_WAIT_S = 15        # how long a turn waits for one of APP_MAX_CONCURRENT_TURNS before a 503
MAX_LABELS = 5000            # turn labels kept for the console after their conversation ended
CONSOLE_LIMIT = 100          # latest tickets and cases the console serves
SWEEP_EVERY_S = 30
CLIENT_HEADERS = ("x-forwarded-email", "x-forwarded-user")  # set by the Databricks Apps proxy
MAX_CLIENT_KEY = 64          # characters kept of a forwarded address
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
       "frame-ancestors {}")
FRAME_ORIGIN = re.compile(r"^https://(\*\.)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*(:\d{1,5})?$")
# On top of the bank tools' demo limits (DEMO_LIMITS: sign-ins per document, calls per session): a conversation may
# sign in again after its session expires, and takes up to APP_MAX_TURNS messages.
APP_LIMITS = {"challenges_per_conversation": 6, "conversation_calls": 400}
SIGNED_IN_FALLBACK = {
    "es": "Tu identidad quedó verificada, pero tuve un problema para continuar. "
          "Vuelve a escribir tu solicitud, por favor.",
    "pt": "Sua identidade foi verificada, mas tive um problema para continuar. "
          "Escreva sua solicitação de novo, por favor.",
}
CUSTOMER_ID = re.compile(r"\bCLI-[A-Z0-9]{6,}\b")
CONSOLE_HIDDEN = {"conversation_id", "session_id_hash", "idempotency_key_hash", "draft_id", "amount_usd"}
CONSOLE_TICKET_FIELDS = ("ticket_id", "turn_label", "reason_code", "reason_check", "created_at", "queue", "priority",
                         "first_response_hours", "identity_verified", "language", "customer_ref", "request_summary",
                         "agent_reported", "service_verified")
CONSOLE_CASE_FIELDS = ("case_id", "turn_label", "dispute_type", "subcategory", "amount", "currency", "priority",
                       "first_response_due_at")
_DEFAULT = object()


# -- demo clock ----------------------------------------------------------------------------------------------------
_clock_conversation = contextvars.ContextVar("demo_clock_conversation", default=None)


class DemoClock:
    """DEMO ONLY service clock. It starts at `start` when the app starts and advances with wall time, so sessions
    expire and rate-limit windows slide as they would in production. Each conversation adds its own offset (the
    "Adelantar 16 min" button). Endpoints select the conversation with bound(), so moving one conversation forward
    never moves another one, and a new conversation never moves time backwards."""

    def __init__(self, start=DEMO_NOW, max_offset_s=MAX_OFFSET_MIN * 60, wall=time.monotonic):
        self._start = parse_dt(start)
        self._wall, self._t0 = wall, wall()
        self.max_offset_s = max_offset_s
        self._offsets = {}
        self._lock = threading.Lock()

    def base(self):
        """The time of a conversation that was never moved forward (the earliest time any conversation sees)."""
        return self._start + timedelta(seconds=int(self._wall() - self._t0))

    def now(self):
        conv_id = _clock_conversation.get()
        return self.base() + timedelta(seconds=self._offsets.get(conv_id, 0) if conv_id else 0)

    def offset_s(self, conv_id):
        return self._offsets.get(conv_id, 0)

    def advance(self, conv_id, seconds):
        """Move one conversation forward; returns the seconds actually added (the total offset is capped)."""
        with self._lock:
            current = self._offsets.get(conv_id, 0)
            self._offsets[conv_id] = min(self.max_offset_s, current + max(0, int(seconds)))
            return self._offsets[conv_id] - current

    def forget(self, conv_id):
        with self._lock:
            self._offsets.pop(conv_id, None)

    @contextmanager
    def bound(self, conv_id):
        token = _clock_conversation.set(conv_id)
        try:
            yield
        finally:
            _clock_conversation.reset(token)


class DemoState(StateStore):
    """Retention purges run at the clock's base time, the earliest time any conversation sees, so a conversation
    moved forward never drops another conversation's challenges, sessions or drafts. Expiry itself is checked
    against each conversation's own time by the service."""

    def __init__(self, clock, path=None):
        super().__init__(path)
        self._clock = clock

    def purge(self, now):
        return super().purge(min(parse_dt(now), self._clock.base()))


class BaseTimeLimiter(RateLimiter):
    """Rate-limit windows on the demo clock's base time, never a conversation's moved time: a conversation moved
    forward would otherwise empty or fill buckets that every conversation shares (sign-ins per document). Windows
    still slide with wall time. Only the test customers' documents get the demo's higher sign-in limit; every other
    document keeps the bank tools' default."""

    def __init__(self, state, clock, wide_buckets=(), document_limit=Config.challenges_per_document):
        super().__init__(state)
        self._clock, self.wide_buckets, self.document_limit = clock, set(wide_buckets), document_limit

    def _limit(self, bucket, limit):
        if bucket.startswith("chl_doc:") and bucket not in self.wide_buckets:
            return min(limit, self.document_limit)
        return limit

    def hit(self, bucket, limit, window_s, now_epoch):
        return super().hit(bucket, self._limit(bucket, limit), window_s, epoch(self._clock.base()))

    def peek(self, bucket, limit, window_s, now_epoch):
        return super().peek(bucket, self._limit(bucket, limit), window_s, epoch(self._clock.base()))


class SerializedService:
    """The bank service keeps per-call state and is not built for concurrent calls. Tool calls take milliseconds,
    so they run one at a time while the model calls of different conversations overlap."""

    def __init__(self, inner):
        self._inner = inner
        self.lock = threading.RLock()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def call_tool(self, *args, **kwargs):
        with self.lock:
            return self._inner.call_tool(*args, **kwargs)

    def end_conversation(self, conversation_id):
        with self.lock:
            return self._inner.end_conversation(conversation_id)


class BodyLimit:
    """Refuses request bodies larger than max_bytes (413) before they are parsed."""

    def __init__(self, app, max_bytes=MAX_BODY_BYTES):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        detail = {"code": "too_large", "max_bytes": self.max_bytes}
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > self.max_bytes):
            return await JSONResponse({"detail": detail}, status_code=413)(scope, receive, send)
        seen = 0

        async def limited():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body") or b"")
                if seen > self.max_bytes:
                    raise HTTPException(413, detail)
            return message

        await self.app(scope, limited, send)


class JsonOnly:
    """Refuses API POSTs whose body is not declared as JSON (415), so a cross-site form or no-cors request cannot
    drive the API."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("method") == "POST" and scope.get("path", "").startswith("/api/"):
            kind = dict(scope.get("headers") or []).get(b"content-type", b"").split(b";")[0].strip().lower()
            if kind != b"application/json":
                response = JSONResponse({"detail": {"code": "unsupported_media_type"}}, status_code=415)
                return await response(scope, receive, send)
        await self.app(scope, receive, send)


def security_headers(frame_ancestors=""):
    """Same-origin CSP, nosniff and no referrer. Without frame_ancestors no page may frame the app; with a
    space-separated list of https origins (APP_FRAME_ANCESTORS: the Hugging Face Space page shows the app in a frame)
    only those may."""
    origins = str(frame_ancestors or "").split()
    bad = [o for o in origins if not FRAME_ORIGIN.match(o)]
    if bad:
        raise ValueError("APP_FRAME_ANCESTORS takes https origins separated by spaces, not: " + " ".join(bad))
    headers = [(b"content-security-policy", CSP.format(" ".join(origins) or "'none'").encode("ascii")),
               (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer")]
    if not origins:
        headers.append((b"x-frame-options", b"DENY"))
    return headers


SECURITY_HEADERS = security_headers()


class SecurityHeaders:
    """Content-Security-Policy (same-origin only, no framing unless APP_FRAME_ANCESTORS names who may) and nosniff on
    every response."""

    def __init__(self, app, headers=None):
        self.app = app
        self.headers = SECURITY_HEADERS if headers is None else headers

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def with_headers(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": list(message.get("headers") or []) + self.headers}
            await send(message)

        await self.app(scope, receive, with_headers)


class DailyCap:
    """Public demo only: at most `limit` model turns (customer messages and sign-ins) per UTC day across the whole
    app, so a public link cannot run up the model bill. 0 turns it off. The count lives in memory, so a restart
    starts the day again. Days follow the real UTC date, never the demo clock."""

    def __init__(self, limit=0, today=None):
        self.limit = max(0, int(limit or 0))
        self.today = today or (lambda: datetime.now(timezone.utc).date())
        self._day, self._used = None, 0
        self._lock = threading.Lock()

    def _roll(self):
        day = self.today()
        if day != self._day:
            self._day, self._used = day, 0
        return day

    def refusal(self):
        resets_at = (self.today() + timedelta(days=1)).isoformat() + "T00:00:00Z"
        return HTTPException(429, {"code": "daily_limit", "limit": self.limit, "resets_at": resets_at})

    def reached(self):
        if not self.limit:
            return False
        with self._lock:
            self._roll()
            return self._used >= self.limit

    def check(self):
        """429 daily_limit when today's turns are used up; counts nothing."""
        if self.reached():
            raise self.refusal()

    def take(self):
        """Count one model turn, or 429 daily_limit when none is left. Returns the day it was counted on."""
        if not self.limit:
            return None
        with self._lock:
            day = self._roll()
            if self._used >= self.limit:
                raise self.refusal()
            self._used += 1
            return day

    def give_back(self, day):
        """Return a turn that never reached the model (a sign-in with a wrong code), while it is still that day."""
        if not self.limit or day is None:
            return
        with self._lock:
            if self._roll() == day and self._used > 0:
                self._used -= 1


def same_key(expected, given):
    """Constant-time comparison of conversation keys, on bytes (a non-ASCII header is simply a wrong key)."""
    if not expected or not given:
        return False
    return hmac.compare_digest(expected.encode("utf-8", "surrogateescape"), given.encode("utf-8", "surrogateescape"))


# -- request bodies --------------------------------------------------------------------------------------------------
class NewConversation(BaseModel):
    language: str = Field("es", max_length=5)
    previous_id: Optional[str] = Field(None, max_length=64)  # ends the browser's previous conversation (same key)


class Message(BaseModel):
    text: str = Field(max_length=MAX_TEXT)


class AuthStart(BaseModel):
    document_type: str = Field(max_length=16)
    document_number: str = Field(max_length=32)


class AuthVerify(BaseModel):
    code: str = Field(max_length=12)


class ClockAdvance(BaseModel):
    conversation_id: str = Field(max_length=64)
    minutes: int = Field(16, ge=0, le=MAX_OFFSET_MIN)


# -- conversations -----------------------------------------------------------------------------------------------------
@dataclass
class Handle:
    """What the server keeps about one browser conversation; the agent keeps the dialogue itself."""
    conv_id: str
    key: str
    label: str
    last_seen: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    customer_turns: int = 0
    phone: bool = False      # the pending challenge belongs to a test customer, so the simulated phone shows its code
    pending_doc: str = None  # keyed digest of the document of the pending challenge
    signed_doc: str = None   # keyed digest of the document that signed in


class Demo:
    """One app instance: the service, the agent, the demo clock and the conversation registry."""

    def __init__(self, *, service, agent, clock, personas, demo_controls, max_turns, idle_s, wall, max_concurrent=8,
                 console_users=(), daily=None, trust_forwarded_for=False, public_demo=False):
        self.service, self.agent, self.clock = service, agent, clock
        self.demo_controls, self.max_turns, self.idle_s, self.wall = demo_controls, max_turns, idle_s, wall
        self.console_users = {u.strip().lower() for u in console_users if u.strip()}
        self.daily = daily or DailyCap(0)
        self.trust_forwarded_for, self.public_demo = bool(trust_forwarded_for), bool(public_demo)
        self.max_concurrent = max(1, max_concurrent)
        self.handles = {}
        self.labels = {}
        self._registry = threading.Lock()
        self._last_sweep = wall()
        self._opened = {}  # client -> wall times of the conversations it opened in the window
        self.turn_slots = threading.BoundedSemaphore(self.max_concurrent)
        self._doc_key = secrets.token_bytes(32)
        self.personas = personas
        self.persona_docs = {self.doc_digest(p.get("document_type"), p.get("document_number")) for p in personas}

    def doc_digest(self, document_type, document_number):
        normalized = normalize_document(document_type, document_number)
        return hmac.new(self._doc_key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()

    def now(self, conv_id=None):
        with self.clock.bound(conv_id):
            return iso(self.clock.now())

    def open(self, language, client):
        """A new conversation. 429 when this client opened too many lately; 503 when the registry is full of
        conversations that were active in the last FULL_IDLE_S (they are never ended to make room)."""
        self.sweep(force=True)
        with self._registry:
            now = self.wall()
            limit, window = NEW_CONVERSATIONS
            recent = [t for t in self._opened.get(client, []) if t > now - window]
            if len(recent) >= limit:
                raise HTTPException(429, {"code": "too_many_conversations",
                                          "retry_after_s": max(1, int(recent[0] + window - now))})
            if len(self.handles) >= MAX_CONVERSATIONS:
                raise HTTPException(503, {"code": "server_busy"})
            self._opened[client] = recent + [now]
            conv = self.agent.new_conversation(language)
            handle = Handle(conv.id, secrets.token_urlsafe(24), conv.label, now)
            self.handles[conv.id] = handle
            self.labels[conv.id] = conv.label
            while len(self.labels) > MAX_LABELS:
                self.labels.pop(next(iter(self.labels)))
        return handle, conv

    def get(self, conv_id, key, touch=True):
        """The conversation for a request, or 404 when it does not exist, ended, or the key does not match.
        touch=False (the page's periodic clock read) does not count as activity."""
        self.sweep()
        with self._registry:
            handle = self.handles.get(conv_id)
            if handle is None or not same_key(handle.key, key):
                raise HTTPException(404, {"code": "conversation_not_found"})
            if touch:
                handle.last_seen = self.wall()
        conv = self.agent.get(conv_id)
        if conv is None:
            raise HTTPException(404, {"code": "conversation_not_found"})
        return handle, conv

    @contextmanager
    def turn(self, handle, wait_s=1.0):
        """The conversation's lock; 409 when another request of the same conversation is still running."""
        if not handle.lock.acquire(timeout=wait_s):
            raise HTTPException(409, {"code": "busy"})
        try:
            if self.handles.get(handle.conv_id) is not handle:
                raise HTTPException(404, {"code": "conversation_not_found"})
            yield
        finally:
            handle.lock.release()

    @contextmanager
    def model_slot(self):
        """One of the app's concurrent model turns (APP_MAX_CONCURRENT_TURNS); 503 when none frees up in time."""
        if not self.turn_slots.acquire(timeout=TURN_SLOT_WAIT_S):
            raise HTTPException(503, {"code": "server_busy"})
        try:
            yield
        finally:
            self.turn_slots.release()

    def end_owned(self, conv_id, key):
        """End the browser's previous conversation when it starts a new one (only with that conversation's key)."""
        with self._registry:
            handle = self.handles.get(conv_id)
            if handle is None or not same_key(handle.key, key) or handle.lock.locked():
                return
        self.end(conv_id)

    def end(self, conv_id):
        """Forget a conversation and let the service flush its audit and drop its counters."""
        with self._registry:
            self.handles.pop(conv_id, None)
            conversations = getattr(self.agent, "conversations", None)
            if isinstance(conversations, dict):
                conversations.pop(conv_id, None)
        try:
            with self.clock.bound(conv_id):
                self.service.end_conversation(conv_id)
        except Exception:  # noqa: BLE001 - ending is cleanup; it never fails a request
            log.exception("end_conversation failed")
        self.clock.forget(conv_id)

    def sweep(self, force=False):
        """End conversations idle for longer than idle_s. When the registry is full, also end the least recently
        used of those idle for longer than FULL_IDLE_S; conversations in use are never ended to make room."""
        now = self.wall()
        if not force and now - self._last_sweep < SWEEP_EVERY_S:
            return
        self._last_sweep = now
        with self._registry:
            idle = [h for h in self.handles.values() if not h.lock.locked()]
            stale = [h.conv_id for h in idle if now - h.last_seen > self.idle_s]
            extra = len(self.handles) - len(stale) - MAX_CONVERSATIONS + 1
            if extra > 0:
                rest = sorted((h for h in idle if h.conv_id not in stale and now - h.last_seen > FULL_IDLE_S),
                              key=lambda h: h.last_seen)
                stale += [h.conv_id for h in rest[:extra]]
            window = NEW_CONVERSATIONS[1]
            for client in [c for c, times in self._opened.items() if not times or times[-1] <= now - window]:
                del self._opened[client]
        for conv_id in stale:
            self.end(conv_id)

    def end_all(self):
        for conv_id in list(self.handles):
            self.end(conv_id)

    # -- console ---------------------------------------------------------------------------------------------------
    def console_open(self):
        """The console exists with the demo controls on, or for the allow-listed specialists (APP_CONSOLE_USERS)."""
        return self.demo_controls or bool(self.console_users)

    def check_console(self, request):
        """404 when the console is off; 403 when an allow-list is set and the signed-in user is not on it."""
        if not self.console_open():
            raise HTTPException(404, {"code": "not_found"})
        user = request.headers.get("x-forwarded-email", "").strip().lower()
        if self.console_users and user not in self.console_users:
            raise HTTPException(403, {"code": "not_a_specialist"})

    def _rows(self, tables=("handoff_tickets", "dispute_cases")):
        repository = self.service.repository
        rows_of = getattr(repository, "store_rows", None)
        if rows_of is None:
            return [None for _ in tables]
        if getattr(repository, "source", "") == "local":  # one SQLite connection, shared with the service
            with self.service.lock:
                return [rows_of(table) for table in tables]
        return [rows_of(table) for table in tables]

    @staticmethod
    def _latest(rows, limit=CONSOLE_LIMIT):
        """recorded_at is wall time, so the newest rows come first whatever each conversation's clock says."""
        return sorted(rows, key=lambda r: str(r.get("recorded_at") or r.get("created_at") or ""), reverse=True)[:limit]

    def console(self):
        tickets, cases = self._rows()
        if tickets is None:
            return {"tickets": [], "cases": [], "unavailable": True}
        out_tickets = []
        for row in self._latest(tickets):
            row = dict(row)
            for key in ("agent_reported", "service_verified"):
                if key + "_json" in row:
                    try:
                        row[key] = json.loads(row.pop(key + "_json") or "null")
                    except (TypeError, ValueError):
                        row[key] = None
            label = self.labels.get(row.get("conversation_id") or "")
            row = masked(row)
            row["turn_label"] = label
            out_tickets.append({k: row.get(k) for k in CONSOLE_TICKET_FIELDS})
        out_cases = []
        for row in self._latest(cases):
            label = self.labels.get(row.get("conversation_id") or "")
            row = masked(dict(row))
            row["turn_label"] = label
            out_cases.append({k: row.get(k) for k in CONSOLE_CASE_FIELDS})
        return {"tickets": out_tickets, "cases": out_cases}

    def console_ticket_ids(self):
        """Only the latest ticket ids, for the console's unread badge (the page asks after every turn)."""
        (tickets,) = self._rows(("handoff_tickets",))
        return {"ticket_ids": [r.get("ticket_id") for r in self._latest(tickets or [])]}


def mask_customer_id(customer_id):
    return "CLI-…" + customer_id[-4:]


def masked(value):
    """Console copy of a stored value: customer ids masked wherever they appear (customer_id becomes customer_ref)
    and internal keys dropped. The full ids stay in the store."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in CONSOLE_HIDDEN:
                continue
            if key == "customer_id":
                out["customer_ref"] = mask_customer_id(item) if isinstance(item, str) and item else None
            else:
                out[key] = masked(item)
        return out
    if isinstance(value, list):
        return [masked(item) for item in value]
    if isinstance(value, str):
        return CUSTOMER_ID.sub(lambda m: mask_customer_id(m.group(0)), value)
    return value


def runtime_ctx(conv):
    return ToolContext(conversation_id=conv.id, turn_index=max(1, conv.turn_index),
                       trace_id=secrets.token_hex(16), caller="runtime")


# -- wiring ------------------------------------------------------------------------------------------------------------
def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_flag(name, default):
    return str(os.environ.get(name, default)).strip().lower() not in ("0", "false", "no", "off", "")


def service_config(config=None):
    """Service config for the app: the bank tools' demo config (BANK_TOOLS_* from the environment, env demo, demo
    limits) plus APP_LIMITS. Without configured keys it uses per-process random ones (demo refuses the dev keys), so
    sessions end on restart. A config passed in (tests) gets the same limits."""
    if config is not None:
        return replace(config, **{**DEMO_LIMITS, **APP_LIMITS})
    overrides = dict(APP_LIMITS)
    for attr, var in (("session_key", "BANK_TOOLS_SESSION_KEY"), ("otp_key", "BANK_TOOLS_OTP_KEY")):
        if not os.environ.get(var):
            overrides[attr] = secrets.token_hex(32)
    if not os.environ.get("DATABRICKS_HOST"):
        overrides["databricks_host"] = DEFAULT_HOST
    return demo_config(**overrides)


def load_personas(path):
    """Test customers for the sign-in form, without the fields the page does not need."""
    path = Path(path) if path else None
    if not path or not path.exists():
        return []
    personas = json.loads(path.read_text(encoding="utf-8")).get("personas") or []
    keep = ("first_name", "country_code", "customer_status", "document_type", "document_number", "story")
    return [{"persona_id": "P" + str(i + 1), **{k: p.get(k) for k in keep}} for i, p in enumerate(personas)]


def load_classifier():
    """The intent classifier, or None: the app runs without it (model not trained or scikit-learn missing)."""
    try:
        from src.classifier.runtime import load_default
        return load_default()
    except Exception as exc:  # noqa: BLE001 - optional component
        log.warning("intent classifier not loaded: %s", type(exc).__name__)
        return None


def client_of(request, trust_forwarded_for=False):
    """Who opens conversations: the Databricks Apps user when the proxy says so, else the client address.

    Behind a proxy that sets neither (the Hugging Face Space), every request comes from the proxy's address, so with
    trust_forwarded_for (APP_TRUST_FORWARDED_FOR=1) the first X-Forwarded-For address comes first. The header is taken
    as sent: it keeps honest visitors apart, it does not stop someone who forges it (APP_DAILY_MODEL_TURNS bounds the
    cost)."""
    if trust_forwarded_for:
        first = request.headers.get("x-forwarded-for", "").split(",")[0].strip().lower()[:MAX_CLIENT_KEY]
        if first:
            return "fwd:" + first
    for name in CLIENT_HEADERS:
        value = request.headers.get(name, "").strip().lower()
        if value:
            return name + ":" + value
    return "addr:" + (request.client.host if request.client else "")


def create_app(*, llm=None, config=None, repository=None, classifier=_DEFAULT, clock=None, personas_path=PERSONAS,
               demo_controls=None, max_turns=None, idle_minutes=None, wall=time.monotonic, max_concurrent=None,
               console_users=None, daily_turns=None, trust_forwarded_for=None, public_demo=None, frame_ancestors=None):
    """Build the app. Every argument has a default read from the environment; tests pass their own."""
    headers = security_headers(os.environ.get("APP_FRAME_ANCESTORS", "") if frame_ancestors is None
                               else frame_ancestors)
    cfg = service_config(config)
    if clock is None:
        start = cfg.clock if str(cfg.clock).strip().lower() != "system" else datetime.now().replace(microsecond=0)
        clock = DemoClock(start, wall=wall)
    if repository is None and cfg.repository == "local":
        store = os.environ.get("APP_STORE", str(DATA / "app_store.sqlite"))
        repository = LocalRepository(cfg.path(cfg.snapshot), store_path=store)
    state = DemoState(clock, cfg.path(cfg.state_path) or None)
    inner = build_service(cfg, clock=clock, repository=repository, state=state)
    personas = load_personas(personas_path)
    wide = {"chl_doc:" + inner.identity.doc_key(document_hash(p.get("document_type"), p.get("document_number")))
            for p in personas}
    inner.limiter = inner.identity.limiter = BaseTimeLimiter(inner.state, clock, wide)
    service = SerializedService(inner)
    if llm is None:
        os.environ.setdefault("DATABRICKS_HOST", DEFAULT_HOST)
        llm = DatabricksChat(timeout_s=_env_int("APP_LLM_TIMEOUT_S", 30),
                             max_retries=_env_int("APP_LLM_MAX_RETRIES", 1))
    if classifier is _DEFAULT:
        classifier = load_classifier()
    prices = (float(os.environ.get("APP_PRICE_IN_PER_MTOK", "0")), float(os.environ.get("APP_PRICE_OUT_PER_MTOK", "0")))
    agent = Agent(service, llm, classifier=classifier, price_per_mtok=prices)
    if console_users is None:
        console_users = os.environ.get("APP_CONSOLE_USERS", "").split(",")
    demo = Demo(service=service, agent=agent, clock=clock, personas=personas,
                demo_controls=_env_flag("APP_DEMO_CONTROLS", "1") if demo_controls is None else demo_controls,
                max_turns=max_turns or _env_int("APP_MAX_TURNS", 40),
                idle_s=60 * (idle_minutes or _env_int("APP_IDLE_MINUTES", 60)), wall=wall,
                max_concurrent=max_concurrent or _env_int("APP_MAX_CONCURRENT_TURNS", 8), console_users=console_users,
                daily=DailyCap(_env_int("APP_DAILY_MODEL_TURNS", 0) if daily_turns is None else daily_turns),
                trust_forwarded_for=(_env_flag("APP_TRUST_FORWARDED_FOR", "0") if trust_forwarded_for is None
                                     else trust_forwarded_for),
                public_demo=_env_flag("APP_PUBLIC_DEMO", "0") if public_demo is None else public_demo)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        demo.end_all()  # flushes buffered audit records

    app = FastAPI(title="Expediente demo", lifespan=lifespan)
    app.state.demo = demo
    app.add_middleware(BodyLimit, max_bytes=MAX_BODY_BYTES)
    app.add_middleware(JsonOnly)
    app.add_middleware(SecurityHeaders, headers=headers)
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    def app_config():
        return {"now": demo.now(), "model": getattr(llm, "endpoint", ""), "policy_version": service.pol["version"],
                "env": service.config.env, "synthetic": True, "demo_controls": demo.demo_controls,
                "console": demo.console_open(), "public_demo": demo.public_demo,
                "daily_limit_reached": demo.daily.reached(),
                "personas": demo.personas if demo.demo_controls else [],
                "classifier": getattr(classifier, "model_version", None) if classifier else None,
                "limits": {"max_turns": demo.max_turns, "max_text": MAX_TEXT, "max_offset_minutes": MAX_OFFSET_MIN,
                           "idle_minutes": demo.idle_s // 60, "daily_model_turns": demo.daily.limit}}

    @app.get("/api/clock")
    def base_clock():
        return {"now": demo.now()}

    @app.post("/api/conversations")
    def new_conversation(body: NewConversation, request: Request,
                         x_conversation_key: str = Header("", max_length=64)):
        if body.previous_id:
            demo.end_owned(body.previous_id, x_conversation_key)
        handle, conv = demo.open(body.language if body.language in ("es", "pt") else "es",
                                 client_of(request, demo.trust_forwarded_for))
        return {"conversation_id": conv.id, "conversation_key": handle.key, "label": conv.label,
                "language": conv.language, "now": demo.now(conv.id), "daily_limit_reached": demo.daily.reached()}

    @app.post("/api/conversations/{conv_id}/messages")
    def customer_message(conv_id: str, body: Message, x_conversation_key: str = Header("", max_length=64)):
        text = body.text.strip()
        if not text:
            raise HTTPException(400, {"code": "empty"})
        handle, conv = demo.get(conv_id, x_conversation_key)
        with demo.turn(handle), clock.bound(conv.id):
            if handle.customer_turns >= demo.max_turns:
                raise HTTPException(429, {"code": "turn_limit", "max_turns": demo.max_turns})
            demo.daily.check()  # before waiting for a model slot
            with demo.model_slot():
                demo.daily.take()
                handle.customer_turns += 1
                try:
                    out = agent.customer_turn(conv, text)
                except Exception:  # noqa: BLE001 - the agent has its own fallback; this is the last resort
                    log.exception("customer turn failed")
                    raise HTTPException(500, {"code": "turn_failed"})
            out.update({"signed_in": conv.session_token is not None, "now": iso(clock.now()),
                        "turns_left": demo.max_turns - handle.customer_turns})
            return out

    @app.post("/api/conversations/{conv_id}/auth/start")
    def auth_start(conv_id: str, body: AuthStart, x_conversation_key: str = Header("", max_length=64)):
        handle, conv = demo.get(conv_id, x_conversation_key)
        doc = demo.doc_digest(body.document_type, body.document_number)
        if handle.signed_doc and doc != handle.signed_doc:  # another customer in the same chat: new conversation
            return {"ok": False, "error": "OTHER_CUSTOMER"}
        demo.daily.check()  # no code is sent when the sign-in could not go on to a model turn
        with demo.turn(handle, wait_s=5), clock.bound(conv.id):
            res = service.call_tool("start_authentication", {"document_type": body.document_type,
                                                             "document_number": body.document_number},
                                    None, runtime_ctx(conv))
            if not res.ok:
                details = res.error.get("details") or {}
                return {"ok": False, "error": res.error["code"], "message": res.error.get("message"),
                        "retry_after_s": details.get("retry_after_s")}
            conv.challenge_id = res.data["challenge_id"]
            handle.pending_doc, handle.phone = doc, doc in demo.persona_docs
            return {"ok": True, "delivery": res.data.get("delivery"), "expires_at": res.data.get("expires_at"),
                    "code_length": res.data.get("code_length"), "now": iso(clock.now())}

    @app.post("/api/conversations/{conv_id}/auth/verify")
    def auth_verify(conv_id: str, body: AuthVerify, x_conversation_key: str = Header("", max_length=64)):
        handle, conv = demo.get(conv_id, x_conversation_key)
        demo.daily.check()
        with demo.turn(handle, wait_s=5), clock.bound(conv.id), demo.model_slot():  # a valid code starts a model turn
            if not conv.challenge_id:
                raise HTTPException(400, {"code": "no_challenge"})
            day = demo.daily.take()
            res = service.call_tool("verify_otp", {"challenge_id": conv.challenge_id, "code": body.code.strip()},
                                    None, runtime_ctx(conv))
            if not res.ok:
                demo.daily.give_back(day)  # a wrong or expired code never reached the model
                details = res.error.get("details") or {}
                return {"ok": False, "error": res.error["code"], "reason": details.get("reason"),
                        "attempts_remaining": details.get("attempts_remaining"),
                        "locked": details.get("challenge_locked", False)}
            conv.session_token = res.runtime.get("session_token")
            conv.challenge_id = None
            handle.signed_doc, handle.phone = handle.pending_doc, False
            try:
                out = agent.signed_in(conv, res.data or {})
            except Exception:  # noqa: BLE001 - the sign-in stands even when the model part fails
                log.exception("sign-in turn failed")
                out = {"reply": SIGNED_IN_FALLBACK.get(conv.language, SIGNED_IN_FALLBACK["es"]), "blocks": [],
                       "turn": None}
            out.update({"ok": True, "signed_in": True, "country_code": conv.country_code, "now": iso(clock.now()),
                        "session": {k: res.data.get(k) for k in ("session_ref", "expires_at")}})
            return out

    @app.get("/api/conversations/{conv_id}/trace")
    def trace(conv_id: str, x_conversation_key: str = Header("", max_length=64)):
        _, conv = demo.get(conv_id, x_conversation_key)
        return {"turns": [public_turn(t) for t in list(conv.trace)]}

    @app.get("/api/conversations/{conv_id}/clock")
    def conversation_clock(conv_id: str, x_conversation_key: str = Header("", max_length=64)):
        _, conv = demo.get(conv_id, x_conversation_key, touch=False)
        return {"now": demo.now(conv.id), "offset_minutes": clock.offset_s(conv.id) // 60}

    @app.get("/api/console")
    def console(request: Request):
        """Human agent console: the latest transfer tickets with their structured package, and the cases created in
        chat. Only with the demo controls on, or for the specialists in APP_CONSOLE_USERS."""
        demo.check_console(request)
        return demo.console()

    @app.get("/api/console/count")
    def console_count(request: Request):
        """The latest ticket ids only, for the unread badge."""
        demo.check_console(request)
        return demo.console_ticket_ids()

    if demo.demo_controls:
        @app.get("/api/conversations/{conv_id}/phone")
        def demo_phone(conv_id: str, x_conversation_key: str = Header("", max_length=64)):
            """DEMO ONLY: the simulated phone of the test customers shows the code the bank 'sent'. Never available
            to the model. Other documents get no code, real or not, so the phone does not reveal which exist."""
            handle, conv = demo.get(conv_id, x_conversation_key)
            code = None
            if handle.phone and conv.challenge_id:
                code = service.identity.outbox.code_for(conv.challenge_id)
            return {"code": code}

        @app.post("/api/demo/clock")
        def demo_clock(body: ClockAdvance, x_conversation_key: str = Header("", max_length=64)):
            """DEMO ONLY: move one conversation's clock forward, for example past the 15-minute session limit."""
            _, conv = demo.get(body.conversation_id, x_conversation_key)
            added = clock.advance(conv.id, body.minutes * 60)
            return {"now": demo.now(conv.id), "added_minutes": added // 60,
                    "offset_minutes": clock.offset_s(conv.id) // 60, "max_offset_minutes": MAX_OFFSET_MIN}

    return app


def __getattr__(name):
    """`uvicorn app.server:app` builds the app on first access, so importing this module (tests) builds nothing."""
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
