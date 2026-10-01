"""Every SQL statement of the bank tools, as module constants (CONTRACT.md section 1.5).

Rules, enforced by review and tests:
- Values always travel as named parameters (`:name`); no statement is built with f-strings, `%` or `format`.
- The only text placed into a statement is the catalog and schema names: `{gold}` and `{ops}` are replaced by
  `render()` with identifiers from config, checked against ^[a-z_][a-z0-9_]*$, never from tool arguments.
- Column lists are the only place that names Gold columns: a Gold rename changes this file only.
Databricks statements use `{gold}`/`{ops}`; LOCAL_* statements are the SQLite twins over the snapshot and store.
"""
import re

IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")

# -- column maps (Gold v1.0.0, src/gold/gold_tables.json) -------------------------------------------------------
IDENTITY_COLUMNS = ("customer_id", "document_type", "document_hash", "country", "country_code", "customer_status",
                    "doc_type_inconsistent")
PROFILE_COLUMNS = ("customer_id", "segment", "country", "country_code", "home_currency", "product_currencies",
                   "currency_usd_for_mexico", "customer_status", "closed_or_suspended", "products_total",
                   "products_active", "products_active_effective", "cards_expired", "customer_status_conflict",
                   "restricted_with_active_products")
PRODUCT_COLUMNS = ("product_id", "customer_id", "product_type", "product_type_en", "is_card", "product_status",
                   "effective_status", "currency", "current_balance", "credit_limit", "credit_limit_null_reason",
                   "balance_as_of", "product_number_last4", "product_number_collision", "last4_shared_within_customer",
                   "expiration_date", "effective_opening_date", "first_movement_at", "last_movement_at",
                   "transaction_count", "has_linked_app", "customer_status_conflict")
TRANSACTION_COLUMNS = ("transaction_id", "customer_id", "product_id", "product_type_en", "is_card_product", "event_ts",
                       "event_date", "amount", "currency", "amount_usd", "transaction_type", "transaction_category",
                       "merchant_name", "merchant_category", "channel", "implausible_type_channel",
                       "transaction_status", "response_code", "response_code_null_reason", "decline_code_key",
                       "transaction_country_code", "is_international", "product_owner_matches",
                       "dispute_eligible_unrecognized", "dispute_eligible_incorrect", "above_handoff_threshold",
                       "activity_before_opening")
DECLINE_COLUMNS = ("code_key", "response_code", "reason", "customer_message_key", "in_policy", "cards_only",
                   "applies_to", "meaning_en", "explanation_es", "explanation_pt", "non_card_explanation_es",
                   "non_card_explanation_pt", "note", "observed_rows", "observed_declined_rows",
                   "observed_declined_non_card_rows", "policy_version")
TABLE_COLUMNS = {"customer_identity": IDENTITY_COLUMNS, "customer_profile": PROFILE_COLUMNS,
                 "customer_products": PRODUCT_COLUMNS, "customer_transactions": TRANSACTION_COLUMNS,
                 "decline_codes": DECLINE_COLUMNS}

SPARK_TYPES = {"string": "STRING", "timestamp": "TIMESTAMP", "date": "DATE", "boolean": "BOOLEAN", "int": "INT",
               "bigint": "BIGINT", "double": "DOUBLE", "decimal(18,2)": "DECIMAL(18,2)"}


def _cols(columns):
    return ", ".join(columns)


# -- Databricks: Gold reads --------------------------------------------------------------------------------------
FIND_CUSTOMER_BY_DOCUMENT = ("SELECT customer_id, customer_status FROM {gold}.customer_identity"
                             " WHERE document_type = :document_type AND document_hash = :document_hash LIMIT 2")
GET_CUSTOMER = "SELECT " + _cols(PROFILE_COLUMNS) + " FROM {gold}.customer_profile WHERE customer_id = :customer_id"
LIST_PRODUCTS = ("SELECT " + _cols(PRODUCT_COLUMNS) + " FROM {gold}.customer_products"
                 " WHERE customer_id = :customer_id ORDER BY product_type_en, product_id")
GET_PRODUCT = ("SELECT " + _cols(PRODUCT_COLUMNS) + " FROM {gold}.customer_products"
               " WHERE customer_id = :customer_id AND product_id = :product_id")
LIST_TRANSACTIONS = ("SELECT " + _cols(TRANSACTION_COLUMNS) + " FROM {gold}.customer_transactions"
                     " WHERE customer_id = :customer_id AND product_owner_matches"
                     " AND event_ts <= CAST(:until_ts AS TIMESTAMP)"
                     " ORDER BY event_ts DESC, transaction_id DESC")
LIST_TRANSACTIONS_SINCE = ("SELECT " + _cols(TRANSACTION_COLUMNS) + " FROM {gold}.customer_transactions"
                           " WHERE customer_id = :customer_id AND product_owner_matches"
                           " AND event_ts <= CAST(:until_ts AS TIMESTAMP) AND event_date >= CAST(:since_date AS DATE)"
                           " ORDER BY event_ts DESC, transaction_id DESC")
GET_TRANSACTION = ("SELECT " + _cols(TRANSACTION_COLUMNS) + " FROM {gold}.customer_transactions"
                   " WHERE customer_id = :customer_id AND transaction_id = :transaction_id AND product_owner_matches"
                   " AND event_ts <= CAST(:until_ts AS TIMESTAMP)")
GET_DECLINE_CODE = "SELECT " + _cols(DECLINE_COLUMNS) + " FROM {gold}.decline_codes WHERE code_key = :code_key"
TRANSACTION_EXISTS = ("SELECT 1 AS hit FROM {gold}.customer_transactions"
                      " WHERE transaction_id = :resource_id AND customer_id <> :exclude_customer_id LIMIT 1")
PRODUCT_EXISTS = ("SELECT 1 AS hit FROM {gold}.customer_products"
                  " WHERE product_id = :resource_id AND customer_id <> :exclude_customer_id LIMIT 1")
CASE_EXISTS = ("SELECT 1 AS hit FROM {ops}.dispute_cases WHERE case_id = :resource_id"
               " AND (customer_id IS NULL OR customer_id <> :exclude_customer_id) LIMIT 1")
TICKET_EXISTS = ("SELECT 1 AS hit FROM {ops}.handoff_tickets WHERE ticket_id = :resource_id"
                 " AND (customer_id IS NULL OR customer_id <> :exclude_customer_id) LIMIT 1")
HEALTH = "SELECT max(balance_as_of) AS snapshot_as_of FROM {gold}.customer_products"

# -- Databricks: snapshot export (read-only, comma-joined id list as one STRING parameter) ----------------------
EXPORT_BY_CUSTOMERS = {
    table: "SELECT " + _cols(cols) + " FROM {gold}." + table + " WHERE array_contains(split(:ids, ','), customer_id)"
    for table, cols in TABLE_COLUMNS.items() if table != "decline_codes"
}
EXPORT_DECLINE_CODES = "SELECT " + _cols(DECLINE_COLUMNS) + " FROM {gold}.decline_codes ORDER BY code_key"
SAMPLE_CUSTOMERS = ("SELECT customer_id FROM {gold}.customer_profile WHERE products_total > 0"
                    " ORDER BY sha2(concat(:seed, ':', customer_id), 256) LIMIT 5000")
SHOW_PROPERTIES = {table: "SHOW TBLPROPERTIES {gold}." + table for table in TABLE_COLUMNS}

# -- Databricks: ops write tables (application state, section 1.5); column maps live in repository.base ---------


def merge_insert(table, key, column_types):
    """MERGE ... WHEN NOT MATCHED THEN INSERT on `key`: a retried insert never duplicates a row."""
    select = ", ".join("CAST(:" + c + " AS " + SPARK_TYPES[t] + ") AS " + c for c, t in column_types.items())
    return ("MERGE INTO {ops}." + table + " AS t USING (SELECT " + select + ") AS s ON t." + key + " = s." + key
            + " WHEN NOT MATCHED THEN INSERT *")


def case_statements(case_columns):
    cols = _cols(case_columns)
    return {
        "insert": merge_insert("dispute_cases", "case_id", case_columns),
        "get": ("SELECT " + cols + " FROM {ops}.dispute_cases"
                " WHERE customer_id = :customer_id AND case_id = :case_id AND env = :env"),
        "list": ("SELECT " + cols + " FROM {ops}.dispute_cases WHERE customer_id = :customer_id AND env = :env"
                 " AND created_via = 'chat' ORDER BY created_at DESC, case_id DESC LIMIT 50"),
        "find_open": ("SELECT " + cols + " FROM {ops}.dispute_cases WHERE customer_id = :customer_id AND env = :env"
                      " AND transaction_id = :transaction_id AND dispute_type = :dispute_type"
                      " AND created_via = 'chat' AND status IN ('Open', 'In Process')"
                      " ORDER BY created_at, case_id LIMIT 1"),
    }


def ticket_statements(ticket_columns):
    cols = _cols(ticket_columns)
    return {
        "insert": merge_insert("handoff_tickets", "ticket_id", ticket_columns),
        "get": "SELECT " + cols + " FROM {ops}.handoff_tickets WHERE ticket_id = :ticket_id AND env = :env",
        "get_for_customer": ("SELECT " + cols + " FROM {ops}.handoff_tickets"
                             " WHERE ticket_id = :ticket_id AND customer_id = :customer_id AND env = :env"),
    }


AUDIT_COLUMN_TYPES = {
    "ts": "timestamp", "recorded_at": "timestamp", "recorded_date": "date", "trace_id": "string",
    "conversation_id": "string", "turn_index": "int", "tool_call_id": "string", "tool": "string", "caller": "string",
    "args_redacted": "string", "outcome": "string", "error_code": "string", "internal_reason": "string",
    "retryable": "boolean", "attempts": "string", "backoff_s": "double", "latency_ms": "int",
    "policy_decision": "string", "result_summary": "string", "session_id_hash": "string", "customer_key": "string",
    "auth_method": "string", "security_events": "string", "faults_injected": "string", "warnings": "string",
    "env": "string", "repository": "string", "service_version": "string", "policy_version": "string",
}


def audit_insert(n_rows):
    """INSERT of n_rows audit records; parameter names are r<i>_<column>."""
    rows = []
    for i in range(n_rows):
        rows.append("(" + ", ".join("CAST(:r" + str(i) + "_" + c + " AS " + SPARK_TYPES[t] + ")"
                                    for c, t in AUDIT_COLUMN_TYPES.items()) + ")")
    return "INSERT INTO {ops}.tool_audit (" + _cols(AUDIT_COLUMN_TYPES) + ") VALUES " + ", ".join(rows)


PURGE_CASES_BY_ENV = "DELETE FROM {ops}.dispute_cases WHERE env = :env"
PURGE_TICKETS_BY_ENV = "DELETE FROM {ops}.handoff_tickets WHERE env = :env"
PURGE_AUDIT_BY_ENV = "DELETE FROM {ops}.tool_audit WHERE env = :env"
PURGE_AUDIT_OLD = "DELETE FROM {ops}.tool_audit WHERE recorded_at < current_date() - INTERVAL 90 DAYS"
VACUUM_AUDIT = "VACUUM {ops}.tool_audit"

# -- SQLite twins (LocalRepository: read-only snapshot + writable store) ----------------------------------------
LOCAL_FIND_CUSTOMER_BY_DOCUMENT = ("SELECT customer_id, customer_status FROM customer_identity"
                                   " WHERE document_type = :document_type AND document_hash = :document_hash LIMIT 2")
LOCAL_GET_CUSTOMER = "SELECT " + _cols(PROFILE_COLUMNS) + " FROM customer_profile WHERE customer_id = :customer_id"
LOCAL_LIST_PRODUCTS = ("SELECT " + _cols(PRODUCT_COLUMNS) + " FROM customer_products"
                       " WHERE customer_id = :customer_id ORDER BY product_type_en, product_id")
LOCAL_GET_PRODUCT = ("SELECT " + _cols(PRODUCT_COLUMNS) + " FROM customer_products"
                     " WHERE customer_id = :customer_id AND product_id = :product_id")
LOCAL_LIST_TRANSACTIONS = ("SELECT " + _cols(TRANSACTION_COLUMNS) + " FROM customer_transactions"
                           " WHERE customer_id = :customer_id AND product_owner_matches = 1 AND event_ts <= :until_ts"
                           " AND (:since_date IS NULL OR event_date >= :since_date)"
                           " ORDER BY event_ts DESC, transaction_id DESC")
LOCAL_GET_TRANSACTION = ("SELECT " + _cols(TRANSACTION_COLUMNS) + " FROM customer_transactions"
                         " WHERE customer_id = :customer_id AND transaction_id = :transaction_id"
                         " AND product_owner_matches = 1 AND event_ts <= :until_ts")
LOCAL_GET_DECLINE_CODE = "SELECT " + _cols(DECLINE_COLUMNS) + " FROM decline_codes WHERE code_key = :code_key"
LOCAL_EXISTS = {
    "transaction": "SELECT 1 FROM customer_transactions WHERE transaction_id = :resource_id"
                   " AND customer_id <> :exclude_customer_id LIMIT 1",
    "product": "SELECT 1 FROM customer_products WHERE product_id = :resource_id"
               " AND customer_id <> :exclude_customer_id LIMIT 1",
}
LOCAL_STORE_EXISTS = {
    "case": "SELECT 1 FROM dispute_cases WHERE case_id = :resource_id"
            " AND (customer_id IS NULL OR customer_id <> :exclude_customer_id) LIMIT 1",
    "ticket": "SELECT 1 FROM handoff_tickets WHERE ticket_id = :resource_id"
              " AND (customer_id IS NULL OR customer_id <> :exclude_customer_id) LIMIT 1",
}


def local_store_statements(case_columns, ticket_columns):
    cc, tc = _cols(case_columns), _cols(ticket_columns)
    return {
        "insert_case": ("INSERT OR IGNORE INTO dispute_cases (" + cc + ") VALUES ("
                        + ", ".join(":" + c for c in case_columns) + ")"),
        "get_case": "SELECT " + cc + " FROM dispute_cases WHERE customer_id = :customer_id AND case_id = :case_id",
        "list_cases": ("SELECT " + cc + " FROM dispute_cases WHERE customer_id = :customer_id AND created_via = 'chat'"
                       " ORDER BY created_at DESC, case_id DESC LIMIT 50"),
        "find_open_case": ("SELECT " + cc + " FROM dispute_cases WHERE customer_id = :customer_id"
                           " AND transaction_id = :transaction_id AND dispute_type = :dispute_type"
                           " AND created_via = 'chat' AND status IN ('Open', 'In Process')"
                           " ORDER BY created_at, case_id LIMIT 1"),
        "insert_ticket": ("INSERT OR IGNORE INTO handoff_tickets (" + tc + ") VALUES ("
                          + ", ".join(":" + c for c in ticket_columns) + ")"),
        "get_ticket": "SELECT " + tc + " FROM handoff_tickets WHERE ticket_id = :ticket_id",
        "get_ticket_for_customer": ("SELECT " + tc + " FROM handoff_tickets"
                                    " WHERE ticket_id = :ticket_id AND customer_id = :customer_id"),
    }


def render(statement, catalog, gold_schema, ops_schema):
    """Place the configured catalog and schema names; nothing else is ever inserted into SQL text."""
    for name in (catalog, gold_schema, ops_schema):
        if not IDENTIFIER.match(name or ""):
            raise ValueError("catalog and schema names must match " + IDENTIFIER.pattern)
    return statement.replace("{gold}", catalog + "." + gold_schema).replace("{ops}", catalog + "." + ops_schema)
