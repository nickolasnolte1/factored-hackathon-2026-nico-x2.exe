"""Service configuration read from the environment (CONTRACT.md section 11).

Secrets have DEV ONLY defaults for test, eval and dev; demo refuses to start with them. Keys and tokens are kept out
of repr() so a logged Config never shows them.
"""
import logging
import os
import re
from dataclasses import dataclass, field, fields, replace

log = logging.getLogger("bank_tools")

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SERVICE_VERSION = "1.0.0"
ENVS = ("test", "eval", "dev", "demo")
FAULT_ENVS = ("test", "eval")
DEV_SESSION_KEY = "dev-only-change-me-session-signing-key-000000"
DEV_OTP_KEY = "dev-only-change-me-otp-hash-key-0000000000000"
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
MIN_KEY_BYTES = 32
# A live demo signs the same personas in many times and runs long sessions; the other envs keep the defaults.
DEMO_LIMITS = {"challenges_per_document": 50, "session_calls_per_window": 400}

_ENV_MAP = {  # attribute -> environment variable
    "env": "BANK_TOOLS_ENV",
    "session_key": "BANK_TOOLS_SESSION_KEY",
    "session_key_previous": "BANK_TOOLS_SESSION_KEY_PREVIOUS",
    "otp_key": "BANK_TOOLS_OTP_KEY",
    "repository": "BANK_TOOLS_REPOSITORY",
    "snapshot": "BANK_TOOLS_SNAPSHOT",
    "state_path": "BANK_TOOLS_STATE_PATH",
    "clock": "BANK_TOOLS_CLOCK",
    "confirmation_ttl_s": "BANK_TOOLS_CONFIRMATION_TTL_S",
    "model_auth": "BANK_TOOLS_MODEL_AUTH",
    "read_only": "BANK_TOOLS_READ_ONLY",
    "audit_dir": "BANK_TOOLS_AUDIT_DIR",
    "audit_retention_days": "BANK_TOOLS_AUDIT_RETENTION_DAYS",
    "otp_outbox_file": "BANK_TOOLS_OTP_OUTBOX_FILE",
    "catalog": "BANK_TOOLS_CATALOG",
    "gold_schema": "BANK_TOOLS_GOLD_SCHEMA",
    "ops_schema": "BANK_TOOLS_OPS_SCHEMA",
    "databricks_host": "DATABRICKS_HOST",
    "databricks_token": "DATABRICKS_TOKEN",
    "databricks_profile": "DATABRICKS_CONFIG_PROFILE",
    "databricks_warehouse_id": "DATABRICKS_WAREHOUSE_ID",
    "databricks_cli": "DATABRICKS_CLI",
}


@dataclass
class Config:
    env: str = "dev"
    session_key: str = field(default="", repr=False)
    session_key_previous: str = field(default="", repr=False)
    otp_key: str = field(default="", repr=False)
    repository: str = "local"
    snapshot: str = "data/bank_tools/snapshot_panel.sqlite"
    state_path: str = ""
    clock: str = "2026-06-19T09:00:00"
    confirmation_ttl_s: int = 600
    model_auth: bool = False
    read_only: bool = False
    audit_dir: str = "data/bank_tools/audit"
    audit_retention_days: int = 30
    otp_outbox_file: str = ""
    catalog: str = "workspace"
    gold_schema: str = "gold"
    ops_schema: str = "ops"
    databricks_host: str = ""
    databricks_token: str = field(default="", repr=False)
    databricks_profile: str = ""
    databricks_warehouse_id: str = ""
    databricks_cli: str = ""
    # Not in the environment table: deterministic ids in test/eval, limits of section 4, deadlines of section 8.
    id_seed: int = 20261005
    session_calls_per_window: int = 40
    session_window_s: int = 300
    conversation_calls: int = 200
    policy_info_calls: int = 20
    challenges_per_conversation: int = 3
    challenge_conversation_window_s: int = 900
    challenges_per_document: int = 5
    challenge_document_window_s: int = 3600
    attempt_deadline_s: float = 8.0
    call_deadline_s: float = 20.0
    foreign_probe_limit: int = 3
    used_dev_keys: bool = field(default=False, repr=False)

    @classmethod
    def from_env(cls, environ=None, **overrides):
        environ = os.environ if environ is None else environ
        values = {}
        types = {f.name: f.type for f in fields(cls)}
        for attr, var in _ENV_MAP.items():
            raw = environ.get(var)
            if raw is None or raw == "":
                continue
            values[attr] = _cast(types[attr], raw)
        values.update(overrides)
        return cls(**values).checked()

    def checked(self):
        """Validate and fill the DEV ONLY key defaults; returns a new Config."""
        if self.env not in ENVS:
            raise ValueError(f"BANK_TOOLS_ENV must be one of {ENVS}")
        if self.repository not in ("local", "databricks"):
            raise ValueError("BANK_TOOLS_REPOSITORY must be local or databricks")
        for name in ("catalog", "gold_schema", "ops_schema"):
            if not IDENTIFIER.match(getattr(self, name)):
                raise ValueError(f"{name} must match {IDENTIFIER.pattern}")
        cfg, dev = self, self.used_dev_keys
        if not cfg.session_key or not cfg.otp_key:
            if cfg.env == "demo":
                raise ValueError("demo requires BANK_TOOLS_SESSION_KEY and BANK_TOOLS_OTP_KEY")
            cfg = replace(cfg, session_key=cfg.session_key or DEV_SESSION_KEY, otp_key=cfg.otp_key or DEV_OTP_KEY)
            dev = True
        if cfg.env == "demo" and (cfg.session_key == DEV_SESSION_KEY or cfg.otp_key == DEV_OTP_KEY):
            raise ValueError("demo refuses to start with the DEV ONLY default keys")
        for name in ("session_key", "otp_key"):
            if len(getattr(cfg, name).encode("utf-8")) < MIN_KEY_BYTES:
                raise ValueError(f"{name} must be at least {MIN_KEY_BYTES} bytes")
        if cfg.confirmation_ttl_s <= 0:
            raise ValueError("BANK_TOOLS_CONFIRMATION_TTL_S must be positive")
        if dev and not self.used_dev_keys:
            log.warning("bank_tools: using the DEV ONLY default keys (env=%s)", cfg.env)
        return replace(cfg, used_dev_keys=dev)

    @property
    def faults_allowed(self):
        return self.env in FAULT_ENVS

    def path(self, value):
        """Resolve a configured path; relative paths are relative to the repository root."""
        if not value:
            return value
        return value if os.path.isabs(value) or value == ":memory:" else os.path.join(REPO_ROOT, value)


def demo_config(environ=None, **overrides):
    """Config for env demo with DEMO_LIMITS (sign-in challenges per document per hour, calls per session per
    window), so a long demo session never hits RATE_LIMITED. Like any demo config it needs real keys."""
    return Config.from_env(environ, **{"env": "demo", **DEMO_LIMITS, **overrides})


def _cast(kind, raw):
    kind = kind if isinstance(kind, str) else getattr(kind, "__name__", str(kind))
    if kind == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    return raw.strip()
