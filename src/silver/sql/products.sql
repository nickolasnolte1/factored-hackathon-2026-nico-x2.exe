-- Silver · products: product book (one snapshot file, 400,000 rows). Contract: contracts/products.json.
-- Placeholders are filled by silver_lib.render_sql; `python silver_lib.py sql products` prints the runnable query.
-- As-of date of the snapshot: 2026-06-18 (dimension dates end 2026-06-17; the last events are 2026-06-18 before 06:00).
-- Rules from docs/02_eda_workflow_selection.md, section 6:
--   *  6 FIX effective_status = 'Expired' for Active products whose expiration_date < as-of date (product_status kept).
--   *  7 FIX last_transaction_date_recomputed = latest transaction event time for the product; the delivered value
--     matches it for 427 of 305,721 products, so last_transaction_date_stale flags the difference.
--   *  8 FIX effective_opening_date = least(opening_date, first transaction date) + FLAG activity_before_opening;
--     FLAG opened_before_registration (opening_date before the owner's registration date).
--   * 10 QUARANTINE product_number shared by two products of different customers (6 pairs): nulled so number lookups
--     never resolve to the wrong customer; raw kept in product_number_raw, flagged by product_number_collision.
--   * 11 FLAG currency_usd_for_mexico: every product of a Mexican customer is in USD. Balances are quoted as delivered.
--   * 20 FLAG customer_status_conflict: Active product held by a Closed customer (precedence: Closed/Suspended -> human).
--   * 21 *_null_reason: 'not_applicable' (field does not apply to the product type) vs 'missing' vs 'invalid'.
--   * product_type is kept in Spanish as delivered; product_type_en is the canonical English name.
-- Reads other Bronze tables with the same casting as their Silver tables, so this table builds on its own:
--   customers (owner country, status, registration) and transactions (4.4M rows aggregated per product: the heavy part).
--   Derived columns are recomputed only for the products in the window: after new transactions or customers land,
--   rebuild products in full mode to refresh them.
WITH src AS (
  SELECT *
  FROM {catalog}.{bronze}.products
  WHERE _ingested_at > TIMESTAMP '{watermark}'
  QUALIFY row_number() OVER (PARTITION BY upper(trim(product_id)) ORDER BY _ingested_at DESC, _source_file DESC) = 1
),
product_types (type_key, product_type, product_type_en, is_card, is_credit) AS (
  VALUES ('cuenta ahorro',        'Cuenta Ahorro',        'Savings Account',  false, false),
         ('cuenta corriente',     'Cuenta Corriente',     'Checking Account', false, false),
         ('tarjeta credito',      'Tarjeta Crédito',      'Credit Card',      true,  true),
         ('tarjeta debito',       'Tarjeta Débito',       'Debit Card',       true,  false),
         ('prestamo personal',    'Préstamo Personal',    'Personal Loan',    false, true),
         ('prestamo hipotecario', 'Préstamo Hipotecario', 'Mortgage',         false, true),
         ('inversion',            'Inversión',            'Investment',       false, false),
         ('seguro',               'Seguro',               'Insurance',        false, false)
),
-- product_number collisions are a property of the whole book, not of the incremental window
book AS (
  SELECT upper(trim(product_id)) AS product_id, upper(trim(product_number)) AS product_number
  FROM {catalog}.{bronze}.products
  QUALIFY row_number() OVER (PARTITION BY upper(trim(product_id)) ORDER BY _ingested_at DESC, _source_file DESC) = 1
),
colliding_numbers AS (
  SELECT product_number FROM book WHERE product_number IS NOT NULL GROUP BY product_number HAVING count(*) > 1
),
owners AS (
  SELECT
    upper(trim(customer_id))                                                     AS customer_id,
    CASE translate(lower(trim(country)), 'áéíóú', 'aeiou')
      WHEN 'mexico' THEN 'MX' WHEN 'colombia' THEN 'CO' WHEN 'argentina' THEN 'AR' END AS country_code,
    initcap(trim(customer_status))                                               AS customer_status,
    try_cast(trim(registration_date) AS TIMESTAMP)                               AS registration_date
  FROM {catalog}.{bronze}.customers
  QUALIFY row_number() OVER (PARTITION BY upper(trim(customer_id)) ORDER BY _ingested_at DESC, _source_file DESC) = 1
),
tx AS (
  SELECT
    upper(trim(product_id))                                                      AS product_id,
    min(try_cast(trim(transaction_date) AS TIMESTAMP))                           AS first_transaction_date,
    max(try_cast(trim(transaction_date) AS TIMESTAMP))                           AS last_transaction_date_recomputed,
    count(DISTINCT upper(trim(transaction_id)))                                  AS transaction_count
  FROM {catalog}.{bronze}.transactions
  WHERE product_id IS NOT NULL
  GROUP BY upper(trim(product_id))
),
typed AS (
  SELECT
    upper(trim(src.product_id))                                                  AS product_id,
    upper(trim(src.customer_id))                                                 AS customer_id,
    coalesce(pt.product_type, trim(src.product_type))                            AS product_type,
    pt.product_type_en,
    pt.is_card,
    pt.is_credit,
    upper(trim(src.product_number))                                              AS product_number_raw,
    cn.product_number IS NOT NULL                                                AS product_number_collision,
    upper(trim(src.currency))                                                    AS currency,
    try_cast(trim(src.current_balance) AS DECIMAL(18,2))                         AS current_balance,
    try_cast(trim(src.credit_limit) AS DECIMAL(18,2))                            AS credit_limit,
    try_cast(trim(src.interest_rate) AS DECIMAL(5,2))                            AS interest_rate,
    try_cast(trim(src.opening_date) AS DATE)                                     AS opening_date,
    try_cast(trim(src.expiration_date) AS DATE)                                  AS expiration_date,
    upper(trim(src.opening_branch_id))                                           AS opening_branch_id,
    coalesce(try_element_at(map('active', 'Active', 'closed', 'Closed', 'blocked', 'Blocked', 'suspended', 'Suspended'),
                            lower(trim(src.product_status))), trim(src.product_status)) AS product_status,
    coalesce(try_element_at(map('branch', 'Branch', 'web', 'Web', 'app', 'App', 'call center', 'Call Center'),
                            lower(trim(src.opening_channel))), trim(src.opening_channel)) AS opening_channel,
    try_cast(trim(src.has_linked_app) AS BOOLEAN)                                AS has_linked_app,
    try_cast(regexp_replace(trim(src.days_past_due), '[.]0+$', '') AS INT)       AS days_past_due,
    try_cast(trim(src.last_transaction_date) AS TIMESTAMP)                       AS last_transaction_date,
    try_cast(trim(src.last_updated) AS TIMESTAMP)                                AS last_updated,
    nullif(trim(src.credit_limit), '') IS NULL                                   AS credit_limit_raw_missing,
    nullif(trim(src.interest_rate), '') IS NULL                                  AS interest_rate_raw_missing,
    nullif(trim(src.expiration_date), '') IS NULL                                AS expiration_date_raw_missing,
    nullif(trim(src.days_past_due), '') IS NULL                                  AS days_past_due_raw_missing,
    o.country_code                                                               AS owner_country_code,
    o.customer_status                                                            AS owner_status,
    o.registration_date                                                          AS owner_registration_date,
    tx.first_transaction_date,
    tx.last_transaction_date_recomputed,
    coalesce(tx.transaction_count, 0)                                            AS transaction_count,
    src._source_file,
    src._ingested_at,
    struct(src.*)                                                                AS _bronze
  FROM src
  LEFT JOIN product_types pt ON pt.type_key = translate(lower(trim(src.product_type)), 'áéíóú', 'aeiou')
  LEFT JOIN colliding_numbers cn ON cn.product_number = upper(trim(src.product_number))
  LEFT JOIN owners o ON o.customer_id = upper(trim(src.customer_id))
  LEFT JOIN tx ON tx.product_id = upper(trim(src.product_id))
)
SELECT
  product_id, customer_id,
  product_type, product_type_en,
  CASE WHEN NOT product_number_collision THEN product_number_raw END           AS product_number,
  product_number_raw, product_number_collision,
  currency,
  coalesce(owner_country_code = 'MX' AND currency = 'USD', false)             AS currency_usd_for_mexico,
  current_balance,
  credit_limit,
  CASE WHEN credit_limit IS NOT NULL THEN NULL WHEN NOT is_credit THEN 'not_applicable'
       WHEN credit_limit_raw_missing THEN 'missing' ELSE 'invalid' END         AS credit_limit_null_reason,
  interest_rate,
  CASE WHEN interest_rate IS NOT NULL THEN NULL
       WHEN interest_rate_raw_missing THEN 'missing' ELSE 'invalid' END        AS interest_rate_null_reason,
  days_past_due,
  CASE WHEN days_past_due IS NOT NULL THEN NULL WHEN NOT is_credit THEN 'not_applicable'
       WHEN days_past_due_raw_missing THEN 'missing' ELSE 'invalid' END        AS days_past_due_null_reason,
  opening_date,
  least(opening_date, to_date(first_transaction_date))                         AS effective_opening_date,
  coalesce(to_date(first_transaction_date) < opening_date, false)              AS activity_before_opening,
  coalesce(opening_date < to_date(owner_registration_date), false)             AS opened_before_registration,
  expiration_date,
  CASE WHEN expiration_date IS NOT NULL THEN NULL WHEN NOT is_card THEN 'not_applicable'
       WHEN expiration_date_raw_missing THEN 'missing' ELSE 'invalid' END      AS expiration_date_null_reason,
  opening_branch_id,
  product_status,
  CASE WHEN product_status = 'Active' AND expiration_date < DATE '2026-06-18' THEN 'Expired'
       ELSE product_status END                                                 AS effective_status,
  coalesce(product_status = 'Active' AND owner_status = 'Closed', false)       AS customer_status_conflict,
  opening_channel,
  has_linked_app,
  last_transaction_date,
  last_transaction_date_recomputed,
  first_transaction_date,
  transaction_count,
  NOT (to_date(last_transaction_date) <=> to_date(last_transaction_date_recomputed)) AS last_transaction_date_stale,
  last_updated,
  coalesce(to_date(last_updated) > DATE '2026-06-18', false)                   AS last_updated_after_as_of,
  _source_file, _ingested_at, _bronze
FROM typed
