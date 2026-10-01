"""Repository interface, row normalization and GuardedRepository (CONTRACT.md sections 1.4 and 8).

Both repositories return the same normalized dicts: Gold column names; amounts as float rounded to 2 decimals;
dates 'YYYY-MM-DD'; timestamps 'YYYY-MM-DDTHH:MM:SS' (naive); booleans as bool; nulls as None.
"""
import json
import os
import re
import time

from ..errors import (MalformedRecord, RepositoryTimeout, TransientRepositoryError, Unavailable)
from ..faults import NullFaultInjector
from ..schemas import ToolSchemas

GOLD_SPEC_PATH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "gold",
                                               "gold_tables.json"))
GOLD_TABLES = ("customer_identity", "customer_profile", "customer_products", "customer_transactions", "decline_codes")

with open(GOLD_SPEC_PATH, encoding="utf-8") as _fh:
    GOLD_SPEC = json.load(_fh)

CASE_COLUMNS = {
    "case_id": "string", "created_at": "timestamp", "recorded_at": "timestamp", "customer_id": "string",
    "product_id": "string", "transaction_id": "string", "case_type": "string", "category": "string",
    "subcategory": "string", "dispute_type": "string", "amount": "decimal(18,2)", "currency": "string",
    "amount_usd": "decimal(18,2)", "event_date": "date", "merchant_name": "string", "channel": "string",
    "is_international": "boolean", "suspected_card_compromise": "boolean", "language": "string", "status": "string",
    "priority": "string", "first_response_hours": "int", "first_response_due_at": "timestamp", "created_via": "string",
    "conversation_id": "string", "session_id_hash": "string", "draft_id": "string", "idempotency_key_hash": "string",
    "policy_version": "string", "service_version": "string", "env": "string",
}
TICKET_COLUMNS = {
    "ticket_id": "string", "created_at": "timestamp", "recorded_at": "timestamp", "conversation_id": "string",
    "session_id_hash": "string", "identity_verified": "boolean", "customer_id": "string", "status": "string",
    "reason_code": "string", "reason_check": "string", "queue": "string", "priority": "string",
    "first_response_hours": "int", "language": "string", "request_summary": "string", "agent_reported_json": "string",
    "service_verified_json": "string", "redactions": "int", "policy_version": "string", "service_version": "string",
    "env": "string",
}
COLUMN_TYPES = {t: {c: spec["type"] for c, spec in GOLD_SPEC["tables"][t]["columns"].items()} for t in GOLD_TABLES}
COLUMN_TYPES["dispute_cases"] = CASE_COLUMNS
COLUMN_TYPES["handoff_tickets"] = TICKET_COLUMNS
OPEN_CASE_STATUSES = ("Open", "In Process")


def normalize_value(kind, value):
    if value is None:
        return None
    if kind.startswith("decimal"):
        return round(float(value), 2)
    if kind == "double":
        return float(value)
    if kind in ("bigint", "int"):
        return int(value)
    if kind == "boolean":
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1")
        return bool(value)
    if kind == "date":
        return str(value)[:10]
    if kind == "timestamp":
        return str(value).replace(" ", "T").rstrip("Z")[:19]
    if kind.startswith("array"):
        return list(json.loads(value)) if isinstance(value, str) else list(value)
    return str(value)


def normalize_row(table, row):
    if row is None:
        return None
    types = COLUMN_TYPES[table]
    return {k: normalize_value(types.get(k, "string"), v) for k, v in row.items()}


# ---------------------------------------------------------------------------------------------------------------
# Row validation (section 8): the fields the service uses, against the domains of tool_schemas.json.

_DEFS = ToolSchemas().defs
TXN_PROPS, PRODUCT_PROPS = _DEFS["TransactionView"]["properties"], _DEFS["ProductView"]["properties"]
CASE_PROPS, TICKET_PROPS = _DEFS["CaseView"]["properties"], _DEFS["TicketView"]["properties"]
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CUSTOMER_STATUSES = ("Active", "Inactive", "Suspended", "Closed")
COUNTRIES = ("MX", "CO", "AR")


def _pat(props, key, value):
    return isinstance(value, str) and re.search(props[key]["pattern"], value) is not None


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _in(props, key, value):
    return value in props[key]["enum"]


def valid_transaction(r):
    return (isinstance(r, dict) and _pat(TXN_PROPS, "transaction_id", r.get("transaction_id"))
            and isinstance(r.get("customer_id"), str) and _pat(TXN_PROPS, "product_id", r.get("product_id"))
            and isinstance(r.get("event_ts"), str) and TS.match(r["event_ts"]) is not None
            and isinstance(r.get("event_date"), str) and DATE.match(r["event_date"]) is not None
            and _num(r.get("amount")) and r["amount"] > 0 and _in(TXN_PROPS, "currency", r.get("currency"))
            and _in(TXN_PROPS, "transaction_status", r.get("transaction_status"))
            and _in(TXN_PROPS, "transaction_type", r.get("transaction_type"))
            and _in(TXN_PROPS, "product_type_en", r.get("product_type_en"))
            and _in(TXN_PROPS, "channel", r.get("channel"))
            and _in(TXN_PROPS, "transaction_country_code", r.get("transaction_country_code"))
            and isinstance(r.get("is_international"), bool) and r.get("product_owner_matches") is not False)


def valid_product(r):
    return (isinstance(r, dict) and _pat(PRODUCT_PROPS, "product_id", r.get("product_id"))
            and isinstance(r.get("customer_id"), str) and _in(PRODUCT_PROPS, "product_type_en", r.get("product_type_en"))
            and _in(PRODUCT_PROPS, "currency", r.get("currency"))
            and _in(PRODUCT_PROPS, "effective_status", r.get("effective_status"))
            and isinstance(r.get("is_card"), bool) and (r.get("current_balance") is None or _num(r["current_balance"])))


def valid_case(r):
    return (isinstance(r, dict) and _pat(CASE_PROPS, "case_id", r.get("case_id")) and _in(CASE_PROPS, "status", r.get("status"))
            and isinstance(r.get("customer_id"), str) and _pat(CASE_PROPS, "transaction_id", r.get("transaction_id"))
            and _pat(CASE_PROPS, "product_id", r.get("product_id")) and _num(r.get("amount")) and r["amount"] > 0
            and _in(CASE_PROPS, "currency", r.get("currency")) and _in(CASE_PROPS, "dispute_type", r.get("dispute_type"))
            and _in(CASE_PROPS, "priority", r.get("priority")) and isinstance(r.get("created_at"), str))


def valid_ticket(r):
    return (isinstance(r, dict) and _pat(TICKET_PROPS, "ticket_id", r.get("ticket_id"))
            and _in(TICKET_PROPS, "status", r.get("status")) and _in(TICKET_PROPS, "queue", r.get("queue"))
            and _in(TICKET_PROPS, "reason_code", r.get("reason_code")) and isinstance(r.get("created_at"), str))


def valid_customer(r):
    return (isinstance(r, dict) and isinstance(r.get("customer_id"), str)
            and r.get("customer_status") in CUSTOMER_STATUSES and r.get("country_code") in COUNTRIES)


def valid_identity(r):
    return isinstance(r, dict) and isinstance(r.get("customer_id"), str) and r.get("customer_status") in CUSTOMER_STATUSES


def valid_decline(r):
    return isinstance(r, dict) and isinstance(r.get("code_key"), str)


# ---------------------------------------------------------------------------------------------------------------


class Repository:
    """Interface shared by LocalRepository and DatabricksRepository. Every customer-scoped method filters by
    customer_id in its query."""

    source = "abstract"

    def find_customer_by_document(self, document_type, document_hash):
        raise NotImplementedError

    def get_customer(self, customer_id):
        raise NotImplementedError

    def list_products(self, customer_id):
        raise NotImplementedError

    def get_product(self, customer_id, product_id):
        raise NotImplementedError

    def list_transactions(self, customer_id, until_ts, since_date=None):
        raise NotImplementedError

    def get_transaction(self, customer_id, transaction_id, until_ts):
        raise NotImplementedError

    def get_decline_code(self, code_key):
        raise NotImplementedError

    def insert_case(self, row):
        raise NotImplementedError

    def get_case(self, customer_id, case_id):
        raise NotImplementedError

    def list_cases(self, customer_id, limit):
        raise NotImplementedError

    def find_open_case(self, customer_id, transaction_id, dispute_type):
        raise NotImplementedError

    def insert_ticket(self, row):
        raise NotImplementedError

    def get_ticket(self, ticket_id, customer_id=None):
        raise NotImplementedError

    def resource_exists(self, kind, resource_id, exclude_customer_id=None):
        """Audit labeling only: does the id exist (for a customer other than `exclude_customer_id`)?"""
        raise NotImplementedError

    def health(self):
        raise NotImplementedError


class CallStats:
    """Per tool call: attempts per fault op, backoff, injected faults, warnings, simulated latency."""

    def __init__(self, sleeper):
        self.started = time.monotonic()
        self.sleep_start = sleeper.simulated_s
        self.sleeper = sleeper
        self.attempts = {}
        self.backoff_s = 0.0
        self.faults_injected = []
        self.malformed = 0
        self.simulated_latency_ms = 0

    def elapsed_s(self):
        return time.monotonic() - self.started + (self.sleeper.simulated_s - self.sleep_start)


class GuardedRepository:
    """Bounded retries (policy tool_max_retries and backoff), the fault hook, row validation and the ownership
    assertion around one Repository. Production uses it with NullFaultInjector, so the code paths are identical."""

    def __init__(self, repository, pol, faults=None, sleeper=None, attempt_deadline_s=8.0, call_deadline_s=20.0):
        from ..clock import RealSleeper
        self.repo = repository
        self.faults = faults or NullFaultInjector()
        self.sleeper = sleeper or RealSleeper()
        self.max_attempts = 1 + int(pol["handoff"]["tool_max_retries"])
        self.backoff = list(pol["handoff"]["tool_retry_backoff_seconds"])
        self.attempt_deadline_s = attempt_deadline_s
        self.call_deadline_s = call_deadline_s
        self.stats = CallStats(self.sleeper)

    def begin_call(self):
        self.stats = CallStats(self.sleeper)
        return self.stats

    def _run(self, op, fn):
        stats, uncertain, made = self.stats, False, 0
        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1 and self.call_deadline_s - stats.elapsed_s() < self.attempt_deadline_s:
                break  # not enough time left for another attempt
            made = attempt
            stats.attempts[op] = stats.attempts.get(op, 0) + 1
            n = self.faults.next_attempt(op)
            try:
                stats.simulated_latency_ms += self.faults.before(op, n, int(self.attempt_deadline_s * 1000))
                result = self.faults.after(op, n, fn())
            except TransientRepositoryError as exc:
                uncertain = uncertain or bool(getattr(exc, "maybe_applied", isinstance(exc, RepositoryTimeout)))
                stats.faults_injected.extend(self.faults.drain())
                if attempt < self.max_attempts:
                    delay = self.backoff[min(attempt - 1, len(self.backoff) - 1)]
                    self.sleeper.sleep(delay)
                    stats.backoff_s += delay
                continue
            stats.faults_injected.extend(self.faults.drain())
            return result, False
        return None, ("unknown" if uncertain else "not_written", made)

    def _read(self, op, fn):
        result, failed = self._run(op, fn)
        if failed:
            raise Unavailable(op, failed[1])
        return result

    def _list(self, op, fn, valid, customer_id=None):
        rows = self._read(op, fn) or []
        good = [r for r in rows if valid(r) and (customer_id is None or r.get("customer_id") == customer_id)]
        self.stats.malformed += len(rows) - len(good)
        return good

    def _one(self, op, fn, valid, customer_id=None):
        row = self._read(op, fn)
        if row is None:
            return None
        if customer_id is not None and isinstance(row, dict) and row.get("customer_id") not in (None, customer_id):
            return None  # ownership assertion: a foreign row looks like a missing one
        if not valid(row) or (customer_id is not None and row.get("customer_id") != customer_id):
            raise MalformedRecord(op)
        return row

    def _write(self, op, fn):
        _, failed = self._run(op, fn)
        if failed:
            raise Unavailable(op, failed[1], failed[0])

    # -- reads -----------------------------------------------------------------------------------------------
    def find_customer_by_document(self, document_type, document_hash):
        return self._one("auth_lookup", lambda: self.repo.find_customer_by_document(document_type, document_hash),
                         valid_identity)

    def get_customer(self, customer_id):
        return self._one("get_customer", lambda: self.repo.get_customer(customer_id), valid_customer, customer_id)

    def list_products(self, customer_id):
        return self._list("get_products", lambda: self.repo.list_products(customer_id), valid_product, customer_id)

    def get_product(self, customer_id, product_id):
        return self._one("get_products", lambda: self.repo.get_product(customer_id, product_id), valid_product,
                         customer_id)

    def list_transactions(self, customer_id, until_ts, since_date=None):
        rows = self._list("get_transactions", lambda: self.repo.list_transactions(customer_id, until_ts, since_date),
                          valid_transaction, customer_id)
        return [r for r in rows if r["event_ts"] <= until_ts]

    def get_transaction(self, customer_id, transaction_id, until_ts):
        row = self._one("get_transactions", lambda: self.repo.get_transaction(customer_id, transaction_id, until_ts),
                        valid_transaction, customer_id)
        return row if row is not None and row["event_ts"] <= until_ts else None

    def get_decline_code(self, code_key):
        return self._one("get_decline_codes", lambda: self.repo.get_decline_code(code_key), valid_decline)

    def get_case(self, customer_id, case_id):
        return self._one("get_case", lambda: self.repo.get_case(customer_id, case_id), valid_case, customer_id)

    def list_cases(self, customer_id, limit):
        return self._list("get_case", lambda: self.repo.list_cases(customer_id, limit), valid_case, customer_id)

    def find_open_case(self, customer_id, transaction_id, dispute_type):
        return self._one("get_case", lambda: self.repo.find_open_case(customer_id, transaction_id, dispute_type),
                         valid_case, customer_id)

    def get_ticket(self, ticket_id, customer_id=None):
        return self._one("get_ticket", lambda: self.repo.get_ticket(ticket_id, customer_id), valid_ticket, customer_id)

    # -- writes (insert-if-absent on an id reserved before the first attempt) --------------------------------
    def insert_case(self, row):
        self._write("create_case", lambda: self.repo.insert_case(row))

    def insert_ticket(self, row):
        self._write("create_ticket", lambda: self.repo.insert_ticket(row))
