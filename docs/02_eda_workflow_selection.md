# 02 — EDA and Workflow Selection

_Full-data profiling of the Bronze layer (raw, pre-dedup, every row) run on 2026-09-28. Five dimensions were analyzed (contact center, complaints, transactions, products/credit, data quality). The key findings (40 of 75) were re-derived by a separate verification pass with independent SQL, and the baseline queries were re-run for this report. Where two passes disagreed, the verified value is used. Figures marked † come from a single pass and are not reproduced in the notebook. Every query is reproducible in [`src/analysis/01_eda_workflow_selection.py`](../src/analysis/01_eda_workflow_selection.py) (cell ids `Q<section>.<n>` below)._

## Summary

- **Recommendation: transaction-dispute intake** (unrecognized charges and incorrect charges or fees), grounded on the customer's own products and transactions, with account/payment lookups embedded as tools. **Runner-up: account/payment inquiries.**
- Complaint calls (`Queja`) are 17.1% of contacts but 41.2% of all unresolved contacts. Their first-contact resolution (FCR) is 43.6%, against 91.5% for transactional inquiries.
- Historical complaint records cannot ground or label a dispute: 0 of 44,570 complaint product links belong to the complainant, and 0 of 21,751 claimed amounts match any of the customer's transactions. Transactions, on the other hand, are clean (100% ownership integrity, 0 duplicates), so the flow is grounded on them.
- No historical label in the dataset carries learnable signal (fraud, delinquency, SLA breach, satisfaction, escalation, recontact). The learned component is therefore an ES/PT intake-understanding classifier with labels that are valid by construction, compared against majority-class and keyword-rule baselines.

## 1. Context and method

- **Scope:** 12 Bronze tables, 7,874,194 rows (`digital_events` not loaded). Event window 2023-06-17 to 2026-06-18. Jul 2024 – Jun 2025 ("FY2") is the only full 12-month window. (Q1.1, Q1.2)
- **Typing:** every Bronze column is a string. Values are cast with `try_cast`, booleans are compared with `= 'True'`, and nulls are kept out of denominators unless stated.
- **Bronze-level numbers:** raw and pre-dedup. Deduplication should not move them: 0 content duplicates were found on the natural keys of the fact tables and of customers (Section 6).
- **Cost proxy:** handle time exists only for voice and video (590,062 of 686,296 contacts). Wait time exists only for inbound calls (480,678).
- **Reason-to-workflow mapping is an assumption.** No field links a call to a card, an account or a dispute case (Section 3), so each workflow is sized through a contact-reason proxy:

| Hackathon workflow | Contact-reason proxy (n, share of 686,296) | Other evidence |
|---|---|---|
| Account/payment inquiries | Transaccional (240,056; 35.0%) | transactions, balances |
| Card support | none: no card reason code exists | 35.0% of 400,000 products are cards; 221,234 declined transactions |
| Transaction-dispute intake | Queja (117,021; 17.1%) | complaints: Transactions + Fees = 27,133 of 67,095 cases (40.4%) |
| Credit info & eligibility | Comercial (54,879; 8.0%) | credit score, income, credit products |

## 2. Demand and pain by contact reason

Full data, n = 686,296 contacts. CSAT is on the 1–4 scale actually present in the data (Q2.1, Q2.2).

| Reason | Contacts (share) | FCR | Strict FCR¹ | Unresolved (share of 160,266) | AHT p50 / p90 s (n with duration) | Handle-hours (share of 52,688) | CSAT mean (n) | CSAT ≤ 2 |
|---|---|---|---|---|---|---|---|---|
| **Queja** | 117,021 (17.1%) | **43.6%** | 33.2% | **66,000 (41.2%)** | 431 / 608 (100,727) | 12,160 (23.1%) | **2.43** (21,843) | 54.5% |
| Técnico | 102,899 (15.0%) | 69.9% | 53.4% | 30,940 (19.3%) | 360 / 499 (88,493) | 8,861 (16.8%) | 2.70 (19,193) | 36.4% |
| Transaccional | 240,056 (35.0%) | 91.5% | 70.1% | 20,385 (12.7%) | 205 / 333 (206,465) | 12,663 (24.0%) | 2.91 (44,837) | 20.8% |
| Comercial | 54,879 (8.0%) | 65.2% | 50.0% | 19,093 (11.9%) | 540 / 747 (47,082) | 7,061 (13.4%) | 2.66 (10,243) | 39.1% |
| Producto | 150,863 (22.0%) | 89.6% | 68.6% | 15,650 (9.8%) | 263 / 379 (129,644) | 9,593 (18.2%) | 2.90 (27,942) | 22.2% |
| Retención | 20,578 (3.0%) | 60.2% | 45.8% | 8,198 (5.1%) | 478 / 665 (17,651) | 2,350 (4.5%) | 2.61 (3,798) | 42.9% |
| **All** | 686,296 | 76.6% | 58.7% | 160,266 | 291 / 538 (590,062) | 52,688 | 2.77 (127,856) | 31.3% |

¹ Strict FCR = resolved AND not escalated AND no follow-up. It is shown because it will be asked about, but it adds no information: both flags are rule-generated (observation 7).

1. **Queja is where resolution fails.** It is 17.1% of volume but 41.2% of unresolved contacts and 23.1% of handle-hours, and its median handle time is 2.1× that of Transaccional (431 vs 205 s).
2. **Transaccional is the largest block but already works.** It holds 35.0% of volume and 24.0% of handle-hours, yet resolves 91.5% of contacts at first contact. Its case is containment and handle time, with only 8.5 pp of FCR headroom.
3. **Comercial (credit proxy) has the longest calls:** p50 540 s, 13.4% of handle-hours on 8.0% of volume.
4. **FCR depends only on the reason.** It is 76.5–77.8% by channel/type (n from 3,395 to 480,678), 76.59–76.67% by customer country, and 43.66% (n = 6,949) vs 43.60% (n = 110,072) for Queja handled by a complaints specialist vs anyone else. Per-agent dispersion equals binomial noise: reason-adjusted χ² = 1,134.8 on 1,089 df across 1,090 agents (Q2.5, Q2.6). There is no routing or agent lever to exploit, and a tabular FCR model cannot beat the per-reason prior.
5. **Satisfaction is a fixed function of `was_resolved`.** Resolved contacts score CSAT {2, 3, 4} (mean 2.999, n = 97,851) and unresolved contacts {1, 2, 3} (mean 2.003, n = 30,005), with the same split in every reason. After removing that effect, segment, country and agent residuals are about 0 (Q2.3). CSAT is therefore reported as a consequence of FCR (+1 pp FCR ≈ +0.01 CSAT) and never used as a label alongside `was_resolved`.
6. **Survey scales differ from the dictionary.** CSAT and CES run 1–4 and NPS runs 2–7, so there are 0 promoters and standard NPS is a meaningless −74.5 (n = 63,668). Surveys cover 212,759 of 686,296 contacts (31.0%), 30.9–31.3% in each reason (Q2.4).
7. **Follow-up and escalation are rule-generated.** `requires_followup` is True for 160,266 of 160,266 unresolved contacts and for 14.98% of resolved ones (78,788 of 526,030). Escalation is a flat 9.96% (68,386 of 686,296), 9.84–10.07% in every reason, and independent of resolution (Q2.7).
8. **Recontact carries no signal.** Same-reason 7-day recontact is 0.67% (n = 681,690). Any-reason 7-day recontact is 2.88%, against 2.91% expected from random arrivals. Within every reason, unresolved and resolved contacts differ by at most 0.09 pp; the pooled gap (0.49% vs 0.73%) is a Simpson's-paradox mix effect (Q2.8). Recontact is neither a KPI nor a label.
9. **Demand is flat except by weekday.** Every hour of the day carries 4.12–4.24% of volume (flat around the clock). Tuesday to Friday average 724.6–731.2 contacts per day against 367.3 on Sunday (156–157 days each) (Q2.9). Year-over-year growth is +0.3% and +0.6%†, monthly volume varies ±3%†, and the reason mix is identical across Mexico, Colombia and Argentina (±0.2 pp)†.
10. **Channel mix is 85.0% phone** (583,250 of 686,296; inbound 480,678, 70.0%), and outcomes are identical across channels, so the data does not show that any channel resolves better.
11. **The interactions table looks like a sample.** Agents declare 453.0 interactions per month on average against 17.3 realized (1,200 agents, 1,090 with interactions; Q2.10). Savings are therefore reported as rates and shares, not FTE counts.

## 3. Complaints and dispute grounding

**Volume and mix** (n = 67,095; Q3.1):

- Case types: Complaint 60.3%, Claim 24.7%, Request 10.1%, Suggestion 4.9%. The five categories are 19.7–20.2% each (uniform, synthetic). Subcategory is a 1:1 function of category, so its 6,698 nulls (9.98%) can be imputed.
- **Dispute slice** (Transactions → *Cargo no reconocido*, Fees → *Cobro indebido*): 27,133 of 67,095 cases (40.4%), or 24,491 (36.5%) counting only rows with an explicit subcategory. That is 753.8 per full month over 35 months (range 676–812).
- Channels: Call Center 50.3%, Email 19.9%, Web 14.7%, App 10.0%, Branch 4.0%, Regulator 1.07% (717).

**Case-process baseline** (Q3.2):

| Group | n | Open / In Process / Escalated | Assignment p50 (n with date) | First response p50 / p90 (n with date) | First response > 48 h | Resolution p50 / p90 (n) | Amount + currency present |
|---|---|---|---|---|---|---|---|
| Disputes (Transactions + Fees) | 27,133 | 74.9% | 12.0 h (17,874) | 37 h / 58 h (16,620; 61.3%) | 26.2% | 15 d / 27 d (6,242) | 31.4% |
| Other categories | 39,962 | 74.9% | 12.0 h (26,093) | 38 h / 58 h (24,233; 60.6%) | 26.7% | 16 d / 28 d (9,121) | 30.5% |

Timing is identical across categories, so the data cannot show that disputes are harder than other complaints. The dispute case rests on volume, contact-level pain and case quality.

**Outcome fields are noise, not labels.** The `sla_breached` flag is 20.11% (13,495 of 67,095) and stays at 18.5–21.4% across resolution-time buckets, 19.2–20.5% across priorities and about 20% across first-response buckets (Q3.7). The status mix does not change with case age: 70.7% of 2023 cases are still Open or In Process, versus 69.4% of 2026 cases, and 45,629 of 46,948 open cases are older than 30 days (Q3.8). Satisfaction and repeat-complainer flags are equally flat, and compensation is granted to 28.8% of 16,121 resolved or closed cases regardless of the resolution text.

**Grounding checks** (Q3.3–Q3.6, Q3.10, Q4.9, Q6.2):

| Check | Result |
|---|---|
| `complaints.customer_id` found in customers | 67,095 / 67,095 |
| `affected_product_id` present / found in products | 44,570 / 67,095 (66.4%); 44,570 / 44,570 |
| … owned by the complainant | **0 / 44,570** |
| Control: transaction customer = product owner | 4,425,008 / 4,425,008 |
| `origin_interaction_id` present | **0 / 67,095** |
| Same-day contact by the complainant | 310 / 67,095 (0.46%) vs 0.42% expected by chance |
| `claimed_amount` equals one of the complainant's transactions (±0.01, any date) | **0 / 21,751** |
| Claim currency is the customer's home currency | 5,429 / 21,776 (24.9%) |
| Transaction on the named product in the 30 days before a dispute | 25.99% vs 25.43% in a placebo window 180–210 days earlier (n = 13,285) |
| Dispute within 30 days after a fraud transaction | 0.43% (18 / 4,186) vs 0.45% after approved transactions (17,844 / 3,936,022) |
| `mentioned_products` in interactions: exist / owned by the caller | 3,562 / 548,680 (0.65%); 0 / 3,562 |

`claimed_amount` is Uniform(50–5,000) in every currency, and USD-tagged rows account for 94.2% of the USD-converted total, so no "money at stake" figure can be reported. Descriptions are 5 fixed strings, one per category (`"Queja relacionada con <category>"`) (Q3.9, Q3.10).

**Conclusion.** Complaints can size demand and provide the timing baseline. They cannot supply the disputed transaction, the product, the originating contact or an outcome label, and calls cannot be linked to cases. The intake must therefore build the case from the customer's own transactions, and the claim "Queja calls are disputes" stays an explicit assumption.

## 4. Transactions and labels

- **Clean grain** (n = 4,425,008; Q4.1). Every product and customer key resolves, and the product owner equals the transaction customer in 100% of rows. There are 0 duplicate groups on (customer, product, timestamp, amount), 0 amounts that fail to cast or are ≤ 0, and 0 rescued rows.
- **No late arrivals** (Q4.2). `process_date` is a business day with a fixed cut-off. For transactions, 1,106,307 rows (25.0%) carry the previous day, all but 51 of them events between 00:00 and 05:59. Interactions carry the previous day in 228,318 of 686,296 rows (33.3%; cut-off 08:00, 7 exceptions) and complaints in 22,585 of 67,095 (33.7%; 1 exception). No row carries a later day. All trends and splits use event time.
- **The candidate set is tiny** (Q4.11). For customers with products (n = 139,578), the number of own transactions in the 30 days before three reference dates is p50 1, p90 2, p99 4, max 10, and 52.3–52.8% have at least one. Finding the disputed transaction is a lookup plus customer confirmation, not a learning problem.
- **`is_fraud` is unlearnable** (Q4.3–Q4.5). Prevalence is 0.0975% (4,316 of 4,425,008), flat by channel (94.8–107.9 per 100k), type (89.0–109.0), hour (χ² 17.2 on 23 df), month (41.6 on 36) and 139 channel × type × merchant × country cells (135.2 on 138). A temporal test (train before 2025-07-01; test 1,302 frauds and 1,429,109 non-frauds) scores AUC 0.504 for a target-encoded cell model and 0.4997 for a naive-Bayes model with 11 features, with a standard error of about 0.008. Both are chance.
- **`fraud_score` is built from the label** (Q4.6, Q4.7). Non-fraud scores never exceed 30.00 and fraud scores span 0–100, which gives AUC 0.847 (3,425 frauds vs 3,536,426 non-frauds) and precision 1.000 for every score above 30 (2,373 flagged). It is null in 20.0% of rows. It must never be a feature or a "baseline to beat".
- **Decline codes are templated** (Q4.8). Declines are 221,234 (5.00%). Codes 51, 14, 54 and 05 each take 23.6–23.9% of declines and 4.95% have no code; about 5% of every status has a null code. Code 54 ("expired card") falls after card expiry in 31.40% of cases, the same as approved card transactions (31.43%). Explanations must come from a code-to-message table.
- **Transaction events do not drive contacts** (Q4.9). A contact within 7 days follows 3.17% of frauds (137 of 4,316), 2.85% of declines (6,310 of 221,019), 2.85% of reversals and 2.87% of all other transactions (119,256 of 4,154,959); z = 1.2 for fraud vs other.
- **Currency** (Q4.10). All products and transactions of Mexican customers are in USD (200,398 products, 2,216,431 transactions); MXN appears only in complaints (5,487) and the FX table. `amount_usd` equals `round(amount / 350, 2)` for 100% of 752,544 ARS rows and `round(amount / 4000, 2)` for 100% of 1,135,008 COP rows, so it uses a fixed rate, not the daily FX table. It is null for all 2,437,979 USD rows and for 99,477 of 1,987,029 non-USD rows (5.0%).
- **Implausible combinations** (Q4.12). 1,240,000 of 4,425,008 transactions (28.0%) have a type × channel pair that cannot happen, such as a withdrawal at a POS or a purchase at an ATM.

## 5. Products, credit and cards

- **Book** (Q5.1). 400,000 products: savings 30.1%, credit cards 25.0%, current accounts 25.0%, debit cards 10.0%, personal loans 5.0%, mortgages 3.0%, investments 1.5%, insurance 0.5%. Product types are stored in Spanish (`Tarjeta Crédito`, …), not in the English of the dictionary. The status mix (84.5–85.7% Active, 4.69–5.32% Blocked in every type) and app linkage (49.65–50.94%) do not depend on product type.
- **Only Active products transact** (Q5.2): 339,963 of 339,965 Active products have transactions, against 0 of 60,035 Closed, Blocked or Suspended ones. The data never shows a blocked-card attempt.
- **Card lifecycle is inconsistent** (Q5.3). 40,484 of 80,864 Active credit cards with an expiry date (50.1%) and 16,180 of 32,141 Active debit cards (50.3%) are already past `expiration_date` at data end. 18.7% of all transactions (827,610) predate the product's `opening_date`.
- **`last_transaction_date` is wrong** (Q5.4): it matches the real last transaction for 427 of 305,721 products (0.14%), with a median gap of 471 days.
- **No credit-risk signal** (Q5.5, Q5.6). `dpd > 90` is 4.94% (6,189 of 125,350 credit products) and flat across score bands (4.76–5.05%), segments (4.79–5.03%) and countries. Credit score ranks delinquency at AUC 0.504 (5,268 positives, 101,637 negatives), and `days_past_due` takes only 7 distinct values.
- **Limits and rates ignore the customer** (Q5.7). Within product × currency cells, correlations of limit or rate with income or score range from −0.032 to 0.016. Mexican credit cards have a median limit of 25,604–26,214 USD in every segment, Student included. Rates sit in fixed ranges per type (credit card 18–45%, personal loan 12–28%, mortgage 6–12%), and these ranges overlap: 37.0% of credit-card rates fall in 18–28%.
- **Eligibility inputs are incomplete** (Q5.9): credit score is null for 22,492 of 150,000 customers (15.0%), income for 30,033 (20.0%), and 48,002 (32.0%) lack at least one.
- **`customer_status` is decorative** (Q5.8). Closed or Inactive is 11.93% (17,893 of 150,000) and flat by complaint count (11.41–12.09%) and by unresolved contacts (11.71–12.16%). 90.4% of Closed customers still hold an Active product, and the status has no timestamp. It is not a churn label.

**Implications.** Credit eligibility has no valid label, so any eligibility logic has to be a documented rule policy served by a synthetic policy service, not by a model. Card support can be grounded on status, expiry and declines, but its demand cannot be measured and its actions (block, replace) would all need mocked tools.

## 6. Data-quality issues and proposed Silver rules

FIX = derive a corrected column (raw value kept); FLAG = keep the value and add a boolean flag; QUARANTINE = null the value (raw copy kept) and exclude it from joins; DROP = remove the column from Silver and from feature sets.

| # | Issue | Evidence (n / denominator) | Impact on the workflow | Silver rule |
|---|---|---|---|---|
| 1 | Complaint ↔ contact link missing | `origin_interaction_id` 0 / 67,095; same-day re-link 0.46% vs 0.42% chance | No call-to-case trace | FLAG `link_status = 'unlinked'`; no fuzzy re-linking |
| 2 | Complaint product belongs to someone else | 44,570 / 44,570 | Wrong product cited in a dispute | QUARANTINE `affected_product_id` |
| 3 | Claimed amount and currency are random | 0 / 21,751 amount matches; currency neither home nor USD 10,916 / 21,776 | Wrong amount or currency | FLAG `claim_untraceable`; never FX-convert; intake takes amount and currency from the selected transaction |
| 4 | Complaint outcome fields random or stale | SLA flag flat 18.5–21.4%; 45,629 / 46,948 open cases > 30 days; `compensation_granted` holds amounts in 4,641 rows | Invalid labels; wrong status answers | FIX `compensation_amount` + `compensation_flag`; FIX `sla_derived` from dates; FLAG `stale_status`; never use as labels |
| 5 | Text reveals the label | 5 descriptions for 67,095 rows (1 per category); subcategory 1:1 with category | Label leakage | FIX impute subcategory from category; DROP `description` from features |
| 6 | Active cards past expiry | 40,484 / 80,864 credit; 16,180 / 32,141 debit | "Your card is active" would be wrong | FIX `effective_status = 'Expired'` when `expiration_date < as_of_date` |
| 7 | Stale last-movement field | 427 / 305,721 match | Wrong "last movement" answer | FIX recompute from `silver.transactions` |
| 8 | Activity before opening or registration | 827,610 / 4,425,008 before opening; 829,540 before registration† | Incoherent timelines | FLAG; `effective_opening_date = least(opening_date, first transaction)` |
| 9 | Email is not an identity key | 79,930 / 147,016 customers share an email (54.4%); `document_number` unique 150,000 / 150,000 | Wrong-customer authentication | Contract: email non-unique; authenticate on document type + number + a second factor |
| 10 | Product-number collisions | 12 / 400,000 (6 pairs, different customers) | Card/account-number lookup collision | QUARANTINE from number lookup |
| 11 | Mexico in USD, document type `DNI` | 200,398 / 200,398 products in USD; 74,907 / 74,907 `DNI` | Currency and ID display | FLAG + documented assumption: quote Mexican balances in USD |
| 12 | `amount_usd` fixed-rate and partly missing | 100% equal amount / 350 or / 4000; 99,477 / 1,987,029 missing | Inconsistent USD equivalents | FIX fill with the same fixed rate; daily FX only for customer-facing conversions, labeled as such |
| 13 | Two spellings of Mexico | `Mexico` 40,515 rows, 18,412 of them from Mexican customers | False "international" flag | FIX normalize accents; derive `is_international` |
| 14 | Branch foreign keys broken | `registration_branch_id` 149,995 / 150,000 orphans; `assigned_branch_id` 831 / 833 | "Your branch" cannot be grounded | QUARANTINE; the other 20 documented keys have 0 orphans → hard expectation |
| 15 | `mentioned_products` is random | 545,118 / 548,680 missing; 3,562 / 3,562 foreign | Privacy and grounding hazard | DROP |
| 16 | Fields that copy the label | `main_topics` = reason 171,321 / 171,321; sentiment label = bins of the score; surveys = f(`was_resolved`) | Leakage | Exclude from features; keep `sentiment_score` only |
| 17 | Truncated survey scales | CSAT 1–4; NPS 2–7 with 0 promoters; `nps_category` null 3,274 / 63,668 | Degenerate KPIs | FIX `nps_category` from score; report relative KPIs only |
| 18 | Implausible type × channel | 1,240,000 / 4,425,008 (28.0%) | Narration sounds wrong | FLAG; omit the channel when narrating flagged rows |
| 19 | `process_date` is a business-day cut-off | Previous day for 25.0% of transactions, 33.3% of contacts; 0 later | "When" answers off by a day; split leakage | `event_date` from the timestamp; `process_date` as partition key only |
| 20 | Customer status conflicts with products | 2,694 / 2,979 Closed customers hold an Active product | Authorization ambiguity | FLAG + precedence rule (Closed/Suspended → human handoff) |
| 21 | Random missingness in key inputs | score 15.0%, income 20.0%, either 32.0%; `fraud_score` 20.0%; `response_code` ~5% | Eligibility and decline answers | `null_reason` (not applicable vs missing); explicit insufficient-data path |
| 22 | Duplicates, typing, lineage | 0 duplicate groups on 6 natural keys; 0 rescued rows; 0 invalid amounts | None today | Keep natural-key dedup with an expectation of 0; strict casting with quarantine |

## 7. Workflow scorecard

Scores run from 1 to 5, where 5 is the most favourable (for risk, 5 = least complex). Weights:

- **Service pain, 25%:** the rubric asks for a problem supported by data and a baseline to beat; with CSAT fixed by FCR, pain is essentially the FCR gap.
- **Demand, grounding data and valid labels, 15% each:** respectively, the size of the opportunity, whether answers can be verified, and the hard requirement for a learned component.
- **Risk and feasibility, 10% each:** both constrain a 7-day build.
- **Cost, 5%:** it is measured only on voice and video, on what looks like a sample, and it largely duplicates demand × handle time.
- **ES/PT feasibility, 5%:** Portuguese is team-generated for every option.

| Criterion (weight) | Account/payment inquiries | Card support | **Transaction-dispute intake** | Credit info & eligibility |
|---|---|---|---|---|
| Demand (15%) | **5** — 240,056 contacts (35.0%) | 2 — not measurable (no card reason; product links random) | 3 — Queja 117,021 (17.1%); 40.4% of formal cases are disputes | 2 — 54,879 (8.0%) |
| Service pain (25%) | 2 — FCR 91.5%; 12.7% of unresolved; CSAT 2.91 | 2 — not measurable | **5** — FCR 43.6%; 41.2% of unresolved; CSAT 2.43; cases 74.9% open, first response p50 37 h | 3 — FCR 65.2%; 11.9% of unresolved |
| Cost (5%) | 5 — 12,663 h (24.0%) | 2 — not measurable | 5 — 12,160 h (23.1%); AHT p50 431 s | 3 — 7,061 h (13.4%); AHT p50 540 s |
| Grounding data (15%) | **5** — balances 100%; transactions 100% consistent | 3 — status, expiry, declines, but 50.1% of Active cards expired and no card-event data | 4 — customer → products → transactions is 100% consistent; complaint history unusable | 2 — score 85%, income 80%; no underwriting history |
| Valid labels + baseline (15%) | 3 — labels by construction; simple intents, keyword baseline likely near ceiling | 2 — fraud unlearnable (AUC 0.504); `fraud_score` leaks | **4** — labels by construction anchored on real transactions; semantic classes leave room over keywords | 1 — no outcome label; delinquency AUC 0.504 |
| Risk / compliance (10%) | **5** — read-only | 3 — block/replace actions, fraud | 3 — regulated claim intake; decision stays with humans | 2 — fair-lending exposure; rules only from a policy service |
| Feasibility in ~7 days (10%) | **5** — lookups only | 3 — mocked action tools and state machine | 4 — reuses lookups; mocked case-write tool | 3 — policy service, FX-converted income, insufficient-data branch |
| ES/PT demo (5%) | 4 | 4 | 4 | 3 — product and regulatory terms vary by country |
| **Weighted score** | **3.90** | **2.45** | **4.05** | **2.30** |

**Sensitivity.** With equal weights, account/payment ranks first (4.25) ahead of disputes (4.00). The choice therefore rests on weighting service pain, which the rubric's "problem supported by data" and "baseline to beat" requirements justify, since account/payment has only 8.5 pp of FCR headroom. Card support and credit eligibility rank last under both weightings.

## 8. Recommendation

**Build transaction-dispute intake**, scoped to unrecognized charges (*Cargo no reconocido*) and incorrect charges or fees (*Cobro indebido*), in Spanish and Portuguese, for customers in all three countries: one flow serves all of them because outcomes and the reason mix do not vary by country.

Proposed flow:

1. Authenticate on document type + number + a second factor (never email).
2. List the customer's own products and recent transactions from `silver.transactions`, on event time.
3. Have the customer confirm the disputed movement.
4. Classify the dispute type.
5. Take amount and currency from the transaction, not from the customer's claim.
6. Create a structured case with `case_type`, category and subcategory, `product_id`, `transaction_id`, amount, currency, channel and a rule-based priority.
7. Return the case number and next steps.
8. Hand off to a human for suspected fraud or card compromise, above-threshold amounts, Closed or Suspended customers, low classifier confidence, or an explicit request.

Why this workflow:

1. **It has the largest measurable gap.** Queja has the lowest FCR (43.6%), 41.2% of all unresolved contacts, and 2.1× the handle time of transactional inquiries. On the case side, disputes arrive at 753.8 per month, and 74.9% of them are still open.
2. **Grounding exists where the flow needs it.** The disputed item is a transaction, and transactions are the cleanest table in the dataset.
3. **Data quality drives the design, visibly.** Historical dispute records are provably unusable (Section 3), which is exactly why the intake builds the case from verified transactions.
4. **It contains the runner-up.** The account/payment lookups (balance, recent movements, decline explanation) are the first steps of the dispute flow.

**Runner-up: account/payment inquiries.** It has the highest volume (35.0%), the best grounding and the lowest risk, but FCR is already 91.5% and CSAT is the best of any reason, so it offers only a containment and handle-time story with little to beat. If the dispute scope slips, the same lookup tools ship as this workflow.

**Not recommended.** Card support has unmeasurable demand, no card-event data and would need mocked actions; an optional extension is a mocked "block card" step inside the dispute flow when the disputed movement is on a card. Credit eligibility has no valid labels, the highest compliance risk and the smallest volume.

## 9. Baseline and target outcomes

**Current-state baseline** (Queja as the contact-level proxy; dispute-type complaints for the case level):

| Metric | Current value (denominator) | Source |
|---|---|---|
| FCR (`was_resolved`) | 43.6% (51,021 / 117,021); FY2 43.8% (17,021 / 38,858) | Q2.1 |
| Strict FCR (secondary) | 33.2% (38,902 / 117,021) | Q2.1 |
| Share of all unresolved contacts | 41.2% (66,000 / 160,266) | Q2.1 |
| Handle time (AHT) | p50 431 s, p90 608 s (n = 100,727 voice/video) | Q2.1 |
| Handle-hours | 12,160 h over 3 years (23.1% of 52,688 h); FY2 4,039 h of 17,489 h | Q2.1 |
| CSAT (consequence of FCR) | 2.43 on 1–4 (n = 21,843); 54.5% score ≤ 2 | Q2.2 |
| Dispute case volume | 753.8 per full month (27,133 cases) | Q3.2 |
| Time to first response | p50 37 h, p90 58 h (n = 16,620, the 61.3% that have one); 26.2% > 48 h | Q3.2 |
| Time to assignment | p50 12.0 h (n = 17,874, 65.9%) | Q3.2 |
| Case backlog | 74.9% Open / In Process / Escalated | Q3.2 |
| Case completeness | amount + currency 31.4%; product owned by the complainant 0% (0 / 44,570, all complaints); linked to the originating contact 0% (0 / 67,095) | Q3.2, Q3.3 |

**Target outcomes** (proposed; measured on a held-out ES/PT scenario set, not on live traffic):

| Outcome | Target | Baseline it beats |
|---|---|---|
| First-contact case completion: in-scope scenarios that end with a complete, verified case | ≥ 80% | Queja FCR 43.6% |
| Correct transaction linked to the case | ≥ 95% of completed cases | 0% valid product links in historical cases |
| Required fields present (transaction, owned product, amount, currency) | 100% of created cases | 31.4% amount + currency |
| Grounding violations (product or transaction not owned by the customer) | 0 | 44,570 / 44,570 historical complaint links |
| Must-handoff scenarios handed off, with a reason code | 100% recall | Escalation today is a random 10% |
| Case number and next step given | Within the conversation | First response p50 37 h |
| Intake duration | Median below 431 s | Queja AHT p50 |
| ES vs PT gap in completion and macro-F1 | ≤ 5 pp | New capability (no Portuguese today) |

**Customer outcomes:** a dispute filed in one contact, the disputed movement confirmed from the customer's own history, a case number and next step immediately, in Spanish or Portuguese, and a human when it matters.

**Business outcomes:** fewer unresolved complaint contacts (41.2% of all unresolved today), complete and investigable case records, and less handle time on the costliest complaint calls. The audit trail also serves regulator cases (1.07% of complaints).

**Sizing (assumption).** If complaint calls mirror the formal case mix, 36.5–40.4% of Queja contacts are disputes: about 14,200–15,700 contacts and 1,470–1,630 handle-hours per year in FY2 terms (38,858 contacts, 4,039 h). Because interactions look like a sample, the case is stated as rates.

**Deliberately not targeted:** recontact (random arrivals), escalation (a 10% coin flip), the SLA flag (random), CSAT on its own (a fixed function of FCR), and money at stake (claimed amounts are random).

## 10. Learned-component plan

**Why not historical labels.** Every historical label tested behaves as noise or leaks:

- `is_fraud`: temporal AUC 0.504 and 0.4997.
- `dpd > 90`: AUC 0.504 against credit score.
- FCR by agent: equal to binomial noise.
- CSAT: a fixed function of `was_resolved`.
- SLA flag: flat 18.5–21.4%.
- Complaint descriptions: a copy of the category.
- Transcripts: templated, with `main_topics` a copy of the reason.
- Reason routing from pre-contact data is not expected to beat the prior either, because transaction events do not change contact rates (Q4.9) and the reason mix is identical across countries and channels.

These results are kept as documented negative controls.

**Task.** Intake understanding for ES/PT customer messages:

- **Intent classifier** (the evaluated learned component), with classes `dispute_unrecognized_charge`, `dispute_incorrect_charge_or_fee`, `account_payment_inquiry`, `card_lost_or_block`, `other_complaint` and `out_of_scope`.
- **Slot extraction** for amount, currency, relative date and merchant or channel hint: rules first, learned if time allows.

**Labels, valid by construction.**

- A scenario generator samples a real customer, product and transaction from Silver (on event time) and records the gold intent, dispute type, `transaction_id`, amount, currency and date.
- It renders ES and PT messages from paraphrase families with controlled noise: rounded amounts, relative dates ("el martes pasado" / "na terça passada"), number formats (1.234,56 vs 1,234.56) and partial merchant names.
- A hand-written holdout (target ≥ 100 messages per language, written by both team members before training) is used only for the final test.

**Baselines.**

- (a) Majority class.
- (b) A keyword/regex router written from a glossary before the test set is seen.
- Transaction resolution stays a deterministic tool (amount ±1%, date ±2 days, then customer confirmation) and is reported as top-1 accuracy.

**Candidate model.** A lightweight multilingual text classifier: character n-gram TF-IDF with logistic regression, optionally sentence embeddings with logistic regression. Calibrated probabilities feed an abstain-and-handoff threshold.

**Splits.**

1. Group split by paraphrase family and by `customer_id`: no family or customer appears on both sides.
2. Temporal split on the anchor transaction's event time: train before 2025-07-01, test on or after. Never `process_date`.
3. Language transfer: train on ES only, test on PT.
4. The hand-written holdout as the final test.

**Metrics.** Macro-F1 per language, recall on the two dispute classes, out-of-scope false-accept rate, abstention rate, slot exact-match and top-1 transaction resolution. All are reported with 95% bootstrap intervals, against both baselines, on the same test sets.

**Leakage traps (checklist).**

- `reason_category`, `contact_reason`, transcript `main_topics` and `detected_intents` are copies of the label.
- Complaint `description` (5 strings, one per category) and `subcategory` (1:1 with category).
- `fraud_score`, which is built from `is_fraud` (precision 1.000 above 30).
- Post-contact fields: `was_resolved`, `requires_followup`, `was_escalated`, duration, sentiment, survey scores.
- Post-intake case fields: status, resolution dates and days, closing date, compensation, `sla_breached`.
- `process_date` as a split key: 25–34% of events carry the previous day.
- Template or paraphrase overlap between train and test; verbatim `transaction_id` or amount strings in the text; the same generator seed on both sides.

## 11. Limitations

- **Synthetic-data artifacts.** Uniform categories, scores and timings; FCR driven only by reason; deterministic satisfaction; random cross-table links; flat demand. Conclusions about what "drives" outcomes describe the generator, not banking behavior. The data is used here for sizing, baselines and grounding, not for causal claims.
- **Spanish only.** All historical text is Spanish (`detected_language = 'es'` in 171,321 of 171,321 transcripts†), and accents cover only Mexico, Colombia and Argentina. Portuguese evaluation data is team-generated, labeled as such, and reported separately.
- **Bronze-level numbers.** Figures are raw and pre-dedup; with 0 duplicates found, Silver should reproduce them except for the fields Section 6 recomputes.
- **Sample and cost caveats.** Interactions appear to be a sample (Section 2, observation 11), so savings are rates, not FTE. Handle time exists only for voice and video (590,062 of 686,296 contacts).
- **Assumptions.** The Queja-to-dispute mapping is an assumption: calls cannot be linked to cases. Historical baselines and scenario-based targets measure different populations, so comparisons are directional.
- **Open questions.** The timezone of event timestamps is unconfirmed; the 06:00 and 08:00 cut-offs suggest a single system clock. `digital_events` is not loaded, so card and app events, and any fraud signal they carry, remain unexamined. The announced ~2% duplicates were not found under any natural key.
