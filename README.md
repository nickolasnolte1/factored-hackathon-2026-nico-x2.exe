# Factored AI & Data Hackathon 2026 — AI-first Banking Customer Service

An AI-first customer service system for a LATAM retail bank (MX / CO / AR), built end-to-end on Databricks: from raw data ingestion and quality contracts to a controlled, auditable conversational agent with human handoff.

## Start here (5 minutes)

1. **Try it.** Live demo: https://nicocon123-expediente.hf.space (public, synthetic data, limited daily usage). Pick a test customer in the sidebar; the simulated phone shows the one-time code. The same app runs as the Databricks App `expediente-demo` in the team workspace.
2. **Watch it.** Video pitch: https://drive.google.com/file/d/1Y9NDcfUnlr13FtUGLLstPGJnolGDKZnG/view?usp=sharing
3. **Check the results** (held-out, offline, 95% intervals):

| What | Result |
|---|---|
| Agent end to end, 140 test scenarios (ES and PT) | 137/140 succeed (97.9%) [95.0, 100.0] |
| Safe automated resolution, in-scope test scenarios | 77/79 (97.5%) [93.7, 100.0] |
| Transfers to a person | 0 missed of 29 needed (28 with the right reason), 0 unnecessary of 111 |
| Unsafe outcomes (wrong customer's data, unconfirmed or ungrounded writes, injected actions) | 0 of 140 (at most 2.1% at 95% confidence) |
| Success by customer country | MX 65/66, CO 37/38, AR 35/36; no gap whose interval excludes zero (segments and language variants in [06](docs/06_evaluation.md#fairness-by-country-segment-and-language-variant)) |
| Intent classifier v2 vs keyword router | Better macro-F1 on all three held-out sets (paired 95% intervals above zero) |
| Model cost | About US$ 0.0044 per attempted case (estimate: model tokens only, list DBU rates, an assumed US$ 0.07 per DBU; [06](docs/06_evaluation.md)) |

4. **Read in this order:** why disputes ([02](docs/02_eda_workflow_selection.md)), how it is built and would be operated ([07](docs/07_architecture_and_operations.md)), how it was evaluated ([06](docs/06_evaluation.md)), then the data and tool layers ([04](docs/04_silver_layer.md), [05](docs/05_gold_and_bank_tools.md)).

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
| `src/classifier/` | ES/PT intake intent classifier: keyword baseline, trained model (v1 text only; v2 text plus keyword features, the default), runtime adapter for the app and the final evaluation ([README](src/classifier/README.md)) |
| `tests/classifier/` | Classifier tests on toy data, including checks that only the evaluation code reads the final test sets |
| `src/agent_eval/` | End-to-end exam of the real agent (`app/agent.py` with the served model) on the e2e scenarios: a fresh bank per scenario, scoring of what was written to the bank, reply checks, and an oracle mode that must score 280/280 ([README](src/agent_eval/README.md)); `challenge_outcomes.py` reports a saved run in the problem statement's five outcome measures, and `fairness.py` breaks it down by customer country, segment and language variant |
| `tests/agent_eval/` | Tests of the exam's scoring on hand-made fixtures and on a scripted model |
| `eval/results/` | Classifier evaluation (`intent_classifier.*`), agent exam results per split and endpoint (`agent_e2e_<split>_<endpoint>.*`), the challenge's outcome measures (`challenge_outcomes_<split>_<endpoint>.*`) and the outcomes by country, segment and language variant (`fairness_<split>_<endpoint>.*`), every number with its 95% interval |
| `docs/` | Data findings, EDA and workflow selection, test scenarios, Silver, Gold and bank tools, the evaluation of the classifier and the agent ([06](docs/06_evaluation.md)), and the architecture and route to operation ([07](docs/07_architecture_and_operations.md)) |
| `docs/03_test_scenarios.md` | Test scenarios (ES/PT): label provenance, splits and leakage checks, e2e categories, audit results |
| `docs/04_silver_layer.md` | Silver layer: issue-to-rule mapping, checks, watermark/MERGE semantics, freshness, results |
| `docs/05_gold_and_bank_tools.md` | Gold layer (tables, privacy, identity hash, checks, results) and the bank tools (identity, tool catalog, access control, confirmation, handoff, errors, audit, test and replay results) |
| `.githooks/` | Repo safety hooks (blocks secrets and data files from being committed) |
| `.env.example` | Local configuration template — copy to `.env`, never commit it |

## Setup

Python 3.11 (the version of the Databricks App and the Space image; `.python-version` pins it).

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

# Intent classifier (local; models/ is git-ignored)
python -m src.classifier.train                                      # v2, bilingual; --view transfer for the ES-only model, --release v1 for v1
python -m src.classifier.evaluate                                   # the only code that reads eval/holdout/; writes eval/results/

# Agent exam (calls the model endpoint; transcripts in the git-ignored data/agent_eval/)
python -m src.agent_eval.run --oracle --split all                  # scorer check without a model: must be 280/280
python -m src.agent_eval.run --split dev                           # the real agent on the 140 dev scenarios
python -m src.agent_eval.challenge_outcomes                        # the five outcome measures of a saved run (no model calls)
python -m src.agent_eval.fairness                                  # outcomes by country, segment and language variant of a saved run (no model calls)
```

Data lands in Unity Catalog under `workspace.{bronze,silver,gold,ops}`.

## Data policy

- The dataset is **synthetic** and provided by the organizers under read-only access: no real customers, and no real or de-identified customer records. Raw data, organizer PDFs and credentials are **never** committed to this public repository.
- Team-generated data is labeled as such ([report 03, section 2](docs/03_test_scenarios.md#2-label-provenance); the policy in [section 7](docs/03_test_scenarios.md#7-synthetic-dispute-policy)): the scenario and intent texts in Spanish and Portuguese and the synthetic dispute policy come from automated authoring for the project, the independent holdout from a separate generation process, and the 61-message team holdout was hand-written by one team member.
- The public demo shows only synthetic test customers, their synthetic movements, and the cases and tickets created in the demo (kept in `/tmp`, gone at restart).
- The Gold snapshot and the intent classifier model are kept in a private Hugging Face dataset repo and in the team's Databricks workspace, and are never committed (`data/` and `models/` are git-ignored).
- The organizers' S3 credentials are stored only in a Databricks secret scope, read by the Bronze copy job; they are never in the repository or in either demo.

## Team

- Nickolas Nolte
- Nicolás Contreras
