"""Rebuttal's own state: one row per dispute (PayPal's JSON, the plan, the gate verdict, the log)."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .playbook import Settings

SCHEMA = """
create table if not exists cases (
    dispute_id text primary key, dispute text, plan text, checks text, autopilot integer default 0,
    status text, log text, updated_at text);
create table if not exists settings (id integer primary key check (id = 1), body text);
"""

# Case status, as Rebuttal sees it (PayPal's own status is inside the dispute JSON).
INVESTIGATING, READY, SENT, CLOSED, FAILED = "investigating", "ready", "sent", "closed", "failed"


class State:
    def __init__(self, path: str | Path = ":memory:"):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    def upsert_dispute(self, dispute: dict) -> bool:
        """Store the latest dispute JSON. True when the case is new."""
        with self.lock:
            row = self.db.execute("select status from cases where dispute_id = ?",
                                  (dispute["dispute_id"],)).fetchone()
            if row:
                self.db.execute("update cases set dispute = ?, updated_at = ? where dispute_id = ?",
                                (json.dumps(dispute), _now(), dispute["dispute_id"]))
            else:
                self.db.execute("insert into cases (dispute_id, dispute, status, log, updated_at) "
                                "values (?, ?, ?, '[]', ?)",
                                (dispute["dispute_id"], json.dumps(dispute), INVESTIGATING, _now()))
            self.db.commit()
            return row is None

    def set_plan(self, dispute_id: str, plan: dict, checks: list, autopilot: bool, status: str = READY):
        with self.lock:
            self.db.execute("update cases set plan = ?, checks = ?, autopilot = ?, status = ?, updated_at = ? "
                            "where dispute_id = ?",
                            (json.dumps(plan), json.dumps([asdict(c) for c in checks]), int(autopilot),
                             status, _now(), dispute_id))
            self.db.commit()

    def set_status(self, dispute_id: str, status: str, log_line: str | None = None):
        with self.lock:
            row = self.db.execute("select log from cases where dispute_id = ?", (dispute_id,)).fetchone()
            log = json.loads(row["log"] or "[]") if row else []
            if log_line:
                log.append({"at": _now(), "text": log_line})
            self.db.execute("update cases set status = ?, log = ?, updated_at = ? where dispute_id = ?",
                            (status, json.dumps(log), _now(), dispute_id))
            self.db.commit()

    def get(self, dispute_id: str) -> dict | None:
        row = self.db.execute("select * from cases where dispute_id = ?", (dispute_id,)).fetchone()
        return _case(row) if row else None

    def all(self) -> list[dict]:
        return [_case(r) for r in self.db.execute("select * from cases")]

    def settings(self) -> Settings:
        row = self.db.execute("select body from settings where id = 1").fetchone()
        return Settings(**json.loads(row["body"])) if row else Settings()

    def save_settings(self, s: Settings):
        with self.lock:
            self.db.execute("insert or replace into settings values (1, ?)", (json.dumps(asdict(s)),))
            self.db.commit()


def _case(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("dispute", "plan", "checks", "log"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    d["log"] = d["log"] or []
    return d


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
