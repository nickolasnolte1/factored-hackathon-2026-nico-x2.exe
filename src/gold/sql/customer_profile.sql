-- Gold · customer_profile: the customer facts the agent may use after authentication (no personal data).
-- Spec: gold_tables.json. Sources: silver.customers, silver.products (pinned Delta versions in the job).
--   * Issue 11: Mexican customers hold every product in USD. home_currency is the legal currency of the country
--     (MXN, COP, ARS, as in the EDA); product_currencies lists what the products are actually held in.
--   * Issue 20 (precedence rule): closed_or_suspended uses the policy's restricted_customer_statuses
--     ({restricted_statuses}); the agent hands these customers off. The conflict flags tell the human agent why.
--   * products_active counts product_status = 'Active' as delivered; products_active_effective applies the card
--     expiry fix (issue 6: an Active card past its expiration date is Expired).
WITH prod AS (
  SELECT
    customer_id,
    count(*)                                        AS products_total,
    count_if(product_status = 'Active')             AS products_active,
    count_if(effective_status = 'Active')           AS products_active_effective,
    count_if(effective_status = 'Expired')          AS cards_expired,
    count_if(customer_status_conflict)              AS products_status_conflict,
    count_if(currency_usd_for_mexico)               AS products_usd_for_mexico,
    array_sort(collect_set(currency))               AS product_currencies
  FROM {products}
  GROUP BY customer_id
)
SELECT
  c.customer_id,
  c.segment,
  c.country,
  c.country_code,
  CASE c.country_code WHEN 'MX' THEN 'MXN' WHEN 'CO' THEN 'COP' WHEN 'AR' THEN 'ARS' END AS home_currency,
  coalesce(p.product_currencies, CAST(array() AS ARRAY<STRING>))                        AS product_currencies,
  coalesce(p.products_usd_for_mexico, 0) > 0                                            AS currency_usd_for_mexico,
  c.customer_status,
  c.customer_status IN ({restricted_statuses})                                           AS closed_or_suspended,
  coalesce(p.products_total, 0)                                                         AS products_total,
  coalesce(p.products_active, 0)                                                        AS products_active,
  coalesce(p.products_active_effective, 0)                                              AS products_active_effective,
  coalesce(p.cards_expired, 0)                                                          AS cards_expired,
  coalesce(p.products_status_conflict, 0) > 0                                           AS customer_status_conflict,
  c.customer_status IN ({restricted_statuses}) AND coalesce(p.products_active, 0) > 0   AS restricted_with_active_products
FROM {customers} c
LEFT JOIN prod p ON p.customer_id = c.customer_id
