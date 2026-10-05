"""The engine: sign-in, listing, sync (iCloud → folder) and upload (files → iCloud).

Used by the protocol server (``serve``, for the digiKam plugin) and by the
command line. Long operations report through an *emit* callback that takes
an event name and its fields, and stop when :attr:`Engine.cancel` is set.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .auth import AppleAuth, AuthError, NotSignedIn
from .paths import Paths
from .photos import (
    PRIMARY, Album, Asset, AuthRequired, Cancelled, Library, OfflineError, Photos, PhotosError, Version,
    extension_for, media_type,
)
from .state import State, file_key

log = logging.getLogger(__name__)

Emit = Callable[..., None]


class Busy(PhotosError):
    """Another engine process is syncing this account."""
    kind = "busy"


def _no_emit(_event: str, **_fields) -> None:
    pass


@dataclass
class Planned:
    asset: Asset
    role: str                   # "original" | "live" | "edited"
    version: Version
    path: Path
    key: str


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def sha256_of(path: str, cancel: threading.Event | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            chunk = f.read(1024 * 1024)
            if not chunk:
                return h.hexdigest()
            h.update(chunk)


class Engine:
    def __init__(self, paths: Paths | None = None) -> None:
        self.paths = paths or Paths()
        self.paths.ensure()
        self.auth = AppleAuth(self.paths.session_file)
        self.photos = Photos(self.auth)
        self.cancel = threading.Event()
        self._state: State | None = None
        self._state_dsid = ""

    # ---------------------------------------------------------------- account

    def status(self) -> dict:
        account = self.auth.account
        return {
            "signed_in": self.auth.signed_in,
            "has_account": self.auth.has_account,
            "apple_id": self.auth.apple_id,
            "name": account.get("name", ""),
            "email": account.get("email", ""),
        }

    def sign_in(self, apple_id: str, password: str, china_mainland: bool = False, emit: Emit = _no_emit) -> dict:
        result = self.auth.sign_in(apple_id, password, china_mainland)
        if result.get("status") == "ok":
            self._after_sign_in(emit)
        return result

    def send_code(self, method: str, phone_id=None) -> dict:
        return self.auth.send_code(method, phone_id)

    def verify_code(self, code: str, emit: Emit = _no_emit) -> dict:
        result = self.auth.verify_code(code)
        self._after_sign_in(emit)
        return result

    def _after_sign_in(self, emit: Emit) -> None:
        self.photos.ensure_web_access(lambda text: emit("message", text=text))

    def sign_out(self) -> dict:
        self.auth.sign_out()
        self._close_state()
        return {}

    def _require_account(self, emit: Emit = _no_emit) -> None:
        if not self.auth.has_account:
            raise AuthRequired("Sign in to iCloud first.", 401)
        if self.auth.lost:
            raise AuthRequired("Your iCloud session expired. Sign in again.", 401)
        self.photos.ensure_web_access(lambda text: emit("message", text=text))

    def state(self) -> State:
        dsid = str(self.auth.state.get("dsid") or "")
        if not dsid:
            raise AuthRequired("Sign in to iCloud first.", 401)
        if self._state is None or self._state_dsid != dsid:
            self._close_state()
            self._state = State(self.paths.state_file(dsid))
            self._state_dsid = dsid
        return self._state

    def close(self) -> None:
        self._close_state()

    def _close_state(self) -> None:
        if self._state is not None:
            self._state.close()
            self._state = None

    @contextlib.contextmanager
    def _exclusive(self):
        """One sync or upload per account at a time, across processes."""
        self.paths.ensure()
        fd = os.open(self.paths.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Busy("Another iCloud Photos sync is running (digiKam or the command line). "
                           "Try again when it has finished.") from None
            yield
        finally:
            os.close(fd)

    # --------------------------------------------------------------- listing

    def libraries(self, emit: Emit = _no_emit) -> dict:
        self._require_account(emit)
        return {"libraries": [{"id": lib.id, "name": lib.name} for lib in self.photos.libraries()]}

    def albums(self, library: str | None = None, emit: Emit = _no_emit) -> dict:
        self._require_account(emit)
        lib = self.photos.library(library)
        self.photos.check_indexing(lib)
        albums = self.photos.albums(lib)
        # Smart albums with nothing in them (no panoramas, say) are just noise.
        return {"albums": [a.as_dict() for a in albums
                           if not (a.kind == "smart" and a.id != "all" and a.count == 0)]}

    # ------------------------------------------------------------------ sync

    def sync(self, dest: str, library: str | None = None, albums: list[str] | None = None,
             layout: str = "date", include_videos: bool = True, live_photos: bool = True,
             edited: bool = False, album_tags: bool = True, redownload_deleted: bool = False,
             limit: int = 0, dry_run: bool = False, emit: Emit = _no_emit) -> dict:
        if not dest or not os.path.isabs(dest):
            raise ValueError("The destination must be an absolute folder path.")
        if layout not in ("date", "flat"):
            raise ValueError(f"Unknown folder layout {layout!r}.")
        self._require_account(emit)
        with self._exclusive():
            return self._sync(Path(dest), library or PRIMARY, albums or ["all"], layout, include_videos,
                              live_photos, edited, album_tags, redownload_deleted, int(limit or 0),
                              dry_run, emit)

    def _sync(self, dest: Path, library_id: str, album_ids: list[str], layout: str, include_videos: bool,
              live_photos: bool, edited: bool, album_tags: bool, redownload_deleted: bool, limit: int,
              dry_run: bool, emit: Emit) -> dict:
        cancel = self.cancel
        self._membership: dict[str, list[str]] = {}
        lib = self.photos.library(library_id)
        self.photos.check_indexing(lib)
        all_albums = self.photos.albums(lib, counts=False)
        chosen: list[Album] = []
        for key in album_ids:
            album = self.photos.find_album(all_albums, key)
            if album is None:
                raise PhotosError(f"There is no album “{key}” in iCloud Photos.")
            if album not in chosen:
                chosen.append(album)

        # 1. What is in the chosen albums (each photo once).
        assets: dict[str, Asset] = {}
        for album in chosen:
            emit("phase", text=f"Listing {album.name}…")
            seen = 0
            for asset in self.photos.assets(lib, album, cancel):
                seen += 1
                if seen % 500 == 0:
                    emit("phase", text=f"Listing {album.name}… {seen}")
                if asset.kind == "video" and not include_videos:
                    continue
                assets.setdefault(asset.id, asset)
        log.info("Sync %s: %d items listed in %s", lib.id, len(assets), ", ".join(a.name for a in chosen))

        # 2. What is missing here.
        state = self.state()
        stats = {"listed": len(assets), "downloaded": 0, "adopted": 0, "skipped": 0,
                 "deleted_locally": 0, "failed": 0, "bytes": 0}
        plan: list[Planned] = []
        adopt: list[Planned] = []
        reserved: set[str] = set()
        known = state.known_paths()
        for asset in sorted(assets.values(), key=lambda a: (a.date, a.id)):
            folder = dest
            if layout == "date":
                when = datetime.fromtimestamp(asset.date) if asset.date > 0 else None
                folder = dest / (f"{when:%Y}" if when else "Unknown date") / (f"{when:%m}" if when else "")
            stem = None
            for role, version in self._versions(asset, live_photos, edited):
                key = file_key(lib.id, asset.id, role)
                row = state.file(key)
                if row is not None:
                    if os.path.exists(row["path"]):
                        stats["skipped"] += 1
                        if role == "original":
                            stem = Path(row["path"]).stem
                        continue
                    if not redownload_deleted:
                        stats["deleted_locally"] += 1
                        continue
                    if row["path"] not in reserved:
                        # Put it back where it was.
                        path = Path(row["path"])
                        if role == "original":
                            stem = path.stem
                        reserved.add(row["path"])
                        plan.append(Planned(asset, role, version, path, key))
                        continue
                path, adopted = self._target(folder, asset, role, version, stem, reserved, known)
                if role == "original":
                    stem = path.stem
                reserved.add(str(path))
                item = Planned(asset, role, version, path, key)
                (adopt if adopted else plan).append(item)
        if limit > 0:
            plan = plan[:limit]

        # 3. Album names for tags, only for what is new.
        if album_tags and (plan or adopt):
            wanted = {p.asset.id for p in plan} | {p.asset.id for p in adopt}
            self._album_membership(lib, all_albums, wanted, emit)

        total_bytes = sum(p.version.size for p in plan)
        emit("total", total=len(plan), bytes=total_bytes)
        if dry_run:
            stats["would_download"] = len(plan)
            stats["would_adopt"] = len(adopt)
            stats["bytes"] = total_bytes
            return stats

        done = 0
        for item in adopt:
            state.record_file(item.key, lib.id, item.asset.id, item.role, str(item.path),
                              item.version.size, item.version.checksum)
            stats["adopted"] += 1
            emit("file", path=str(item.path), status="adopted", done=done, total=len(plan),
                 asset=self._asset_event(item))

        # 4. Download.
        bytes_done = 0
        last_progress = 0.0
        network_failures = 0

        def on_bytes(n: int) -> None:
            nonlocal bytes_done, last_progress
            bytes_done += n
            now = time.monotonic()
            if now - last_progress >= 0.25:
                last_progress = now
                emit("progress", done=done, total=len(plan), bytes_done=bytes_done, bytes_total=total_bytes)

        if plan:
            emit("phase", text=f"Downloading {len(plan)} file{'s' if len(plan) != 1 else ''}…")
        for item in plan:
            if cancel.is_set():
                raise Cancelled()
            try:
                written = self._download(item, on_bytes)
            except Cancelled:
                raise
            except AuthRequired:
                raise
            except (OfflineError, OSError, PhotosError) as e:
                stats["failed"] += 1
                log.warning("Download of %s (%s) failed: %s", item.asset.filename, item.role, e)
                emit("error", filename=item.path.name, message=str(e))
                if isinstance(e, OfflineError):
                    network_failures += 1
                    if network_failures >= 3:
                        raise
                done += 1
                continue
            network_failures = 0
            state.record_file(item.key, lib.id, item.asset.id, item.role, str(item.path),
                              written, item.version.checksum)
            done += 1
            stats["downloaded"] += 1
            stats["bytes"] += written
            emit("file", path=str(item.path), status="downloaded", done=done, total=len(plan),
                 asset=self._asset_event(item))
            emit("progress", done=done, total=len(plan), bytes_done=bytes_done, bytes_total=total_bytes)
        log.info("Sync %s finished: %s", lib.id, stats)
        return stats

    @staticmethod
    def _versions(asset: Asset, live_photos: bool, edited: bool):
        if asset.original is not None:
            yield "original", asset.original
        if live_photos and asset.live is not None:
            yield "live", asset.live
        if edited and asset.edited is not None:
            yield "edited", asset.edited

    @staticmethod
    def _target(folder: Path, asset: Asset, role: str, version: Version, stem: str | None,
                reserved: set[str], known: set[str]) -> tuple[Path, bool]:
        """Where a file goes, and whether an identical-looking file is already there.

        A file of the same name and size that no sync wrote is taken to be
        the same picture (copied here earlier by other means): it is adopted
        instead of downloaded. Otherwise a free name is found: IMG_1.HEIC,
        IMG_1_1.HEIC, IMG_1_2.HEIC… The video of a Live Photo and the edited
        render follow the original's (possibly numbered) name.
        """
        base, ext = os.path.splitext(asset.filename)
        if role == "live":
            base, ext = stem or base, extension_for(version.uti, ".MOV")
        elif role == "edited":
            base, ext = (stem or base) + "-edited", extension_for(version.uti, ext or ".JPG")
        n = 0
        while True:
            name = f"{base}{ext}" if n == 0 else f"{base}_{n}{ext}"
            path = folder / name
            sp = str(path)
            if sp not in reserved and sp not in known:
                try:
                    size = path.stat().st_size
                except FileNotFoundError:
                    return path, False
                except OSError:
                    size = -1
                if version.size and size == version.size:
                    return path, True
            n += 1

    def _album_membership(self, lib: Library, albums: list[Album], wanted: set[str], emit: Emit) -> None:
        user_albums = [a for a in albums if a.kind == "album"]
        if not user_albums:
            return
        emit("phase", text="Reading album names…")
        by_master: dict[str, list[str]] = {}
        for album in user_albums:
            for master in self.photos.members(lib, album, self.cancel):
                if master in wanted:
                    by_master.setdefault(master, []).append(album.path)
        self._membership = by_master

    def _asset_event(self, item: Planned) -> dict:
        a = item.asset
        return {
            "id": a.id,
            "filename": a.filename,
            "date": _iso(a.date) if a.date > 0 else "",
            "favorite": a.favorite,
            "albums": sorted(getattr(self, "_membership", {}).get(a.id, [])),
            "caption": a.caption,
            "kind": "video" if item.role == "live" else a.kind,
            "role": item.role,
        }

    def _download(self, item: Planned, on_bytes: Callable[[int], None]) -> int:
        item.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=item.path.parent, prefix=f".{item.path.name}.", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as out:
                written = self.photos.download(item.version, item.asset.filename, out, on_bytes, self.cancel)
            if item.asset.date > 0:
                os.utime(tmp, (item.asset.date, item.asset.date))
            os.chmod(tmp, 0o666 & ~_UMASK)
            if item.path.exists():
                raise OSError(f"{item.path.name} appeared while downloading; it was left alone")
            os.replace(tmp, item.path)
            return written
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    # ---------------------------------------------------------------- upload

    def upload(self, files: list[str], force: bool = False, emit: Emit = _no_emit) -> dict:
        self._require_account(emit)
        with self._exclusive():
            return self._upload(files, force, emit)

    def _upload(self, files: list[str], force: bool, emit: Emit) -> dict:
        state = self.state()
        stats = {"uploaded": 0, "skipped": 0, "failed": 0}
        total = len(files)
        for done, path in enumerate(files, 1):
            if self.cancel.is_set():
                raise Cancelled()
            status, message = "uploaded", ""
            try:
                if not os.path.isfile(path):
                    status, message = "failed", "File not found"
                elif media_type(path) is None:
                    status, message = "failed", "Not a photo or video"
                else:
                    digest = sha256_of(path, self.cancel)
                    if not force and state.came_from_icloud(os.path.abspath(path)):
                        status, message = "skipped", "Came from iCloud Photos"
                    elif not force and state.uploaded(digest):
                        status, message = "skipped", "Uploaded before"
                    else:
                        record = self.photos.upload(path)
                        state.record_upload(digest, os.path.abspath(path), record)
            except (Cancelled, AuthRequired):
                raise
            except (PhotosError, OSError) as e:
                status, message = "failed", str(e)
                log.warning("Upload of %s failed: %s", path, e)
            stats[status] += 1
            fields = {"path": path, "status": status, "done": done, "total": total}
            if message:
                fields["message"] = message
            emit("file", **fields)
        log.info("Upload finished: %s", stats)
        return stats


def _read_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


_UMASK = _read_umask()   # read once, before any worker thread starts


__all__ = ["Engine", "Busy", "AuthError", "NotSignedIn"]
