"""Amounts written as text in find_candidate_transactions hints (CONTRACT §3.8): one reading for every country (a
single separator followed by three digits groups thousands), explicit currencies only, and no customer data read."""
import pytest

from src.bank_tools.amounts import parse_amount
from src.policy import dispute_policy as dp
from tests.bank_tools import fixture_data as fx
from tests.bank_tools import harness as hz
from tests.bank_tools.harness import expect_error

UNREC = dp.DISPUTE_INTENTS[0]

# (text, amount, currency). One "." or "," followed by three digits groups thousands, followed by one or two digits it
# is the decimal mark; with both, the last one is the decimal mark; "'" and spaces group.
READINGS = [
    ("109.686", 109686.0, None),
    ("109,686", 109686.0, None),
    ("ARS 109.686", 109686.0, "ARS"),
    ("AR$ 109.686", 109686.0, "ARS"),
    ("$1'985.843", 1985843.0, None),
    ("9.951 dólares", 9951.0, None),  # a Portuguese speaker in Mexico (e2e-pt-0080)
    ("1.500", 1500.0, None),
    ("US$1.500", 1500.0, "USD"),
    ("US$ 1,250", 1250.0, "USD"),
    ("USD 1,250", 1250.0, "USD"),
    ("1.299,90", 1299.9, None),
    ("1,299.90", 1299.9, None),
    ("R$ 1.200,00", 1200.0, "BRL"),
    ("494.11", 494.11, None),
    ("$494.11", 494.11, None),
    ("494,11", 494.11, None),
    ("353,72 verdes", 353.72, None),
    ("USD 6 733.52", 6733.52, "USD"),
    ("US$ 1.500", 1500.0, "USD"),
    ("U$S 1.500", 1500.0, "USD"),
    ("u$s 100", 100.0, "USD"),
    ("U$D 100", 100.0, "USD"),
    ("COP$ 50.000", 50000.0, "COP"),
    ("COL$ 50.000", 50000.0, "COP"),
    ("mxn 1,250", 1250.0, "MXN"),
    ("MX$ 1,250", 1250.0, "MXN"),
    ("50.000 pesos", 50000.0, None),
    ("1.234.567", 1234567.0, None),
    ("1,234,567.89", 1234567.89, None),
    ("1234.567", 1234.567, None),  # "." cannot group a 4-digit head: it is the decimal mark
    ("1.2345", 1.2345, None),
    ("0,500", 0.5, None),  # a first group of 0 does not group thousands
    ("12,5", 12.5, None),
    ("250000", 250000.0, None),
    (" 1 234,56", 1234.56, None),
]


@pytest.mark.parametrize("text,amount,currency", READINGS)
def test_amount_text_is_read_the_same_way_in_every_country(text, amount, currency):
    assert parse_amount(text) == (pytest.approx(amount), currency)


@pytest.mark.parametrize("text", ["", "cien", "-5", "0", "0,00", "$", "1.234.56", "1,23,456", "1.234,567.89",
                                  "USD 5 COP", "AR$ 5 USD", "50 mil", "1..2", "1e5", "4111 1111 1111 1111",
                                  "2.000.000.000.000"])
def test_text_that_is_not_one_amount_is_refused(text):
    with pytest.raises(ValueError):
        parse_amount(text)


# ---------------------------------------------------------------- through the service


def _find(conv, hints, purpose="dispute", intent=UNREC):
    return conv.call("find_candidate_transactions", {"purpose": purpose, "intent": intent, "hints": hints})


def _mexican(snapshot_path):
    """The main fixture customer read as Mexican: the same movements, another country."""
    def as_mx(original, *args, **kwargs):
        row = original(*args, **kwargs)
        return dict(row, country_code="MX") if row else row
    return hz.stub_repository(snapshot_path, {"get_customer": as_mx})


@pytest.mark.parametrize("country", ["CO", "MX"])
@pytest.mark.parametrize("text", ["250.000", "$250.000", "250,000", "$250,000", "250000,00", "250000.00",
                                  "$40'000.000"])
def test_amount_text_matches_the_same_way_whatever_the_country(make_bank, snapshot_path, country, text):
    bank = make_bank(repo=_mexican(snapshot_path) if country == "MX" else None)
    hints = {"amount": text, **({"txn_type": "Transfer"} if "'" in text else {})}
    data = hz.expect_ok(_find(bank.customer(fx.C1), hints))
    assert data["match_status"] == "unique"
    assert data["candidates"][0]["transaction_id"] == fx.T["big" if "'" in text else "super"]


def test_amount_text_for_an_argentine_customer(bank):
    conv = bank.customer(fx.C2)
    for text in ("15.432,10", "15432,10", "ARS 15.432", "$15.432"):
        data = hz.expect_ok(_find(conv, {"amount": text}))
        assert data["match_status"] == "unique" and data["candidates"][0]["transaction_id"] == fx.T["b_super"], text
    assert hz.expect_ok(_find(conv, {"amount": "500.000"}))["candidates"][0]["transaction_id"] == fx.T["b_transfer"]


def test_explicit_currency_in_the_text_sets_the_hint(bank):
    conv = bank.customer(fx.C1)
    assert hz.expect_ok(_find(conv, {"amount": "R$ 250.000,00"}))["match_status"] == "none"
    assert hz.expect_ok(_find(conv, {"amount": "COP 250.000", "currency": "USD"}))["match_status"] == "unique"
    assert hz.expect_ok(_find(conv, {"amount": "$250.000", "currency": "BRL"}))["match_status"] == "none"
    assert hz.expect_ok(_find(conv, {"amount": "$250.000", "currency": "COP"}))["match_status"] == "unique"


def test_text_and_number_give_the_same_answer(bank):
    conv = bank.customer(fx.C1)
    as_text = hz.expect_ok(_find(conv, {"amount": "51.000"}, "decline_inquiry", None))
    as_number = hz.expect_ok(_find(conv, {"amount": 51000.0}, "decline_inquiry", None))
    assert as_text == as_number
    assert as_text["match_status"] == "unique" and as_text["policy"]["next_action"] == "explain_decline"


def test_amount_text_reads_no_customer_data(bank):
    conv = bank.customer(fx.C1)
    for text in ("250.000", "250000,00"):
        assert "get_customer" not in bank.audit_for(_find(conv, {"amount": text}))["attempts"]


def test_bad_amount_text_is_a_validation_error_without_echo(bank):
    conv = bank.customer(fx.C1)
    for text in ("cien mil", "1.234.56", "USD 5 COP", "x" * 41):
        env = _find(conv, {"amount": text})
        details = expect_error(env, "VALIDATION_ERROR", next_action="fix_arguments")
        assert {f["path"] for f in details["fields"]} == {"$.hints.amount"}
        assert text not in str(env)


def test_a_number_with_three_decimals_is_read_as_grouped_thousands(bank):
    conv = bank.customer(fx.C2)
    data = hz.expect_ok(_find(conv, {"amount": 15.432}))  # "15.432" misread as a number
    assert data["match_status"] == "unique" and data["candidates"][0]["transaction_id"] == fx.T["b_super"]
    assert hz.expect_ok(_find(conv, {"amount": 15432.1}))["candidates"][0]["transaction_id"] == fx.T["b_super"]


@pytest.mark.parametrize("key", ["date", "date_from"])
def test_a_date_after_now_is_a_validation_error(bank, key):
    env = _find(bank.customer(fx.C1), {key: "2099-01-05"})
    details = expect_error(env, "VALIDATION_ERROR", next_action="fix_arguments")
    assert details["fields"] == [{"path": "$.hints." + key, "problem": "after_now"}]
