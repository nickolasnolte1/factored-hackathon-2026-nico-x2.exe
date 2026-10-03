"""Shared data helpers for the intake intent classifier: class list, dataset loaders, text normalization, the
acceptable-intents scoring rule, the out-of-scope false-accept rule and the policy confidence threshold.

    from src.classifier import data
    train = data.load_intent_rows(("train",))
    data.normalize_text("Me cobraron $1.250,00 el 12/03")   # -> 'me cobraron $0.0,0 el 0/0'

Inputs: data/scenarios/intent_dataset.jsonl and intent_lang_transfer_es_to_pt.jsonl (src/scenarios/generate.py),
        src/policy/dispute_policy.json.

The loaders filter by split while reading, so callers only receive the splits they ask for. Training code asks for
train and dev only; the generated test split is left to the evaluation code.

Two views of a message feed the models: normalize_text for the TF-IDF features and keyword_text for the keyword
features of release v2. Training and the runtime call the same two functions.
"""
import hashlib
import json
import os
import re
import unicodedata

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(REPO, "data", "scenarios")
INTENT_DATASET = os.path.join(DATA_DIR, "intent_dataset.jsonl")
TRANSFER_DATASET = os.path.join(DATA_DIR, "intent_lang_transfer_es_to_pt.jsonl")
POLICY_PATH = os.path.join(REPO, "src", "policy", "dispute_policy.json")

CLASSES = (
    "dispute_unrecognized_charge",
    "dispute_incorrect_charge_or_fee",
    "account_payment_inquiry",
    "card_lost_or_block",
    "other_complaint",
    "out_of_scope",
)
LANGUAGES = ("es", "pt")
SPLITS = ("train", "dev", "test")
OUT_OF_SCOPE = "out_of_scope"
ABSTAIN = len(CLASSES)          # prediction code of an abstention (a clarifying question): it matches no class
OUT_OF_SCOPE_FALSE_ACCEPT_RULE = ("rows whose intent is out_of_scope; share predicted as a class that is not "
                                  "acceptable for the row (abstentions excluded from the numerator)")

# Normalization shared by training and runtime. Changing it means retraining: the id is stored in the artifact and
# checked when the model is loaded.
NORMALIZATION_ID = "nfkc-lower-digits0-ws-v1"
MAX_CHARS = 2000
_DIGITS = re.compile(r"\d+")
_SPACES = re.compile(r"\s+")


def as_text(text):
    """The message as a string: None becomes "", bytes are decoded as UTF-8 and any other non-string goes through
    str()."""
    if text is None:
        return ""
    if isinstance(text, bytes):
        return text.decode("utf-8", errors="replace")
    if not isinstance(text, str):
        return str(text)
    return text


def normalize_text(text):
    """NFKC, lowercase, every digit run replaced by "0", whitespace collapsed, cut to MAX_CHARS characters.

    Input goes through as_text first. Raw input is cut to 4 * MAX_CHARS characters so that a huge message cannot slow
    the runtime down; training texts are far shorter.
    """
    text = unicodedata.normalize("NFKC", as_text(text)[:4 * MAX_CHARS]).lower()
    text = _DIGITS.sub("0", text)
    text = _SPACES.sub(" ", text).strip()
    return text[:MAX_CHARS]


def keyword_text(text):
    """Input of the keyword features (release v2): the message through as_text, cut to MAX_CHARS characters. It is
    not normalized here: keywords.normalize does its own, and some rules need the digits ("2 veces")."""
    return as_text(text)[:MAX_CHARS]


def _read_rows(path, splits):
    wanted = set(splits)
    unknown = wanted - set(SPLITS)
    if unknown:
        raise ValueError(f"unknown split(s): {sorted(unknown)}")
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("split") in wanted:
                rows.append(row)
    return rows


def load_intent_rows(splits, path=INTENT_DATASET):
    """Rows of the bilingual intent dataset whose split is in `splits` (file order)."""
    return _read_rows(path, splits)


def load_transfer_rows(splits, path=TRANSFER_DATASET):
    """Rows of the ES -> PT language-transfer view (ES train and dev, PT test) whose split is in `splits`."""
    return _read_rows(path, splits)


def effective_label(pred, row):
    """Acceptable-intents rule: the prediction counts as the true label when it is one of the row's acceptable
    intents, otherwise the true label is the row's intent."""
    acceptable = row.get("acceptable_intents") or [row["intent"]]
    return pred if pred in acceptable else row["intent"]


def out_of_scope_false_accepts(pred, intent, accept):
    """Out-of-scope false accepts, the one definition behind the dev numbers in the model card and the evaluation.

    Arguments are codes in CLASSES order: `pred` the predictions (ABSTAIN for an abstention), `intent` the rows'
    intents and `accept` the (n, 6) boolean matrix of their acceptable intents. Returns (hit, mask): mask marks rows
    whose intent is out_of_scope; hit marks those predicted as a class that is not acceptable for the row. An
    abstention is not an acceptance, so it never counts as a hit.
    """
    pred, accept = np.asarray(pred), np.asarray(accept, dtype=bool)
    mask = np.asarray(intent) == CLASSES.index(OUT_OF_SCOPE)
    hit = np.zeros(len(pred), dtype=bool)
    rows = np.flatnonzero(mask & (pred < ABSTAIN))
    hit[rows] = ~accept[rows, pred[rows]]
    return hit, mask


def min_intent_confidence(path=POLICY_PATH):
    """handoff.min_intent_confidence from the dispute policy."""
    with open(path, encoding="utf-8") as fh:
        value = json.load(fh)["handoff"]["min_intent_confidence"]
    value = float(value)
    if not 0.0 < value < 1.0:
        raise ValueError(f"handoff.min_intent_confidence must be in (0, 1), got {value}")
    return value


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def inside_repo(path):
    """True when `path` is the repo root or lies below it."""
    try:
        rel = os.path.relpath(os.path.abspath(path), REPO)
    except ValueError:          # another drive on Windows
        return False
    return rel != os.pardir and not rel.startswith(os.pardir + os.sep)


def repo_relative(path):
    """Path relative to the repo root with forward slashes, or the file name when it is outside the repo."""
    path = os.path.abspath(path)
    if not inside_repo(path):
        return os.path.basename(path)
    return os.path.relpath(path, REPO).replace(os.sep, "/")
