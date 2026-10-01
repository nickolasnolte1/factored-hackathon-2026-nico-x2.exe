-- Scenario anchors: real customers, products and transactions sampled from workspace.silver (read-only).
-- Run with `python -m src.scenarios.extract_anchors` (it fills the placeholders, pins the Delta versions and writes
-- data/scenarios/). Sections: `-- @cte <name>` blocks are prepended as one WITH clause to every `-- @query <name>`.
-- Placeholders: {seed} {split_date} {transactions} {products} {customers} {transactions_quarantine}
--               {threshold_usd} {high_usd} {medium_usd} {panel_active} {panel_restricted}
-- Rules:
--   * Owner-consistent: the product belongs to the transaction's customer (product_owner_matches) and the join
--     also requires products.customer_id = transactions.customer_id. Rows in the transactions quarantine are excluded.
--   * Narratable: rows flagged implausible_type_channel (report issue 18) are never anchors (the e2e panel keeps
--     them, because the agent's tools return them and must omit the channel).
--   * Splits are fixed here: customer hash bucket (sha256 of '<seed>:<customer_id>', first 8 hex digits, mod 100)
--     0-69 train, 70-84 dev, 85-99 test; train/dev anchors have event_date < split_date, test anchors >= split_date.
--     generate.py recomputes the bucket in Python and asserts it matches.
--   * Customers: Active/Inactive ('active' group) and Closed/Suspended ('restricted' group) are sampled separately.
--   * No personal data: ids, amounts, dates, merchant, channel, product type, country codes and statuses only.

-- @cte cust
cust AS (
  SELECT
    customer_id,
    country_code                                                                              AS customer_country_code,
    customer_status,
    CAST(conv(substr(sha2(concat('{seed}', ':', customer_id), 256), 1, 8), 16, 10) AS BIGINT) % 100 AS customer_bucket
  FROM {customers}
)
-- @cte cust_split
cust_split AS (
  SELECT
    *,
    CASE WHEN customer_bucket < 70 THEN 'train' WHEN customer_bucket < 85 THEN 'dev' ELSE 'test' END AS split,
    CASE WHEN customer_status IN ('Closed', 'Suspended') THEN 'restricted' ELSE 'active' END       AS status_group
  FROM cust
)
-- @cte txn
txn AS (
  SELECT
    t.transaction_id, t.customer_id, t.product_id, t.event_ts, t.event_date, t.transaction_type, t.transaction_category,
    t.amount, t.currency, t.amount_usd, t.channel, t.merchant_name, t.merchant_category, t.transaction_country_code,
    t.is_international, t.transaction_status, t.response_code, t.response_code_null_reason, t.implausible_type_channel,
    p.product_type, p.product_type_en, p.product_status, p.effective_status, p.expiration_date,
    c.customer_country_code, c.customer_status, c.customer_bucket, c.split, c.status_group
  FROM {transactions} t
  JOIN {products} p ON p.product_id = t.product_id AND p.customer_id = t.customer_id
  JOIN cust_split c ON c.customer_id = t.customer_id
  LEFT ANTI JOIN {transactions_quarantine} q ON q.transaction_id = t.transaction_id
  WHERE t.product_owner_matches
)
-- @cte kinds
kinds AS (
  SELECT
    *,
    CASE
      WHEN transaction_type = 'Purchase' AND merchant_name IS NOT NULL AND transaction_status = 'Approved' THEN 'purchase_approved'
      WHEN transaction_type = 'Purchase' AND merchant_name IS NOT NULL AND transaction_status = 'Declined' THEN 'purchase_declined'
      WHEN transaction_type = 'Purchase' AND merchant_name IS NOT NULL AND transaction_status = 'Pending'  THEN 'purchase_pending'
      WHEN transaction_type = 'Purchase' AND merchant_name IS NOT NULL AND transaction_status = 'Reversed' THEN 'purchase_reversed'
      WHEN transaction_type = 'Withdrawal' AND transaction_status = 'Approved'                            THEN 'withdrawal_approved'
      WHEN transaction_type = 'Transfer' AND transaction_status = 'Approved'
           AND product_type_en IN ('Savings Account', 'Checking Account')                                 THEN 'transfer_approved'
      WHEN transaction_type = 'Transfer' AND transaction_status = 'Pending'
           AND product_type_en IN ('Savings Account', 'Checking Account')                                 THEN 'transfer_pending'
      WHEN transaction_type = 'Deposit' AND transaction_status IN ('Approved', 'Pending')                 THEN 'deposit'
      WHEN transaction_type = 'Payment' AND transaction_status = 'Approved'                               THEN 'payment_approved'
      WHEN transaction_type = 'Adjustment' AND transaction_status = 'Approved'                            THEN 'adjustment_approved'
      WHEN transaction_status = 'Declined'                                                                THEN 'other_declined'
      WHEN transaction_status = 'Pending'                                                                 THEN 'other_pending'
    END                                                   AS anchor_kind,
    sha2(concat('{seed}', ':', transaction_id), 256)      AS anchor_hash
  FROM txn
  WHERE NOT implausible_type_channel
    AND ((split IN ('train', 'dev') AND event_date < DATE '{split_date}') OR (split = 'test' AND event_date >= DATE '{split_date}'))
)
-- @cte quota
quota AS (
  SELECT * FROM VALUES
    ('train', 'active', 2000, 400), ('dev', 'active', 600, 150), ('test', 'active', 600, 150),
    ('train', 'restricted', 150, 30), ('dev', 'restricted', 150, 30), ('test', 'restricted', 150, 30)
    AS quota(split, status_group, n_purchase, n_other)
)
-- @cte panel_stats
panel_stats AS (
  SELECT
    c.customer_id, c.customer_country_code, c.customer_status, c.customer_bucket, c.split, c.status_group,
    count_if(t.in_period)                                                                             AS n_period,
    count_if(t.in_period AND t.transaction_type = 'Purchase' AND t.transaction_status = 'Approved') AS n_purchase
  FROM cust_split c
  JOIN (
    SELECT tt.customer_id, tt.transaction_type, tt.transaction_status,
           (cs.split = 'dev' AND tt.event_date < DATE '{split_date}')
           OR (cs.split = 'test' AND tt.event_date >= DATE '{split_date}')                            AS in_period
    FROM {transactions} tt
    JOIN cust_split cs ON cs.customer_id = tt.customer_id
    WHERE tt.product_owner_matches AND cs.split IN ('dev', 'test')
  ) t ON t.customer_id = c.customer_id
  WHERE c.split IN ('dev', 'test') AND c.customer_status IN ('Active', 'Closed', 'Suspended')
  GROUP BY ALL
)
-- @cte panel
panel AS (
  SELECT * FROM (
    SELECT s.*, row_number() OVER (PARTITION BY split, status_group
                                   ORDER BY sha2(concat('{seed}', ':panel:', customer_id), 256)) AS panel_rank
    FROM panel_stats s
    WHERE n_period >= 8 AND n_purchase >= 2
  )
  WHERE panel_rank <= CASE status_group WHEN 'active' THEN {panel_active} ELSE {panel_restricted} END
)

-- @query anchors
-- Intent-dataset anchors: one row per sampled transaction, at most one per customer and kind, stratified by
-- kind x split x status group with the quotas above.
SELECT
  r.transaction_id, r.customer_id, r.product_id,
  date_format(r.event_ts, 'yyyy-MM-dd HH:mm:ss') AS event_ts, CAST(r.event_date AS STRING) AS event_date,
  r.transaction_type, r.transaction_category, r.amount, r.currency, r.amount_usd, r.channel,
  r.merchant_name, r.merchant_category, r.transaction_country_code, r.is_international, r.transaction_status,
  r.response_code, r.response_code_null_reason,
  r.product_type, r.product_type_en, r.product_status, r.effective_status, CAST(r.expiration_date AS STRING) AS expiration_date,
  r.customer_country_code, r.customer_status, r.customer_bucket, r.split, r.status_group, r.anchor_kind
FROM (
  SELECT k.*, row_number() OVER (PARTITION BY anchor_kind, split, status_group ORDER BY anchor_hash) AS kind_rank
  FROM (
    SELECT k0.*, row_number() OVER (PARTITION BY customer_id, anchor_kind ORDER BY anchor_hash) AS customer_rank
    FROM kinds k0
    WHERE anchor_kind IS NOT NULL
  ) k
  WHERE customer_rank = 1
) r
JOIN quota q ON q.split = r.split AND q.status_group = r.status_group
WHERE r.kind_rank <= CASE WHEN r.anchor_kind = 'purchase_approved' THEN q.n_purchase ELSE q.n_other END
ORDER BY r.split, r.anchor_kind, r.status_group, r.kind_rank

-- @query panel_customers
-- End-to-end panel: dev/test customers (never train) with enough activity in their split's period.
SELECT customer_id, customer_country_code, customer_status, customer_bucket, split, status_group, n_period, n_purchase
FROM panel
ORDER BY split, status_group, panel_rank

-- @query panel_products
SELECT
  p.product_id, p.customer_id, p.product_type, p.product_type_en, p.currency, p.current_balance, p.credit_limit,
  p.product_status, p.effective_status, CAST(p.expiration_date AS STRING) AS expiration_date, p.has_linked_app
FROM {products} p
JOIN panel USING (customer_id)
ORDER BY p.customer_id, p.product_id

-- @query panel_transactions
-- Full history of the panel customers: what the agent's transactions tool returns (it must filter by the session clock).
SELECT
  t.transaction_id, t.customer_id, t.product_id,
  date_format(t.event_ts, 'yyyy-MM-dd HH:mm:ss') AS event_ts, CAST(t.event_date AS STRING) AS event_date,
  t.transaction_type, t.transaction_category, t.amount, t.currency, t.amount_usd, t.channel, t.merchant_name,
  t.merchant_category, t.transaction_country_code, t.is_international, t.transaction_status, t.response_code,
  t.response_code_null_reason, t.implausible_type_channel
FROM {transactions} t
JOIN panel USING (customer_id)
LEFT ANTI JOIN {transactions_quarantine} q ON q.transaction_id = t.transaction_id
WHERE t.product_owner_matches
ORDER BY t.customer_id, t.event_ts, t.transaction_id

-- @query calibration
-- Handoff threshold calibration on the dispute-eligible anchor population (all dates, all customers).
SELECT
  count(*)                                                                  AS n_rows,
  percentile(amount_usd, 0.50)                                              AS p50_usd,
  percentile(amount_usd, 0.75)                                              AS p75_usd,
  percentile(amount_usd, 0.90)                                              AS p90_usd,
  percentile(amount_usd, 0.95)                                              AS p95_usd,
  avg(CASE WHEN amount_usd > {threshold_usd} THEN 1.0 ELSE 0.0 END)         AS share_above_threshold,
  avg(CASE WHEN amount_usd >= {high_usd} THEN 1.0 ELSE 0.0 END)             AS share_at_or_above_high,
  avg(CASE WHEN amount_usd >= {medium_usd} THEN 1.0 ELSE 0.0 END)           AS share_at_or_above_medium
FROM txn
WHERE transaction_status = 'Approved'
  AND transaction_type IN ('Purchase', 'Withdrawal', 'Transfer', 'Payment', 'Adjustment')
  AND NOT implausible_type_channel
