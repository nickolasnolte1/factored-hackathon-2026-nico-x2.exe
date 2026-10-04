"""Start step of the public Hugging Face Space (app/fetch_assets.py), with a fake download function: no network and no
huggingface_hub needed.

    python -m pytest tests/app/test_fetch_assets.py -q
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import bundle, fetch_assets as fa  # noqa: E402

TOKEN = "hf_test_only_token_never_printed_0123456789"
ENV = {"HF_DATA_REPO": "team/expediente-data", "HF_TOKEN": TOKEN}
CONTENT = {
    "data/bank_tools/snapshot_panel.sqlite": fa.SQLITE_HEADER + b"\x00" * 84,
    "data/bank_tools/demo_personas.json": json.dumps({"synthetic": True, "personas": []}).encode(),
    "models/intent_classifier/model.joblib": b"\x80\x04fake model",
    "models/intent_classifier/model_card.json": b'{"model_version": "test"}',
}


class Hub:
    """Fake hf_hub_download over a folder that plays the dataset repo; records every call."""

    def __init__(self, folder, content=CONTENT, fail=None):
        self.folder, self.calls, self.fail = Path(folder), [], fail
        for rel, data in content.items():
            (self.folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.folder / rel).write_bytes(data)

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise self.fail
        return str(self.folder / kwargs["filename"])


class Response:
    def __init__(self, status_code):
        self.status_code = status_code


def hub_error(name, status=None, bases=(Exception,)):
    """An exception named like huggingface_hub's, whose text carries the token (it must never be printed)."""
    exc = type(name, bases, {})("401 for https://huggingface.co/api/datasets/x with token " + TOKEN)
    exc.response = Response(status) if status else None
    return exc


def run(root, environ, hub):
    lines = []
    status = fa.fetch(root, environ=environ, download=hub, out=lines.append)
    text = "\n".join(lines)
    assert TOKEN not in text
    return status, text


def test_assets_match_the_data_files_of_the_databricks_bundle():
    assert sorted(fa.ASSETS) == sorted(p for p in bundle.FILES if p.startswith(("data/", "models/")))
    assert set(fa.ASSETS) == set(CONTENT)


def test_nothing_happens_when_every_file_is_in_place(tmp_path):
    for rel, data in CONTENT.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(data)
    hub = Hub(tmp_path / "hub")
    status, text = run(tmp_path, {}, hub)
    assert status == 0 and hub.calls == [] and "already in place" in text


def test_downloads_only_the_missing_files_to_the_paths_the_server_reads(tmp_path):
    root = tmp_path / "app"
    (root / "data/bank_tools").mkdir(parents=True)
    (root / "data/bank_tools/demo_personas.json").write_bytes(b'{"personas": ["kept"]}')
    hub = Hub(tmp_path / "hub")
    status, text = run(root, dict(ENV, HF_DATA_REVISION="v1"), hub)
    assert status == 0
    assert [c["filename"] for c in hub.calls] == [p for p in fa.ASSETS if not p.endswith("demo_personas.json")]
    assert all(c["repo_id"] == "team/expediente-data" and c["repo_type"] == "dataset" and c["token"] == TOKEN
               and c["revision"] == "v1" for c in hub.calls)
    for rel in fa.ASSETS:
        expected = b'{"personas": ["kept"]}' if rel.endswith("demo_personas.json") else CONTENT[rel]
        assert (root / rel).read_bytes() == expected
    assert not list(root.rglob("*.part"))
    assert text.count("downloaded") == 3


def test_missing_files_without_a_repo_or_a_token_fail_clearly(tmp_path):
    hub = Hub(tmp_path / "hub")
    status, text = run(tmp_path / "app", {}, hub)
    assert status == 1 and "HF_DATA_REPO" in text and "snapshot_panel.sqlite" in text and hub.calls == []
    status, text = run(tmp_path / "app", {"HF_DATA_REPO": "team/expediente-data"}, hub)
    assert status == 1 and "HF_TOKEN is not set" in text and hub.calls == []


@pytest.mark.parametrize("repo", [TOKEN, "expediente-data", "https://huggingface.co/datasets/team/expediente-data"])
def test_a_repo_that_is_not_owner_slash_name_is_refused_without_printing_it(tmp_path, repo):
    hub = Hub(tmp_path / "hub")
    status, text = run(tmp_path / "app", {"HF_DATA_REPO": repo, "HF_TOKEN": TOKEN}, hub)
    assert status == 1 and "owner/name" in text and repo not in text and hub.calls == []


@pytest.mark.parametrize("error, expected", [
    (hub_error("RepositoryNotFoundError", 401), "refused HF_TOKEN"),
    (hub_error("RepositoryNotFoundError", 404), "cannot read the dataset repo"),
    (hub_error("HfHubHTTPError", 403), "cannot read the dataset repo"),
    (hub_error("RevisionNotFoundError", 404), "HF_DATA_REVISION"),
    (hub_error("EntryNotFoundError", 404), "is not in 'team/expediente-data'"),
    (hub_error("LocalEntryNotFoundError", None, (hub_error("EntryNotFoundError").__class__,)), "could not reach"),
    (RuntimeError("boom " + TOKEN), "failed (RuntimeError). Restart the Space"),
])
def test_download_errors_are_explained_without_the_token(tmp_path, error, expected):
    hub = Hub(tmp_path / "hub", fail=error)
    status, text = run(tmp_path / "app", ENV, hub)
    assert status == 1 and expected in text, text
    assert not (tmp_path / "app/data/bank_tools/snapshot_panel.sqlite").exists()


def test_a_file_that_is_not_what_it_should_be_is_not_installed(tmp_path):
    hub = Hub(tmp_path / "hub", content=dict(CONTENT, **{"data/bank_tools/snapshot_panel.sqlite": b"<html>login</html>"}))
    status, text = run(tmp_path / "app", ENV, hub)
    assert status == 1 and "not a SQLite database" in text
    assert not (tmp_path / "app/data/bank_tools/snapshot_panel.sqlite").exists()


def test_the_public_demo_warns_when_no_model_credentials_are_set():
    lines = []
    fa.warn_without_model_credentials({"APP_PUBLIC_DEMO": "1"}, out=lines.append)
    assert len(lines) == 1 and "DATABRICKS_CLIENT_ID" in lines[0]
    for environ in ({"APP_PUBLIC_DEMO": "1", "DATABRICKS_CLIENT_ID": "id", "DATABRICKS_CLIENT_SECRET": "secret"},
                    {"APP_PUBLIC_DEMO": "1", "DATABRICKS_TOKEN": "token"}, {}):
        lines = []
        fa.warn_without_model_credentials(environ, out=lines.append)
        assert lines == []
