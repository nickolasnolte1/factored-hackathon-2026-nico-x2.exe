"""Surface rendering for scenario messages (ES / PT): amounts, dates, merchants, products, channels and text noise.

Every function takes an explicit `random.Random`, so the generator stays deterministic. Rendering never changes
the gold values: an amount is rendered from the transaction amount and returns the value the text states
(`stated`, which may be rounded), a date phrase returns the calendar date it denotes relative to `now`.
"""
import re
import unicodedata
from datetime import timedelta

COUNTRY_OF_VARIANT = {"es-MX": "MX", "es-CO": "CO", "es-AR": "AR"}
LOCAL_PESO = {"CO": "COP", "AR": "ARS"}

WEEKDAYS = {"es": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
            "pt": ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]}
MONTHS = {"es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
                 "noviembre", "diciembre"],
          "pt": ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
                 "novembro", "dezembro"]}
NUM_WORDS = {"es": {2: "dos", 3: "tres", 4: "cuatro", 5: "cinco", 6: "seis", 7: "siete", 8: "ocho", 9: "nueve", 10: "diez"},
             "pt": {2: "dois", 3: "três", 4: "quatro", 5: "cinco", 6: "seis", 7: "sete", 8: "oito", 9: "nove", 10: "dez"}}

# Partial names a customer would type; each must still match its merchant (and only it) under the policy matcher.
MERCHANT_PARTIAL = {
    "Super Ahorro": ["SuperAhorro", "super ahorro"],
    "Tienda Don José": ["Don José", "tienda don jose"],
    "Restaurante El Buen Sabor": ["El Buen Sabor", "Buen Sabor"],
    "Mercado Central": ["mercado central", "MercadoCentral"],
    "Empresa Telefónica": ["Telefónica", "Telefonica"],
    "Cable TV": ["CableTV", "cable tv"],
    "Servicios Públicos": ["Serv. Públicos", "servicios publicos"],
    "Internet Plus": ["InternetPlus", "internet plus"],
    "Estación de Servicio": ["Estación de Serv.", "estacion de servicio"],
    "Uber": ["UBER", "uber"],
    "Taxi Seguro": ["TaxiSeguro", "Taxi Seg."],
    "Ferretería": ["Ferreteria", "ferretería"],
    "Tienda General": ["Tda. General", "tienda general"],
    "Cine Premium": ["CinePremium", "cine premium"],
    "Boutique Moda": ["Boutique Moda", "boutique moda"],
    "Centro Comercial": ["Centro Cial.", "centro comercial"],
    "Conciertos Live": ["ConciertosLive", "Conciertos"],
    "Streaming Music": ["StreamingMusic", "Streaming"],
    "Teatro Nacional": ["Teatro Nal.", "teatro nacional"],
    "Gasolinera Express": ["Gasolinera Exp.", "Gasolinera"],
    "Clínica Médica": ["Clínica", "Clinica Medica"],
    "Farmacia Salud": ["Farm. Salud", "Farmacia"],
    "Laboratorio Central": ["Laboratorio Ctral.", "Laboratorio"],
    "Óptica Visión": ["Óptica", "Optica Vision"],
}

PRODUCT_ES = {
    "Credit Card": {"*": ["tarjeta de crédito"]},
    "Debit Card": {"*": ["tarjeta de débito"]},
    "Savings Account": {"es-MX": ["cuenta de ahorro"], "es-CO": ["cuenta de ahorros"], "es-AR": ["caja de ahorro"]},
    "Checking Account": {"es-MX": ["cuenta de cheques", "cuenta corriente"], "*": ["cuenta corriente"]},
    "Personal Loan": {"es-CO": ["crédito de libre inversión", "préstamo personal"],
                      "*": ["préstamo personal", "crédito personal"]},
    "Mortgage": {"*": ["crédito hipotecario", "hipoteca"]},
    "Investment": {"*": ["inversión"]},
    "Insurance": {"*": ["seguro"]},
}
PRODUCT_PT = {  # (noun phrase, feminine)
    "Credit Card": ("cartão de crédito", False), "Debit Card": ("cartão de débito", False),
    "Savings Account": ("conta poupança", True), "Checking Account": ("conta corrente", True),
    "Personal Loan": ("empréstimo pessoal", False), "Mortgage": ("financiamento imobiliário", False),
    "Investment": ("investimento", False), "Insurance": ("seguro", False),
}
CHANNEL_ES = {
    "ATM": {"*": ["el cajero", "el cajero automático"]},
    "App": {"*": ["la app", "la aplicación"]},
    "Branch": {"es-CO": ["la oficina", "la sucursal"], "*": ["la sucursal"]},
    "POS": {"es-MX": ["la terminal"], "es-CO": ["el datáfono"], "es-AR": ["el posnet"]},
    "Web": {"es-AR": ["home banking", "la web"], "*": ["la página web", "la banca en línea"]},
}
CHANNEL_PT = {"ATM": ["caixa eletrônico"], "App": ["app", "aplicativo"], "Branch": ["agência"],
              "POS": ["maquininha"], "Web": ["internet banking", "site"]}
TXN_TYPE = {
    "es": {"Purchase": "compra", "Withdrawal": "retiro", "Transfer": "transferencia", "Payment": "pago", "Deposit": "depósito"},
    "pt": {"Purchase": "compra", "Withdrawal": "saque", "Transfer": "transferência", "Payment": "pagamento", "Deposit": "depósito"},
}

GREETINGS = {"es": ["Hola, ", "Buenas, ", "Buenos días, ", "Buenas tardes. ", "Hola buenas, "],
             "pt": ["Oi, ", "Olá, ", "Bom dia, ", "Boa tarde. ", "Oi, tudo bem? "]}
GREETING_WORDS = {"hola", "buenas", "buen", "buenos", "che", "oiga", "oigan", "estimados", "oi", "olá", "ola", "bom",
                  "boa", "prezados", "prezado", "hello"}
CLOSINGS = {"es": [" Gracias.", " Gracias!", " Muchas gracias.", " Quedo atento.", " Saludos.", " Por favor, es urgente."],
            "pt": [" Obrigado.", " Obrigada!", " Valeu.", " Aguardo retorno.", " Por favor, é urgente."]}
ABBREVIATIONS = {"es": [("por favor", "porfa"), ("por favor", "x favor"), ("porque", "xq"), ("también", "tmb"),
                        ("que ", "q "), ("gracias", "grax"), ("para", "pa")],
                 "pt": [("você", "vc"), ("vocês", "vcs"), ("por favor", "pfv"), ("porque", "pq"), ("também", "tb"),
                        ("que ", "q "), ("não", "ñ")]}
EMOJIS = [" 😡", " 🙏", " 😰", " 😤", " 🙄"]


# --------------------------------------------------------------------------------------------------------------
# Text helpers


def strip_accents(text, keep_enye=False):
    out = []
    for ch in text:
        if keep_enye and ch in "ñÑ":
            out.append(ch)
            continue
        out.append(unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii") or ch)
    return "".join(out)


def normalize_key(text):
    """Key for duplicate detection: case, accents, punctuation and spacing do not make two messages different."""
    text = strip_accents(text.lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text)).strip()


def cap_first(text):
    for i, ch in enumerate(text):
        if ch.isalpha():
            return text[:i] + ch.upper() + text[i + 1:]
    return text


def typo(word, rng):
    """One keyboard-style edit: delete, duplicate or swap adjacent letters (never the first letter)."""
    if len(word) < 4:
        return word
    i = rng.randrange(1, len(word) - 1)
    op = rng.choice(("delete", "double", "swap"))
    if op == "delete":
        return word[:i] + word[i + 1:]
    if op == "double":
        return word[:i] + word[i] + word[i:]
    return word[:i] + word[i + 1] + word[i] + word[i + 2:]


# --------------------------------------------------------------------------------------------------------------
# Amounts


def _fmt(x, decimals, style):
    s = f"{x:,.{decimals}f}"
    if style == "en":
        return s
    if style == "plain_en":
        return s.replace(",", "")
    if style == "plain_es":
        return s.replace(",", "").replace(".", ",")
    if style == "apostrophe":  # Colombian 1'234.567
        es = s.replace(",", "#").replace(".", ",").replace("#", ".")
        parts = es.split(".")
        return parts[0] + "'" + ".".join(parts[1:]) if len(parts) > 2 else es
    return s.replace(",", "#").replace(".", ",").replace("#", ".")  # es


AMOUNT_STYLES = {  # (template, number style, explicit currency or None, special)
    ("es-MX", "USD"): [("{n} dólares", "en", "USD", None), ("USD {n}", "en", "USD", None), ("US${n}", "en", "USD", None),
                       ("${n} USD", "en", "USD", None), ("{n} dls", "en", "USD", None), ("${n}", "en", None, None),
                       ("{n} usd", "plain_en", "USD", None)],
    ("es-CO", "COP"): [("${n}", "es", None, None), ("$ {n}", "es", None, None), ("{n} pesos", "es", "COP", None),
                       ("COP {n}", "es", "COP", None), ("{n} pesos", "plain_es", "COP", None),
                       ("${n}", "apostrophe", None, "millions"), ("{k} mil pesos", None, "COP", "thousands"),
                       ("{m} millones de pesos", None, "COP", "millions_word")],
    ("es-CO", "USD"): [("USD {n}", "es", "USD", None), ("{n} dólares", "es", "USD", None), ("US$ {n}", "es", "USD", None),
                       ("{n} dólares", "en", "USD", None)],
    ("es-AR", "ARS"): [("${n}", "es", None, None), ("$ {n}", "es", None, None), ("{n} pesos", "es", "ARS", None),
                       ("ARS {n}", "es", "ARS", None), ("${n}", "plain_es", None, None),
                       ("{k} lucas", None, "ARS", "lucas"), ("{m} millones", None, "ARS", "millions_word")],
    ("es-AR", "USD"): [("U$S {n}", "es", "USD", None), ("USD {n}", "es", "USD", None), ("{n} dólares", "es", "USD", None),
                       ("u$s {n}", "es", "USD", None), ("{n} verdes", "es", "USD", None)],
    ("pt", "USD"): [("US$ {n}", "es", "USD", None), ("{n} dólares", "es", "USD", None), ("USD {n}", "es", "USD", None),
                    ("$ {n}", "es", None, None), ("{n} dólares", "plain_es", "USD", None)],
    ("pt", "COP"): [("COP {n}", "es", "COP", None), ("{n} pesos colombianos", "es", "COP", None),
                    ("{n} pesos", "es", None, None), ("$ {n}", "es", None, None), ("{k} mil pesos", None, None, "thousands")],
    ("pt", "ARS"): [("ARS {n}", "es", "ARS", None), ("{n} pesos argentinos", "es", "ARS", None),
                    ("{n} pesos", "es", None, None), ("$ {n}", "es", None, None), ("{k} mil pesos", None, None, "thousands")],
    ("pt", "BRL"): [("R$ {n}", "es", "BRL", None), ("{n} reais", "es", "BRL", None), ("R${n}", "es", "BRL", None)],
}


def _special_ok(special, v):
    return {None: True, "thousands": 10_000 <= v < 1_000_000, "lucas": 1_000 <= v < 1_000_000,
            "millions": v >= 1_000_000, "millions_word": v >= 1_000_000}[special]


def round_sig(v, digits=2):
    if v < 10:
        return round(v)
    mag = 10 ** (len(str(int(v))) - digits)
    return round(v / mag) * mag


def render_amount(value, currency, variant, rng, precise=False, tolerance_pct=1.0):
    """Render an amount the way a customer writes it. Returns (text, stated_value, explicit_currency, noise_tags).

    precise=True keeps the stated value within tolerance_pct of the true value (end-to-end scripts)."""
    value = float(value)
    key = ("pt" if variant.startswith("pt") or variant == "mixed" else variant, currency)
    styles = AMOUNT_STYLES[key]
    tags = []

    def within(s):
        return abs(s - value) <= value * tolerance_pct / 100.0

    r = rng.random()
    if precise:
        stated = round(value) if r < 0.4 and within(round(value)) else round(value, 2)
    else:
        p_exact, p_units = {"COP": (0.15, 0.55), "ARS": (0.35, 0.40)}.get(currency, (0.5, 0.3))
        if r < p_exact:
            stated = round(value, 2)
        elif r < p_exact + p_units:
            stated = round(value)
        else:
            stated = round_sig(value, 2 if value < 10_000 else 3)
            tags.append("amount_approx")
    if stated != round(value, 2) and "amount_approx" not in tags:
        tags.append("amount_no_cents")
    options = [s for s in styles if _special_ok(s[3], stated)]
    if precise:
        options = [s for s in options if s[3] in (None, "millions")]
    tmpl, style, explicit, special = options[rng.randrange(len(options))]
    if special in ("thousands", "lucas"):
        k = round(stated / 1000)
        text = tmpl.format(k=k)
        stated = k * 1000
        tags.append("amount_slang" if special == "lucas" else "amount_words")
    elif special == "millions_word":
        m = round(stated / 1e6, 1)
        text = tmpl.format(m=_fmt(m, 0 if m == int(m) else 1, "es"))
        stated = m * 1e6
        tags.append("amount_words")
    else:
        decimals = 0 if float(stated) == int(stated) else 2
        text = tmpl.format(n=_fmt(float(stated), decimals, style))
        tags.append(f"number_format:{style}")
    if explicit is None:
        tags.append("currency_implicit")
    if precise and not within(stated):
        raise AssertionError(f"precise amount {stated} outside tolerance of {value}")
    stated = round(float(stated), 2)
    return text, stated, explicit, tags


# --------------------------------------------------------------------------------------------------------------
# Dates


def render_date(event_date, now_date, lang, variant, rng):
    """Adverbial date phrase for event_date as seen on now_date. Returns (text, resolved_iso, tag)."""
    d, n = event_date, now_date
    delta = (n - d).days
    wd = WEEKDAYS[lang][d.weekday()]
    same_week = d.isocalendar()[:2] == n.isocalendar()[:2]
    prev_week = (n - timedelta(days=7)).isocalendar()[:2] == d.isocalendar()[:2]
    month = MONTHS[lang][d.month - 1]
    opts = []  # (weight, text, tag)
    if lang == "es":
        if delta == 1:
            opts.append((4, "ayer", "date:relative"))
        if delta == 2:
            opts.append((2, "antier" if variant in ("es-MX", "es-CO") else "anteayer", "date:relative"))
        if 2 <= delta <= 20:
            num = NUM_WORDS["es"].get(delta) if rng.random() < 0.5 else None
            opts.append((2, f"hace {num or delta} días", "date:relative"))
        if delta == 7:
            opts.append((2, "hace una semana", "date:relative"))
        if delta == 14:
            opts.append((2, "hace dos semanas", "date:relative"))
        if delta == 15:
            opts.append((1, "hace quince días", "date:relative"))
        if same_week and 2 <= delta <= 6:
            opts.append((2, f"el {wd}", "date:weekday"))
            opts.append((1, f"este {wd}", "date:weekday"))
        if prev_week and delta >= 2:
            opts.append((3, f"el {wd} pasado", "date:weekday"))
        opts.append((3, f"el {d.day} de {month}", "date:absolute_text"))
        opts.append((1, f"el {d.day:02d}/{d.month:02d}", "date:absolute_numeric"))
        opts.append((1, f"el {d.day:02d}/{d.month:02d}/{d.year}", "date:absolute_numeric"))
        if d.month == n.month and d.year == n.year:
            opts.append((1, f"el día {d.day}", "date:absolute_text"))
    else:
        fem = d.weekday() < 5
        if delta == 1:
            opts.append((4, "ontem", "date:relative"))
        if delta == 2:
            opts.append((2, "anteontem", "date:relative"))
        if 2 <= delta <= 20:
            num = NUM_WORDS["pt"].get(delta) if rng.random() < 0.4 else None
            opts.append((2, f"{rng.choice(['há', 'faz'])} {num or delta} dias", "date:relative"))
        if delta == 7:
            opts.append((2, "há uma semana", "date:relative"))
        if delta == 14:
            opts.append((2, "há duas semanas", "date:relative"))
        if delta == 15:
            opts.append((1, "há 15 dias", "date:relative"))
        if same_week and 2 <= delta <= 6:
            opts.append((2, f"{'na' if fem else 'no'} {wd}", "date:weekday"))
            opts.append((1, f"{'nesta' if fem else 'neste'} {wd}", "date:weekday"))
        if prev_week and delta >= 2:
            opts.append((3, f"{'na' if fem else 'no'} {wd} {'passada' if fem else 'passado'}", "date:weekday"))
        opts.append((3, f"no dia {d.day} de {month}", "date:absolute_text"))
        opts.append((1, f"dia {d.day:02d}/{d.month:02d}", "date:absolute_numeric"))
        opts.append((1, f"em {d.day:02d}/{d.month:02d}", "date:absolute_numeric"))
        opts.append((1, f"em {d.day:02d}/{d.month:02d}/{d.year}", "date:absolute_numeric"))
        if d.month == n.month and d.year == n.year:
            opts.append((1, f"no dia {d.day}", "date:absolute_text"))
    total = sum(w for w, _, _ in opts)
    x = rng.random() * total
    for w, text, tag in opts:
        x -= w
        if x < 0:
            return text, d.isoformat(), tag
    return opts[-1][1], d.isoformat(), opts[-1][2]


# --------------------------------------------------------------------------------------------------------------
# Merchants, products, channels


def render_merchant(name, rng, precise=False):
    """Returns (text, noise_tags). precise=True never introduces a typo."""
    r = rng.random()
    if r < 0.4:
        return name, []
    if r < 0.6:
        return name.lower(), ["merchant:lowercase"]
    if r < 0.85 or precise:
        return rng.choice(MERCHANT_PARTIAL.get(name, [name])), ["merchant:partial"]
    words = name.split()
    i = max(range(len(words)), key=lambda j: len(words[j]))
    words[i] = typo(words[i], rng)
    text = " ".join(words)
    return (text.lower() if rng.random() < 0.5 else text), ["merchant:typo"]


def _pick(table, variant, rng):
    opts = table.get(variant) or table.get("*")
    return rng.choice(opts)


def render_product(product_type_en, lang, variant, rng):
    if lang == "es":
        return _pick(PRODUCT_ES[product_type_en], variant, rng)
    return PRODUCT_PT[product_type_en][0]


def product_pt_forms(product_type_en):
    """Portuguese possessive forms: ({product_de}, {product_em}), e.g. 'da minha conta corrente'."""
    noun, fem = PRODUCT_PT[product_type_en]
    return (f"da minha {noun}", f"na minha {noun}") if fem else (f"do meu {noun}", f"no meu {noun}")


def render_channel(channel, lang, variant, rng):
    if channel not in CHANNEL_PT:
        return None
    if lang == "es":
        return _pick(CHANNEL_ES[channel], variant, rng)
    return rng.choice(CHANNEL_PT[channel])


def render_txn_type(transaction_type, lang, variant):
    word = TXN_TYPE[lang].get(transaction_type)
    if lang == "es" and transaction_type == "Withdrawal" and variant == "es-AR":
        return "extracción"
    return word


# --------------------------------------------------------------------------------------------------------------
# Templates and noise

SLOT_RE = re.compile(r"\{([a-z_]+)\}")


def fill(template, values):
    """Fill a template. Returns (parts, surfaces): parts = [(kind, text)] with kind 'lit' or a slot name.
    A slot at the start of a sentence is capitalized in the text (its surface keeps what the text shows)."""
    parts, pos = [], 0
    for m in SLOT_RE.finditer(template):
        if m.start() > pos:
            parts.append(("lit", template[pos:m.start()]))
        name = m.group(1)
        prev_lit = parts[-1][1] if parts and parts[-1][0] == "lit" else None
        at_start = not parts or (len(parts) == 1 and prev_lit is not None and not prev_lit.strip(" ¿¡\"("))
        text = values[name]
        if at_start or (prev_lit is not None and re.search(r"[.!?]\s+$", prev_lit)):
            text = cap_first(text)
        parts.append((name, text))
        pos = m.end()
    if pos < len(template):
        parts.append(("lit", template[pos:]))
    surfaces = {k: t for k, t in parts if k != "lit"}
    return parts, surfaces


def apply_noise(parts, lang, register, rng, level=1.0):
    """Apply customer-style noise. Typos and abbreviations touch literal text only, so slot surfaces stay
    recoverable; case and accent changes are applied to the surfaces too. Returns (text, surfaces, tags)."""
    tags = []
    informal = register == "informal"
    parts = [list(p) for p in parts]
    lits = [p for p in parts if p[0] == "lit"]
    if lits and rng.random() < 0.12 * level * (1.3 if informal else 0.8):
        words = [(p, w) for p in lits for w in re.findall(r"[A-Za-zÁÉÍÓÚáéíóúñÑãõçâêô]{5,}", p[1])]
        if words:
            p, w = rng.choice(words)
            p[1] = p[1].replace(w, typo(w, rng), 1)
            tags.append("typo")
    if lits and rng.random() < 0.10 * level * (1.6 if informal else 0.5):
        for src, dst in rng.sample(ABBREVIATIONS[lang], len(ABBREVIATIONS[lang])):
            hit = next((p for p in lits if src in p[1]), None)
            if hit:
                hit[1] = hit[1].replace(src, dst, 1)
                tags.append("abbreviation")
                break
    text = "".join(t for _, t in parts)
    surfaces = {k: t for k, t in parts if k != "lit"}
    first = re.match(r"\W*(\w+)", text)
    if rng.random() < 0.18 * level and not (first and first.group(1).lower() in GREETING_WORDS):
        g = rng.choice(GREETINGS[lang])
        m = re.match(r"[¿¡(\"]*(\w)(\w*)", text)
        first_slot = parts[0][0]
        if (m and not g.rstrip().endswith((".", "?", "!")) and m.group(1).isupper()
                and (m.group(2) == "" or m.group(2)[0].islower()) and first_slot != "merchant"):
            i = m.start(1)
            text = text[:i] + text[i].lower() + text[i + 1:]
            if first_slot != "lit" and surfaces.get(first_slot):
                surfaces[first_slot] = surfaces[first_slot][0].lower() + surfaces[first_slot][1:]
        text = g + text
        tags.append("greeting")
    if rng.random() < 0.15 * level:
        text = text.rstrip()
        if text and text[-1].isalnum():
            text += "."
        text += rng.choice(CLOSINGS[lang])
        tags.append("closing")
    r = rng.random()
    if r < 0.22 * level:
        text, surfaces = text.lower(), {k: v.lower() for k, v in surfaces.items()}
        tags.append("lowercase")
    elif r < 0.24 * level and len(text) < 140:
        text, surfaces = text.upper(), {k: v.upper() for k, v in surfaces.items()}
        tags.append("uppercase")
    if rng.random() < 0.22 * level * (1.3 if informal else 0.8):
        keep = lang == "es" and rng.random() < 0.5
        text, surfaces = strip_accents(text, keep), {k: strip_accents(v, keep) for k, v in surfaces.items()}
        tags.append("no_accents")
    if rng.random() < 0.20 * level * (1.3 if informal else 0.7):
        new = re.sub(r"[¿¡]", "", text)
        new = re.sub(r"[.!]$", "", new.rstrip())
        if new != text:
            text = new
            tags.append("no_punctuation")
    if rng.random() < 0.04 * level:
        text += rng.choice(EMOJIS)
        tags.append("emoji")
    return re.sub(r"\s{2,}", " ", text).strip(), surfaces, tags
