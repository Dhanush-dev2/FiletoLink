# Telegram File-to-Link Bot

A zero-cost Telegram bot that receives files from users and generates a public
link that can be used to access/download those files.

> **Status — Phase 15 (deployment readiness: reproducible startup, restart,
> and backup for the same Windows/local, ₹0-cost deployment — see
> **Deployment & operations** below). Earlier work — Phase 14B file
> ownership: FileToLink keeps its own copy of every received file, so
> downloads no longer depend on the Telegram Local Bot API Server's file
> storage; security & abuse protection (per-client rate limiting, security
> headers, MIME hardening, HEAD+Range).
> Everything runs locally at ₹0
> cost. **Mock mode** (no `BOT_TOKEN`, no network) remains the default:
> `python run.py` gives you a chat loop, sending files yields metadata →
> SQLite → public id → working download URL via `python run.py --api`. A
> **real Telegram transport** is implemented (`python run.py --telegram`):
> python-telegram-bot in `local_mode` against a self-hosted Local Bot API
> Server (`--local`), normalizing updates into the same pipeline. The real
> end-to-end run requires the Local Bot API Server + a real bot to be set up
> (see below).

## What it does

```
User → Telegram bot → backend processes file → unique public link → user
opens link → file is served/downloaded
```

## Architecture

```
                    TELEGRAM
                       │
                       ▼
             Telegram transport ──┐
        (mock today, real later)  │
                       │          │
                       ▼          ▼
                 app/bot/handlers.py   (framework-agnostic bot logic)
                       │
                       ▼
        app/services/storage.py  → FileToLink-owned files
        (real mode: the received file is transferred out of the        │
         Local Bot API Server's work dir before any link exists)     │
                       │                                              │
                       ▼                                              │
                  SQLite (data/files.db, source_path in storage)      │
                       │                                              │
                       ▼                                              │
                  Download endpoint (GET /f/{public_id}) ──────────────┘
                       │
                       ▼
                      User
```

The key design choice: bot logic (`app/bot/handlers.py`) only sees normalized
update objects (`app/bot/models.py`). The **mock transport**
(`app/bot/mock.py`) and the future **real integration** (`python-telegram-bot`
polling) both plug into the same handlers, so switching to the real bot later
does not touch any bot logic.

### Why this is zero-cost

- **Mock transport** — the project runs with no bot token and no network.
- **Python + FastAPI + SQLite + Uvicorn** — all free and open source.
- **Local development** — everything runs on your own machine.
- **No paid services** — no paid hosting, databases, storage, or CDNs.
  Free-tier options are only considered in a later deployment phase, and
  never assumed to be permanently free.

## Requirements

- Python 3.10+ (developed against 3.12)
- Git
- (Later) a Telegram bot created via [@BotFather](https://t.me/BotFather)

## Installation

```bash
# 1. Create a virtual environment
python -m venv .venv

# 2. Activate it
#    Windows (Git Bash):
source .venv/Scripts/activate
#    Linux/macOS:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. (Optional) Create your local config
cp .env.example .env
```

The `.env` file is optional in this milestone — sensible defaults are built in.
Never commit it.

## Environment variables

| Variable       | Default                | Purpose                                        |
| -------------- | ---------------------- | ---------------------------------------------- |
| `BOT_TOKEN`    | *(unset)*              | Real bot token from @BotFather (real mode)     |
| `BOT_USERNAME` | `file_to_link_bot`     | Placeholder bot username                       |
| `BOT_MODE`     | `mock` (auto)          | `mock` or `real`                               |
| `TELEGRAM_API_ID` | *(unset)*           | Free api_id for the Local Bot API Server       |
| `TELEGRAM_API_HASH` | *(unset)*         | Free api_hash for the Local Bot API Server     |
| `LOCAL_BOT_API_URL` | `http://127.0.0.1:8081` | Your self-hosted Local Bot API Server      |
| `TELEGRAM_BOT_WORK_DIR` | `telegram_bot_data/` | Server's file storage dir (ingestion source, not long-term storage) |
| `FILE_STORAGE_DIR` | `data/storage/` | FileToLink's **own** storage for received files (must differ from `TELEGRAM_BOT_WORK_DIR`) |
| `TELEGRAM_GET_FILE_READ_TIMEOUT_SECONDS` | `300` | Read timeout for the Local Bot API `getFile` request (large files) |
| `BASE_URL`     | `http://localhost:8000`| Base URL for generated links (`{BASE_URL}/f/{id}`) |
| `HOST`         | `127.0.0.1`            | Download API bind host                         |
| `PORT`         | `8000`                 | Download API port                              |
| `DATABASE_PATH` | `data/files.db`  | SQLite database file (relative to project root) |
| `RATE_LIMIT_ENABLED` | `true`         | Throttle `GET`/`HEAD /f/{id}` per client        |
| `RATE_LIMIT_REQUESTS` | `60`          | Requests allowed per window                     |
| `RATE_LIMIT_WINDOW_SECONDS` | `60`    | Window length for the limit                     |
| `RATE_LIMIT_MAX_KEYS` | `10000`      | Max tracked clients (bounds limiter memory)     |
| `TRUST_PROXY_HEADERS` | `false`      | Trust `X-Forwarded-For` (only behind a proxy)   |
| `TRUSTED_PROXIES` | *(unset)*          | Comma-separated proxy IPs allowed to set it     |

## Mock vs real mode

| | Mock (default) | Real |
|---|---|---|
| Requires `BOT_TOKEN`? | No | Yes |
| Requires Local Bot API Server? | No | Yes |
| Network access | None | Local server only |
| Files come from | Local paths you `send` | Real Telegram users |
| Run with | `python run.py` | `python run.py --telegram` or `BOT_MODE=real` |

`BOT_MODE=mock` is the safe default whenever real Telegram configuration is
absent. Both transports feed the **same** normalized updates into the same
handlers, SQLite, FileService, and download endpoint.

## Running the bot (mock)

```bash
python run.py
```

or directly:

```bash
python -m app.bot.mock
```

## Real Telegram mode

The bot connects to a **self-hosted Telegram Local Bot API Server** running
with `--local`, using **long polling** — no public IP, no webhooks, no TLS
needed for development. File bytes are never re-downloaded through the
remote Telegram URL: the local server already stored them and `get_file`
returns the absolute local path. `telegram_bot_data/` is only the
*ingestion source* — the file is then taken into FileToLink's own storage
(see **File ownership** below) before any link is created.

### 1. Create the bot and get credentials

- **`BOT_TOKEN`** — talk to [@BotFather](https://t.me/BotFather):
  `/newbot` → copy the token into `.env`.
- **`TELEGRAM_API_ID` / `TELEGRAM_API_HASH`** — free, from
  [my.telegram.org](https://my.telegram.org) → API development tools. These
  are for the **Local Bot API Server** (different from `BOT_TOKEN`).

### 2. Run the Local Bot API Server (`--local`)

Official source: [`tdlib/telegram-bot-api`](https://github.com/tdlib/telegram-bot-api)
(Boost license — free). It needs `api_id`/`api_hash` and stores received
files under a working directory (`--dir`).

**Linux (easiest):**

```bash
sudo apt install gperf cmake g++ libssl-dev zlib1g-dev
mkdir -p ~/telegram-bot-api-build && cd ~/telegram-bot-api-build
git clone --recursive https://github.com/tdlib/telegram-bot-api.git
cd telegram-bot-api
cmake -DCMAKE_BUILD_TYPE=Release .
cmake --build . --target install
# run it (keep the work dir in sync with TELEGRAM_BOT_WORK_DIR):
telegram-bot-api --local --api-id=YOUR_ID --api-hash=YOUR_HASH --dir=$HOME/telegram-bot-data --http-port=8081
```

**Windows:** two options —
1. *WSL2 (recommended):* follow the Linux steps inside a WSL2 Ubuntu distro,
   then reach the server from Windows at `http://127.0.0.1:8081`.
2. *Native:* use the official build-instructions generator
   ([Windows](https://tdlib.github.io/telegram-bot-api/build.html?os=Windows))
   with Visual Studio C++ + CMake, or a community prebuilt binary.

### 3. Configure and run

```bash
# .env
BOT_MODE=real
BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_API_ID=12345
TELEGRAM_API_HASH=deadbeef
LOCAL_BOT_API_URL=http://127.0.0.1:8081
TELEGRAM_BOT_WORK_DIR=telegram_bot_data
TELEGRAM_GET_FILE_READ_TIMEOUT_SECONDS=300
FILE_STORAGE_DIR=data/storage

python run.py --telegram   # or: python run.py (BOT_MODE=real)
```

Startup is fail-fast and secret-free: missing `BOT_TOKEN`/API credentials or
an unreachable local server produce clear error messages and exit code 1.

### How the real file flow works

```
Telegram user sends file
   ↓ Local Bot API Server (--local) stores bytes in its work dir
python-telegram-bot (base_url + local_mode) receives update
   ↓ normalize_update() → our Message/Document models
bot.get_file(file_id, read_timeout=...) → absolute local path
   ↓ FileStorage.adopt() → the file is MOVED (or streamed, see below)
     into FILE_STORAGE_DIR/<public_id>/<filename>; destination verified
   ↓ FileService.save_document() → SQLite (public_id + owned source_path)
   ↓ reply with the link (only after the file and the record exist)
FastAPI → StorageFileSource (FILE_STORAGE_DIR enforced) → /f/{public_id}
```

The transfer happens **before** the database record and the public id, so a
failed transfer creates no link, and an `active` record always has a valid
FileToLink-owned file. If the transfer fails the user gets an error reply and
nothing is stored.

Both contained sources share one implementation: `StorageFileSource`
(FileToLink storage) and `TelegramFileSource` (the Local Bot API work dir)
only serve files physically inside their own root — traversal, absolute paths
outside it, and symlinks escaping it are rejected, and filesystem paths are
never shown to users.

## File ownership (Phase 14B)

The Telegram Local Bot API Server manages the files in its own work
directory and may evict them at any time. A link that pointed there could
silently break — a 400+ MB MKV was successfully stored and served, then
vanished from the server's directory while the database record stayed active.

FileToLink therefore takes ownership of every received file:

- **`FILE_STORAGE_DIR`** (default `data/storage/`) is FileToLink's permanent
  storage; its layout is one directory per link:
  `data/storage/<public_id>/<filename>`. It is created when needed and must
  not be the same directory as `TELEGRAM_BOT_WORK_DIR` (validated at
  startup).- **A move when it is safe**: if the Telegram file and FileToLink storage are
  on the same filesystem (the usual case on one Windows drive) the file is
  moved with an atomic rename — instant, and with no second copy of a 400+ MB
  file. Otherwise (different drive, or a rename that is not permitted) it is
  **streamed** in 1 MiB chunks into a `.part` file, size-verified, moved into
  place, and only then is the source removed. The whole file is never read
  into memory (`read_bytes()`/`bytes(file)` are not used anywhere).
- **Never loses the file**: the source is deleted only after the destination
  is fully written and verified; if the transfer fails, the source stays put
  and the partial destination is removed.
- **No orphans**: if the record cannot be written after a successful transfer,
  the adopted file is rolled back, so no unreferenced file is left in storage.
- **Disk budget**: FileToLink holds a copy of every file it links to, so
  reserve space accordingly (a 400 MB upload needs 400 MB in
  `FILE_STORAGE_DIR`). Metadata stays in SQLite; the bytes never leave your
  machine — this is local storage, not cloud durability.

### Deleting an owned file

`/delete` soft-deletes the link first (it stops being downloadable
immediately), then removes the FileToLink-owned file and its link directory.
Cleanup never touches anything outside `FILE_STORAGE_DIR` — in particular
never the Local Bot API Server's files — and if the filesystem removal fails
the deletion still succeeds (the failure is logged internally for diagnosis,
never exposed and never turned into an API error). Expired or
fully-downloaded links keep their file; removing it stays an explicit action
(there is no background cleanup daemon).

## Running the API (download server)

```bash
python run.py --api
# or: uvicorn --factory app.api.app:create_app --host 127.0.0.1 --port 8000
```

The API serves `GET /f/{public_id}` on `HOST:PORT` (default
`127.0.0.1:8000`). Files sent through the mock bot are served from the
local path recorded at send time — so links keep working across restarts.

```bash
curl -i http://localhost:8000/f/t9obSSQq      # 200 + file bytes
curl -i http://localhost:8000/f/doesNotExist  # 404
```

Notes:

- The endpoint increments `download_count` once per successful GET (range
  GETs count once; HEAD never counts).
- The file source is abstracted (`app/services/file_sources.py`): local
  paths (mock) or FileToLink-owned storage (real mode); the API never reads
  the Local Bot API Server's work directory.
- Lifecycle checks take precedence over range handling: an expired, deleted,
  or exhausted link returns `410 Gone` even when a `Range` header is sent.

## Deployment & operations (Phase 15)

Everything stays on one Windows machine at ₹0/$0 cost. This section is the
operational runbook: what runs, in what order, and how to recover.

### Prerequisites

- Python 3.12 with the project's `.venv` (`pip install -r requirements.txt`)
- Git (`.env` and `data/` stay untracked)
- Real Telegram mode only: a Local Bot API Server binary
  (`tdlib/telegram-bot-api`, built natively or inside WSL2) and Tailscale for
  the public funnel
- Nothing else: SQLite is a file, storage is a directory, there is no daemon

### What runs (five windows)

| Window | Process | Start with | Depends on |
|---|---|---|---|
| 1 | Local Bot API Server (`--local`, port 8081) | `scripts\start_bot_api_server.bat <exe> <api_id> <api_hash>` (or the WSL2 equivalent) | api_id/api_hash from my.telegram.org |
| 2 | FileToLink bot (long polling) | `scripts\start_bot.bat` | Window 1 (pings it, exits if unreachable) |
| 3 | FastAPI download server | `scripts\start_api.bat` | SQLite + FileToLink storage only |
| 4 | Tailscale Funnel | `tailscale funnel 8000` (publishes HOST:PORT) | Window 3 |
| 5 | Testing / commands | `pytest`, `curl`, a Telegram client | — |

**Startup order: 1 → 2 → 3 → 4 → 5.** Windows 2 and 3 are independent of
each other; Window 4 must come after 3. The scripts run in the foreground of
their own terminal window and hide nothing; the bot and API print the
resolved database and file-storage paths at startup, so a misconfiguration
is visible immediately. On Linux/macOS (or WSL2 for every window), run the
same commands directly: `python run.py --telegram` / `python run.py --api`.

**Shutdown order: reverse — 4, then 3, then 2, then 1** (Ctrl+C in each
window). Stopping 1 while 2/3 still run does not break existing downloads
(serving never touches the Local Bot API work dir); it only stops new files
from being received.

### Directory layout

| Path | Env var | Created | Role |
|---|---|---|---|
| `data/files.db` | `DATABASE_PATH` | automatically on first run | SQLite metadata (all state about links) |
| `data/storage/<public_id>/<filename>` | `FILE_STORAGE_DIR` | automatically on first transfer | FileToLink-owned bytes, one directory per link |
| `telegram_bot_data/` | `TELEGRAM_BOT_WORK_DIR` | by the Local Bot API Server | its own work dir — disposable staging, **never** application storage |
| `.env` | — | you (copy `.env.example`) | deployment configuration, never committed |

Relative path values resolve against the project root. The database + the
storage directory together are the entire application state; everything else
is reproducible from the repository.

### Environment configuration

Copy `.env.example` to `.env` and set the deployment values — `BASE_URL`
must be your funnel URL when publishing through Tailscale. Mock mode with
zero configuration keeps working for development (`python run.py`). Never
commit `.env`, paste it into reports, or log it.

### Restart / recovery

- **FastAPI restart — always safe.** It is stateless (fresh rate limiter,
  records re-read from SQLite); links keep working across the restart apart
  from the brief port-rebind gap.
- **Bot restart — safe.** Long polling resumes; adopted files and records
  persist. Updates still queued in the Local Bot API Server are re-delivered.
- **Local Bot API Server restart — safe for existing links.** They are
  served from `FILE_STORAGE_DIR`, never from its work dir, which may even be
  wiped between restarts (files not yet adopted at that moment are lost to
  ingestion; no link ever depended on them).
- After any restart, compare Window 2 and Window 3 startup banners: their
  database and file-storage paths must match (a mismatch is the classic
  "bot stores here, API serves there" failure).

### Testing procedure

```bash
pytest
```

The suite is hermetic: it runs against the shipped defaults regardless of
the deployment `.env` (a shared fixture isolates FileToLink environment
variables per test), so results are identical on a fresh clone and on an
operator machine. Symlink tests skip where Windows lacks the privilege.

### Backup basics

Back up exactly two things, together:

1. `data/files.db` — all metadata: links, owners, counters, lifecycle state
2. `data/storage/` — the bytes those links point to

They are one logical unit (`source_path` in the database points into the
storage tree). To back up safely, stop Window 2 and Window 3 first, or use
`sqlite3 data/files.db ".backup 'backup/files.db'"` for a live-consistent DB
copy, then copy the storage directory. Restoring only one half yields:

- database without files → links answer `404` (record active, bytes missing)
- files without database → nothing is served (no records); the storage is
  unreferenced and can be deleted

There is no background cleanup daemon: expired or fully-downloaded links
keep their bytes until an owner deletes the link.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Bot exits: "Cannot reach the Local Bot API Server" | Window 1 not running, or `LOCAL_BOT_API_URL` wrong — start Window 1 first |
| Bot exits listing `BOT_TOKEN`/`TELEGRAM_API_*` | `.env` incomplete for real mode; mock mode still works (`python run.py`) |
| `run.py --api`: bind error 10048 | Port already in use — another API instance is running, or `PORT` conflicts |
| Links 404 after a restart | Bot and API resolved different `DATABASE_PATH`/`FILE_STORAGE_DIR` — compare the startup banners |
| Link active but GET 404 | Owned file missing (storage moved/restored incompletely) |
| `410 Gone` | Terminal by design: deleted, expired, or download limit consumed |
| Works locally, dead publicly | `BASE_URL` ≠ funnel URL, Window 4 funnel not started, or Window 3 not running |

### Deployment security notes

- Only the FastAPI server needs public exposure, and only through Tailscale
  Funnel. `HOST` stays `127.0.0.1` (the funnel connects from localhost); the
  server warns if bound wider. Never funnel/forward the Local Bot API port
  8081 — it speaks plain HTTP and is for this machine only.
- Behind the funnel every visitor arrives from `127.0.0.1`, so per-visitor
  rate limiting needs `TRUST_PROXY_HEADERS=true` and
  `TRUSTED_PROXIES=127.0.0.1` (with the `X-Forwarded-For` the funnel adds);
  otherwise all visitors share one loopback bucket.
- Data-at-rest protection is the Windows user profile's file permissions:
  keep `DATABASE_PATH`/`FILE_STORAGE_DIR` inside the user profile on shared
  machines.

## Link management (expiration, limits, deletion)

Every stored file link can carry two optional settings: an expiration time
and a maximum number of downloads. Both are stored per record (`expires_at`,
`max_downloads`) and are optional — when omitted the link lives forever with
no download limit.

### Expiration

- `expires_at` is a Unix timestamp (UTC). `NULL` means the link never
  expires; a future timestamp means active; a past timestamp means expired.
- Expiration is **lazy**: no background worker runs. The first request after
  the deadline marks the link `expired` and returns `410 Gone` — no bytes are
  served and the download counter is not incremented.

### Maximum download counts

- `max_downloads = NULL` → unlimited downloads.
- `max_downloads = 0` → no downloads allowed (always `410`).
- `max_downloads = N` → at most `N` successful GET requests.
- Each GET (including a range GET) consumes **exactly one** download slot;
  `HEAD` consumes none.
- The slot is reserved with a **single atomic conditional UPDATE**, so
  concurrent requests can never exceed the limit (no check-then-act race).
  A request that finds the limit reached returns `410` without serving bytes
  or incrementing the counter.
- The GET that consumes the final permitted slot immediately transitions the
  link `active → expired` in the same atomic operation, so `/info` shows the
  real state right away. If a reserved slot cannot be served (missing source
  file, unsatisfiable range), the slot is released and the transition is
  rolled back, so failed requests do not burn a download.

### Deletion

- Deleting a link sets `status = 'deleted'` and prevents all future
  downloads (`410 Gone`). Metadata is preserved.
- The FileToLink-owned file (and its link directory) is then removed — see
  **File ownership → Deleting an owned file**. A mock-mode/operator file
  outside FileToLink storage is never touched, and a filesystem cleanup
  failure never turns a successful deletion into an error.
- Deletion is idempotent and terminal: a deleted link is never reactivated.

### Lifecycle statuses

`active` → `deleted` (owner deletes) or `active` → `expired` (time passed or
final download consumed). No other transitions exist: deleted or expired
links stay that way.

### /delete and /info (owner-only)

```
/delete <public_id>   permanently disable one of your links
/info   <public_id>   show status, downloads, and expiration
```

- **Ownership is enforced server-side**: the requesting Telegram user id is
  compared against the record's stored `user_id`. Only the owner can delete
  or inspect a link.
- `/delete` is idempotent: deleting an already-deleted link gives a friendly
  "already deleted" reply.
- `/info` shows the file name, size, status, download count, max downloads,
  expiration, and the public URL — and never exposes `telegram_file_id`,
  `source_path`, database ids, or any secrets.
- Non-owner and unknown-id requests get a generic reply that reveals no
  private metadata.

### Creating links with expiration/limits

The storage layer accepts optional `expires_at` and `max_downloads` when a
file is saved (`FileService.save_document` / `FileRepository.create`);
omitting them creates a permanent, unlimited link. No interactive
configuration flow is added in this phase.

### HTTP Range Requests

`GET /f/{public_id}` supports standard single byte ranges
(`Accept-Ranges: bytes`):

- No `Range` header → `200` with the complete file.
- `Range: bytes=0-99` / `bytes=100-` / `bytes=-100` → `206 Partial Content`
  with `Content-Range: bytes <start>-<end>/<total>`, `Content-Length` of the
  range, `Content-Type`, and `Content-Disposition`. Only the requested bytes
  are streamed (seek + bounded 64 KB reads — never a whole-file read).
- Malformed or unsatisfiable ranges (e.g. `bytes=999-1000` on a small file,
  `bytes=100-50`, bad syntax, multi-range) → `416` with
  `Content-Range: bytes */<total>`. Multi-range responses are not
  implemented.
- `HEAD /f/{public_id}` returns the same metadata headers with no body and
  does not touch the download counter; 404/410 behavior is preserved.

You get an interactive chat loop simulating a Telegram client:

```
▶ Starting file_to_link_bot in MOCK mode
🤖 file_to_link_bot — mock Telegram bot (mode: mock)
No bot token required. Type /start, /help, any message, or:
  send <path>   — simulate sending a local file to the bot
Type 'quit' or press Ctrl+C to exit.

You: /start
🤖 👋 Hi! I'm the File-to-Link Bot, ...

You: /help
🤖 📚 Help — File-to-Link Bot, ...
```

Type `quit` or press `Ctrl+C` to exit.

### Simulating a file send

Use the bundled sample file:

```
You: send samples/hello.txt
🤖 ✅ File received

📁 hello.txt
📦 117 B
🏷️ Type: text/plain

🆔 File ID: BQAD9jTeavgvVtD5oU_WF5ZEbvtM
#️⃣ Message ID: 1
👤 User ID: 123456789
🕒 Received: 2026-09-05 15:48:38 UTC
💾 Stored in database (record #1)
🔑 Public ID: t9obSSQq

🔗 Download:
http://localhost:8000/f/t9obSSQq
```

The URL is generated from the configured `BASE_URL`. With the API running
(`python run.py --api`) it **actually serves the file**: `GET /f/{public_id}`
returns the bytes with `200`, unknown/invalid ids give `404`, and
inactive (deleted/expired) links give `410 Gone`.

`send <path>` reads the local file for its name/size, guesses the MIME type
from the extension, and fabricates a mock Telegram file id. The metadata is
persisted to SQLite (`data/files.db` by default), and the reply notes the
stored record id. Missing or unreadable files get a friendly error.

## Security (Phase 11)

Everything below is free/open-source and local — no paid service, no Redis,
no external infrastructure.

### Rate limiting

- `GET` and `HEAD /f/{public_id}` are limited **per client identity**: by
default `RATE_LIMIT_REQUESTS` requests per `RATE_LIMIT_WINDOW_SECONDS`
(60 per 60 s). Over the limit returns **`429 Too Many Requests`** with a
`Retry-After` header. Disable with `RATE_LIMIT_ENABLED=false`.
- The limiter is a small in-process sliding window (`app/utils/ratelimit.py`).
That is appropriate for this single-process, self-hosted app; if you ever run
several worker processes, each keeps its own window (limits become
per-worker).
- The limit is checked **before any lookup**, so probing for public ids is
throttled too. Keys are **client identities only — never link ids**, so
random/invalid ids cannot grow per-file state. The number of tracked keys is
capped by `RATE_LIMIT_MAX_KEYS` (least-recently-used eviction), so memory
cannot grow without bound from attacker-controlled addresses.

### Client identity / proxy trust

By default the limiter keys on the **actual TCP peer address**, and
`X-Forwarded-For` is ignored — a client cannot spoof its identity. If you
run behind a reverse proxy, set `TRUST_PROXY_HEADERS=true` and list the
proxy's IP(s) in `TRUSTED_PROXIES`; the forwarded address is then trusted
**only** when the direct peer is a listed proxy. Configuration validation
refuses `TRUST_PROXY_HEADERS=true` with an empty `TRUSTED_PROXIES`, and
rejects invalid IPs.

### Response security headers

Every download response (including errors and 429s) carries
`X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src
'none'; sandbox`, and `Referrer-Policy: no-referrer`. Files are always sent
with `Content-Disposition: attachment`, so range requests and normal
downloads are unaffected.

### MIME / content hardening

Stored MIME metadata is untrusted, so it is sanitised
(`app/utils/mime.py`): well-formed `type/subtype` values are preserved, while
missing, empty, or malformed values (including anything containing control
characters that could inject headers) fall back to
`application/octet-stream`. This only guarantees a safe *header* — it is not
content inspection.

### Request hardening

- The public-id path parameter is length-checked (exactly 8 chars) before the
format regex, so oversized paths are rejected cheaply.
- Malformed/oversized/multi-range headers are rejected as `416` without
opening the file; parsing stays bounded and files are streamed in 64 KB
chunks (never fully read into memory).
- `HEAD` mirrors the matching `GET` status and headers (`200`/`206`/`416`)
with no body and **without consuming a download slot**.

### Host binding

`HOST` defaults to `127.0.0.1` (localhost only). Binding to a non-loopback
address (e.g. `0.0.0.0`) exposes the API beyond your machine; the server
prints a warning, and since links are public URLs anyone holding one can
download, do this only on a trusted network.

### LocalFileSource (mock mode)

`LocalFileSource` (mock mode) has **no path containment**, by design:
`source_path` is set only by the local operator via `send <path>` (or tests),
never from untrusted remote input, so serving operator-chosen local files is
the intended local-testing behavior. Real Telegram mode uses
`StorageFileSource`, which only ever serves files inside `FILE_STORAGE_DIR`
and rejects traversal/symlink escapes (`TelegramFileSource` enforces the same
rules for records that still point at the Local Bot API work directory).

## Database

- **Engine**: Python's built-in `sqlite3` — local file, zero cost, no server.
- **Location**: `data/files.db` by default; configurable via `DATABASE_PATH`.
  The directory is created automatically on first run.
- **Files table**: `id`, `public_id` (UNIQUE, written together with the row
  since Phase 14B),
  `telegram_file_id`, `telegram_message_id`, `user_id`, `file_name`,
  `file_size`, `mime_type`, `created_at` (epoch seconds), `expires_at`
  (NULL = never), `download_count` (default 0), `max_downloads` (NULL =
  unlimited), `status` (`active`/`deleted`/`expired`, default `active`).
  Indexed on `public_id`, `telegram_file_id`, and `status`.
- **Migration**: pre-existing databases (without `max_downloads`) get the
  column added automatically on startup; existing records receive
  `max_downloads = NULL` (unlimited) and are otherwise untouched.
- **`source_path`** is the FileToLink-owned path in real mode (Phase 14B).
  Records created before that point still point at the Local Bot API Server's
  work directory (which the server may have evicted); re-send those files
  through the bot to get FileToLink-owned copies.
- **Layering**: `app/database/` owns all SQL (connection + schema +
  repository). `app/services/files.py` turns normalized file messages into
  stored records. Bot handlers and the mock transport never touch raw SQL.
- **Inspect it**: `sqlite3 data/files.db 'SELECT * FROM files;'`

## Testing

```bash
pytest
```

The tests cover the bot handlers (`/start`, `/help`, unknown commands, plain
text, empty/absent messages), file receiving (full metadata display, missing
name/size/MIME type/timestamp, captions with documents, size formatting,
mock file-document extraction, missing-file errors), the database
(schema creation, create/retrieve/update/invalidate/delete, defaults,
indexes, service-layer persistence, handler + transport integration, and
the handler's no-service path), public ids (format, uniqueness, collision
retry, exhaustion), URL generation (format, normalization, validation), and
the API (valid download with headers + counter, invalid/unknown → 404,
deleted/expired → 410, missing source → 404, full mock-send → HTTP loop,
schema migration), and the real Telegram transport (PTB update → normalized
models incl. missing mime/name/captions, source containment,
symlink/traversal rejection, config validation, application wiring, and the
simulated document flow storing + serving bytes — all offline, no token or
server needed), file ownership (move vs. streamed copy, size verification,
source removal only after the destination is verified, partial-destination
cleanup, unreadable/directory/missing sources, duplicate names, path-traversal
filenames, bounded-chunk streaming of a multi-megabyte file, rollback when the
record cannot be written, owned-file deletion + cleanup failure, and the
real-mode API serving/Range/HEAD/lifecycle behaviour on owned files), and link
management (expiration, download limits incl. a
real concurrency test with >N parallel GETs against max_downloads=N,
deletion idempotency, owner-only /delete and /info, metadata privacy,
unsatisfiable ranges not consuming slots, and the max_downloads database
migration keeping existing records intact). Because handlers are
transport-free, the same tests pass in both mock and real modes.

## Public IDs

- Every stored file gets an 8-character public id from
  `app/utils/public_ids.py` — generated with Python's `secrets` (CSPRNG),
  URL-safe (`A-Z a-z 0-9 _ -`), never sequential, timestamps, or Telegram
  file ids.
- The UNIQUE index on `public_id` is the source of truth: on the
  astronomically unlikely collision, `FileService` generates a fresh id and
  retries (bounded, 10 attempts) — another file's id is never overwritten.

## Public links (URLs)

- **Format**: `{BASE_URL}/f/{public_id}` — e.g.
  `http://localhost:8000/f/t9obSSQq`. The public id is used unchanged;
  internal row ids and Telegram file ids are never exposed.
- **`BASE_URL`** comes from the environment (`.env`), defaulting to
  `http://localhost:8000` for local development. It must include a scheme;
  trailing slashes and surrounding whitespace are tolerated. See the
  environment table below.
- **Construction** lives in `app/utils/urls.py` (`generate_file_url`),
  kept out of the repository, transports, and SQL. Inputs are validated
  (public id format, base URL scheme).
- **Flow**: `FileService.save_document()` → record with public id →
  `generate_file_url(base_url, public_id)` → handler reply.
- **The URL is live**: `GET /f/{public_id}` serves the stored bytes — `200`
  for a full download, `206` for a valid `Range`, `416` for an unsatisfiable
  range, and `HEAD` for metadata only. Deleted, expired, or
  download-exhausted links return `410 Gone`.

## How the mock works

`app/bot/mock.py` builds normalized `Update` objects — from text lines or
from local files via `send <path>` — and feeds them into `handle_update()`,
the exact function the real bot will call. There is no network, no token,
and nothing to leak. The real transport (`app/bot/telegram.py`) does the
same normalization for live Telegram updates, so both transports share
100% of the downstream logic.

## Milestone plan (phase-by-phase)

1. ✅ Bot scaffold + mock transport (`/start`, `/help`)
2. ✅ File receiving (documents) + metadata extraction
3. ✅ SQLite database layer
4. ✅ Unique public IDs
5. ✅ Link generation
6. ✅ Download endpoint
7. ✅ Telegram Local Bot API Server (research + real transport wiring)
8. ✅ Streaming (chunked, bounded reads)
9. ✅ HTTP Range Requests
10. ✅ Link management (expiration, counters, deletion, /delete, /info)
11. ✅ Security & abuse protection (rate limiting, headers, MIME, validation)
12. Error handling
13. Logging
14. Testing
15. ✅ Deployment readiness & reproducible operations (config, scripts,
    restart/backup runbook)

Each milestone ends in a working, testable state. The real Telegram bot will
be connected after the core project is finalized.

## Limitations (current milestone)

- **Real end-to-end not yet run here**: the Local Bot API Server is not
  installed on this machine, so the live Telegram flow is implemented and
  unit-tested offline, but the actual server + bot verification is pending
  external setup (see Real Telegram mode above).
- Mock transport remains the default and is fully functional.
- Single byte ranges only (no multipart/byteranges); malformed/unsatisfiable
  ranges return 416.
- Expiration, download limits, and deletion are per-link database settings.
  Rate limiting is per-client (per-process) rather than per-link; there is
  still no signed-URL or per-user authentication beyond the owner check on
  `/delete` and `/info`.
- Expired or fully-downloaded links keep their FileToLink-owned file until
  the owner deletes the link (Phase 14B deliberately ships no background
  cleanup daemon); the file is never served once the link is inactive.
- A range-size cap is deliberately **not** implemented: a range can never
  return more than the file itself (no amplification), and full large-file
  downloads are the project's purpose — request frequency is what the rate
  limiter covers.