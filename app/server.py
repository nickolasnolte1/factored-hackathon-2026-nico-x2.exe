"""Demo app: customer chat with the secure sign-in form, the agent's trace, and the human agent console.

    python -m uvicorn app.server:app --port 8000        # from the repository root

Local mode reads the Gold snapshot (python -m src.bank_tools.snapshot ...) and keeps cases and tickets in a local
SQLite store. The secure form calls the runtime-only tools directly, so the document number and the one-time code
never reach the model. The "phone" panel and the clock controls exist only in the demo.
"""
import json
import os
import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/bank_tools"
STATIC = Path(__file__).resolve().parent / "static"

os.environ.setdefault("BANK_TOOLS_ENV", "demo")
for _key in ("BANK_TOOLS_SESSION_KEY", "BANK_TOOLS_OTP_KEY"):
    # Demo refuses the dev keys. Without configured keys, use per-process random ones: sessions end on restart.
    os.environ.setdefault(_key, secrets.token_hex(32))
os.environ.setdefault("DATABRICKS_HOST", "https://dbc-779a8237-d9dd.cloud.databricks.com")

from src.bank_tools import FixedClock, LocalRepository, ToolContext, build_service  # noqa: E402
from src.bank_tools.clock import DEMO_NOW  # noqa: E402

from .agent import Agent, public_turn  # noqa: E402
from .llm import DatabricksChat  # noqa: E402

SNAPSHOT = Path(os.environ.get("BANK_TOOLS_SNAPSHOT", DATA / "snapshot_panel.sqlite"))
STORE = Path(os.environ.get("APP_STORE", DATA / "app_store.sqlite"))
PERSONAS = DATA / "demo_personas.json"

clock = FixedClock()
repository = LocalRepository(str(SNAPSHOT), store_path=str(STORE))
service = build_service(clock=clock, repository=repository)
llm = DatabricksChat()
agent = Agent(service, llm, price_per_mtok=(float(os.environ.get("APP_PRICE_IN_PER_MTOK", "0")),
                                             float(os.environ.get("APP_PRICE_OUT_PER_MTOK", "0"))))
lock = threading.Lock()  # one turn at a time: the demo serves one presenter

app = FastAPI(title="Expediente demo")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class NewConversation(BaseModel):
    language: str = "es"
    reset_clock: bool = False


class Message(BaseModel):
    text: str


class AuthStart(BaseModel):
    document_type: str
    document_number: str


class AuthVerify(BaseModel):
    code: str


class ClockAdvance(BaseModel):
    minutes: int


def _conv(conv_id):
    conv = agent.get(conv_id)
    if not conv:
        raise HTTPException(404, "conversation not found")
    return conv


def _runtime_ctx(conv):
    return ToolContext(conversation_id=conv.id, turn_index=max(1, conv.turn_index),
                       trace_id=secrets.token_hex(16), caller="runtime")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def config():
    personas = json.loads(PERSONAS.read_text())["personas"] if PERSONAS.exists() else []
    return {"now": clock.now().isoformat(), "model": llm.endpoint, "policy_version": service.pol["version"],
            "env": service.config.env, "personas": personas, "synthetic": True}


@app.post("/api/conversations")
def new_conversation(body: NewConversation):
    if body.reset_clock:  # DEMO ONLY: a fresh conversation starts at the demo's service time
        with lock:
            clock.set(DEMO_NOW)
    conv = agent.new_conversation(body.language if body.language in ("es", "pt") else "es")
    return {"conversation_id": conv.id, "language": conv.language, "now": clock.now().isoformat()}


@app.post("/api/conversations/{conv_id}/messages")
def customer_message(conv_id: str, body: Message):
    text = body.text.strip()[:1000]
    if not text:
        raise HTTPException(400, "empty message")
    with lock:
        conv = _conv(conv_id)
        out = agent.customer_turn(conv, text)
        out["signed_in"] = conv.session_token is not None
        return out


@app.post("/api/conversations/{conv_id}/auth/start")
def auth_start(conv_id: str, body: AuthStart):
    with lock:
        conv = _conv(conv_id)
        res = service.call_tool("start_authentication", {"document_type": body.document_type,
                                                         "document_number": body.document_number},
                                None, _runtime_ctx(conv))
        if not res.ok:
            return {"ok": False, "error": res.error["code"], "message": res.error.get("message")}
        conv.challenge_id = res.data["challenge_id"]
        return {"ok": True, "delivery": res.data.get("delivery"), "expires_at": res.data.get("expires_at"),
                "code_length": res.data.get("code_length")}


@app.post("/api/conversations/{conv_id}/auth/verify")
def auth_verify(conv_id: str, body: AuthVerify):
    with lock:
        conv = _conv(conv_id)
        if not conv.challenge_id:
            raise HTTPException(400, "no pending challenge")
        res = service.call_tool("verify_otp", {"challenge_id": conv.challenge_id, "code": body.code.strip()},
                                None, _runtime_ctx(conv))
        if not res.ok:
            details = res.error.get("details") or {}
            return {"ok": False, "error": res.error["code"], "attempts_remaining": details.get("attempts_remaining"),
                    "locked": details.get("challenge_locked", False)}
        conv.session_token = res.runtime.get("session_token")
        conv.challenge_id = None
        out = agent.signed_in(conv, res.data or {})
        out.update({"ok": True, "signed_in": True, "session": {k: res.data.get(k) for k in ("session_ref", "expires_at")},
                    "country_code": conv.country_code})
        return out


@app.get("/api/conversations/{conv_id}/phone")
def demo_phone(conv_id: str):
    """DEMO ONLY: the simulated phone shows the one-time code the bank 'sent'. Never available to the model."""
    conv = _conv(conv_id)
    code = service.identity.outbox.code_for(conv.challenge_id) if conv.challenge_id else None
    return {"code": code}


@app.get("/api/conversations/{conv_id}/trace")
def trace(conv_id: str):
    conv = _conv(conv_id)
    return {"turns": [public_turn(t) for t in conv.trace]}


@app.post("/api/demo/clock")
def demo_clock(body: ClockAdvance):
    """DEMO ONLY: move the service clock forward, for example past the 15-minute session limit."""
    with lock:
        clock.advance(max(0, min(body.minutes, 24 * 60)) * 60)
        return {"now": clock.now().isoformat()}


@app.get("/api/console")
def console():
    """Human agent console: transfer tickets with their structured package, and the cases created in chat."""
    tickets = []
    for row in repository.store_rows("handoff_tickets"):
        row = dict(row)
        for key in ("agent_reported_json", "service_verified_json"):
            try:
                row[key[:-5]] = json.loads(row.pop(key) or "null")
            except (TypeError, ValueError):
                row[key[:-5]] = None
        cid = row.pop("customer_id", None)
        row["customer_ref"] = ("CLI-…" + cid[-4:]) if cid else None  # masked: the full id stays in the store
        row.pop("session_id_hash", None)
        tickets.append(row)
    cases = []
    for r in repository.store_rows("dispute_cases"):
        r = dict(r)
        cid = r.pop("customer_id", None)
        r["customer_ref"] = ("CLI-…" + cid[-4:]) if cid else None
        for key in ("session_id_hash", "idempotency_key_hash", "draft_id", "amount_usd"):
            r.pop(key, None)
        cases.append(r)
    tickets.sort(key=lambda r: str(r.get("recorded_at") or r.get("created_at") or ""), reverse=True)  # recorded_at: wall clock
    cases.sort(key=lambda r: str(r.get("recorded_at") or r.get("created_at") or ""), reverse=True)
    return {"tickets": tickets, "cases": cases}
