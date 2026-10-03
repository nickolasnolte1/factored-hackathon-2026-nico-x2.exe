"""Untrusted-text wrapper, PII scrubber and audit argument redaction (CONTRACT.md section 6)."""
import copy
import hashlib
import re
import unicodedata

UNTRUSTED_MAX = 160
AUDIT_TEXT_MAX = 200
INSTRUCTION_MARKERS = ("instruc", "sistema", "system", "###", "nota para", "ignora", "olvida", "aprobar", "aprovar",
                       "reembols", "estorno", "devoluc", "sin validac", "sem valida", "no pedir confirm",
                       "sem pedir confirm", "marcar", "marque", "admin", "prompt")
MARKUP_CHARS = set("<>{}")

# Service ids are opaque surrogate keys and are never scrubbed.
SERVICE_ID = re.compile(r"CNF-[A-Z0-9]{12}\.[A-Za-z0-9_-]{22}|TRX-[A-Z0-9]{20}|PRD-[A-Z0-9]{12}|DSP-[A-Z0-9]{12}"
                        r"|HND-[A-Z0-9]{12}|CHL-[A-Z0-9]{12}|tc_[0-9a-f]{16}")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
CARD = re.compile(r"(?<![\w])(?:\d[ ./-]?){12,18}\d(?![\w])")
IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?!\d|\.\d)")
IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}(?![\w:])"
                  r"|(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){1,6}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,5})?(?![\w:])")
PHONE_PLUS = re.compile(r"\+\d[\d ().-]{7,}\d")
PHONE_GROUPED = re.compile(r"(?<![\w+.,])(?:\(\d{1,4}\)[ -]?)?\d{2,5}(?:[ .-]\d{2,5}){1,5}(?![\w])")
THOUSANDS = re.compile(r"\d{1,3}(?:\.\d{3})+")  # 400.000.000 is an amount, not a phone
CPF = re.compile(r"(?<![\w.])\d{3}\.\d{3}\.\d{3}-\d{2}(?![\w])")  # Brazilian CPF, recognizable without a keyword
CUSTOMER_ID = re.compile(r"\bCLI-[A-Z0-9]{12}\b", re.IGNORECASE)
PRODUCT_NUMBER = re.compile(r"\b(?:LOAN|INV|POL)-\d{4,}\b", re.IGNORECASE)
DOCUMENT = re.compile(r"(\b(?:DNI|CC|CE|pasaporte|passaporte|documento|c[eé]dula|CPF|RG)\b\s*"
                      r"(?:(?:n[°º.o]?|nro\.?|n[uú]mero|#|:|-|(?:es|é|e|era|do|da|de|del)\b)\s*)*)"
                      r"(?=[A-Za-z0-9.\-]*\d)[A-Za-z0-9][A-Za-z0-9.\-]{4,}",
                      re.IGNORECASE)
LONG_NUMBER = re.compile(r"(?<![\w.,])\d{7,}(?![\w]|[.,]\d)")


def fold(text):
    """Lower-case and strip accents."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def normalize_text(text):
    """NFKC (full-width and other compatibility forms become ASCII), format characters (zero-width, bidi
    controls, soft hyphen) removed and control characters made spaces, so formatting cannot hide a pattern."""
    out = []
    for c in unicodedata.normalize("NFKC", str(text)):
        kind = unicodedata.category(c)
        if kind == "Cf":
            continue
        out.append(" " if kind in ("Cc", "Zl", "Zp") else c)
    return "".join(out)


def strip_controls(text):
    cleaned = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c for c in str(text))
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _luhn(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def scrub_pii(text):
    """Replace personal data patterns with placeholders. Returns (text, redaction_count)."""
    if not text:
        return text, 0
    text = normalize_text(text)
    protected = []

    def protect(m):
        protected.append(m.group(0))
        return "" + chr(0xE100 + len(protected) - 1)

    out = SERVICE_ID.sub(protect, str(text))
    count = 0

    def sub(pattern, repl, s, check=None):
        nonlocal count

        def fn(m):
            nonlocal count
            if check is not None and not check(m):
                return m.group(0)
            count += 1
            return repl(m) if callable(repl) else repl
        return pattern.sub(fn, s)

    def card_ok(m):
        digits = re.sub(r"\D", "", m.group(0))
        return 13 <= len(digits) <= 19 and _luhn(digits)

    def phone_ok(m):
        return len(re.sub(r"\D", "", m.group(0))) >= 9 and not THOUSANDS.fullmatch(m.group(0))

    out = sub(EMAIL, "[EMAIL]", out)
    out = sub(CARD, "[CARD]", out, card_ok)
    out = sub(IPV4, "[IP]", out)
    out = sub(IPV6, "[IP]", out)
    out = sub(CPF, "[DOCUMENT]", out)
    out = sub(PHONE_PLUS, "[PHONE]", out, phone_ok)
    out = sub(PHONE_GROUPED, "[PHONE]", out, phone_ok)
    out = sub(CUSTOMER_ID, "[CUSTOMER_ID]", out)
    out = sub(PRODUCT_NUMBER, "[PRODUCT_NUMBER]", out)
    out = sub(DOCUMENT, lambda m: m.group(1) + "[DOCUMENT]", out)
    out = sub(LONG_NUMBER, "[NUMBER]", out)
    for i, original in enumerate(protected):
        out = out.replace("" + chr(0xE100 + i), original)
    return out, count


def wrap_untrusted(text):
    """{"untrusted_text", "flags"} for free text that comes from data, or None."""
    if text is None:
        return None
    clean = strip_controls(text)
    clean, _ = scrub_pii(clean)
    flags = []
    folded = fold(clean)
    if any(marker in folded for marker in INSTRUCTION_MARKERS):
        flags.append("instruction_like")
    if any(c in MARKUP_CHARS for c in clean):
        flags.append("markup")
    if len(clean) > UNTRUSTED_MAX:
        clean = clean[:UNTRUSTED_MAX]
        flags.append("truncated")
    return {"untrusted_text": clean, "flags": flags}


def reference_text_ok(text):
    """Reference text the service quotes as trusted (decline explanations): no control or format characters, no
    markup, nothing instruction-like and no personal data. Anything else is treated as a malformed record."""
    if not isinstance(text, str) or any(unicodedata.category(c) in ("Cc", "Cf") for c in text):
        return False
    if any(c in MARKUP_CHARS for c in text) or any(marker in fold(text) for marker in INSTRUCTION_MARKERS):
        return False
    return scrub_pii(text)[1] == 0


def short_hash(value, n=16):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:n]


def _scrub_cut(text, limit=AUDIT_TEXT_MAX):
    return scrub_pii(text)[0][:limit] if isinstance(text, str) else text


def redact_args(args):
    """Arguments as they may appear in the audit: secrets removed, keys hashed, free text scrubbed and cut."""
    if not isinstance(args, dict):
        return None
    out = copy.deepcopy(args)
    for key in ("document_number", "code"):
        if key in out:
            out[key] = "[REDACTED]"
    if isinstance(out.get("idempotency_key"), str):
        out["idempotency_key"] = "[hash:" + short_hash(out["idempotency_key"], 6) + "]"
    if isinstance(out.get("confirmation_id"), str):
        out["confirmation_id"] = out["confirmation_id"].split(".", 1)[0][:16] + ".[sig]"
    hints = out.get("hints")
    if isinstance(hints, dict):
        for key in ("merchant", "amount"):  # an amount may arrive as the customer's own text
            if isinstance(hints.get(key), str):
                hints[key] = _scrub_cut(hints[key])
    package = out.get("package")
    if isinstance(package, dict):
        for key, value in package.items():
            if key == "evidence":
                continue
            if isinstance(value, list):
                package[key] = [_scrub_cut(v) for v in value]
            else:
                package[key] = _scrub_cut(value)
    for key, value in list(out.items()):  # anything else that is long free text
        if isinstance(value, str) and len(value) > AUDIT_TEXT_MAX:
            out[key] = _scrub_cut(value)
    return out
