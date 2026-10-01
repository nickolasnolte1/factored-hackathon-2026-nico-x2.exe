# Databricks notebook source
# MAGIC %md
# MAGIC # 01 · Silver build (Bronze → typed, checked, merged)
# MAGIC Builds `workspace.silver.<table>` from `workspace.bronze.<table>`, one table per contract in `contracts/`.
# MAGIC
# MAGIC - **Contract-driven:** `sql/<table>.sql` types, normalizes and de-duplicates; `contracts/<table>.json` declares keys,
# MAGIC   column types and domains, foreign keys and checks. The shared logic lives in `silver_lib.py` (see `README.md`).
# MAGIC - **Quality gates:** rows failing a *hard* check go to `<table>_quarantine` with `_dq_reasons`; *warn* failures stay
# MAGIC   in Silver, listed in `_dq_warnings`. Every check is counted in `workspace.ops.dq_results`.
# MAGIC - **Incremental & idempotent:** `incremental` reads Bronze rows ingested after the table's watermark and MERGEs them by
# MAGIC   primary key (update only when the incoming `_ingested_at` is newer), so re-runs change nothing; `full` rebuilds.
# MAGIC - **Observability:** one row per table and run in `workspace.ops.pipeline_runs`; `workspace.ops.freshness` is refreshed
# MAGIC   at the end. Tables run in dependency order; a failing table does not stop the others, but the run fails at the end.

# COMMAND ----------

dbutils.widgets.text("tables", "all", "Comma-separated tables (or 'all')")
dbutils.widgets.dropdown("mode", "incremental", ["incremental", "full"], "Mode")
dbutils.widgets.text("watermark_override", "", "Incremental lower bound (yyyy-MM-dd HH:mm:ss); blank = automatic")
dbutils.widgets.text("run_id", "", "Run id (blank = generated; jobs pass {{job.run_id}})")

MODE = dbutils.widgets.get("mode")
WATERMARK_OVERRIDE = dbutils.widgets.get("watermark_override").strip() or None
REQUESTED = [t.strip() for t in dbutils.widgets.get("tables").split(",") if t.strip()]

# COMMAND ----------

import os
import sys

sys.path.insert(0, os.getcwd())  # silver_lib.py, sql/ and contracts/ sit next to this notebook
import silver_lib as sl

spark.conf.set("spark.sql.session.timeZone", "UTC")  # watermarks travel as UTC strings

raw_run_id = dbutils.widgets.get("run_id").strip()
RUN_ID = raw_run_id if raw_run_id and "{{" not in raw_run_id else sl.new_run_id()

broken = {}
contracts = sl.load_contracts(errors=broken)  # a broken contract fails its own table, not the whole run
order = sl.order_tables(contracts)
TABLES = order if REQUESTED == ["all"] else [t for t in order if t in REQUESTED]
unknown = sorted(broken) if REQUESTED == ["all"] else [t for t in REQUESTED if t not in contracts]
print(f"run_id={RUN_ID} mode={MODE} tables={TABLES} not_runnable={unknown}")

# COMMAND ----------

sl.ensure_ops_tables(spark)
results = []
for t in TABLES:
    r = sl.run_table(spark, t, MODE, RUN_ID, watermark_override=WATERMARK_OVERRIDE)
    results.append(r)
    print(f"{t}: {r['status']}" + (f" - {r['error']}" if r["error"] else ""))
for t in unknown:
    results.append({"run_id": RUN_ID, "table_name": t, "mode": MODE, "started_at": None, "finished_at": None,
                    "rows_source": 0, "rows_valid": 0, "rows_quarantined": 0, "rows_inserted": 0, "rows_updated": 0,
                    "status": "failed", "error": broken.get(t, f"no contract: contracts/{t}.json"), "dq": []})

try:
    sl.refresh_freshness(spark, contracts)
except Exception as e:  # freshness is a report; never fail the build for it
    print(f"freshness refresh skipped: {type(e).__name__}: {str(e)[:300]}")

# COMMAND ----------

print(sl.format_results(results))
display(spark.sql(f"SELECT * FROM workspace.ops.pipeline_runs WHERE run_id = '{RUN_ID}' ORDER BY started_at"))

# COMMAND ----------

# MAGIC %md
# MAGIC Checks that failed in this run (warn rows stay in Silver and are flagged; hard rows are in the quarantine table).

# COMMAND ----------

display(spark.sql(f"""
  SELECT table_name, check_name, severity, rule_ref, failed_rows, total_rows, pass_rate
  FROM workspace.ops.dq_results
  WHERE run_id = '{RUN_ID}' AND (passed = false OR passed IS NULL)
  ORDER BY table_name, severity, failed_rows DESC"""))
display(spark.table("workspace.ops.freshness").orderBy("table_name"))

# COMMAND ----------

failed = [r for r in results if r["status"] != "succeeded"]
if failed:
    raise RuntimeError("Silver build failed for: " + "; ".join(f"{r['table_name']}: {r['error']}" for r in failed))

import json

keys = ["table_name", "status", "rows_source", "rows_valid", "rows_quarantined", "rows_inserted", "rows_updated"]
dbutils.notebook.exit(json.dumps({"run_id": RUN_ID, "mode": MODE, "tables": [{k: r[k] for k in keys} for r in results]}))
