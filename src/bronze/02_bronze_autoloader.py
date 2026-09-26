# Databricks notebook source
# MAGIC %md
# MAGIC # 02 · Bronze ingestion (Auto Loader → Delta)
# MAGIC Loads raw CSV files from the landing volume into `workspace.bronze.<table>` Delta tables.
# MAGIC
# MAGIC - **Incremental & idempotent:** Auto Loader tracks processed files in a checkpoint, so re-runs only ingest
# MAGIC   new or late-arriving partitions (`trigger(availableNow=True)` = batch semantics, streaming bookkeeping).
# MAGIC - **Raw fidelity:** every column is read as string; typing and cleaning happen in Silver.
# MAGIC - **Schema evolution:** new columns are added (`addNewColumns`); unexpected values land in `_rescued_data`.
# MAGIC - **Lineage:** each row keeps its source file, file modification time and ingestion timestamp.

# COMMAND ----------

dbutils.widgets.text("tables", "all", "Comma-separated tables (or 'all')")
dbutils.widgets.text("source_prefix", "data", "Landing sub-folder: data | data_backup_20260831")

SOURCE_PREFIX = dbutils.widgets.get("source_prefix").strip("/")
LANDING = f"/Volumes/workspace/bronze/landing/{SOURCE_PREFIX}"
STATE = "/Volumes/workspace/bronze/landing/_autoloader"
TABLE_SUFFIX = "" if SOURCE_PREFIX == "data" else "_backup"

FACT_TABLES = ["transactions", "call_center_interactions", "call_transcripts", "satisfaction_surveys",
               "complaints", "campaign_sends", "digital_events"]
DIM_TABLES = ["branches", "customers", "products", "service_agents", "marketing_campaigns", "daily_exchange_rates"]

requested = [t.strip() for t in dbutils.widgets.get("tables").split(",") if t.strip()]
TABLES = FACT_TABLES + DIM_TABLES if requested == ["all"] else requested

# COMMAND ----------

from pyspark.sql import functions as F


def ingest(table):
    is_fact = table in FACT_TABLES
    path = f"{LANDING}/{table}/" if is_fact else LANDING
    target = f"workspace.bronze.{table}{TABLE_SUFFIX}"
    checkpoint = f"{STATE}/{SOURCE_PREFIX}/{table}"

    reader = (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "csv")
        .option("cloudFiles.schemaLocation", f"{checkpoint}/schema")
        .option("cloudFiles.inferColumnTypes", "false")
        .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
        .option("header", "true")
        .option("multiLine", "true")  # transcripts and free-text fields contain line breaks
        .option("escape", '"')
    )
    if is_fact:
        reader = reader.option("recursiveFileLookup", "true")
    else:
        reader = reader.option("pathGlobFilter", f"{table}.csv")

    df = (
        reader.load(path)
        .withColumn("_source_file", F.col("_metadata.file_path"))
        .withColumn("_source_modified_at", F.col("_metadata.file_modification_time"))
        .withColumn("_ingested_at", F.current_timestamp())
    )
    (
        df.writeStream.option("checkpointLocation", checkpoint)
        .option("mergeSchema", "true")
        .trigger(availableNow=True)
        .toTable(target)
        .awaitTermination()
    )
    return target, spark.table(target).count()

# COMMAND ----------

results = []
for t in TABLES:
    try:
        results.append((*ingest(t), "ok"))
    except Exception as e:
        # A schema change stops the stream once by design; the next run picks up the evolved schema.
        results.append((t, None, f"error: {str(e)[:300]}"))
    print(results[-1])

display(spark.createDataFrame(results, "table string, rows long, status string"))
if any(r[2] != "ok" for r in results):
    raise RuntimeError("Some tables failed; re-run to resume (checkpoints make this safe).")
