-- Silver · branches: branch directory (one snapshot file, 350 rows). Contract: contracts/branches.json.
-- Placeholders are filled by silver_lib.render_sql; `python silver_lib.py sql branches` prints the runnable query.
-- Rules found while profiling Bronze (not in the EDA issue list):
--   * Colombian postal codes of Medellín (05xxxx) and Barranquilla (08xxxx) lost their leading zero (39 rows):
--     FIX postal_code = lpad to 6 digits, raw kept in postal_code_raw.
--   * Every branch in 7 of 16 cities (167 of 350) has placeholder coordinates within 0.1 degrees of (0, 0):
--     latitude/longitude are NULL unless inside the branch's country; raw values kept; has_valid_coordinates flags it.
--   * Mexican branch phones carry +54 (Argentina) instead of +52: flagged by a check, not rewritten.
WITH src AS (
  SELECT *
  FROM {catalog}.{bronze}.branches
  WHERE _ingested_at > TIMESTAMP '{watermark}'
  QUALIFY row_number() OVER (PARTITION BY upper(trim(branch_id)) ORDER BY _ingested_at DESC, _source_file DESC) = 1
),
typed AS (
  SELECT
    upper(trim(branch_id))                                                   AS branch_id,
    upper(trim(branch_code))                                                 AS branch_code,
    trim(branch_name)                                                        AS branch_name,
    trim(branch_type)                                                        AS branch_type,
    trim(branch_status)                                                      AS branch_status,
    trim(country)                                                            AS country,
    CASE translate(lower(trim(country)), 'áéíóú', 'aeiou')
      WHEN 'mexico' THEN 'MX' WHEN 'colombia' THEN 'CO' WHEN 'argentina' THEN 'AR' END AS country_code,
    trim(state)                                                              AS state,
    trim(city)                                                               AS city,
    trim(address)                                                            AS address,
    upper(trim(postal_code))                                                 AS postal_code_raw,
    trim(geographic_zone)                                                    AS geographic_zone,
    try_cast(trim(latitude) AS DOUBLE)                                       AS latitude_raw,
    try_cast(trim(longitude) AS DOUBLE)                                      AS longitude_raw,
    trim(phone)                                                              AS phone,
    lower(trim(email))                                                       AS email,
    trim(opening_time)                                                       AS opening_time,
    trim(closing_time)                                                       AS closing_time,
    try_cast(trim(has_atms) AS BOOLEAN)                                      AS has_atms,
    try_cast(regexp_replace(trim(atm_count), '[.]0+$', '') AS INT)           AS atm_count,
    try_cast(trim(has_teller_windows) AS BOOLEAN)                            AS has_teller_windows,
    try_cast(regexp_replace(trim(teller_window_count), '[.]0+$', '') AS INT) AS teller_window_count,
    try_cast(trim(branch_opening_date) AS DATE)                              AS branch_opening_date,
    _source_file,
    _ingested_at,
    struct(src.*)                                                            AS _bronze
  FROM src
),
geo AS (
  SELECT *,
    coalesce(CASE country_code  -- loose national bounding boxes
      WHEN 'MX' THEN latitude_raw BETWEEN 14.0 AND 33.0 AND longitude_raw BETWEEN -119.0 AND -86.0
      WHEN 'CO' THEN latitude_raw BETWEEN -5.0 AND 14.0 AND longitude_raw BETWEEN -82.0 AND -66.0
      WHEN 'AR' THEN latitude_raw BETWEEN -56.0 AND -21.0 AND longitude_raw BETWEEN -74.0 AND -53.0
    END, false) AS has_valid_coordinates
  FROM typed
)
SELECT
  branch_id, branch_code, branch_name, branch_type, branch_status,
  country, country_code, state, city, address,
  CASE WHEN country_code = 'CO' AND postal_code_raw RLIKE '^[0-9]{5}$' THEN lpad(postal_code_raw, 6, '0')
       ELSE postal_code_raw END                                            AS postal_code,
  postal_code_raw,
  geographic_zone,
  CASE WHEN has_valid_coordinates THEN latitude_raw END                    AS latitude,
  CASE WHEN has_valid_coordinates THEN longitude_raw END                   AS longitude,
  latitude_raw, longitude_raw, has_valid_coordinates,
  phone, email, opening_time, closing_time,
  has_atms, atm_count, has_teller_windows, teller_window_count,
  branch_opening_date,
  _source_file, _ingested_at, _bronze
FROM geo
