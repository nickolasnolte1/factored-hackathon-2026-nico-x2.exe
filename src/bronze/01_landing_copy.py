# Databricks notebook source
# MAGIC %md
# MAGIC # 01 · Landing copy (S3 → UC Volume)
# MAGIC Copies the organizer's raw CSV files, unchanged, from the read-only S3 bucket into the
# MAGIC `workspace.bronze.landing` volume. Idempotent: files already present with the same size are skipped,
# MAGIC so re-running only picks up new or late-arriving partitions.

# COMMAND ----------

dbutils.widgets.text("tables", "branches", "Comma-separated tables (or 'all')")
dbutils.widgets.text("source_prefix", "data", "S3 prefix: data | data_backup_20260831")
dbutils.widgets.text("max_workers", "16", "Parallel downloads")

TABLES = [t.strip() for t in dbutils.widgets.get("tables").split(",") if t.strip()]
SOURCE_PREFIX = dbutils.widgets.get("source_prefix").strip("/")
MAX_WORKERS = int(dbutils.widgets.get("max_workers"))
LANDING_ROOT = f"/Volumes/workspace/bronze/landing/{SOURCE_PREFIX}"

ALL_TABLES = [
    "branches", "customers", "products", "service_agents", "marketing_campaigns", "daily_exchange_rates",
    "transactions", "call_center_interactions", "call_transcripts", "satisfaction_surveys",
    "complaints", "campaign_sends", "digital_events",
]
if TABLES == ["all"]:
    TABLES = ALL_TABLES

# COMMAND ----------

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3

s3 = boto3.client(
    "s3",
    aws_access_key_id=dbutils.secrets.get("datathon", "aws_access_key_id"),
    aws_secret_access_key=dbutils.secrets.get("datathon", "aws_secret_access_key"),
    region_name="us-east-2",
)
BUCKET = dbutils.secrets.get("datathon", "bucket")


def list_objects(prefix):
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        yield from page.get("Contents", [])


def copy_one(obj):
    rel = obj["Key"][len(SOURCE_PREFIX) + 1:]
    dest = f"{LANDING_ROOT}/{rel}"
    if os.path.exists(dest) and os.path.getsize(dest) == obj["Size"]:
        return "skipped", obj["Size"]
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # Stream straight into the volume: the FUSE mount does not reliably support temp-file renames.
    body = s3.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"]
    with open(dest, "wb") as fh:
        for chunk in body.iter_chunks(8 * 1024 * 1024):
            fh.write(chunk)
    return "copied", obj["Size"]

# COMMAND ----------

summary, errors = [], []
for table in TABLES:
    # Dimension tables are single files; fact tables are folders partitioned by year/month/day.
    objs = [o for o in list_objects(f"{SOURCE_PREFIX}/{table}") if o["Key"].endswith(".csv")
            and (o["Key"].startswith(f"{SOURCE_PREFIX}/{table}/") or o["Key"] == f"{SOURCE_PREFIX}/{table}.csv")]
    counts = {"copied": 0, "skipped": 0, "failed": 0}
    size = 0
    with ThreadPoolExecutor(MAX_WORKERS) as pool:
        futures = {pool.submit(copy_one, o): o for o in objs}
        for f in as_completed(futures):
            try:
                status, n = f.result()
                counts[status] += 1
                size += n
            except Exception as e:  # keep going; failures are reported and retried on next run
                counts["failed"] += 1
                errors.append(f"{futures[f]['Key']}: {type(e).__name__}: {e}")
    summary.append((table, len(objs), counts["copied"], counts["skipped"], counts["failed"], round(size / 1e6, 1)))
    print(summary[-1])

display(spark.createDataFrame(summary, "table string, files int, copied int, skipped int, failed int, mb double"))
if errors:
    raise RuntimeError(f"{len(errors)} files failed (re-run to retry). First errors:\n" + "\n".join(errors[:5]))
