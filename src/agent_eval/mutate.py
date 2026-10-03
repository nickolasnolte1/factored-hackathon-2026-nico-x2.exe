"""Mutation check of the scorer: break the scripted oracle's transcripts on purpose and count how many the scorer
catches (a broken transcript must not score as a success).

    python -m src.agent_eval.run --oracle --split all     # writes data/agent_eval/all_oracle/transcripts.jsonl
    python -m src.agent_eval.mutate                       # mutates those transcripts and scores each copy

Each mutation applies to the scenarios whose expected outcome it concerns; "caught" means the mutated copy is not a
success. Everything stays in memory: no file is written. Exit code 1 when a mutation is not caught everywhere.
"""
import argparse
import copy
import json
import os
import sys

from src.bank_tools.replay import DEFAULT_SCENARIOS

from .harness import read_transcripts
from .score import CREATE_OUTCOMES, HANDOFF_OUTCOMES, SnapshotLookup, earliest_write_turn, score_scenario

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_TRANSCRIPTS = os.path.join(REPO, "data", "agent_eval", "all_oracle", "transcripts.jsonl")
DEFAULT_SNAPSHOT = os.path.join(REPO, "data", "bank_tools", "snapshot_panel.sqlite")
FOREIGN_TRANSACTION = "TRX-" + "Q" * 20


def _creates(sc):
    return sc["expected"]["outcome"] in CREATE_OUTCOMES


def _handoffs(sc):
    return sc["expected"]["outcome"] in HANDOFF_OUTCOMES


def _writes(sc):
    return _creates(sc) or _handoffs(sc)


def _draft_expected(sc):
    return (sc["expected"].get("case_fields") or {}).get("status") == "pending_human_review"


def _drop_rows(tr, sc):
    tr["store"] = {"dispute_cases": [], "handoff_tickets": []}


def _wrong_reason(tr, sc):
    for r in tr["store"]["handoff_tickets"]:
        r["reason_code"] = "explicit_human_request" if r["reason_code"] != "explicit_human_request" else "tool_failure"


def _write_in_turn_1(tr, sc):
    for r in tr["audit"]:
        if r["tool"] in ("create_dispute_case", "handoff_to_human") and r["outcome"] == "ok":
            r["turn_index"] = 1


def _amount_plus_10(tr, sc):
    for r in tr["store"]["dispute_cases"]:
        r["amount"] = round(r["amount"] * 1.1, 2)


def _other_transaction(tr, sc):
    for r in tr["store"]["dispute_cases"]:
        r["transaction_id"] = FOREIGN_TRANSACTION


def _runtime_ticket(tr, sc):
    for r in tr["audit"]:
        if r["tool"] == "handoff_to_human":
            r["caller"] = "runtime"


def _inconsistent_reason_check(tr, sc):
    for r in tr["store"]["handoff_tickets"]:
        r["reason_check"] = "inconsistent" if r.get("reason_check") != "inconsistent" else "consistent"


def _second_ticket(tr, sc):
    first = (tr["store"]["handoff_tickets"] or [None])[0]
    if first is None:
        return
    extra = dict(first, ticket_id="HND-ZZZZZZZZZZZZ", reason_code="explicit_human_request")
    tr["store"]["handoff_tickets"].append(extra)
    tr["audit"].append({"tool": "handoff_to_human", "outcome": "ok", "caller": "model", "turn_index": 1,
                        "result_summary": {"ticket_id": "HND-ZZZZZZZZZZZZ", "verified": True},
                        "security_events": [], "ts": sc["now"]})


def _no_draft(tr, sc):
    for r in tr["store"]["handoff_tickets"]:
        v = json.loads(r["service_verified_json"])
        v["draft"] = None
        r["service_verified_json"] = json.dumps(v)


def _extra_ticket(tr, sc):
    tr["store"]["handoff_tickets"].append({
        "ticket_id": "HND-ZZZZZZZZZZZZ", "reason_code": "explicit_human_request", "queue": "disputes",
        "customer_id": sc["customer_id"], "service_verified_json": "{}", "identity_verified": True})
    tr["audit"].append({"tool": "handoff_to_human", "outcome": "ok", "caller": "model", "turn_index": 1,
                        "result_summary": {"ticket_id": "HND-ZZZZZZZZZZZZ", "verified": True},
                        "security_events": [], "ts": sc["now"]})


def _foreign_probe(tr, sc):
    tr["audit"].append({"tool": "get_balance", "outcome": "error", "error_code": "NOT_FOUND", "caller": "model",
                        "turn_index": 1, "result_summary": {}, "security_events": ["foreign_resource_probe"],
                        "ts": sc["now"]})


def _drop_answer_call(tr, sc):
    for t in tr["turns"]:
        t["events"] = [ev for ev in t["events"]
                       if ev["tool"] not in ("get_balance", "list_recent_transactions", "explain_decline")]


MUTATIONS = (  # (name, function, applies to)
    ("drop all store rows", _drop_rows, _writes),
    ("wrong ticket reason", _wrong_reason, _handoffs),
    ("write moved to turn 1", _write_in_turn_1, lambda sc: _writes(sc) and earliest_write_turn(sc) > 1),
    ("case amount +10%", _amount_plus_10, _creates),
    ("case on another transaction", _other_transaction, _creates),
    ("ticket written by the runtime fallback", _runtime_ticket, _handoffs),
    ("service reason check flipped", _inconsistent_reason_check, _handoffs),
    ("second ticket with another reason", _second_ticket, _handoffs),
    ("draft removed", _no_draft, _draft_expected),
    ("extra verified ticket", _extra_ticket, lambda sc: not _writes(sc)),
    ("foreign-resource probe added", _foreign_probe, lambda sc: True),
    ("answer call removed", _drop_answer_call, lambda sc: sc["expected"]["outcome"] == "answer"),
)


def run(scenarios, transcripts, lookup):
    """[{name, scenarios, caught, missed: [ids]}] over the transcripts with status ok."""
    out = []
    for name, fn, applies in MUTATIONS:
        hit, caught, missed = 0, 0, []
        for sid, tr in sorted(transcripts.items()):
            sc = scenarios.get(sid)
            if sc is None or tr.get("status") != "ok" or not applies(sc):
                continue
            t = copy.deepcopy(tr)
            fn(t, sc)
            v = score_scenario(sc, t["store"], t["audit"], t["turns"], lookup)
            hit += 1
            if v["success"]:
                missed.append(sid)
            else:
                caught += 1
        out.append({"name": name, "scenarios": hit, "caught": caught, "missed": missed})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Mutation check of the scorer on the oracle transcripts.")
    ap.add_argument("--transcripts", default=DEFAULT_TRANSCRIPTS)
    ap.add_argument("--scenarios", default=DEFAULT_SCENARIOS)
    ap.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    args = ap.parse_args(argv)
    with open(args.scenarios, encoding="utf-8") as fh:
        scenarios = {sc["scenario_id"]: sc for sc in (json.loads(line) for line in fh if line.strip())}
    transcripts = read_transcripts(args.transcripts)
    if not transcripts:
        print(f"no transcripts at {args.transcripts}: run python -m src.agent_eval.run --oracle first")
        return 1
    lookup = SnapshotLookup(os.path.abspath(args.snapshot))
    rows = run(scenarios, transcripts, lookup)
    lookup.close()
    print(f"{'mutation':40} {'scenarios':>9} {'caught':>7}")
    for r in rows:
        print(f"{r['name']:40} {r['scenarios']:>9} {r['caught']:>7}"
              + (f"  missed: {','.join(r['missed'][:5])}" if r["missed"] else ""))
    return 1 if any(r["missed"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
