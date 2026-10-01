-- Gold · customer_identity: what the identity service needs to verify a customer, and nothing more.
-- Spec: gold_tables.json. Placeholders are filled by gold_lib.render_sql; `python gold_lib.py sql customer_identity`
-- prints the runnable query. Source: silver.customers (one row per customer, pinned Delta version in the job).
--   * The document number never leaves Silver. document_hash = SHA-256 (hex) of
--     '<DOCUMENT_TYPE upper-case>|<document number upper-case, only A-Z and 0-9>' (gold_lib.document_hash is the
--     Python twin). The identity service hashes what the customer typed the same way and compares hashes; a match is
--     only the first factor, the OTP is the second (policy authentication.required_factors).
--   * DEV ONLY: an unkeyed SHA-256 of a short identifier (7 to 10 characters) can be reversed by enumeration.
--     Production would use a keyed hash (HMAC-SHA-256) with the key held in a managed secret store, or a tokenization
--     service, so that Gold alone never allows recovering a document number.
--   * Issue 9: email, phone and customer number are not identity keys and are not carried.
SELECT
  customer_id,
  document_type,
  sha2(concat(upper(trim(document_type)), '|', regexp_replace(upper(document_number), '[^A-Z0-9]', '')), 256)
                                                                              AS document_hash,
  country,
  country_code,
  customer_status,
  doc_type_inconsistent
FROM {customers}
