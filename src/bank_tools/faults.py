"""Deterministic fault injection (CONTRACT.md section 8). The only hook is GuardedRepository.

Scenario `tool_faults` objects are used as they are:
    {"tool": "get_transactions", "type": "timeout", "failing_attempts": 2}
    {"tool": "create_case", "type": "error", "failing_attempts": 99}
    {"tool": "get_transactions", "type": "injected_text", "transaction_id": "TRX-...", "field": "merchant_name", "value": "..."}
    {"tool": "get_products", "type": "unavailable", "on_attempts": [3]}
    {"tool": "get_transactions", "type": "malformed", "transaction_id": "TRX-...", "field": "amount", "value": null}
    {"tool": "get_case", "type": "latency", "latency_ms": 4000}
Attempts are counted per fault op per injector (one injector per conversation or scenario), across tools.
"""
import copy
import os

from .config import FAULT_ENVS
from .errors import RepositoryTimeout, RepositoryUnavailable

FAULT_OPS = ("auth_lookup", "get_customer", "get_products", "get_transactions", "get_decline_codes", "create_case",
             "get_case", "create_ticket", "get_ticket")
FAILING_TYPES = ("timeout", "error", "unavailable")
REWRITE_TYPES = ("injected_text", "malformed")
MATCH_KEYS = ("transaction_id", "product_id", "case_id", "ticket_id")


class InjectedTimeout(RepositoryTimeout):
    pass


class InjectedUnavailable(RepositoryUnavailable):
    pass


class NullFaultInjector:
    active = False

    def next_attempt(self, op):
        return 0

    def before(self, op, attempt, deadline_ms):
        return 0

    def after(self, op, attempt, result):
        return result

    def drain(self):
        return []


class FaultInjector:
    active = True

    def __init__(self, tool_faults, env=None):
        env = env or os.environ.get("BANK_TOOLS_ENV", "dev")
        if env not in FAULT_ENVS:
            raise RuntimeError("fault injection is allowed only when BANK_TOOLS_ENV is test or eval")
        self.faults = []
        for f in tool_faults or []:
            if f.get("tool") not in FAULT_OPS:
                raise ValueError(f"unknown fault op: {f.get('tool')!r}")
            if f.get("type") not in FAILING_TYPES + REWRITE_TYPES + ("latency",):
                raise ValueError(f"unknown fault type: {f.get('type')!r}")
            self.faults.append(dict(f))
        self._attempts = {}
        self._records = []

    def next_attempt(self, op):
        self._attempts[op] = self._attempts.get(op, 0) + 1
        return self._attempts[op]

    def attempts(self, op):
        return self._attempts.get(op, 0)

    @staticmethod
    def _selected(fault, attempt):
        if "on_attempts" in fault:
            return attempt in fault["on_attempts"]
        if "failing_attempts" in fault:
            return attempt <= fault["failing_attempts"]
        return True

    def before(self, op, attempt, deadline_ms):
        """Raise a transient failure for a selected attempt. Returns the simulated latency in ms."""
        latency = 0
        for f in self.faults:
            if f["tool"] != op or not self._selected(f, attempt):
                continue
            if f["type"] == "latency":
                latency += int(f.get("latency_ms", 0))
                self._records.append({"op": op, "type": "latency", "attempt": attempt, "latency_ms": latency})
                if deadline_ms and latency > deadline_ms:
                    raise InjectedTimeout(op)
            elif f["type"] == "timeout":
                self._records.append({"op": op, "type": "timeout", "attempt": attempt,
                                      "simulated_timeout_ms": deadline_ms})
                raise InjectedTimeout(op)
            elif f["type"] in ("error", "unavailable"):
                self._records.append({"op": op, "type": f["type"], "attempt": attempt})
                raise InjectedUnavailable(op)
        return latency

    def after(self, op, attempt, result):
        """Rewrite fields of the returned row(s) (injected_text, malformed)."""
        rewrites = [f for f in self.faults if f["tool"] == op and f["type"] in REWRITE_TYPES
                    and self._selected(f, attempt)]
        if not rewrites or result is None:
            return result
        result = copy.deepcopy(result)
        rows = result if isinstance(result, list) else [result]
        for f in rewrites:
            hit = 0
            for row in rows:
                if not isinstance(row, dict) or f.get("field") is None:
                    continue
                if any(k in f and row.get(k) != f[k] for k in MATCH_KEYS):
                    continue
                row[f["field"]] = f.get("value")
                hit += 1
            if hit:
                self._records.append({"op": op, "type": f["type"], "attempt": attempt, "field": f["field"], "rows": hit})
        return result

    def drain(self):
        out, self._records = self._records, []
        return out
