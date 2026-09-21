# Telegram File-to-Link Bot

A zero-cost Telegram bot that receives files from users and generates a public
link that can be used to access/download those files.

> **Status — Phase 19** (long-running operation on the one Windows machine:
> verified dependency/startup/shutdown order, a secret-free logging path
> (the Local Bot API URL used to print `BOT_TOKEN` at INFO), a `GET /health`
> check that never burns a download slot, a one-command backup script, and an
> evidence-based diagnostics playbook — see *Long-running operation (Phase 19)*
> below). No service manager, scheduler, container, or paid component was
> added; every process still runs in a visible console window.
> The complete public path was then verified by hand on that deployment —
> Telegram upload → FileToLink-owned storage → FastAPI → Tailscale Funnel →
> the public Internet → a phone on mobile data (see *Public-link verification
> (Phase 19)* below).
> Phase 18 (reliability & operations: restart, backup/restore, and
> missing/orphaned-file recovery are verified end-to-end, and a backup restored
> to a *different* folder, drive, or machine keeps every public link working
> with no manual database surgery — see *Deployment & operations* below).
> The restart and staging-loss checks were additionally verified by hand on
> the real Windows deployment (see *Real-machine verification (Phase 18)*
> below).
> Phase 15 deployment readiness: reproducible startup, restart, and backup for
> the same Windows/local, ₹0-cost deployment. Earlier work — Phase 14B file
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
        (mock or real transport)  │
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
(`app/bot/mock.py`) and the real **Telegram transport** (`python-telegram-bot`
polling) both plug into the same handlers, so neither transport changes any
bot logic.

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
curl -i http://localhost:8000/health          # 200: process + database + storage
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
- Git (`.env`, `data/`, and the Local Bot API staging dir stay untracked)
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
| 5 | Testing / commands | `pytest`, `curl`, `tailscale`, `sqlite`, `curl /health` | — |

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
- **A missing owned file — 404, never 500, and never a burned download.** A
  link `active` in the database but missing on disk answers `404` (no
  download slot is consumed); putting the bytes back makes the same public
  link work again immediately — no restart and no database edit.
- After any restart, compare Window 2 and Window 3 startup banners: their
  database and file-storage paths must match (a mismatch is the classic
  "bot stores here, API serves there" failure). This still catches a genuine
  misconfiguration, because the API only finds a restored file when its own
  `FILE_STORAGE_DIR` actually contains it.

### Testing procedure

```bash
pytest
```

The suite is hermetic: it runs against the shipped defaults regardless of
the deployment `.env` (a shared fixture isolates FileToLink environment
variables per test), so results are identical on a fresh clone and on an
operator machine. Symlink tests skip where Windows lacks the privilege.

### Backup basics

For a one-command backup, use `scripts\backup.py` (see *Long-running
operation (Phase 19)* → *Backup & restore*). To do it by hand, back up exactly
two things, together:

1. `data/files.db` — all metadata: links, owners, counters, lifecycle state
2. `data/storage/` — the bytes those links point to

They are one logical unit (`source_path` in the database points into the
storage tree). To back up safely, stop Window 2 and Window 3 first, or use
`sqlite3 data/files.db ".backup 'backup/files.db'"` for a live-consistent DB
copy, then copy the storage directory. Restoring only one half yields:

- database without files → links answer `404` (record active, bytes missing)
- files without database → nothing is served (no records); the storage is
  unreferenced and can be deleted

**Restore both halves together and links survive — wherever they land.** The
storage layout is keyed by the public id
(`data/storage/<public_id>/<filename>`), so after restoring the database and
the storage directory together, each link is resolved through its public id.
This holds even when the restore is to a different folder, a different drive,
or another machine: the absolute paths recorded at ingest time are no longer
required, and no SQL needs rewriting. `/delete` uses the same resolution, so
cleanup also works after a moved restore.

Crashed or interrupted ingestion can leave an unreferenced file in storage
(e.g. a transfer that succeeded but whose record was never written). It has no
public id of its own in the database, so it is never served and is safe to
delete; a partially copied `.part` file is likewise never served. There is no
background cleanup daemon: expired or fully-downloaded links keep their bytes
until an owner deletes the link.

### Real-machine verification (Phase 18)

The restart, ownership, and staging-loss behaviour described above was
verified by hand on the real Windows deployment (windows 1–3: Local Bot API
Server, bot, FastAPI), using a real DOCX upload — public id `ylb6n993`,
stored size 46,101 bytes:

| Check | Procedure | Result |
|---|---|---|
| Bot restart (Window 2) | Restart the bot; upload a new DOCX | PASS — new upload ingested; public id `ylb6n993`, 46,101 bytes stored |
| FastAPI restart (Window 3) | Restart the API; fetch the link | PASS — HTTP 200, full download of exactly 46,101 bytes |
| Local Bot API Server restart (Window 1) | Restart the server using the full executable path | PASS — the FileToLink-owned file remained available |
| Staging work-dir loss | Rename/replace `D:\telegram-bot-data`; restart Window 1 | PASS — the existing link `ylb6n993` still downloaded exactly 46,101 bytes |

All real-machine operational checks for Parts 2–5 of the Phase 18 plan PASS.
Links survived every restart — including the destruction of the Local Bot API
Server's staging work dir — because downloads are served exclusively from
FileToLink-owned storage; the staging dir is only an ingestion source.

### Public-link verification (Phase 19 real deployment)

The complete public path was verified by hand on the real Windows deployment
after the Phase 19 work, using a fresh PDF upload — Telegram → FileToLink-owned
storage → FastAPI (:8000) → Tailscale Funnel → the public Internet → a phone on
mobile data:

| Check | Procedure | Result |
|---|---|---|
| Public link generated | Upload a fresh PDF through the bot (Window 2) | PASS — the reply carried a `{BASE_URL}/f/{public_id}` link; 97,328 bytes stored |
| Public `HEAD` | `curl -I <funnel URL>/f/<public_id>` from Window 5 | PASS — `HTTP 200`, `Content-Length: 97328`, `Content-Type: application/pdf` |
| Public full download | `curl -o … <funnel URL>/f/<public_id>` from Window 5 | PASS — exactly 97,328 bytes |
| Off-network phone | Wi-Fi off, mobile data on; open the same URL on a phone | PASS — the PDF downloaded over mobile data |

This is the first check that leaves the machine, so it exercises the funnel and
`BASE_URL` rather than only the app. Two configuration points decide whether it
works, both covered above: `BASE_URL` must be the funnel host (see *Tailscale
Funnel (Window 4)*), and `HOST` stays `127.0.0.1` because the funnel connects
from localhost. A malformed `.env` line (stray quotes or whitespace around
`KEY=value`) is the other trap: the value is not parsed as written, so
`BASE_URL` keeps its `http://localhost:8000` default, the generated link still
works *on this machine*, and only a fetch from another network reveals the
mistake — the failure the "works locally, dead publicly" row below describes.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Bot exits: "Cannot reach the Local Bot API Server" | Window 1 not running, or `LOCAL_BOT_API_URL` wrong — start Window 1 first. The failure class is printed (`NetworkError`, …) |
| Bot exits: "rejected BOT_TOKEN" | The server answered and refused the credentials — the token is wrong or was revoked at @BotFather. Not a connectivity problem |
| Bot exits: "LOCAL_BOT_API_URL must be an absolute http(s) URL" | Typo in `.env` (missing scheme/port) — validated at startup instead of surfacing as "unreachable" |
| `run.py --api` exits: "Cannot open the SQLite database at …" | `DATABASE_PATH` is a directory, an unreadable location, or a missing drive |
| `/health` → 503 | The named component is unusable: `database` (path/moved file) or `storage` (missing/unwritable `FILE_STORAGE_DIR`) |
| `tailscale funnel status` → "No serve config" | Not proof the funnel is down — a working *foreground* funnel prints this line too (observed). Check `tailscale funnel status --json`: no `Foreground` entry means Window 4 is not publishing (run `tailscale funnel 8000`; public links down, local links fine) |
| Bot exits listing `BOT_TOKEN`/`TELEGRAM_API_*` | `.env` incomplete for real mode; mock mode still works (`python run.py`) |
| `run.py --api`: bind error 10048 | Port already in use — another API instance is running, or `PORT` conflicts |
| Links 404 after a restart | Bot and API resolved different `DATABASE_PATH`/`FILE_STORAGE_DIR` — compare the startup banners |
| Link active but GET 404 | Owned file missing (storage moved/restored incompletely) — restore the bytes, or check the startup banners |
| `410 Gone` | Terminal by design: deleted, expired, or download limit consumed |
| Works locally, dead publicly | `BASE_URL` ≠ funnel URL, Window 4 funnel not started, or Window 3 not running |

### Long-running operation (Phase 19)

Everything above is for one operator on one Windows machine. This part is what
to check before leaving FileToLink running for hours or days — and what each
component failure looks like.

#### Dependency graph (verified on the real deployment)

```
Window 1  Local Bot API Server  (:8081, host-only)
              │
              ▼
Window 2  FileToLink bot  (long polling)          ── new Telegram ingestion

Window 3  FastAPI  (:8000)  ── owns SQLite + FILE_STORAGE_DIR
              │
              ▼
Window 4  Tailscale Funnel  (:443 → 127.0.0.1:8000) ── public links

Window 5  testing / commands  (pytest, curl, tailscale, sqlite)
```

- **Serving an existing link needs Window 3 only.** Windows 1 and 2 can be
down; downloads never touch the Local Bot API work directory (Phase 14B).
- **New Telegram ingestion needs Window 1 + Window 2.**
- **Public access needs Window 3 + Window 4.** Local access needs Window 3.
- Window 4 publishes **:8000 only** — never funnel :8081 (plain HTTP, and it
  must stay on this machine).

#### What a component failure looks like

| Stopped | Existing links | New uploads | Public links | Local links |
|---|---|---|---|---|
| Window 1 (Local Bot API) | keep working | fail (bot cannot fetch the file) | unaffected | unaffected |
| Window 2 (bot) | keep working | fail until restarted (long polling resumes; queued updates are re-delivered) | unaffected | unaffected |
| Window 3 (FastAPI) | unavailable while down; files/database untouched | ingested and stored, but no link can be served yet | fail | fail |
| Window 4 (Funnel) | unaffected | unaffected | fail (`BASE_URL` unreachable) | unaffected |

Expect these. A dependency failure is not a bug: each one is reported by the
failing component itself (Window 2 exits with an actionable message when
Window 1 is missing, wrong BOT_TOKEN, or unreachable) and recovers by starting
the missing window again.

#### Health check

```bash
curl -i http://127.0.0.1:8000/health
# 200 {"status":"ok","database":"ok","storage":"ok"}
# 503 {"status":"error","database":"ok","storage":"error"}
```

- 200 = this process is serving, the configured SQLite file opens and carries
the schema, and `FILE_STORAGE_DIR` exists or can be created. 503 names the
failing component and nothing else — no secrets, no paths, no rows.
- It **never touches a record**, so checking it cannot burn a `max_downloads`
slot (a real download can). Use it instead of fetching a link to test Window 3.
- Independent of Telegram and Window 1 by design: serving links needs neither.
- It is rate limited like any other public route, so polling it more than
`RATE_LIMIT_REQUESTS` times per window returns `429`.

#### Logging (console only, secret-free)

- **No log files are created anywhere** — every process logs to the console of
its own window. There is nothing to rotate and no logging framework beyond the
standard library; use the window's scrollback, or redirect with `>` if you want
a file (your choice, not the app's).
- **`BOT_TOKEN` never reaches the log.** The Local Bot API request URL embeds
the token (`<LOCAL_BOT_API_URL>/bot<TOKEN>/<method>`), and `httpx` logs every
request URL at `INFO` — which is exactly how earlier real runs printed the
token. `app/utils/logging_setup.py` now (a) raises the `httpx`/`httpcore`
loggers to `WARNING` and (b) attaches a redaction filter to the process's root
handlers, so even a token appearing in a third-party record at any level is
masked as `<redacted>` before it is written.
- Startup banners print configuration that is *not* secret (bot mode, database
path, storage path); configuration errors never include secret values.

#### Backup & restore (one command)

```bash
.venv\Scripts\python.exe scripts\backup.py
# → backups/20260921-120000/{files.db, storage/, MANIFEST.txt}
.venv\Scripts\python.exe scripts\backup.py --dest D:\filetolink-backups\weekly
.venv\Scripts\python.exe scripts\backup.py --force   # write into an existing directory
```

- Copies **both halves of the application state together** — a live-consistent
SQLite snapshot (SQLite's online backup API, so Window 2/3 may keep running)
and the FileToLink-owned storage tree — preserving the
`<public_id>/<filename>` layout the database resolves against.
- Copies **nothing else**: no `.env` (it refuses outright if `FILE_STORAGE_DIR`
would contain one), no `TELEGRAM_BOT_WORK_DIR` (Local Bot API staging), no
logs. A stub `MANIFEST.txt` records the timestamp, record counts, storage file
count/bytes, and what was excluded.
- Never overwrites an existing backup silently: a non-empty destination is
refused with exit code 2 unless `--force` is given. Exit 0 = success, 1 =
failure, 2 = refused.
- **Restore** = copy both halves back *together* while Window 2 and Window 3
are stopped: `files.db` over `DATABASE_PATH`, `storage/` over
`FILE_STORAGE_DIR`. Links then work again, including at a different folder,
drive, or machine (Phase 18).
- `backups/` is git-ignored. No scheduler is installed — run it by hand, or
wire it into Windows Task Scheduler yourself if you want it automatic.

#### Tailscale Funnel (Window 4)

```bash
tailscale funnel 8000          # start (foreground; Ctrl+C stops it)
tailscale funnel status        # is anything published?
tailscale funnel status --json # the exact mapping ("Proxy": "http://127.0.0.1:8000")
tailscale funnel off           # stop publishing
```

- **Running?** `tailscale funnel status --json` shows a `Foreground` entry
whose `Proxy` is `http://127.0.0.1:8000` — this is the authoritative check.
Plain `tailscale funnel status` is not: a working foreground funnel was
observed to print `No serve config` as well, so never read that line as
"public links are down".
- **`BASE_URL` is what makes the funnel useful.** Generated links are
`{BASE_URL}/f/{public_id}`, so with the default
`BASE_URL=http://127.0.0.1:8000` links are *local-only* even while the funnel
is up. Set `BASE_URL` to the funnel host shown by
`tailscale funnel status` (e.g. `https://<machine>.<tailnet>.ts.net`) to hand
out publicly usable links — then check the local URL first when a public link
fails.
- Changing `BASE_URL` affects only newly generated links: stored files and
public ids are untouched, and the id is generated independently of the
hostname (never hard-code your Tailscale hostname in the project — keep it in
`.env`).
- Funnel publishes **:8000 only**. Public links work only while Window 3 *and*
Window 4 are up; stopping Window 4 leaves local links working.
- Phase 19 deliberately leaves the funnel in the **foreground** (no `--bg`, no
service). If your foreground funnel has been reliable, keep it that way.

#### Diagnostics playbook

**Bot not receiving files** (nothing arrives in Telegram):
1. Window 2 running? Its console shows the startup banner or an exit reason.
2. If it exited: the message says whether the Local Bot API Server was
   unreachable (start Window 1) or **rejected `BOT_TOKEN`** (a credential
   problem — re-check the token with @BotFather; this is *not* a connectivity
   issue).
3. Window 1 running and listening on `LOCAL_BOT_API_URL` (`netstat -ano | grep
   8081`)? Its window shows its own errors (wrong `api_id`/`api_hash`, port in
   use).
4. Configuration: `BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
   `LOCAL_BOT_API_URL`, and `TELEGRAM_BOT_WORK_DIR` matching Window 1's `--dir`.
5. Logs: the bot window prints one line per failure; tokens are redacted.

**Local link not working** (`http://127.0.0.1:8000/f/<id>`):
1. Window 3 running, and `curl -i http://127.0.0.1:8000/health` → 200?
2. `health` naming `database` → `DATABASE_PATH` wrong/DB moved (Window 3 prints
   its resolved database path at startup).
3. `health` naming `storage` → `FILE_STORAGE_DIR` missing/unwritable.
4. `404` for the link itself → the owned file is missing (restore the bytes) or
   the public id is wrong; `410` → deleted/expired/download limit reached, by
   design.
5. Compare Window 2's and Window 3's startup banners: a `DATABASE_PATH` or
   `FILE_STORAGE_DIR` mismatch is the classic "bot stores here, API serves
there" failure.

**Public link not working** (the `{BASE_URL}/f/<id>` you sent someone):
1. Does the *local* URL work? If not, it is not a funnel problem.
2. Window 3 running (above).
3. Window 4 running — `tailscale funnel status --json` shows the `Proxy`.
4. `BASE_URL` equals the funnel URL (default `http://127.0.0.1:8000` is
   local-only, and links already sent keep the old hostname).
5. Local first, then public: that ordering isolates Funnel from the app.

**New uploads fail but old links work** → the ingestion side (Window 1 and
Window 2), not storage or serving. Serving never touches the Local Bot API work
directory; check the bot window's message, then Window 1.

**After a Windows restart** (no reboot test was performed in Phase 19 — this
is the expected sequence): start `scripts\start_bot_api_server.bat`, then
`scripts\start_bot.bat`, then `scripts\start_api.bat`, then `tailscale funnel
8000`; compare the Window 2/Window 3 banners, then `curl /health` and fetch one
old link. Nothing is installed to auto-start these — by design.

#### Deliberately not installed

No Windows service, no Task Scheduler entry, no background funnel, no log
rotation, no monitoring, no containers, no Redis/PostgreSQL, no cloud storage.
The app is a bot process, an API process, a SQLite file, and a storage
directory.

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
migration keeping existing records intact), and Phase 18 reliability &
operations (FastAPI/bot/Local Bot API restarts, a wiped staging work dir,
live-consistent SQLite backup, restoring the database + storage together —
including to a different location, so no database surgery is needed —
counters/limits across restarts, a missing owned file giving 404 without
burning a download and recovering when the bytes return, orphaned and partial
`.part` files never being served or touched by unrelated deletion, interrupted
ingestion leaving no broken active record, and expired/deleted records staying
terminal across a restart), and Phase 19 operations (health-endpoint semantics
and its refusal to consume a download slot, token redaction reaching root
handlers, quieted request-URL loggers, the credential-vs-connectivity startup
messages, invalid `LOCAL_BOT_API_URL` and unusable `DATABASE_PATH` failing fast
without a traceback, and the backup script: both halves present, byte-identical
storage, no `.env`/staging data, no silent overwrite, and a restore that still
serves the links). Because handlers are
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
16. ✅ Long-running operation & deployment hardening (Phase 19: dependency
    graph, failure matrix, `/health`, secret-free logging, one-command backup,
    diagnostics playbook)

Each milestone ends in a working, testable state. The real Telegram bot will
be connected after the core project is finalized.

## Limitations (current milestone)

- **Real end-to-end verified (Phase 18)**: the live flow — bot restart plus a
  real DOCX upload, FastAPI restart plus a full download, Local Bot API Server
  restart, and staging work-dir loss — was verified on the real Windows
  deployment (see *Real-machine verification (Phase 18)* above). Mock
  transport remains the default for development and is fully functional.
- **Public path verified (Phase 19)**: the same deployment served a real upload
  over Tailscale Funnel — a public `HEAD` and a full download matching the
  stored byte count, and the same link downloaded by a phone on mobile data
  with Wi-Fi off (see *Public-link verification (Phase 19)* above).
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