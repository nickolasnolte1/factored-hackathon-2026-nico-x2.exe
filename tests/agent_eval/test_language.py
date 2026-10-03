"""The ES/PT reply-language scorer."""
import pytest

from src.agent_eval import language


@pytest.mark.parametrize("text,lang", [
    ("Listo, registré tu reclamo. Te responderemos dentro de 24 horas.", "es"),
    ("¿Me confirmas que es este movimiento?", "es"),
    ("Vos podés confirmar el movimiento en la tarjeta que ves arriba.", "es"),
    ("Pronto, registrei sua reclamação. Você recebe uma resposta em até 24 horas.", "pt"),
    ("Você confirma que é essa compra no cartão?", "pt"),
    ("Não consigo ajudar com empréstimos por aqui, mas posso te encaminhar a um atendente.", "pt"),
])
def test_detects_spanish_and_portuguese(text, lang):
    assert language.detect(text)["language"] == lang


def test_no_evidence_is_unknown():
    assert language.detect("")["language"] == "unknown"
    assert language.detect("DSP-AAAAAAAAAAAA 250.000,00 COP TRX-ABCDEFGHIJ0123456789")["language"] == "unknown"


def test_ids_and_numbers_are_not_evidence():
    base = language.scores("Tu reclamo quedó registrado.")
    noisy = language.scores("Tu reclamo DSP-NAOAAAAAAAAA quedó registrado (250.000,00 COP).")
    assert base == noisy


def test_shared_words_count_for_neither():
    assert not set(language.ES_WORDS) & set(language.PT_WORDS)
    assert language.scores("que de para por se me") == (0.0, 0.0)


def test_accuracy_counts_unknown_as_not_correct():
    acc = language.accuracy([("Hola, gracias.", "es"), ("Obrigado!", "pt"), ("123", "es"), ("Gracias", "pt")])
    assert (acc["correct"], acc["unknown"], acc["wrong"], acc["accuracy"]) == (2, 1, 1, 0.5)
