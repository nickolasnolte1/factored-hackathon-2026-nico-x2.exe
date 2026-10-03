"""Trains the intake intent classifier (report 02, section 10). Two releases share the data rules, the metrics and
the artifact read by src.classifier.runtime; release v2 is the default.

    python -m src.classifier.train                                # v2, bilingual -> models/intent_classifier/
    python -m src.classifier.train --view transfer                # v2, ES only -> models/intent_classifier_es_only/
    python -m src.classifier.train --release v1                   # v1, bilingual -> models/intent_classifier_v1/
    python -m src.classifier.train --release v1 --view transfer   # v1, ES only -> models/intent_classifier_es_only_v1/
    python -m src.classifier.train --view bilingual --out DIR --seed 20261005

Views:
  bilingual  data/scenarios/intent_dataset.jsonl, ES + PT train and dev rows.
  transfer   data/scenarios/intent_lang_transfer_es_to_pt.jsonl, ES train and dev rows. No PT row is read, so the PT
             test rows of that file stay unseen.

Only the train and dev splits are loaded; the generated test split and the final test sets belong to the evaluation
code, and no evaluation result is read here.

Release v2 (model_version intent-clf-2.0.0 and intent-clf-es-only-2.0.0). Protocol:
  Rows         train + dev together; the dev split is no longer a separate selection set.
  Folds        GroupKFold, CV_FOLDS (5) folds, groups = family_id: every validation fold holds whole paraphrase
               families that its model never saw. TF-IDF vocabularies and models are fitted on the fold's training
               rows only; keyword features are fixed rules, so nothing in them is fitted.
  Score        mean over the folds of the acceptable-intents macro-F1 (effective label, averaged over the gold
               classes of the fold's validation rows). The ES and PT means and the standard deviation over folds
               (ddof 1) are recorded too.
  Tie rule     candidates within TIE_TOLERANCE (0.005) of the best mean are tied, and the simpler one wins: fewer
               feature blocks, then smaller C, then a smaller keyword weight or mix.
  Calibration  one softmax temperature fitted on the chosen candidate's out-of-fold decision scores (acceptable-set
               log-loss, as in v1). Most out-of-fold rows are train-split rows, where the keyword features look
               better than on unseen text (see below), so v2's confidence runs high on new text; the evaluation
               reports its calibration.
  Final model  the chosen candidate refitted on all train + dev rows, with that temperature.
Candidates:
  (a) union_svc_C0.1, union_svc_C0.3   the v1 configurations (v1 bilingual and v1 ES-only choices): word 1-2 +
                                       char_wb 2-5 TF-IDF + LinearSVC
  (b) char_lr_C1, C4, C16              char_wb 2-5 TF-IDF + logistic regression
  (c) hybrid_svc_C0.1_w*, hybrid_lr_C4_w*   the v1 TF-IDF union plus the keyword features times w (KEYWORD_WEIGHTS),
                                       into LinearSVC or logistic regression. Keyword features
                                       (runtime.keyword_features, also used by the runtime) are one indicator per
                                       rule group of keywords.py that fired (keywords.matches) and a one-hot of
                                       keywords.predict, computed on data.keyword_text of the message.
  (d) fusion_<base>_a*                 late fusion: p = (1 - a) * p_model + a * one-hot of keywords.predict, a in
                                       FUSION_MIXES. p_model is the calibrated probability of the text-only candidate
                                       that the same rule picks among (a) and (b); its pooled out-of-fold temperature
                                       is used inside the fold scores, so a is chosen by the same grouped CV.
Keyword features look better on train rows than on unseen ones, because the glossary's coverage passes read
train-split messages (keywords.py). The candidate table therefore also shows macro-F1 on out-of-fold rows from the
train split and from the dev split separately (the gap between the two is larger for the hybrids), and the card
repeats the chosen model's out-of-fold metrics on the dev rows alone. The keyword router on its own is scored on the
same folds as a reference row; it is not a candidate.

Release v1 (model_version intent-clf-1.0.0 and intent-clf-es-only-1.0.0), kept reproducible: the same data, seed and
scikit-learn version write the same bytes as the v1 models in the evaluation report (without the sklearn_version key
added later, the bytes of the first evaluated v1 files). Fit on train, select and calibrate on dev:
  char_lr     char_wb TF-IDF 2-5 (accents stripped) + logistic regression
  union_lr    word 1-2 + char_wb 2-5 TF-IDF + logistic regression
  union_svc   the same union + LinearSVC (scores from decision_function)
  word_cnb    word 1-2 TF-IDF + ComplementNB (scores from predict_log_proba)
Selection: dev macro-F1 with the acceptable-intents rule, tie-break by dev log-loss after temperature scaling.

Log-loss and calibration use the acceptable set: the loss of a row is -log of the probability mass on its acceptable
intents (plain log-loss on unambiguous rows), and a top intent counts as correct when it is acceptable.

Outputs: model.joblib (the fitted model, the temperature and the scikit-learn version; format 1 for v1, format 2 for
v2, see runtime.py) and model_card.json (no timestamps; two runs with the same seed and data write the same card).
The card's out-of-scope false-accept rate uses data.out_of_scope_false_accepts, the definition of the evaluation.
"""
import argparse
import json
import os
import warnings

import joblib
import numpy as np
import sklearn
from scipy.optimize import minimize_scalar
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import confusion_matrix, f1_score, precision_recall_fscore_support
from sklearn.model_selection import GroupKFold
from sklearn.naive_bayes import ComplementNB
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.svm import LinearSVC

from src.classifier import data
from src.classifier import runtime

SEED = 20261005
RELEASES = {
    "v1": {
        "bilingual": {"model_version": "intent-clf-1.0.0", "out": "intent_classifier_v1", "languages": ("es", "pt")},
        "transfer": {"model_version": "intent-clf-es-only-1.0.0", "out": "intent_classifier_es_only_v1",
                     "languages": ("es",)},
    },
    "v2": {
        "bilingual": {"model_version": "intent-clf-2.0.0", "out": "intent_classifier", "languages": ("es", "pt")},
        "transfer": {"model_version": "intent-clf-es-only-2.0.0", "out": "intent_classifier_es_only",
                     "languages": ("es",)},
    },
}
DEFAULT_RELEASE = "v2"
VIEWS = RELEASES[DEFAULT_RELEASE]       # the folders runtime.load_default and the evaluation read
MODELS_DIR = os.path.join(data.REPO, "models")
ECE_BINS = 10
TEMPERATURE_BOUNDS = (0.01, 100.0)
TEMPERATURE_DECIMALS = 4
CARD_DECIMALS = 6
SELECTION_DECIMALS = 4      # macro-F1 values equal at this precision count as a tie

CHAR_FEATURES = {"analyzer": "char_wb", "ngram_range": (2, 5), "min_df": 2}
WORD_FEATURES = {"analyzer": "word", "ngram_range": (1, 2), "min_df": 1, "token_pattern": r"(?u)\b\w+\b"}
FEATURE_SETS = {
    "char": (("char", CHAR_FEATURES),),
    "word_char": (("word", WORD_FEATURES), ("char", CHAR_FEATURES)),
    "word": (("word", WORD_FEATURES),),
}
V1_CANDIDATES = tuple(
    [{"name": f"char_lr_C{c:g}", "features": "char", "model": "logreg", "C": c} for c in (1.0, 4.0, 16.0)]
    + [{"name": f"union_lr_C{c:g}", "features": "word_char", "model": "logreg", "C": c} for c in (1.0, 4.0, 16.0)]
    + [{"name": f"union_svc_C{c:g}", "features": "word_char", "model": "linear_svc", "C": c} for c in (0.03, 0.1, 0.3)]
    + [{"name": f"word_cnb_a{a:g}", "features": "word", "model": "complement_nb", "alpha": a} for a in (0.3, 1.0, 3.0)]
)
SCORE_METHOD = {"logreg": "decision_function", "linear_svc": "decision_function", "complement_nb": "predict_log_proba"}

CV_FOLDS = 5
TIE_TOLERANCE = 0.005
CV_DECIMALS = 6             # fold means are compared at this precision
KEYWORD_WEIGHTS = (0.5, 1.0, 2.0)
FUSION_MIXES = (0.1, 0.2, 0.3, 0.4)
HYBRID_SVC_C = 0.1
HYBRID_LR_C = 4.0
V2_CANDIDATES = tuple(
    [{"name": f"union_svc_C{c:g}", "kind": "v1_config", "features": "word_char", "model": "linear_svc", "C": c}
     for c in (0.1, 0.3)]
    + [{"name": f"char_lr_C{c:g}", "kind": "char_lr", "features": "char", "model": "logreg", "C": c}
       for c in (1.0, 4.0, 16.0)]
    + [{"name": f"hybrid_svc_C{HYBRID_SVC_C:g}_w{w:g}", "kind": "hybrid", "features": "word_char",
        "keyword_weight": w, "model": "linear_svc", "C": HYBRID_SVC_C} for w in KEYWORD_WEIGHTS]
    + [{"name": f"hybrid_lr_C{HYBRID_LR_C:g}_w{w:g}", "kind": "hybrid", "features": "word_char",
        "keyword_weight": w, "model": "logreg", "C": HYBRID_LR_C} for w in KEYWORD_WEIGHTS]
)
FUSION_BASE_KINDS = ("v1_config", "char_lr")


# ---------------------------------------------------------------- model building

def _vectorizer(params):
    return TfidfVectorizer(strip_accents="unicode", lowercase=True, sublinear_tf=True, dtype=np.float64, **params)


def build_features(feature_key):
    parts = FEATURE_SETS[feature_key]
    if len(parts) == 1:
        return _vectorizer(parts[0][1])
    return FeatureUnion([(name, _vectorizer(params)) for name, params in parts])


def build_model(candidate, seed):
    kind = candidate["model"]
    if kind == "logreg":
        return LogisticRegression(C=candidate["C"], class_weight="balanced", solver="lbfgs", max_iter=5000,
                                  random_state=seed)
    if kind == "linear_svc":
        return LinearSVC(C=candidate["C"], class_weight="balanced", dual=True, max_iter=20000, random_state=seed)
    if kind == "complement_nb":
        return ComplementNB(alpha=candidate["alpha"])
    raise ValueError(f"unknown model: {kind}")


def describe(candidate):
    """JSON-friendly description of a candidate configuration."""
    features = [{"name": name, **{k: list(v) if isinstance(v, tuple) else v for k, v in params.items()},
                 "strip_accents": "unicode", "sublinear_tf": True} for name, params in FEATURE_SETS[candidate["features"]]]
    model = {k: v for k, v in candidate.items() if k not in ("name", "features")}
    if candidate["model"] in ("logreg", "linear_svc"):
        model["class_weight"] = "balanced"
    return {"name": candidate["name"], "features": features, "model": model,
            "score_method": SCORE_METHOD[candidate["model"]]}


# ---------------------------------------------------------------- metrics

def acceptable_mask(rows):
    mask = np.zeros((len(rows), len(data.CLASSES)), dtype=bool)
    index = {c: i for i, c in enumerate(data.CLASSES)}
    for r, row in enumerate(rows):
        for intent in row.get("acceptable_intents") or [row["intent"]]:
            mask[r, index[intent]] = True
    return mask


def set_log_loss(proba, mask):
    """Mean of -log(probability mass on the acceptable intents)."""
    mass = np.clip((proba * mask).sum(axis=1), 1e-15, 1.0)
    return float(-np.log(mass).mean())


def expected_calibration_error(proba, mask, bins=ECE_BINS):
    conf = proba.max(axis=1)
    correct = mask[np.arange(len(proba)), proba.argmax(axis=1)]
    edges = np.linspace(0.0, 1.0, bins + 1)
    which = np.clip(np.digitize(conf, edges[1:-1], right=True), 0, bins - 1)
    ece = 0.0
    for b in range(bins):
        sel = which == b
        if sel.any():
            ece += sel.mean() * abs(correct[sel].mean() - conf[sel].mean())
    return float(ece)


def fit_temperature(scores, mask):
    """Temperature minimizing the acceptable-set log-loss of softmax(scores / T), rounded."""
    log_lo, log_hi = np.log(TEMPERATURE_BOUNDS[0]), np.log(TEMPERATURE_BOUNDS[1])
    res = minimize_scalar(lambda lt: set_log_loss(runtime.softmax(scores, np.exp(lt)), mask),
                          bounds=(log_lo, log_hi), method="bounded", options={"xatol": 1e-6})
    return round(float(np.exp(res.x)), TEMPERATURE_DECIMALS)


def macro_f1(pred, rows):
    """Macro-F1 over the classes present in the effective labels or the predictions (acceptable-intents rule)."""
    truth = [data.effective_label(p, row) for p, row in zip(pred, rows)]
    labels = [c for c in data.CLASSES if c in set(truth) | set(pred)]
    return float(f1_score(truth, pred, labels=labels, average="macro", zero_division=0))


def block_metrics(proba, rows):
    """Macro-F1, accuracy, log-loss and ECE of probabilities (data.CLASSES order), acceptable-intents rule."""
    if not rows:
        return None
    mask = acceptable_mask(rows)
    pred = [data.CLASSES[i] for i in proba.argmax(axis=1)]
    return {
        "n": len(rows),
        "macro_f1": macro_f1(pred, rows),
        "accuracy": float(mask[np.arange(len(rows)), proba.argmax(axis=1)].mean()),
        "log_loss": set_log_loss(proba, mask),
        "ece": expected_calibration_error(proba, mask),
    }


def by_language(proba, rows, languages):
    out = {"overall": block_metrics(proba, rows)}
    for lang in languages:
        idx = [i for i, row in enumerate(rows) if row["language"] == lang]
        out[lang] = block_metrics(proba[idx], [rows[i] for i in idx])
    return out


def threshold_metrics(proba, rows, threshold):
    """Dev view of the policy threshold: how often the agent would ask a clarifying question, accuracy on each
    side, and out_of_scope messages answered with a non-acceptable intent instead of a question
    (data.out_of_scope_false_accepts, the definition the evaluation uses)."""
    mask = acceptable_mask(rows)
    top = proba.argmax(axis=1)
    conf = np.round(proba.max(axis=1), 3)
    below = conf < threshold
    correct = mask[np.arange(len(rows)), top]
    intent = np.array([data.CLASSES.index(row["intent"]) for row in rows], dtype=np.int64)
    hit, oos = data.out_of_scope_false_accepts(np.where(below, data.ABSTAIN, top), intent, mask)
    return {
        "threshold": threshold,
        "below_threshold_rate": float(below.mean()),
        "accuracy_at_or_above": float(correct[~below].mean()) if (~below).any() else None,
        "accuracy_below": float(correct[below].mean()) if below.any() else None,
        "out_of_scope_rows": int(oos.sum()),
        "out_of_scope_false_accepts": int(hit.sum()),
        "out_of_scope_false_accept_rate": float(hit.sum() / oos.sum()) if oos.any() else None,
    }


def per_class(proba, rows):
    pred = [data.CLASSES[i] for i in proba.argmax(axis=1)]
    truth = [data.effective_label(p, row) for p, row in zip(pred, rows)]
    p, r, f, s = precision_recall_fscore_support(truth, pred, labels=list(data.CLASSES), zero_division=0)
    table = {c: {"precision": float(p[i]), "recall": float(r[i]), "f1": float(f[i]), "support": int(s[i])}
             for i, c in enumerate(data.CLASSES)}
    matrix = confusion_matrix(truth, pred, labels=list(data.CLASSES)).tolist()
    return table, matrix


def _round(obj, decimals=CARD_DECIMALS):
    if isinstance(obj, float):
        return round(obj, decimals)
    if isinstance(obj, dict):
        return {k: _round(v, decimals) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round(v, decimals) for v in obj]
    return obj


# ---------------------------------------------------------------- fit + calibrate

def _check_rows(train_rows, dev_rows):
    for name, rows in (("train", train_rows), ("dev", dev_rows)):
        if not rows:
            raise ValueError(f"no {name} rows")
        if any(row.get("split") == "test" for row in rows):
            raise ValueError(f"{name} rows include the test split")
        bad = {row["intent"] for row in rows} - set(data.CLASSES)
        if bad:
            raise ValueError(f"unknown intents in {name}: {sorted(bad)}")
    missing = set(data.CLASSES) - {row["intent"] for row in train_rows}
    if missing:
        raise ValueError(f"train rows lack classes: {sorted(missing)}")


def fit_and_calibrate(train_rows, dev_rows, seed=SEED, candidates=V1_CANDIDATES, languages=("es", "pt"),
                      model_version=RELEASES["v1"]["bilingual"]["model_version"], view="bilingual"):
    """Release v1: fits every candidate on train_rows, selects on dev_rows and fits the chosen model's temperature on
    dev_rows.

    Returns (artifact, report). The artifact is what model.joblib holds and what runtime.IntentClassifier takes;
    the report has the candidate table and the dev metrics before and after calibration.
    """
    _check_rows(train_rows, dev_rows)
    train_texts = [data.normalize_text(row["text"]) for row in train_rows]
    dev_texts = [data.normalize_text(row["text"]) for row in dev_rows]
    train_y = [row["intent"] for row in train_rows]
    dev_mask = acceptable_mask(dev_rows)

    fitted_features = {}
    results = []
    for cand in candidates:
        key = cand["features"]
        if key not in fitted_features:
            vec = build_features(key)
            fitted_features[key] = (vec, vec.fit_transform(train_texts))
        vec, train_x = fitted_features[key]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ConvergenceWarning)
            model = build_model(cand, seed).fit(train_x, train_y)
        pipeline = Pipeline([("features", vec), ("model", model)])
        method = SCORE_METHOD[cand["model"]]
        scores = runtime.raw_scores(pipeline, method, dev_texts)
        before = runtime.softmax(scores, 1.0)
        temperature = fit_temperature(scores, dev_mask)
        after = runtime.softmax(scores, temperature)
        metrics = by_language(after, dev_rows, languages)
        results.append({
            "candidate": cand, "pipeline": pipeline, "score_method": method,
            "temperature": temperature, "before": before, "after": after, "metrics": metrics,
            "row": {
                "name": cand["name"],
                "macro_f1": metrics["overall"]["macro_f1"],
                **{f"macro_f1_{lang}": metrics[lang]["macro_f1"] for lang in languages},
                "accuracy": metrics["overall"]["accuracy"],
                "log_loss_t1": set_log_loss(before, dev_mask),
                "temperature": temperature,
                "log_loss": metrics["overall"]["log_loss"],
                "ece": metrics["overall"]["ece"],
            },
        })

    best = min(results, key=lambda r: (-round(r["row"]["macro_f1"], SELECTION_DECIMALS), r["row"]["log_loss"]))
    threshold = data.min_intent_confidence()
    classes_table, matrix = per_class(best["after"], dev_rows)
    report = {
        "candidates": [r["row"] for r in results],
        "chosen": describe(best["candidate"]),
        "temperature": best["temperature"],
        "dev": {
            "calibrated": best["metrics"],
            "calibration": {
                "before": {"temperature": 1.0, "log_loss": set_log_loss(best["before"], dev_mask),
                           "ece": expected_calibration_error(best["before"], dev_mask)},
                "after": {"temperature": best["temperature"], "log_loss": set_log_loss(best["after"], dev_mask),
                          "ece": expected_calibration_error(best["after"], dev_mask)},
            },
            "policy_threshold": threshold_metrics(best["after"], dev_rows, threshold),
            "per_class": classes_table,
            "confusion_matrix": {"labels": list(data.CLASSES), "rows_true_cols_pred": matrix},
        },
    }
    artifact = {
        "format": runtime.ARTIFACT_FORMAT_V1,
        "model_version": model_version,
        "view": view,
        "classes": list(data.CLASSES),
        "normalization_id": data.NORMALIZATION_ID,
        "pipeline": best["pipeline"],
        "score_method": best["score_method"],
        "temperature": best["temperature"],
        "config": describe(best["candidate"]),
        "sklearn_version": sklearn.__version__,
    }
    return artifact, report


# ---------------------------------------------------------------- release v2: grouped cross-validation on train + dev

def fusion_candidate(base, mix):
    return {"name": f"fusion_{base['name']}_a{mix:g}", "kind": "fusion", "base": base, "keyword_mix": mix}


def fitted_candidate(cand):
    """The candidate whose model is fitted: the base of a late-fusion candidate, else the candidate itself."""
    return cand["base"] if cand["kind"] == "fusion" else cand


def feature_blocks(cand):
    """Feature blocks for the tie rule: TF-IDF blocks, plus one for keyword features or for the fused router."""
    if cand["kind"] == "fusion":
        return feature_blocks(cand["base"]) + 1
    return len(FEATURE_SETS[cand["features"]]) + (1 if cand.get("keyword_weight") else 0)


def describe_v2(cand):
    """JSON-friendly description of a v2 candidate."""
    fitted = fitted_candidate(cand)
    base = describe({k: fitted[k] for k in ("name", "features", "model", "C")})
    features = base["features"]
    if fitted.get("keyword_weight"):
        features = features + [{"name": "keywords", "weight": fitted["keyword_weight"],
                                "columns": list(runtime.KEYWORD_COLUMNS), "input": "data.keyword_text",
                                "rules_id": runtime.KEYWORD_RULES_ID}]
    out = {"name": cand["name"], "kind": cand["kind"], "feature_blocks": feature_blocks(cand), "features": features,
           "model": base["model"], "score_method": base["score_method"]}
    if cand["kind"] == "fusion":
        out["late_fusion"] = {"base": fitted["name"], "keyword_mix": cand["keyword_mix"],
                              "router": "one-hot of keywords.predict", "rules_id": runtime.KEYWORD_RULES_ID}
    return out


def gold_macro_f1(pred, rows):
    """Acceptable-intents macro-F1 averaged over the gold classes of the rows (the intents present), as in the
    evaluation: a class that is only predicted is not averaged in."""
    truth = [data.effective_label(p, row) for p, row in zip(pred, rows)]
    labels = [c for c in data.CLASSES if c in {row["intent"] for row in rows}]
    return float(f1_score(truth, pred, labels=labels, average="macro", zero_division=0))


def grouped_folds(rows, n_folds=CV_FOLDS):
    """GroupKFold folds over family_id: (train indices, validation indices) per fold. Raises when a fold's training
    rows lack a class."""
    groups = [row["family_id"] for row in rows]
    if len(set(groups)) < n_folds:
        raise ValueError(f"{len(set(groups))} families, need at least {n_folds}")
    folds = list(GroupKFold(n_splits=n_folds).split(np.zeros(len(rows)), groups=groups))
    for k, (tr, _) in enumerate(folds, 1):
        missing = set(data.CLASSES) - {rows[i]["intent"] for i in tr}
        if missing:
            raise ValueError(f"fold {k}: training rows lack classes {sorted(missing)}")
    return folds


def fit_model(cand, x_text, x_keywords, y, seed):
    """Fits a text or hybrid candidate's model on its design matrix (runtime.design_matrix)."""
    x = runtime.design_matrix(x_text, x_keywords, cand.get("keyword_weight", 0.0))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        return build_model(cand, seed).fit(x, y)


def fold_scores(pred, correct, rows, folds, languages):
    """Per-fold macro-F1 (overall and by language) and accuracy of out-of-fold predictions, and their summary: fold
    mean, standard deviation (ddof 1), language means, and macro-F1 on the rows from the train and dev splits."""
    per_fold = []
    for k, (_, va) in enumerate(folds, 1):
        entry = {"fold": k, "macro_f1": gold_macro_f1([pred[i] for i in va], [rows[i] for i in va]),
                 "accuracy": float(correct[va].mean())}
        for lang in languages:
            idx = [i for i in va if rows[i]["language"] == lang]
            entry[f"macro_f1_{lang}"] = gold_macro_f1([pred[i] for i in idx], [rows[i] for i in idx]) if idx else None
        per_fold.append(entry)
    f1 = np.array([f["macro_f1"] for f in per_fold])
    summary = {"macro_f1": float(f1.mean()), "macro_f1_sd": float(f1.std(ddof=1)) if len(f1) > 1 else 0.0}
    for lang in languages:
        values = [f[f"macro_f1_{lang}"] for f in per_fold if f[f"macro_f1_{lang}"] is not None]
        summary[f"macro_f1_{lang}"] = float(np.mean(values)) if values else None
    summary["accuracy"] = float(np.mean([f["accuracy"] for f in per_fold]))
    for split in ("train", "dev"):
        idx = [i for i, r in enumerate(rows) if r["split"] == split]
        value = gold_macro_f1([pred[i] for i in idx], [rows[i] for i in idx]) if idx else None
        summary[f"macro_f1_rows_from_{split}"] = value
    return per_fold, summary


def out_of_fold_scores(candidates, texts, x_keywords, y, folds, seed):
    """Out-of-fold decision scores of each text or hybrid candidate ({name: (n, 6) array}): every row is scored by
    the model fitted on the other folds. TF-IDF features are fitted on the fold's training rows only."""
    scores = {cand["name"]: np.zeros((len(texts), len(data.CLASSES))) for cand in candidates}
    for tr, va in folds:
        text_x = {}
        for cand in candidates:
            key = cand["features"]
            if key not in text_x:
                vec = build_features(key)
                text_x[key] = (vec.fit_transform([texts[i] for i in tr]), vec.transform([texts[i] for i in va]))
            x_tr, x_va = text_x[key]
            model = fit_model(cand, x_tr, x_keywords[tr], y[tr], seed)
            x_va = runtime.design_matrix(x_va, x_keywords[va], cand.get("keyword_weight", 0.0))
            scores[cand["name"]][va] = runtime.model_scores(model, SCORE_METHOD[cand["model"]], x_va)
    return scores


def summarize_cv(cand, before, after, temperature, rows, folds, mask, languages):
    """Candidate-table row and per-fold values from out-of-fold probabilities (before and after the temperature)."""
    top = after.argmax(axis=1)
    per_fold, summary = fold_scores([data.CLASSES[i] for i in top], mask[np.arange(len(rows)), top], rows, folds,
                                    languages)
    row = {"name": cand["name"], "kind": cand["kind"], "feature_blocks": feature_blocks(cand),
           "C": fitted_candidate(cand)["C"], "keyword_weight": fitted_candidate(cand).get("keyword_weight", 0.0),
           "keyword_mix": cand.get("keyword_mix", 0.0), **summary,
           "temperature": temperature, "log_loss_t1": set_log_loss(before, mask),
           "log_loss": set_log_loss(after, mask), "ece": expected_calibration_error(after, mask)}
    return row, per_fold


def select_cv(table):
    """Tie rule: rows within TIE_TOLERANCE of the best fold mean are tied; the simplest wins (fewer feature blocks,
    then smaller C, then smaller keyword weight or mix, then higher mean, then name). Returns (row, best, tied)."""
    best = max(round(r["macro_f1"], CV_DECIMALS) for r in table)
    floor = round(best - TIE_TOLERANCE, CV_DECIMALS)
    tied = [r for r in table if round(r["macro_f1"], CV_DECIMALS) >= floor]
    chosen = min(tied, key=lambda r: (r["feature_blocks"], r["C"], r["keyword_weight"] + r["keyword_mix"],
                                      -round(r["macro_f1"], CV_DECIMALS), r["name"]))
    return chosen, best, [r["name"] for r in tied]


def fit_cv(train_rows, dev_rows, seed=SEED, candidates=V2_CANDIDATES, mixes=FUSION_MIXES, languages=("es", "pt"),
           model_version=RELEASES["v2"]["bilingual"]["model_version"], view="bilingual"):
    """Release v2: grouped cross-validation over train_rows + dev_rows, the tie rule, one temperature on the chosen
    candidate's out-of-fold scores, and the chosen candidate refitted on all rows.

    Returns (artifact, report) like fit_and_calibrate; the report has the candidate table, the folds, the selection
    and the out-of-fold metrics of the chosen candidate.
    """
    _check_rows(train_rows, dev_rows)
    rows = list(train_rows) + list(dev_rows)
    texts = [data.normalize_text(row["text"]) for row in rows]
    x_keywords = runtime.keyword_features([data.keyword_text(row["text"]) for row in rows])
    y = np.array([row["intent"] for row in rows])
    mask = acceptable_mask(rows)
    folds = grouped_folds(rows)
    scores = out_of_fold_scores(candidates, texts, x_keywords, y, folds, seed)

    results = {}
    for cand in candidates:
        temperature = fit_temperature(scores[cand["name"]], mask)
        before = runtime.softmax(scores[cand["name"]], 1.0)
        after = runtime.softmax(scores[cand["name"]], temperature)
        row, per_fold = summarize_cv(cand, before, after, temperature, rows, folds, mask, languages)
        results[cand["name"]] = {"candidate": cand, "row": row, "folds": per_fold, "before": before, "after": after}

    text_only = [results[c["name"]]["row"] for c in candidates if c["kind"] in FUSION_BASE_KINDS]
    fusion_base = None
    if text_only and mixes:
        fusion_base = results[select_cv(text_only)[0]["name"]]
        base = fusion_base["candidate"]
        temperature = fusion_base["row"]["temperature"]
        for mix in mixes:
            cand = fusion_candidate(base, mix)
            before = runtime.fuse(runtime.softmax(scores[base["name"]], 1.0), x_keywords, mix)
            after = runtime.fuse(runtime.softmax(scores[base["name"]], temperature), x_keywords, mix)
            row, per_fold = summarize_cv(cand, before, after, temperature, rows, folds, mask, languages)
            results[cand["name"]] = {"candidate": cand, "row": row, "folds": per_fold, "before": before,
                                     "after": after}

    table = [r["row"] for r in results.values()]
    chosen_row, best, tied = select_cv(table)
    router = x_keywords[:, len(runtime.KEYWORD_GROUPS):].toarray().argmax(axis=1)
    _, router_summary = fold_scores([data.CLASSES[i] for i in router], mask[np.arange(len(rows)), router], rows,
                                    folds, languages)
    reference = {"name": "keyword_router", "kind": "reference",
                 "note": "keywords.predict alone, scored on the same folds; nothing is fitted, not a candidate",
                 **router_summary}
    best_result = results[chosen_row["name"]]
    chosen = best_result["candidate"]
    fitted = fitted_candidate(chosen)
    temperature = chosen_row["temperature"]

    vec = build_features(fitted["features"])
    model = fit_model(fitted, vec.fit_transform(texts), x_keywords, y, seed)
    weight = float(fitted.get("keyword_weight", 0.0))
    mix = float(chosen.get("keyword_mix", 0.0))

    threshold = data.min_intent_confidence()
    after, before = best_result["after"], best_result["before"]
    classes_table, matrix = per_class(after, rows)
    from_dev = [i for i, row in enumerate(rows) if row["split"] == "dev"]
    dev_part = [rows[i] for i in from_dev]
    report = {
        "candidates": table,
        "reference": reference,
        "chosen": describe_v2(chosen),
        "temperature": temperature,
        "selection": {"score": "mean fold macro-F1", "best_macro_f1": best, "tolerance": TIE_TOLERANCE,
                      "tied": tied, "chosen": chosen["name"],
                      "fusion_base": fusion_base["candidate"]["name"] if fusion_base else None},
        "folds": [{"fold": k, "rows": len(va), "families": len({rows[i]["family_id"] for i in va}),
                   "rows_by_language": {lang: sum(rows[i]["language"] == lang for i in va) for lang in languages},
                   "rows_from_dev": sum(rows[i]["split"] == "dev" for i in va)}
                  for k, (_, va) in enumerate(folds, 1)],
        "cv": {
            "predictions": "out-of-fold: every train + dev row scored by the fold model that did not see its family",
            "per_fold": best_result["folds"],
            "calibrated": by_language(after, rows, languages),
            "calibration": {
                "before": {"temperature": 1.0, "log_loss": set_log_loss(before, mask),
                           "ece": expected_calibration_error(before, mask)},
                "after": {"temperature": temperature, "log_loss": set_log_loss(after, mask),
                          "ece": expected_calibration_error(after, mask)},
            },
            "policy_threshold": threshold_metrics(after, rows, threshold),
            "per_class": classes_table,
            "confusion_matrix": {"labels": list(data.CLASSES), "rows_true_cols_pred": matrix},
            "rows_from_dev": {
                "note": "the same out-of-fold predictions, rows from the dev split only: messages the keyword "
                        "glossary's coverage passes never read, so the keyword features are not optimistic here",
                "calibrated": by_language(after[from_dev], dev_part, languages),
                "policy_threshold": threshold_metrics(after[from_dev], dev_part, threshold),
            } if from_dev else None,
        },
    }
    artifact = {
        "format": runtime.ARTIFACT_FORMAT,
        "model_version": model_version,
        "view": view,
        "classes": list(data.CLASSES),
        "normalization_id": data.NORMALIZATION_ID,
        "text_features": vec,
        "model": model,
        "score_method": SCORE_METHOD[fitted["model"]],
        "temperature": temperature,
        "keyword_weight": weight,
        "keyword_mix": mix,
        "keyword_rules": runtime.KEYWORD_RULES_ID if (weight or mix) else None,
        "config": describe_v2(chosen),
        "sklearn_version": sklearn.__version__,
    }
    return artifact, report


PROTOCOL_V2 = {
    "rows": "train + dev splits together",
    "folds": f"GroupKFold, {CV_FOLDS} folds, groups = family_id: every validation fold holds whole paraphrase families "
             "that its model never saw; TF-IDF vocabularies and models are fitted on the fold's training rows only",
    "score": "mean over folds of the acceptable-intents macro-F1 (effective label, averaged over the gold classes of "
             "the fold's validation rows); ES and PT means and the fold standard deviation (ddof 1) are recorded",
    "selection": f"candidates within {TIE_TOLERANCE} of the best fold mean are tied; the simpler one wins: fewer "
                 "feature blocks, then smaller C (compared as a number, also across LinearSVC and logistic "
                 "regression), then smaller keyword weight or mix",
    "late_fusion": "p = (1 - a) * p_model + a * one-hot of keywords.predict; p_model is the calibrated probability of "
                   "the text-only candidate the same rule picks among the v1 configurations and char_lr, and its "
                   "pooled out-of-fold temperature is used inside the fold scores",
    "calibration": "one softmax temperature fitted on the chosen candidate's out-of-fold decision scores, minimizing "
                   "the acceptable-set log-loss (for late fusion, the base model's scores)",
    "final_model": "the chosen candidate refitted on all train + dev rows, with the out-of-fold temperature",
    "keyword_features": "one 0/1 indicator per rule group of src/classifier/keywords.py that fired (keywords.matches) "
                        "and a one-hot of keywords.predict, on data.keyword_text of the message; computed by "
                        "runtime.keyword_features in training and at runtime",
    "keyword_note": "keyword features look better on train rows than on unseen ones, because the glossary's "
                    "coverage passes read train-split messages; compare macro_f1_rows_from_train with "
                    "macro_f1_rows_from_dev in the candidate table (the gap is larger for the hybrid candidates and "
                    "the keyword_router reference), and read cv_metrics.rows_from_dev for the chosen model",
    "log_loss": "-log(probability mass on the acceptable intents); plain log-loss on unambiguous rows",
    "ece": f"{ECE_BINS} equal-width bins on top probability; correct = top intent is acceptable",
    "out_of_scope_false_accept": data.OUT_OF_SCOPE_FALSE_ACCEPT_RULE + "; below the policy threshold the agent asks "
                                 "a question, which is an abstention",
    "test_split": "not loaded",
}


def build_card_v2(artifact, report, view, seed, dataset_path, rows_by_split):
    card = {
        "model_version": artifact["model_version"],
        "release": "v2",
        "view": view,
        "task": "intake intent classification for ES/PT transaction-dispute messages (report 02, section 10)",
        "classes": list(data.CLASSES),
        "normalization": {"id": data.NORMALIZATION_ID, "max_chars": data.MAX_CHARS,
                          "steps": ["NFKC", "lowercase", "digit runs -> 0", "collapse whitespace",
                                    f"keep the first {data.MAX_CHARS} characters"]},
        "chosen": artifact["config"],
        "temperature": artifact["temperature"],
        "keyword_weight": artifact["keyword_weight"],
        "keyword_mix": artifact["keyword_mix"],
        "keyword_rules_id": artifact["keyword_rules"],
        "procedure": PROTOCOL_V2,
        "folds": report["folds"],
        "candidates": report["candidates"],
        "reference": report["reference"],
        "selection": report["selection"],
        "policy": {"file": data.repo_relative(data.POLICY_PATH),
                   "min_intent_confidence": data.min_intent_confidence()},
        "seed": seed,
        "sklearn_version": sklearn.__version__,
        "data": {"file": data.repo_relative(dataset_path), "sha256": data.file_sha256(dataset_path),
                 "rows": row_counts(rows_by_split)},
        "cv_metrics": report["cv"],
    }
    return _round(card)


# ---------------------------------------------------------------- CLI path

def row_counts(rows_by_split):
    out = {}
    for split, rows in rows_by_split.items():
        counts = {}
        for row in rows:
            counts[row["language"]] = counts.get(row["language"], 0) + 1
        out[split] = dict(sorted(counts.items()))
    return out


def build_card(artifact, report, view, seed, dataset_path, rows_by_split):
    card = {
        "model_version": artifact["model_version"],
        "view": view,
        "task": "intake intent classification for ES/PT transaction-dispute messages (report 02, section 10)",
        "classes": list(data.CLASSES),
        "normalization": {"id": data.NORMALIZATION_ID, "max_chars": data.MAX_CHARS,
                          "steps": ["NFKC", "lowercase", "digit runs -> 0", "collapse whitespace",
                                    f"keep the first {data.MAX_CHARS} characters"]},
        "chosen": artifact["config"],
        "temperature": artifact["temperature"],
        "procedure": {
            "fit": "train split",
            "selection": "dev split: acceptable-intents macro-F1, tie-break by dev log-loss after temperature "
                         f"(macro-F1 compared at {SELECTION_DECIMALS} decimals)",
            "calibration": "one softmax temperature on the dev split, minimizing the acceptable-set log-loss",
            "log_loss": "-log(probability mass on the acceptable intents); plain log-loss on unambiguous rows",
            "ece": f"{ECE_BINS} equal-width bins on top probability; correct = top intent is acceptable",
            "out_of_scope_false_accept": data.OUT_OF_SCOPE_FALSE_ACCEPT_RULE + "; below the policy threshold the "
                                         "agent asks a question, which is an abstention",
            "test_split": "not loaded",
        },
        "candidates": report["candidates"],
        "policy": {"file": data.repo_relative(data.POLICY_PATH),
                   "min_intent_confidence": data.min_intent_confidence()},
        "seed": seed,
        "sklearn_version": sklearn.__version__,
        "data": {"file": data.repo_relative(dataset_path), "sha256": data.file_sha256(dataset_path),
                 "rows": row_counts(rows_by_split)},
        "dev_metrics": report["dev"],
    }
    return _round(card)


def run(view="bilingual", out_dir=None, seed=SEED, dataset_path=None, quiet=False, release=DEFAULT_RELEASE):
    """CLI path: loads train and dev rows of the view, fits the release, writes model.joblib and model_card.json."""
    if release not in RELEASES:
        raise ValueError(f"unknown release: {release}")
    if view not in RELEASES[release]:
        raise ValueError(f"unknown view: {view}")
    spec = RELEASES[release][view]
    if view == "bilingual":
        path = dataset_path or data.INTENT_DATASET
        train_rows = data.load_intent_rows(("train",), path)
        dev_rows = data.load_intent_rows(("dev",), path)
    else:
        path = dataset_path or data.TRANSFER_DATASET
        train_rows = data.load_transfer_rows(("train",), path)
        dev_rows = data.load_transfer_rows(("dev",), path)
        other = {row["language"] for row in train_rows + dev_rows} - {"es"}
        if other:
            raise ValueError(f"transfer view expects ES train/dev rows only, found {sorted(other)}")
    out_dir = out_dir or os.path.join(MODELS_DIR, spec["out"])

    if release == "v1":
        artifact, report = fit_and_calibrate(train_rows, dev_rows, seed=seed, languages=spec["languages"],
                                             model_version=spec["model_version"], view=view)
        card = build_card(artifact, report, view, seed, path, {"train": train_rows, "dev": dev_rows})
    else:
        artifact, report = fit_cv(train_rows, dev_rows, seed=seed, languages=spec["languages"],
                                  model_version=spec["model_version"], view=view)
        card = build_card_v2(artifact, report, view, seed, path, {"train": train_rows, "dev": dev_rows})
    os.makedirs(out_dir, exist_ok=True)
    joblib.dump(artifact, os.path.join(out_dir, runtime.MODEL_FILE))
    with open(os.path.join(out_dir, runtime.CARD_FILE), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(card, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    if not quiet:
        (print_report if release == "v1" else print_report_v2)(card, out_dir)
    return card


def _fmt(value, width=7):
    return f"{value:>{width}.4f}" if isinstance(value, float) else f"{'-':>{width}}"


def print_report(card, out_dir):
    langs = [lang for lang in ("es", "pt") if lang in card["data"]["rows"]["train"]]
    print(f"view={card['view']}  model_version={card['model_version']}  seed={card['seed']}  "
          f"sklearn={card['sklearn_version']}")
    print(f"data={card['data']['file']}  rows={json.dumps(card['data']['rows'])}")
    print()
    head = f"{'candidate':<18}{'F1':>7}" + "".join(f"{'F1_' + lang:>7}" for lang in langs)
    print(head + f"{'acc':>7}{'LL_T1':>8}{'T':>8}{'LL_cal':>8}{'ECE':>7}")
    chosen = card["chosen"]["name"]
    for row in card["candidates"]:
        line = f"{row['name']:<18}{_fmt(row['macro_f1'])}" + "".join(_fmt(row.get(f"macro_f1_{lang}")) for lang in langs)
        line += f"{_fmt(row['accuracy'])}{_fmt(row['log_loss_t1'], 8)}{_fmt(row['temperature'], 8)}"
        line += f"{_fmt(row['log_loss'], 8)}{_fmt(row['ece'])}"
        print(line + ("  <- chosen" if row["name"] == chosen else ""))
    print()
    dev = card["dev_metrics"]
    print(f"chosen: {chosen} ({card['chosen']['score_method']}), temperature {card['temperature']}")
    for block in ["overall"] + langs:
        m = dev["calibrated"][block]
        print(f"  dev {block:<8} n={m['n']:<5} macro-F1 {m['macro_f1']:.4f}  accuracy {m['accuracy']:.4f}  "
              f"log-loss {m['log_loss']:.4f}  ECE {m['ece']:.4f}")
    cal = dev["calibration"]
    print(f"  calibration before (T=1): log-loss {cal['before']['log_loss']:.4f}  ECE {cal['before']['ece']:.4f}")
    print(f"  calibration after (T={cal['after']['temperature']}): log-loss {cal['after']['log_loss']:.4f}  "
          f"ECE {cal['after']['ece']:.4f}")
    th = dev["policy_threshold"]
    print(f"  policy threshold {th['threshold']}: below {th['below_threshold_rate']:.4f} of dev rows, "
          f"accuracy at/above {_fmt(th['accuracy_at_or_above'], 0)}, below {_fmt(th['accuracy_below'], 0)}, "
          f"out_of_scope false accept {_fmt(th['out_of_scope_false_accept_rate'], 0)}")
    print(f"wrote {os.path.join(out_dir, runtime.MODEL_FILE)} and {runtime.CARD_FILE}")


def print_report_v2(card, out_dir):
    langs = [lang for lang in ("es", "pt") if lang in card["data"]["rows"]["train"]]
    print(f"release=v2  view={card['view']}  model_version={card['model_version']}  seed={card['seed']}  "
          f"sklearn={card['sklearn_version']}")
    print(f"data={card['data']['file']}  rows={json.dumps(card['data']['rows'])}")
    print(f"folds: GroupKFold by family_id, rows {[f['rows'] for f in card['folds']]}, "
          f"families {[f['families'] for f in card['folds']]}")
    print()
    head = f"{'candidate':<30}{'blk':>4}{'F1':>7}{'sd':>7}" + "".join(f"{'F1_' + lang:>7}" for lang in langs)
    print(head + f"{'acc':>7}{'F1_trn':>8}{'F1_dev':>8}{'T':>8}{'LL_cal':>8}{'ECE':>7}")
    sel = card["selection"]
    for row in card["candidates"]:
        line = f"{row['name']:<30}{row['feature_blocks']:>4}{_fmt(row['macro_f1'])}{_fmt(row['macro_f1_sd'])}"
        line += "".join(_fmt(row.get(f"macro_f1_{lang}")) for lang in langs)
        line += f"{_fmt(row['accuracy'])}{_fmt(row['macro_f1_rows_from_train'], 8)}"
        line += f"{_fmt(row['macro_f1_rows_from_dev'], 8)}{_fmt(row['temperature'], 8)}{_fmt(row['log_loss'], 8)}"
        line += _fmt(row["ece"])
        mark = "  <- chosen" if row["name"] == sel["chosen"] else ("  (tied)" if row["name"] in sel["tied"] else "")
        print(line + mark)
    ref = card["reference"]
    line = f"{'reference: ' + ref['name']:<30}{'':>4}{_fmt(ref['macro_f1'])}{_fmt(ref['macro_f1_sd'])}"
    line += "".join(_fmt(ref.get(f"macro_f1_{lang}")) for lang in langs)
    line += f"{_fmt(ref['accuracy'])}{_fmt(ref['macro_f1_rows_from_train'], 8)}{_fmt(ref['macro_f1_rows_from_dev'], 8)}"
    print(line + "  (not a candidate)")
    print()
    print(f"best fold mean {sel['best_macro_f1']:.4f}; tied within {sel['tolerance']}: {', '.join(sel['tied'])}; "
          f"late-fusion base: {sel['fusion_base']}")
    print(f"chosen: {sel['chosen']} ({card['chosen']['kind']}, {card['chosen']['feature_blocks']} feature blocks, "
          f"{card['chosen']['score_method']}), temperature {card['temperature']}, keyword weight "
          f"{card['keyword_weight']}, keyword mix {card['keyword_mix']}")
    cv = card["cv_metrics"]
    for fold in cv["per_fold"]:
        print(f"  fold {fold['fold']}  macro-F1 {fold['macro_f1']:.4f}"
              + "".join(f"  {lang} {_fmt(fold.get(f'macro_f1_{lang}'), 0)}" for lang in langs)
              + f"  accuracy {fold['accuracy']:.4f}")
    for block in ["overall"] + langs:
        m = cv["calibrated"][block]
        print(f"  out-of-fold {block:<8} n={m['n']:<5} macro-F1 {m['macro_f1']:.4f}  accuracy {m['accuracy']:.4f}  "
              f"log-loss {m['log_loss']:.4f}  ECE {m['ece']:.4f}")
    cal = cv["calibration"]
    print(f"  calibration before (T=1): log-loss {cal['before']['log_loss']:.4f}  ECE {cal['before']['ece']:.4f}")
    print(f"  calibration after (T={cal['after']['temperature']}): log-loss {cal['after']['log_loss']:.4f}  "
          f"ECE {cal['after']['ece']:.4f}")
    blocks = [("all rows", cv["policy_threshold"])]
    if cv["rows_from_dev"]:
        m = cv["rows_from_dev"]["calibrated"]["overall"]
        print(f"  out-of-fold, rows from dev  n={m['n']:<5} macro-F1 {m['macro_f1']:.4f}  "
              f"accuracy {m['accuracy']:.4f}  log-loss {m['log_loss']:.4f}  ECE {m['ece']:.4f}")
        blocks.append(("rows from dev", cv["rows_from_dev"]["policy_threshold"]))
    for name, th in blocks:
        print(f"  policy threshold {th['threshold']}, {name}: below {th['below_threshold_rate']:.4f}, "
              f"accuracy at/above {_fmt(th['accuracy_at_or_above'], 0)}, below {_fmt(th['accuracy_below'], 0)}, "
              f"out_of_scope false accept {_fmt(th['out_of_scope_false_accept_rate'], 0)} "
              f"({th['out_of_scope_false_accepts']}/{th['out_of_scope_rows']})")
    print(f"wrote {os.path.join(out_dir, runtime.MODEL_FILE)} and {runtime.CARD_FILE}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--view", choices=sorted(VIEWS), default="bilingual")
    ap.add_argument("--release", choices=sorted(RELEASES), default=DEFAULT_RELEASE)
    ap.add_argument("--out", default=None, help="output folder (default models/intent_classifier[_es_only][_v1])")
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args(argv)
    run(args.view, args.out, args.seed, release=args.release)


if __name__ == "__main__":
    main()
