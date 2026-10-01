"""DatabricksRepository (CONTRACT §1.5, §12.3, §12.12).

Offline: SQL constants use named parameters only, and every Statement Execution request carries `parameters`
(requests is patched; nothing leaves the machine), with the document hash, never the number.
Live (BANK_TOOLS_TEST_DATABRICKS=1, plus DATABRICKS_WAREHOUSE_ID and a CLI profile or host/token): reads through
workspace.gold, and parity with the local panel snapshot when it exists.
"""
import json
import os
import re

import pytest

from src.gold import gold_lib
from tests.bank_tools import fixture_data as fx
from tests.bank_tools import harness as hz
from tests.bank_tools.harness import expect_error, expect_ok, without_meta

LIVE = os.environ.get("BANK_TOOLS_TEST_DATABRICKS") == "1"
live = pytest.mark.skipif(not LIVE, reason="set BANK_TOOLS_TEST_DATABRICKS=1 to run against the SQL warehouse")
PANEL = hz.REPO_ROOT / "data" / "scenarios" / "panel.jsonl"
PANEL_SNAPSHOT = hz.REPO_ROOT / "data" / "bank_tools" / "snapshot_panel.sqlite"
DEMO_NOW = "2026-06-19T09:00:00"


def test_sql_constants_use_named_parameters_only():
    hz.require_impl()
    module = hz._module("src.bank_tools.repository.sql")
    if module is None:
        pytest.skip("repository/sql.py not written yet")
    statements = {name: value for name, value in vars(module).items() if isinstance(value, str)
                  and re.search(r"\b(SELECT|INSERT|MERGE|DELETE|UPDATE)\b", value, re.IGNORECASE)}
    assert statements, "every SQL statement is a module constant"
    for name, sql in statements.items():
        assert not re.search(r"'(CLI|TRX|PRD|DSP|HND|CHL)-", sql), f"{name}: literal id in SQL"
        assert "%s" not in sql and "%(" not in sql, f"{name}: %-style parameter"
        for placeholder in filter(None, re.findall(r"\{([^{}]*)\}", sql)):
            assert re.search(r"catalog|schema|gold|ops|table", placeholder, re.IGNORECASE), f"{name}: {{{placeholder}}}"
        for column in ("customer_id", "transaction_id", "product_id", "case_id", "ticket_id", "document_hash"):
            for match in re.finditer(rf"\b{column}\s*=\s*([^\s,)]+)", sql):
                rhs = match.group(1)
                assert rhs.startswith(":") or "." in rhs, f"{name}: {column} compared with {rhs}"


class _FakeResponse:
    status_code = 200
    ok = True
    headers = {"Content-Type": "application/json"}

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def test_statement_requests_carry_named_parameters_and_never_the_document_number(monkeypatch):
    hz.require_impl()
    if hz.impl().build_service is None:
        pytest.skip("build_service not available")
    import requests

    captured = []

    def fake_request(self, method, url, *args, **kwargs):
        captured.append({"method": method, "url": url, "json": kwargs.get("json"), "data": kwargs.get("data")})
        return _FakeResponse({"statement_id": "stmt-test", "status": {"state": "SUCCEEDED"},
                              "manifest": {"format": "JSON_ARRAY", "schema": {"column_count": 0, "columns": []},
                                           "total_row_count": 0, "truncated": False},
                              "result": {"row_count": 0, "data_array": []}})

    monkeypatch.setattr(requests.sessions.Session, "request", fake_request)
    for key, value in {"BANK_TOOLS_REPOSITORY": "databricks", "DATABRICKS_HOST": "https://workspace.example.invalid",
                       "DATABRICKS_TOKEN": "test-only-not-a-token", "DATABRICKS_WAREHOUSE_ID": "test-warehouse",
                       "DATABRICKS_CLI": "missing-cli-for-tests"}.items():
        monkeypatch.setenv(key, value)
    service = hz.impl().build_service()
    doc_type, number = fx.CUSTOMERS[fx.C1]["document"]
    ctx = hz.impl().ctx("c-dbx-offline", 1, "0" * 32, "runtime")
    service.call_tool("start_authentication", {"document_type": doc_type, "document_number": number}, None, ctx)
    statements = [c for c in captured if isinstance(c["json"], dict) and "statement" in c["json"]]
    assert statements, "the identity lookup goes through the Statement Execution API"
    document_hash = gold_lib.document_hash(doc_type, number)
    for call in captured:
        body = json.dumps(call["json"] or call["data"] or "", default=str)
        assert not hz.contains_token(body, number), "the document number left the process"
    lookups = [c for c in statements if document_hash in json.dumps(c["json"].get("parameters") or [])]
    assert lookups, "the document hash is sent as a named parameter"
    for call in statements:
        assert isinstance(call["json"].get("parameters", []), list)
        assert document_hash not in call["json"]["statement"]
        assert call["json"].get("warehouse_id") == "test-warehouse"


# ---------------------------------------------------------------- live checks (skipped by default)


def _live_service(monkeypatch, repository):
    hz.require_impl()
    if not os.environ.get("DATABRICKS_WAREHOUSE_ID"):
        pytest.skip("DATABRICKS_WAREHOUSE_ID is not set")
    monkeypatch.setenv("BANK_TOOLS_REPOSITORY", repository)
    monkeypatch.setenv("BANK_TOOLS_CLOCK", DEMO_NOW)
    monkeypatch.setenv("BANK_TOOLS_SNAPSHOT", str(PANEL_SNAPSHOT))
    if not os.environ.get("DATABRICKS_HOST"):
        monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", os.environ.get("DATABRICKS_CONFIG_PROFILE", "factored"))
    return hz.impl().build_service()


def _panel_customers(n_active=3, n_restricted=2):
    if not PANEL.exists():
        pytest.skip("data/scenarios/panel.jsonl is not available locally")
    active, restricted = [], []
    with PANEL.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            group = restricted if row["customer_status"] in hz.POLICY["handoff"]["restricted_customer_statuses"] else active
            group.append(row["customer_id"])
            if len(active) >= n_active and len(restricted) >= n_restricted:
                break
    return active[:n_active], restricted[:n_restricted]


def _call(service, token, tool, args, conversation="c-live"):
    ctx = hz.impl().ctx(conversation, 1, "1" * 32, "model")
    return service.call_tool(tool, args, token, ctx).for_model()


@live
@pytest.mark.databricks
def test_live_unknown_document_gets_a_decoy(monkeypatch):
    service = _live_service(monkeypatch, "databricks")
    ctx = hz.impl().ctx("c-live-auth", 1, "2" * 32, "runtime")
    env = service.call_tool("start_authentication", {"document_type": "CC", "document_number": "0000000000001"},
                            None, ctx).for_model()
    data = expect_ok(env)
    assert data["delivery"]["status"] == "sent" and re.fullmatch(r"CHL-[A-Z0-9]{12}", data["challenge_id"])


@live
@pytest.mark.databricks
def test_live_gold_reads_follow_the_contract(monkeypatch):
    service = _live_service(monkeypatch, "databricks")
    active, restricted = _panel_customers()
    for customer_id in active:
        token = service.identity.issue_test_session(customer_id)
        overview = expect_ok(_call(service, token, "get_customer_overview", {}))
        hz.check_output("get_customer_overview", overview)
        products = expect_ok(_call(service, token, "list_products", {}))
        hz.check_output("list_products", products)
        movements = _call(service, token, "list_recent_transactions", {"limit": 20})
        hz.check_output("list_recent_transactions", expect_ok(movements))
        assert not hz.pii_hits(movements) and "CLI-" not in json.dumps([overview, products, movements])
        assert all(t["event_ts"] <= DEMO_NOW[:16] for t in movements["data"]["transactions"])
    for customer_id in restricted:
        token = service.identity.issue_test_session(customer_id)
        overview = expect_ok(_call(service, token, "get_customer_overview", {}))
        assert overview["service_restriction"]["handoff_required"] is True
        expect_error(_call(service, token, "list_products", {}), "POLICY_BLOCKED", reason="customer_status_restricted")


@live
@pytest.mark.databricks
def test_live_local_snapshot_and_gold_return_identical_outputs(monkeypatch):
    if not PANEL_SNAPSHOT.exists():
        pytest.skip("local panel snapshot not built (python -m src.bank_tools.snapshot)")
    remote = _live_service(monkeypatch, "databricks")
    local = _live_service(monkeypatch, "local")
    active, restricted = _panel_customers(4, 1)
    for customer_id in active + restricted:
        outputs = []
        for service in (local, remote):
            token = service.identity.issue_test_session(customer_id)
            overview = without_meta(_call(service, token, "get_customer_overview", {}))
            overview.get("data", {}).pop("session", None)
            outputs.append([overview] + [without_meta(_call(service, token, tool, args)) for tool, args in (
                ("list_products", {}), ("list_recent_transactions", {"limit": 20}),
                ("find_candidate_transactions", {"purpose": "dispute", "hints": {}}))])
        assert outputs[0] == outputs[1], "LocalRepository and DatabricksRepository disagree"
