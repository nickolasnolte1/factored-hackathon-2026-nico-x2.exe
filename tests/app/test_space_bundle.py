"""The public Space bundle (app/space_bundle.py): code only in the Space folder, the data apart, and a refusal when data
would become public. Runs on a copy of the code with small stand-in data files, so it needs neither data/ nor models/.

    python -m pytest tests/app/test_space_bundle.py -q
"""
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import fetch_assets as fa, space_bundle as sb  # noqa: E402

DOCUMENT = "51234987"  # a stand-in test customer's document number
ASSETS = {
    "data/bank_tools/snapshot_panel.sqlite": fa.SQLITE_HEADER + b"\x00" * 84,
    "data/bank_tools/demo_personas.json": json.dumps({"personas": [{"document_type": "CC",
                                                                    "document_number": DOCUMENT}]}).encode(),
    "models/intent_classifier/model.joblib": b"\x80\x04stand-in model",
    "models/intent_classifier/model_card.json": b'{"model_version": "stand-in"}',
}


@pytest.fixture
def root(tmp_path):
    """A copy of the code the bundles take, plus stand-in data files."""
    base = tmp_path / "repo"
    for pattern in sb.CODE:
        for src in ROOT.glob(pattern):
            dst = base / src.relative_to(ROOT)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    for tree in sb.TREES:
        shutil.copytree(ROOT / tree, base / tree, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for src in sb.AT_SPACE_ROOT:
        (base / src).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / src, base / src)
    for rel, data in ASSETS.items():
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_bytes(data)
    return base


def files(folder):
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


def test_space_gets_code_only_and_data_gets_the_assets(root, tmp_path):
    space, data = sb.build(tmp_path / "out", root=root)
    listed = files(space)
    assert {"Dockerfile", "README.md", "requirements.txt", "requirements-space.txt", "app/server.py",
            "app/fetch_assets.py", "app/static/index.html", "src/bank_tools/service.py"} <= set(listed)
    assert not [p for p in listed if p.startswith(("data/", "models/", "tests/")) or p.endswith((".sqlite", ".joblib"))]
    assert not [p for p in listed if "app_store" in p or "demo_personas" in p or "model_card" in p]
    assert files(data) == sorted(fa.ASSETS)
    assert all((data / rel).read_bytes() == content for rel, content in ASSETS.items())
    assert sb.leaks(space, root) == []


def test_refuses_a_folder_inside_the_repository(root):
    with pytest.raises(SystemExit, match="outside the repository"):
        sb.build(root / "space-out", root=root)
    with pytest.raises(SystemExit, match="outside the repository"):
        sb.build(ROOT / "space-out")
    assert not (ROOT / "space-out").exists()


@pytest.mark.parametrize("rel, content, reason", [
    ("app/static/snapshot.sqlite", b"anything", "file type"),
    ("app/static/backup.bin.txt", ASSETS["models/intent_classifier/model.joblib"], "copy of"),
    ("app/static/notes.js", ("// test login " + DOCUMENT).encode(), "document number"),
    ("app/static/models/keep.txt", b"x", "folder"),
    ("app/static/.env", b"MODE=demo", "credentials"),
    ("app/static/config.js", b"const t = 'dapi" + b"0123456789abcdef" * 2 + b"';", "token"),
    ("app/static/fonts/notes.css", b"/* hf_" + b"A1b2C3d4E5" * 4 + b" */", "token"),
])
def test_fails_and_removes_the_space_folder_when_data_would_leak(root, tmp_path, rel, content, reason):
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes(content)
    with pytest.raises(SystemExit, match=reason):
        sb.build(tmp_path / "out", root=root)
    assert not (tmp_path / "out" / "space").exists() and not (tmp_path / "out" / "data").exists()


def test_space_card_and_dockerfile_follow_the_spaces_rules():
    card = (ROOT / "app/space/README.md").read_text(encoding="utf-8")
    header = re.match(r"^---\n(.*?)\n---\n", card, re.S).group(1)
    meta = dict(line.split(": ", 1) for line in header.splitlines())
    assert meta["title"] == "Expediente" and meta["sdk"] == "docker" and meta["app_port"] == "7860"
    assert len(meta["short_description"]) <= 60
    assert "github.com/nickolasnolte1/factored-hackathon-2026-nico-x2.exe" in card
    docker = (ROOT / "app/space/Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.11-slim" in docker and "--uid 1000" in docker and "USER user" in docker
    assert "--port 7860" in docker and "--workers 1" in docker and "python -m app.fetch_assets &&" in docker
    copies = re.findall(r"^COPY .*$", docker, re.M)
    assert copies and not [c for c in copies if re.search(r"\b(data|models)\b|\s\.\s", c)]
    for setting in ("BANK_TOOLS_ENV=demo", "BANK_TOOLS_REPOSITORY=local", "APP_STORE=/tmp/", "BANK_TOOLS_AUDIT_DIR=/tmp/",
                    "APP_DEMO_CONTROLS=1", "APP_PUBLIC_DEMO=1", "APP_TRUST_FORWARDED_FOR=1",
                    "APP_DAILY_MODEL_TURNS=400", "APP_MAX_CONCURRENT_TURNS=3",
                    "APP_LLM_ENDPOINT=databricks-gpt-oss-120b"):
        assert setting in docker, setting
