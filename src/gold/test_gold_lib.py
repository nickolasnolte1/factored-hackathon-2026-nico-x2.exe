"""Unit tests for the Gold helpers (stdlib only, no Spark).

    python -m unittest src.gold.test_gold_lib -v
"""
import copy
import unittest

from src.gold import gold_lib as gl
from src.policy import dispute_policy as dp

SPEC = gl.load_spec()


class SpecTest(unittest.TestCase):
    def test_spec_and_sql_are_consistent(self):
        self.assertEqual(gl.validate(SPEC), [])

    def test_every_placeholder_is_filled(self):
        for table in SPEC["build_order"]:
            sql = gl.render_sql(table)
            self.assertIsNone(gl.PLACEHOLDER.search(sql), table)

    def test_schema_assertion_rejects_undeclared_columns(self):
        fields = [(n, c["type"]) for n, c in SPEC["tables"]["customer_profile"]["columns"].items()]
        gl.assert_schema("customer_profile", fields, SPEC)
        with self.assertRaises(gl.GoldSpecError):
            gl.assert_schema("customer_profile", fields + [("email", "string")], SPEC)
        with self.assertRaises(gl.GoldSpecError):
            gl.assert_schema("customer_profile", fields[:-1], SPEC)


class PolicyRenderingTest(unittest.TestCase):
    def test_flags_come_from_the_policy(self):
        pol = dp.load_policy()
        sql = gl.render_sql("customer_transactions", pol=pol)
        for t in pol["eligibility"]["transaction_types"]["dispute_incorrect_charge_or_fee"]:
            self.assertIn(f"'{t}'", sql)
        self.assertIn(f"amount_usd > {pol['handoff']['amount_usd_threshold']}", sql)

    def test_policy_change_reaches_the_sql(self):
        pol = copy.deepcopy(dp.load_policy())
        pol["handoff"]["amount_usd_threshold"] = 12345
        pol["handoff"]["restricted_customer_statuses"] = ["Closed"]
        self.assertIn("amount_usd > 12345", gl.render_sql("customer_transactions", pol=pol))
        self.assertNotIn("'Suspended'", gl.render_sql("customer_profile", pol=pol))

    def test_decline_keys(self):
        self.assertEqual(sorted(gl.policy_decline_keys()), ["05", "14", "51", "54", "missing"])

    def test_reference_checks(self):
        rows = [{"transaction_id": "T1", "customer_id": "C", "transaction_status": "Approved",
                 "transaction_type": "Adjustment", "amount_usd": 8000,
                 "dispute_eligible_unrecognized": False, "dispute_eligible_incorrect": True,
                 "above_handoff_threshold": True},
                {"transaction_id": "T2", "customer_id": "C", "transaction_status": "Declined",
                 "transaction_type": "Purchase", "amount_usd": 10,
                 "dispute_eligible_unrecognized": True, "dispute_eligible_incorrect": False,
                 "above_handoff_threshold": False}]
        self.assertEqual(gl.transaction_flag_mismatches(rows), ["T2"])
        codes = [{"code_key": "54", "response_code": "54", "reason": "expired_card", "in_policy": True,
                  "customer_message_key": "decline_expired_card", "cards_only": True},
                 {"code_key": "51", "response_code": "51", "reason": "insufficient_funds", "in_policy": True,
                  "customer_message_key": "decline_insufficient_funds", "cards_only": False}]
        self.assertEqual(gl.decline_mismatches(codes), [])
        codes[1]["reason"] = "wrong"
        self.assertEqual(gl.decline_mismatches(codes), ["51:Credit Card", "51:Savings Account"])


class PrivacyTest(unittest.TestCase):
    def test_personal_data_columns_are_flagged(self):
        bad = ["first_name", "last_name", "full_name", "email", "mobile_phone", "landline_phone", "address",
               "document_number", "ip", "ip_address", "date_of_birth", "product_number", "product_number_raw",
               "customer_name", "card_number", "postal_code"]
        self.assertEqual(gl.pii_columns(bad, SPEC), bad)

    def test_allowed_columns_pass(self):
        ok = ["customer_id", "document_type", "document_hash", "merchant_name", "product_number_last4",
              "product_number_collision", "transaction_country_code", "customer_message_key", "description"]
        self.assertEqual(gl.pii_columns(ok, SPEC), [])

    def test_silver_pii_columns_are_forbidden(self):
        silver = gl.silver_pii_columns()
        self.assertIn("document_number", silver)
        self.assertIn("product_number_raw", silver)
        self.assertEqual(sorted(gl.pii_columns(sorted(silver), SPEC)), sorted(silver))

    def test_no_gold_column_is_personal_data(self):
        for table, ts in SPEC["tables"].items():
            self.assertEqual(gl.pii_columns(ts["columns"], SPEC), [], table)

    def test_value_scan_skips_ids_and_hash(self):
        pred = gl.pii_value_predicate("customer_identity", SPEC)
        self.assertNotIn("`customer_id`", pred)
        self.assertNotIn("`document_hash`", pred)
        self.assertIn("`document_type`", pred)


class DocumentHashTest(unittest.TestCase):
    # Expected values computed with the SQL expression of customer_identity.sql on the SQL warehouse (synthetic inputs).
    VECTORS = [
        ("DNI", "12.345.678", "a3615ce1a2840782b852932d8ea6866da07775f9c3e452b2a1c3bdd9d103c8c5"),
        ("Pasaporte", "ab-1234567", "86fd638328bf09237348871cf6c34710fc7d0837b3faa71aa1f1513946296d87"),
        ("CC", " 1023 456 789 ", "29e4117b1c4a73de0ee9bbb7fbd302ea43a0a87647f14a5966eb2d7fb63901ea"),
    ]

    def test_python_matches_sql(self):
        for doc_type, number, expected in self.VECTORS:
            self.assertEqual(gl.document_hash(doc_type, number), expected)

    def test_normalization(self):
        self.assertEqual(gl.document_hash("dni ", "12345678"), gl.document_hash("DNI", "12.345.678"))
        self.assertNotEqual(gl.document_hash("CC", "12345678"), gl.document_hash("DNI", "12345678"))


if __name__ == "__main__":
    unittest.main()
