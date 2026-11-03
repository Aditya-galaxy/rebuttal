"""A SQLite connection that several threads can share safely.

Investigations run in parallel threads against one connection. sqlite3 lets a connection cross
threads (check_same_thread=False) but not be used by two at once: interleaved cursors can hand one
thread another's rows. Every statement here runs and fetches all its rows under one lock.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class Rows(list):
    """Fetched rows, with the cursor methods the callers use."""

    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


class SharedDB:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()

    def execute(self, sql: str, params: tuple = ()) -> Rows:
        with self.lock:
            return Rows(self.conn.execute(sql, params).fetchall())

    def executescript(self, sql: str) -> None:
        with self.lock:
            self.conn.executescript(sql)

    def commit(self) -> None:
        with self.lock:
            self.conn.commit()

    @property
    def total_changes(self) -> int:
        return self.conn.total_changes

    def backup(self, target: sqlite3.Connection) -> None:
        with self.lock:
            self.conn.backup(target)
