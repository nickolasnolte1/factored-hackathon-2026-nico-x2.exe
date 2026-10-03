"""Runtime adapter for the intake intent classifier: loads the artifact written by src.classifier.train and scores
one customer message at a time.

    from src.classifier.runtime import IntentClassifier, load_default
    clf = load_default()                 # None, with a logged warning, when the model is missing or unusable
    clf("me cobraron dos veces")
    # {"intent": "dispute_incorrect_charge_or_fee", "confidence": 0.981,
    #  "top": [{"intent": "dispute_incorrect_charge_or_fee", "p": 0.981}, {"intent": "account_payment_inquiry",
    #          "p": 0.01}, {"intent": "other_complaint", "p": 0.004}],
    #  "below_threshold": False, "threshold": 0.55, "model_version": "intent-clf-2.0.0", "latency_ms": 1}

    python -m src.classifier.runtime "me cobraron dos veces"        # quick check from the shell

`confidence` is the calibrated probability of the top intent (softmax of the model scores divided by the fitted
temperature), rounded to 3 decimals; `top` holds the three most likely intents. `below_threshold` compares the
rounded confidence with handoff.min_intent_confidence from the dispute policy; the policy then asks one clarifying
question and hands off with reason low_intent_confidence if confidence stays low.

Two artifact formats load:
  1  release v1: a scikit-learn Pipeline on normalized text (data.normalize_text).
  2  release v2: the TF-IDF features and the model kept apart, so that keyword features can join them. A hybrid
     model appends keyword_features() times keyword_weight to the TF-IDF features; a late-fusion model mixes the
     calibrated probabilities with the keyword router's one-hot: (1 - keyword_mix) * p + keyword_mix * one-hot.
     Training calls the same keyword_features, design_matrix and fuse functions, on data.keyword_text of each message.
A model that uses keyword features stores KEYWORD_RULES_ID, and loading refuses it when the rules in keywords.py have
changed since training, as it does for the normalization id.

Calls never raise. Odd input (whitespace, very long, non-string) is normalized like training text. A message with
no evidence gets the no-evidence answer: uniform probabilities, out_of_scope listed first, confidence 0.167 and
below_threshold True, so the agent asks what the customer needs. No evidence means nothing is left after
normalization, or no TF-IDF feature fires and no keyword rule group fires (emoji only, scripts the model never saw,
control or zero-width characters). The keyword router's fallback answer is not evidence. The batch path used by the
evaluation (proba, batch) applies the same rule, so both agree. A scoring failure also gets the no-evidence answer
and is logged once per process with its traceback (never the message).

Loading is strict: IntentClassifier.load raises on a bad artifact and scores a fixed message before returning, so
a model that cannot score fails at load time instead of on every call. A scikit-learn version other than the one
the artifact was trained with is logged as a warning. load_default() never raises: it returns None and logs a
warning when the model is missing or cannot be loaded, so an app can call it at import time.
The loaded model is read-only, so one instance can serve several threads.
"""
import argparse
import hashlib
import json
import logging
import os
import threading
import time

import joblib
import numpy as np
import sklearn
from scipy import sparse

from src.classifier import data, keywords

ARTIFACT_FORMAT_V1 = 1
ARTIFACT_FORMAT = 2
DEFAULT_DIR = os.path.join(data.REPO, "models", "intent_classifier")
MODEL_FILE = "model.joblib"
CARD_FILE = "model_card.json"
TOP_K = 3
FALLBACK_INTENT = "out_of_scope"
LOAD_CHECK_TEXT = "no reconozco un cargo de 120 en mi tarjeta / não reconheço uma compra no meu cartão"

# Keyword features: one column per rule group of keywords.py (resolution order), then the router's answer as a
# one-hot in data.CLASSES order.
KEYWORD_GROUPS = tuple(group for group, _ in keywords.RESOLUTION_ORDER)
KEYWORD_COLUMNS = KEYWORD_GROUPS + tuple(f"router={c}" for c in data.CLASSES)
_GROUP_INDEX = {group: i for i, group in enumerate(KEYWORD_GROUPS)}


def keyword_rules_id():
    """sha256 of the keyword rules: intents, default intent, abbreviations, glossary and resolution order."""
    rules = {"intents": keywords.INTENTS, "default_intent": keywords.DEFAULT_INTENT,
             "abbreviations": keywords.ABBREVIATIONS, "glossary": keywords.GLOSSARY,
             "resolution_order": keywords.RESOLUTION_ORDER}
    return hashlib.sha256(json.dumps(rules, sort_keys=True, ensure_ascii=True).encode("ascii")).hexdigest()


KEYWORD_RULES_ID = keyword_rules_id()

LOG = logging.getLogger(__name__)
_failure_lock = threading.Lock()
_failure_logged = False


# ---------------------------------------------------------------- shared with training

def model_scores(model, score_method, x):
    """Scores of a fitted model on a feature matrix, columns in data.CLASSES order."""
    if score_method == "decision_function":
        scores = model.decision_function(x)
    elif score_method == "predict_log_proba":
        scores = model.predict_log_proba(x)
    else:
        raise ValueError(f"unknown score method: {score_method}")
    scores = np.asarray(scores, dtype=np.float64)
    order = [list(model.classes_).index(c) for c in data.CLASSES]
    return scores[:, order]


def features_and_scores(pipeline, score_method, texts):
    """Feature matrix and model scores of a format-1 pipeline for already normalized texts, score columns in
    data.CLASSES order. The steps are the ones Pipeline.decision_function / predict_log_proba run, so the scores are
    the same."""
    x = texts
    for _, step in pipeline.steps[:-1]:
        if step is not None and step != "passthrough":
            x = step.transform(x)
    return x, model_scores(pipeline.steps[-1][1], score_method, x)


def raw_scores(pipeline, score_method, texts):
    """Model scores of a format-1 pipeline for already normalized texts, columns in data.CLASSES order."""
    return features_and_scores(pipeline, score_method, texts)[1]


def softmax(scores, temperature=1.0):
    z = np.asarray(scores, dtype=np.float64) / float(temperature)
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def keyword_features(texts):
    """Keyword features of texts already passed through data.keyword_text: a 0/1 sparse matrix with the columns of
    KEYWORD_COLUMNS, the rule groups that fired (keywords.matches) and the router's answer (keywords.predict)."""
    out = np.zeros((len(texts), len(KEYWORD_COLUMNS)))
    for i, text in enumerate(texts):
        for group in keywords.matches(text):
            out[i, _GROUP_INDEX[group]] = 1.0
        out[i, len(KEYWORD_GROUPS) + data.CLASSES.index(keywords.predict(text))] = 1.0
    return sparse.csr_matrix(out)


def keyword_groups_fired(x_keywords):
    """True for the rows where at least one rule group fired (the router's one-hot is left out)."""
    return np.asarray(x_keywords[:, :len(KEYWORD_GROUPS)].sum(axis=1)).ravel() > 0


def design_matrix(x_text, x_keywords, weight):
    """Model input: the TF-IDF features, followed by the keyword features times weight when weight is not 0."""
    if not weight:
        return x_text
    return sparse.hstack([sparse.csr_matrix(x_text), x_keywords * float(weight)], format="csr")


def fuse(p, x_keywords, mix):
    """Late fusion: (1 - mix) * p + mix * the keyword router's one-hot; p unchanged when mix is 0."""
    if not mix:
        return p
    onehot = x_keywords[:, len(KEYWORD_GROUPS):].toarray()
    return (1.0 - float(mix)) * p + float(mix) * onehot


def no_feature_rows(x):
    """True for the rows of a feature matrix where no feature fires (all zero)."""
    if sparse.issparse(x):
        return np.asarray(abs(x).sum(axis=1)).ravel() == 0
    return ~np.any(np.asarray(x) != 0, axis=1)


def _log_scoring_failure():
    """Logs the exception being handled, once per process."""
    global _failure_logged
    with _failure_lock:
        if _failure_logged:
            return
        _failure_logged = True
    LOG.exception("intent scoring failed; answering with the no-evidence result (logged once per process)")


# ---------------------------------------------------------------- the classifier

class IntentClassifier:
    def __init__(self, artifact, threshold=None):
        fmt = artifact.get("format")
        if fmt not in (ARTIFACT_FORMAT_V1, ARTIFACT_FORMAT):
            raise ValueError(f"unsupported artifact format: {fmt}")
        if artifact.get("normalization_id") != data.NORMALIZATION_ID:
            raise ValueError(f"artifact was trained with normalization {artifact.get('normalization_id')!r}, "
                             f"this code uses {data.NORMALIZATION_ID!r}: retrain with src.classifier.train")
        if tuple(artifact.get("classes", ())) != data.CLASSES:
            raise ValueError("artifact classes differ from data.CLASSES")
        if fmt == ARTIFACT_FORMAT_V1:
            self.pipeline = artifact["pipeline"]
            self.text_steps = [step for _, step in self.pipeline.steps[:-1]
                               if step is not None and step != "passthrough"]
            self.model = self.pipeline.steps[-1][1]
            self.keyword_weight = self.keyword_mix = 0.0
        else:
            self.pipeline = None
            self.text_steps = [artifact["text_features"]]
            self.model = artifact["model"]
            self.keyword_weight = float(artifact["keyword_weight"])
            self.keyword_mix = float(artifact["keyword_mix"])
        self.uses_keywords = bool(self.keyword_weight or self.keyword_mix)
        if self.uses_keywords and artifact.get("keyword_rules") != KEYWORD_RULES_ID:
            raise ValueError("the keyword rules in src/classifier/keywords.py changed since this model was trained: "
                             "retrain with src.classifier.train")
        self.score_method = artifact["score_method"]
        self.temperature = float(artifact["temperature"])
        self.model_version = artifact["model_version"]
        self.view = artifact.get("view")
        self.sklearn_version = artifact.get("sklearn_version")
        self.threshold = data.min_intent_confidence() if threshold is None else float(threshold)
        if self.sklearn_version is None:
            LOG.warning("model %s does not record its scikit-learn version; installed %s", self.model_version,
                        sklearn.__version__)
        elif self.sklearn_version != sklearn.__version__:
            LOG.warning("model %s was trained with scikit-learn %s, installed %s; retrain with "
                        "src.classifier.train if scores look wrong", self.model_version, self.sklearn_version,
                        sklearn.__version__)
        self._check_scoring()

    def _check_scoring(self):
        """Scores LOAD_CHECK_TEXT outside the never-raise wrapper and raises when that fails."""
        p, _ = self._score([LOAD_CHECK_TEXT])
        if p.shape != (1, len(data.CLASSES)) or not np.all(np.isfinite(p)):
            raise ValueError(f"model {self.model_version} failed the load check on a fixed message")

    @classmethod
    def load(cls, path=DEFAULT_DIR, threshold=None):
        """Load from a model directory (or a model.joblib file). Raises when the artifact is missing, unreadable,
        incompatible or cannot score."""
        if os.path.isdir(path):
            path = os.path.join(path, MODEL_FILE)
        return cls(joblib.load(path), threshold=threshold)

    def _score(self, texts):
        """(probabilities, evidence) for raw texts, the one scoring path of calls and batches. Probabilities of rows
        without evidence are returned as computed; raises on any failure."""
        normalized = [data.normalize_text(t) for t in texts]
        x = normalized
        for step in self.text_steps:
            x = step.transform(x)
        evidence = np.array([bool(t) for t in normalized], dtype=bool)
        found = ~no_feature_rows(x)
        x_keywords = None
        if self.uses_keywords:
            x_keywords = keyword_features([data.keyword_text(t) for t in texts])
            found |= keyword_groups_fired(x_keywords)
            x = design_matrix(x, x_keywords, self.keyword_weight)
        p = softmax(model_scores(self.model, self.score_method, x), self.temperature)
        return fuse(p, x_keywords, self.keyword_mix), evidence & found

    def evidence_proba(self, texts):
        """(probabilities, no_evidence) for a list of raw texts: probabilities of shape (n, 6), columns in
        data.CLASSES order, and a boolean mask of the rows without evidence, whose probabilities are uniform."""
        p, evidence = self._score(list(texts))
        no_evidence = ~evidence | ~np.all(np.isfinite(p), axis=1)
        p[no_evidence] = 1.0 / len(data.CLASSES)
        return p, no_evidence

    def proba(self, texts):
        """Calibrated probabilities for a list of texts, shape (n, 6), columns in data.CLASSES order; rows without
        evidence are uniform, as in a call. Unlike a call, a scoring failure raises."""
        return self.evidence_proba(list(texts))[0]

    def batch(self, texts):
        """(probabilities, response dicts) for a list of texts: the answers a call gives, from one batch of scores
        (latency_ms 0). Used by the evaluation; a scoring failure raises."""
        p, no_evidence = self.evidence_proba(list(texts))
        results = [self.no_evidence_result() if none else self.result_from_proba(row)
                   for row, none in zip(p, no_evidence)]
        return p, results

    def result_from_proba(self, p, latency_ms=0):
        """Response dict for one row of probabilities (data.CLASSES order)."""
        p = np.asarray(p, dtype=np.float64)
        order = sorted(range(len(data.CLASSES)), key=lambda i: (-p[i], i))
        confidence = round(float(p[order[0]]), 3)
        return {
            "intent": data.CLASSES[order[0]],
            "confidence": confidence,
            "top": [{"intent": data.CLASSES[i], "p": round(float(p[i]), 3)} for i in order[:TOP_K]],
            "below_threshold": confidence < self.threshold,
            "threshold": self.threshold,
            "model_version": self.model_version,
            "latency_ms": int(latency_ms),
        }

    def no_evidence_result(self, latency_ms=0):
        """Uniform probabilities with out_of_scope first: always below the threshold."""
        uniform = round(1.0 / len(data.CLASSES), 3)
        order = [FALLBACK_INTENT] + [c for c in data.CLASSES if c != FALLBACK_INTENT]
        return {
            "intent": FALLBACK_INTENT,
            "confidence": uniform,
            "top": [{"intent": c, "p": uniform} for c in order[:TOP_K]],
            "below_threshold": True,
            "threshold": self.threshold,
            "model_version": self.model_version,
            "latency_ms": int(latency_ms),
        }

    def __call__(self, text):
        started = time.perf_counter()
        try:
            if data.normalize_text(text):
                p, evidence = self._score([text])
                if evidence[0]:
                    if not np.all(np.isfinite(p[0])):
                        raise FloatingPointError("non-finite intent probabilities")
                    return self.result_from_proba(p[0], round((time.perf_counter() - started) * 1000))
        except Exception:  # noqa: BLE001 - the caller must always get an answer
            _log_scoring_failure()
        return self.no_evidence_result(round((time.perf_counter() - started) * 1000))


def load_default(path=DEFAULT_DIR):
    """The bilingual model from models/intent_classifier/, or None with a logged warning when it is missing or
    cannot be loaded (unreadable, incompatible, failing the load check). Never raises."""
    model_path = os.path.join(path, MODEL_FILE)
    if not os.path.isfile(model_path):
        LOG.warning("no intent model at %s; run python -m src.classifier.train", model_path)
        return None
    try:
        return IntentClassifier.load(path)
    except Exception:  # noqa: BLE001 - an app may call this at import time
        LOG.warning("could not load the intent model at %s; retrain with python -m src.classifier.train",
                    model_path, exc_info=True)
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="+")
    ap.add_argument("--model-dir", default=DEFAULT_DIR)
    args = ap.parse_args(argv)
    clf = load_default(args.model_dir)
    if clf is None:
        raise SystemExit(f"no usable model in {args.model_dir}; run python -m src.classifier.train first")
    for text in args.text:
        print(json.dumps(clf(text), ensure_ascii=False))


if __name__ == "__main__":
    main()
