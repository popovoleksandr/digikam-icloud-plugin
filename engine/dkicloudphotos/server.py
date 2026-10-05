"""``serve``: the JSON Lines protocol the digiKam plugin speaks (docs/PROTOCOL.md).

A reader thread takes requests from stdin; quick ones (hello, status,
cancel, shutdown) are answered at once, the rest are queued for a single
worker thread. Everything written to stdout goes through :meth:`send`, one
JSON object per line, so nothing else may print to stdout.
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
from typing import Any, TextIO

from . import PROTOCOL, __version__
from .auth import AuthError, AuthNetworkError, NotSignedIn, WrongCode
from .engine import Engine
from .photos import Cancelled, PhotosError

log = logging.getLogger(__name__)

IMMEDIATE = {"hello", "status", "cancel", "shutdown"}


def error_kind(exc: BaseException) -> str:
    if isinstance(exc, Cancelled):
        return "cancelled"
    if isinstance(exc, WrongCode):
        return "wrong_code"
    if isinstance(exc, NotSignedIn):
        return "not_signed_in"
    if isinstance(exc, AuthNetworkError):
        return "network"
    if isinstance(exc, AuthError):
        return "auth"
    if isinstance(exc, PhotosError):
        return exc.kind
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "bad_request"
    return "error"


def error_message(exc: BaseException, kind: str) -> str:
    if kind == "cancelled":
        return "Cancelled."
    if kind == "bad_request" and isinstance(exc, KeyError):
        return f"Missing argument {exc}."
    text = str(exc).strip()
    if kind == "error" and not isinstance(exc, PhotosError):
        return f"Unexpected error: {exc.__class__.__name__}: {text}" if text else f"Unexpected error: {exc.__class__.__name__}"
    return text or exc.__class__.__name__


class Server:
    def __init__(self, engine: Engine, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        self.engine = engine
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self._out_lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._running_id: int | None = None
        self._stopping = threading.Event()
        # A cancel applies to what was queued before it: each queued request
        # carries the generation it was queued in.
        self._cancel_lock = threading.Lock()
        self._cancel_gen = 0

    # ---------------------------------------------------------------- output

    def send(self, message: dict) -> None:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._out_lock:
            try:
                self.stdout.write(line + "\n")
                self.stdout.flush()
            except (BrokenPipeError, ValueError):
                self._stopping.set()

    def reply(self, rid: Any, result: dict | None = None) -> None:
        self.send({"id": rid, "ok": True, "result": result if result is not None else {}})

    def fail(self, rid: Any, kind: str, message: str) -> None:
        self.send({"id": rid, "ok": False, "error": {"kind": kind, "message": message}})

    def emitter(self, rid: Any):
        def emit(event: str, **fields) -> None:
            self.send({"id": rid, "event": event, **fields})
        return emit

    # ------------------------------------------------------------------ loop

    def run(self) -> int:
        worker = threading.Thread(target=self._work, name="worker", daemon=True)
        worker.start()
        try:
            for line in self.stdin:
                line = line.strip()
                if not line:
                    continue
                self._dispatch(line)
                if self._stopping.is_set():
                    break
        finally:
            self._stopping.set()
            self.engine.cancel.set()
            self._queue.put(None)
            worker.join(timeout=10)
        return 0

    def _dispatch(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            self.fail(None, "bad_request", "Not a JSON line.")
            return
        if not isinstance(msg, dict):
            self.fail(None, "bad_request", "A request must be a JSON object.")
            return
        rid, cmd, args = msg.get("id"), msg.get("cmd"), msg.get("args") or {}
        if not isinstance(cmd, str) or not isinstance(args, dict):
            self.fail(rid, "bad_request", "A request needs a command name and an args object.")
            return
        if cmd == "hello":
            self.reply(rid, {"version": __version__, "protocol": PROTOCOL})
        elif cmd == "status":
            self.reply(rid, self.engine.status())
        elif cmd == "cancel":
            self._cancel_all()
            self.reply(rid)
        elif cmd == "shutdown":
            self._cancel_all()
            self.reply(rid)
            self._stopping.set()
        elif cmd in HANDLERS:
            with self._cancel_lock:
                self._queue.put((rid, cmd, args, self._cancel_gen))
        else:
            self.fail(rid, "bad_request", f"Unknown command {cmd!r}.")

    def _cancel_all(self) -> None:
        with self._cancel_lock:
            self._cancel_gen += 1
            self.engine.cancel.set()

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None or self._stopping.is_set():
                if item is not None:
                    self.fail(item[0], "cancelled", "Cancelled.")
                    continue
                return
            rid, cmd, args, gen = item
            with self._cancel_lock:
                cancelled = gen != self._cancel_gen
                if not cancelled:
                    self.engine.cancel.clear()
                    self._running_id = rid
            if cancelled:
                self.fail(rid, "cancelled", "Cancelled.")
                continue
            try:
                result = HANDLERS[cmd](self.engine, args, self.emitter(rid))
                if self.engine.cancel.is_set() and cmd in ("sync", "upload"):
                    raise Cancelled()
            except BaseException as exc:  # noqa: BLE001 - every failure becomes a reply
                kind = error_kind(exc)
                if kind in ("error", "bad_request") and not isinstance(exc, PhotosError):
                    log.exception("%s failed", cmd)
                else:
                    log.info("%s failed: %s: %s", cmd, kind, exc)
                self.fail(rid, kind, error_message(exc, kind))
            else:
                self.reply(rid, result)
            finally:
                self._running_id = None


def _bool(args: dict, key: str, default: bool) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false.")
    return value


def _sync(engine: Engine, a: dict, emit) -> dict:
    albums = a.get("albums") or ["all"]
    if not isinstance(albums, list) or not all(isinstance(x, str) for x in albums):
        raise ValueError("albums must be a list of album ids.")
    return engine.sync(
        dest=str(a["dest"]),
        library=a.get("library") or None,
        albums=albums,
        layout=str(a.get("layout") or "date"),
        include_videos=_bool(a, "include_videos", True),
        live_photos=_bool(a, "live_photos", True),
        edited=_bool(a, "edited", False),
        album_tags=_bool(a, "album_tags", True),
        redownload_deleted=_bool(a, "redownload_deleted", False),
        limit=int(a.get("limit") or 0),
        dry_run=_bool(a, "dry_run", False),
        emit=emit,
    )


def _upload(engine: Engine, a: dict, emit) -> dict:
    files = a.get("files")
    if not isinstance(files, list) or not all(isinstance(x, str) for x in files):
        raise ValueError("files must be a list of paths.")
    return engine.upload(files, force=_bool(a, "force", False), emit=emit)


HANDLERS = {
    "sign_in": lambda e, a, emit: e.sign_in(str(a["apple_id"]), str(a["password"]),
                                            _bool(a, "china_mainland", False), emit=emit),
    "send_code": lambda e, a, emit: e.send_code(str(a.get("method") or "device"), a.get("phone_id")),
    "verify_code": lambda e, a, emit: e.verify_code(str(a["code"]), emit=emit),
    "sign_out": lambda e, a, emit: e.sign_out(),
    "libraries": lambda e, a, emit: e.libraries(emit=emit),
    "albums": lambda e, a, emit: e.albums(a.get("library") or None, emit=emit),
    "sync": _sync,
    "upload": _upload,
}


def serve() -> int:
    # Line-buffered UTF-8 whatever the locale; the plugin reads lines.
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stdin.reconfigure(encoding="utf-8")
    return Server(Engine()).run()
