"""Fixtures for the agent evaluation harness tests (no network: the agent runs on a scripted fake model).

    python -m pytest tests/agent_eval -q

The bank service runs on the tiny synthetic snapshot of tests/bank_tools/fixture_data.py, written per session.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.bank_tools import fixture_data as fx  # noqa: E402
from tests.bank_tools import harness as hz  # noqa: E402


@pytest.fixture(scope="session")
def snapshot_path(tmp_path_factory):
    return fx.write_snapshot(tmp_path_factory.mktemp("agent_eval_fixture") / "snapshot_fixture.sqlite")


@pytest.fixture(autouse=True)
def service_env(monkeypatch, tmp_path, snapshot_path):
    for key, value in hz.service_env(tmp_path, snapshot_path).items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def env(snapshot_path):
    from src.agent_eval import harness
    from src.agent_eval.score import SnapshotLookup
    from src.bank_tools.schemas import ToolSchemas
    from src.policy import dispute_policy as dp
    lookup = SnapshotLookup(str(snapshot_path))
    yield {"snapshot": str(snapshot_path), "cfg": harness.eval_config(), "schemas": ToolSchemas(),
           "pol": dp.load_policy(), "lookup": lookup}
    lookup.close()
