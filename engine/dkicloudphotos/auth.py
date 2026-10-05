"""Apple Account sign-in for the iCloud web services.

Adapted from icloud4linux (same author, GPL-3.0): identical sign-in, but it
asks for the iCloud Photos services instead of iCloud Drive.

This is what www.icloud.com does when you sign in:

1. idmsa.apple.com: authorize/signin, federate, then an SRP-6a handshake
   (signin/init, signin/complete). The password never leaves this machine;
   only proofs derived from it do.
2. If Apple wants a second factor (HTTP 409): a six-digit code shown on a
   trusted Apple device or sent by SMS, then 2sv/trust, which yields a trust
   token so later sign-ins skip the code for a while.
3. setup.icloud.com accountLogin with the session token: this sets the
   iCloud cookies and lists the web services (iCloud Photos is CloudKit on
   the "ckdatabasews" host; uploads go to "uploadimagews").

Only the outcome is stored (cookies and tokens, in session.json, readable by
the user alone); the password is not stored anywhere.

All methods block and are meant to run in worker threads.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from requests.cookies import RequestsCookieJar, create_cookie

from . import srp
from .paths import write_private

log = logging.getLogger(__name__)

# The iCloud website's registration with Apple's identity service.
WIDGET_KEY = "d39ba9916b7251055b22c7f910e2ea796ee65e98b2ddecea8f5dde8d9d1a815d"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.3.1 Safari/605.1.15"
)
CLIENT_BUILD_NUMBER = "2534Project66"
CLIENT_MASTERING_NUMBER = "2534B22"
PCS_COOKIE = "X-APPLE-WEBAUTH-PCS-Photos"
VALIDATE_COOKIE = "X-APPLE-WEBAUTH-VALIDATE"

# Response headers carrying sign-in state, and where we keep each one.
HEADER_FIELDS = {
    "X-Apple-ID-Account-Country": "account_country",
    "X-Apple-ID-Session-Id": "session_id",
    "X-Apple-Session-Token": "session_token",
    "X-Apple-TwoSV-Trust-Token": "trust_token",
    "X-Apple-Auth-Attributes": "auth_attributes",
    "scnt": "scnt",
}


class AuthError(Exception):
    """Sign-in failed; the message is meant for the user."""


class NotSignedIn(AuthError):
    """The stored session no longer works: the user has to sign in again."""


class AuthNetworkError(AuthError):
    """Apple's servers could not be reached."""


class WrongCode(AuthError):
    """The verification code was not accepted."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _apple_message(r: requests.Response, fallback: str) -> str:
    """The user-facing message Apple put in an error reply, if any."""
    try:
        body = r.json()
    except ValueError:
        return fallback
    if isinstance(body, dict):
        for key in ("serviceErrors", "service_errors", "validationErrors"):
            errors = body.get(key)
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                message = errors[0].get("message") or errors[0].get("title")
                if message:
                    return str(message)
        for key in ("errorMessage", "reason", "error"):
            if isinstance(body.get(key), str) and body[key]:
                return body[key]
    return fallback


class Endpoints:
    def __init__(self, china_mainland: bool = False) -> None:
        test = os.environ.get("DKICLOUD_TEST_SERVER")
        if test:
            test = test.rstrip("/")
            self.idmsa, self.home, self.setup = test, test, f"{test}/setup/ws/1"
        else:
            cn = ".cn" if china_mainland else ""
            self.idmsa = "https://idmsa.apple.com"
            self.home = f"https://www.icloud.com{cn}"
            self.setup = f"https://setup.icloud.com{cn}/setup/ws/1"
        self.auth = f"{self.idmsa}/appleauth/auth"


def parse_twofactor_options(root: dict) -> dict:
    """How a pending two-factor challenge can be answered.

    Apple puts this in the JSON of the sign-in page (``boot_args``), nested
    under ``direct.twoSV``, or in a flatter JSON reply; both are accepted.
    """
    direct = root.get("direct") if isinstance(root.get("direct"), dict) else root
    two_sv = direct.get("twoSV") if isinstance(direct.get("twoSV"), dict) else direct
    bridge = two_sv.get("bridgeInitiateData") if isinstance(two_sv.get("bridgeInitiateData"), dict) else {}
    phones: list[dict] = []

    def add(value: Any) -> None:
        if not isinstance(value, dict) or "id" not in value:
            return
        if any(p["id"] == value["id"] for p in phones):
            return
        phones.append({
            "id": value["id"],
            "number": value.get("numberWithDialCode") or value.get("obfuscatedNumber") or f"phone {value['id']}",
            "mode": value.get("pushMode") or "sms",
            "nonFTEU": value.get("nonFTEU"),
        })

    for source in (two_sv.get("phoneNumberVerification"), bridge.get("phoneNumberVerification"),
                   root.get("phoneNumberVerification"), root):
        if isinstance(source, dict):
            add(source.get("trustedPhoneNumber"))
            for phone in source.get("trustedPhoneNumbers") or []:
                add(phone)

    if "hasTrustedDevices" in direct or "hasTrustedDevices" in root:
        has_devices = bool(direct.get("hasTrustedDevices") or root.get("hasTrustedDevices"))
    elif "noTrustedDevices" in root:
        has_devices = not root.get("noTrustedDevices")
    elif "trustedDeviceCount" in root:
        has_devices = bool(root.get("trustedDeviceCount"))
    else:
        has_devices = True
    code = root.get("securityCode") if isinstance(root.get("securityCode"), dict) else {}
    return {
        "phones": phones,
        "has_devices": has_devices,
        # Apple's newer websocket "bridge" push is not implemented: on such
        # accounts a code may only arrive by SMS or from the device settings.
        "bridge": bool(bridge) and direct.get("authInitialRoute") == "auth/bridge/step",
        "security_key": bool(root.get("fsaChallenge") or root.get("keyNames")),
        "length": int(code.get("length") or 6),
    }


_BOOT_ARGS = re.compile(r"<script[^>]*class=[\"'][^\"']*\bboot_args\b[^\"']*[\"'][^>]*>(.*?)</script", re.S)


def parse_boot_args(html: str) -> dict:
    m = _BOOT_ARGS.search(html or "")
    if not m:
        return {}
    try:
        value = json.loads(m.group(1).strip())
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class AppleAuth:
    def __init__(self, session_file: Path) -> None:
        self.session_file = Path(session_file)
        self.jar = RequestsCookieJar()
        self._lock = threading.RLock()
        self._login_lock = threading.Lock()
        self._local = threading.local()
        self.state: dict[str, Any] = {}
        self.generation = 0          # bumped whenever the cookies are renewed
        self._pending: dict[str, Any] | None = None   # a sign-in waiting for its code
        self._load()
        self.endpoints = Endpoints(bool(self.state.get("china_mainland")))

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        cookies = data.pop("cookies", []) or []
        self.state = data
        if not self.state.get("client_id"):
            self.state["client_id"] = str(uuid.uuid4()).lower()
        for c in cookies:
            try:
                self.jar.set_cookie(create_cookie(
                    c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"),
                    expires=c.get("expires"), secure=bool(c.get("secure")), rest=c.get("rest") or {},
                ))
            except (KeyError, TypeError):
                continue

    def save(self) -> None:
        with self._lock:
            data = dict(self.state)
            cookies = []
            with self.jar._cookies_lock:  # noqa: SLF001 - the jar's own lock
                for c in list(self.jar):
                    cookies.append({
                        "name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                        "expires": c.expires, "secure": c.secure, "rest": dict(c._rest),  # noqa: SLF001
                    })
            data["cookies"] = cookies
        write_private(self.session_file, json.dumps(data))

    # ------------------------------------------------------------- properties

    @property
    def client_id(self) -> str:
        return self.state["client_id"]

    @property
    def has_account(self) -> bool:
        """Someone signed in on this computer (the session may have expired since)."""
        with self._lock:
            return bool(self.state.get("dsid") and self.state.get("webservices"))

    @property
    def signed_in(self) -> bool:
        return self.has_account and not self.lost

    @property
    def lost(self) -> bool:
        """The session stopped working: the user has to sign in again."""
        return bool(self.state.get("expired"))

    @property
    def account(self) -> dict:
        with self._lock:
            return dict(self.state.get("account") or {})

    @property
    def apple_id(self) -> str:
        return self.state.get("apple_id") or ""

    @property
    def params(self) -> dict[str, str]:
        p = {
            "clientBuildNumber": CLIENT_BUILD_NUMBER,
            "clientMasteringNumber": CLIENT_MASTERING_NUMBER,
            "clientId": self.client_id,
        }
        if self.state.get("dsid"):
            p["dsid"] = str(self.state["dsid"])
        return p

    def service_url(self, key: str) -> str | None:
        with self._lock:
            ws = (self.state.get("webservices") or {}).get(key) or {}
        url = ws.get("url")
        return url.rstrip("/") if url else None

    def pcs_required(self) -> bool:
        with self._lock:
            ws = (self.state.get("webservices") or {}).get("ckdatabasews") or {}
        return bool(ws.get("pcsRequired"))

    def cookie(self, name: str) -> str | None:
        with self.jar._cookies_lock:  # noqa: SLF001
            for c in list(self.jar):
                if c.name == name and c.value:
                    return c.value
        return None

    def upload_token(self) -> str | None:
        """The t= part of the validate cookie; iCloud wants it to start an upload."""
        value = (self.cookie(VALIDATE_COOKIE) or "").strip('"')
        m = re.search(r"\bt=([^:]+)", value)
        return m.group(1) if m else None

    # ------------------------------------------------------------------- HTTP

    def http(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = USER_AGENT
            s.cookies = self.jar
            self._local.session = s
        return s

    def service_headers(self) -> dict[str, str]:
        return {"Origin": self.endpoints.home, "Referer": self.endpoints.home + "/"}

    def note_response(self, r: requests.Response) -> None:
        """Keep the sign-in state Apple sends in headers and cookies."""
        changed = False
        with self._lock:
            for header, field in HEADER_FIELDS.items():
                value = r.headers.get(header)
                if value and self.state.get(field) != value:
                    self.state[field] = value
                    changed = True
        if changed or "set-cookie" in r.headers:
            try:
                self.save()
            except OSError:
                log.exception("Could not save the iCloud session")

    def _call(self, method: str, url: str, *, headers: dict | None = None, timeout=(20, 60), **kwargs) -> requests.Response:
        """One request for the sign-in flow (no retries: every proof counts)."""
        for attempt in range(2):
            try:
                r = self.http().request(method, url, headers=headers, timeout=timeout, **kwargs)
            except RuntimeError:
                # The shared cookie jar changed under us; harmless, try again.
                if attempt:
                    raise
                continue
            except (requests.ConnectionError, requests.Timeout) as e:
                raise AuthNetworkError(f"Could not reach Apple: {e.__class__.__name__}") from e
            self.note_response(r)
            return r
        raise AssertionError("unreachable")

    def _auth_headers(self, **extra: str) -> dict[str, str]:
        frame = "auth-" + self.client_id
        client_info = json.dumps(
            {"U": USER_AGENT, "L": "en-US", "Z": "GMT+00:00", "V": "1.1", "F": ""}, separators=(",", ":")
        )
        h = {
            "Accept": "application/json, text/javascript",
            "Content-Type": "application/json",
            "Origin": self.endpoints.idmsa,
            "Referer": self.endpoints.idmsa + "/",
            "X-Apple-Widget-Key": WIDGET_KEY,
            "X-Apple-OAuth-Client-Id": WIDGET_KEY,
            "X-Apple-OAuth-Client-Type": "firstPartyAuth",
            "X-Apple-OAuth-Redirect-URI": self.endpoints.home,
            "X-Apple-OAuth-Require-Grant-Code": "true",
            "X-Apple-OAuth-Response-Mode": "web_message",
            "X-Apple-OAuth-Response-Type": "code",
            "X-Apple-OAuth-State": frame,
            "X-Apple-Frame-Id": frame,
            "X-Requested-With": "XMLHttpRequest",
            "X-Apple-Mandate-Security-Upgrade": "0",
            "X-Apple-I-Require-UE": "true",
            "X-Apple-I-FD-Client-Info": client_info,
            "X-Apple-FD-Client-Info": client_info,
        }
        with self._lock:
            for field, header in (("scnt", "scnt"), ("session_id", "X-Apple-ID-Session-Id"),
                                  ("auth_attributes", "X-Apple-Auth-Attributes")):
                if self.state.get(field):
                    h[header] = self.state[field]
        h.update(extra)
        return h

    # ---------------------------------------------------------------- sign-in

    def sign_in(self, apple_id: str, password: str, china_mainland: bool = False) -> dict:
        """Check the password; returns {"status": "ok"} or a pending challenge.

        A challenge ({"status": "2fa", ...}) is answered with verify_code().
        """
        apple_id = apple_id.strip().lower()
        if not apple_id or not password:
            raise AuthError("Enter your Apple Account email or phone number and password.")
        with self._lock:
            if self.state.get("apple_id") and self.state["apple_id"] != apple_id:
                # Another account: its trust token and cookies are of no use.
                self._forget(keep_client=True)
            self.state["apple_id"] = apple_id
            self.state["china_mainland"] = bool(china_mainland)
            self.endpoints = Endpoints(bool(china_mainland))
            for field in ("scnt", "session_id", "auth_attributes"):
                self.state.pop(field, None)
            self._pending = None
        auth = self.endpoints.auth
        frame = "auth-" + self.client_id
        self._call("GET", f"{auth}/authorize/signin", headers={"Accept": "*/*"}, params={
            "frame_id": frame, "language": "en_US", "skVersion": "7", "iframeId": frame,
            "client_id": WIDGET_KEY, "redirect_uri": self.endpoints.home, "response_type": "code",
            "response_mode": "web_message", "state": frame, "authVersion": "latest",
        })
        self._call("POST", f"{auth}/federate", headers=self._auth_headers(),
                   params={"isRememberMeEnabled": "true"}, json={"accountName": apple_id, "rememberMe": True})

        keys: dict[tuple, bytes] = {}
        for _attempt in range(5):
            client = srp.SrpClient()
            r = self._call("POST", f"{auth}/signin/init", headers=self._auth_headers(), json={
                "a": _b64(client.public), "accountName": apple_id, "protocols": list(srp.PROTOCOLS),
            })
            if r.status_code != 200:
                raise AuthError(_apple_message(r, f"Apple refused the sign-in (HTTP {r.status_code})."))
            try:
                init = r.json()
                salt = base64.b64decode(init["salt"])
                server_public = base64.b64decode(init["b"])
                iterations, protocol, c = int(init["iteration"]), init["protocol"], init["c"]
            except (ValueError, KeyError, TypeError) as e:
                raise AuthError("Apple's sign-in service sent an unexpected reply.") from e
            spec = (salt, iterations, protocol)
            try:
                if spec not in keys:
                    keys[spec] = srp.derive_password(password, salt, iterations, protocol)
            except srp.SrpError as e:
                raise AuthError(str(e)) from e
            proof = client.process_challenge(apple_id, keys[spec], salt, server_public)
            if proof is not None and not proof.ambiguous:
                break
            log.debug("SRP values not in canonical form; starting a fresh handshake")
        else:
            raise AuthError("Couldn't start a secure sign-in with Apple. Please try again.")

        trust = self.state.get("trust_token")
        r = self._call("POST", f"{auth}/signin/complete", headers=self._auth_headers(),
                       params={"isRememberMeEnabled": "true"}, json={
                           "accountName": apple_id, "c": c, "m1": _b64(proof.m1), "m2": _b64(proof.m2),
                           "rememberMe": True, "trustTokens": [trust] if trust else [],
                       })
        if r.status_code == 409:
            return self._start_challenge()
        if r.status_code == 412:
            r = self._call("POST", f"{auth}/repair/complete", headers=self._auth_headers(), json={})
            if r.status_code not in (200, 204):
                raise AuthError(_apple_message(r, "Apple needs you to review your account: sign in at icloud.com once."))
        elif r.status_code in (401, 403):
            raise AuthError(_apple_message(r, "Your Apple Account or password was incorrect."))
        elif r.status_code != 200:
            raise AuthError(_apple_message(r, f"Sign-in failed (HTTP {r.status_code})."))
        self._account_login()
        return {"status": "ok", "account": self.account}

    def _start_challenge(self) -> dict:
        r = self._call("GET", self.endpoints.auth, headers=self._auth_headers(Accept="text/html"))
        try:
            root = r.json()
        except ValueError:
            root = parse_boot_args(r.text)
        options = parse_twofactor_options(root if isinstance(root, dict) else {})
        if options["security_key"]:
            raise AuthError("This account asks for a security key, which isn't supported yet.")
        self._pending = {"phones": options["phones"], "delivery": "device", "phone": None,
                         "length": options["length"], "bridge": options["bridge"]}
        if options["has_devices"]:
            # Recent iOS versions no longer push a code on their own.
            self._call("PUT", f"{self.endpoints.auth}/verify/trusteddevice/securitycode", headers=self._auth_headers())
        elif options["phones"]:
            self._send_sms(options["phones"][0])
        return self.challenge()

    def challenge(self) -> dict:
        p = self._pending or {}
        phone = p.get("phone") or {}
        return {
            "status": "2fa",
            "delivery": p.get("delivery", "device"),
            "sent_to": phone.get("number") if p.get("delivery") == "sms" else None,
            "phones": [{"id": x["id"], "number": x["number"]} for x in p.get("phones", [])],
            "length": p.get("length", 6),
            "bridge": p.get("bridge", False),
        }

    def _send_sms(self, phone: dict) -> None:
        payload: dict[str, Any] = {"id": phone["id"]}
        if phone.get("nonFTEU") is not None:
            payload["nonFTEU"] = phone["nonFTEU"]
        r = self._call("PUT", f"{self.endpoints.auth}/verify/phone", headers=self._auth_headers(),
                       json={"phoneNumber": payload, "mode": phone.get("mode") or "sms"})
        if r.status_code not in (200, 202, 204):
            raise AuthError(_apple_message(r, "Apple couldn't send a text message. Try again later."))
        assert self._pending is not None
        self._pending.update(delivery="sms", phone=phone)

    def send_code(self, method: str, phone_id: Any = None) -> dict:
        """Deliver a (new) code: "device" or "sms" (to phone_id or the first number)."""
        if self._pending is None:
            raise AuthError("This sign-in has expired. Please start again.")
        if method == "sms":
            phones = self._pending["phones"]
            phone = next((p for p in phones if str(p["id"]) == str(phone_id)), phones[0] if phones else None)
            if phone is None:
                raise AuthError("This account has no trusted phone number.")
            self._send_sms(phone)
        else:
            self._call("PUT", f"{self.endpoints.auth}/verify/trusteddevice/securitycode", headers=self._auth_headers())
            self._pending.update(delivery="device", phone=None)
        return self.challenge()

    def verify_code(self, code: str) -> dict:
        if self._pending is None:
            raise AuthError("This sign-in has expired. Please start again.")
        code = re.sub(r"\D", "", code or "")
        if not code:
            raise WrongCode("Enter the verification code.")
        auth = self.endpoints.auth
        headers = self._auth_headers(Accept="application/json")
        if self._pending["delivery"] == "sms":
            phone = self._pending["phone"]
            number: dict[str, Any] = {"id": phone["id"]}
            if phone.get("nonFTEU") is not None:
                number["nonFTEU"] = phone["nonFTEU"]
            r = self._call("POST", f"{auth}/verify/phone/securitycode", headers=headers, json={
                "phoneNumber": number, "securityCode": {"code": code}, "mode": phone.get("mode") or "sms",
            })
        else:
            r = self._call("POST", f"{auth}/verify/trusteddevice/securitycode", headers=headers,
                           json={"securityCode": {"code": code}})
        # Since 2026 a correct code may be answered with 409, but a session
        # token is issued only when the code was accepted.
        accepted = r.status_code in (200, 204) or (r.status_code == 409 and "X-Apple-Session-Token" in r.headers)
        if not accepted:
            if r.status_code in (400, 401, 403, 409, 412):
                raise WrongCode(_apple_message(r, "Incorrect verification code."))
            raise AuthError(_apple_message(r, f"Verification failed (HTTP {r.status_code})."))
        r = self._call("GET", f"{auth}/2sv/trust", headers=self._auth_headers())
        if r.status_code not in (200, 204):
            log.warning("Trusting this computer failed (HTTP %s)", r.status_code)
        self._pending = None
        self._account_login()
        return {"status": "ok", "account": self.account}

    def _account_login(self) -> dict:
        with self._lock:
            token = self.state.get("session_token")
            body = {
                "accountCountryCode": self.state.get("account_country") or "",
                "dsWebAuthToken": token,
                "extended_login": True,
                "trustToken": self.state.get("trust_token") or "",
            }
        if not token:
            raise NotSignedIn("Sign in to iCloud again.")
        r = self._call("POST", f"{self.endpoints.setup}/accountLogin",
                       headers={**self.service_headers(), "Content-Type": "application/json"}, json=body)
        if r.status_code in (401, 403, 421, 450):
            raise NotSignedIn("Your iCloud session expired. Sign in again.")
        if r.status_code != 200:
            raise AuthError(_apple_message(r, f"iCloud sign-in failed (HTTP {r.status_code})."))
        try:
            data = r.json()
        except ValueError as e:
            raise AuthError("iCloud sent an unexpected reply.") from e
        if data.get("termsUpdateNeeded"):
            raise AuthError("Apple needs you to accept updated terms: sign in at icloud.com once, then try again.")
        ds = data.get("dsInfo") or {}
        webservices = data.get("webservices") or {}
        if not ds.get("dsid"):
            raise AuthError("iCloud sent an unexpected reply (no account id).")
        if data.get("hsaChallengeRequired") and not data.get("hsaTrustedBrowser", True):
            raise NotSignedIn("Apple wants you to verify your identity again.")
        if not (webservices.get("ckdatabasews") or {}).get("url"):
            raise AuthError("iCloud Photos isn't available for this account. Turn on iCloud Photos on "
                            "your iPhone, iPad or Mac and try again.")
        with self._lock:
            self.state["dsid"] = str(ds["dsid"])
            self.state["webservices"] = webservices
            self.state["account"] = {
                "name": ds.get("fullName") or " ".join(p for p in (ds.get("firstName"), ds.get("lastName")) if p),
                "email": ds.get("appleId") or ds.get("primaryEmail") or self.state.get("apple_id") or "",
                "dsid": str(ds["dsid"]),
            }
            self.state.pop("expired", None)
            self.generation += 1
        self.save()
        return data

    def refresh_login(self, generation: int) -> None:
        """Renew the iCloud cookies after a service said the session expired."""
        with self._login_lock:
            if self.generation != generation and not self.lost:
                return  # another thread renewed it meanwhile
            if self.lost:
                raise NotSignedIn("Sign in to iCloud again.")
            log.info("iCloud session expired; renewing it")
            try:
                self._account_login()
            except NotSignedIn:
                log.warning("Renewing the iCloud session failed; sign-in required")
                with self._lock:
                    self.state["expired"] = True
                self.save()
                raise

    # ------------------------------------------------ Advanced Data Protection

    def _setup_post(self, endpoint: str, body: Any = None) -> dict:
        r = self._call("POST", f"{self.endpoints.setup}/{endpoint}", params=self.params,
                       headers={**self.service_headers(), "Content-Type": "application/json"},
                       data=json.dumps(body) if body is not None else None)
        if r.status_code in (401, 421, 450):
            raise NotSignedIn("Your iCloud session expired. Sign in again.")
        try:
            data = r.json()
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def ensure_web_access(self, on_wait=None, rounds: int = 30, pause: float = 10.0) -> None:
        """On accounts with Advanced Data Protection, get the iCloud Photos keys.

        Apple asks the user to allow web access on a trusted device; this
        polls until they do (about five minutes at most).
        """
        if not self.pcs_required() or self.cookie(PCS_COOKIE):
            return
        state = self._setup_post("requestWebAccessState")
        if not state.get("isICDRSDisabled", False):
            return  # standard data protection: nothing to unlock
        if not state.get("isDeviceConsentedForPCS", True):
            reply = self._setup_post("enableDeviceConsentForPCS")
            if not reply.get("isDeviceConsentNotificationSent"):
                raise AuthError("Turn on “Access iCloud Data on the Web” in iCloud settings on your iPhone, "
                                "iPad or Mac, then try again.")
        for _ in range(rounds):
            if state.get("isDeviceConsentedForPCS", True):
                break
            if on_wait:
                on_wait("Allow access to your iCloud data on your iPhone, iPad or Mac")
            time.sleep(pause)
            state = self._setup_post("requestWebAccessState")
        message = ""
        for attempt in range(rounds):
            reply = self._setup_post("requestPCS", {"appName": "photos", "derivedFromUserAction": attempt == 0})
            if reply.get("status") == "success":
                with self._lock:
                    self.generation += 1
                self.save()
                return
            message = reply.get("message") or ""
            if message not in ("Requested the device to upload cookies.", "Cookies not available yet on server."):
                break
            if on_wait:
                on_wait("Waiting for your iPhone, iPad or Mac to share access to iCloud Photos")
            time.sleep(pause)
        raise AuthError(f"Couldn't unlock iCloud Photos (Advanced Data Protection){': ' + message if message else ''}.")

    # --------------------------------------------------------------- sign-out

    def _forget(self, keep_client: bool = True) -> None:
        client_id = self.state.get("client_id")
        china = self.state.get("china_mainland")
        self.state = {"client_id": client_id, "china_mainland": china} if keep_client else {}
        self.jar.clear()
        self._pending = None

    def sign_out(self) -> None:
        if self.state.get("dsid") and not self.lost:
            try:
                self._call("POST", f"{self.endpoints.setup}/logout", params=self.params, timeout=(10, 15),
                           headers={**self.service_headers(), "Content-Type": "text/plain;charset=UTF-8"},
                           data=json.dumps({"trustBrowser": False, "allBrowsers": False}))
            except AuthError:
                pass
        with self._lock:
            self._forget(keep_client=True)
            self.generation += 1
        try:
            self.session_file.unlink()
        except FileNotFoundError:
            pass
