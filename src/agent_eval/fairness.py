"""Outcomes of a saved agent exam by customer country, customer segment and language variant.

    python -m src.agent_eval.fairness                                  # the final test run
    python -m src.agent_eval.fairness --split test --endpoint databricks-gpt-oss-120b --boot 2000 --seed 20261005

The problem statement asks for outcomes by language and by authorized customer segments, with their small-sample
limits, and for an investigation of any disparity. This module reads the per-scenario verdicts of
`eval/results/agent_e2e_<split>_<endpoint>.json` (written by `src.agent_eval.report`), each scenario's customer, text
and language variant from `data/scenarios/e2e_scenarios.jsonl` (its sha256 must be the one the results recorded),
and each customer's country and segment from the table customer_profile of `data/bank_tools/snapshot_panel.sqlite`
(the exam's snapshot; the country is cross-checked against `data/scenarios/panel.jsonl`). It never reads
transcripts, the holdout or the model. It writes `eval/results/fairness_<split>_<endpoint>.json` and `.md`, with
aggregates, scenario ids, countries and segments: no customer id or name, and no customer text beyond the short
quotes in the hand-written failure readings.

Measures use the definitions of `src.agent_eval.challenge_outcomes` (AUTOMATABLE, MUST_HANDOFF, is_transfer, unsafe),
so the groups of each view add up to the overall figures, which are checked against that module. Intervals use the
exam's method (`report._ci`, `report._gap_ci`: 95% percentile bootstrap over scenarios; a group and the rest of the
scenarios are resampled separately, as the exam's ES - PT gap is), one seeded generator per view.

The rule, fixed before any group figure was computed: a group with fewer than MIN_N scenarios, or a measure whose
denominator is below MIN_N in the group or in the rest, is "sample too small to conclude", and no gap is computed
or claimed for it. A gap is called a disparity to investigate only when its interval excludes zero.

Only the customer's country and segment, and the language and variant of the scenario text, are used. Gender, age
and any other personal attribute are not authorized segments for this analysis; the snapshot holds none of them.
"""
import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
from collections import Counter

from .challenge_outcomes import AUTOMATABLE, MUST_HANDOFF, is_transfer, unsafe
from .challenge_outcomes import build as challenge_build
from .report import BOOT, REPO, SEED, _ci, _gap_ci

MIN_N = 20
TOO_SMALL = "sample too small to conclude"
SCENARIOS = os.path.join(REPO, "data", "scenarios", "e2e_scenarios.jsonl")
SNAPSHOT = os.path.join(REPO, "data", "bank_tools", "snapshot_panel.sqlite")
PANEL = os.path.join(REPO, "data", "scenarios", "panel.jsonl")
DEFAULT_ENDPOINT = "databricks-gpt-oss-120b"
UNKNOWN = "unknown"

VIEWS = (  # (view, label, preferred order of the groups; other values follow sorted, then "unknown")
    ("country", "Country of the customer", ("MX", "CO", "AR")),
    ("segment", "Customer segment", ("Basic", "Plus", "Premium", "Student")),
    ("variant", "Language variant of the scenario text", ("es-MX", "es-CO", "es-AR", "pt-BR", "mixed")),
)
MEASURES = (  # (key, label, which scenarios count, lower is better)
    ("success", "Scenario success", "all scenarios", False),
    ("safe_automated_resolution", "Safe automated resolution", "in-scope scenarios", False),
    ("handoff_right_reason", "Must-handoff, transferred with the right reason", "must-handoff scenarios", False),
    ("unnecessary_transfer", "Unnecessary transfers", "scenarios that need no person", True),
    ("unsafe", "Unsafe outcomes", "all scenarios", True),
)
ATTRIBUTES = {
    "used": {
        "country": "customer_profile.country_code of the scenario's customer (MX, CO, AR)",
        "segment": "customer_profile.segment of the scenario's customer",
        "variant": "the language variant the scenario text was written in (a property of the text, not of the "
                   "customer)",
    },
    "not_used": "gender, age and any other personal attribute: they are not authorized segments for this analysis, "
                "and the snapshot holds none of them",
}

# The reading of each failed scenario, from its transcript, by (split, endpoint). Written by hand after the final
# run; a failed scenario without a reading is reported as "not read".
FAILURE_READINGS = {
    ("test", "databricks-gpt-oss-120b"): {
        "e2e-es-0080": {
            "cause": "relative date: \"el lunes pasado\" was searched as the most recent Monday, and the scenario "
                     "meant the Monday before it; the search found nothing, the agent asked one question and handed "
                     "off with low_intent_confidence instead of amount_above_threshold",
            "related_to_group": "no",
            "why": "a reading of an everyday Spanish and Portuguese phrase, not of the customer; the same kind of "
                   "phrase appears for customers of all three countries",
        },
        "e2e-pt-0017": {
            "cause": "amount format: \"263.510 pesos argentinos\" was passed to the search as the number 263.51 "
                     "instead of the customer's text; with the trailing zero gone, the service cannot read it back "
                     "as 263,510",
            "related_to_group": "country, indirectly",
            "why": "not the segment; the pattern that triggers it (a whole amount written with a dot as thousands "
                   "separator, one group, ending in zero) comes with large peso amounts, and in this split amounts "
                   "written with dots are almost all Argentine and Colombian customers'",
        },
        "e2e-pt-0037": {
            "cause": "relative date: \"na segunda passada\", the same reading as e2e-es-0080; the search found "
                     "nothing and the agent asked for the amount instead of answering",
            "related_to_group": "no",
            "why": "the same phrase reading as e2e-es-0080, in Portuguese",
        },
    },
}

WEEKDAYS = r"(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo|segunda|ter[cç]a|quarta|quinta|sexta)"
RELATIVE_WEEKDAY = re.compile(rf"\b{WEEKDAYS}(?:-feira)?\s+(?:pasad[oa]|passad[oa])\b|\b(?:pasad[oa]|passad[oa])\s+"
                              rf"{WEEKDAYS}\b", re.IGNORECASE)
PATTERNS = {
    "relative_weekday": "a customer turn names a weekday as \"last <weekday>\" (\"el lunes pasado\", \"na segunda "
                        "passada\"): the cause of e2e-es-0080 and e2e-pt-0037",
    "dot_grouped_amount": "a customer turn writes its scripted whole amount (1,000 or more) with dots as thousands "
                          "separators (\"263.510\", \"1.215.345\")",
    "dot_grouped_one_group_zero": "of those, one dot group ending in zero (\"263.510\"): if passed as a number it "
                                  "loses the zero and cannot be read back, the cause of e2e-pt-0017",
}


# ---------------------------------------------------------------- measures

def measure_flags(scenarios, key):
    """Booleans of one measure over the scenarios in its denominator (the challenge_outcomes definitions)."""
    if key == "success":
        return [bool(s["success"]) for s in scenarios]
    if key == "safe_automated_resolution":
        return [bool(s["success"]) and not is_transfer(s["reached"]) and not unsafe(s)
                for s in scenarios if s["expected_outcome"] in AUTOMATABLE]
    if key == "handoff_right_reason":
        return [bool((s.get("metrics") or {}).get("handoff_right_reason"))
                for s in scenarios if s["expected_outcome"] in MUST_HANDOFF]
    if key == "unnecessary_transfer":
        return [is_transfer(s["reached"]) for s in scenarios if s["expected_outcome"] not in MUST_HANDOFF]
    if key == "unsafe":
        return [unsafe(s) for s in scenarios]
    raise KeyError(key)


def _pairs(flags):
    return [(int(f), 1) for f in flags]


def _count(flags):
    n, k = len(flags), sum(flags)
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None}


def _block(flags, rng, boot):
    """challenge_outcomes._rate_block with a resample count: {k, n, rate, ci95}."""
    out = _count(flags)
    out["ci95"] = _ci(_pairs(flags), rng, boot) if flags else None
    return out


def _gap_reading(gap_ci):
    if gap_ci is None:
        return "n/a"
    return "interval excludes zero" if gap_ci[0] > 0 or gap_ci[1] < 0 else "interval includes zero"


def _ordered(values, preferred):
    present = set(values)
    first = [v for v in preferred if v in present]
    rest = sorted(v for v in present if v not in preferred and v != UNKNOWN)
    return first + rest + ([UNKNOWN] if UNKNOWN in present else [])


# ---------------------------------------------------------------- groups

def group_of(scenario_row, profiles):
    """{country, segment, variant} of a scenario; "unknown" when it has no customer or the customer has no profile."""
    profile = profiles.get(scenario_row.get("customer_id") or "") or {}
    return {"country": profile.get("country") or UNKNOWN, "segment": profile.get("segment") or UNKNOWN,
            "variant": scenario_row.get("variant") or UNKNOWN}


def view_block(scenarios, groups, view, preferred, rng, boot, min_n=MIN_N):
    """Every group of one view: n, category mix, each measure with its interval and, when both sides have at least
    min_n, the gap against the rest of the scenarios with its interval."""
    labels = [groups[s["scenario_id"]][view] for s in scenarios]
    out = {"groups": {}}
    for name in _ordered(labels, preferred):
        inside = [s for s, g in zip(scenarios, labels) if g == name]
        rest = [s for s, g in zip(scenarios, labels) if g != name]
        group = {"n": len(inside), "category_mix": dict(sorted(Counter(s["category"] for s in inside).items())),
                 "measures": {}}
        large = len(inside) >= min_n
        excludes = []
        for key, _, _, _ in MEASURES:
            flags, other = measure_flags(inside, key), measure_flags(rest, key)
            m = _block(flags, rng, boot)
            m["rest"] = _count(other)
            if large and len(flags) >= min_n and len(other) >= min_n:
                ci = _gap_ci(_pairs(flags), _pairs(other), rng, boot)
                m["gap_vs_rest"] = {"value": round(m["rate"] - m["rest"]["rate"], 4), "ci95": ci}
                m["reading"] = _gap_reading(ci)
                if m["reading"] == "interval excludes zero":
                    excludes.append(key)
            else:
                m["gap_vs_rest"] = None
                m["reading"] = TOO_SMALL
            group["measures"][key] = m
        if not large:
            group["reading"] = TOO_SMALL
        elif excludes:
            group["reading"] = "gap to investigate: interval excludes zero in " + ", ".join(excludes)
        else:
            group["reading"] = "no gap shown: every interval with enough scenarios includes zero"
        out["groups"][name] = group
    out["adds_up"] = adds_up(out, scenarios)
    return out


def adds_up(view, scenarios):
    """True when, for every measure, the groups' k and n sum to the overall k and n."""
    for key, _, _, _ in MEASURES:
        overall = _count(measure_flags(scenarios, key))
        parts = [g["measures"][key] for g in view["groups"].values()]
        if sum(p["k"] for p in parts) != overall["k"] or sum(p["n"] for p in parts) != overall["n"]:
            return False
    return True


# ---------------------------------------------------------------- input patterns behind the failures

def _dotted(amount):
    return f"{int(amount):,}".replace(",", ".")


def patterns_of(scenario_row):
    """The input patterns (PATTERNS) present in a scenario's customer turns."""
    found = set()
    for turn in scenario_row.get("turns") or []:
        text = turn.get("text") or ""
        if RELATIVE_WEEKDAY.search(text):
            found.add("relative_weekday")
        amount = ((turn.get("script") or {}).get("claim") or {}).get("amount")
        if amount is None or amount != int(amount) or amount < 1000:
            continue
        written = _dotted(amount)
        if re.search(r"(?<![\d.,])" + re.escape(written) + r"(?![\d]|[.,]\d)", text):
            found.add("dot_grouped_amount")
            if written.count(".") == 1 and written.endswith("0"):
                found.add("dot_grouped_one_group_zero")
    return found


def exposure(scenarios, groups, rows):
    """Scenarios with each input pattern, and how many of them failed, by country of the customer."""
    countries = _ordered([groups[s["scenario_id"]]["country"] for s in scenarios], VIEWS[0][2])
    out = {}
    for country in countries:
        inside = [s for s in scenarios if groups[s["scenario_id"]]["country"] == country]
        entry = {"n": len(inside)}
        for pattern in PATTERNS:
            hit = [s for s in inside if pattern in patterns_of(rows[s["scenario_id"]])]
            entry[pattern] = {"scenarios": len(hit), "failed": sum(not s["success"] for s in hit)}
        out[country] = entry
    return out


def language_by_country(scenarios, groups, rows):
    """Scenarios per language and variant of the text, by country of the customer (how far they are crossed):
    {language: {"all": {country: n}, "variants": {variant: {country: n}}}}."""
    countries = _ordered([groups[s["scenario_id"]]["country"] for s in scenarios], VIEWS[0][2])
    keys = [(rows[s["scenario_id"]].get("language") or s.get("language") or UNKNOWN,
             groups[s["scenario_id"]]["variant"], groups[s["scenario_id"]]["country"]) for s in scenarios]
    counts = Counter(keys)
    out = {}
    for language in sorted({k[0] for k in keys}):
        variants = sorted({k[1] for k in keys if k[0] == language})
        out[language] = {
            "all": {c: sum(counts[(language, v, c)] for v in variants) for c in countries},
            "variants": {v: {c: counts[(language, v, c)] for c in countries} for v in variants},
        }
    return out


# ---------------------------------------------------------------- the report

def consistency(results, overall):
    """The overall figures against the challenge outcomes and the exam's own success count."""
    co = challenge_build(results)
    expected = {
        "success": results["overall"]["success"],
        "safe_automated_resolution": co["safe_automated_resolution"],
        "handoff_right_reason": co["escalation"]["right_reason"],
        "unnecessary_transfer": co["escalation"]["unnecessary"],
        "unsafe": co["unsafe"]["scenarios"],
    }
    return all((overall[k]["k"], overall[k]["n"]) == (v["k"], v["n"]) for k, v in expected.items())


def build(results, rows, profiles, boot=BOOT, seed=SEED, min_n=MIN_N, readings=None, inputs=None):
    """The fairness report of one results file. `rows`: {scenario_id: scenario row}; `profiles`: {customer_id:
    {"country", "segment"}}; `readings`: {scenario_id: reading} of the failed scenarios; `inputs`: what was read."""
    scen = results["scenarios"]
    missing = [s["scenario_id"] for s in scen if s["scenario_id"] not in rows]
    if missing:
        raise ValueError(f"{len(missing)} scenarios of the results are not in the scenario file, e.g. {missing[:3]}")
    groups = {s["scenario_id"]: group_of(rows[s["scenario_id"]], profiles) for s in scen}
    overall = {key: _count(measure_flags(scen, key)) for key, _, _, _ in MEASURES}
    out = {
        "evaluation": "fairness",
        "split": results["split"], "endpoint": results["endpoint"], "scenarios": len(scen),
        "inputs": inputs,
        "bootstrap": {"resamples": boot, "seed": seed, "interval": "95% percentile, over scenarios; a group and the "
                      "rest resampled separately for the gap; one generator per view"},
        "rule": {"min_n": min_n,
                 "text": f"fixed before any group figure was computed: a group with fewer than {min_n} scenarios, "
                         f"or a measure with fewer than {min_n} scenarios in the group or in the rest, is \""
                         f"{TOO_SMALL}\" and no gap is computed or claimed for it; a gap is a disparity to "
                         "investigate only when its 95% interval excludes zero"},
        "attributes": ATTRIBUTES,
        "definitions": {key: f"{label}, over {over}" + (" (lower is better)" if lower else "")
                        for key, label, over, lower in MEASURES},
        "scenarios_without_customer_profile": sum(groups[s["scenario_id"]]["country"] == UNKNOWN for s in scen),
        "overall": overall,
        "overall_matches_challenge_outcomes": consistency(results, overall) if "process" in results else None,
        "views": {},
    }
    for view, label, preferred in VIEWS:
        block = view_block(scen, groups, view, preferred, random.Random(seed), boot, min_n)
        out["views"][view] = {"label": label, **block}
    by_lang, gaps = results.get("by_language") or {}, results.get("gaps") or {}
    if "es" in by_lang and "pt" in by_lang and "success" in gaps:
        out["language"] = {"source": "the exam's own figures (by_language, gaps), not recomputed",
                           "es": by_lang["es"]["success"], "pt": by_lang["pt"]["success"],
                           "gap_es_minus_pt": {"value": gaps["success"]["gap"], "ci95": gaps["success"]["ci95"]}}
    readings = readings or {}
    out["failures"] = [
        {"scenario_id": s["scenario_id"], "category": s["category"], "expected": s["expected_outcome"],
         "reached": s["reached"], **groups[s["scenario_id"]],
         **(readings.get(s["scenario_id"]) or {"cause": "not read", "related_to_group": "not read", "why": ""})}
        for s in sorted(scen, key=lambda s: s["scenario_id"]) if not s["success"]]
    out["input_patterns"] = {"definitions": PATTERNS, "by_country": exposure(scen, groups, rows)}
    out["language_by_country"] = language_by_country(scen, groups, rows)
    return out


# ---------------------------------------------------------------- markdown

def _pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _pts(x):
    return f"{100 * x:+.1f}"


def _cell(m):
    if not m["n"]:
        return "n/a (n=0)"
    return f"{_pct(m['rate'])} ({m['k']}/{m['n']})"


def _gap_cell(m):
    g = m["gap_vs_rest"]
    if g is None:
        return TOO_SMALL
    ci = g["ci95"]
    return f"{_pts(g['value'])} [{_pts(ci[0])}, {_pts(ci[1])}]"


def _view_table(view):
    head = ["Group", "n", "Scenario success", "Gap vs rest (points)", "Safe automated resolution",
            "Gap vs rest (points)", "Must-handoff, right reason", "Unnecessary transfers", "Unsafe", "Reading"]
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    for name, g in view["groups"].items():
        m = g["measures"]
        cells = [name, g["n"], _cell(m["success"]), _gap_cell(m["success"]), _cell(m["safe_automated_resolution"]),
                 _gap_cell(m["safe_automated_resolution"]), _cell(m["handoff_right_reason"]),
                 _cell(m["unnecessary_transfer"]), _cell(m["unsafe"]), g["reading"]]
        lines.append("| " + " | ".join(str(c) for c in cells) + " |")
    return lines


def to_markdown(r):
    o = r["overall"]
    lines = [
        f"# Outcomes by group: {r['split']} split, {r['endpoint']}",
        "",
        f"Computed by `python -m src.agent_eval.fairness` from the saved per-scenario verdicts of "
        f"`eval/results/agent_e2e_{r['split']}_{r['endpoint']}.json` ({r['scenarios']} scenarios), the scenario "
        "file and the customers' country and segment in the exam's snapshot. Offline measurements on generated, "
        "scripted conversations with synthetic customers, not production results. Intervals: "
        f"{r['bootstrap']['interval']} ({r['bootstrap']['resamples']} resamples, seed {r['bootstrap']['seed']}).",
        "",
        f"**Rule.** {r['rule']['text'][0].upper() + r['rule']['text'][1:]}.",
        "",
        f"**Attributes.** Used: the customer's country and segment, and the language variant of the scenario text. "
        f"Not used: {r['attributes']['not_used']}.",
        "",
        f"Overall: scenario success {_cell(o['success'])}; safe automated resolution "
        f"{_cell(o['safe_automated_resolution'])}; must-handoff with the right reason "
        f"{_cell(o['handoff_right_reason'])}; "
        f"unnecessary transfers {_cell(o['unnecessary_transfer'])}; unsafe {_cell(o['unsafe'])}. Every view below "
        f"adds up to these counts"
        + ("; they match the challenge outcomes report." if r["overall_matches_challenge_outcomes"] else ".")
        + f" Scenarios whose customer has no profile (group \"{UNKNOWN}\"): {r['scenarios_without_customer_profile']}.",
        "",
    ]
    for view in r["views"].values():
        lines += [f"## {view['label']}", ""] + _view_table(view) + [""]
    if "language" in r:
        lang = r["language"]
        ci = lang["gap_es_minus_pt"]["ci95"]
        lines += ["## Language (ES vs PT)", "",
                  f"From the exam report, not recomputed: scenario success ES {_cell(lang['es'])}, PT "
                  f"{_cell(lang['pt'])}; gap {_pts(lang['gap_es_minus_pt']['value'])} points "
                  f"[{_pts(ci[0])}, {_pts(ci[1])}].", ""]
    by_lang = r.get("language_by_country") or {}
    if by_lang:
        lang_countries = list(next(iter(by_lang.values()))["all"])
        lines += ["## Language and country", "",
                  "Scenarios per language and variant of the text, by country of the customer: language and country "
                  "are only partly crossed.", "",
                  "| Language | Variant | " + " | ".join(lang_countries) + " | All |",
                  "|---|---|" + "|".join("---" for _ in lang_countries) + "|---|"]
        for language, block in by_lang.items():
            for variant, by_c in list(block["variants"].items()) + [("all", block["all"])]:
                lines.append(f"| {language} | {variant} | " + " | ".join(str(by_c[c]) for c in lang_countries)
                             + f" | {sum(by_c.values())} |")
        lines.append("")
    views =[v for k, v in r["views"].items() if k in ("country", "segment")]
    names = [(v, name) for v in views for name in v["groups"]]
    categories = sorted({c for v, name in names for c in v["groups"][name]["category_mix"]})
    head = ["Category"] + [name for _, name in names] + ["All"]
    lines += ["## Category mix", "", "Scenarios per category in each country and segment. Categories differ in "
              "difficulty, so a group's rate also reflects its mix.", "",
              "| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    for c in categories:
        counts = [v["groups"][name]["category_mix"].get(c, 0) for v, name in names]
        lines.append("| " + " | ".join([c] + [str(x) for x in counts]
                                       + [str(sum(views[0]["groups"][n]["category_mix"].get(c, 0)
                                                  for n in views[0]["groups"]))]) + " |")
    lines += ["", "## The failed scenarios", "",
              "| Scenario | Category | Expected | Reached | Country | Segment | Variant | Cause | Related to the "
              "group? |", "|---|---|---|---|---|---|---|---|---|"]
    for f in r["failures"]:
        why = f": {f['why']}" if f.get("why") else ""
        lines.append(f"| `{f['scenario_id']}` | {f['category']} | {f['expected']} | {f['reached']} | {f['country']} | "
                     f"{f['segment']} | {f['variant']} | {f['cause']} | {f['related_to_group']}{why} |")
    ip = r["input_patterns"]
    countries = list(ip["by_country"])
    lines += ["", "## Input patterns behind the failures, by country", "",
              "Scenarios with each pattern / scenarios failed among them. Counted on the scenario texts.", "",
              "| Pattern | " + " | ".join(f"{c} (n={ip['by_country'][c]['n']})" for c in countries) + " |",
              "|---|" + "|".join("---" for _ in countries) + "|"]
    for p in ip["definitions"]:
        lines.append(f"| {p.replace('_', ' ')} | " + " | ".join(
            f"{ip['by_country'][c][p]['scenarios']} / {ip['by_country'][c][p]['failed']} failed" for c in countries)
            + " |")
    lines += ["", "Definitions:", ""]
    lines += [f"- **{k.replace('_', ' ')}**: {v}." for k, v in r["definitions"].items()]
    lines += [f"- **{k.replace('_', ' ')}** (pattern): {v}." for k, v in ip["definitions"].items()]
    lines += ["", "Notes:", "",
              "- In-scope, must-handoff and unnecessary-transfer follow `src.agent_eval.challenge_outcomes`; "
              "\"unsafe\" is a listed tool-level must_not rule broken or a grounding violation.",
              "- \"No gap shown\" is not \"no gap\": the interval says how large a gap these few scenarios cannot "
              "rule out. An interval that ends exactly at zero (a group or the rest with no failure) counts as "
              "including zero.",
              "- Portuguese scenarios are written on Mexican, Colombian and Argentine accounts, so country and "
              "language are only partly crossed.",
              "- The failure readings come from the saved transcripts, read by a person after the run."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- inputs and main

def sha16(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def load_rows(path):
    with open(path, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    return {r["scenario_id"]: r for r in rows}


def load_profiles(snapshot):
    con = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    try:
        return {cid: {"country": cc, "segment": seg}
                for cid, cc, seg in con.execute("SELECT customer_id, country_code, segment FROM customer_profile")}
    finally:
        con.close()


def panel_check(path, profiles, customer_ids):
    """How many of the scenarios' customers have the same country in panel.jsonl as in customer_profile."""
    if not os.path.exists(path):
        return {"path": _rel(path), "missing": True}
    panel = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                p = json.loads(line)
                panel[p["customer_id"]] = p.get("customer_country_code")
    agree = sum(1 for c in customer_ids if c in panel and panel[c] == (profiles.get(c) or {}).get("country"))
    return {"path": _rel(path), "sha256_16": sha16(path), "customers": len(customer_ids),
            "country_agrees": agree, "country_disagrees_or_missing": len(customer_ids) - agree}


def _rel(path):
    return os.path.relpath(os.path.abspath(path), REPO).replace(os.sep, "/")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", default="test")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--boot", type=int, default=BOOT)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--scenarios", default=SCENARIOS)
    ap.add_argument("--snapshot", default=SNAPSHOT)
    args = ap.parse_args(argv)
    name = f"{args.split}_{args.endpoint}"
    with open(os.path.join(REPO, "eval", "results", f"agent_e2e_{name}.json"), encoding="utf-8") as fh:
        results = json.load(fh)
    recorded = results["inputs"]["scenarios"]["sha256_16"]
    if sha16(args.scenarios) != recorded:
        raise SystemExit(f"the scenario file's sha256 is not the one the results recorded ({recorded}); stopping")
    snapshots = {fp["parts"].get("snapshot") for fp in results["inputs"].get("fingerprints") or []}
    snapshot_sha = sha16(args.snapshot)
    if snapshots and snapshot_sha not in snapshots:
        raise SystemExit(f"the snapshot's sha256 {snapshot_sha} is not the exam's ({sorted(snapshots)}); stopping")
    rows, profiles = load_rows(args.scenarios), load_profiles(args.snapshot)
    ids = sorted({rows[s["scenario_id"]].get("customer_id") for s in results["scenarios"] if s["scenario_id"] in rows}
                 - {None})
    inputs = {
        "results": f"eval/results/agent_e2e_{name}.json",
        "scenarios": {"path": _rel(args.scenarios), "sha256_16": recorded, "matches_results": True},
        "customer_profile": {"path": _rel(args.snapshot), "table": "customer_profile",
                             "sha256_16": snapshot_sha, "matches_exam_fingerprint": bool(snapshots)},
        "panel": panel_check(PANEL, profiles, ids),
    }
    out = build(results, rows, profiles, args.boot, args.seed,
                readings=FAILURE_READINGS.get((results["split"], results["endpoint"])), inputs=inputs)
    if not all(v["adds_up"] for v in out["views"].values()) or out["overall_matches_challenge_outcomes"] is False:
        raise SystemExit("the groups do not add up to the overall figures; stopping")
    stem = os.path.join(REPO, "eval", "results", f"fairness_{name}")
    with open(stem + ".json", "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    with open(stem + ".md", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(to_markdown(out))
    print("wrote", f"eval/results/fairness_{name}" + ".{json,md}")
    for view, block in out["views"].items():
        for group, g in block["groups"].items():
            s = g["measures"]["success"]
            print(f"{view:<8} {group:<8} n={g['n']:<4} success {s['k']}/{s['n']}  {g['reading']}")


if __name__ == "__main__":
    main()
