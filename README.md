# digiKam iCloud Photos

A digiKam plugin that syncs your **iCloud Photos** library with digiKam on Linux:

- **Import ▸ Import from iCloud Photos…** downloads your library (or chosen
  albums) into a digiKam album, incrementally: each run only fetches what is
  new. iCloud albums become digiKam tags (`iCloud Photos/<album>`), favorites
  get 5 stars, and captions become comments.
- **Export ▸ Export to iCloud Photos…** uploads the selected photos and
  videos to your iCloud Photos library.
- The same engine works from a terminal (`digikam-icloud-photos sync …`), so
  a timer can keep the folder up to date while digiKam is closed.

> **This uses Apple's private web API.** There is no public iCloud Photos
> API. This tool makes the same requests as the Photos app on icloud.com:
> SRP sign-in at idmsa.apple.com, then CloudKit calls. Apple can change these
> requests at any time and break the tool. Use it for your own account only.
> Your password is used once to sign in and is never stored. Only the
> resulting session cookies are kept, in a file only you can read.

## What a sync does

- **iCloud → this computer only.** A sync never deletes or changes anything
  in iCloud, and it never deletes local files.
- Files are saved as originals (HEIC, JPEG, MOV…) under
  `<destination>/YYYY/MM/`, or all in one folder. Each file's modification
  time is set to the date the photo was taken.
- The video half of a Live Photo is saved next to the photo
  (`IMG_1234.HEIC` + `IMG_1234.MOV`). Edited renders are optional
  (`IMG_1234-edited.JPG`).
- Photos are matched by their iCloud record, not by name. Running a sync
  again downloads only new photos. Adding a photo to another album doesn't
  download it twice.
- **Deleting a synced file in digiKam keeps it deleted.** The next sync
  doesn't bring it back unless you tick *Download again photos I deleted
  here*.
- If a different photo already has the same name in that folder (iPhone file
  counters repeat), the new one is saved as `IMG_1234_1.HEIC`. A file of the
  same name *and* size that was already there (copied in earlier by other
  means) is adopted rather than downloaded again.
- Videos are downloaded too, as their original files (MOV, MP4). Untick
  *Include videos* (CLI: `--no-videos`) to skip them, or sync only the
  *Videos* album. Edited versions are only saved for photos: a trimmed video
  arrives as its full original, and slo-mo and time-lapse arrive as
  recorded.
- Hidden photos and Recently Deleted are left out.

Uploading skips files that came from iCloud through a sync, and files that
were uploaded before (matched by SHA-256). Uploads go into the library; they
can't be added to an iCloud album yet.

## Install (Arch Linux / Omarchy)

```sh
cd packaging/arch
makepkg -si
```

This builds the plugin against the digiKam you have installed and installs:

| What | Where |
|---|---|
| the plugin | `/usr/lib/qt6/plugins/digikam/generic/Generic_ICloudPhotos_Plugin.so` |
| the engine (Python) | `/usr/share/digikam-icloud-photos/dkicloudphotos/` |
| the command line | `/usr/bin/digikam-icloud-photos` |

Restart digiKam afterwards. **After each digiKam upgrade, rebuild with
`makepkg -sif`.** The plugin is compiled against digiKam's own libraries.

Runtime needs: digiKam 9.1 or newer, Python 3.10+ with `requests`.

Also install ExifTool (recommended):

```sh
sudo pacman -S perl-image-exiftool
```

digiKam needs it to write album tags, ratings and captions into HEIC photos
and videos, which is most of what an iPhone takes. Without it those files are
still imported, and favorites still get 5 stars in digiKam's database, but
they get no album tags.

### Other distributions

You need the digiKam development files, Qt 6, CMake and a C++17 compiler:

```sh
cmake -S . -B build -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release
cmake --build build
sudo cmake --install build
```

## Use it in digiKam

1. **Import ▸ Import from iCloud Photos…**
2. Sign in with your Apple Account. Enter the code shown on your iPhone, or
   choose *Text me instead*. With Advanced Data Protection, approve web
   access on your iPhone when asked (Settings ▸ [your name] ▸ iCloud ▸
   *Access iCloud Data on the Web* must be on).
3. Tick the albums to sync (*All Photos* by default), pick the destination
   digiKam album, choose options, then click **Sync**.

You only sign in once. The session is renewed automatically until Apple
ends it (sign-out on icloud.com, password change, or roughly two months for
the 2FA trust). Then the sign-in dialog comes back.

## Command line

```sh
digikam-icloud-photos sign-in                # password + 2FA code, once
digikam-icloud-photos albums                 # albums with photo counts and ids
digikam-icloud-photos sync ~/Pictures/iCloud --dry-run
digikam-icloud-photos sync ~/Pictures/iCloud --album "All Photos"
digikam-icloud-photos sync ~/Pictures/iCloud --album Travel/Italy --layout flat --edited
digikam-icloud-photos upload ~/Pictures/scans/*.jpg
```

The command line and the plugin share the sign-in and the record of what
was synced. Only one sync or upload per account runs at a time.

### Keep it in sync automatically

`~/.config/systemd/user/icloud-photos-sync.service`:

```ini
[Unit]
Description=Download new iCloud Photos

[Service]
Type=oneshot
ExecStart=/usr/bin/digikam-icloud-photos sync %h/Pictures/iCloud --quiet
```

`~/.config/systemd/user/icloud-photos-sync.timer`:

```ini
[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

```sh
systemctl --user enable --now icloud-photos-sync.timer
```

digiKam picks up the new files when it scans its collections, at startup or
via *Tools ▸ Maintenance*. Files that arrive through the timer don't get
album tags; syncing from the plugin does that.

## Files

`~/.local/share/digikam-icloud-photos/`:

- `session.json`: the iCloud session (cookies and tokens, mode 600)
- `state-<account id>.db`: what was downloaded and uploaded
- `engine.log`: what the engine did (no passwords, no download URLs)

To start over, sign out (plugin: *Change Account*, CLI: `sign-out`). Don't
delete the state database while keeping the files. The next sync would
adopt only files whose size still matches iCloud. Files whose tags digiKam
wrote into them have changed size, so they would be downloaded a second
time with a `_1` name.

## How it works

```
digiKam ── Generic_ICloudPhotos_Plugin.so (C++, Qt dialogs)
               │  JSON Lines over a pipe (docs/PROTOCOL.md)
               ▼
           python3 -m dkicloudphotos serve
               │  sign-in: idmsa.apple.com (SRP-6a, 2FA) → setup.icloud.com
               │  listing: CloudKit com.apple.photos.cloud (ckdatabasews)
               │  files:   signed content URLs; uploads: uploadimagews
               ▼
           iCloud Photos
```

When a photo arrives, the plugin writes the tags, rating and caption through
digiKam's own metadata code, which follows your digiKam metadata settings
(write to file or to an XMP sidecar). It then asks digiKam to scan the file
into its database.

The sign-in code comes from [icloud4linux](https://github.com/popovoleksandr/icloud4linux),
the iCloud Drive client by the same author.

## Development

```sh
python3 -m unittest discover -s tests -t . -v      # engine: fake iCloud, no network
cmake -S . -B build -DBUILD_TESTING=ON && cmake --build build -j
ctest --test-dir build --output-on-failure          # plugin: loads it through digiKam's loader
```

`python3 tests/fake_icloud.py` starts a fake iCloud and prints its URL. Point
the engine at it with `DKICLOUD_TEST_SERVER=<url> DKICLOUD_HOME=$(mktemp -d)`.

To try a build in digiKam without installing it:

```sh
DK_PLUGIN_PATH="$PWD/build/plugin;/usr/lib/qt6/plugins/digikam" \
DKICLOUD_ENGINE_DIR="$PWD/engine" digikam
```

`DK_PLUGIN_PATH` *replaces* digiKam's plugin folder, so keep the system one
in the list (folders are separated by `;`). `DKICLOUD_ENGINE_DIR` makes the
plugin run the engine from this checkout.

## Limitations

- Download only from iCloud; deletions and edits are not synced either way.
- Uploads can't go into an iCloud album.
- Album tags are added to newly downloaded photos only.
- iCloud *Shared Albums* (the invitation-based kind) are not supported. The
  iCloud *Shared Photo Library* is.
- Security-key sign-in (FIDO) is not supported. Codes from a trusted device
  or by SMS are.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE).
