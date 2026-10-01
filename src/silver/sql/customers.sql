-- Silver · customers: customer master (one snapshot file, 150,000 rows). Contract: contracts/customers.json.
-- Placeholders are filled by silver_lib.render_sql; `python silver_lib.py sql customers` prints the runnable query.
-- Rules from docs/02_eda_workflow_selection.md, section 6:
--   * 13 FIX country: accents folded to the canonical name (Mexico, Colombia, Argentina) + country_code; raw kept.
--   * 11 FLAG doc_type_inconsistent: every Mexican customer carries 'DNI' (an Argentine document). Kept as delivered.
--   *  9 email is NOT an identity key (54% of customers share one); the natural key is document type + number.
--   * 14 QUARANTINE registration_branch_id: 149,995 of 150,000 values match no branch. The value is nulled, the raw
--     value is kept in registration_branch_id_raw and registration_branch_orphan flags it.
--   * 21 credit_score / estimated_monthly_income: NULL is never imputed; *_null_reason says 'missing' or 'invalid'.
--   * last_updated later than the snapshot as-of date (2026-06-18) is flagged (last_updated_after_as_of).
--   * 20 customer_status vs Active products is flagged at product grain in silver.products (customer_status_conflict).
-- Rules found while profiling Bronze (same defects as branches):
--   * Medellín and Barranquilla postal codes lost their leading zero (16,390 rows): FIX lpad to 6, raw in postal_code_raw.
--   * Mexican phone numbers carry +54 (Argentina) instead of +52: flagged by a check, not rewritten.
-- Branch keys are read from Bronze branches (same casting as silver.branches) so the table builds on its own.
WITH src AS (
  SELECT *
  FROM {catalog}.{bronze}.customers
  WHERE _ingested_at > TIMESTAMP '{watermark}'
  QUALIFY row_number() OVER (PARTITION BY upper(trim(document_type)), upper(trim(document_number))
                             ORDER BY _ingested_at DESC, _source_file DESC) = 1
),
branch_keys AS (
  SELECT DISTINCT upper(trim(branch_id)) AS branch_id
  FROM {catalog}.{bronze}.branches
  WHERE branch_id IS NOT NULL
),
typed AS (
  SELECT
    upper(trim(src.customer_id))                                                   AS customer_id,
    coalesce(try_element_at(map('dni', 'DNI', 'cc', 'CC', 'ce', 'CE', 'pasaporte', 'Pasaporte'),
                            lower(trim(src.document_type))), trim(src.document_type)) AS document_type,
    upper(trim(src.document_number))                                               AS document_number,
    trim(src.first_name)                                                           AS first_name,
    trim(src.last_name)                                                            AS last_name,
    try_cast(trim(src.date_of_birth) AS DATE)                                      AS date_of_birth,
    upper(trim(src.gender))                                                        AS gender,
    lower(trim(src.email))                                                         AS email,
    trim(src.mobile_phone)                                                         AS mobile_phone,
    trim(src.landline_phone)                                                       AS landline_phone,
    trim(src.address)                                                              AS address,
    trim(src.city)                                                                 AS city,
    trim(src.state)                                                                AS state,
    trim(src.country)                                                              AS country_raw,
    CASE translate(lower(trim(src.country)), 'áéíóú', 'aeiou')
      WHEN 'mexico' THEN 'MX' WHEN 'colombia' THEN 'CO' WHEN 'argentina' THEN 'AR' END AS country_code,
    upper(trim(src.postal_code))                                                   AS postal_code_raw,
    lower(trim(src.detected_accent))                                               AS detected_accent,
    coalesce(try_element_at(map('basic', 'Basic', 'plus', 'Plus', 'premium', 'Premium', 'student', 'Student'),
                            lower(trim(src.segment))), trim(src.segment))          AS segment,
    try_cast(regexp_replace(trim(src.credit_score), '[.]0+$', '') AS INT)          AS credit_score,
    try_cast(trim(src.estimated_monthly_income) AS DECIMAL(18,2))                  AS estimated_monthly_income,
    trim(src.occupation)                                                           AS occupation,
    coalesce(try_element_at(map('married', 'Married', 'single', 'Single', 'divorced', 'Divorced', 'widowed', 'Widowed'),
                            lower(trim(src.marital_status))), trim(src.marital_status)) AS marital_status,
    coalesce(try_element_at(map('elementary', 'Elementary', 'high school', 'High School', 'college prep', 'College Prep',
                                'university', 'University', 'graduate', 'Graduate'),
                            lower(trim(src.education_level))), trim(src.education_level)) AS education_level,
    try_cast(trim(src.registration_date) AS TIMESTAMP)                             AS registration_date,
    upper(trim(src.registration_branch_id))                                        AS registration_branch_id_raw,
    b.branch_id                                                                    AS registration_branch_id,
    coalesce(try_element_at(map('active', 'Active', 'inactive', 'Inactive', 'suspended', 'Suspended', 'closed', 'Closed'),
                            lower(trim(src.customer_status))), trim(src.customer_status)) AS customer_status,
    try_cast(trim(src.last_updated) AS TIMESTAMP)                                  AS last_updated,
    try_cast(trim(src.accepts_marketing) AS BOOLEAN)                               AS accepts_marketing,
    nullif(trim(src.credit_score), '') IS NULL                                     AS credit_score_raw_missing,
    nullif(trim(src.estimated_monthly_income), '') IS NULL                         AS income_raw_missing,
    src._source_file,
    src._ingested_at,
    struct(src.*)                                                                  AS _bronze
  FROM src
  LEFT JOIN branch_keys b ON b.branch_id = upper(trim(src.registration_branch_id))
)
SELECT
  customer_id, document_type, document_number,
  first_name, last_name, date_of_birth, gender,
  email, mobile_phone, landline_phone, address, city, state,
  CASE country_code WHEN 'MX' THEN 'Mexico' WHEN 'CO' THEN 'Colombia' WHEN 'AR' THEN 'Argentina' ELSE country_raw END AS country,
  country_raw, country_code,
  CASE WHEN country_code = 'CO' AND postal_code_raw RLIKE '^[0-9]{5}$' THEN lpad(postal_code_raw, 6, '0')
       ELSE postal_code_raw END                                                    AS postal_code,
  postal_code_raw,
  detected_accent, segment,
  credit_score,
  CASE WHEN credit_score_raw_missing THEN 'missing' WHEN credit_score IS NULL THEN 'invalid' END AS credit_score_null_reason,
  estimated_monthly_income,
  CASE WHEN income_raw_missing THEN 'missing' WHEN estimated_monthly_income IS NULL THEN 'invalid' END
                                                                                   AS estimated_monthly_income_null_reason,
  occupation, marital_status, education_level,
  registration_date,
  registration_branch_id, registration_branch_id_raw,
  registration_branch_id_raw IS NOT NULL AND registration_branch_id IS NULL        AS registration_branch_orphan,
  customer_status,
  last_updated,
  coalesce(to_date(last_updated) > DATE '2026-06-18', false)                       AS last_updated_after_as_of,
  accepts_marketing,
  coalesce((country_code = 'MX' AND document_type IN ('DNI', 'CC', 'CE'))
        OR (country_code = 'CO' AND document_type = 'DNI')
        OR (country_code = 'AR' AND document_type IN ('CC', 'CE')), false)         AS doc_type_inconsistent,
  _source_file, _ingested_at, _bronze
FROM typed
