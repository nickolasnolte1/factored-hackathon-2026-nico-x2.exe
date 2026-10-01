-- Silver · complaints: formal complaint / claim cases, one row per case (67,095 rows). Contract: contracts/complaints.json.
-- Placeholders are filled by silver_lib.render_sql; `python silver_lib.py sql complaints` prints the runnable query.
-- Rules (docs/02_eda_workflow_selection.md, section 6). Historical cases size demand and the timing baseline only:
-- they can never ground a dispute or supply a label.
--   * Issue 1:  origin_interaction_id is NULL in 67,095 / 67,095 rows -> FLAG link_status = 'unlinked'. No fuzzy re-linking
--               (same-day contact 0.46% vs 0.42% by chance).
--   * Issue 2:  affected_product_id belongs to someone else in 44,570 / 44,570 rows -> QUARANTINE the value:
--               affected_product_id is NULL unless the complainant owns the product, raw kept, FLAG product_not_owned.
--   * Issue 3:  claimed_amount / currency are random (0 / 21,751 amounts match a transaction of the customer) -> FLAG
--               claim_untraceable. Never FX-convert; the intake takes amount and currency from the selected transaction.
--   * Issue 4:  compensation_granted holds amounts -> FIX compensation_amount + compensation_flag. sla_breached is random
--               -> FIX sla_derived from the dates. Status never ages -> FLAG stale_status. None of these are labels.
--   * Issue 5:  subcategory is a 1:1 function of category -> FIX impute the 9.98% NULLs, FLAG subcategory_imputed.
--               description is one fixed string per category: kept for display, label-leaking, never a feature.
--   * Issue 19: event_ts / event_date come from creation_date; process_date is a business-day cut-off.
-- Ownership and traceability are checked against Bronze products and transactions (typed as in their Silver SQL), so
-- the table builds on its own. As-of date for ageing: 2026-06-18, the last day of the dataset.
-- De-duplicated on (customer, creation time, category, subcategory); 0 duplicate groups in the delivered data.
WITH params AS (
  SELECT DATE '2026-06-18' AS as_of_date
),
src AS (
  SELECT *
  FROM {catalog}.{bronze}.complaints
  WHERE _ingested_at > TIMESTAMP '{watermark}'
),
typed AS (
  SELECT
    upper(trim(complaint_id))                                                     AS complaint_id,
    try_cast(trim(creation_date) AS TIMESTAMP)                                    AS event_ts,
    try_cast(trim(process_date) AS DATE)                                          AS process_date,
    upper(trim(customer_id))                                                      AS customer_id,
    initcap(lower(trim(case_type)))                                               AS case_type,
    initcap(lower(trim(category)))                                                AS category,
    CASE translate(lower(trim(subcategory)), 'áéíóú', 'aeiou')
      WHEN 'cargo no reconocido' THEN 'Cargo no reconocido' WHEN 'cobro indebido' THEN 'Cobro indebido'
      WHEN 'problema con app' THEN 'Problema con app' WHEN 'atencion en sucursal' THEN 'Atención en sucursal'
      WHEN 'calidad de servicio' THEN 'Calidad de servicio'
      ELSE nullif(trim(subcategory), '') END                                      AS subcategory_raw,
    initcap(lower(trim(reception_channel)))                                       AS reception_channel,
    upper(nullif(trim(affected_product_id), ''))                                  AS affected_product_id_raw,
    upper(nullif(trim(related_branch_id), ''))                                    AS related_branch_id,
    upper(nullif(trim(origin_interaction_id), ''))                                AS origin_interaction_id,
    trim(description)                                                             AS description,
    try_cast(trim(claimed_amount) AS DECIMAL(18,2))                               AS claimed_amount,
    upper(nullif(trim(currency), ''))                                             AS currency,
    initcap(lower(trim(priority)))                                                AS priority,
    initcap(lower(trim(status)))                                                  AS status,
    upper(nullif(trim(assigned_agent_id), ''))                                    AS assigned_agent_id,
    try_cast(trim(assignment_date) AS TIMESTAMP)                                  AS assignment_date,
    try_cast(trim(first_response_date) AS TIMESTAMP)                              AS first_response_date,
    try_cast(trim(resolution_date) AS TIMESTAMP)                                  AS resolution_date,
    try_cast(trim(closing_date) AS TIMESTAMP)                                     AS closing_date,
    try_cast(trim(sla_breached) AS BOOLEAN)                                       AS sla_breached,
    try_cast(regexp_replace(trim(resolution_days), '[.]0+$', '') AS INT)          AS resolution_days,
    nullif(trim(resolution), '')                                                  AS resolution,
    try_cast(trim(compensation_granted) AS DECIMAL(18,2))                         AS compensation_amount,
    try_cast(regexp_replace(trim(resolution_satisfaction), '[.]0+$', '') AS INT)  AS resolution_satisfaction,
    try_cast(trim(is_repeat_complainer) AS BOOLEAN)                               AS is_repeat_complainer,
    _source_file,
    _ingested_at,
    struct(src.*)                                                                 AS _bronze
  FROM src
),
imputed AS (
  SELECT *,
    coalesce(subcategory_raw, CASE category
      WHEN 'Transactions' THEN 'Cargo no reconocido' WHEN 'Fees' THEN 'Cobro indebido'
      WHEN 'Technical' THEN 'Problema con app' WHEN 'Branch' THEN 'Atención en sucursal'
      WHEN 'Service' THEN 'Calidad de servicio' END)                              AS subcategory
  FROM typed
),
dedup AS (
  SELECT *
  FROM imputed
  QUALIFY row_number() OVER (PARTITION BY customer_id, event_ts, category, subcategory
                             ORDER BY _ingested_at DESC, _source_file DESC) = 1
),
products AS (
  SELECT upper(trim(product_id)) AS product_id, upper(trim(customer_id)) AS owner_id
  FROM {catalog}.{bronze}.products
  QUALIFY row_number() OVER (PARTITION BY upper(trim(product_id)) ORDER BY _ingested_at DESC, _source_file DESC) = 1
),
claims AS (
  SELECT DISTINCT customer_id, claimed_amount, currency
  FROM dedup
  WHERE claimed_amount IS NOT NULL
),
tx_amounts AS (  -- every amount a customer was charged, in the transaction currency and in USD
  SELECT upper(trim(customer_id)) AS customer_id, try_cast(trim(amount) AS DECIMAL(18,2)) AS amount,
         upper(trim(currency)) AS currency
  FROM {catalog}.{bronze}.transactions
  UNION ALL
  SELECT upper(trim(customer_id)), try_cast(trim(amount_usd) AS DECIMAL(18,2)), 'USD'
  FROM {catalog}.{bronze}.transactions
  WHERE nullif(trim(amount_usd), '') IS NOT NULL
),
traced AS (
  SELECT DISTINCT c.customer_id, c.claimed_amount, c.currency
  FROM claims c
  JOIN tx_amounts t
    ON t.customer_id = c.customer_id AND t.amount = c.claimed_amount AND (c.currency IS NULL OR t.currency = c.currency)
),
derived AS (
  SELECT d.*, p.as_of_date,
    d.affected_product_id_raw IS NOT NULL AND (pr.owner_id IS NULL OR pr.owner_id <> d.customer_id) AS product_not_owned,
    (d.claimed_amount IS NOT NULL OR d.currency IS NOT NULL) AND tr.customer_id IS NULL        AS claim_untraceable,
    d.status IN ('Open', 'In Process', 'Escalated')                                            AS is_open,
    coalesce(d.first_response_date, d.resolution_date, d.closing_date)                         AS responded_at,
    coalesce(d.resolution_date, d.closing_date)                                                AS resolved_at
  FROM dedup d
  CROSS JOIN params p
  LEFT JOIN products pr ON pr.product_id = d.affected_product_id_raw
  LEFT JOIN traced tr
    ON tr.customer_id = d.customer_id AND tr.claimed_amount = d.claimed_amount AND tr.currency <=> d.currency
)
SELECT
  complaint_id, event_ts, to_date(event_ts) AS event_date, process_date, customer_id,
  case_type, category, subcategory,
  subcategory_raw IS NULL AND subcategory IS NOT NULL                                AS subcategory_imputed,
  CASE category WHEN 'Transactions' THEN 'unrecognized_charge'
                WHEN 'Fees' THEN 'incorrect_charge_or_fee' END                       AS dispute_type,
  reception_channel,
  CASE WHEN NOT product_not_owned THEN affected_product_id_raw END                   AS affected_product_id,
  affected_product_id_raw, product_not_owned,
  related_branch_id, origin_interaction_id,
  CASE WHEN origin_interaction_id IS NULL THEN 'unlinked' ELSE 'linked' END          AS link_status,
  description, claimed_amount, currency, claim_untraceable,
  priority, status, assigned_agent_id, assignment_date, first_response_date,
  round(CAST(unix_timestamp(first_response_date) - unix_timestamp(event_ts) AS DOUBLE) / 3600, 2) AS first_response_hours,
  resolution_date, closing_date, resolution_days, resolution,
  sla_breached,
  CASE  -- targets: first response within 48 h, resolution within 30 days; open cases are aged at the end of as_of_date
    WHEN responded_at IS NOT NULL AND responded_at > event_ts + INTERVAL 48 HOURS THEN true
    WHEN resolved_at IS NOT NULL AND datediff(to_date(resolved_at), to_date(event_ts)) > 30 THEN true
    WHEN is_open AND responded_at IS NULL
         AND CAST(date_add(as_of_date, 1) AS TIMESTAMP) > event_ts + INTERVAL 48 HOURS THEN true
    WHEN is_open AND datediff(as_of_date, to_date(event_ts)) > 30 THEN true
    WHEN responded_at IS NOT NULL OR is_open THEN false
  END                                                                                AS sla_derived,
  coalesce(is_open AND datediff(as_of_date, to_date(event_ts)) > 30, false)          AS stale_status,
  compensation_amount,
  coalesce(compensation_amount > 0, false)                                           AS compensation_flag,
  resolution_satisfaction, is_repeat_complainer,
  _source_file, _ingested_at, _bronze
FROM derived
