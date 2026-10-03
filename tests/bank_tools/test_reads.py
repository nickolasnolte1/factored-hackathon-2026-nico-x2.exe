"""Read tools against the policy reference implementation (CONTRACT §3.4-§3.10, §3.14): overview, products,
balances, movements and the clock, candidate matching and the clarification counter, declines, eligibility, policy."""
import json

import pytest

from src.policy import dispute_policy as dp
from tests.bank_tools import fixture_data as fx
from tests.bank_tools import harness as hz
from tests.bank_tools.harness import expect_error, expect_ok

UNREC, INCORRECT = dp.DISPUTE_INTENTS
POL = hz.POLICY
MAX_SHOWN = POL["transaction_matching"]["max_candidates_shown"]


def _ids(views):
    return [v["transaction_id"] for v in views]


def _find(conv, hints, purpose="dispute", intent=UNREC):
    args = {"purpose": purpose, "hints": hints}
    if intent is not None:
        args["intent"] = intent
    return conv.ok("find_candidate_transactions", args)


# ---------------------------------------------------------------- overview, products, balances


def test_customer_overview(bank):
    data = bank.customer(fx.C1).ok("get_customer_overview")
    hz.check_output("get_customer_overview", data)
    assert data["customer_status"] == "Active" and data["country_code"] == "CO"
    own = [p for p in fx.PRODUCTS if p["customer_id"] == fx.C1]
    assert {(s["product_type_en"], s["count"]) for s in data["products_summary"]} == {
        (t, sum(p["product_type_en"] == t for p in own)) for t in {p["product_type_en"] for p in own}}
    assert data["now"] == hz.iso(fx.NOW) and data["data_as_of"] == fx.AS_OF
    assert data["session"]["authenticated_at"] == hz.iso(fx.NOW) and data["session"]["minutes_left"] == 15
    assert sorted(data["languages"]) == ["es", "pt"]


def test_list_products_views_and_filters(bank):
    conv = bank.customer(fx.C1)
    data = conv.ok("list_products")
    hz.check_output("list_products", data)
    own = sorted((p for p in fx.PRODUCTS if p["customer_id"] == fx.C1), key=lambda p: (p["product_type_en"], p["product_id"]))
    assert [p["product_id"] for p in data["products"]] == [p["product_id"] for p in own]
    assert data["count"] == len(own)
    for view, src in zip(data["products"], own):
        assert view["number_last4"] == src["product_number_last4"] and view["effective_status"] == src["effective_status"]
        assert view["is_card"] is src["is_card"] and view["currency"] == src["currency"]
        assert view["status_as_of"] == fx.AS_OF
    active = conv.ok("list_products", {"only_active": True})["products"]
    assert fx.P["expired"] not in {p["product_id"] for p in active}
    cards = conv.ok("list_products", {"product_types": ["Credit Card"]})["products"]
    assert [p["product_id"] for p in cards] == [fx.P["credit"]]


@pytest.mark.parametrize("key,kind", [("savings", "funds"), ("credit", "outstanding_debt"), ("expired", "funds")])
def test_balance_is_the_snapshot_with_its_kind(bank, key, kind):
    data = bank.customer(fx.C1).ok("get_balance", {"product_id": fx.P[key]})
    hz.check_output("get_balance", data)
    src = fx.PRODUCT_BY_ID[fx.P[key]]
    assert data["current_balance"] == src["current_balance"] and data["currency"] == src["currency"]
    assert data["balance_kind"] == kind and data["balance_as_of"] == fx.AS_OF
    assert data["credit_limit"] == (src["credit_limit"] if src["product_type_en"] in fx.CREDIT_TYPES else None)
    assert data["effective_status"] == src["effective_status"]


# ---------------------------------------------------------------- movements and the clock


def test_recent_movements_equal_the_policy_reference(bank):
    data = bank.customer(fx.C1).ok("list_recent_transactions")
    hz.check_output("list_recent_transactions", data)
    expected = dp.recent_movements(fx.served_rows(fx.C1), fx.C1, fx.NOW, pol=POL)
    assert _ids(data["transactions"]) == [t["transaction_id"] for t in expected]
    assert data["returned"] == POL["narration"]["recent_movements_count"] and data["has_more"] is True
    assert data["now"] == hz.iso(fx.NOW)
    for view in data["transactions"]:
        src = fx.TXN_BY_ID[view["transaction_id"]]
        assert view["amount"] == src["amount"] and view["currency"] == src["currency"]
        assert view["event_ts"] == src["event_ts"][:16] and view["event_date"] == src["event_date"]


def test_movements_after_now_are_never_returned(bank):
    conv = bank.customer(fx.C1)
    views = conv.ok("list_recent_transactions", {"limit": 20, "date_to": "2026-12-31"})["transactions"]
    assert all(v["event_ts"] <= hz.iso(fx.NOW)[:16] for v in views)
    assert not {fx.T["future_today"], fx.T["future"]} & set(_ids(views))
    same_day = conv.ok("list_recent_transactions", {"date_from": "2026-06-19", "date_to": "2026-06-30"})
    assert same_day["transactions"] == [] and same_day["returned"] == 0
    assert _find(conv, {"merchant": "Kiosko Luna"})["match_status"] == "none"
    expect_error(conv.call("explain_decline", {"transaction_id": fx.T["future_today"], "language": "es"}), "NOT_FOUND")
    expect_error(conv.call("prepare_dispute_case", {"transaction_id": fx.T["future_today"], "intent": UNREC,
                                                    "language": "es"}), "NOT_FOUND")
    bank.set_now(fx.NOW.replace(hour=15, minute=1))  # the clock passes the movement (new session: TTL is 15 min)
    assert fx.T["future_today"] in _ids(bank.customer(fx.C1).ok("list_recent_transactions")["transactions"])


def test_movement_filters(bank):
    conv = bank.customer(fx.C1)
    declined = conv.ok("list_recent_transactions", {"statuses": ["Declined"], "limit": 20})["transactions"]
    assert set(_ids(declined)) == {t["transaction_id"] for t in fx.served_rows(fx.C1)
                                   if t["transaction_status"] == "Declined"}
    savings = conv.ok("list_recent_transactions", {"product_id": fx.P["savings"], "limit": 20})["transactions"]
    assert set(_ids(savings)) == {t["transaction_id"] for t in fx.served_rows(fx.C1) if t["product_id"] == fx.P["savings"]}
    deposits = conv.ok("list_recent_transactions", {"transaction_types": ["Deposit"]})["transactions"]
    assert _ids(deposits) == [fx.T["deposit"]]
    june = conv.ok("list_recent_transactions", {"date_from": "2026-06-10", "date_to": "2026-06-11", "limit": 20})
    assert set(_ids(june["transactions"])) == {fx.T["uber_a"], fx.T["uber_b"]}
    default_limit = conv.ok("list_recent_transactions", {"limit": None})
    assert default_limit["returned"] == POL["narration"]["recent_movements_count"]
    expect_error(conv.call("list_recent_transactions", {"date_from": "2026-06-12", "date_to": "2026-06-10"}),
                 "VALIDATION_ERROR")
    expect_error(conv.call("list_recent_transactions", {"limit": 21}), "VALIDATION_ERROR")


def test_implausible_channel_is_never_narrated(bank):
    views = bank.customer(fx.C1).ok("list_recent_transactions", {"limit": 20})["transactions"]
    by_id = {v["transaction_id"]: v for v in views}
    assert by_id[fx.T["implausible"]]["channel"] is None
    assert by_id[fx.T["super"]]["channel"] == "POS"


# ---------------------------------------------------------------- candidate matching


MATCH_CASES = [
    ("dispute", UNREC, {"merchant": "Super Ahorro"}, "unique"),
    ("dispute", UNREC, {"merchant": "super ahoro"}, "unique"),
    ("dispute", UNREC, {"amount": 252500.0, "currency": "COP"}, "unique"),
    ("dispute", UNREC, {"amount": 252600.0, "currency": "COP"}, "none"),
    ("dispute", UNREC, {"amount": 250000.0, "currency": "BRL"}, "none"),
    ("dispute", UNREC, {"amount": 32100.0}, "multiple"),
    ("dispute", UNREC, {"merchant": "Uber", "date": "2026-06-08"}, "unique"),
    ("dispute", UNREC, {"merchant": "Uber", "date": "2026-06-07"}, "none"),
    ("dispute", UNREC, {"merchant": "Super Ahorro", "date": "2026-06-17"}, "unique"),
    ("dispute", UNREC, {"merchant": "Super Ahorro", "date": "2026-06-18"}, "none"),
    ("dispute", UNREC, {"merchant": "Supermercado Gigante"}, "none"),
    ("dispute", UNREC, {"txn_type": "Transfer", "amount": 40000000.0}, "unique"),
    ("dispute", UNREC, {"txn_type": "Adjustment"}, "none"),
    ("dispute", INCORRECT, {"txn_type": "Adjustment"}, "unique"),
    ("dispute", None, {"merchant": "Cafe Andino"}, "unique"),
    ("dispute", UNREC, {"merchant": "Tienda Norte"}, "none"),
    ("dispute", UNREC, {"channel": "Web", "txn_type": "Withdrawal"}, "none"),
    ("dispute", UNREC, {"merchant": "Farmacia Central"}, "unique"),
    ("dispute", UNREC, {"merchant": "Kiosko Luna"}, "none"),
    ("dispute", UNREC, {"merchant": "Bazar Rojo"}, "none"),
    ("dispute", UNREC, {"currency": "COP"}, "no_hints"),
    ("dispute", UNREC, {}, "no_hints"),
    ("decline_inquiry", None, {"merchant": "Tienda Norte"}, "unique"),
    ("decline_inquiry", None, {"amount": 51000.0}, "unique"),
    ("decline_inquiry", None, {}, "no_hints"),
    # date ranges: inclusive bounds, either one optional, no tolerance (the exact date keeps its own)
    ("dispute", UNREC, {"date_from": "2026-06-10", "date_to": "2026-06-11"}, "multiple"),
    ("dispute", UNREC, {"date_from": "2026-06-15", "date_to": "2026-06-15"}, "unique"),
    ("dispute", UNREC, {"merchant": "Uber", "date_from": "2026-06-08", "date_to": "2026-06-09"}, "none"),
    ("dispute", UNREC, {"merchant": "Uber", "date_from": "2026-06-11"}, "unique"),
    ("dispute", UNREC, {"merchant": "Uber", "date_to": "2026-06-10"}, "unique"),
    ("dispute", UNREC, {"date_from": "2026-06-01", "date_to": "2026-06-30"}, "multiple"),
    ("dispute", UNREC, {"merchant": "Super Ahorro", "date_from": "2026-06-01", "date_to": "2026-06-30"}, "unique"),
    ("dispute", UNREC, {"date": "2026-06-16", "date_from": "2026-06-15", "date_to": "2026-06-15"}, "unique"),
    ("dispute", UNREC, {"date_from": "2026-06-19", "date_to": "2026-06-30"}, "none"),
    ("decline_inquiry", None, {"date_from": "2026-06-13", "date_to": "2026-06-14"}, "multiple"),
]


@pytest.mark.parametrize("purpose,intent,hints,status", MATCH_CASES, ids=[f"{c[0]}-{c[3]}-{i}" for i, c in
                                                                           enumerate(MATCH_CASES)])
def test_candidate_matching_equals_the_policy(bank, purpose, intent, hints, status):
    expected = fx.expected_match(fx.C1, purpose, intent, hints)
    assert expected["status"] == status, "fixture assumption"
    data = _find(bank.customer(fx.C1), hints, purpose, intent)
    hz.check_output("find_candidate_transactions", data)
    assert data["match_status"] == status
    assert _ids(data["candidates"]) == expected["matches"][:MAX_SHOWN]
    if status != "no_hints":
        assert data["total_matches"] == len(expected["matches"])
    want_action = {"unique": "confirm_candidate" if purpose == "dispute" else "explain_decline",
                   "multiple": "ask_customer_to_pick", "no_hints": "ask_customer_to_pick",
                   "none": "ask_one_clarifying_question"}[status]
    assert data["policy"]["next_action"] == want_action and data["policy"]["handoff_reason"] is None
    assert data["policy"]["max_clarifications"] == POL["handoff"]["max_clarifications_before_handoff"]


def test_hints_filter_but_never_become_facts(bank):
    data = _find(bank.customer(fx.C1), {"amount": 251000.0, "currency": "COP", "date": "2026-06-16"})
    assert data["match_status"] == "unique"
    view = data["candidates"][0]
    assert view["amount"] == fx.row("super")["amount"] and view["event_date"] == fx.row("super")["event_date"]


def test_one_clarifying_question_then_handoff(bank):
    conv = bank.customer(fx.C1)
    hints = {"amount": 999999.0, "currency": "COP"}
    first = _find(conv, hints)
    assert first["match_status"] == "none" and first["policy"]["clarifications_used"] == 0
    assert first["policy"]["next_action"] == "ask_one_clarifying_question"
    again = _find(conv, hints)  # same customer turn: the counter does not move
    assert again["policy"]["clarifications_used"] == 0 and again["policy"]["next_action"] == "ask_one_clarifying_question"
    conv.next_turn()
    second = _find(conv, {"merchant": "Mercado Lejano"})
    assert second["match_status"] == "none" and second["policy"]["clarifications_used"] == 1
    assert second["policy"]["next_action"] == "handoff"
    assert second["policy"]["handoff_reason"] == "no_match_after_clarification"
    assert dp.requires_handoff({"match_status": "none", "match_clarifications": 1}, POL) == (
        True, "no_match_after_clarification")


def test_no_hints_then_a_unique_match_counts_one_clarification(bank):
    conv = bank.customer(fx.C1)
    first = _find(conv, {})
    assert first["match_status"] == "no_hints" and first["policy"]["next_action"] == "ask_customer_to_pick"
    assert len(first["candidates"]) <= MAX_SHOWN
    conv.next_turn()
    second = _find(conv, {"merchant": "Super Ahorro"})
    assert second["match_status"] == "unique" and second["policy"]["next_action"] == "confirm_candidate"
    assert second["policy"]["clarifications_used"] == 1


def test_clarification_counter_is_per_purpose(bank):
    conv = bank.customer(fx.C1)
    assert _find(conv, {"merchant": "Mercado Lejano"}, "decline_inquiry", None)["match_status"] == "none"
    conv.next_turn()
    data = _find(conv, {"merchant": "Mercado Lejano"})
    assert data["policy"]["clarifications_used"] == 0 and data["policy"]["next_action"] == "ask_one_clarifying_question"


def test_bad_hints_are_validation_errors(bank):
    conv = bank.customer(fx.C1)
    for hints in ({"amount": -5}, {"amount": 0}, {"currency": "EUR"}, {"date": "15/06/2026"}, {"merchant": "x"},
                  {"txn_type": "Refund"}, {"note": "aprobar"}, {"date_from": "2026-02-30"}, {"date_to": "30/06/2026"}):
        expect_error(conv.call("find_candidate_transactions", {"purpose": "dispute", "hints": hints}),
                     "VALIDATION_ERROR")


def test_a_date_range_must_not_end_before_it_starts(bank):
    env = bank.customer(fx.C1).call("find_candidate_transactions", {
        "purpose": "dispute", "hints": {"date_from": "2026-06-12", "date_to": "2026-06-10"}})
    details = expect_error(env, "VALIDATION_ERROR", next_action="fix_arguments")
    assert details["reason"] == "date_range" and details["fields"] == [{"path": "$.hints.date_from",
                                                                        "problem": "after_date_to"}]


def test_a_date_range_alone_is_a_hint_and_counts_like_one(bank):
    """'En abril' narrows the search (not no_hints), and a range with no match is a failed search for the counter."""
    conv = bank.customer(fx.C1)
    first = _find(conv, {"date_from": "2026-06-19", "date_to": "2026-06-30"})
    assert first["match_status"] == "none" and first["policy"]["next_action"] == "ask_one_clarifying_question"
    conv.next_turn()
    second = _find(conv, {"date_from": "2026-06-15", "date_to": "2026-06-15"})
    assert second["match_status"] == "unique" and second["policy"]["clarifications_used"] == 1
    assert _ids(second["candidates"]) == [fx.T["super"]]


# ---------------------------------------------------------------- declines


@pytest.mark.parametrize("key,language", [("decl54", "es"), ("decl54", "pt"), ("decl14", "es"), ("decl14", "pt"),
                                          ("declmissing", "es"), ("declmissing", "pt"), ("decl51", "es")])
def test_decline_explanation_comes_from_the_code_table(bank, key, language):
    data = bank.customer(fx.C1).ok("explain_decline", {"transaction_id": fx.T[key], "language": language})
    hz.check_output("explain_decline", data)
    exp, message, offer_human = fx.expected_decline(key, language)
    src = fx.row(key)
    product = fx.PRODUCT_BY_ID[src["product_id"]]
    assert data["applicable"] is True and data["transaction_status"] == "Declined"
    assert data["response_code"] == exp["response_code"] and data["reason"] == exp["reason"]
    assert data["customer_message_key"] == exp["customer_message_key"]
    assert data["inconsistent_code"] is exp["inconsistent_code"]
    assert data["customer_message"] == message and data["offer_human"] is offer_human
    expiry = product["expiration_date"]
    assert data["product"] == {
        "product_id": product["product_id"], "product_type_en": product["product_type_en"],
        "effective_status": product["effective_status"], "card_expiration_month": expiry[:7] if expiry else None,
        "card_expired_at_transaction": (expiry < src["event_date"]) if expiry else None}


@pytest.mark.parametrize("key", ["pending", "super", "reversed"])
def test_non_declined_movements_are_not_explained(bank, key):
    data = bank.customer(fx.C1).ok("explain_decline", {"transaction_id": fx.T[key], "language": "es"})
    assert data["applicable"] is False and data["transaction_status"] == fx.row(key)["transaction_status"]
    assert data["reason"] is None and data["customer_message"] is None and data["customer_message_key"] is None


def test_only_spanish_and_portuguese(bank):
    conv = bank.customer(fx.C1)
    for language in ("en", "ES", "es-CO", None):
        expect_error(conv.call("explain_decline", {"transaction_id": fx.T["decl54"], "language": language}),
                     "VALIDATION_ERROR")


# ---------------------------------------------------------------- eligibility


ELIGIBILITY = [("super", UNREC), ("super", INCORRECT), ("fee", UNREC), ("fee", INCORRECT), ("decl54", UNREC),
               ("pending", UNREC), ("deposit", INCORRECT), ("reversed", UNREC), ("big", UNREC), ("old", UNREC),
               ("boundary", UNREC), ("intl", UNREC), ("flagtrap", UNREC), ("implausible", UNREC)]


@pytest.mark.parametrize("key,intent", ELIGIBILITY)
def test_eligibility_equals_the_policy(bank, key, intent):
    """Decided by dispute_policy at request time, never by Gold's precomputed flags (see the flagtrap row)."""
    data = bank.customer(fx.C1).ok("check_dispute_eligibility", {"transaction_id": fx.T[key], "intent": intent})
    hz.check_output("check_dispute_eligibility", data)
    src = fx.row(key)
    ok, reason = dp.is_eligible(src, fx.C1, intent, POL)
    reasons = [r for r in dp.handoff_reasons({"transaction": src, "now": fx.NOW, "customer_status": "Active"}, POL)
               if r in ("outside_dispute_window", "amount_above_threshold")]
    assert data["eligible"] is ok and data["ineligible_reason"] == (None if ok else reason)
    assert data["within_dispute_window"] is dp.in_dispute_window(src["event_date"], fx.NOW, POL)
    assert data["days_since_transaction"] == dp.days_since(src["event_date"], fx.NOW)
    assert data["dispute_window_days"] == POL["dispute_window"]["days_since_transaction"]
    assert data["handoff_required"] is bool(reasons) and data["handoff_reason"] == (reasons[0] if reasons else None)


def test_eligibility_is_read_only_and_hides_the_threshold(bank):
    conv = bank.customer(fx.C1)
    env = conv.call("check_dispute_eligibility", {"transaction_id": fx.T["big"], "intent": UNREC})
    assert expect_ok(env)["handoff_reason"] == "amount_above_threshold"
    threshold = POL["handoff"]["amount_usd_threshold"]
    assert threshold not in list(hz.iter_numbers(env["data"])) and str(threshold) not in json.dumps(env["data"])
    assert conv.ok("get_case_status") == {"cases": []}


# ---------------------------------------------------------------- policy information


TOPICS = hz.TOOLS["get_policy_info"]["input_schema"]["properties"]["topic"]["enum"]
INTERNAL_NUMBERS = {POL["handoff"]["amount_usd_threshold"], POL["priority"]["high_amount_usd"],
                    POL["priority"]["medium_amount_usd"], POL["handoff"]["min_intent_confidence"],
                    POL["transaction_matching"]["lookback_days"], POL["transaction_matching"]["merchant_min_similarity"]}
INTERNAL_TEXTS = ["7000", "7.000", "7,000", "2500", "2.500", "2,500", "0.55", "0,55", "{policy:"]
INTERNAL_KEYS = ("threshold", "tolerance", "similarity", "confidence", "retries", "lookback", "calibration")


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)
EXPECTED_VALUES = {
    "dispute_window": {POL["dispute_window"]["days_since_transaction"]},
    "authentication": {POL["authentication"]["second_factor"]["digits"],
                       POL["authentication"]["second_factor"]["ttl_seconds"], POL["authentication"]["session_ttl_minutes"]},
    "session_expiry": {POL["authentication"]["session_ttl_minutes"]},
    "response_times": set(POL["priority"]["first_response_hours"].values()),
    "dispute_process": set(POL["priority"]["first_response_hours"].values()),
}


@pytest.mark.parametrize("language", ["es", "pt"])
def test_policy_info_is_public_grounded_and_has_no_internal_values(bank, language):
    conv = bank.conv(token=None)
    texts = {}
    for topic in TOPICS:
        env = conv.call("get_policy_info", {"topic": topic, "language": language})
        data = expect_ok(env)
        hz.check_output("get_policy_info", data)
        assert data["topic"] == topic and data["language"] == language and data["synthetic"] is True
        assert data["policy_id"] == POL["policy_id"] and data["policy_version"] == POL["version"]
        numbers = set(hz.iter_numbers(data["values"]))
        assert EXPECTED_VALUES.get(topic, set()) <= numbers, topic
        assert not numbers & INTERNAL_NUMBERS, topic
        text = json.dumps(data, ensure_ascii=False)
        assert not [t for t in INTERNAL_TEXTS if t in text], topic
        assert not [k for k in _keys(data["values"]) if any(word in k for word in INTERNAL_KEYS)], topic
        texts[topic] = " ".join(s["text"] for s in data["snippets"])
    assert str(POL["dispute_window"]["days_since_transaction"]) in texts["dispute_window"]


def test_policy_info_differs_by_language_and_ignores_the_session(bank):
    token = bank.session(fx.C1)
    es = bank.conv(token).ok("get_policy_info", {"topic": "refunds", "language": "es"})
    bank.advance(16 * 60)  # expired session: still served, the session is not used
    pt = bank.conv(token).ok("get_policy_info", {"topic": "refunds", "language": "pt"})
    assert es["snippets"] != pt["snippets"]
