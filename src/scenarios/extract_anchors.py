"""Extract scenario anchors and the end-to-end panel from workspace.silver (read-only) to data/scenarios/.

Runs the sections of anchors.sql on a Databricks SQL warehouse through the Databricks CLI (`databricks api`),
pinning every Silver table to its current Delta version (or to the versions of an earlier snapshot), so the files
can be re-extracted byte for byte while those versions exist.

    python -m src.scenarios.extract_anchors --seed 20261005 --warehouse-id <id> [--profile factored]
                                            [--cli databricks] [--pin-from data/scenarios/silver_snapshot.json]

Outputs (data/ is git-ignored):
    anchors.jsonl          intent-dataset anchors (one transaction per row, with product and customer context)
    panel.jsonl            e2e panel: one customer per row with its products and full transaction history
    silver_snapshot.json   pinned table versions and timestamps, threshold calibration, row counts
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

from src.policy.dispute_policy import load_policy

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SQL_PATH = os.path.join(REPO, "src", "scenarios", "anchors.sql")
OUT_DIR = os.path.join(REPO, "data", "scenarios")
TABLES = {"transactions": "workspace.silver.transactions", "products": "workspace.silver.products",
          "customers": "workspace.silver.customers",
          "transactions_quarantine": "workspace.silver.transactions_quarantine"}
SPLIT_DATE = "2025-07-01"
PANEL_ACTIVE, PANEL_RESTRICTED = 320, 40
# Only these columns may leave Silver (no names, documents, contact data or product numbers).
ALLOWED_COLUMNS = {
    "transaction_id", "customer_id", "product_id", "event_ts", "event_date", "transaction_type", "transaction_category",
    "amount", "currency", "amount_usd", "channel", "merchant_name", "merchant_category", "transaction_country_code",
    "is_international", "transaction_status", "response_code", "response_code_null_reason", "implausible_type_channel",
    "product_type", "product_type_en", "product_status", "effective_status", "expiration_date", "current_balance",
    "credit_limit", "has_linked_app", "customer_country_code", "customer_status", "customer_bucket", "split",
    "status_group", "anchor_kind", "n_period", "n_purchase", "n_rows", "p50_usd", "p75_usd", "p90_usd", "p95_usd",
    "share_above_threshold", "share_at_or_above_high", "share_at_or_above_medium",
}


def parse_sql(path=SQL_PATH):
    """Split anchors.sql into CTE blocks and named queries."""
    text = open(path, encoding="utf-8").read()
    ctes, queries = [], {}
    for kind, name, body in re.findall(r"^-- @(cte|query) (\w+)\n(.*?)(?=^-- @(?:cte|query) |\Z)", text, re.S | re.M):
        body = "\n".join(line for line in body.strip().splitlines() if not line.lstrip().startswith("--")).strip()
        (ctes.append(body) if kind == "cte" else queries.__setitem__(name, body))
    return ctes, queries


def render(ctes, query, params):
    sql = "WITH " + ",\n".join(ctes) + "\n" + query
    for key, value in params.items():
        sql = sql.replace("{" + key + "}", str(value))
    leftover = re.findall(r"\{[a-z_]+\}", sql)
    if leftover:
        raise ValueError(f"unfilled placeholders: {sorted(set(leftover))}")
    return sql


class Warehouse:
    """Minimal SQL Statement Execution API client over the Databricks CLI."""

    def __init__(self, cli, profile, warehouse_id):
        self.cli, self.profile, self.warehouse_id = cli, profile, warehouse_id

    def _api(self, method, path, body=None):
        cmd = [self.cli, "api", method, path, "--profile", self.profile]
        tmp = None
        if body is not None:
            tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
            json.dump(body, tmp)
            tmp.close()
            cmd += ["--json", "@" + tmp.name]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        finally:
            if tmp:
                os.unlink(tmp.name)
        if r.returncode != 0:
            raise RuntimeError(f"databricks api {method} {path} failed: {r.stderr or r.stdout}")
        return json.loads(r.stdout)

    def query(self, sql):
        res = self._api("post", "/api/2.0/sql/statements", {
            "warehouse_id": self.warehouse_id, "statement": sql, "wait_timeout": "50s", "on_wait_timeout": "CONTINUE",
            "format": "JSON_ARRAY", "disposition": "INLINE"})
        sid = res["statement_id"]
        while res["status"]["state"] in ("PENDING", "RUNNING"):
            time.sleep(3)
            res = self._api("get", f"/api/2.0/sql/statements/{sid}")
        if res["status"]["state"] != "SUCCEEDED":
            raise RuntimeError(f"statement {res['status']['state']}: {json.dumps(res['status'].get('error'))}")
        cols = res["manifest"]["schema"]["columns"]
        rows = list(res.get("result", {}).get("data_array") or [])
        nxt = res.get("result", {}).get("next_chunk_index")
        while nxt is not None:
            chunk = self._api("get", f"/api/2.0/sql/statements/{sid}/result/chunks/{nxt}")
            rows += chunk.get("data_array") or []
            nxt = chunk.get("next_chunk_index")
        names = [c["name"] for c in cols]
        types = [c.get("type_name", "STRING") for c in cols]
        return [{n: _typed(v, t) for n, v, t in zip(names, row, types)} for row in rows]


def _typed(value, type_name):
    if value is None:
        return None
    if type_name in ("DECIMAL", "DOUBLE", "FLOAT"):
        return round(float(value), 6)
    if type_name in ("INT", "LONG", "BIGINT", "SHORT", "BYTE"):
        return int(value)
    if type_name == "BOOLEAN":
        return value in (True, "true", "True")
    if type_name == "TIMESTAMP":
        return str(value).replace(" ", "T").replace("Z", "")[:19]
    return value


def _iso_ts(value):
    return value.replace(" ", "T")[:19] if isinstance(value, str) else value


def table_versions(wh, pin_from=None):
    if pin_from:
        snap = json.load(open(pin_from, encoding="utf-8"))
        return {k: snap["tables"][k] for k in TABLES}
    out = {}
    for key, name in TABLES.items():
        h = wh.query(f"DESCRIBE HISTORY {name} LIMIT 1")[0]
        out[key] = {"table": name, "version": int(h["version"]), "timestamp": _typed(h["timestamp"], "TIMESTAMP")}
    return out


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def check_columns(name, rows):
    extra = set().union(*(r.keys() for r in rows)) - ALLOWED_COLUMNS if rows else set()
    if extra:
        raise ValueError(f"{name}: columns outside the allowed list (possible personal data): {sorted(extra)}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=20261005)
    ap.add_argument("--warehouse-id", default=os.environ.get("DATABRICKS_WAREHOUSE_ID"))
    ap.add_argument("--profile", default=os.environ.get("DATABRICKS_CONFIG_PROFILE", "factored"))
    ap.add_argument("--cli", default=os.environ.get("DATABRICKS_CLI", "databricks"))
    ap.add_argument("--pin-from", help="reuse the table versions of an earlier silver_snapshot.json")
    ap.add_argument("--out-dir", default=OUT_DIR)
    args = ap.parse_args(argv)
    if not args.warehouse_id:
        sys.exit("--warehouse-id (or DATABRICKS_WAREHOUSE_ID) is required")
    os.makedirs(args.out_dir, exist_ok=True)
    wh = Warehouse(args.cli, args.profile, args.warehouse_id)
    pol = load_policy()

    versions = table_versions(wh, args.pin_from)
    params = {k: f"{v['table']} VERSION AS OF {v['version']}" for k, v in versions.items()}
    params.update({"seed": args.seed, "split_date": SPLIT_DATE, "panel_active": PANEL_ACTIVE,
                   "panel_restricted": PANEL_RESTRICTED, "threshold_usd": pol["handoff"]["amount_usd_threshold"],
                   "high_usd": pol["priority"]["high_amount_usd"], "medium_usd": pol["priority"]["medium_amount_usd"]})
    ctes, queries = parse_sql()
    results = {}
    for name in ("anchors", "panel_customers", "panel_products", "panel_transactions", "calibration"):
        t0 = time.time()
        results[name] = wh.query(render(ctes, queries[name], params))
        check_columns(name, results[name])
        print(f"{name}: {len(results[name]):,} rows in {time.time() - t0:.0f} s")

    anchors = sorted(results["anchors"], key=lambda r: (r["split"], r["anchor_kind"], r["status_group"], r["transaction_id"]))
    for r in anchors:
        r["event_ts"] = _iso_ts(r["event_ts"])
    write_jsonl(os.path.join(args.out_dir, "anchors.jsonl"), anchors)

    products, txns = {}, {}
    for p in results["panel_products"]:
        products.setdefault(p["customer_id"], []).append(p)
    for t in results["panel_transactions"]:
        t["event_ts"] = _iso_ts(t["event_ts"])
        txns.setdefault(t["customer_id"], []).append(t)
    panel = []
    for c in sorted(results["panel_customers"], key=lambda r: r["customer_id"]):
        c = dict(c)
        c["products"] = sorted(products.get(c["customer_id"], []), key=lambda p: p["product_id"])
        c["transactions"] = sorted(txns.get(c["customer_id"], []), key=lambda t: (t["event_ts"], t["transaction_id"]))
        panel.append(c)
    write_jsonl(os.path.join(args.out_dir, "panel.jsonl"), panel)

    snapshot = {
        "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seed": args.seed, "split_date": SPLIT_DATE, "tables": versions,
        "calibration": {**results["calibration"][0], "threshold_usd": params["threshold_usd"],
                        "high_usd": params["high_usd"], "medium_usd": params["medium_usd"]},
        "counts": {"anchors": len(anchors), "panel_customers": len(panel),
                   "panel_products": len(results["panel_products"]),
                   "panel_transactions": len(results["panel_transactions"])},
    }
    with open(os.path.join(args.out_dir, "silver_snapshot.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(snapshot, fh, indent=2, sort_keys=True)
    cal = snapshot["calibration"]
    print(f"threshold {cal['threshold_usd']} USD: p90 = {cal['p90_usd']}, share above = {cal['share_above_threshold']:.4f}")


if __name__ == "__main__":
    main()
