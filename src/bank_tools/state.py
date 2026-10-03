"""Conversation state: challenges, sessions, drafts, counters, idempotency records, revocations and the tool-call
index (CONTRACT.md sections 2, 3 and 7). In memory by default, or a SQLite file at BANK_TOOLS_STATE_PATH.

Single-process by design (a documented limitation): values are JSON documents keyed by (namespace, key), with an
optional expiry on the service clock that `purge(now)` enforces (section 7 retention).
"""
import json
import sqlite3
import threading

from .clock import from_epoch, iso


class StateStore:
    def __init__(self, path=None):
        self._lock = threading.RLock()
        self._mem = None
        self._db = None
        if path:
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.execute("CREATE TABLE IF NOT EXISTS state (ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,"
                             " expires_at TEXT, PRIMARY KEY (ns, key))")
            self._db.commit()
        else:
            self._mem = {}

    def get(self, ns, key, default=None):
        with self._lock:
            if self._db is None:
                hit = self._mem.get(ns, {}).get(key)
                return json.loads(hit[0]) if hit else default
            row = self._db.execute("SELECT value FROM state WHERE ns = ? AND key = ?", (ns, key)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, ns, key, value, expires_at=None):
        doc = json.dumps(value, sort_keys=True, default=str)
        exp = iso(expires_at) if expires_at is not None else None
        with self._lock:
            if self._db is None:
                self._mem.setdefault(ns, {})[key] = (doc, exp)
            else:
                self._db.execute("INSERT OR REPLACE INTO state (ns, key, value, expires_at) VALUES (?, ?, ?, ?)",
                                 (ns, key, doc, exp))
                self._db.commit()

    def delete(self, ns, key):
        with self._lock:
            if self._db is None:
                self._mem.get(ns, {}).pop(key, None)
            else:
                self._db.execute("DELETE FROM state WHERE ns = ? AND key = ?", (ns, key))
                self._db.commit()

    def append(self, ns, key, item, expires_at=None):
        with self._lock:
            items = self.get(ns, key, [])
            items.append(item)
            self.put(ns, key, items, expires_at)
            return items

    def purge(self, now):
        """Delete every entry whose expiry is at or before `now` (service clock). Returns the count."""
        cutoff = iso(now)
        with self._lock:
            if self._db is None:
                n = 0
                for space in self._mem.values():
                    for key in [k for k, (_, exp) in space.items() if exp is not None and exp <= cutoff]:
                        del space[key]
                        n += 1
                return n
            cur = self._db.execute("DELETE FROM state WHERE expires_at IS NOT NULL AND expires_at <= ?", (cutoff,))
            self._db.commit()
            return cur.rowcount

    def count(self, ns):
        with self._lock:
            if self._db is None:
                return len(self._mem.get(ns, {}))
            return self._db.execute("SELECT COUNT(*) FROM state WHERE ns = ?", (ns,)).fetchone()[0]


class RateLimiter:
    """Sliding-window counters on the service clock (section 4). A bucket expires with its last stamp's window, so
    purge() drops the counters of ended conversations and sessions."""

    def __init__(self, state):
        self.state = state

    def hit(self, bucket, limit, window_s, now_epoch):
        """Record one call. Returns None when allowed, else the seconds to wait (the call is not recorded)."""
        stamps = self.state.get("rate", bucket, [])
        if window_s:
            stamps = [t for t in stamps if t > now_epoch - window_s]
        if len(stamps) >= limit:
            return max(1, int(stamps[0] + window_s - now_epoch)) if window_s else 3600
        stamps.append(now_epoch)
        self.state.put("rate", bucket, stamps, expires_at=from_epoch(now_epoch + window_s) if window_s else None)
        return None

    def peek(self, bucket, limit, window_s, now_epoch):
        stamps = [t for t in self.state.get("rate", bucket, []) if not window_s or t > now_epoch - window_s]
        return len(stamps) >= limit
