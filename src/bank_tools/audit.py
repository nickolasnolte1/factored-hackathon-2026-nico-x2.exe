"""Audit sinks (CONTRACT.md section 7). One redacted record per tool call, written before the result is returned.

Records hold ids, counts, flags and hashes only: no document numbers, codes, tokens, amounts, merchants or raw
free text (the service builds them; see BankService._audit).
"""
import json
import logging
import os
import threading
from datetime import datetime, timezone

from .repository import sql

log = logging.getLogger("bank_tools")
NESTED_FIELDS = ("args_redacted", "attempts", "policy_decision", "result_summary", "security_events",
                 "faults_injected", "warnings")


class ListAuditSink:
    """In-memory sink for tests and the evaluation harness."""

    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(json.loads(json.dumps(record)))

    def flush(self):
        pass

    def health(self):
        return {"sink": "list", "records": len(self.records)}


class JsonlAuditSink:
    """data/bank_tools/audit/tool_audit_<YYYYMMDD>.jsonl, one file per wall-clock (UTC) day."""

    def __init__(self, directory):
        self.directory = directory
        self._lock = threading.Lock()
        os.makedirs(directory, exist_ok=True)

    def path_for(self, when=None):
        when = when or datetime.now(timezone.utc)
        return os.path.join(self.directory, "tool_audit_" + when.strftime("%Y%m%d") + ".jsonl")

    def write(self, record):
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock, open(self.path_for(), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def flush(self):
        pass

    def health(self):
        return {"sink": "jsonl", "directory": self.directory}


class DatabricksAuditSink:
    """Buffered named-parameter inserts into ops.tool_audit, flushed every `batch_size` records and at the end of
    each conversation. A failed flush never fails a tool call: it is counted (`audit_flush_failed`) and the records
    stay in the JSONL sink of the Tee."""

    def __init__(self, repository, batch_size=20):
        self.repo = repository
        self.batch_size = batch_size
        self.buffer = []
        self.audit_flush_failed = 0
        self._lock = threading.Lock()

    def write(self, record):
        with self._lock:
            self.buffer.append(record)
            full = len(self.buffer) >= self.batch_size
        if full:
            self.flush()

    def flush(self):
        with self._lock:
            batch, self.buffer = self.buffer, []
        if not batch:
            return
        params = {}
        for i, rec in enumerate(batch):
            row = dict(rec)
            row["recorded_date"] = str(rec.get("recorded_at") or "")[:10] or None
            for key in NESTED_FIELDS:
                row[key] = json.dumps(rec.get(key), ensure_ascii=False, sort_keys=True)
            for col in sql.AUDIT_COLUMN_TYPES:
                value = row.get(col)
                if isinstance(value, str) and col in ("ts", "recorded_at"):
                    value = value.rstrip("Z")
                params["r" + str(i) + "_" + col] = value
        try:
            self.repo.ensure_ops_tables()
            self.repo.execute_statement(sql.audit_insert(len(batch)), params, deadline_s=60)
        except Exception as exc:  # noqa: BLE001 - never fail a tool call on audit export
            self.audit_flush_failed += len(batch)
            log.warning("bank_tools: audit flush failed (%s, %d records)", type(exc).__name__, len(batch))

    def end_conversation(self, conversation_id=None):
        self.flush()

    def health(self):
        return {"sink": "databricks", "buffered": len(self.buffer), "audit_flush_failed": self.audit_flush_failed}


class TeeAuditSink:
    """Local JSONL plus Databricks (demo)."""

    def __init__(self, *sinks):
        self.sinks = sinks

    def write(self, record):
        for sink in self.sinks:
            sink.write(record)

    def flush(self):
        for sink in self.sinks:
            sink.flush()

    def health(self):
        return {"sink": "tee", "sinks": [s.health() for s in self.sinks]}
