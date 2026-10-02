"""One-command rebuild of the scenario datasets.

    python -m src.scenarios.build --seed 20261005                     # generate from the local anchors
    python -m src.scenarios.build --seed 20261005 --extract \\
        --warehouse-id <id> [--profile factored] [--cli databricks]   # re-extract anchors from Silver first
    python -m src.scenarios.build --seed 20261005 --extract --pin-from data/scenarios/silver_snapshot.json ...
                                                                      # same Delta versions as the last snapshot
Steps: validate the families -> (extract anchors) -> policy unit tests -> generate -> regenerate into a temporary
folder and compare checksums with manifest.json (determinism check).
"""
import argparse
import json
import os
import re
import sys
import tempfile
import unittest

from src.scenarios import extract_anchors, generate
from src.scenarios.anchor_specs import spec_for

SLOTS = {"amount", "date", "merchant", "channel", "product", "txn_type"}
INTENTS = {"dispute_unrecognized_charge", "dispute_incorrect_charge_or_fee", "account_payment_inquiry",
           "card_lost_or_block", "other_complaint", "out_of_scope"}


def validate_families():
    """Structural checks the generator relies on (wording, style and language quality are not checked by code)."""
    errors, seen = [], set()
    for lang in ("es", "pt"):
        doc = json.load(open(os.path.join(generate.FAMILY_DIR, f"{lang}.json"), encoding="utf-8"))
        for f in doc["families"]:
            fid = f["family_id"]
            if not fid.startswith(lang + "-") or fid in seen:
                errors.append(f"{fid}: bad or duplicate id")
            seen.add(fid)
            acc = f["acceptable_intents"]
            if f["intent"] not in INTENTS or not set(acc) <= INTENTS or f["intent"] not in acc:
                errors.append(f"{fid}: intents")
            if f["is_ambiguous"] != (len(acc) > 1):
                errors.append(f"{fid}: is_ambiguous vs acceptable_intents")
            for t in f["templates"]:
                if set(re.findall(r"\{([a-z_]+)\}", t)) != set(f["requires"]) or not set(f["requires"]) <= SLOTS:
                    errors.append(f"{fid}: placeholders != requires in {t!r}")
            try:
                spec_for(f)
            except KeyError as e:
                errors.append(str(e))
    if errors:
        sys.exit("family validation failed:\n  " + "\n  ".join(errors))
    print(f"families: {len(seen)} valid")


def run_policy_tests():
    suite = unittest.defaultTestLoader.loadTestsFromName("src.policy.test_dispute_policy")
    result = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w")).run(suite)
    if not result.wasSuccessful():
        sys.exit(f"policy tests failed: {len(result.failures)} failures, {len(result.errors)} errors")
    print(f"policy tests: {result.testsRun} passed")


def verify_determinism(seed):
    manifest = json.load(open(os.path.join(generate.DATA_DIR, "manifest.json"), encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        generate.main(["--seed", str(seed), "--out-dir", tmp])
        for name, info in manifest["outputs"].items():
            again = generate.sha256_file(os.path.join(tmp, name))
            if again != info["sha256"]:
                sys.exit(f"non-deterministic output: {name}")
    print("determinism: regenerated outputs match manifest.json checksums")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=20261005)
    ap.add_argument("--extract", action="store_true", help="re-extract anchors from workspace.silver first")
    ap.add_argument("--warehouse-id", default=os.environ.get("DATABRICKS_WAREHOUSE_ID"))
    ap.add_argument("--profile", default=os.environ.get("DATABRICKS_CONFIG_PROFILE", "factored"))
    ap.add_argument("--cli", default=os.environ.get("DATABRICKS_CLI", "databricks"))
    ap.add_argument("--pin-from", help="reuse the Delta versions of an earlier silver_snapshot.json")
    ap.add_argument("--skip-verify", action="store_true", help="skip the determinism re-run")
    args = ap.parse_args(argv)

    validate_families()
    anchors = os.path.join(generate.DATA_DIR, "anchors.jsonl")
    if args.extract or not os.path.exists(anchors):
        cmd = ["--seed", str(args.seed), "--profile", args.profile, "--cli", args.cli]
        if args.warehouse_id:
            cmd += ["--warehouse-id", args.warehouse_id]
        if args.pin_from:
            cmd += ["--pin-from", args.pin_from]
        extract_anchors.main(cmd)
    run_policy_tests()
    generate.main(["--seed", str(args.seed)])
    if not args.skip_verify:
        verify_determinism(args.seed)


if __name__ == "__main__":
    main()
