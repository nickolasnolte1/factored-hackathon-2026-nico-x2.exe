"""DatabricksRepository over the SQL Statement Execution API (CONTRACT.md section 1.5). Named parameters only.

Credentials: DATABRICKS_HOST / DATABRICKS_TOKEN, else the CLI profile (`databricks auth describe` and
`databricks auth token`). Tokens are cached in memory and never logged.
"""
import json
import os
import shutil
import subprocess
import threading
import time

import requests

from ..errors import RepositoryError, RepositoryTimeout, RepositoryUnavailable, TransientRepositoryError
from . import sql
from .base import CASE_COLUMNS, TICKET_COLUMNS, Repository, normalize_row

TRANSIENT_HTTP = (429, 500, 502, 503, 504)
REJECTED_HTTP = (429, 503)  # refused before execution; 500, 502 and 504 may come after the statement ran
OPS_DDL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sql", "ops_tables.sql")


def _param(name, value):
    if value is None:
        return {"name": name, "type": "STRING"}  # no value -> NULL
    if isinstance(value, bool):
        value = "true" if value else "false"
    elif isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return {"name": name, "value": str(value), "type": "STRING"}


class CliCredentials:
    """Host and token from env vars or the Databricks CLI profile."""

    def __init__(self, host=None, token=None, profile=None, cli=None):
        self._host = (host or os.environ.get("DATABRICKS_HOST") or "").rstrip("/")
        self._static_token = token or os.environ.get("DATABRICKS_TOKEN") or ""
        self.profile = profile or os.environ.get("DATABRICKS_CONFIG_PROFILE") or ""
        self.cli = cli or os.environ.get("DATABRICKS_CLI") or shutil.which("databricks") or "databricks"
        self._token, self._token_until = "", 0.0
        self._lock = threading.Lock()

    def _cli_json(self, *args):
        cmd = [self.cli, *args] + (["--profile", self.profile] if self.profile else [])
        env = dict(os.environ, MSYS_NO_PATHCONV="1")
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
        if out.returncode != 0:
            raise RepositoryError("databricks CLI call failed: " + " ".join(args[:2]))
        return json.loads(out.stdout)

    @property
    def host(self):
        if not self._host:
            self._host = self._cli_json("auth", "describe", "-o", "json")["details"]["host"].rstrip("/")
        if not self._host.startswith("http"):
            self._host = "https://" + self._host
        return self._host

    def token(self):
        if self._static_token:
            return self._static_token
        with self._lock:
            if not self._token or time.time() >= self._token_until:
                data = self._cli_json("auth", "token")
                self._token = data["access_token"]
                self._token_until = time.time() + float(data.get("expires_in") or 600) - 60
            return self._token


class StatementClient:
    def __init__(self, warehouse_id=None, credentials=None, attempt_deadline_s=8.0, session=None):
        self.warehouse_id = warehouse_id or os.environ.get("DATABRICKS_WAREHOUSE_ID") or ""
        if not self.warehouse_id:
            raise ValueError("DATABRICKS_WAREHOUSE_ID is required for the Databricks repository")
        self.creds = credentials or CliCredentials()
        self.attempt_deadline_s = attempt_deadline_s
        self.http = session or requests.Session()

    def _request(self, method, path, body=None, timeout=30):
        url = self.creds.host + path
        headers = {"Authorization": "Bearer " + self.creds.token()}
        try:
            resp = self.http.request(method, url, json=body, headers=headers, timeout=timeout)
        except requests.ConnectTimeout:
            raise RepositoryUnavailable("ConnectTimeout") from None  # never reached the warehouse
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise RepositoryUnavailable(type(exc).__name__, maybe_applied=True) from None
        if resp.status_code in TRANSIENT_HTTP:
            raise RepositoryUnavailable("http_" + str(resp.status_code),
                                        maybe_applied=resp.status_code not in REJECTED_HTTP)
        if resp.status_code >= 400:
            raise RepositoryError("http_" + str(resp.status_code))
        return resp.json()

    def execute(self, statement, params=None, deadline_s=None):
        """Run one statement; returns a list of {column: raw value} dicts."""
        deadline_s = deadline_s or self.attempt_deadline_s
        started = time.monotonic()
        wait = max(5, min(50, int(deadline_s)))
        body = {"warehouse_id": self.warehouse_id, "statement": statement,
                "parameters": [_param(k, v) for k, v in (params or {}).items()],
                "wait_timeout": str(wait) + "s", "on_wait_timeout": "CONTINUE",
                "disposition": "INLINE", "format": "JSON_ARRAY"}
        res = self._request("POST", "/api/2.0/sql/statements/", body, timeout=wait + 15)
        sid = res.get("statement_id")
        try:
            while res.get("status", {}).get("state") in ("PENDING", "RUNNING"):
                if time.monotonic() - started >= deadline_s:
                    try:
                        self._request("POST", "/api/2.0/sql/statements/" + sid + "/cancel", {}, timeout=10)
                    except Exception:  # noqa: BLE001 - best effort
                        pass
                    raise RepositoryTimeout("statement_deadline")
                time.sleep(min(2.0, max(0.2, deadline_s / 10)))
                res = self._request("GET", "/api/2.0/sql/statements/" + sid, timeout=30)
        except TransientRepositoryError as exc:  # the statement was accepted: it may still complete
            exc.maybe_applied = True
            raise
        state = res.get("status", {}).get("state")
        if state != "SUCCEEDED":
            raise RepositoryError("statement_" + str(state).lower())
        manifest = res.get("manifest") or {}
        columns = [c["name"] for c in (manifest.get("schema") or {}).get("columns", [])]
        data = list((res.get("result") or {}).get("data_array") or [])
        for i in range(1, int(manifest.get("total_chunk_count") or 1)):
            chunk = self._request("GET", "/api/2.0/sql/statements/" + sid + "/result/chunks/" + str(i), timeout=60)
            data.extend(chunk.get("data_array") or [])
        return [dict(zip(columns, row)) for row in data]


class DatabricksRepository(Repository):
    source = "databricks"

    def __init__(self, client, catalog="workspace", gold_schema="gold", ops_schema="ops", env="dev"):
        self.client = client
        self.env = env
        self._names = (catalog, gold_schema, ops_schema)
        self._ops_ready = False
        self._case_sql = sql.case_statements(CASE_COLUMNS)
        self._ticket_sql = sql.ticket_statements(TICKET_COLUMNS)

    @classmethod
    def from_config(cls, config):
        creds = CliCredentials(config.databricks_host, config.databricks_token, config.databricks_profile,
                               config.databricks_cli)
        client = StatementClient(config.databricks_warehouse_id, creds, config.attempt_deadline_s)
        return cls(client, config.catalog, config.gold_schema, config.ops_schema, config.env)

    def _sql(self, statement):
        return sql.render(statement, *self._names)

    def _rows(self, statement, params, table, deadline_s=None):
        raw = self.client.execute(self._sql(statement), params, deadline_s)
        return [normalize_row(table, r) for r in raw]

    def _first(self, statement, params, table):
        rows = self._rows(statement, params, table)
        return rows[0] if rows else None

    def ensure_ops_tables(self):
        """CREATE TABLE IF NOT EXISTS for ops.dispute_cases, ops.handoff_tickets and ops.tool_audit (once)."""
        if self._ops_ready:
            return
        with open(OPS_DDL_PATH, encoding="utf-8") as fh:
            text = "\n".join(line for line in fh if not line.lstrip().startswith("--"))
        for statement in [s.strip() for s in text.split(";") if s.strip()]:
            self.client.execute(self._sql(statement), None, deadline_s=60)
        self._ops_ready = True

    # -- Gold reads ----------------------------------------------------------------------------------------------
    def find_customer_by_document(self, document_type, document_hash):
        rows = self._rows(sql.FIND_CUSTOMER_BY_DOCUMENT,
                          {"document_type": document_type, "document_hash": document_hash}, "customer_identity")
        return rows[0] if len(rows) == 1 else None

    def get_customer(self, customer_id):
        return self._first(sql.GET_CUSTOMER, {"customer_id": customer_id}, "customer_profile")

    def list_products(self, customer_id):
        return self._rows(sql.LIST_PRODUCTS, {"customer_id": customer_id}, "customer_products")

    def get_product(self, customer_id, product_id):
        return self._first(sql.GET_PRODUCT, {"customer_id": customer_id, "product_id": product_id}, "customer_products")

    def list_transactions(self, customer_id, until_ts, since_date=None):
        if since_date is None:
            return self._rows(sql.LIST_TRANSACTIONS, {"customer_id": customer_id, "until_ts": until_ts},
                              "customer_transactions")
        return self._rows(sql.LIST_TRANSACTIONS_SINCE,
                          {"customer_id": customer_id, "until_ts": until_ts, "since_date": since_date},
                          "customer_transactions")

    def get_transaction(self, customer_id, transaction_id, until_ts):
        return self._first(sql.GET_TRANSACTION,
                           {"customer_id": customer_id, "transaction_id": transaction_id, "until_ts": until_ts},
                           "customer_transactions")

    def get_decline_code(self, code_key):
        return self._first(sql.GET_DECLINE_CODE, {"code_key": code_key}, "decline_codes")

    # -- ops writes and reads ------------------------------------------------------------------------------------
    def insert_case(self, row):
        self.ensure_ops_tables()
        self.client.execute(self._sql(self._case_sql["insert"]), {c: row.get(c) for c in CASE_COLUMNS})

    def get_case(self, customer_id, case_id):
        self.ensure_ops_tables()
        return self._first(self._case_sql["get"], {"customer_id": customer_id, "case_id": case_id, "env": self.env},
                           "dispute_cases")

    def list_cases(self, customer_id, limit):
        self.ensure_ops_tables()
        return self._rows(self._case_sql["list"], {"customer_id": customer_id, "env": self.env},
                          "dispute_cases")[:limit]

    def find_open_case(self, customer_id, transaction_id, dispute_type):
        self.ensure_ops_tables()
        return self._first(self._case_sql["find_open"],
                           {"customer_id": customer_id, "transaction_id": transaction_id,
                            "dispute_type": dispute_type, "env": self.env}, "dispute_cases")

    def insert_ticket(self, row):
        self.ensure_ops_tables()
        self.client.execute(self._sql(self._ticket_sql["insert"]), {c: row.get(c) for c in TICKET_COLUMNS})

    def get_ticket(self, ticket_id, customer_id=None):
        self.ensure_ops_tables()
        if customer_id is None:
            return self._first(self._ticket_sql["get"], {"ticket_id": ticket_id, "env": self.env}, "handoff_tickets")
        return self._first(self._ticket_sql["get_for_customer"],
                           {"ticket_id": ticket_id, "customer_id": customer_id, "env": self.env}, "handoff_tickets")

    # -- audit labeling and health ------------------------------------------------------------------------------
    def resource_exists(self, kind, resource_id, exclude_customer_id=None):
        statement = {"transaction": sql.TRANSACTION_EXISTS, "product": sql.PRODUCT_EXISTS,
                     "case": sql.CASE_EXISTS, "ticket": sql.TICKET_EXISTS}.get(kind)
        if statement is None:
            return False
        if kind in ("case", "ticket"):
            self.ensure_ops_tables()
        rows = self.client.execute(self._sql(statement),
                                   {"resource_id": resource_id, "exclude_customer_id": exclude_customer_id or ""})
        return bool(rows)

    def health(self):
        try:
            rows = self.client.execute(self._sql(sql.HEALTH), None, deadline_s=30)
            as_of = (rows[0].get("snapshot_as_of") if rows else None)
            return {"ok": True, "source": "databricks", "snapshot_as_of": as_of}
        except Exception as exc:  # noqa: BLE001 - health never raises
            return {"ok": False, "source": "databricks", "snapshot_as_of": None, "error": type(exc).__name__}

    # -- console and harness helper (not part of the tool interface) -------------------------------------------
    def store_rows(self, table):
        """All rows of an ops table (dispute_cases or handoff_tickets) of the configured env, normalized: the same
        output as LocalRepository.store_rows. Used by the human agent console."""
        statements = {"dispute_cases": self._case_sql, "handoff_tickets": self._ticket_sql}
        if table not in statements:
            raise ValueError(table)
        self.ensure_ops_tables()
        return self._rows(statements[table]["all"], {"env": self.env}, table, deadline_s=30)

    def execute_statement(self, statement, params=None, deadline_s=60):
        """Run one statement from repository/sql.py (retention, audit sink)."""
        return self.client.execute(self._sql(statement), params, deadline_s)
