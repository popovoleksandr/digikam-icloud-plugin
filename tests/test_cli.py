"""The command line, as a child process against the fake iCloud.

Run with:  python -m unittest tests.test_cli -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT))

from tests.fake_icloud import APPLE_ID, PASSWORD, FakeICloud, Photo  # noqa: E402

MAY = datetime(2024, 5, 1, 12, 0).timestamp()


class Cli(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeICloud().start()
        self.fake.account.require_2fa = False
        a, = self.fake.library.add(Photo("IMG_1.JPG", b"one" * 100, MAY))
        self.fake.library.album("Trip", a)
        self.tmp = Path(tempfile.mkdtemp(prefix="dkicloud-cli-"))
        self.env = dict(os.environ, DKICLOUD_HOME=str(self.tmp / "home"), DKICLOUD_TEST_SERVER=self.fake.url,
                        PYTHONPATH=str(ROOT / "engine"))

    def tearDown(self) -> None:
        self.fake.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "dkicloudphotos", *args], env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    def sign_in(self) -> None:
        os.environ.update(DKICLOUD_HOME=self.env["DKICLOUD_HOME"], DKICLOUD_TEST_SERVER=self.fake.url)
        from dkicloudphotos.engine import Engine
        engine = Engine()
        engine.sign_in(APPLE_ID, PASSWORD)
        engine.close()

    def test_status_before_and_after_sign_in(self):
        r = self.run_cli("status")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Not signed in", r.stdout)
        self.sign_in()
        r = self.run_cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(APPLE_ID, r.stdout)

    def test_albums_sync_upload(self):
        r = self.run_cli("albums")
        self.assertEqual(r.returncode, 2)
        self.assertIn("Sign in", r.stderr)
        self.sign_in()
        r = self.run_cli("albums")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Trip", r.stdout)
        dest = self.tmp / "out"
        r = self.run_cli("sync", str(dest), "--album", "trip", "--dry-run")
        self.assertIn("Would download 1 files", r.stdout)
        r = self.run_cli("sync", str(dest), "--album", "Trip")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((dest / "2024/05/IMG_1.JPG").exists())
        self.assertIn("Downloaded 1", r.stdout)
        photo = self.tmp / "x.jpg"
        photo.write_bytes(b"\xff\xd8 x")
        r = self.run_cli("upload", str(photo), str(dest / "2024/05/IMG_1.JPG"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Uploaded 1, skipped 1, failed 0", r.stdout)


if __name__ == "__main__":
    unittest.main()
