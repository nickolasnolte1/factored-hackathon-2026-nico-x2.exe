-- Silver · service_agents: contact-center agent directory (one snapshot file, 1,200 rows). Contract: contracts/service_agents.json.
-- Placeholders are filled by silver_lib.render_sql; `python silver_lib.py sql service_agents` prints the runnable query.
-- Rules from docs/02_eda_workflow_selection.md, section 6:
--   * 14 QUARANTINE assigned_branch_id: 831 of 833 values match no branch. The value is nulled, the raw value is kept in
--     assigned_branch_id_raw and assigned_branch_orphan flags it.
--   * 13 country_of_origin folded to the canonical name (delivered values are already unaccented) + country_code.
-- Rules found while profiling Bronze:
--   * employee_code is not unique: 13 codes are shared by 26 agents. Flagged (employee_code_collision); key on agent_id.
--   * Mexican agents' phones carry +54 (Argentina), as for branches and customers: flagged by a check, not rewritten.
--   * languages is a comma list in Spanish: parsed into ISO 639-1 codes (language_codes) and speaks_portuguese.
--   * avg_csat runs 3.5-5.0 while surveys score CSAT 1-4; total_monthly_interactions is declared (avg 453) vs about 17
--     realized per month in interactions. Both are kept as delivered and described, never used as outcomes.
-- Branch keys are read from Bronze branches (same casting as silver.branches) so the table builds on its own.
WITH src AS (
  SELECT *
  FROM {catalog}.{bronze}.service_agents
  WHERE _ingested_at > TIMESTAMP '{watermark}'
  QUALIFY row_number() OVER (PARTITION BY upper(trim(agent_id)) ORDER BY _ingested_at DESC, _source_file DESC) = 1
),
branch_keys AS (
  SELECT DISTINCT upper(trim(branch_id)) AS branch_id
  FROM {catalog}.{bronze}.branches
  WHERE branch_id IS NOT NULL
),
-- employee_code collisions are a property of the whole directory, not of the incremental window
directory AS (
  SELECT upper(trim(agent_id)) AS agent_id, upper(trim(employee_code)) AS employee_code
  FROM {catalog}.{bronze}.service_agents
  QUALIFY row_number() OVER (PARTITION BY upper(trim(agent_id)) ORDER BY _ingested_at DESC, _source_file DESC) = 1
),
shared_codes AS (
  SELECT employee_code FROM directory WHERE employee_code IS NOT NULL GROUP BY employee_code HAVING count(*) > 1
),
specialties (specialty_key, specialty, specialty_en) AS (
  VALUES ('fraudes',           'Fraudes',           'Fraud'),
         ('cobranza',          'Cobranza',          'Collections'),
         ('retencion',         'Retención',         'Retention'),
         ('soporte tecnico',   'Soporte Técnico',   'Technical Support'),
         ('creditos',          'Créditos',          'Credit'),
         ('inversiones',       'Inversiones',       'Investments'),
         ('ventas',            'Ventas',            'Sales'),
         ('quejas y reclamos', 'Quejas y Reclamos', 'Complaints and Claims')
),
typed AS (
  SELECT
    upper(trim(src.agent_id))                                                    AS agent_id,
    upper(trim(src.employee_code))                                               AS employee_code,
    sc.employee_code IS NOT NULL                                                 AS employee_code_collision,
    trim(src.first_name)                                                         AS first_name,
    trim(src.last_name)                                                          AS last_name,
    lower(trim(src.email))                                                       AS email,
    trim(src.phone)                                                              AS phone,
    lower(trim(src.native_accent))                                               AS native_accent,
    CASE translate(lower(trim(src.country_of_origin)), 'áéíóú', 'aeiou')
      WHEN 'mexico' THEN 'MX' WHEN 'colombia' THEN 'CO' WHEN 'argentina' THEN 'AR' END AS country_code,
    trim(src.country_of_origin)                                                  AS country_of_origin_raw,
    upper(trim(src.assigned_branch_id))                                          AS assigned_branch_id_raw,
    b.branch_id                                                                  AS assigned_branch_id,
    coalesce(try_element_at(map('phone', 'Phone', 'digital', 'Digital', 'in-person', 'In-Person', 'hybrid', 'Hybrid'),
                            lower(trim(src.agent_type))), trim(src.agent_type))  AS agent_type,
    coalesce(try_element_at(map('junior', 'Junior', 'mid-senior', 'Mid-Senior', 'senior', 'Senior', 'specialist', 'Specialist'),
                            lower(trim(src.experience_level))), trim(src.experience_level)) AS experience_level,
    trim(src.languages)                                                          AS languages,
    filter(transform(split(translate(lower(trim(src.languages)), 'áéíóúñ', 'aeioun'), ','),
                     x -> coalesce(try_element_at(map('espanol', 'es', 'ingles', 'en', 'portugues', 'pt'), trim(x)), trim(x))),
           x -> x <> '')                                                         AS language_codes,
    coalesce(sp.specialty, trim(src.specialty))                                  AS specialty,
    sp.specialty_en,
    try_cast(trim(src.hire_date) AS DATE)                                        AS hire_date,
    try_cast(trim(src.avg_csat) AS DOUBLE)                                       AS avg_csat,
    try_cast(regexp_replace(trim(src.total_monthly_interactions), '[.]0+$', '') AS INT) AS total_monthly_interactions,
    coalesce(try_element_at(map('active', 'Active', 'vacation', 'Vacation', 'leave', 'Leave', 'inactive', 'Inactive'),
                            lower(trim(src.agent_status))), trim(src.agent_status)) AS agent_status,
    coalesce(try_element_at(map('morning', 'Morning', 'afternoon', 'Afternoon', 'night', 'Night', 'rotating', 'Rotating'),
                            lower(trim(src.work_shift))), trim(src.work_shift))  AS work_shift,
    src._source_file,
    src._ingested_at,
    struct(src.*)                                                                AS _bronze
  FROM src
  LEFT JOIN branch_keys b ON b.branch_id = upper(trim(src.assigned_branch_id))
  LEFT JOIN shared_codes sc ON sc.employee_code = upper(trim(src.employee_code))
  LEFT JOIN specialties sp ON sp.specialty_key = translate(lower(trim(src.specialty)), 'áéíóú', 'aeiou')
)
SELECT
  agent_id, employee_code, employee_code_collision,
  first_name, last_name, email, phone,
  native_accent,
  CASE country_code WHEN 'MX' THEN 'Mexico' WHEN 'CO' THEN 'Colombia' WHEN 'AR' THEN 'Argentina'
       ELSE country_of_origin_raw END                                            AS country_of_origin,
  country_code,
  assigned_branch_id, assigned_branch_id_raw,
  assigned_branch_id_raw IS NOT NULL AND assigned_branch_id IS NULL              AS assigned_branch_orphan,
  agent_type, experience_level,
  languages, language_codes,
  coalesce(array_contains(language_codes, 'pt'), false)                          AS speaks_portuguese,
  specialty, specialty_en,
  hire_date, avg_csat, total_monthly_interactions,
  agent_status, work_shift,
  _source_file, _ingested_at, _bronze
FROM typed
