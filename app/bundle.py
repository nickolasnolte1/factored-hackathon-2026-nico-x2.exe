"""Build the source folder of the Databricks App: only what the app needs at runtime.

    python -m app.bundle --out <dir>

The folder gets the app code and static files, the runtime modules of src/ (bank tools, policy, intent classifier,
gold_lib and the Gold table spec), the intent model (models/intent_classifier/), the local Gold snapshot and the test customers
(data/bank_tools/), app.yaml and requirements.txt at its root. data/ and models/ are git-ignored: they are uploaded
to the workspace with the app and never committed. No tests, evaluation data, transcripts, audit logs or caches.
Deployment steps: "Deploy as a Databricks App" in app/README.md.
"""
import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "app/__init__.py", "app/agent.py", "app/llm.py", "app/server.py",
    "src/bank_tools/*.py", "src/bank_tools/*.json", "src/bank_tools/repository/*.py", "src/bank_tools/sql/*.sql",
    "src/policy/dispute_policy.py", "src/policy/dispute_policy.json",
    "src/classifier/__init__.py", "src/classifier/data.py", "src/classifier/keywords.py",
    "src/classifier/runtime.py",
    "src/gold/gold_lib.py", "src/gold/gold_tables.json",
    "models/intent_classifier/model.joblib", "models/intent_classifier/model_card.json",
    "data/bank_tools/snapshot_panel.sqlite", "data/bank_tools/demo_personas.json",
]
TREES = ["app/static"]
AT_ROOT = {"app/app.yaml": "app.yaml", "app/requirements.txt": "requirements.txt"}
REQUIRED = ["models/intent_classifier/model.joblib", "data/bank_tools/snapshot_panel.sqlite",
            "data/bank_tools/demo_personas.json"]


def build(out):
    out = Path(out).resolve()
    if out == ROOT or ROOT in out.parents:
        raise SystemExit("choose a folder outside the repository, so the bundle's data is never committed")
    missing = [p for p in REQUIRED if not (ROOT / p).is_file()]
    if missing:
        raise SystemExit("missing (see 'Run it locally' in app/README.md): " + ", ".join(missing))
    if out.exists():
        shutil.rmtree(out)
    copied = 0
    for pattern in FILES:
        matches = sorted(ROOT.glob(pattern))
        if not matches:
            raise SystemExit("nothing matches " + pattern)
        for src in matches:
            dst = out / src.relative_to(ROOT)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
    for tree in TREES:
        shutil.copytree(ROOT / tree, out / tree, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        copied += sum(1 for p in (out / tree).rglob("*") if p.is_file())
    for src, name in AT_ROOT.items():
        shutil.copy2(ROOT / src, out / name)
        copied += 1
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"wrote {copied} files ({size / 1e6:.1f} MB) to {out}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="folder to (re)create, outside the repository")
    build(ap.parse_args(argv).out)


if __name__ == "__main__":
    sys.exit(main())
