"""Where the engine keeps its files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

APP_ID = "digikam-icloud-photos"


class Paths:
    """All on-disk locations. DKICLOUD_HOME relocates everything (tests)."""

    def __init__(self) -> None:
        override = os.environ.get("DKICLOUD_HOME")
        if override:
            self.data_dir = Path(override)
        else:
            base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
            self.data_dir = Path(base) / APP_ID

    @property
    def session_file(self) -> Path:
        return self.data_dir / "session.json"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "engine.log"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "engine.lock"

    def state_file(self, dsid: str) -> Path:
        """One database per Apple Account: what was downloaded and uploaded."""
        return self.data_dir / f"state-{dsid}.db"

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)


def write_private(path: Path, text: str) -> None:
    """Atomically replace *path* with *text*, readable only by the user."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
