"""Training checks on toy rows written by the test: both releases (v1 dev selection, v2 grouped cross-validation),
determinism, the model cards, the release flag, keyword features, the split guard, the scoring rule, calibration
helpers, and static guards that keep the final test sets and their results out of the classifier code."""
import ast
import glob
import io
import json
import os
import re
import tokenize

import joblib
import numpy as np
import pytest
import sklearn

from src.classifier import data, keywords, runtime, train
from tests.classifier import toy_intents

CLASSIFIER_DIR = os.path.join(data.REPO, "src", "classifier")
GUARDED_FILES = ("data.py", "train.py", "runtime.py", "keywords.py")
# The final test sets (also as os.path.join("eval", "holdout")) and the evaluation results.
HOLDOUT = r"eval[\\/]+holdout|[\"']eval[\"']\s*,\s*[\"']holdout[\"']|independent_es|independent_pt|team_handwritten"
RESULTS = r"eval[\\/]+results|[\"']eval[\"']\s*,\s*[\"']results[\"']"
FORBIDDEN = re.compile(f"{HOLDOUT}|{RESULTS}", re.IGNORECASE)
HOLDOUT_READERS = ("src/classifier/evaluate.py", "src/scenarios/validate.py")


def _split(rows, split):
    return [r for r in rows if r["split"] == split]


# ---------------------------------------------------------------- static guards

def test_classifier_code_never_references_the_final_test_sets():
    checked = 0
    for name in GUARDED_FILES:
        path = os.path.join(CLASSIFIER_DIR, name)
        if not os.path.exists(path):
            assert name == "keywords.py", f"missing {name}"
            continue
        with open(path, encoding="utf-8") as fh:
            hits = FORBIDDEN.findall(fh.read())
        assert not hits, f"{name} references {sorted(set(hits))}"
        checked += 1
    assert checked >= 3


def _code_without_comments(path):
    """The tokens of a Python file except comments (strings and docstrings stay), joined by spaces."""
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        return " ".join(tok.string for tok in tokens if tok.type != tokenize.COMMENT)
    except (tokenize.TokenError, SyntaxError):
        return source


def test_only_the_final_evaluation_and_the_overlap_check_reference_the_final_test_sets():
    """Python sources under src/ that name eval/holdout or a final test file. Comments are ignored: the Databricks
    notebooks in src/analysis describe the holdout in markdown cells (comment lines) and read nothing."""
    pattern = re.compile(HOLDOUT, re.IGNORECASE)
    paths = sorted(glob.glob(os.path.join(data.REPO, "src", "**", "*.py"), recursive=True))
    assert len(paths) > 20
    hits = sorted(data.repo_relative(p) for p in paths if pattern.search(_code_without_comments(p)))
    assert hits == sorted(HOLDOUT_READERS)


def test_train_source_only_requests_train_and_dev():
    with open(os.path.join(CLASSIFIER_DIR, "train.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr in ("load_intent_rows", "load_transfer_rows")]
    assert len(calls) == 4
    for call in calls:
        splits = ast.literal_eval(call.args[0])
        assert set(splits) <= {"train", "dev"}, ast.dump(call)


# ---------------------------------------------------------------- the CLI path on toy files

@pytest.fixture()
def toy_bilingual(tmp_path):
    return toy_intents.write_jsonl(tmp_path / "intent_dataset.jsonl", toy_intents.rows(test_languages=("es", "pt")))


@pytest.fixture()
def toy_transfer(tmp_path):
    return toy_intents.write_jsonl(tmp_path / "transfer.jsonl", toy_intents.rows(languages=("es",), test_languages=("pt",)))


def _spy(monkeypatch, name, calls):
    original = getattr(data, name)

    def spy(splits, path):
        calls.append(tuple(splits))
        rows = original(splits, path)
        assert all(row["split"] != "test" for row in rows)
        return rows
    monkeypatch.setattr(data, name, spy)


@pytest.mark.parametrize("release", sorted(train.RELEASES))
def test_run_never_loads_the_test_split(tmp_path, monkeypatch, toy_bilingual, toy_transfer, release):
    calls = []
    _spy(monkeypatch, "load_intent_rows", calls)
    _spy(monkeypatch, "load_transfer_rows", calls)
    card = train.run("bilingual", str(tmp_path / "bi"), dataset_path=toy_bilingual, quiet=True, release=release)
    card_t = train.run("transfer", str(tmp_path / "tr"), dataset_path=toy_transfer, quiet=True, release=release)
    assert calls and all(set(c) <= {"train", "dev"} for c in calls)
    assert set(card["data"]["rows"]) == {"train", "dev"}
    assert card_t["data"]["rows"] == {"train": {"es": 48}, "dev": {"es": 13}}     # no PT row is read


@pytest.mark.parametrize("fit", [train.fit_and_calibrate, train.fit_cv], ids=["v1", "v2"])
def test_fit_refuses_test_rows(fit):
    rows = toy_intents.rows(test_languages=("es",))
    with pytest.raises(ValueError):
        fit(_split(rows, "train") + _split(rows, "test"), _split(rows, "dev"))
    with pytest.raises(ValueError):
        fit(_split(rows, "train"), _split(rows, "dev") + _split(rows, "test"))


@pytest.mark.parametrize("fit", [train.fit_and_calibrate, train.fit_cv], ids=["v1", "v2"])
def test_fit_needs_every_class(fit):
    rows = toy_intents.rows()
    train_rows = [r for r in _split(rows, "train") if r["intent"] != "other_complaint"]
    with pytest.raises(ValueError):
        fit(train_rows, [r for r in _split(rows, "dev") if r["intent"] != "other_complaint"])


@pytest.mark.parametrize("release", sorted(train.RELEASES))
def test_two_runs_write_identical_files(tmp_path, toy_bilingual, release):
    for name in ("a", "b"):
        train.run("bilingual", str(tmp_path / name), seed=11, dataset_path=toy_bilingual, quiet=True, release=release)
    for file in (runtime.CARD_FILE, runtime.MODEL_FILE):
        assert (tmp_path / "a" / file).read_bytes() == (tmp_path / "b" / file).read_bytes(), file
    clf_a = runtime.IntentClassifier.load(str(tmp_path / "a"))
    clf_b = runtime.IntentClassifier.load(str(tmp_path / "b"))
    texts = [r["text"] for r in toy_intents.rows()]
    np.testing.assert_array_equal(clf_a.proba(texts), clf_b.proba(texts))


def test_release_flag_and_output_folders(tmp_path, monkeypatch, toy_bilingual, toy_transfer):
    monkeypatch.setattr(data, "INTENT_DATASET", toy_bilingual)
    monkeypatch.setattr(data, "TRANSFER_DATASET", toy_transfer)
    models = tmp_path / "models"
    monkeypatch.setattr(train, "MODELS_DIR", str(models))
    expected = {(): ("intent_classifier", "intent-clf-2.0.0", 2),
                ("--view", "transfer"): ("intent_classifier_es_only", "intent-clf-es-only-2.0.0", 2),
                ("--release", "v1"): ("intent_classifier_v1", "intent-clf-1.0.0", 1),
                ("--release", "v1", "--view", "transfer"): ("intent_classifier_es_only_v1", "intent-clf-es-only-1.0.0",
                                                            1)}
    for argv, (folder, version, fmt) in expected.items():
        train.main(list(argv))
        artifact = joblib.load(models / folder / runtime.MODEL_FILE)
        assert (artifact["model_version"], artifact["format"]) == (version, fmt), argv
        assert runtime.IntentClassifier.load(str(models / folder)).model_version == version
    assert sorted(p.name for p in models.iterdir()) == sorted(f for f, _, _ in expected.values())
    # the app and the evaluation read the default release's folders
    assert train.DEFAULT_RELEASE == "v2" and train.VIEWS is train.RELEASES["v2"]
    assert runtime.DEFAULT_DIR == os.path.join(data.REPO, "models", train.VIEWS["bilingual"]["out"])
    with pytest.raises(SystemExit):
        train.main(["--release", "v3"])


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def test_v1_model_card_contents(tmp_path, toy_bilingual):
    card = train.run("bilingual", str(tmp_path / "m"), dataset_path=toy_bilingual, quiet=True, release="v1")
    on_disk = json.loads((tmp_path / "m" / runtime.CARD_FILE).read_text(encoding="utf-8"))
    assert on_disk == card
    assert card["model_version"] == train.RELEASES["v1"]["bilingual"]["model_version"] == "intent-clf-1.0.0"
    assert "release" not in card            # the v1 card keeps its original layout, byte for byte
    assert card["view"] == "bilingual" and card["classes"] == list(data.CLASSES)
    assert card["chosen"]["name"] in {c["name"] for c in train.V1_CANDIDATES}
    assert [c["name"] for c in card["candidates"]] == [c["name"] for c in train.V1_CANDIDATES]
    for row in card["candidates"]:
        assert {"macro_f1", "macro_f1_es", "macro_f1_pt", "log_loss", "temperature"} <= set(row)
    assert card["temperature"] > 0
    assert card["policy"]["min_intent_confidence"] == data.min_intent_confidence()
    assert card["sklearn_version"]
    assert card["data"]["sha256"] == data.file_sha256(toy_bilingual)
    assert card["data"]["rows"] == {"train": {"es": 48, "pt": 48}, "dev": {"es": 13, "pt": 13}}
    dev = card["dev_metrics"]
    assert set(dev["calibrated"]) == {"overall", "es", "pt"}
    assert set(dev["calibration"]) == {"before", "after"}
    assert dev["calibration"]["after"]["log_loss"] <= dev["calibration"]["before"]["log_loss"] + 1e-9
    th = dev["policy_threshold"]
    assert {"out_of_scope_rows", "out_of_scope_false_accepts", "out_of_scope_false_accept_rate"} <= set(th)
    assert th["out_of_scope_rows"] == 4            # one dev row per out_of_scope template and language
    assert card["procedure"]["out_of_scope_false_accept"].startswith(data.OUT_OF_SCOPE_FALSE_ACCEPT_RULE)
    assert not [k for k in _keys(card) if re.search(r"time|^date$|_date|created|_at$", k, re.IGNORECASE)]
    artifact = runtime.IntentClassifier.load(str(tmp_path / "m"))
    assert artifact.temperature == card["temperature"] and artifact.model_version == card["model_version"]
    assert artifact.sklearn_version == sklearn.__version__ == card["sklearn_version"]
    stored = joblib.load(tmp_path / "m" / runtime.MODEL_FILE)
    assert stored["sklearn_version"] == sklearn.__version__ and stored["format"] == runtime.ARTIFACT_FORMAT_V1
    assert set(stored) == {"format", "model_version", "view", "classes", "normalization_id", "pipeline",
                           "score_method", "temperature", "config", "sklearn_version"}


def test_v2_model_card_contents(tmp_path, toy_bilingual):
    card = train.run("bilingual", str(tmp_path / "m"), dataset_path=toy_bilingual, quiet=True)
    on_disk = json.loads((tmp_path / "m" / runtime.CARD_FILE).read_text(encoding="utf-8"))
    assert on_disk == card
    assert card["model_version"] == "intent-clf-2.0.0" and card["release"] == "v2"
    assert {"rows", "folds", "score", "selection", "late_fusion", "calibration", "final_model", "keyword_features",
            "keyword_note", "test_split"} <= set(card["procedure"])
    assert "train rows" in card["procedure"]["keyword_note"] and card["procedure"]["test_split"] == "not loaded"
    assert card["procedure"]["out_of_scope_false_accept"].startswith(data.OUT_OF_SCOPE_FALSE_ACCEPT_RULE)
    # five grouped folds that partition train + dev; the dev rows are part of them
    assert len(card["folds"]) == train.CV_FOLDS
    assert sum(f["rows"] for f in card["folds"]) == 96 + 26
    assert sum(f["rows_from_dev"] for f in card["folds"]) == 26
    names = [c["name"] for c in card["candidates"]]
    base = card["selection"]["fusion_base"]
    assert names == [c["name"] for c in train.V2_CANDIDATES] + [f"fusion_{base}_a{a:g}" for a in train.FUSION_MIXES]
    assert base in {c["name"] for c in train.V2_CANDIDATES if c["kind"] in train.FUSION_BASE_KINDS}
    for row in card["candidates"]:
        assert {"macro_f1", "macro_f1_sd", "macro_f1_es", "macro_f1_pt", "macro_f1_rows_from_train",
                "macro_f1_rows_from_dev", "feature_blocks", "temperature", "log_loss"} <= set(row)
    sel = card["selection"]
    assert sel["chosen"] == card["chosen"]["name"] and sel["chosen"] in sel["tied"]
    assert sel["tolerance"] == train.TIE_TOLERANCE
    assert card["reference"]["name"] == "keyword_router" and card["reference"]["name"] not in names
    assert card["temperature"] > 0
    cv = card["cv_metrics"]
    assert set(cv["calibrated"]) == {"overall", "es", "pt"} and cv["calibrated"]["overall"]["n"] == 122
    assert len(cv["per_fold"]) == train.CV_FOLDS
    assert cv["calibration"]["after"]["log_loss"] <= cv["calibration"]["before"]["log_loss"] + 1e-9
    assert cv["policy_threshold"]["out_of_scope_rows"] == 20      # 5 merchants x 2 templates x 2 languages
    assert cv["rows_from_dev"]["calibrated"]["overall"]["n"] == 26
    assert not [k for k in _keys(card) if re.search(r"time|^date$|_date|created|_at$", k, re.IGNORECASE)]
    stored = joblib.load(tmp_path / "m" / runtime.MODEL_FILE)
    assert stored["format"] == runtime.ARTIFACT_FORMAT and stored["sklearn_version"] == sklearn.__version__
    assert stored["keyword_weight"] == card["keyword_weight"] and stored["keyword_mix"] == card["keyword_mix"]
    uses_keywords = bool(stored["keyword_weight"] or stored["keyword_mix"])
    assert card["keyword_rules_id"] == (runtime.KEYWORD_RULES_ID if uses_keywords else None)
    clf = runtime.IntentClassifier.load(str(tmp_path / "m"))
    assert clf.temperature == card["temperature"] and clf.model_version == card["model_version"]


def test_selection_rule_prefers_macro_f1_then_log_loss():
    rows = toy_intents.rows()
    _, report = train.fit_and_calibrate(_split(rows, "train"), _split(rows, "dev"), seed=3)
    table = report["candidates"]
    best = max(round(r["macro_f1"], train.SELECTION_DECIMALS) for r in table)
    tied = [r for r in table if round(r["macro_f1"], train.SELECTION_DECIMALS) == best]
    assert report["chosen"]["name"] == min(tied, key=lambda r: r["log_loss"])["name"]


# ---------------------------------------------------------------- scoring rule and calibration helpers

def test_effective_label():
    amb = {"intent": "card_lost_or_block", "acceptable_intents": ["card_lost_or_block", "dispute_unrecognized_charge"]}
    assert data.effective_label("dispute_unrecognized_charge", amb) == "dispute_unrecognized_charge"
    assert data.effective_label("card_lost_or_block", amb) == "card_lost_or_block"
    assert data.effective_label("out_of_scope", amb) == "card_lost_or_block"
    assert data.effective_label("out_of_scope", {"intent": "other_complaint"}) == "other_complaint"


def test_macro_f1_uses_acceptable_intents():
    rows = [{"intent": "card_lost_or_block", "acceptable_intents": ["card_lost_or_block", "dispute_unrecognized_charge"]},
            {"intent": "dispute_unrecognized_charge", "acceptable_intents": ["dispute_unrecognized_charge"]}]
    assert train.macro_f1(["dispute_unrecognized_charge", "dispute_unrecognized_charge"], rows) == 1.0
    assert train.macro_f1(["out_of_scope", "dispute_unrecognized_charge"], rows) < 1.0


def test_dev_false_accepts_use_the_shared_definition():
    O, P, Q, U = (data.CLASSES.index(c) for c in ("out_of_scope", "other_complaint", "account_payment_inquiry",
                                                  "dispute_unrecognized_charge"))
    spec = [(O, [O], Q, 0.9),        # out_of_scope answered as an inquiry: false accept
            (O, [O], P, 0.9),        # answered as a complaint: also a false accept (not acceptable for the row)
            (O, [O, P], P, 0.9),     # complaint is acceptable here: not a false accept
            (O, [O], Q, 0.3),        # below the threshold: the agent asks, an abstention never counts
            (O, [O], O, 0.9),        # right answer
            (U, [U], Q, 0.9)]        # not out_of_scope: not in the denominator
    rows, proba = [], np.zeros((len(spec), len(data.CLASSES)))
    for i, (intent, acceptable, top, conf) in enumerate(spec):
        rows.append({"intent": data.CLASSES[intent], "acceptable_intents": [data.CLASSES[a] for a in acceptable]})
        proba[i] = (1.0 - conf) / (len(data.CLASSES) - 1)
        proba[i, top] = conf
    th = train.threshold_metrics(proba, rows, 0.55)
    assert th["out_of_scope_rows"] == 5 and th["out_of_scope_false_accepts"] == 2
    assert th["out_of_scope_false_accept_rate"] == pytest.approx(2 / 5)
    pred = np.where(np.round(proba.max(axis=1), 3) < 0.55, data.ABSTAIN, proba.argmax(axis=1))
    intent = np.array([s[0] for s in spec])
    hit, mask = data.out_of_scope_false_accepts(pred, intent, train.acceptable_mask(rows))
    assert hit.tolist() == [True, True, False, False, False, False] and mask.sum() == 5


def test_set_log_loss_and_ece():
    rows = [{"intent": "card_lost_or_block", "acceptable_intents": ["card_lost_or_block", "dispute_unrecognized_charge"]},
            {"intent": "out_of_scope", "acceptable_intents": ["out_of_scope"]}]
    mask = train.acceptable_mask(rows)
    p = np.zeros((2, 6))
    p[0, 0] = p[0, 3] = 0.5          # all mass on the two acceptable intents
    p[1, 5] = 1.0
    assert train.set_log_loss(p, mask) == pytest.approx(0.0, abs=1e-12)
    assert train.expected_calibration_error(p, mask) == pytest.approx(0.25)    # row 0: confidence 0.5, correct
    wrong = np.full((2, 6), 0.02)
    wrong[:, 4] = 0.9
    assert train.expected_calibration_error(wrong, mask) == pytest.approx(0.9)


def test_fit_temperature_recovers_a_known_temperature():
    rng = np.random.default_rng(0)
    scores = rng.normal(scale=3.0, size=(5000, 6))
    p = runtime.softmax(scores, 2.0)
    labels = [rng.choice(6, p=row) for row in p]
    rows = [{"intent": data.CLASSES[y], "acceptable_intents": [data.CLASSES[y]]} for y in labels]
    assert train.fit_temperature(scores, train.acceptable_mask(rows)) == pytest.approx(2.0, rel=0.1)


def test_loaders_filter_by_split(tmp_path):
    path = toy_intents.write_jsonl(tmp_path / "rows.jsonl", toy_intents.rows(test_languages=("pt",)))
    assert {r["split"] for r in data.load_intent_rows(("train",), path)} == {"train"}
    assert {r["split"] for r in data.load_transfer_rows(("train", "dev"), path)} == {"train", "dev"}
    with pytest.raises(ValueError):
        data.load_intent_rows(("validation",), path)


# ---------------------------------------------------------------- release v2: protocol pieces

def _cv_rows(languages=("es", "pt")):
    rows = toy_intents.rows(languages=languages)
    return _split(rows, "train") + _split(rows, "dev")


def test_grouped_folds_hold_whole_unseen_families():
    rows = _cv_rows()
    folds = train.grouped_folds(rows)
    assert len(folds) == train.CV_FOLDS
    seen = np.concatenate([va for _, va in folds])
    assert sorted(seen.tolist()) == list(range(len(rows)))          # every row is validated exactly once
    for tr, va in folds:
        assert not {rows[i]["family_id"] for i in tr} & {rows[i]["family_id"] for i in va}
        assert {rows[i]["intent"] for i in tr} == set(data.CLASSES)
    again = train.grouped_folds(rows)
    assert all(np.array_equal(a, c) and np.array_equal(b, d) for (a, b), (c, d) in zip(folds, again))
    few = [dict(r, family_id=f"f{i % 3}") for i, r in enumerate(rows)]
    with pytest.raises(ValueError):
        train.grouped_folds(few)                                    # fewer families than folds
    lonely = [dict(r, family_id="only-complaints") if r["intent"] == "other_complaint" else r for r in rows]
    with pytest.raises(ValueError):
        train.grouped_folds(lonely)                                 # a fold's training rows would lack a class


def test_out_of_fold_scores_come_from_models_that_never_saw_the_family():
    rows = _cv_rows()
    texts = [data.normalize_text(r["text"]) for r in rows]
    x_keywords = runtime.keyword_features([data.keyword_text(r["text"]) for r in rows])
    y = np.array([r["intent"] for r in rows])
    folds = train.grouped_folds(rows)
    cands = [c for c in train.V2_CANDIDATES if c["name"] in ("char_lr_C4", "hybrid_svc_C0.1_w1")]
    scores = train.out_of_fold_scores(cands, texts, x_keywords, y, folds, seed=5)
    tr, va = folds[2]
    for cand in cands:
        vec = train.build_features(cand["features"])
        model = train.fit_model(cand, vec.fit_transform([texts[i] for i in tr]), x_keywords[tr], y[tr], 5)
        x_va = runtime.design_matrix(vec.transform([texts[i] for i in va]), x_keywords[va],
                                     cand.get("keyword_weight", 0.0))
        expected = runtime.model_scores(model, train.SCORE_METHOD[cand["model"]], x_va)
        np.testing.assert_array_equal(scores[cand["name"]][va], expected)


def _table_row(name, f1, blocks, c, weight=0.0, mix=0.0):
    return {"name": name, "macro_f1": f1, "feature_blocks": blocks, "C": c, "keyword_weight": weight,
            "keyword_mix": mix}


def test_tie_rule_prefers_the_simpler_candidate():
    table = [_table_row("hybrid", 0.900, 3, 0.1, weight=1.0), _table_row("union", 0.897, 2, 0.1),
             _table_row("char_c16", 0.896, 1, 16.0), _table_row("char_c4", 0.8951, 1, 4.0),
             _table_row("char_c1", 0.8949, 1, 1.0)]
    chosen, best, tied = train.select_cv(table)
    assert best == 0.9 and tied == ["hybrid", "union", "char_c16", "char_c4"]      # char_c1 is 0.0051 behind
    assert chosen["name"] == "char_c4"                  # fewest blocks, then the smaller C among the tied
    same = [_table_row("w2", 0.80, 3, 0.1, weight=2.0), _table_row("w05", 0.799, 3, 0.1, weight=0.5),
            _table_row("a03", 0.801, 3, 0.1, mix=0.3)]
    assert train.select_cv(same)[0]["name"] == "a03"    # same blocks and C: the smaller keyword weight or mix
    assert train.select_cv([_table_row("best", 0.9, 3, 1.0), _table_row("simple", 0.89, 1, 1.0)])[0]["name"] == "best"


def test_fusion_candidates_use_the_text_only_pick_and_its_temperature():
    rows = _cv_rows()
    cands = [c for c in train.V2_CANDIDATES if c["name"] in ("union_svc_C0.1", "char_lr_C4", "hybrid_svc_C0.1_w1")]
    _, report = train.fit_cv(_split(rows, "train"), _split(rows, "dev"), seed=3, candidates=cands, mixes=(0.2, 0.4))
    table = {r["name"]: r for r in report["candidates"]}
    base = report["selection"]["fusion_base"]
    assert base == train.select_cv([table["union_svc_C0.1"], table["char_lr_C4"]])[0]["name"]
    for mix in (0.2, 0.4):
        row = table[f"fusion_{base}_a{mix:g}"]
        assert row["temperature"] == table[base]["temperature"] and row["keyword_mix"] == mix
        assert row["feature_blocks"] == table[base]["feature_blocks"] + 1
    assert report["chosen"]["name"] == train.select_cv(list(table.values()))[0]["name"]


def test_keyword_features_follow_the_router():
    texts = ["me cobraron 2 veces el super", "perdí mi tarjeta, bloquéenla", "hola", "", "😤😤"] + \
        [r["text"] for r in toy_intents.rows()]
    x = runtime.keyword_features([data.keyword_text(t) for t in texts]).toarray()
    assert x.shape == (len(texts), len(runtime.KEYWORD_GROUPS) + len(data.CLASSES))
    assert runtime.KEYWORD_COLUMNS[:len(runtime.KEYWORD_GROUPS)] == tuple(g for g, _ in keywords.RESOLUTION_ORDER)
    for text, row in zip(texts, x):
        fired = {g for g, v in zip(runtime.KEYWORD_GROUPS, row) if v}
        assert fired == set(keywords.matches(text))
        onehot = row[len(runtime.KEYWORD_GROUPS):]
        assert onehot.sum() == 1 and data.CLASSES[int(onehot.argmax())] == keywords.predict(text)
    assert x[0, runtime.KEYWORD_GROUPS.index("dispute_incorrect_charge_or_fee")] == 1   # digits are kept: "2 veces"
    assert data.keyword_text("me cobraron 2 veces") == "me cobraron 2 veces"
    assert data.normalize_text("me cobraron 2 veces") == "me cobraron 0 veces"
    no_rule = runtime.keyword_features(["hola"])
    assert not runtime.keyword_groups_fired(no_rule)[0]         # the router's fallback answer is not evidence
    assert len(data.keyword_text("a" * 50_000)) == data.MAX_CHARS and data.keyword_text(None) == ""


def test_design_matrix_and_fusion():
    x_text = runtime.sparse.csr_matrix(np.array([[0.5, 0.0, 0.5], [0.0, 0.0, 0.0]]))
    x_kw = runtime.keyword_features(["me cobraron dos veces", "hola"])
    assert runtime.design_matrix(x_text, x_kw, 0.0) is x_text
    x = runtime.design_matrix(x_text, x_kw, 0.5).toarray()
    assert x.shape == (2, 3 + len(runtime.KEYWORD_COLUMNS))
    np.testing.assert_array_equal(x[:, :3], x_text.toarray())
    np.testing.assert_array_equal(x[:, 3:], 0.5 * x_kw.toarray())
    p = np.full((2, 6), 1 / 6)
    assert runtime.fuse(p, x_kw, 0.0) is p
    fused = runtime.fuse(p, x_kw, 0.3)
    np.testing.assert_allclose(fused.sum(axis=1), 1.0)
    np.testing.assert_allclose(fused, 0.7 * p + 0.3 * x_kw.toarray()[:, len(runtime.KEYWORD_GROUPS):])
    assert data.CLASSES[int(fused[1].argmax())] == keywords.DEFAULT_INTENT


def test_keyword_rules_id_tracks_the_rules(monkeypatch):
    assert runtime.keyword_rules_id() == runtime.KEYWORD_RULES_ID and len(runtime.KEYWORD_RULES_ID) == 64
    monkeypatch.setitem(keywords.GLOSSARY["refund_request"], "es", keywords.GLOSSARY["refund_request"]["es"] + ["x"])
    assert runtime.keyword_rules_id() != runtime.KEYWORD_RULES_ID
