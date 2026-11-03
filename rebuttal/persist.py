"""Keep the SQLite files on a mounted bucket, for hosts with no lasting disk (Cloud Run).

The databases live on local disk, where SQLite is happy. On start they are restored from the
snapshot folder; a background thread writes a consistent copy back whenever they change, and
once more at shutdown. A demo's few writes a minute make this cheap.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("rebuttal")


def restore(snapshot_dir: Path, data_dir: Path, names: list[str]) -> None:
    for name in names:
        src, dst = snapshot_dir / name, data_dir / name
        if src.exists() and not dst.exists():
            shutil.copyfile(src, dst)
            log.info("restored %s from %s", name, snapshot_dir)


class Snapshotter:
    def __init__(self, snapshot_dir: Path, dbs: dict[str, sqlite3.Connection], every: float = 15.0):
        self.dir, self.dbs, self.every = snapshot_dir, dbs, every
        self._seen = {name: -1 for name in dbs}
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="snapshot").start()

    def _run(self) -> None:
        while not self._stop.wait(self.every):
            self.flush()

    def flush(self) -> None:
        with self._lock:
            for name, conn in self.dbs.items():
                if conn.total_changes == self._seen[name]:
                    continue
                try:
                    tmp = Path(tempfile.gettempdir()) / f"snap-{name}"
                    dst = sqlite3.connect(tmp)
                    conn.backup(dst)
                    dst.close()
                    shutil.copyfile(tmp, self.dir / name)
                    self._seen[name] = conn.total_changes
                except Exception:  # a failed snapshot is retried next round
                    log.exception("snapshot of %s failed", name)

    def stop(self) -> None:
        self._stop.set()
        self.flush()
