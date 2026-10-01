"""Scenario generator: the ES/PT intent dataset and the end-to-end agent scenarios, from template families and
Silver anchors. Deterministic for a given seed and inputs (stdlib only; no clock, no network).

    python -m src.scenarios.generate --seed 20261005

Inputs:  src/scenarios/families/{es,pt}.json, src/scenarios/e2e_phrases.json, src/policy/dispute_policy.json,
         data/scenarios/{anchors.jsonl, panel.jsonl, silver_snapshot.json} (from extract_anchors.py)
Outputs: data/scenarios/intent_dataset.jsonl, intent_lang_transfer_es_to_pt.jsonl, e2e_scenarios.jsonl, manifest.json

Leak-free splits (report 02, section 10), asserted before anything is written:
  * whole families per split, about 70/15/15 per language and bucket (the six intents, ambiguous, adversarial);
  * customers disjoint across splits (sha256 hash buckets, the same function as anchors.sql);
  * temporal: train/dev anchors have event_date < 2025-07-01, test anchors >= 2025-07-01;
  * no duplicate message (after case, accent and punctuation folding) anywhere in the dataset.
"""
import argparse
import collections
import hashlib
import json
import os
import random
import re
from datetime import date, datetime, timedelta

from src.policy import dispute_policy as dp
from src.scenarios import e2e
from src.scenarios import render as R
from src.scenarios.anchor_specs import spec_for
from src.scenarios.splits import SPLIT_DATE, customer_bucket, split_of_bucket

GENERATOR_VERSION = "1.0.0"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FAMILY_DIR = os.path.join(REPO, "src", "scenarios", "families")
DATA_DIR = os.path.join(REPO, "data", "scenarios")
SPLITS = ("train", "dev", "test")
SPLIT_SHARES = (0.70, 0.15, 0.15)
TARGET_ROWS = {"train": 3000, "dev": 600, "test": 600}
PERIODS = {"train": (date(2023, 7, 1), date(2025, 6, 30)), "dev": (date(2023, 7, 1), date(2025, 6, 30)),
           "test": (date(2025, 7, 1), date(2026, 6, 30))}
RESTRICTED_SHARE = 0.07          # share of anchored rows drawn from Closed/Suspended customers
SYNTH_USD = {"refund": [150, 200, 250, 300, 500, 800, 1000, 1500, 2000, 3000],
             "financing": [20000, 35000, 50000, 80000, 120000, 200000, 300000]}
FX_FIXED = {"USD": 1, "COP": 4000, "ARS": 350, "BRL": 5}   # source's fixed rates; BRL only for synthetic PT amounts
PII_PATTERNS = [re.compile(p) for p in (r"[\w.+-]+@[\w-]+\.\w+", r"https?://", r"\bCLI-[A-Z0-9]{6,}", r"\bPRD-[A-Z0-9]{6,}",
                                        r"\bTRX-[A-Z0-9]{6,}")]


def rng_for(*parts):
    return random.Random(":".join(str(p) for p in parts))


def hkey(*parts):
    return hashlib.sha256(":".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def allocate(n, shares):
    """Largest-remainder allocation of n items to shares (ties go to the earlier share)."""
    raw = [n * s for s in shares]
    out = [int(x) for x in raw]
    for i in sorted(range(len(shares)), key=lambda i: (-(raw[i] - out[i]), i))[: n - sum(out)]:
        out[i] += 1
    return out


def family_bucket(f):
    return "adversarial" if f["attack_type"] else "ambiguous" if f["is_ambiguous"] else f["intent"]


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------------------------------------------
# Family splits


def assign_family_splits(families, seed):
    """{family_id: split}: whole families, ~70/15/15 within each bucket, order fixed by a seeded hash.

    Exception: the adversarial bucket (8 families) is split by attack type so that test holds one family of every
    attack type with 2+ families, dev one more, and train keeps at least one of each (4/1/3 with the current files).
    Portunhol (mixed_language) families are then swapped so dev and test each get at least one."""
    split = {}
    buckets = collections.defaultdict(list)
    for f in families:
        buckets[family_bucket(f)].append(f)
    for b in sorted(buckets):
        fams = sorted(buckets[b], key=lambda f: hkey(seed, "family-split", f["family_id"]))
        if b == "adversarial":
            by_type = collections.defaultdict(list)
            for f in fams:
                by_type[f["attack_type"]].append(f["family_id"])
            for typ in sorted(by_type):
                if len(by_type[typ]) >= 2:
                    split[by_type[typ].pop(0)] = "test"
            typ = max(sorted(by_type), key=lambda t: len(by_type[t]))
            if len(by_type[typ]) >= 2:
                split[by_type[typ].pop(0)] = "dev"
            for ids in by_type.values():
                for fid in ids:
                    split[fid] = "train"
            continue
        n_train, n_test, n_dev = allocate(len(fams), (SPLIT_SHARES[0], SPLIT_SHARES[2], SPLIT_SHARES[1]))  # ties to test
        for i, f in enumerate(fams):
            split[f["family_id"]] = "test" if i < n_test else "dev" if i < n_test + n_dev else "train"
    mixed = {f["family_id"] for f in families if f.get("mixed_language")}
    if len(mixed) >= 3:
        for target in ("test", "dev"):
            if any(split[m] == target for m in mixed):
                continue
            for b in sorted(buckets):
                ids = [f["family_id"] for f in sorted(buckets[b], key=lambda f: hkey(seed, "family-split", f["family_id"]))]
                src = next((i for i in ids if i in mixed and split[i] == "train"), None)
                dst = next((i for i in ids if i not in mixed and split[i] == target), None)
                if src and dst:
                    split[src], split[dst] = target, "train"
                    break
    return split


# --------------------------------------------------------------------------------------------------------------
# Anchor pools


class AnchorPool:
    """Anchors by split; filtered lists are cached per (spec, split, country, status group) and served round-robin
    in a seeded order, so rows spread over many customers and transactions."""

    def __init__(self, anchors, seed):
        self.seed = seed
        self.by_split = collections.defaultdict(list)
        for a in anchors:
            self.by_split[a["split"]].append(a)
        for rows in self.by_split.values():
            rows.sort(key=lambda a: a["transaction_id"])
        self.cache, self.cursor = {}, collections.Counter()

    def _filtered(self, spec, split, country, group, relax_intl=False):
        key = (spec, split, country, group, relax_intl)
        if key not in self.cache:
            rows = [a for a in self.by_split[split]
                    if a["anchor_kind"] in spec.kinds and a["status_group"] == group
                    and (country is None or a["customer_country_code"] == country)
                    and (spec.products is None or a["product_type_en"] in spec.products)
                    and (spec.channels is None or a["channel"] in spec.channels)
                    and (spec.merchants is None or a["merchant_name"] in spec.merchants)
                    and (spec.types is None or a["transaction_type"] in spec.types)
                    and (spec.max_usd is None or (a["amount_usd"] or 0) <= spec.max_usd)
                    and (spec.intl is None or relax_intl or bool(a["is_international"]) == spec.intl)]
            rng_for(self.seed, "pool", split, country, group, spec.kinds, spec.products and sorted(spec.products),
                    spec.channels and sorted(spec.channels), relax_intl).shuffle(rows)
            self.cache[key] = rows
        return self.cache[key]

    def pick(self, spec, split, country, rng):
        groups = ["restricted", "active"] if rng.random() < RESTRICTED_SHARE else ["active"]
        for relax in (False, True):
            for group in groups:
                rows = self._filtered(spec, split, country, group, relax)
                if rows:
                    key = (spec, split, country, group, relax)
                    a = rows[self.cursor[key] % len(rows)]
                    self.cursor[key] += 1
                    return a
        raise LookupError(f"no anchor for {spec} split={split} country={country}")


# --------------------------------------------------------------------------------------------------------------
# Intent dataset


def synth_amount(kind, variant, rng):
    usd = rng.choice(SYNTH_USD[kind])
    if variant == "es-MX":
        cur = "USD"
    elif variant in ("es-CO", "es-AR"):
        cur = R.LOCAL_PESO[R.COUNTRY_OF_VARIANT[variant]] if rng.random() < 0.8 else "USD"
    else:
        cur = rng.choices(["BRL", "USD", "COP", "ARS"], weights=[4, 3, 1.5, 1.5])[0]
    value = usd * FX_FIXED[cur]
    return float(R.round_sig(value, 2)), cur


def random_now(split, rng):
    lo, hi = PERIODS[split]
    d = lo + timedelta(days=rng.randrange((hi - lo).days + 1))
    return datetime(d.year, d.month, d.day, rng.randrange(7, 23), rng.randrange(60), rng.randrange(60))


def render_row(fam, template, lang, split, pool, rng):
    spec = spec_for(fam)
    variant = rng.choice(fam["variants"]) if lang == "es" else ("mixed" if fam.get("mixed_language") else "pt-BR")
    country = R.COUNTRY_OF_VARIANT.get(variant)
    anchor = pool.pick(spec, split, country, rng) if spec.anchored else None
    if anchor and spec.same_day:
        now = datetime.fromisoformat(anchor["event_ts"]) + timedelta(minutes=rng.randint(2, 90))
    elif anchor:
        ev = date.fromisoformat(anchor["event_date"])
        d = ev + timedelta(days=rng.randint(1, 20))
        now = datetime(d.year, d.month, d.day, rng.randrange(7, 23), rng.randrange(60), rng.randrange(60))
    else:
        now = random_now(split, rng)
    values, noise = {}, []
    slots = {"amount": None, "currency": None, "date_ref": None, "resolved_date": None, "merchant_hint": None,
             "channel_hint": None, "product_hint": None, "txn_type_hint": None}
    for slot in fam["requires"]:
        if slot == "amount":
            if anchor:
                value, cur = anchor["amount"], anchor["currency"]
            else:
                value, cur = synth_amount(spec.synth or "refund", variant, rng)
            text, stated, explicit, tags = R.render_amount(value, cur, variant, rng)
            values["amount"], slots["amount"], slots["currency"] = text, stated, explicit
            noise += tags
        elif slot == "date":
            if anchor and not spec.free_date:
                d = date.fromisoformat(anchor["event_date"])
            else:
                d = now.date() - timedelta(days=rng.randint(1, 20))
            text, resolved, tag = R.render_date(d, now.date(), lang, variant, rng)
            values["date"], slots["resolved_date"] = text, resolved
            noise.append(tag)
        elif slot == "merchant":
            text, tags = R.render_merchant(anchor["merchant_name"], rng)
            values["merchant"] = text
            noise += tags
        elif slot == "product":
            values["product"] = R.render_product(anchor["product_type_en"], lang, variant, rng)
        elif slot == "channel":
            values["channel"] = R.render_channel(anchor["channel"], lang, variant, rng)
        elif slot == "txn_type":
            values["txn_type"] = R.render_txn_type(anchor["transaction_type"], lang, variant)
    parts, _ = R.fill(template, values)
    text, surfaces, tags = R.apply_noise(parts, lang, fam["register"], rng)
    noise += tags
    slots.update({"date_ref": surfaces.get("date"), "merchant_hint": surfaces.get("merchant"),
                  "channel_hint": surfaces.get("channel"), "product_hint": surfaces.get("product"),
                  "txn_type_hint": surfaces.get("txn_type")})
    row = {
        "text": text, "language": lang, "variant": variant, "intent": fam["intent"],
        "acceptable_intents": list(fam["acceptable_intents"]), "is_ambiguous": fam["is_ambiguous"],
        "attack_type": fam["attack_type"], "slots": slots, "family_id": fam["family_id"],
        "anchor": None if not anchor else {
            "customer_id": anchor["customer_id"], "product_id": anchor["product_id"],
            "transaction_id": anchor["transaction_id"] if spec.refs else None, "event_date": anchor["event_date"]},
        "now": now.isoformat(), "noise": sorted(set(noise)), "split": split,
    }
    return row


def generate_language(lang, families, fam_split, pool, seed, seen):
    """Rows for one language: per split, rows go to buckets by their share of families, then evenly to families."""
    share = collections.Counter(family_bucket(f) for f in families)
    buckets = sorted(share)
    rows = []
    for split in SPLITS:
        per_bucket = dict(zip(buckets, allocate(TARGET_ROWS[split], [share[b] / len(families) for b in buckets])))
        for b in buckets:
            fams = sorted([f for f in families if family_bucket(f) == b and fam_split[f["family_id"]] == split],
                          key=lambda f: hkey(seed, "family-rows", f["family_id"]))
            if not fams:
                continue
            for fam, k in zip(fams, allocate(per_bucket[b], [1 / len(fams)] * len(fams))):
                rows += generate_family(fam, lang, split, k, pool, seed, seen)
    return rows


def generate_family(fam, lang, split, k, pool, seed, seen):
    order = list(range(len(fam["templates"])))
    rng_for(seed, "templates", fam["family_id"]).shuffle(order)
    out, attempt, fails = [], 0, 0
    while len(out) < k and attempt < k * 60:
        t_idx = order[(len(out) + fails // 8) % len(order)]
        rng = rng_for(seed, lang, fam["family_id"], split, attempt)
        attempt += 1
        row = render_row(fam, fam["templates"][t_idx], lang, split, pool, rng)
        key = R.normalize_key(row["text"])
        if key in seen:
            fails += 1
            continue
        seen.add(key)
        row["template_index"] = t_idx
        out.append(row)
    if len(out) < k:
        raise RuntimeError(f"{fam['family_id']}: only {len(out)} of {k} unique messages")
    return out


def finalize_intent_rows(rows, lang, seed):
    ordered = []
    for split in SPLITS:
        part = [r for r in rows if r["split"] == split]
        part.sort(key=lambda r: hkey(seed, "order", r["family_id"], r["text"]))
        ordered += part
    out = []
    for i, r in enumerate(ordered, 1):
        out.append({"id": f"gen-{lang}-{i:05d}", **r, "source": "template_generated",
                    "generator_version": GENERATOR_VERSION, "seed": seed})
    return out


def check_intent_rows(rows, fam_split, seed, same_day=frozenset()):
    """Leak and hygiene assertions on the intent dataset. `same_day`: families whose clock is minutes after the event."""
    fam_splits = collections.defaultdict(set)
    cust_splits = collections.defaultdict(set)
    keys = collections.Counter()
    for r in rows:
        fam_splits[r["family_id"]].add(r["split"])
        keys[R.normalize_key(r["text"])] += 1
        assert r["intent"] in r["acceptable_intents"], r["id"]
        assert (len(r["acceptable_intents"]) > 1) == r["is_ambiguous"], r["id"]
        assert not any(p.search(r["text"]) for p in PII_PATTERNS), f"id-like or contact token in {r['id']}"
        a = r["anchor"]
        if a:
            cust_splits[a["customer_id"]].add(r["split"])
            assert split_of_bucket(customer_bucket(seed, a["customer_id"])) == r["split"], f"bucket {r['id']}"
            ev = date.fromisoformat(a["event_date"])
            assert (ev >= SPLIT_DATE) == (r["split"] == "test"), f"temporal {r['id']}"
            now = datetime.fromisoformat(r["now"]).date()
            assert (0 if r["family_id"] in same_day else 1) <= (now - ev).days <= 20, f"now {r['id']}"
    assert all(len(s) == 1 for s in fam_splits.values()), "family in more than one split"
    assert all(fam_split[f] in s for f, s in fam_splits.items())
    assert all(len(s) == 1 for s in cust_splits.values()), "customer in more than one split"
    dup = [k for k, c in keys.items() if c > 1]
    assert not dup, f"duplicate messages: {dup[:3]}"


# --------------------------------------------------------------------------------------------------------------
# Manifest


def counts(rows, *keys):
    c = collections.Counter(tuple(r[k] if not callable(k) else k(r) for k in keys) for r in rows)
    return {"|".join(map(str, k)): v for k, v in sorted(c.items())}


def build_manifest(seed, out_dir, files, intent_rows, transfer_rows, e2e_rows, fam_split, families, inputs):
    snap_path = os.path.join(DATA_DIR, "silver_snapshot.json")
    snapshot = json.load(open(snap_path, encoding="utf-8")) if os.path.exists(snap_path) else None
    fam_counts = collections.Counter((fid.split("-")[0], family_bucket(f), fam_split[fid])
                                     for fid, f in families.items())
    return {
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "split_rules": {"families": "whole families per split, ~70/15/15 per language and bucket",
                        "customers": "sha256('<seed>:<customer_id>')[:8] mod 100: 0-69 train, 70-84 dev, 85-99 test",
                        "temporal": f"train/dev anchors event_date < {SPLIT_DATE}, test >= {SPLIT_DATE}",
                        "language_transfer_view": "ES train + ES dev, PT test"},
        "silver_snapshot": snapshot and {"extracted_at": snapshot["extracted_at"], "tables": snapshot["tables"],
                                         "threshold_calibration": snapshot["calibration"]},
        "inputs_sha256": inputs,
        "outputs": {os.path.basename(p): {"rows": n, "sha256": sha256_file(p)} for p, n in files},
        "family_splits": {"|".join(k): v for k, v in sorted(fam_counts.items())},
        "intent_dataset": {
            "by_language_split": counts(intent_rows, "language", "split"),
            "by_language_split_intent": counts(intent_rows, "language", "split", "intent"),
            "ambiguous_by_language_split": counts([r for r in intent_rows if r["is_ambiguous"]], "language", "split"),
            "attack_by_language_split": counts([r for r in intent_rows if r["attack_type"]], "language", "split", "attack_type"),
            "by_variant": counts(intent_rows, "language", "variant"),
            "anchored_by_language_split": counts([r for r in intent_rows if r["anchor"]], "language", "split"),
            "with_transaction_by_language_split": counts([r for r in intent_rows if r["anchor"] and r["anchor"]["transaction_id"]],
                                                         "language", "split"),
            "distinct_customers_by_split": {s: len({r["anchor"]["customer_id"] for r in intent_rows
                                                    if r["anchor"] and r["split"] == s}) for s in SPLITS},
            "families_by_language_split": {
                f"{lang}|{s}": len({r["family_id"] for r in intent_rows if r["language"] == lang and r["split"] == s})
                for lang in ("es", "pt") for s in SPLITS},
        },
        "lang_transfer_view": counts(transfer_rows, "language", "split"),
        "e2e_scenarios": {
            "by_language_split": counts(e2e_rows, "language", "split"),
            "by_category_language": counts(e2e_rows, "category", "language"),
            "by_outcome": counts(e2e_rows, lambda r: r["expected"]["outcome"]),
            "by_handoff_reason": counts([r for r in e2e_rows if r["expected"]["handoff_reason"]],
                                        lambda r: r["expected"]["handoff_reason"]),
            "by_variant": counts(e2e_rows, "language", "variant"),
        },
    }


# --------------------------------------------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=20261005)
    ap.add_argument("--data-dir", default=DATA_DIR, help="where anchors.jsonl and panel.jsonl are")
    ap.add_argument("--out-dir", default=DATA_DIR)
    args = ap.parse_args(argv)
    os.makedirs(args.out_dir, exist_ok=True)
    seed = args.seed
    pol = dp.load_policy()

    anchors = read_jsonl(os.path.join(args.data_dir, "anchors.jsonl"))
    panel = read_jsonl(os.path.join(args.data_dir, "panel.jsonl"))
    for a in anchors:
        assert split_of_bucket(customer_bucket(seed, a["customer_id"])) == a["split"], "anchors.sql bucket mismatch"
    pool = AnchorPool(anchors, seed)

    families, fam_split, intent_rows, seen = {}, {}, [], set()
    for lang in ("es", "pt"):
        doc = json.load(open(os.path.join(FAMILY_DIR, f"{lang}.json"), encoding="utf-8"))
        fams = doc["families"]
        for f in fams:
            spec_for(f)                                    # fail fast on a family without an anchor spec
            families[f["family_id"]] = f
        split = assign_family_splits(fams, seed)
        fam_split.update(split)
        intent_rows += finalize_intent_rows(generate_language(lang, fams, split, pool, seed, seen), lang, seed)
    check_intent_rows(intent_rows, fam_split, seed, {fid for fid, f in families.items() if spec_for(f).same_day})
    transfer_rows = [r for r in intent_rows if (r["language"] == "es" and r["split"] in ("train", "dev"))
                     or (r["language"] == "pt" and r["split"] == "test")]

    e2e_rows = e2e.build_scenarios(panel, pol, seed)
    e2e.check_scenarios(e2e_rows, seed)

    files = []
    for name, rows in (("intent_dataset.jsonl", intent_rows), ("intent_lang_transfer_es_to_pt.jsonl", transfer_rows),
                       ("e2e_scenarios.jsonl", e2e_rows)):
        path = os.path.join(args.out_dir, name)
        write_jsonl(path, rows)
        files.append((path, len(rows)))
    inputs = {os.path.relpath(p, REPO).replace(os.sep, "/"): sha256_file(p) for p in [
        os.path.join(FAMILY_DIR, "es.json"), os.path.join(FAMILY_DIR, "pt.json"), e2e.PHRASES_PATH, dp.POLICY_PATH,
        os.path.join(args.data_dir, "anchors.jsonl"), os.path.join(args.data_dir, "panel.jsonl")]}
    manifest = build_manifest(seed, args.out_dir, files, intent_rows, transfer_rows, e2e_rows, fam_split, families, inputs)
    with open(os.path.join(args.out_dir, "manifest.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False, sort_keys=True)

    for name, n in ((os.path.basename(p), n) for p, n in files):
        print(f"{name}: {n:,} rows")
    for k, v in manifest["intent_dataset"]["by_language_split"].items():
        print(f"  intent {k}: {v}")
    for k, v in manifest["e2e_scenarios"]["by_language_split"].items():
        print(f"  e2e {k}: {v}")


if __name__ == "__main__":
    main()
