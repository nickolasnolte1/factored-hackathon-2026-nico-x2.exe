"""Mock banking tool service for the dispute-intake agent (contract: CONTRACT.md, tool definitions: tool_schemas.json).

    from src.bank_tools import build_service, ToolContext
    service = build_service()                       # reads BANK_TOOLS_* from the environment (section 11)
    ctx = ToolContext(conversation_id="c-1", turn_index=1, trace_id="0" * 32)
    service.call_tool("get_policy_info", {"topic": "scope", "language": "es"}, None, ctx).for_model()
"""
from .audit import DatabricksAuditSink, JsonlAuditSink, ListAuditSink, TeeAuditSink
from .clock import FixedClock, SystemClock, clock_from_config
from .config import SERVICE_VERSION, Config, demo_config
from .errors import ToolError
from .faults import FaultInjector, NullFaultInjector
from .identity import TestOutbox
from .repository import LocalRepository
from .service import BankService, ToolContext, ToolResult

__all__ = ["build_service", "BankService", "ToolContext", "ToolResult", "ToolError", "Config", "demo_config",
           "FixedClock", "SystemClock", "FaultInjector", "NullFaultInjector", "LocalRepository", "ListAuditSink",
           "JsonlAuditSink", "DatabricksAuditSink", "TeeAuditSink", "TestOutbox", "SERVICE_VERSION"]


def build_service(config=None, *, clock=None, repository=None, audit=None, faults=None, **kwargs):
    """Wire a BankService from the environment (or `config`); any part can be overridden.

    Repository: LocalRepository over BANK_TOOLS_SNAPSHOT, or DatabricksRepository (BANK_TOOLS_REPOSITORY=databricks).
    Audit: ListAuditSink in test/eval, JSONL under BANK_TOOLS_AUDIT_DIR in dev, JSONL + ops.tool_audit in demo
    with the Databricks repository."""
    cfg = config or Config.from_env()
    clock = clock or clock_from_config(cfg.clock)
    if repository is None:
        if cfg.repository == "databricks":
            from .repository.databricks import DatabricksRepository
            repository = DatabricksRepository.from_config(cfg)
        else:
            repository = LocalRepository(cfg.path(cfg.snapshot))
    if audit is None:
        if cfg.env in ("test", "eval"):
            audit = ListAuditSink()
        else:
            audit = JsonlAuditSink(cfg.path(cfg.audit_dir))
            if cfg.env == "demo" and getattr(repository, "source", "") == "databricks":
                audit = TeeAuditSink(audit, DatabricksAuditSink(repository))
    return BankService(repository, clock, audit, faults, cfg, **kwargs)
