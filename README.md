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
| `src/gold/` | Gold: agent-ready, privacy-minimized tables read by the bank tools, with policy-rendered flags and privacy checks (`01_build_gold.py`, `gold_lib.py`, `sql/`, `gold_tables.json`) |
| `src/bank_tools/` | Mock banking tool service for the agent: 14 tools with sessions, ownership checks, policy enforcement, confirmation before writes, handoff packages and an audit trail, over a local Gold snapshot or Databricks ([contract](src/bank_tools/CONTRACT.md), [README](src/bank_tools/README.md)) |
| `tests/bank_tools/` | Acceptance tests of the bank tools, written from the contract and run through a contract-based harness, over a small synthetic fixture (live Databricks tests are opt-in), plus the security review's red-team tests (`test_security_redteam.py`) |
| `app/` | Demo app: customer chat with secure sign-in, candidate and confirmation cards, per-turn trace, and the human agent console, over the bank tools with a Databricks-served LLM ([README](app/README.md)) |
| `src/policy/` | Synthetic dispute-intake policy (`dispute_policy.json`, prepared for the project by an automated authoring process, not a real bank policy) and its deterministic reference implementation |
| `src/scenarios/` | ES/PT scenario generator: Silver anchors, intent dataset and end-to-end agent scenarios ([README](src/scenarios/README.md)) |
| `docs/` | Data findings, EDA and workflow selection, test scenarios, Silver, Gold and bank tools (architecture and evaluation reports to come) |
| `docs/03_test_scenarios.md` | Test scenarios (ES/PT): label provenance, splits and leakage checks, e2e categories, audit results |
| `docs/04_silver_layer.md` | Silver layer: issue-to-rule mapping, checks, watermark/MERGE semantics, freshness, results |
| `docs/05_gold_and_bank_tools.md` | Gold layer (tables, privacy, identity hash, checks, results) and the bank tools (identity, tool catalog, access control, confirmation, handoff, errors, audit, test and replay results) |
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
databricks bundle run build_gold                                    # full rebuild of the Gold tables from pinned Silver versions

# Scenario datasets (local, written to the git-ignored data/scenarios/)
python -m src.scenarios.build --seed 20261005 --extract --warehouse-id <sql-warehouse-id>
python -m src.scenarios.validate                                    # leakage checks, including the holdout

# Bank tools (local snapshot of Gold in the git-ignored data/bank_tools/; read-only SELECTs)
python -m src.bank_tools.snapshot --source gold --customers panel,sample:50 --warehouse-id <sql-warehouse-id> --profile factored
python -m src.bank_tools.demo                                       # scripted happy path, prints every tool call
python -m src.bank_tools.replay                                     # 280 e2e scenarios through the tools, no model
python -m pytest tests/bank_tools -q                                # acceptance tests (BANK_TOOLS_TEST_DATABRICKS=1 adds live ones)
```

Data lands in Unity Catalog under `workspace.{bronze,silver,gold,ops}`.

## Data policy

- The dataset is **synthetic** and provided by the organizers under read-only access. Raw data, organizer PDFs and credentials are **never** committed to this public repository.
- Any team-generated data (e.g. Portuguese test conversations) is labeled as such.

## Team

- Nickolas Nolte
- Nicolás Contreras
