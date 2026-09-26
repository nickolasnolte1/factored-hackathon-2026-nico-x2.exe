# Factored AI & Data Hackathon 2026 — AI-first Banking Customer Service

An AI-first customer service system for a LATAM retail bank (MX / CO / AR), built end-to-end on Databricks: from raw data ingestion and quality contracts to a controlled, auditable conversational agent with human handoff.

> Status: 🚧 in progress (challenge window: Sep 25 – Oct 5, 2026)

## Repository layout

| Path | Purpose |
|---|---|
| `databricks.yml` | Databricks Asset Bundle: jobs and pipelines, deployable with one command |
| `src/bronze/` | Ingestion: S3 → landing volume (idempotent copy) → Bronze Delta tables (Auto Loader) |
| `docs/` | Data findings, architecture, trade-offs, evaluation reports |
| `.githooks/` | Repo safety hooks (blocks secrets and data files from being committed) |
| `.env.example` | Local configuration template — copy to `.env`, never commit it |

## Setup

```bash
git clone https://github.com/nickolasnolte1/factored-hackathon-2026-nico-x2.exe.git
cd factored-hackathon-2026-nico-x2.exe
git config core.hooksPath .githooks   # required for every contributor
cp .env.example .env                  # fill in credentials locally
```

### Databricks

Requires the [Databricks CLI](https://docs.databricks.com/dev-tools/cli/) and a workspace login:

```bash
databricks auth login --host <workspace-url> --profile factored
export DATABRICKS_CONFIG_PROFILE=factored

# One-time: store the organizer's read-only S3 credentials as workspace secrets (never in code)
databricks secrets create-scope datathon
databricks secrets put-secret datathon aws_access_key_id
databricks secrets put-secret datathon aws_secret_access_key
databricks secrets put-secret datathon bucket

databricks bundle deploy
databricks bundle run ingest_bronze
```

Data lands in Unity Catalog under `workspace.{bronze,silver,gold,ops}`.

## Data policy

- The dataset is **synthetic** and provided by the organizers under read-only access. Raw data, organizer PDFs and credentials are **never** committed to this public repository.
- Any team-generated data (e.g. Portuguese test conversations) is labeled as such.

## Team

- Nickolas Nolte
