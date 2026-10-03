# Intake intent classifier (ES / PT)

Reads the customer's message at the start of the dispute-intake chat and says which intake intent it is:

- `dispute_unrecognized_charge`: a charge the customer did not make.
- `dispute_incorrect_charge_or_fee`: a charge that is theirs but wrong (duplicate, wrong amount, fee, interest).
- `account_payment_inquiry`: a question about balance, movements, payments or declines.
- `card_lost_or_block`: a lost, stolen, retained or cloned card, or a block request.
- `other_complaint`: service, staff, app or ATM complaints.
- `out_of_scope`: everything else, including requests the agent must refuse.

Ambiguous messages list more than one acceptable intent, and a prediction is correct when it is one of them. The learned model is a linear classifier with calibrated probabilities. Release v2 picks it among TF-IDF models, hybrids that add the keyword router's rule hits as features, and a late fusion of a TF-IDF model with the router's answer; the model card names the choice. The keyword router on its own is the rule-based baseline. Both work on Spanish (es-MX, es-CO, es-AR) and Brazilian Portuguese.

| Module | What it does |
|---|---|
| `data.py` | Class list, dataset loaders that filter by split, the two views of a message (normalized text for TF-IDF, raw text for keyword features), the acceptable-intents rule, the out-of-scope false-accept rule and the policy threshold |
| `keywords.py` | Keyword and regex router (the baseline, and the source of the keyword features) |
| `train.py` | Release v2 (default): grouped cross-validation over train + dev by paraphrase family, a tie rule that prefers simpler models, one temperature on out-of-fold scores, and a refit on all train + dev rows. Release v1: the original selection on the dev split, kept reproducible. Writes `model.joblib` and `model_card.json` |
| `runtime.py` | Loads a model of either release and scores messages: the adapter the app calls. Keyword features are computed by the same function as in training |
| `evaluate.py` | Final evaluation of releases v1 and v2 side by side, with the keyword router and a majority baseline, on every test set |

## Commands

```bash
python -m src.classifier.train                                # release v2, bilingual -> models/intent_classifier/
python -m src.classifier.train --view transfer                # release v2, Spanish only, for the ES -> PT check -> models/intent_classifier_es_only/
python -m src.classifier.train --release v1                   # release v1, bilingual -> models/intent_classifier_v1/
python -m src.classifier.train --release v1 --view transfer   # release v1, Spanish only -> models/intent_classifier_es_only_v1/
python -m src.classifier.evaluate                             # final evaluation of v1 and v2 (needs all four model folders) -> eval/results/
python -m src.classifier.keywords "me cobraron dos veces el super"   # keyword router: intent and the rules that fired
python -m src.classifier.runtime "me cobraron dos veces"             # one message through the trained model
python -m pytest tests/classifier -q
```

`models/` is git-ignored. Training is deterministic: the same data, seed and scikit-learn version write the same `model.joblib` and `model_card.json`, and release v1 writes the same bytes as the v1 models scored in `eval/results` (without the `sklearn_version` key added later, the bytes of the first evaluated v1 files). The app and the evaluation read the default folders, which hold release v2. The docstring of `train.py` has the full v2 protocol and the candidate list.

## Runtime contract

```python
from src.classifier.runtime import load_default

clf = load_default()          # None, with a logged warning, when the model is missing or cannot be loaded
if clf is not None:
    result = clf("me cobraron dos veces")
```

```json
{"intent": "dispute_incorrect_charge_or_fee", "confidence": 0.981,
 "top": [{"intent": "dispute_incorrect_charge_or_fee", "p": 0.981}, {"intent": "account_payment_inquiry", "p": 0.01},
         {"intent": "other_complaint", "p": 0.004}],
 "below_threshold": false, "threshold": 0.55, "model_version": "intent-clf-2.0.0", "latency_ms": 1}
```

- `intent`: the most likely intent.
- `confidence`: its calibrated probability (softmax of the model scores divided by the fitted temperature; for a late-fusion model, mixed with the router's answer), rounded.
- `top`: the most likely intents with their rounded probabilities, best first.
- `below_threshold`: `confidence` is below `threshold`, which is `handoff.min_intent_confidence` in `src/policy/dispute_policy.json`. The agent then asks a clarifying question, and hands off with reason `low_intent_confidence` if confidence stays low.
- `model_version`: the version written by `train.py`. `latency_ms`: time spent in the call.

What the adapter guarantees:

- A call never raises. A message with no evidence gets the no-evidence answer: uniform probabilities, `out_of_scope` listed first and `below_threshold` true. No evidence means nothing is left after normalization, or no TF-IDF feature fires and no keyword rule group fires (emoji only, scripts the model never saw, control or zero-width characters). The router's fallback answer is not evidence. A scoring failure gets the same answer and is logged once per process with its traceback; the message itself is never logged.
- `IntentClassifier.load` is strict: it raises when the artifact is missing, unreadable or incompatible, and it scores a fixed message before returning, so a model that cannot score fails at load time. `load_default()` never raises, so the app can call it at import time.
- A model that uses keyword features records a fingerprint of the rules in `keywords.py`. If the rules change after training, loading refuses the model (retrain), as it does for a change in text normalization.
- A warning is logged when the installed scikit-learn differs from the version stored in the artifact.
- `clf.batch(texts)` and `clf.proba(texts)` give the evaluation the same answers as one call per message, and raise on scoring failure.
- The loaded model is read-only, so one instance can serve several threads.

## Data rules

Training, selection and calibration load only the train and dev splits. Release v2 uses both inside the grouped cross-validation, so for v2 the dev split is no longer a held-out set; its honest numbers are the out-of-fold ones in the model card. The keyword glossary was written while reading train-split messages, so keyword features look better on train rows than on unseen ones. The model card therefore also reports the out-of-fold metrics on rows from the dev split alone.

In this package, the final test sets in `eval/holdout/` and the generated test split are read only by `evaluate.py`, which fits nothing; outside it, only `src/scenarios/validate.py` reads `eval/holdout/`, for its overlap check. `tests/classifier/test_train.py` checks both rules statically and checks that training never loads a test row.

## Results

The readable tables are in [`eval/results/intent_classifier.md`](../../eval/results/intent_classifier.md), and every number, with its interval and count, is in `eval/results/intent_classifier.json`. Releases v1 and v2 are scored side by side on the same rows and bootstrap resamples, with paired differences such as v2 minus v1 and v2 minus the keyword router. The report opens with the protocol and history of both releases: v1 was selected on the dev split and scored before v2 existed; v2 was designed after v1's results were known and selected only by grouped cross-validation on train + dev; neither was designed or selected with the final sets. Those files come from the last run of `evaluate.py` and record the `model_version` and sha256 of each model they scored: run it again after retraining. The cross-validation table, the chosen candidate and its out-of-fold metrics are in `models/intent_classifier/model_card.json` (release v2). The dev metrics of release v1 are in `models/intent_classifier_v1/model_card.json`.
