"""Fixtures for the bank tool service tests (contract: src/bank_tools/CONTRACT.md v1.0.0).

    python -m pytest tests/bank_tools -q                                    # local, no Databricks
    BANK_TOOLS_TEST_DATABRICKS=1 python -m pytest tests/bank_tools -q -m databricks   # live Gold checks

The suite builds its own synthetic SQLite snapshot (fixture_data.py) and drives the service through call_tool,
for_model() and the classes named in the contract (harness.py). It was written from the contract, not from the
implementation. Tests that need the service are skipped while src/bank_tools/service.py does not exist.

After every test, each service built by `make_bank` is checked for leaks: no customer id, document number or hash,
one-time code or session token in any model-facing output or audit record.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.bank_tools import fixture_data as fx  # noqa: E402
from tests.bank_tools import harness as hz  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "databricks: needs a Databricks SQL warehouse (BANK_TOOLS_TEST_DATABRICKS=1)")


@pytest.fixture(scope="session")
def snapshot_path(tmp_path_factory):
    return fx.write_snapshot(tmp_path_factory.mktemp("bank_tools_fixture") / "snapshot_fixture.sqlite")


@pytest.fixture(autouse=True)
def service_env(monkeypatch, tmp_path, snapshot_path):
    env = hz.service_env(tmp_path, snapshot_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture
def make_bank(snapshot_path, monkeypatch):
    """Factory: make_bank(faults=None, repo=None, env=None, now=NOW, snapshot=None, audit_sink=None) -> Bank."""
    banks = []

    def factory(faults=None, repo=None, env=None, now=fx.NOW, snapshot=None, audit_sink=None):
        hz.require_impl()
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, str(value))
        bank = hz.Bank(snapshot or snapshot_path, now=now, faults=faults, repo=repo, audit_sink=audit_sink)
        banks.append(bank)
        return bank

    yield factory
    for bank in banks:
        bank.assert_no_leaks()


@pytest.fixture
def bank(make_bank):
    return make_bank()
