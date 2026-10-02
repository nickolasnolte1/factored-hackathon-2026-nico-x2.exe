# Databricks notebook source
# MAGIC %md
# MAGIC # 01 · EDA and workflow selection
# MAGIC Read-only queries behind `docs/02_eda_workflow_selection.md`. Every figure in that report can be reproduced here or in the
# MAGIC visual notebook (`02_eda_visual_report.py`), except the figures the report marks with †.
# MAGIC
# MAGIC - **Source:** `workspace.bronze.*` (raw, pre-dedup, full data). Every business column is a string, so values are cast
# MAGIC   with `try_cast` and booleans are compared with `= 'True'`.
# MAGIC - **Event time, not `process_date`:** `process_date` is a business-day partition with a fixed cut-off, so 25-34% of events
# MAGIC   carry the previous day. All trends and splits use the event timestamp.
# MAGIC - **Read-only:** only `SELECT` / `WITH`. Aggregates only; no personal data is displayed.
# MAGIC - Cells are grouped by report section and numbered `Q<section>.<n>`, matching the references in the report.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1 · Context and method
# MAGIC Scope, volumes, lineage and event windows of the Bronze tables.

# COMMAND ----------

# MAGIC %md
# MAGIC **Q1.1.** How many rows does each Bronze table hold, and did any row fail schema parsing (`_rescued_data`)?

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT 'branches' tbl, count(*) n, count_if(_rescued_data IS NOT NULL) rescued FROM workspace.bronze.branches
# MAGIC UNION ALL SELECT 'customers', count(*), count_if(_rescued_data IS NOT NULL) FROM workspace.bronze.customers
# MAGIC UNION ALL SELECT 'products', count(*), count_if(_rescued_data IS NOT NULL) FROM workspace.bronze.products
# MAGIC UNION ALL SELECT 'service_agents', count(*), count_if(_rescued_data IS NOT NULL) FROM workspace.bronze.service_agents
# MAGIC UNION ALL SELECT 'marketing_campaigns', count(*), count_if(_rescued_data IS NOT NULL) FROM workspace.bronze.marketing_campaigns
# MAGIC UNION ALL SELECT 'daily_exchange_rates', count(*), count_if(_rescued_data IS NOT NULL) FROM workspace.bronze.daily_exchange_rates
# MAGIC UNION ALL SELECT 'transactions', count(*), count_if(_rescued_data IS NOT NULL) FROM workspace.bronze.transactions
# MAGIC UNION ALL SELECT 'call_center_interactions', count(*), count_if(_rescued_data IS NOT NULL) FROM workspace.bronze.call_center_interactions
# MAGIC UNION ALL SELECT 'call_transcripts', count(*), count_if(_rescued_data IS NOT NULL) FROM workspace.bronze.call_transcripts
# MAGIC UNION ALL SELECT 'satisfaction_surveys', count(*), count_if(_rescued_data IS NOT NULL) FROM workspace.bronze.satisfaction_surveys
# MAGIC UNION ALL SELECT 'complaints', count(*), count_if(_rescued_data IS NOT NULL) FROM workspace.bronze.complaints
# MAGIC UNION ALL SELECT 'campaign_sends', count(*), count_if(_rescued_data IS NOT NULL) FROM workspace.bronze.campaign_sends
# MAGIC ORDER BY n DESC

# COMMAND ----------

# MAGIC %md
# MAGIC **Q1.2.** What event window does each fact table cover, and do all event timestamps parse?

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT 'transactions' tbl, min(try_cast(transaction_date AS TIMESTAMP)) first_event, max(try_cast(transaction_date AS TIMESTAMP)) last_event,
# MAGIC        count_if(try_cast(transaction_date AS TIMESTAMP) IS NULL) unparsed
# MAGIC FROM workspace.bronze.transactions
# MAGIC UNION ALL
# MAGIC SELECT 'call_center_interactions', min(try_cast(interaction_date AS TIMESTAMP)), max(try_cast(interaction_date AS TIMESTAMP)),
# MAGIC        count_if(try_cast(interaction_date AS TIMESTAMP) IS NULL)
# MAGIC FROM workspace.bronze.call_center_interactions
# MAGIC UNION ALL
# MAGIC SELECT 'complaints', min(try_cast(creation_date AS TIMESTAMP)), max(try_cast(creation_date AS TIMESTAMP)),
# MAGIC        count_if(try_cast(creation_date AS TIMESTAMP) IS NULL)
# MAGIC FROM workspace.bronze.complaints

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2 · Demand and pain by contact reason
# MAGIC `call_center_interactions` (n = 686,296) joined to `satisfaction_surveys` and `service_agents`.

# COMMAND ----------

# MAGIC %md
# MAGIC **Q2.1.** Per contact reason: volume, FCR (raw and strict), follow-up, escalation, handle time and handle-hours (full data and Jul 2024 - Jun 2025).

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH b AS (
# MAGIC   SELECT reason_category r,
# MAGIC          CASE WHEN was_resolved = 'True' THEN 1 ELSE 0 END res,
# MAGIC          CASE WHEN was_resolved = 'True' AND was_escalated = 'False' AND requires_followup = 'False' THEN 1 ELSE 0 END strict_res,
# MAGIC          CASE WHEN requires_followup = 'True' THEN 1 ELSE 0 END fup,
# MAGIC          CASE WHEN was_escalated = 'True' THEN 1 ELSE 0 END esc,
# MAGIC          try_cast(duration_seconds AS DOUBLE) dur,
# MAGIC          try_cast(interaction_date AS TIMESTAMP) ts
# MAGIC   FROM workspace.bronze.call_center_interactions)
# MAGIC SELECT coalesce(r, 'ALL') reason, count(*) n,
# MAGIC        round(100.0 * count(*) / (SELECT count(*) FROM workspace.bronze.call_center_interactions), 2) share_pct,
# MAGIC        sum(res) resolved, round(100.0 * avg(res), 2) fcr_pct,
# MAGIC        sum(strict_res) strict_resolved, round(100.0 * avg(strict_res), 2) fcr_strict_pct,
# MAGIC        count(*) - sum(res) unresolved,
# MAGIC        round(100.0 * avg(fup), 2) followup_pct, round(100.0 * avg(esc), 2) escalated_pct,
# MAGIC        count(dur) n_dur, percentile(dur, 0.5) aht_p50_s, percentile(dur, 0.9) aht_p90_s,
# MAGIC        round(sum(dur) / 3600, 0) handle_h,
# MAGIC        sum(CASE WHEN ts >= TIMESTAMP'2024-07-01' AND ts < TIMESTAMP'2025-07-01' THEN 1 ELSE 0 END) n_fy2,
# MAGIC        sum(CASE WHEN ts >= TIMESTAMP'2024-07-01' AND ts < TIMESTAMP'2025-07-01' THEN 1 - res ELSE 0 END) unresolved_fy2,
# MAGIC        round(sum(CASE WHEN ts >= TIMESTAMP'2024-07-01' AND ts < TIMESTAMP'2025-07-01' THEN dur END) / 3600, 0) handle_h_fy2
# MAGIC FROM b GROUP BY ROLLUP(r) ORDER BY n DESC

# COMMAND ----------

# MAGIC %md
# MAGIC **Q2.2.** What is CSAT (1-4 scale) per reason, and how does it split by resolution?

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH s AS (SELECT interaction_id, try_cast(main_score AS DOUBLE) sc FROM workspace.bronze.satisfaction_surveys WHERE survey_type = 'CSAT')
# MAGIC SELECT coalesce(i.reason_category, 'ALL') reason, count(*) n_csat, round(avg(s.sc), 3) csat_mean,
# MAGIC        round(100.0 * avg(CASE WHEN s.sc <= 2 THEN 1.0 ELSE 0.0 END), 1) csat_low_pct,
# MAGIC        round(avg(CASE WHEN i.was_resolved = 'True' THEN s.sc END), 3) csat_if_resolved,
# MAGIC        round(avg(CASE WHEN i.was_resolved = 'False' THEN s.sc END), 3) csat_if_unresolved
# MAGIC FROM s JOIN workspace.bronze.call_center_interactions i ON s.interaction_id = i.interaction_id
# MAGIC GROUP BY ROLLUP(i.reason_category) ORDER BY n_csat DESC

# COMMAND ----------

# MAGIC %md
# MAGIC **Q2.3.** Do survey scores depend only on `was_resolved`? (score distribution per survey type and resolution)

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT s.survey_type, i.was_resolved, try_cast(s.main_score AS INT) score, count(*) n,
# MAGIC        round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY s.survey_type, i.was_resolved), 2) pct
# MAGIC FROM workspace.bronze.satisfaction_surveys s
# MAGIC JOIN workspace.bronze.call_center_interactions i ON s.interaction_id = i.interaction_id
# MAGIC GROUP BY 1, 2, 3 ORDER BY 1, 2, 3

# COMMAND ----------

# MAGIC %md
# MAGIC **Q2.4.** Survey coverage per reason and standard NPS on the 2-7 scale actually present.

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH sv AS (SELECT interaction_id, max(CASE WHEN survey_type = 'NPS' THEN try_cast(main_score AS DOUBLE) END) nps
# MAGIC             FROM workspace.bronze.satisfaction_surveys GROUP BY 1)
# MAGIC SELECT coalesce(i.reason_category, 'ALL') reason, count(*) n_int, count(sv.interaction_id) n_surveyed,
# MAGIC        round(100.0 * count(sv.interaction_id) / count(*), 2) coverage_pct, count(sv.nps) n_nps,
# MAGIC        sum(CASE WHEN sv.nps >= 9 THEN 1 ELSE 0 END) promoters, sum(CASE WHEN sv.nps <= 6 THEN 1 ELSE 0 END) detractors,
# MAGIC        round(100.0 * (sum(CASE WHEN sv.nps >= 9 THEN 1 ELSE 0 END) - sum(CASE WHEN sv.nps <= 6 THEN 1 ELSE 0 END)) / count(sv.nps), 1) nps_standard,
# MAGIC        round(avg(sv.nps), 2) nps_mean, max(sv.nps) nps_max
# MAGIC FROM workspace.bronze.call_center_interactions i LEFT JOIN sv ON i.interaction_id = sv.interaction_id
# MAGIC GROUP BY ROLLUP(i.reason_category) ORDER BY n_int DESC

# COMMAND ----------

# MAGIC %md
# MAGIC **Q2.5.** Is there an agent effect on FCR beyond reason mix? (per-agent dispersion vs binomial noise)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH pr AS (SELECT reason_category r, avg(CASE WHEN was_resolved = 'True' THEN 1.0 ELSE 0.0 END) p
# MAGIC             FROM workspace.bronze.call_center_interactions GROUP BY 1),
# MAGIC x AS (SELECT i.agent_id, CASE WHEN i.was_resolved = 'True' THEN 1.0 ELSE 0.0 END y, pr.p
# MAGIC       FROM workspace.bronze.call_center_interactions i JOIN pr ON i.reason_category = pr.r),
# MAGIC a AS (SELECT agent_id, count(*) n, avg(y) fcr, sum(y) obs, sum(p) exp_, sum(p * (1 - p)) v FROM x GROUP BY agent_id)
# MAGIC SELECT count(*) n_agents, percentile(n, 0.5) p50_n, round(stddev(fcr), 4) sd_raw,
# MAGIC        round(sqrt(avg(0.7665 * (1 - 0.7665) / n)), 4) binomial_sd_expected,
# MAGIC        round(stddev((obs - exp_) / sqrt(v)), 3) sd_z_reason_adjusted,
# MAGIC        sum(CASE WHEN abs((obs - exp_) / sqrt(v)) > 1.96 THEN 1 ELSE 0 END) n_abs_z_gt_1_96,
# MAGIC        round(sum(power(obs - exp_, 2) / v), 1) chi2, count(*) - 1 df
# MAGIC FROM a

# COMMAND ----------

# MAGIC %md
# MAGIC **Q2.6.** Does FCR vary by channel, customer country or specialist routing?

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT 'channel / type' dim, concat(channel, ' / ', interaction_type) v, count(*) n,
# MAGIC        round(100.0 * avg(CASE WHEN was_resolved = 'True' THEN 1 ELSE 0 END), 2) fcr_pct
# MAGIC FROM workspace.bronze.call_center_interactions GROUP BY 2
# MAGIC UNION ALL
# MAGIC SELECT 'customer country', c.country, count(*), round(100.0 * avg(CASE WHEN i.was_resolved = 'True' THEN 1 ELSE 0 END), 2)
# MAGIC FROM workspace.bronze.call_center_interactions i JOIN workspace.bronze.customers c ON i.customer_id = c.customer_id GROUP BY 2
# MAGIC UNION ALL
# MAGIC SELECT concat('routing | ', i.reason_category),
# MAGIC        CASE WHEN (i.reason_category = 'Queja' AND a.specialty = 'Quejas y Reclamos')
# MAGIC               OR (i.reason_category = 'Técnico' AND a.specialty = 'Soporte Técnico') THEN 'specialist' ELSE 'other' END,
# MAGIC        count(*), round(100.0 * avg(CASE WHEN i.was_resolved = 'True' THEN 1 ELSE 0 END), 2)
# MAGIC FROM workspace.bronze.call_center_interactions i LEFT JOIN workspace.bronze.service_agents a ON i.agent_id = a.agent_id
# MAGIC WHERE i.reason_category IN ('Queja', 'Técnico') GROUP BY 1, 2
# MAGIC ORDER BY 1, 3 DESC

# COMMAND ----------

# MAGIC %md
# MAGIC **Q2.7.** Are follow-up and escalation rule-generated? (follow-up vs resolution, escalation rate per reason)

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT coalesce(reason_category, 'ALL') reason, count(*) n,
# MAGIC        sum(CASE WHEN was_resolved = 'False' THEN 1 ELSE 0 END) unresolved,
# MAGIC        sum(CASE WHEN was_resolved = 'False' AND requires_followup = 'True' THEN 1 ELSE 0 END) unresolved_with_followup,
# MAGIC        sum(CASE WHEN was_resolved = 'True' THEN 1 ELSE 0 END) resolved,
# MAGIC        round(100.0 * sum(CASE WHEN was_resolved = 'True' AND requires_followup = 'True' THEN 1 ELSE 0 END)
# MAGIC              / sum(CASE WHEN was_resolved = 'True' THEN 1 ELSE 0 END), 2) resolved_followup_pct,
# MAGIC        round(100.0 * avg(CASE WHEN was_escalated = 'True' THEN 1 ELSE 0 END), 2) escalated_pct,
# MAGIC        round(100.0 * avg(CASE WHEN was_resolved = 'True' THEN CASE WHEN was_escalated = 'True' THEN 1.0 ELSE 0.0 END END), 2) esc_if_resolved,
# MAGIC        round(100.0 * avg(CASE WHEN was_resolved = 'False' THEN CASE WHEN was_escalated = 'True' THEN 1.0 ELSE 0.0 END END), 2) esc_if_unresolved
# MAGIC FROM workspace.bronze.call_center_interactions GROUP BY ROLLUP(reason_category) ORDER BY n DESC

# COMMAND ----------

# MAGIC %md
# MAGIC **Q2.8.** Does an unresolved contact lead to more repeat contacts? (7- and 30-day recontact, within reason)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH b AS (SELECT interaction_id, customer_id, reason_category r, was_resolved wr,
# MAGIC                   unix_timestamp(try_cast(interaction_date AS TIMESTAMP)) t
# MAGIC            FROM workspace.bronze.call_center_interactions),
# MAGIC pairs AS (
# MAGIC   SELECT a.interaction_id,
# MAGIC          max(CASE WHEN c.r = a.r AND c.t - a.t <= 7 * 86400 THEN 1 ELSE 0 END) same7,
# MAGIC          max(CASE WHEN c.t - a.t <= 7 * 86400 THEN 1 ELSE 0 END) any7,
# MAGIC          max(CASE WHEN c.r = a.r THEN 1 ELSE 0 END) same30
# MAGIC   FROM b a JOIN b c ON a.customer_id = c.customer_id AND c.interaction_id <> a.interaction_id
# MAGIC                    AND c.t >= a.t AND c.t - a.t <= 30 * 86400
# MAGIC   GROUP BY a.interaction_id),
# MAGIC f AS (SELECT b.*, coalesce(p.same7, 0) same7, coalesce(p.any7, 0) any7, coalesce(p.same30, 0) same30
# MAGIC       FROM b LEFT JOIN pairs p ON b.interaction_id = p.interaction_id
# MAGIC       WHERE b.t < unix_timestamp(TIMESTAMP'2026-06-11 00:00:00'))
# MAGIC SELECT coalesce(r, 'ALL') reason, count(*) n,
# MAGIC        round(100.0 * avg(same7), 2) same_reason_7d_pct, round(100.0 * avg(any7), 2) any_reason_7d_pct,
# MAGIC        round(100.0 * avg(same30), 2) same_reason_30d_pct,
# MAGIC        round(100.0 * avg(CASE WHEN wr = 'False' THEN same7 END), 2) same7_if_unresolved,
# MAGIC        round(100.0 * avg(CASE WHEN wr = 'True' THEN same7 END), 2) same7_if_resolved
# MAGIC FROM f GROUP BY ROLLUP(r) ORDER BY n DESC

# COMMAND ----------

# MAGIC %md
# MAGIC **Q2.9.** Is there any temporal demand pattern? (contacts per day by weekday; min/max hourly share)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH d AS (SELECT to_date(try_cast(interaction_date AS TIMESTAMP)) dt, count(*) n
# MAGIC            FROM workspace.bronze.call_center_interactions GROUP BY 1)
# MAGIC SELECT 'weekday' dim, date_format(dt, 'E') v, dayofweek(dt) k, count(*) n_days, sum(n) n_contacts, round(avg(n), 1) avg_per_day
# MAGIC FROM d WHERE dt BETWEEN DATE'2023-06-18' AND DATE'2026-06-17' GROUP BY 2, 3
# MAGIC UNION ALL
# MAGIC SELECT 'hour share % (min / max)', NULL, NULL, NULL, min(pct), max(pct)
# MAGIC FROM (SELECT hour(try_cast(interaction_date AS TIMESTAMP)) h, round(100.0 * count(*) / sum(count(*)) OVER (), 2) pct
# MAGIC       FROM workspace.bronze.call_center_interactions GROUP BY 1)
# MAGIC ORDER BY dim, k

# COMMAND ----------

# MAGIC %md
# MAGIC **Q2.10.** Do agent-declared KPIs match realized activity? (suggests the interactions table is a sample)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH a AS (SELECT agent_id, try_cast(total_monthly_interactions AS DOUBLE) declared_monthly FROM workspace.bronze.service_agents),
# MAGIC r AS (SELECT agent_id, count(*) / 36.5 realized_monthly FROM workspace.bronze.call_center_interactions GROUP BY 1)
# MAGIC SELECT count(*) n_agents, count(r.agent_id) agents_with_interactions,
# MAGIC        round(avg(a.declared_monthly), 1) mean_declared_monthly, round(avg(r.realized_monthly), 1) mean_realized_monthly
# MAGIC FROM a LEFT JOIN r ON a.agent_id = r.agent_id

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3 · Complaints and dispute grounding
# MAGIC `complaints` (n = 67,095) and its links to customers, products, transactions and interactions.

# COMMAND ----------

# MAGIC %md
# MAGIC **Q3.1.** What is the complaint mix, and how large is the dispute slice (Transactions + Fees)?

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT 'case_type' dim, case_type lvl, count(*) n, round(100.0 * count(*) / sum(count(*)) OVER (), 2) pct FROM workspace.bronze.complaints GROUP BY case_type
# MAGIC UNION ALL SELECT 'category', category, count(*), round(100.0 * count(*) / sum(count(*)) OVER (), 2) FROM workspace.bronze.complaints GROUP BY category
# MAGIC UNION ALL SELECT 'channel', reception_channel, count(*), round(100.0 * count(*) / sum(count(*)) OVER (), 2) FROM workspace.bronze.complaints GROUP BY reception_channel
# MAGIC UNION ALL SELECT 'priority', priority, count(*), round(100.0 * count(*) / sum(count(*)) OVER (), 2) FROM workspace.bronze.complaints GROUP BY priority
# MAGIC UNION ALL SELECT 'dispute (Transactions + Fees)', CASE WHEN category IN ('Transactions', 'Fees') THEN 'yes' ELSE 'no' END, count(*),
# MAGIC                  round(100.0 * count(*) / sum(count(*)) OVER (), 2) FROM workspace.bronze.complaints GROUP BY 2
# MAGIC UNION ALL SELECT 'dispute (explicit subcategory)', CASE WHEN subcategory IN ('Cargo no reconocido', 'Cobro indebido') THEN 'yes' ELSE 'no' END, count(*),
# MAGIC                  round(100.0 * count(*) / sum(count(*)) OVER (), 2) FROM workspace.bronze.complaints GROUP BY 2
# MAGIC ORDER BY 1, 3 DESC

# COMMAND ----------

# MAGIC %md
# MAGIC **Q3.2.** What is the case-process baseline for disputes vs other categories (backlog, assignment, first response, resolution, completeness)?

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH c AS (
# MAGIC   SELECT CASE WHEN category IN ('Transactions', 'Fees') THEN 'dispute (Transactions+Fees)' ELSE 'other' END grp,
# MAGIC          status, reception_channel, sla_breached, claimed_amount, currency,
# MAGIC          try_cast(creation_date AS TIMESTAMP) cd,
# MAGIC          (unix_timestamp(try_cast(assignment_date AS TIMESTAMP)) - unix_timestamp(try_cast(creation_date AS TIMESTAMP))) / 3600.0 h_assign,
# MAGIC          (unix_timestamp(try_cast(first_response_date AS TIMESTAMP)) - unix_timestamp(try_cast(creation_date AS TIMESTAMP))) / 3600.0 h_first,
# MAGIC          try_cast(resolution_days AS DOUBLE) rd
# MAGIC   FROM workspace.bronze.complaints)
# MAGIC SELECT coalesce(grp, 'ALL') grp, count(*) n,
# MAGIC        round(sum(CASE WHEN cd >= TIMESTAMP'2023-07-01' AND cd < TIMESTAMP'2026-06-01' THEN 1 ELSE 0 END) / 35.0, 1) per_full_month,
# MAGIC        round(100.0 * avg(CASE WHEN status IN ('Open', 'In Process', 'Escalated') THEN 1 ELSE 0 END), 1) pct_open_inproc_esc,
# MAGIC        round(100.0 * avg(CASE WHEN sla_breached = 'True' THEN 1 ELSE 0 END), 1) sla_flag_pct,
# MAGIC        count(h_assign) n_assigned, round(percentile(h_assign, 0.5), 1) assign_p50_h,
# MAGIC        count(h_first) n_first_resp, round(100.0 * count(h_first) / count(*), 1) pct_first_resp,
# MAGIC        round(percentile(h_first, 0.5), 1) first_p50_h, round(percentile(h_first, 0.9), 1) first_p90_h,
# MAGIC        round(100.0 * avg(CASE WHEN h_first IS NOT NULL THEN CASE WHEN h_first > 48 THEN 1.0 ELSE 0.0 END END), 1) pct_first_gt48h,
# MAGIC        count(rd) n_resolved_days, percentile(rd, 0.5) res_p50_d, percentile(rd, 0.9) res_p90_d,
# MAGIC        round(100.0 * avg(CASE WHEN claimed_amount IS NOT NULL THEN 1 ELSE 0 END), 1) pct_with_amount,
# MAGIC        round(100.0 * avg(CASE WHEN claimed_amount IS NOT NULL AND currency IS NOT NULL THEN 1 ELSE 0 END), 1) pct_amount_and_currency,
# MAGIC        round(100.0 * avg(CASE WHEN reception_channel = 'Regulator' THEN 1 ELSE 0 END), 2) pct_regulator
# MAGIC FROM c GROUP BY ROLLUP(grp) ORDER BY n DESC

# COMMAND ----------

# MAGIC %md
# MAGIC **Q3.3.** Does the complaint's `affected_product_id` belong to the complainant? (control: transaction ownership)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH p AS (SELECT product_id, customer_id owner FROM workspace.bronze.products)
# MAGIC SELECT 'complaints.affected_product_id' chk, count(*) n_rows,
# MAGIC        count_if(c.affected_product_id IS NOT NULL) n_nonnull,
# MAGIC        count_if(c.affected_product_id IS NOT NULL AND p.product_id IS NULL) n_not_found,
# MAGIC        count_if(p.owner = c.customer_id) n_owned_by_same_customer
# MAGIC FROM workspace.bronze.complaints c LEFT JOIN p ON c.affected_product_id = p.product_id
# MAGIC UNION ALL
# MAGIC SELECT 'transactions.product_id (control)', count(*), count_if(t.product_id IS NOT NULL),
# MAGIC        count_if(t.product_id IS NOT NULL AND p.product_id IS NULL), count_if(p.owner = t.customer_id)
# MAGIC FROM workspace.bronze.transactions t LEFT JOIN p ON t.product_id = p.product_id

# COMMAND ----------

# MAGIC %md
# MAGIC **Q3.4.** Can complaints be re-linked to the originating contact? (same-day match vs chance expectation)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH c AS (SELECT complaint_id, customer_id, try_cast(process_date AS DATE) c_pd, origin_interaction_id FROM workspace.bronze.complaints),
# MAGIC i AS (SELECT customer_id, try_cast(process_date AS DATE) i_pd FROM workspace.bronze.call_center_interactions),
# MAGIC span AS (SELECT datediff(max(i_pd), min(i_pd)) + 1 ndays FROM i),
# MAGIC cd AS (SELECT customer_id, count(DISTINCT i_pd) n_days FROM i GROUP BY customer_id),
# MAGIC hit AS (SELECT DISTINCT c.complaint_id FROM c JOIN i ON i.customer_id = c.customer_id AND i.i_pd = c.c_pd)
# MAGIC SELECT count(*) n_complaints, count(c.origin_interaction_id) origin_interaction_nonnull,
# MAGIC        count(hit.complaint_id) same_day_contact, round(100.0 * count(hit.complaint_id) / count(*), 2) same_day_pct,
# MAGIC        round(sum(coalesce(cd.n_days, 0) / span.ndays), 1) expected_by_chance,
# MAGIC        round(100.0 * avg(coalesce(cd.n_days, 0) / span.ndays), 2) expected_pct
# MAGIC FROM c CROSS JOIN span LEFT JOIN cd ON c.customer_id = cd.customer_id LEFT JOIN hit ON c.complaint_id = hit.complaint_id

# COMMAND ----------

# MAGIC %md
# MAGIC **Q3.5.** Does any `claimed_amount` match one of the complainant's own transactions?

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH c AS (SELECT complaint_id, customer_id, try_cast(claimed_amount AS DOUBLE) amt, try_cast(creation_date AS TIMESTAMP) cts
# MAGIC            FROM workspace.bronze.complaints WHERE try_cast(claimed_amount AS DOUBLE) IS NOT NULL),
# MAGIC t AS (SELECT customer_id, try_cast(amount AS DOUBLE) amt, try_cast(amount_usd AS DOUBLE) usd, try_cast(transaction_date AS TIMESTAMP) ts
# MAGIC       FROM workspace.bronze.transactions),
# MAGIC m AS (SELECT c.complaint_id,
# MAGIC              max(CASE WHEN abs(t.amt - c.amt) < 0.01 AND t.ts BETWEEN c.cts - INTERVAL 60 DAYS AND c.cts + INTERVAL 1 DAY THEN 1 ELSE 0 END) exact_in_window,
# MAGIC              max(CASE WHEN abs(t.amt - c.amt) < 0.01 THEN 1 ELSE 0 END) exact_any_date,
# MAGIC              max(CASE WHEN abs(t.usd - c.amt) < 0.01 THEN 1 ELSE 0 END) exact_usd_any_date,
# MAGIC              max(CASE WHEN t.ts BETWEEN c.cts - INTERVAL 60 DAYS AND c.cts + INTERVAL 1 DAY THEN 1 ELSE 0 END) any_tx_in_window
# MAGIC       FROM c LEFT JOIN t ON t.customer_id = c.customer_id GROUP BY c.complaint_id)
# MAGIC SELECT count(*) n_with_amount, sum(exact_in_window) exact_in_window, sum(exact_any_date) exact_any_date,
# MAGIC        sum(exact_usd_any_date) exact_usd_any_date, sum(any_tx_in_window) any_tx_in_window
# MAGIC FROM m

# COMMAND ----------

# MAGIC %md
# MAGIC **Q3.6.** Is the 30-day pre-dispute window on the named product different from a placebo window 180-210 days earlier?

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH d AS (SELECT c.complaint_id, c.affected_product_id pid, try_cast(c.creation_date AS TIMESTAMP) cd
# MAGIC            FROM workspace.bronze.complaints c
# MAGIC            WHERE c.subcategory IN ('Cargo no reconocido', 'Cobro indebido')
# MAGIC              AND c.affected_product_id IN (SELECT product_id FROM workspace.bronze.products)
# MAGIC              AND try_cast(c.creation_date AS TIMESTAMP) >= TIMESTAMP'2024-01-01'),
# MAGIC t AS (SELECT product_id, try_cast(transaction_date AS TIMESTAMP) td FROM workspace.bronze.transactions
# MAGIC       WHERE product_id IN (SELECT pid FROM d)),
# MAGIC f AS (SELECT d.complaint_id,
# MAGIC              max(CASE WHEN t.td >= d.cd - INTERVAL 30 DAYS AND t.td < d.cd THEN 1 ELSE 0 END) pre30,
# MAGIC              max(CASE WHEN t.td >= d.cd - INTERVAL 210 DAYS AND t.td < d.cd - INTERVAL 180 DAYS THEN 1 ELSE 0 END) placebo_180_210
# MAGIC       FROM d LEFT JOIN t ON t.product_id = d.pid AND t.td >= d.cd - INTERVAL 210 DAYS AND t.td < d.cd
# MAGIC       GROUP BY d.complaint_id)
# MAGIC SELECT count(*) n_disputes, round(100.0 * avg(pre30), 2) pct_tx_pre30, round(100.0 * avg(placebo_180_210), 2) pct_tx_placebo
# MAGIC FROM f

# COMMAND ----------

# MAGIC %md
# MAGIC **Q3.7.** Is the SLA-breach flag related to anything? (by priority, resolution time and first-response time)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH c AS (SELECT priority, sla_breached, try_cast(resolution_days AS DOUBLE) rd,
# MAGIC                   (unix_timestamp(try_cast(first_response_date AS TIMESTAMP)) - unix_timestamp(try_cast(creation_date AS TIMESTAMP))) / 3600.0 h_fr
# MAGIC            FROM workspace.bronze.complaints)
# MAGIC SELECT 'ALL' dim, 'ALL' lvl, count(*) n, round(100.0 * avg(CASE WHEN sla_breached = 'True' THEN 1 ELSE 0 END), 2) sla_true_pct FROM c
# MAGIC UNION ALL SELECT 'priority', priority, count(*), round(100.0 * avg(CASE WHEN sla_breached = 'True' THEN 1 ELSE 0 END), 2) FROM c GROUP BY 2
# MAGIC UNION ALL SELECT 'resolution days', CASE WHEN rd IS NULL THEN 'unresolved' WHEN rd <= 5 THEN '01-05' WHEN rd <= 10 THEN '06-10'
# MAGIC                                          WHEN rd <= 15 THEN '11-15' WHEN rd <= 20 THEN '16-20' WHEN rd <= 25 THEN '21-25' ELSE '26-30' END,
# MAGIC                  count(*), round(100.0 * avg(CASE WHEN sla_breached = 'True' THEN 1 ELSE 0 END), 2) FROM c GROUP BY 2
# MAGIC UNION ALL SELECT 'first response', CASE WHEN h_fr IS NULL THEN 'none' WHEN h_fr <= 24 THEN '<=24h' WHEN h_fr <= 48 THEN '24-48h' ELSE '>48h' END,
# MAGIC                  count(*), round(100.0 * avg(CASE WHEN sla_breached = 'True' THEN 1 ELSE 0 END), 2) FROM c GROUP BY 2
# MAGIC ORDER BY 1, 2

# COMMAND ----------

# MAGIC %md
# MAGIC **Q3.8.** Does complaint status depend on case age? (status mix by creation year; open cases older than 30 days)

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT coalesce(CAST(year(try_cast(creation_date AS TIMESTAMP)) AS STRING), 'ALL') created, count(*) n,
# MAGIC        round(100.0 * count_if(status IN ('Open', 'In Process')) / count(*), 1) pct_open_inproc,
# MAGIC        round(100.0 * count_if(status IN ('Resolved', 'Closed')) / count(*), 1) pct_resolved_closed,
# MAGIC        count_if(status IN ('Open', 'In Process') AND datediff(DATE'2026-06-17', to_date(try_cast(creation_date AS TIMESTAMP))) > 30) open_older_30d
# MAGIC FROM workspace.bronze.complaints GROUP BY ROLLUP(year(try_cast(creation_date AS TIMESTAMP))) ORDER BY 1

# COMMAND ----------

# MAGIC %md
# MAGIC **Q3.9.** Is complaint text informative? (distinct descriptions and category-subcategory pairs)

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT count(*) n, count(DISTINCT description) distinct_descriptions,
# MAGIC        count(DISTINCT concat_ws('|', description, category)) description_category_pairs,
# MAGIC        count(DISTINCT concat_ws('|', category, subcategory)) category_subcategory_pairs_incl_null,
# MAGIC        count_if(subcategory IS NULL) subcategory_null, count_if(subcategory IS NULL AND category IS NULL) both_null
# MAGIC FROM workspace.bronze.complaints

# COMMAND ----------

# MAGIC %md
# MAGIC **Q3.10.** Does the claim currency follow the customer's country? (claimed amount by currency and home-currency match)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH c AS (SELECT x.currency, try_cast(x.claimed_amount AS DOUBLE) amt, cu.country,
# MAGIC                   CASE cu.country WHEN 'México' THEN 'MXN' WHEN 'Colombia' THEN 'COP' WHEN 'Argentina' THEN 'ARS' END home
# MAGIC            FROM workspace.bronze.complaints x JOIN workspace.bronze.customers cu ON x.customer_id = cu.customer_id
# MAGIC            WHERE x.currency IS NOT NULL)
# MAGIC SELECT coalesce(currency, 'ALL') currency, count(*) n_with_currency, count(amt) n_with_amount,
# MAGIC        round(min(amt), 2) amt_min, round(percentile(amt, 0.5), 2) amt_p50, round(max(amt), 2) amt_max,
# MAGIC        count_if(currency = home) n_home_currency, count_if(currency <> home AND currency <> 'USD') n_neither_home_nor_usd
# MAGIC FROM c GROUP BY ROLLUP(currency) ORDER BY n_with_currency DESC

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4 · Transactions and labels
# MAGIC `transactions` (n = 4,425,008): integrity, timing, fraud label validity, declines and currency.

# COMMAND ----------

# MAGIC %md
# MAGIC **Q4.1.** Is the transaction grain clean? (ownership, duplicates, amounts)

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT 'product owner = transaction customer' chk, count(*) denom, count_if(p.customer_id = t.customer_id) num
# MAGIC FROM workspace.bronze.transactions t LEFT JOIN workspace.bronze.products p ON t.product_id = p.product_id
# MAGIC UNION ALL
# MAGIC SELECT 'duplicate groups on (customer, product, ts, amount)', count(*), count_if(k > 1)
# MAGIC FROM (SELECT count(*) k FROM workspace.bronze.transactions GROUP BY customer_id, product_id, transaction_date, amount)
# MAGIC UNION ALL
# MAGIC SELECT 'amount not castable or <= 0', count(*), count_if(try_cast(amount AS DOUBLE) IS NULL OR try_cast(amount AS DOUBLE) <= 0)
# MAGIC FROM workspace.bronze.transactions

# COMMAND ----------

# MAGIC %md
# MAGIC **Q4.2.** Is `process_date` ever after the event date? (late arrivals vs a business-day cut-off at 06:00 / 08:00)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH d AS (
# MAGIC   SELECT 'transactions' tbl, 6 cutoff_hour, datediff(try_cast(process_date AS DATE), to_date(try_cast(transaction_date AS TIMESTAMP))) lag,
# MAGIC          hour(try_cast(transaction_date AS TIMESTAMP)) hr FROM workspace.bronze.transactions
# MAGIC   UNION ALL SELECT 'call_center_interactions', 8, datediff(try_cast(process_date AS DATE), to_date(try_cast(interaction_date AS TIMESTAMP))),
# MAGIC          hour(try_cast(interaction_date AS TIMESTAMP)) FROM workspace.bronze.call_center_interactions
# MAGIC   UNION ALL SELECT 'complaints', 8, datediff(try_cast(process_date AS DATE), to_date(try_cast(creation_date AS TIMESTAMP))),
# MAGIC          hour(try_cast(creation_date AS TIMESTAMP)) FROM workspace.bronze.complaints)
# MAGIC SELECT tbl, max(cutoff_hour) cutoff_hour, count(*) n, count_if(lag < 0) previous_day, round(100.0 * count_if(lag < 0) / count(*), 2) previous_day_pct,
# MAGIC        count_if(lag > 0) later_day, count_if(lag < 0 AND hr >= cutoff_hour) previous_day_after_cutoff,
# MAGIC        count_if(lag = 0 AND hr < cutoff_hour) same_day_before_cutoff
# MAGIC FROM d GROUP BY tbl ORDER BY tbl

# COMMAND ----------

# MAGIC %md
# MAGIC **Q4.3.** Does the fraud rate vary along any observable dimension? (min/max fraud per 100k per dimension)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH t AS (
# MAGIC   SELECT t.channel, t.transaction_type, t.currency, t.merchant_category, c.country cust_country,
# MAGIC          CAST(hour(try_cast(t.transaction_date AS TIMESTAMP)) AS STRING) hr,
# MAGIC          date_format(try_cast(t.transaction_date AS TIMESTAMP), 'yyyy-MM') ym,
# MAGIC          CASE WHEN t.is_fraud = 'True' THEN 1 ELSE 0 END y
# MAGIC   FROM workspace.bronze.transactions t LEFT JOIN workspace.bronze.customers c ON c.customer_id = t.customer_id),
# MAGIC long AS (SELECT stack(7, 'channel', channel, 'type', transaction_type, 'currency', currency, 'merchant_category', merchant_category,
# MAGIC                       'customer_country', cust_country, 'hour', hr, 'month', ym) AS (dim, val), y FROM t),
# MAGIC agg AS (SELECT dim, coalesce(val, '<NULL>') val, count(*) n, sum(y) f, 100000.0 * sum(y) / count(*) r FROM long GROUP BY 1, 2)
# MAGIC SELECT dim, count(*) n_values, sum(n) total_n, sum(f) total_fraud,
# MAGIC        round(min(r), 1) min_per_100k, min_by(concat(val, ' ', f, '/', n), r) min_cell,
# MAGIC        round(max(r), 1) max_per_100k, max_by(concat(val, ' ', f, '/', n), r) max_cell
# MAGIC FROM agg GROUP BY dim ORDER BY dim

# COMMAND ----------

# MAGIC %md
# MAGIC **Q4.4.** Is fraud dispersion across cells larger than Bernoulli noise? (chi-square vs degrees of freedom)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH base AS (
# MAGIC   SELECT channel, transaction_type, coalesce(merchant_category, '<NULL>') mc, transaction_country tc,
# MAGIC          date_format(try_cast(transaction_date AS TIMESTAMP), 'yyyy-MM') ym, hour(try_cast(transaction_date AS TIMESTAMP)) hr,
# MAGIC          CASE WHEN is_fraud = 'True' THEN 1 ELSE 0 END y
# MAGIC   FROM workspace.bronze.transactions),
# MAGIC cells4 AS (SELECT channel, transaction_type, mc, tc, count(*) n, sum(y) f FROM base GROUP BY 1, 2, 3, 4),
# MAGIC pall AS (SELECT sum(y) / count(*) p FROM base),
# MAGIC m AS (SELECT ym, count(*) n, sum(y) f FROM base GROUP BY ym),
# MAGIC h AS (SELECT hr, count(*) n, sum(y) f FROM base GROUP BY hr)
# MAGIC SELECT 'channel x type x merchant x country (n >= 5000)' test, count(*) - 1 df, round(sum(power(f - n * p, 2) / (n * p * (1 - p))), 1) chi2
# MAGIC FROM cells4 CROSS JOIN pall WHERE n >= 5000
# MAGIC UNION ALL SELECT 'month', count(*) - 1, round(sum(power(f - n * p, 2) / (n * p * (1 - p))), 1) FROM m CROSS JOIN pall
# MAGIC UNION ALL SELECT 'hour', count(*) - 1, round(sum(power(f - n * p, 2) / (n * p * (1 - p))), 1) FROM h CROSS JOIN pall

# COMMAND ----------

# MAGIC %md
# MAGIC **Q4.5.** Can fraud be predicted from features known at transaction time? (temporal test AUC of a target-encoded cell model; train < 2025-07-01)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH raw AS (
# MAGIC   SELECT t.channel ch, t.transaction_type ty, coalesce(t.merchant_category, '<NULL>') mc,
# MAGIC          translate(t.transaction_country, 'é', 'e') tc, translate(c.country, 'é', 'e') cc,
# MAGIC          try_cast(t.amount AS DOUBLE) / CASE t.currency WHEN 'ARS' THEN 350.0 WHEN 'COP' THEN 4000.0 ELSE 1.0 END usd,
# MAGIC          try_cast(t.transaction_date AS TIMESTAMP) ts, CASE WHEN t.is_fraud = 'True' THEN 1 ELSE 0 END y
# MAGIC   FROM workspace.bronze.transactions t LEFT JOIN workspace.bronze.customers c ON c.customer_id = t.customer_id),
# MAGIC f AS (SELECT ch, ty, mc, tc, CASE WHEN usd < 200 THEN 'lo' WHEN usd < 3000 THEN 'mid' ELSE 'hi' END ab,
# MAGIC              CASE WHEN hour(ts) < 6 THEN 'night' ELSE 'day' END nb, CASE WHEN tc <> cc THEN 'x' ELSE 'd' END xb, y,
# MAGIC              CASE WHEN ts < TIMESTAMP'2025-07-01 00:00:00' THEN 'train' ELSE 'test' END split
# MAGIC       FROM raw),
# MAGIC pr AS (SELECT sum(y) / count(*) p0 FROM f WHERE split = 'train'),
# MAGIC cell AS (SELECT ch, ty, mc, tc, ab, nb, xb, (sum(y) + 200 * max(p0)) / (count(*) + 200) s
# MAGIC          FROM f CROSS JOIN pr WHERE split = 'train' GROUP BY 1, 2, 3, 4, 5, 6, 7),
# MAGIC sc AS (SELECT round(coalesce(cell.s, (SELECT p0 FROM pr)), 9) s, f.y
# MAGIC        FROM f LEFT JOIN cell ON f.ch = cell.ch AND f.ty = cell.ty AND f.mc = cell.mc AND f.tc = cell.tc
# MAGIC                             AND f.ab = cell.ab AND f.nb = cell.nb AND f.xb = cell.xb
# MAGIC        WHERE f.split = 'test'),
# MAGIC g AS (SELECT s, sum(y) pos, count(*) - sum(y) neg FROM sc GROUP BY s),
# MAGIC r AS (SELECT s, pos, neg, sum(neg) OVER (ORDER BY s RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - neg nlow FROM g)
# MAGIC SELECT sum(pos) test_pos, sum(neg) test_neg, round(sum(pos * (nlow + 0.5 * neg)) / (sum(pos) * sum(neg)), 4) auc_test
# MAGIC FROM r

# COMMAND ----------

# MAGIC %md
# MAGIC **Q4.6.** Does `fraud_score` leak the label? (score distribution by label)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH x AS (SELECT is_fraud = 'True' y, try_cast(fraud_score AS DOUBLE) s FROM workspace.bronze.transactions)
# MAGIC SELECT y is_fraud, count(*) n, count(s) n_scored, round(100.0 * (count(*) - count(s)) / count(*), 2) score_null_pct,
# MAGIC        min(s) mn, round(percentile(s, 0.5), 2) p50, max(s) mx,
# MAGIC        sum(CASE WHEN s > 30 THEN 1 ELSE 0 END) above_30, sum(CASE WHEN s >= 40 THEN 1 ELSE 0 END) at_or_above_40
# MAGIC FROM x GROUP BY y

# COMMAND ----------

# MAGIC %md
# MAGIC **Q4.7.** How does `fraud_score` rank fraud (AUC) and what precision/recall do thresholds give?

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH x AS (SELECT CASE WHEN is_fraud = 'True' THEN 1 ELSE 0 END y, try_cast(fraud_score AS DOUBLE) s FROM workspace.bronze.transactions),
# MAGIC g AS (SELECT s, sum(y) pos, count(*) - sum(y) neg FROM x WHERE s IS NOT NULL GROUP BY s),
# MAGIC r AS (SELECT s, pos, neg, sum(neg) OVER (ORDER BY s RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - neg nlow FROM g),
# MAGIC auc AS (SELECT round(sum(pos * (nlow + 0.5 * neg)) / (sum(pos) * sum(neg)), 4) auc FROM r),
# MAGIC thr AS (SELECT explode(array(30.0, 30.005, 40.0, 50.0, 70.0, 90.0)) t)
# MAGIC SELECT CAST(t AS STRING) threshold, sum(CASE WHEN s >= t AND y = 1 THEN 1 ELSE 0 END) tp, sum(CASE WHEN s >= t AND y = 0 THEN 1 ELSE 0 END) fp,
# MAGIC        round(sum(CASE WHEN s >= t AND y = 1 THEN 1 ELSE 0 END) / sum(CASE WHEN s >= t THEN 1 ELSE 0 END), 4) precision,
# MAGIC        round(100.0 * sum(CASE WHEN s >= t AND y = 1 THEN 1 ELSE 0 END) / sum(y), 1) recall_all_fraud_pct,
# MAGIC        auc.auc auc_scored_rows
# MAGIC FROM x CROSS JOIN thr CROSS JOIN auc GROUP BY t, auc.auc ORDER BY t

# COMMAND ----------

# MAGIC %md
# MAGIC **Q4.8.** Are decline response codes informative? (code mix within each status)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH g AS (SELECT transaction_status st, coalesce(response_code, '<NULL>') rc, count(*) n FROM workspace.bronze.transactions GROUP BY 1, 2)
# MAGIC SELECT st, rc, n, round(100.0 * n / sum(n) OVER (PARTITION BY st), 2) pct_within_status, sum(n) OVER (PARTITION BY st) n_status
# MAGIC FROM g ORDER BY st, n DESC

# COMMAND ----------

# MAGIC %md
# MAGIC **Q4.9.** Do fraud, declines or reversals trigger a contact within 7 days?

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH ev AS (
# MAGIC   SELECT transaction_id, customer_id, try_cast(transaction_date AS TIMESTAMP) ts,
# MAGIC          CASE WHEN is_fraud = 'True' THEN 'fraud' WHEN transaction_status = 'Declined' THEN 'declined'
# MAGIC               WHEN transaction_status = 'Reversed' THEN 'reversed' ELSE 'other' END ev_type
# MAGIC   FROM workspace.bronze.transactions),
# MAGIC i AS (SELECT customer_id, try_cast(interaction_date AS TIMESTAMP) its FROM workspace.bronze.call_center_interactions),
# MAGIC hit AS (SELECT DISTINCT ev.transaction_id FROM ev JOIN i ON i.customer_id = ev.customer_id
# MAGIC           AND i.its > ev.ts AND i.its <= ev.ts + INTERVAL 7 DAYS)
# MAGIC SELECT ev.ev_type, count(*) n, count(hit.transaction_id) with_contact_7d,
# MAGIC        round(100.0 * count(hit.transaction_id) / count(*), 2) pct_contact_7d
# MAGIC FROM ev LEFT JOIN hit ON hit.transaction_id = ev.transaction_id GROUP BY ev.ev_type ORDER BY ev.ev_type

# COMMAND ----------

# MAGIC %md
# MAGIC **Q4.10.** Which currencies does each country hold and transact in, and is `amount_usd` a fixed or a daily rate?

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT 'products' src, c.country, p.currency, count(*) n,
# MAGIC        CAST(NULL AS BIGINT) amount_usd_null, CAST(NULL AS BIGINT) amount_usd_equals_fixed_rate
# MAGIC FROM workspace.bronze.products p LEFT JOIN workspace.bronze.customers c ON p.customer_id = c.customer_id GROUP BY 2, 3
# MAGIC UNION ALL
# MAGIC SELECT 'transactions', c.country, t.currency, count(*), count_if(t.amount_usd IS NULL),
# MAGIC        count_if(t.currency IN ('ARS', 'COP') AND abs(try_cast(t.amount_usd AS DOUBLE)
# MAGIC                 - round(try_cast(t.amount AS DOUBLE) / CASE t.currency WHEN 'ARS' THEN 350.0 ELSE 4000.0 END, 2)) <= 0.011)
# MAGIC FROM workspace.bronze.transactions t LEFT JOIN workspace.bronze.customers c ON t.customer_id = c.customer_id GROUP BY 2, 3
# MAGIC ORDER BY 1, 2, 3

# COMMAND ----------

# MAGIC %md
# MAGIC **Q4.11.** How many candidate transactions would an intake flow search? (own transactions in the 30 days before three reference dates)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH ref AS (SELECT explode(array(DATE'2024-03-15', DATE'2025-03-15', DATE'2026-03-15')) d),
# MAGIC cust AS (SELECT DISTINCT customer_id FROM workspace.bronze.products),
# MAGIC t AS (SELECT customer_id, to_date(try_cast(transaction_date AS TIMESTAMP)) td FROM workspace.bronze.transactions),
# MAGIC k AS (SELECT ref.d, cust.customer_id, count(t.td) n30
# MAGIC       FROM ref CROSS JOIN cust
# MAGIC       LEFT JOIN t ON t.customer_id = cust.customer_id AND t.td > date_sub(ref.d, 30) AND t.td <= ref.d
# MAGIC       GROUP BY ref.d, cust.customer_id)
# MAGIC SELECT d ref_date, count(*) n_customers, round(100.0 * avg(CASE WHEN n30 > 0 THEN 1 ELSE 0 END), 1) pct_with_tx_30d,
# MAGIC        percentile(n30, 0.5) p50, percentile(n30, 0.9) p90, percentile(n30, 0.99) p99, max(n30) mx
# MAGIC FROM k GROUP BY d ORDER BY d

# COMMAND ----------

# MAGIC %md
# MAGIC **Q4.12.** How many transactions have a physically implausible type x channel combination?

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT coalesce(concat(transaction_type, ' via ', channel), 'ALL implausible') combination, count(*) n,
# MAGIC        round(100.0 * count(*) / (SELECT count(*) FROM workspace.bronze.transactions), 2) pct_of_all_transactions
# MAGIC FROM workspace.bronze.transactions
# MAGIC WHERE (transaction_type = 'Withdrawal' AND channel IN ('POS', 'Web', 'App', 'Transfer'))
# MAGIC    OR (transaction_type = 'Deposit' AND channel = 'POS')
# MAGIC    OR (transaction_type = 'Purchase' AND channel IN ('ATM', 'Transfer', 'Branch'))
# MAGIC GROUP BY ROLLUP(concat(transaction_type, ' via ', channel)) ORDER BY n DESC

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5 · Products, credit and cards
# MAGIC `products` (n = 400,000) and `customers` (n = 150,000).

# COMMAND ----------

# MAGIC %md
# MAGIC **Q5.1.** Product book: type mix and status mix per type.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT product_type, count(*) n, round(100.0 * count(*) / sum(count(*)) OVER (), 2) pct_of_book,
# MAGIC        round(100.0 * avg(CASE WHEN product_status = 'Active' THEN 1 ELSE 0 END), 2) pct_active,
# MAGIC        round(100.0 * avg(CASE WHEN product_status = 'Blocked' THEN 1 ELSE 0 END), 2) pct_blocked,
# MAGIC        round(100.0 * avg(CASE WHEN product_status = 'Closed' THEN 1 ELSE 0 END), 2) pct_closed,
# MAGIC        round(100.0 * avg(CASE WHEN has_linked_app = 'True' THEN 1 ELSE 0 END), 2) pct_linked_app
# MAGIC FROM workspace.bronze.products GROUP BY 1 ORDER BY 2 DESC

# COMMAND ----------

# MAGIC %md
# MAGIC **Q5.2.** Which product statuses have any transactions?

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH t AS (SELECT product_id, count(*) ntx FROM workspace.bronze.transactions GROUP BY 1)
# MAGIC SELECT coalesce(p.product_status, 'ALL') status, count(*) n_products, count(t.product_id) n_with_tx,
# MAGIC        round(100.0 * count(t.product_id) / count(*), 2) pct_with_tx, sum(coalesce(t.ntx, 0)) total_tx
# MAGIC FROM workspace.bronze.products p LEFT JOIN t ON p.product_id = t.product_id
# MAGIC GROUP BY ROLLUP(p.product_status) ORDER BY n_products DESC

# COMMAND ----------

# MAGIC %md
# MAGIC **Q5.3.** How many Active cards are already past their expiration date at data end (2026-06-17)?

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT product_type, count_if(product_status = 'Active') n_active,
# MAGIC        count_if(product_status = 'Active' AND try_cast(expiration_date AS DATE) IS NOT NULL) active_with_expiry,
# MAGIC        count_if(product_status = 'Active' AND try_cast(expiration_date AS DATE) < DATE'2026-06-17') active_but_expired,
# MAGIC        min(try_cast(expiration_date AS DATE)) exp_min, max(try_cast(expiration_date AS DATE)) exp_max
# MAGIC FROM workspace.bronze.products WHERE product_type IN ('Tarjeta Crédito', 'Tarjeta Débito') GROUP BY 1

# COMMAND ----------

# MAGIC %md
# MAGIC **Q5.4.** Does `products.last_transaction_date` match the real last transaction?

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH tx AS (SELECT product_id, max(to_date(try_cast(transaction_date AS TIMESTAMP))) last_tx FROM workspace.bronze.transactions GROUP BY product_id),
# MAGIC p AS (SELECT product_id, try_cast(last_transaction_date AS DATE) ltd FROM workspace.bronze.products)
# MAGIC SELECT count_if(tx.product_id IS NOT NULL AND p.ltd IS NOT NULL) n_both, count_if(p.ltd = tx.last_tx) n_equal,
# MAGIC        count_if(abs(datediff(p.ltd, tx.last_tx)) <= 1) n_within_1d, percentile(abs(datediff(p.ltd, tx.last_tx)), 0.5) median_abs_gap_days,
# MAGIC        count_if(tx.product_id IS NOT NULL AND p.ltd IS NULL) n_tx_but_null_field
# MAGIC FROM p LEFT JOIN tx ON p.product_id = tx.product_id

# COMMAND ----------

# MAGIC %md
# MAGIC **Q5.5.** Does delinquency (dpd > 90) depend on credit score, segment or country?

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH cu AS (SELECT customer_id, segment, country, try_cast(credit_score AS DOUBLE) score FROM workspace.bronze.customers),
# MAGIC cp AS (SELECT try_cast(p.days_past_due AS DOUBLE) dpd, cu.segment, cu.country, cu.score
# MAGIC        FROM workspace.bronze.products p LEFT JOIN cu ON p.customer_id = cu.customer_id
# MAGIC        WHERE p.product_type IN ('Tarjeta Crédito', 'Préstamo Personal', 'Préstamo Hipotecario')
# MAGIC          AND try_cast(p.days_past_due AS DOUBLE) IS NOT NULL)
# MAGIC SELECT 'all' dim, 'all' lvl, count(*) n, sum(IF(dpd > 90, 1, 0)) n_dpd90, round(100.0 * avg(IF(dpd > 90, 1, 0)), 2) pct_dpd90 FROM cp
# MAGIC UNION ALL SELECT 'credit score', CASE WHEN score IS NULL THEN 'null' WHEN score < 580 THEN 'a <580' WHEN score < 650 THEN 'b 580-649'
# MAGIC                                       WHEN score < 720 THEN 'c 650-719' WHEN score < 780 THEN 'd 720-779' ELSE 'e 780+' END,
# MAGIC                  count(*), sum(IF(dpd > 90, 1, 0)), round(100.0 * avg(IF(dpd > 90, 1, 0)), 2) FROM cp GROUP BY 2
# MAGIC UNION ALL SELECT 'segment', coalesce(segment, 'null'), count(*), sum(IF(dpd > 90, 1, 0)), round(100.0 * avg(IF(dpd > 90, 1, 0)), 2) FROM cp GROUP BY 2
# MAGIC UNION ALL SELECT 'country', coalesce(country, 'null'), count(*), sum(IF(dpd > 90, 1, 0)), round(100.0 * avg(IF(dpd > 90, 1, 0)), 2) FROM cp GROUP BY 2
# MAGIC ORDER BY 1, 2

# COMMAND ----------

# MAGIC %md
# MAGIC **Q5.6.** How well does credit score alone rank delinquency? (rank AUC of -credit_score for dpd > 90)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH cp AS (SELECT IF(try_cast(p.days_past_due AS DOUBLE) > 90, 1, 0) y, -try_cast(c.credit_score AS DOUBLE) s
# MAGIC             FROM workspace.bronze.products p JOIN workspace.bronze.customers c ON p.customer_id = c.customer_id
# MAGIC             WHERE p.product_type IN ('Tarjeta Crédito', 'Préstamo Personal', 'Préstamo Hipotecario')
# MAGIC               AND try_cast(p.days_past_due AS DOUBLE) IS NOT NULL AND c.credit_score IS NOT NULL),
# MAGIC r AS (SELECT y, rank() OVER (ORDER BY s) rk, count(*) OVER (PARTITION BY s) ties FROM cp),
# MAGIC r2 AS (SELECT y, rk + (ties - 1) / 2.0 ar FROM r)
# MAGIC SELECT sum(y) n_pos, count(*) - sum(y) n_neg,
# MAGIC        round((sum(IF(y = 1, ar, 0)) - sum(y) * (sum(y) + 1) / 2.0) / (sum(y) * (count(*) - sum(y))), 4) auc
# MAGIC FROM r2

# COMMAND ----------

# MAGIC %md
# MAGIC **Q5.7.** Do credit limits and rates depend on income or score? (correlations within product x currency cells)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH x AS (SELECT p.product_type, p.currency, try_cast(p.credit_limit AS DOUBLE) lim, try_cast(p.interest_rate AS DOUBLE) rate,
# MAGIC                   try_cast(c.credit_score AS DOUBLE) score, try_cast(c.estimated_monthly_income AS DOUBLE) inc
# MAGIC            FROM workspace.bronze.products p JOIN workspace.bronze.customers c ON p.customer_id = c.customer_id
# MAGIC            WHERE p.product_type IN ('Tarjeta Crédito', 'Préstamo Personal', 'Préstamo Hipotecario'))
# MAGIC SELECT product_type, currency, count(*) n,
# MAGIC        round(corr(IF(lim > 0, ln(lim), NULL), IF(inc > 0, ln(inc), NULL)), 3) r_loglimit_logincome,
# MAGIC        round(corr(lim, score), 3) r_limit_score, round(corr(rate, score), 3) r_rate_score,
# MAGIC        min(rate) rate_min, round(percentile(rate, 0.5), 2) rate_p50, max(rate) rate_max
# MAGIC FROM x GROUP BY 1, 2 ORDER BY 1, 2

# COMMAND ----------

# MAGIC %md
# MAGIC **Q5.8.** Is `customer_status` (Closed/Inactive) related to service experience or activity?

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH cm AS (SELECT customer_id, count(*) ncmp FROM workspace.bronze.complaints GROUP BY 1),
# MAGIC it AS (SELECT customer_id, count(*) nint, sum(IF(was_resolved = 'False', 1, 0)) nunres FROM workspace.bronze.call_center_interactions GROUP BY 1),
# MAGIC p AS (SELECT customer_id, sum(IF(product_status = 'Active', 1, 0)) nact FROM workspace.bronze.products GROUP BY 1),
# MAGIC b AS (SELECT c.customer_status, IF(c.customer_status IN ('Closed', 'Inactive'), 1, 0) y,
# MAGIC              coalesce(cm.ncmp, 0) ncmp, coalesce(it.nint, 0) nint, coalesce(it.nunres, 0) nunres, coalesce(p.nact, 0) nact
# MAGIC       FROM workspace.bronze.customers c LEFT JOIN cm ON c.customer_id = cm.customer_id
# MAGIC       LEFT JOIN it ON c.customer_id = it.customer_id LEFT JOIN p ON c.customer_id = p.customer_id)
# MAGIC SELECT 'all' dim, 'all' lvl, count(*) n, round(100.0 * avg(y), 2) pct_closed_inactive FROM b
# MAGIC UNION ALL SELECT 'complaints', CASE WHEN ncmp = 0 THEN '0' WHEN ncmp = 1 THEN '1' ELSE '2+' END, count(*), round(100.0 * avg(y), 2) FROM b GROUP BY 2
# MAGIC UNION ALL SELECT 'unresolved contacts (callers)', CASE WHEN nunres = 0 THEN '0' WHEN nunres = 1 THEN '1' WHEN nunres = 2 THEN '2' ELSE '3+' END,
# MAGIC                  count(*), round(100.0 * avg(y), 2) FROM b WHERE nint > 0 GROUP BY 2
# MAGIC UNION ALL SELECT 'Closed customers with an Active product', NULL, count_if(customer_status = 'Closed'),
# MAGIC                  round(100.0 * count_if(customer_status = 'Closed' AND nact > 0) / count_if(customer_status = 'Closed'), 1) FROM b
# MAGIC ORDER BY 1, 2

# COMMAND ----------

# MAGIC %md
# MAGIC **Q5.9.** How complete are the inputs a credit-eligibility policy would need?

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT coalesce(segment, 'ALL') segment, count(*) n,
# MAGIC        round(100.0 * avg(IF(credit_score IS NULL, 1, 0)), 2) pct_score_null,
# MAGIC        round(100.0 * avg(IF(estimated_monthly_income IS NULL, 1, 0)), 2) pct_income_null,
# MAGIC        round(100.0 * avg(IF(credit_score IS NULL OR estimated_monthly_income IS NULL, 1, 0)), 2) pct_either_null
# MAGIC FROM workspace.bronze.customers GROUP BY ROLLUP(segment) ORDER BY n DESC

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6 · Data-quality checks behind the proposed Silver rules

# COMMAND ----------

# MAGIC %md
# MAGIC **Q6.1.** Documented foreign keys: null share and orphans (non-null values with no parent).

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH cu AS (SELECT DISTINCT customer_id k FROM workspace.bronze.customers),
# MAGIC br AS (SELECT DISTINCT branch_id k FROM workspace.bronze.branches),
# MAGIC ag AS (SELECT DISTINCT agent_id k FROM workspace.bronze.service_agents),
# MAGIC pr AS (SELECT DISTINCT product_id k FROM workspace.bronze.products),
# MAGIC it AS (SELECT DISTINCT interaction_id k FROM workspace.bronze.call_center_interactions),
# MAGIC ca AS (SELECT DISTINCT campaign_id k FROM workspace.bronze.marketing_campaigns),
# MAGIC fk_cu AS (SELECT 'products.customer_id' fk, customer_id v FROM workspace.bronze.products
# MAGIC           UNION ALL SELECT 'transactions.customer_id', customer_id FROM workspace.bronze.transactions
# MAGIC           UNION ALL SELECT 'call_center_interactions.customer_id', customer_id FROM workspace.bronze.call_center_interactions
# MAGIC           UNION ALL SELECT 'call_transcripts.customer_id', customer_id FROM workspace.bronze.call_transcripts
# MAGIC           UNION ALL SELECT 'satisfaction_surveys.customer_id', customer_id FROM workspace.bronze.satisfaction_surveys
# MAGIC           UNION ALL SELECT 'complaints.customer_id', customer_id FROM workspace.bronze.complaints
# MAGIC           UNION ALL SELECT 'campaign_sends.customer_id', customer_id FROM workspace.bronze.campaign_sends),
# MAGIC fk_br AS (SELECT 'customers.registration_branch_id' fk, registration_branch_id v FROM workspace.bronze.customers
# MAGIC           UNION ALL SELECT 'products.opening_branch_id', opening_branch_id FROM workspace.bronze.products
# MAGIC           UNION ALL SELECT 'service_agents.assigned_branch_id', assigned_branch_id FROM workspace.bronze.service_agents
# MAGIC           UNION ALL SELECT 'transactions.branch_id', branch_id FROM workspace.bronze.transactions
# MAGIC           UNION ALL SELECT 'complaints.related_branch_id', related_branch_id FROM workspace.bronze.complaints),
# MAGIC fk_ag AS (SELECT 'call_center_interactions.agent_id' fk, agent_id v FROM workspace.bronze.call_center_interactions
# MAGIC           UNION ALL SELECT 'call_transcripts.agent_id', agent_id FROM workspace.bronze.call_transcripts
# MAGIC           UNION ALL SELECT 'satisfaction_surveys.agent_id', agent_id FROM workspace.bronze.satisfaction_surveys
# MAGIC           UNION ALL SELECT 'complaints.assigned_agent_id', assigned_agent_id FROM workspace.bronze.complaints),
# MAGIC fk_pr AS (SELECT 'transactions.product_id' fk, product_id v FROM workspace.bronze.transactions
# MAGIC           UNION ALL SELECT 'complaints.affected_product_id', affected_product_id FROM workspace.bronze.complaints),
# MAGIC fk_it AS (SELECT 'call_transcripts.interaction_id' fk, interaction_id v FROM workspace.bronze.call_transcripts
# MAGIC           UNION ALL SELECT 'satisfaction_surveys.interaction_id', interaction_id FROM workspace.bronze.satisfaction_surveys
# MAGIC           UNION ALL SELECT 'complaints.origin_interaction_id', origin_interaction_id FROM workspace.bronze.complaints),
# MAGIC fk_ca AS (SELECT 'campaign_sends.campaign_id' fk, campaign_id v FROM workspace.bronze.campaign_sends)
# MAGIC SELECT fk, count(*) n, count_if(v IS NULL) n_null, round(100.0 * count_if(v IS NULL) / count(*), 2) null_pct,
# MAGIC        count_if(v IS NOT NULL AND cu.k IS NULL) orphans FROM fk_cu LEFT JOIN cu ON fk_cu.v = cu.k GROUP BY fk
# MAGIC UNION ALL SELECT fk, count(*), count_if(v IS NULL), round(100.0 * count_if(v IS NULL) / count(*), 2), count_if(v IS NOT NULL AND br.k IS NULL)
# MAGIC           FROM fk_br LEFT JOIN br ON fk_br.v = br.k GROUP BY fk
# MAGIC UNION ALL SELECT fk, count(*), count_if(v IS NULL), round(100.0 * count_if(v IS NULL) / count(*), 2), count_if(v IS NOT NULL AND ag.k IS NULL)
# MAGIC           FROM fk_ag LEFT JOIN ag ON fk_ag.v = ag.k GROUP BY fk
# MAGIC UNION ALL SELECT fk, count(*), count_if(v IS NULL), round(100.0 * count_if(v IS NULL) / count(*), 2), count_if(v IS NOT NULL AND pr.k IS NULL)
# MAGIC           FROM fk_pr LEFT JOIN pr ON fk_pr.v = pr.k GROUP BY fk
# MAGIC UNION ALL SELECT fk, count(*), count_if(v IS NULL), round(100.0 * count_if(v IS NULL) / count(*), 2), count_if(v IS NOT NULL AND it.k IS NULL)
# MAGIC           FROM fk_it LEFT JOIN it ON fk_it.v = it.k GROUP BY fk
# MAGIC UNION ALL SELECT fk, count(*), count_if(v IS NULL), round(100.0 * count_if(v IS NULL) / count(*), 2), count_if(v IS NOT NULL AND ca.k IS NULL)
# MAGIC           FROM fk_ca LEFT JOIN ca ON fk_ca.v = ca.k GROUP BY fk
# MAGIC ORDER BY orphans DESC, fk

# COMMAND ----------

# MAGIC %md
# MAGIC **Q6.2.** Do interaction `mentioned_products` point to real products owned by the caller?

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH tok AS (SELECT i.customer_id, trim(x) pid FROM workspace.bronze.call_center_interactions i
# MAGIC              LATERAL VIEW explode(split(i.mentioned_products, ',')) e AS x
# MAGIC              WHERE i.mentioned_products IS NOT NULL AND trim(i.mentioned_products) <> ''),
# MAGIC p AS (SELECT product_id, customer_id owner FROM workspace.bronze.products)
# MAGIC SELECT count(*) n_tokens, count(DISTINCT pid) n_distinct, count_if(p.product_id IS NULL) not_in_products,
# MAGIC        count_if(p.product_id IS NOT NULL) existing, count_if(p.owner = tok.customer_id) owned_by_caller
# MAGIC FROM tok LEFT JOIN p ON tok.pid = p.product_id

# COMMAND ----------

# MAGIC %md
# MAGIC **Q6.3.** Which identifiers can authenticate a customer? (sharing of email, mobile, document number, product number)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH e AS (SELECT lower(trim(email)) k, count(*) n FROM workspace.bronze.customers WHERE email IS NOT NULL GROUP BY 1),
# MAGIC m AS (SELECT regexp_replace(mobile_phone, '[^0-9]', '') k, count(*) n FROM workspace.bronze.customers WHERE mobile_phone IS NOT NULL GROUP BY 1),
# MAGIC d AS (SELECT document_number k, count(*) n FROM workspace.bronze.customers GROUP BY 1),
# MAGIC pn AS (SELECT product_number k, count(*) n, count(DISTINCT customer_id) nc FROM workspace.bronze.products WHERE product_number IS NOT NULL GROUP BY 1)
# MAGIC SELECT 'email' key_col, sum(n) rows_nonnull, sum(CASE WHEN n > 1 THEN n ELSE 0 END) rows_sharing, count_if(n > 1) shared_groups, max(n) max_group FROM e
# MAGIC UNION ALL SELECT 'mobile_phone (digits)', sum(n), sum(CASE WHEN n > 1 THEN n ELSE 0 END), count_if(n > 1), max(n) FROM m
# MAGIC UNION ALL SELECT 'document_number', sum(n), sum(CASE WHEN n > 1 THEN n ELSE 0 END), count_if(n > 1), max(n) FROM d
# MAGIC UNION ALL SELECT 'product_number', sum(n), sum(CASE WHEN n > 1 THEN n ELSE 0 END), count_if(n > 1 AND nc > 1), max(n) FROM pn

# COMMAND ----------

# MAGIC %md
# MAGIC **Q6.4.** Document type by country (identity-validation rules).

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT country, document_type, count(*) n FROM workspace.bronze.customers GROUP BY 1, 2 ORDER BY 1, 2

# COMMAND ----------

# MAGIC %md
# MAGIC **Q6.5.** Are there content-level duplicates on natural keys? (the announced ~2%)

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH k AS (
# MAGIC   SELECT 'transactions (customer, product, ts, amount)' nk, count(*) cnt FROM workspace.bronze.transactions GROUP BY customer_id, product_id, transaction_date, amount
# MAGIC   UNION ALL SELECT 'interactions (customer, ts, agent, reason)', count(*) FROM workspace.bronze.call_center_interactions GROUP BY customer_id, interaction_date, agent_id, reason_category
# MAGIC   UNION ALL SELECT 'complaints (customer, creation_date, category, subcategory)', count(*) FROM workspace.bronze.complaints GROUP BY customer_id, creation_date, category, subcategory
# MAGIC   UNION ALL SELECT 'surveys (interaction_id, survey_type)', count(*) FROM workspace.bronze.satisfaction_surveys GROUP BY interaction_id, survey_type
# MAGIC   UNION ALL SELECT 'transcripts (interaction_id)', count(*) FROM workspace.bronze.call_transcripts GROUP BY interaction_id
# MAGIC   UNION ALL SELECT 'customers (document_number)', count(*) FROM workspace.bronze.customers GROUP BY document_number)
# MAGIC SELECT nk, sum(cnt) rows_total, count_if(cnt > 1) dup_groups, sum(CASE WHEN cnt > 1 THEN cnt - 1 ELSE 0 END) extra_rows
# MAGIC FROM k GROUP BY nk ORDER BY nk

# COMMAND ----------

# MAGIC %md
# MAGIC **Q6.6.** Which fields copy the label? (transcript topics vs reason; sentiment label vs score bins)

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT 'transcripts.main_topics = interactions.reason_category' chk, count(*) n, count_if(tr.main_topics = i.reason_category) n_equal
# MAGIC FROM workspace.bronze.call_transcripts tr JOIN workspace.bronze.call_center_interactions i ON tr.interaction_id = i.interaction_id
# MAGIC UNION ALL
# MAGIC SELECT 'detected_sentiment = bin(sentiment_score) off the +-0.3 / +-0.7 cut points', count_if(abs(sc) NOT IN (0.3, 0.7)),
# MAGIC        count_if(abs(sc) NOT IN (0.3, 0.7) AND ds = CASE WHEN sc < -0.7 THEN 'Muy Negativo' WHEN sc < -0.3 THEN 'Negativo' WHEN sc <= 0.3 THEN 'Neutral'
# MAGIC                                                      WHEN sc <= 0.7 THEN 'Positivo' ELSE 'Muy Positivo' END)
# MAGIC FROM (SELECT detected_sentiment ds, try_cast(sentiment_score AS DOUBLE) sc FROM workspace.bronze.call_center_interactions)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7 · Negative controls for the learned component
# MAGIC Historical labels were tested before choosing the learned component. Each one either carries no learnable signal or leaks the label:
# MAGIC
# MAGIC | Candidate label | Evidence | Cell |
# MAGIC |---|---|---|
# MAGIC | `is_fraud` | temporal test AUC 0.504; fraud rate flat on every dimension tested | Q4.3, Q4.4, Q4.5 |
# MAGIC | `fraud_score` as feature | precision 1.000 above 30: built from the label | Q4.6, Q4.7 |
# MAGIC | `days_past_due > 90` | AUC 0.504 against credit score; flat by segment and country | Q5.5, Q5.6 |
# MAGIC | FCR per agent | dispersion equals binomial noise | Q2.5 |
# MAGIC | CSAT / NPS / CES | depend only on `was_resolved` (same score split shifted by one point; NPS ranges do not overlap) | Q2.3 |
# MAGIC | Complaint `sla_breached` | flat 18.5-21.4% across priority, resolution time and first response | Q3.7 |
# MAGIC | Complaint `description`, transcript `main_topics` | copies of category / reason | Q3.9, Q6.6 |
# MAGIC
# MAGIC The learned component therefore uses ES/PT intake messages with labels valid by construction (see section 10 of the report).
