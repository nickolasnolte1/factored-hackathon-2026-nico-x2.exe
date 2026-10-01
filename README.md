# Factored AI & Data Hackathon 2026 — AI-first Banking Customer Service

An AI-first customer service system for a LATAM retail bank (MX / CO / AR), built end-to-end on Databricks: from raw data ingestion and quality contracts to a controlled, auditable conversational agent with human handoff.

> Status: 🚧 in progress (challenge window: Sep 25 – Oct 5, 2026)

## Repository layout

| Path | Purpose |
|---|---|
| `databricks.yml` | Databricks Asset Bundle: jobs and pipelines, deployable with one command |
| `src/bronze/` | Ingestion: S3 → landing volume (idempotent copy) → Bronze Delta tables (Auto Loader) |
| `src/silver/` | Silver: contract-driven typing, data-quality checks, quarantine and incremental MERGE (`01_build_silver.py`, `silver_lib.py`, `sql/`, `contracts/`) |
| `src/silver/02_update_fixture_test.py` | Update-correctness test on labeled fixture tables: new, late, updated, duplicate, invalid and schema-evolved rows |
| `src/policy/` | Synthetic dispute-intake policy (`dispute_policy.json`, team-made, not a real bank policy) and its deterministic reference implementation |
| `src/scenarios/` | ES/PT scenario generator: Silver anchors, intent dataset and end-to-end agent scenarios ([README](src/scenarios/README.md)) |
| `docs/` | Data findings, architecture, trade-offs, evaluation reports |
| `docs/03_test_scenarios.md` | Test scenarios (ES/PT): label provenance, splits and leakage checks, e2e categories, audit results |
| `docs/04_silver_layer.md` | Silver layer: issue-to-rule mapping, checks, watermark/MERGE semantics, freshness, results |
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
databricks bundle run build_silver                                  # incremental; --params mode=full for a rebuild

# Scenario datasets (local, written to the git-ignored data/scenarios/)
python -m src.scenarios.build --seed 20261005 --extract --warehouse-id <sql-warehouse-id>
python -m src.scenarios.validate                                    # leakage checks, including the holdout
```

Data lands in Unity Catalog under `workspace.{bronze,silver,gold,ops}`.

## Data policy

- The dataset is **synthetic** and provided by the organizers under read-only access. Raw data, organizer PDFs and credentials are **never** committed to this public repository.
- Any team-generated data (e.g. Portuguese test conversations) is labeled as such.

## Team

- Nickolas Nolte
