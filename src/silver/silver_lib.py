"""Silver framework: contract-driven typing, data-quality checks, quarantine, MERGE and run logging.

Each Silver table is a SQL file (`sql/<table>.sql`) plus a contract (`contracts/<table>.json`); see README.md.
pyspark is imported lazily, so the command line below works on a laptop without Spark. Every SQL it prints is
read-only and runs as-is on the SQL warehouse:

    python silver_lib.py validate               # validate every contract, print the build order
    python silver_lib.py sql branches           # rendered SELECT (full-mode watermark)
    python silver_lib.py describe branches      # DESCRIBE QUERY of the rendered SELECT
    python silver_lib.py checks branches        # DQ preview: failed rows per check + column-type asserts
    python silver_lib.py fk branches            # foreign-key orphans against the current Silver parents
"""
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# ops_prefix renames the ops tables (fixture tests log to fixture_pipeline_runs, never to the real run log)
DEFAULTS = {"catalog": "workspace", "bronze": "bronze", "silver": "silver", "ops": "ops", "ops_prefix": ""}

FULL_WATERMARK = "1900-01-01 00:00:00.000000"   # full mode: every Bronze row is newer than this
OPEN_END = "9999-12-31 23:59:59.999999"         # upper bound when no run pins one (CLI previews)
TS_FMT = "yyyy-MM-dd HH:mm:ss.SSSSSS"           # Spark pattern used to carry watermarks as strings
PLACEHOLDERS = ("{catalog}", "{bronze}", "{silver}", "{watermark}")
LINEAGE = ("_source_file", "_ingested_at")      # required in every SQL output, copied from Bronze
RAW = "_bronze"                                 # optional struct(src.*) of the raw row: cast checks + quarantine copy
PK_DUP = "unique:primary_key"
SILVER_EXTRA = ("_dq_warnings", "_source_file", "_ingested_at", "_silver_processed_at", "_run_id")

IDENT = re.compile(r"^[a-z][a-z0-9_]*$")
CHECK_NAME = re.compile(r"^[a-z0-9_][a-z0-9_:.\-]*$")
RUN_ID = re.compile(r"^[A-Za-z0-9_.:\-]+$")
WATERMARK = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2}(\.\d{1,6})?)?$")
TYPE = re.compile(r"^(string|boolean|tinyint|smallint|int|bigint|float|double|date|timestamp|timestamp_ntz"
                  r"|decimal\(\d{1,2},\d{1,2}\)|array<(string|int|bigint|double)>)$")
NUMERIC = ("tinyint", "smallint", "int", "bigint", "float", "double")

FRAMEWORK_COMMENTS = {
    "_dq_warnings": "Names of the warn-severity checks this row failed (kept in Silver, flagged)",
    "_source_file": "Bronze lineage: landing file the row was read from",
    "_ingested_at": "Bronze lineage: Auto Loader ingestion time; drives the incremental watermark",
    "_silver_processed_at": "Time the Silver run wrote this version of the row",
    "_run_id": "Silver run that wrote this version of the row (ops.pipeline_runs.run_id)",
}


class ContractError(Exception):
    """The contract, the SQL or the target schema are inconsistent; fix the code, not the data."""


class QualityGateError(Exception):
    """Hard-check failures exceed the contract's max_quarantine_rate; nothing was written."""


# --------------------------------------------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------------------------------------------

def _path(base_dir, *parts):
    return os.path.join(base_dir or BASE_DIR, *parts)


def list_tables(base_dir=None):
    folder = _path(base_dir, "contracts")
    return sorted(f[:-5] for f in os.listdir(folder) if f.endswith(".json") and not f.startswith("_"))


def load_contract(table, base_dir=None):
    with open(_path(base_dir, "contracts", f"{table}.json"), encoding="utf-8") as fh:
        c = json.load(fh)
    c.setdefault("table", table)
    c.setdefault("source", table)
    c.setdefault("natural_key", list(c.get("primary_key", [])))
    c.setdefault("event_time", None)
    c.setdefault("load", {})
    c.setdefault("foreign_keys", [])
    c.setdefault("checks", [])
    c.setdefault("depends_on", [])
    errors = validate_contract(c)
    if c["table"] != table:
        errors.append(f"'table' is {c['table']!r} but the file is {table}.json")
    if errors:
        raise ContractError(f"contracts/{table}.json: " + "; ".join(errors))
    return c


def load_contracts(base_dir=None, errors=None):
    """All contracts by table. With an `errors` dict, broken contracts are reported there instead of raising."""
    out = {}
    for t in list_tables(base_dir):
        try:
            out[t] = load_contract(t, base_dir)
        except Exception as e:
            if errors is None:
                raise
            errors[t] = f"{type(e).__name__}: {e}"
    return out


def validate_contract(c):
    """Return a list of problems (empty when the contract is usable)."""
    e = []
    if not IDENT.match(str(c.get("table", ""))):
        e.append("'table' must be a lowercase identifier")
    if not IDENT.match(str(c.get("source", ""))):
        e.append("'source' must be a lowercase identifier")
    if c.get("kind") not in ("dimension", "fact"):
        e.append("'kind' must be 'dimension' or 'fact'")
    cols = c.get("columns")
    if not isinstance(cols, dict) or not cols:
        return e + ["'columns' must be a non-empty object"]
    for name, spec in cols.items():
        where = f"columns.{name}"
        if not IDENT.match(name):
            e.append(f"{where}: names are lowercase identifiers and must not start with '_' (reserved)")
        if not isinstance(spec, dict):
            e.append(f"{where}: must be an object")
            continue
        typ = norm_type(spec.get("type", ""))
        if not TYPE.match(typ):
            e.append(f"{where}.type {spec.get('type')!r} is not a supported Spark SQL type")
        if not isinstance(spec.get("nullable"), bool):
            e.append(f"{where}.nullable must be true or false")
        if not str(spec.get("description", "")).strip():
            e.append(f"{where}.description is required")
        if "allowed" in spec and (not isinstance(spec["allowed"], list) or not spec["allowed"]):
            e.append(f"{where}.allowed must be a non-empty list")
        if ("min" in spec or "max" in spec) and typ in ("string", "boolean"):
            e.append(f"{where}: min/max need a numeric, date or timestamp type")
        if "source" in spec and not IDENT.match(str(spec["source"])):
            e.append(f"{where}.source must be a Bronze column name")
    for key in ("primary_key", "natural_key"):
        keys = c.get(key)
        if not isinstance(keys, list) or not keys:
            e.append(f"'{key}' must be a non-empty list of columns")
        else:
            e += [f"{key} column {k!r} is not in columns" for k in keys if k not in cols]
    et = c.get("event_time")
    if et is not None and (et not in cols or norm_type(cols[et].get("type", "")) not in ("date", "timestamp", "timestamp_ntz")):
        e.append("'event_time' must be null or a date/timestamp column")
    load = c.get("load", {})
    for key in ("partition_by", "cluster_by"):
        e += [f"load.{key} column {k!r} is not in columns" for k in load.get(key, []) if k not in cols]
    if load.get("partition_by") and load.get("cluster_by"):
        e.append("load: use partition_by or cluster_by, not both")
    rate = load.get("max_quarantine_rate")
    if rate is not None and not (isinstance(rate, (int, float)) and 0 <= rate <= 1):
        e.append("load.max_quarantine_rate must be a number between 0 and 1")
    for i, fk in enumerate(c.get("foreign_keys", [])):
        if fk.get("column") not in cols:
            e.append(f"foreign_keys[{i}].column is not in columns")
        if not (IDENT.match(str(fk.get("ref_table", ""))) and IDENT.match(str(fk.get("ref_column", "")))):
            e.append(f"foreign_keys[{i}]: ref_table and ref_column must be identifiers")
        if fk.get("severity") not in ("hard", "flag"):
            e.append(f"foreign_keys[{i}].severity must be 'hard' or 'flag'")
    names = set()
    for i, chk in enumerate(c.get("checks", [])):
        name = str(chk.get("name", ""))
        if not CHECK_NAME.match(name) or name in names or name == PK_DUP:
            e.append(f"checks[{i}].name {name!r} must be unique and match {CHECK_NAME.pattern}")
        names.add(name)
        if not str(chk.get("expr", "")).strip():
            e.append(f"checks[{i}].expr is required")
        if chk.get("severity") not in ("hard", "warn"):
            e.append(f"checks[{i}].severity must be 'hard' or 'warn'")
    return e


def order_tables(contracts):
    """Dependency order: a table comes after the Silver tables it references (foreign_keys, depends_on);
    among ready tables, dimensions go first, then alphabetical."""
    deps = {t: ({fk["ref_table"] for fk in c["foreign_keys"]} | set(c["depends_on"])) & set(contracts) - {t}
            for t, c in contracts.items()}
    ordered, done = [], set()
    while len(ordered) < len(contracts):
        ready = [t for t in contracts if t not in done and deps[t] <= done]
        if not ready:
            raise ContractError(f"dependency cycle among {sorted(set(contracts) - done)}")
        nxt = min(ready, key=lambda t: (contracts[t]["kind"] != "dimension", t))
        ordered.append(nxt)
        done.add(nxt)
    return ordered


# --------------------------------------------------------------------------------------------------------------
# SQL rendering
# --------------------------------------------------------------------------------------------------------------

def norm_type(t):
    return re.sub(r"\s+", "", str(t).lower())


def quote(name):
    return f"`{name}`"


def sql_str(v):
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def sql_lit(v, typ):
    t = norm_type(typ)
    if v is None:
        return "NULL"
    if t == "date":
        return f"DATE {sql_str(v)}"
    if t.startswith("timestamp"):
        return f"TIMESTAMP {sql_str(v)}"
    if t in NUMERIC or t.startswith("decimal"):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ContractError(f"numeric literal expected, got {v!r}")
        return repr(v)
    if t == "boolean":
        return "true" if v else "false"
    return sql_str(v)


def norm_watermark(s):
    s = str(s).strip()
    if not WATERMARK.match(s):
        raise ContractError(f"watermark {s!r} must look like yyyy-MM-dd[ HH:mm:ss[.ffffff]]")
    return s if " " in s else s + " 00:00:00"


def fqn(cfg, layer, table):
    return f"{cfg['catalog']}.{cfg[layer]}.{table}"


def ops_table(cfg, name):
    cfg = {**DEFAULTS, **(cfg or {})}
    return fqn(cfg, "ops", f"{cfg['ops_prefix'] or ''}{name}")


def render_sql(table, watermark=FULL_WATERMARK, cfg=None, base_dir=None, source_view=None, contract=None, sources=None):
    """Read sql/<table>.sql and fill the placeholders. Tests swap Bronze tables for views or fixture tables:
    `source_view` replaces the contract's source, `sources` maps any other Bronze table name to a replacement."""
    cfg = {**DEFAULTS, **(cfg or {})}
    c = contract or load_contract(table, base_dir)
    with open(_path(base_dir, "sql", f"{table}.sql"), encoding="utf-8") as fh:
        text = fh.read().strip().rstrip(";").rstrip()
    if "{watermark}" not in text:
        raise ContractError(f"sql/{table}.sql must filter Bronze with _ingested_at > TIMESTAMP '{{watermark}}'")
    swaps = dict(sources or {})
    if source_view:
        swaps[c["source"]] = source_view
    for name, replacement in swaps.items():
        ref = re.compile(re.escape("{catalog}.{bronze}." + name) + r"(?![A-Za-z0-9_])")
        if not ref.search(text):
            raise ContractError(f"sql/{table}.sql does not reference {{catalog}}.{{bronze}}.{name}")
        text = ref.sub(lambda _: replacement, text)
    values = {"{catalog}": cfg["catalog"], "{bronze}": cfg["bronze"], "{silver}": cfg["silver"],
              "{watermark}": norm_watermark(watermark)}
    for token in PLACEHOLDERS:  # plain replace, not str.format: SQL regexes use braces
        text = text.replace(token, values[token])
    return text


def build_checks(c, has_raw=True):
    """Row-level checks in evaluation order. Each: name, expr (the row PASSES when expr is TRUE; NULL fails),
    severity (hard -> quarantine, warn -> kept and flagged), rule_ref, description."""
    keys = set(c["primary_key"]) | ({c["event_time"]} if c.get("event_time") else set())
    out = []

    def add(name, expr, severity, rule_ref=None, description=""):
        out.append({"name": name, "expr": expr, "severity": severity,
                    "rule_ref": None if rule_ref is None else str(rule_ref), "description": description})

    for col, spec in c["columns"].items():
        q, typ, sev = quote(col), norm_type(spec["type"]), "hard" if col in keys else "warn"
        if not spec["nullable"]:
            add(f"not_null:{col}", f"{q} IS NOT NULL", sev, spec.get("rule_ref"), f"{col} is required")
        if has_raw and spec.get("source") and typ != "string":
            raw = f"{RAW}.{quote(spec['source'])}"
            add(f"cast:{col}", f"NOT (nullif(trim({raw}), '') IS NOT NULL AND {q} IS NULL)", sev, 22,
                f"raw {spec['source']} is present but did not cast to {typ}")
        if spec.get("allowed"):
            values = ", ".join(sql_lit(v, typ) for v in spec["allowed"])
            add(f"allowed:{col}", f"{q} IS NULL OR {q} IN ({values})", "warn", spec.get("rule_ref"),
                f"{col} outside its allowed domain")
        if "min" in spec or "max" in spec:
            parts = ([f"{q} >= {sql_lit(spec['min'], typ)}"] if "min" in spec else []) + \
                    ([f"{q} <= {sql_lit(spec['max'], typ)}"] if "max" in spec else [])
            add(f"range:{col}", f"{q} IS NULL OR ({' AND '.join(parts)})", "warn", spec.get("rule_ref"),
                f"{col} outside [{spec.get('min', '-inf')}, {spec.get('max', '+inf')}]")
    for chk in c.get("checks", []):
        add(chk["name"], chk["expr"], chk["severity"], chk.get("rule_ref"), chk.get("description", ""))
    if has_raw:
        add("no_rescued_data", f"{RAW}.`_rescued_data` IS NULL", "warn", 22, "Auto Loader rescued unparsed data")
    return out


def _flag_array(checks):
    cases = [f"CASE WHEN NOT coalesce(({ch['expr']}), false) THEN '{ch['name']}' END" for ch in checks]
    return cases


def checked_sql(c, rendered, checks, upper=OPEN_END):
    """CTE chain ending in _silver_checked: the rendered rows, capped at `upper`, with _pk_rank,
    _dq_reasons (hard failures) and _dq_warnings (warn failures)."""
    pk = [quote(k) for k in c["primary_key"]]
    pk_present = " AND ".join(f"{k} IS NOT NULL" for k in pk)
    hard = _flag_array([ch for ch in checks if ch["severity"] == "hard"])
    hard.append(f"CASE WHEN {pk_present} AND _pk_rank > 1 THEN '{PK_DUP}' END")
    warn = _flag_array([ch for ch in checks if ch["severity"] == "warn"])

    def arr(cases):
        if not cases:
            return "CAST(array() AS ARRAY<STRING>)"
        return "filter(array(\n      " + ",\n      ".join(cases) + "\n    ), x -> x IS NOT NULL)"

    return f"""WITH _silver_src AS (
{rendered}
),
_silver_ranked AS (
  SELECT *, row_number() OVER (PARTITION BY {', '.join(pk)} ORDER BY _ingested_at DESC, _source_file DESC) AS _pk_rank
  FROM _silver_src
  WHERE _ingested_at <= TIMESTAMP '{norm_watermark(upper)}'
),
_silver_checked AS (
  SELECT *,
    {arr(hard)} AS _dq_reasons,
    {arr(warn)} AS _dq_warnings
  FROM _silver_ranked
)"""


def check_sql(c, rendered, checks, source, wm_in, wm_out=OPEN_END, type_asserts=False):
    """One query, long format (check_name, severity, rule_ref, failed_rows). Metric rows start with '__'."""
    items = [("__rows_output", "metric", None, "count(*)"),
             ("__rows_quarantined", "metric", None, "count_if(size(_dq_reasons) > 0)")]
    for ch in checks:
        arr = "_dq_reasons" if ch["severity"] == "hard" else "_dq_warnings"
        items.append((ch["name"], ch["severity"], ch["rule_ref"], f"count_if(array_contains({arr}, '{ch['name']}'))"))
    items.append((PK_DUP, "hard", "22", f"count_if(array_contains(_dq_reasons, '{PK_DUP}'))"))
    if type_asserts:
        for col, spec in c["columns"].items():
            items.append((f"type:{col}", "hard", None,
                          f"max(CASE WHEN typeof({quote(col)}) = '{norm_type(spec['type'])}' THEN 0 ELSE 1 END)"))
    aggs = ",\n    ".join(f"{expr} AS m{i}" for i, (_, _, _, expr) in enumerate(items))
    rows = [f"named_struct('check_name', '__rows_source', 'severity', 'metric', 'rule_ref', CAST(NULL AS STRING), "
            f"'failed_rows', CAST(raw_rows AS BIGINT))"]
    for i, (name, sev, ref, _) in enumerate(items):
        ref_sql = "CAST(NULL AS STRING)" if ref is None else sql_str(ref)
        rows.append(f"named_struct('check_name', '{name}', 'severity', '{sev}', 'rule_ref', {ref_sql}, "
                    f"'failed_rows', CAST(m{i} AS BIGINT))")
    return (checked_sql(c, rendered, checks, wm_out) +
            f""",
_silver_agg AS (
  SELECT
    {aggs}
  FROM _silver_checked
),
_silver_raw AS (
  SELECT count(*) AS raw_rows FROM {source}
  WHERE _ingested_at > TIMESTAMP '{norm_watermark(wm_in)}' AND _ingested_at <= TIMESTAMP '{norm_watermark(wm_out)}'
)
SELECT inline(array(
  {(',' + chr(10) + '  ').join(rows)}
))
FROM _silver_agg CROSS JOIN _silver_raw""")


def valid_sql(c, checked, processed_at, run_id):
    cols = ", ".join(quote(k) for k in c["columns"])
    return (f"{checked}\nSELECT {cols}, _dq_warnings, _source_file, _ingested_at, "
            f"TIMESTAMP '{processed_at}' AS _silver_processed_at, '{run_id}' AS _run_id\n"
            f"FROM _silver_checked WHERE size(_dq_reasons) = 0")


def quarantine_sql(c, checked, processed_at, run_id, has_raw):
    cols = ", ".join(quote(k) for k in c["columns"])
    raw = f"to_json({RAW})" if has_raw else "CAST(NULL AS STRING)"
    return (f"{checked}\nSELECT {cols}, _source_file, _ingested_at, {raw} AS _raw_json, _dq_reasons, _dq_warnings, "
            f"'{run_id}' AS _run_id, TIMESTAMP '{processed_at}' AS _quarantined_at\n"
            f"FROM _silver_checked WHERE size(_dq_reasons) > 0")


def fk_sql(c, target, cfg, fk):
    col, parent = quote(fk["column"]), fqn(cfg, "silver", fk["ref_table"])
    return (f"SELECT count_if(c.{col} IS NOT NULL) AS total_rows, "
            f"count_if(c.{col} IS NOT NULL AND p._k IS NULL) AS failed_rows\n"
            f"FROM {target} c LEFT JOIN (SELECT DISTINCT {quote(fk['ref_column'])} AS _k FROM {parent}) p "
            f"ON c.{col} = p._k")


# --------------------------------------------------------------------------------------------------------------
# Spark execution
# --------------------------------------------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc)


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def _parse_ts(s):
    """Watermark string (UTC session time zone) -> aware datetime."""
    if s is None:
        return None
    s = norm_watermark(s)
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f" if "." in s else "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def new_run_id():
    return f"{_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def table_exists(spark, name):
    try:
        return spark.catalog.tableExists(name)
    except Exception:
        try:
            spark.table(name).schema
            return True
        except Exception:
            return False


def _retry(fn, attempts=4):
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # concurrent writers on shared ops tables
            if i == attempts - 1 or "oncurrent" not in str(e):
                raise
            time.sleep(3 * (i + 1))


def ensure_ops_tables(spark, cfg=None):
    cfg = {**DEFAULTS, **(cfg or {})}
    ddl = {
        "pipeline_runs": ("run_id STRING, table_name STRING, mode STRING, started_at TIMESTAMP, finished_at TIMESTAMP, "
                          "watermark_in TIMESTAMP, watermark_out TIMESTAMP, rows_source BIGINT, rows_valid BIGINT, "
                          "rows_quarantined BIGINT, rows_inserted BIGINT, rows_updated BIGINT, status STRING, error STRING",
                          "Silver runs, one row per table per run. Watermark = watermark_out of the last succeeded run."),
        "dq_results": ("run_id STRING, table_name STRING, check_name STRING, severity STRING, rule_ref STRING, "
                       "failed_rows BIGINT, total_rows BIGINT, pass_rate DOUBLE, passed BOOLEAN, evaluated_at TIMESTAMP",
                       "Silver data-quality results per run and check. passed is NULL when the check was not evaluated."),
        "freshness": ("table_name STRING, silver_table STRING, `rows` BIGINT, quarantine_rows BIGINT, max_event_date DATE, "
                      "max_ingested_at TIMESTAMP, last_run_id STRING, last_run_mode STRING, last_run_status STRING, "
                      "last_run_finished_at TIMESTAMP, last_success_at TIMESTAMP, refreshed_at TIMESTAMP",
                      "Silver freshness per contract table, refreshed by the Silver runner."),
    }
    for name, (cols, comment) in ddl.items():
        _retry(lambda: spark.sql(f"CREATE TABLE IF NOT EXISTS {ops_table(cfg, name)} ({cols}) COMMENT {sql_str(comment)}"))


def last_watermark(spark, cfg, table):
    runs = ops_table(cfg, "pipeline_runs")
    if not table_exists(spark, runs):
        return None
    rows = spark.sql(f"SELECT date_format(watermark_out, '{TS_FMT}') AS wm FROM {runs} "
                     f"WHERE table_name = {sql_str(table)} AND status = 'succeeded' AND watermark_out IS NOT NULL "
                     f"ORDER BY finished_at DESC LIMIT 1").collect()
    return rows[0]["wm"] if rows else None


def assert_schema(c, schema):
    actual = {f.name: f.dataType.simpleString() for f in schema.fields}
    problems = []
    for name, spec in c["columns"].items():
        if name not in actual:
            problems.append(f"missing column {name}")
        elif actual[name] != norm_type(spec["type"]):
            problems.append(f"{name} is {actual[name]}, contract says {norm_type(spec['type'])}")
    problems += [f"missing lineage column {k}" for k in LINEAGE if k not in actual]
    extra = [n for n in actual if n not in c["columns"] and n not in LINEAGE and n != RAW]
    problems += [f"column {n} is not in the contract" for n in extra]
    if problems:
        raise ContractError(f"sql/{c['table']}.sql output does not match the contract: " + "; ".join(problems))


def _annotate(df, c):
    """Carry contract descriptions as column comments (Delta keeps them from the field metadata)."""
    from pyspark.sql import functions as F
    desc = {**{k: v["description"] for k, v in c["columns"].items()}, **FRAMEWORK_COMMENTS}
    try:
        return df.select([F.col(quote(n)).alias(n, metadata={"comment": desc[n]}) if n in desc else F.col(quote(n))
                          for n in df.columns])
    except Exception:
        return df


def _merge_counts(row):
    d = row.asDict() if row is not None else {}
    return int(d.get("num_inserted_rows") or 0), int(d.get("num_updated_rows") or 0)


def _dq(name, severity, rule_ref, failed, total):
    if failed is None:
        rate = None
    elif total:
        rate = round(1 - failed / total, 6)
    else:
        rate = 1.0 if failed == 0 else 0.0
    return {"check_name": name, "severity": severity, "rule_ref": rule_ref, "failed_rows": failed,
            "total_rows": total, "pass_rate": rate, "passed": None if failed is None else failed == 0}


def post_checks(spark, c, target, cfg):
    """Table-level checks after the write: foreign keys against Silver parents (orphans counted, never dropped)
    and natural-key uniqueness across the whole table when it differs from the primary key."""
    out = []
    for fk in c["foreign_keys"]:
        name = f"fk:{fk['column']}->{fk['ref_table']}.{fk['ref_column']}"
        ref = str(fk["rule_ref"]) if fk.get("rule_ref") is not None else None
        if not table_exists(spark, fqn(cfg, "silver", fk["ref_table"])):
            total = spark.sql(f"SELECT count_if({quote(fk['column'])} IS NOT NULL) AS n FROM {target}").collect()[0]["n"]
            out.append(_dq(name, fk["severity"], ref, None, total))  # parent not built yet: not evaluated
            continue
        r = spark.sql(fk_sql(c, target, cfg, fk)).collect()[0]
        out.append(_dq(name, fk["severity"], ref, r["failed_rows"], r["total_rows"]))
    if c["natural_key"] != c["primary_key"]:
        nk = ", ".join(f"'{k}', {quote(k)}" for k in c["natural_key"])
        r = spark.sql(f"SELECT count(*) AS total, count(*) - count(DISTINCT named_struct({nk})) AS dups "
                      f"FROM {target}").collect()[0]
        out.append(_dq("unique:natural_key", "warn", "22", r["dups"], r["total"]))
    return out


def run_table(spark, table, mode="incremental", run_id=None, cfg=None, base_dir=None, watermark_override=None,
              source_view=None, target_table=None, log=True, sources=None):
    """Build one Silver table. Returns the pipeline_runs row as a dict, plus 'dq' (list of check results).

    full        -> every Bronze row; the Silver table and its quarantine are rebuilt (CREATE OR REPLACE), so
                   changes to the SQL or contract are applied to all rows.
    incremental -> Bronze rows with _ingested_at > watermark (last succeeded run, or watermark_override),
                   MERGEd by primary key: insert new keys; update when the incoming _ingested_at is newer and the
                   content differs (a re-delivered identical row changes nothing).
    For tests: source_view / sources swap Bronze tables for views or fixture tables, target_table must be a fixture_
    table, and logging then needs cfg['ops_prefix'] = 'fixture_...' (or log=False), so the real run log and its
    watermarks are never touched."""
    cfg = {**DEFAULTS, **(cfg or {})}
    run_id = run_id or new_run_id()
    if not RUN_ID.match(run_id):
        raise ValueError(f"invalid run_id {run_id!r}")
    started = _now()
    res = {"run_id": run_id, "table_name": table, "mode": mode, "started_at": started, "finished_at": None,
           "watermark_in": None, "watermark_out": None, "rows_source": 0, "rows_valid": 0, "rows_quarantined": 0,
           "rows_inserted": 0, "rows_updated": 0, "status": "failed", "error": None}
    dq = []
    try:
        if mode not in ("full", "incremental"):
            raise ValueError(f"mode must be 'full' or 'incremental', got {mode!r}")
        c = load_contract(table, base_dir)
        target = target_table or fqn(cfg, "silver", table)
        if target_table and not target_table.split(".")[-1].startswith("fixture_"):
            raise ValueError("target_table overrides must be named fixture_*")
        if target_table and log and not str(cfg.get("ops_prefix") or "").startswith("fixture_"):
            raise ValueError("fixture runs log only to fixture_ ops tables: pass cfg ops_prefix='fixture_' or log=False")
        quarantine = f"{target}_quarantine"
        exists = table_exists(spark, target)
        if mode == "full" or not exists:
            wm_in = FULL_WATERMARK
        elif watermark_override:
            wm_in = norm_watermark(watermark_override)
        else:
            wm_in = last_watermark(spark, cfg, table) or FULL_WATERMARK
        res["watermark_in"] = wm_in
        swaps = dict(sources or {})
        if source_view:
            swaps[c["source"]] = source_view
        source = swaps.get(c["source"]) or fqn(cfg, "bronze", c["source"])
        # Pin the upper bound first, so every pass below sees the same Bronze window.
        wm_out = spark.sql(f"SELECT date_format(max(_ingested_at), '{TS_FMT}') AS wm FROM {source} "
                           f"WHERE _ingested_at > TIMESTAMP '{wm_in}'").collect()[0]["wm"]
        if wm_out is not None:
            rendered = render_sql(table, wm_in, cfg, base_dir, contract=c, sources=swaps)
            src_schema = spark.sql(rendered).schema
            has_raw = RAW in src_schema.names
            assert_schema(c, src_schema)
            checks = build_checks(c, has_raw)
            counts = {r["check_name"]: r for r in
                      spark.sql(check_sql(c, rendered, checks, source, wm_in, wm_out)).collect()}
            n_src = counts["__rows_source"]["failed_rows"]
            n_out = counts["__rows_output"]["failed_rows"]
            n_bad = counts["__rows_quarantined"]["failed_rows"]
            res.update(rows_source=n_src, rows_valid=n_out - n_bad, rows_quarantined=n_bad)
            dq.append(_dq("dedup:natural_key", "warn", "22", n_src - n_out, n_src))
            for name, r in counts.items():
                if not name.startswith("__"):
                    dq.append(_dq(name, r["severity"], r["rule_ref"], r["failed_rows"], n_out))
            limit = c["load"].get("max_quarantine_rate")
            if limit is not None and n_out and n_bad / n_out > limit:
                raise QualityGateError(f"{n_bad} of {n_out} rows fail hard checks (> {limit:.2%}); nothing written")

            checked = checked_sql(c, rendered, checks, wm_out)
            processed_at = _ts(started)
            view = f"_silver_valid_{table}"
            valid_df = spark.sql(valid_sql(c, checked, processed_at, run_id))
            if mode == "full" or not exists:
                _annotate(valid_df, c).createOrReplaceTempView(view)
                load = c["load"]
                layout = (f"CLUSTER BY ({', '.join(map(quote, load['cluster_by']))}) " if load.get("cluster_by") else
                          f"PARTITIONED BY ({', '.join(map(quote, load['partition_by']))}) " if load.get("partition_by") else "")
                spark.sql(f"CREATE OR REPLACE TABLE {target} {layout}COMMENT {sql_str(c.get('description', ''))} "
                          f"AS SELECT * FROM {view}")
                res["rows_inserted"] = res["rows_valid"]
            else:
                current = [(f.name, f.dataType.simpleString()) for f in spark.table(target).schema.fields]
                incoming = [(f.name, f.dataType.simpleString()) for f in valid_df.schema.fields]
                if current != incoming:
                    raise ContractError(f"{target} schema differs from the SQL/contract output; run mode=full to rebuild")
                valid_df.createOrReplaceTempView(view)
                on = " AND ".join(f"t.{quote(k)} = s.{quote(k)}" for k in c["primary_key"])
                same = " AND ".join(f"t.{quote(k)} <=> s.{quote(k)}" for k in [*c["columns"], "_dq_warnings"])
                r = spark.sql(f"MERGE INTO {target} t USING {view} s ON {on}\n"
                              f"WHEN MATCHED AND s._ingested_at > t._ingested_at AND NOT ({same}) THEN UPDATE SET *\n"
                              f"WHEN NOT MATCHED THEN INSERT *").collect()
                res["rows_inserted"], res["rows_updated"] = _merge_counts(r[0] if r else None)

            q_df = spark.sql(quarantine_sql(c, checked, processed_at, run_id, has_raw))
            if mode == "full" or not table_exists(spark, quarantine):
                q_df.createOrReplaceTempView(f"_silver_quarantine_{table}")
                spark.sql(f"CREATE OR REPLACE TABLE {quarantine} COMMENT "
                          f"{sql_str(f'Rows of {table} that failed hard checks; _dq_reasons lists them, _raw_json keeps the Bronze row')} "
                          f"AS SELECT * FROM _silver_quarantine_{table}")
            elif n_bad:
                q_df.write.mode("append").option("mergeSchema", "true").saveAsTable(quarantine)
        if table_exists(spark, target):
            dq += post_checks(spark, c, target, cfg)
        res["watermark_out"] = wm_out or wm_in
        res["status"] = "succeeded"
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"[:4000]
    res["finished_at"] = _now()
    if log:
        write_logs(spark, cfg, res, dq)
    res["dq"] = dq
    return res


def write_logs(spark, cfg, res, dq):
    runs_schema = ("run_id string, table_name string, mode string, started_at timestamp, finished_at timestamp, "
                   "watermark_in timestamp, watermark_out timestamp, rows_source bigint, rows_valid bigint, "
                   "rows_quarantined bigint, rows_inserted bigint, rows_updated bigint, status string, error string")
    row = dict(res, watermark_in=_parse_ts(res["watermark_in"]), watermark_out=_parse_ts(res["watermark_out"]))
    cols = [c.split(" ")[0] for c in runs_schema.split(", ")]
    runs_df = spark.createDataFrame([tuple(row[k] for k in cols)], runs_schema)
    _retry(lambda: runs_df.write.mode("append").saveAsTable(ops_table(cfg, "pipeline_runs")))
    if dq:
        dq_schema = ("run_id string, table_name string, check_name string, severity string, rule_ref string, "
                     "failed_rows bigint, total_rows bigint, pass_rate double, passed boolean, evaluated_at timestamp")
        now = _now()
        rows = [(res["run_id"], res["table_name"], d["check_name"], d["severity"], d["rule_ref"], d["failed_rows"],
                 d["total_rows"], d["pass_rate"], d["passed"], now) for d in dq]
        dq_df = spark.createDataFrame(rows, dq_schema)
        _retry(lambda: dq_df.write.mode("append").saveAsTable(ops_table(cfg, "dq_results")))


def refresh_freshness(spark, contracts, cfg=None):
    """Upsert one ops.freshness row per contract table (rows NULL when the Silver table is not built yet)."""
    cfg = {**DEFAULTS, **(cfg or {})}
    stats, quar = [], []
    for t, c in contracts.items():
        target = fqn(cfg, "silver", t)
        if table_exists(spark, target):
            ev = f"max(CAST({quote(c['event_time'])} AS DATE))" if c.get("event_time") else "CAST(NULL AS DATE)"
            stats.append(f"SELECT '{t}' AS table_name, '{target}' AS silver_table, count(*) AS `rows`, {ev} AS max_event_date, "
                         f"max(_ingested_at) AS max_ingested_at FROM {target}")
        else:
            stats.append(f"SELECT '{t}', '{target}', CAST(NULL AS BIGINT), CAST(NULL AS DATE), CAST(NULL AS TIMESTAMP)")
        if table_exists(spark, f"{target}_quarantine"):
            quar.append(f"SELECT '{t}' AS table_name, count(*) AS quarantine_rows FROM {target}_quarantine")
    if not stats:
        return
    quar = quar or ["SELECT CAST(NULL AS STRING) AS table_name, CAST(NULL AS BIGINT) AS quarantine_rows"]
    runs = ops_table(cfg, "pipeline_runs")
    spark.sql(f"""
WITH s AS ({' UNION ALL '.join(stats)}),
q AS ({' UNION ALL '.join(quar)}),
r AS (
  SELECT table_name, max_by(run_id, finished_at) AS last_run_id, max_by(mode, finished_at) AS last_run_mode,
         max_by(status, finished_at) AS last_run_status, max(finished_at) AS last_run_finished_at,
         max(CASE WHEN status = 'succeeded' THEN finished_at END) AS last_success_at
  FROM {runs} GROUP BY table_name)
SELECT s.table_name, s.silver_table, s.`rows`, q.quarantine_rows, s.max_event_date, s.max_ingested_at,
       r.last_run_id, r.last_run_mode, r.last_run_status, r.last_run_finished_at, r.last_success_at,
       current_timestamp() AS refreshed_at
FROM s LEFT JOIN q ON q.table_name = s.table_name LEFT JOIN r ON r.table_name = s.table_name""").createOrReplaceTempView(
        "_silver_freshness")
    _retry(lambda: spark.sql(f"MERGE INTO {ops_table(cfg, 'freshness')} t USING _silver_freshness s "
                             f"ON t.table_name = s.table_name WHEN MATCHED THEN UPDATE SET * "
                             f"WHEN NOT MATCHED THEN INSERT *"))


def format_results(results):
    """Plain-text summary table for notebook output."""
    head = ["table", "status", "mode", "source", "valid", "quar", "ins", "upd", "warn_fail", "hard_fk", "secs"]
    lines = []
    for r in results:
        dq = r.get("dq", [])
        warn = sum(1 for d in dq if d["severity"] == "warn" and d["passed"] is False)
        fk = sum(1 for d in dq if d["check_name"].startswith("fk:") and d["severity"] == "hard" and d["passed"] is False)
        secs = (r["finished_at"] - r["started_at"]).total_seconds() if r.get("finished_at") else 0
        lines.append([r["table_name"], r["status"], r["mode"], r["rows_source"], r["rows_valid"], r["rows_quarantined"],
                      r["rows_inserted"], r["rows_updated"], warn, fk, f"{secs:.0f}"])
    widths = [max(len(str(x)) for x in col) for col in zip(head, *lines)]
    fmt = lambda row: " | ".join(str(v).ljust(w) for v, w in zip(row, widths))
    return "\n".join([fmt(head), "-+-".join("-" * w for w in widths)] + [fmt(l) for l in lines])


# --------------------------------------------------------------------------------------------------------------
# Command line (no Spark): validate contracts and print read-only SQL for the SQL warehouse
# --------------------------------------------------------------------------------------------------------------

def _cli(argv):
    import argparse
    p = argparse.ArgumentParser(description="Validate Silver contracts and render read-only SQL.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="validate every contract and print the build order")
    for name in ("sql", "describe", "checks", "fk"):
        sp = sub.add_parser(name)
        sp.add_argument("table")
        sp.add_argument("--watermark", default=FULL_WATERMARK)
    a = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = dict(DEFAULTS)
    if a.cmd == "validate":
        contracts = load_contracts()
        for t in contracts:
            render_sql(t, contract=contracts[t])
        print("ok:", " -> ".join(order_tables(contracts)))
        return
    c = load_contract(a.table)
    rendered = render_sql(a.table, a.watermark, contract=c)
    if a.cmd == "sql":
        print(rendered)
    elif a.cmd == "describe":
        print(f"DESCRIBE QUERY\n{rendered}")
    elif a.cmd == "checks":
        has_raw = re.search(rf"\bAS\s+{RAW}\b", rendered, re.I) is not None
        print(check_sql(c, rendered, build_checks(c, has_raw), fqn(cfg, "bronze", c["source"]), a.watermark,
                        type_asserts=True))
    elif a.cmd == "fk":
        target = fqn(cfg, "silver", a.table)
        parts = [f"SELECT '{fk['column']}->{fk['ref_table']}.{fk['ref_column']}' AS fk, '{fk['severity']}' AS severity, *\n"
                 f"FROM ({fk_sql(c, target, cfg, fk)})" for fk in c["foreign_keys"]]
        print("\nUNION ALL\n".join(parts) if parts else "-- no foreign keys in this contract")


if __name__ == "__main__":
    _cli(sys.argv[1:])
