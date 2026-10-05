"""iCloud Photos web service client: what the iCloud website's Photos app
asks of CloudKit.

The library lives in the container com.apple.photos.cloud. Every photo or
video is a pair of records: a CPLMaster (the original file and its
resources) and a CPLAsset (what Photos shows: date, favorite, caption,
edits). The personal library is the zone "PrimarySync" of the private
database; an iCloud Shared Photo Library is a "SharedSync-…" zone of the
shared database.

Lists are "index" queries: the record type names an index (all photos by
date, a smart album, the contents of one album) and the query pages through
it by rank (startRank + direction).

All methods are blocking and meant to run in worker threads.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator
from urllib.parse import quote

import requests

from .auth import AppleAuth, AuthError, AuthNetworkError, NotSignedIn

log = logging.getLogger(__name__)

CONTAINER = "com.apple.photos.cloud"
PRIMARY = "private/PrimarySync"
PAGE_SIZE = 100                     # asset+master pairs per query page
DOWNLOAD_CHUNK = 256 * 1024
PLAIN = "text/plain"                # what the iCloud website sends for its JSON

ROOT_FOLDERS = {"----Root-Folder----", "----Project-Root-Folder----"}

# Smart albums: (id, name, index counted by HyperionIndexCountLookup, smartAlbum filter value)
SMART_ALBUMS = [
    ("smart:FAVORITE", "Favorites", "CPLAssetInSmartAlbumByAssetDate:Favorite", "FAVORITE"),
    ("smart:VIDEO", "Videos", "CPLAssetInSmartAlbumByAssetDate:Video", "VIDEO"),
    ("smart:LIVE", "Live Photos", "CPLAssetInSmartAlbumByAssetDate:Live", "LIVE"),
    ("smart:PANORAMA", "Panoramas", "CPLAssetInSmartAlbumByAssetDate:Panorama", "PANORAMA"),
    ("smart:SCREENSHOT", "Screenshots", "CPLAssetInSmartAlbumByAssetDate:Screenshot", "SCREENSHOT"),
    ("smart:SLOMO", "Slo-mo", "CPLAssetInSmartAlbumByAssetDate:Slomo", "SLOMO"),
    ("smart:TIMELAPSE", "Time-lapse", "CPLAssetInSmartAlbumByAssetDate:Timelapse", "TIMELAPSE"),
]
ALL_INDEX = "CPLAssetByAssetDateWithoutHiddenOrDeleted"
ALL_LIST = "CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted"
SMART_LIST = "CPLAssetAndMasterInSmartAlbumByAssetDate"
ALBUM_LIST = "CPLContainerRelationLiveByAssetDate"

# Fields an asset listing asks for (CloudKit returns only these).
ASSET_KEYS = [
    "recordName", "recordType", "recordChangeTag", "masterRef",
    "filenameEnc", "itemType", "originalOrientation",
    "resOriginalRes", "resOriginalFileType", "resOriginalWidth", "resOriginalHeight",
    "resOriginalVidComplRes", "resOriginalVidComplFileType",
    "resJPEGFullRes", "resJPEGFullFileType",
    "assetDate", "addedDate", "isFavorite", "isHidden", "isDeleted", "captionEnc",
    "adjustmentType", "timeZoneOffset",
]
MEMBER_KEYS = ["recordName", "recordType", "masterRef"]

EXTENSIONS = {
    "public.jpeg": ".JPG",
    "public.heic": ".HEIC",
    "public.heif": ".HEIF",
    "public.png": ".PNG",
    "public.tiff": ".TIF",
    "com.compuserve.gif": ".GIF",
    "com.adobe.raw-image": ".DNG",
    "com.apple.quicktime-movie": ".MOV",
    "public.mpeg-4": ".MP4",
    "public.avi": ".AVI",
}


class PhotosError(Exception):
    kind = "error"

    def __init__(self, message: str, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class OfflineError(PhotosError):
    """The network or the service is unreachable."""
    kind = "network"


class SessionRenewed(OfflineError):
    """The session was renewed while a streamed body was being sent: send it again."""


class AuthRequired(PhotosError):
    """The user must sign in again."""
    kind = "not_signed_in"


class WebAccessRequired(PhotosError):
    """Advanced Data Protection: web access to iCloud Photos is not allowed."""
    kind = "auth"


class Cancelled(Exception):
    pass


@dataclass
class Version:
    """One downloadable file of an asset."""
    url: str
    size: int
    checksum: str
    uti: str


@dataclass
class Asset:
    id: str                     # the CPLMaster record name: stable for the original file
    asset_id: str               # the CPLAsset record name
    filename: str
    uti: str
    kind: str                   # "image" | "video"
    date: float                 # when it was taken (epoch seconds)
    added: float
    favorite: bool = False
    caption: str = ""
    original: Version | None = None
    live: Version | None = None
    edited: Version | None = None
    albums: list[str] = field(default_factory=list)


@dataclass
class Library:
    id: str                     # "private/PrimarySync", "shared/SharedSync-…"
    name: str
    database: str               # "private" | "shared"
    zone: dict


@dataclass
class Album:
    id: str
    name: str
    kind: str                   # "smart" | "album"
    path: str
    index: str                  # what HyperionIndexCountLookup counts
    list_type: str
    filters: list = field(default_factory=list)
    count: int | None = None

    def as_dict(self) -> dict:
        d = {"id": self.id, "name": self.name, "kind": self.kind, "count": self.count}
        if self.kind == "album":
            d["path"] = self.path
        return d


def _field(record: dict, name: str):
    value = (record.get("fields") or {}).get(name)
    return value.get("value") if isinstance(value, dict) else None


def _text(record: dict, name: str) -> str:
    """A string field; "…Enc" fields are base64 unless typed STRING."""
    entry = (record.get("fields") or {}).get(name)
    if not isinstance(entry, dict) or entry.get("value") in (None, ""):
        return ""
    value = entry["value"]
    if entry.get("type") == "STRING" or not name.endswith("Enc"):
        return str(value)
    try:
        return base64.b64decode(value).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


_UNSAFE = re.compile(r'[\x00-\x1f/\\]')


def safe_name(name: str, fallback: str) -> str:
    name = _UNSAFE.sub("_", name or "").strip().lstrip(".")
    return name[:200] or fallback


def extension_for(uti: str, fallback: str = "") -> str:
    return EXTENSIONS.get(uti or "", fallback)


def is_video(uti: str) -> bool:
    uti = uti or ""
    return "movie" in uti or "video" in uti or uti in ("public.mpeg-4", "public.avi", "public.mpeg")


def _version(record: dict, prefix: str) -> Version | None:
    res = _field(record, f"{prefix}Res")
    if not isinstance(res, dict) or not res.get("downloadURL"):
        return None
    return Version(
        url=res["downloadURL"],
        size=int(res.get("size") or 0),
        checksum=str(res.get("fileChecksum") or ""),
        uti=str(_field(record, f"{prefix}FileType") or ""),
    )


def parse_asset(master: dict, asset: dict) -> Asset:
    record_name = master["recordName"]
    uti = str(_field(master, "itemType") or _field(master, "resOriginalFileType") or "")
    original = _version(master, "resOriginal")
    fallback = safe_name(record_name, "photo") + extension_for(uti, ".JPG")
    filename = safe_name(_text(master, "filenameEnc"), fallback)
    date_ms = _field(asset, "assetDate") or _field(asset, "addedDate") or 0
    added_ms = _field(asset, "addedDate") or date_ms
    kind = "video" if is_video(uti) else "image"
    return Asset(
        id=record_name,
        asset_id=asset.get("recordName") or "",
        filename=filename,
        uti=uti,
        kind=kind,
        date=float(date_ms) / 1000.0,
        added=float(added_ms) / 1000.0,
        favorite=bool(_field(asset, "isFavorite")),
        caption=_text(asset, "captionEnc"),
        original=original,
        live=_version(master, "resOriginalVidCompl") if kind == "image" else None,
        # The edited render is a resource of the asset, not of the master.
        edited=_version(asset, "resJPEGFull") if kind == "image" else None,
    )


def _retry_after(r: requests.Response) -> float | None:
    try:
        return float(r.headers.get("Retry-After") or "")
    except ValueError:
        return None


class Photos:
    def __init__(self, auth: AppleAuth) -> None:
        self.auth = auth
        self._web_access_generation = -1

    # ------------------------------------------------------------------ HTTP

    def _root(self, service: str = "ckdatabasews") -> str:
        base = self.auth.service_url(service)
        if not base:
            if not self.auth.has_account:
                raise AuthRequired("Sign in to iCloud again.", 401)
            raise PhotosError(f"iCloud doesn't offer the {service} service for this account")
        return base

    def endpoint(self, database: str) -> str:
        return f"{self._root()}/database/1/{CONTAINER}/production/{database}"

    def request(
        self,
        method: str,
        url: str,
        *,
        body=None,
        data=None,
        params: dict | None = None,
        headers: dict | None = None,
        expect: tuple[int, ...] = (200,),
        retries: int = 4,
        timeout: tuple[float, float] = (20, 120),
        stream: bool = False,
        service: bool = True,
        extra_params: bool = True,
    ) -> requests.Response:
        """A request to an iCloud web service, or to a signed content URL
        (*service* False: no extra parameters, no session to renew)."""
        attempt = 0
        renewed = False
        jar_retry = False
        rewindable = data is None or isinstance(data, (bytes, str))
        while True:
            generation = self.auth.generation
            h = self.auth.service_headers() if service else {}
            payload = data
            if body is not None:
                payload = json.dumps(body)
                h["Content-Type"] = PLAIN
            h.update(headers or {})
            p: dict = {}
            if service and extra_params:
                p.update(self.auth.params)
            p.update(params or {})
            try:
                r = self.auth.http().request(method, url, params=p, data=payload, headers=h,
                                             timeout=timeout, stream=stream, allow_redirects=True)
            except RuntimeError:
                # The shared cookie jar changed while being read: harmless.
                if jar_retry:
                    raise
                jar_retry = True
                continue
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < retries and rewindable:
                    time.sleep(min(2 ** attempt, 30) + random.random())
                    attempt += 1
                    continue
                raise OfflineError(f"Network error: {e.__class__.__name__}") from e
            if service:
                self.auth.note_response(r)
            if r.status_code in expect:
                return r
            if r.status_code in (401, 421, 450) and service and not renewed:
                r.close()
                try:
                    self.auth.refresh_login(generation)
                except NotSignedIn as e:
                    raise AuthRequired(str(e), 401) from e
                except AuthNetworkError as e:
                    raise OfflineError(str(e)) from e
                except AuthError as e:
                    raise PhotosError(str(e), r.status_code) from e
                renewed = True
                if not rewindable:
                    raise SessionRenewed("The session was renewed; try again")
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries and rewindable:
                delay = _retry_after(r) or min(2 ** attempt * 2, 60) + random.random()
                log.info("%s %s -> %s, retrying in %.0fs", method, url.split("?")[0], r.status_code, delay)
                r.close()
                time.sleep(min(delay, 300))
                attempt += 1
                continue
            raise self._error(r, service)

    @staticmethod
    def _error(r: requests.Response, service: bool = True) -> PhotosError:
        code, message = "", ""
        try:
            body = r.json()
            if isinstance(body, dict):
                code = str(body.get("serverErrorCode") or body.get("errorCode") or body.get("error") or "")
                message = str(body.get("reason") or body.get("errorMessage") or body.get("errorReason") or "")
        except ValueError:
            pass
        message = message or f"HTTP {r.status_code} {r.reason or ''}".strip()
        if r.status_code in (401, 421, 450) and service:
            return AuthRequired("Sign in to iCloud again.", r.status_code, code)
        if r.status_code == 423 or code in ("ACCESS_DENIED", "PCS_REQUIRED"):
            return WebAccessRequired("Turn on “Access iCloud Data on the Web” in iCloud settings on your "
                                     "iPhone, iPad or Mac, then try again.", r.status_code, code)
        if r.status_code in (429, 502, 503, 504):
            return OfflineError(message, r.status_code, code)
        return PhotosError(message, r.status_code, code)

    def ck(self, database: str, path: str, body: dict, params: dict | None = None) -> dict:
        """A CloudKit call: POST JSON, read JSON, surface record-level errors."""
        p = {"remapEnums": "true", "getCurrentSyncToken": "true"}
        p.update(params or {})
        r = self.request("POST", f"{self.endpoint(database)}/{path}", body=body, params=p)
        try:
            data = r.json()
        except ValueError as e:
            raise PhotosError(f"iCloud sent an unreadable reply (HTTP {r.status_code})") from e
        if not isinstance(data, dict):
            raise PhotosError("iCloud sent an unexpected reply")
        return data

    def ensure_web_access(self, on_wait: Callable[[str], None] | None = None) -> None:
        """Advanced Data Protection: ask for the Photos keys once per session."""
        if self._web_access_generation == self.auth.generation:
            return
        for attempt in range(2):
            generation = self.auth.generation
            try:
                self.auth.ensure_web_access(on_wait)
                break
            except NotSignedIn as e:
                # The web cookies of a saved session expire within hours:
                # renew them with the session token before giving up.
                if attempt or self.auth.lost:
                    raise AuthRequired(str(e), 401) from e
                try:
                    self.auth.refresh_login(generation)
                except NotSignedIn as e2:
                    raise AuthRequired(str(e2), 401) from e2
                except AuthNetworkError as e2:
                    raise OfflineError(str(e2)) from e2
                except AuthError as e2:
                    raise PhotosError(str(e2)) from e2
            except AuthNetworkError as e:
                raise OfflineError(str(e)) from e
            except AuthError as e:
                raise WebAccessRequired(str(e)) from e
        self._web_access_generation = self.auth.generation

    # ------------------------------------------------------------- libraries

    def libraries(self) -> list[Library]:
        libs = [Library(PRIMARY, "Personal Library", "private", {"zoneName": "PrimarySync"})]
        try:
            data = self.ck("shared", "changes/database", {})
        except (PhotosError, OfflineError) as e:
            log.info("No shared libraries listed: %s", e)
            return libs
        for zone in data.get("zones") or []:
            zid = zone.get("zoneID") or {}
            name = zid.get("zoneName") or ""
            if zone.get("deleted") or not name.startswith("SharedSync"):
                continue
            libs.append(Library(f"shared/{name}", "Shared Library", "shared", zid))
        return libs

    def library(self, library_id: str | None) -> Library:
        library_id = library_id or PRIMARY
        if library_id == PRIMARY:
            return Library(PRIMARY, "Personal Library", "private", {"zoneName": "PrimarySync"})
        for lib in self.libraries():
            if lib.id == library_id:
                return lib
        raise PhotosError(f"The library {library_id} wasn't found in iCloud.")

    def check_indexing(self, lib: Library) -> None:
        data = self.ck(lib.database, "records/query",
                       {"query": {"recordType": "CheckIndexingState"}, "zoneID": lib.zone})
        records = data.get("records") or []
        state = _field(records[0], "state") if records else None
        if state not in (None, "FINISHED"):
            raise PhotosError("iCloud is still preparing your photo library for the web "
                              f"({str(state).lower()}). Try again later.")

    # ---------------------------------------------------------------- albums

    def albums(self, lib: Library, counts: bool = True) -> list[Album]:
        albums = [Album("all", "All Photos", "smart", "All Photos", ALL_INDEX, ALL_LIST)]
        for album_id, name, index, value in SMART_ALBUMS:
            albums.append(Album(album_id, name, "smart", name, index, SMART_LIST, [
                {"fieldName": "smartAlbum", "comparator": "EQUALS", "fieldValue": {"type": "STRING", "value": value}},
            ]))
        albums.extend(self._user_albums(lib))
        if counts:
            self._count(lib, albums)
        return albums

    def _query_all(self, lib: Library, query: dict) -> Iterator[dict]:
        """Every record of a plain (non-index) query, following continuation markers."""
        marker = None
        while True:
            body = {"query": query, "zoneID": lib.zone, "resultsLimit": 200}
            if marker:
                body["continuationMarker"] = marker
            data = self.ck(lib.database, "records/query", body)
            yield from data.get("records") or []
            marker = data.get("continuationMarker")
            if not marker:
                return

    def _user_albums(self, lib: Library) -> list[Album]:
        records = list(self._query_all(lib, {"recordType": "CPLAlbumByPositionLive"}))
        nodes: dict[str, tuple[str, str | None]] = {}
        for rec in records:
            name = rec.get("recordName") or ""
            if not name or name in ROOT_FOLDERS or _field(rec, "isDeleted"):
                continue
            title = _text(rec, "albumNameEnc")
            if not title:
                continue
            nodes[name] = (title, _field(rec, "parentId"))
        parents = {parent for _title, parent in nodes.values() if parent}

        def path_of(name: str) -> str:
            parts, seen = [], set()
            while name in nodes and name not in seen:
                seen.add(name)
                title, parent = nodes[name]
                parts.append(title.replace("/", "∕"))
                name = parent or ""
            return "/".join(reversed(parts))

        out = []
        for rec in records:
            name = rec.get("recordName") or ""
            if name not in nodes:
                continue
            album_type = _field(rec, "albumType")
            if name in parents or album_type == 3:
                continue  # a folder of albums
            title = nodes[name][0]
            out.append(Album(name, title, "album", path_of(name),
                             f"CPLContainerRelationNotDeletedByAssetDate:{name}", ALBUM_LIST, [
                                 {"fieldName": "parentId", "comparator": "EQUALS",
                                  "fieldValue": {"type": "STRING", "value": name}},
                             ]))
        out.sort(key=lambda a: a.path.casefold())
        return out

    def _count(self, lib: Library, albums: list[Album]) -> None:
        batch = [{
            "resultsLimit": 1,
            "query": {
                "filterBy": {"fieldName": "indexCountID", "comparator": "IN",
                             "fieldValue": {"type": "STRING_LIST", "value": [a.index]}},
                "recordType": "HyperionIndexCountLookup",
            },
            "zoneWide": True,
            "zoneID": lib.zone,
        } for a in albums]
        for start in range(0, len(batch), 50):
            try:
                data = self.ck(lib.database, "internal/records/query/batch", {"batch": batch[start:start + 50]})
            except PhotosError as e:
                if isinstance(e, (AuthRequired, OfflineError)):
                    raise
                log.info("Counting albums failed: %s", e)
                return
            for album, result in zip(albums[start:start + 50], data.get("batch") or []):
                records = (result or {}).get("records") or []
                count = _field(records[0], "itemCount") if records else None
                album.count = int(count) if isinstance(count, (int, float)) else None

    def find_album(self, albums: list[Album], key: str) -> Album | None:
        """By id, then by path or name (case-insensitive), for the command line."""
        for a in albums:
            if a.id == key:
                return a
        folded = key.casefold()
        for a in albums:
            if a.path.casefold() == folded:
                return a
        for a in albums:
            if a.name.casefold() == folded:
                return a
        return None

    # ---------------------------------------------------------------- assets

    def _pages(self, lib: Library, album: Album, keys: list[str],
               cancel: threading.Event | None = None) -> Iterator[tuple[dict, dict]]:
        """(master, asset) record pairs of an album, oldest first."""
        offset = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            body = {
                "query": {
                    "filterBy": [
                        {"fieldName": "startRank", "comparator": "EQUALS",
                         "fieldValue": {"type": "INT64", "value": offset}},
                        {"fieldName": "direction", "comparator": "EQUALS",
                         "fieldValue": {"type": "STRING", "value": "ASCENDING"}},
                    ] + list(album.filters),
                    "recordType": album.list_type,
                },
                "resultsLimit": PAGE_SIZE * 2,
                "desiredKeys": keys,
                "zoneID": lib.zone,
            }
            data = self.ck(lib.database, "records/query", body)
            assets: dict[str, dict] = {}
            masters: list[dict] = []
            for rec in data.get("records") or []:
                if rec.get("recordType") == "CPLAsset":
                    ref = _field(rec, "masterRef")
                    if isinstance(ref, dict) and ref.get("recordName"):
                        assets[ref["recordName"]] = rec
                elif rec.get("recordType") == "CPLMaster":
                    masters.append(rec)
            if not masters:
                return
            offset += len(masters)
            for master in masters:
                asset = assets.get(master.get("recordName"))
                if asset is not None:
                    yield master, asset

    def assets(self, lib: Library, album: Album, cancel: threading.Event | None = None) -> Iterator[Asset]:
        for master, asset in self._pages(lib, album, ASSET_KEYS, cancel):
            if _field(asset, "isDeleted") or _field(asset, "isHidden"):
                continue
            try:
                yield parse_asset(master, asset)
            except (KeyError, TypeError, ValueError) as e:
                log.warning("Skipping an unreadable photo record %s: %s", master.get("recordName"), e)

    def members(self, lib: Library, album: Album, cancel: threading.Event | None = None) -> Iterator[str]:
        """Master record names of the photos in an album (light listing)."""
        for master, _asset in self._pages(lib, album, MEMBER_KEYS, cancel):
            yield master["recordName"]

    # ------------------------------------------------------------- transfers

    def download(self, version: Version, filename: str, out, on_bytes: Callable[[int], None] | None = None,
                 cancel: threading.Event | None = None) -> int:
        """Write one file to the open binary *out*; returns the byte count."""
        url = version.url.replace("${f}", quote(filename))
        r = self.request("GET", url, service=False, stream=True, timeout=(20, 300))
        written = 0
        try:
            for chunk in r.iter_content(DOWNLOAD_CHUNK):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if chunk:
                    out.write(chunk)
                    written += len(chunk)
                    if on_bytes:
                        on_bytes(len(chunk))
        except (requests.ConnectionError, requests.Timeout) as e:
            raise OfflineError(f"Network error: {e.__class__.__name__}") from e
        finally:
            r.close()
        if version.size and written != version.size:
            raise PhotosError(f"The download was incomplete ({written} of {version.size} bytes)")
        return written

    def upload(self, path: str, filename: str | None = None) -> str:
        """Add a photo or video to the library; returns its CPLAsset record name."""
        base = self._root("uploadimagews")
        name = filename or os.path.basename(path)
        for attempt in range(2):
            # Streamed from disk: videos can be larger than memory.
            with open(path, "rb") as f:
                try:
                    r = self.request("POST", f"{base}/upload", data=f, extra_params=False, timeout=(20, 900),
                                     params={"filename": name, "dsid": self.auth.state.get("dsid") or ""})
                    break
                except SessionRenewed:
                    if attempt:
                        raise
        try:
            body = r.json()
        except ValueError as e:
            raise PhotosError(f"iCloud sent an unreadable reply to the upload (HTTP {r.status_code})") from e
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            first = errors[0] if isinstance(errors, list) and errors else {}
            message = (first.get("errorMessage") or first.get("reason") or first.get("errorCode")
                       if isinstance(first, dict) else str(first))
            raise PhotosError(str(message or "iCloud refused the file"))
        for rec in (body.get("records") or []) if isinstance(body, dict) else []:
            if rec.get("recordType") == "CPLAsset" and rec.get("recordName"):
                return rec["recordName"]
        raise PhotosError("iCloud didn't confirm the upload")


def media_type(path: str) -> str | None:
    kind, _enc = mimetypes.guess_type(path)
    if kind is None:
        ext = os.path.splitext(path)[1].lower()
        kind = {".heic": "image/heic", ".heif": "image/heif", ".dng": "image/x-adobe-dng",
                ".mov": "video/quicktime", ".m4v": "video/x-m4v"}.get(ext)
    return kind if kind and kind.split("/")[0] in ("image", "video") else None
