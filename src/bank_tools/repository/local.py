"""LocalRepository: a read-only SQLite snapshot of Gold plus a separate writable store for cases and tickets
(CONTRACT.md section 1.6). The store is in memory by default, so every evaluation scenario starts clean."""
import os
import sqlite3

from . import sql
from .base import CASE_COLUMNS, TICKET_COLUMNS, Repository, normalize_row

STORE_DDL = (
    "CREATE TABLE IF NOT EXISTS dispute_cases (" + ", ".join(
        c + (" TEXT PRIMARY KEY" if c == "case_id" else "") for c in CASE_COLUMNS) + ")",
    "CREATE TABLE IF NOT EXISTS handoff_tickets (" + ", ".join(
        c + (" TEXT PRIMARY KEY" if c == "ticket_id" else "") for c in TICKET_COLUMNS) + ")",
    "CREATE INDEX IF NOT EXISTS ix_cases_customer ON dispute_cases (customer_id, transaction_id)",
)
STORE_SQL = sql.local_store_statements(tuple(CASE_COLUMNS), tuple(TICKET_COLUMNS))


def _sqlite_value(value):
    if isinstance(value, bool):
        return int(value)
    return value


class LocalRepository(Repository):
    source = "local"

    def __init__(self, snapshot_path, store_path=":memory:"):
        if not os.path.exists(snapshot_path):
            raise FileNotFoundError(f"snapshot not found: {snapshot_path} (build it with python -m src.bank_tools.snapshot)")
        uri = "file:" + os.path.abspath(snapshot_path).replace("\\", "/") + "?mode=ro"
        self.snapshot_path = snapshot_path
        self._snap = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._snap.row_factory = sqlite3.Row
        self._store = sqlite3.connect(store_path or ":memory:", check_same_thread=False)
        self._store.row_factory = sqlite3.Row
        for ddl in STORE_DDL:
            self._store.execute(ddl)
        self._store.commit()
        self._tables = {r[0] for r in self._snap.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    # -- helpers -----------------------------------------------------------------------------------------------
    def _all(self, db, statement, params, table):
        return [normalize_row(table, dict(r)) for r in db.execute(statement, params).fetchall()]

    def _first(self, db, statement, params, table):
        rows = self._all(db, statement, params, table)
        return rows[0] if rows else None

    # -- Gold reads ----------------------------------------------------------------------------------------------
    def find_customer_by_document(self, document_type, document_hash):
        if "customer_identity" not in self._tables:
            return None  # panel snapshots carry no identities (section 1.6)
        rows = self._all(self._snap, sql.LOCAL_FIND_CUSTOMER_BY_DOCUMENT,
                         {"document_type": document_type, "document_hash": document_hash}, "customer_identity")
        return rows[0] if len(rows) == 1 else None

    def get_customer(self, customer_id):
        return self._first(self._snap, sql.LOCAL_GET_CUSTOMER, {"customer_id": customer_id}, "customer_profile")

    def list_products(self, customer_id):
        return self._all(self._snap, sql.LOCAL_LIST_PRODUCTS, {"customer_id": customer_id}, "customer_products")

    def get_product(self, customer_id, product_id):
        return self._first(self._snap, sql.LOCAL_GET_PRODUCT, {"customer_id": customer_id, "product_id": product_id},
                           "customer_products")

    def list_transactions(self, customer_id, until_ts, since_date=None):
        return self._all(self._snap, sql.LOCAL_LIST_TRANSACTIONS,
                         {"customer_id": customer_id, "until_ts": until_ts, "since_date": since_date},
                         "customer_transactions")

    def get_transaction(self, customer_id, transaction_id, until_ts):
        return self._first(self._snap, sql.LOCAL_GET_TRANSACTION,
                           {"customer_id": customer_id, "transaction_id": transaction_id, "until_ts": until_ts},
                           "customer_transactions")

    def get_decline_code(self, code_key):
        return self._first(self._snap, sql.LOCAL_GET_DECLINE_CODE, {"code_key": code_key}, "decline_codes")

    # -- cases and tickets (store) ----------------------------------------------------------------------------
    def insert_case(self, row):
        self._store.execute(STORE_SQL["insert_case"], {c: _sqlite_value(row.get(c)) for c in CASE_COLUMNS})
        self._store.commit()

    def get_case(self, customer_id, case_id):
        return self._first(self._store, STORE_SQL["get_case"], {"customer_id": customer_id, "case_id": case_id},
                           "dispute_cases")

    def list_cases(self, customer_id, limit):
        return self._all(self._store, STORE_SQL["list_cases"], {"customer_id": customer_id}, "dispute_cases")[:limit]

    def find_open_case(self, customer_id, transaction_id, dispute_type):
        return self._first(self._store, STORE_SQL["find_open_case"],
                           {"customer_id": customer_id, "transaction_id": transaction_id,
                            "dispute_type": dispute_type}, "dispute_cases")

    def insert_ticket(self, row):
        self._store.execute(STORE_SQL["insert_ticket"], {c: _sqlite_value(row.get(c)) for c in TICKET_COLUMNS})
        self._store.commit()

    def get_ticket(self, ticket_id, customer_id=None):
        if customer_id is None:
            return self._first(self._store, STORE_SQL["get_ticket"], {"ticket_id": ticket_id}, "handoff_tickets")
        return self._first(self._store, STORE_SQL["get_ticket_for_customer"],
                           {"ticket_id": ticket_id, "customer_id": customer_id}, "handoff_tickets")

    # -- audit labeling and health ------------------------------------------------------------------------------
    def resource_exists(self, kind, resource_id, exclude_customer_id=None):
        params = {"resource_id": resource_id, "exclude_customer_id": exclude_customer_id or ""}
        if kind in sql.LOCAL_EXISTS:
            return self._snap.execute(sql.LOCAL_EXISTS[kind], params).fetchone() is not None
        if kind in sql.LOCAL_STORE_EXISTS:
            return self._store.execute(sql.LOCAL_STORE_EXISTS[kind], params).fetchone() is not None
        return False

    def health(self):
        meta = {}
        if "snapshot_meta" in self._tables:
            meta = {r[0]: r[1] for r in self._snap.execute("SELECT key, value FROM snapshot_meta")}
        return {"ok": True, "source": "local:" + meta.get("source", "unknown"),
                "snapshot_as_of": meta.get("snapshot_as_of")}

    # -- console, test and harness helpers (not part of the tool interface) -----------------------------------
    def store_rows(self, table):
        """All rows of a store table (dispute_cases or handoff_tickets), normalized. DatabricksRepository has the
        same helper, filtered by env."""
        if table not in ("dispute_cases", "handoff_tickets"):
            raise ValueError(table)
        return self._all(self._store, "SELECT * FROM " + table + " ORDER BY created_at", {}, table)

    def close(self):
        self._snap.close()
        self._store.close()
