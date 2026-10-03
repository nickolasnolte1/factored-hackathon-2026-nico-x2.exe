"""Amounts as customers write them, for find_candidate_transactions hints (CONTRACT.md section 3.8).

    parse_amount("$1'985.843")     -> (1985843.0, None)
    parse_amount("R$ 1.200,00")    -> (1200.0, "BRL")
    parse_amount("9.951 dólares")  -> (9951.0, None)
    parse_amount("$494.11")        -> (494.11, None)

The reading is the same for every country and language, since money amounts never have three decimals:
- a single "." or "," followed by exactly three digits groups thousands ("109.686", "1,250"), unless the digits
  before it are too many to group ("1234.567" is 1234.567);
- a single separator followed by one, two or four or more digits is the decimal mark ("494.11", "1299,90");
- with both "." and ",", the last one is the decimal mark ("1.299,90", "1,299.90");
- a separator that appears more than once groups ("1.234.567"); "'" and spaces always group.
Groups after the first must have three digits and the first cannot start with 0; a reading that breaks this is dropped
for the other one, if valid ("0,500" is 0.5).

A currency code or symbol in the text sets the currency only when it is explicit: USD, COP, ARS, MXN, BRL, R$ (BRL),
US$, U$S and U$D (USD), AR$ (ARS), MX$ (MXN) and COL$ (COP). A bare "$" and the words pesos, reais, dólares or
verdes set nothing.
"""
import re
import unicodedata
from decimal import Decimal, InvalidOperation

MAX_AMOUNT = 1e12
_LEFT, _RIGHT = r"(?<![A-Za-z])", r"(?![A-Za-z])"
_SYMBOLS = ((re.compile(_LEFT + r"AR\$", re.I), "ARS"), (re.compile(_LEFT + r"MX\$", re.I), "MXN"),
            (re.compile(_LEFT + r"COL\$", re.I), "COP"), (re.compile(_LEFT + r"(?:U\$[SD]|US\$)", re.I), "USD"),
            (re.compile(_LEFT + r"R\$", re.I), "BRL"))
_CODES = re.compile(_LEFT + r"(USD|COP|ARS|MXN|BRL)" + _RIGHT, re.I)
_WORDS = re.compile(_LEFT + r"(pesos?|reais|real|d[oó]lares|d[oó]lar|dollars?|verdes)" + _RIGHT, re.I)
_NUMBER = re.compile(r"^\d+(?:[.,' ]\d+)*$")


def parse_amount(text):
    """(amount, currency or None) for an amount the customer wrote. Raises ValueError when the text is not a single
    positive amount."""
    if not isinstance(text, str):
        raise ValueError("not text")
    s = unicodedata.normalize("NFKC", text).replace("’", "'").replace("´", "'")
    found = set()
    for pattern, code in _SYMBOLS:
        found |= {code for _ in pattern.findall(s)}
        s = pattern.sub(" ", s)
    found |= {m.upper() for m in _CODES.findall(s)}
    s = _WORDS.sub(" ", _CODES.sub(" ", s)).replace("$", " ")
    if len(found) > 1:
        raise ValueError("two currencies")
    s = re.sub(r"\s+", " ", s).strip()
    if not _NUMBER.match(s):
        raise ValueError("not a number")
    value = _read_number(s)
    if not 0 < value <= MAX_AMOUNT:
        raise ValueError("out of range")
    return value, (found.pop() if found else None)


def regroup_three_decimals(amount):
    """A number amount with exactly three decimals read as grouped thousands (73.462 -> 73462, 1985.843 -> 1985843):
    money amounts never have three decimals, so such a number is a misread "73.462"."""
    value = float(amount)
    thousandths = value * 1000
    if abs(thousandths - round(thousandths)) < 1e-6 and abs(value * 100 - round(value * 100)) > 1e-6:
        return float(round(thousandths))
    return amount


def _read_number(s):
    groups = re.split(r"[.,' ]", s)
    seps = re.findall(r"[.,' ]", s)
    marks = [c for c in seps if c in ".,"]
    if not marks:
        readings = [None]
    elif len(set(marks)) == 2:
        readings = [marks[-1]]
    elif len(marks) > 1 or seps[-1] != marks[0]:
        readings = [None]
    elif len(groups[-1]) != 3:
        readings = [marks[0]]
    else:  # one "." or "," followed by three digits: thousands, unless the head is too long to group
        readings = [None, marks[0]]
    for decimal in readings:
        value = _reading(groups, seps, decimal)
        if value is not None:
            return value
    raise ValueError("bad grouping")


def _reading(groups, seps, decimal):
    """The value when `decimal` is the decimal mark (None: no decimals), or None when the groups do not fit."""
    if decimal is not None:
        if seps.count(decimal) != 1 or seps[-1] != decimal:
            return None
        whole, fraction = groups[:-1], groups[-1]
        grouping = seps[:-1]
    else:
        whole, fraction, grouping = groups, "", seps
    if len(set(c for c in grouping if c in ".,")) > 1:
        return None
    if grouping and (not 1 <= len(whole[0]) <= 3 or whole[0].startswith("0") or any(len(g) != 3 for g in whole[1:])):
        return None
    try:
        return float(Decimal("".join(whole) + ("." + fraction if fraction else "")))
    except InvalidOperation:
        return None
