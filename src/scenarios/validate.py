"""Leakage and hygiene checks for the scenario datasets, including overlap with the independent holdout.

    python -m src.scenarios.validate                 # prints one line per check, exit code 1 if any fails
    python -m src.scenarios.validate --json out.json # also writes the metrics

Checks:
  1. families        no family_id in two splits
  2. customers       no customer_id in two splits (intent rows, e2e scenarios, anchors, panel); buckets match splits.py
  3. transactions    no transaction_id in two splits (intent anchors, e2e anchors, e2e gold and candidates)
  4. temporal        test anchors dated >= 2025-07-01, train and dev anchors before
  5. id_tokens       no transaction or product id in any text; customer ids only in the e2e unauthorized-access turns
  6. amounts         amount strings shared by train and test texts, against a permutation null (chance level)
  7. holdout_dups    no holdout text equal to, near or contained in a generated text or template (normalized match,
                     char 3-gram Jaccard, word 3-gram containment in a short text)
  8. holdout_refs    no generator source file mentions the holdout
  9. text_dups       no duplicate message in the intent dataset; no e2e turn equal to an intent message
 10. labels          intent in acceptable_intents, is_ambiguous consistent, attack types valid, all three in test
 11. transfer_view   the ES->PT view holds exactly ES train+dev and PT test
 12. e2e_categories  every e2e category has at least 10 scenarios per language

This module and src/classifier/evaluate.py (the final evaluation) are the only modules under src/ that read
eval/holdout/; this one reads it only to prove that nothing generated overlaps it.
The generator never imports it. Stdlib only.
"""
import argparse
import collections
import glob
import json
import os
import random
import re
import sys
from datetime import date

from src.scenarios import render as R
from src.scenarios.splits import SPLIT_DATE, customer_split

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(REPO, "data", "scenarios")
FAMILY_DIR = os.path.join(REPO, "src", "scenarios", "families")
HOLDOUT_DIR = os.path.join(REPO, "eval", "holdout")
GENERATOR_SOURCES = [os.path.join(REPO, "src", "scenarios"), os.path.join(REPO, "src", "policy")]
HOLDOUT_PATTERNS = re.compile(r"holdout|independent_(es|pt)|team_handwritten|eval[/\\]", re.IGNORECASE)
ATTACKS = {"prompt_injection", "other_customer_data", "social_engineering"}
MIN_E2E_PER_CATEGORY = 10
CHAR_JACCARD_MAX = 0.75        # holdout vs generated: char 3-gram Jaccard at or above this is a near-duplicate
CONTAINMENT_MIN_WORDS = 5      # a holdout text this long, found word for word inside a short generated text,
CONTAINMENT_MAX_RATIO = 2.0    # (at most this many times its length) counts as contained
PERMUTATIONS = 200


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# --------------------------------------------------------------------------------------------------------------
# Text helpers


def masked_key(text):
    """normalize_key with digit runs and template placeholders folded, so amounts and slots do not hide a copy."""
    text = re.sub(r"\{[a-z_]+\}", " 0 ", text)
    return re.sub(r"\d+", "0", R.normalize_key(text))


def char_grams(key, n=3):
    padded = f" {key} "
    return {padded[i:i + n] for i in range(max(1, len(padded) - n + 1))}


def word_grams(key, n=3):
    words = key.split()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


AMOUNT_RE = re.compile(r"(?<![\w/])\d[\d.,']*\d(?![\w/])")


def amount_keys(text):
    """Amount-like numbers in a text as digit strings ('1.234,56' and '1,234.56' both give '123456').
    Four or more digits only, and not a bare year, so days, instalments and small counts do not count."""
    keys = set()
    for m in AMOUNT_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) < 4 or (len(digits) == 4 and m.group(0).isdigit() and 1990 <= int(digits) <= 2030):
            continue
        keys.add(digits)
    return keys


# --------------------------------------------------------------------------------------------------------------
# Checks: each returns (ok, detail, metrics)


def check_families(intent):
    splits = collections.defaultdict(set)
    for r in intent:
        splits[r["family_id"]].add(r["split"])
    bad = sorted(f for f, s in splits.items() if len(s) > 1)
    per_split = collections.Counter(next(iter(s)) for f, s in splits.items() if len(s) == 1)
    return not bad, f"{len(splits)} families, {len(bad)} in more than one split", {
        "families": len(splits), "violations": bad, "families_by_split": dict(sorted(per_split.items()))}


def check_customers(intent, e2e, anchors, panel, seed):
    splits = collections.defaultdict(set)
    for r in intent:
        if r["anchor"]:
            splits[r["anchor"]["customer_id"]].add(("intent", r["split"]))
    for r in e2e:
        splits[r["customer_id"]].add(("e2e", r["split"]))
    for a in anchors:
        splits[a["customer_id"]].add(("anchors", a["split"]))
    for c in panel:
        splits[c["customer_id"]].add(("panel", c["split"]))
    bad = sorted(c for c, s in splits.items() if len({x[1] for x in s}) > 1)
    wrong_bucket = sorted(c for c, s in splits.items() if {x[1] for x in s} != {customer_split(seed, c)})
    by_split = collections.Counter(customer_split(seed, c) for c in splits)
    intent_by_split = {s: len({r["anchor"]["customer_id"] for r in intent if r["anchor"] and r["split"] == s})
                       for s in ("train", "dev", "test")}
    ok = not bad and not wrong_bucket
    return ok, f"{len(splits)} customers, {len(bad)} in more than one split, {len(wrong_bucket)} off their hash bucket", {
        "customers": len(splits), "violations": bad[:20], "wrong_bucket": wrong_bucket[:20],
        "customers_by_split": dict(sorted(by_split.items())), "intent_customers_by_split": intent_by_split}


def check_transactions(intent, e2e):
    splits = collections.defaultdict(set)
    for r in intent:
        if r["anchor"] and r["anchor"]["transaction_id"]:
            splits[r["anchor"]["transaction_id"]].add(r["split"])
    for r in e2e:
        e = r["expected"]
        for t in [r["anchor"]["transaction_id"], e["transaction_id"]] + list(e["candidate_transaction_ids"] or []):
            if t:
                splits[t].add(r["split"])
    bad = sorted(t for t, s in splits.items() if len(s) > 1)
    return not bad, f"{len(splits)} transactions, {len(bad)} in more than one split", {
        "transactions": len(splits), "violations": bad[:20]}


def check_temporal(intent, e2e, anchors):
    dates = collections.defaultdict(list)
    bad = []
    rows = ([(r["id"], r["split"], r["anchor"]["event_date"]) for r in intent if r["anchor"]]
            + [(r["scenario_id"], r["split"], r["anchor"]["event_date"]) for r in e2e]
            + [(a["transaction_id"], a["split"], a["event_date"]) for a in anchors])
    for rid, split, ev in rows:
        d = date.fromisoformat(ev[:10])
        dates[split].append(d)
        if (d >= SPLIT_DATE) != (split == "test"):
            bad.append(rid)
    span = {s: [str(min(v)), str(max(v))] for s, v in sorted(dates.items())}
    return not bad, f"cut-off {SPLIT_DATE}; anchor date span {span}; {len(bad)} violations", {
        "cutoff": str(SPLIT_DATE), "span_by_split": span, "violations": bad[:20]}


def check_id_tokens(intent, e2e, seed):
    txn_prd = re.compile(r"\b(TRX|PRD)-[A-Z0-9]{6,}", re.IGNORECASE)   # noise may lower-case a whole text
    cli = re.compile(r"\bCLI-[A-Z0-9]{6,}", re.IGNORECASE)
    contact = re.compile(r"[\w.+-]+@[\w-]+\.\w+|https?://")
    bad = []
    for r in intent:
        if txn_prd.search(r["text"]) or cli.search(r["text"]) or contact.search(r["text"]):
            bad.append(r["id"])
    cli_turns = 0
    for r in e2e:
        for t in r["turns"]:
            if txn_prd.search(t["text"]) or contact.search(t["text"]):
                bad.append(r["scenario_id"])
            for m in cli.finditer(t["text"]):
                cli_turns += 1
                if r["category"] != "unauthorized_access" or customer_split(seed, m.group(0).upper()) != r["split"]:
                    bad.append(r["scenario_id"])
    return not bad, f"{len(bad)} texts with id or contact tokens; {cli_turns} customer numbers in unauthorized-access turns", {
        "violations": sorted(set(bad))[:20], "customer_numbers_in_e2e_turns": cli_turns}


def check_amounts(intent, e2e, seed):
    """Distinct (amount string, source) units: the anchor transaction, or the family for synthetic amounts.
    Observed = amount strings found in both train and test units; null = the same after shuffling units between
    the two sides (sizes kept). Passes when the observed overlap is not above the null's 95th percentile."""
    out, ok = {}, True
    for lang in ("es", "pt"):
        units = {"train": set(), "test": set()}
        for r in intent:
            if r["language"] != lang or r["split"] not in units:
                continue
            src = (r["anchor"] or {}).get("transaction_id") or r["family_id"]
            for k in amount_keys(r["text"]):
                units[r["split"]].add((k, src))
        for r in e2e:
            if r["language"] == lang and r["split"] == "test":
                for t in r["turns"]:
                    for k in amount_keys(t["text"]):
                        units["test"].add((k, r["anchor"]["transaction_id"]))

        def overlap(a, b):
            return len({k for k, _ in a} & {k for k, _ in b})

        observed = overlap(units["train"], units["test"])
        pool = sorted(units["train"] | units["test"])
        n_train = len(units["train"])
        rng = random.Random(f"{seed}:amounts:{lang}")
        null = []
        for _ in range(PERMUTATIONS):
            rng.shuffle(pool)
            null.append(overlap(pool[:n_train], pool[n_train:]))
        null.sort()
        p95 = null[int(0.95 * (len(null) - 1))]
        shared = sorted({k for k, _ in units["train"]} & {k for k, _ in units["test"]})
        long_shared = [k for k in shared if len(k) >= 6]
        lang_ok = observed <= p95
        ok &= lang_ok
        out[lang] = {"train_units": n_train, "test_units": len(units["test"]), "observed_shared": observed,
                     "null_mean": round(sum(null) / len(null), 1), "null_p95": p95,
                     "p_value": round(sum(1 for x in null if x >= observed) / len(null), 3),
                     "shared_with_6plus_digits": len(long_shared), "ok": lang_ok}
    detail = "; ".join(f"{l}: {v['observed_shared']} shared vs null mean {v['null_mean']} (p95 {v['null_p95']})"
                       for l, v in out.items())
    return ok, detail, out


def check_holdout_dups(intent, e2e, holdout, families):
    generated = {}
    for r in intent:
        generated.setdefault(masked_key(r["text"]), ("intent", r["id"], r["text"]))
    for r in e2e:
        for t in r["turns"]:
            generated.setdefault(masked_key(t["text"]), ("e2e", f"{r['scenario_id']}#{t['turn']}", t["text"]))
    for f in families:
        for i, t in enumerate(f["templates"]):
            generated.setdefault(masked_key(t), ("template", f"{f['family_id']}[{i}]", t))
    gen = [(k, v, char_grams(k), word_grams(k), len(k.split())) for k, v in generated.items()]
    hits, closest = [], []
    for h in holdout:
        hk = masked_key(h["text"])
        hc, hw, hn = char_grams(hk), word_grams(hk), len(hk.split())
        best_jac, best_cont = (0.0, None), (0.0, None)
        for k, v, gc, gw, gn in gen:
            inter = len(hc & gc)
            jac = inter / (len(hc) + len(gc) - inter)
            if jac > best_jac[0]:
                best_jac = (jac, v)
            if hn >= CONTAINMENT_MIN_WORDS and gn <= CONTAINMENT_MAX_RATIO * hn and hw <= gw:
                best_cont = (1.0, v)
        why = ("exact" if best_jac[0] == 1.0 else "near" if best_jac[0] >= CHAR_JACCARD_MAX
               else "contained" if best_cont[1] else None)
        match = best_cont[1] if why == "contained" else best_jac[1]
        entry = {"holdout_id": h["id"], "holdout_text": h["text"], "char3_jaccard": round(best_jac[0], 3),
                 "kind": why, "source": match[0], "generated_id": match[1], "generated_text": match[2]}
        closest.append(entry)
        if why:
            hits.append(entry)
    closest.sort(key=lambda e: -e["char3_jaccard"])
    kinds = collections.Counter(e["kind"] for e in hits)
    return not hits, (f"{len(holdout)} holdout texts vs {len(generated)} distinct generated texts and templates; "
                      f"{len(hits)} flagged {dict(kinds) or ''}; max char 3-gram Jaccard "
                      f"{closest[0]['char3_jaccard'] if closest else 0}"), {
        "holdout_rows": len(holdout), "generated_keys": len(generated), "violations": hits,
        "closest_pairs": closest[:15],
        "rules": {"exact": "equal after case, accent, punctuation and digit folding",
                  "near": f"char 3-gram Jaccard >= {CHAR_JACCARD_MAX}",
                  "contained": f"every word 3-gram of a holdout text of {CONTAINMENT_MIN_WORDS}+ words inside one "
                               f"generated text at most {CONTAINMENT_MAX_RATIO}x as long"}}


def check_holdout_refs():
    this = os.path.abspath(__file__)
    bad = []
    scanned = 0
    for root in GENERATOR_SOURCES:
        for path in sorted(glob.glob(os.path.join(root, "**", "*"), recursive=True)):
            if os.path.isdir(path) or os.path.abspath(path) == this or "__pycache__" in path:
                continue
            if not path.endswith((".py", ".json", ".sql", ".md")) or path.endswith("README.md"):
                continue
            scanned += 1
            with open(path, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if HOLDOUT_PATTERNS.search(line):
                        bad.append(f"{os.path.relpath(path, REPO)}:{n}")
    return not bad, f"{scanned} generator files scanned, {len(bad)} holdout references", {
        "files_scanned": scanned, "violations": bad}


def check_text_dups(intent, e2e):
    keys = collections.Counter(R.normalize_key(r["text"]) for r in intent)
    dups = [k for k, c in keys.items() if c > 1]
    e2e_in_intent = sorted({f"{r['scenario_id']}#{t['turn']}" for r in e2e for t in r["turns"]
                            if len(t["text"]) > 25 and R.normalize_key(t["text"]) in keys})
    return not dups and not e2e_in_intent, (f"{len(dups)} duplicate intent messages; "
                                            f"{len(e2e_in_intent)} e2e turns (over 25 chars) equal to an intent message"), {
        "duplicates": dups[:20], "e2e_turns_in_intent": e2e_in_intent[:20]}


def check_labels(intent):
    bad = []
    attacks_test = collections.defaultdict(set)
    for r in intent:
        acc = r["acceptable_intents"]
        if r["intent"] not in acc or r["is_ambiguous"] != (len(acc) > 1) or len(set(acc)) != len(acc):
            bad.append(r["id"])
        if r["attack_type"] is not None and r["attack_type"] not in ATTACKS:
            bad.append(r["id"])
        if r["attack_type"] and r["split"] == "test":
            attacks_test[r["language"]].add(r["attack_type"])
    missing = {l: sorted(ATTACKS - attacks_test[l]) for l in ("es", "pt") if ATTACKS - attacks_test[l]}
    return not bad and not missing, f"{len(bad)} label inconsistencies; attack types missing from test: {missing or 'none'}", {
        "violations": bad[:20], "attack_types_in_test": {l: sorted(v) for l, v in attacks_test.items()}}


def check_transfer_view(intent, transfer):
    want = {r["id"] for r in intent if (r["language"], r["split"]) in {("es", "train"), ("es", "dev"), ("pt", "test")}}
    got = {r["id"] for r in transfer}
    comp = collections.Counter(f"{r['language']}|{r['split']}" for r in transfer)
    return want == got, f"{len(got)} rows {dict(sorted(comp.items()))}", {
        "rows": len(got), "missing": len(want - got), "unexpected": len(got - want)}


def check_e2e_categories(e2e):
    counts = collections.Counter((r["category"], r["language"]) for r in e2e)
    cats = sorted({c for c, _ in counts})
    low = {f"{c}|{l}": counts[(c, l)] for c in cats for l in ("es", "pt") if counts[(c, l)] < MIN_E2E_PER_CATEGORY}
    return not low, f"{len(cats)} categories, minimum per language {min(counts.values())}; below {MIN_E2E_PER_CATEGORY}: {low or 'none'}", {
        "by_category_language": {f"{c}|{l}": counts[(c, l)] for c in cats for l in ("es", "pt")}, "below_minimum": low}


# --------------------------------------------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=20261005)
    ap.add_argument("--data-dir", default=DATA_DIR)
    ap.add_argument("--json", help="write the full results to this file")
    args = ap.parse_args(argv)

    intent = read_jsonl(os.path.join(args.data_dir, "intent_dataset.jsonl"))
    transfer = read_jsonl(os.path.join(args.data_dir, "intent_lang_transfer_es_to_pt.jsonl"))
    e2e = read_jsonl(os.path.join(args.data_dir, "e2e_scenarios.jsonl"))
    anchors = read_jsonl(os.path.join(args.data_dir, "anchors.jsonl"))
    panel = read_jsonl(os.path.join(args.data_dir, "panel.jsonl"))
    holdout = [r for p in sorted(glob.glob(os.path.join(HOLDOUT_DIR, "*.jsonl"))) for r in read_jsonl(p)]
    families = [f for lang in ("es", "pt")
                for f in json.load(open(os.path.join(FAMILY_DIR, f"{lang}.json"), encoding="utf-8"))["families"]]

    checks = [
        ("families", lambda: check_families(intent)),
        ("customers", lambda: check_customers(intent, e2e, anchors, panel, args.seed)),
        ("transactions", lambda: check_transactions(intent, e2e)),
        ("temporal", lambda: check_temporal(intent, e2e, anchors)),
        ("id_tokens", lambda: check_id_tokens(intent, e2e, args.seed)),
        ("amounts", lambda: check_amounts(intent, e2e, args.seed)),
        ("holdout_dups", lambda: check_holdout_dups(intent, e2e, holdout, families)),
        ("holdout_refs", check_holdout_refs),
        ("text_dups", lambda: check_text_dups(intent, e2e)),
        ("labels", lambda: check_labels(intent)),
        ("transfer_view", lambda: check_transfer_view(intent, transfer)),
        ("e2e_categories", lambda: check_e2e_categories(e2e)),
    ]
    results, failed = {}, []
    for name, fn in checks:
        ok, detail, metrics = fn()
        results[name] = {"ok": ok, "detail": detail, **metrics}
        print(f"{'PASS' if ok else 'FAIL'}  {name:<15} {detail}")
        if not ok:
            failed.append(name)
    for v in results["holdout_dups"]["violations"]:
        print(f"      {v['kind']:<9} {v['holdout_id']} ~ {v['source']} {v['generated_id']} "
              f"(char 3-gram Jaccard {v['char3_jaccard']})")
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"seed": args.seed, "failed": failed, "checks": results}, fh, indent=2, ensure_ascii=False)
    print(f"{len(checks) - len(failed)} of {len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
