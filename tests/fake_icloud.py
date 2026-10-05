"""In-memory imitation of Apple's iCloud web services, for tests.

Implements what the engine uses, with the shapes of the real replies:

* idmsa (/appleauth/auth) and setup (/setup/ws/1): sign-in with an SRP-6a
  server, two-factor codes, trust tokens, accountLogin, Advanced Data
  Protection web access (taken from icloud4linux's fake, same author);
* ckdatabasews: the CloudKit calls of the iCloud website's Photos app
  (CheckIndexingState, album list, index queries by rank for all photos,
  smart albums and album contents, HyperionIndexCountLookup counts, shared
  zones);
* signed content URLs for downloads, and uploadimagews for uploads.

The Library object can be changed directly (under its lock) to simulate
photos taken or deleted on other devices.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from dkicloudphotos import srp  # noqa: E402  (only the group constants and helpers)

APPLE_ID = "user@example.com"
PASSWORD = "correct horse battery staple"
DEVICE_CODE = "123456"
SMS_CODE = "654321"
CK_PREFIX = "/ckdatabasews/database/1/com.apple.photos.cloud/production/"
ALL_LIST = "CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted"
SMART_LIST = "CPLAssetAndMasterInSmartAlbumByAssetDate"
ALBUM_LIST = "CPLContainerRelationLiveByAssetDate"


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class Photo:
    def __init__(self, filename: str, data: bytes, date: float, *, uti: str = "public.jpeg",
                 favorite: bool = False, hidden: bool = False, caption: str = "",
                 live: bytes | None = None, edited: bytes | None = None, plain_name: bool = False) -> None:
        self.master = "M" + uuid.uuid4().hex[:20].upper()
        self.asset = "A" + uuid.uuid4().hex[:20].upper()
        self.filename = filename
        self.data = data
        self.date = date
        self.added = date + 60
        self.uti = uti
        self.favorite = favorite
        self.hidden = hidden
        self.deleted = False
        self.caption = caption
        self.live = live
        self.edited = edited
        self.plain_name = plain_name      # filenameEnc typed STRING instead of base64

    def is_video(self) -> bool:
        return "movie" in self.uti


class Library:
    """The photo library: photos oldest first, albums and folders."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.photos: list[Photo] = []
        self.albums: dict[str, dict] = {}       # recordName -> {name, parent, type, members}
        self.indexing = "FINISHED"
        self.shared_zone: str | None = None
        self.shared_photos: list[Photo] = []

    def add(self, *photos: Photo) -> list[Photo]:
        with self.lock:
            self.photos.extend(photos)
            self.photos.sort(key=lambda p: p.date)
        return list(photos)

    def album(self, name: str, *photos: Photo, parent: str | None = None, folder: bool = False) -> str:
        rid = ("F" if folder else "L") + uuid.uuid4().hex[:12].upper()
        with self.lock:
            self.albums[rid] = {"name": name, "parent": parent, "type": 3 if folder else 0,
                                "members": [p.master for p in photos]}
        return rid

    def by_master(self, master: str) -> Photo | None:
        for p in self.photos + self.shared_photos:
            if p.master == master:
                return p
        return None

class Account:
    """The Apple Account, its password verifier and its sign-in state."""

    def __init__(self) -> None:
        self.apple_id = APPLE_ID
        self.dsid = "8123456789"
        self.salt = os.urandom(16)
        self.iterations = 1000
        self.protocol = "s2k_fo"
        key = srp.derive_password(PASSWORD, self.salt, self.iterations, self.protocol)
        x = int.from_bytes(srp.H(self.salt, srp.H(b":", key)), "big")
        self.verifier = pow(srp.G, x, srp.N)
        self.require_2fa = True
        self.quirk_409 = False          # answer accepted codes with 409 (Apple, 2026)
        self.phones = [{"id": 1, "numberWithDialCode": "+1 (•••) •••-••12", "pushMode": "sms", "nonFTEU": False}]
        self.challenges: dict[str, tuple] = {}
        self.sessions: dict[str, dict] = {}       # idmsa session id -> state
        self.session_tokens: set[str] = set()     # tokens accepted by accountLogin
        self.trust_tokens: set[str] = set()
        self.web_tokens: set[str] = set()         # valid X-APPLE-WEBAUTH-TOKEN values
        self.validate_token = "vt" + secrets.token_hex(6)
        self.pushes = 0
        self.sms: list[int] = []
        self.logins = 0
        self.failed_proofs = 0



class Handler(BaseHTTPRequestHandler):
    library: Library
    account: Account
    state: dict
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002
        pass

    # -- plumbing ---------------------------------------------------------------------------

    def _body(self) -> bytes:
        if getattr(self, "_consumed", False):
            return b""
        self._consumed = True
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json_body(self):
        raw = self._body()
        return json.loads(raw) if raw else None

    def _send(self, status: int, payload=None, raw: bytes | None = None, headers: dict | None = None,
              cookies: list[str] | None = None) -> None:
        self._body()  # never leave an unread request body on a keep-alive connection
        if status >= 400:
            self.state["errors"].append((self.command, self.path.split("?")[0], status, payload))
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else b"")
        self.send_response(status)
        if raw is None and payload is not None:
            self.send_header("Content-Type", "application/json;charset=UTF-8")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _cookies(self) -> dict[str, str]:
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, _, v = part.strip().partition("=")
                out[k] = v.strip('"')
        return out

    def _base(self) -> str:
        return f"http://{self.headers['Host']}"

    def _route(self, method: str) -> None:
        self._consumed = False
        url = urlparse(self.path)
        path = unquote(url.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        with self.library.lock:
            self.state["requests"].append(f"{method} {path}")
            offline = self.state["offline"] and not path.startswith("/_test")
        if offline:
            self.close_connection = True
            self.connection.shutdown(2)
            return
        if path.startswith("/content/"):
            return self._content(url.path[len("/content/"):], query)
        with self.library.lock:
            if path.startswith("/_test/"):
                return self._test(method, path[len("/_test/"):])
            if path.startswith("/appleauth/auth"):
                return self._idmsa(method, path[len("/appleauth/auth"):], query)
            if path.startswith("/setup/ws/1/"):
                return self._setup(method, path[len("/setup/ws/1/"):], query)
            # Everything else needs the iCloud web cookies.
            if self._cookies().get("X-APPLE-WEBAUTH-TOKEN") not in self.account.web_tokens:
                return self._send(401, {"serverErrorCode": "AUTHENTICATION_FAILED", "reason": "no auth"})
            if query.get("dsid") != self.account.dsid:
                return self._send(400, {"serverErrorCode": "BAD_REQUEST", "reason": "missing dsid"})
            if self.state["adp"] and self._cookies().get("X-APPLE-WEBAUTH-PCS-Photos") != "pcs-photos":
                return self._send(423, {"serverErrorCode": "ACCESS_DENIED", "reason": "PCS required"})
            if path.startswith(CK_PREFIX) and method == "POST":
                db, _, action = path[len(CK_PREFIX):].partition("/")
                return self._ck(db, action, query)
            if path == "/uploadimagews/upload" and method == "POST":
                return self._upload(query)
            return self._send(404, {"error": "not found"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    # -- idmsa --------------------------------------------------------------------------------

    def _auth_session(self) -> dict | None:
        return self.account.sessions.get(self.headers.get("X-Apple-ID-Session-Id") or "")

    def _idmsa(self, method: str, action: str, query: dict) -> None:
        acct = self.account
        if action == "/authorize/signin" and method == "GET":
            sid = "sid-" + secrets.token_hex(8)
            acct.sessions[sid] = {"verified": False, "delivery": None}
            return self._send(200, raw=b"<html>sign in</html>",
                              headers={"Content-Type": "text/html", "X-Apple-ID-Session-Id": sid,
                                       "scnt": "scnt-" + secrets.token_hex(4),
                                       "X-Apple-Auth-Attributes": "attrs-" + secrets.token_hex(4)})
        session = self._auth_session()
        if session is None or not self.headers.get("scnt") or self.headers.get("X-Apple-Widget-Key") is None:
            return self._send(403, {"serviceErrors": [{"code": "-20209", "message": "Session missing"}]})
        if action == "/federate" and method == "POST":
            self._json_body()
            return self._send(200, {"federated": False, "showFederatedIdpConfirmation": False})
        if action == "/signin/init" and method == "POST":
            body = self._json_body()
            if body.get("accountName") != acct.apple_id or "s2k_fo" not in body.get("protocols", []):
                return self._send(401, {"serviceErrors": [{"code": "-20101", "message": "Your Apple Account or password was incorrect."}]})
            A = int.from_bytes(base64.b64decode(body["a"]), "big")
            b = int.from_bytes(os.urandom(32), "big")
            k = int.from_bytes(srp.H(srp.to_bytes(srp.N), srp.pad(srp.to_bytes(srp.G))), "big")
            B = (k * acct.verifier + pow(srp.G, b, srp.N)) % srp.N
            c = "c-" + secrets.token_hex(8)
            acct.challenges[c] = (A, b, B, body["accountName"])
            return self._send(200, {"iteration": acct.iterations, "salt": base64.b64encode(acct.salt).decode(),
                                    "protocol": acct.protocol, "b": base64.b64encode(srp.to_bytes(B)).decode(), "c": c})
        if action == "/signin/complete" and method == "POST":
            body = self._json_body()
            A, b, B, name = acct.challenges.pop(body.get("c"), (None,) * 4)
            if A is None:
                return self._send(400, {"serviceErrors": [{"code": "-20751", "message": "Sign-in expired"}]})
            u = int.from_bytes(srp.H(srp.pad(srp.to_bytes(A)), srp.pad(srp.to_bytes(B))), "big")
            S = pow(A * pow(acct.verifier, u, srp.N), b, srp.N)
            K = srp.H(srp.to_bytes(S))
            h_n, h_g = srp.H(srp.to_bytes(srp.N)), srp.H(srp.pad(srp.to_bytes(srp.G)))
            m1 = srp.H(bytes(x ^ y for x, y in zip(h_n, h_g)), srp.H(name.encode()), acct.salt,
                       srp.to_bytes(A), srp.to_bytes(B), K)
            if base64.b64decode(body.get("m1") or "") != m1:
                acct.failed_proofs += 1
                return self._send(401, {"serviceErrors": [{"code": "-20101", "message": "Your Apple Account or password was incorrect."}]})
            trusted = any(t in acct.trust_tokens for t in body.get("trustTokens") or [])
            if trusted or not acct.require_2fa:
                token = "st-" + secrets.token_hex(8)
                acct.session_tokens.add(token)
                session["verified"] = True
                return self._send(200, {"authType": "hsa2"}, headers={"X-Apple-Session-Token": token,
                                                                      "X-Apple-ID-Account-Country": "USA"})
            return self._send(409, {"authType": "hsa2"}, headers={"X-Apple-ID-Account-Country": "USA"})
        if action == "" and method == "GET":
            boot = {"direct": {"authInitialRoute": "auth/twoSV", "hasTrustedDevices": True, "twoSV": {
                "authFactors": ["sms", "robocall", "generatedcode"],
                "phoneNumberVerification": {"trustedPhoneNumber": acct.phones[0], "trustedPhoneNumbers": acct.phones,
                                            "securityCode": {"length": 6}},
            }}}
            html = ('<html><head><script type="application/json" class="boot_args">'
                    + json.dumps(boot) + "</script></head><body></body></html>")
            return self._send(200, raw=html.encode(), headers={"Content-Type": "text/html"})
        if action == "/verify/trusteddevice/securitycode" and method == "PUT":
            acct.pushes += 1
            session["delivery"] = "device"
            return self._send(202, {"trustedDeviceCount": 1, "securityCode": {"length": 6}})
        if action == "/verify/phone" and method == "PUT":
            body = self._json_body()
            acct.sms.append(body["phoneNumber"]["id"])
            session["delivery"] = "sms"
            return self._send(200, {"trustedPhoneNumber": acct.phones[0], "mode": "sms"})
        if action in ("/verify/trusteddevice/securitycode", "/verify/phone/securitycode") and method == "POST":
            body = self._json_body()
            expected = DEVICE_CODE if action.startswith("/verify/trusteddevice") else SMS_CODE
            if action == "/verify/phone/securitycode" and body.get("phoneNumber", {}).get("id") not in acct.sms:
                return self._send(400, {"service_errors": [{"code": "-21669", "message": "No code was sent to this number."}]})
            if (body.get("securityCode") or {}).get("code") != expected:
                return self._send(400, {"service_errors": [{"code": "-21669", "message": "Incorrect verification code."}]})
            session["verified"] = True
            token = "st-" + secrets.token_hex(8)
            acct.session_tokens.add(token)
            return self._send(409 if acct.quirk_409 else 204,
                              {"securityCode": {"valid": True}} if acct.quirk_409 else None,
                              headers={"X-Apple-Session-Token": token})
        if action == "/2sv/trust" and method == "GET":
            if not session.get("verified"):
                return self._send(401, {"serviceErrors": [{"code": "-20101", "message": "Not verified"}]})
            trust = "tt-" + secrets.token_hex(8)
            acct.trust_tokens.add(trust)
            token = "st-" + secrets.token_hex(8)
            acct.session_tokens.add(token)
            return self._send(204, headers={"X-Apple-TwoSV-Trust-Token": trust, "X-Apple-Session-Token": token})
        return self._send(404, {"error": "not found"})


    # -- setup ----------------------------------------------------------------------------------

    def _setup(self, method: str, action: str, query: dict) -> None:
        acct = self.account
        base = self._base()
        if action == "accountLogin":
            body = self._json_body()
            if body.get("dsWebAuthToken") not in acct.session_tokens or not body.get("extended_login"):
                return self._send(421, {"error": "Invalid session token"})
            acct.logins += 1
            web = "v=2:t=" + secrets.token_hex(12)
            acct.web_tokens.add(web)
            return self._send(200, {
                "dsInfo": {"dsid": acct.dsid, "fullName": "Test User", "firstName": "Test", "lastName": "User",
                           "appleId": acct.apple_id, "primaryEmail": acct.apple_id, "hsaVersion": 2},
                "hasMinimumDeviceForPhotosWeb": True,
                "hsaChallengeRequired": False,
                "hsaTrustedBrowser": True,
                "webservices": {
                    "ckdatabasews": {"url": f"{base}/ckdatabasews", "status": "active", "pcsRequired": True},
                    "uploadimagews": {"url": f"{base}/uploadimagews", "status": "active"},
                    "account": {"url": f"{base}/account", "status": "active"},
                },
            }, cookies=[
                f'X-APPLE-WEBAUTH-TOKEN="{web}"; Path=/; HttpOnly',
                f'X-APPLE-WEBAUTH-VALIDATE="v=1:t={acct.validate_token}"; Path=/',
            ])
        if self._cookies().get("X-APPLE-WEBAUTH-TOKEN") not in acct.web_tokens:
            return self._send(421, {"error": "Missing X-APPLE-WEBAUTH-TOKEN cookie"})
        if action == "logout":
            acct.web_tokens.discard(self._cookies().get("X-APPLE-WEBAUTH-TOKEN"))
            return self._send(200, {"success": True})
        if action == "requestWebAccessState":
            return self._send(200, {"isICDRSDisabled": bool(self.state["adp"]), "isDeviceConsentedForPCS": True})
        if action == "requestPCS":
            body = self._json_body() or {}
            self.state["pcs_requests"].append(body.get("appName"))
            if body.get("appName") != "photos":
                return self._send(200, {"status": "failure", "message": "Unknown app"})
            return self._send(200, {"status": "success", "message": "Cookies attached."},
                              cookies=['X-APPLE-WEBAUTH-PCS-Photos="pcs-photos"; Path=/'])
        return self._send(404, {"error": "not found"})


    # -- CloudKit (Photos) ------------------------------------------------------------------------

    def _zone(self, db: str) -> dict:
        if db == "shared":
            return {"zoneName": self.library.shared_zone, "ownerRecordName": "_owner", "zoneType": "REGULAR_CUSTOM_ZONE"}
        return {"zoneName": "PrimarySync", "ownerRecordName": "_me", "zoneType": "REGULAR_CUSTOM_ZONE"}

    def _photos_of(self, db: str, zone: dict) -> list[Photo]:
        if db == "shared":
            if not self.library.shared_zone or zone.get("zoneName") != self.library.shared_zone:
                raise LookupError("zone")
            return self.library.shared_photos
        if zone.get("zoneName") != "PrimarySync":
            raise LookupError("zone")
        return self.library.photos

    def _ck(self, db: str, action: str, query: dict) -> None:
        if query.get("remapEnums") != "true":
            return self._send(400, {"serverErrorCode": "BAD_REQUEST", "reason": "remapEnums missing"})
        body = self._json_body() or {}
        self.state["ck"].append((db, action, body))
        if action == "changes/database":
            zones = []
            if db == "shared" and self.library.shared_zone:
                zones.append({"zoneID": self._zone("shared"), "syncToken": "t", "deleted": False})
            if db == "private":
                zones.append({"zoneID": self._zone("private"), "syncToken": "t"})
            return self._send(200, {"zones": zones, "moreComing": False, "syncToken": "db"})
        if action == "internal/records/query/batch":
            out = []
            for entry in body.get("batch") or []:
                q = entry.get("query") or {}
                ids = ((q.get("filterBy") or {}).get("fieldValue") or {}).get("value") or []
                count = self._count(ids[0] if ids else "")
                out.append({"records": [] if count is None else [{
                    "recordName": "count", "recordType": "HyperionIndexCountLookup",
                    "fields": {"itemCount": {"value": count, "type": "INT64"}}}]})
            return self._send(200, {"batch": out})
        if action != "records/query":
            return self._send(404, {"serverErrorCode": "NOT_FOUND"})
        try:
            photos = self._photos_of(db, body.get("zoneID") or {})
        except LookupError:
            return self._send(404, {"serverErrorCode": "ZONE_NOT_FOUND", "reason": "Zone does not exist"})
        q = body.get("query") or {}
        rtype = q.get("recordType")
        if rtype == "CheckIndexingState":
            return self._send(200, {"records": [{"recordName": "_", "recordType": "CheckIndexingState",
                                                 "fields": {"state": {"value": self.library.indexing, "type": "STRING"}}}]})
        if rtype == "CPLAlbumByPositionLive":
            return self._albums(body)
        filters = {f["fieldName"]: f["fieldValue"]["value"] for f in q.get("filterBy") or []}
        if rtype == ALL_LIST:
            chosen = [p for p in photos if not p.hidden and not p.deleted]
        elif rtype == SMART_LIST:
            smart = filters.get("smartAlbum")
            test = {"FAVORITE": lambda p: p.favorite, "VIDEO": lambda p: p.is_video(),
                    "LIVE": lambda p: p.live is not None}.get(smart, lambda p: False)
            chosen = [p for p in photos if not p.hidden and not p.deleted and test(p)]
        elif rtype == ALBUM_LIST:
            album = self.library.albums.get(filters.get("parentId"))
            if album is None:
                return self._send(400, {"serverErrorCode": "BAD_REQUEST", "reason": "no such album"})
            chosen = [p for p in photos if p.master in album["members"] and not p.deleted]
        else:
            return self._send(400, {"serverErrorCode": "BAD_REQUEST", "reason": f"unknown index {rtype}"})
        if filters.get("direction") != "ASCENDING":
            chosen = list(reversed(chosen))
        start = int(filters.get("startRank") or 0)
        limit = int(body.get("resultsLimit") or 200) // 2
        records = []
        zone = self._zone(db)
        for p in chosen[start:start + limit]:
            if rtype == ALBUM_LIST:
                records.append({"recordName": f"{p.asset}-IN-{filters['parentId']}",
                                "recordType": "CPLContainerRelation", "fields": {}})
            records.append(self._asset_record(p, zone))
            records.append(self._master_record(p, zone))
        self.state["pages"] += 1
        return self._send(200, {"records": records, "syncToken": "x"})

    def _count(self, index: str) -> int | None:
        lib = self.library
        live = [p for p in lib.photos if not p.hidden and not p.deleted]
        if index == "CPLAssetByAssetDateWithoutHiddenOrDeleted":
            return len(live)
        if index == "CPLAssetInSmartAlbumByAssetDate:Favorite":
            return sum(p.favorite for p in live)
        if index == "CPLAssetInSmartAlbumByAssetDate:Video":
            return sum(p.is_video() for p in live)
        if index == "CPLAssetInSmartAlbumByAssetDate:Live":
            return sum(p.live is not None for p in live)
        if index.startswith("CPLContainerRelationNotDeletedByAssetDate:"):
            album = lib.albums.get(index.partition(":")[2])
            return len(album["members"]) if album else None
        if index.startswith("CPLAssetInSmartAlbumByAssetDate:"):
            return 0
        return None

    def _albums(self, body: dict) -> None:
        records = [{"recordName": "----Root-Folder----", "recordType": "CPLAlbum",
                    "fields": {"albumNameEnc": {"value": b64("root"), "type": "ENCRYPTED_BYTES"}}}]
        for rid, a in self.library.albums.items():
            fields = {"albumNameEnc": {"value": b64(a["name"]), "type": "ENCRYPTED_BYTES"},
                      "albumType": {"value": a["type"], "type": "INT64"},
                      "isDeleted": {"value": 0, "type": "INT64"}}
            if a["parent"]:
                fields["parentId"] = {"value": a["parent"], "type": "STRING"}
            records.append({"recordName": rid, "recordType": "CPLAlbum", "fields": fields})
        # Page with continuation markers, a few at a time, to exercise them.
        start = int(body.get("continuationMarker") or 0)
        page = records[start:start + 3]
        reply = {"records": page}
        if start + 3 < len(records):
            reply["continuationMarker"] = str(start + 3)
        return self._send(200, reply)

    def _url(self, p: Photo, role: str) -> str:
        return f"{self._base()}/content/{p.master}/{role}/${{f}}?o=signed"

    def _res(self, p: Photo, role: str, data: bytes) -> dict:
        return {"value": {"size": len(data), "downloadURL": self._url(p, role),
                          "fileChecksum": base64.b64encode(hashlib.sha1(data).digest()).decode()},
                "type": "ASSETID"}

    def _master_record(self, p: Photo, zone: dict) -> dict:
        name = {"value": p.filename, "type": "STRING"} if p.plain_name else \
            {"value": b64(p.filename), "type": "ENCRYPTED_BYTES"}
        fields = {
            "filenameEnc": name,
            "itemType": {"value": p.uti, "type": "STRING"},
            "resOriginalRes": self._res(p, "original", p.data),
            "resOriginalFileType": {"value": p.uti, "type": "STRING"},
            "resOriginalWidth": {"value": 4032, "type": "INT64"},
            "resOriginalHeight": {"value": 3024, "type": "INT64"},
        }
        if p.live is not None:
            fields["resOriginalVidComplRes"] = self._res(p, "live", p.live)
            fields["resOriginalVidComplFileType"] = {"value": "com.apple.quicktime-movie", "type": "STRING"}
        return {"recordName": p.master, "recordType": "CPLMaster", "recordChangeTag": "c1",
                "fields": fields, "zoneID": zone}

    def _asset_record(self, p: Photo, zone: dict) -> dict:
        fields = {
            "masterRef": {"value": {"recordName": p.master, "action": "DELETE_SELF", "zoneID": zone},
                          "type": "REFERENCE"},
            "assetDate": {"value": int(p.date * 1000), "type": "TIMESTAMP"},
            "addedDate": {"value": int(p.added * 1000), "type": "TIMESTAMP"},
            "isFavorite": {"value": 1 if p.favorite else 0, "type": "INT64"},
            "isHidden": {"value": 1 if p.hidden else 0, "type": "INT64"},
        }
        if p.caption:
            fields["captionEnc"] = {"value": b64(p.caption), "type": "ENCRYPTED_BYTES"}
        if p.edited is not None:
            fields["resJPEGFullRes"] = self._res(p, "edited", p.edited)
            fields["resJPEGFullFileType"] = {"value": "public.jpeg", "type": "STRING"}
            fields["adjustmentType"] = {"value": "com.apple.photo", "type": "STRING"}
        return {"recordName": p.asset, "recordType": "CPLAsset", "recordChangeTag": "c2",
                "fields": fields, "zoneID": zone}

    # -- content and uploads ---------------------------------------------------------------------

    def _content(self, rest: str, query: dict) -> None:
        master, _, tail = rest.partition("/")
        role, _, name = tail.partition("/")
        if query.get("o") != "signed":
            return self._send(403, {"error": "unsigned"})
        with self.library.lock:
            p = self.library.by_master(master)
            self.state["downloads"].append((master, role, unquote(name)))
            fail = master in self.state["fail_downloads"]
            delay = self.state["content_delay"]
        if p is None or fail:
            return self._send(404, {"error": "gone"})
        data = {"original": p.data, "live": p.live, "edited": p.edited}.get(role)
        if data is None:
            return self._send(404, {"error": "no such resource"})
        self._body()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        step = max(1, len(data) // 4)
        for i in range(0, len(data), step):
            if delay:
                time.sleep(delay)
            try:
                self.wfile.write(data[i:i + step])
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    def _upload(self, query: dict) -> None:
        raw = self._body()
        name = query.get("filename") or ""
        if set(query) - {"filename", "dsid"}:
            return self._send(400, {"errors": [{"errorCode": "BAD_REQUEST", "errorMessage": "unexpected parameters"}]})
        if not name or not raw:
            return self._send(200, {"errors": [{"errorCode": "BAD_REQUEST", "errorMessage": "Missing file"}]})
        if name in self.state["refuse_uploads"]:
            return self._send(200, {"errors": [{"errorCode": "UNSUPPORTED", "errorMessage": "Unsupported file type"}]})
        uti = "com.apple.quicktime-movie" if name.lower().endswith(".mov") else "public.jpeg"
        p = Photo(name, raw, time.time(), uti=uti)
        self.library.add(p)
        self.state["uploads"].append(name)
        return self._send(200, {"records": [
            {"recordName": p.master, "recordType": "CPLMaster"},
            {"recordName": p.asset, "recordType": "CPLAsset"},
        ]})

    def _test(self, method: str, action: str) -> None:
        body = self._json_body() or {}
        if action == "offline":
            self.state["offline"] = bool(body.get("value"))
            return self._send(200, {})
        if action == "expire_cookies":
            self.account.web_tokens.clear()
            return self._send(200, {})
        if action == "expire_session":
            self.account.web_tokens.clear()
            self.account.session_tokens.clear()
            return self._send(200, {})
        return self._send(404, {"error": "unknown test hook"})


class FakeICloud:
    def __init__(self) -> None:
        self.library = Library()
        self.account = Account()
        self.state = {"requests": [], "errors": [], "offline": False, "ck": [], "pages": 0,
                      "downloads": [], "uploads": [], "fail_downloads": set(), "refuse_uploads": set(),
                      "content_delay": 0.0, "adp": False, "pcs_requests": []}
        handler = type("BoundHandler", (Handler,), {"library": self.library, "account": self.account,
                                                    "state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda request, client_address: None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "FakeICloud":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    fake = FakeICloud().start()
    fake.account.require_2fa = False
    a, b = fake.library.add(Photo("IMG_0001.JPG", b"one" * 1000, 1714557600, favorite=True),
                            Photo("IMG_0002.HEIC", b"two" * 1000, 1717236000, uti="public.heic", live=b"mov" * 500))
    fake.library.album("Trip", a, b)
    print(fake.url, flush=True)
    fake.thread.join()
