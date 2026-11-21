from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CallLog:
    """Every call, spoken turn, tool call, lead and status callback in SQLite.

    Twilio sends no event id on status callbacks, so (call_sid, status) is the delivery key: a retry
    of the same callback is handled once, the next status for the same call is not a duplicate."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(
            """CREATE TABLE IF NOT EXISTS calls (
                call_sid TEXT PRIMARY KEY, direction TEXT, caller TEXT, callee TEXT, purpose TEXT,
                started_utc TEXT, status TEXT, duration_seconds INTEGER, turns INTEGER DEFAULT 0,
                empty_turns INTEGER DEFAULT 0, outcome TEXT, handover INTEGER DEFAULT 0,
                summary_emailed INTEGER DEFAULT 0, ended INTEGER DEFAULT 0, day TEXT, updated_utc TEXT);
               CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT, call_sid TEXT, speaker TEXT, text TEXT,
                confidence REAL, latency_ms INTEGER, at_utc TEXT);
               CREATE TABLE IF NOT EXISTS status_events (
                call_sid TEXT, status TEXT, at_utc TEXT, PRIMARY KEY (call_sid, status));
               CREATE TABLE IF NOT EXISTS tool_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, call_sid TEXT, tool TEXT, arguments TEXT,
                result TEXT, at_utc TEXT);
               CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT, call_sid TEXT, name TEXT, phone TEXT, email TEXT,
                interest TEXT, consent_to_contact INTEGER, at_utc TEXT,
                UNIQUE (call_sid, phone, email));"""
        )
        self._db.commit()

    # calls ---------------------------------------------------------------------------------------
    def _upsert(self, call_sid: str, **fields: Any) -> None:
        fields["updated_utc"] = _now()
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{k}=COALESCE(excluded.{k}, calls.{k})" for k in fields)
        with self._lock:
            self._db.execute(
                f"INSERT INTO calls (call_sid, {cols}) VALUES (?, {marks}) ON CONFLICT(call_sid) DO UPDATE SET {updates}",
                [call_sid, *fields.values()],
            )
            self._db.commit()

    def start(self, call_sid: str, direction: str, caller: str, callee: str, day: str,
              purpose: Optional[str] = None) -> bool:
        """Record a new call. False if this call sid was already started (Twilio retried the webhook)."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO calls (call_sid, direction, caller, callee, purpose, started_utc, day, "
                "updated_utc) VALUES (?,?,?,?,?,?,?,?)",
                (call_sid, direction, caller, callee, purpose, _now(), day, _now()))
            self._db.commit()
            return cur.rowcount == 1

    def mark(self, call_sid: str, **fields: Any) -> None:
        self._upsert(call_sid, **fields)

    def get(self, call_sid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute("SELECT * FROM calls WHERE call_sid=?", (call_sid,))
            row = cur.fetchone()
            return dict(zip([c[0] for c in cur.description], row, strict=True)) if row else None

    def bump(self, call_sid: str, column: str) -> int:
        if column not in ("turns", "empty_turns"):
            raise ValueError(column)
        with self._lock:
            self._db.execute(f"UPDATE calls SET {column} = COALESCE({column}, 0) + 1, updated_utc=? WHERE call_sid=?",
                             (_now(), call_sid))
            self._db.commit()
            row = self._db.execute(f"SELECT {column} FROM calls WHERE call_sid=?", (call_sid,)).fetchone()
            return int(row[0]) if row else 0

    def reset_empty(self, call_sid: str) -> None:
        with self._lock:
            self._db.execute("UPDATE calls SET empty_turns = 0 WHERE call_sid=?", (call_sid,))
            self._db.commit()

    def add_outcome(self, call_sid: Optional[str], text: str, **fields: Any) -> None:
        if not call_sid:
            return
        before = (self.get(call_sid) or {}).get("outcome")
        self.mark(call_sid, outcome=f"{before}; {text}" if before else text, **fields)

    def claim_summary(self, call_sid: str) -> bool:
        """True exactly once per call: whoever gets True sends the summary email."""
        with self._lock:
            cur = self._db.execute("UPDATE calls SET summary_emailed = 1 WHERE call_sid=? AND summary_emailed = 0",
                                   (call_sid,))
            self._db.commit()
            return cur.rowcount == 1

    # turns ---------------------------------------------------------------------------------------
    def add_turn(self, call_sid: str, speaker: str, text: str, confidence: Optional[float] = None,
                 latency_ms: Optional[int] = None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO turns (call_sid, speaker, text, confidence, latency_ms, at_utc) "
                             "VALUES (?,?,?,?,?,?)", (call_sid, speaker, text, confidence, latency_ms, _now()))
            self._db.commit()

    def transcript(self, call_sid: str) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute("SELECT speaker, text, confidence, latency_ms, at_utc FROM turns WHERE call_sid=? "
                                   "ORDER BY id", (call_sid,))
            cols = ["speaker", "text", "confidence", "latency_ms", "at_utc"]
            return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    # status callbacks ----------------------------------------------------------------------------
    def first_status(self, call_sid: str, status: str) -> bool:
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO status_events VALUES (?,?,?)", (call_sid, status, _now()))
            self._db.commit()
            return cur.rowcount == 1

    def forget_status(self, call_sid: str, status: str) -> None:
        """Processing failed after we claimed the delivery: let Twilio's retry try again."""
        with self._lock:
            self._db.execute("DELETE FROM status_events WHERE call_sid=? AND status=?", (call_sid, status))
            self._db.commit()

    # tools and leads -----------------------------------------------------------------------------
    def tool_event(self, call_sid: Optional[str], tool: str, arguments: Dict[str, Any], result: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO tool_events (call_sid, tool, arguments, result, at_utc) VALUES (?,?,?,?,?)",
                (call_sid, tool, json.dumps(arguments), result, _now()),
            )
            self._db.commit()

    def tool_events(self, call_sid: str) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute("SELECT tool, arguments, result, at_utc FROM tool_events WHERE call_sid=? ORDER BY id",
                                   (call_sid,))
            return [dict(zip(["tool", "arguments", "result", "at_utc"], r, strict=True)) for r in cur.fetchall()]

    def add_lead(self, call_sid: Optional[str], name: str, phone: str, email: str, interest: str,
                 consent: bool) -> bool:
        """True if stored, False if the same contact was already captured on this call."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO leads (call_sid, name, phone, email, interest, consent_to_contact, at_utc) "
                "VALUES (?,?,?,?,?,?,?)", (call_sid or "", name, phone, email, interest, int(consent), _now()))
            self._db.commit()
            return cur.rowcount == 1

    def leads(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    # totals for the cost guard -------------------------------------------------------------------
    def minutes_on(self, day: str) -> int:
        """Billed minutes (Twilio rounds each call up to the next whole minute)."""
        with self._lock:
            rows = self._db.execute("SELECT duration_seconds FROM calls WHERE day=? AND duration_seconds IS NOT NULL",
                                    (day,)).fetchall()
        return sum((int(s) + 59) // 60 for (s,) in rows)

    def calls_on(self, day: str) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) FROM calls WHERE day=?", (day,)).fetchone()
            return int(row[0] or 0)

    def calls(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute("SELECT * FROM calls ORDER BY started_utc DESC LIMIT ?", (limit,))
            cols = [c[0] for c in cur.description]
            calls = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        for c in calls:
            c["transcript"] = self.transcript(c["call_sid"])
            c["tool_events"] = self.tool_events(c["call_sid"])
        return calls
