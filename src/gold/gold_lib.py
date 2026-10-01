"""Gold layer helpers: table specs, SQL rendering from the synthetic policy, privacy rules and check SQL.

Each Gold table is a SELECT over Silver (`sql/<table>.sql`) plus an entry in `gold_tables.json` (column types and
descriptions, keys, clustering, row reconciliation). Policy values (eligible statuses and types, handoff threshold,
restricted statuses, decline codes, card types) are rendered from `src/policy/dispute_policy.json` and
`dispute_policy.py`, never re-typed in SQL. pyspark is not needed here; the notebook `01_build_gold.py` runs it.

    python gold_lib.py validate                      # spec, SQL placeholders and privacy rules (no Spark)
    python gold_lib.py sql customer_transactions     # rendered read-only SELECT over the current Silver tables
    python gold_lib.py describe customer_profile     # DESCRIBE QUERY of it (for the SQL warehouse)
    python gold_lib.py checks customer_transactions  # post-build check queries (need the Gold tables)
"""
import hashlib
import json
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SPEC_PATH = os.path.join(BASE_DIR, "gold_tables.json")
SILVER_CONTRACTS_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "silver", "contracts"))

try:  # repo root on sys.path (local runs, tests, tools)
    from src.policy import dispute_policy as dp
except ImportError:  # Databricks workspace: the notebook folder is on sys.path, the repo root is not
    sys.path.insert(0, os.path.normpath(os.path.join(BASE_DIR, "..", "policy")))
    import dispute_policy as dp

PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
SAFE_TEXT = r"^[\\p{L}\\p{N} .,&()-]{1,60}$"  # SQL literal; Spark unescapes \\p to \p (letters, digits, basic punctuation)


class GoldSpecError(Exception):
    """The spec, the SQL or the query output are inconsistent; fix the code, not the data."""


class PrivacyError(Exception):
    """A Gold table would expose personal data; it is not written (or it is dropped)."""


# --------------------------------------------------------------------------------------------------------------
# Spec, identity hash and policy values
# --------------------------------------------------------------------------------------------------------------

def load_spec(path=SPEC_PATH):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def normalize_document(document_type, document_number):
    """'<TYPE upper-case>|<number upper-case, only A-Z and 0-9>': the same normalization as customer_identity.sql."""
    doc_type = str(document_type or "").strip().upper()
    number = re.sub(r"[^A-Z0-9]", "", str(document_number or "").upper())
    return f"{doc_type}|{number}"


def document_hash(document_type, document_number):
    """Python twin of gold.customer_identity.document_hash (unkeyed SHA-256; DEV ONLY, production would use HMAC)."""
    return hashlib.sha256(normalize_document(document_type, document_number).encode("utf-8")).hexdigest()


def sql_lit(value):
    """SQL string literal (Spark default escaping)."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _in_list(values):
    return ", ".join(sql_lit(v) for v in values)


def policy_decline_keys(pol=None):
    """Decline-code keys of the policy: its codes, with the 'null' entry named 'missing'."""
    pol = pol if pol is not None else dp.load_policy()
    return ["missing" if k == "null" else k for k, v in pol["decline_codes"].items() if isinstance(v, dict)]


def policy_values(pol=None):
    """Placeholder -> SQL text, all taken from the policy JSON and the reference implementation."""
    pol = pol if pol is not None else dp.load_policy()
    elig = pol["eligibility"]
    rows = []
    for key, entry in pol["decline_codes"].items():
        if not isinstance(entry, dict):  # the 'note'
            continue
        rows.append(f"({sql_lit('missing' if key == 'null' else key)}, {sql_lit(entry['reason'])}, "
                    f"{sql_lit(entry['customer_message_key'])}, {'true' if entry.get('cards_only') else 'false'})")
    return {
        "as_of_date": pol["as_of_date"],
        "policy_version": pol["version"],
        "restricted_statuses": _in_list(pol["handoff"]["restricted_customer_statuses"]),
        "eligible_statuses": _in_list(elig["transaction_statuses"]),
        "types_unrecognized": _in_list(elig["transaction_types"]["dispute_unrecognized_charge"]),
        "types_incorrect": _in_list(elig["transaction_types"]["dispute_incorrect_charge_or_fee"]),
        "handoff_amount_usd": str(pol["handoff"]["amount_usd_threshold"]),
        "card_types": _in_list(dp.CARD_TYPES),
        "policy_decline_codes": ", ".join(rows),  # one line: placeholders may also appear in SQL comments
    }


def source_refs(spec=None, versions=None, catalog=None):
    """Silver source placeholder -> table reference, pinned with VERSION AS OF when versions are given."""
    spec = spec or load_spec()
    catalog = catalog or spec["catalog"]
    names = sorted({s for t in spec["tables"].values() for s in t["sources"]})
    return {n: f"{catalog}.silver.{n}" + (f" VERSION AS OF {int(versions[n])}" if versions and n in versions else "")
            for n in names}


def render_sql(table, sources=None, pol=None, base_dir=BASE_DIR):
    """The table's SELECT with every {placeholder} filled; an unknown placeholder is an error."""
    with open(os.path.join(base_dir, "sql", f"{table}.sql"), encoding="utf-8") as fh:
        text = fh.read()
    values = {**policy_values(pol), **(sources or source_refs())}

    def fill(m):
        if m.group(1) not in values:
            raise GoldSpecError(f"sql/{table}.sql: unknown placeholder {{{m.group(1)}}}")
        return values[m.group(1)]

    return PLACEHOLDER.sub(fill, text)


def target(spec, table):
    return f"{spec['catalog']}.{spec['schema']}.{table}"


def assert_schema(table, fields, spec=None):
    """fields = [(name, simpleString type)] of the query output; must equal the spec (names, order and types)."""
    spec = spec or load_spec()
    declared = [(n, c["type"]) for n, c in spec["tables"][table]["columns"].items()]
    if list(fields) != declared:
        got, want = dict(fields), dict(declared)
        problems = [f"missing {n}" for n in want if n not in got]
        problems += [f"{n} is not in the spec" for n in got if n not in want]
        problems += [f"{n} is {got[n]}, spec says {want[n]}" for n in want if n in got and got[n] != want[n]]
        if not problems:
            problems = ["column order differs from the spec"]
        raise GoldSpecError(f"sql/{table}.sql output does not match gold_tables.json: " + "; ".join(problems))


# --------------------------------------------------------------------------------------------------------------
# Privacy rules
# --------------------------------------------------------------------------------------------------------------

def silver_pii_columns(contracts_dir=SILVER_CONTRACTS_DIR):
    """Columns that a Silver contract describes as 'PII.' (any table)."""
    out = set()
    if not os.path.isdir(contracts_dir):
        return out
    for f in sorted(os.listdir(contracts_dir)):
        if f.endswith(".json"):
            with open(os.path.join(contracts_dir, f), encoding="utf-8") as fh:
                c = json.load(fh)
            out |= {n for n, col in c["columns"].items() if str(col.get("description", "")).startswith("PII.")}
    return out


def pii_columns(columns, spec=None, silver_pii=None):
    """Column names that look like personal data: Silver PII columns, or the forbidden pattern, minus the allowlist."""
    spec = spec or load_spec()
    priv = spec["privacy"]
    pattern = re.compile(priv["forbidden_column_pattern"], re.I)
    silver_pii = silver_pii_columns() if silver_pii is None else silver_pii
    allowed = set(priv["allowed_columns"])
    return [c for c in columns if c not in allowed and (c in silver_pii or pattern.search(c))]


def pii_value_predicate(table, spec=None):
    """SQL boolean: some scanned text column of the row looks like an e-mail, document/phone/card number or IP."""
    spec = spec or load_spec()
    regex = "|".join(f"({p})" for p in spec["privacy"]["value_patterns"].values())
    exprs = []
    for name, col in spec["tables"][table]["columns"].items():
        if name.endswith("_id") or name == "document_hash":
            continue
        if col["type"] == "string":
            exprs.append(f"coalesce(`{name}` RLIKE {sql_lit(regex)}, false)")
        elif col["type"] == "array<string>":
            exprs.append(f"coalesce(array_join(`{name}`, ' ') RLIKE {sql_lit(regex)}, false)")
    return " OR ".join(exprs) if exprs else "false"


# --------------------------------------------------------------------------------------------------------------
# Post-build checks: each query returns one row (failed_rows, total_rows)
# --------------------------------------------------------------------------------------------------------------

def _check(name, severity, rule_ref, sql):
    return {"name": name, "severity": severity, "rule_ref": rule_ref, "sql": sql}


def _fill(sql, **kw):
    for k, v in kw.items():
        sql = sql.replace(f"<<{k}>>", v)
    return sql


def check_queries(table, sources=None, spec=None, pol=None):
    """Hard checks fail the build (privacy ones also drop the table); warn checks are reported only."""
    spec = spec or load_spec()
    pol = pol if pol is not None else dp.load_policy()
    src = sources or source_refs(spec)
    ts = spec["tables"][table]
    t = target(spec, table)
    g = lambda name: target(spec, name)
    pk = ", ".join(f"'{k}', `{k}`" for k in ts["primary_key"])
    pk_null = " OR ".join(f"`{k}` IS NULL" for k in ts["primary_key"])
    out = [
        _check("not_null:primary_key", "hard", "gold", f"SELECT count_if({pk_null}) AS failed_rows, count(*) AS total_rows FROM {t}"),
        _check("unique:primary_key", "hard", "gold",
               f"SELECT count(*) - count(DISTINCT named_struct({pk})) AS failed_rows, count(*) AS total_rows FROM {t}"),
    ]
    for cols in ts.get("unique", []):
        key = ", ".join(f"'{k}', `{k}`" for k in cols)
        out.append(_check(f"unique:{'+'.join(cols)}", "hard", "gold",
                          f"SELECT count(*) - count(DISTINCT named_struct({key})) AS failed_rows, count(*) AS total_rows FROM {t}"))
    rec = ts["reconcile_rows"]
    if "source" in rec:
        expected = f"(SELECT count(*) FROM {src[rec['source']]})"
    elif "policy_decline_codes_plus" in rec:
        expected = str(len(policy_decline_keys(pol)) + int(rec["policy_decline_codes_plus"]))
    else:
        expected = str(int(rec["expected"]))
    out.append(_check("reconcile:rows", "hard", "gold",
                      f"SELECT abs(count(*) - {expected}) AS failed_rows, {expected} AS total_rows FROM {t}"))
    if ts.get("pii_value_scan"):
        out.append(_check("privacy:no_pii_values", "hard", "privacy",
                          f"SELECT count_if({pii_value_predicate(table, spec)}) AS failed_rows, count(*) AS total_rows FROM {t}"))

    as_of = pol["as_of_date"]
    if table == "customer_identity":
        out.append(_check("format:document_hash", "hard", "9",
                          f"SELECT count_if(NOT coalesce(document_hash RLIKE '^[0-9a-f]{{64}}$', false)) AS failed_rows, "
                          f"count(*) AS total_rows FROM {t}"))
    elif table == "customer_profile":
        out.append(_check("reconcile:products_total", "hard", "gold",
                          f"SELECT abs(sum(products_total) - (SELECT count(*) FROM {src['products']})) AS failed_rows, "
                          f"(SELECT count(*) FROM {src['products']}) AS total_rows FROM {t}"))
    elif table == "customer_products":
        out += [
            _check("fk:customer_id->customer_profile.customer_id", "hard", "gold",
                   f"SELECT count_if(c.customer_id IS NULL) AS failed_rows, count(*) AS total_rows "
                   f"FROM {t} p LEFT JOIN {g('customer_profile')} c ON c.customer_id = p.customer_id"),
            _check("last4:null_only_on_collision", "hard", "10",
                   f"SELECT count_if((product_number_last4 IS NULL) <> product_number_collision "
                   f"OR length(product_number_last4) <> 4) AS failed_rows, count(*) AS total_rows FROM {t}"),
            _check("reconcile:transaction_count", "hard", "gold",
                   f"SELECT abs(sum(transaction_count) - (SELECT count(*) FROM {src['transactions']})) AS failed_rows, "
                   f"(SELECT count(*) FROM {src['transactions']}) AS total_rows FROM {t}"),
            _check("recompute:last_movement_matches_silver", "warn", "7",
                   f"SELECT count_if(NOT (g.last_movement_at <=> s.last_transaction_date_recomputed)) AS failed_rows, "
                   f"count(*) AS total_rows FROM {t} g JOIN {src['products']} s ON s.product_id = g.product_id"),
            _check("recompute:effective_opening_matches_silver", "warn", "8",
                   f"SELECT count_if(NOT (g.effective_opening_date <=> s.effective_opening_date)) AS failed_rows, "
                   f"count(*) AS total_rows FROM {t} g JOIN {src['products']} s ON s.product_id = g.product_id"),
        ]
    elif table == "customer_transactions":
        same = " AND ".join(f"g.{c} <=> s.{c}" for c in ("customer_id", "product_id", "event_ts", "event_date", "amount",
                                                          "currency", "amount_usd", "transaction_type", "transaction_status",
                                                          "merchant_name", "response_code", "is_international"))
        out += [
            _check("grounding:product_owner", "hard", "eligibility.ownership",
                   f"SELECT count_if(p.customer_id IS NULL OR p.customer_id <> t.customer_id OR NOT t.product_owner_matches) "
                   f"AS failed_rows, count(*) AS total_rows FROM {t} t LEFT JOIN {g('customer_products')} p "
                   f"ON p.product_id = t.product_id"),
            _check("reconcile:values_match_silver", "hard", "gold",
                   f"SELECT count_if(s.transaction_id IS NULL OR NOT ({same})) AS failed_rows, count(*) AS total_rows "
                   f"FROM {t} g LEFT JOIN {src['transactions']} s ON s.transaction_id = g.transaction_id"),
            _check("narration:channel_withheld_when_implausible", "hard", "18",
                   f"SELECT count_if(implausible_type_channel AND channel IS NOT NULL) AS failed_rows, "
                   f"count_if(implausible_type_channel) AS total_rows FROM {t}"),
            _check("fk:decline_code_key->decline_codes.code_key", "hard", "21",
                   f"SELECT count_if(d.code_key IS NULL) AS failed_rows, count(*) AS total_rows "
                   f"FROM {t} t LEFT JOIN {g('decline_codes')} d ON d.code_key = t.decline_code_key"),
            _check("text:merchant_name_safe_charset", "warn", "injection",
                   f"SELECT count_if(merchant_name IS NOT NULL AND NOT merchant_name RLIKE '{SAFE_TEXT}') AS failed_rows, "
                   f"count(merchant_name) AS total_rows FROM {t}"),
            _check("time:event_not_after_as_of", "warn", "19",
                   f"SELECT count_if(event_date > DATE '{as_of}') AS failed_rows, count(*) AS total_rows FROM {t}"),
        ]
    elif table == "decline_codes":
        keys = ", ".join(f"({sql_lit(k)})" for k in policy_decline_keys(pol))
        observed = (f"SELECT DISTINCT CASE WHEN response_code IS NOT NULL THEN response_code "
                    f"WHEN response_code_null_reason = 'not_applicable' THEN '00' ELSE 'missing' END AS code_key "
                    f"FROM {src['transactions']}")
        out += [
            _check("policy:all_codes_present", "hard", "policy.decline_codes",
                   f"SELECT count_if(d.code_key IS NULL) AS failed_rows, count(*) AS total_rows "
                   f"FROM (VALUES {keys}) AS k(code_key) LEFT JOIN {t} d ON d.code_key = k.code_key"),
            _check("coverage:silver_codes", "hard", "21",
                   f"SELECT count_if(d.code_key IS NULL) AS failed_rows, count(*) AS total_rows "
                   f"FROM ({observed}) o LEFT JOIN {t} d ON d.code_key = o.code_key"),
            _check("text:explanations_present", "hard", "policy.decline_codes",
                   f"SELECT count_if(meaning_en IS NULL OR explanation_es IS NULL OR explanation_pt IS NULL "
                   f"OR (cards_only AND (non_card_explanation_es IS NULL OR non_card_explanation_pt IS NULL))) "
                   f"AS failed_rows, count(*) AS total_rows FROM {t}"),
        ]
    elif table == "contact_baseline":
        out += [
            _check("not_null:value", "hard", "gold",
                   f"SELECT count_if(value IS NULL) AS failed_rows, count(*) AS total_rows FROM {t}"),
            _check("reconcile:report_section_9", "warn", "report.9",
                   f"SELECT count_if(abs(value - report_value) > report_tolerance + 1e-9) AS failed_rows, "
                   f"count(report_value) AS total_rows FROM {t}"),
        ]
    return out


# --------------------------------------------------------------------------------------------------------------
# Reference checks against the policy implementation (rows collected by the notebook)
# --------------------------------------------------------------------------------------------------------------

def transaction_flag_mismatches(rows, pol=None):
    """Rows (dicts) whose dispute flags differ from dispute_policy.is_eligible / handoff_reasons."""
    pol = pol if pol is not None else dp.load_policy()
    bad = []
    for r in rows:
        exp_unrec = dp.is_eligible(r, r["customer_id"], "dispute_unrecognized_charge", pol)[0]
        exp_inc = dp.is_eligible(r, r["customer_id"], "dispute_incorrect_charge_or_fee", pol)[0]
        exp_high = "amount_above_threshold" in dp.handoff_reasons({"transaction": r}, pol)
        if (exp_unrec, exp_inc, exp_high) != (r["dispute_eligible_unrecognized"], r["dispute_eligible_incorrect"],
                                              r["above_handoff_threshold"]):
            bad.append(r["transaction_id"])
    return bad


def decline_mismatches(rows, pol=None):
    """Policy codes whose Gold reason or message key differs from dispute_policy.decline_explanation, for a card and
    for a non-card product."""
    pol = pol if pol is not None else dp.load_policy()
    bad = []
    for r in rows:
        if not r["in_policy"]:
            continue
        for product_type_en in ("Credit Card", "Savings Account"):
            ref = dp.decline_explanation(r["response_code"], pol, product_type_en)
            non_card = r["cards_only"] and product_type_en not in dp.CARD_TYPES
            if non_card:
                null = pol["decline_codes"]["null"]
                want = (null["reason"], null["customer_message_key"])
            else:
                want = (r["reason"], r["customer_message_key"])
            if (ref["reason"], ref["customer_message_key"]) != want:
                bad.append(f"{r['code_key']}:{product_type_en}")
    return bad


# --------------------------------------------------------------------------------------------------------------
# Command line (no Spark)
# --------------------------------------------------------------------------------------------------------------

def validate(spec=None):
    """Spec <-> SQL consistency without Spark. Returns the list of problems (empty = ok)."""
    spec = spec or load_spec()
    problems = []
    if sorted(spec["build_order"]) != sorted(spec["tables"]):
        problems.append("build_order and tables differ")
    for name, ts in spec["tables"].items():
        if not os.path.exists(os.path.join(BASE_DIR, "sql", f"{name}.sql")):
            problems.append(f"{name}: sql/{name}.sql missing")
            continue
        try:
            render_sql(name)
        except GoldSpecError as e:
            problems.append(str(e))
        for k in ("description", "sources", "primary_key", "reconcile_rows", "columns"):
            if k not in ts:
                problems.append(f"{name}: missing {k}")
        for c, col in ts["columns"].items():
            if not col.get("type") or not col.get("description"):
                problems.append(f"{name}.{c}: type and description are required")
        for k in ts["primary_key"] + ts.get("cluster_by", []):
            if k not in ts["columns"]:
                problems.append(f"{name}: key or cluster column {k} is not a column")
        bad = pii_columns(ts["columns"], spec)
        if bad:
            problems.append(f"{name}: PII-like columns {bad}")
    return problems


def _cli(argv):
    import argparse
    p = argparse.ArgumentParser(description="Validate the Gold spec and render read-only SQL.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    for name in ("sql", "describe", "checks"):
        sub.add_parser(name).add_argument("table")
    a = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    if a.cmd == "validate":
        problems = validate()
        print("\n".join(problems) if problems else "ok: " + " -> ".join(load_spec()["build_order"]))
        sys.exit(1 if problems else 0)
    if a.cmd == "sql":
        print(render_sql(a.table))
    elif a.cmd == "describe":
        print("DESCRIBE QUERY\n" + render_sql(a.table))
    elif a.cmd == "checks":
        print("\nUNION ALL\n".join(f"SELECT '{c['name']}' AS check_name, '{c['severity']}' AS severity, * FROM ({c['sql']})"
                                   for c in check_queries(a.table)))


if __name__ == "__main__":
    _cli(sys.argv[1:])
