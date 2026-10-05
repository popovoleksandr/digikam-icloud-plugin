"""The ``serve`` protocol (docs/PROTOCOL.md), with the engine as a real child process.

Run with:  python -m unittest tests.test_server -v
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.fake_icloud import APPLE_ID, DEVICE_CODE, PASSWORD, FakeICloud, Photo  # noqa: E402

MAY = datetime(2024, 5, 1, 12, 0).timestamp()


class Session:
    """The plugin's side of the pipe."""

    def __init__(self, env: dict) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "dkicloudphotos", "serve"], cwd=str(ROOT / "engine"),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            text=True, encoding="utf-8", bufsize=1,
        )
        self.lines: queue.Queue = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(json.loads(line))
        self.lines.put(None)

    def send(self, cmd: str, **args) -> int:
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"id": self.next_id, "cmd": cmd, "args": args}) + "\n")
        self.proc.stdin.flush()
        return self.next_id

    def wait(self, rid: int, timeout: float = 30) -> tuple[dict, list[dict]]:
        """The reply to *rid* and the events that came before it."""
        events = []
        while True:
            msg = self.lines.get(timeout=timeout)
            if msg is None:
                raise AssertionError("the engine exited")
            if msg.get("id") != rid:
                raise AssertionError(f"unexpected message {msg}")
            if "event" in msg:
                events.append(msg)
            else:
                return msg, events

    def call(self, cmd: str, **args) -> tuple[dict, list[dict]]:
        return self.wait(self.send(cmd, **args))

    def close(self) -> int:
        try:
            self.proc.stdin.close()
        except BrokenPipeError:
            pass
        code = self.proc.wait(timeout=15)
        self.proc.stdout.close()
        self.proc.stderr.close()
        return code


class Protocol(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeICloud().start()
        self.tmp = Path(tempfile.mkdtemp(prefix="dkicloud-srv-"))
        self.dest = self.tmp / "Pictures"
        self.dest.mkdir()
        env = dict(os.environ)
        env.update(DKICLOUD_HOME=str(self.tmp / "home"), DKICLOUD_TEST_SERVER=self.fake.url,
                   PYTHONPATH=str(ROOT / "engine"), LC_ALL="C")
        self.s = Session(env)

    def tearDown(self) -> None:
        self.s.close()
        self.fake.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sign_in(self) -> None:
        self.fake.account.require_2fa = False
        reply, _ = self.s.call("sign_in", apple_id=APPLE_ID, password=PASSWORD)
        self.assertTrue(reply["ok"], reply)

    def test_hello_and_status(self):
        reply, events = self.s.call("hello")
        self.assertEqual(reply, {"id": 1, "ok": True, "result": {"version": "1.0.0", "protocol": 1}})
        self.assertEqual(events, [])
        reply, _ = self.s.call("status")
        self.assertEqual(reply["result"]["signed_in"], False)

    def test_sign_in_with_code(self):
        reply, _ = self.s.call("sign_in", apple_id=APPLE_ID, password=PASSWORD)
        self.assertEqual(reply["result"]["status"], "2fa")
        self.assertEqual(reply["result"]["delivery"], "device")
        reply, _ = self.s.call("verify_code", code="999999")
        self.assertEqual(reply["error"]["kind"], "wrong_code")
        reply, _ = self.s.call("send_code", method="sms")
        self.assertEqual(reply["result"]["delivery"], "sms")
        reply, _ = self.s.call("verify_code", code="654321")
        self.assertEqual(reply["result"]["status"], "ok")
        self.assertEqual(reply["result"]["account"]["email"], APPLE_ID)
        reply, _ = self.s.call("status")
        self.assertTrue(reply["result"]["signed_in"])

    def test_wrong_password(self):
        reply, _ = self.s.call("sign_in", apple_id=APPLE_ID, password="nope")
        self.assertEqual(reply["error"]["kind"], "auth")
        self.assertIn("incorrect", reply["error"]["message"])

    def test_not_signed_in(self):
        reply, _ = self.s.call("albums")
        self.assertEqual(reply["error"]["kind"], "not_signed_in")

    def test_queued_requests_sync_and_upload(self):
        a, = self.fake.library.add(Photo("IMG_1.JPG", b"one" * 100, MAY, favorite=True))
        self.fake.library.album("Trip", a)
        self.sign_in()
        # Sent back to back: the engine queues them.
        r1 = self.s.send("libraries")
        r2 = self.s.send("albums")
        r3 = self.s.send("sync", dest=str(self.dest))
        reply, _ = self.s.wait(r1)
        self.assertEqual(reply["result"]["libraries"][0]["id"], "private/PrimarySync")
        reply, _ = self.s.wait(r2)
        self.assertIn("Trip", [x["name"] for x in reply["result"]["albums"]])
        reply, events = self.s.wait(r3)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["downloaded"], 1)
        kinds = [e["event"] for e in events]
        self.assertIn("phase", kinds)
        self.assertEqual(kinds.count("total"), 1)
        file_event = next(e for e in events if e["event"] == "file")
        self.assertEqual(file_event["path"], str(self.dest / "2024/05/IMG_1.JPG"))
        self.assertEqual(file_event["asset"]["albums"], ["Trip"])
        self.assertTrue(file_event["asset"]["favorite"])

        photo = self.tmp / "new photo.jpg"
        photo.write_bytes(b"\xff\xd8 new")
        reply, events = self.s.call("upload", files=[str(photo), str(self.dest / "2024/05/IMG_1.JPG")])
        self.assertEqual(reply["result"], {"uploaded": 1, "skipped": 1, "failed": 0})
        self.assertEqual([e["status"] for e in events], ["uploaded", "skipped"])

    def test_cancel_running_and_queued(self):
        self.fake.library.add(*[Photo(f"IMG_{i}.JPG", b"x" * 4000, MAY + i) for i in range(5)])
        self.fake.state["content_delay"] = 0.2
        self.sign_in()
        running = self.s.send("sync", dest=str(self.dest))
        queued = self.s.send("albums")
        # Wait until the download has started, then cancel.
        events = []
        while True:
            msg = self.s.lines.get(timeout=30)
            if msg.get("event") == "total":
                break
            events.append(msg)
        cancel = self.s.send("cancel")
        seen = {}
        while len(seen) < 3:
            msg = self.s.lines.get(timeout=30)
            if "event" not in msg:
                seen[msg["id"]] = msg
        self.assertTrue(seen[cancel]["ok"])
        self.assertEqual(seen[running]["error"]["kind"], "cancelled")
        self.assertEqual(seen[queued]["error"]["kind"], "cancelled")
        # The engine keeps working after a cancel.
        self.fake.state["content_delay"] = 0.0
        reply, _ = self.s.call("sync", dest=str(self.dest))
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(len(list(self.dest.rglob("*.JPG"))), 5)

    def test_bad_requests(self):
        self.s.proc.stdin.write("this is not json\n")
        self.s.proc.stdin.flush()
        msg = self.s.lines.get(timeout=10)
        self.assertEqual(msg["error"]["kind"], "bad_request")
        reply, _ = self.s.call("frobnicate")
        self.assertEqual(reply["error"]["kind"], "bad_request")
        self.sign_in()
        reply, _ = self.s.call("sync")
        self.assertEqual(reply["error"]["kind"], "bad_request")
        self.assertIn("dest", reply["error"]["message"])
        reply, _ = self.s.call("sync", dest=str(self.dest), include_videos="yes")
        self.assertEqual(reply["error"]["kind"], "bad_request")

    def test_shutdown(self):
        reply, _ = self.s.call("shutdown")
        self.assertTrue(reply["ok"])
        self.assertEqual(self.s.proc.wait(timeout=10), 0)


if __name__ == "__main__":
    unittest.main()
