"""Tiny ES/PT toy rows in the intent dataset format, for classifier tests that must not depend on data/ or models/."""
import json

TEMPLATES = {
    "dispute_unrecognized_charge": {
        "es": ["no reconozco un cargo de {n} en {m}", "hay una compra de {n} en {m} que yo no hice"],
        "pt": ["não reconheço uma compra de {n} em {m}", "tem uma cobrança de {n} na {m} que eu não fiz"],
    },
    "dispute_incorrect_charge_or_fee": {
        "es": ["me cobraron dos veces {n} en {m}", "la comisión de {n} de {m} está mal cobrada"],
        "pt": ["fui cobrado duas vezes {n} na {m}", "a tarifa de {n} da {m} veio errada"],
    },
    "account_payment_inquiry": {
        "es": ["cuál es el saldo de mi cuenta después del pago en {m}", "quiero ver mis movimientos de {m}"],
        "pt": ["qual é o saldo da minha conta depois do pagamento na {m}", "quero ver meus extratos da {m}"],
    },
    "card_lost_or_block": {
        "es": ["perdí mi tarjeta en {m}, bloquéenla", "me robaron la tarjeta saliendo de {m}"],
        "pt": ["perdi meu cartão na {m}, quero bloquear", "roubaram meu cartão saindo da {m}"],
    },
    "other_complaint": {
        "es": ["el cajero de la sucursal {m} no funciona", "la atención en la sucursal {m} fue pésima"],
        "pt": ["o caixa eletrônico da agência {m} não funciona", "o atendimento na agência {m} foi péssimo"],
    },
    "out_of_scope": {
        "es": ["quiero un crédito hipotecario de {n} cerca de {m}", "cómo abro una cuenta de inversión con {n}"],
        "pt": ["quero simular um financiamento de {n} perto da {m}", "como abro uma conta de investimento com {n}"],
    },
}
MERCHANTS = ["super ahorro", "farmacia central", "cafe luna", "taxi seguro", "libreria sol", "posto azul"]
AMOUNTS = ["120", "250,50", "99", "1.500", "37", "4.210,00"]
VARIANTS = {"es": "es-MX", "pt": "pt-BR"}


def _row(idx, text, lang, intent, split, family, acceptable=None):
    acceptable = acceptable or [intent]
    return {"id": f"toy-{idx:04d}", "text": text, "language": lang, "variant": VARIANTS[lang], "intent": intent,
            "acceptable_intents": acceptable, "is_ambiguous": len(acceptable) > 1, "attack_type": None,
            "family_id": f"toy-{lang}-{family}", "split": split}


def rows(languages=("es", "pt"), test_languages=None):
    """Train rows (4 per template), dev rows (1 per template plus one ambiguous row per language) and, when
    test_languages is given, test rows for those languages. A family is one intent, language and merchant, so every
    class has five families per language and grouped folds keep every class in their training rows."""
    out = []
    for intent, by_lang in TEMPLATES.items():
        for lang in languages:
            for template in by_lang[lang]:
                for k in range(5):
                    split = "train" if k < 4 else "dev"
                    out.append(_row(len(out), template.format(n=AMOUNTS[k], m=MERCHANTS[k]), lang, intent, split,
                                    f"{intent}-{k}"))
    for lang in languages:
        text = {"es": "perdí la tarjeta y hay un cargo que no hice en {m}",
                "pt": "perdi o cartão e tem uma compra que não fiz na {m}"}[lang].format(m=MERCHANTS[5])
        out.append(_row(len(out), text, lang, "card_lost_or_block", "dev", "ambiguous",
                        ["card_lost_or_block", "dispute_unrecognized_charge"]))
    for lang in test_languages or ():
        for intent, by_lang in TEMPLATES.items():
            text = by_lang[lang][0].format(n=AMOUNTS[5], m=MERCHANTS[5])
            out.append(_row(len(out), "TEST ROW " + text, lang, "out_of_scope", "test", "test"))
    return out


def write_jsonl(path, items):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    return str(path)
