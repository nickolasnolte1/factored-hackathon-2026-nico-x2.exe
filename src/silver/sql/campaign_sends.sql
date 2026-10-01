-- Silver · campaign_sends: one row per marketing message sent to a customer (1.7M rows, daily files).
-- Contract: contracts/campaign_sends.json. `python silver_lib.py sql campaign_sends` prints the runnable query.
-- Rules: 19 event_ts/event_date from send_date, process_date = business day (06:00 cut-off, 25.0% previous day);
-- 13 open_country accent-folded to ISO; 22 natural-key dedup on (campaign, customer, event_ts, channel), expected 0.
-- Found while profiling (not in the issue list):
--   * Opens are tracked only for delivered Email, Push and SMS; was_opened is NULL elsewhere, while was_clicked and
--     had_conversion say False: engagement_tracked is the denominator for open, click and conversion rates.
--   * 322,739 sends predate the customer's registration and 8,277 fall after the campaign's end date: FLAG.
--   * 874,417 sends (50.1%) go to customers whose accepts_marketing is False today (no consent timestamp): FLAG.
-- customers and marketing_campaigns are read from Bronze (same casting as their Silver SQL) so this table builds on
-- its own; both are small and broadcast. Flags reflect the dimensions at build time; a full run recomputes them.
WITH src AS (
  SELECT *
  FROM {catalog}.{bronze}.campaign_sends
  WHERE _ingested_at > TIMESTAMP '{watermark}'
  QUALIFY row_number() OVER (
    PARTITION BY upper(trim(campaign_id)), upper(trim(customer_id)),
                 try_cast(trim(send_date) AS TIMESTAMP), lower(trim(send_channel))
    ORDER BY _ingested_at DESC, _source_file DESC, send_id) = 1
),
cust AS (
  SELECT
    upper(trim(customer_id))                                     AS customer_id,
    try_cast(trim(registration_date) AS DATE)                    AS registration_date,
    try_cast(trim(accepts_marketing) AS BOOLEAN)                 AS accepts_marketing
  FROM {catalog}.{bronze}.customers
  QUALIFY row_number() OVER (PARTITION BY upper(trim(customer_id)) ORDER BY _ingested_at DESC, _source_file DESC) = 1
),
camp AS (
  SELECT
    upper(trim(campaign_id))                                     AS campaign_id,
    try_cast(trim(start_date) AS DATE)                           AS start_date,
    try_cast(trim(end_date) AS DATE)                             AS end_date
  FROM {catalog}.{bronze}.marketing_campaigns
  QUALIFY row_number() OVER (PARTITION BY upper(trim(campaign_id)) ORDER BY _ingested_at DESC, _source_file DESC) = 1
),
typed AS (
  SELECT
    upper(trim(send_id))                                         AS send_id,
    try_cast(trim(send_date) AS TIMESTAMP)                       AS event_ts,
    try_cast(trim(process_date) AS DATE)                         AS process_date,
    upper(trim(campaign_id))                                     AS campaign_id,
    upper(trim(customer_id))                                     AS customer_id,
    CASE lower(trim(send_channel))
      WHEN 'email' THEN 'Email' WHEN 'sms' THEN 'SMS' WHEN 'whatsapp' THEN 'WhatsApp'
      WHEN 'push' THEN 'Push' WHEN 'voice' THEN 'Voice' ELSE trim(send_channel) END AS send_channel,
    trim(template_used)                                          AS template_used,
    trim(subject)                                                AS subject,
    initcap(lower(trim(send_status)))                            AS send_status,
    try_cast(trim(was_delivered) AS BOOLEAN)                     AS was_delivered,
    try_cast(trim(was_opened) AS BOOLEAN)                        AS was_opened,
    try_cast(trim(open_date) AS TIMESTAMP)                       AS open_ts,
    try_cast(trim(was_clicked) AS BOOLEAN)                       AS was_clicked,
    try_cast(trim(click_date) AS TIMESTAMP)                      AS click_ts,
    try_cast(regexp_replace(trim(click_count), '[.]0+$', '') AS INT) AS click_count,
    try_cast(trim(had_conversion) AS BOOLEAN)                    AS had_conversion,
    try_cast(trim(conversion_date) AS TIMESTAMP)                 AS conversion_ts,
    try_cast(trim(conversion_value) AS DECIMAL(18,2))            AS conversion_value,
    initcap(lower(trim(open_device)))                            AS open_device,
    trim(open_country)                                           AS open_country,
    CASE translate(lower(trim(open_country)), 'áéíóú', 'aeiou')
      WHEN 'mexico' THEN 'MX' WHEN 'colombia' THEN 'CO' WHEN 'argentina' THEN 'AR' END AS open_country_code,
    CASE lower(trim(failure_reason))
      WHEN 'smtp error' THEN 'SMTP error' WHEN 'invalid email address' THEN 'Invalid email address'
      WHEN 'user blocked sender' THEN 'User blocked sender' ELSE trim(failure_reason) END AS failure_reason,
    try_cast(trim(send_cost) AS DECIMAL(18,4))                   AS send_cost,
    _source_file,
    _ingested_at,
    struct(src.*)                                                AS _bronze
  FROM src
)
SELECT /*+ BROADCAST(c), BROADCAST(m) */
  t.send_id, t.event_ts, to_date(t.event_ts) AS event_date, t.process_date,
  t.campaign_id, t.customer_id,
  t.send_channel, t.template_used, t.subject,
  t.send_status, t.was_delivered,
  t.was_opened, t.open_ts,
  t.was_clicked, t.click_ts, t.click_count,
  t.had_conversion, t.conversion_ts, t.conversion_value,
  t.open_device, t.open_country, t.open_country_code,
  t.failure_reason, t.send_cost,
  t.was_delivered AND t.send_channel IN ('Email', 'Push', 'SMS')                  AS engagement_tracked,
  to_date(t.event_ts) NOT BETWEEN m.start_date AND m.end_date                     AS sent_outside_campaign_window,
  to_date(t.event_ts) < c.registration_date                                       AS sent_before_registration,
  NOT c.accepts_marketing                                                         AS customer_opted_out,
  t._source_file, t._ingested_at, t._bronze
FROM typed t
LEFT JOIN cust c ON c.customer_id = t.customer_id
LEFT JOIN camp m ON m.campaign_id = t.campaign_id
