"""End-to-end evaluation of the dispute-intake agent on the e2e scenarios.

    python -m src.agent_eval.run                                   # dev split, databricks-gpt-oss-120b, 3 workers
    python -m src.agent_eval.run --only e2e-es-0001,no_match --limit 4 --workers 2
    python -m src.agent_eval.run --rescore                         # re-score saved transcripts, no model call
    python -m src.agent_eval.run --fresh                           # set the old transcripts aside, run everything
    python -m src.agent_eval.run --oracle --split all              # scorer validation with replay.py's oracle
    python -m src.agent_eval.run --split test --final              # the one final run on the generated test split

Each scenario gets its own bank service and its own Agent (app/agent.py, unchanged) with DatabricksChat on the
endpoint under test. The workspace host comes from DATABRICKS_HOST or the profile in ~/.databrickscfg (no API call);
the token from `databricks auth token`, kept until the expiry it reports and refreshed once after an HTTP 401 or
403; only model-serving calls reach Databricks. Workers run scenarios in parallel threads; a call rate-limited with
HTTP 429 waits and retries (harness.PatientChat). The run stops after --max-consecutive-failures scenarios in a row
fail for the endpoint (infra_error or rate_limited).

Transcripts (full replies, tool events and messages; synthetic data) go to the git-ignored
data/agent_eval/<split>_<endpoint>/transcripts.jsonl, one line per scenario, appended as each one finishes. Every
transcript carries the fingerprint of what produced it (harness.fingerprint: agent, LLM client, prompt, tool schemas,
policy, snapshot, endpoint, classifier). A new run skips only the scenarios already there with status ok and the
current fingerprint, so a crashed or rate-limited run resumes where it stopped and a changed prompt is re-run; results
refuse to mix fingerprints unless --allow-mixed. Results are always computed from that file, so --rescore writes the
same bytes: eval/results/agent_e2e_<split>_<endpoint>.json and .md. Oracle results describe every scenario's expected
outcome, so they stay next to their transcripts in data/agent_eval/<split>_oracle/.

The generated test split is reserved for one final run after the prompt is fixed: --split test (or all) is refused
without --final, except in --oracle mode, which uses no model and writes only to git-ignored data/. The hand-written
final test sets are never read. Exit code 1 when a scenario could not be run, when the run stopped early, with
--final when any selected scenario is not scored, or, with --oracle, when any scenario fails a check; 2 for a refused
selection or mixed fingerprints.
"""
import argparse
import configparser
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.bank_tools.replay import DEFAULT_SCENARIOS
from src.bank_tools.schemas import ToolSchemas
from src.policy import dispute_policy as dp

from . import harness, report
from .score import SnapshotLookup, score_scenario

REPO = report.REPO
DEFAULT_ENDPOINT = "databricks-gpt-oss-120b"
DEFAULT_PROFILE = "factored"
TRANSCRIPTS_DIR = os.path.join(REPO, "data", "agent_eval")
RESULTS_DIR = os.path.join(REPO, "eval", "results")
SPLITS = ("dev", "test", "all")
ENDPOINT_FAILURES = ("infra_error", "rate_limited")


def slug(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def paths(split, endpoint, oracle=False):
    """(transcripts file, results stem). Oracle results stay in the git-ignored transcripts folder."""
    name = f"{split}_{slug(endpoint)}"
    folder = os.path.join(TRANSCRIPTS_DIR, name)
    return (os.path.join(folder, "transcripts.jsonl"),
            os.path.join(folder if oracle else RESULTS_DIR, f"agent_e2e_{name}"))


def load_scenarios(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def select(scenarios, split, only=None, limit=None):
    keys = {k.strip() for k in (only or "").split(",") if k.strip()}
    out = [sc for sc in scenarios if split == "all" or sc.get("split") == split]
    if keys:
        out = [sc for sc in out if keys & {sc["scenario_id"], sc["category"], sc["subtype"]}]
    return out[:limit] if limit else out


def resolve_host(profile):
    """DATABRICKS_HOST, else the profile's host in the Databricks config file; read locally, no API call."""
    host = os.environ.get("DATABRICKS_HOST")
    if host:
        return host
    path = os.environ.get("DATABRICKS_CONFIG_FILE") or os.path.join(os.path.expanduser("~"), ".databrickscfg")
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")
    section = profile if cfg.has_section(profile) else ("DEFAULT" if profile == "DEFAULT" else None)
    host = cfg.get(section, "host", fallback=None) if section else None
    if not host:
        raise SystemExit(f"no Databricks host: set DATABRICKS_HOST or add host to [{profile}] in {path}")
    return host


def set_aside(path):
    """Rename an existing transcripts file to transcripts.<time>.jsonl (for --fresh); returns the new name."""
    if not os.path.exists(path):
        return None
    new = path[:-len(".jsonl")] + time.strftime(".%Y%m%d-%H%M%S") + ".jsonl"
    os.replace(path, new)
    return new


def _line(i, n, tr, verdict):
    p = verdict["process"] if verdict else {}
    ok = "" if verdict is None else ("ok  " if verdict["success"] else "FAIL")
    tokens = (p.get("prompt_tokens", 0) + p.get("completion_tokens", 0)) if p else 0
    failed = ",".join(verdict["failed"]) if verdict and verdict["failed"] else ""
    flags = ",".join(sorted(k for k, x in verdict["must_not"].items() if x.get("violated"))) if verdict else ""
    return (f"[{i}/{n}] {tr['scenario_id']} {tr['category']}/{tr['subtype']} {tr['language']} status={tr['status']} "
            f"{ok} reached={verdict['reached']['outcome'] if verdict else '-'} turns={p.get('turns', '-')} "
            f"model_calls={p.get('model_calls', '-')} tokens={tokens} wall={tr.get('wall_s')}s"
            + (f" failed={failed}" if failed else "") + (f" must_not={flags}" if flags else "")
            + (f" error={tr['error']}" if tr.get("error") else ""))


def load_classifier():
    from src.classifier.runtime import load_default
    classifier = load_default()
    if classifier is None:
        print("warning: intent classifier not loaded; the trace will not carry its output")
    return classifier


def run_agent(todo, args, cfg, snapshot, writer, lookup, by_id, classifier, fp):
    """Run the scenarios in parallel; returns (scenarios not run to status ok, stopped early)."""
    host = resolve_host(args.profile)
    tokens = harness.CliTokenProvider(host.rstrip("/") if host.startswith("http") else "https://" + host.rstrip("/"),
                                      args.profile)
    schemas, pol = ToolSchemas(), dp.load_policy()

    def one(sc):
        llm = harness.PatientChat(endpoint=args.endpoint, host=host, profile=args.profile)
        llm.tokens = tokens
        tr = harness.run_agent_scenario(sc, snapshot, cfg, llm, classifier, schemas, pol, endpoint=args.endpoint,
                                        fingerprint=fp)
        writer.write(tr)
        return tr

    errors, in_a_row, stopped = 0, 0, False
    pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
    try:
        futures = {pool.submit(one, sc): sc for sc in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            tr = fut.result()
            verdict = None
            if tr["status"] == "ok":
                verdict = score_scenario(by_id[tr["scenario_id"]], tr["store"], tr["audit"], tr["turns"], lookup,
                                         tr.get("nonce"), tr.get("system_prompt"))
            else:
                errors += 1
            in_a_row = in_a_row + 1 if tr["status"] in ENDPOINT_FAILURES else 0
            print(_line(i, len(todo), tr, verdict), flush=True)
            if args.max_consecutive_failures and in_a_row >= args.max_consecutive_failures:
                print(f"stopping: {in_a_row} scenarios in a row failed for the endpoint; finished scenarios are "
                      "saved and the next run resumes", flush=True)
                stopped = True
                break
    except KeyboardInterrupt:  # finished scenarios are already saved; the next run resumes from them
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True, cancel_futures=True)
    return errors, stopped


def run_oracle(todo, cfg, snapshot, writer, fp):
    schemas, pol = ToolSchemas(), dp.load_policy()
    errors = 0
    for sc in todo:
        tr = harness.run_oracle_scenario(sc, snapshot, cfg, schemas, pol, fingerprint=fp)
        if tr["status"] != "ok":
            errors += 1
            print(f"{tr['scenario_id']}: {tr['error']}")
        writer.write(tr)
    return errors


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="End-to-end evaluation of the dispute-intake agent on the e2e scenarios.")
    ap.add_argument("--split", default="dev", choices=SPLITS)
    ap.add_argument("--only", default=None, help="comma-separated scenario ids, categories or subtypes")
    ap.add_argument("--limit", type=int, default=None, help="at most N scenarios (after --only, in file order)")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="model serving endpoint under test")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--final", action="store_true", help="allow the generated test split (the one final run)")
    ap.add_argument("--rescore", action="store_true", help="only re-score the saved transcripts")
    ap.add_argument("--fresh", action="store_true", help="set the existing transcripts file aside and run everything")
    ap.add_argument("--allow-mixed", action="store_true",
                    help="score transcripts produced with different fingerprints together")
    ap.add_argument("--max-consecutive-failures", type=int, default=5,
                    help="stop after N scenarios in a row fail for the endpoint (0: never)")
    ap.add_argument("--oracle", action="store_true", help="run replay.py's scripted oracle instead of the agent")
    ap.add_argument("--profile", default=os.environ.get("DATABRICKS_CONFIG_PROFILE", DEFAULT_PROFILE))
    ap.add_argument("--scenarios", default=DEFAULT_SCENARIOS)
    ap.add_argument("--snapshot", default=None, help="SQLite snapshot (default BANK_TOOLS_SNAPSHOT)")
    args = ap.parse_args(argv)

    if args.split != "dev" and not args.final and not args.oracle:
        ap.error("--split test or all is reserved for the final run: add --final (or use --oracle)")
    if args.fresh and args.rescore:
        ap.error("--fresh runs the scenarios again; it cannot go with --rescore")
    endpoint = "oracle" if args.oracle else args.endpoint
    transcripts_path, stem = paths(args.split, endpoint, oracle=args.oracle)
    cfg = harness.eval_config()
    snapshot = args.snapshot or cfg.path(cfg.snapshot)
    scenarios = load_scenarios(args.scenarios)
    by_id = {sc["scenario_id"]: sc for sc in scenarios}
    chosen = select(scenarios, args.split, args.only, args.limit)
    if not chosen:
        ap.error("no scenario matches the selection")
    lookup = SnapshotLookup(os.path.abspath(snapshot))
    errors, stopped = 0, False
    started = time.monotonic()
    if not args.rescore:
        if args.oracle:
            fp = harness.fingerprint("oracle", snapshot, cfg)
            writer = harness.TranscriptWriter(transcripts_path, truncate=True)
            errors = run_oracle(chosen, cfg, snapshot, writer, fp)
        else:
            if args.fresh:
                old = set_aside(transcripts_path)
                if old:
                    print(f"previous transcripts set aside as {os.path.relpath(old, REPO)}")
            classifier = load_classifier()
            fp = harness.fingerprint(args.endpoint, snapshot, cfg, classifier)
            done = harness.read_transcripts(transcripts_path)
            todo = [sc for sc in chosen if not ((done.get(sc["scenario_id"]) or {}).get("status") == "ok"
                                                and harness.fingerprint_id(done[sc["scenario_id"]]) == fp["id"])]
            stale = sum(1 for sc in chosen if sc["scenario_id"] in done
                        and harness.fingerprint_id(done[sc["scenario_id"]]) != fp["id"])
            print(f"{len(chosen)} scenarios selected, {len(chosen) - len(todo)} already done, {len(todo)} to run "
                  f"({stale} with another fingerprint; endpoint {args.endpoint}, {args.workers} workers, "
                  f"fingerprint {fp['id']})", flush=True)
            if todo:
                writer = harness.TranscriptWriter(transcripts_path)
                errors, stopped = run_agent(todo, args, cfg, snapshot, writer, lookup, by_id, classifier, fp)
    transcripts = harness.read_transcripts(transcripts_path)
    ids = [sc["scenario_id"] for sc in chosen]
    fps = report.fingerprints(transcripts, ids)
    if len(fps) > 1 and not args.allow_mixed:
        lookup.close()
        print("refusing to score transcripts made with different fingerprints: "
              + ", ".join(f"{f['id']} ({f['n']})" for f in fps)
              + ". Re-run the stale ones (a normal run does), or pass --allow-mixed.")
        return 2
    mode = "oracle" if args.oracle else "agent"
    from app.agent import SYSTEM_PROMPT
    results = report.build_results(by_id, transcripts, lookup, args.split, endpoint, mode, args.scenarios,
                                   transcripts_path, selected_ids=ids, fallback_prompt=SYSTEM_PROMPT)
    lookup.close()
    json_path, md_path = report.write_results(results, stem)
    c = results["counts"]
    print(f"\n{c['scored']} of {c['selected']} selected scenarios scored ({len(c['missing'])} without a transcript, "
          f"{sum(len(v) for v in c['skipped'].values())} skipped for endpoint failures, {c['harness_errors']} harness "
          f"errors counted as failures) in {time.monotonic() - started:.0f} s -> {os.path.relpath(json_path, REPO)}, "
          f"{os.path.relpath(md_path, REPO)}")
    print_summary(results)
    if args.oracle:
        failed = [s for s in results["scenarios"] if not s["success"] or s["must_not_violated"]]
        return 1 if (failed or errors or not c["complete"]) else 0
    if args.final and not c["complete"]:
        print("the final run is incomplete: re-run until every selected scenario is scored")
        return 1
    return 1 if (errors or stopped) else 0


def print_summary(r):
    o = r["overall"]
    print(f"success {report._rate_cell(o['success'])} {report._ci_text(o['success']['ci95'])}; outcome reached "
          f"{report._rate_cell(o['outcome_reached'])}; language ok {report._rate_cell(o['language_ok'])}")
    if not r["counts"]["complete"]:
        cs = r["conservative_success"]
        print(f"INCOMPLETE: success counting unscored scenarios as failures {report._pct(cs['rate'])} "
              f"({cs['k']}/{cs['n']})")
    for t in r["targets"]:
        print(f"  {t['label']}: {report.target_value_text(t)} {report.target_ci_text(t)} target {t['target']} -> "
              f"{t['verdict']}" + (" (incomplete)" if t["incomplete"] else ""))
    print("by category: " + ", ".join(f"{c} {m['success']['k']}/{m['success']['n']}"
                                      for c, m in r["by_category"].items()))
    print("by language: " + ", ".join(f"{c} {m['success']['k']}/{m['success']['n']}"
                                      for c, m in r["by_language"].items()))
    mn = {k: x for k, x in r["must_not_tool_level"].items() if x["violated_listed"] or x["violated_other"]}
    print("tool-level must_not violations: "
          + (", ".join(f"{k} {x['violated_listed']}+{x['violated_other']}" for k, x in mn.items()) or "none"))
    rf = {k: x for k, x in r["reply_checks_heuristic"].items()
          if k != "reply_grounding" and (x["flagged_listed"] or x["flagged_other"])}
    print("reply-level flags (heuristic): "
          + (", ".join(f"{k} {x['flagged_listed']}+{x['flagged_other']}" for k, x in rf.items()) or "none"))
    g = r["reply_checks_heuristic"]["reply_grounding"]
    print(f"reply grounding flags: {g['flagged']} of {g['checked']} scenarios")
    failed = [s for s in r["scenarios"] if not s["success"]]
    for s in failed[:40]:
        detail = "; ".join(f"{k} {v}".strip() for k, v in s["failed_detail"].items())
        print(f"  FAIL {s['scenario_id']} [{s['category']}/{s['subtype']} {s['language']}] expected "
              f"{s['expected_outcome']} reached {s['reached']}: {detail}")


if __name__ == "__main__":
    sys.exit(main())
