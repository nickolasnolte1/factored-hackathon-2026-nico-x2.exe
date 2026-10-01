"""Contract checks that need no implementation: tool_schemas.json against the policy and Gold, and the synthetic
fixture against the policy (so every behavioral test means what it claims)."""
import re
import sqlite3

import pytest

from src.gold import gold_lib
from src.policy import dispute_policy as dp
from tests.bank_tools import fixture_data as fx
from tests.bank_tools import harness as hz

EXPECTED_TOOLS = {
    "start_authentication": ("runtime", "none"), "verify_otp": ("runtime", "challenge"),
    "get_customer_overview": ("model", "required"), "list_products": ("model", "required"),
    "get_balance": ("model", "required"), "list_recent_transactions": ("model", "required"),
    "find_candidate_transactions": ("model", "required"), "explain_decline": ("model", "required"),
    "check_dispute_eligibility": ("model", "required"), "prepare_dispute_case": ("model", "required"),
    "create_dispute_case": ("model", "required"), "get_case_status": ("model", "required"),
    "get_policy_info": ("model", "none"), "handoff_to_human": ("model", "optional"),
}
INPUT_KEYWORDS = {"type", "properties", "required", "additionalProperties", "enum", "const", "pattern", "minLength",
                  "maxLength", "minimum", "maximum", "exclusiveMinimum", "items", "minItems", "maxItems",
                  "uniqueItems", "default", "description"}


def _walk_schema(schema, path="$"):
    yield path, schema
    for name, sub in (schema.get("properties") or {}).items():
        yield from _walk_schema(sub, f"{path}.{name}")
    if isinstance(schema.get("items"), dict):
        yield from _walk_schema(schema["items"], f"{path}[]")
    for sub in schema.get("anyOf", []):
        yield from _walk_schema(sub, path)


def _property_names(schema):
    for _, node in _walk_schema(schema):
        yield from (node.get("properties") or {})


def test_tool_catalog_matches_the_contract():
    assert {t["name"]: (t["exposure"], t["auth"]) for t in hz.SCHEMAS["tools"]} == EXPECTED_TOOLS
    for tool in hz.SCHEMAS["tools"]:
        assert 0 < len(tool["description"]) <= 1024, tool["name"]
        schema = tool["input_schema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False, tool["name"]
        for path, node in _walk_schema(schema):
            assert set(node) <= INPUT_KEYWORDS, f"{tool['name']} {path}: keyword outside the subset"
            if node.get("type") == "object" or "properties" in node:
                assert node.get("additionalProperties") is False, f"{tool['name']} {path}: open object"
            if "pattern" in node:
                re.compile(node["pattern"])


def test_no_tool_takes_a_customer_id_or_weak_identity_factor():
    """No argument can carry a customer id, customer number, contact data or session: identity comes only from the
    runtime-held session (policy never_authenticates_alone)."""
    weak = re.compile(r"customer|client|cliente|email|e_mail|phone|mobile|full_name|^name$|session|token|password"
                      r"|card_number|product_number|account_number|^cvv$|^pan$")
    for tool in hz.SCHEMAS["tools"]:
        for name in _property_names(tool["input_schema"]):
            if name == "customer_confirmed":
                continue
            assert not weak.search(name), f"{tool['name']} takes {name}"
            if name.startswith("document"):
                assert tool["name"] == "start_authentication", f"{tool['name']} takes {name}"


def test_create_dispute_case_cannot_receive_claimed_amounts():
    """use_claimed_amount is enforced structurally: no amount, currency or date argument exists."""
    props = set(hz.TOOLS["create_dispute_case"]["input_schema"]["properties"])
    assert props == {"confirmation_id", "transaction_id", "customer_confirmed", "idempotency_key"}
    assert set(hz.TOOLS["create_dispute_case"]["input_schema"]["required"]) == props


def test_handoff_reason_enums_are_derived_from_the_policy():
    expected = [t["reason"] for t in hz.POLICY["handoff"]["triggers_in_order"]] + ["complaint_routing"]
    assert "complaint_routing" in hz.POLICY["scope"]["on_other_complaint"]
    assert hz.TOOLS["handoff_to_human"]["input_schema"]["properties"]["reason_code"]["enum"] == expected
    assert hz.SCHEMAS["$defs"]["TicketView"]["properties"]["reason_code"]["enum"] == expected
    decision = hz.TOOLS["prepare_dispute_case"]["output_schema"]["properties"]["policy_decision"]["properties"]
    assert decision["handoff_reasons_all"]["items"]["enum"] == expected


def test_error_catalog_is_complete_and_only_rate_limits_are_retryable():
    envelope_error = hz.SCHEMAS["envelope"]["properties"]["error"]["properties"]
    assert set(envelope_error["code"]["enum"]) == set(hz.ERRORS) == set(hz.ERROR_DETAIL_KEYS)
    actions = set(envelope_error["details"]["properties"]["next_action"]["enum"])
    for code, spec in hz.ERRORS.items():
        assert spec["message"] and "{" not in spec["message"], code
        assert set(spec["next_action"]) <= actions, code
        assert spec["retryable"] is (code == "RATE_LIMITED"), code
    assert "Do not tell the customer that anything was done" in hz.ERRORS["UNAVAILABLE"]["message"]


def test_output_schemas_have_no_personal_or_internal_fields():
    forbidden = re.compile(gold_lib.load_spec()["privacy"]["forbidden_column_pattern"])
    internal = {"customer_id", "amount_usd", "document_hash", "document_type", "segment", "credit_score", "income",
                "is_fraud", "fraud_score", "session_token", "code", "otp", "process_date", "city", "latitude",
                "longitude", "branch_id", "product_number_raw", "threshold", "amount_usd_threshold"}
    schemas = [t["output_schema"] for t in hz.SCHEMAS["tools"]] + list(hz.SCHEMAS["$defs"].values())
    for schema in schemas:
        for name in _property_names(schema):
            assert not forbidden.search(name), name
            assert name not in internal, name


def test_merchant_is_only_ever_untrusted_text():
    for view in ("TransactionView", "VerifiedFacts"):
        merchant = hz.SCHEMAS["$defs"][view]["properties"]["merchant"]
        assert merchant == {"anyOf": [{"$ref": "#/$defs/UntrustedText"}, {"type": "null"}]}
    wrapper = hz.SCHEMAS["$defs"]["UntrustedText"]
    assert wrapper["properties"]["untrusted_text"]["maxLength"] == 160
    assert set(wrapper["properties"]["flags"]["items"]["enum"]) == {"instruction_like", "markup", "truncated"}


def test_policy_snippets_never_reference_internal_values():
    path = hz.REPO_ROOT / "src" / "bank_tools" / "policy_snippets.json"
    if not path.exists():
        pytest.skip("policy_snippets.json not written yet")
    text = path.read_text(encoding="utf-8")
    for internal in ("handoff.amount_usd_threshold", "threshold_calibration", "high_amount_usd", "medium_amount_usd",
                     "transaction_matching.", "min_intent_confidence", "max_clarifications_before_handoff",
                     "tool_max_retries", "triggers_in_order", "must_not_vocabulary"):
        assert internal not in text, internal
    for placeholder in re.findall(r"\{policy:([a-z_][a-z0-9_.]*)\}", text):
        node = hz.POLICY
        for part in placeholder.split("."):
            assert isinstance(node, dict) and part in node, f"unresolvable placeholder {placeholder}"
            node = node[part]


def test_fixture_snapshot_has_exactly_the_gold_columns(snapshot_path):
    spec = gold_lib.load_spec()
    con = sqlite3.connect(str(snapshot_path))
    try:
        for table in fx.TABLES:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
            assert cols == list(spec["tables"][table]["columns"]), table
            assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] > 0, table
    finally:
        con.close()


def test_fixture_rows_mean_what_the_tests_assume():
    now, row = fx.NOW, fx.row
    unrec, incorrect = dp.DISPUTE_INTENTS
    assert dp.handoff_reasons({"transaction": row("big"), "now": now}) == ["amount_above_threshold"]
    assert dp.handoff_reasons({"transaction": row("old"), "now": now}) == ["outside_dispute_window"]
    assert dp.days_since(row("boundary")["event_date"], now) == hz.POLICY["dispute_window"]["days_since_transaction"]
    assert dp.in_dispute_window(row("boundary")["event_date"], now)
    assert not dp.in_dispute_window(row("boundary")["event_date"], "2026-06-20T00:05:00")
    assert dp.handoff_reasons({"transaction": row("super"), "now": now}) == []
    assert dp.is_eligible(row("super"), fx.C1, unrec) == (True, "ok")
    assert dp.is_eligible(row("fee"), fx.C1, unrec) == (False, "type_adjustment")
    assert dp.is_eligible(row("fee"), fx.C1, incorrect) == (True, "ok")
    assert dp.is_eligible(row("decl54"), fx.C1, unrec) == (False, "status_declined")
    assert dp.is_eligible(row("deposit"), fx.C1, incorrect) == (False, "type_deposit")
    assert dp.is_eligible(row("flagtrap"), fx.C1, unrec) == (True, "ok")
    assert row("flagtrap")["dispute_eligible_unrecognized"] is False and row("flagtrap")["above_handoff_threshold"]
    assert row("notowned")["product_owner_matches"] is False
    assert fx.PRODUCT_BY_ID[row("notowned")["product_id"]]["customer_id"] == fx.C2
    assert dp.as_datetime(row("future_today")["event_ts"]) > now > dp.as_datetime(row("super")["event_ts"])
    assert dp.as_datetime(row("future")["event_ts"]) > now
    assert row("implausible")["channel"] is None and row("implausible")["implausible_type_channel"]
    assert dp.build_case(unrec, row("intl"), fx.C1)["priority"] == "high"
    assert dp.build_case(unrec, row("super"), fx.C1)["priority"] == "medium"
    assert len(fx.LONG_MERCHANT) > 160 and "\x07" in fx.LONG_MERCHANT
    assert dp.merchant_similarity("Farmacia Central", fx.INJECTED_MERCHANT) >= 0.8
    assert dp.merchant_similarity("super ahoro", "Super Ahorro") >= 0.8
    assert fx.PRODUCT_BY_ID[fx.P["expired"]]["effective_status"] == "Expired"
    assert fx.expected_match(fx.C1, "dispute", unrec, {"amount": 32100.0})["status"] == "multiple"
    assert fx.expected_match(fx.C1, "dispute", unrec, {"merchant": "Super Ahorro"})["matches"] == [fx.T["super"]]
    assert fx.expected_match(fx.C1, "dispute", None, {"merchant": "Cafe Andino"})["matches"] == [fx.T["old"]]
    assert fx.expected_match(fx.C1, "decline_inquiry", None, {"merchant": "Tienda Norte"})["matches"] == [
        fx.T["decl54"]]
    exp, _, offer = fx.expected_decline("decl14", "es")
    assert exp["inconsistent_code"] and offer
    exp, _, offer = fx.expected_decline("decl54", "es")
    assert exp["reason"] == "expired_card" and not offer
    for t in fx.TRANSACTIONS:
        assert re.fullmatch(r"TRX-[A-Z0-9]{20}", t["transaction_id"]) and t["amount"] > 0
    for cid in fx.CUSTOMERS:
        assert re.fullmatch(r"CLI-[A-Z0-9]{12}", cid)
