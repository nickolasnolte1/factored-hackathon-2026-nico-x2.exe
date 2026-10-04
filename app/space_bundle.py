"""Build the two folders of the public demo on Hugging Face Spaces, outside the repository.

    python -m app.space_bundle --out <dir>

<dir>/space is the Space repository, which anyone can read: the Dockerfile and the Space card (README.md) of
app/space/, the requirements (app/requirements.txt and app/requirements-space.txt), app/fetch_assets.py, and the code
files of the Databricks App folder (app/bundle.py: the app code and static files and the runtime modules of src/).
No data, no model, no test customers.

<dir>/data holds the files for the private dataset repo: the Gold snapshot, the test customers and the intent model,
at the relative paths app/fetch_assets.py downloads them to.

The build refuses a folder inside the repository, and fails, removing <dir>/space, when a data or model file would
land there: by path, by file type, by content (a copy of any data file) or by a test customer's document number. It
also fails on an env or key file and on a Databricks or Hugging Face token in any file.
Steps: "Public demo on Hugging Face Spaces" in app/README.md.
"""
import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

from .bundle import FILES, ROOT, TREES
from .fetch_assets import ASSETS

DATA_PREFIXES = ("data/", "models/")
CODE = [p for p in FILES if not p.startswith(DATA_PREFIXES)] + ["app/fetch_assets.py"]
DATA = [p for p in FILES if p.startswith(DATA_PREFIXES)]
AT_SPACE_ROOT = {"app/space/Dockerfile": "Dockerfile", "app/space/README.md": "README.md",
                 "app/requirements.txt": "requirements.txt", "app/requirements-space.txt": "requirements-space.txt"}
DATA_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".joblib", ".pkl", ".pickle", ".parquet", ".csv", ".jsonl", ".npy",
                 ".npz", ".safetensors", ".pt", ".bin"}
DATA_NAMES = {Path(p).name for p in ASSETS} | {"app_store.sqlite", "snapshot_panel.manifest.json"}
TEXT_SUFFIXES = {".py", ".json", ".sql", ".md", ".txt", ".html", ".css", ".js", ""}
# Credentials never belong in the Space repo either: env files, key files, and Databricks or Hugging Face tokens.
SECRET_NAMES = re.compile(r"^\.env|\.(pem|key|p12|pfx)$|^\.(netrc|databrickscfg)$|credentials", re.I)
SECRET_TEXT = re.compile(rb"dapi[0-9a-f]{32}|dose[0-9a-f]{32}|hf_[A-Za-z0-9]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _documents(personas_path):
    """The test customers' document numbers (only long enough ones, so a short number never matches by chance)."""
    try:
        personas = json.loads(Path(personas_path).read_text(encoding="utf-8")).get("personas") or []
    except (OSError, ValueError):
        return set()
    docs = {str(p.get("document_number") or "").strip() for p in personas}
    return {d for d in docs if len(d) >= 6}


def leaks(space, root=ROOT):
    """Files of the space folder that look like data, a model or credentials, with the reason; empty when the folder
    is clean."""
    space = Path(space)
    asset_hashes = {_sha256(root / p): p for p in ASSETS if (root / p).is_file()}
    documents = _documents(root / "data/bank_tools/demo_personas.json")
    found = []
    for file in sorted(f for f in space.rglob("*") if f.is_file()):
        rel = file.relative_to(space).as_posix()
        copy_of = asset_hashes.get(_sha256(file))
        if "/data/" in "/" + rel or "/models/" in "/" + rel:
            found.append((rel, "data or models folder"))
        elif file.suffix.lower() in DATA_SUFFIXES or file.name in DATA_NAMES:
            found.append((rel, "data or model file type"))
        elif copy_of:
            found.append((rel, "copy of " + copy_of))
        elif SECRET_NAMES.search(file.name) or SECRET_TEXT.search(file.read_bytes()):
            found.append((rel, "looks like a token or credentials file"))
        elif documents and file.suffix.lower() in TEXT_SUFFIXES:
            text = file.read_text(encoding="utf-8", errors="ignore")
            if any(d in text for d in documents):
                found.append((rel, "contains a test customer's document number"))
    return found


def _copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _size(folder):
    files = [p for p in Path(folder).rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def build(out, root=ROOT):
    root = Path(root).resolve()
    out = Path(out).resolve()
    if out == root or root in out.parents:
        raise SystemExit("choose a folder outside the repository, so neither folder is ever committed")
    if sorted(DATA) != sorted(ASSETS):
        raise SystemExit("app/bundle.py and app/fetch_assets.py list different data files: " +
                         ", ".join(sorted(set(DATA) ^ set(ASSETS))))
    missing = [p for p in ASSETS if not (root / p).is_file()]
    if missing:
        raise SystemExit("missing (see 'Run it locally' in app/README.md): " + ", ".join(missing))
    space, data = out / "space", out / "data"
    for folder in (space, data):  # only these two: anything else in <dir> is left alone
        if folder.exists():
            shutil.rmtree(folder)

    for pattern in CODE:
        matches = sorted(root.glob(pattern))
        if not matches:
            raise SystemExit("nothing matches " + pattern)
        for src in matches:
            _copy(src, space / src.relative_to(root))
    for tree in TREES:
        shutil.copytree(root / tree, space / tree, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for src, name in AT_SPACE_ROOT.items():
        _copy(root / src, space / name)

    found = leaks(space, root)
    if found:
        shutil.rmtree(space)
        raise SystemExit("refused: these would make data or the model public in the Space repo (folder removed):\n" +
                         "\n".join(f"  {rel}: {why}" for rel, why in found))

    for rel in ASSETS:
        _copy(root / rel, data / rel)

    for name, folder in (("space (public Space repo)", space), ("data (private dataset repo)", data)):
        count, size = _size(folder)
        print(f"{name}: {count} files, {size / 1e6:.1f} MB in {folder}")
    return space, data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="folder outside the repository; its space/ and data/ are (re)created")
    build(ap.parse_args(argv).out)


if __name__ == "__main__":
    sys.exit(main())
