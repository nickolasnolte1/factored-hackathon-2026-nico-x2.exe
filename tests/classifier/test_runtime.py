"""Runtime adapter checks on toy models trained inside the test (never the real artifact in models/): release v1
artifacts (format 1, one per model kind) and release v2 artifacts (format 2: text only, hybrid with keyword features,
late fusion with the keyword router)."""
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import joblib
import numpy as np
import pytest
import sklearn

from src.classifier import data, keywords, runtime, train
from tests.classifier import toy_intents

KEYS = {"intent", "confidence", "top", "below_threshold", "threshold", "model_version", "latency_ms"}
ONE_PER_KIND = {
    "logreg": [c for c in train.V1_CANDIDATES if c["model"] == "logreg"][:1],
    "linear_svc": [c for c in train.V1_CANDIDATES if c["model"] == "linear_svc"][:1],
    "complement_nb": [c for c in train.V1_CANDIDATES if c["model"] == "complement_nb"][:1],
}
V2_KINDS = ("v2_text", "v2_hybrid", "v2_fusion")
ALL_KINDS = sorted(ONE_PER_KIND) + list(V2_KINDS)


def _split_rows():
    rows = toy_intents.rows()
    return [r for r in rows if r["split"] == "train"], [r for r in rows if r["split"] == "dev"]


def _fit(candidates):
    artifact, _ = train.fit_and_calibrate(*_split_rows(), seed=7, candidates=candidates,
                                          model_version="intent-clf-toy")
    return artifact


def _fit_v2(name):
    cands = [c for c in train.V2_CANDIDATES if c["name"] == name]
    artifact, _ = train.fit_cv(*_split_rows(), seed=7, candidates=cands, mixes=(), model_version="intent-clf-toy")
    return artifact


@pytest.fixture(scope="module")
def artifacts():
    out = {kind: _fit(cands) for kind, cands in ONE_PER_KIND.items()}
    out["v2_text"] = _fit_v2("char_lr_C4")
    out["v2_hybrid"] = _fit_v2("hybrid_svc_C0.1_w1")
    # late fusion on top of the text-only model, as fit_cv writes it when a fusion candidate wins
    out["v2_fusion"] = dict(out["v2_text"], keyword_mix=0.3, keyword_rules=runtime.KEYWORD_RULES_ID)
    assert out["v2_hybrid"]["keyword_weight"] == 1.0 and out["v2_hybrid"]["keyword_rules"] == runtime.KEYWORD_RULES_ID
    assert out["v2_text"]["keyword_weight"] == 0.0 and out["v2_text"]["keyword_rules"] is None
    return out


@pytest.fixture(scope="module")
def clf(artifacts):
    return runtime.IntentClassifier(artifacts["logreg"])


def _check_format(out, threshold):
    assert set(out) == KEYS
    assert out["intent"] in data.CLASSES
    assert isinstance(out["confidence"], float) and 0.0 <= out["confidence"] <= 1.0
    assert out["confidence"] == round(out["confidence"], 3)
    assert len(out["top"]) == 3
    assert [set(t) for t in out["top"]] == [{"intent", "p"}] * 3
    assert len({t["intent"] for t in out["top"]}) == 3
    ps = [t["p"] for t in out["top"]]
    assert ps == sorted(ps, reverse=True) and all(p == round(p, 3) for p in ps)
    assert out["top"][0]["intent"] == out["intent"] and out["top"][0]["p"] == out["confidence"]
    assert out["below_threshold"] is (out["confidence"] < threshold)
    assert out["threshold"] == threshold
    assert isinstance(out["latency_ms"], int) and out["latency_ms"] >= 0


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_output_format_every_model_kind(artifacts, kind):
    clf = runtime.IntentClassifier(artifacts[kind])
    for text in ("no reconozco un cargo de 80 en mercado sol", "roubaram meu cartão ontem", "quiero un crédito"):
        out = clf(text)
        _check_format(out, data.min_intent_confidence())
        assert out["model_version"] == "intent-clf-toy"


@pytest.mark.parametrize("kind", ["logreg"] + list(V2_KINDS))
def test_learns_the_toy_task(artifacts, kind):
    clf = runtime.IntentClassifier(artifacts[kind])
    assert clf("no reconozco un cargo de 75 en cafe luna")["intent"] == "dispute_unrecognized_charge"
    assert clf("roubaram meu cartão saindo da farmacia")["intent"] == "card_lost_or_block"
    assert clf("quero simular um financiamento de 9000")["intent"] == "out_of_scope"


def test_threshold_comes_from_policy(clf, tmp_path):
    assert clf.threshold == data.min_intent_confidence()
    with open(data.POLICY_PATH, encoding="utf-8") as fh:
        assert clf.threshold == json.load(fh)["handoff"]["min_intent_confidence"]
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"handoff": {"min_intent_confidence": 0.42}}), encoding="utf-8")
    assert data.min_intent_confidence(str(policy)) == 0.42
    policy.write_text(json.dumps({"handoff": {"min_intent_confidence": 1.5}}), encoding="utf-8")
    with pytest.raises(ValueError):
        data.min_intent_confidence(str(policy))


def test_rounding_and_threshold_edges(artifacts):
    clf = runtime.IntentClassifier(artifacts["logreg"], threshold=0.55)
    rest = lambda top: [(1.0 - top) / 5] * 5  # noqa: E731

    out = clf.result_from_proba([0.5496] + rest(0.5496))
    assert out["confidence"] == 0.55 and out["below_threshold"] is False      # compared after rounding
    out = clf.result_from_proba([0.5494] + rest(0.5494))
    assert out["confidence"] == 0.549 and out["below_threshold"] is True
    out = clf.result_from_proba(rest(0.9)[:3] + [0.9] + rest(0.9)[:2])
    assert out["intent"] == "card_lost_or_block" and out["below_threshold"] is False
    assert [t["p"] for t in out["top"]] == [0.9, 0.02, 0.02]
    # ties keep data.CLASSES order
    assert [t["intent"] for t in out["top"]][1:] == ["dispute_unrecognized_charge", "dispute_incorrect_charge_or_fee"]

    strict = runtime.IntentClassifier(artifacts["logreg"], threshold=0.999)
    lenient = runtime.IntentClassifier(artifacts["logreg"], threshold=0.01)
    text = "hay una compra de 60 en tienda norte que yo no hice"
    assert lenient(text)["below_threshold"] is False
    assert strict(text)["below_threshold"] is (strict(text)["confidence"] < 0.999)


class _BadStr:
    def __str__(self):
        raise RuntimeError("no text")


ODD_INPUTS = {
    "empty": "", "whitespace": "   \n\t ", "none": None, "int": 12345, "float": 3.5,
    "bytes": b"perdi meu cart\xc3\xa3o", "list": ["lista"], "long_x": "x" * 200_000, "long_words": "no reconozco " * 5_000,
    "lone_surrogate": "\ud800 sustituto", "emoji": "😤😤😤", "control_chars": "\x00\x01",
    "fullwidth": "ＣＡＲＧＯ　ＮＯ　ＲＥＣＯＮＯＣＩＤＯ", "bad_str": _BadStr(),
}
# Text left after normalization in which no feature of the toy models fires: an all-zero feature vector.
NO_FEATURE_INPUTS = {
    "emoji_only": "😤😤😤", "cjk": "我的卡被偷了", "arabic": "سرقت بطاقتي", "nul": "\x00", "bom": "﻿",
    "zero_width": "​‌‍", "mixed": "﻿​😤 \x00",
}


@pytest.mark.parametrize("value", list(ODD_INPUTS.values()), ids=list(ODD_INPUTS))
def test_odd_input_never_raises(clf, value):
    out = clf(value)
    _check_format(out, clf.threshold)


def _no_evidence(out):
    return (out["intent"] == "out_of_scope" and out["confidence"] == 0.167 and out["below_threshold"] is True
            and [t["intent"] for t in out["top"]] == ["out_of_scope", "dispute_unrecognized_charge",
                                                      "dispute_incorrect_charge_or_fee"])


@pytest.mark.parametrize("value", ["", "   ", "\n\t", None, _BadStr()] + list(NO_FEATURE_INPUTS.values()),
                         ids=["empty", "spaces", "newline_tab", "none", "bad_str"] + list(NO_FEATURE_INPUTS))
def test_no_evidence_answer(clf, value):
    assert _no_evidence(clf(value))


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_no_feature_inputs_have_an_all_zero_vector_and_no_evidence(artifacts, kind):
    clf = runtime.IntentClassifier(artifacts[kind])
    for text in NO_FEATURE_INPUTS.values():
        normalized = data.normalize_text(text)
        assert normalized, "these inputs survive normalization; only the features are empty"
        x = [normalized]
        for step in clf.text_steps:
            x = step.transform(x)
        assert runtime.no_feature_rows(x).tolist() == [True]
        if clf.pipeline is not None:
            x, _ = runtime.features_and_scores(clf.pipeline, clf.score_method, [normalized])
            assert runtime.no_feature_rows(x).tolist() == [True]
        # no keyword rule group fires either; the router's fallback one-hot is not evidence
        assert not runtime.keyword_groups_fired(runtime.keyword_features([data.keyword_text(text)]))[0]
        assert _no_evidence(clf(text))


class _ZeroText:
    """A text step under which no TF-IDF feature ever fires."""
    def __init__(self, width):
        self.width = width

    def transform(self, texts):
        return runtime.sparse.csr_matrix((len(texts), self.width))


@pytest.mark.parametrize("kind", V2_KINDS)
def test_a_keyword_rule_alone_is_evidence(artifacts, kind):
    clf = runtime.IntentClassifier(artifacts[kind])
    clf.text_steps = [_ZeroText(clf.text_steps[0].transform(["x"]).shape[1])]
    assert runtime.keyword_groups_fired(runtime.keyword_features(["bloqueo", "hola"])).tolist() == [True, False]
    _, no_evidence = clf.evidence_proba(["bloqueo", "hola"])
    # with keyword features a fired rule group is evidence; the router's fallback answer ("hola") is not
    assert no_evidence.tolist() == ([True, True] if kind == "v2_text" else [False, True])
    assert _no_evidence(clf("bloqueo")) is (kind == "v2_text") and _no_evidence(clf("hola"))


@pytest.mark.parametrize("kind", ["logreg"] + list(V2_KINDS))
def test_batch_path_agrees_with_calls(artifacts, kind):
    clf = runtime.IntentClassifier(artifacts[kind])
    texts = (["", "   ", None, "no reconozco un cargo de 80", "roubaram meu cartão ontem", "quiero un crédito"]
             + list(NO_FEATURE_INPUTS.values()))
    proba, results = clf.batch(texts)
    keys = ("intent", "confidence", "top", "below_threshold", "threshold", "model_version")
    for text, row, res in zip(texts, proba, results):
        call = clf(text)
        assert {k: res[k] for k in keys} == {k: call[k] for k in keys}, text
        if _no_evidence(call):
            np.testing.assert_allclose(row, 1.0 / len(data.CLASSES))
    np.testing.assert_array_equal(clf.proba(texts), proba)
    _, no_evidence = clf.evidence_proba(texts)
    assert no_evidence.tolist() == [True, True, True, False, False, False] + [True] * len(NO_FEATURE_INPUTS)


@pytest.fixture()
def fresh_failure_log(monkeypatch):
    monkeypatch.setattr(runtime, "_failure_logged", False)


@pytest.mark.parametrize("kind", ["logreg", "v2_hybrid"])
def test_scoring_failure_falls_back_and_is_logged_once(artifacts, caplog, fresh_failure_log, kind):
    clf = runtime.IntentClassifier(artifacts[kind])
    clf.model = None             # any exception inside scoring
    with caplog.at_level(logging.WARNING, logger=runtime.__name__):
        outs = [clf("no reconozco un cargo"), clf("perdi meu cartão")]
    for out in outs:
        _check_format(out, clf.threshold)
        assert _no_evidence(out)
    errors = [r for r in caplog.records if r.name == runtime.__name__ and r.levelno == logging.ERROR]
    assert len(errors) == 1 and errors[0].exc_info is not None
    assert "no reconozco" not in caplog.text            # the customer's message is never logged


def test_non_finite_scores_are_a_logged_failure(artifacts, caplog, fresh_failure_log):
    clf = runtime.IntentClassifier(artifacts["logreg"])
    clf.temperature = float("nan")
    with caplog.at_level(logging.WARNING, logger=runtime.__name__):
        assert _no_evidence(clf("no reconozco un cargo"))
    assert [r.levelno for r in caplog.records if r.name == runtime.__name__] == [logging.ERROR]


def test_load_check_rejects_a_model_that_cannot_score(artifacts):
    with pytest.raises(ValueError):
        runtime.IntentClassifier(dict(artifacts["logreg"], score_method="predict_nothing"))
    with pytest.raises(ValueError):
        runtime.IntentClassifier(dict(artifacts["logreg"], temperature=float("nan")))
    with pytest.raises(AttributeError):
        runtime.IntentClassifier(dict(artifacts["logreg"], pipeline=object()))
    with pytest.raises(AttributeError):
        runtime.IntentClassifier(dict(artifacts["v2_hybrid"], model=object()))
    with pytest.raises(ValueError):
        runtime.IntentClassifier(dict(artifacts["v2_text"], temperature=float("nan")))
    with pytest.raises(ValueError):
        runtime.IntentClassifier(dict(artifacts["v2_text"], format=3))


def test_keyword_rules_must_match_training(artifacts, monkeypatch):
    for kind in ("v2_hybrid", "v2_fusion"):
        with pytest.raises(ValueError):
            runtime.IntentClassifier(dict(artifacts[kind], keyword_rules="0" * 64))
    monkeypatch.setattr(runtime, "KEYWORD_RULES_ID", "changed")         # keywords.py edited after training
    for kind in ("v2_hybrid", "v2_fusion"):
        with pytest.raises(ValueError):
            runtime.IntentClassifier(artifacts[kind])
    runtime.IntentClassifier(artifacts["v2_text"])                       # no keyword features: still loads
    runtime.IntentClassifier(artifacts["logreg"])


@pytest.mark.parametrize("kind", V2_KINDS)
def test_v2_runtime_matches_the_training_feature_path(artifacts, kind):
    art = artifacts[kind]
    clf = runtime.IntentClassifier(art)
    texts = [r["text"] for r in toy_intents.rows()] + ["me cobraron 2 veces 120 en taxi seguro"]
    x = runtime.design_matrix(art["text_features"].transform([data.normalize_text(t) for t in texts]),
                              runtime.keyword_features([data.keyword_text(t) for t in texts]), art["keyword_weight"])
    p = runtime.softmax(runtime.model_scores(art["model"], art["score_method"], x), art["temperature"])
    p = runtime.fuse(p, runtime.keyword_features([data.keyword_text(t) for t in texts]), art["keyword_mix"])
    np.testing.assert_array_equal(clf.proba(texts), p)
    assert clf.uses_keywords is (kind != "v2_text")


def test_late_fusion_mixes_in_the_router(artifacts):
    text = "me cobraron dos veces 120 en taxi seguro"
    base = runtime.IntentClassifier(artifacts["v2_text"]).proba([text])[0]
    fused = runtime.IntentClassifier(artifacts["v2_fusion"]).proba([text])[0]
    onehot = np.array([c == keywords.predict(text) for c in data.CLASSES], dtype=float)
    np.testing.assert_allclose(fused, 0.7 * base + 0.3 * onehot)


def test_sklearn_version_is_stored_and_checked(artifacts, caplog):
    assert artifacts["logreg"]["sklearn_version"] == sklearn.__version__
    with caplog.at_level(logging.WARNING, logger=runtime.__name__):
        runtime.IntentClassifier(artifacts["logreg"])
    assert not caplog.records
    with caplog.at_level(logging.WARNING, logger=runtime.__name__):
        clf = runtime.IntentClassifier(dict(artifacts["logreg"], sklearn_version="0.0.1"))
    assert clf.sklearn_version == "0.0.1"
    assert [r.levelno for r in caplog.records] == [logging.WARNING] and "0.0.1" in caplog.text
    caplog.clear()
    legacy = {k: v for k, v in artifacts["logreg"].items() if k != "sklearn_version"}
    with caplog.at_level(logging.WARNING, logger=runtime.__name__):
        runtime.IntentClassifier(legacy)
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_normalization_matches_training(clf):
    assert data.normalize_text("  Me   cobraron $1.250,00\n el 12/03 ") == "me cobraron $0.0,0 el 0/0"
    assert data.normalize_text("ＣＡＲＧＯ") == "cargo"                      # NFKC
    assert data.normalize_text("Número ٣٤") == "número 0"                     # any Unicode digit run
    assert len(data.normalize_text("a " * 10_000)) == data.MAX_CHARS

    def strip(out):
        return {k: v for k, v in out.items() if k != "latency_ms"}
    assert strip(clf("No reconozco un cargo de 120 en Cafe Luna")) == \
        strip(clf("no   reconozco un cargo de 98765 en CAFE LUNA"))
    p_runtime = clf.proba(["Me cobraron dos veces 120 en taxi seguro"])
    p_train_path = runtime.softmax(
        runtime.raw_scores(clf.pipeline, clf.score_method, [data.normalize_text("me cobraron dos veces 0 en taxi seguro")]),
        clf.temperature)
    np.testing.assert_allclose(p_runtime, p_train_path)


def test_probabilities_are_calibrated_softmax(clf):
    p = clf.proba(["no reconozco un cargo", "perdi meu cartão"])
    assert p.shape == (2, len(data.CLASSES))
    np.testing.assert_allclose(p.sum(axis=1), 1.0)


@pytest.mark.parametrize("kind", ["logreg", "v2_hybrid"])
def test_deterministic_and_thread_safe(artifacts, kind):
    clf = runtime.IntentClassifier(artifacts[kind])
    texts = [r["text"] for r in toy_intents.rows()] * 3

    def strip(out):
        return {k: v for k, v in out.items() if k != "latency_ms"}
    expected = [strip(clf(t)) for t in texts]
    assert expected == [strip(clf(t)) for t in texts]
    with ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(lambda t: strip(clf(t)), texts))
    assert got == expected


def test_load_roundtrip_and_missing_default(artifacts, tmp_path, caplog):
    model_dir = tmp_path / "intent_classifier"
    model_dir.mkdir()
    joblib.dump(artifacts["linear_svc"], model_dir / runtime.MODEL_FILE)
    loaded = runtime.load_default(str(model_dir))
    direct = runtime.IntentClassifier(artifacts["linear_svc"])
    text = "a tarifa de 30 da loja veio errada"
    assert {k: v for k, v in loaded(text).items() if k != "latency_ms"} == \
        {k: v for k, v in direct(text).items() if k != "latency_ms"}
    assert runtime.IntentClassifier.load(str(model_dir / runtime.MODEL_FILE)).model_version == "intent-clf-toy"
    with caplog.at_level(logging.WARNING, logger=runtime.__name__):
        assert runtime.load_default(str(tmp_path / "missing")) is None
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_load_default_never_raises_but_load_stays_strict(artifacts, tmp_path, caplog):
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    (unreadable / runtime.MODEL_FILE).write_bytes(b"not a joblib file")
    incompatible = tmp_path / "incompatible"
    incompatible.mkdir()
    joblib.dump(dict(artifacts["logreg"], normalization_id="something-else"), incompatible / runtime.MODEL_FILE)
    broken = tmp_path / "broken"
    broken.mkdir()
    joblib.dump(dict(artifacts["logreg"], score_method="predict_nothing"), broken / runtime.MODEL_FILE)
    old_rules = tmp_path / "old_rules"
    old_rules.mkdir()
    joblib.dump(dict(artifacts["v2_hybrid"], keyword_rules="0" * 64), old_rules / runtime.MODEL_FILE)
    for folder in (unreadable, incompatible, broken, old_rules):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=runtime.__name__):
            assert runtime.load_default(str(folder)) is None
        assert [r.levelno for r in caplog.records] == [logging.WARNING], folder.name
        with pytest.raises(Exception):
            runtime.IntentClassifier.load(str(folder))


def test_rejects_artifact_with_other_normalization(artifacts):
    bad = dict(artifacts["logreg"], normalization_id="something-else")
    with pytest.raises(ValueError):
        runtime.IntentClassifier(bad)
    with pytest.raises(ValueError):
        runtime.IntentClassifier(dict(artifacts["logreg"], classes=list(reversed(data.CLASSES))))
