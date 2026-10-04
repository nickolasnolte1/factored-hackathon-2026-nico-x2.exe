"""Download the demo's data and model when they are missing: the start step of the public Hugging Face Space.

    python -m app.fetch_assets

The Space repository is public and holds code only. The Gold snapshot, the test customers and the intent model live
in a private Hugging Face dataset repo, at the same relative paths they have here (python -m app.space_bundle writes
that folder). At start, every file of ASSETS that is missing is downloaded from the repo named by HF_DATA_REPO with
the read token in HF_TOKEN (both Space secrets; HF_DATA_REVISION optionally pins a branch, tag or commit) and put
where the server reads it. Files already present are left alone, so on a machine that has them this does nothing.

The token is never printed. Exit status 0 when every file is in place, 1 otherwise, with a message that says what to
fix. Steps: "Public demo on Hugging Face Spaces" in app/README.md.
"""
import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Relative to the repository root here, to the app folder in the Space, and to the root of the dataset repo.
ASSETS = [
    "data/bank_tools/snapshot_panel.sqlite",
    "data/bank_tools/demo_personas.json",
    "models/intent_classifier/model.joblib",
    "models/intent_classifier/model_card.json",
]
SQLITE_HEADER = b"SQLite format 3\x00"
# owner/name. Anything else is never printed: a token pasted into the wrong setting would end up in the logs.
REPO_ID = re.compile(r"^[A-Za-z0-9][\w.-]{0,95}/[A-Za-z0-9][\w.-]{0,95}$")


def missing(root=ROOT):
    return [p for p in ASSETS if not (Path(root) / p).is_file()]


def _flag(environ, name):
    return str(environ.get(name, "")).strip().lower() not in ("", "0", "false", "no", "off")


def explain(exc, repo, path):
    """What went wrong, from the exception's class names and HTTP status only: never its text, which could echo a
    request."""
    names = {c.__name__ for c in type(exc).__mro__}
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 401:
        return (f"the Hub refused HF_TOKEN for {repo!r} (HTTP 401): the token is wrong, expired or revoked, cannot "
                "read that repo, or HF_DATA_REPO names a repo that does not exist.")
    # LocalEntryNotFoundError (the Hub could not be reached) is also an EntryNotFoundError: it goes first.
    if names & {"LocalEntryNotFoundError", "ConnectionError", "ConnectError", "Timeout", "TimeoutError",
                "TimeoutException", "NetworkError"}:
        return f"could not reach huggingface.co to download {path}; restart the Space to try again."
    if "RevisionNotFoundError" in names:
        return f"HF_DATA_REVISION is not a branch, tag or commit of {repo!r}."
    if "EntryNotFoundError" in names:
        return f"{path} is not in {repo!r}: upload the data folder that python -m app.space_bundle builds."
    if names & {"RepositoryNotFoundError", "GatedRepoError", "DisabledRepoError"} or status in (403, 404):
        return (f"cannot read the dataset repo {repo!r}: it does not exist, or HF_TOKEN cannot read it. Check "
                "HF_DATA_REPO (owner/name, a dataset repo) and that the token has read access to it.")
    # Also a network failure on huggingface_hub 1.19: after a refused connection its retry raises RuntimeError.
    return (f"download of {path} failed ({type(exc).__name__}" + (f", HTTP {status})" if status else ")") +
            ". Restart the Space to try again; if it fails again, check HF_DATA_REPO, HF_TOKEN and the Hub's status.")


def check(path, file):
    """A quick look at a downloaded file before it is put in place; None when it looks right."""
    file = Path(file)
    if not file.is_file() or file.stat().st_size == 0:
        return f"{path} came down empty"
    if path.endswith(".sqlite"):
        with open(file, "rb") as fh:
            if fh.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
                return f"{path} is not a SQLite database"
    if path.endswith(".json"):
        try:
            json.loads(file.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            return f"{path} is not valid JSON"
    return None


def fetch(root=ROOT, environ=None, download=None, out=print):
    """Download the missing assets into root. Returns the exit status (0 when every asset is in place)."""
    environ = os.environ if environ is None else environ
    root = Path(root)
    todo = missing(root)
    if not todo:
        out("fetch_assets: data and model already in place")
        return 0
    repo = str(environ.get("HF_DATA_REPO", "")).strip()
    token = str(environ.get("HF_TOKEN", "")).strip()
    if not repo:
        out("fetch_assets: missing " + ", ".join(todo) + ". Set HF_DATA_REPO (the private dataset repo, owner/name) "
            "and HF_TOKEN (a token that can read it), or copy the files here (see 'Run it locally' in app/README.md).")
        return 1
    if not REPO_ID.match(repo):
        out("fetch_assets: HF_DATA_REPO must name the private dataset repo as owner/name, without a URL (the value "
            "set is not printed).")
        return 1
    if not token:
        out(f"fetch_assets: HF_TOKEN is not set. {repo!r} is private: add a read token as the Space secret HF_TOKEN.")
        return 1
    if download is None:
        try:
            from huggingface_hub import hf_hub_download as download
        except ImportError:
            out("fetch_assets: huggingface_hub is not installed (pip install -r app/requirements-space.txt)")
            return 1
    revision = str(environ.get("HF_DATA_REVISION", "")).strip() or None
    for path in todo:
        try:
            cached = download(repo_id=repo, filename=path, repo_type="dataset", revision=revision, token=token)
        except Exception as exc:  # noqa: BLE001 - every failure becomes one readable line, without the token
            out("fetch_assets: " + explain(exc, repo, path))
            return 1
        problem = check(path, cached)
        if problem:
            out("fetch_assets: " + problem + f" (in {repo!r}); upload the data folder that python -m app.space_bundle builds.")
            return 1
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        shutil.copyfile(cached, part)
        os.replace(part, dest)  # a half-copied file is never left where the server reads
        out(f"fetch_assets: downloaded {path} ({dest.stat().st_size / 1e6:.1f} MB)")
    return 0


def warn_without_model_credentials(environ=None, out=print):
    """In the public demo, say early when no model credentials are set: every turn would get the fixed fallback."""
    environ = os.environ if environ is None else environ
    if not _flag(environ, "APP_PUBLIC_DEMO"):
        return
    has_token = bool(str(environ.get("DATABRICKS_TOKEN", "")).strip())
    has_client = all(str(environ.get(v, "")).strip() for v in ("DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET"))
    if not (has_token or has_client):
        out("fetch_assets: warning: neither DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET nor DATABRICKS_TOKEN "
            "is set, so every model call will fail. Add them as Space secrets.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args(argv)
    status = fetch()
    warn_without_model_credentials()
    return status


if __name__ == "__main__":
    sys.exit(main())
