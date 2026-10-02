"""Append-only, hash-chained audit log.

Each event carries its sequence number, payload and the hash of the previous
event, so an edit to any earlier row breaks the chain. This detects tampering;
it is not tamper-proof against someone with database access (documented).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from domain import canonical_json, sha256_hex


def append_event(conn: sqlite3.Connection, run_id: str, event_type: str,
                 payload: Dict[str, Any], at_iso: str,
                 operation_id: Optional[str] = None) -> Dict[str, Any]:
    row = conn.execute(
        "SELECT seq, event_hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
    seq = (row["seq"] + 1) if row else 1
    prev_hash = row["event_hash"] if row else ""
    body = {
        "seq": seq,
        "run_id": run_id,
        "operation_id": operation_id,
        "event_type": event_type,
        "payload": payload,
        "at": at_iso,
        "prev_hash": prev_hash,
    }
    event_hash = sha256_hex(canonical_json(body))
    conn.execute(
        "INSERT INTO audit_events (seq, run_id, operation_id, event_type, payload, at,"
        " prev_hash, event_hash) VALUES (?,?,?,?,?,?,?,?)",
        (seq, run_id, operation_id, event_type, canonical_json(payload), at_iso,
         prev_hash, event_hash))
    return {"seq": seq, "event_hash": event_hash, "event": body}


def verify_chain(conn: sqlite3.Connection) -> Tuple[bool, Optional[int]]:
    """Returns (intact, first_broken_seq)."""
    prev = ""
    for row in conn.execute("SELECT * FROM audit_events ORDER BY seq ASC"):
        body = {
            "seq": row["seq"],
            "run_id": row["run_id"],
            "operation_id": row["operation_id"],
            "event_type": row["event_type"],
            "payload": __import__("json").loads(row["payload"]),
            "at": row["at"],
            "prev_hash": prev,
        }
        if row["prev_hash"] != prev or sha256_hex(canonical_json(body)) != row["event_hash"]:
            return False, row["seq"]
        prev = row["event_hash"]
    return True, None


def events_for(conn: sqlite3.Connection, run_id: str) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM audit_events WHERE run_id=? ORDER BY seq ASC", (run_id,))]
