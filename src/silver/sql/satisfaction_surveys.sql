-- Silver · satisfaction_surveys: one row per post-contact survey (212,759 rows). Contract: contracts/satisfaction_surveys.json.
-- Placeholders are filled by silver_lib.render_sql; `python silver_lib.py sql satisfaction_surveys` prints the runnable query.
-- Rules (docs/02_eda_workflow_selection.md, section 6):
--   * Issue 17: scales are truncated (CSAT and CES 1-4, NPS 2-7, so 0 promoters). FIX nps_category recomputed from
--     main_score with the standard NPS cut points (0-6 Detractor, 7-8 Passive, 9-10 Promoter); the delivered value
--     (NULL in 3,274 of 63,668 NPS rows, otherwise identical) is kept in nps_category_raw. Report relative KPIs only.
--   * Issue 16: scores are a fixed function of the interaction's was_resolved: consequences of FCR, never labels or features.
--   * Issue 19: event_ts / event_date come from survey_date (1-24 h after the contact). process_date is the
--     interaction's business day, not the survey's.
-- De-duplicated on (interaction_id, survey_type); 0 duplicate groups in the delivered data.
WITH src AS (
  SELECT *
  FROM {catalog}.{bronze}.satisfaction_surveys
  WHERE _ingested_at > TIMESTAMP '{watermark}'
),
typed AS (
  SELECT
    upper(trim(survey_id))                                                        AS survey_id,
    upper(trim(interaction_id))                                                   AS interaction_id,
    try_cast(trim(survey_date) AS TIMESTAMP)                                      AS event_ts,
    try_cast(trim(process_date) AS DATE)                                          AS process_date,
    upper(trim(customer_id))                                                      AS customer_id,
    upper(trim(agent_id))                                                         AS agent_id,
    upper(trim(survey_type))                                                      AS survey_type,
    CASE WHEN upper(trim(send_channel)) IN ('IVR', 'SMS') THEN upper(trim(send_channel))
      ELSE initcap(lower(trim(send_channel))) END                                 AS send_channel,
    try_cast(regexp_replace(trim(main_score), '[.]0+$', '') AS INT)               AS main_score,
    initcap(lower(trim(nps_category)))                                            AS nps_category_raw,
    trim(question_1_text)                                                         AS question_1_text,
    try_cast(regexp_replace(trim(question_1_response), '[.]0+$', '') AS INT)      AS question_1_response,
    trim(question_2_text)                                                         AS question_2_text,
    try_cast(regexp_replace(trim(question_2_response), '[.]0+$', '') AS INT)      AS question_2_response,
    trim(question_3_text)                                                         AS question_3_text,
    try_cast(regexp_replace(trim(question_3_response), '[.]0+$', '') AS INT)      AS question_3_response,
    nullif(trim(open_comments), '')                                               AS open_comments,
    initcap(lower(trim(comment_sentiment)))                                       AS comment_sentiment,
    try_cast(trim(response_time_hours) AS DOUBLE)                                 AS response_time_hours,
    try_cast(trim(campaign_response_rate) AS DOUBLE)                              AS campaign_response_rate,
    _source_file,
    _ingested_at,
    struct(src.*)                                                                 AS _bronze
  FROM src
),
dedup AS (
  SELECT *
  FROM typed
  QUALIFY row_number() OVER (PARTITION BY interaction_id, survey_type ORDER BY _ingested_at DESC, _source_file DESC) = 1
)
SELECT
  survey_id, interaction_id, event_ts, to_date(event_ts) AS event_date, process_date,
  customer_id, agent_id, survey_type, send_channel, main_score,
  CASE WHEN survey_type = 'NPS' THEN
    CASE WHEN main_score BETWEEN 0 AND 6 THEN 'Detractor'
         WHEN main_score BETWEEN 7 AND 8 THEN 'Passive'
         WHEN main_score BETWEEN 9 AND 10 THEN 'Promoter' END
  END                                                                             AS nps_category,
  nps_category_raw,
  question_1_text, question_1_response, question_2_text, question_2_response, question_3_text, question_3_response,
  open_comments, comment_sentiment, response_time_hours, campaign_response_rate,
  _source_file, _ingested_at, _bronze
FROM dedup
