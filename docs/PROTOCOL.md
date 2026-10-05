# Plugin ↔ engine protocol

The digiKam plugin (C++) starts the engine (Python) as a child process:

    python3 -m dkicloudphotos serve

with `PYTHONPATH` pointing at the directory that contains the `dkicloudphotos`
package. They talk over the engine's stdin/stdout in **JSON Lines**: one
UTF-8 JSON object per line, `\n`-terminated. stderr is free-form diagnostics
(the plugin may log it; it is never parsed). The engine also writes its own
log to `~/.local/share/digikam-icloud-photos/engine.log`.

## Messages

Plugin → engine (request):

```json
{"id": 7, "cmd": "sync", "args": {"dest": "/home/me/Pictures/iCloud"}}
```

`id` is a positive integer chosen by the plugin, unique per request. `args`
may be omitted when empty.

Engine → plugin, final reply (exactly one per request):

```json
{"id": 7, "ok": true, "result": {"downloaded": 3}}
{"id": 7, "ok": false, "error": {"kind": "network", "message": "Could not reach Apple"}}
```

Engine → plugin, events while a request runs (zero or more, always before
the reply, carrying the request's `id`):

```json
{"id": 7, "event": "progress", "done": 1, "total": 3}
```

Error `kind` values: `auth` (sign-in refused, message is for the user),
`not_signed_in` (session gone: show the sign-in dialog), `wrong_code`
(2FA code rejected: let the user retry), `network`, `cancelled`, `busy`
(another engine, e.g. a scheduled CLI sync, is syncing the same account),
`bad_request` (unknown command or bad args), `error` (anything else). `message` is always a human-readable
sentence suitable for a message box.

## Concurrency

Requests are queued and run one at a time in a worker thread, in the order
received, so the plugin may send several without waiting. `hello`, `status`,
`cancel` and `shutdown` skip the queue and are answered right away. `cancel`
asks the running request to stop (between files, or inside a transfer) and
drops queued ones; each of them replies with
`{"ok": false, "error": {"kind": "cancelled"}}`.

Closing stdin (or `shutdown`) makes the engine exit after cancelling the
running request.

## Commands

### `hello`
→ `{"version": "1.0.0", "protocol": 1}`

### `status`
→ `{"signed_in": bool, "has_account": bool, "apple_id": str, "name": str, "email": str}`

`has_account` true + `signed_in` false means the saved session expired.

### `sign_in` `{apple_id, password, china_mainland?: bool}`
→ `{"status": "ok", "account": {"name", "email", "dsid"}}`
or a two-factor challenge:
`{"status": "2fa", "delivery": "device"|"sms", "sent_to": str|null, "phones": [{"id", "number"}], "length": 6}`

The password is only ever sent through this pipe; it is never stored.
May emit `message` events (see below) while unlocking Advanced Data Protection.

### `send_code` `{method: "device"|"sms", phone_id?}`
→ the challenge object again (now with the new delivery).

### `verify_code` `{code}`
→ `{"status": "ok", "account": {...}}`; error kind `wrong_code` if rejected.

### `sign_out`
→ `{}`

### `libraries`
→ `{"libraries": [{"id": "private/PrimarySync", "name": "Personal Library"}, {"id": "shared/SharedSync-…", "name": "Shared Library"}]}`

### `albums` `{library?: str}` (default `private/PrimarySync`)
→
```json
{"albums": [
  {"id": "all",            "name": "All Photos", "kind": "smart", "count": 1234},
  {"id": "smart:FAVORITE", "name": "Favorites",  "kind": "smart", "count": 12},
  {"id": "smart:VIDEO",    "name": "Videos",     "kind": "smart", "count": 40},
  {"id": "9C2D…",          "name": "Trip",       "kind": "album", "path": "Travel/Trip", "count": 40}
]}
```
`path` is the album's place in iCloud folders ("Folder/Album"); equal to
`name` for top-level albums. `count` may be `null` when unknown.

### `sync`
Downloads what is not on this computer yet.

Args:

| key | type | default | meaning |
|---|---|---|---|
| `dest` | str | required | absolute folder (inside a digiKam collection) |
| `library` | str | `private/PrimarySync` | from `libraries` |
| `albums` | [str] | `["all"]` | album ids from `albums` |
| `layout` | `"date"`/`"flat"` | `"date"` | `date`: `dest/YYYY/MM/name`, `flat`: `dest/name` |
| `include_videos` | bool | true | videos (not Live Photo companions) |
| `live_photos` | bool | true | also save the video half of Live Photos (`IMG_1.MOV` next to `IMG_1.HEIC`) |
| `edited` | bool | false | also save edited renders as `name-edited.ext` |
| `album_tags` | bool | true | report each photo's user albums (costs extra listing requests) |
| `redownload_deleted` | bool | false | download again files that were synced before but are gone locally |
| `limit` | int | 0 | stop after this many downloads (0 = no limit) |
| `dry_run` | bool | false | list and count, download nothing |

Events:

```json
{"id": 7, "event": "phase", "text": "Listing All Photos…"}
{"id": 7, "event": "total", "total": 120, "bytes": 734003200}
{"id": 7, "event": "file", "path": "/abs/2024/05/IMG_1.HEIC", "status": "downloaded",
 "done": 1, "total": 120,
 "asset": {"id": "AbC…", "filename": "IMG_1.HEIC", "date": "2024-05-01T10:00:00+00:00",
           "favorite": true, "albums": ["Travel/Trip"], "caption": "",
           "kind": "image", "role": "original"}}
{"id": 7, "event": "progress", "done": 1, "total": 120, "bytes_done": 4194304, "bytes_total": 734003200}
{"id": 7, "event": "error", "filename": "IMG_2.HEIC", "message": "HTTP 500"}
```

`file.status`: `downloaded` (new file written) or `adopted` (a file with the
same name and size already existed and was not known yet; it is now tracked
and not downloaded again). `asset.kind`: `image` | `video`. `asset.role`:
`original` | `live` (Live Photo video) | `edited`. The plugin registers every
`file` path with digiKam. `progress` is throttled (about 4 per second).
Per-file failures are reported as `error` events and the sync goes on.

Result:
`{"listed": n, "downloaded": n, "adopted": n, "skipped": n, "deleted_locally": n, "failed": n, "bytes": n}`

### `upload` `{files: [abs paths], force?: bool}`
Uploads local files to the iCloud Photos library (not into an album).
Files that came from iCloud through `sync`, or that were uploaded before
(same SHA-256), are skipped unless `force` is true.

Events:
```json
{"id": 9, "event": "file", "path": "/abs/x.jpg", "status": "uploaded", "done": 1, "total": 4}
{"id": 9, "event": "file", "path": "/abs/y.jpg", "status": "skipped", "message": "Already in iCloud Photos", "done": 2, "total": 4}
{"id": 9, "event": "file", "path": "/abs/z.txt", "status": "failed", "message": "Not a photo or video", "done": 3, "total": 4}
```
Result: `{"uploaded": n, "skipped": n, "failed": n}`

### `cancel`
→ `{}` (acknowledged immediately; see Concurrency).

### `shutdown`
→ `{}` then the engine exits.

### Event `message`
Any long request may emit `{"id": n, "event": "message", "text": "Allow access on your iPhone…"}`:
a status line for the user (shown in the dialog, no action needed).
