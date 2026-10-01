"""Build the SQLite snapshot of Gold that LocalRepository reads (CONTRACT.md section 1.6).

    python -m src.bank_tools.snapshot --source gold --customers panel,sample:50 \\
        --warehouse-id $DATABRICKS_WAREHOUSE_ID --profile factored        # Gold, read-only SELECTs (default source)
    python -m src.bank_tools.snapshot --source panel                       # offline, from data/scenarios/panel.jsonl

--customers takes comma-separated specs: `panel` (the e2e panel customers), `sample:N` (N other customers picked by a
seeded hash, products_total > 0) and `ids:<file>` (one CLI- id per line). With `panel`, the Gold export must equal
panel.jsonl for those customers (ids, amounts, statuses, channel projection, products) or the command fails, so the
e2e expectations stay valid. Output goes to the git-ignored data/bank_tools/ with a manifest next to it. Only Gold
columns are exported: document hashes, never document numbers.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

from src.policy import dispute_policy as dp

from .config import REPO_ROOT
from .repository import sql
from .repository.base import COLUMN_TYPES, GOLD_SPEC, normalize_row

DEFAULT_OUT = os.path.join(REPO_ROOT, "data", "bank_tools", "snapshot_panel.sqlite")
PANEL_PATH = os.path.join(REPO_ROOT, "data", "scenarios", "panel.jsonl")
REFERENCE_DECLINES = os.path.join(REPO_ROOT, "data", "bank_tools", "reference", "decline_codes.json")
CUSTOMER_ID = re.compile(r"^CLI-[A-Z0-9]{12}$")
CUSTOMER_TABLES = ("customer_identity", "customer_profile", "customer_products", "customer_transactions")
SQLITE_TYPES = {"string": "TEXT", "boolean": "INTEGER", "bigint": "INTEGER", "int": "INTEGER", "double": "REAL",
                "date": "TEXT", "timestamp": "TEXT"}
INDEXES = (
    "CREATE UNIQUE INDEX ix_identity_doc ON customer_identity (document_type, document_hash)",
    "CREATE UNIQUE INDEX ix_profile_pk ON customer_profile (customer_id)",
    "CREATE UNIQUE INDEX ix_products_pk ON customer_products (product_id)",
    "CREATE INDEX ix_products_customer ON customer_products (customer_id)",
    "CREATE UNIQUE INDEX ix_transactions_pk ON customer_transactions (transaction_id)",
    "CREATE INDEX ix_transactions_customer ON customer_transactions (customer_id, event_ts)",
    "CREATE UNIQUE INDEX ix_declines_pk ON decline_codes (code_key)",
)
BATCH = 250


def _sqlite_type(kind):
    if kind.startswith("decimal"):
        return "REAL"
    if kind.startswith("array"):
        return "TEXT"
    return SQLITE_TYPES[kind]


def _cell(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def table_digest(rows, key):
    h = hashlib.sha256()
    for row in sorted(rows, key=lambda r: tuple(str(r.get(k)) for k in key)):
        h.update(json.dumps(row, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8"))
    return h.hexdigest()


def write_sqlite(path, tables, meta):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    for table, rows in tables.items():
        columns = sql.TABLE_COLUMNS[table]
        types = COLUMN_TYPES[table]
        db.execute("CREATE TABLE " + table + " (" + ", ".join(c + " " + _sqlite_type(types[c]) for c in columns) + ")")
        db.executemany("INSERT INTO " + table + " (" + ", ".join(columns) + ") VALUES ("
                       + ", ".join("?" for _ in columns) + ")",
                       [tuple(_cell(r.get(c)) for c in columns) for r in rows])
    for statement in INDEXES:
        if statement.split(" ON ")[1].split(" ")[0] in tables:
            db.execute(statement)
    db.execute("CREATE TABLE snapshot_meta (key TEXT PRIMARY KEY, value TEXT)")
    db.executemany("INSERT INTO snapshot_meta VALUES (?, ?)", [(k, str(v)) for k, v in meta.items()])
    db.commit()
    db.close()
    os.replace(tmp, path)


def load_panel(path=PANEL_PATH):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# -------------------------------------------------------------------------------------------------------------
# Gold source


def resolve_customers(specs, client, render, panel, seed):
    ids, panel_ids = [], [c["customer_id"] for c in panel]
    for spec in [s.strip() for s in specs.split(",") if s.strip()]:
        if spec == "panel":
            ids += panel_ids
        elif spec.startswith("sample:"):
            n = int(spec.split(":", 1)[1])
            rows = client.execute(render(sql.SAMPLE_CUSTOMERS), {"seed": str(seed)}, deadline_s=600)
            taken = set(ids) | set(panel_ids)
            picked = [r["customer_id"] for r in rows if r["customer_id"] not in taken][:n]
            ids += picked
        elif spec.startswith("ids:"):
            with open(spec.split(":", 1)[1], encoding="utf-8") as fh:
                ids += [line.strip() for line in fh if line.strip()]
        else:
            raise SystemExit("unknown --customers spec: " + spec)
    bad = [i for i in ids if not CUSTOMER_ID.match(i)]
    if bad:
        raise SystemExit(f"{len(bad)} malformed customer ids")
    return list(dict.fromkeys(ids))


def export_gold(args):
    from .repository.databricks import CliCredentials, StatementClient
    creds = CliCredentials(profile=args.profile)
    client = StatementClient(args.warehouse_id, creds, attempt_deadline_s=600)
    catalog, gold = args.catalog, args.gold_schema

    def render(statement):
        return sql.render(statement, catalog, gold, "ops")

    panel = load_panel() if "panel" in args.customers else []
    ids = resolve_customers(args.customers, client, render, panel, args.seed)
    print(f"exporting {len(ids)} customers from {catalog}.{gold}", file=sys.stderr)
    tables = {t: [] for t in CUSTOMER_TABLES}
    for i in range(0, len(ids), BATCH):
        batch = ",".join(ids[i:i + BATCH])
        for table in CUSTOMER_TABLES:
            raw = client.execute(render(sql.EXPORT_BY_CUSTOMERS[table]), {"ids": batch}, deadline_s=600)
            tables[table] += [normalize_row(table, r) for r in raw]
    tables["decline_codes"] = [normalize_row("decline_codes", r)
                               for r in client.execute(render(sql.EXPORT_DECLINE_CODES), None, deadline_s=300)]
    props = {}
    for table in tables:
        rows = client.execute(render(sql.SHOW_PROPERTIES[table]), None, deadline_s=300)
        props[table] = {r["key"]: r["value"] for r in rows if str(r.get("key", "")).startswith("gold.")}
    if panel:
        problems = panel_parity(panel, tables)
        if problems:
            for p in problems[:20]:
                print("parity:", p, file=sys.stderr)
            raise SystemExit(f"Gold export differs from panel.jsonl ({len(problems)} differences); snapshot not written")
        print(f"parity with panel.jsonl: {len(panel)} customers, 0 differences", file=sys.stderr)
    os.makedirs(os.path.dirname(REFERENCE_DECLINES), exist_ok=True)
    with open(REFERENCE_DECLINES, "w", encoding="utf-8") as fh:
        json.dump(tables["decline_codes"], fh, ensure_ascii=False, indent=1, sort_keys=True)
    return tables, {"source": "gold", "catalog": catalog, "gold_schema": gold, "customers_spec": args.customers,
                    "seed": args.seed, "gold_table_properties": props, "panel_parity": bool(panel)}


def panel_parity(panel, tables):
    """Differences between the Gold export and panel.jsonl for the panel customers (empty when equal)."""
    problems = []
    gold_tx = {t["transaction_id"]: t for t in tables["customer_transactions"]}
    gold_pr = {p["product_id"]: p for p in tables["customer_products"]}
    for c in panel:
        cid = c["customer_id"]
        want_tx = {t["transaction_id"] for t in c["transactions"]}
        have_tx = {t for t, row in gold_tx.items() if row["customer_id"] == cid}
        if want_tx != have_tx:
            problems.append(f"{cid}: transaction ids differ ({len(want_tx ^ have_tx)})")
            continue
        for t in c["transactions"]:
            g = gold_tx[t["transaction_id"]]
            expected_channel = None if t.get("implausible_type_channel") else t.get("channel")
            checks = {
                "amount": round(float(t["amount"]), 2) == g["amount"],
                "currency": t["currency"] == g["currency"],
                "status": t["transaction_status"] == g["transaction_status"],
                "type": t["transaction_type"] == g["transaction_type"],
                "event_ts": str(t["event_ts"])[:19] == g["event_ts"],
                "event_date": str(t["event_date"])[:10] == g["event_date"],
                "channel": expected_channel == g["channel"],
                "implausible": bool(t.get("implausible_type_channel")) == bool(g["implausible_type_channel"]),
                "response_code": t.get("response_code") == g["response_code"],
                "merchant": t.get("merchant_name") == g["merchant_name"],
                "product": t["product_id"] == g["product_id"],
            }
            bad = [k for k, ok in checks.items() if not ok]
            if bad:
                problems.append(f"{t['transaction_id']}: {','.join(bad)}")
        want_pr = {p["product_id"] for p in c["products"]}
        have_pr = {p for p, row in gold_pr.items() if row["customer_id"] == cid}
        if want_pr != have_pr:
            problems.append(f"{cid}: product ids differ")
            continue
        for p in c["products"]:
            g = gold_pr[p["product_id"]]
            for key in ("effective_status", "currency", "product_type_en"):
                if p.get(key) != g.get(key):
                    problems.append(f"{p['product_id']}: {key}")
            if round(float(p["current_balance"]), 2) != g["current_balance"]:
                problems.append(f"{p['product_id']}: current_balance")
    return problems


# -------------------------------------------------------------------------------------------------------------
# Panel source (offline)


def export_panel(args):
    if not os.path.exists(REFERENCE_DECLINES):
        raise SystemExit("data/bank_tools/reference/decline_codes.json is missing: run one --source gold export first")
    with open(REFERENCE_DECLINES, encoding="utf-8") as fh:
        declines = json.load(fh)
    pol = dp.load_policy()
    threshold = pol["handoff"]["amount_usd_threshold"]
    tables = {"customer_profile": [], "customer_products": [], "customer_transactions": [], "decline_codes": declines}
    for c in load_panel():
        cid, restricted = c["customer_id"], c["customer_status"] in pol["handoff"]["restricted_customer_statuses"]
        products = {p["product_id"]: p for p in c["products"]}
        tables["customer_profile"].append({
            "customer_id": cid, "country_code": c.get("customer_country_code"), "customer_status": c["customer_status"],
            "closed_or_suspended": restricted, "products_total": len(products),
            "products_active": sum(p.get("product_status") == "Active" for p in products.values()),
            "products_active_effective": sum(p.get("effective_status") == "Active" for p in products.values()),
            "product_currencies": sorted({p["currency"] for p in products.values()})})
        for p in products.values():
            tables["customer_products"].append({
                "product_id": p["product_id"], "customer_id": cid, "product_type": p.get("product_type"),
                "product_type_en": p["product_type_en"], "is_card": p["product_type_en"] in dp.CARD_TYPES,
                "product_status": p.get("product_status"), "effective_status": p["effective_status"],
                "currency": p["currency"], "current_balance": p.get("current_balance"),
                "credit_limit": p.get("credit_limit"), "balance_as_of": pol["as_of_date"],
                "product_number_last4": None, "expiration_date": p.get("expiration_date"),
                "has_linked_app": p.get("has_linked_app")})
        for t in c["transactions"]:
            prod = products.get(t["product_id"], {})
            code = t.get("response_code")
            row = dict(t)
            row.update({
                "product_type_en": prod.get("product_type_en"),
                "is_card_product": prod.get("product_type_en") in dp.CARD_TYPES,
                "channel": None if t.get("implausible_type_channel") else t.get("channel"),
                "decline_code_key": code if code else ("00" if t["transaction_status"] == "Approved" else "missing"),
                "product_owner_matches": True,
                "dispute_eligible_unrecognized": dp.is_eligible(t, cid, "dispute_unrecognized_charge", pol)[0],
                "dispute_eligible_incorrect": dp.is_eligible(t, cid, "dispute_incorrect_charge_or_fee", pol)[0],
                "above_handoff_threshold": float(t.get("amount_usd") or 0) > threshold})
            tables["customer_transactions"].append({k: row.get(k) for k in sql.TRANSACTION_COLUMNS})
    for table in ("customer_profile", "customer_products", "customer_transactions"):
        tables[table] = [normalize_row(table, r) for r in tables[table]]
    return tables, {"source": "panel", "panel_path": os.path.relpath(PANEL_PATH, REPO_ROOT)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", choices=("gold", "panel"), default="gold")
    ap.add_argument("--customers", default="panel", help="panel | sample:N | ids:<file>, comma-separated")
    ap.add_argument("--seed", type=int, default=20261005)
    ap.add_argument("--warehouse-id", default=os.environ.get("DATABRICKS_WAREHOUSE_ID", ""))
    ap.add_argument("--profile", default=os.environ.get("DATABRICKS_CONFIG_PROFILE", ""))
    ap.add_argument("--catalog", default=os.environ.get("BANK_TOOLS_CATALOG", "workspace"))
    ap.add_argument("--gold-schema", default=os.environ.get("BANK_TOOLS_GOLD_SCHEMA", "gold"))
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    if args.source == "gold" and not args.warehouse_id:
        raise SystemExit("--warehouse-id (or DATABRICKS_WAREHOUSE_ID) is required for --source gold")
    tables, meta = export_gold(args) if args.source == "gold" else export_panel(args)
    pol = dp.load_policy()
    keys = {"customer_identity": ("customer_id",), "customer_profile": ("customer_id",),
            "customer_products": ("product_id",), "customer_transactions": ("transaction_id",),
            "decline_codes": ("code_key",)}
    manifest = dict(meta)
    manifest.update({
        "gold_spec_version": GOLD_SPEC["version"], "policy_version": pol["version"],
        "snapshot_as_of": pol["as_of_date"],
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "customers": len({r["customer_id"] for r in tables["customer_profile"]}),
        "row_counts": {t: len(rows) for t, rows in tables.items()},
        "sha256": {t: table_digest(rows, keys[t]) for t, rows in tables.items()},
    })
    meta_rows = {"source": meta["source"], "snapshot_as_of": pol["as_of_date"], "policy_version": pol["version"],
                 "gold_spec_version": GOLD_SPEC["version"], "exported_at": manifest["exported_at"]}
    write_sqlite(args.out, tables, meta_rows)
    manifest_path = os.path.splitext(args.out)[0] + ".manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    print(json.dumps({"out": os.path.relpath(args.out, REPO_ROOT), "customers": manifest["customers"],
                      "row_counts": manifest["row_counts"]}, indent=1))


if __name__ == "__main__":
    main()
