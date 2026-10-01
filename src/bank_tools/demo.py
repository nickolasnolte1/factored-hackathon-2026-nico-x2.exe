"""Scripted happy path over the local snapshot: prints every tool call and the envelope the model would receive.

    python -m src.bank_tools.demo                         # first panel customer with a disputable recent purchase
    python -m src.bank_tools.demo --customer CLI-XXXXXXXXXXXX [--now 2026-06-19T09:00:00] [--language pt]

No model is involved. It runs with BANK_TOOLS_ENV=eval semantics: the session comes from the trusted test session
issuer (the snapshot holds document hashes only), and the OTP flow is shown with a document that matches no
customer (a decoy challenge). Tool outputs carry no personal data; the customer id itself is never printed.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import timedelta

from src.policy import dispute_policy as dp

from . import BankService, Config, FixedClock, ListAuditSink, LocalRepository, ToolContext
from .clock import parse_dt
from .redaction import redact_args


SCRIPT = {
    "es": {"open": "Hola, me aparece un cargo que no reconozco.",
           "details": "Fue un cargo de unos {amount} el {date}.", "confirm": "Sí, confirmo, es ese movimiento."},
    "pt": {"open": "Olá, aparece uma cobrança que eu não reconheço.",
           "details": "Foi uma cobrança de uns {amount} em {date}.", "confirm": "Sim, confirmo, é essa movimentação."},
}


class Demo:
    def __init__(self, service, conversation_id, width):
        self.svc = service
        self.conv = conversation_id
        self.turn = 1
        self.token = None
        self.width = width
        self.calls = 0

    def say(self, who, text):
        print(f"\n[{who}] {text}")

    def call(self, name, caller="model", **args):
        self.calls += 1
        ctx = ToolContext(self.conv, self.turn, f"{self.turn:016x}{self.calls:016x}", caller)
        result = self.svc.call_tool(name, args, self.token, ctx)
        shown = json.dumps(redact_args(args), ensure_ascii=False)
        print(f"\n-> {name}({shown})  turn={self.turn} caller={caller}")
        body = json.dumps(result.for_model(), ensure_ascii=False, indent=1)
        lines = body.splitlines()
        if self.width and len(lines) > self.width:
            lines = lines[:self.width] + [f"   ... ({len(lines) - self.width} more lines)"]
        print("<- " + "\n   ".join(lines))
        return result


def pick_customer(snapshot, now, pol):
    """First Active customer with an eligible, in-window purchase below the handoff threshold."""
    db = sqlite3.connect("file:" + os.path.abspath(snapshot).replace("\\", "/") + "?mode=ro", uri=True)
    oldest = (parse_dt(now) - timedelta(days=pol["dispute_window"]["days_since_transaction"])).date().isoformat()
    row = db.execute(
        "SELECT t.customer_id FROM customer_transactions t JOIN customer_profile p USING (customer_id)"
        " WHERE p.customer_status = 'Active' AND t.transaction_status = 'Approved' AND t.transaction_type = 'Purchase'"
        " AND t.above_handoff_threshold = 0 AND t.event_date >= :oldest AND t.event_ts <= :now"
        " ORDER BY t.customer_id LIMIT 1", {"oldest": oldest, "now": now}).fetchone()
    db.close()
    return row[0] if row else None


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Scripted bank tools demo (local snapshot, no model).")
    ap.add_argument("--customer", help="CLI- id from the snapshot (never printed)")
    ap.add_argument("--snapshot", default=None, help="SQLite snapshot (default BANK_TOOLS_SNAPSHOT)")
    ap.add_argument("--now", default="2026-06-19T09:00:00", help="service clock (dataset local time)")
    ap.add_argument("--language", choices=("es", "pt"), default="es")
    ap.add_argument("--lines", type=int, default=40, help="max lines printed per result (0 = all)")
    args = ap.parse_args(argv)

    cfg = Config.from_env(env="eval")
    snapshot = args.snapshot or cfg.path(cfg.snapshot)
    pol = dp.load_policy()
    customer = args.customer or pick_customer(snapshot, args.now, pol)
    if not customer:
        raise SystemExit("no suitable customer in the snapshot; pass --customer")
    audit = ListAuditSink()
    svc = BankService(LocalRepository(snapshot), FixedClock(args.now), audit, None, cfg)
    demo = Demo(svc, "demo-conversation", args.lines)
    lang = args.language

    say = SCRIPT[lang]
    demo.say("customer", say["open"])
    demo.call("get_policy_info", topic="scope", language=lang)
    demo.call("get_customer_overview")  # no session yet: AUTH_REQUIRED
    demo.say("app", "secure form: identity verification (document + one-time code)")
    start = demo.call("start_authentication", caller="runtime", document_type="DNI", document_number="00000000")
    demo.call("verify_otp", caller="runtime", challenge_id=start.data["challenge_id"], code="000000")
    demo.say("app", "decoy document: same response, nothing delivered, no code can verify it."
             " Using the trusted test session issuer for the demo customer.")
    demo.token = svc.identity.issue_test_session(customer, authenticated_at=args.now, conversation_id=demo.conv)

    overview = demo.call("get_customer_overview")
    if overview.data["service_restriction"]["handoff_required"]:
        demo.call("handoff_to_human", reason_code="customer_status_restricted", language=lang, package={
            "request_summary": "Restricted customer reports an unrecognized charge.", "verified_facts": [],
            "actions_taken": ["get_customer_overview"], "evidence": [overview.meta["tool_call_id"]],
            "open_questions": []})
        return
    products = demo.call("list_products")
    if products.data["products"]:
        demo.call("get_balance", product_id=products.data["products"][0]["product_id"])
    demo.call("list_recent_transactions", limit=3)

    # The customer describes the movement; the script takes amount and date from the bank record, as a customer would.
    rows = svc.repository.list_transactions(customer, args.now)
    cands = dp.candidate_transactions(rows, customer, args.now, "dispute_unrecognized_charge", pol)
    usable = [t for t in cands if not t.get("above_handoff_threshold")
              and dp.in_dispute_window(t["event_date"], args.now, pol)]
    target = next((t for t in usable if t["transaction_type"] == "Purchase"), usable[0] if usable else None)
    if target is None:
        raise SystemExit("the customer has no disputable movement at this clock; try another --customer or --now")
    hints = {"amount": round(float(target["amount"])), "date": target["event_date"]}
    if target.get("merchant_name"):
        hints["merchant"] = target["merchant_name"].split()[0]
    demo.say("customer", say["details"].format(**hints))
    found = demo.call("find_candidate_transactions", purpose="dispute", intent="dispute_unrecognized_charge", hints=hints)
    if found.data["match_status"] != "unique":
        demo.say("agent", "more than one movement matches: the customer would pick one; the demo takes the first")
    tid = found.data["candidates"][0]["transaction_id"]
    prepared = demo.call("prepare_dispute_case", transaction_id=tid, intent="dispute_unrecognized_charge", language=lang)
    if not prepared.ok:
        return
    cnf = prepared.data["confirmation_id"]
    demo.say("agent", "shows verified_facts and asks the customer to confirm")
    demo.call("create_dispute_case", confirmation_id=cnf, transaction_id=tid, customer_confirmed=True,
              idempotency_key="demo-case-0001")  # same turn: refused (CONFIRMATION_REQUIRED, same_turn)

    demo.turn = 2
    svc.clock.advance(60)
    demo.say("customer", say["confirm"])
    if prepared.data["policy_decision"]["handoff_required"]:
        demo.call("handoff_to_human", reason_code=prepared.data["policy_decision"]["handoff_reason"], language=lang,
                  confirmation_id=cnf, package={
                      "request_summary": "Customer disputes a movement they do not recognize.",
                      "verified_facts": ["transaction " + tid], "actions_taken": ["prepare_dispute_case"],
                      "evidence": [found.meta["tool_call_id"], prepared.meta["tool_call_id"]], "open_questions": []})
    else:
        created = demo.call("create_dispute_case", confirmation_id=cnf, transaction_id=tid, customer_confirmed=True,
                            idempotency_key="demo-case-0002")
        if created.ok:
            demo.call("get_case_status", case_id=created.data["case_id"])
    tools = {}
    for rec in audit.records:
        tools[rec["tool"]] = tools.get(rec["tool"], 0) + 1
    print(f"\naudit: {len(audit.records)} records (one per call, redacted): {json.dumps(tools)}")


if __name__ == "__main__":
    main()
