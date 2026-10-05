"""What was synced: one SQLite database per Apple Account.

files    one row per file a sync wrote (or found already there): the key is
         "<library>:<master record>:<role>", so a photo is downloaded once
         even if it is in several albums or the sync is run again.
uploads  one row per file uploaded from this computer, by SHA-256, so the
         same picture isn't sent twice.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    key      TEXT PRIMARY KEY,
    library  TEXT NOT NULL,
    master   TEXT NOT NULL,
    role     TEXT NOT NULL,
    path     TEXT NOT NULL,
    size     INTEGER NOT NULL DEFAULT 0,
    checksum TEXT NOT NULL DEFAULT '',
    synced   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS files_path ON files(path);
CREATE TABLE IF NOT EXISTS uploads (
    sha256   TEXT PRIMARY KEY,
    path     TEXT NOT NULL,
    record   TEXT NOT NULL DEFAULT '',
    uploaded REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS uploads_path ON uploads(path);
"""


def file_key(library: str, master: str, role: str) -> str:
    return f"{library}:{master}:{role}"


class State:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ files

    def file(self, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT path, size, checksum FROM files WHERE key = ?", (key,)).fetchone()
        return {"path": row[0], "size": row[1], "checksum": row[2]} if row else None

    def record_file(self, key: str, library: str, master: str, role: str, path: str,
                    size: int, checksum: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO files (key, library, master, role, path, size, checksum, synced)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, library, master, role, path, size, checksum, time.time()),
            )

    def known_paths(self) -> set[str]:
        with self._lock:
            return {row[0] for row in self._db.execute("SELECT path FROM files")}

    def came_from_icloud(self, path: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM files WHERE path = ? LIMIT 1", (path,)).fetchone() is not None

    # ---------------------------------------------------------------- uploads

    def uploaded(self, sha256: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM uploads WHERE sha256 = ?", (sha256,)).fetchone() is not None

    def record_upload(self, sha256: str, path: str, record: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO uploads (sha256, path, record, uploaded) VALUES (?, ?, ?, ?)",
                (sha256, path, record, time.time()),
            )
