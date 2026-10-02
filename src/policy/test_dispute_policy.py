"""Unit tests for the dispute policy reference implementation (stdlib only).

    python -m unittest src.policy.test_dispute_policy -v
"""
import unittest
from datetime import datetime, timedelta

from src.policy import dispute_policy as dp

CID = "CLI-TEST00000001"
NOW = "2025-03-20T10:00:00"


def txn(tid, day, amount, ttype="Purchase", status="Approved", merchant="Super Ahorro", usd=None, **kw):
    row = {"transaction_id": tid, "customer_id": CID, "product_id": "PRD-TEST00000001",
           "event_ts": f"{day}T12:00:00", "event_date": day, "transaction_type": ttype,
           "transaction_status": status, "amount": amount, "currency": "USD",
           "amount_usd": amount if usd is None else usd, "channel": "POS",
           "merchant_name": merchant if ttype == "Purchase" else None, "implausible_type_channel": False,
           "is_international": False, "response_code": "00" if status == "Approved" else None}
    row.update(kw)
    return row


TXNS = [
    txn("T1", "2025-03-15", 250.00),
    txn("T2", "2025-03-12", 80.00, merchant="Uber"),
    txn("T3", "2025-03-11", 81.00, merchant="Uber"),
    txn("T4", "2025-03-18", 9000.00, ttype="Transfer"),
    txn("T5", "2025-03-19", 40.00, status="Declined", response_code="51"),
    txn("T6", "2025-03-25", 99.00),                       # after now: never a candidate
    txn("T7", "2024-10-01", 300.00, merchant="Cine Premium"),  # inside lookback, outside the 90-day window
]
SESSION = {"authenticated": True, "auth_factors": ["document", "otp"], "expires_at": "2025-03-20T10:15:00"}


def flow(turns, **kw):
    f = {"now": NOW, "language": "es", "session": dict(SESSION), "customer": {"customer_id": CID, "customer_status": "Active"},
         "transactions": TXNS, "products": [], "tool_faults": [], "turns": turns}
    f.update(kw)
    return f


UNREC = "dispute_unrecognized_charge"
CARD = "card_lost_or_block"


class PolicyRules(unittest.TestCase):
    def test_policy_is_labeled_synthetic(self):
        p = dp.load_policy()
        self.assertTrue(p["synthetic"])
        self.assertIn("SYNTHETIC", p["disclaimer"])

    def test_authentication(self):
        self.assertEqual(dp.authenticate(["document", "otp"]), (True, "ok"))
        self.assertFalse(dp.authenticate(["customer_number"])[0])
        self.assertEqual(dp.authenticate(["customer_number", "email"])[1], "insufficient_factor:customer_number,email")
        self.assertEqual(dp.authenticate(["document"])[1], "missing_factors:otp")

    def test_session_ttl(self):
        start = datetime(2025, 3, 20, 10, 0)
        s = {"authenticated": True, "auth_factors": ["document", "otp"], "expires_at": dp.session_expires_at(start)}
        self.assertTrue(dp.session_active(s, start + timedelta(minutes=14, seconds=59))[0])
        self.assertEqual(dp.session_active(s, start + timedelta(minutes=15))[1], "session_expired")

    def test_candidates_respect_now_and_type(self):
        ids = [t["transaction_id"] for t in dp.candidate_transactions(TXNS, CID, NOW, UNREC)]
        self.assertEqual(ids, ["T4", "T1", "T2", "T3", "T7"])

    def test_match_tolerances(self):
        c = dp.candidate_transactions(TXNS, CID, NOW, UNREC)
        self.assertEqual(dp.match_transactions({"amount": 252.4}, c)["matches"], ["T1"])      # +0.96%
        self.assertEqual(dp.match_transactions({"amount": 253.0}, c)["status"], "none")      # +1.2%
        self.assertEqual(dp.match_transactions({"date": "2025-03-13", "merchant": "uber"}, c)["status"], "multiple")
        self.assertEqual(dp.match_transactions({"amount": 81, "merchant": "UBR"}, c)["matches"], ["T3"])
        self.assertEqual(dp.match_transactions({"merchant": "superahorro"}, c)["matches"], ["T1"])
        self.assertEqual(dp.match_transactions({"merchant": "super ahoro"}, c)["matches"], ["T1"])
        self.assertEqual(dp.match_transactions({"amount": 250, "currency": "COP"}, c)["status"], "none")
        self.assertEqual(dp.match_transactions({}, c)["status"], "no_hints")

    def test_handoff_order_and_priority(self):
        big = TXNS[3]
        self.assertEqual(dp.requires_handoff({"transaction": big, "now": NOW}), (True, "amount_above_threshold"))
        self.assertEqual(dp.requires_handoff({"transaction": big, "now": NOW, "customer_status": "Suspended"})[1],
                         "customer_status_restricted")
        self.assertEqual(dp.requires_handoff({"transaction": TXNS[6], "now": NOW})[1], "outside_dispute_window")
        self.assertEqual(dp.requires_handoff({"transaction": TXNS[0], "now": NOW}), (False, None))
        self.assertEqual(dp.priority({"dispute_type": "unrecognized", "amount_usd": 3000}), "high")
        self.assertEqual(dp.priority({"dispute_type": "unrecognized", "amount_usd": 10}), "medium")
        self.assertEqual(dp.priority({"dispute_type": "incorrect", "amount_usd": 600}), "medium")
        self.assertEqual(dp.priority({"dispute_type": "incorrect", "amount_usd": 60}), "low")

    def test_conversation_triggers_follow_the_policy_order(self):
        order = [t["reason"] for t in dp.load_policy()["handoff"]["triggers_in_order"]][:4]
        self.assertEqual(order, ["customer_status_restricted", "suspected_card_compromise", "card_block_request",
                                 "explicit_human_request"])
        self.assertEqual(dp.requires_handoff({"customer_status": "Closed", "explicit_human_request": True}),
                         (True, "customer_status_restricted"))
        self.assertEqual(dp.handoff_reasons({"card_block_request": True, "explicit_human_request": True}),
                         ["card_block_request", "explicit_human_request"])

    def test_decline_codes(self):
        self.assertEqual(dp.decline_explanation("51")["reason"], "insufficient_funds")
        self.assertEqual(dp.decline_explanation(None)["reason"], "unknown_insufficient_data")
        self.assertEqual(dp.decline_explanation("54", product_type_en="Credit Card")["reason"], "expired_card")
        r = dp.decline_explanation("54", product_type_en="Savings Account")
        self.assertEqual((r["reason"], r["inconsistent_code"]), ("unknown_insufficient_data", True))

    def test_tool_retries(self):
        fault = {"tool": "get_transactions", "type": "timeout"}
        self.assertEqual(dp.tool_call("get_transactions", [{**fault, "failing_attempts": 2}])[:2], (True, 3))
        self.assertFalse(dp.tool_call("get_transactions", [{**fault, "failing_attempts": 3}])[0])


class ReferenceFlow(unittest.TestCase):
    def test_normal_dispute_creates_case_from_transaction(self):
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": UNREC, "claim": {"amount": 250, "merchant": "super ahorro"}}},
                                      {"offset_s": 60, "acts": {"confirm": True}}]))
        self.assertEqual((r["outcome"], r["transaction_id"]), ("create_case", "T1"))
        self.assertEqual(r["case_fields"]["subcategory"], "Cargo no reconocido")
        self.assertEqual(r["case_fields"]["amount"], 250.0)

    def test_ambiguous_match_then_clarify(self):
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": UNREC, "claim": {"merchant": "uber"}}},
                                      {"offset_s": 60, "acts": {"claim": {"amount": 81}}},
                                      {"offset_s": 90, "acts": {"confirm": True}}]))
        self.assertEqual((r["outcome"], r["transaction_id"]), ("clarify_then_create_case", "T3"))

    def test_no_match_hands_off_after_one_clarification(self):
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": UNREC, "claim": {"amount": 700, "merchant": "boutique"}}},
                                      {"offset_s": 60, "acts": {"claim": {"amount": 650}}}]))
        self.assertEqual((r["outcome"], r["handoff_reason"]), ("clarify_then_handoff", "no_match_after_clarification"))

    def test_expired_session_reauthenticates(self):
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": UNREC, "claim": {"amount": 250}}},
                                      {"offset_s": 16 * 60, "acts": {"confirm": True}}]))
        self.assertEqual((r["outcome"], r["case_fields"]), ("reauthenticate", None))

    def test_threshold_and_restricted_handoffs(self):
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": UNREC, "claim": {"amount": 9000, "txn_type": "Transfer"}}},
                                      {"offset_s": 60, "acts": {"confirm": True}}]))
        self.assertEqual((r["outcome"], r["handoff_reason"]), ("handoff", "amount_above_threshold"))
        self.assertEqual(r["case_fields"]["status"], "pending_human_review")
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": UNREC, "claim": {"amount": 250}}}],
                                     customer={"customer_id": CID, "customer_status": "Closed"}))
        self.assertEqual(r["handoff_reason"], "customer_status_restricted")

    def test_attacks_and_scope(self):
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": "out_of_scope", "attack": "prompt_injection"}}]))
        self.assertEqual(r["outcome"], "refuse")
        injected = {"intent": UNREC, "attack": "prompt_injection", "claim": {"amount": 250}}
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": injected},
                                      {"offset_s": 60, "acts": {"confirm": True}}]))
        self.assertEqual(r["outcome"], "create_case")
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": "out_of_scope"}}]))
        self.assertEqual(r["outcome"], "abstain")
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": "account_payment_inquiry"}}],
                                     session={"authenticated": False, "auth_factors": ["customer_number"], "expires_at": None}))
        self.assertEqual(r["outcome"], "reauthenticate")

    def test_tool_failure_never_creates_case(self):
        faults = [{"tool": "get_transactions", "type": "timeout", "failing_attempts": 99}]
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": UNREC, "claim": {"amount": 250}}}], tool_faults=faults))
        self.assertEqual((r["outcome"], r["handoff_reason"], r["case_fields"]), ("handoff", "tool_failure", None))
        faults = [{"tool": "create_case", "type": "error", "failing_attempts": 99}]
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": UNREC, "claim": {"amount": 250}}},
                                      {"offset_s": 60, "acts": {"confirm": True}}], tool_faults=faults))
        self.assertEqual((r["outcome"], r["handoff_reason"]), ("handoff", "tool_failure"))

    def test_decline_inquiry(self):
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": "account_payment_inquiry",
                                                               "inquiry": {"kind": "decline", "claim": {"date": "2025-03-19"}}}}]))
        self.assertEqual((r["outcome"], r["answer_facts"]["reason"]), ("answer", "insufficient_funds"))


class AuditedPaths(unittest.TestCase):
    """The three paths where the reference flow used to depart from the written policy."""

    def test_regression_plain_card_block_is_not_a_compromise(self):
        # was ('handoff', 'suspected_card_compromise') for every card request
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": CARD}}]))
        self.assertEqual((r["outcome"], r["handoff_reason"], r["intent"]), ("handoff", "card_block_request", CARD))
        self.assertEqual((r["case_fields"], r["trace"]), (None, ["turn 1: card block request"]))

    def test_card_requests(self):
        compromise = {"intent": CARD, "suspected_compromise": True, "claim": {"amount": 250}}
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": compromise}]))
        self.assertEqual((r["outcome"], r["handoff_reason"]), ("handoff", "suspected_card_compromise"))
        ambiguous = {"intent": CARD, "ambiguous": True, "acceptable_intents": [CARD, UNREC]}
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": ambiguous},
                                      {"offset_s": 60, "acts": {"clarifies_intent": CARD}}]))
        self.assertEqual((r["outcome"], r["handoff_reason"]), ("clarify_then_handoff", "card_block_request"))
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": ambiguous},
                                      {"offset_s": 60, "acts": {"clarifies_intent": UNREC, "claim": {"amount": 250}}},
                                      {"offset_s": 90, "acts": {"confirm": True}}]))
        self.assertEqual((r["outcome"], r["transaction_id"]), ("clarify_then_create_case", "T1"))
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": CARD, "explicit_human": True}}]))
        self.assertEqual(r["handoff_reason"], "card_block_request")
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": CARD}}],
                                     customer={"customer_id": CID, "customer_status": "Suspended"}))
        self.assertEqual(r["handoff_reason"], "customer_status_restricted")

    def test_regression_attack_with_a_real_dispute_serves_the_dispute(self):
        # was ('refuse', None): the dispute in the same message was not served
        for attack in ({"attack": "other_customer_data", "requests_other_customer": True},
                       {"attack": "social_engineering"}):
            r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": UNREC, "claim": {"amount": 250}, **attack}},
                                          {"offset_s": 60, "acts": {"confirm": True}}]))
            self.assertEqual((r["outcome"], r["transaction_id"]), ("create_case", "T1"), attack)
            self.assertEqual(r["trace"][0], f"turn 1: refuse {attack['attack']}, serve the real request")

    def test_attacks_alone_are_still_refused(self):
        for acts in ({"intent": "out_of_scope", "attack": "other_customer_data", "requests_other_customer": True},
                     {"intent": "out_of_scope", "attack": "social_engineering"}):
            r = dp.expected_outcome(flow([{"offset_s": 0, "acts": acts}]))
            self.assertEqual((r["outcome"], r["intent"], r["case_fields"]), ("refuse", "out_of_scope", None))
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": UNREC, "claim": {"amount": 250}}},
                                      {"offset_s": 60, "acts": {"attack": "other_customer_data", "requests_other_customer": True}},
                                      {"offset_s": 90, "acts": {"confirm": True}}]))
        self.assertEqual((r["outcome"], r["transaction_id"]), ("create_case", "T1"))

    def test_regression_restricted_status_wins_over_a_request_for_a_person(self):
        # was ('handoff', 'explicit_human_request'): the explicit request was checked first
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": UNREC, "explicit_human": True, "claim": {"amount": 250}}}],
                                     customer={"customer_id": CID, "customer_status": "Closed"}))
        self.assertEqual((r["outcome"], r["handoff_reason"]), ("handoff", "customer_status_restricted"))
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"explicit_human": True}}],
                                     customer={"customer_id": CID, "customer_status": "Suspended"}))
        self.assertEqual((r["handoff_reason"], r["trace"]), ("customer_status_restricted", ["turn 1: customer status Suspended"]))

    def test_regression_restricted_status_wins_over_complaint_routing(self):
        # was ('handoff', 'complaint_routing'): complaints skipped the restricted-status trigger
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": "other_complaint"}}],
                                     customer={"customer_id": CID, "customer_status": "Closed"}))
        self.assertEqual((r["outcome"], r["handoff_reason"]), ("handoff", "customer_status_restricted"))
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": "other_complaint"}}]))
        self.assertEqual((r["outcome"], r["handoff_reason"]), ("handoff", "complaint_routing"))

    def test_explicit_request_for_active_customers(self):
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": UNREC, "claim": {"amount": 250}}},
                                      {"offset_s": 60, "acts": {"explicit_human": True}}]))
        self.assertEqual((r["outcome"], r["handoff_reason"], r["intent"]), ("handoff", "explicit_human_request", UNREC))
        r = dp.expected_outcome(flow([{"offset_s": 0, "acts": {"intent": UNREC, "claim": {"merchant": "uber"}}},
                                      {"offset_s": 60, "acts": {"explicit_human": True}}]))
        self.assertEqual((r["outcome"], r["handoff_reason"]), ("clarify_then_handoff", "explicit_human_request"))


if __name__ == "__main__":
    unittest.main()
