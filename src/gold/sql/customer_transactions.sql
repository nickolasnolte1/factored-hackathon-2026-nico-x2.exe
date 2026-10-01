-- Gold · customer_transactions: the customer's movements as the dispute tools read them.
-- Spec: gold_tables.json. Sources: silver.transactions, silver.products (pinned Delta versions in the job).
--   * Grounding: every row keeps product_owner_matches, and the job asserts that the product's owner is the
--     transaction's customer for every row. Tools always filter by the authenticated customer_id.
--   * Issue 18: channel is NULL when the type x channel pair is implausible, so it cannot be narrated
--     (policy narration.omit_channel_when); implausible_type_channel says why.
--   * Policy flags are rendered from src/policy/dispute_policy.json (v{policy_version}), never re-typed here:
--     eligible statuses {eligible_statuses}; unrecognized-charge types {types_unrecognized};
--     incorrect-charge or fee types {types_incorrect}; handoff above {handoff_amount_usd} USD (amount_usd).
--     Ownership by the session customer and the 90-day window depend on the request and stay in the tool layer.
--   * decline_code_key joins gold.decline_codes: the code itself, '00' for Approved rows without a code (00 implied),
--     'missing' for Declined, Pending or Reversed rows without one (issue 21).
--   * Not carried: is_fraud and fraud_score (label-derived, issue 16), coordinates and city (location data),
--     branch_id, process_date and lineage columns.
SELECT
  t.transaction_id,
  t.customer_id,
  t.product_id,
  p.product_type_en,
  p.product_type_en IN ({card_types})                                         AS is_card_product,
  t.event_ts,
  t.event_date,
  t.amount,
  t.currency,
  t.amount_usd,
  t.transaction_type,
  t.transaction_category,
  t.merchant_name,
  t.merchant_category,
  CASE WHEN t.implausible_type_channel THEN NULL ELSE t.channel END           AS channel,
  t.implausible_type_channel,
  t.transaction_status,
  t.response_code,
  t.response_code_null_reason,
  CASE WHEN t.response_code IS NOT NULL THEN t.response_code
       WHEN t.response_code_null_reason = 'not_applicable' THEN '00'
       ELSE 'missing' END                                                     AS decline_code_key,
  t.transaction_country_code,
  t.is_international,
  t.product_owner_matches,
  t.product_owner_matches AND t.transaction_status IN ({eligible_statuses})
    AND t.transaction_type IN ({types_unrecognized})                          AS dispute_eligible_unrecognized,
  t.product_owner_matches AND t.transaction_status IN ({eligible_statuses})
    AND t.transaction_type IN ({types_incorrect})                             AS dispute_eligible_incorrect,
  coalesce(t.amount_usd > {handoff_amount_usd}, false)                        AS above_handoff_threshold,
  t.activity_before_opening
FROM {transactions} t
LEFT JOIN {products} p ON p.product_id = t.product_id
