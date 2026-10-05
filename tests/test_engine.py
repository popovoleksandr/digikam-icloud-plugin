"""The engine against the fake iCloud: sign-in, albums, sync and upload.

Run with:  python -m unittest tests.test_engine -v
"""

from __future__ import annotations

import fcntl
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT))

from tests.fake_icloud import APPLE_ID, DEVICE_CODE, PASSWORD, FakeICloud, Photo  # noqa: E402

MAY = datetime(2024, 5, 1, 12, 0).timestamp()
JUNE = datetime(2024, 6, 2, 9, 30).timestamp()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeICloud().start()
        self.fake.account.require_2fa = False
        self.tmp = Path(tempfile.mkdtemp(prefix="dkicloud-"))
        self.dest = self.tmp / "Pictures" / "iCloud"
        self.dest.mkdir(parents=True)
        os.environ["DKICLOUD_HOME"] = str(self.tmp / "home")
        os.environ["DKICLOUD_TEST_SERVER"] = self.fake.url
        from dkicloudphotos.engine import Engine
        self.Engine = Engine
        self.engine = Engine()
        self.events: list[tuple[str, dict]] = []

    def tearDown(self) -> None:
        self.engine.close()
        self.fake.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def emit(self, event: str, **fields) -> None:
        self.events.append((event, fields))

    def of(self, name: str) -> list[dict]:
        return [f for e, f in self.events if e == name]

    def sign_in(self):
        result = self.engine.sign_in(APPLE_ID, PASSWORD)
        self.assertEqual(result["status"], "ok")

    def sync(self, **kw):
        self.events.clear()
        kw.setdefault("emit", self.emit)
        return self.engine.sync(str(self.dest), **kw)

    def files(self) -> list[str]:
        return sorted(str(p.relative_to(self.dest)) for p in self.dest.rglob("*") if p.is_file())


class SignIn(Base):
    def test_two_factor_then_status(self):
        from dkicloudphotos.auth import WrongCode

        self.fake.account.require_2fa = True
        result = self.engine.sign_in(APPLE_ID, PASSWORD)
        self.assertEqual(result["status"], "2fa")
        with self.assertRaises(WrongCode):
            self.engine.verify_code("000000")
        result = self.engine.verify_code(DEVICE_CODE)
        self.assertEqual(result["status"], "ok")
        again = self.Engine()
        status = again.status()
        again.close()
        self.assertTrue(status["signed_in"])
        self.assertEqual(status["email"], APPLE_ID)
        self.assertEqual(status["name"], "Test User")

    def test_not_signed_in(self):
        from dkicloudphotos.photos import AuthRequired

        with self.assertRaises(AuthRequired):
            self.engine.albums()
        with self.assertRaises(AuthRequired):
            self.sync()

    def test_sign_out_forgets_session(self):
        self.sign_in()
        self.engine.sign_out()
        again = self.Engine()
        self.assertFalse(again.status()["has_account"])
        again.close()

    def test_session_renewed_transparently(self):
        self.fake.library.add(Photo("IMG_1.JPG", b"x" * 100, MAY))
        self.sign_in()
        logins = self.fake.account.logins
        self.fake.account.web_tokens.clear()        # the iCloud cookies expire
        self.assertEqual(self.sync()["downloaded"], 1)
        self.assertEqual(self.fake.account.logins, logins + 1)

    def test_next_day_the_expired_cookies_are_renewed(self):
        self.fake.library.add(Photo("IMG_1.JPG", b"x" * 100, MAY))
        self.sign_in()
        self.engine.close()
        self.fake.account.web_tokens.clear()        # overnight
        self.engine = self.Engine()                 # a new process
        self.assertEqual(self.sync()["downloaded"], 1)

    def test_revoked_session_needs_sign_in(self):
        from dkicloudphotos.photos import AuthRequired

        self.sign_in()
        self.engine.close()
        self.fake.account.web_tokens.clear()
        self.fake.account.session_tokens.clear()    # e.g. signed out on icloud.com
        self.engine = self.Engine()
        with self.assertRaises(AuthRequired):
            self.engine.albums()
        self.assertFalse(self.engine.status()["signed_in"])
        self.assertTrue(self.engine.status()["has_account"])

    def test_advanced_data_protection_asks_for_photos_keys(self):
        self.fake.state["adp"] = True
        self.fake.library.add(Photo("IMG_1.JPG", b"x" * 100, MAY))
        self.sign_in()
        self.assertEqual(self.fake.state["pcs_requests"], ["photos"])
        self.assertEqual(self.sync()["downloaded"], 1)


class Albums(Base):
    def test_smart_and_user_albums_with_folders_and_counts(self):
        lib = self.fake.library
        a, b, c = lib.add(Photo("A.JPG", b"a", MAY, favorite=True),
                          Photo("B.MOV", b"b", MAY + 1, uti="com.apple.quicktime-movie"),
                          Photo("C.HEIC", b"c", MAY + 2, uti="public.heic", live=b"cv"))
        folder = lib.album("Travel", folder=True)
        lib.album("Italy", a, b, parent=folder)
        lib.album("Cats", c)
        for i in range(4):                      # enough albums to need continuation markers
            lib.album(f"Empty {i}")
        self.sign_in()
        albums = {x["name"]: x for x in self.engine.albums()["albums"]}
        self.assertEqual(albums["All Photos"]["count"], 3)
        self.assertEqual(albums["Favorites"]["count"], 1)
        self.assertEqual(albums["Videos"]["count"], 1)
        self.assertEqual(albums["Live Photos"]["count"], 1)
        self.assertEqual(albums["Italy"]["path"], "Travel/Italy")
        self.assertEqual(albums["Italy"]["count"], 2)
        self.assertEqual(albums["Cats"]["kind"], "album")
        self.assertIn("Empty 3", albums)
        self.assertNotIn("Travel", albums, "folders are not albums")
        self.assertNotIn("Panoramas", albums, "empty smart albums are left out")
        self.assertNotIn("root", albums)

    def test_indexing_not_finished(self):
        from dkicloudphotos.photos import PhotosError

        self.fake.library.indexing = "RUNNING"
        self.sign_in()
        with self.assertRaisesRegex(PhotosError, "preparing"):
            self.engine.albums()

    def test_shared_library_listed(self):
        self.fake.library.shared_zone = "SharedSync-1234"
        self.fake.library.shared_photos.append(Photo("S.JPG", b"shared", JUNE))
        self.sign_in()
        libs = self.engine.libraries()["libraries"]
        self.assertEqual([x["id"] for x in libs], ["private/PrimarySync", "shared/SharedSync-1234"])
        self.sync(library="shared/SharedSync-1234")
        self.assertEqual(self.files(), ["2024/06/S.JPG"])


class Sync(Base):
    def setUp(self) -> None:
        super().setUp()
        lib = self.fake.library
        self.a, self.b, self.c = lib.add(
            Photo("IMG_0001.JPG", b"one" * 400, MAY, favorite=True, caption="Lake"),
            Photo("IMG_0002.HEIC", b"two" * 300, JUNE, uti="public.heic", live=b"live" * 50),
            Photo("Clip 1.MOV", b"vid" * 700, JUNE + 60, uti="com.apple.quicktime-movie", plain_name=True),
        )
        lib.add(Photo("Hidden.JPG", b"h", JUNE, hidden=True))
        self.trip = lib.album("Trip", self.a, self.b)
        self.sign_in()

    def test_downloads_into_year_month_folders(self):
        result = self.sync()
        self.assertEqual(self.files(), ["2024/05/IMG_0001.JPG", "2024/06/Clip 1.MOV",
                                        "2024/06/IMG_0002.HEIC", "2024/06/IMG_0002.MOV"])
        self.assertEqual((self.dest / "2024/05/IMG_0001.JPG").read_bytes(), self.a.data)
        self.assertEqual((self.dest / "2024/06/IMG_0002.MOV").read_bytes(), self.b.live)
        self.assertAlmostEqual((self.dest / "2024/05/IMG_0001.JPG").stat().st_mtime, MAY, delta=1)
        self.assertEqual(result["downloaded"], 4)
        self.assertEqual(result["listed"], 3)
        self.assertEqual(self.of("total"), [{"total": 4, "bytes": 1200 + 900 + 200 + 2100}])
        # The ${f} placeholder of the signed URL carries the file name.
        self.assertIn(("IMG_0001.JPG"), [name for _m, _r, name in self.fake.state["downloads"]])
        self.assertFalse(list(self.dest.rglob("*.part")))

        events = {Path(f["path"]).name: f for f in self.of("file")}
        one = events["IMG_0001.JPG"]
        self.assertEqual(one["status"], "downloaded")
        self.assertEqual(one["asset"]["favorite"], True)
        self.assertEqual(one["asset"]["caption"], "Lake")
        self.assertEqual(one["asset"]["albums"], ["Trip"])
        self.assertEqual(one["asset"]["role"], "original")
        self.assertEqual(one["asset"]["kind"], "image")
        self.assertEqual(events["IMG_0002.MOV"]["asset"]["role"], "live")
        self.assertEqual(events["IMG_0002.MOV"]["asset"]["kind"], "video")
        self.assertEqual(events["Clip 1.MOV"]["asset"]["albums"], [])
        self.assertEqual(self.of("progress")[-1]["done"], 4)

    def test_second_sync_downloads_nothing(self):
        self.sync()
        downloads = len(self.fake.state["downloads"])
        result = self.sync()
        self.assertEqual(result["downloaded"], 0)
        self.assertEqual(result["skipped"], 4)
        self.assertEqual(len(self.fake.state["downloads"]), downloads)
        self.assertEqual(self.of("file"), [])
        # Only new photos come down later.
        self.fake.library.add(Photo("IMG_0003.JPG", b"three", JUNE + 3600))
        result = self.sync()
        self.assertEqual(result["downloaded"], 1)
        self.assertIn("2024/06/IMG_0003.JPG", self.files())

    def test_deleted_here_stays_deleted_unless_asked(self):
        self.sync()
        (self.dest / "2024/05/IMG_0001.JPG").unlink()
        result = self.sync()
        self.assertEqual(result["deleted_locally"], 1)
        self.assertNotIn("2024/05/IMG_0001.JPG", self.files())
        result = self.sync(redownload_deleted=True)
        self.assertEqual(result["downloaded"], 1)
        self.assertEqual(self.files(), ["2024/05/IMG_0001.JPG", "2024/06/Clip 1.MOV",
                                        "2024/06/IMG_0002.HEIC", "2024/06/IMG_0002.MOV"])

    def test_same_name_same_month_gets_numbered_and_live_video_follows(self):
        self.fake.library.add(Photo("IMG_0002.HEIC", b"other" * 10, JUNE + 5, uti="public.heic", live=b"lv"))
        self.sync()
        files = self.files()
        self.assertIn("2024/06/IMG_0002.HEIC", files)
        self.assertIn("2024/06/IMG_0002_1.HEIC", files)
        self.assertIn("2024/06/IMG_0002_1.MOV", files)
        self.assertEqual((self.dest / "2024/06/IMG_0002_1.MOV").read_bytes(), b"lv")

    def test_existing_identical_file_is_adopted(self):
        (self.dest / "2024/05").mkdir(parents=True)
        (self.dest / "2024/05/IMG_0001.JPG").write_bytes(self.a.data)
        (self.dest / "2024/06").mkdir(parents=True)
        (self.dest / "2024/06/IMG_0002.HEIC").write_bytes(b"different size")
        result = self.sync()
        self.assertEqual(result["adopted"], 1)
        self.assertEqual(result["downloaded"], 3)
        self.assertIn("2024/06/IMG_0002_1.HEIC", self.files())
        self.assertIn("2024/06/IMG_0002_1.MOV", self.files())
        self.assertEqual((self.dest / "2024/06/IMG_0002.HEIC").read_bytes(), b"different size")
        adopted = [f for f in self.of("file") if f["status"] == "adopted"]
        self.assertEqual([Path(f["path"]).name for f in adopted], ["IMG_0001.JPG"])
        self.assertEqual(self.sync()["skipped"], 4)

    def test_options(self):
        self.fake.library.add(Photo("IMG_0009.JPG", b"orig", JUNE + 9, edited=b"edited render"))
        result = self.sync(layout="flat", include_videos=False, live_photos=False, edited=True)
        self.assertEqual(self.files(), ["IMG_0001.JPG", "IMG_0002.HEIC", "IMG_0009-edited.JPG", "IMG_0009.JPG"])
        self.assertEqual(result["downloaded"], 4)

    def test_album_selection_and_dry_run(self):
        result = self.sync(albums=[self.trip], dry_run=True)
        self.assertEqual(result["would_download"], 3)
        self.assertEqual(self.files(), [])
        self.sync(albums=["Trip"], album_tags=False)
        self.assertEqual(self.files(), ["2024/05/IMG_0001.JPG", "2024/06/IMG_0002.HEIC", "2024/06/IMG_0002.MOV"])
        self.assertEqual(self.of("file")[0]["asset"]["albums"], [])

    def test_unknown_album(self):
        from dkicloudphotos.photos import PhotosError

        with self.assertRaisesRegex(PhotosError, "Nope"):
            self.sync(albums=["Nope"])

    def test_failed_download_is_reported_and_others_continue(self):
        self.fake.state["fail_downloads"].add(self.a.master)
        result = self.sync()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["downloaded"], 3)
        self.assertEqual(self.of("error")[0]["filename"], "IMG_0001.JPG")
        self.fake.state["fail_downloads"].clear()
        self.assertEqual(self.sync()["downloaded"], 1, "a failed file is tried again next time")

    def test_limit(self):
        self.assertEqual(self.sync(limit=2)["downloaded"], 2)
        self.assertEqual(self.sync()["downloaded"], 2)

    def test_cancel_mid_download_leaves_no_partial_file(self):
        from dkicloudphotos.photos import Cancelled

        self.fake.state["content_delay"] = 0.3
        timer = threading.Timer(0.5, self.engine.cancel.set)
        timer.start()
        with self.assertRaises(Cancelled):
            self.sync()
        timer.cancel()
        self.assertFalse(list(self.dest.rglob("*.part")))
        self.engine.cancel.clear()
        self.fake.state["content_delay"] = 0.0
        self.sync()
        self.assertEqual(len(self.files()), 4)

    def test_second_engine_is_told_busy(self):
        from dkicloudphotos.engine import Busy

        fd = os.open(self.engine.paths.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with self.assertRaises(Busy):
                self.sync()
        finally:
            os.close(fd)

    def test_relative_destination_refused(self):
        with self.assertRaises(ValueError):
            self.engine.sync("relative/path")


class Upload(Base):
    def setUp(self) -> None:
        super().setUp()
        self.fake.library.add(Photo("FROM_ICLOUD.JPG", b"cloud" * 10, MAY))
        self.sign_in()
        self.local = self.tmp / "local"
        self.local.mkdir()

    def write(self, name: str, data: bytes) -> str:
        path = self.local / name
        path.write_bytes(data)
        return str(path)

    def upload(self, files, **kw):
        self.events.clear()
        return self.engine.upload(files, emit=self.emit, **kw)

    def test_upload_once_and_skip_after(self):
        new = self.write("new.jpg", b"\xff\xd8 new photo")
        result = self.upload([new])
        self.assertEqual(result, {"uploaded": 1, "skipped": 0, "failed": 0})
        self.assertEqual(self.fake.state["uploads"], ["new.jpg"])
        copy = self.write("copy of new.jpg", b"\xff\xd8 new photo")
        result = self.upload([new, copy])
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(self.of("file")[0]["message"], "Uploaded before")
        self.assertEqual(self.upload([new], force=True)["uploaded"], 1)

    def test_files_from_icloud_are_not_sent_back(self):
        self.engine.sync(str(self.dest))
        path = str(self.dest / "2024/05/FROM_ICLOUD.JPG")
        result = self.upload([path])
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.of("file")[0]["message"], "Came from iCloud Photos")
        self.assertEqual(self.fake.state["uploads"], [])

    def test_failures(self):
        text = self.write("notes.txt", b"hello")
        refused = self.write("weird.jpg", b"\xff\xd8 x")
        self.fake.state["refuse_uploads"].add("weird.jpg")
        result = self.upload([text, refused, str(self.local / "missing.jpg")])
        self.assertEqual(result, {"uploaded": 0, "skipped": 0, "failed": 3})
        messages = [f["message"] for f in self.of("file")]
        self.assertEqual(messages, ["Not a photo or video", "Unsupported file type", "File not found"])

    def test_uploaded_photo_is_synced_as_adopted_not_downloaded_twice(self):
        # Uploading puts the photo in iCloud; a later sync into the same
        # folder finds the identical file there.
        self.engine.sync(str(self.dest), layout="flat")
        local = self.write("IMG_7.JPG", b"\xff\xd8 seven")
        self.upload([local])
        shutil.copy(local, self.dest / "IMG_7.JPG")
        result = self.engine.sync(str(self.dest), layout="flat")
        self.assertEqual(result["adopted"], 1)
        self.assertEqual(result["downloaded"], 0)


if __name__ == "__main__":
    unittest.main()
