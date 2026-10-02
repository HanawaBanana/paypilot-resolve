"""SQLite storage: transactions, reservations, and the policy snapshot builder.

The invariant this module protects: a financial operation, its reservation and
its audit record are committed *before* anything touches the network, inside a
single `BEGIN IMMEDIATE` transaction, so two concurrent requests cannot both
observe the same headroom.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

import config
from domain import Approval, Operation, OpState, PolicySnapshot, canonical_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  goal TEXT NOT NULL,
  mode TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS plans (
  run_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  plan_hash TEXT NOT NULL,
  steps_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, version)
);
CREATE TABLE IF NOT EXISTS steps (
  run_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  action TEXT NOT NULL,
  args_json TEXT NOT NULL,
  depends_on TEXT,
  reason TEXT NOT NULL,
  postcondition TEXT,
  state TEXT NOT NULL,
  PRIMARY KEY (run_id, step_id)
);
CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  plan_hash TEXT NOT NULL,
  arguments_hash TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  session_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
  operation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  action TEXT NOT NULL,
  amount_raw TEXT,
  amount_minor INTEGER,
  currency TEXT NOT NULL,
  reason TEXT NOT NULL,
  mode TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  recipient_alias TEXT,
  recipient_hash TEXT,
  resource_id TEXT,
  fingerprint TEXT NOT NULL,
  arguments_hash TEXT NOT NULL,
  request_id TEXT NOT NULL UNIQUE,
  payload_hash TEXT NOT NULL,
  canonical_payload TEXT NOT NULL,
  state TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  platform_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_operations_fingerprint
  ON operations (fingerprint);
CREATE TABLE IF NOT EXISTS reservations (
  operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
  action TEXT NOT NULL,
  amount_minor INTEGER NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  settled_at TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  sent_at TEXT
);
CREATE TABLE IF NOT EXISTS webhook_inbox (
  mode TEXT NOT NULL,
  event_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  payload TEXT NOT NULL,
  received_at TEXT NOT NULL,
  applied_at TEXT,
  PRIMARY KEY (mode, event_id)
);
CREATE TABLE IF NOT EXISTS resources (
  resource_id TEXT PRIMARY KEY,
  parent_id TEXT,
  kind TEXT NOT NULL,
  amount_minor INTEGER,
  currency TEXT,
  status TEXT NOT NULL,
  snapshot_json TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  seq INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  operation_id TEXT,
  event_type TEXT NOT NULL,
  payload TEXT NOT NULL,
  at TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL
);
"""

# Reservation states that still consume authorization.
LIVE_RESERVATION_STATES = (OpState.RESERVED, OpState.DISPATCHED, OpState.UNKNOWN)


class DuplicateFingerprint(Exception):
    """A different operation already owns this business fingerprint."""


def _now_iso(now: Optional[datetime] = None) -> str:
    dt = now or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


class Storage:
    def __init__(self, path: Optional[str] = None):
        self.path = path or config.DB_PATH
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")

    # ----------------------------------------------------------------- plumbing
    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE: writers serialize, so headroom cannot be double-spent."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # --------------------------------------------------------------------- runs
    def create_run(self, run_id: str, goal: str, mode: str, at: Optional[datetime] = None,
                   state: str = "PLANNING") -> None:
        ts = _now_iso(at)
        self.conn.execute(
            "INSERT INTO runs (run_id, goal, mode, state, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)", (run_id, goal, mode, state, ts, ts))

    def get_run(self, run_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    def set_run_state(self, run_id: str, state: str, note: str = "",
                      at: Optional[datetime] = None) -> None:
        self.conn.execute("UPDATE runs SET state=?, note=?, updated_at=? WHERE run_id=?",
                          (state, note, _now_iso(at), run_id))

    def list_runs(self, limit: int = 20) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)))

    # ---------------------------------------------------------------- plans
    def save_plan(self, run_id: str, plan_hash: str, steps: List[Dict[str, Any]],
                  at: Optional[datetime] = None) -> int:
        row = self.conn.execute(
            "SELECT MAX(version) AS v FROM plans WHERE run_id=?", (run_id,)).fetchone()
        version = (row["v"] or 0) + 1
        ts = _now_iso(at)
        self.conn.execute(
            "INSERT INTO plans (run_id, version, plan_hash, steps_json, created_at)"
            " VALUES (?,?,?,?,?)", (run_id, version, plan_hash, canonical_json(steps), ts))
        for seq, step in enumerate(steps, start=1):
            self.conn.execute(
                "INSERT OR REPLACE INTO steps (run_id, step_id, seq, action, args_json,"
                " depends_on, reason, postcondition, state) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, step["step_id"], seq, step["action"], canonical_json(step.get("args", {})),
                 step.get("depends_on"), step.get("reason", ""), step.get("postcondition"),
                 step.get("state", "PENDING")))
        return version

    def get_plan(self, run_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM plans WHERE run_id=? ORDER BY version DESC LIMIT 1",
            (run_id,)).fetchone()

    def get_steps(self, run_id: str) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY seq ASC", (run_id,)))

    def set_step_state(self, run_id: str, step_id: str, state: str) -> None:
        self.conn.execute("UPDATE steps SET state=? WHERE run_id=? AND step_id=?",
                          (state, run_id, step_id))

    # ------------------------------------------------------------- approvals
    def create_approval(self, approval: Approval) -> None:
        self.conn.execute(
            "INSERT INTO approvals (approval_id, run_id, plan_hash, arguments_hash,"
            " actor_id, approved_at, expires_at, session_id) VALUES (?,?,?,?,?,?,?,?)",
            (approval.approval_id, approval.run_id, approval.plan_hash,
             approval.arguments_hash, approval.actor_id, approval.approved_at,
             approval.expires_at, approval.session_id))

    def get_approval(self, run_id: str, arguments_hash: Optional[str] = None) -> Optional[Approval]:
        if arguments_hash:
            row = self.conn.execute(
                "SELECT * FROM approvals WHERE run_id=? AND arguments_hash=?"
                " ORDER BY approved_at DESC LIMIT 1", (run_id, arguments_hash)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM approvals WHERE run_id=? ORDER BY approved_at DESC LIMIT 1",
                (run_id,)).fetchone()
        if not row:
            return None
        return Approval(approval_id=row["approval_id"], run_id=row["run_id"],
                        plan_hash=row["plan_hash"], arguments_hash=row["arguments_hash"],
                        actor_id=row["actor_id"], approved_at=row["approved_at"],
                        expires_at=row["expires_at"], session_id=row["session_id"])

    # ------------------------------------------------------------- operations
    def create_operation(self, operation: Operation, request_id: str, state: str,
                         at: Optional[datetime] = None,
                         amount_minor: Optional[int] = None,
                         allow_duplicate_fingerprint: bool = False) -> None:
        ts = _now_iso(at)
        payload = {
            "action": operation.action, "amount": operation.amount_raw,
            "currency": operation.currency, "recipient_alias": operation.recipient_alias,
            "resource_id": operation.resource_id, "reason": operation.reason,
        }
        try:
            self.conn.execute(
                "INSERT INTO operations (operation_id, run_id, step_id, action, amount_raw,"
                " amount_minor, currency, reason, mode, actor_id, recipient_alias, recipient_hash,"
                " resource_id, fingerprint, arguments_hash, request_id, payload_hash,"
                " canonical_payload, state, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (operation.operation_id, operation.run_id, operation.step_id, operation.action,
                 operation.amount_raw, amount_minor, operation.currency, operation.reason,
                 operation.mode,
                 operation.actor_id, operation.recipient_alias, operation.recipient_hash,
                 operation.resource_id, operation.fingerprint, operation.arguments_hash,
                 request_id, __import__("hashlib").sha256(canonical_json(payload).encode()).hexdigest(),
                 canonical_json(payload), state, ts, ts))
        except sqlite3.IntegrityError as exc:
            if allow_duplicate_fingerprint:
                raise DuplicateFingerprint(str(exc))
            raise

    def get_operation(self, operation_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM operations WHERE operation_id=?",
                                 (operation_id,)).fetchone()

    def find_by_fingerprint(self, fingerprint: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM operations WHERE fingerprint=?",
                                 (fingerprint,)).fetchone()

    def mark_attempt(self, operation_id: str, at: Optional[datetime] = None) -> int:
        self.conn.execute(
            "UPDATE operations SET attempts = attempts + 1, updated_at=? WHERE operation_id=?",
            (_now_iso(at), operation_id))
        row = self.get_operation(operation_id)
        return row["attempts"] if row else 0

    def set_operation_state(self, operation_id: str, state: str,
                            platform_id: Optional[str] = None,
                            at: Optional[datetime] = None) -> None:
        self.conn.execute(
            "UPDATE operations SET state=?, platform_id=COALESCE(?, platform_id), updated_at=?"
            " WHERE operation_id=?", (state, platform_id, _now_iso(at), operation_id))

    def operations_for_run(self, run_id: str) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM operations WHERE run_id=? ORDER BY created_at ASC", (run_id,)))

    # ----------------------------------------------------------- reservations
    def reserve(self, operation_id: str, action: str, amount_minor: int,
                at: Optional[datetime] = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO reservations (operation_id, action, amount_minor,"
            " state, created_at) VALUES (?,?,?,?,?)",
            (operation_id, action, amount_minor, OpState.RESERVED, _now_iso(at)))

    def settle_reservation(self, operation_id: str, final_state: str,
                           at: Optional[datetime] = None) -> None:
        self.conn.execute(
            "UPDATE reservations SET state=?, settled_at=? WHERE operation_id=?",
            (final_state, _now_iso(at), operation_id))

    def exposure(self, action: str, since_iso: str) -> Dict[str, int]:
        """completed / reserved / unknown exposure for one action inside a window."""
        completed = self.conn.execute(
            "SELECT COALESCE(SUM(amount_minor),0) AS s FROM operations"
            " WHERE action=? AND state=? AND created_at>=?",
            (action, OpState.SUCCEEDED, since_iso)).fetchone()["s"]
        reserved = self.conn.execute(
            "SELECT COALESCE(SUM(amount_minor),0) AS s FROM operations"
            " WHERE action=? AND state=? AND created_at>=?",
            (action, OpState.RESERVED, since_iso)).fetchone()["s"]
        dispatched = self.conn.execute(
            "SELECT COALESCE(SUM(amount_minor),0) AS s FROM operations"
            " WHERE action=? AND state=? AND created_at>=?",
            (action, OpState.DISPATCHED, since_iso)).fetchone()["s"]
        unknown = self.conn.execute(
            "SELECT COALESCE(SUM(amount_minor),0) AS s FROM operations"
            " WHERE action=? AND state=? AND created_at>=?",
            (action, OpState.UNKNOWN, since_iso)).fetchone()["s"]
        return {"completed": completed, "reserved": reserved + dispatched,
                "unknown": unknown}

    def recent_operation_count(self, since_iso: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM operations WHERE created_at>=?",
            (since_iso,)).fetchone()["c"]

    # ---------------------------------------------------------------- outbox
    def enqueue(self, operation_id: str, kind: str, payload: Dict[str, Any],
                at: Optional[datetime] = None) -> None:
        self.conn.execute(
            "INSERT INTO outbox (operation_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (operation_id, kind, canonical_json(payload), _now_iso(at)))

    def pending_outbox(self) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id"))

    # ------------------------------------------------------------- resources
    def upsert_resource(self, resource_id: str, kind: str, status: str,
                        parent_id: Optional[str] = None,
                        amount_minor: Optional[int] = None,
                        currency: Optional[str] = None,
                        snapshot: Optional[Dict[str, Any]] = None,
                        at: Optional[datetime] = None) -> None:
        self.conn.execute(
            "INSERT INTO resources (resource_id, parent_id, kind, amount_minor, currency,"
            " status, snapshot_json, updated_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(resource_id) DO UPDATE SET status=excluded.status,"
            " parent_id=COALESCE(excluded.parent_id, resources.parent_id),"
            " snapshot_json=excluded.snapshot_json, updated_at=excluded.updated_at",
            (resource_id, parent_id, kind, amount_minor, currency, status,
             canonical_json(snapshot or {}), _now_iso(at)))

    def get_resource(self, resource_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM resources WHERE resource_id=?",
                                 (resource_id,)).fetchone()

    # -------------------------------------------------- policy snapshot build
    def build_snapshot(self, operation: Operation, now: datetime) -> PolicySnapshot:
        """Server-built, immutable view for the pure evaluator."""
        since = (now - timedelta(seconds=config.WINDOW_24H_SECONDS))
        velocity_since = (now - timedelta(seconds=config.VELOCITY_WINDOW_SECONDS))
        completed: Dict[str, int] = {}
        reserved: Dict[str, int] = {}
        unknown: Dict[str, int] = {}
        for action in config.ALLOWED_ACTIONS:
            exposure = self.exposure(action, _now_iso(since))
            completed[action] = exposure["completed"]
            reserved[action] = exposure["reserved"]
            unknown[action] = exposure["unknown"]
        duplicate = self.find_by_fingerprint(operation.fingerprint)
        duplicate_id = None
        conflict = False
        if duplicate and duplicate["operation_id"] != operation.operation_id:
            same_arguments = duplicate["arguments_hash"] == operation.arguments_hash
            duplicate_id = duplicate["operation_id"] if same_arguments else None
            conflict = not same_arguments
        row = self.conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM audit_events").fetchone()
        return PolicySnapshot(
            policy_version=config.POLICY_VERSION,
            mode=operation.mode,
            snapshot_version=row["s"] + 1,
            now_iso=_now_iso(now),
            window_24h_start_iso=_now_iso(since),
            velocity_window_start_iso=_now_iso(velocity_since),
            completed_24h_minor=completed,
            reserved_24h_minor=reserved,
            unknown_24h_minor=unknown,
            recent_operation_count=self.recent_operation_count(_now_iso(velocity_since)),
            duplicate_operation_id=duplicate_id,
            duplicate_conflict=conflict,
            allowlist=self.allowlist(),
        )

    @staticmethod
    def allowlist(path: Optional[str] = None) -> Dict[str, str]:
        """alias -> recipient hash, from the operator-controlled registry."""
        import hashlib
        import os
        target = path or config.RECIPIENTS_PATH
        if not os.path.exists(target):
            return {}
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        out: Dict[str, str] = {}
        for alias, entry in (data.get("recipients") or {}).items():
            if not entry.get("enabled", True):
                continue
            address = (entry.get("address") or "").strip().lower()
            out[alias] = hashlib.sha256(address.encode("utf-8")).hexdigest()
        return out

    def resolve_recipient(self, alias: str, path: Optional[str] = None) -> Optional[str]:
        """Alias -> real address. Only the executor calls this, only at dispatch."""
        import os
        target = path or config.RECIPIENTS_PATH
        if not os.path.exists(target):
            return None
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        entry = (data.get("recipients") or {}).get(alias)
        if not entry or not entry.get("enabled", True):
            return None
        return entry.get("address")
