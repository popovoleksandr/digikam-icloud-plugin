"""A stand-in for the dkicloudphotos engine, for the plugin's C++ tests.

It speaks the protocol of docs/PROTOCOL.md with canned answers. The tests
install it as dkicloudphotos/__main__.py in the build tree and steer it with
environment variables:

  DKICLOUD_STUB_SIGNED_IN   "0" to start signed out (default "1")
  DKICLOUD_STUB_FILES       JSON list of {"path", "favorite", "albums", "caption", "role"}
                            reported as downloaded by "sync"
  DKICLOUD_STUB_LIBRARIES   number of libraries to list (default 1)

Uploads succeed unless the file name contains "skip" (skipped) or "fail"
(failed). The "crash" command exits with status 3 after writing to stderr.
"""

import json
import os
import sys

PASSWORD = "secret"
CODE = "123456"

state = {"signed_in": os.environ.get("DKICLOUD_STUB_SIGNED_IN", "1") == "1", "pending_2fa": False}


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def reply(rid, result=None):
    send({"id": rid, "ok": True, "result": result or {}})


def fail(rid, kind, message):
    send({"id": rid, "ok": False, "error": {"kind": kind, "message": message}})


def event(rid, name, **fields):
    send(dict({"id": rid, "event": name}, **fields))


def status():
    if state["signed_in"]:
        return {"signed_in": True, "has_account": True, "apple_id": "user@example.com",
                "name": "Test User", "email": "user@example.com"}
    return {"signed_in": False, "has_account": False, "apple_id": "", "name": "", "email": ""}


def challenge(delivery="device"):
    return {"status": "2fa", "delivery": delivery, "sent_to": "+1 (•••) •••-••12" if delivery == "sms" else None,
            "phones": [{"id": 1, "number": "+1 (•••) •••-••12"}], "length": 6}


def handle(rid, cmd, args):
    if cmd == "hello":
        return reply(rid, {"version": "stub", "protocol": 1})
    if cmd == "status":
        return reply(rid, status())
    if cmd == "sign_in":
        event(rid, "message", text="Checking your password…")
        if args.get("password") != PASSWORD:
            return fail(rid, "auth", "Your Apple Account or password was incorrect.")
        state["pending_2fa"] = True
        return reply(rid, challenge())
    if cmd == "send_code":
        return reply(rid, challenge(args.get("method", "device")))
    if cmd == "verify_code":
        if not state["pending_2fa"]:
            return fail(rid, "auth", "This sign-in has expired. Please start again.")
        if args.get("code") != CODE:
            return fail(rid, "wrong_code", "Incorrect verification code.")
        state["signed_in"], state["pending_2fa"] = True, False
        return reply(rid, {"status": "ok", "account": {"name": "Test User", "email": "user@example.com", "dsid": "1"}})
    if cmd == "sign_out":
        state["signed_in"] = False
        return reply(rid)
    if not state["signed_in"] and cmd in ("libraries", "albums", "sync", "upload"):
        return fail(rid, "not_signed_in", "Sign in to iCloud again.")
    if cmd == "libraries":
        libs = [{"id": "private/PrimarySync", "name": "Personal Library"}]
        if int(os.environ.get("DKICLOUD_STUB_LIBRARIES", "1")) > 1:
            libs.append({"id": "shared/SharedSync-1", "name": "Shared Library"})
        return reply(rid, {"libraries": libs})
    if cmd == "albums":
        return reply(rid, {"albums": [
            {"id": "all", "name": "All Photos", "kind": "smart", "count": 3},
            {"id": "smart:FAVORITE", "name": "Favorites", "kind": "smart", "count": 1},
            {"id": "ALB1", "name": "Trip", "kind": "album", "path": "Travel/Trip", "count": None},
        ]})
    if cmd == "sync":
        files = json.loads(os.environ.get("DKICLOUD_STUB_FILES", "[]"))
        event(rid, "phase", text="Listing All Photos…")
        total = len(files)
        event(rid, "total", total=total, bytes=1000 * total)
        for n, f in enumerate(files, 1):
            event(rid, "file", path=f["path"], status="downloaded", done=n, total=total, asset={
                "id": f"REC{n}", "filename": os.path.basename(f["path"]), "date": "2024-05-01T10:00:00+00:00",
                "favorite": f.get("favorite", False), "albums": f.get("albums", []), "caption": f.get("caption", ""),
                "kind": "image", "role": f.get("role", "original")})
            event(rid, "progress", done=n, total=total, bytes_done=1000 * n, bytes_total=1000 * total)
        event(rid, "error", filename="IMG_9.HEIC", message="HTTP 500")
        return reply(rid, {"listed": total + 1, "downloaded": total, "adopted": 0, "skipped": 0,
                           "deleted_locally": 0, "failed": 1, "bytes": 1000 * total})
    if cmd == "upload":
        files = args.get("files", [])
        counts = {"uploaded": 0, "skipped": 0, "failed": 0}
        for n, path in enumerate(files, 1):
            name = os.path.basename(path)
            if "skip" in name:
                st, msg = "skipped", "Already in iCloud Photos"
            elif "fail" in name or not os.path.exists(path):
                st, msg = "failed", "Not a photo or video"
            else:
                st, msg = "uploaded", None
            counts[st] += 1
            ev = {"path": path, "status": st, "done": n, "total": len(files)}
            if msg:
                ev["message"] = msg
            event(rid, "file", **ev)
        return reply(rid, counts)
    if cmd == "cancel":
        return reply(rid)
    if cmd == "crash":
        sys.stderr.write("Traceback (most recent call last):\nRuntimeError: stub crash\n")
        sys.stderr.flush()
        sys.exit(3)
    return fail(rid, "bad_request", f"Unknown command {cmd!r}")


def main():
    if sys.argv[1:] != ["serve"]:
        sys.stderr.write("usage: python -m dkicloudphotos serve\n")
        return 2
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("cmd") == "shutdown":
            reply(msg["id"])
            return 0
        handle(msg["id"], msg.get("cmd"), msg.get("args") or {})
    return 0


if __name__ == "__main__":
    sys.exit(main())
