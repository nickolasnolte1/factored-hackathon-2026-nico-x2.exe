# Databricks notebook source
# MAGIC %md
# MAGIC # Silver framework self-test (fixtures)
# MAGIC Exercises quarantine, natural-key dedup, cast checks, MERGE insert/update and idempotency on `branches`
# MAGIC without touching real Silver tables or ops logs:
# MAGIC
# MAGIC - Bronze rows plus injected defects are served from **temp views** (nothing is written to Bronze).
# MAGIC - Output goes to `workspace.silver.fixture_branches` and `fixture_branches_quarantine`, dropped at the end.
# MAGIC - `run_table(..., log=False)` keeps `workspace.ops` clean. The notebook fails if any assertion fails.

# COMMAND ----------

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.getcwd()))  # tests/ -> silver/
import silver_lib as sl

spark.conf.set("spark.sql.session.timeZone", "UTC")

BRONZE = "workspace.bronze.branches"
TARGET = "workspace.silver.fixture_branches"
COLS = spark.table(BRONZE).columns
MAX_TS = f"(SELECT max(_ingested_at) FROM {BRONZE})"


def variant(offset, overrides, ingested_at=MAX_TS):
    """One real Bronze row (by position) with some raw string columns replaced."""
    exprs = [f"{overrides[c]} AS `{c}`" if c in overrides else
             f"{ingested_at} AS _ingested_at" if c == "_ingested_at" else f"`{c}`" for c in COLS]
    return f"SELECT {', '.join(exprs)} FROM (SELECT * FROM {BRONZE} ORDER BY branch_id LIMIT 1 OFFSET {offset})"


V1_EXTRA = [
    variant(0, {"branch_id": "CAST(NULL AS STRING)"}),                                          # hard: null PK
    variant(1, {"branch_id": "'SUC-TEST0001'", "branch_code": "'S9001'", "atm_count": "'2.5'"}),  # warn: cast
    variant(2, {}, ingested_at=f"{MAX_TS} - INTERVAL 1 DAY"),                                     # older duplicate
    variant(3, {"branch_id": "'SUC-TEST0002'", "branch_code": "'S9002'", "latitude": "'95.0'"}),  # warn: range
    variant(4, {"branch_id": "'SUC-TEST0003'", "branch_code": "'S9003'", "branch_opening_date": "'not-a-date'"}),
]
V2_EXTRA = [
    variant(1, {"branch_id": "'SUC-TEST0001'", "branch_code": "'S9001'", "atm_count": "'3'"},
            ingested_at=f"{MAX_TS} + INTERVAL 1 HOUR"),                                             # update
    variant(5, {"branch_id": "'SUC-TEST0004'", "branch_code": "'S9004'"},
            ingested_at=f"{MAX_TS} + INTERVAL 1 HOUR"),                                             # insert
]
spark.sql(f"CREATE OR REPLACE TEMP VIEW fixture_src_v1 AS SELECT * FROM {BRONZE} UNION ALL " + " UNION ALL ".join(V1_EXTRA))
spark.sql("CREATE OR REPLACE TEMP VIEW fixture_src_v2 AS SELECT * FROM fixture_src_v1 UNION ALL " + " UNION ALL ".join(V2_EXTRA))
N = spark.table(BRONZE).count()
BASE_WM = spark.sql(f"SELECT date_format(max(_ingested_at), '{sl.TS_FMT}') AS wm FROM {BRONZE}").first()["wm"]

# COMMAND ----------

outcomes = []


def expect(name, actual, expected):
    outcomes.append({"assertion": name, "expected": str(expected), "actual": str(actual), "ok": actual == expected})


def run(mode, view, override=None):
    r = sl.run_table(spark, "branches", mode, sl.new_run_id(), source_view=view, target_table=TARGET,
                     watermark_override=override, log=False)
    if r["status"] != "succeeded":
        raise RuntimeError(r["error"])
    return r, {d["check_name"]: d["failed_rows"] for d in r["dq"]}


try:
    # 1. Full build: 5 injected rows -> 1 removed by natural-key dedup, 1 quarantined, 3 kept with warnings.
    r, dq = run("full", "fixture_src_v1")
    expect("full.rows_source", r["rows_source"], N + 5)
    expect("full.rows_quarantined", r["rows_quarantined"], 1)
    expect("full.rows_valid", r["rows_valid"], N + 3)
    expect("full.rows_inserted", r["rows_inserted"], N + 3)
    expect("full.dedup:natural_key", dq["dedup:natural_key"], 1)
    expect("full.not_null:branch_id", dq["not_null:branch_id"], 1)
    expect("full.cast:atm_count", dq["cast:atm_count"], 1)
    expect("full.cast:branch_opening_date", dq["cast:branch_opening_date"], 1)
    expect("full.range:latitude_raw", dq["range:latitude_raw"], 1)
    q = spark.sql(f"SELECT count(*) AS n, count_if(array_contains(_dq_reasons, 'not_null:branch_id')) AS reason, "
                  f"count_if(_raw_json IS NOT NULL) AS raw FROM {TARGET}_quarantine").first()
    expect("quarantine.rows/reason/raw", (q["n"], q["reason"], q["raw"]), (1, 1, 1))
    w = spark.sql(f"SELECT _dq_warnings AS w, atm_count FROM {TARGET} WHERE branch_id = 'SUC-TEST0001'").first()
    expect("flagged row kept with cast warning", ("cast:atm_count" in w["w"], w["atm_count"]), (True, None))
    comment = [x for x in spark.sql(f"DESCRIBE TABLE {TARGET}").collect() if x["col_name"] == "branch_id"][0]["comment"]
    expect("column comment from contract", bool(comment), True)

    # 2. Incremental after the base watermark: 1 update (newer version of SUC-TEST0001) and 1 insert.
    r, _ = run("incremental", "fixture_src_v2", BASE_WM)
    expect("incr.rows_source", r["rows_source"], 2)
    expect("incr.inserted/updated", (r["rows_inserted"], r["rows_updated"]), (1, 1))
    w = spark.sql(f"SELECT array_contains(_dq_warnings, 'cast:atm_count') AS w, atm_count FROM {TARGET} "
                  f"WHERE branch_id = 'SUC-TEST0001'").first()
    expect("updated row repaired", (w["w"], w["atm_count"]), (False, 3))

    # 3. Same window again, then the whole history: MERGE must change nothing.
    r, _ = run("incremental", "fixture_src_v2", BASE_WM)
    expect("rerun.inserted/updated", (r["rows_inserted"], r["rows_updated"]), (0, 0))
    r, _ = run("incremental", "fixture_src_v2", "1900-01-01")
    expect("replay.inserted/updated", (r["rows_inserted"], r["rows_updated"]), (0, 0))
    t = spark.sql(f"SELECT count(*) AS n, count(DISTINCT branch_id) AS k FROM {TARGET}").first()
    expect("final rows / distinct keys", (t["n"], t["k"]), (N + 4, N + 4))
    q = spark.table(f"{TARGET}_quarantine").count()
    expect("quarantine is an append log: only the replay re-logs the null-key row", q, 2)

    # 4. Errors fail the table (status + message), they do not raise into the runner.
    r = sl.run_table(spark, "branches", "incremental", sl.new_run_id(), source_view="fixture_src_v2",
                     target_table=TARGET, watermark_override="not-a-date", log=False)
    expect("bad input -> failed status with error", (r["status"], "ContractError" in (r["error"] or "")), ("failed", True))
finally:
    spark.sql(f"DROP TABLE IF EXISTS {TARGET}")
    spark.sql(f"DROP TABLE IF EXISTS {TARGET}_quarantine")

# COMMAND ----------

display(spark.createDataFrame(outcomes))
bad = [o for o in outcomes if not o["ok"]]
if bad:
    raise AssertionError(f"{len(bad)} assertions failed: {bad}")
dbutils.notebook.exit(json.dumps({"passed": len(outcomes), "failed": 0}))
