"""Spanish or Portuguese? A small function-word scorer for the agent's replies (the answer_in_wrong_language check).

    from src.agent_eval.language import detect
    detect("Ya registré tu reclamo.")          # {"language": "es", "es": 4.0, "pt": 0.0, "margin": 4.0}

    python -m src.agent_eval.language          # accuracy on texts of known language (generated dev data only)

Each token that is a Spanish-only or Portuguese-only function word (articles, contractions, pronouns, frequent verbs
and adverbs) adds its weight to that language; letters and endings that exist in one language only (ñ, ¿, ¡; ã, õ, ç,
ê, ô, â, à, nh, lh, -ção / -ción) add too. Words both languages share (que, de, para, por, se, me, compra, valor...)
count for neither. The larger score wins; a tie, including no evidence at all, is "unknown", which the scorer never
counts as a violation. Service ids, numbers and URLs are removed first, so a reply full of ids is judged on its words.

The validation command scores texts whose language is known: the dev-split customer turns of the e2e scenarios (by
variant, with the portunhol rows apart, whose label is the dominant language), the dev split of the intent dataset,
and reply-like bank text (the get_policy_info snippets and the decline-code explanations, ES and PT columns). It is
a heuristic: it reads words, not meaning, and short or mixed messages can go either way.
"""
import argparse
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCENARIOS = os.path.join(REPO, "data", "scenarios", "e2e_scenarios.jsonl")
INTENT_DATASET = os.path.join(REPO, "data", "scenarios", "intent_dataset.jsonl")
POLICY_SNIPPETS = os.path.join(REPO, "src", "bank_tools", "policy_snippets.json")
DECLINE_CODES = os.path.join(REPO, "data", "bank_tools", "reference", "decline_codes.json")

# Weight 2: words that exist in one language only. Weight 1: words that are mostly one language's, or that the other
# language uses only rarely. Portuguese "o" (also Spanish "or") gets 0.5.
ES_STRONG = ("el", "los", "las", "del", "al", "y", "una", "unos", "unas", "pero", "muy", "con", "usted", "ustedes",
             "hay", "ya", "aquí", "ahora", "hoy", "ayer", "también", "gracias", "hola", "tarjeta", "cuenta", "dinero",
             "más", "después", "nosotros", "ella", "ellos", "número", "ayudar", "ayuda", "en", "un", "su", "sus",
             "lo", "le", "les", "mi", "mis", "tú", "tus", "estás", "estoy", "puedo", "puedes", "pueden", "necesito",
             "quiero", "tengo", "tienes", "tiene", "hice", "hizo", "fue", "sí", "eso", "esto", "ese", "esa", "hemos",
             "he", "ha", "han", "hasta", "entonces", "nuestro", "nuestra", "respuesta", "fecha", "monto", "días",
             "cuál", "cuándo", "dónde", "donde", "cuando", "mucho", "soy", "eres", "hacer", "voy", "vos", "podés",
             "querés", "tenés", "sos", "confirmás", "revisá", "registré", "creé")
ES_WEAK = ("es", "la", "tu", "te", "solo", "sólo", "bueno", "listo", "nada", "ningún", "ninguna", "él", "cobro",
           "cargo",
           "reclamo", "especialista", "asesor", "agente", "creado", "están", "puede", "saldo", "favor", "caso",
           "dentro", "claro", "revisa", "revise", "confirmes")
PT_STRONG = ("os", "do", "da", "dos", "das", "na", "nas", "num", "numa", "um", "uma", "uns", "umas", "em", "com",
             "seu", "sua", "seus", "suas", "meu", "minha", "meus", "minhas", "teu", "tua", "nosso", "nossa", "isso",
             "isto", "esse", "essa", "esses", "essas", "foi", "são", "é", "já", "também", "obrigado", "obrigada",
             "olá", "pelo", "pela", "pelos", "pelas", "ao", "aos", "à", "às", "muito", "mais", "onde", "então",
             "ainda", "posso", "vou", "estou", "tenho", "cartão", "cobrança", "cobranças", "não", "nao", "você",
             "voce", "vocês", "depois", "lhe", "lhes", "atendente", "dinheiro", "há", "lá", "hoje", "ontem", "agora",
             "aqui", "sim", "pode", "podem", "tem", "fiz", "fez", "vai", "conta", "nenhum", "nenhuma", "mesmo", "nós",
             "gostaria", "preciso", "quero", "ajudar", "ajuda", "pra", "pro", "resposta", "até", "registrei",
             "encaminhei", "encaminhado", "transferi", "chamado", "reclamação", "solicitação", "qual", "quando")
PT_WEAK = ("e", "oi", "ele", "eles", "elas", "dias", "data", "valor", "certo", "criado", "só", "estão", "tá", "tô",
           "né")
ES_WORDS = {**dict.fromkeys(ES_WEAK, 1.0), **dict.fromkeys(ES_STRONG, 2.0)}
PT_WORDS = {"o": 0.5, **dict.fromkeys(PT_WEAK, 1.0), **dict.fromkeys(PT_STRONG, 2.0)}
# A word listed for both languages counts for neither.
SHARED = set(ES_WORDS) & set(PT_WORDS)
for _w in SHARED:
    ES_WORDS.pop(_w)
    PT_WORDS.pop(_w)
ES_CHARS = {"ñ": 3.0, "¿": 3.0, "¡": 3.0}
PT_CHARS = {"ã": 2.0, "õ": 2.0, "ç": 2.0, "ê": 2.0, "ô": 2.0, "â": 2.0, "à": 1.0}
ES_SUFFIXES = (("ción", 2.0), ("ciones", 2.0), ("ll", 1.0))
PT_SUFFIXES = (("ção", 2.0), ("ções", 2.0), ("ões", 1.0), ("nh", 1.0), ("lh", 1.0))
NOISE = re.compile(r"\b(?:TRX|PRD|DSP|HND|CNF|CLI|CHL)-[A-Za-z0-9.]+|https?://\S+|\btc_[0-9a-f]+|[0-9][0-9.,:/-]*")
TOKEN = re.compile(r"[a-záéíóúüñâêôãõçà]+")
MAX_CHAR_SCORE = 6.0  # one accented word repeated many times should not outweigh the words


def _fold_spaces(text):
    return unicodedata.normalize("NFC", text or "").lower()


def scores(text):
    """(es, pt) evidence of a text."""
    text = NOISE.sub(" ", _fold_spaces(text))
    tokens = TOKEN.findall(text)
    es = sum(ES_WORDS.get(t, 0) for t in tokens)
    pt = sum(PT_WORDS.get(t, 0) for t in tokens)
    es += min(MAX_CHAR_SCORE, sum(w * text.count(c) for c, w in ES_CHARS.items()))
    pt += min(MAX_CHAR_SCORE, sum(w * text.count(c) for c, w in PT_CHARS.items()))
    for t in tokens:
        for part, w in ES_SUFFIXES:
            if (part == "ll" and part in t) or (part != "ll" and t.endswith(part)):
                es += w
                break
        for part, w in PT_SUFFIXES:
            if (part in ("nh", "lh") and part in t) or (part not in ("nh", "lh") and t.endswith(part)):
                pt += w
                break
    return float(es), float(pt)


def detect(text):
    """{"language": "es" | "pt" | "unknown", "es", "pt", "margin"}."""
    es, pt = scores(text)
    lang = "es" if es > pt else "pt" if pt > es else "unknown"
    return {"language": lang, "es": es, "pt": pt, "margin": abs(es - pt)}


# -- validation ---------------------------------------------------------------------------------------------------
def _jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def validation_sets(scenarios_path=SCENARIOS, intent_path=INTENT_DATASET, split="dev"):
    """{set name: [(text, language)]} of texts whose language is known; only the given split of generated data."""
    sets = defaultdict(list)
    if os.path.exists(scenarios_path):
        for sc in _jsonl(scenarios_path):
            if sc.get("split") != split:
                continue
            name = "e2e_turns_mixed" if sc.get("variant") == "mixed" else "e2e_turns"
            for t in sc["turns"]:
                sets[name].append((t["text"], sc["language"]))
    if os.path.exists(intent_path):
        for r in _jsonl(intent_path):
            if r.get("split") == split:
                name = "intent_messages_mixed" if r.get("variant") == "mixed" else "intent_messages"
                sets[name].append((r["text"], r["language"]))
    if os.path.exists(POLICY_SNIPPETS):
        with open(POLICY_SNIPPETS, encoding="utf-8") as fh:
            doc = json.load(fh)

        def walk(node, lang=None):
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, k if k in ("es", "pt") else lang)
            elif isinstance(node, list):
                for v in node:
                    walk(v, lang)
            elif isinstance(node, str) and lang and len(node.split()) >= 4:
                sets["policy_snippets"].append((re.sub(r"\{[^}]*\}", " ", node), lang))
        walk(doc.get("topics", {}))
    if os.path.exists(DECLINE_CODES):
        with open(DECLINE_CODES, encoding="utf-8") as fh:
            for row in json.load(fh):
                for lang in ("es", "pt"):
                    for col in ("explanation_", "non_card_explanation_"):
                        if row.get(col + lang):
                            sets["decline_explanations"].append((row[col + lang], lang))
    return dict(sets)


def accuracy(pairs):
    """{n, correct, unknown, wrong, accuracy}: unknown counts as not correct."""
    c = Counter()
    for text, lang in pairs:
        got = detect(text)["language"]
        c["correct" if got == lang else "unknown" if got == "unknown" else "wrong"] += 1
    n = len(pairs)
    return {"n": n, "correct": c["correct"], "unknown": c["unknown"], "wrong": c["wrong"],
            "accuracy": round(c["correct"] / n, 4) if n else None}


def validate(split="dev"):
    sets = validation_sets(split=split)
    out = {}
    for name in sorted(sets):
        out[name] = {"all": accuracy(sets[name])}
        for lang in ("es", "pt"):
            out[name][lang] = accuracy([p for p in sets[name] if p[1] == lang])
    return out


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Accuracy of the ES/PT reply-language scorer on texts of known language.")
    ap.add_argument("--split", default="dev", choices=("dev", "train"), help="split of the generated data to read")
    ap.add_argument("-v", "--verbose", action="store_true", help="print the misread texts")
    args = ap.parse_args(argv)
    result = validate(args.split)
    print(f"{'set':24} {'lang':4} {'n':>6} {'correct':>8} {'unknown':>8} {'wrong':>6} {'accuracy':>9}")
    for name, by in result.items():
        for lang in ("all", "es", "pt"):
            r = by[lang]
            if r["n"]:
                print(f"{name:24} {lang:4} {r['n']:>6} {r['correct']:>8} {r['unknown']:>8} {r['wrong']:>6} "
                      f"{r['accuracy']:>9.3f}")
    if args.verbose:
        for name, pairs in validation_sets(split=args.split).items():
            for text, lang in pairs:
                got = detect(text)
                if got["language"] != lang:
                    print(f"  [{name}] {lang} -> {got['language']} ({got['es']:.0f}/{got['pt']:.0f}): {text[:140]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
