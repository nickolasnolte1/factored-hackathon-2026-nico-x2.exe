# Databricks notebook source
# MAGIC %md
# MAGIC # 01 · Gold build (Silver → agent-ready, privacy-minimized)
# MAGIC Builds `workspace.gold.<table>` from `workspace.silver`, one table per entry in `gold_tables.json`. These are the
# MAGIC only tables the bank tools read.
# MAGIC
# MAGIC - **Spec-driven:** `sql/<table>.sql` is one SELECT over Silver; `gold_tables.json` declares column types and
# MAGIC   descriptions (written as column comments), keys, clustering and row reconciliation. The query output must match
# MAGIC   the spec exactly, so no undeclared column can reach Gold.
# MAGIC - **Policy-rendered:** eligibility, handoff threshold, restricted statuses, card types and decline codes are filled
# MAGIC   from `src/policy/dispute_policy.json`; a sample of the flags is re-checked with `dispute_policy.py`.
# MAGIC - **Consistent snapshot:** every Silver source is pinned (`VERSION AS OF`) at the start of the run; the versions
# MAGIC   are stored as table properties.
# MAGIC - **Privacy:** a query with a personal-data column is never written; a table whose values look like personal data
# MAGIC   is dropped. The whole `gold` schema is audited at the end.
# MAGIC - **Observability:** one row per table in `workspace.ops.pipeline_runs` (`table_name = gold.<table>`), every check
# MAGIC   in `workspace.ops.dq_results`. Hard failures fail the run; warn failures are reported.

# COMMAND ----------

dbutils.widgets.text("tables", "all", "Comma-separated Gold tables (or 'all')")
dbutils.widgets.text("run_id", "", "Run id (blank = generated; jobs pass {{job.run_id}})")

REQUESTED = [t.strip() for t in dbutils.widgets.get("tables").split(",") if t.strip()]

# COMMAND ----------

import json
import os
import sys

sys.path.insert(0, os.getcwd())                                            # gold_lib.py, sql/, gold_tables.json
sys.path.insert(0, os.path.join(os.path.dirname(os.getcwd()), "silver"))   # silver_lib.py: run log helpers
import gold_lib as gl
import silver_lib as sl
from pyspark.sql import functions as F

spark.conf.set("spark.sql.session.timeZone", "UTC")

raw_run_id = dbutils.widgets.get("run_id").strip()
RUN_ID = raw_run_id if raw_run_id and "{{" not in raw_run_id else sl.new_run_id()
OPS = dict(sl.DEFAULTS)
SPEC = gl.load_spec()
POLICY = gl.dp.load_policy()
problems = gl.validate(SPEC)
if problems:
    raise gl.GoldSpecError("; ".join(problems))

ORDER = SPEC["build_order"]
TABLES = ORDER if REQUESTED == ["all"] else [t for t in ORDER if t in REQUESTED]
unknown = [t for t in REQUESTED if t != "all" and t not in ORDER]

# Pin one Silver version per source for the whole run.
needed = sorted({s for t in TABLES for s in SPEC["tables"][t]["sources"]})
VERSIONS = {s: int(spark.sql(f"DESCRIBE HISTORY {SPEC['catalog']}.silver.{s} LIMIT 1").collect()[0]["version"])
            for s in needed}
SOURCES = gl.source_refs(SPEC, VERSIONS)
print(f"run_id={RUN_ID} tables={TABLES} unknown={unknown}")
print("silver versions:", VERSIONS, "| policy:", POLICY["policy_id"], POLICY["version"])

# COMMAND ----------

def describe_comments(name):
    """Column -> comment of a written table."""
    out = {}
    for r in spark.sql(f"DESCRIBE TABLE {name}").collect():
        if not r["col_name"] or r["col_name"].startswith("#"):
            break
        out[r["col_name"]] = r["comment"]
    return out


def reference_checks(table, name):
    """Gold values re-derived with the policy's reference implementation (dispute_policy.py)."""
    if table == "customer_transactions":
        cols = ["transaction_id", "customer_id", "transaction_status", "transaction_type", "amount_usd",
                "dispute_eligible_unrecognized", "dispute_eligible_incorrect", "above_handoff_threshold"]
        rows = [r.asDict() for r in spark.table(name).where("abs(hash(transaction_id)) % 1000 = 7").select(*cols).collect()]
        bad = gl.transaction_flag_mismatches(rows, POLICY)
        return [sl._dq("policy:flags_match_reference_sample", "hard", "policy", len(bad), len(rows))]
    if table == "decline_codes":
        rows = [r.asDict() for r in spark.table(name).collect()]
        bad = gl.decline_mismatches(rows, POLICY)
        return [sl._dq("policy:decline_matches_reference", "hard", "policy.decline_codes", len(bad),
                       2 * sum(1 for r in rows if r["in_policy"]))]
    return []


def build_table(table):
    ts = SPEC["tables"][table]
    name = gl.target(SPEC, table)
    res = {"run_id": RUN_ID, "table_name": f"gold.{table}", "mode": "full", "started_at": sl._now(), "finished_at": None,
           "watermark_in": None, "watermark_out": None, "rows_source": 0, "rows_valid": 0, "rows_quarantined": 0,
           "rows_inserted": 0, "rows_updated": 0, "status": "failed", "error": None}
    dq = []
    try:
        df = spark.sql(gl.render_sql(table, SOURCES, POLICY))
        gl.assert_schema(table, [(f.name, f.dataType.simpleString()) for f in df.schema.fields], SPEC)
        bad_cols = gl.pii_columns(df.columns, SPEC)
        dq.append(sl._dq("privacy:no_pii_columns", "hard", "privacy", len(bad_cols), len(df.columns)))
        if bad_cols:
            raise gl.PrivacyError(f"personal-data columns {bad_cols} in sql/{table}.sql; nothing written")

        desc = {n: c["description"] for n, c in ts["columns"].items()}
        view = f"_gold_{table}"
        df.select([F.col(f"`{n}`").alias(n, metadata={"comment": desc[n]}) for n in df.columns]).createOrReplaceTempView(view)
        layout = f"CLUSTER BY ({', '.join(ts['cluster_by'])}) " if ts.get("cluster_by") else ""
        props = {
            "gold.run_id": RUN_ID,
            "gold.spec_version": SPEC["version"],
            "gold.policy": f"{POLICY['policy_id']}@{POLICY['version']}",
            "gold.silver_sources": ",".join(f"{s}@v{VERSIONS[s]}" for s in ts["sources"]),
        }
        prop_sql = ", ".join(f"{sl.sql_str(k)} = {sl.sql_str(v)}" for k, v in props.items())
        spark.sql(f"CREATE OR REPLACE TABLE {name} {layout}COMMENT {sl.sql_str(ts['description'])} "
                  f"TBLPROPERTIES ({prop_sql}) AS SELECT * FROM {view}")
        n = spark.table(name).count()
        res.update(rows_valid=n, rows_inserted=n)

        comments = describe_comments(name)
        missing = [c for c in ts["columns"] if not comments.get(c)]
        dq.append(sl._dq("comments:all_columns", "hard", "gold", len(missing), len(ts["columns"])))
        for chk in gl.check_queries(table, SOURCES, SPEC, POLICY):
            try:
                r = spark.sql(chk["sql"]).collect()[0]
                failed, total = int(r["failed_rows"]), int(r["total_rows"])
            except Exception as e:  # not evaluated (e.g. a referenced Gold table is missing): never a pass
                print(f"{table}: check {chk['name']} not evaluated: {type(e).__name__}: {str(e)[:300]}")
                failed, total = None, None
            dq.append(sl._dq(chk["name"], chk["severity"], chk["rule_ref"], failed, total))
            if chk["name"] == "reconcile:rows" and total is not None:
                res["rows_source"] = total
        dq += reference_checks(table, name)

        hard = [d for d in dq if d["severity"] == "hard" and d["passed"] is not True]
        if any(d["check_name"].startswith("privacy:") for d in hard):
            spark.sql(f"DROP TABLE IF EXISTS {name}")
            raise gl.PrivacyError(f"{name} dropped: " + ", ".join(d["check_name"] for d in hard))
        if hard:
            raise RuntimeError("hard checks failed: " + ", ".join(f"{d['check_name']} ({d['failed_rows']})" for d in hard))
        res["status"] = "succeeded"
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"[:4000]
    res["finished_at"] = sl._now()
    sl.write_logs(spark, OPS, res, dq)
    res["dq"] = dq
    return res

# COMMAND ----------

sl.ensure_ops_tables(spark)
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {SPEC['catalog']}.{SPEC['schema']}")
results = []
for t in TABLES:
    r = build_table(t)
    results.append(r)
    print(f"{t}: {r['status']} rows={r['rows_valid']}" + (f" - {r['error']}" if r["error"] else ""))

# COMMAND ----------

# MAGIC %md
# MAGIC **Privacy audit of the whole `gold` schema** (tables built here or by anyone else): no column may look like
# MAGIC personal data. One `privacy:schema_audit` row per table in `ops.dq_results`.

# COMMAND ----------

audit = []
for row in spark.sql(f"SHOW TABLES IN {SPEC['catalog']}.{SPEC['schema']}").collect():
    if row["isTemporary"]:
        continue
    cols = spark.table(f"{SPEC['catalog']}.{SPEC['schema']}.{row['tableName']}").columns
    bad = gl.pii_columns(cols, SPEC)
    audit.append((RUN_ID, f"gold.{row['tableName']}", "privacy:schema_audit", "hard", "privacy", len(bad), len(cols),
                  1.0 - len(bad) / len(cols) if cols else 1.0, not bad, sl._now()))
    if bad:
        print(f"PII-like columns in gold.{row['tableName']}: {bad}")
if audit:
    dq_schema = ("run_id string, table_name string, check_name string, severity string, rule_ref string, "
                 "failed_rows bigint, total_rows bigint, pass_rate double, passed boolean, evaluated_at timestamp")
    audit_df = spark.createDataFrame(audit, dq_schema)
    sl._retry(lambda: audit_df.write.mode("append").saveAsTable(sl.ops_table(OPS, "dq_results")))
audit_failed = [a[1] for a in audit if not a[8]]

# COMMAND ----------

print(sl.format_results(results))
display(spark.sql(f"""
  SELECT table_name, check_name, severity, rule_ref, failed_rows, total_rows, pass_rate, passed
  FROM {sl.ops_table(OPS, 'dq_results')}
  WHERE run_id = '{RUN_ID}' AND table_name LIKE 'gold.%'
  ORDER BY table_name, severity, check_name"""))

# COMMAND ----------

failed = [r for r in results if r["status"] != "succeeded"]
if failed or unknown or audit_failed:
    raise RuntimeError("Gold build failed: " + "; ".join(
        [f"{r['table_name']}: {r['error']}" for r in failed] + [f"unknown table {t}" for t in unknown] +
        [f"PII-like columns in {t}" for t in audit_failed]))

dbutils.notebook.exit(json.dumps({
    "run_id": RUN_ID, "silver_versions": VERSIONS, "policy": f"{POLICY['policy_id']}@{POLICY['version']}",
    "tables": [{"table": r["table_name"], "status": r["status"], "rows": r["rows_valid"], "rows_source": r["rows_source"],
                "warn_failed": [d["check_name"] for d in r["dq"] if d["severity"] == "warn" and d["passed"] is False]}
               for r in results]}))
