# 01 — Initial Data Findings

_Exploratory profiling of a random 40-day sample (of 1,097 daily partitions) taken on 2026-09-25. Figures will be re-computed on the full dataset in the Silver layer._

## Dataset at a glance

- 13 tables, ~19M rows, ~10 GB of CSV in S3, daily partitions `year=/month=/day=` for fact tables.
- Synthetic data for Mexico, Colombia and Argentina, June 2023 – June 2026.
- The bucket contains `data/` and a `data_backup_20260831/` snapshot whose file counts and sizes differ (e.g. `transactions`: 1,097 vs 453 files) — to be analyzed as a late-arrival / schema-evolution scenario.

## Contact demand (call_center_interactions, n = 26,868)

| Reason category | Share | First-contact resolution | Requires follow-up | Escalated |
|---|---|---|---|---|
| Transaccional | 34.9% | 91.3% | 21.9% | 10.3% |
| Producto | 21.9% | 89.0% | 24.7% | 9.7% |
| Queja | 17.1% | **43.0%** | **63.2%** | 10.4% |
| Técnico | 15.0% | 70.0% | 40.6% | 9.1% |
| Comercial | 8.1% | 65.8% | 43.6% | 9.9% |
| Retención | 3.0% | 59.2% | 49.6% | 12.0% |

Channel mix is dominated by phone (84.6%).

Complaints (n = 2,438) split almost evenly across subcategories: *Cargo no reconocido*, *Cobro indebido*, *Problema con app*, *Atención en sucursal*, *Calidad de servicio* (10.6% null subcategory).

## Data quality issues that shape the design

1. **No Portuguese data.** All text is Spanish (`detected_language = es` in 100% of sampled transcripts). Portuguese coverage must come from clearly labeled, team-generated test cases, and is reported as a limitation.
2. **Transcripts are templated and not label-bearing.** 6,423 sampled transcripts contain only 511 distinct texts and **2 distinct customer openings**, independent of `main_topics`. 100% contain unfilled placeholders (`{monto}`, `{moneda}`). `detected_intents` is `consulta_general` for 95% of rows. A text classifier trained on these transcripts would learn noise or template leakage.
3. **Complaint descriptions are templated** (`"Queja relacionada con fees"`); structured fields (category, amount, SLA, resolution) carry the signal.
4. **Redundant columns:** `contact_reason` is identical to `reason_category`.
5. **Missing accents:** ~30% of interactions have no `customer_detected_accent`, ~37% of transcripts no `detected_accent`.
6. Announced by the organizers: ~2% duplicates, ~5% nulls, late arrivals, schema evolution. To be quantified per table with data-quality expectations.

## Implications

- Workflow choice is driven by structured operational data (FCR, follow-up, complaint categories, transactions), not transcript text.
- The learned component should be trained on label-bearing structured data (e.g. transaction `is_fraud`), with a documented baseline and leakage-safe temporal splits.
- Intent understanding for the conversational layer is evaluated on a labeled, team-generated ES/PT test set, reported separately from organizer data.
