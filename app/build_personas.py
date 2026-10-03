"""Build the demo personas for the app's secure sign-in form.

Gold stores only a hash of each customer's document, so the demo needs the document numbers of a few snapshot
customers to sign in for real (document + one-time code). This script reads them from Bronze for a fixed list of
customers, checks that each one hashes to the same `document_hash` as in the local Gold snapshot, and writes
`data/bank_tools/demo_personas.json` (git-ignored). The data is synthetic. The file is not committed, but the app
serves its contents (names and document numbers) to every browser while APP_DEMO_CONTROLS is on, so the test
customers can sign in.

    python -m app.build_personas --warehouse-id <id> [--profile factored]
"""
import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from src.gold.gold_lib import document_hash

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data/bank_tools/snapshot_panel.sqlite"
OUT = ROOT / "data/bank_tools/demo_personas.json"

# Chosen from the 60-customer snapshot to cover the demo script: one normal path per country, an amount above the
# policy's handoff threshold, and a restricted (Suspended) customer.
PERSONAS = [
    {"customer_id": "CLI-YL1PRBOG38VV", "story": {"es": "Cliente regular, montos bajos", "pt": "Cliente comum, valores baixos"}},
    {"customer_id": "CLI-22MY069LYG6U", "story": {"es": "Muchos movimientos y un pago rechazado", "pt": "Muitos movimentos e um pagamento recusado"}},
    {"customer_id": "CLI-Q8XWLZTRQRNK", "story": {"es": "Cliente regular", "pt": "Cliente comum"}},
    {"customer_id": "CLI-3GB2N2JT821H", "story": {"es": "Tiene un movimiento sobre el umbral de 7.000 USD", "pt": "Tem um movimento acima do limite de 7.000 USD"}},
    {"customer_id": "CLI-ZHNWIA94F12H", "story": {"es": "Cuenta suspendida: debe pasar a un humano", "pt": "Conta suspensa: deve ir para um humano"}},
]


def sql(statement, warehouse_id, profile):
    body = {"warehouse_id": warehouse_id, "statement": statement, "wait_timeout": "50s"}
    out = subprocess.run(["databricks", "api", "post", "/api/2.0/sql/statements", "--json", json.dumps(body),
                          "--profile", profile], capture_output=True, text=True, encoding="utf-8",
                         check=True).stdout
    res = json.loads(out)
    if res["status"]["state"] != "SUCCEEDED":
        sys.exit("query failed: " + json.dumps(res["status"]))
    cols = [c["name"] for c in res["manifest"]["schema"]["columns"]]
    return [dict(zip(cols, row)) for row in res["result"].get("data_array") or []]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--warehouse-id", required=True)
    ap.add_argument("--profile", default="factored")
    args = ap.parse_args()

    db = sqlite3.connect(SNAPSHOT)
    db.row_factory = sqlite3.Row
    ids = [p["customer_id"] for p in PERSONAS]
    marks = ",".join("?" * len(ids))
    gold = {r["customer_id"]: dict(r) for r in db.execute(
        "SELECT i.customer_id, i.document_type, i.document_hash, p.country_code, p.customer_status "
        "FROM customer_identity i JOIN customer_profile p USING (customer_id) WHERE i.customer_id IN (" + marks + ")",
        ids)}
    missing = set(ids) - set(gold)
    if missing:
        sys.exit("not in the snapshot: " + ", ".join(sorted(missing)))

    in_list = ",".join("'" + i + "'" for i in ids)
    bronze = {r["customer_id"]: r for r in sql(
        "SELECT customer_id, document_number, first_name FROM workspace.bronze.customers "
        "WHERE customer_id IN (" + in_list + ")", args.warehouse_id, args.profile)}

    personas = []
    for n, p in enumerate(PERSONAS, start=1):
        g, b = gold[p["customer_id"]], bronze[p["customer_id"]]
        if document_hash(g["document_type"], b["document_number"]) != g["document_hash"]:
            sys.exit("document hash mismatch for " + p["customer_id"])
        personas.append({
            "persona_id": "P" + str(n),  # unrelated to the customer id, whose last 4 characters the console shows
            "first_name": b["first_name"],
            "country_code": g["country_code"],
            "customer_status": g["customer_status"],
            "document_type": g["document_type"],
            "document_number": b["document_number"],
            "story": p["story"],
        })
    OUT.write_text(json.dumps({"synthetic": True, "personas": personas}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print("wrote", OUT.relative_to(ROOT), "with", len(personas), "personas (all document hashes verified)")


if __name__ == "__main__":
    main()
