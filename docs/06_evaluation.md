# 06 — Evaluation: intent classifier and end-to-end agent

This report gives the held-out results of the two evaluated components and how they were obtained. Every number comes from the files in `eval/results/`, which hold all intervals and per-scenario verdicts.

## Summary

| Component | Held-out set | Result (95% interval) | Baseline on the same rows |
|---|---|---|---|
| Intent classifier v2 (macro-F1) | Generated test split, 1,200 messages, families never seen in training | 0.835 [0.815, 0.854] | Keyword router 0.813; majority class 0.055 |
| | Independent free-form holdout, 300 messages | 0.846 [0.802, 0.883] | Keyword router 0.828; majority 0.037 |
| | Team hand-written holdout, 61 messages | 0.770 [0.652, 0.863] | Keyword router 0.664; majority 0.069 |
| Agent end to end (scenario success) | 140 test scenarios, run once with the final agent | 97.9% (137/140) [95.0, 100.0] | Same agent before the fixes, on the 140 dev scenarios: 81.4% (114/140) |

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
| **Operating efficiency**: latency, and cost per attempted case and per successful automated resolution | Turn latency p50 12.4 s, p95 61.5 s (with conversations in flight on a shared endpoint); model cost about US$ 0.0044 per attempted case and US$ 0.0081 per successful automated resolution |

- **Scope.** "In scope" means the expected outcome is a case or an answer (79 scenarios). The other 61 expect a transfer (29), a refusal or abstention (16) or a new sign-in (16). "Unsafe" means a listed tool-level `must_not` rule broken or a grounding violation in a case or ticket. The one transfer with the wrong reason (`e2e-es-0080`, below) is counted under escalation quality, not as unsafe: the customer still reached a person.
- **Cost assumptions.** Pay-per-token rates for `gpt-oss-120b` of 2.143 DBU per 1M input tokens and 8.571 per 1M output tokens ([Databricks pricing table](https://learn.microsoft.com/en-us/azure/databricks/resources/pricing), retrieved 2026-10-04), and US$ 0.07 per DBU, an assumption that varies by cloud, region and contract. Model tokens only: app hosting, the SQL warehouse, storage and the local classifier are not included. The whole run used 3.73M prompt and 0.10M completion tokens (97% prompt): 8.9 DBU, about US$ 0.62.
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

## Limitations

- The scenarios and their customer turns are generated. The customer is scripted: when the agent takes a different but reasonable path (an extra question), later scripted turns may no longer fit, and the scenario fails.
- 140 scenarios per split give wide intervals: the ES vs PT gap and the must-handoff recall cannot be confirmed at their targets.
- The model is not deterministic. The same agent scored between 134 and 137 out of 140 on dev in its last three runs.
- The team hand-written holdout has 61 messages from one author.
- Reply checks are pattern-based. They passed manual review of every failure reported here, but they can miss problems a person would see.
