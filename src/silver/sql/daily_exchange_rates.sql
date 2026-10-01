-- Silver · daily_exchange_rates: one rate per day and ordered currency pair (12 pairs x 1,097 days = 13,164 rows).
-- Contract: contracts/daily_exchange_rates.json. `python silver_lib.py sql daily_exchange_rates` prints the runnable query.
-- Found while profiling: each series is flat noise within about +/-2% of a constant (USD/ARS 343-357, USD/COP 3,920-4,080,
-- USD/MXN 16.66-17.34) for three years, centred on the fixed rates behind transactions.amount_usd (issue 12). The
-- provider (`source`) changes from day to day within a pair. Direct X->USD rates keep only 6 decimals (COP->USD has 3
-- significant digits), so COP amounts convert more precisely as amount / rate(USD->COP).
WITH src AS (
  SELECT *
  FROM {catalog}.{bronze}.daily_exchange_rates
  WHERE _ingested_at > TIMESTAMP '{watermark}'
  QUALIFY row_number() OVER (
    PARTITION BY try_cast(trim(`date`) AS DATE), upper(trim(source_currency)), upper(trim(target_currency))
    ORDER BY _ingested_at DESC, _source_file DESC) = 1
)
SELECT
  try_cast(trim(`date`) AS DATE)                         AS `date`,
  upper(trim(source_currency))                           AS source_currency,
  upper(trim(target_currency))                           AS target_currency,
  try_cast(trim(exchange_rate) AS DECIMAL(18,6))         AS exchange_rate,
  try_cast(trim(buy_rate) AS DECIMAL(18,6))              AS buy_rate,
  try_cast(trim(sell_rate) AS DECIMAL(18,6))             AS sell_rate,
  CASE lower(trim(`source`))
    WHEN 'central bank' THEN 'Central Bank' WHEN 'reuters' THEN 'Reuters'
    WHEN 'bloomberg' THEN 'Bloomberg' WHEN 'internal' THEN 'Internal'
    ELSE trim(`source`) END                              AS `source`,
  _source_file,
  _ingested_at,
  struct(src.*)                                          AS _bronze
FROM src
