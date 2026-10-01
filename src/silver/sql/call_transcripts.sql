-- Silver · call_transcripts: one row per transcribed interaction (171,321 rows). Contract: contracts/call_transcripts.json.
-- Placeholders are filled by silver_lib.render_sql; `python silver_lib.py sql call_transcripts` prints the runnable query.
-- Rules (docs/02_eda_workflow_selection.md, section 6, and docs/01_data_findings.md):
--   * Issue 19: the source has no timestamp, so event_ts / event_date come from the parent interaction (Bronze, typed as
--     in sql/call_center_interactions.sql, so this table builds on its own). A transcript without its interaction has no
--     event time and is quarantined (0 orphans in the delivered data).
--   * Issue 16: main_topics is a copy of the interaction's reason_category (171,321 / 171,321) and detected_intents is
--     'consulta_general' or NULL: both are kept for lineage but must never be features or labels.
--   * Text is templated (546 distinct full texts, 42 distinct customer texts) and every agent turn keeps unfilled
--     placeholders such as {monto}: FLAG has_template_placeholders. All text is Spanish (detected_language = 'es').
-- De-duplicated on interaction_id (one transcript per interaction); 0 duplicate groups in the delivered data.
WITH src AS (
  SELECT *
  FROM {catalog}.{bronze}.call_transcripts
  WHERE _ingested_at > TIMESTAMP '{watermark}'
),
interactions AS (
  SELECT upper(trim(interaction_id)) AS interaction_id,
         try_cast(trim(interaction_date) AS TIMESTAMP) AS event_ts
  FROM {catalog}.{bronze}.call_center_interactions
  QUALIFY row_number() OVER (PARTITION BY upper(trim(interaction_id)) ORDER BY _ingested_at DESC, _source_file DESC) = 1
),
typed AS (
  SELECT
    upper(trim(transcript_id))                                                    AS transcript_id,
    upper(trim(interaction_id))                                                   AS interaction_id,
    try_cast(trim(process_date) AS DATE)                                          AS process_date,
    upper(trim(customer_id))                                                      AS customer_id,
    upper(trim(agent_id))                                                         AS agent_id,
    trim(full_text)                                                               AS full_text,
    trim(customer_text)                                                           AS customer_text,
    trim(agent_text)                                                              AS agent_text,
    lower(trim(detected_language))                                                AS detected_language,
    lower(trim(detected_accent))                                                  AS detected_accent,
    try_cast(trim(accent_confidence) AS DOUBLE)                                   AS accent_confidence,
    CASE WHEN nullif(trim(detected_keywords), '') IS NOT NULL
      THEN split(lower(trim(detected_keywords)), '\\s*,\\s*') END                 AS detected_keywords,
    nullif(trim(mentioned_entities), '')                                          AS mentioned_entities,
    CASE WHEN nullif(trim(detected_intents), '') IS NOT NULL
      THEN split(lower(trim(detected_intents)), '\\s*,\\s*') END                  AS detected_intents,
    CASE translate(lower(trim(main_topics)), 'áéíóú', 'aeiou')
      WHEN 'transaccional' THEN 'Transaccional' WHEN 'producto' THEN 'Producto' WHEN 'queja' THEN 'Queja'
      WHEN 'tecnico' THEN 'Técnico' WHEN 'comercial' THEN 'Comercial' WHEN 'retencion' THEN 'Retención'
      ELSE trim(main_topics) END                                                  AS main_topics,
    trim(transcription_model)                                                     AS transcription_model,
    initcap(lower(trim(audio_quality)))                                           AS audio_quality,
    try_cast(regexp_replace(trim(duration_seconds), '[.]0+$', '') AS INT)         AS duration_seconds,
    _source_file,
    _ingested_at,
    struct(src.*)                                                                 AS _bronze
  FROM src
),
dedup AS (
  SELECT *
  FROM typed
  QUALIFY row_number() OVER (PARTITION BY interaction_id ORDER BY _ingested_at DESC, _source_file DESC) = 1
)
SELECT
  d.transcript_id, d.interaction_id, i.event_ts, to_date(i.event_ts) AS event_date, d.process_date,
  d.customer_id, d.agent_id,
  d.full_text, d.customer_text, d.agent_text,
  coalesce(d.full_text RLIKE '[{][a-z_]+[}]' OR d.agent_text RLIKE '[{][a-z_]+[}]'
           OR d.customer_text RLIKE '[{][a-z_]+[}]', false)                      AS has_template_placeholders,
  d.detected_language, d.detected_accent, d.accent_confidence,
  d.detected_keywords, d.mentioned_entities, d.detected_intents, d.main_topics,
  d.transcription_model, d.audio_quality, d.duration_seconds,
  d._source_file, d._ingested_at, d._bronze
FROM dedup d
LEFT JOIN interactions i ON i.interaction_id = d.interaction_id
