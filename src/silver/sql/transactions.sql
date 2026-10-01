-- Silver · transactions: one row per card/account movement (4.4M rows, daily files). Contract: contracts/transactions.json.
-- Placeholders are filled by silver_lib.render_sql; `python silver_lib.py sql transactions` prints the runnable query.
-- Rules (docs/02_eda_workflow_selection.md, section 6):
--   * 19: event_ts/event_date come from transaction_date; process_date is the business day (06:00 cut-off) = landing partition.
--   * 12: amount_usd filled with the source's fixed rates (ARS/350, COP/4000, USD 1:1); raw kept, amount_usd_imputed flags it.
--   * 13: country names accent-folded to ISO codes; is_international = transaction country <> customer home country.
--   * 18: implausible_type_channel flags type x channel pairs that cannot happen (report Q4.12).
--   *  8: activity_before_opening / activity_before_registration against the product and the customer.
--   * 16: fraud_score is kept but is built from is_fraud; never a feature. 21: response_code_null_reason.
--   * 22: natural-key dedup on (customer, product, event_ts, amount); expected 0 duplicates.
-- Found while profiling (not in the issue list): coordinates are the customer's home-city point, never the transaction
-- country for international rows, and every Mexican point is a placeholder near (0, 0). latitude/longitude are NULL
-- unless inside the transaction country; raw values kept; has_valid_coordinates flags it.
-- customers and products are read from Bronze (same casting as their Silver SQL) so this table builds on its own;
-- both are small and broadcast. Derived flags reflect the dimensions at build time; a full run recomputes them.
WITH src AS (
  SELECT *
  FROM {catalog}.{bronze}.transactions
  WHERE _ingested_at > TIMESTAMP '{watermark}'
  QUALIFY row_number() OVER (
    PARTITION BY upper(trim(customer_id)), upper(trim(product_id)),
                 try_cast(trim(transaction_date) AS TIMESTAMP), try_cast(trim(amount) AS DECIMAL(18,2))
    ORDER BY _ingested_at DESC, _source_file DESC, transaction_id) = 1
),
cust AS (
  SELECT
    upper(trim(customer_id))                                                   AS customer_id,
    CASE translate(lower(trim(country)), 'áéíóúüñ', 'aeiouun')
      WHEN 'mexico' THEN 'MX' WHEN 'colombia' THEN 'CO' WHEN 'argentina' THEN 'AR' END AS home_country_code,
    try_cast(trim(registration_date) AS DATE)                                  AS registration_date
  FROM {catalog}.{bronze}.customers
  QUALIFY row_number() OVER (PARTITION BY upper(trim(customer_id)) ORDER BY _ingested_at DESC, _source_file DESC) = 1
),
prod AS (
  SELECT
    upper(trim(product_id))                                                    AS product_id,
    upper(trim(customer_id))                                                   AS owner_id,
    try_cast(trim(opening_date) AS DATE)                                       AS opening_date
  FROM {catalog}.{bronze}.products
  QUALIFY row_number() OVER (PARTITION BY upper(trim(product_id)) ORDER BY _ingested_at DESC, _source_file DESC) = 1
),
typed AS (
  SELECT
    upper(trim(transaction_id))                                                AS transaction_id,
    try_cast(trim(transaction_date) AS TIMESTAMP)                              AS event_ts,
    try_cast(trim(process_date) AS DATE)                                       AS process_date,
    upper(trim(product_id))                                                    AS product_id,
    upper(trim(customer_id))                                                   AS customer_id,
    initcap(lower(trim(transaction_type)))                                     AS transaction_type,
    initcap(lower(trim(transaction_category)))                                 AS transaction_category,
    try_cast(trim(amount) AS DECIMAL(18,2))                                    AS amount,
    upper(trim(currency))                                                      AS currency,
    try_cast(trim(amount_usd) AS DECIMAL(18,2))                                AS amount_usd_raw,
    CASE WHEN upper(trim(channel)) IN ('POS', 'ATM') THEN upper(trim(channel))
         ELSE initcap(lower(trim(channel))) END                                AS channel,
    upper(trim(branch_id))                                                     AS branch_id,
    trim(merchant_name)                                                        AS merchant_name,
    initcap(lower(trim(merchant_category)))                                    AS merchant_category,
    trim(transaction_country)                                                  AS transaction_country,
    CASE translate(lower(trim(transaction_country)), 'áéíóúüñ', 'aeiouun')
      WHEN 'mexico' THEN 'MX' WHEN 'colombia' THEN 'CO' WHEN 'argentina' THEN 'AR'
      WHEN 'usa' THEN 'US' WHEN 'united states' THEN 'US' WHEN 'estados unidos' THEN 'US'
      WHEN 'spain' THEN 'ES' WHEN 'espana' THEN 'ES' WHEN 'brazil' THEN 'BR' WHEN 'brasil' THEN 'BR' END
                                                                               AS transaction_country_code,
    trim(transaction_city)                                                     AS transaction_city,
    initcap(lower(trim(transaction_status)))                                   AS transaction_status,
    nullif(trim(response_code), '')                                            AS response_code,
    try_cast(trim(is_fraud) AS BOOLEAN)                                        AS is_fraud,
    try_cast(trim(fraud_score) AS DOUBLE)                                      AS fraud_score,
    try_cast(trim(latitude) AS DOUBLE)                                         AS latitude_raw,
    try_cast(trim(longitude) AS DOUBLE)                                        AS longitude_raw,
    _source_file,
    _ingested_at,
    struct(src.*)                                                              AS _bronze
  FROM src
),
joined AS (
  SELECT /*+ BROADCAST(c), BROADCAST(p) */
    t.*,
    to_date(t.event_ts)                                                        AS event_date,
    c.home_country_code                                                        AS customer_country_code,
    c.registration_date                                                        AS customer_registration_date,
    p.owner_id                                                                 AS product_owner_id,
    p.opening_date                                                             AS product_opening_date,
    coalesce(CASE t.transaction_country_code  -- loose national bounding boxes
      WHEN 'MX' THEN t.latitude_raw BETWEEN 14.0 AND 33.0 AND t.longitude_raw BETWEEN -119.0 AND -86.0
      WHEN 'CO' THEN t.latitude_raw BETWEEN -5.0 AND 14.0 AND t.longitude_raw BETWEEN -82.0 AND -66.0
      WHEN 'AR' THEN t.latitude_raw BETWEEN -56.0 AND -21.0 AND t.longitude_raw BETWEEN -74.0 AND -53.0
      WHEN 'US' THEN t.latitude_raw BETWEEN 18.0 AND 72.0 AND t.longitude_raw BETWEEN -180.0 AND -65.0
      WHEN 'ES' THEN t.latitude_raw BETWEEN 27.0 AND 44.5 AND t.longitude_raw BETWEEN -18.5 AND 4.5
      WHEN 'BR' THEN t.latitude_raw BETWEEN -34.0 AND 5.5 AND t.longitude_raw BETWEEN -74.0 AND -34.0
                     AND (t.latitude_raw > -20.0 OR t.longitude_raw > -58.0)  -- keeps central Argentina out
                     AND (t.latitude_raw > -32.0 OR t.longitude_raw > -54.0)  -- keeps Buenos Aires and Uruguay out
                     AND (t.latitude_raw < -4.5 OR t.longitude_raw > -70.0)   -- keeps the Colombian Andes out
    END, false)                                                               AS has_valid_coordinates
  FROM typed t
  LEFT JOIN cust c ON c.customer_id = t.customer_id
  LEFT JOIN prod p ON p.product_id = t.product_id
)
SELECT
  transaction_id, event_ts, event_date, process_date,
  product_id, customer_id,
  transaction_type, transaction_category,
  amount, currency,
  coalesce(amount_usd_raw,
           CAST(bround(amount / CASE currency WHEN 'USD' THEN 1 WHEN 'ARS' THEN 350 WHEN 'COP' THEN 4000 END, 2)
                AS DECIMAL(18,2)))                                             AS amount_usd,
  amount_usd_raw,
  amount_usd_raw IS NULL                                                       AS amount_usd_imputed,
  channel, branch_id, merchant_name, merchant_category,
  transaction_country, transaction_country_code, transaction_city,
  customer_country_code,
  transaction_country_code <> customer_country_code                           AS is_international,
  transaction_status, response_code,
  CASE WHEN response_code IS NOT NULL THEN NULL
       WHEN transaction_status = 'Approved' THEN 'not_applicable'
       ELSE 'missing' END                                                      AS response_code_null_reason,
  is_fraud, fraud_score,
  CASE WHEN has_valid_coordinates THEN latitude_raw END                        AS latitude,
  CASE WHEN has_valid_coordinates THEN longitude_raw END                       AS longitude,
  latitude_raw, longitude_raw, has_valid_coordinates,
  (transaction_type = 'Withdrawal' AND channel IN ('POS', 'Web', 'App', 'Transfer'))
    OR (transaction_type = 'Deposit' AND channel = 'POS')
    OR (transaction_type = 'Purchase' AND channel IN ('ATM', 'Transfer', 'Branch'))  AS implausible_type_channel,
  event_date < product_opening_date                                            AS activity_before_opening,
  event_date < customer_registration_date                                      AS activity_before_registration,
  product_owner_id = customer_id                                               AS product_owner_matches,
  _source_file, _ingested_at, _bronze
FROM joined
