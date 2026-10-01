"""Retention jobs of the bank tool service (CONTRACT.md section 7).

    python -m src.bank_tools.retention                     # local audit JSONL older than BANK_TOOLS_AUDIT_RETENTION_DAYS,
                                                           # expired entries of the BANK_TOOLS_STATE_PATH store
    python -m src.bank_tools.retention --purge-env demo    # delete mock cases and tickets of one env in workspace.ops
    python -m src.bank_tools.retention --ops-audit         # ops.tool_audit: delete rows older than 90 days, then VACUUM
Add --dry-run to only report. Databricks options come from the environment (DATABRICKS_*, BANK_TOOLS_CATALOG/OPS_SCHEMA).
"""
import argparse
import glob
import os
import re
from datetime import datetime, timedelta, timezone

from .config import ENVS, Config
from .repository import sql
from .state import StateStore

AUDIT_FILE = re.compile(r"tool_audit_(\d{8})\.jsonl$")


def purge_local_audit(directory, days, dry_run=False, today=None):
    today = today or datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=days)
    removed = []
    for path in sorted(glob.glob(os.path.join(directory, "tool_audit_*.jsonl"))):
        m = AUDIT_FILE.search(os.path.basename(path))
        if m and datetime.strptime(m.group(1), "%Y%m%d").date() < cutoff:
            removed.append(os.path.basename(path))
            if not dry_run:
                os.remove(path)
    return removed


def main(argv=None):
    ap = argparse.ArgumentParser(description="Purge bank tool audit records, state and mock rows.")
    ap.add_argument("--purge-env", choices=ENVS, help="delete ops.dispute_cases and ops.handoff_tickets rows of this env")
    ap.add_argument("--ops-audit", action="store_true", help="apply the 90-day retention to ops.tool_audit")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    cfg = Config.from_env()
    removed = purge_local_audit(cfg.path(cfg.audit_dir), cfg.audit_retention_days, args.dry_run)
    print(f"local audit: {len(removed)} file(s) older than {cfg.audit_retention_days} days"
          + (" would be removed" if args.dry_run else " removed"))
    if cfg.state_path and os.path.exists(cfg.path(cfg.state_path)) and not args.dry_run:
        n = StateStore(cfg.path(cfg.state_path)).purge(datetime.now())
        print(f"state store: {n} expired entr(ies) removed")
    if args.purge_env or args.ops_audit:
        from .repository.databricks import DatabricksRepository
        repo = DatabricksRepository.from_config(cfg)
        repo.ensure_ops_tables()
        statements = []
        if args.purge_env:
            statements += [(sql.PURGE_CASES_BY_ENV, {"env": args.purge_env}),
                           (sql.PURGE_TICKETS_BY_ENV, {"env": args.purge_env}),
                           (sql.PURGE_AUDIT_BY_ENV, {"env": args.purge_env})]
        if args.ops_audit:
            statements += [(sql.PURGE_AUDIT_OLD, None), (sql.VACUUM_AUDIT, None)]
        for statement, params in statements:
            label = statement.split(" WHERE ")[0]
            if args.dry_run:
                print("would run:", label)
                continue
            rows = repo.execute_statement(statement, params, deadline_s=300)
            affected = rows[0].get("num_affected_rows") if rows else None
            print(label + (f": {affected} row(s)" if affected is not None else ": done"))


if __name__ == "__main__":
    main()
