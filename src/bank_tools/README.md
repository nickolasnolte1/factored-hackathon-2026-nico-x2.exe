# Bank tools (mock banking service)

The tool service the dispute-intake agent calls: 14 tools behind one pipeline that enforces identity, ownership, the synthetic policy, confirmation before writes and a redacted audit trail. The behavior is specified in [CONTRACT.md](CONTRACT.md); the tool definitions the model reads are in [tool_schemas.json](tool_schemas.json). Data is synthetic: no money moves, and cases and tickets are mock records.

## Run it

```bash
# 1. Local snapshot of Gold (git-ignored data/bank_tools/), read-only SELECTs on the SQL warehouse.
#    `panel` = the 720 e2e panel customers (checked field by field against panel.jsonl); sample:N adds demo customers.
python -m src.bank_tools.snapshot --source gold --customers panel,sample:50 \
    --warehouse-id $DATABRICKS_WAREHOUSE_ID --profile factored
python -m src.bank_tools.snapshot --source panel        # offline rebuild from panel.jsonl (after one Gold export)

# 2. Scripted happy path: prints every tool call and the envelope the model receives (no model, no personal data)
python -m src.bank_tools.demo [--customer CLI-XXXXXXXXXXXX] [--language pt] [--now 2026-06-19T09:00:00]

# 3. Scenario replay (no model): drives the 280 e2e scenarios through the tools with a scripted oracle and checks
#    outcome, must_not constraints, policy_trace, policy parity and output hygiene; exit code 1 on any failure
python -m src.bank_tools.replay [--only <category|subtype|scenario_id>] [-v] [--json data/bank_tools/replay_report.json]

# 4. Acceptance tests (contract section 12, run through a contract-based harness) and security tests; the live Databricks tests are opt-in
python -m pytest tests/bank_tools -q
BANK_TOOLS_TEST_DATABRICKS=1 DATABRICKS_CONFIG_PROFILE=factored DATABRICKS_WAREHOUSE_ID=<id> python -m pytest tests/bank_tools -q

# 5. Retention (section 7 of the contract)
python -m src.bank_tools.retention [--purge-env demo] [--ops-audit] [--dry-run]
```

In code:

```python
from src.bank_tools import build_service, ToolContext

service = build_service()                                   # BANK_TOOLS_* from the environment
ctx = ToolContext(conversation_id="c-1", turn_index=1, trace_id="0" * 32)   # supplied by the runtime
result = service.call_tool("get_policy_info", {"topic": "dispute_window", "language": "es"}, None, ctx)
result.for_model()          # envelope for the model; result.runtime holds the session token after verify_otp
service.model_tools()       # tool definitions to offer the model
```

Evaluation harness (contract section 10): `BankService(LocalRepository(snapshot), FixedClock(now), ListAuditSink(), FaultInjector(tool_faults), Config(env="eval").checked())` and `service.identity.issue_test_session(customer_id, authenticated_at=now)`.

Databricks mode: `BANK_TOOLS_REPOSITORY=databricks`, `DATABRICKS_WAREHOUSE_ID`, and `DATABRICKS_HOST`/`DATABRICKS_TOKEN` or `DATABRICKS_CONFIG_PROFILE` (+ `DATABRICKS_CLI`). Reads come from `workspace.gold`; cases, tickets and the audit go to `workspace.ops.dispute_cases`, `ops.handoff_tickets` and `ops.tool_audit`, created on first use from [`sql/ops_tables.sql`](sql/ops_tables.sql). Warm the warehouse first: an attempt has an 8-second deadline. Both repositories have `store_rows(table)` for the agent console; on Databricks it returns only the configured env's rows.

Live demo: `build_service(demo_config())` uses env `demo` with higher limits (50 sign-in challenges per document per hour, 400 calls per session per 5 minutes), so a long demo never hits `RATE_LIMITED`. It needs real keys, like any `demo` config.

Configuration and its DEV ONLY defaults: contract section 11 and [`.env.example`](../../.env.example).

## Modules

| Module | Role |
|---|---|
| `service.py` | `BankService.call_tool` pipeline and the 14 handlers; `ToolContext`, `ToolResult` |
| `identity.py` | Document + OTP challenges, `TestOutbox`, HMAC-signed session tokens, test session issuer |
| `repository/` | `Repository` interface, `GuardedRepository` (retries, fault hook, row validation), `LocalRepository` (SQLite), `DatabricksRepository` (Statement Execution API), every SQL statement in `sql.py` |
| `schemas.py`, `errors.py` | Argument normalization and validation, output allow-list check, typed errors |
| `amounts.py` | Amounts the customer wrote as text ("109.686", "$1'985.843"), read the same way for every country |
| `redaction.py` | Untrusted-text wrapper, PII scrubber, audit argument redaction |
| `state.py` | Challenges, sessions, drafts, counters, idempotency, rate limits, tool-call index |
| `faults.py`, `clock.py`, `ids.py`, `config.py` | Fault injection (test/eval only), clocks and sleepers, id factory, environment |
| `audit.py` | JSONL, list, Databricks and tee sinks |
| `policy_snippets.json` | ES/PT policy text with placeholders filled from the policy file |
| `snapshot.py`, `demo.py`, `replay.py`, `retention.py` | The commands above |

## Checks run so far

- **Acceptance tests** (`tests/bank_tools/` except `test_security_redteam.py`, written from the contract and run through a contract-based harness): 266 of the original 267 tests, plus 90 tests added on 2026-10-03 with the contract changes of that day (written with the implementation in view; see [report 05, section 20](../../docs/05_gold_and_bank_tools.md)). The 353 offline ones pass; the 3 live Databricks tests (decoy challenge for an unknown document, Gold reads, identical outputs from the local snapshot and from Gold for 4 read tools on 5 panel customers) were last run before the `card_block_request` handoff reason was added, and passed then.
- **Security review** (`tests/bank_tools/test_security_redteam.py`, an internal review run in the same build session as the service): 12 findings (1 high, 5 medium, 6 low), all fixed, each covered by a test that passes with the fix (the review notes say each failed before its fix; the pre-fix runs are not kept in the repository). The suite now has 397 tests; the 394 offline ones pass. Attacks, fixes and residual risks are in [report 05, section 24](../../docs/05_gold_and_bank_tools.md).
- **Scenario replay** (`replay.py`): 280 of 280 e2e scenarios pass, with 6,423 tool-level checks and 0 failures. Every expected outcome, handoff reason, transaction and case field is reproduced. Audit attempt counts match each scenario's `policy_trace`. Results per category are in [report 05](../../docs/05_gold_and_bank_tools.md).
- **Repository parity.** In the live test, 4 read tools (overview, products, recent movements, candidate search) return identical model envelopes from the local snapshot and from Databricks for 5 panel customers. A case and a ticket were written to `workspace.ops` with read-back, then purged.
- **Reproducible snapshot.** Re-exporting the panel snapshot from Gold gives the same per-table sha256 checksums.
