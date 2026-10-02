# 04 — Silver Layer

_Built on 2026-09-30 from the 12 Bronze tables (source prefix `data/`). Every figure below comes from the Silver tables and the `workspace.ops` run log, not from the EDA. Code: [`src/silver/`](../src/silver/) ([README](../src/silver/README.md) has the contract reference and library API)._

## Summary

- **12 of 12 tables built** in `workspace.silver`: 7,874,194 rows, equal to Bronze. 0 rows quarantined, 0 duplicates removed, 0 cast failures, 0 rescued rows.
- **Reconciled:** for every table, Bronze rows = Silver rows + quarantined rows + natural-key duplicates removed (7,874,194 = 7,874,194 + 0 + 0). Primary keys are unique and non-null in all 12 tables. All 23 declared foreign keys have 0 orphans. The two broken branch keys reach 0 because their orphan values are nulled and flagged, with the raw value kept.
- **All 22 data-quality issues from the EDA** ([02](02_eda_workflow_selection.md), section 6) are implemented as a column, flag or check, or documented as a feature-layer rule (section 3).
- **616 checks per full run:** 63 hard (all pass), 552 warn, 1 flag. The 42 warn checks that fail are counted row by row in `ops.dq_results`: 26 implement an EDA issue and 16 are checks added while building Silver (`rule_ref` null; listed in section 3).
- **Incremental and idempotent:** a second run inserts 0 and updates 0 rows in every table. So does a replay of all of Bronze through the MERGE. Update correctness (new, late, updated, duplicate, uncastable and schema-evolved rows) is proven on a labeled test fixture: 35 of 35 assertions pass.

## 1. What Silver does

Silver turns each Bronze table (all columns are strings, plus lineage) into a typed, normalized and checked table with the same name. Each table is driven by two files:

| File | Role |
|---|---|
| `src/silver/sql/<table>.sql` | One `SELECT` over Bronze. It casts, trims and normalizes every column, derives the corrected columns and flags, de-duplicates on the natural key and keeps lineage |
| `src/silver/contracts/<table>.json` | Keys, column types, nullability, domains and ranges, foreign keys and named checks, tied to their EDA issue (`rule_ref`) when they implement one |

One shared engine, `silver_lib.py`, handles every table the same way:

1. Render the SQL for the incremental window.
2. Check that its output matches the contract.
3. Evaluate every check in one pass.
4. Split the rows: hard failures go to quarantine and warn failures stay in Silver, flagged.
5. Write with `CREATE OR REPLACE` or `MERGE`.
6. Check foreign keys on the written table.
7. Log the run.

`01_build_silver.py` runs the tables in dependency order: parents first, dimensions before facts.

The rule vocabulary is the one from the EDA:

- **FIX:** a corrected column. The delivered value stays next to it: in the untouched source column when the fix is a new column (e.g. `transaction_country` next to `transaction_country_code`), or in `<column>_raw` when the column keeps its name (imputed nulls are marked by a flag instead, e.g. `subcategory_imputed`).
- **FLAG:** a boolean next to the untouched value.
- **QUARANTINE (value):** the value is nulled and the raw copy kept, so it never joins.
- **DROP:** the column is not carried into Silver.

## 2. Contract format

```json
{
  "table": "transactions", "source": "transactions", "kind": "fact",
  "primary_key": ["transaction_id"],
  "natural_key": ["customer_id", "product_id", "event_ts", "amount"],
  "event_time": "event_ts",
  "load": {"cadence": "daily", "cluster_by": ["customer_id", "event_date"], "max_quarantine_rate": 0.01},
  "columns": {
    "amount": {"type": "decimal(18,2)", "nullable": false, "source": "amount", "rule_ref": 22,
               "description": "Amount in the transaction currency, always positive ..."}
  },
  "foreign_keys": [{"column": "customer_id", "ref_table": "customers", "ref_column": "customer_id",
                    "severity": "hard", "rule_ref": 14}],
  "checks": [{"name": "amount_positive", "severity": "hard", "rule_ref": 22, "expr": "amount > 0",
              "description": "..."}]
}
```

- **Descriptions are required.** They become the Delta column comments.
- **Generated checks.** The framework generates `not_null:<col>`, `cast:<col>` (the raw value is present but did not cast), `allowed:<col>`, `range:<col>`, `no_rescued_data`, `unique:primary_key`, `dedup:natural_key` and `fk:<col>-><table>.<col>`.
- **Validation.** `python silver_lib.py validate` checks every contract without Spark.

## 3. The 22 EDA issues and where each is implemented

"Silver result" is measured on the built tables.

| # | Issue | Rule | Implemented in | Silver result |
|---|---|---|---|---|
| 1 | Complaint ↔ contact link missing | FLAG | `complaints.link_status`; FK `origin_interaction_id` (severity `flag`); check `origin_interaction_linked` | 67,095 / 67,095 `unlinked`; no fuzzy re-linking |
| 2 | Complaint product belongs to someone else | QUARANTINE value | `complaints.affected_product_id` nulled unless owned; `affected_product_id_raw`; `product_not_owned`; check `product_owned_by_complainant` | 44,570 / 44,570 nulled; 0 foreign products left |
| 3 | Claimed amount and currency are random | FLAG | `complaints.claim_untraceable`; checks `claim_traceable`, `claim_amount_and_currency_paired` | 22,816 / 22,816 claims with an amount or currency are untraceable (21,751 with an amount) |
| 4 | Complaint outcome fields random or stale | FIX + FLAG | `complaints.compensation_amount` + `compensation_flag`, `sla_derived` (48 h first response, 30-day resolution), `stale_status`; 6 consistency checks | 4,641 compensations; `sla_derived` disagrees with the delivered flag on 45,892 of 66,356 cases; 48,884 of 50,269 open cases stale (Open, In Process or Escalated; the EDA's 45,629 / 46,948 left out Escalated and aged at 2026-06-17). Never labels |
| 5 | Text reveals the label | FIX + feature rule | `complaints.subcategory` imputed from category, `subcategory_imputed`; `description` kept for display and marked label-leaking in the contract | 6,698 imputed (9.98%) |
| 6 | Active cards past expiry | FIX | `products.effective_status` (`product_status` kept); check `active_not_expired` | Of the Active cards with an expiry date, 40,518 / 80,864 credit and 16,187 / 32,141 debit are `Expired` (as-of 2026-06-18; the EDA's 40,484 and 16,180 used 2026-06-17) |
| 7 | Stale last-movement field | FIX | `products.last_transaction_date_recomputed`, `first_transaction_date`, `transaction_count`, `last_transaction_date_stale` | 339,538 flagged: 305,294 wrong dates, 34,242 missing, 2 without transactions |
| 8 | Activity before opening or registration | FIX + FLAG | `products.effective_opening_date`, `activity_before_opening`, `opened_before_registration`; `transactions.activity_before_opening` / `activity_before_registration`; `campaign_sends.sent_before_registration` | 117,640 products; 827,610 transactions before opening and 829,540 before registration (of 4,425,008); 322,739 sends |
| 9 | Email is not an identity key | Contract | `customers.natural_key = (document_type, document_number)`, check `unique:natural_key`; `email` described as non-key | 0 duplicate documents; 79,930 of 147,016 customers with an email share it |
| 10 | Product-number collisions | QUARANTINE value | `products.product_number` nulled for colliding rows, `product_number_raw`, `product_number_collision` | 12 products (6 pairs) excluded from number lookup |
| 11 | Mexico in USD, document type `DNI` | FLAG | `customers.doc_type_inconsistent`; `products.currency_usd_for_mexico`; `transactions.currency` domain | 74,907 / 74,907 Mexican customers; 200,398 products |
| 12 | `amount_usd` fixed-rate and partly missing | FIX | `transactions.amount_usd` (fixed rate), `amount_usd_raw`, `amount_usd_imputed`; checks `amount_usd_fixed_rate`, `amount_usd_delivered` | 99,477 / 1,987,029 ARS/COP rows filled, plus all 2,437,979 USD rows at 1:1 (never delivered), so `amount_usd_imputed` is true on 2,537,456 rows; 0 NULL `amount_usd`. `amount_usd` is internal (policy thresholds and priority). Rule: a customer-facing conversion, if one is ever added, must use `daily_exchange_rates`; no tool converts amounts today |
| 13 | Two spellings of Mexico | FIX | `*_country_code` in customers, branches, service_agents, marketing_campaigns, campaign_sends and transactions; `transactions.is_international` | 40,515 unaccented rows normalized; 202,800 international (no false positives from the 18,412 Mexican-customer rows) |
| 14 | Branch foreign keys broken | QUARANTINE value + hard FKs | `customers.registration_branch_id` / `service_agents.assigned_branch_id` nulled when orphan, `*_raw` kept, `*_orphan` flags; 23 FK checks | 149,995 / 150,000 and 831 / 833 nulled; 0 orphans on all 23 keys |
| 15 | `mentioned_products` is random | DROP | Not selected in `call_center_interactions.sql` (nor `contact_reason`, a copy of `reason_category`) | Column absent from Silver |
| 16 | Fields that copy the label | Feature rule | Kept for lineage, each described as "never a feature": `transactions.fraud_score`, `call_transcripts.main_topics` / `detected_intents`, `call_center_interactions.detected_sentiment` (check `sentiment_label_matches_score`), survey scores | To be enforced where features are built ([02](02_eda_workflow_selection.md), section 10 leakage checklist); no model or feature pipeline exists yet, and Silver does not drop them |
| 17 | Truncated survey scales | FIX | `satisfaction_surveys.nps_category` recomputed from the score, `nps_category_raw`; scale checks | 3,274 / 63,668 missing categories filled. Rule: relative KPIs only |
| 18 | Implausible type × channel | FLAG | `transactions.implausible_type_channel`; check `type_channel_plausible` | 1,240,000 / 4,425,008 (28.0%) |
| 19 | `process_date` is a business-day cut-off | FIX | `event_ts` / `event_date` in all six fact tables; `process_date` kept as the business date; cut-off checks | 1,106,307 transactions and 228,318 interactions carry the previous day; 0 a later day |
| 20 | Customer status conflicts with products | FLAG | `products.customer_status_conflict`; check `customer_status_consistent` | 6,721 Active products of 2,694 Closed customers; precedence rule (Closed/Suspended → human) lives in the agent policy |
| 21 | Random missingness in key inputs | `null_reason` | `customers.credit_score_null_reason`, `estimated_monthly_income_null_reason`; products `credit_limit`, `interest_rate`, `days_past_due`, `expiration_date` null reasons; `transactions.response_code_null_reason` | 22,492 scores and 30,033 incomes `missing`; 17,664 declined/pending/reversed rows without a code, 203,369 approved rows `not_applicable` |
| 22 | Duplicates, typing, lineage | Dedup + strict casts | Natural-key `QUALIFY` in every SQL (`dedup:natural_key`), `cast:<col>` on every typed column, `no_rescued_data`, hard `amount_positive`, lineage columns | 0 duplicates, 0 cast failures, 0 rescued rows in all 12 tables |

**Found while building (not in the issue list), handled the same way.** Each contract documents these:

- **Placeholder coordinates:** branches and transactions. Nulled, raw kept, `has_valid_coordinates`.
- **Colombian postal codes that lost a leading zero:** branches and customers. Padded back, raw kept.
- **`+54` prefix on Mexican phones:** branches, customers and agents. Checked only, not rewritten.
- **`employee_code` collisions:** 26 agents. Flagged.
- **Engagement tracking by channel in campaign_sends:** the `engagement_tracked` denominator.
- **Sends outside the campaign window:** 8,277. Flagged.
- **Other checks added while building that fail on some rows** (warn, counted in `ops.dq_results`): `accent_confidence` without an accent on 56,776 transcripts; `last_updated` after the as-of date on 9,258 customers and 24,996 products; 3,828 customers registered before age 18 (`adult_at_registration`); 1,203 credit cards with a balance above their limit (`card_balance_within_limit`); 10 campaigns whose promoted product was filled from the campaign code; 3 Completed campaigns that end after the as-of date.

With the coordinate, postal-code and phone-prefix checks above, these are the 16 failing warn checks that are not tied to an EDA issue (`rule_ref` null).

## 4. Data-quality checks and quarantine

- **Severity.**
  - `hard`: the row goes to `workspace.silver.<table>_quarantine`, with the failing check names in `_dq_reasons` and the Bronze row as JSON in `_raw_json`.
  - `warn`: the row stays in Silver, with the names in `_dq_warnings`.
  - Foreign keys are evaluated after the write on the whole table, and are counted, never dropped. A `hard` key must hold; a `flag` key is known to be broken.
- **Hard by default:** `not_null` and `cast` on primary-key and event-time columns, `unique:primary_key`, plus declared business rules. Examples: `amount_positive` and `product_owned_by_customer`, the grounding invariant of the dispute flow.
- **Quality gate:** `load.max_quarantine_rate` (1% for transactions and campaign_sends, 5% elsewhere). Above it the table fails without writing anything and its watermark does not move.
- **Accounting:** `ops.dq_results` stores failed and total rows and the pass rate for every check in every run. `total_rows` is the number of rows the SQL returned in the window, so pass rates are comparable between full and incremental runs.

## 5. Lineage

| Column | Where | Meaning |
|---|---|---|
| `_source_file` | Bronze, Silver, quarantine | Landing file the row came from |
| `_ingested_at` | Bronze, Silver, quarantine | Auto Loader ingestion time; drives the watermark and the "newer version" test |
| `_silver_processed_at`, `_run_id` | Silver | Run that wrote this version of the row (joins `ops.pipeline_runs.run_id`, the job run id) |
| `_dq_warnings` | Silver, quarantine | Warn checks the row failed |
| `_dq_reasons`, `_raw_json`, `_quarantined_at` | Quarantine | Hard checks failed, the raw Bronze row, and when |
| `<column>_raw` | Silver | Delivered value next to every QUARANTINE-value column and every FIX column that keeps its source name (except the imputed `subcategory`, marked by `subcategory_imputed`). A FIX in a new column (e.g. `transaction_country_code`) keeps the delivered value in the source column instead |

## 6. Incremental loads: watermark and MERGE

- **Watermark:** the `watermark_out` of the table's last succeeded run in `ops.pipeline_runs`, which is the maximum `_ingested_at` that run processed.
  - The window is `_ingested_at > watermark`, capped at the maximum pinned when the run starts, so rows landing mid-run wait for the next run.
  - `watermark_override` replays any window.
- **`mode=full`:** reads all of Bronze and rebuilds the table and its quarantine (`CREATE OR REPLACE`), so SQL or contract changes reach every row. An incremental run on a missing table does the same.
- **`mode=incremental`:** `MERGE` on the primary key.
  - New keys are inserted.
  - An existing key is updated only when the incoming `_ingested_at` is newer **and** the content (contract columns and `_dq_warnings`) differs. A re-delivered identical row changes nothing and keeps its first lineage.
  - Rows with an older `_ingested_at` never overwrite newer ones.
  - If the SQL output schema no longer matches the table, the run fails and asks for `mode=full`.
- **Inside one window,** the SQL keeps the latest copy per natural key (`ORDER BY _ingested_at DESC, _source_file DESC`), and `unique:primary_key` quarantines any remaining primary-key collision.
- **Not propagated:** deletes (Bronze is append-only). In incremental mode the quarantine is an append log.

## 7. Freshness policy

- **Cadence: daily batch.** The source has one file per calendar day (`process_date`, weekends included) for every fact table and a snapshot per dimension; all of them landed at once on 2026-09-26. Run `build_silver` after `ingest_bronze`: they are separate jobs and nothing chains them (both are defined in `databricks.yml` and neither is deployed yet, section 10). The target is Silver `max(_ingested_at)` = Bronze `max(_ingested_at)` after each run, visible in `ops.freshness`.
- **Event time vs business date.**
  - Every time-based answer, filter and split uses `event_ts` / `event_date`.
  - `process_date` is the business day with a cut-off: 06:00 for transactions and campaign sends, 08:00 for contacts. 25.0% of transactions and 33.3% of contacts carry the previous day. Surveys carry the business day of their interaction.
  - `process_date` is kept only for partition pruning and lineage. The cut-off checks confirm that no row carries a later day.
- **Late arrivals.** The watermark is on ingestion time, not on event time or `process_date`, so a partition that lands days late (for example a re-delivered backup folder) is picked up by the next run whatever its event date. It keeps its original event date, which the fixture test asserts (section 8).
- **Cross-table derived columns are computed at build time:**
  - products: last movement, transaction count and effective opening date;
  - transactions: customer country and activity flags;
  - campaign_sends and complaints: flags against their dimensions.

  An incremental run recomputes them only for rows in the window. After late transactions or customer changes land, run `products` (or the affected table) with `mode=full`.
- **Freshness of the data itself:** event dates end on 2026-06-18, the dataset's as-of date. Surveys sent the day after end on 2026-06-19, and exchange rates end on 2026-06-17. `ops.freshness.max_event_date` reports it per table.

## 8. Update-correctness test (fixture)

The delivered data is a static snapshot: after the first load, incremental runs are empty. [`src/silver/02_update_fixture_test.py`](../src/silver/02_update_fixture_test.py) proves the update path on a small, clearly labeled fixture.

**What the fixture is:**

- **Same code as production:** `run_table`, `sql/transactions.sql` and `contracts/transactions.json`. Only table names change.
- **Fixture Bronze:**
  - `workspace.ops.fixture_bronze_transactions`: real rows with fixed `_ingested_at` and `_source_file = fixture://...`.
  - `fixture_bronze_customers` / `fixture_bronze_products`: only the id, country and date columns the SQL reads, no personal data.
- **Fixture Silver:** `workspace.ops.fixture_silver_transactions` and `_quarantine`.
- **Run log:** `fixture_pipeline_runs` / `fixture_dq_results` (`ops_prefix = 'fixture_'`), so the real run log and watermarks are never touched.
- **Guards:** `run_table` refuses a target that is not `fixture_*`, and refuses to log fixture runs to the real ops tables. Every fixture table carries a `TEST FIXTURE` comment.

| Step | Input | Asserted outcome |
|---|---|---|
| Batch 1, `full` | 300 rows + 1 identical copy of one of them in a second file | 301 read, 1 removed by natural-key dedup, 300 inserted, watermark = batch 1 |
| Batch 2, `incremental` | 200 new rows; 1 late row (event 2024-01-15, new `_ingested_at`); 1 updated version (Pending → Approved); 1 identical copy of a batch-1 row; 1 amount `'12,50 EUR'`; 1 row with a new Bronze column `loyalty_points` (appended with `mergeSchema`, as Auto Loader's `addNewColumns` does) | Automatic watermark from the fixture log. 205 read, 1 quarantined (`amount_positive`; the raw amount is kept in `_raw_json`). 202 inserted, 1 updated with batch-2 lineage. The copy is unchanged and keeps batch-1 lineage. The late row keeps its 2024 event date. The Silver schema is unchanged by the new column |
| Rerun | nothing new | 0 read, 0 inserted, 0 updated, watermark unchanged |
| Replay (`watermark_override = 1900-01-01`) | whole fixture history | 506 read; 3 superseded copies de-duplicated; 0 inserted, 0 updated, 0 rows rewritten; the bad row is logged again in the quarantine (append log) |
| Batch 3, `incremental` | 1 valid + 1 uncastable row (50% > the 1% gate) | Run fails with `QualityGateError`; nothing written (not even the valid row); watermark stays at batch 2 |

Last run: job run `423425889197422`, 35 of 35 assertions passed in 170 s. The fixture tables are kept for inspection. Set the `cleanup` widget to `true` to drop them.

## 9. Observability

| Table | Content |
|---|---|
| `workspace.ops.pipeline_runs` | One row per table and run: mode, `watermark_in` / `watermark_out`, rows source / valid / quarantined / inserted / updated, status, error |
| `workspace.ops.dq_results` | One row per run, table and check: severity, `rule_ref` (EDA issue), failed and total rows, pass rate, passed (`NULL` = parent not built) |
| `workspace.ops.freshness` | One row per table: rows, quarantine rows, max event date, max `_ingested_at`, last run and last success (refreshed at the end of every build) |
| `workspace.silver.<table>_quarantine` | Rows that failed a hard check, with reasons and the raw row |
| `workspace.ops.fixture_*` | Fixture Bronze, Silver and run log of the update test |

The runner prints a summary table and displays the failed checks and freshness. A failing table does not stop the others, but the job fails at the end.

## 10. How to run

**Bundle.** The `build_silver` job has two tasks: `silver_build`, then `update_fixture_test` (which runs even if the build fails). It is defined in `databricks.yml` but not deployed yet; the runs in section 11 were one-off `jobs submit` runs of the same notebooks.

```bash
databricks bundle validate --profile factored
databricks bundle deploy --profile factored
databricks bundle run build_silver --profile factored                                 # incremental, all tables
databricks bundle run build_silver --profile factored --params tables=all,mode=full   # full rebuild
databricks bundle run build_silver --profile factored --params tables=products,mode=full
```

**Manual (one-time run, no deploy).**

```bash
databricks workspace import-dir src /Users/<you>/_staging/repo/src --overwrite --profile factored
databricks jobs submit --no-wait --profile factored --json '{"run_name": "silver", "tasks": [{"task_key": "t",
  "notebook_task": {"notebook_path": "/Users/<you>/_staging/repo/src/silver/01_build_silver",
  "base_parameters": {"tables": "all", "mode": "full", "run_id": "{{job.run_id}}"}}}]}'
```

**Read-only previews on the SQL warehouse (no Spark).** Run these from `src/silver/`:

- `python silver_lib.py validate`
- `sql <table>`
- `describe <table>`
- `checks <table>`
- `fk <table>`

## 11. Results

**Build runs** (serverless, 2026-09-30; one-off `jobs submit` runs, not the bundle job):

| Run | Job run id | Wall-clock | Result |
|---|---|---|---|
| Full, all tables | `984194527234981` | 561 s (tables 464 s; transactions 82 s) | 12 / 12 succeeded |
| Incremental, all tables | `574516312881487` | 179 s (2–13 s per table) | 12 / 12 succeeded, 0 rows read, 0 inserted, 0 updated |
| Replay of all Bronze through MERGE (`watermark_override = 1900-01-01`) | `364048081240012` | 406 s (transactions 67 s) | 12 / 12 succeeded, 7,874,194 rows re-merged, 0 inserted, 0 updated |
| Fixture test | `423425889197422` | 170 s | 35 / 35 assertions |
| Framework self-test (`tests/test_silver_framework.py`) after the library changes | `890280484353900` | 161 s | 20 / 20 assertions |

**Tables** (full run). "Checks" counts hard / warn / flag. "Rows without warnings" is low where known issues flag most rows: complaints (every case is unlinked), customers (149,995 of 150,000 branch orphans), products (stale last movement on 84.9%, plus the Mexico-in-USD and timeline flags).

| Table | Bronze rows | Silver rows | Quarantined | Dedup removed | Checks | Hard passed | Warn checks failing | Rows without warnings |
|---|---|---|---|---|---|---|---|---|
| branches | 350 | 350 | 0 | 0 | 2 / 56 / 0 | 2 / 2 | 3 | 23.1% |
| customers | 150,000 | 150,000 | 0 | 0 | 3 / 56 / 0 | 3 / 3 | 6 | 0.0% |
| daily_exchange_rates | 13,164 | 13,164 | 0 | 0 | 6 / 17 / 0 | 6 / 6 | 0 | 100.0% |
| marketing_campaigns | 200 | 200 | 0 | 0 | 2 / 33 / 0 | 2 / 2 | 2 | 93.5% |
| products | 400,000 | 400,000 | 0 | 0 | 4 / 64 / 0 | 4 / 4 | 9 | 3.3% |
| service_agents | 1,200 | 1,200 | 0 | 0 | 3 / 43 / 0 | 3 / 3 | 3 | 17.0% |
| call_center_interactions | 686,296 | 686,296 | 0 | 0 | 6 / 46 / 0 | 6 / 6 | 0 | 100.0% |
| call_transcripts | 171,321 | 171,321 | 0 | 0 | 6 / 33 / 0 | 6 / 6 | 1 | 66.9% |
| campaign_sends | 1,746,801 | 1,746,801 | 0 | 0 | 6 / 47 / 0 | 6 / 6 | 2 | 81.1% |
| complaints | 67,095 | 67,095 | 0 | 0 | 9 / 62 / 1 | 9 / 9 | 7 | 0.0% |
| satisfaction_surveys | 212,759 | 212,759 | 0 | 0 | 7 / 38 / 0 | 7 / 7 | 2 | 84.7% |
| transactions | 4,425,008 | 4,425,008 | 0 | 0 | 9 / 57 / 0 | 9 / 9 | 7 | 42.2% |
| **Total** | **7,874,194** | **7,874,194** | **0** | **0** | **63 / 552 / 1** | **63 / 63** | **42** | |

**Integrity** (separate read-only SQL on the built tables, outside the pipeline's own checks):

- **Primary keys:** 0 duplicate and 0 NULL primary keys in all 12 tables.
- **Foreign keys:** 0 orphans on all 23 foreign keys.
- **Branch keys:** the raw columns keep the broken values (149,995 and 831 orphans) for audit.

## 12. Known limitations

- **`digital_events` is not loaded** (deferred at Bronze ingestion; no documented reason), so there is no Silver table for it. Card and app events and any fraud signal they carry remain unexamined, so whether disputes need them is unknown.
- **Synthetic-data artifacts are flagged, not repaired.** Some flags fire on many rows:
  - `link_status` (every case) and the registration-branch orphans (149,995 of 150,000);
  - `doc_type_inconsistent` and `currency_usd_for_mexico` (every Mexican customer and product, about half of each table);
  - the 28% of implausible type × channel pairs;
  - templated transcripts;
  - label-derived fields (`fraud_score`, `main_topics`, survey scores).

  Silver makes them visible; feature sets and the agent must respect the flags.
- **The dataset's as-of date (2026-06-18) is hard-coded** in the products, customers and complaints SQL and in some checks: effective card status, stale complaint status, derived SLA, and dates after the as-of date.
- **Derived cross-table columns are not refreshed incrementally** (section 7). After late facts or dimension changes, run the affected table with `mode=full`.
- **Personal data** (names, documents, contact details) stays in `silver.customers` and `silver.service_agents`. It must be masked or excluded in Gold and in anything the agent logs.
- **Timezone and scope of the proof.** The timezone of event timestamps is not documented, and the cut-offs suggest a single system clock. Schema evolution and late arrivals are proven on the fixture only: `data_backup_20260831/` is not ingested, and the announced ~2% duplicates were not found under any natural key.
- **Replays re-log quarantined rows,** because the quarantine is an append log in incremental mode. Deletes are not propagated.
- **Coordinate fix applied with a full rebuild of transactions.** The loose Brazil bounding box marked 1,115 international rows as `has_valid_coordinates` although they carry the customer's home-city point (Bogotá or Buenos Aires). `sql/transactions.sql` now excludes those areas. Because a MERGE never rewrites a row with the same `_ingested_at`, the fix was applied with `transactions` in `mode=full` (2026-09-30): 4,425,008 rows, 0 quarantined, 0 Brazil rows left with valid coordinates. The scenario anchors were extracted before this rebuild; they do not use coordinates.
