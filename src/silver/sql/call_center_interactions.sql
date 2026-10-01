-- Silver · call_center_interactions: one row per contact (686,296 rows). Contract: contracts/call_center_interactions.json.
-- Placeholders are filled by silver_lib.render_sql; `python silver_lib.py sql call_center_interactions` prints the runnable query.
-- Rules (docs/02_eda_workflow_selection.md, section 6):
--   * Issue 19: event_ts / event_date come from interaction_date; process_date is a business-day cut-off (previous day
--     for 33.3% of rows, before 08:00) and is kept for partition pruning only.
--   * Issue 15: mentioned_products is DROPPED (545,118 of 548,680 listed product ids do not exist; the other 3,562 belong
--     to another customer).
--   * contact_reason is DROPPED: identical to reason_category in 686,296 / 686,296 rows.
--   * Issue 16: detected_sentiment is a binning of sentiment_score (cut points +-0.3 / +-0.7); keep sentiment_score for
--     analysis, never use either (or any post-contact outcome) as a model feature.
-- De-duplicated on the content key (customer, event time, agent, reason); 0 duplicate groups in the delivered data.
WITH src AS (
  SELECT *
  FROM {catalog}.{bronze}.call_center_interactions
  WHERE _ingested_at > TIMESTAMP '{watermark}'
),
typed AS (
  SELECT
    upper(trim(interaction_id))                                                   AS interaction_id,
    try_cast(trim(interaction_date) AS TIMESTAMP)                                 AS event_ts,
    try_cast(trim(process_date) AS DATE)                                          AS process_date,
    upper(trim(customer_id))                                                      AS customer_id,
    upper(trim(agent_id))                                                         AS agent_id,
    initcap(lower(trim(interaction_type)))                                        AS interaction_type,
    CASE lower(trim(channel)) WHEN 'whatsapp' THEN 'WhatsApp'
      ELSE initcap(lower(trim(channel))) END                                      AS channel,
    CASE translate(lower(trim(reason_category)), 'áéíóú', 'aeiou')
      WHEN 'transaccional' THEN 'Transaccional' WHEN 'producto' THEN 'Producto' WHEN 'queja' THEN 'Queja'
      WHEN 'tecnico' THEN 'Técnico' WHEN 'comercial' THEN 'Comercial' WHEN 'retencion' THEN 'Retención'
      ELSE trim(reason_category) END                                              AS reason_category,
    try_cast(regexp_replace(trim(duration_seconds), '[.]0+$', '') AS INT)         AS duration_seconds,
    try_cast(regexp_replace(trim(wait_time_seconds), '[.]0+$', '') AS INT)        AS wait_time_seconds,
    try_cast(trim(was_resolved) AS BOOLEAN)                                       AS was_resolved,
    try_cast(trim(requires_followup) AS BOOLEAN)                                  AS requires_followup,
    try_cast(trim(was_escalated) AS BOOLEAN)                                      AS was_escalated,
    initcap(lower(trim(detected_sentiment)))                                      AS detected_sentiment,
    try_cast(trim(sentiment_score) AS DOUBLE)                                     AS sentiment_score,
    lower(trim(customer_detected_accent))                                         AS customer_detected_accent,
    lower(trim(agent_used_accent))                                                AS agent_used_accent,
    try_cast(trim(has_transcript) AS BOOLEAN)                                     AS has_transcript,
    try_cast(trim(has_recording) AS BOOLEAN)                                      AS has_recording,
    _source_file,
    _ingested_at,
    struct(src.*)                                                                 AS _bronze
  FROM src
),
dedup AS (
  SELECT *
  FROM typed
  QUALIFY row_number() OVER (PARTITION BY customer_id, event_ts, agent_id, reason_category
                             ORDER BY _ingested_at DESC, _source_file DESC) = 1
)
SELECT
  interaction_id, event_ts, to_date(event_ts) AS event_date, process_date,
  customer_id, agent_id, interaction_type, channel, reason_category,
  duration_seconds, wait_time_seconds, was_resolved, requires_followup, was_escalated,
  detected_sentiment, sentiment_score, customer_detected_accent, agent_used_accent,
  has_transcript, has_recording,
  _source_file, _ingested_at, _bronze
FROM dedup
