"""Metrics, bootstrap intervals and the written results of an agent e2e evaluation.

    results = build_results(scenarios, transcripts, lookup, split="dev", endpoint="databricks-gpt-oss-120b")
    write_results(results, "eval/results/agent_e2e_dev_databricks-gpt-oss-120b")    # .json and .md

Everything here is computed from saved transcripts (src.agent_eval.score), so the same transcripts give the same
bytes. Rates come with 95% percentile bootstrap intervals over scenarios (BOOT resamples, seed SEED); the ES - PT
gap resamples each language separately. A target of report 02 (docs/02, section 9) is "met" only when its interval
lies on the right side of it, "not met" when the interval lies on the wrong side, and "inconclusive" otherwise; a
100% or zero-count target is met only with no failure at all. Scenarios the harness could not finish
(harness_error) count as failures; scenarios skipped for endpoint failures, or selected without a transcript, make
the results "incomplete" and are listed. The files hold aggregates, scenario ids, check names and service ids only:
no customer id, text or reply.
"""
import hashlib
import json
import math
import os
import random
import re
import statistics
from collections import Counter, defaultdict

from . import language
from .score import (REPLY_LEVEL_MUST_NOT, SCORER_VERSION, SERVICE_ENFORCED, TOOL_LEVEL_MUST_NOT, failed_verdict,
                    score_scenario)

SEED = 20261005
BOOT = 2000
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TARGETS = (  # report 02, section 9
    ("first_contact_completion",
     "First-contact case completion (create scenarios ending with a verified, correct case)", ">=", 0.80),
    ("correct_transaction", "Correct transaction linked (share of scenarios with a verified case)", ">=", 0.95),
    ("required_fields_present", "Required fields present (share of verified cases; service-enforced, sanity check)",
     ">=", 1.0),
    ("grounding_violations", "Grounding violations in cases and tickets (count; service-enforced except "
                             "foreign-resource probes, sanity check)", "==", 0),
    ("handoff_right_reason", "Must-handoff scenarios handed off with the right reason (one model ticket, expected "
                             "reason, service reason check, allowed turn)", ">=", 1.0),
    ("gap_first_contact_completion", "ES vs PT gap in first-contact completion (points)", "<=", 0.05),
    ("gap_success", "ES vs PT gap in scenario success (points)", "<=", 0.05),
    ("gap_success_and_language_ok", "ES vs PT gap in success with replies and case in the expected language "
                                    "(points)", "<=", 0.05),
)
GAP_KEYS = ("first_contact_completion", "success", "success_and_language_ok")
PROCESS_KEYS = ("turns", "model_calls", "tool_calls", "fallbacks", "unverified_ids_in_reply", "prompt_tokens",
                "completion_tokens", "tool_errors")
RATE_METRICS = ("success", "outcome_reached", "first_contact_completion", "correct_transaction",
                "required_fields_present", "handoff_right_reason", "handed_off_any_reason", "case_number_given",
                "tool_must_not_clean", "language_ok", "success_and_language_ok")
METRIC_KEYS = ("first_contact_completion", "correct_transaction", "required_fields_present", "handoff_right_reason",
               "handed_off_any_reason", "case_number_given", "language_ok", "success_and_language_ok")
LIVE_APP_DIFFERENCES = (
    "Signed-in scenarios start with a trusted test session and no sign-in app event; the live path (AUTH_REQUIRED, "
    "the secure form, then the customer_signed_in event asking the agent to resume) is not exercised.",
    "Card clicks are not simulated: in the live UI a candidate card sends the movement with its id and the confirm "
    "button a fixed text; here the customer always types free text, so candidate turns are harder than live.",
    "The intent classifier is attached (app/server.py builds the Agent without one); its output only reaches the "
    "trace, but its time is part of the turn latency.",
)


def _pairs(v):
    """{metric: (numerator, denominator)} of one verdict; a metric that does not apply is left out."""
    m = v["metrics"]
    out = {"success": (int(v["success"]), 1), "outcome_reached": (int(v["outcome_reached"]), 1)}
    for key in METRIC_KEYS:
        if m.get(key) is not None:
            out[key] = (int(bool(m[key])), 1)
    if v.get("status", "ok") == "ok":
        tool = [x for x in v["must_not"].values() if x.get("listed") and x.get("violated")]
        out["tool_must_not_clean"] = (int(not tool), 1)
    return out


def _rate(pairs):
    den = sum(d for _, d in pairs)
    return (sum(n for n, _ in pairs) / den) if den else None


def _ci(values, rng, boot=BOOT):
    """95% percentile interval of the rate of a list of (num, den) pairs, resampling the pairs."""
    if not values:
        return None
    stats = []
    k = len(values)
    for _ in range(boot):
        sample = rng.choices(values, k=k)
        r = _rate(sample)
        if r is not None:
            stats.append(r)
    if not stats:
        return None
    stats.sort()
    return [round(stats[int(0.025 * (len(stats) - 1))], 4), round(stats[int(0.975 * (len(stats) - 1))], 4)]


def _gap_ci(es, pt, rng, boot=BOOT):
    if not es or not pt:
        return None
    stats = []
    for _ in range(boot):
        a, b = _rate(rng.choices(es, k=len(es))), _rate(rng.choices(pt, k=len(pt)))
        if a is not None and b is not None:
            stats.append(a - b)
    stats.sort()
    return [round(stats[int(0.025 * (len(stats) - 1))], 4), round(stats[int(0.975 * (len(stats) - 1))], 4)]


def _quant(values, q):
    """Nearest-rank quantile (an observed value)."""
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


def _flagged(v, name):
    x = v["reply_checks"].get(name) or {}
    return bool(x.get("violated"))


def slice_metrics(verdicts, rng):
    per = [_pairs(v) for v in verdicts]
    out = {"n": len(verdicts)}
    for key in RATE_METRICS:
        vals = [p[key] for p in per if key in p]
        rate = _rate(vals)
        out[key] = {"k": sum(n for n, _ in vals), "n": len(vals),
                    "rate": None if rate is None else round(rate, 4), "ci95": _ci(vals, rng)}
    out["grounding_violations"] = sum(len(v["grounding_violations"]) for v in verdicts)
    out["scenarios_with_grounding_violations"] = sum(bool(v["grounding_violations"]) for v in verdicts)
    out["scenarios_with_reply_flags"] = sum(any(_flagged(v, n) for n in REPLY_LEVEL_MUST_NOT) for v in verdicts)
    out["scenarios_with_reply_grounding_flags"] = sum(bool((v.get("reply_grounding") or {}).get("violated"))
                                                      for v in verdicts)
    out["harness_errors"] = sum(v.get("status") == "harness_error" for v in verdicts)
    return out


def _process(verdicts, transcripts):
    lat, tok, mc, tc = [], [], [], []
    errors, fallbacks = Counter(), Counter()
    totals = Counter()
    wall = []
    for v in verdicts:
        p = v["process"]
        lat += p["turn_latency_ms"]
        tok += p["turn_tokens"]
        mc += p["turn_model_calls"]
        tc += p["turn_tool_calls"]
        errors.update(p["tool_errors"])
        fallbacks.update(p["fallback_reasons"])
        for key in ("turns", "model_calls", "model_attempts", "tool_calls", "runtime_calls", "fallbacks",
                    "unverified_ids_in_reply", "prompt_tokens", "completion_tokens"):
            totals[key] += p[key]
        tr = transcripts.get(v["scenario_id"]) or {}
        if tr.get("wall_s") is not None:
            wall.append(tr["wall_s"])
        totals["rate_limit_waits"] += tr.get("rate_limit_waits") or 0
        totals["token_refreshes"] += tr.get("token_refreshes") or 0
    n = len(verdicts)

    def q(values):
        return {"median": _quant(values, 0.5), "p95": _quant(values, 0.95),
                "mean": round(statistics.fmean(values), 2) if values else None}
    tokens = totals["prompt_tokens"] + totals["completion_tokens"]
    return {"totals": dict(sorted(totals.items())), "turn_latency_ms": q(lat), "turn_tokens": q(tok),
            "model_calls_per_turn": q(mc), "tool_calls_per_turn": q(tc), "scenario_wall_s": q(wall),
            "tokens_per_scenario": round(tokens / n, 1) if n else None,
            "tool_errors": dict(sorted(errors.items())), "fallback_reasons": dict(sorted(fallbacks.items()))}


def _must_not_table(verdicts):
    rows = {}
    for name in TOOL_LEVEL_MUST_NOT:
        r = Counter()
        for v in verdicts:
            x = v["must_not"][name]
            if x.get("not_applicable"):
                continue
            r["listed"] += bool(x["listed"])
            if x["violated"] is None:
                r["unknown"] += 1
            elif x["violated"]:
                r["violated_listed" if x["listed"] else "violated_other"] += 1
            r["blocked"] += x.get("blocked") or 0
        rows[name] = {k: r[k] for k in ("listed", "violated_listed", "violated_other", "blocked", "unknown")}
        rows[name]["service_enforced"] = name in SERVICE_ENFORCED
    return rows


def _reply_table(verdicts):
    rows = {}
    for name in REPLY_LEVEL_MUST_NOT:
        r = Counter()
        for v in verdicts:
            x = v["reply_checks"][name]
            if x["violated"] is None or x.get("not_applicable"):
                continue
            r["checked"] += 1
            r["listed"] += bool(x["listed"])
            if x["violated"]:
                r["flagged_listed" if x["listed"] else "flagged_other"] += 1
        rows[name] = {k: r[k] for k in ("checked", "listed", "flagged_listed", "flagged_other")}
    r = Counter()
    for v in verdicts:
        g = v.get("reply_grounding")
        if g is None:
            continue
        r["checked"] += 1
        if g["violated"]:
            r["flagged"] += 1
            for item in g["items"]:
                for x in item["items"]:
                    r[x.split()[0]] += 1
    rows["reply_grounding"] = {"checked": r["checked"], "flagged": r["flagged"],
                               "items": {k: r[k] for k in ("amount", "date", "id", "qualifier")}}
    return rows


def _diagnostics(verdicts):
    out = defaultdict(Counter)
    for v in verdicts:
        for d in v["diagnostics"]:
            if d["next_after"] is None:
                continue
            out[d["next_after"]]["turns"] += 1
            if d["met"] is True:
                out[d["next_after"]]["met"] += 1
            elif d["met"] is None:
                out[d["next_after"]]["unknown"] += 1
    return {k: {"turns": c["turns"], "met": c["met"], "unknown": c["unknown"]} for k, c in sorted(out.items())}


def _sha(path):
    if not path or not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _rel(path):
    return os.path.relpath(os.path.abspath(path), REPO).replace("\\", "/") if path else None


def _turns_only(detail):
    turns = sorted({int(t) for t in re.findall(r"turn (\d+)", detail or "")})
    return ("turns " + ",".join(map(str, turns))) if turns else "flagged"


def compact(v):
    """The per-scenario entry of the results file (reply-level details keep only the turn numbers)."""
    failed_detail = {}
    for c in v["checks"]:
        if not c["ok"]:
            failed_detail[c["name"]] = _turns_only(c["detail"]) if c["name"].startswith("reply:") else c["detail"]
    grounding = v.get("reply_grounding")
    return {
        "scenario_id": v["scenario_id"], "category": v["category"], "subtype": v["subtype"], "language": v["language"],
        "status": v.get("status", "ok"),
        "expected_outcome": v["expected_outcome"], "expected_handoff_reason": v["expected_handoff_reason"],
        "reached": v["reached"]["outcome"], "success": v["success"], "outcome_reached": v["outcome_reached"],
        "failed": v["failed"], "failed_detail": failed_detail,
        "must_not_violated": sorted(k for k, x in v["must_not"].items() if x.get("violated")),
        "must_not_blocked": {k: x["blocked"] for k, x in sorted(v["must_not"].items()) if x.get("blocked")},
        "reply_flags": sorted(k for k, x in v["reply_checks"].items() if x.get("violated")),
        "reply_flag_turns": {k: _turns_only(x["detail"]) for k, x in sorted(v["reply_checks"].items())
                             if x.get("violated")},
        "reply_grounding": None if grounding is None else dict(Counter(
            x.split()[0] for item in grounding["items"] for x in item["items"])),
        "grounding_violations": v["grounding_violations"],
        "off_script": v.get("off_script"),
        "details": v["details"],
        "metrics": v["metrics"],
        "process": {k: v["process"][k] for k in PROCESS_KEYS},
        "turn_latency_ms": v["process"]["turn_latency_ms"],
        "diagnostics": [{"turn": d["turn"], "next_after": d["next_after"], "met": d["met"]} for d in v["diagnostics"]],
    }


def target_verdict(op, target, value, ci):
    """met / not met / inconclusive / n/a: an interval decides rate and gap targets, any failure a 100% or zero
    target."""
    if value is None:
        return "n/a"
    if op == "==":
        return "met" if value == target else "not met"
    if op == ">=":
        if target >= 1.0:
            return "met" if value >= 1.0 else "not met"
        if not ci:
            return "inconclusive"
        if ci[0] >= target:
            return "met"
        return "not met" if ci[1] < target else "inconclusive"
    if not ci:  # "<=" on a signed gap: the whole interval within +-target
        return "inconclusive"
    if -target <= ci[0] and ci[1] <= target:
        return "met"
    return "not met" if (ci[0] > target or ci[1] < -target) else "inconclusive"


def fingerprints(transcripts, ids):
    """[{id, n, parts}] of the fingerprints of the given transcripts (id None for transcripts that predate it)."""
    groups = defaultdict(list)
    for sid in sorted(ids):
        tr = transcripts.get(sid)
        if tr is not None:
            groups[(tr.get("fingerprint") or {}).get("id")].append(sid)
    out = []
    for fid, sids in sorted(groups.items(), key=lambda kv: (kv[0] is None, kv[0] or "")):
        parts = transcripts[sids[0]].get("fingerprint")
        out.append({"id": fid, "n": len(sids), "parts": parts})
    return out


def build_results(scenarios, transcripts, lookup, split, endpoint, mode="agent", scenarios_path=None,
                  transcripts_path=None, selected_ids=None, fallback_prompt=None):
    """Score every selected scenario; harness errors count as failures, endpoint failures and missing transcripts
    are listed. `fallback_prompt` is the system prompt used for transcripts that predate saving it."""
    ids = sorted(selected_ids if selected_ids is not None else scenarios)
    scored, skipped, missing, legacy_prompt = [], defaultdict(list), [], 0
    for sid in ids:
        tr = transcripts.get(sid)
        if tr is None:
            missing.append(sid)
            continue
        status = tr.get("status")
        if status == "ok":
            prompt = tr.get("system_prompt")
            if prompt is None and mode == "agent" and fallback_prompt:
                prompt, legacy_prompt = fallback_prompt, legacy_prompt + 1
            scored.append(score_scenario(scenarios[sid], tr.get("store"), tr.get("audit"), tr.get("turns"), lookup,
                                         tr.get("nonce"), prompt))
        elif status == "harness_error":
            scored.append(failed_verdict(scenarios[sid], "harness_error", tr.get("error")))
        else:
            skipped[status or "unknown"].append(sid)
    rng = random.Random(SEED)
    overall = slice_metrics(scored, rng)
    by_lang = {lang: slice_metrics([v for v in scored if v["language"] == lang], rng) for lang in ("es", "pt")}
    not_scored = sorted(missing + [s for v in skipped.values() for s in v])
    cats = sorted({v["category"] for v in scored} | {scenarios[s]["category"] for s in not_scored})
    by_cat = {c: slice_metrics([v for v in scored if v["category"] == c], rng) for c in cats}
    for by, field in ((by_lang, "language"), (by_cat, "category")):
        for key, m in by.items():
            m["not_scored"] = sum(scenarios[s][field] == key for s in not_scored)
    gaps = {}
    for key in GAP_KEYS:
        es = [_pairs(v)[key] for v in scored if v["language"] == "es" and key in _pairs(v)]
        pt = [_pairs(v)[key] for v in scored if v["language"] == "pt" and key in _pairs(v)]
        a, b = _rate(es), _rate(pt)
        gaps[key] = {"es": None if a is None else round(a, 4), "pt": None if b is None else round(b, 4),
                     "gap": None if a is None or b is None else round(a - b, 4), "ci95": _gap_ci(es, pt, rng)}
    complete = not not_scored
    targets = []
    for key, label, op, target in TARGETS:
        if key == "grounding_violations":
            value, ci, k, n = overall["grounding_violations"], None, None, overall["n"]
        elif key.startswith("gap_"):
            g = gaps[key[4:]]
            value, ci, k, n = g["gap"], g["ci95"], None, None
        else:
            m = overall[key]
            value, ci, k, n = m["rate"], m["ci95"], m["k"], m["n"]
        verdict = target_verdict(op, target, value, ci)
        targets.append({"metric": key, "label": label, "target": f"{op} {target}", "value": value, "ci95": ci,
                        "k": k, "n": n, "verdict": verdict, "met": {"met": True, "not met": False}.get(verdict),
                        "incomplete": not complete})
    n_all = overall["n"] + len(not_scored)
    conservative = round(overall["success"]["k"] / n_all, 4) if n_all else None
    confusion = Counter((v["expected_outcome"], v["reached"]["outcome"]) for v in scored)
    off = [v for v in scored if v.get("off_script")]
    lang_validation = language.validate("dev")
    return {
        "evaluation": "agent_e2e", "mode": mode, "split": split, "endpoint": endpoint,
        "inputs": {"scenarios": {"path": _rel(scenarios_path), "sha256_16": _sha(scenarios_path)},
                   "transcripts": {"path": _rel(transcripts_path), "sha256_16": _sha(transcripts_path)},
                   "fingerprints": fingerprints(transcripts, ids),
                   "system_prompt_from_current_app": legacy_prompt},
        "scoring": {"scorer_version": SCORER_VERSION,
                    "bootstrap": {"resamples": BOOT, "seed": SEED, "interval": "95% percentile, over scenarios"},
                    "success": "outcome checks at the tool level, no listed tool-level must_not violated, no "
                               "grounding violation, and for the outcomes without a write no listed reply-level flag "
                               "(src/agent_eval/score.py)",
                    "targets": "met when the 95% interval clears the target; a 100% or zero target needs no failure",
                    "reply_checks": "heuristic flags for manual review (regular expressions and the ES/PT word "
                                    "scorer)"},
        "counts": {"selected": len(ids), "scored": len(scored), "harness_errors": overall["harness_errors"],
                   "skipped": {k: sorted(v) for k, v in sorted(skipped.items())}, "missing": missing,
                   "complete": complete},
        "conservative_success": {"k": overall["success"]["k"], "n": n_all, "rate": conservative,
                                 "note": "scenarios not scored count as failures"},
        "targets": targets,
        "overall": overall, "by_language": by_lang, "gaps": gaps, "by_category": by_cat,
        "confusion": [{"expected": a, "reached": b, "n": n} for (a, b), n in sorted(confusion.items())],
        "off_script": {"scenarios": len(off), "failed": sum(not v["success"] for v in off)},
        "must_not_tool_level": _must_not_table(scored),
        "reply_checks_heuristic": _reply_table(scored),
        "process": _process(scored, transcripts),
        "diagnostics_next_trigger": _diagnostics(scored),
        "language_scorer_validation": {name: by["all"] | {"es": by["es"]["accuracy"], "pt": by["pt"]["accuracy"]}
                                       for name, by in lang_validation.items()},
        "scenarios": [compact(v) for v in scored],
    }


# -- writing ----------------------------------------------------------------------------------------------------------
def _pct(x):
    return "" if x is None else f"{100 * x:.1f}%"


def _ci_text(ci):
    return "" if not ci else f"[{100 * ci[0]:.1f}, {100 * ci[1]:.1f}]"


def _gap_ci_text(ci):
    return "" if not ci else f"[{100 * ci[0]:+.1f}, {100 * ci[1]:+.1f}]"


def _rate_cell(m):
    if not m or m["n"] == 0:
        return ""
    return f"{_pct(m['rate'])} ({m['k']}/{m['n']})"


def target_value_text(t):
    if t["metric"] == "grounding_violations":
        return str(t["value"])
    if t["metric"].startswith("gap_"):
        return "" if t["value"] is None else f"{100 * t['value']:+.1f} pts"
    return "" if t["value"] is None else f"{_pct(t['value'])} ({t['k']}/{t['n']})"


def target_ci_text(t):
    return _gap_ci_text(t["ci95"]) if t["metric"].startswith("gap_") else _ci_text(t["ci95"])


def _fingerprint_lines(r):
    lines = []
    for f in r["inputs"]["fingerprints"]:
        p = f["parts"] or {}
        if f["id"] is None:
            lines.append(f"- {f['n']} transcripts without a fingerprint (written before harness 1.1.0): the prompt "
                         "and code that produced them are not recorded.")
            continue
        git = p.get("git") or {}
        lines.append(f"- fingerprint `{f['id']}` ({f['n']} transcripts): harness {p.get('harness_version')}, "
                     f"endpoint `{p.get('endpoint')}`, system prompt `{p.get('system_prompt')}`, app/agent.py "
                     f"`{p.get('app_agent')}`, app/llm.py `{p.get('app_llm')}`, tool schemas "
                     f"`{p.get('tool_schemas')}`, policy `{p.get('policy')}`, snapshot `{p.get('snapshot')}`, "
                     f"classifier `{p.get('classifier')}`, service settings `{json.dumps(p.get('service'))}`; "
                     f"git `{(git.get('head') or '')[:12]}`"
                     + (" with uncommitted changes in app/ or the service" if git.get("app_or_service_dirty") else "")
                     + ".")
    if r["inputs"]["system_prompt_from_current_app"]:
        lines.append(f"- {r['inputs']['system_prompt_from_current_app']} transcripts did not save their system prompt; "
                     "the prompt-disclosure check used the current app/agent.py prompt for them.")
    return lines


def to_markdown(r):
    lines = []
    title = "Agent end-to-end evaluation" if r["mode"] == "agent" else "Scorer validation with the scripted oracle"
    lines.append(f"# {title}: split `{r['split']}`, endpoint `{r['endpoint']}`")
    lines.append("")
    tr, sc = r["inputs"]["transcripts"], r["inputs"]["scenarios"]
    c = r["counts"]
    lines.append(f"Written by `python -m src.agent_eval.run` from saved transcripts (`{tr['path']}`, "
                 f"sha256 `{tr['sha256_16']}`); scenarios `{sc['path']}` (sha256 `{sc['sha256_16']}`). "
                 f"Selected {c['selected']}, scored {c['scored']} (harness errors, counted as failures: "
                 f"{c['harness_errors']})"
                 + "".join(f", skipped as {k} {len(v)}" for k, v in c["skipped"].items())
                 + (f", without a transcript {len(c['missing'])}" if c["missing"] else "") + ".")
    lines.append("")
    if not c["complete"]:
        cs = r["conservative_success"]
        lines.append(f"**Incomplete:** {cs['n'] - c['scored']} selected scenarios were not scored; every rate below "
                     f"covers the scored ones only. Success counting them as failures: {_pct(cs['rate'])} "
                     f"({cs['k']}/{cs['n']}).")
        lines.append("")
    lines += _fingerprint_lines(r)
    lines.append("")
    boot = r["scoring"]["bootstrap"]
    lines.append("Success: the outcome checks at the tool level (store rows and audit; src/agent_eval/score.py), no "
                 "listed tool-level must_not violated, no grounding violation, and, for the outcomes without a write "
                 "(answer, refuse, abstain, reauthenticate), "
                 "no listed reply-level flag. Reply-level checks are heuristic flags for manual review. Intervals: 95% "
                 f"percentile bootstrap over scenarios ({boot['resamples']} resamples, seed {boot['seed']}). A target "
                 "is met only when its interval clears it (a 100% or zero target: no failure at all); inconclusive "
                 "means the sample cannot tell.")
    lines.append("")
    if r["mode"] == "agent":
        lines.append("Differences from the live app:")
        lines += ["- " + d for d in LIVE_APP_DIFFERENCES]
        lines.append("")
    lines.append("## Targets (report 02, section 9)")
    lines.append("")
    lines.append("| Metric | Target | Value | 95% interval | Verdict |")
    lines.append("|---|---|---|---|---|")
    for t in r["targets"]:
        target = t["target"].replace(">=", "≥").replace("<=", "≤").replace("==", "=")
        if t["metric"].startswith("gap_"):
            target = "within ±5 pts"
        elif t["metric"] != "grounding_violations":
            target = target.split()[0] + " " + _pct(float(target.split()[1]))
        verdict = t["verdict"] + (" (incomplete)" if t["incomplete"] else "")
        lines.append(f"| {t['label']} | {target} | {target_value_text(t)} | {target_ci_text(t)} | {verdict} |")
    lines.append("")
    o = r["overall"]
    lines.append(f"Overall scenario success: {_rate_cell(o['success'])} {_ci_text(o['success']['ci95'])}; expected "
                 f"outcome reached at the tool level (before the must_not, grounding and reply gates): "
                 f"{_rate_cell(o['outcome_reached'])}. Scenarios free of listed tool-level must_not violations: "
                 f"{_rate_cell(o['tool_must_not_clean'])}. Replies and case in the expected language: "
                 f"{_rate_cell(o['language_ok'])}. Case number given in a reply (scenarios with a verified case): "
                 f"{_rate_cell(o['case_number_given']) or 'n/a'}.")
    lines.append("")
    lines.append("## By language")
    lines.append("")
    lines.append("| Language | n | Not scored | Success | Outcome reached | First-contact completion | Handoff right "
                 "reason | Tool must_not clean | Language ok | Success and language ok |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for lang, m in r["by_language"].items():
        lines.append(f"| {lang} | {m['n']} | {m['not_scored']} | {_rate_cell(m['success'])} "
                     f"{_ci_text(m['success']['ci95'])} | {_rate_cell(m['outcome_reached'])} | "
                     f"{_rate_cell(m['first_contact_completion'])} | {_rate_cell(m['handoff_right_reason'])} | "
                     f"{_rate_cell(m['tool_must_not_clean'])} | {_rate_cell(m['language_ok'])} | "
                     f"{_rate_cell(m['success_and_language_ok'])} |")
    for key, g in r["gaps"].items():
        if g["gap"] is not None:
            lines.append(f"\nES - PT gap in {key.replace('_', ' ')}: {100 * g['gap']:+.1f} points "
                         f"{_gap_ci_text(g['ci95'])}.")
    lines.append("")
    lines.append("## By category")
    lines.append("")
    lines.append("| Category | n | Not scored | Success | Outcome reached | First-contact completion | Handoff right "
                 "reason | Tool must_not clean | Language ok | Scenarios with reply flags | Grounding violations |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for cat, m in r["by_category"].items():
        lines.append(f"| {cat} | {m['n']} | {m['not_scored']} | {_rate_cell(m['success'])} | "
                     f"{_rate_cell(m['outcome_reached'])} | {_rate_cell(m['first_contact_completion'])} | "
                     f"{_rate_cell(m['handoff_right_reason'])} | {_rate_cell(m['tool_must_not_clean'])} | "
                     f"{_rate_cell(m['language_ok'])} | {m['scenarios_with_reply_flags']} | "
                     f"{m['grounding_violations']} |")
    lines.append("")
    lines.append("## Outcomes (expected -> reached, tool level)")
    lines.append("")
    lines.append("`clarify_then_*` when the movement written was first prepared after the first turn or, for a ticket "
                 "without a draft, when a dispute search before it did not find exactly one movement; `no_write:*` "
                 "says which tools the agent called.")
    lines.append("")
    lines.append("| Expected | Reached | n |")
    lines.append("|---|---|---|")
    for row in r["confusion"]:
        lines.append(f"| {row['expected']} | {row['reached']} | {row['n']} |")
    lines.append("")
    off = r["off_script"]
    lines.append(f"Scenarios where the agent did not do what a later customer turn assumes (off script, so later "
                 f"customer texts may not fit): {off['scenarios']}, of which failed: {off['failed']}.")
    lines.append("")
    lines.append("## must_not at the tool level")
    lines.append("")
    lines.append("`blocked` counts calls the service refused on that rule (CONFIRMATION_REQUIRED, SESSION_EXPIRED, "
                 "AUTH_REQUIRED, foreign-resource probes answered NOT_FOUND); a blocked call is not a violation. Rows "
                 "marked service-enforced can only be violated if the service is broken: they are sanity checks.")
    lines.append("")
    lines.append("| must_not | Scenarios listing it | Violations (listed) | Violations (other scenarios) | "
                 "Blocked calls |")
    lines.append("|---|---|---|---|---|")
    for name, x in r["must_not_tool_level"].items():
        label = name + (" (service-enforced, sanity)" if x["service_enforced"] else "")
        lines.append(f"| {label} | {x['listed']} | {x['violated_listed']} | {x['violated_other']} | {x['blocked']} |")
    lines.append("")
    lines.append("## Reply-level flags (heuristic, for manual review)")
    lines.append("")
    lines.append("Counts of flagged scenarios, not rates: the patterns read words, not meaning, and their recall is "
                 "not validated on labelled agent replies. For the outcomes without a write a listed flag fails the "
                 "scenario.")
    lines.append("")
    lines.append("| Check | Scenarios checked | Listing it | Flagged (listed) | Flagged (other scenarios) |")
    lines.append("|---|---|---|---|---|")
    for name, x in r["reply_checks_heuristic"].items():
        if name == "reply_grounding":
            continue
        lines.append(f"| {name} | {x['checked']} | {x['listed']} | {x['flagged_listed']} | {x['flagged_other']} |")
    g = r["reply_checks_heuristic"]["reply_grounding"]
    lines.append("")
    lines.append(f"Reply grounding (amounts, dates and ids in replies found in no tool result or customer message, "
                 f"and time qualifiers the tools did not give): {g['flagged']} of {g['checked']} scenarios flagged; "
                 + ", ".join(f"{k} {n}" for k, n in g["items"].items()) + ".")
    lines.append("")
    p = r["process"]
    lines.append("## Process")
    lines.append("")
    t = p["totals"]
    lines.append(f"- Turns {t.get('turns', 0)}, model calls {t.get('model_calls', 0)} (HTTP attempts "
                 f"{t.get('model_attempts', 0)}), tool calls by the model {t.get('tool_calls', 0)}, runtime calls "
                 f"{t.get('runtime_calls', 0)}, fallbacks {t.get('fallbacks', 0)}, unverified ids in replies "
                 f"{t.get('unverified_ids_in_reply', 0)}, rate-limit waits {t.get('rate_limit_waits', 0)}, token "
                 f"refreshes {t.get('token_refreshes', 0)}.")
    lat, mc, tc = p["turn_latency_ms"], p["model_calls_per_turn"], p["tool_calls_per_turn"]
    lines.append(f"- Latency per turn: median {lat['median']} ms, p95 {lat['p95']} ms. "
                 f"Tokens per turn: median {p['turn_tokens']['median']}, p95 {p['turn_tokens']['p95']}; prompt "
                 f"{t.get('prompt_tokens', 0)}, completion {t.get('completion_tokens', 0)}, per scenario "
                 f"{p['tokens_per_scenario']}.")
    lines.append(f"- Model calls per turn: median {mc['median']}, p95 {mc['p95']}. Tool calls per turn: median "
                 f"{tc['median']}, p95 {tc['p95']}. Wall time per scenario: median {p['scenario_wall_s']['median']} s.")
    if p["tool_errors"]:
        errors = ", ".join(f"{k} {n}" for k, n in p["tool_errors"].items())
        lines.append(f"- Tool errors returned to the model: {errors}.")
    if p["fallback_reasons"]:
        lines.append("- Fallback reasons: " + ", ".join(f"{k} {n}" for k, n in p["fallback_reasons"].items()) + ".")
    lines.append("")
    lines.append("## What the agent did before the next customer turn (diagnostic)")
    lines.append("")
    lines.append("| Next turn's trigger | Turns | Agent did it | Unknown |")
    lines.append("|---|---|---|---|")
    for k, d in r["diagnostics_next_trigger"].items():
        lines.append(f"| {k} | {d['turns']} | {d['met']} | {d['unknown']} |")
    lines.append("")
    lines.append("## Reply-language scorer accuracy (generated dev split and reply-like bank text only)")
    lines.append("")
    lines.append("| Set | n | Accuracy | ES | PT | Unknown | Wrong |")
    lines.append("|---|---|---|---|---|---|---|")
    for name, a in r["language_scorer_validation"].items():
        lines.append(f"| {name} | {a['n']} | {_pct(a['accuracy'])} | {_pct(a['es'])} | {_pct(a['pt'])} | "
                     f"{a['unknown']} | {a['wrong']} |")
    lines.append("")
    lines.append("The `_mixed` sets are portunhol messages labeled with the generator's language, which is not always "
                 "the dominant one. Before detection, case subcategory labels, merchant names and product types from "
                 "tool results are removed from the reply.")
    lines.append("")
    rows = r["scenarios"]
    if len(rows) > 40:
        rows = [s for s in rows if not s["success"] or s["must_not_violated"] or s["reply_flags"]]
    lines.append("## Scenarios" if len(r["scenarios"]) <= 40 else "## Scenarios with a failure or a flag")
    lines.append("")
    lines.append("| Scenario | Category / subtype | Lang | Expected | Reached | Success | Failed checks | "
                 "must_not violated | Reply flags | Off script |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for s in rows:
        lines.append(f"| {s['scenario_id']} | {s['category']} / {s['subtype']} | {s['language']} | "
                     f"{s['expected_outcome']} | {s['reached']} | {'yes' if s['success'] else 'no'} | "
                     f"{', '.join(s['failed'])} | "
                     f"{', '.join(s['must_not_violated'])} | {', '.join(s['reply_flags'])} | "
                     f"{'yes' if s['off_script'] else ''} |")
    lines.append("")
    return "\n".join(lines)


def write_results(results, stem):
    os.makedirs(os.path.dirname(os.path.abspath(stem)), exist_ok=True)
    text = json.dumps(results, ensure_ascii=False, indent=1, sort_keys=False)
    if "CLI-" in text.upper():
        raise RuntimeError("a customer id reached the results file")
    with open(stem + ".json", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")
    with open(stem + ".md", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(to_markdown(results))
    return stem + ".json", stem + ".md"
