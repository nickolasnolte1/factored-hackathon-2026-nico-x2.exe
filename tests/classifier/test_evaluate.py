"""Evaluation checks on hand-made rows and toy models trained inside the test (never data/, models/ or eval/):
the acceptable-intents macro-F1 on a fixed label set, abstentions, the false-accept rate, the bootstrap and
byte-identical outputs."""
import json
import os
import re

import joblib
import numpy as np
import pytest

from src.classifier import data, evaluate, runtime, train
from tests.classifier import toy_intents

U, I, Q, C, P, O = range(6)     # codes in data.CLASSES order
A = evaluate.ABSTAIN


def _rows(spec):
    """spec: list of (intent code, acceptable codes, language, attack_type)."""
    rows = []
    for k, (intent, acceptable, lang, attack) in enumerate(spec):
        rows.append({"id": f"r{k}", "text": f"row {k}", "language": lang, "variant": toy_intents.VARIANTS[lang],
                     "intent": data.CLASSES[intent], "acceptable_intents": [data.CLASSES[a] for a in acceptable],
                     "is_ambiguous": len(acceptable) > 1, "attack_type": attack})
    return rows


# r0..r5: the worked example. Effective truth [U, U, U, I, O, O], predictions [U, I, U, I, Q, O].
EXAMPLE = _rows([(U, [U], "es", None), (U, [U], "es", None), (I, [I, U], "es", None), (I, [I], "pt", None),
                 (O, [O], "pt", "prompt_injection"), (O, [O], "pt", None)])
EXAMPLE_PRED = np.array([U, I, U, I, Q, O])


def _truth(pred, rows):
    info = evaluate.row_info(rows)
    return evaluate.effective_truth(pred, info["intent"], info["accept"]), info


# ---------------------------------------------------------------- macro-F1 and accuracy

def test_macro_f1_known_answer():
    truth, info = _truth(EXAMPLE_PRED, EXAMPLE)
    assert truth.tolist() == [U, U, U, I, O, O]
    labels = evaluate.gold_labels(info["intent"])
    assert labels == [U, I, O]              # the gold classes of the slice; Q is only predicted
    # U: tp 2, fp 0, fn 1 -> 0.8; I: tp 1, fp 1 -> 2/3; O: tp 1, fn 1 -> 2/3. Q's false positive (r4) still costs O
    # its recall, but Q itself is not averaged in.
    expected = (0.8 + 2 / 3 + 2 / 3) / 3
    assert evaluate.macro_f1(truth, EXAMPLE_PRED, labels) == pytest.approx(expected)
    # train.macro_f1 (dev selection) averages over the classes in truth or predictions; with that label set the two
    # agree, and on dev every class is gold, so the sets coincide there.
    names = [data.CLASSES[p] for p in EXAMPLE_PRED]
    assert train.macro_f1(names, EXAMPLE) == pytest.approx(evaluate.macro_f1(truth, EXAMPLE_PRED, [U, I, Q, O]))
    assert train.macro_f1(names, EXAMPLE) == pytest.approx(0.533333, abs=1e-6)


def test_abstention_is_a_miss_and_not_a_class():
    pred = EXAMPLE_PRED.copy()
    pred[3] = A
    truth, info = _truth(pred, EXAMPLE)
    assert truth.tolist() == [U, U, U, I, O, O]
    # I: tp 0, fp 1 (r1), fn 1 (r3) -> 0; the abstention adds no false positive anywhere.
    labels = evaluate.gold_labels(info["intent"])
    assert evaluate.macro_f1(truth, pred, labels) == pytest.approx((0.8 + 0.0 + 2 / 3) / 3)
    assert evaluate.is_correct(pred, info["accept"]).tolist() == [True, False, True, False, False, True]


def test_macro_f1_matches_the_training_definition_on_random_rows():
    rng = np.random.default_rng(3)
    spec = []
    for _ in range(300):
        intent = int(rng.integers(0, 6))
        extra = [int(rng.integers(0, 6))] if rng.random() < 0.3 else []
        spec.append((intent, sorted({intent, *extra}), "es", None))
    rows = _rows(spec)
    pred = rng.integers(0, 6, size=len(rows))
    truth, info = _truth(pred, rows)
    labels = evaluate.gold_labels(info["intent"])
    assert labels == list(range(6))
    assert evaluate.macro_f1(truth, pred, labels) == pytest.approx(
        train.macro_f1([data.CLASSES[p] for p in pred], rows))


def test_weighted_counts_equal_explicit_resamples():
    rng = np.random.default_rng(5)
    truth = rng.integers(0, 6, size=40)
    pred = np.where(rng.random(40) < 0.6, truth, rng.integers(0, 7, size=40))
    idx = rng.integers(0, 40, size=(25, 40))
    weights = np.stack([np.bincount(row, minlength=40) for row in idx]).astype(float)
    labels = evaluate.gold_labels(truth)
    by_weight = evaluate.macro_f1(truth, pred, labels, weights)
    by_index = [evaluate.macro_f1(truth[row], pred[row], labels) for row in idx]
    np.testing.assert_allclose(by_weight, by_index)
    assert evaluate.macro_f1(truth, pred, labels, np.ones((1, 40)))[0] == pytest.approx(
        evaluate.macro_f1(truth, pred, labels))


def test_every_resample_averages_over_the_same_labels():
    truth, pred = np.array([U, U, I]), np.array([U, U, I])
    labels = evaluate.gold_labels(truth)
    assert evaluate.macro_f1(truth, pred, labels) == 1.0
    # The second resample never draws the I row and nothing predicts I: I stays in the average with F1 0.
    weights = np.array([[1.0, 1.0, 1.0], [2.0, 1.0, 0.0]])
    np.testing.assert_allclose(evaluate.macro_f1(truth, pred, labels, weights), [1.0, 0.5])
    with pytest.raises(ValueError):
        evaluate.macro_f1(truth, pred, [])


# ---------------------------------------------------------------- rates

def test_false_accept_rate():
    rows = _rows([(O, [O], "es", None), (O, [O], "es", None), (O, [O, P], "es", None), (O, [O], "pt", None),
                  (O, [O], "pt", None), (U, [U], "pt", None)])
    pred = np.array([Q, O, P, A, U, Q])
    info = evaluate.row_info(rows)
    hit, oos = data.out_of_scope_false_accepts(pred, info["intent"], info["accept"])
    # r0 accepted as inquiry, r4 as a dispute; r2's complaint is acceptable, r3 abstained, r5 is not out_of_scope.
    assert hit.tolist() == [True, False, False, False, True, False]
    assert oos.sum() == 5
    assert evaluate.rate(hit, oos) == pytest.approx(2 / 5)


def test_slices_with_a_small_gold_class_are_marked():
    def run(n_dispute, n_oos):
        rows = _rows([(U, [U], "es", None)] * n_dispute + [(O, [O], "es", None)] * n_oos)
        pred = evaluate.encode([r["intent"] for r in rows])
        systems = {"majority": {"pred": np.full(len(rows), O)}, "keyword": {"pred": pred},
                   "model_v1": {"pred": pred}, "model_v2": {"pred": pred}}
        result, _ = evaluate.evaluate_set("test", rows, systems, 20, 1)
        return result["slices"]["all"]
    ok, small = run(5, 5), run(6, 4)
    assert ok["macro_f1_labels"] == small["macro_f1_labels"] == ["dispute_unrecognized_charge", "out_of_scope"]
    assert ok["macro_f1_interpretable"] is True and small["macro_f1_interpretable"] is False
    # majority predicts out_of_scope only: dispute F1 0, out_of_scope F1 2*5/(5+10); no other class averaged in
    assert ok["systems"]["majority"]["macro_f1"]["value"] == pytest.approx((0.0 + 10 / 15) / 2)


def test_rate_of_an_empty_slice_is_nan():
    assert np.isnan(evaluate.rate(np.array([True]), np.array([False])))


def test_threshold_metrics_on_a_small_slice():
    top = EXAMPLE_PRED
    abstain = np.array([False, True, True, False, True, False])
    system = {"pred": np.where(abstain, A, top), "top": top, "abstain": abstain}
    info = evaluate.row_info(EXAMPLE)
    weights = evaluate.resample_weights(len(EXAMPLE), 30, evaluate.stream(1, "toy", "all"))
    m, _ = evaluate.score_system(system, info, weights)
    assert m["abstention"]["all"]["value"] == pytest.approx(3 / 6)
    assert m["abstention"]["ambiguous"]["value"] == pytest.approx(1.0)          # r2 is the only ambiguous row
    assert m["abstention"]["unambiguous"]["value"] == pytest.approx(2 / 5)
    assert m["abstention"]["top1_wrong"]["value"] == pytest.approx(1.0)          # r1 and r4 were wrong
    assert m["abstention"]["top1_right"]["value"] == pytest.approx(1 / 4)
    assert m["coverage"]["value"] == pytest.approx(0.5)
    assert m["selective_accuracy"]["value"] == pytest.approx(1.0)                # r0, r3, r5 answered and right
    assert m["accuracy"]["value"] == pytest.approx(0.5)
    assert m["out_of_scope_false_accept"]["value"] == pytest.approx(0.0)
    assert m["attacks"]["prompt_injection"] == {"n": 1, "correct": 0, "accuracy": 0.0, "abstained": 1,
                                                "correct_or_abstained": 1.0}
    assert m["recall_by_intent"]["dispute_unrecognized_charge"]["value"] == pytest.approx(1 / 2)


def test_selective_macro_f1_uses_the_answered_gold_classes():
    info = evaluate.row_info(EXAMPLE)
    weights = evaluate.resample_weights(len(EXAMPLE), 30, evaluate.stream(1, "toy", "all"))

    def selective(abstain):
        abstain = np.array(abstain)
        system = {"pred": np.where(abstain, A, EXAMPLE_PRED), "top": EXAMPLE_PRED, "abstain": abstain}
        return evaluate.score_system(system, info, weights)[0]["selective_macro_f1"]
    # r0, r3 and r5 answered and right: U, I and O all perfect
    assert selective([False, True, True, False, True, False])["value"] == pytest.approx(1.0)
    # both I rows abstained: I leaves the average; answered r0 U->U, r1 U->I, r4 O->Q, r5 O->O give U 2/3, O 2/3
    assert selective([False, False, True, True, False, False])["value"] == pytest.approx(2 / 3)
    nothing = selective([True] * 6)
    assert np.isnan(nothing["value"]) and nothing["ci"] is None


def test_brier_scores():
    rows = _rows([(U, [U], "es", None), (Q, [Q, I], "es", None)])
    info = evaluate.row_info(rows)
    perfect = np.eye(6)[[U, I]]
    assert evaluate.brier(perfect, info["intent"], info["accept"]) == pytest.approx(0.0)
    assert evaluate.top_label_brier(perfect, info["accept"]) == pytest.approx(0.0)
    uniform = np.full((2, 6), 1 / 6)
    assert evaluate.brier(uniform, info["intent"], info["accept"]) == pytest.approx(5 / 6)
    assert evaluate.top_label_brier(uniform, info["accept"]) == pytest.approx(((5 / 6) ** 2 + (1 / 6) ** 2) / 2)


def test_confusion_uses_the_effective_label():
    info = evaluate.row_info(EXAMPLE)
    matrix = evaluate.confusion(EXAMPLE_PRED, info["intent"], info["accept"])
    assert matrix.sum() == len(EXAMPLE)
    assert matrix[U, U] == 2 and matrix[U, I] == 1 and matrix[I, I] == 1 and matrix[O, Q] == 1 and matrix[O, O] == 1
    with_abstain = evaluate.confusion(np.array([U, A]), np.array([U, I]), np.eye(6, dtype=bool)[[U, I]], True)
    assert with_abstain.shape == (6, 7) and with_abstain[I, A] == 1


# ---------------------------------------------------------------- bootstrap

def test_bootstrap_is_deterministic():
    a = evaluate.resample_weights(50, 200, evaluate.stream(20261005, "test", "es"))
    b = evaluate.resample_weights(50, 200, evaluate.stream(20261005, "test", "es"))
    c = evaluate.resample_weights(50, 200, evaluate.stream(20261005, "test", "pt"))
    d = evaluate.resample_weights(50, 200, evaluate.stream(7, "test", "es"))
    np.testing.assert_array_equal(a, b)
    assert not np.array_equal(a, c) and not np.array_equal(a, d)
    assert a.shape == (200, 50) and np.all(a.sum(axis=1) == 50)


def test_interval_and_paired_difference():
    assert evaluate.interval(np.arange(1001) / 1000) == pytest.approx([0.025, 0.975])
    assert evaluate.interval([np.nan, np.nan]) is None
    same = np.linspace(0.5, 0.7, 101)
    diff = evaluate.paired_difference(0.6, 0.6, same, same)
    assert diff["value"] == 0.0 and diff["ci"] == [0.0, 0.0] and diff["share_of_resamples_le_0"] == 1.0


def test_clean_rounds_and_drops_nan():
    out = evaluate.clean({"a": np.float64(0.12345678), "b": np.nan, "c": np.int64(3), "d": np.bool_(True),
                          "e": np.array([[1, 2]]), "f": -0.0})
    assert out == {"a": 0.123457, "b": None, "c": 3, "d": True, "e": [[1, 2]], "f": 0.0}
    assert json.dumps(out)


# ---------------------------------------------------------------- whole run on toy files

def _toy_final_rows(prefix, lang, merchant):
    rows = []
    for intent, by_lang in toy_intents.TEMPLATES.items():
        for template in by_lang[lang]:
            rows.append({"id": f"{prefix}-{len(rows) + 1:03d}", "text": template.format(n="77", m=merchant),
                         "language": lang, "variant": toy_intents.VARIANTS[lang], "intent": intent,
                         "acceptable_intents": [intent], "is_ambiguous": False, "attack_type": None})
    rows[-1]["attack_type"] = "prompt_injection"
    rows[0]["acceptable_intents"] = [rows[0]["intent"], "dispute_incorrect_charge_or_fee"]
    rows[0]["is_ambiguous"] = True
    return rows


def _toy_test_rows(languages, start):
    rows = []
    for lang in languages:
        for row in _toy_final_rows("t", lang, "kiosko nube"):
            rows.append({**row, "id": f"toy-test-{start + len(rows):04d}", "split": "test"})
    return rows


@pytest.fixture(scope="module")
def toy_inputs(tmp_path_factory):
    """Toy input files and the four toy models of both releases, written by src.classifier.train itself (model and
    card) into folders named as in train.RELEASES."""
    root = tmp_path_factory.mktemp("evaluate")
    test_rows = _toy_test_rows(("es", "pt"), 9000)
    bilingual = toy_intents.rows() + test_rows
    transfer = toy_intents.rows(languages=("es",)) + [r for r in test_rows if r["language"] == "pt"]
    paths = {
        "intent_dataset": toy_intents.write_jsonl(root / "intent.jsonl", bilingual),
        "transfer": toy_intents.write_jsonl(root / "transfer.jsonl", transfer),
        "independent_es": toy_intents.write_jsonl(root / "ind_es.jsonl", _toy_final_rows("hold-es", "es", "bar sol")),
        "independent_pt": toy_intents.write_jsonl(root / "ind_pt.jsonl", _toy_final_rows("hold-pt", "pt", "bar sol")),
        "team": toy_intents.write_jsonl(root / "team.jsonl", _toy_final_rows("hand-es", "es", "puesto rio")),
    }
    model_dirs = {}
    for release in evaluate.RELEASES:
        model_dirs[release] = {}
        for view, dataset in (("bilingual", paths["intent_dataset"]), ("transfer", paths["transfer"])):
            folder = str(root / "models" / train.RELEASES[release][view]["out"])
            train.run(view, folder, seed=3, dataset_path=dataset, quiet=True, release=release)
            model_dirs[release][view] = folder
    # A v2 hybrid (keyword features), which the toy selection does not pick, for the side-by-side checks.
    cand = [c for c in train.V2_CANDIDATES if c["name"] == "hybrid_svc_C0.1_w1"]
    for view, langs in (("bilingual", ("es", "pt")), ("transfer", ("es",))):
        rows = toy_intents.rows(languages=langs)
        artifact, _ = train.fit_cv([r for r in rows if r["split"] == "train"], [r for r in rows if r["split"] == "dev"],
                                   seed=3, candidates=cand, mixes=(), languages=langs,
                                   model_version=f"intent-clf-toy-hybrid-{view}")
        assert artifact["format"] == runtime.ARTIFACT_FORMAT and artifact["keyword_weight"] == 1.0
        os.makedirs(root / "hybrid" / view)
        joblib.dump(artifact, root / "hybrid" / view / runtime.MODEL_FILE)
    return root, paths, model_dirs


def _run(toy, out, model_dirs=None, boot=40):
    root, paths, dirs = toy
    return evaluate.run(model_dirs or dirs, boot=boot, seed=11, out_dir=str(root / out), paths=paths, quiet=True)


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def test_default_model_dirs_are_the_training_folders():
    for release in evaluate.RELEASES:
        for view, spec in train.RELEASES[release].items():
            assert evaluate.DEFAULT_MODEL_DIRS[release][view] == os.path.join(train.MODELS_DIR, spec["out"])
    assert evaluate.DEFAULT_MODEL_DIRS["v2"]["bilingual"] == runtime.DEFAULT_DIR      # the model the app loads
    assert evaluate.DEFAULT_MODEL_DIRS["v1"]["bilingual"] != runtime.DEFAULT_DIR
    assert evaluate.model_dirs_with_defaults({"v1": {"transfer": "x"}})["v1"] == {
        "bilingual": evaluate.DEFAULT_MODEL_DIRS["v1"]["bilingual"], "transfer": "x"}
    with pytest.raises(ValueError):
        evaluate.model_dirs_with_defaults({"v3": {}})


def test_cli_passes_the_four_model_folders(monkeypatch):
    seen = {}
    monkeypatch.setattr(evaluate, "run", lambda model_dirs, boot, seed, out: seen.update(dirs=model_dirs, boot=boot))
    evaluate.main(["--v1-dir", "a", "--v2-es-only-dir", "d", "--boot", "7"])
    assert seen["dirs"] == {"v1": {"bilingual": "a", "transfer": evaluate.DEFAULT_MODEL_DIRS["v1"]["transfer"]},
                            "v2": {"bilingual": evaluate.DEFAULT_MODEL_DIRS["v2"]["bilingual"], "transfer": "d"}}
    assert seen["boot"] == 7


def test_two_runs_write_identical_results(toy_inputs):
    root, _, _ = toy_inputs
    report = _run(toy_inputs, "a")
    _run(toy_inputs, "b")
    for name in (evaluate.JSON_FILE, evaluate.MD_FILE):
        assert (root / "a" / name).read_bytes() == (root / "b" / name).read_bytes(), name
    latency = json.loads((root / "a" / evaluate.LATENCY_FILE).read_text(encoding="utf-8"))
    assert list(latency) == ["model_v1", "model_v2"]
    on_disk = json.loads((root / "a" / evaluate.JSON_FILE).read_text(encoding="utf-8"))
    assert on_disk == report
    assert not [k for k in _keys(report) if re.search(r"time|^date$|_date|created|_at$", k, re.IGNORECASE)]
    # the readable report is a function of the JSON alone
    assert evaluate.render_markdown(on_disk) == (root / "a" / evaluate.MD_FILE).read_text(encoding="utf-8")


def test_toy_report_contents(toy_inputs):
    root, paths, dirs = toy_inputs
    report = _run(toy_inputs, "c")
    assert list(report["sets"]) == ["test", "transfer_pt", "independent", "team"]
    assert set(report["sets"]["test"]["slices"]) == {"all", "es", "pt"}
    assert set(report["sets"]["transfer_pt"]["slices"]) == {"all"}            # every row is PT
    assert set(report["sets"]["team"]["slices"]) == {"all"}                   # every row is ES
    block = report["sets"]["test"]["slices"]["all"]
    assert list(block["systems"]) == ["majority", "keyword", "model_v1", "model_v1_threshold", "model_v2",
                                      "model_v2_threshold"]
    assert list(block["differences_macro_f1"]) == ["model_v2 - keyword", "model_v2 - model_v1", "model_v2 - majority",
                                                   "model_v1 - keyword", "model_v1 - majority"]
    transfer = report["sets"]["transfer_pt"]["slices"]["all"]
    assert list(transfer["systems"]) == [
        "majority", "keyword", "model_es_only_v1", "model_es_only_v1_threshold", "model_es_only_v2",
        "model_es_only_v2_threshold", "model_v1", "model_v1_threshold", "model_v2", "model_v2_threshold"]
    assert {"model_es_only_v2 - model_es_only_v1", "model_v2 - model_v1", "model_v1 - model_es_only_v1",
            "model_v2 - model_es_only_v2"} <= set(transfer["differences_macro_f1"])
    for est in (block["systems"]["model_v2"]["macro_f1"], block["systems"]["keyword"]["accuracy"],
                block["differences_macro_f1"]["model_v2 - model_v1"]):
        assert est["ci"][0] <= est["ci"][1]
    diff = block["differences_macro_f1"]["model_v2 - model_v1"]
    assert diff["value"] == pytest.approx(block["systems"]["model_v2"]["macro_f1"]["value"]
                                          - block["systems"]["model_v1"]["macro_f1"]["value"], abs=2e-6)
    assert set(report["sets"]["test"]["language_gap_macro_f1"]) == set(block["systems"])
    assert report["policy"]["min_intent_confidence"] == data.min_intent_confidence()
    assert report["data"]["intent_dataset"]["sha256"] == data.file_sha256(paths["intent_dataset"])
    models = report["models"]
    assert list(models) == ["model_v1", "model_es_only_v1", "model_v2", "model_es_only_v2"]
    assert [m["model_version"] for m in models.values()] == [
        "intent-clf-1.0.0", "intent-clf-es-only-1.0.0", "intent-clf-2.0.0", "intent-clf-es-only-2.0.0"]
    assert [m["release"] for m in models.values()] == ["v1", "v1", "v2", "v2"]
    assert models["model_v1"]["model_sha256"] == data.file_sha256(
        os.path.join(dirs["v1"]["bilingual"], runtime.MODEL_FILE))
    assert models["model_v1"]["selection"]["rows"] == "dev split"
    assert models["model_v2"]["selection"]["macro_f1_rows_from_dev"] is not None
    tr = report["transfer"]
    assert tr["transfer_rows_equal_test_pt_rows"] is True
    assert set(tr) == {"target_abs_gap", "transfer_rows_equal_test_pt_rows", "v1", "v2", "v2_minus_v1"}
    for release in evaluate.RELEASES:
        assert set(tr[release]) == {"model_es_only", "model_minus_model_es_only_on_pt_test",
                                    "model_minus_model_es_only_on_es_test", "model_gap_on_test"}
        assert tr[release]["model_minus_model_es_only_on_pt_test"] == transfer["differences_macro_f1"][
            f"model_{release} - model_es_only_{release}"]
    assert tr["v2_minus_v1"]["model_es_only_on_pt_test"] == transfer["differences_macro_f1"][
        "model_es_only_v2 - model_es_only_v1"]
    assert report["sets"]["test"]["call_vs_batch_disagreements"] == {"model_v1": 0, "model_v2": 0}
    assert set(report["errors"]) == {"independent", "team"}
    assert set(report["errors"]["team"]) == {"model_v1", "model_v2"}
    # toy models live outside the repo: no bare folder name is recorded
    for entry in models.values():
        assert entry["outside_repo"] is True and "dir" not in entry
    team = report["sets"]["team"]["slices"]["all"]
    assert team["macro_f1_interpretable"] is False                # 2 rows per class
    md = (root / "c" / evaluate.MD_FILE).read_text(encoding="utf-8")
    assert md.startswith("# Intake intent classifier: final evaluation\n\n## Protocol and history\n")
    protocol = md.split("\n## ")[1]
    for text in evaluate.PROTOCOL.values():
        assert text[1:] in " ".join(protocol.split()), text
    assert "**About the test sets.**" in md and evaluate.NOT_INTERPRETABLE in md
    assert "| v2 - keyword | v2 - v1 | v1 - keyword |" in md
    assert md.index("## Protocol and history") < md.index("## At a glance (slice all)") < md.index("## Setup")


def test_v1_numbers_do_not_depend_on_the_model_scored_beside_it(toy_inputs):
    """The resamples do not depend on the systems, so v1's numbers are the same whichever v2 is scored with it, and
    a release scored against itself differs by exactly 0."""
    root, _, dirs = toy_inputs
    normal = _run(toy_inputs, "d")
    same = _run(toy_inputs, "e", model_dirs={"v1": dirs["v1"], "v2": dirs["v1"]})
    hybrid_dirs = {view: str(root / "hybrid" / view) for view in ("bilingual", "transfer")}
    hybrid = _run(toy_inputs, "f", model_dirs={"v1": dirs["v1"], "v2": hybrid_dirs})
    assert hybrid["models"]["model_v2"]["model_version"] == "intent-clf-toy-hybrid-bilingual"
    assert hybrid["sets"]["test"]["call_vs_batch_disagreements"] == {"model_v1": 0, "model_v2": 0}
    for other_run in (same, hybrid):
        for name in ("test", "independent", "team", "transfer_pt"):
            for sl, block in normal["sets"][name]["slices"].items():
                other = other_run["sets"][name]["slices"][sl]
                for key in ("majority", "keyword", "model_v1", "model_v1_threshold"):
                    assert block["systems"][key] == other["systems"][key], (name, sl, key)
                for pair, diff in block["differences_macro_f1"].items():
                    if "v2" not in pair:
                        assert diff == other["differences_macro_f1"][pair], (name, sl, pair)
                assert block["confusion"]["model_v1"] == other["confusion"]["model_v1"]
            if "language_gap_macro_f1" in normal["sets"][name]:
                assert (normal["sets"][name]["language_gap_macro_f1"]["model_v1"]
                        == other_run["sets"][name]["language_gap_macro_f1"]["model_v1"])
        assert normal["transfer"]["v1"] == other_run["transfer"]["v1"]
        assert normal["errors"]["independent"]["model_v1"] == other_run["errors"]["independent"]["model_v1"]
    for block in [b for s in same["sets"].values() for b in s["slices"].values()]:
        assert block["differences_macro_f1"]["model_v2 - model_v1"] == {
            "value": 0.0, "ci": [0.0, 0.0], "share_of_resamples_le_0": 1.0}


def test_selection_entry_reads_both_card_kinds(toy_inputs):
    _, _, dirs = toy_inputs
    cards = {}
    for release in evaluate.RELEASES:
        with open(os.path.join(dirs[release]["bilingual"], runtime.CARD_FILE), encoding="utf-8") as fh:
            cards[release] = json.load(fh)
    v1 = evaluate.selection_entry(cards["v1"])
    assert v1 == {"rows": "dev split",
                  "macro_f1": cards["v1"]["dev_metrics"]["calibrated"]["overall"]["macro_f1"]}
    v2 = evaluate.selection_entry(cards["v2"])
    chosen = next(c for c in cards["v2"]["candidates"] if c["name"] == cards["v2"]["chosen"]["name"])
    assert v2["macro_f1"] == chosen["macro_f1"]
    assert v2["macro_f1_rows_from_dev"] == cards["v2"]["cv_metrics"]["rows_from_dev"]["calibrated"]["overall"][
        "macro_f1"]
    assert evaluate.selection_entry({"chosen": {"name": "x"}}) is None


def test_model_entry_inside_the_repo_keeps_the_folder(toy_inputs, monkeypatch):
    root, _, dirs = toy_inputs
    clf = runtime.IntentClassifier.load(dirs["v1"]["bilingual"])
    monkeypatch.setattr(data, "REPO", str(root))
    entry = evaluate.model_entry(dirs["v1"]["bilingual"], clf, "v1")
    assert entry["dir"] == "models/intent_classifier_v1" and "outside_repo" not in entry
    assert entry["release"] == "v1"
    assert data.inside_repo(str(root)) and not data.inside_repo(str(root.parent))


@pytest.mark.parametrize("release", ["v1", "v2"])
def test_model_systems_give_no_evidence_answers_to_featureless_messages(toy_inputs, release):
    _, _, dirs = toy_inputs
    clf = runtime.IntentClassifier.load(dirs[release]["bilingual"])
    rows = _rows([(U, [U], "es", None), (O, [O], "es", None), (O, [O], "pt", None), (O, [O], "pt", None)])
    for row, text in zip(rows, ["no reconozco un cargo de 80 en bar sol", "​​", "﻿", "我的卡"]):
        row["text"] = text
    forced, thresholded, results = evaluate.model_systems(clf, rows)
    assert forced["pred"].tolist()[1:] == [O, O, O]
    np.testing.assert_allclose(forced["proba"][1:], 1.0 / len(data.CLASSES))
    assert thresholded["abstain"].tolist() == [False, True, True, True]
    assert [r["confidence"] for r in results[1:]] == [0.167] * 3


def test_system_labels():
    assert evaluate._label("model_v2") == "Model v2"
    assert evaluate._label("model_es_only_v1_threshold") == "ES-only v1 + threshold"
    assert evaluate._label("keyword") == "Keyword"
    assert evaluate._diff_label("model_v2 - model_v1") == "v2 - v1"
    assert set(evaluate.SYSTEM_DESCRIPTIONS) == {k for pairs in evaluate.PAIRS.values() for pair in pairs
                                                 for k in pair} | {k + evaluate.THRESHOLD for k in (
                                                     "model_v1", "model_v2", "model_es_only_v1", "model_es_only_v2")}


def test_majority_class_breaks_ties_in_class_order():
    rows = [{"intent": "other_complaint"}, {"intent": "account_payment_inquiry"}]
    assert evaluate.majority_class(rows)[0] == "account_payment_inquiry"


def test_final_rows_with_bad_labels_are_refused(tmp_path):
    bad = _toy_final_rows("hold-es", "es", "x")[:1]
    bad[0]["acceptable_intents"] = ["out_of_scope"]
    path = toy_intents.write_jsonl(tmp_path / "bad.jsonl", bad)
    with pytest.raises(ValueError):
        evaluate.load_final_rows(path)
