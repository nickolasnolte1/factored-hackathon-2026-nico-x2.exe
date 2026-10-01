# Silver layer

Typed, cleaned and checked copies of the Bronze tables: `workspace.bronze.<table>` → `workspace.silver.<table>`.
Each table is defined by two files and built by one shared runner. Issue-to-rule mapping, freshness policy and results:
[`docs/04_silver_layer.md`](../../docs/04_silver_layer.md).

| Path | Purpose |
|---|---|
| `sql/<table>.sql` | One `SELECT` over Bronze: casts, trims and normalizes every column, derives columns, de-duplicates on the natural key, keeps lineage |
| `contracts/<table>.json` | Keys, column types and domains, foreign keys and named checks (format below) |
| `silver_lib.py` | Shared engine: render SQL, evaluate checks, quarantine, MERGE, FK checks, run logging, freshness. Also a no-Spark CLI |
| `01_build_silver.py` | Runner notebook (widgets `tables`, `mode`, `watermark_override`, `run_id`) |
| `02_update_fixture_test.py` | Update-correctness test on labeled `workspace.ops.fixture_*` tables: new, late, updated, duplicate, uncastable and schema-evolved rows, idempotent replay, quality gate |
| `tests/test_silver_framework.py` | Self-test on fixtures (quarantine, dedup, casts, MERGE insert/update, idempotency) |

Outputs:

| Table | Content |
|---|---|
| `workspace.silver.<table>` | Rows that passed every hard check, in contract column order, plus `_dq_warnings`, `_source_file`, `_ingested_at`, `_silver_processed_at`, `_run_id`. Column comments come from the contract |
| `workspace.silver.<table>_quarantine` | Rows that failed a hard check: the typed columns plus `_dq_reasons`, `_dq_warnings`, `_raw_json` (the Bronze row), `_run_id`, `_quarantined_at`. Rebuilt in full mode, appended in incremental mode |
| `workspace.ops.pipeline_runs` | One row per table and run: mode, watermarks, row counts (source, valid, quarantined, inserted, updated), status, error |
| `workspace.ops.dq_results` | One row per run, table and check: severity, `rule_ref`, failed and total rows, pass rate, passed (`NULL` = not evaluated) |
| `workspace.ops.freshness` | One row per contract table: rows, quarantine rows, max event date, max `_ingested_at`, last run and last success |

## How to run

```bash
DBX=databricks   # the CLI, with --profile factored
$DBX workspace import-dir src /Users/<you>/_staging/repo/src --overwrite
$DBX jobs submit --no-wait --json '{"run_name": "silver", "tasks": [{"task_key": "t", "notebook_task": {
  "notebook_path": "/Users/<you>/_staging/repo/src/silver/01_build_silver",
  "base_parameters": {"tables": "branches", "mode": "full", "run_id": "{{job.run_id}}"}}}]}'
```

- `tables`: comma list or `all` (every contract, in dependency order: parents referenced by `foreign_keys`/`depends_on` first, dimensions before facts).
- `mode=full`: reads every Bronze row and rebuilds the table and its quarantine (`CREATE OR REPLACE`), so SQL or contract changes reach every row.
- `mode=incremental` (default): reads Bronze rows with `_ingested_at` > the table's watermark and MERGEs them by primary key: new keys are inserted, existing keys are updated only when the incoming `_ingested_at` is newer and the content (contract columns and `_dq_warnings`) differs, so a re-delivered identical row keeps its first lineage. Re-running changes nothing. If the Silver table does not exist yet, it is built as in full mode. If the SQL output no longer matches the table schema, the table fails with a request to run `full`.
- Watermark = `watermark_out` of the table's last succeeded run in `ops.pipeline_runs` (the max `_ingested_at` it processed). The upper bound is pinned when the run starts, so rows landing mid-run wait for the next run. `watermark_override` replays a window (e.g. `1900-01-01` re-merges all of Bronze).
- Deletes are not propagated: Bronze is append-only, so a key dropped from a newer snapshot stays in Silver. A dimension that needs this would filter its SQL to the latest `_source_file`.
- A failing table is logged (`status = 'failed'`, `error`) and the others still run; the notebook raises at the end so the job fails. Serverless retries a failed task once, and the retry logs rows under the same `run_id` (the job run id); `started_at` tells them apart.
- Run Silver after `ingest_bronze`. The bundle job `build_silver` (`databricks.yml`) runs this notebook with the job parameters `tables` and `mode` (`databricks bundle run build_silver --params tables=all,mode=full`), then `02_update_fixture_test.py`.

## Adding a table

1. Write `sql/<table>.sql` and `contracts/<table>.json` (conventions below; `branches` is the reference).
2. Preview read-only on the SQL warehouse (no job needed), from `src/silver/`:
   ```bash
   python silver_lib.py validate                                     # contracts parse, build order
   python silver_lib.py sql <table>     > /tmp/q.sql && python dbsql.py -f /tmp/q.sql --max-rows 5
   python silver_lib.py describe <table> > /tmp/q.sql && python dbsql.py -f /tmp/q.sql   # output columns and types
   python silver_lib.py checks <table>  > /tmp/q.sql && python dbsql.py -f /tmp/q.sql --max-rows 500
   ```
   `checks` returns one row per check with its failed rows (plus `__rows_source`, `__rows_output`, `__rows_quarantined`
   and a `type:<column>` assert per contract column, 1 = the SQL type differs from the contract).
3. Run the notebook with `tables=<table> mode=full`, then `mode=incremental` (must insert and update 0 rows).
4. After the parents exist, `python silver_lib.py fk <table>` shows orphan counts.

## SQL conventions

```sql
WITH src AS (
  SELECT *
  FROM {catalog}.{bronze}.<table>
  WHERE _ingested_at > TIMESTAMP '{watermark}'                       -- required: incremental window
  QUALIFY row_number() OVER (PARTITION BY <natural key, as raw expressions>
                             ORDER BY _ingested_at DESC, _source_file DESC) = 1
)
SELECT
  upper(trim(<table>_id)) AS <table>_id,
  ...,
  _source_file, _ingested_at,                                         -- required lineage
  struct(src.*) AS _bronze                                            -- optional, recommended
FROM src
```

- Placeholders `{catalog}`, `{bronze}`, `{silver}`, `{watermark}` are replaced verbatim (not `str.format`), so regex braces such as `{5}` are safe. Reference Bronze exactly as `{catalog}.{bronze}.<source>`; join other Silver tables as `{catalog}.{silver}.<table>` and list them in `depends_on`. No trailing `;` needed.
- The output must have exactly the contract columns (any order) plus `_source_file`, `_ingested_at` and optionally `_bronze`. Names starting with `_` are reserved.
- `_bronze` (the raw row) enables the automatic `cast:<column>` checks and is stored as `_raw_json` in quarantine. It is never written to Silver.
- Typing idioms (Bronze values are strings; `try_cast` is strict):
  - integers delivered as `'223.0'`: `try_cast(regexp_replace(trim(x), '[.]0+$', '') AS INT)`; `'2.5'` stays NULL and is counted.
  - booleans `'True'`/`'False'`: `try_cast(trim(x) AS BOOLEAN)`.
  - timestamps `yyyy-MM-dd HH:mm:ss`: `try_cast(trim(x) AS TIMESTAMP)`; dates: `try_cast(trim(x) AS DATE)`.
  - money: `try_cast(trim(x) AS DECIMAL(18,2))`.
  - accents (issue 13): `translate(lower(trim(x)), 'áéíóú', 'aeiou')` before comparing.
- FIX rules keep the raw value in a `<column>_raw` column; QUARANTINE-value rules null the column and keep `<column>_raw`; FLAG rules add a boolean (see `docs/02_eda_workflow_selection.md`, section 6).

## Contract format

```json
{
  "table": "branches",                    // must equal the file name
  "source": "branches",                   // Bronze table (default: table)
  "kind": "dimension",                    // dimension | fact (dimensions build first)
  "description": "...",                   // table comment
  "primary_key": ["branch_id"],           // MERGE key; null or duplicate -> quarantine
  "natural_key": ["branch_id"],           // business key used by the SQL dedup (default: primary_key)
  "event_time": null,                     // date/timestamp column for freshness, or null
  "load": {
    "cadence": "snapshot",                // informative: snapshot | daily | ...
    "partition_by": [], "cluster_by": [], // optional layout (one or the other)
    "max_quarantine_rate": 0.05           // optional gate: fail without writing above this share
  },
  "depends_on": [],                       // optional extra Silver tables the SQL reads
  "columns": {
    "atm_count": {
      "type": "int",                      // Spark SQL type: string, boolean, int, bigint, double, decimal(p,s), date, timestamp, array<string>
      "nullable": false,                  // false -> not_null check
      "description": "...",               // required; becomes the column comment
      "source": "atm_count",              // optional Bronze column; enables cast:<column> for non-string types
      "allowed": ["..."],                 // optional domain (warn)
      "min": 0, "max": 50,                // optional inclusive range (warn); dates as "yyyy-MM-dd"
      "rule_ref": 22                      // optional issue number from the EDA report, section 6
    }
  },
  "foreign_keys": [
    {"column": "registration_branch_id", "ref_table": "branches", "ref_column": "branch_id",
     "severity": "flag", "rule_ref": 14}  // hard = must hold; flag = known broken. Both are counted, never dropped
  ],
  "checks": [
    {"name": "has_atms_matches_count", "severity": "warn", "rule_ref": null,
     "expr": "has_atms IS NULL OR atm_count IS NULL OR has_atms = (atm_count > 0)",
     "description": "..."}
  ]
}
```

(Comments are for illustration; the files are plain JSON.)

### Checks

A check's `expr` is a SQL boolean over the output columns (and `_bronze`). The row **passes when it is TRUE**; FALSE or
NULL fail, so write `col IS NULL OR ...` when nulls are acceptable.

| Check name | Generated from | Severity |
|---|---|---|
| `not_null:<col>` | `nullable: false` | hard for primary-key and event-time columns, else warn |
| `cast:<col>` | `source` on a non-string column: raw present but typed value NULL | hard for key columns, else warn |
| `allowed:<col>` | `allowed` | warn |
| `range:<col>` | `min` / `max` | warn |
| `<name>` | `checks[]` | as declared |
| `no_rescued_data` | `_bronze._rescued_data` is NULL | warn |
| `unique:primary_key` | duplicate primary key after the SQL dedup (older copies) | hard |
| `dedup:natural_key` | Bronze rows in the window minus SQL output rows (expected 0) | warn, table level |
| `fk:<col>-><table>.<col>` | `foreign_keys`, after the write, on the whole table; `passed` NULL while the parent is not built | as declared |
| `unique:natural_key` | natural key differs from the primary key: duplicates across the whole table | warn, table level |

Hard failures go to quarantine with their names in `_dq_reasons`; warn failures stay in Silver with their names in
`_dq_warnings`. Everything is counted in `ops.dq_results` with `total_rows` = rows returned by the SQL in the window.

## Library API (`silver_lib.py`)

| Function | What it does |
|---|---|
| `load_contract(table)` / `load_contracts(errors=None)` | Read and validate contracts (defaults filled); with `errors`, broken contracts are reported instead of raised |
| `validate_contract(c)` | List of problems in a contract (no Spark) |
| `order_tables(contracts)` | Dependency order (FK parents and `depends_on` first, dimensions before facts) |
| `render_sql(table, watermark, cfg, source_view=None, sources=None)` | SQL file with placeholders filled; `source_view` / `sources` swap Bronze tables for views or fixture tables (tests) |
| `build_checks(c, has_raw)` | Ordered row-level checks (generated + declared) |
| `checked_sql(...)`, `check_sql(...)` | CTE with `_dq_reasons` / `_dq_warnings`; one-query check counts in long format |
| `valid_sql(...)`, `quarantine_sql(...)`, `fk_sql(...)` | SELECTs for the Silver rows, the quarantined rows and FK orphans |
| `run_table(spark, table, mode, run_id, watermark_override=None, source_view=None, target_table=None, log=True, sources=None)` | Builds one table end to end and returns its `pipeline_runs` row plus `dq`. Tests pass `sources` (Bronze table → fixture table), a `fixture_*` target and `cfg['ops_prefix'] = 'fixture_'` |
| `post_checks(spark, c, target, cfg)` | FK orphans and natural-key uniqueness on the written table |
| `last_watermark(spark, cfg, table)` | Watermark of the last succeeded run |
| `ensure_ops_tables(spark)`, `write_logs(...)`, `refresh_freshness(spark, contracts)`, `ops_table(cfg, name)` | Ops tables: create, append run and DQ rows, upsert freshness; names carry `cfg['ops_prefix']` |
| `format_results(results)` | Plain-text summary for the notebook |

`cfg` defaults to `{"catalog": "workspace", "bronze": "bronze", "silver": "silver", "ops": "ops", "ops_prefix": ""}`.

## Table notes

### branches

350 rows, 0 quarantined. Findings not in the EDA issue list, handled here:

- **Placeholder coordinates:** every branch in 7 of 16 cities (167 of 350) sits within 0.1° of (0, 0). `latitude`/`longitude` are NULL when the raw point falls outside the branch's country, the raw values are kept, and `has_valid_coordinates` flags them (check `coordinates_in_country`).
- **Lost leading zeros:** the 39 Colombian postal codes of Medellín and Barranquilla have 5 digits; `postal_code` is re-padded to 6 (`postal_code_raw` keeps the original).
- **Wrong dialing prefix:** all 175 Mexican branches carry `+54` (Argentina); flagged by `phone_prefix_matches_country`, not rewritten.
- **Names are not unique:** 175 names for 350 branches, repeated within a city; look branches up by `branch_id` or `branch_code`.
