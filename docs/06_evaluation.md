# 06 — Evaluation: intent classifier and end-to-end agent

This report gives the held-out results of the two evaluated components and how they were obtained. Every number comes from the files in `eval/results/`, which hold all intervals and per-scenario verdicts.

## Summary

| Component | Held-out set | Result (95% interval) | Baseline: same rows for the classifier; none for the agent |
|---|---|---|---|
| Intent classifier v2 (macro-F1) | Generated test split, 1,200 messages, families never seen in training | 0.835 [0.815, 0.854] | Keyword router 0.813; majority class 0.055 |
| | Independent free-form holdout, 300 messages | 0.846 [0.802, 0.883] | Keyword router 0.828; majority 0.037 |
| | Team hand-written holdout, 61 messages | 0.770 [0.652, 0.863] | Keyword router 0.664; majority 0.069 |
| Agent end to end (scenario success) | 140 test scenarios, run once with the final agent | 97.9% (137/140) [95.0, 100.0] | No baseline agent was run on these 140 test scenarios. For reference only, on a different split: the original agent (before the fixes) scored 81.4% (114/140) on the 140 dev scenarios |

**The agent row is not a before-and-after on the same workload.** 81.4% (original agent, 140 dev scenarios) and 97.9% (final agent, 140 test scenarios) come from different splits, and the dev split was used to make the fixes. Their difference is not a same-workload comparison and not a measured improvement, offline or in production. The evidence on the same 140 test scenarios is the report 02 targets, evaluated on them in the next table, and the scripted oracle (`replay.py`'s agent, which follows the policy exactly), which scores 140/140 on them through the same scorer (280/280 with the dev split): an upper bound that checks the scorer, not a baseline. On the dev split both versions ran, 114/140 and then 137/140, but that comparison is in-sample (protocol, below).

Against the targets of report 02 (section 9), on the 140 test scenarios:

| Target | Value | Verdict |
|---|---|---|
| First-contact case completion ≥ 80% | 98.5% (66/67) [95.5, 100.0] | met |
| Correct transaction linked ≥ 95% of completed cases | 100.0% (66/66) | met |
| Required fields present in 100% of cases | 100.0% (66/66) | met (enforced by the service) |
| Grounding violations = 0 | 0 | met (enforced by the service) |
| Must-handoff scenarios handed off with the right reason, 100% | 96.5% (28/29) [89.7, 100.0] | not met (1 miss) |
| ES vs PT gap ≤ 5 points | +1.4 points [-2.9, +5.7] in scenario success | point within target; the interval cannot confirm it |

No test scenario broke a listed tool-level rule (`must_not`): no case without confirmation, no other customer's data, no action on an expired or missing session, no injected instruction followed. Every reply was in the customer's language (140/140).

## Outcomes in the challenge's terms

The problem statement asks for five outcomes, reported separately. They are computed from the final test run's saved results by `python -m src.agent_eval.challenge_outcomes` ([`eval/results/challenge_outcomes_test_databricks-gpt-oss-120b.md`](../eval/results/challenge_outcomes_test_databricks-gpt-oss-120b.md)), with the same bootstrap as the rest of this report. They are offline measurements on generated scenarios, not production results.

| Outcome, as the problem statement defines it | Test split, 140 scenarios (95% interval) |
|---|---|
| **Safe automated resolution**: an in-scope case reaches the correct, policy-compliant outcome without a person, over all in-scope cases | 97.5% (77/79) [93.7, 100.0]; automation attempted on 79/79 |
| **Containment**: the case ends without a transfer (it does not mean the problem was solved) | 79.3% (111/140) [72.1, 85.7] |
| **Escalation quality**: transfers that were needed, and transfers that were not | 29 must-handoff scenarios: 29 transferred, 28 with the right reason; 0 missed; 0 unnecessary transfers in the other 111 |
| **Unsafe outcomes**: unauthorized disclosures or actions, or materially incorrect outcomes | 0 of 140 scenarios (95% upper bound 2.1% by the rule of three); 66 of 66 cases linked to the right transaction; 0 case or ticket ids without a tool result in 258 turns |
| **Operating efficiency**: latency, and cost per attempted case and per successful automated resolution | Turn latency p50 12.4 s, p95 61.5 s (with conversations in flight on a shared endpoint); model cost about US$ 0.0044 per attempted case and US$ 0.0081 per successful automated resolution (estimate: model tokens only, list DBU rates, an assumed US$ 0.07 per DBU; see the cost assumptions below) |

- **Scope (decision).** "In scope" means the customer brings a request the system is built to resolve without a person: the expected outcome is a case, with or without one clarifying question, or an answer to an account question (79 scenarios). The other 61 are left out of safe automated resolution on purpose. 29 expect a transfer: the policy requires a person, and they are measured under escalation quality. 16 expect a refusal or an abstention: an attack with no real request, or a credit, investment or loan question the system does not handle, so there is nothing to resolve. 16 expect a new sign-in: a customer number only, or a session that expired before the confirmation, so nothing can be resolved in that conversation. A correct "no" or a request to sign in again is safe behavior, not a resolution: these 32 count under unsafe outcomes and in containment, not in safe automated resolution. All 32 reached their expected outcome; counting them as resolutions would give 109/111 instead of 77/79. Because they count in containment, containment (no transfer) is not the same as resolution.
- **Unsafe.** A listed tool-level `must_not` rule broken or a grounding violation in a case or ticket. The one transfer with the wrong reason (`e2e-es-0080`, below) is counted under escalation quality, not as unsafe: the customer still reached a person.
- **Cost assumptions (estimate).** Pay-per-token list rates for `gpt-oss-120b` of 2.143 DBU per 1M input tokens and 8.571 per 1M output tokens, as listed on the [Databricks Foundation Model Serving pricing page](https://www.databricks.com/product/pricing/foundation-model-serving) and in the [Azure Databricks serverless pricing table](https://learn.microsoft.com/en-us/azure/databricks/resources/pricing) (both retrieved 2026-10-04). US$ 0.07 per DBU is an assumption: it is the Azure Databricks Premium list price for Serverless Real-Time Inference in East US (Azure retail prices API, retrieved 2026-10-04); the price for the project's workspace, which runs on AWS, was not confirmed, and it varies by cloud, region and contract. Model tokens only: app hosting, the SQL warehouse, storage and the local classifier are not included. The whole run used 3.73M prompt and 0.10M completion tokens (97% prompt): 8.9 DBU, about US$ 0.62 (estimate, same assumptions).
- **Reading the zeros.** No unsafe outcome and no unnecessary transfer were observed in 140 scenarios. That does not establish zero risk: with 140 trials, the rate could still be up to about 2% at 95% confidence.

## Intent classifier

Details: `eval/results/intent_classifier.md`, code in `src/classifier/`.

- **Two releases.** v1 is TF-IDF (word and character n-grams) with a linear SVM, selected on the dev split. v2 adds the keyword router's rule matches as features and was selected only by grouped cross-validation over paraphrase families on train + dev. v2 was designed after v1's results were known; the report says so in its first section.
- **Paired differences, v2 minus keyword router (macro-F1):** test +0.021 [+0.012, +0.032], independent +0.018 [+0.005, +0.034], team +0.106 [+0.025, +0.196]. v1 was behind the router on the test split (−0.056 [−0.083, −0.027]).
- **Trade-offs of v2:** lower dispute recall on the independent holdout (113/150 against 133/150 for v1) and worse calibration (ECE 0.082 against 0.035), so it asks for clarification less often under the policy threshold of 0.55.
- **Use in the agent:** a hint on the first substantive message (runtime facts), never a router. A single-message classifier is confidently wrong on follow-ups such as "gracias" or "sí".
- **Labels.** Two team hand-written labels were corrected to `out_of_scope` on 2026-10-03 to match the labeling convention; the change log in `eval/holdout/README.md` records why, and the correction lowered v2's team score.

## End-to-end agent

Details: `eval/results/agent_e2e_test_databricks-gpt-oss-120b.md` and `..._dev_...md`; code in `src/agent_eval/`.

**What is measured.** `src/agent_eval` runs the real `app/agent.py` with the served model (`databricks-gpt-oss-120b`) on each e2e scenario: a fresh bank per scenario (the same snapshot, clock, injected faults and test session as `replay.py`), the scenario's customer turns sent in order, and a verdict from what was written to the bank (the case or ticket, its fields and handoff reason) plus the scenario's `must_not` rules. Reply checks (language, invented ids, refund promises, leaked instructions, invented channels) are heuristics. An oracle mode runs `replay.py`'s scripted agent through the same scorer and must score 280/280; it does.

**Protocol.**

1. The 140 dev scenarios were used to find and fix problems. The original agent scored 114/140 (81.4%).
2. Fixes in three places: the bank tools (amounts and periods as customers write them, a direct answer for declined payments, demo limits), the agent runtime and prompt (an explicit-yes gate before any case is written, a function-word language detector, loop and error fallbacks, out-of-scope and third-party refusals, card-compromise handoffs, a calendar of recent days) and the server and UI (per-conversation clock and lock, conversation keys, a masked console, input limits). They are in commits 11bdda5, 291f0c7 and 97308ea.
3. Five full dev runs, all scored with the current scorer: 114, 127, 135, 134 and 137 out of 140. Runs 3 to 5 differ by only a few scenarios, which is within the run-to-run variation of the model.
4. The 140 test scenarios were run once (`--final`) with the final agent (fingerprint `2a66119c170cd36e`, commit 97308ea), in parallel with the last dev run.
5. **Scorer corrections after the final run.** Reading the failed test and dev transcripts showed 4 false failures of the scorer, not of the agent: a reply that said "no había saldo o cupo suficiente" was not recognized as an insufficient-funds explanation; a decline reason written in parentheses was ignored; a merchant named "Internet Plus" was read as the channel "internet"; and a movement's line was found by its merchant name when the same merchant appeared on another movement's line. The scorer was corrected, with a test for each case, and both runs were re-scored from the saved transcripts. The agent did not change. The oracle still scores 280/280. Before these corrections the final run scored 134/140.
6. **What the developers saw of the test split.** Test-split scenario texts were read when checking the confirmation detector (tuned on dev confirmation turns, then measured on test ones) and the amount parser (checked on every scenario amount). No prompt or rule was changed because of a test-split agent failure.

**Results on the 140 test scenarios.** 137/140 succeed: Spanish 69/70, Portuguese 68/70. Every category scores 10/10, 12/12 or 14/14 except three: account inquiries 11/12, human-required 11/12 and incorrect fees 11/12.

**The 3 test failures.**

- `e2e-pt-0037` and `e2e-es-0080`: "na segunda passada" and "el lunes pasado". The agent searched the most recent Monday, and the scenario meant the Monday before it, so the search found nothing. In `e2e-es-0080` the agent then asked a clarifying question and handed off with `low_intent_confidence` instead of `amount_above_threshold`, the one must-handoff miss. The same reading explains the dev failure `e2e-es-0037` ("el jueves pasado"). Both readings are used in everyday Spanish and Portuguese. Searching the period that covers both days would remove this failure. It was not done before the final run.
- `e2e-pt-0017`: "263.510 pesos argentinos" was passed to the search as the number 263.51 instead of the customer's text. The service reads three-decimal numbers as grouped thousands, but a trailing zero disappears in a number, so this reading cannot be recovered. The prompt asks for the text; the model did not follow it here.

**Cost and speed in the exam.** Median 15,113 tokens per turn (97% of all tokens are prompt), about 27,000 per scenario. Median 12.4 s and p95 61 s per turn. These latencies were measured with 6 conversations in flight on a shared endpoint (two runs of 3 workers), so they are not single-user latencies.

## Fairness by country, segment and language variant

The problem statement asks for outcomes by language and by authorized customer segments, with their small-sample limits, and for any disparity to be investigated. For the agent, `python -m src.agent_eval.fairness` computes them from the final test run's saved per-scenario verdicts ([`eval/results/fairness_test_databricks-gpt-oss-120b.md`](../eval/results/fairness_test_databricks-gpt-oss-120b.md)). For the classifier, `python -m src.classifier.evaluate` adds a section "By language variant" to `eval/results/intent_classifier.md`. No model was called. These are offline measurements on generated, scripted conversations with synthetic customers (the dataset documents state that the data holds no real customer information), not production outcomes.

- **Groups.** The customer's country (MX, CO, AR) and segment (Basic, Plus, Premium, Student), from `customer_profile` in the snapshot the exam ran on (same sha256 as the run's fingerprint; the country agrees with `data/scenarios/panel.jsonl` for all 140 customers), and the language variant the scenario text is written in. All 140 test scenarios have a customer with a profile, so no scenario falls in an "unknown" group.
- **Not used.** Gender, age and any other personal attribute. They are not authorized segments for this analysis, and the snapshot holds none of them (report 05, section 3, lists the personal data Gold leaves out).
- **Measures.** Scenario success and four of the challenge outcomes above, with the same definitions (`challenge_outcomes.py`): safe automated resolution (over in-scope scenarios), must-handoff scenarios transferred with the right reason, unnecessary transfers and unsafe outcomes. The groups of each view add up to the overall counts, and those match the outcomes table above.
- **Rule, fixed before any group figure was computed.** A group with fewer than 20 scenarios, or a measure with fewer than 20 scenarios in the group or in the rest, is "sample too small to conclude": no gap is computed and no claim is made. Otherwise the gap is the group's rate minus the rate of all other test scenarios, with a 95% bootstrap interval (2,000 resamples, seed 20261005, the group and the rest resampled separately). A gap counts as a disparity to investigate only when its interval excludes zero.

**Agent, 140 test scenarios.** Gaps are in points, against all other scenarios. "Too small" means "sample too small to conclude"; no gap is computed there.

| Group | n | Scenario success | Gap (95% interval) | Safe automated resolution | Gap (95% interval) | Must-handoff, right reason |
|---|---|---|---|---|---|---|
| MX | 66 | 98.5% (65/66) | +1.2 [-3.2, +5.4] | 97.6% (40/41) | +0.2 [-7.3, +7.9] | 13/13, too small |
| CO | 38 | 97.4% (37/38) | -0.7 [-6.9, +3.9] | 19/19, too small | | 8/9, too small |
| AR | 36 | 97.2% (35/36) | -0.9 [-7.4, +3.9] | 18/19, too small | | 7/7, too small |
| Basic | 78 | 98.7% (77/78) | +1.9 [-2.6, +6.8] | 100.0% (41/41) | +5.3 [+0.0, +13.2] | 17/18, too small |
| Plus | 36 | 97.2% (35/36) | -0.9 [-7.4, +3.9] | 95.0% (19/20) | -3.3 [-15.0, +5.1] | 6/6, too small |
| Premium | 21 | 95.2% (20/21) | -3.1 [-13.5, +3.4] | 13/14, too small | | 4/4, too small |
| Student | 5 | 5/5, too small | | 4/4, too small | | 1/1, too small |

Unnecessary transfers and unsafe outcomes are 0 in every group.

- **What is concluded.** No country and no segment shows a gap whose interval excludes zero, on any measure with enough scenarios. The three failures fall one in each country and one in each of Basic, Plus and Premium. No group had an unsafe outcome or an unnecessary transfer.
- **What is not concluded.** That there is no disparity. With 21 to 78 scenarios per group, the intervals still allow a group to be several points worse than the rest: down to 13.5 points for Premium in scenario success, and 15.0 points for Plus in safe automated resolution. Student customers (5 scenarios) cannot be read at all, and every group is too small for must-handoff (29 scenarios in all). An interval that ends exactly at zero (Basic, safe automated resolution) comes from a group with no failure and is read as including zero.
- **Category mix.** The groups do not hold the same kinds of scenarios: 9 of the 12 account-inquiry scenarios have Mexican customers, 6 of the 10 unsupported requests Argentine ones, and 5 of the 12 incorrect-fee scenarios Premium ones. A group's rate partly reflects its mix; the full table is in the results file. Group sizes follow how the scenario generator drew customers, not the bank's customer base.

**The three failures, read against the groups** (causes in "The 3 test failures" above).

| Scenario | Country, segment, variant | Cause | Related to the group? |
|---|---|---|---|
| `e2e-es-0080` | CO, Basic, es-CO | "el lunes pasado" searched as the most recent Monday | No. "Last <weekday>" phrases appear for customers of all three countries: 8 test scenarios (MX 4, CO 2, AR 2), 2 of them failed |
| `e2e-pt-0037` | MX, Plus, pt-BR | "na segunda passada", the same reading | No. The same phrase reading, in Portuguese |
| `e2e-pt-0017` | AR, Premium, pt-BR | "263.510" passed to the search as the number 263.51 | Not to the segment; to the country only indirectly. Whole amounts written with dots as thousands separators come with large peso amounts: 10 Argentine and 11 Colombian test scenarios, 1 Mexican. The form that loses its last zero as a number (one dot group ending in zero) appears in 2 test scenarios, both Argentine: this one failed, and in the other the agent never passed the amount to a search |

One of the three causes is linked to country, through the way large peso amounts are written. 140 scenarios cannot measure it as a gap, so it is recorded as a risk for Argentine and Colombian customers. The search's amount hint accepts a number or text; accepting text only would remove the number form that loses the zero (the agent's prompt already asks for the text when the amount has separators, and the model did not follow it here). This was not changed before the final run. The date reading is a language issue, not a group one.

**Language.** Spanish 69/70 and Portuguese 68/70, a gap of +1.4 points [-2.9, +5.7] (the exam's own figures, above); this analysis adds nothing to it. Spanish texts use the customer's own variant (es-MX for Mexican customers, and so on), except 5 in portunhol, while Portuguese texts are written on Mexican, Colombian and Argentine accounts (36, 14 and 20 scenarios), so language and country are only partly separate. By variant: es-MX 30/30, es-CO 21/22, pt-BR 63/65, es-AR 13/13 and portunhol 10/10. es-AR and portunhol are below 20 scenarios, and none of the others shows a gap whose interval excludes zero. Both in-scope failures are in pt-BR text (safe automated resolution 33/35, gap -5.7 [-14.3, +0.0]); their causes are the amount and date readings above.

**Intent classifier, by language variant.** Accuracy of v2 (forced choice). The keyword router, v1, macro-F1 and every interval are in `eval/results/intent_classifier.md`, section "By language variant". That section was added after every other number in the report was known. It re-slices the same predictions, changes no model, rule or threshold, and leaves every earlier number in `intent_classifier.json` as it was (checked key by key). The same rule applies: fewer than 20 messages in the variant, or in the rest of its set, is too small to conclude.

| Set | es-MX | es-CO | es-AR | pt-BR | Portunhol (mixed) |
|---|---|---|---|---|---|
| Generated test split, 1,200 messages | 0.763 (116/152) | 0.827 (263/318) | 0.823 (107/130) | 0.854 (479/561) | 0.410 (16/39) |
| Independent holdout, 300 messages | 0.900 (45/50) | 0.760 (38/50) | 0.860 (43/50) | 0.822 (111/135) | 1.000 (15/15), too small |
| Team hand-written holdout, 61 messages | 0.625 (10/16), too small | 0.769 (10/13), too small | 0.900 (9/10), too small | 0.769 (10/13), too small | 0.667 (6/9), too small |

- **Independent holdout.** It has no paraphrase families. No variant with 20 or more messages has an accuracy gap against the rest of the set whose interval excludes zero, for the keyword router, v1 or v2. The intervals are wide: v2 on es-CO is 9.6 points below the other messages [-22.4, +2.4].
- **Generated test split.** Some gaps exclude zero: v2 on pt-BR +6.8 points [+2.4, +11.2] and on portunhol -42.1 [-57.8, -26.3]; the keyword router on es-MX -9.5 [-17.0, -1.7], on pt-BR +5.3 [+0.6, +9.9] and on portunhol -39.6 [-55.4, -23.7]; v1 on es-MX +12.8 [+7.0, +18.4]. Investigated: each variant there is a handful of paraphrase families (8 to 14, and a single one for portunhol) with its own mix of intents, and the interval resamples messages, not families. So these gaps also measure which families a variant holds. They are not read as differences between groups of customers.
- **Portunhol.** In the generated test split it is one family of 39 vague "problem with a charge" messages where either dispute class is acceptable. The keyword router and v2 read 22 of them as an account question and 1 as out of scope (v1: 11 as an account question). This is the classifier's clearest weak spot by variant, and it was already visible in the mixed slice of the classifier report. In the agent the classifier is only a hint, and the 10 portunhol test scenarios ended 10/10 (too few to conclude).
- **Team set.** Every variant has fewer than 20 messages, so nothing is concluded from it.

**Limits of this analysis.** Offline measurements on generated, scripted conversations with synthetic customers; not production outcomes, and not a statement about real customers of these countries or segments. The customer records are synthetic, the conversations come from the team's scenario generator, and the classifier sets are described in the classifier report. A real disparity check needs more scenarios per group (at least 20 per group and measure even to be read), in particular for Student customers, es-AR and portunhol texts, and must-handoff cases.

## Limitations

- The scenarios and their customer turns are generated. The customer is scripted: when the agent takes a different but reasonable path (an extra question), later scripted turns may no longer fit, and the scenario fails.
- 140 scenarios per split give wide intervals: the ES vs PT gap and the must-handoff recall cannot be confirmed at their targets.
- Outcomes by country, segment and language variant rest on 5 to 78 test scenarios per group. No gap with enough scenarios has an interval that excludes zero, but gaps of several points cannot be ruled out, and Student customers, es-AR and portunhol texts are too few to read (section above).
- The model is not deterministic. The same agent scored between 134 and 137 out of 140 on dev in its last three runs.
- The agent has no baseline on the held-out workload: the original agent ran only on the dev split, and the test split was run once, with the final agent. 81.4% and 97.9% are not a before-and-after on the same scenarios (see the summary).
- The team hand-written holdout has 61 messages from one author.
- Reply checks are pattern-based. They passed manual review of every failure reported here, but they can miss problems a person would see.
