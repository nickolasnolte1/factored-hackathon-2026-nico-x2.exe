-- Gold · decline_codes: team-made reference to explain a declined movement from its response code.
-- Spec: gold_tables.json. Sources: src/policy/dispute_policy.json (decline_codes, v{policy_version}) and, for the
-- observed counts only, silver.transactions and silver.products (pinned Delta versions in the job).
--   * reason, customer_message_key and cards_only come from the policy (the policy_decline_codes placeholder; the
--     policy's 'null' entry is the 'missing' key); they are never re-typed here. '00' is not a decline: it is added so that
--     every decline_code_key of gold.customer_transactions has a row.
--   * The explanation texts are team-written. Codes are templated in the source (report Q4.8): 05, 14, 51 and 54 each
--     take about 24% of declines on every product type, so a card-only code (14, 54) on an account, loan, investment
--     or insurance movement takes the insufficient-data path (non_card_explanation_*) instead of a wrong reason.
--   * Explain only Declined movements: the same codes also appear on Pending and Reversed rows.
WITH policy (code_key, reason, customer_message_key, cards_only) AS (
  VALUES {policy_decline_codes}
),
texts (code_key, meaning_en, explanation_es, explanation_pt, non_card_explanation_es, non_card_explanation_pt, note) AS (
  VALUES
    ('00', 'Approved; there is no decline to explain.',
     'La operación fue aprobada; no tuvo ningún rechazo.',
     'A operação foi aprovada; não houve nenhuma recusa.',
     NULL, NULL,
     'Not a decline. Approved rows without a code (response_code_null_reason = not_applicable) map here too.'),
    ('05', 'Do not honor: the issuer declined without giving a specific reason.',
     'La operación fue rechazada sin un motivo específico (código 05, rechazo general). Si quieres revisarla en detalle, te comunico con un asesor.',
     'A operação foi recusada sem um motivo específico (código 05, recusa genérica). Se quiser analisar em detalhe, transfiro você para um atendente.',
     NULL, NULL,
     'Generic code: never add a cause the code does not state.'),
    ('14', 'Invalid card number entered at the merchant or terminal.',
     'La operación fue rechazada porque el número de tarjeta ingresado no era válido (código 14). Revisa los datos de la tarjeta antes de intentarlo de nuevo.',
     'A operação foi recusada porque o número do cartão informado não era válido (código 14). Confira os dados do cartão antes de tentar novamente.',
     'El código de esta operación solo aplica a tarjetas y la operación se hizo con otro tipo de producto, así que no tenemos datos suficientes para explicar el rechazo. Te puedo comunicar con un asesor.',
     'O código desta operação só se aplica a cartões e a operação foi feita com outro tipo de produto, então não temos dados suficientes para explicar a recusa. Posso transferir você para um atendente.',
     'Card-only code; on a non-card product it is not enough data to explain the decline.'),
    ('51', 'Insufficient funds or available credit at the time of the movement.',
     'La operación fue rechazada por saldo o cupo insuficiente en el momento en que se hizo (código 51).',
     'A operação foi recusada por saldo ou limite insuficiente no momento em que foi feita (código 51).',
     NULL, NULL,
     'Refers to the moment of the movement, not to the current balance.'),
    ('54', 'Expired card.',
     'La operación fue rechazada porque la tarjeta figuraba como vencida (código 54).',
     'A operação foi recusada porque o cartão constava como vencido (código 54).',
     'El código de esta operación solo aplica a tarjetas y la operación se hizo con otro tipo de producto, así que no tenemos datos suficientes para explicar el rechazo. Te puedo comunicar con un asesor.',
     'O código desta operação só se aplica a cartões e a operação foi feita com outro tipo de produto, então não temos dados suficientes para explicar a recusa. Posso transferir você para um atendente.',
     'Card-only code. Templated: it falls after the card expiry in about 31% of cases, the same rate as approved card movements, so quote the code and do not infer the card state from it.'),
    ('missing', 'No response code was recorded for the movement.',
     'No tenemos el código de respuesta de esta operación, así que no podemos explicar el motivo del rechazo. Te puedo comunicar con un asesor.',
     'Não temos o código de resposta desta operação, então não podemos explicar o motivo da recusa. Posso transferir você para um atendente.',
     NULL, NULL,
     'Declined, Pending or Reversed rows without a code (response_code_null_reason = missing, about 5% of each status).')
),
observed AS (
  SELECT
    CASE WHEN t.response_code IS NOT NULL THEN t.response_code
         WHEN t.response_code_null_reason = 'not_applicable' THEN '00'
         ELSE 'missing' END                                                              AS code_key,
    count(*)                                                                             AS observed_rows,
    count_if(t.transaction_status = 'Declined')                                          AS observed_declined_rows,
    count_if(t.transaction_status = 'Declined'
             AND NOT coalesce(p.product_type_en IN ({card_types}), false))               AS observed_declined_non_card_rows
  FROM {transactions} t
  LEFT JOIN {products} p ON p.product_id = t.product_id
  GROUP BY 1
)
SELECT
  coalesce(x.code_key, pol.code_key)                                                     AS code_key,
  CASE WHEN coalesce(x.code_key, pol.code_key) = 'missing' THEN NULL
       ELSE coalesce(x.code_key, pol.code_key) END                                       AS response_code,
  coalesce(pol.reason, 'approved')                                                       AS reason,
  coalesce(pol.customer_message_key, 'approved_no_decline')                              AS customer_message_key,
  pol.code_key IS NOT NULL                                                               AS in_policy,
  coalesce(pol.cards_only, false)                                                        AS cards_only,
  CASE WHEN coalesce(pol.cards_only, false) THEN 'card' ELSE 'any' END                   AS applies_to,
  x.meaning_en,
  x.explanation_es,
  x.explanation_pt,
  x.non_card_explanation_es,
  x.non_card_explanation_pt,
  x.note,
  coalesce(o.observed_rows, 0)                                                           AS observed_rows,
  coalesce(o.observed_declined_rows, 0)                                                  AS observed_declined_rows,
  coalesce(o.observed_declined_non_card_rows, 0)                                         AS observed_declined_non_card_rows,
  '{policy_version}'                                                                     AS policy_version
FROM texts x
FULL OUTER JOIN policy pol ON pol.code_key = x.code_key
LEFT JOIN observed o ON o.code_key = coalesce(x.code_key, pol.code_key)
