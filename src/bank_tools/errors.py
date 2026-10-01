"""Typed tool errors (CONTRACT.md section 4) and the repository exceptions behind them.

Messages, `retryable` and the allowed `next_action` values come from the `errors` catalog of tool_schemas.json.
`details` keeps only the whitelisted keys of each code; internal reasons go to the audit, never to the model.
"""
import json

from .schemas import SCHEMAS_PATH

with open(SCHEMAS_PATH, encoding="utf-8") as _fh:
    CATALOG = json.load(_fh)["errors"]

DETAIL_KEYS = {
    "AUTH_REQUIRED": {"next_action"},
    "AUTH_FAILED": {"reason", "attempts_remaining", "challenge_locked", "next_action"},
    "SESSION_EXPIRED": {"expired_at", "next_action"},
    "FORBIDDEN": {"next_action"},
    "NOT_FOUND": {"resource", "next_action"},
    "VALIDATION_ERROR": {"fields", "reason", "next_action"},
    "CONFIRMATION_REQUIRED": {"reason", "next_action"},
    "POLICY_BLOCKED": {"reason", "handoff_reason", "eligibility_reason", "confirmation_id_usable_for_handoff",
                       "next_action"},
    "RATE_LIMITED": {"retry_after_s", "next_action"},
    "UNAVAILABLE": {"attempts", "write_state", "handoff_reason", "next_action"},
    "INTERNAL": {"reason", "handoff_reason", "next_action"},
}
assert set(DETAIL_KEYS) == set(CATALOG), "error catalog and detail whitelist differ"


class ToolError(Exception):
    """An error returned to the model as {code, message, retryable, details}."""

    def __init__(self, code, details=None, internal_reason=None):
        spec = CATALOG[code]
        super().__init__(code)
        self.code = code
        self.message = spec["message"]
        self.retryable = bool(spec["retryable"])
        clean = {k: v for k, v in (details or {}).items() if k in DETAIL_KEYS[code]}
        clean.setdefault("next_action", spec["next_action"][0])
        if clean["next_action"] not in spec["next_action"]:
            raise ValueError(f"next_action {clean['next_action']!r} not allowed for {code}")
        self.details = clean
        self.internal_reason = internal_reason or code.lower()

    def to_dict(self):
        return {"code": self.code, "message": self.message, "retryable": self.retryable,
                "details": json.loads(json.dumps(self.details))}


class RepositoryError(Exception):
    """Non-transient repository failure (bad request, FAILED statement, schema mismatch): never retried."""


class TransientRepositoryError(Exception):
    """Transient failure: retried by GuardedRepository within the policy budget.

    `maybe_applied` is true when the statement may have run before the failure (a timeout, a lost response, a
    gateway error), so a failed write's outcome is unknown rather than "not written"."""

    maybe_applied = False

    def __init__(self, *args, maybe_applied=None):
        super().__init__(*args)
        if maybe_applied is not None:
            self.maybe_applied = bool(maybe_applied)


class RepositoryUnavailable(TransientRepositoryError):
    pass


class RepositoryTimeout(TransientRepositoryError):
    maybe_applied = True


class MalformedRecord(Exception):
    """A directly fetched row failed validation (becomes INTERNAL malformed_record)."""

    def __init__(self, op):
        super().__init__(op)
        self.op = op


class Unavailable(Exception):
    """The retry budget of one repository operation is spent (becomes UNAVAILABLE)."""

    def __init__(self, op, attempts, write_state=None):
        super().__init__(op)
        self.op = op
        self.attempts = attempts
        self.write_state = write_state
