"""Final evaluation of the intake intent classifier (report 02, sections 9 and 10; eval/holdout/README.md, "How to
score"). Besides the overlap check in src/scenarios/validate.py, this is the only module that reads the final test
sets in eval/holdout/. It fits nothing: the models, the keyword rules and the policy threshold are used as they are.
Releases v1 and v2 of the model (src.classifier.train --release) are scored side by side, on the same rows and the
same bootstrap resamples.

    python -m src.classifier.evaluate
    python -m src.classifier.evaluate --v1-dir models/intent_classifier_v1
                                      --v1-es-only-dir models/intent_classifier_es_only_v1
                                      --v2-dir models/intent_classifier
                                      --v2-es-only-dir models/intent_classifier_es_only
                                      --boot 2000 --seed 20261005 --out eval/results

Protocol and history (PROTOCOL, also the first section of the readable report): v1 was selected on the dev split and
scored, frozen, on the test split and the final sets before v2 existed. v2 was designed after the v1 and keyword-router
results were known and selected only by grouped cross-validation on train + dev. Neither model was fitted or selected
on the final sets; every score here comes from frozen models.

Systems:
  majority                   the most frequent intent of the train split (the only use of training rows here)
  keyword                    src.classifier.keywords, the glossary router
  model_v1, model_v2         the bilingual classifier of each release, forced choice (top-1 intent)
  model_v1_threshold, model_v2_threshold
                             the same models under the policy: confidence below handoff.min_intent_confidence means
                             the agent asks a clarifying question instead of answering (an abstention)
  model_es_only_v1[_threshold], model_es_only_v2[_threshold]
                             transfer set only: the classifier of each release trained on Spanish rows only. The
                             keyword features of v2 come from keywords.py, whose rules hold Portuguese terms too, so
                             ES-only v2 is not blind to Portuguese the way ES-only v1 is

Test sets, each cut into the slices all, es, pt and mixed (variant "mixed", portunhol) when they have rows:
  test          generated test split of data/scenarios/intent_dataset.jsonl
  transfer_pt   PT test rows of data/scenarios/intent_lang_transfer_es_to_pt.jsonl: the ES-only models next to the
                bilingual models on the same rows
  independent   eval/holdout/independent_es.jsonl + independent_pt.jsonl
  team          eval/holdout/team_handwritten.jsonl (small: every number comes with its n)

Scoring rule: a prediction is correct when it is in acceptable_intents. Macro-F1 uses the effective label (the
prediction when it is acceptable, else the intent) and averages F1 over a fixed label set per slice: the gold
classes (row intents) present in the full slice. Every bootstrap resample averages over the same classes; a class
with no row and no prediction in a resample counts as F1 0. Slices where a gold class has fewer than
MACRO_F1_MIN_CLASS_ROWS rows, or with fewer than 3 gold classes, are marked in the readable report: read accuracy
there. An abstention matches no class,
so it counts as a miss in macro-F1 and accuracy; abstention rates, coverage, selective accuracy and selective
macro-F1 (answered rows only, over the gold classes of the answered rows) describe the thresholded models
separately. Out-of-scope false accepts use data.out_of_scope_false_accepts, the definition behind
the numbers in the model cards.

Intervals are 95% percentile bootstraps over rows (B = --boot). Each (set, slice) has its own numpy Generator seeded
from --seed and the set and slice names, and every system is scored on the same resamples, so differences such as
model_v2 - keyword and model_v2 - model_v1 are paired (PAIRS). The resamples do not depend on which systems are
scored, so scoring v2 beside v1 leaves every v1 number as it was. Language gaps (ES minus PT) combine the independent
ES and PT resamples.

Outputs in --out:
  intent_classifier.json          every number; no timestamps, so two runs on the same inputs are byte-identical
  intent_classifier.md            the readable tables
  intent_classifier_latency.json  runtime latency per message of each release over the generated test texts; it
                                  depends on the machine, so it is kept out of the two files above
"""
import argparse
import json
import os
import platform
import time
import zlib

import numpy as np
import sklearn

from src.classifier import data, keywords, runtime, train

SEED = 20261005
BOOT = 2000
LEVEL = 0.95
LANGUAGE_GAP_TARGET = 0.05      # report 02, section 9: ES vs PT macro-F1 gap of at most 5 points
DECIMALS = 6
LATENCY_PASSES = 3
LATENCY_WARMUP = 50
MD_TEXT_CHARS = 160
MACRO_F1_MIN_CLASS_ROWS = 5     # below this many rows in a gold class, macro-F1 of the slice is not interpretable
NOT_INTERPRETABLE = "macro-F1 not interpretable, read accuracy"

CLASSES = data.CLASSES
ABSTAIN = data.ABSTAIN          # prediction code of an abstention: it matches no class
DISPUTES = ("dispute_unrecognized_charge", "dispute_incorrect_charge_or_fee")
ATTACK_TYPES = ("prompt_injection", "other_customer_data", "social_engineering")

RELEASES = ("v1", "v2")         # scored side by side, older first; the app loads v2
THRESHOLD = "_threshold"        # suffix of the thresholded system of a model

HOLDOUT_DIR = os.path.join(data.REPO, "eval", "holdout")
INPUTS = {
    "intent_dataset": data.INTENT_DATASET,
    "transfer": data.TRANSFER_DATASET,
    "independent_es": os.path.join(HOLDOUT_DIR, "independent_es.jsonl"),
    "independent_pt": os.path.join(HOLDOUT_DIR, "independent_pt.jsonl"),
    "team": os.path.join(HOLDOUT_DIR, "team_handwritten.jsonl"),
}
KEYWORDS_SOURCE = os.path.join(data.REPO, "src", "classifier", "keywords.py")
# The folders src.classifier.train writes: {release: {"bilingual": dir, "transfer": dir}}.
DEFAULT_MODEL_DIRS = {release: {view: os.path.join(train.MODELS_DIR, spec["out"])
                                for view, spec in train.RELEASES[release].items()} for release in RELEASES}
DEFAULT_OUT = os.path.join(data.REPO, "eval", "results")
JSON_FILE = "intent_classifier.json"
MD_FILE = "intent_classifier.md"
LATENCY_FILE = "intent_classifier_latency.json"

SET_DESCRIPTIONS = {
    "test": "generated test split (split == test) of intent_dataset.jsonl",
    "transfer_pt": "PT test rows of the ES -> PT transfer view; the ES-only models were trained on Spanish rows only, "
                   "but the keyword features of v2 include the glossary's Portuguese terms",
    "independent": "independent free-form holdout, independent_es.jsonl + independent_pt.jsonl",
    "team": "hand-written messages by the team, team_handwritten.jsonl (small set)",
}
SYSTEM_DESCRIPTIONS = {
    "majority": "the most frequent intent of the train split",
    "keyword": "src.classifier.keywords, the glossary router",
    "model_v1": "release v1, bilingual, forced choice (top-1 intent)",
    "model_v1_threshold": "release v1, bilingual, under the policy threshold (abstains below it)",
    "model_v2": "release v2, bilingual, forced choice (top-1 intent)",
    "model_v2_threshold": "release v2, bilingual, under the policy threshold (abstains below it)",
    "model_es_only_v1": "release v1, trained on Spanish rows only (transfer set)",
    "model_es_only_v1_threshold": "release v1, Spanish only, under the policy threshold (transfer set)",
    "model_es_only_v2": "release v2, trained on Spanish rows only (transfer set)",
    "model_es_only_v2_threshold": "release v2, Spanish only, under the policy threshold (transfer set)",
}
_MAIN_PAIRS = (("model_v2", "keyword"), ("model_v2", "model_v1"), ("model_v2", "majority"),
               ("model_v1", "keyword"), ("model_v1", "majority"))
PAIRS = {
    "test": _MAIN_PAIRS,
    "transfer_pt": (("model_es_only_v2", "keyword"), ("model_es_only_v2", "model_es_only_v1"),
                    ("model_es_only_v2", "majority"), ("model_es_only_v1", "keyword"),
                    ("model_es_only_v1", "majority"), ("model_v2", "model_es_only_v2"),
                    ("model_v1", "model_es_only_v1"), ("model_v2", "model_v1")),
    "independent": _MAIN_PAIRS,
    "team": _MAIN_PAIRS,
}
# Read as sentences: "v1 was <v1>.", "v2 was <v2>."; the others start a sentence.
PROTOCOL = {
    "v1": "fitted on the train split, selected and calibrated on the dev split, and scored on the generated test "
          "split and the final sets before v2 existed, in repeated runs of the same frozen model; the macro-F1 "
          "averaging rule was corrected once between those runs, which changed only the mixed and team slices",
    "v1_repeat": "this report scores v1 with the same models, rows, seed and resamples as that run, so its v1 "
                 "numbers repeat that run's, except on the team set, where two labels were corrected afterwards (below)",
    "v2": "designed after the v1 and keyword-router results were known: on the generated test split the router was "
          "ahead of v1, mostly in Portuguese and on out-of-scope rows, and v1 dropped on paraphrase families it had "
          "not seen, which is why v2 adds the router's rules as features; it was then selected only by grouped "
          "cross-validation on train + dev (folds by paraphrase family) and is scored here with frozen weights",
    "final_sets": "the final sets in eval/holdout/ were not used to design or select either model; they are only "
                  "scored",
    "known_results": "the results of v1 and of the keyword router on every set, including v1's error lists, were "
                     "known when v2 was designed, so the v2 comparisons on these sets are a second look at rows whose "
                     "results were known, not a first one",
    "next_check": "no fresh set exists for v2: the team hand-written set has 61 messages by one team member and will "
                  "not grow before the deadline",
    "labels": "two team hand-written labels (hand-es-025, hand-pt-009, a relative's balance) were corrected to "
              "out_of_scope on 2026-10-03 to match the labeling convention, as logged in eval/holdout/README.md; the "
              "inconsistency was flagged when the file was imported, before any model was scored, and this report was "
              "regenerated after the change",
}


def model_key(release, es_only=False):
    """System name of a release's model: model_v2, or model_es_only_v2 for the one trained on Spanish only."""
    return f"model_es_only_{release}" if es_only else f"model_{release}"


# ---------------------------------------------------------------- metric helpers (no I/O)

def encode(labels):
    """Class names to codes in data.CLASSES order; "abstain" becomes ABSTAIN."""
    index = {c: i for i, c in enumerate(CLASSES)}
    index["abstain"] = ABSTAIN
    return np.array([index[label] for label in labels], dtype=np.int64)


def is_correct(pred, accept):
    """A prediction is correct when it is one of the row's acceptable intents; an abstention never is."""
    pred = np.asarray(pred)
    out = np.zeros(len(pred), dtype=bool)
    answered = np.flatnonzero(pred < ABSTAIN)
    out[answered] = accept[answered, pred[answered]]
    return out


def effective_truth(pred, intent, accept):
    """The prediction when it is acceptable, else the row's intent."""
    return np.where(is_correct(pred, accept), pred, np.asarray(intent))


def _total(flags, weights=None):
    flags = np.asarray(flags, dtype=np.float64)
    return flags.sum() if weights is None else weights @ flags


def macro_f1(truth, pred, labels, weights=None):
    """Macro-F1 averaged over the fixed class codes in `labels` (codes; ABSTAIN matches no class). A label with no
    row and no prediction has F1 0, so every resample averages over the same classes.

    With `weights` of shape (B, n), one value per bootstrap resample: row i counts weights[b, i] times.
    """
    truth, pred = np.asarray(truth), np.asarray(pred)
    labels = [int(c) for c in labels]
    if not labels:
        raise ValueError("macro_f1 needs at least one label")
    f1s = []
    for c in labels:
        t, p = truth == c, pred == c
        tp = _total(t & p, weights)
        denom = _total(t, weights) + _total(p, weights)
        f1s.append(np.where(denom > 0, 2.0 * tp / np.where(denom > 0, denom, 1.0), 0.0))
    out = np.mean(np.array(f1s, dtype=np.float64), axis=0)
    return float(out) if weights is None else out


def gold_labels(intent):
    """The fixed macro-F1 label set of a slice: the gold classes (row intents) present in it, as codes."""
    return np.unique(np.asarray(intent)).tolist()


def rate(hit, mask, weights=None):
    """Share of rows in `mask` where `hit` holds; NaN when `mask` selects no row."""
    hit, mask = np.asarray(hit, dtype=bool), np.asarray(mask, dtype=bool)
    num, den = _total(hit & mask, weights), _total(mask, weights)
    out = np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)
    return float(out) if weights is None else out


def brier(proba, intent, accept):
    """Multi-class Brier score against the effective label of the top intent: mean squared distance between the
    probability vector and the one-hot label (0 is perfect, 2 the worst)."""
    truth = effective_truth(proba.argmax(axis=1), intent, accept)
    onehot = np.eye(len(CLASSES))[truth]
    return float(((proba - onehot) ** 2).sum(axis=1).mean())


def top_label_brier(proba, accept):
    """Mean of (confidence of the top intent - 1 if it is acceptable else 0) squared."""
    correct = is_correct(proba.argmax(axis=1), accept)
    return float(((proba.max(axis=1) - correct) ** 2).mean())


def confusion(pred, intent, accept, with_abstain=False):
    """Counts with rows = effective label and columns = prediction (plus an abstain column when asked)."""
    truth = effective_truth(pred, intent, accept)
    matrix = np.zeros((len(CLASSES), len(CLASSES) + (1 if with_abstain else 0)), dtype=np.int64)
    np.add.at(matrix, (truth, np.asarray(pred)), 1)
    return matrix


# ---------------------------------------------------------------- bootstrap

def stream(seed, *names):
    """numpy Generator for one (set, slice): the same seed and names always give the same draws."""
    keys = [zlib.crc32(name.encode("utf-8")) for name in names]
    return np.random.default_rng(np.random.SeedSequence([int(seed)] + keys))


def resample_weights(n, boot, rng):
    """`boot` bootstrap resamples of n rows as a (boot, n) matrix of how often each row was drawn."""
    idx = rng.integers(0, n, size=(boot, n))
    flat = (idx + n * np.arange(boot)[:, None]).ravel()
    return np.bincount(flat, minlength=boot * n).reshape(boot, n).astype(np.float64)


def interval(values, level=LEVEL):
    """Percentile interval of the finite bootstrap values, or None when there are none."""
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return None
    tail = 50.0 * (1.0 - level)
    lo, hi = np.percentile(values, [tail, 100.0 - tail])
    return [float(lo), float(hi)]


def estimate(point, boot_values):
    return {"value": point, "ci": interval(boot_values)}


def paired_difference(point_a, point_b, boot_a, boot_b):
    diff = np.asarray(boot_a) - np.asarray(boot_b)
    return {"value": point_a - point_b, "ci": interval(diff), "share_of_resamples_le_0": float((diff <= 0).mean())}


# ---------------------------------------------------------------- scoring one system on one slice

def row_info(rows):
    return {
        "intent": encode([row["intent"] for row in rows]),
        "accept": train.acceptable_mask(rows),
        "language": np.array([row["language"] for row in rows], dtype=object),
        "variant": np.array([row.get("variant") or "" for row in rows], dtype=object),
        "ambiguous": np.array([bool(row.get("is_ambiguous")) for row in rows], dtype=bool),
        "attack": np.array([row.get("attack_type") or "" for row in rows], dtype=object),
    }


def take(arrays, pos):
    return {k: (None if v is None else v[pos]) for k, v in arrays.items()}


def slice_positions(info):
    """Row positions of each slice: all, es, pt and mixed; empty slices and language slices equal to all are left
    out."""
    n = len(info["intent"])
    out = {"all": np.arange(n)}
    for lang in data.LANGUAGES:
        out[lang] = np.flatnonzero(info["language"] == lang)
    out["mixed"] = np.flatnonzero(info["variant"] == "mixed")
    return {k: v for k, v in out.items() if k == "all" or 0 < len(v) < n}


def _counted(point, boot, n, count=None):
    out = {"n": int(n)}
    if count is not None:
        out["count"] = int(count)
    out.update(estimate(point, boot))
    return out


def score_system(system, info, weights):
    """Every metric of one system on one slice. Returns (metrics, bootstrap macro-F1 values)."""
    intent, accept = info["intent"], info["accept"]
    pred = system["pred"]
    n = len(pred)
    correct = is_correct(pred, accept)
    truth = np.where(correct, pred, intent)
    labels = gold_labels(intent)
    f1_boot = macro_f1(truth, pred, labels, weights)
    everything = np.ones(n, dtype=bool)
    out = {
        "macro_f1": estimate(macro_f1(truth, pred, labels), f1_boot),
        "accuracy": _counted(rate(correct, everything), rate(correct, everything, weights), n, correct.sum()),
    }

    out["recall_by_intent"] = {}
    for c, name in enumerate(CLASSES):
        mask = intent == c
        out["recall_by_intent"][name] = _counted(rate(correct, mask), rate(correct, mask, weights), mask.sum(),
                                                 (correct & mask).sum())
    disputes = np.isin(intent, encode(DISPUTES))
    detected = np.isin(pred, encode(DISPUTES))
    out["dispute_recall"] = _counted(rate(correct, disputes), rate(correct, disputes, weights), disputes.sum(),
                                     (correct & disputes).sum())
    out["dispute_detected"] = _counted(rate(detected, disputes), rate(detected, disputes, weights), disputes.sum(),
                                       (detected & disputes).sum())
    hit, oos = data.out_of_scope_false_accepts(pred, intent, accept)
    out["out_of_scope_false_accept"] = _counted(rate(hit, oos), rate(hit, oos, weights), oos.sum(), hit.sum())

    abstain = system.get("abstain")
    attacks = {}
    kinds = [k for k in ATTACK_TYPES if (info["attack"] == k).any()]
    kinds += sorted({k for k in info["attack"] if k and k not in ATTACK_TYPES})
    for kind in kinds + (["any"] if kinds else []):
        mask = info["attack"] != "" if kind == "any" else info["attack"] == kind
        entry = {"n": int(mask.sum()), "correct": int((correct & mask).sum()), "accuracy": rate(correct, mask)}
        if abstain is not None:
            entry["abstained"] = int((abstain & mask).sum())
            entry["correct_or_abstained"] = rate(correct | abstain, mask)
        attacks[kind] = entry
    out["attacks"] = attacks

    if abstain is not None:
        top_correct = is_correct(system["top"], accept)
        amb = info["ambiguous"]
        groups = (("all", everything), ("ambiguous", amb), ("unambiguous", ~amb),
                  ("top1_wrong", ~top_correct), ("top1_right", top_correct))
        out["abstention"] = {name: _counted(rate(abstain, mask), rate(abstain, mask, weights), mask.sum(),
                                            (abstain & mask).sum()) for name, mask in groups}
        out["clarification_rate_ambiguous"] = out["abstention"]["ambiguous"]["value"]
        out["coverage"] = _counted(rate(~abstain, everything), rate(~abstain, everything, weights), n,
                                   (~abstain).sum())
        out["selective_accuracy"] = _counted(rate(correct, ~abstain), rate(correct, ~abstain, weights),
                                             (~abstain).sum(), (correct & ~abstain).sum())
        answered_truth = np.where(abstain, ABSTAIN, truth)
        answered_labels = gold_labels(intent[~abstain])
        if answered_labels:
            out["selective_macro_f1"] = estimate(macro_f1(answered_truth, pred, answered_labels),
                                                 macro_f1(answered_truth, pred, answered_labels, weights))
        else:
            out["selective_macro_f1"] = estimate(float("nan"), np.full(len(weights), np.nan))

    proba = system.get("proba")
    if proba is not None:
        out["calibration"] = {
            "ece": train.expected_calibration_error(proba, accept),
            "ece_bins": train.ECE_BINS,
            "brier": brier(proba, intent, accept),
            "top_label_brier": top_label_brier(proba, accept),
            "log_loss": train.set_log_loss(proba, accept),
            "mean_confidence": float(proba.max(axis=1).mean()),
        }
    return out, f1_boot


def evaluate_set(name, rows, systems, boot, seed):
    """All slices of one test set. Returns (results, bootstrap macro-F1 values by slice and system)."""
    info = row_info(rows)
    result = {"description": SET_DESCRIPTIONS[name], "n": len(rows), "slices": {}}
    boots = {}
    for slice_name, pos in slice_positions(info).items():
        sub = take(info, pos)
        weights = resample_weights(len(pos), boot, stream(seed, name, slice_name))
        labels = gold_labels(sub["intent"])
        smallest = min(int((sub["intent"] == c).sum()) for c in labels)
        block = {
            "n": len(pos),
            "n_ambiguous": int(sub["ambiguous"].sum()),
            "n_attack": int((sub["attack"] != "").sum()),
            "intent_counts": {c: int((sub["intent"] == i).sum()) for i, c in enumerate(CLASSES)},
            "gold_classes": len(labels),
            "macro_f1_labels": [CLASSES[c] for c in labels],
            "macro_f1_interpretable": smallest >= MACRO_F1_MIN_CLASS_ROWS,
            "systems": {},
            "differences_macro_f1": {},
            "confusion": {"rows": "effective label", "columns": list(CLASSES)},
        }
        boots[slice_name] = {}
        for sys_name, system in systems.items():
            metrics, boots[slice_name][sys_name] = score_system(take(system, pos), sub, weights)
            block["systems"][sys_name] = metrics
        for a, b in PAIRS[name]:
            block["differences_macro_f1"][f"{a} - {b}"] = paired_difference(
                block["systems"][a]["macro_f1"]["value"], block["systems"][b]["macro_f1"]["value"],
                boots[slice_name][a], boots[slice_name][b])
        for sys_name, system in systems.items():
            if system.get("proba") is not None:
                block["confusion"][sys_name] = confusion(system["pred"][pos], sub["intent"], sub["accept"])
            elif system.get("abstain") is not None:
                block["confusion"][f"{sys_name} (last column: abstain)"] = confusion(
                    system["pred"][pos], sub["intent"], sub["accept"], with_abstain=True)
        result["slices"][slice_name] = block
    if "es" in boots and "pt" in boots:
        result["language_gap_macro_f1"] = {sys_name: language_gap(result["slices"], boots, sys_name)
                                           for sys_name in systems}
    return result, boots


def language_gap(slices, boots, sys_name):
    es = slices["es"]["systems"][sys_name]["macro_f1"]["value"]
    pt = slices["pt"]["systems"][sys_name]["macro_f1"]["value"]
    gap = es - pt
    return {"es": es, "pt": pt, "gap_es_minus_pt": gap, "ci": interval(boots["es"][sys_name] - boots["pt"][sys_name]),
            "target_abs_gap": LANGUAGE_GAP_TARGET, "within_target": bool(abs(gap) <= LANGUAGE_GAP_TARGET)}


# ---------------------------------------------------------------- predictions

def model_systems(clf, rows):
    """Forced-choice and thresholded systems of a model. Top intent and below_threshold come from the runtime's own
    batch path (result_from_proba, and the no-evidence answer for messages without evidence), so the threshold is
    applied exactly as in the app; probabilities feed calibration."""
    proba, results = clf.batch([row["text"] for row in rows])
    top = encode([r["intent"] for r in results])
    below = np.array([r["below_threshold"] for r in results], dtype=bool)
    confidence = np.array([r["confidence"] for r in results], dtype=np.float64)
    forced = {"pred": top, "proba": proba, "confidence": confidence}
    thresholded = {"pred": np.where(below, ABSTAIN, top), "top": top, "abstain": below}
    return forced, thresholded, results


def baseline_systems(rows, majority_intent):
    texts = [row["text"] for row in rows]
    return {
        "majority": {"pred": encode([majority_intent] * len(rows))},
        "keyword": {"pred": encode(keywords.predict_many(texts))},
    }


def majority_class(train_rows):
    counts = {c: 0 for c in CLASSES}
    for row in train_rows:
        counts[row["intent"]] += 1
    best = max(CLASSES, key=lambda c: (counts[c], -CLASSES.index(c)))
    return best, counts


# ---------------------------------------------------------------- inputs

REQUIRED_KEYS = ("id", "text", "language", "variant", "intent", "acceptable_intents", "is_ambiguous", "attack_type")


def load_final_rows(path):
    """Rows of a final test file (eval/holdout/ format), with schema checks."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            missing = [k for k in REQUIRED_KEYS if k not in row]
            if missing:
                raise ValueError(f"{data.repo_relative(path)}:{n} lacks {missing}")
            check_labels(row, f"{data.repo_relative(path)}:{n}")
            rows.append(row)
    return rows


def check_labels(row, where):
    acceptable = row.get("acceptable_intents") or [row["intent"]]
    if row["intent"] not in CLASSES or any(a not in CLASSES for a in acceptable) or row["intent"] not in acceptable:
        raise ValueError(f"{where}: bad labels {row['intent']!r} / {acceptable!r}")
    if row["language"] not in data.LANGUAGES:
        raise ValueError(f"{where}: unknown language {row['language']!r}")


def file_entry(path, rows=None):
    entry = {"file": data.repo_relative(path), "sha256": data.file_sha256(path)}
    if rows is not None:
        entry["rows"] = len(rows)
        entry["rows_by_language"] = {lang: sum(r["language"] == lang for r in rows) for lang in data.LANGUAGES
                                     if any(r["language"] == lang for r in rows)}
    return entry


def model_entry(model_dir, clf, release):
    path = os.path.join(model_dir, runtime.MODEL_FILE)
    entry = {"dir": data.repo_relative(model_dir)} if data.inside_repo(model_dir) else {"outside_repo": True}
    entry.update({"model_version": clf.model_version, "view": clf.view, "temperature": clf.temperature,
                  "score_method": clf.score_method, "model_sha256": data.file_sha256(path)})
    card_path = os.path.join(model_dir, runtime.CARD_FILE)
    if os.path.isfile(card_path):
        with open(card_path, encoding="utf-8") as fh:
            card = json.load(fh)
        entry["chosen"] = card.get("chosen", {}).get("name")
        entry["card_sha256"] = data.file_sha256(card_path)
        entry["trained_with_sklearn"] = card.get("sklearn_version")
        entry["sklearn_matches"] = card.get("sklearn_version") == sklearn.__version__
        selection = selection_entry(card)
        if selection is not None:
            entry["selection"] = selection
    entry["release"] = release
    return entry


def selection_entry(card):
    """What the model's own selection measured, from its card, to set beside the final results: macro-F1 on the dev
    split for a model selected there (v1), or the chosen candidate's fold-mean macro-F1 and its out-of-fold macro-F1
    on the dev-split rows for grouped cross-validation (v2). None for a card with neither."""
    def overall(block):
        return ((block or {}).get("calibrated") or {}).get("overall", {}).get("macro_f1")
    if "cv_metrics" in card:
        chosen = (card.get("chosen") or {}).get("name")
        row = next((c for c in card.get("candidates", ()) if c.get("name") == chosen), {})
        return {"rows": "train + dev, grouped cross-validation by paraphrase family (out-of-fold)",
                "macro_f1": row.get("macro_f1"),
                "macro_f1_rows_from_dev": overall(card["cv_metrics"].get("rows_from_dev"))}
    if "dev_metrics" in card:
        return {"rows": "dev split", "macro_f1": overall(card["dev_metrics"])}
    return None


def load_model(model_dir):
    if not os.path.isfile(os.path.join(model_dir, runtime.MODEL_FILE)):
        raise SystemExit(f"no model in {model_dir}; run python -m src.classifier.train first")
    return runtime.IntentClassifier.load(model_dir)


# ---------------------------------------------------------------- latency and the call path

def measure_latency(clf, texts, passes=LATENCY_PASSES, warmup=LATENCY_WARMUP):
    """Per-message wall time of clf(text), the call the app makes. Returns (summary, results of the first pass)."""
    for text in texts[:warmup]:
        clf(text)
    wall, reported, first = [], [], []
    for k in range(passes):
        for text in texts:
            started = time.perf_counter()
            res = clf(text)
            wall.append((time.perf_counter() - started) * 1000.0)
            reported.append(res["latency_ms"])
            if k == 0:
                first.append(res)
    wall = np.array(wall)
    summary = {
        "model_version": clf.model_version,
        "texts": "generated test split",
        "n_texts": len(texts),
        "passes": passes,
        "calls": len(wall),
        "wall_ms": {"median": float(np.median(wall)), "p95": float(np.percentile(wall, 95)),
                    "max": float(wall.max())},
        "reported_latency_ms": {"median": float(np.median(reported)), "p95": float(np.percentile(reported, 95))},
        "machine": {"platform": platform.platform(), "processor": platform.processor(),
                    "python": platform.python_version(), "sklearn": sklearn.__version__},
        "note": "depends on the machine and its load; kept apart so the main results stay byte-identical",
    }
    return summary, first


def call_agreement(batch_results, call_results):
    """Rows where the per-message call and the batch path disagree on intent, confidence or below_threshold."""
    keys = ("intent", "confidence", "below_threshold")
    return sum(any(a[k] != b[k] for k in keys) for a, b in zip(batch_results, call_results))


# ---------------------------------------------------------------- errors list

def error_rows(rows, forced, thresholded, keyword_pred):
    """Rows where the model's top intent is not acceptable, for reporting only."""
    accept = train.acceptable_mask(rows)
    correct = is_correct(forced["pred"], accept)
    out = []
    for i in np.flatnonzero(~correct):
        row = rows[i]
        out.append({
            "id": row["id"], "language": row["language"], "variant": row.get("variant"),
            "attack_type": row.get("attack_type"), "text": row["text"], "gold": row["intent"],
            "acceptable": list(row.get("acceptable_intents") or [row["intent"]]),
            "predicted": CLASSES[forced["pred"][i]], "confidence": float(forced["confidence"][i]),
            "below_threshold": bool(thresholded["abstain"][i]), "keyword": CLASSES[keyword_pred[i]],
        })
    return sorted(out, key=lambda e: e["id"])


# ---------------------------------------------------------------- the run

def model_dirs_with_defaults(model_dirs=None):
    """{release: {"bilingual": dir, "transfer": dir}}: the given folders over DEFAULT_MODEL_DIRS."""
    given = model_dirs or {}
    unknown = set(given) - set(RELEASES)
    if unknown:
        raise ValueError(f"unknown release(s): {sorted(unknown)}")
    return {release: {**DEFAULT_MODEL_DIRS[release], **given.get(release, {})} for release in RELEASES}


def evaluate(model_dirs=None, boot=BOOT, seed=SEED, paths=None, latency=True):
    """Scores every system on every test set. `model_dirs` overrides DEFAULT_MODEL_DIRS, as {release: {"bilingual":
    dir, "transfer": dir}}. Returns (report, latency summaries by model or None)."""
    if boot < 1:
        raise ValueError("boot must be at least 1")
    paths = {**INPUTS, **(paths or {})}
    dirs = model_dirs_with_defaults(model_dirs)
    models = {release: {view: load_model(path) for view, path in views.items()} for release, views in dirs.items()}
    threshold = data.min_intent_confidence()

    train_rows = data.load_intent_rows(("train",), paths["intent_dataset"])
    transfer_train = data.load_transfer_rows(("train",), paths["transfer"])
    majority, majority_counts = majority_class(train_rows)
    majority_t, majority_t_counts = majority_class(transfer_train)

    test_rows = data.load_intent_rows(("test",), paths["intent_dataset"])
    transfer_rows = data.load_transfer_rows(("test",), paths["transfer"])
    for i, row in enumerate(test_rows + transfer_rows):
        check_labels(row, f"generated row {row.get('id', i)}")
    if any(row["language"] != "pt" for row in transfer_rows):
        raise ValueError("the transfer view's test split should hold PT rows only")
    independent_es = load_final_rows(paths["independent_es"])
    independent_pt = load_final_rows(paths["independent_pt"])
    independent_rows = independent_es + independent_pt
    team_rows = load_final_rows(paths["team"]) if os.path.isfile(paths["team"]) else []

    model_entries = {}
    for release in RELEASES:
        model_entries[model_key(release)] = model_entry(dirs[release]["bilingual"], models[release]["bilingual"],
                                                        release)
        model_entries[model_key(release, es_only=True)] = model_entry(dirs[release]["transfer"],
                                                                      models[release]["transfer"], release)
    report = {
        "evaluation": "intake intent classifier, final evaluation of releases v1 and v2 side by side (report 02, "
                      "sections 9 and 10)",
        "protocol": PROTOCOL,
        "systems": SYSTEM_DESCRIPTIONS,
        "scoring": {
            "correct": "the prediction is one of acceptable_intents",
            "macro_f1": "F1 against the effective label (prediction if acceptable, else intent), averaged over the "
                        "gold classes present in the full slice; every bootstrap resample uses the same classes, and "
                        "a class with no row and no prediction in a resample counts as F1 0",
            "macro_f1_interpretable": f"false when a gold class of the slice has fewer than "
                                      f"{MACRO_F1_MIN_CLASS_ROWS} rows: read accuracy there",
            "selective_macro_f1": "macro-F1 over the rows the thresholded model answers, averaged over the gold "
                                  "classes of those rows in the full slice (fixed across resamples)",
            "abstention": f"a *{THRESHOLD} system abstains when the rounded confidence is below {threshold} "
                          "(handoff.min_intent_confidence); an abstention matches no class, so it is a miss in "
                          "macro-F1 and accuracy",
            "recall_by_intent": "rows grouped by their intent; share whose prediction is acceptable",
            "dispute_recall": "rows whose intent is one of the two dispute classes; share predicted acceptably",
            "dispute_detected": "the same rows; share predicted as either dispute class",
            "out_of_scope_false_accept": data.OUT_OF_SCOPE_FALSE_ACCEPT_RULE,
            "calibration": "ECE on the top probability (correct = top intent acceptable), multi-class Brier against "
                           "the effective label, log-loss of the probability mass on the acceptable intents",
            "interval": f"{int(LEVEL * 100)}% percentile bootstrap over rows; paired across systems within a slice",
        },
        "settings": {"boot": boot, "seed": seed, "level": LEVEL, "language_gap_target": LANGUAGE_GAP_TARGET},
        "policy": {**file_entry(data.POLICY_PATH), "min_intent_confidence": threshold},
        "models": model_entries,
        "keyword_router": {"file": data.repo_relative(KEYWORDS_SOURCE), "sha256": data.file_sha256(KEYWORDS_SOURCE),
                           "default_intent": keywords.DEFAULT_INTENT},
        "majority": {
            "test": {"intent": majority, "from": "train split of intent_dataset.jsonl", "train_counts": majority_counts},
            "transfer_pt": {"intent": majority_t, "from": "train split of the transfer view (ES only)",
                            "train_counts": majority_t_counts},
        },
        "data": {
            "intent_dataset": {**file_entry(paths["intent_dataset"]), "train_rows_read": len(train_rows),
                               "test_rows": len(test_rows)},
            "transfer": {**file_entry(paths["transfer"]), "train_rows_read": len(transfer_train),
                         "test_rows": len(transfer_rows)},
            "independent_es": file_entry(paths["independent_es"], independent_es),
            "independent_pt": file_entry(paths["independent_pt"], independent_pt),
            "team": file_entry(paths["team"], team_rows) if os.path.isfile(paths["team"]) else {"missing": True},
        },
        "environment": {"python": platform.python_version(), "numpy": np.__version__,
                        "sklearn": sklearn.__version__},
        "sets": {},
    }

    boots, keep = {}, {}
    for name, rows in (("test", test_rows), ("independent", independent_rows), ("team", team_rows)):
        if not rows:
            report["sets"][name] = {"description": SET_DESCRIPTIONS[name], "n": 0, "missing": True}
            continue
        systems, results = baseline_systems(rows, majority), {}
        for release in RELEASES:
            key = model_key(release)
            systems[key], systems[key + THRESHOLD], results[key] = model_systems(models[release]["bilingual"], rows)
        report["sets"][name], boots[name] = evaluate_set(name, rows, systems, boot, seed)
        keep[name] = (rows, systems, results)

    systems_t = baseline_systems(transfer_rows, majority_t)
    for es_only, view in ((True, "transfer"), (False, "bilingual")):
        for release in RELEASES:
            key = model_key(release, es_only)
            systems_t[key], systems_t[key + THRESHOLD], _ = model_systems(models[release][view], transfer_rows)
    report["sets"]["transfer_pt"], boots["transfer_pt"] = evaluate_set("transfer_pt", transfer_rows, systems_t,
                                                                         boot, seed)
    report["transfer"] = transfer_summary(report, boots, models, test_rows, transfer_rows, boot, seed)
    report["sets"] = {k: report["sets"][k] for k in ("test", "transfer_pt", "independent", "team")}

    report["errors"] = {}
    for name in ("independent", "team"):
        if name in keep:
            rows, systems, _ = keep[name]
            report["errors"][name] = {model_key(release): error_rows(
                rows, systems[model_key(release)], systems[model_key(release) + THRESHOLD], systems["keyword"]["pred"])
                for release in RELEASES}

    latency_summary = None
    if latency and "test" in keep:
        rows, _, results = keep["test"]
        texts = [row["text"] for row in rows]
        latency_summary, disagreements = {}, {}
        for release in RELEASES:
            key = model_key(release)
            latency_summary[key], call_results = measure_latency(models[release]["bilingual"], texts)
            disagreements[key] = call_agreement(results[key], call_results)
        report["sets"]["test"]["call_vs_batch_disagreements"] = disagreements
    report["latency"] = {"file": LATENCY_FILE, "measured": bool(latency),
                         "texts": "generated test split, one clf(text) call per message, for each bilingual model"}
    return report, latency_summary


def transfer_summary(report, boots, models, test_rows, transfer_rows, boot, seed):
    """ES -> PT transfer, for each release: the ES-only model on the generated ES test rows and on the PT transfer
    rows, the bilingual model's ES vs PT gap on the generated test split, and the paired bilingual minus ES-only
    differences. Then the paired v2 minus v1 differences of the ES-only models."""
    es_rows = [row for row in test_rows if row["language"] == "es"]
    test_pt_ids = [row["id"] for row in test_rows if row["language"] == "pt"]
    out = {"target_abs_gap": LANGUAGE_GAP_TARGET,
           "transfer_rows_equal_test_pt_rows": test_pt_ids == [row["id"] for row in transfer_rows]}
    pt_block = report["sets"]["transfer_pt"]["slices"]["all"]
    test_gap = report["sets"].get("test", {}).get("language_gap_macro_f1", {})
    weights = resample_weights(len(es_rows), boot, stream(seed, "test", "es")) if es_rows else None  # test/es draws
    es_scores = {}
    for release in RELEASES:
        es_key, bi_key = model_key(release, es_only=True), model_key(release)
        pt_es_only = pt_block["systems"][es_key]
        entry = {"pt_test": {"n": pt_block["n"], "macro_f1": pt_es_only["macro_f1"],
                             "accuracy": pt_es_only["accuracy"]}}
        summary = {"model_es_only": entry,
                   "model_minus_model_es_only_on_pt_test": pt_block["differences_macro_f1"][f"{bi_key} - {es_key}"]}
        if es_rows:
            forced, _, _ = model_systems(models[release]["transfer"], es_rows)
            metrics, es_boot = score_system(forced, row_info(es_rows), weights)
            es_scores[release] = (metrics["macro_f1"]["value"], es_boot)
            entry["es_test"] = {"n": len(es_rows), "macro_f1": metrics["macro_f1"], "accuracy": metrics["accuracy"]}
            gap = metrics["macro_f1"]["value"] - pt_es_only["macro_f1"]["value"]
            entry["gap_es_minus_pt"] = {"value": gap,
                                        "ci": interval(es_boot - boots["transfer_pt"]["all"][es_key]),
                                        "within_target": bool(abs(gap) <= LANGUAGE_GAP_TARGET)}
            if "es" in boots.get("test", {}):
                es_model = report["sets"]["test"]["slices"]["es"]["systems"][bi_key]["macro_f1"]["value"]
                summary["model_minus_model_es_only_on_es_test"] = paired_difference(
                    es_model, metrics["macro_f1"]["value"], boots["test"]["es"][bi_key], es_boot)
        if bi_key in test_gap:
            summary["model_gap_on_test"] = test_gap[bi_key]
        out[release] = summary
    old, new = RELEASES
    pair = f"{model_key(new, es_only=True)} - {model_key(old, es_only=True)}"
    out[f"{new}_minus_{old}"] = {"model_es_only_on_pt_test": pt_block["differences_macro_f1"][pair]}
    if es_rows:
        (new_value, new_boot), (old_value, old_boot) = es_scores[new], es_scores[old]
        out[f"{new}_minus_{old}"]["model_es_only_on_es_test"] = paired_difference(new_value, old_value, new_boot,
                                                                                  old_boot)
    return out


# ---------------------------------------------------------------- writing

def clean(obj):
    """JSON-ready copy: numpy types to Python, floats rounded to DECIMALS, NaN and inf to None."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return clean(obj.tolist())
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        value = float(obj)
        return round(value, DECIMALS) + 0.0 if np.isfinite(value) else None
    return obj


def write_json(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _f(value, digits=3):
    return "n/a" if value is None else f"{value:.{digits}f}"


def _est(entry, digits=3):
    if entry is None or entry.get("value") is None:
        return "n/a"
    ci = entry.get("ci")
    text = _f(entry["value"], digits)
    return text if not ci else f"{text} [{_f(ci[0], digits)}, {_f(ci[1], digits)}]"


def _signed(entry):
    if entry is None or entry.get("value") is None:
        return "n/a"
    ci = entry.get("ci") or [None, None]
    return f"{entry['value']:+.3f} [{ci[0]:+.3f}, {ci[1]:+.3f}]" if ci[0] is not None else f"{entry['value']:+.3f}"


def _share(entry):
    """value [ci] (count/n)"""
    if entry is None:
        return "n/a"
    if not entry.get("n"):
        return "n/a (n=0)"
    return f"{_est(entry)} ({entry.get('count', '?')}/{entry['n']})"


def _table(head, rows):
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return lines


def _md_text(text):
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= MD_TEXT_CHARS else text[:MD_TEXT_CHARS - 3] + "..."


def _gold(error):
    others = [a for a in error["acceptable"] if a != error["gold"]]
    attack = f"; attack: {error['attack_type']}" if error.get("attack_type") else ""
    return error["gold"] + (f" (also {', '.join(others)})" if others else "") + attack


def _slices(report, set_name):
    s = report["sets"].get(set_name, {})
    return [] if s.get("missing") else list(s["slices"].items())


def _smallest_class(block):
    return min(block["intent_counts"][c] for c in block["macro_f1_labels"])


def _f1_note(block):
    return "" if block["macro_f1_interpretable"] else NOT_INTERPRETABLE


def _f1_suffix(block):
    return "" if block["macro_f1_interpretable"] else f" ({NOT_INTERPRETABLE})"


def _label(key):
    """Readable name of a system: model_v2 -> Model v2, model_es_only_v1_threshold -> ES-only v1 + threshold."""
    base = key[:-len(THRESHOLD)] if key.endswith(THRESHOLD) else key
    if base.startswith("model_es_only_"):
        name = "ES-only " + base[len("model_es_only_"):]
    elif base.startswith("model_"):
        name = "Model " + base[len("model_"):]
    else:
        name = base.capitalize()
    return name + (" + threshold" if key.endswith(THRESHOLD) else "")


def _diff_label(pair):
    """model_v2 - model_v1 -> v2 - v1; model_es_only_v2 - keyword -> ES-only v2 - keyword."""
    return " - ".join(_label(k).replace("Model ", "") if k.startswith("model_") else k for k in pair.split(" - "))


def _sentence(text):
    return text[0].upper() + text[1:] + "."


def _protocol(report):
    """The "Protocol and history" section: PROTOCOL with the numbers that motivated and selected each release."""
    p, models = report["protocol"], report["models"]
    old, new = RELEASES
    out = ["## Protocol and history", ""]
    for release in RELEASES:
        bi, es = models[model_key(release)], models[model_key(release, es_only=True)]
        name = f"**{release}** (`{bi['model_version']}`, ES-only `{es['model_version']}`) was "
        selection = bi.get("selection") or {}
        test = report["sets"].get("test", {})
        if release == old:
            line = name + p[old] + "."
            if selection.get("macro_f1") is not None and not test.get("missing"):
                line += (f" Its macro-F1 was {_f(selection['macro_f1'])} on the dev split it was selected on, and "
                         f"{_est(test['slices']['all']['systems'][model_key(old)]['macro_f1'])} on the generated "
                         "test split.")
            line += " " + _sentence(p["v1_repeat"])
        else:
            line = name + p[new] + "."
            if selection.get("macro_f1") is not None:
                line += f" Its grouped cross-validation macro-F1 was {_f(selection['macro_f1'])} (mean over folds)"
                if selection.get("macro_f1_rows_from_dev") is not None:
                    line += (f", and {_f(selection['macro_f1_rows_from_dev'])} on the out-of-fold rows from the dev "
                             "split alone (the keyword glossary was written while reading train-split messages)")
                line += "."
        out.append("- " + line)
    out += ["- " + _sentence(p["final_sets"]) + " " + _sentence(p["known_results"]),
            "- " + _sentence(p["next_check"]), "- " + _sentence(p["labels"]), ""]
    return out


def _glance(report):
    """The main numbers of slice all of each set, keyword router and both bilingual models side by side."""
    old, new = (model_key(release) for release in RELEASES)
    out = ["## At a glance (slice all)", "",
           "Point values with 95% intervals (and counts where they help); every slice is in the sections below. "
           "Abstention under the policy threshold and ECE apply to the models only.", ""]
    rows = []
    for name in ("test", "independent", "team"):
        if not _slices(report, name):
            continue
        block = report["sets"][name]["slices"]["all"]
        sy, n = block["systems"], block["n"]
        rows.append([name, n, "macro-F1"] + [_est(sy[k]["macro_f1"]) for k in ("keyword", old, new)]
                    + [_signed(block["differences_macro_f1"][f"{new} - {old}"])])
        rows.append([name, n, "accuracy"] + [_est(sy[k]["accuracy"]) for k in ("keyword", old, new)] + [""])
        rows.append([name, n, "dispute recall"] + [_share(sy[k]["dispute_recall"]) for k in ("keyword", old, new)]
                    + [""])
        rows.append([name, n, "out-of-scope false accept"]
                    + [_share(sy[k]["out_of_scope_false_accept"]) for k in ("keyword", old, new)] + [""])
        rows.append([name, n, "abstention under the threshold", ""]
                    + [_share(sy[k + THRESHOLD]["abstention"]["all"]) for k in (old, new)] + [""])
        rows.append([name, n, "ECE", ""] + [_f(sy[k]["calibration"]["ece"]) for k in (old, new)] + [""])
    out += _table(["Set", "n", "Metric", "Keyword", _label(old), _label(new), _diff_label(f"{new} - {old}")],
                  rows) + [""]
    return out


def render_markdown(report):
    """The readable tables, built from the cleaned report so both files show the same numbers."""
    models = report["models"]
    st = report["settings"]
    old, new = (model_key(release) for release in RELEASES)
    bilingual = [old, new]
    with_threshold = [k for key in bilingual for k in (key, key + THRESHOLD)]
    out = ["# Intake intent classifier: final evaluation", ""]
    out += _protocol(report)
    out += _glance(report)
    out += ["## Setup", "",
            f"Written by `python -m src.classifier.evaluate` (boot {st['boot']}, seed {st['seed']}). Every number, "
            f"with its interval and count, is in `{JSON_FILE}`. Latency is in `{LATENCY_FILE}`, apart from this file "
            "because it depends on the machine.", ""]
    for key, m in models.items():
        where = f"`{m['dir']}`" if "dir" in m else "a folder outside the repo"
        out.append(f"- {_label(key)}: `{m['model_version']}` ({m.get('chosen')}, temperature {m['temperature']}), "
                   f"{where}, model.joblib sha256 `{m['model_sha256'][:12]}`.")
    out += [f"- Policy threshold (handoff.min_intent_confidence): {report['policy']['min_intent_confidence']}.",
            f"- Majority baseline: `{report['majority']['test']['intent']}` (train split); on the transfer set "
            f"`{report['majority']['transfer_pt']['intent']}` (ES train split).",
            "- Scoring: a prediction is correct when it is in `acceptable_intents`; macro-F1 uses the effective label "
            "and averages over the gold classes of the full slice, the same classes in every bootstrap resample. "
            f"Systems marked `+ threshold` (`*{THRESHOLD}` in the JSON) abstain (ask a clarifying question) below "
            "the threshold, and an abstention counts as a miss in macro-F1 and accuracy.",
            "- Intervals: 95% percentile bootstrap over rows, paired across systems within a slice: every system, "
            "v1 and v2 alike, is scored on the same resamples.", ""]
    out += ["**About the test sets.** The generated test split holds paraphrase families never seen in training. "
            "The independent holdout was produced by an automated authoring process separate from the scenario "
            "generator, but 13 generator templates were rewritten with 14 of its rows in view "
            "(docs/03_test_scenarios.md, section 5), and its rows sit slightly closer to the training texts than the "
            "test split does, so its numbers may be about 1.5 points optimistic for both the keyword router and the "
            "models. team_handwritten is the only set written by a person; it is small (61 messages) and comes from "
            "one team member.", ""]
    out += ["| Input | Rows | sha256 |", "|---|---|---|"]
    for key, entry in report["data"].items():
        if entry.get("missing"):
            out.append(f"| {key} | missing | |")
            continue
        rows = entry.get("rows", entry.get("test_rows"))
        label = f"test {entry['test_rows']}, train read {entry['train_rows_read']}" if "test_rows" in entry else rows
        out.append(f"| `{entry['file']}` | {label} | `{entry['sha256'][:12]}` |")
    out.append("")

    main_sets = [s for s in ("test", "independent", "team") if _slices(report, s)]
    diffs = [f"{new} - keyword", f"{new} - {old}", f"{old} - keyword"]
    out += ["## Macro-F1", ""]
    rows = []
    for name in main_sets:
        for sl, block in _slices(report, name):
            sy = block["systems"]
            rows.append([name, sl, block["n"], _est(sy["majority"]["macro_f1"]), _est(sy["keyword"]["macro_f1"])]
                        + [_est(sy[key]["macro_f1"]) for key in bilingual]
                        + [_signed(block["differences_macro_f1"][d]) for d in diffs] + [_f1_note(block)])
    out += _table(["Set", "Slice", "n", "Majority", "Keyword"] + [_label(k) for k in bilingual]
                  + [_diff_label(d) for d in diffs] + ["Note"], rows) + [""]
    out += [f"Differences are paired (same resamples); `{_diff_label(diffs[1])}` is the change from release "
            f"{RELEASES[0]} to release {RELEASES[1]}. Every difference, including those against the majority "
            f"baseline, is in `{JSON_FILE}`.", ""]
    flagged = [f"{name} {sl} (smallest gold class: {_smallest_class(block)} row"
               f"{'s' if _smallest_class(block) > 1 else ''})"
               for name in main_sets + ["transfer_pt"] for sl, block in _slices(report, name)
               if not block["macro_f1_interpretable"]]
    out += [f"Macro-F1 averages over the gold classes of each slice. Where a gold class has fewer than "
            f"{MACRO_F1_MIN_CLASS_ROWS} rows, one row moves that class's F1 a lot and many resamples miss the class, "
            f"so the slice is marked \"{NOT_INTERPRETABLE}\"" + (": " + ", ".join(flagged) if flagged else
                                                                 "; no slice is marked") + ".", ""]
    few = [f"{name} {sl} ({block['gold_classes']} gold class{'es' if block['gold_classes'] > 1 else ''})"
           for name in main_sets + ["transfer_pt"] for sl, block in _slices(report, name) if block["gold_classes"] < 3]
    if few:
        out += ["Slices with fewer than 3 gold classes: " + ", ".join(few) + ". Macro-F1 there averages over those "
                "classes only, and an answer that is an acceptable alternative outside them earns no credit, so read "
                "accuracy instead.", ""]
    if "team" in main_sets:
        out += [f"The team set is small (n={report['sets']['team']['n']}); read its intervals, not its points.", ""]

    out += ["## Accuracy", "", "With the threshold, an abstention counts as a miss.", ""]
    rows = []
    for name in main_sets:
        for sl, block in _slices(report, name):
            sy = block["systems"]
            rows.append([name, sl, block["n"], _est(sy["majority"]["accuracy"]), _est(sy["keyword"]["accuracy"])]
                        + [_est(sy[key]["accuracy"]) for key in with_threshold])
    out += _table(["Set", "Slice", "n", "Majority", "Keyword"] + [_label(k) for k in with_threshold], rows) + [""]

    out += ["## Policy threshold", "",
            "Abstention means the agent asks a clarifying question. On ambiguous rows that is the wanted behavior "
            "(clarification rate, higher is better). `top1 wrong` is the share of the model's errors the threshold "
            "catches; `top1 right` the share of correct answers it holds back.", ""]
    rows = []
    for name in main_sets + ["transfer_pt"]:
        for sl, block in _slices(report, name):
            for key, t in block["systems"].items():
                if not key.endswith(THRESHOLD):
                    continue
                ab = t["abstention"]
                rows.append([name, sl, key, block["n"], _share(ab["all"]), _share(ab["ambiguous"]),
                             _share(ab["unambiguous"]), _share(ab["top1_wrong"]), _share(ab["top1_right"]),
                             _share(t["selective_accuracy"])])
    out += _table(["Set", "Slice", "System", "n", "Abstain (all)", "Abstain ambiguous", "Abstain unambiguous",
                   "Abstain, top1 wrong", "Abstain, top1 right", "Selective accuracy"], rows) + [""]

    out += ["## Out-of-scope false accepts", "",
            "Rows whose intent is out_of_scope that a system accepts as another, non-acceptable class (lower is "
            "better). For a thresholded model an abstention is not an acceptance.", ""]
    keys = ["keyword"] + with_threshold
    rows = []
    for name in main_sets:
        for sl, block in _slices(report, name):
            sy = block["systems"]
            rows.append([name, sl] + [_share(sy[key]["out_of_scope_false_accept"]) for key in keys])
    out += _table(["Set", "Slice"] + [_label(k) for k in keys], rows) + [""]

    out += ["## Dispute recall", "",
            "Rows whose intent is one of the two dispute classes: share predicted acceptably (pooled), and per class.",
            ""]
    rows = []
    for name in main_sets:
        for sl, block in _slices(report, name):
            sy = block["systems"]
            for key in ["keyword"] + bilingual:
                r = sy[key]["recall_by_intent"]
                rows.append([name, sl, key, _share(sy[key]["dispute_recall"]), _share(r[DISPUTES[0]]),
                             _share(r[DISPUTES[1]]), _share(sy[key]["dispute_detected"])])
    out += _table(["Set", "Slice", "System", "Both disputes", "Unrecognized", "Incorrect / fee",
                   "Detected as any dispute"], rows) + [""]

    out += ["## Recall by intent (slice all)", ""]
    for name in main_sets + ["transfer_pt"]:
        block = report["sets"][name]["slices"]["all"]
        keys = [k for k in block["systems"] if k != "majority"]
        out += [f"**{name}** (n={block['n']})", ""]
        rows = [[c, block["intent_counts"][c]] + [_f(block["systems"][k]["recall_by_intent"][c]["value"]) for k in keys]
                for c in CLASSES]
        out += _table(["Intent", "n"] + keys, rows) + [""]

    out += ["## Attacks", "",
            "Accuracy on rows with an attack_type (the intent is what the customer literally asks for, usually "
            "out_of_scope), with the number correct. `correct or abstained` counts a clarifying question as safe.", ""]
    rows = []
    for name in main_sets:
        sy = report["sets"][name]["slices"]["all"]["systems"]
        for kind, k in sy["keyword"]["attacks"].items():
            row = [name, kind, k["n"], f"{_f(k['accuracy'])} ({k['correct']})"]
            for key in bilingual:
                mo, a = sy[key]["attacks"][kind], sy[key + THRESHOLD]["attacks"][kind]
                row += [f"{_f(mo['accuracy'])} ({mo['correct']})",
                        f"{_f(a['accuracy'])} ({a['correct']}), abstained {a['abstained']}",
                        _f(a["correct_or_abstained"])]
            rows.append(row)
    head = ["Set", "Attack type", "n", "Keyword"]
    for key in bilingual:
        head += [_label(key), _label(key + THRESHOLD), f"{_label(key)}: correct or abstained"]
    out += _table(head, rows) + [""]

    out += ["## Calibration", "", f"ECE with {train.ECE_BINS} bins on the top probability; Brier is multi-class "
            "against the effective label; log-loss is on the probability mass of the acceptable intents.", ""]
    rows = []
    for name in main_sets + ["transfer_pt"]:
        for sl, block in _slices(report, name):
            for key, sysm in block["systems"].items():
                cal = sysm.get("calibration")
                if cal:
                    rows.append([name, sl, key, block["n"], _f(cal["ece"]), _f(cal["brier"]),
                                 _f(cal["top_label_brier"]), _f(cal["log_loss"]), _f(cal["mean_confidence"]),
                                 _f(sysm["accuracy"]["value"])])
    out += _table(["Set", "Slice", "System", "n", "ECE", "Brier", "Top-label Brier", "Log-loss", "Mean confidence",
                   "Accuracy"], rows) + [""]

    tr = report["transfer"]
    tp = report["sets"]["transfer_pt"]
    out += ["## Language transfer", "",
            f"PT test rows of the transfer view (n={tp['n']}). Same rows as the PT slice of the generated test split: "
            f"{tr['transfer_rows_equal_test_pt_rows']}. The ES-only models were trained on Spanish rows only. ES-only "
            f"{RELEASES[0]} never saw Portuguese. ES-only {RELEASES[1]} uses the keyword features, and the rules in "
            "keywords.py list Portuguese terms too (the glossary covers both languages, and its coverage was checked "
            "on the bilingual train split): its PT numbers measure Spanish training plus a bilingual glossary, not "
            "transfer alone.", ""]
    rows = []
    for sl, block in _slices(report, "transfer_pt"):
        for key, sysm in block["systems"].items():
            rows.append([sl, key, block["n"], _est(sysm["macro_f1"]) + _f1_suffix(block), _est(sysm["accuracy"]),
                         _share(sysm["out_of_scope_false_accept"])])
    out += _table(["Slice", "System", "n", "Macro-F1", "Accuracy", "OOS false accept"], rows) + [""]
    gap_rows = []
    for release in RELEASES:
        eo = tr[release]["model_es_only"]
        if "es_test" in eo:
            gap_rows.append([_label(model_key(release, es_only=True)), _est(eo["es_test"]["macro_f1"]),
                             _est(eo["pt_test"]["macro_f1"]), _signed(eo["gap_es_minus_pt"]),
                             eo["gap_es_minus_pt"]["within_target"]])
    for name in ("test", "independent", "team"):
        gaps = report["sets"].get(name, {}).get("language_gap_macro_f1", {})
        for key in ["keyword"] + bilingual:
            if key in gaps:
                g = gaps[key]
                sl = report["sets"][name]["slices"]
                gap_rows.append([f"{key} on {name}", _f(g["es"]) + _f1_suffix(sl["es"]),
                                 _f(g["pt"]) + _f1_suffix(sl["pt"]),
                                 _signed({"value": g["gap_es_minus_pt"], "ci": g["ci"]}), g["within_target"]])
    out += [f"ES minus PT macro-F1 (target: absolute gap at most {tr['target_abs_gap']}). The ES-only rows use the "
            "generated ES test rows and the PT transfer rows.", ""]
    out += _table(["System", "ES", "PT", "Gap ES - PT", "Within target"], gap_rows) + [""]
    for release in RELEASES:
        t = tr[release]
        line = f"- {release}, bilingual minus ES-only: on PT test {_signed(t['model_minus_model_es_only_on_pt_test'])}"
        if "model_minus_model_es_only_on_es_test" in t:
            line += f"; on ES test {_signed(t['model_minus_model_es_only_on_es_test'])}"
        out.append(line + ".")
    change = tr.get(f"{RELEASES[1]}_minus_{RELEASES[0]}", {})
    if change:
        line = (f"- ES-only {RELEASES[1]} minus ES-only {RELEASES[0]}: on PT test "
                f"{_signed(change['model_es_only_on_pt_test'])}")
        if "model_es_only_on_es_test" in change:
            line += f"; on ES test {_signed(change['model_es_only_on_es_test'])}"
        out.append(line + ".")
    out += [""]

    out += ["## Confusion matrices (slice all)", "", "Rows are the effective label, columns the prediction.", ""]
    short = {c: c.replace("dispute_", "").replace("_charge_or_fee", "").replace("_charge", "")
             .replace("account_payment_", "").replace("card_lost_or_block", "card").replace("other_", "")
             .replace("out_of_scope", "oos") for c in CLASSES}
    for name in main_sets + ["transfer_pt"]:
        conf = report["sets"][name]["slices"]["all"]["confusion"]
        for key, matrix in conf.items():
            if key in ("rows", "columns") or "abstain" in key:
                continue
            out += [f"**{name}, {key}**", ""]
            out += _table(["true \\ pred"] + [short[c] for c in CLASSES],
                          [[short[c]] + row for c, row in zip(CLASSES, matrix)]) + [""]

    for name in ("independent", "team"):
        errors = report["errors"].get(name)
        if errors is None:
            continue
        by_id = {}
        for key in bilingual:
            for e in errors[key]:
                by_id.setdefault(e["id"], {})[key] = e
        both = sum(len(found) == len(bilingual) for found in by_id.values())
        counts = ", ".join(f"{_label(key)} wrong on {len(errors[key])}" for key in bilingual)
        out += [f"## Model errors: {name} ({counts}, both on {both}; of {report['sets'][name]['n']} rows)", "",
                "Rows where the top intent of either model is not acceptable; `correct` marks the model that got the "
                "row right. For reporting only: these rows are never used to change a model, the keyword rules or the "
                "threshold.", ""]
        rows = []
        for row_id in sorted(by_id):
            found = by_id[row_id]
            first = next(iter(found.values()))
            cells = []
            for key in bilingual:
                e = found.get(key)
                below = ", below thr." if e is not None and e["below_threshold"] else ""
                cells.append("correct" if e is None else f"{e['predicted']} ({_f(e['confidence'])}{below})")
            rows.append([row_id, _md_text(first["text"]), _gold(first)] + cells + [first["keyword"]])
        out += _table(["id", "Text", "Gold"] + [f"{_label(key)} (conf.)" for key in bilingual] + ["Keyword"],
                      rows) + [""]
    return "\n".join(out).rstrip() + "\n"


def run(model_dirs=None, boot=BOOT, seed=SEED, out_dir=DEFAULT_OUT, paths=None, latency=True, quiet=False):
    report, latency_summary = evaluate(model_dirs, boot, seed, paths, latency)
    report = clean(report)
    os.makedirs(out_dir, exist_ok=True)
    write_json(os.path.join(out_dir, JSON_FILE), report)
    with open(os.path.join(out_dir, MD_FILE), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_markdown(report))
    if latency_summary is not None:
        write_json(os.path.join(out_dir, LATENCY_FILE), clean(latency_summary))
    if not quiet:
        print_summary(report, clean(latency_summary) if latency_summary else None, out_dir)
    return report


def print_summary(report, latency_summary, out_dir):
    old, new = (model_key(release) for release in RELEASES)
    print("  ".join(f"{key}={m['model_version']}" for key, m in report["models"].items())
          + f"  threshold={report['policy']['min_intent_confidence']}  boot={report['settings']['boot']}  "
            f"seed={report['settings']['seed']}")
    print(f"{'set':<12}{'slice':<7}{'n':>6}  {'majority':<24}{'keyword':<24}{old:<24}{new:<24}"
          f"{new + '-keyword':<24}{new + '-' + old:<24}")
    for name in ("test", "independent", "team"):
        for sl, block in _slices(report, name):
            sy, d = block["systems"], block["differences_macro_f1"]
            print(f"{name:<12}{sl:<7}{block['n']:>6}  {_est(sy['majority']['macro_f1']):<24}"
                  f"{_est(sy['keyword']['macro_f1']):<24}{_est(sy[old]['macro_f1']):<24}{_est(sy[new]['macro_f1']):<24}"
                  f"{_signed(d[f'{new} - keyword']):<24}{_signed(d[f'{new} - {old}']):<24}")
    print(f"{'set':<12}{'slice':<7}{'abstain ' + old:>18}{'abstain ' + new:>18}{'FA keyword':>12}"
          + "".join(f"{'FA ' + k:>24}" for k in (old, old + THRESHOLD, new, new + THRESHOLD)))
    for name in ("test", "independent", "team"):
        for sl, block in _slices(report, name):
            sy = block["systems"]
            print(f"{name:<12}{sl:<7}{_f(sy[old + THRESHOLD]['abstention']['all']['value']):>18}"
                  f"{_f(sy[new + THRESHOLD]['abstention']['all']['value']):>18}"
                  f"{_f(sy['keyword']['out_of_scope_false_accept']['value']):>12}"
                  + "".join(f"{_f(sy[k]['out_of_scope_false_accept']['value']):>24}"
                            for k in (old, old + THRESHOLD, new, new + THRESHOLD)))
    tr = report["transfer"]
    for release in RELEASES:
        eo = tr[release]["model_es_only"]
        print(f"transfer {release}: ES-only on PT {_est(eo['pt_test']['macro_f1'])}"
              + (f", on ES {_est(eo['es_test']['macro_f1'])}, gap {_signed(eo['gap_es_minus_pt'])}" if "es_test" in eo
                 else "")
              + f"; bilingual - ES-only on PT {_signed(tr[release]['model_minus_model_es_only_on_pt_test'])}")
        if "model_gap_on_test" in tr[release]:
            g = tr[release]["model_gap_on_test"]
            print(f"  bilingual {release} ES - PT macro-F1 gap on test: "
                  f"{_signed({'value': g['gap_es_minus_pt'], 'ci': g['ci']})}, within the {g['target_abs_gap']} "
                  f"target: {g['within_target']}")
    if latency_summary:
        disagreements = report["sets"]["test"].get("call_vs_batch_disagreements", {})
        for key, summary in latency_summary.items():
            w = summary["wall_ms"]
            print(f"latency per message, {key}: median {w['median']:.3f} ms  p95 {w['p95']:.3f} ms  "
                  f"(n={summary['calls']}); call vs batch disagreements: {disagreements.get(key)}")
    print(f"wrote {os.path.join(out_dir, JSON_FILE)}, {MD_FILE}" + (f" and {LATENCY_FILE}" if latency_summary else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for release in RELEASES:
        ap.add_argument(f"--{release}-dir", default=DEFAULT_MODEL_DIRS[release]["bilingual"],
                        help=f"bilingual model of release {release} (default %(default)s)")
        ap.add_argument(f"--{release}-es-only-dir", default=DEFAULT_MODEL_DIRS[release]["transfer"],
                        help=f"Spanish-only model of release {release} (default %(default)s)")
    ap.add_argument("--boot", type=int, default=BOOT, help="bootstrap resamples (default %(default)s)")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--out", default=DEFAULT_OUT, help="output folder (default eval/results)")
    args = ap.parse_args(argv)
    model_dirs = {release: {"bilingual": getattr(args, f"{release}_dir"),
                            "transfer": getattr(args, f"{release}_es_only_dir")} for release in RELEASES}
    run(model_dirs, args.boot, args.seed, args.out)


if __name__ == "__main__":
    main()
