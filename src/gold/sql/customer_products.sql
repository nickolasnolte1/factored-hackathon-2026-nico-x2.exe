-- Gold · customer_products: the customer's products as the agent may quote them (no full product numbers).
-- Spec: gold_tables.json. Sources: silver.products, silver.transactions (pinned Delta versions in the job).
--   * Issue 6: effective_status ('Expired' for Active cards past expiry) is the status to quote; product_status is
--     the delivered value.
--   * Issues 7 and 8: first and last movement, transaction count and effective_opening_date are recomputed here from
--     the same silver.transactions version as gold.customer_transactions, so the two Gold tables always agree (Silver
--     derives them from Bronze at its own build time; the job compares both as a warn check).
--   * Issue 10: product_number_last4 comes from silver.products.product_number, which is NULL for the 12 colliding
--     numbers, so a collision never yields a last-4 hint. The full number (product_number_raw) is not carried.
--   * Balances are the Silver snapshot as of the policy as-of date ({as_of_date}); balance_as_of says so.
WITH mov AS (
  SELECT product_id,
         min(event_ts) AS first_movement_at,
         max(event_ts) AS last_movement_at,
         count(*)      AS transaction_count
  FROM {transactions}
  GROUP BY product_id
),
p AS (
  SELECT *, right(product_number, 4) AS last4
  FROM {products}
)
SELECT
  p.product_id,
  p.customer_id,
  p.product_type,
  p.product_type_en,
  p.product_type_en IN ({card_types})                                         AS is_card,
  p.product_status,
  p.effective_status,
  p.currency,
  p.current_balance,
  p.credit_limit,
  p.credit_limit_null_reason,
  DATE '{as_of_date}'                                                         AS balance_as_of,
  p.last4                                                                     AS product_number_last4,
  p.product_number_collision,
  p.last4 IS NOT NULL
    AND count(p.last4) OVER (PARTITION BY p.customer_id, p.last4) > 1         AS last4_shared_within_customer,
  p.expiration_date,
  least(p.opening_date, CAST(m.first_movement_at AS DATE))                    AS effective_opening_date,
  m.first_movement_at,
  m.last_movement_at,
  coalesce(m.transaction_count, 0)                                            AS transaction_count,
  p.has_linked_app,
  p.customer_status_conflict
FROM p
LEFT JOIN mov m ON m.product_id = p.product_id
