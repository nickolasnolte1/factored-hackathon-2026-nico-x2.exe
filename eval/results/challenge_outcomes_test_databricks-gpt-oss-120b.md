# Challenge outcomes: test split, databricks-gpt-oss-120b

Computed by `python -m src.agent_eval.challenge_outcomes` from `eval/results/agent_e2e_test_databricks-gpt-oss-120b.json` (140 scenarios). Offline measurements on generated scenarios, not production results. Intervals: 95% percentile, over scenarios (2000 resamples, seed 20261005).

| Outcome | Value |
|---|---|
| Safe automated resolution (over in-scope scenarios) | 97.5% (77/79) [93.7%, 100.0%] |
| Automation attempted (over in-scope scenarios) | 100.0% (79/79) [100.0%, 100.0%] |
| Containment (no transfer, over all scenarios) | 79.3% (111/140) [72.1%, 85.7%] |
| Must-handoff scenarios transferred | 100.0% (29/29) [100.0%, 100.0%] |
| Must-handoff scenarios transferred with the right reason | 96.5% (28/29) [89.7%, 100.0%] |
| Missed transfers | 0 of 29 |
| Unnecessary transfers | 0.0% (0/111) [0.0%, 0.0%] |
| Unsafe outcomes | 0.0% (0/140) [0.0%, 0.0%]; 95% upper bound by the rule of three: 2.1% |
| Case or ticket ids in replies without a tool result | 0 in 258 turns |
| Turn latency p50 / p95 | 12.4 s / 61.5 s |
| Scenario wall time p50 / p95 | 40.8 s / 101.9 s |
| Model tokens (prompt / completion) | 3,733,741 / 104,619; 27,417 per scenario |
| Model cost, whole run (estimate) | 8.898 DBU, about US$ 0.62 |
| Model cost per attempted case (estimate) | about US$ 0.0044 |
| Model cost per successful automated resolution (estimate) | about US$ 0.0081 |

Definitions:

- **in scope for automation**: expected outcome create_case, clarify_then_create_case or answer; refuse, abstain and reauthenticate are left out (a correct no or a new sign-in is not a resolution), and so are must-handoff scenarios.
- **safe automated resolution**: in scope; scenario succeeded (correct, policy-compliant outcome); no transfer; no tool-level must_not broken; no grounding violation.
- **automation attempted**: in scope and not transferred to a person.
- **containment**: ended without a transfer to a person, over all scenarios (does not mean solved).
- **must handoff**: expected outcome handoff or clarify_then_handoff.
- **unnecessary transfer**: transferred although the expected outcome needed no person.
- **unsafe**: a listed tool-level must_not rule broken, or a grounding violation in a case or ticket.

Notes:

- Latency was measured with 221 rate-limit waits on a shared endpoint and several conversations in flight (see the exam report), so it is not single-user latency.
- Cost is an estimate. Rates: 2.143 DBU per 1M input tokens and 8.571 per 1M output tokens, list rates (https://www.databricks.com/product/pricing/foundation-model-serving and https://learn.microsoft.com/en-us/azure/databricks/resources/pricing (retrieved 2026-10-04)). Price: US$ 0.07 per DBU, an assumption: the Azure Databricks Premium list price for Serverless Real-Time Inference in East US (https://prices.azure.com/api/retail/prices, retrieved 2026-10-04), not confirmed for the project's workspace on AWS; it varies by cloud, region and contract. Scope: model tokens only; excludes app hosting, SQL warehouse, storage and the local intent classifier.
- Zero observed unsafe outcomes in a small set does not establish zero risk; the rule-of-three bound is the honest reading.
