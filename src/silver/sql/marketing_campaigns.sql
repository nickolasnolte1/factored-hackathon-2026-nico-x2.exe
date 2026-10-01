-- Silver · marketing_campaigns: campaign catalog (one snapshot file, 200 rows). Contract: contracts/marketing_campaigns.json.
-- Placeholders are filled by silver_lib.render_sql; `python silver_lib.py sql marketing_campaigns` prints the runnable query.
-- campaign_name encodes the campaign: CMP_<objective>_<product>_<MonYYYY of start_date>_<seq>, e.g. CMP_ACQ_CC_Jan2024_0001.
-- The encoding agrees with campaign_objective, promoted_product and start_date on every row where they are present.
-- Rules found while profiling Bronze:
--   * promoted_product is NULL on 22 rows: 12 general campaigns (code GEN) and 10 whose name names a product.
--     FIX: the 10 are filled from the name code (as issue 5 fills subcategory from category); raw kept in
--     promoted_product_raw. promoted_product_en is the canonical English type (same names as products.product_type_en).
--   * target_country / target_segment NULL means no restriction. Targeting is decorative: campaign_sends reach every
--     country and segment whatever the target. target_country is folded like customers.country (issue 13).
--   * 3 'Completed' campaigns end after the as-of date 2026-06-18: flagged by a check (status_matches_dates).
WITH src AS (
  SELECT *
  FROM {catalog}.{bronze}.marketing_campaigns
  WHERE _ingested_at > TIMESTAMP '{watermark}'
  QUALIFY row_number() OVER (PARTITION BY upper(trim(campaign_id)) ORDER BY _ingested_at DESC, _source_file DESC) = 1
),
product_types (type_key, name_code, product_type, product_type_en) AS (
  VALUES ('cuenta ahorro',        'SAV', 'Cuenta Ahorro',        'Savings Account'),
         ('cuenta corriente',     'CHK', 'Cuenta Corriente',     'Checking Account'),
         ('tarjeta credito',      'CC',  'Tarjeta Crédito',      'Credit Card'),
         ('tarjeta debito',       NULL,  'Tarjeta Débito',       'Debit Card'),
         ('prestamo personal',    'PL',  'Préstamo Personal',    'Personal Loan'),
         ('prestamo hipotecario', 'MTG', 'Préstamo Hipotecario', 'Mortgage'),
         ('inversion',            'INV', 'Inversión',            'Investment'),
         ('seguro',               'INS', 'Seguro',               'Insurance')
),
typed AS (
  SELECT
    upper(trim(src.campaign_id))                                                 AS campaign_id,
    trim(src.campaign_name)                                                      AS campaign_name,
    trim(src.description)                                                        AS description,
    coalesce(try_element_at(map('email', 'Email', 'sms', 'SMS', 'whatsapp', 'WhatsApp', 'push', 'Push', 'mix', 'Mix',
                                'voice', 'Voice'), lower(trim(src.campaign_type))), trim(src.campaign_type)) AS campaign_type,
    coalesce(try_element_at(map('acquisition', 'Acquisition', 'retention', 'Retention', 'cross-sell', 'Cross-sell',
                                'up-sell', 'Up-sell', 'reactivation', 'Reactivation'),
                            lower(trim(src.campaign_objective))), trim(src.campaign_objective)) AS campaign_objective,
    upper(split_part(trim(src.campaign_name), '_', 2))                           AS name_objective_code,
    upper(split_part(trim(src.campaign_name), '_', 3))                           AS name_product_code,
    split_part(trim(src.campaign_name), '_', 4)                                  AS name_start_month,
    coalesce(p_raw.product_type, trim(src.promoted_product))                     AS promoted_product_raw,
    coalesce(p_raw.product_type, p_name.product_type)                            AS promoted_product,
    coalesce(p_raw.product_type_en, p_name.product_type_en)                      AS promoted_product_en,
    p_raw.name_code                                                              AS raw_product_code,
    coalesce(try_element_at(map('basic', 'Basic', 'plus', 'Plus', 'premium', 'Premium', 'student', 'Student'),
                            lower(trim(src.target_segment))), trim(src.target_segment)) AS target_segment,
    trim(src.target_country)                                                     AS target_country_raw,
    CASE translate(lower(trim(src.target_country)), 'áéíóú', 'aeiou')
      WHEN 'mexico' THEN 'MX' WHEN 'colombia' THEN 'CO' WHEN 'argentina' THEN 'AR' END AS target_country_code,
    try_cast(trim(src.start_date) AS DATE)                                       AS start_date,
    try_cast(trim(src.end_date) AS DATE)                                         AS end_date,
    try_cast(trim(src.budget) AS DECIMAL(18,2))                                  AS budget,
    coalesce(try_element_at(map('active', 'Active', 'paused', 'Paused', 'completed', 'Completed'),
                            lower(trim(src.campaign_status))), trim(src.campaign_status)) AS campaign_status,
    try_cast(trim(src.expected_conversion_rate) AS DOUBLE)                       AS expected_conversion_rate,
    src._source_file,
    src._ingested_at,
    struct(src.*)                                                                AS _bronze
  FROM src
  LEFT JOIN product_types p_raw ON p_raw.type_key = translate(lower(trim(src.promoted_product)), 'áéíóú', 'aeiou')
  LEFT JOIN product_types p_name ON p_name.name_code = upper(split_part(trim(src.campaign_name), '_', 3))
)
SELECT
  campaign_id, campaign_name, description,
  campaign_type, campaign_objective,
  promoted_product, promoted_product_raw, promoted_product_en,
  coalesce(promoted_product_raw IS NULL AND promoted_product IS NOT NULL, false) AS promoted_product_imputed,
  target_segment,
  CASE target_country_code WHEN 'MX' THEN 'Mexico' WHEN 'CO' THEN 'Colombia' WHEN 'AR' THEN 'Argentina'
       ELSE target_country_raw END                                               AS target_country,
  target_country_code,
  start_date, end_date,
  budget, campaign_status, expected_conversion_rate,
  coalesce(name_objective_code = CASE campaign_objective WHEN 'Acquisition' THEN 'ACQ' WHEN 'Retention' THEN 'RET'
             WHEN 'Cross-sell' THEN 'XSL' WHEN 'Up-sell' THEN 'UPS' WHEN 'Reactivation' THEN 'REA' END
           AND (promoted_product_raw IS NULL OR raw_product_code = name_product_code)
           AND name_start_month = date_format(start_date, 'MMMyyyy'), false)     AS name_matches_fields,
  _source_file, _ingested_at, _bronze
FROM typed
