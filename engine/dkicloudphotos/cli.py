"""Command line: the same engine the digiKam plugin uses, for a terminal or a timer.

    digikam-icloud-photos sign-in
    digikam-icloud-photos albums
    digikam-icloud-photos sync ~/Pictures/iCloud --album "All Photos"
    digikam-icloud-photos upload ~/Pictures/new/*.jpg
"""

from __future__ import annotations

import argparse
import getpass
import logging
import logging.handlers
import sys

from . import __version__
from .paths import Paths


def setup_logging(paths: Paths, verbose: bool = False, to_stderr: bool = True) -> None:
    paths.ensure()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = logging.handlers.RotatingFileHandler(paths.log_file, maxBytes=1_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    if to_stderr and verbose:
        err = logging.StreamHandler(sys.stderr)
        err.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(err)
    # urllib3 logs every URL at DEBUG, signed download URLs included.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class Printer:
    """Turns engine events into terminal lines."""

    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet

    def __call__(self, event: str, **f) -> None:
        if event == "message":
            print(f["text"], file=sys.stderr)
        elif self.quiet:
            if event == "error":
                print(f"error: {f.get('filename')}: {f.get('message')}", file=sys.stderr)
        elif event == "phase":
            print(f["text"])
        elif event == "total":
            print(f"{f['total']} to download ({_size(f['bytes'])})")
        elif event == "file":
            extra = f" ({f['message']})" if f.get("message") else ""
            print(f"[{f.get('done', 0)}/{f.get('total', 0)}] {f['status']}: {f['path']}{extra}")
        elif event == "error":
            print(f"error: {f.get('filename')}: {f.get('message')}", file=sys.stderr)


def cmd_sign_in(engine, args) -> int:
    from .auth import WrongCode

    apple_id = args.apple_id or input("Apple Account (email or phone): ").strip()
    password = getpass.getpass("Password: ")
    result = engine.sign_in(apple_id, password, args.china, emit=Printer())
    del password
    while result.get("status") == "2fa":
        where = (f"a text message to {result['sent_to']}" if result.get("delivery") == "sms"
                 else "your trusted Apple devices")
        code = input(f"Enter the {result.get('length', 6)}-digit code sent to {where} "
                     "(or 'sms' to get a text message): ").strip()
        if code.lower() == "sms":
            result = engine.send_code("sms")
            continue
        try:
            result = engine.verify_code(code, emit=Printer())
        except WrongCode as e:
            print(e, file=sys.stderr)
    account = result.get("account") or {}
    print(f"Signed in as {account.get('name') or ''} <{account.get('email') or ''}>")
    return 0


def cmd_status(engine, args) -> int:
    s = engine.status()
    if s["signed_in"]:
        print(f"Signed in as {s['name']} <{s['email']}>")
        return 0
    print("Session expired: run 'sign-in' again." if s["has_account"] else "Not signed in.")
    return 1


def cmd_sign_out(engine, args) -> int:
    engine.sign_out()
    print("Signed out.")
    return 0


def cmd_libraries(engine, args) -> int:
    for lib in engine.libraries(emit=Printer())["libraries"]:
        print(f"{lib['id']}\t{lib['name']}")
    return 0


def cmd_albums(engine, args) -> int:
    for a in engine.albums(args.library, emit=Printer())["albums"]:
        count = "?" if a.get("count") is None else a["count"]
        print(f"{count:>7}  {a.get('path') or a['name']}\t[{a['id']}]")
    return 0


def cmd_sync(engine, args) -> int:
    import os

    dest = os.path.abspath(os.path.expanduser(args.dest))
    result = engine.sync(
        dest, library=args.library, albums=args.album or ["all"], layout=args.layout,
        include_videos=not args.no_videos, live_photos=not args.no_live, edited=args.edited,
        album_tags=False, redownload_deleted=args.redownload_deleted, limit=args.limit,
        dry_run=args.dry_run, emit=Printer(args.quiet),
    )
    if args.dry_run:
        print(f"Would download {result['would_download']} files ({_size(result['bytes'])}), "
              f"adopt {result['would_adopt']}; {result['skipped']} already here, "
              f"{result['deleted_locally']} deleted here.")
    else:
        print(f"Downloaded {result['downloaded']} ({_size(result['bytes'])}), adopted {result['adopted']}, "
              f"already here {result['skipped']}, deleted here {result['deleted_locally']}, "
              f"failed {result['failed']}.")
    return 1 if result.get("failed") else 0


def cmd_upload(engine, args) -> int:
    result = engine.upload(args.files, force=args.force, emit=Printer(args.quiet))
    print(f"Uploaded {result['uploaded']}, skipped {result['skipped']}, failed {result['failed']}.")
    return 1 if result["failed"] else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="digikam-icloud-photos",
                                description="Sync iCloud Photos with a folder (the digiKam plugin's engine).")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="log details to stderr")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sign-in", help="sign in to iCloud (asks for the password and a code)")
    s.add_argument("--apple-id")
    s.add_argument("--china", action="store_true", help="an account in mainland China (icloud.com.cn)")
    s.set_defaults(func=cmd_sign_in)

    sub.add_parser("status", help="who is signed in").set_defaults(func=cmd_status)
    sub.add_parser("sign-out", help="sign out and forget the session").set_defaults(func=cmd_sign_out)
    sub.add_parser("libraries", help="list photo libraries (personal, shared)").set_defaults(func=cmd_libraries)

    s = sub.add_parser("albums", help="list albums with their photo counts")
    s.add_argument("--library")
    s.set_defaults(func=cmd_albums)

    s = sub.add_parser("sync", help="download what is not in DEST yet")
    s.add_argument("dest")
    s.add_argument("--library")
    s.add_argument("--album", action="append", help="album id, path or name (repeatable; default All Photos)")
    s.add_argument("--layout", choices=("date", "flat"), default="date",
                   help="date: DEST/YYYY/MM/ (default); flat: everything in DEST")
    s.add_argument("--no-videos", action="store_true")
    s.add_argument("--no-live", action="store_true", help="skip the video half of Live Photos")
    s.add_argument("--edited", action="store_true", help="also save edited versions (name-edited.ext)")
    s.add_argument("--redownload-deleted", action="store_true",
                   help="download again files that were synced before but deleted here")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("upload", help="add photos or videos to iCloud Photos")
    s.add_argument("files", nargs="+")
    s.add_argument("--force", action="store_true", help="upload even if it came from iCloud or was sent before")
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(func=cmd_upload)

    sub.add_parser("serve", help="speak the plugin protocol on stdin/stdout")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = Paths()
    if args.command == "serve":
        setup_logging(paths, args.verbose)
        from .server import serve
        return serve()
    setup_logging(paths, args.verbose)

    from .auth import AuthError
    from .engine import Engine
    from .photos import Cancelled, PhotosError

    engine = Engine(paths)
    try:
        return args.func(engine, args)
    except KeyboardInterrupt:
        engine.cancel.set()
        print("Interrupted.", file=sys.stderr)
        return 130
    except Cancelled:
        print("Cancelled.", file=sys.stderr)
        return 130
    except (AuthError, PhotosError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
