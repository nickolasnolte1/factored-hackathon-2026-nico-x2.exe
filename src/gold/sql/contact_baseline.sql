-- Gold · contact_baseline: the current-state baseline of report 02 (section 9), recomputed from Silver on every build,
-- one row per metric, so the evaluation compares the agent's results with live numbers instead of copied ones.
-- Spec: gold_tables.json. Sources: silver.call_center_interactions, silver.satisfaction_surveys, silver.complaints
-- (pinned Delta versions in the job). Aggregates only: no customer-level row leaves this query.
--   * Contact level: reason Queja is the dispute-intake proxy (an assumption: calls cannot be linked to cases).
--     FY2 = event_ts in [2024-07-01, 2025-07-01). Handle time exists only for voice and video.
--   * Case level: disputes = complaints with a dispute_type (Transactions -> Cargo no reconocido, Fees -> Cobro
--     indebido). Per month = cases created from 2023-07-01 to 2026-05-31 (35 full months) / 35.
--   * report_value is the figure published in section 9 (and the all-contacts FCR of section 2); the job flags any
--     metric that moved by more than report_tolerance (warn check reconcile:report_section_9).
--   * Percentiles are exact (percentile), as in the EDA queries Q2.1 and Q3.2. Shares are in percent.
WITH i AS (
  SELECT interaction_id, reason_category, was_resolved, was_escalated, requires_followup, duration_seconds,
         event_ts >= TIMESTAMP '2024-07-01' AND event_ts < TIMESTAMP '2025-07-01'                    AS fy2
  FROM {call_center_interactions}
),
alls AS (
  SELECT count(*) AS n, count_if(was_resolved) AS resolved, count_if(NOT was_resolved) AS unresolved,
         sum(duration_seconds) / 3600.0 AS handle_h
  FROM i
),
q AS (
  SELECT count(*)                                                              AS n,
         count_if(was_resolved)                                                AS resolved,
         count_if(was_resolved AND NOT was_escalated AND NOT requires_followup) AS strict_resolved,
         count_if(NOT was_resolved)                                            AS unresolved,
         count(duration_seconds)                                               AS n_dur,
         percentile(duration_seconds, 0.5)                                     AS aht_p50,
         percentile(duration_seconds, 0.9)                                     AS aht_p90,
         sum(duration_seconds) / 3600.0                                        AS handle_h,
         count_if(fy2)                                                         AS n_fy2,
         count_if(fy2 AND was_resolved)                                        AS resolved_fy2,
         count_if(fy2 AND duration_seconds IS NOT NULL)                        AS n_dur_fy2,
         sum(CASE WHEN fy2 THEN duration_seconds END) / 3600.0                 AS handle_h_fy2
  FROM i
  WHERE reason_category = 'Queja'
),
csat AS (
  SELECT count(*) AS n, avg(s.main_score) AS mean, count_if(s.main_score <= 2) AS low
  FROM {satisfaction_surveys} s
  JOIN i ON i.interaction_id = s.interaction_id
  WHERE s.survey_type = 'CSAT' AND i.reason_category = 'Queja'
),
c AS (
  SELECT dispute_type IS NOT NULL                                              AS is_dispute,
         status, event_ts, claimed_amount, currency, affected_product_id, affected_product_id_raw, link_status,
         (unix_timestamp(assignment_date) - unix_timestamp(event_ts)) / 3600.0     AS h_assign,
         (unix_timestamp(first_response_date) - unix_timestamp(event_ts)) / 3600.0 AS h_first
  FROM {complaints}
),
d AS (
  SELECT count(*)                                                                            AS n,
         count_if(event_ts >= TIMESTAMP '2023-07-01' AND event_ts < TIMESTAMP '2026-06-01') AS n_full_months,
         count_if(status IN ('Open', 'In Process', 'Escalated'))                            AS backlog,
         count(h_assign)                                                                     AS n_assign,
         percentile(h_assign, 0.5)                                                           AS assign_p50,
         count(h_first)                                                                      AS n_first,
         percentile(h_first, 0.5)                                                            AS first_p50,
         percentile(h_first, 0.9)                                                            AS first_p90,
         count_if(h_first > 48)                                                              AS first_gt48,
         count_if(claimed_amount IS NOT NULL AND currency IS NOT NULL)                       AS amount_currency
  FROM c
  WHERE is_dispute
),
ca AS (
  SELECT count(*) AS n, count(affected_product_id_raw) AS n_product, count(affected_product_id) AS n_owned,
         count_if(link_status = 'linked') AS n_linked
  FROM c
),
m (metric_id, metric_group, description, population, period, unit, value, numerator, denominator, n_observations,
   report_value, report_tolerance, target_outcome, source_tables) AS (
  SELECT 'queja_contacts', 'contact', 'Contacts with reason Queja', 'all contacts', 'all', 'count',
         q.n, q.n, NULL, q.n, 117021, 0, NULL, 'silver.call_center_interactions' FROM q
  UNION ALL
  SELECT 'queja_contact_share', 'contact', 'Share of all contacts with reason Queja', 'all contacts', 'all', 'pct',
         100.0 * q.n / alls.n, q.n, alls.n, alls.n, 17.1, 0.05, NULL, 'silver.call_center_interactions' FROM q, alls
  UNION ALL
  SELECT 'queja_fcr', 'contact', 'First-contact resolution (was_resolved) of Queja contacts', 'Queja contacts', 'all', 'pct',
         100.0 * q.resolved / q.n, q.resolved, q.n, q.n, 43.6, 0.05,
         'First-contact case completion >= 80% of in-scope scenarios (held-out ES/PT set)', 'silver.call_center_interactions' FROM q
  UNION ALL
  SELECT 'queja_fcr_fy2', 'contact', 'First-contact resolution of Queja contacts in FY2', 'Queja contacts', 'FY2', 'pct',
         100.0 * q.resolved_fy2 / q.n_fy2, q.resolved_fy2, q.n_fy2, q.n_fy2, 43.8, 0.05, NULL,
         'silver.call_center_interactions' FROM q
  UNION ALL
  SELECT 'queja_strict_fcr', 'contact', 'Resolved, not escalated and no follow-up (secondary; both flags are rule-generated)',
         'Queja contacts', 'all', 'pct',
         100.0 * q.strict_resolved / q.n, q.strict_resolved, q.n, q.n, 33.2, 0.05, NULL, 'silver.call_center_interactions' FROM q
  UNION ALL
  SELECT 'queja_share_of_unresolved', 'contact', 'Share of all unresolved contacts that are Queja', 'unresolved contacts', 'all', 'pct',
         100.0 * q.unresolved / alls.unresolved, q.unresolved, alls.unresolved, alls.unresolved, 41.2, 0.05, NULL,
         'silver.call_center_interactions' FROM q, alls
  UNION ALL
  SELECT 'queja_aht_p50_s', 'contact', 'Handle time p50 of Queja contacts (voice and video only)', 'Queja contacts with duration',
         'all', 'seconds', q.aht_p50, NULL, NULL, q.n_dur, 431, 0.5, 'Intake duration median below this value',
         'silver.call_center_interactions' FROM q
  UNION ALL
  SELECT 'queja_aht_p90_s', 'contact', 'Handle time p90 of Queja contacts (voice and video only)', 'Queja contacts with duration',
         'all', 'seconds', q.aht_p90, NULL, NULL, q.n_dur, 608, 0.5, NULL, 'silver.call_center_interactions' FROM q
  UNION ALL
  SELECT 'queja_handle_hours', 'contact', 'Handle-hours of Queja contacts', 'Queja contacts with duration', 'all', 'hours',
         q.handle_h, NULL, NULL, q.n_dur, 12160, 0.5, NULL, 'silver.call_center_interactions' FROM q
  UNION ALL
  SELECT 'queja_handle_hours_share', 'contact', 'Share of all handle-hours spent on Queja contacts', 'contacts with duration',
         'all', 'pct', 100.0 * q.handle_h / alls.handle_h, q.handle_h, alls.handle_h, q.n_dur, 23.1, 0.05, NULL,
         'silver.call_center_interactions' FROM q, alls
  UNION ALL
  SELECT 'queja_handle_hours_fy2', 'contact', 'Handle-hours of Queja contacts in FY2', 'Queja contacts with duration', 'FY2',
         'hours', q.handle_h_fy2, NULL, NULL, q.n_dur_fy2, 4039, 0.5, NULL, 'silver.call_center_interactions' FROM q
  UNION ALL
  SELECT 'queja_csat_mean', 'contact', 'Mean CSAT of Queja contacts on the 1-4 scale present in the data (a consequence of FCR)',
         'CSAT surveys of Queja contacts', 'all', 'score_1_4', csat.mean, NULL, NULL, csat.n, 2.43, 0.005, NULL,
         'silver.satisfaction_surveys, silver.call_center_interactions' FROM csat
  UNION ALL
  SELECT 'queja_csat_low_share', 'contact', 'Share of Queja CSAT scores <= 2', 'CSAT surveys of Queja contacts', 'all', 'pct',
         100.0 * csat.low / csat.n, csat.low, csat.n, csat.n, 54.5, 0.05, NULL,
         'silver.satisfaction_surveys, silver.call_center_interactions' FROM csat
  UNION ALL
  SELECT 'all_contacts_fcr', 'contact', 'First-contact resolution of all contacts (context)', 'all contacts', 'all', 'pct',
         100.0 * alls.resolved / alls.n, alls.resolved, alls.n, alls.n, 76.6, 0.05, NULL, 'silver.call_center_interactions' FROM alls
  UNION ALL
  SELECT 'dispute_cases', 'case', 'Dispute cases (Transactions + Fees)', 'complaints', 'all', 'count',
         d.n, d.n, NULL, d.n, 27133, 0, NULL, 'silver.complaints' FROM d
  UNION ALL
  SELECT 'dispute_cases_per_month', 'case', 'Dispute cases per full month', 'dispute cases', '2023-07 to 2026-05', 'cases_per_month',
         d.n_full_months / 35.0, d.n_full_months, 35, d.n_full_months, 753.8, 0.05, NULL, 'silver.complaints' FROM d
  UNION ALL
  SELECT 'dispute_backlog_share', 'case', 'Dispute cases still Open, In Process or Escalated', 'dispute cases', 'all', 'pct',
         100.0 * d.backlog / d.n, d.backlog, d.n, d.n, 74.9, 0.05, NULL, 'silver.complaints' FROM d
  UNION ALL
  SELECT 'dispute_assignment_p50_h', 'case', 'Time from creation to assignment, p50', 'dispute cases with an assignment date',
         'all', 'hours', d.assign_p50, NULL, NULL, d.n_assign, 12.0, 0.05, NULL, 'silver.complaints' FROM d
  UNION ALL
  SELECT 'dispute_first_response_coverage', 'case', 'Dispute cases with a first response', 'dispute cases', 'all', 'pct',
         100.0 * d.n_first / d.n, d.n_first, d.n, d.n, 61.3, 0.05, NULL, 'silver.complaints' FROM d
  UNION ALL
  SELECT 'dispute_first_response_p50_h', 'case', 'Time from creation to first response, p50', 'dispute cases with a first response',
         'all', 'hours', d.first_p50, NULL, NULL, d.n_first, 37, 0.5, 'Case number and next step given within the conversation',
         'silver.complaints' FROM d
  UNION ALL
  SELECT 'dispute_first_response_p90_h', 'case', 'Time from creation to first response, p90', 'dispute cases with a first response',
         'all', 'hours', d.first_p90, NULL, NULL, d.n_first, 58, 0.5, NULL, 'silver.complaints' FROM d
  UNION ALL
  SELECT 'dispute_first_response_over_48h', 'case', 'Share of first responses later than 48 h', 'dispute cases with a first response',
         'all', 'pct', 100.0 * d.first_gt48 / d.n_first, d.first_gt48, d.n_first, d.n_first, 26.2, 0.05, NULL, 'silver.complaints' FROM d
  UNION ALL
  SELECT 'dispute_amount_currency_present', 'case', 'Dispute cases with both a claimed amount and a currency', 'dispute cases',
         'all', 'pct', 100.0 * d.amount_currency / d.n, d.amount_currency, d.n, d.n, 31.4, 0.05,
         'Required fields (transaction, owned product, amount, currency) present in 100% of created cases', 'silver.complaints' FROM d
  UNION ALL
  SELECT 'complaint_product_owned_share', 'case', 'Complaint product links that belong to the complainant',
         'complaints with a product id', 'all', 'pct', 100.0 * ca.n_owned / ca.n_product, ca.n_owned, ca.n_product, ca.n_product,
         0.0, 0, '0 grounding violations; correct transaction linked in >= 95% of completed cases', 'silver.complaints' FROM ca
  UNION ALL
  SELECT 'complaint_linked_share', 'case', 'Complaints linked to the originating contact', 'complaints', 'all', 'pct',
         100.0 * ca.n_linked / ca.n, ca.n_linked, ca.n, ca.n, 0.0, 0, NULL, 'silver.complaints' FROM ca
)
SELECT
  metric_id,
  metric_group,
  description,
  population,
  period,
  unit,
  CAST(value AS DOUBLE)            AS value,
  CAST(numerator AS DOUBLE)        AS numerator,
  CAST(denominator AS DOUBLE)      AS denominator,
  CAST(n_observations AS BIGINT)   AS n_observations,
  CAST(report_value AS DOUBLE)     AS report_value,
  CAST(report_tolerance AS DOUBLE) AS report_tolerance,
  target_outcome,
  source_tables
FROM m
