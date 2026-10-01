# Databricks notebook source
# MAGIC %md
# MAGIC # 02 · Silver update-correctness test (TEST FIXTURE)
# MAGIC The delivered data is a static snapshot: after the first load every incremental run is empty. This notebook proves the
# MAGIC incremental path on a small, clearly labeled **test fixture** instead, with the production code: `silver_lib.run_table`,
# MAGIC the real `sql/transactions.sql` and `contracts/transactions.json`. Only table names change.
# MAGIC
# MAGIC - **Fixture Bronze** (`workspace.ops.fixture_bronze_*`): a few hundred real Bronze transactions with fixed
# MAGIC   `_ingested_at` and `_source_file = fixture://...`, plus the customers and products they reference (only the columns
# MAGIC   `transactions.sql` reads: ids, country and dates, no personal data).
# MAGIC - **Fixture Silver**: `workspace.ops.fixture_silver_transactions` and `_quarantine`; run log in
# MAGIC   `workspace.ops.fixture_pipeline_runs` / `fixture_dq_results` (`ops_prefix = 'fixture_'`). Real Silver tables, the real
# MAGIC   run log and their watermarks are never touched; `run_table` refuses a non-`fixture_` target.
# MAGIC
# MAGIC | Batch | Bronze rows added | Expected Silver outcome |
# MAGIC |---|---|---|
# MAGIC | 1 (`full`) | 300 rows of 2026-06-01 + 1 re-delivered copy of one of them | 300 inserted, 1 removed by natural-key dedup, watermark = batch 1 |
# MAGIC | 2 (`incremental`) | 200 new rows of 2026-06-02; 1 **late** row (event 2024-01-15, new `_ingested_at`); 1 **updated** version of a batch-1 row (Pending → Approved); 1 identical **content duplicate** of a batch-1 row; 1 **uncastable amount**; 1 row carrying an **unexpected column** (`loyalty_points`, added by schema evolution) | 202 inserted, 1 updated, duplicate unchanged, 1 quarantined, extra column ignored, watermark = batch 2 |
# MAGIC | rerun / replay | nothing / whole history (`watermark_override = 1900-01-01`) | 0 inserted, 0 updated |
# MAGIC | 3 (`incremental`) | 1 valid + 1 uncastable row (50% bad > the 1% gate) | run fails, nothing written, watermark stays at batch 2 |
# MAGIC
# MAGIC The notebook fails if any assertion fails. Fixture tables are kept for inspection unless `cleanup = true`; every run
# MAGIC rebuilds them from scratch.

# COMMAND ----------

dbutils.widgets.dropdown("cleanup", "false", ["false", "true"], "Drop fixture tables at the end")
CLEANUP = dbutils.widgets.get("cleanup") == "true"

# COMMAND ----------

import json
import os
import sys
import uuid

sys.path.insert(0, os.getcwd())  # silver_lib.py, sql/ and contracts/ sit next to this notebook
import silver_lib as sl

spark.conf.set("spark.sql.session.timeZone", "UTC")

TABLE = "transactions"
BRONZE = "workspace.bronze"
FX = "workspace.ops.fixture_"
FX_TX, FX_CUST, FX_PROD = f"{FX}bronze_transactions", f"{FX}bronze_customers", f"{FX}bronze_products"
TARGET = f"{FX}silver_transactions"
CFG = {**sl.DEFAULTS, "ops_prefix": "fixture_"}  # run log: workspace.ops.fixture_pipeline_runs / fixture_dq_results
SOURCES = {"transactions": FX_TX, "customers": FX_CUST, "products": FX_PROD}
FIXTURE_TABLES = [FX_TX, FX_CUST, FX_PROD, TARGET, f"{TARGET}_quarantine",
                  sl.ops_table(CFG, "pipeline_runs"), sl.ops_table(CFG, "dq_results"), sl.ops_table(CFG, "freshness")]
assert all(t.startswith(FX) for t in FIXTURE_TABLES), "fixture writes are limited to workspace.ops.fixture_*"

T0, T1, T2, T3 = "2026-09-01 00:00:00", "2026-09-02 00:00:00", "2026-09-03 00:00:00", "2026-09-04 00:00:00"
LABEL = "TEST FIXTURE for src/silver/02_update_fixture_test.py (not production data)"
for t in FIXTURE_TABLES:
    spark.sql(f"DROP TABLE IF EXISTS {t}")
sl.ensure_ops_tables(spark, CFG)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Fixture Bronze
# MAGIC Real rows from four business days (`process_date`), numbered by `transaction_id` so the picks are deterministic.

# COMMAND ----------

spark.sql(f"""
CREATE OR REPLACE TEMP VIEW fixture_pool AS
SELECT *, row_number() OVER (PARTITION BY process_date ORDER BY transaction_id) AS _fx_rn
FROM {BRONZE}.transactions
WHERE process_date IN ('2026-06-01', '2026-06-02', '2026-06-03', '2024-01-15')""")
TX_COLS = spark.table(f"{BRONZE}.transactions").columns

base = spark.sql("SELECT transaction_id, transaction_status FROM fixture_pool "
                 "WHERE process_date = '2026-06-01' AND _fx_rn <= 300 ORDER BY _fx_rn").collect()
UPDATED_ID = next(r["transaction_id"] for r in base if r["transaction_status"] == "Pending")
others = [r["transaction_id"] for r in base if r["transaction_id"] != UPDATED_ID]
CROSS_DUP_ID, WITHIN_DUP_ID = others[0], others[1]
pick_id = lambda day, rn: spark.sql(f"SELECT transaction_id FROM fixture_pool "
                                    f"WHERE process_date = '{day}' AND _fx_rn = {rn}").first()["transaction_id"]
LATE_ID, BAD_ID, EVOLVED_ID = pick_id("2024-01-15", 1), pick_id("2026-06-03", 1), pick_id("2026-06-03", 2)
GATE_OK_ID, GATE_BAD_ID = pick_id("2026-06-03", 3), pick_id("2026-06-03", 4)


def pick(where, ingested_at, source_file, overrides=None, extra=None):
    """Bronze-shaped SELECT over fixture_pool: lineage replaced, some raw strings overridden, optional extra columns."""
    overrides, exprs = overrides or {}, []
    for c in TX_COLS:
        if c in overrides:
            exprs.append(f"{overrides[c]} AS `{c}`")
        elif c in ("_ingested_at", "_source_modified_at"):
            exprs.append(f"TIMESTAMP '{ingested_at}' AS `{c}`")
        elif c == "_source_file":
            exprs.append(f"'{source_file}' AS _source_file")
        else:
            exprs.append(f"`{c}`")
    exprs += [f"{v} AS `{k}`" for k, v in (extra or {}).items()]
    return f"SELECT {', '.join(exprs)} FROM fixture_pool WHERE {where}"


by_id = lambda i: f"transaction_id = '{i}'"
FILE = "fixture://transactions/batch={}/{}.csv"

# Dimensions: only the columns transactions.sql reads (ids, country, dates), for every customer and product in the pool.
for name, cols, key in ((FX_CUST, "customer_id, country, registration_date", "customer_id"),
                        (FX_PROD, "product_id, customer_id, opening_date", "product_id")):
    src = name.split("fixture_bronze_")[1]
    spark.sql(f"""
    CREATE OR REPLACE TABLE {name} COMMENT '{LABEL}' AS
    SELECT {cols}, CAST(NULL AS STRING) AS _rescued_data, 'fixture://{src}/{src}.csv' AS _source_file,
           TIMESTAMP '{T0}' AS _source_modified_at, TIMESTAMP '{T0}' AS _ingested_at
    FROM {BRONZE}.{src} WHERE {key} IN (SELECT {key} FROM fixture_pool)""")

# Batch 1: 300 rows + the same row re-delivered in a second file (content duplicate inside one batch).
batch1 = " UNION ALL ".join([
    pick("process_date = '2026-06-01' AND _fx_rn <= 300", T1, FILE.format(1, "part-000")),
    pick(by_id(WITHIN_DUP_ID), T1, FILE.format(1, "part-001-redelivered")),
])
spark.sql(f"CREATE OR REPLACE TABLE {FX_TX} COMMENT '{LABEL}' AS {batch1}")

# COMMAND ----------

outcomes = []


def expect(name, actual, expected):
    outcomes.append({"assertion": name, "expected": str(expected), "actual": str(actual), "ok": actual == expected})


def run(mode, override=None, run_tag="run"):
    return sl.run_table(spark, TABLE, mode, f"fixture-{run_tag}-{uuid.uuid4().hex[:8]}", cfg=CFG, sources=SOURCES,
                        target_table=TARGET, watermark_override=override)


def dq_of(r):
    return {d["check_name"]: d["failed_rows"] for d in r["dq"]}


def silver(where="true"):
    return spark.sql(f"SELECT * FROM {TARGET} WHERE {where}").collect()


def wm(ts):
    return f"{ts}.000000"


# COMMAND ----------

# MAGIC %md
# MAGIC ## Batch 1 · full build

# COMMAND ----------

r1 = run("full", run_tag="b1")
d1 = dq_of(r1)
expect("b1.status", r1["status"], "succeeded")
expect("b1.rows_source", r1["rows_source"], 301)
expect("b1.dedup:natural_key (re-delivered copy)", d1.get("dedup:natural_key"), 1)
expect("b1.rows_valid/quarantined/inserted", (r1["rows_valid"], r1["rows_quarantined"], r1["rows_inserted"]), (300, 0, 300))
expect("b1.watermark_in/out", (r1["watermark_in"], r1["watermark_out"]), (sl.FULL_WATERMARK, wm(T1)))
t = spark.sql(f"SELECT count(*) AS n, count(DISTINCT transaction_id) AS k FROM {TARGET}").first()
expect("b1.silver rows / distinct keys", (t["n"], t["k"]), (300, 300))
expect("b1.logged watermark", sl.last_watermark(spark, CFG, TABLE), wm(T1))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Batch 2 · incremental with every edge case
# MAGIC Appended with `mergeSchema`, as Auto Loader's `addNewColumns` does: `loyalty_points` becomes a new Bronze column.

# COMMAND ----------

f2 = FILE.format(2, "part-000")
null_extra = {"loyalty_points": "CAST(NULL AS STRING)"}
batch2 = " UNION ALL ".join([
    pick("process_date = '2026-06-02' AND _fx_rn <= 200", T2, f2, extra=null_extra),               # 200 new rows
    pick(by_id(LATE_ID), T2, FILE.format(2, "late-2024-01-15"), extra=null_extra),                 # late arrival
    pick(by_id(UPDATED_ID), T2, f2, {"transaction_status": "'Approved'", "response_code": "'00'"},
         extra=null_extra),                                                                         # updated version
    pick(by_id(CROSS_DUP_ID), T2, FILE.format(2, "part-001-redelivered"), extra=null_extra),       # identical copy
    pick(by_id(BAD_ID), T2, f2, {"amount": "'12,50 EUR'"}, extra=null_extra),                       # uncastable amount
    pick(by_id(EVOLVED_ID), T2, f2, extra={"loyalty_points": "'120'"}),                             # new column
])
spark.sql(batch2).write.mode("append").option("mergeSchema", "true").saveAsTable(FX_TX)

r2 = run("incremental", run_tag="b2")
d2 = dq_of(r2)
expect("b2.status", r2["status"], "succeeded")
expect("b2.watermark_in/out (automatic, from the fixture run log)", (r2["watermark_in"], r2["watermark_out"]), (wm(T1), wm(T2)))
expect("b2.rows_source (window only)", r2["rows_source"], 205)
expect("b2.dedup:natural_key", d2.get("dedup:natural_key"), 0)
expect("b2.rows_valid/quarantined", (r2["rows_valid"], r2["rows_quarantined"]), (204, 1))
expect("b2.inserted/updated (200 new + late + evolved; 1 update; duplicate unchanged)",
       (r2["rows_inserted"], r2["rows_updated"]), (202, 1))
expect("b2.cast:amount / amount_positive failures", (d2.get("cast:amount"), d2.get("amount_positive")), (1, 1))

t = spark.sql(f"SELECT count(*) AS n, count(DISTINCT transaction_id) AS k FROM {TARGET}").first()
expect("b2.silver rows / distinct keys", (t["n"], t["k"]), (502, 502))
u = silver(by_id(UPDATED_ID))
expect("b2.updated row: new status, batch-2 lineage",
       (len(u), u[0]["transaction_status"], u[0]["_run_id"] == r2["run_id"], str(u[0]["_ingested_at"])),
       (1, "Approved", True, T2))
dup = silver(by_id(CROSS_DUP_ID))
expect("b2.content duplicate: one row, batch-1 lineage kept",
       (len(dup), dup[0]["_run_id"] == r1["run_id"], dup[0]["_source_file"]), (1, True, FILE.format(1, "part-000")))
late = silver(by_id(LATE_ID))
expect("b2.late row inserted with its old event date (process_date 2024-01-15)",
       (len(late), str(late[0]["event_date"]) < "2024-01-17", late[0]["_run_id"] == r2["run_id"]), (1, True, True))
expect("b2.uncastable row not in Silver", len(silver(by_id(BAD_ID))), 0)
q = spark.sql(f"SELECT transaction_id, _dq_reasons, get_json_object(_raw_json, '$.amount') AS raw_amount "
              f"FROM {TARGET}_quarantine").collect()
expect("b2.quarantine: 1 row, reason, raw amount kept",
       (len(q), q[0]["transaction_id"] == BAD_ID, "amount_positive" in q[0]["_dq_reasons"], q[0]["raw_amount"]),
       (1, True, True, "12,50 EUR"))
expect("b2.evolved row inserted", len(silver(by_id(EVOLVED_ID))), 1)
expect("b2.schema evolution: new Bronze column present, Silver schema unchanged",
       ("loyalty_points" in spark.table(FX_TX).columns, "loyalty_points" in spark.table(TARGET).columns,
        [c for c in spark.table(TARGET).columns if c.startswith("_")]),
       (True, False, ["_dq_warnings", "_source_file", "_ingested_at", "_silver_processed_at", "_run_id"]))
expect("b2.logged watermark", sl.last_watermark(spark, CFG, TABLE), wm(T2))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Idempotence · rerun and full replay

# COMMAND ----------

r = run("incremental", run_tag="rerun")
expect("rerun.rows_source/inserted/updated", (r["rows_source"], r["rows_inserted"], r["rows_updated"]), (0, 0, 0))
expect("rerun.watermark unchanged", r["watermark_out"], wm(T2))

r = run("incremental", override="1900-01-01", run_tag="replay")
expect("replay.rows_source (whole fixture history)", r["rows_source"], 506)
expect("replay.dedup:natural_key (in-batch copy, cross-batch copy, superseded version)", dq_of(r).get("dedup:natural_key"), 3)
expect("replay.inserted/updated", (r["rows_inserted"], r["rows_updated"]), (0, 0))
t = spark.sql(f"SELECT count(*) AS n, count_if(_run_id = '{r['run_id']}') AS touched FROM {TARGET}").first()
expect("replay.silver rows / rows rewritten", (t["n"], t["touched"]), (502, 0))
expect("replay.quarantine is an append log (the bad row is logged again)", spark.table(f"{TARGET}_quarantine").count(), 2)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Batch 3 · quality gate
# MAGIC `transactions` allows at most 1% of rows to fail hard checks per run. Above that, nothing is written and the watermark
# MAGIC does not move, so the batch is retried after the data or the contract is fixed.

# COMMAND ----------

f3 = FILE.format(3, "part-000")
batch3 = " UNION ALL ".join([
    pick(by_id(GATE_OK_ID), T3, f3, extra=null_extra),
    pick(by_id(GATE_BAD_ID), T3, f3, {"amount": "'N/A'"}, extra=null_extra),
])
spark.sql(batch3).write.mode("append").option("mergeSchema", "true").saveAsTable(FX_TX)

r3 = run("incremental", run_tag="b3")
expect("b3.status / gate error", (r3["status"], (r3["error"] or "").startswith("QualityGateError")), ("failed", True))
expect("b3.nothing written (valid row not inserted either)",
       (spark.table(TARGET).count(), len(silver(by_id(GATE_OK_ID))), spark.table(f"{TARGET}_quarantine").count()),
       (502, 0, 2))
expect("b3.watermark stays at batch 2", sl.last_watermark(spark, CFG, TABLE), wm(T2))
runs = spark.sql(f"SELECT status, count(*) AS n FROM {sl.ops_table(CFG, 'pipeline_runs')} GROUP BY status").collect()
expect("fixture run log: 4 succeeded, 1 failed", sorted((x["status"], x["n"]) for x in runs), [("failed", 1), ("succeeded", 4)])
ids = [x["run_id"] for x in spark.table(sl.ops_table(CFG, "pipeline_runs")).select("run_id").collect()]
leaked = spark.sql("SELECT count(*) AS n FROM workspace.ops.pipeline_runs WHERE run_id IN ("
                   + ", ".join(sl.sql_str(i) for i in ids) + ")").first()["n"]
expect("real ops.pipeline_runs untouched by fixture runs", leaked, 0)

# COMMAND ----------

for t in FIXTURE_TABLES:
    if sl.table_exists(spark, t):
        spark.sql(f"COMMENT ON TABLE {t} IS '{LABEL}'")
if CLEANUP:
    for t in FIXTURE_TABLES:
        spark.sql(f"DROP TABLE IF EXISTS {t}")

display(spark.createDataFrame(outcomes))
bad = [o for o in outcomes if not o["ok"]]
if bad:
    raise AssertionError(f"{len(bad)} of {len(outcomes)} assertions failed: {bad}")
dbutils.notebook.exit(json.dumps({"passed": len(outcomes), "failed": 0, "kept_tables": not CLEANUP}))
