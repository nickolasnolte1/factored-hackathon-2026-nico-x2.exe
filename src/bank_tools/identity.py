"""Identity: document + one-time code challenges, the test outbox, HMAC-signed session tokens and the evaluation-only
test session issuer (CONTRACT.md section 2).

A customer number, e-mail, phone, product number or name never authenticates: no tool accepts them as a factor.
The document number is hashed in-process (the Gold twin `gold_lib.document_hash`) and never leaves it.
"""
import base64
import hashlib
import hmac
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.gold.gold_lib import document_hash
from src.policy import dispute_policy as dp

from .clock import epoch, from_epoch, iso, wall_utc
from .config import FAULT_ENVS
from .errors import ToolError
from .ids import b64url

TOKEN_PREFIX = "bts1"
KID = "k1"
FUTURE_SKEW_S = 60
AMR = ["document", "otp"]


def derive(secret, label):
    return hmac.new(secret.encode("utf-8"), ("bank-tools/" + label + "/v1").encode("utf-8"), hashlib.sha256).digest()


def _b64decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Keys:
    """Per-purpose keys derived from the environment secrets (section 2.3)."""

    def __init__(self, session_key, otp_key, previous_session_key=""):
        self.session = derive(session_key, "session")
        self.session_previous = derive(previous_session_key, "session") if previous_session_key else None
        self.confirm = derive(session_key, "confirmation")
        self.ref = derive(session_key, "ref")
        self.audit = derive(session_key, "audit")
        self.otp = derive(otp_key, "otp")

    def mac(self, key, message):
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    def mac_hex(self, key, message, n=16):
        return self.mac(key, message).hex()[:n]


class TestOutbox:
    """OTP delivery for tests, evaluation and the demo: codes stay in memory for the harness or simulated customer,
    optionally appended to BANK_TOOLS_OTP_OUTBOX_FILE (a demo 'phone' panel). Never readable by a tool."""

    def __init__(self, file_path=None):
        self.file_path = file_path
        self._codes = {}
        self._lock = threading.Lock()

    def send(self, challenge_id, customer_id, channel, code):
        """Deliver `code`. A decoy (code None) does the same work but leaves nothing deliverable, so the time a
        start_authentication call takes never tells whether the document exists."""
        with self._lock:
            if code is not None:
                self._codes[challenge_id] = {"code": code, "channel": channel}
            if self.file_path:
                os.makedirs(os.path.dirname(os.path.abspath(self.file_path)), exist_ok=True)
                with open(self.file_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"challenge_id": challenge_id, "channel": channel, "code": code,
                                         "sent_at": wall_utc()}) + "\n")

    def code_for(self, challenge_id):
        hit = self._codes.get(challenge_id)
        return hit["code"] if hit else None

    def delivered(self):
        return len(self._codes)


@dataclass
class Session:
    sid: str
    customer_id: str
    customer_status: str
    authenticated_at: datetime
    expires_at: datetime
    method: str
    amr: tuple = tuple(AMR)

    @property
    def sid_hash(self):
        return hashlib.sha256(self.sid.encode("utf-8")).hexdigest()[:16]


class IdentityService:
    def __init__(self, config, keys, state, guarded, repository, ids, outbox, pol, limiter, audit_hook=None):
        self.config = config
        self.keys = keys
        self.state = state
        self.guarded = guarded
        self.repository = repository
        self.ids = ids
        self.outbox = outbox
        self.pol = pol
        self.limiter = limiter
        self.audit_hook = audit_hook
        auth = pol["authentication"]
        self.digits = auth["second_factor"]["digits"]
        self.code_ttl_s = auth["second_factor"]["ttl_seconds"]
        self.max_attempts = auth["second_factor"]["max_attempts"]
        self.ttl_minutes = auth["session_ttl_minutes"]

    # -- references --------------------------------------------------------------------------------------------
    def session_ref(self, sid):
        return "S-" + self.keys.mac(self.keys.ref, sid).hex()[:8]

    def conversation_ref(self, conversation_id):
        """The `cnv` claim: binds a token to the conversation that authenticated it (opaque, keyed)."""
        return self.keys.mac_hex(self.keys.ref, "conversation|" + conversation_id)

    def doc_key(self, doc_hash):
        return self.keys.mac_hex(self.keys.audit, doc_hash)

    def customer_key(self, customer_id):
        return self.keys.mac_hex(self.keys.audit, customer_id) if customer_id else None

    # -- challenges --------------------------------------------------------------------------------------------
    def start(self, conversation_id, document_type, document_number, channel, now):
        doc_hash = document_hash(document_type, document_number)
        doc_key = self.doc_key(doc_hash)
        cfg, now_e = self.config, epoch(now)
        self.state.purge(now)
        for bucket, limit, window in (("chl_conv:" + conversation_id, cfg.challenges_per_conversation,
                                       cfg.challenge_conversation_window_s),
                                      ("chl_doc:" + doc_key, cfg.challenges_per_document,
                                       cfg.challenge_document_window_s)):
            if self.limiter.peek(bucket, limit, window, now_e):
                wait = self.limiter.hit(bucket, limit, window, now_e)
                raise ToolError("RATE_LIMITED", {"retry_after_s": wait}, internal_reason="challenge_rate_limit")
        for bucket, limit, window in (("chl_conv:" + conversation_id, cfg.challenges_per_conversation,
                                       cfg.challenge_conversation_window_s),
                                      ("chl_doc:" + doc_key, cfg.challenges_per_document,
                                       cfg.challenge_document_window_s)):
            self.limiter.hit(bucket, limit, window, now_e)
        found = self.guarded.find_customer_by_document(document_type, doc_hash)
        challenge_id = self.ids.code("CHL-")
        code = self.ids.otp(self.digits)  # drawn for decoys too, so both paths look alike
        expires_at = now + timedelta(seconds=self.code_ttl_s)
        record = {"conversation_id": conversation_id, "customer_id": None, "customer_status": None,
                  "code_hash": None, "expires_at": iso(expires_at), "attempts": 0, "status": "pending",
                  "doc_key": doc_key}
        if found:
            record.update({"customer_id": found["customer_id"], "customer_status": found["customer_status"],
                           "code_hash": self.keys.mac(self.keys.otp, challenge_id + "|" + code).hex()})
            self.outbox.send(challenge_id, found["customer_id"], channel, code)
        else:  # same work as a real challenge, delivery included, with nothing deliverable
            self.keys.mac(self.keys.otp, challenge_id + "|" + code)
            self.outbox.send(challenge_id, None, channel, None)
        self.state.put("challenge", challenge_id, record, expires_at=expires_at)
        data = {"challenge_id": challenge_id, "delivery": {"channel": channel, "status": "sent"},
                "expires_at": iso(expires_at), "code_length": self.digits, "max_attempts": self.max_attempts}
        return data, {"doc_key": doc_key, "decoy": found is None}

    def verify(self, conversation_id, challenge_id, code, now):
        ch = self.state.get("challenge", challenge_id)

        def fail(reason, remaining, locked=False, internal=None):
            raise ToolError("AUTH_FAILED", {"reason": reason, "attempts_remaining": remaining,
                                            "challenge_locked": locked,
                                            "next_action": "ask_code_again" if reason == "invalid_code"
                                            else "restart_authentication"},
                            internal_reason=internal or reason)

        if not ch or ch["conversation_id"] != conversation_id:
            fail("invalid_challenge", 0)
        if ch["status"] == "locked":
            fail("challenge_locked", 0, True)
        if ch["status"] != "pending":
            fail("invalid_challenge", 0, internal="challenge_" + ch["status"])
        if now >= datetime.fromisoformat(ch["expires_at"]):
            ch["status"] = "expired"
            self.state.put("challenge", challenge_id, ch, expires_at=ch["expires_at"])
            fail("challenge_expired", 0)
        offered = self.keys.mac(self.keys.otp, challenge_id + "|" + code).hex()
        if ch["code_hash"] is None or not hmac.compare_digest(ch["code_hash"], offered):
            ch["attempts"] += 1
            remaining = max(0, self.max_attempts - ch["attempts"])
            if remaining == 0:
                ch["status"] = "locked"
            self.state.put("challenge", challenge_id, ch, expires_at=ch["expires_at"])
            if remaining == 0:
                fail("challenge_locked", 0, True, internal="locked_after_wrong_code")
            fail("invalid_code", remaining)
        ch["status"] = "verified"
        self.state.put("challenge", challenge_id, ch, expires_at=ch["expires_at"])
        token, session = self.issue(ch["customer_id"], ch["customer_status"], now, "otp", conversation_id)
        data = {"authenticated": True, "session_ref": self.session_ref(session.sid),
                "expires_at": iso(session.expires_at), "ttl_minutes": self.ttl_minutes}
        return data, token, session

    # -- sessions ----------------------------------------------------------------------------------------------
    def issue(self, customer_id, customer_status, authenticated_at, method, conversation_id=None):
        """Sign a new session; revokes any earlier session of the conversation."""
        if conversation_id:
            previous = self.state.get("conv_session", conversation_id)
            if previous:
                self.revoke(previous)
        sid = self.ids.sid()
        expires_at = dp.session_expires_at(authenticated_at, self.pol)
        payload = {"v": 1, "sid": sid, "sub": customer_id, "iat": epoch(authenticated_at), "exp": epoch(expires_at),
                   "amr": list(AMR), "method": method, "kid": KID}
        if conversation_id:
            payload["cnv"] = self.conversation_ref(conversation_id)
        body = b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        sig = b64url(self.keys.mac(self.keys.session, TOKEN_PREFIX + "." + body))
        record = {"customer_id": customer_id, "customer_status": customer_status,
                  "authenticated_at": iso(authenticated_at), "expires_at": iso(expires_at), "method": method,
                  "revoked": False, "conversation_id": conversation_id}
        self.state.put("session", sid, record, expires_at=expires_at)
        if conversation_id:
            self.state.put("conv_session", conversation_id, sid, expires_at=expires_at)
        session = Session(sid, customer_id, customer_status, authenticated_at, expires_at, method)
        return TOKEN_PREFIX + "." + body + "." + sig, session

    def revoke(self, sid):
        rec = self.state.get("session", sid)
        if rec is None:
            rec = {"revoked": True, "expires_at": None}
        rec["revoked"] = True
        self.state.put("session", sid, rec, expires_at=rec.get("expires_at"))

    def _decode(self, token):
        parts = token.split(".") if isinstance(token, str) else []
        if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
            return None, "token_malformed"
        try:
            sig = _b64decode(parts[2])
        except (ValueError, TypeError):
            return None, "token_malformed"
        if b64url(sig) != parts[2]:  # one token, one spelling: no padding, junk or spare bits
            return None, "token_malformed"
        message = TOKEN_PREFIX + "." + parts[1]
        keys = [k for k in (self.keys.session, self.keys.session_previous) if k is not None]
        if not any(hmac.compare_digest(self.keys.mac(k, message), sig) for k in keys):
            return None, "bad_signature"
        try:
            payload = json.loads(_b64decode(parts[1]))
        except (ValueError, TypeError):
            return None, "token_malformed"
        if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("kid") != KID:
            return None, "token_malformed"
        if not all(isinstance(payload.get(k), int) for k in ("iat", "exp")) or not payload.get("sid"):
            return None, "token_malformed"
        return payload, None

    def validate(self, token, now, conversation_id=None):
        """Session for a token, or ToolError(AUTH_REQUIRED | SESSION_EXPIRED) in the order of section 2.3.

        A token that carries a `cnv` claim is valid only in that conversation, and a test-issuer token only when
        BANK_TOOLS_ENV is test or eval (security review, findings F1 and F2)."""
        if not token:
            raise ToolError("AUTH_REQUIRED", internal_reason="no_token")
        payload, reason = self._decode(token)
        if payload is None:
            raise ToolError("AUTH_REQUIRED", internal_reason=reason)
        if payload.get("method") == "test_issuer" and self.config.env not in FAULT_ENVS:
            raise ToolError("AUTH_REQUIRED", internal_reason="test_issuer_outside_test_env")
        bound = payload.get("cnv")
        if bound is not None and not (isinstance(bound, str) and conversation_id and hmac.compare_digest(
                bound, self.conversation_ref(conversation_id))):
            raise ToolError("AUTH_REQUIRED", internal_reason="conversation_mismatch")
        if payload["iat"] > epoch(now) + FUTURE_SKEW_S:
            raise ToolError("AUTH_REQUIRED", internal_reason="issued_in_future")
        rec = self.state.get("session", payload["sid"])
        if rec and rec.get("revoked"):
            raise ToolError("AUTH_REQUIRED", internal_reason="revoked")
        ok, why = dp.authenticate(payload.get("amr"), self.pol)
        if not ok:
            raise ToolError("AUTH_REQUIRED", internal_reason=why)
        expires_at = from_epoch(payload["exp"])
        ok, why = dp.session_active({"authenticated": True, "auth_factors": payload.get("amr"),
                                     "expires_at": expires_at}, now, self.pol)
        if not ok:
            raise ToolError("SESSION_EXPIRED", {"expired_at": iso(expires_at)}, internal_reason=why)
        if rec is None:  # e.g. after a restart in memory mode: re-read the status once
            profile = self.guarded.get_customer(payload["sub"])
            if profile is None:
                raise ToolError("AUTH_REQUIRED", internal_reason="unknown_subject")
            rec = {"customer_id": payload["sub"], "customer_status": profile["customer_status"],
                   "authenticated_at": iso(from_epoch(payload["iat"])), "expires_at": iso(expires_at),
                   "method": payload.get("method"), "revoked": False, "conversation_id": None}
            self.state.put("session", payload["sid"], rec, expires_at=expires_at)
        return Session(payload["sid"], payload["sub"], rec["customer_status"], from_epoch(payload["iat"]), expires_at,
                       payload.get("method") or "otp", tuple(payload.get("amr") or ()))

    # -- evaluation only ---------------------------------------------------------------------------------------
    def issue_test_session(self, customer_id, authenticated_at=None, conversation_id=None):
        """Trusted test session for the e2e harness (BANK_TOOLS_ENV test or eval only). Not a tool."""
        if self.config.env not in FAULT_ENVS:
            raise RuntimeError("issue_test_session is available only when BANK_TOOLS_ENV is test or eval")
        profile = self.repository.get_customer(customer_id)
        if profile is None:
            raise ValueError("unknown customer")
        at = authenticated_at or self.audit_hook.now()
        token, session = self.issue(customer_id, profile["customer_status"], at, "test_issuer", conversation_id)
        if self.audit_hook is not None:
            self.audit_hook.test_session_issued(session, conversation_id)
        return token
