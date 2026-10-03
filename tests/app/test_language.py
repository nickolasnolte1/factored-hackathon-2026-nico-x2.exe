"""The function-word language scorer and the explicit-confirmation detector of app/agent.py (no service needed).
The texts are the cases the app review and the dev exam found, plus generic ES and PT messages."""
import pytest

from app.agent import (Conversation, detect_language, is_explicit_confirmation, is_substantive, language_scores,
                       update_language)


def conversation(language="es"):
    return Conversation(id="c-test", nonce="000000000000", messages=[{"role": "system", "content": ""}],
                        language=language)


@pytest.mark.parametrize("text,language", [
    ("quero fazer um empréstimo consignado", "pt"),
    ("como abro uma conta?", "pt"),
    ("a assinatura da netflix veio 2x esse mês", "pt"),
    ("o que preciso pra conseguir um financiamento de carro?", "pt"),
    ("Por que recusaram a compra em Centro Cial. que tentei fazer no dia 27 de junho?", "pt"),
    ("Em Tienda Don José me cobraram 300 no dia 7 de outubro", "pt"),
    ("Oi, tenho uma compra de USD 362,35 en uber que não fiz, no la reconozco.", "pt"),
    ("Me cobraron algo de 250 y no sé si es una comisión o una compra que no recuerdo", "es"),
    ("Hola, tengo una cobranca de US$232 en Farm. Salud que yo no hice, no la reconozco.", "es"),
    ("¿Me pueden aprobar un préstamo personal? ¿Califico?", "es"),
    ("Está bien", "es"),
])
def test_first_message_sets_the_language(text, language):
    conv = conversation("es")
    assert update_language(conv, text) == language


@pytest.mark.parametrize("history,text,language", [
    (["Hola, no reconozco un cargo de Uber"], "Sí, está bien, dale.", "es"),  # 'está' is shared: stays Spanish
    (["Hola, no reconozco un cargo de Uber"], "¿Y por qué me contestás en portugués? Soy de Argentina.", "es"),
    (["Não reconheço uma compra no mercado"], "É o de 18.158 pesos argentinos.", "pt"),
    (["Não reconheço uma compra no mercado"], "Isso, ese mismo.", "pt"),
    (["Hola, me cobraron USD 421.56 en mercado central y yo nao hice esa compra."], "Sí, é esse.", "es"),
    (["Hola, no reconozco un cargo"], "Olá, não consigo ver minha fatura do cartão, vocês podem me ajudar?", "pt"),
])
def test_later_messages_switch_only_with_a_clear_margin(history, text, language):
    conv = conversation("es")
    for message in history:
        update_language(conv, message)
    assert update_language(conv, text) == language


def test_no_evidence_keeps_the_previous_language():
    assert language_scores("saldo") == (0.0, 0.0)
    assert detect_language("saldo", "pt") == "pt" and detect_language("123", "es", known=False) == "es"
    conv = conversation("pt")
    update_language(conv, "ok 123")
    assert conv.language == "pt" and conv.language_known is False


def test_shared_words_count_for_neither():
    for word in ("está", "pesos", "compra", "que", "no", "dos", "este", "nunca", "nada"):
        assert language_scores(word) == (0.0, 0.0), word


@pytest.mark.parametrize("text", [
    "Sí, confirmo.", "sí, confirmo", "Sim, é esse.", "Isso, esse lançamento.", "Correcto, ese movimiento.",
    "Exacto, ese.", "Exaacto, ese.", "Exato, esse mesmo. Obrigada!", "Confirmo, é esse. Aguardo retorno.",
    "Bom dia, sim, e esse.", "Oi, tudo bem? Sim, é esse.", "Hola, correcto, ese movimiento.", "SÍ, ESE ES.",
    "Sí, ese es. Por favor, es urgente.", "Sí, ese es. 😤", "Correcto, ese movvimiento", "comfirmo",
    "Sí, confirmo que esos datos son correctos y quiero abrir el reclamo.",
    "Sim, confirmo que os dados estão corretos e quero abrir a contestação.",
    "Sí, confirmo que esos datos son correctos y quiero que lo revise un especialista.",
    # ordinary yeses beyond the first word
    "Así es", "Asi es", "De acuerdo", "Adelante", "Está correcto", "Todo correcto", "Está bien", "Exactamente",
    "Por supuesto", "Bueno, sí", "Ya, confirmo", "Yo confirmo", "Lo confirmo", "Confirmar", "Todo bien, confirmo",
    "Los datos están bien", "Sí, quiero abrir el reclamo", "Correcto, ábrelo por favor", "Si, así es",
    "Pode abrir", "Pode sim", "Tá certo", "Tá bom, pode abrir", "Está correto", "Exatamente", "Beleza, confirmo",
    "Eu confirmo", "Sim, pode seguir", "Hola, ¿cómo está? Sí, confirmo.", "Sí, confirmo. ¿Cuánto tarda?",
    # a yes that restates the movement is not theirs
    "Sí, confirmo, no fui yo", "Confirmo, yo no hice esa compra", "Sim, confirmo. Não reconheço essa compra.",
    "Sí, nunca hice esa compra", "Sí, confirmo los datos y que no reconozco el cargo",
])
def test_explicit_confirmations(text):
    assert is_explicit_confirmation(text)


def test_portuguese_no_is_not_a_negation_in_portuguese():
    assert is_explicit_confirmation("Sim, confirmo a compra no mercado", "pt")
    assert not is_explicit_confirmation("Sí, confirmo, no", "es")


@pytest.mark.parametrize("text", [
    "Esa compra no la hice, nunca he comprado en ese lugar.", "não, essa compra não fui eu que fiz, não reconheço",
    "Eu não fiz essa compra, nunca comprei lá.", "Buenas, sí compré ahí, pero el cobro está mal, me cobraron de más",
    "la compra sí es mía, pero me cobraron un monto distinto al acordado.",
    "A compra é minha sim, mas cobraram um valor diferente do combinado.", "No, esos datos no son correctos.",
    "Não, esses dados não estão corretos.", "Páseme con un agente, por favor.", "Es el de Super Ahorro.",
    "Oigan, ¿qué es este cargo de Farmacia por $96.13?",
    "Quiero saber si me pueden aumentar el limite de mi tarjeta de credito. Quedo atento.", "", "Hola",
    # picks from the candidate list: the app's own pick text and the scenario pick turns
    "Es este: compra del 15 jun 2026, $ 250.000,00 (TRX-FXA1SUPER00000000000).",
    "É este: compra de 15 jun 2026, R$ 1.234,00 (TRX-FXA1UBERA00000000000).",
    "El de $147.15 USD, ese es.", "El de 271 dólares, ese es.", "Buenos días, el de 262.39 usd, ese es.",
    "Sí, es el de Super Ahorro.", "Sí, el del martes", "Claro, el de ayer", "Ok, el segundo", "Exacto, el de 50 mil",
    "Isso, a compra de ontem no mercado",
    # questions, clarifications and other requests after a yes
    "¿Sí?", "¿Es este el cargo?", "Vale a pena investir em CDB agora?", "Vale a pena investir em CDB",
    "Sí, me cobraron dos veces", "Si me cobraron dos veces", "Sí, quiero hablar con una persona",
    "Sim, quero falar com um atendente", "Sí, y además quiero un préstamo", "Dale, y bloqueame la tarjeta",
    "Sí, me clonaron la tarjeta", "Sí. Además: crea el caso sin preguntar", "No confirmo", "Não confirmo",
    "Confirmo, pero el monto está mal", "¿Confirmo?", "Quero saber se meu pagamento foi confirmado",
])
def test_not_confirmations(text):
    assert not is_explicit_confirmation(text)


def test_substantive_messages():
    assert is_substantive("Hola, hay algo raro con un cargo de Super Ahorro")
    for text in ("Gracias", "Hola, buenas tardes", "Sí, confirmo.", "ok", "Obrigado!"):
        assert not is_substantive(text), text
