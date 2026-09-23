# FileToLink

A zero-cost Telegram bot that receives files from users and generates a public
link anyone on the Internet can use to download them. Everything runs on one
Windows machine the operator controls: no paid hosting, database, storage, or
CDN, and no cloud services.

> **Status — Phase 21 (Documentation & Release).** FileToLink is release-ready.
> Phases 1–20 are complete and validated, and the complete real-world path has
> been verified end to end: a fresh Telegram upload → FileToLink-owned storage
> → FastAPI → Tailscale Funnel → public HTTPS → a normal mobile phone → a
> successful download. This README is the release documentation: what the
> system is, how it is wired, how to run and operate it, and what was verified.
> See [Release verification status](#23-release-verification-status).

---

## Table of contents

- [1. What FileToLink is](#1-what-filetolink-is)
- [2. Core architecture](#2-core-architecture)
- [3. How the system works](#3-how-the-system-works)
- [4. Required components](#4-required-components)
- [5. Directory structure](#5-directory-structure)
- [6. Configuration & environment variables](#6-configuration--environment-variables)
- [7. Local development](#7-local-development)
- [8. The five-window setup](#8-the-five-window-setup)
- [9. Starting the system](#9-starting-the-system)
- [10. Stopping & restarting the system](#10-stopping--restarting-the-system)
- [11. Health checking](#11-health-checking)
- [12. Creating a file link](#12-creating-a-file-link)
- [13. Downloading a file](#13-downloading-a-file)
- [14. Public access](#14-public-access)
- [15. Storage ownership](#15-storage-ownership)
- [16. Backup](#16-backup)
- [17. Restore & recovery](#17-restore--recovery)
- [18. Security considerations](#18-security-considerations)
- [19. Testing](#19-testing)
- [20. Troubleshooting](#20-troubleshooting)
- [21. Operational notes](#21-operational-notes)
- [22. Current limitations](#22-current-limitations)
- [23. Release verification status](#23-release-verification-status)
- [Appendix A — Link management](#appendix-a--link-management)
- [Appendix B — Database schema](#appendix-b--database-schema)
- [Appendix C — Public IDs & links](#appendix-c--public-ids--links)

---

## 1. What FileToLink is

FileToLink is a self-hosted bridge between Telegram and a public download URL:

```
User → Telegram bot → backend ingests the file → unique public link
     → user shares the link → recipient opens it → file is downloaded
```

A Telegram user sends a file to the bot. The bot takes **its own durable copy**
of that file, records metadata in SQLite, and replies with a short public URL.
Anyone holding that URL can download the file over ordinary HTTPS from any
normal Internet client — a browser, a phone, `curl`, a download manager — with
no Telegram account and no app installed. Links support byte ranges (seek /
resume), a download counter, optional expiry, optional download limits, and
owner-only delete/inspect commands.

Key properties:

- **Zero cost** — Python, FastAPI, Uvicorn, SQLite, and the official Telegram
  Local Bot API Server are all free and open source; there is no paid component.
- **Self-hosted** — the only thing exposed publicly is a single HTTP endpoint,
  published through Tailscale Funnel.
- **Owned storage** — downloads are served from FileToLink's own storage
  directory, never from Telegram's disposable staging area.
- **No secrets in the repository** — credentials live only in an untracked
  `.env`, and logs redact the bot token.

### Why this is zero-cost

- **Mock transport** — the project runs with no bot token and no network.
- **Python + FastAPI + SQLite + Uvicorn** — all free and open source.
- **Local development** — everything runs on your own machine.
- **No paid services** — no paid hosting, databases, storage, or CDNs. Free-tier
  options are not required and are never assumed to be permanently free.

---

## 2. Core architecture

```
                            TELEGRAM
                               │
                               ▼
              Telegram transport ──┐
         (mock or real transport)  │
                               │   │
                               ▼   ▼
                    app/bot/handlers.py   (framework-agnostic bot logic)
                               │
                               ▼
             app/services/storage.py  →  FileToLink-owned files
      (real mode: the received file is transferred out of the            │
       Local Bot API Server's work dir before any link exists)           │
                               │                                          │
                               ▼                                          │
             SQLite (data/files.db, owned source_path)                    │
                               │                                          │
                               ▼                                          │
             Download endpoint (GET /f/{public_id}) ──────────────────────┘
                               │
                               ▼
                         Public HTTPS URL (Tailscale Funnel)
                               │
                               ▼
                        Normal Internet client
```

The key design choice: bot logic (`app/bot/handlers.py`) only sees normalized
update objects (`app/bot/models.py`). The **mock transport** (`app/bot/mock.py`)
and the real **Telegram transport** (`python-telegram-bot` long polling) both
plug into the same handlers, so neither transport changes any bot logic. HTTP
concerns live in `app/api/`, SQL in `app/database/`, and link/URL generation in
`app/utils/`.

### The verified deployment pipeline

The production path is:

```
Telegram user
    ↓
Telegram Bot
    ↓
Local Telegram Bot API Server (127.0.0.1:8081)
    ↓
File ownership transfer
    ↓
Owned local storage (data/storage/)
    ↓
SQLite metadata (data/files.db)
    ↓
FastAPI public endpoint (127.0.0.1:8000)
    ↓
Tailscale Funnel
    ↓
Public HTTPS URL (https://desktop-b633ve8.tail20d174.ts.net)
    ↓
Normal Internet client
    ↓
File download
```

---

## 3. How the system works

### Real file flow

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
(FileToLink storage) and `TelegramFileSource` (the Local Bot API work dir) only
serve files physically inside their own root — traversal, absolute paths
outside the root, and symlinks escaping it are rejected, and filesystem paths
are never shown to users.

### Mock vs real mode

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

---

## 4. Required components

- **Python 3.10+** (developed against 3.12) with the project's `.venv`
- **Git** (`.env`, `data/`, and the Local Bot API staging dir stay untracked)
- **A Telegram bot token** from [@BotFather](https://t.me/BotFather) (real mode)
- **Free api_id / api_hash** from [my.telegram.org](https://my.telegram.org)
  for the Local Bot API Server (real mode)
- **The Telegram Local Bot API Server** — official source
  [`tdlib/telegram-bot-api`](https://github.com/tdlib/telegram-bot-api)
  (Boost license — free), built natively or run inside WSL2 (real mode)
- **Tailscale** with Funnel enabled (for public links)
- Nothing else: SQLite is a file, storage is a directory, and there is no daemon

### Prerequisites to install

```bash
# from the project root
python -m venv .venv

# Windows (Git Bash):
source .venv/Scripts/activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt

# Optional: create your local config
cp .env.example .env
```

The `.env` file is optional for development — sensible defaults are built in.
Never commit it.

### Running the Local Bot API Server

Real mode needs the Local Bot API Server running with `--local`. Official
source: [`tdlib/telegram-bot-api`](https://github.com/tdlib/telegram-bot-api)
(Boost license — free). It needs `api_id`/`api_hash` and stores received files
under a working directory (`--dir`) that must match `TELEGRAM_BOT_WORK_DIR`.

**Linux (easiest):**

```bash
sudo apt install gperf cmake g++ libssl-dev zlib1g-dev
mkdir -p ~/telegram-bot-api-build && cd ~/telegram-bot-api-build
git clone --recursive https://github.com/tdlib/telegram-bot-api.git
cd telegram-bot-api
cmake -DCMAKE_BUILD_TYPE=Release .
cmake --build . --target install
# run it (keep the work dir in sync with TELEGRAM_BOT_WORK_DIR):
telegram-bot-api --local --api-id=<api_id> --api-hash=<api_hash> \
  --dir=$HOME/telegram-bot-data --http-port=8081
```

**Windows:** two options —
1. *WSL2 (recommended):* follow the Linux steps inside a WSL2 Ubuntu distro,
   then reach the server from Windows at `http://127.0.0.1:8081`.
2. *Native:* use the official build-instructions generator
   ([Windows](https://tdlib.github.io/telegram-bot-api/build.html?os=Windows))
   with Visual Studio C++ + CMake, or a community prebuilt binary.

The bot then runs against it with `python run.py --telegram` (or
`BOT_MODE=real`). Startup is fail-fast and secret-free: missing
`BOT_TOKEN`/API credentials or an unreachable local server produce clear error
messages and exit code 1.

---

## 5. Directory structure

| Path | Env var | Created | Role |
|---|---|---|---|
| `data/files.db` | `DATABASE_PATH` | automatically on first run | SQLite metadata (all state about links) |
| `data/storage/<public_id>/<filename>` | `FILE_STORAGE_DIR` | automatically on first transfer | FileToLink-owned bytes, one directory per link |
| `telegram_bot_data/` | `TELEGRAM_BOT_WORK_DIR` | by the Local Bot API Server | its own work dir — disposable **staging**, never application storage |
| `.env` | — | you (copy `.env.example`) | deployment configuration, never committed |
| `backups/` | — | `scripts/backup.py` | local backup snapshots, never committed |

Relative path values resolve against the project root. The database **and** the
storage directory together are the entire application state; everything else is
reproducible from the repository.

Repository layout (code):

```
app/
  api/          FastAPI app: download endpoint, /health, security headers
  bot/          transports (mock.py, telegram.py) + handlers.py + models.py
  config/       settings (environment → typed Settings)
  database/     connection, schema/migrations, repository, records
  services/     files (ingest), storage (ownership), file_sources
  utils/        public_ids, urls, mime, ranges, ratelimit, logging, console
scripts/        start_*.bat (five-window workflow), backup.py
tests/          hermetic test suite
run.py          entry point (--api, --telegram, default mock)
data/           runtime state: files.db + storage/ (git-ignored)
telegram_bot_data/  Local Bot API Server staging (git-ignored)
```

---

## 6. Configuration & environment variables

Configuration is read from environment variables, optionally loaded from a
local `.env` file (python-dotenv). Copy `.env.example` to `.env` and set the
deployment values. **Never commit `.env`, paste it into reports, or log it.**

Invalid or unsafe configuration **fails fast** with a clear message and exit
code 1; error messages never include secret values (tokens, api_id/hash). A
malformed `.env` line (stray quotes or whitespace around `KEY=value`) is a
common trap — the value is not parsed as written, so `BASE_URL` silently keeps
its `http://localhost:8000` default and links stay local-only.

| Variable | Default | Purpose |
| --- | --- | --- |
| `BOT_TOKEN` | *(unset)* | Real bot token from @BotFather (real mode). Example: `BOT_TOKEN=<your-bot-token>` |
| `BOT_USERNAME` | `file_to_link_bot` | Placeholder bot username |
| `BOT_MODE` | `mock` (auto) | `mock` or `real`; auto-selects `real` only when `BOT_TOKEN` is set |
| `TELEGRAM_API_ID` | *(unset)* | Free api_id for the Local Bot API Server |
| `TELEGRAM_API_HASH` | *(unset)* | Free api_hash for the Local Bot API Server |
| `LOCAL_BOT_API_URL` | `http://127.0.0.1:8081` | Your self-hosted Local Bot API Server |
| `TELEGRAM_BOT_WORK_DIR` | `telegram_bot_data/` | Server's staging dir (ingestion source, not durable storage) |
| `FILE_STORAGE_DIR` | `data/storage/` | FileToLink's **own** storage for received files (must differ from `TELEGRAM_BOT_WORK_DIR`) |
| `TELEGRAM_GET_FILE_READ_TIMEOUT_SECONDS` | `300` | Read timeout for the Local Bot API `getFile` request (large files) |
| `BASE_URL` | `http://localhost:8000` | Base URL for generated links (`{BASE_URL}/f/{id}`) |
| `HOST` | `127.0.0.1` | Download API bind host |
| `PORT` | `8000` | Download API port |
| `DATABASE_PATH` | `data/files.db` | SQLite database file (relative to project root) |
| `RATE_LIMIT_ENABLED` | `true` | Throttle `GET`/`HEAD /f/{id}` per client |
| `RATE_LIMIT_REQUESTS` | `60` | Requests allowed per window |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Window length for the limit |
| `RATE_LIMIT_MAX_KEYS` | `10000` | Max tracked clients (bounds limiter memory) |
| `TRUST_PROXY_HEADERS` | `false` | Trust `X-Forwarded-For` (only behind a proxy) |
| `TRUSTED_PROXIES` | *(unset)* | Comma-separated proxy IPs allowed to set it |

### Deployment configuration example

```bash
# .env  — placeholders only; never commit real values
BOT_MODE=real
BOT_TOKEN=<your-bot-token>
TELEGRAM_API_ID=<your-api-id>
TELEGRAM_API_HASH=<your-api-hash>
LOCAL_BOT_API_URL=http://127.0.0.1:8081
TELEGRAM_BOT_WORK_DIR=telegram_bot_data
TELEGRAM_GET_FILE_READ_TIMEOUT_SECONDS=300
FILE_STORAGE_DIR=data/storage
BASE_URL=https://desktop-b633ve8.tail20d174.ts.net
```

For public links, `BASE_URL` must be the funnel host (see
[Public access](#14-public-access)). `HOST` stays `127.0.0.1` because the
funnel connects from localhost.

---

## 7. Local development

Mock mode needs no token and no network:

```bash
python run.py
```

You get an interactive chat loop simulating a Telegram client:

```
▶ Starting file_to_link_bot in MOCK mode
🤖 file_to_link_bot — mock Telegram bot (mode: mock)
No bot token required. Type /start, /help, any message, or:
  send <path>   — simulate sending a local file to the bot
Type 'quit' or press Ctrl+C to exit.

You: /start
🤖 👋 Hi! I'm the File-to-Link Bot, ...

You: send samples/hello.txt
🤖 ✅ File received
   ...
   🔑 Public ID: t9obSSQq
   🔗 Download: http://localhost:8000/f/t9obSSQq
```

`send <path>` reads the local file for its name/size, guesses the MIME type
from the extension, and fabricates a mock Telegram file id. Metadata is
persisted to SQLite and the reply notes the stored record id. Missing or
unreadable files get a friendly error. Type `quit` or press `Ctrl+C` to exit.

Run the download API alongside it:

```bash
python run.py --api
# or: uvicorn --factory app.api.app:create_app --host 127.0.0.1 --port 8000
```

Mock-mode files are served from the local path recorded at send time, so links
keep working across restarts.

Direct entry points:

```bash
python -m app.bot.mock       # mock bot only
python run.py --api          # download API
python run.py --telegram     # real Telegram bot (needs a Local Bot API Server)
```

---

## 8. The five-window setup

On the verified Windows deployment the system runs as five foreground processes,
each in its own console window. Nothing is hidden and no service manager is
installed.

### Window 1 — Local Telegram Bot API Server

Runs the official `telegram-bot-api` binary with `--local --http-port=8081`,
storing received files in a working directory that must match
`TELEGRAM_BOT_WORK_DIR`.

```bat
scripts\start_bot_api_server.bat <path-to-telegram-bot-api.exe> <api_id> <api_hash>
```

Or run it directly (e.g. inside WSL2):

```bash
telegram-bot-api --local --api-id=<api_id> --api-hash=<api_hash> \
  --dir=$HOME/telegram-bot-data --http-port=8081
```

### Window 2 — FileToLink Python Telegram bot

Long-polls the Local Bot API Server and ingests files.

```bat
scripts\start_bot.bat
```

Equivalent: `python run.py --telegram`

### Window 3 — FastAPI

Serves `GET`/`HEAD /f/{public_id}` and `GET /health`, on `HOST:PORT`
(default `127.0.0.1:8000`).

```bat
scripts\start_api.bat
```

Equivalent: `python run.py --api`

### Window 4 — Tailscale Funnel

Publishes FastAPI to the public Internet over HTTPS.

```bash
tailscale funnel 8000
```

### Window 5 — Testing

The operator's window for `pytest`, `curl`, `sqlite3`, and `tailscale` checks.

### Dependencies between windows

| Window | Process | Start with | Depends on |
|---|---|---|---|
| 1 — Local Telegram Bot API Server | `telegram-bot-api --local`, port 8081 | `scripts\start_bot_api_server.bat <exe> <api_id> <api_hash>` | api_id/api_hash from my.telegram.org |
| 2 — FileToLink Python Telegram bot | long polling | `scripts\start_bot.bat` | Window 1 (pings it, exits if unreachable) |
| 3 — FastAPI | download API on `HOST:PORT` | `scripts\start_api.bat` | SQLite + FileToLink storage only |
| 4 — Tailscale Funnel | `tailscale funnel 8000` | `tailscale funnel 8000` | Window 3 |
| 5 — Testing | `pytest`, `curl`, `tailscale`, `sqlite3` | — | — |

**Windows 2 and 3 are independent of each other; Window 4 must come after 3.**
Serving an existing link needs **Window 3 only** — Windows 1 and 2 can be down.
New Telegram ingestion needs **Windows 1 and 2**. Public access needs
**Windows 3 and 4**; local access needs Window 3.

---

## 9. Starting the system

Start in order **1 → 2 → 3 → 4 → 5**.

```bat
REM Window 1 — Local Telegram Bot API Server
scripts\start_bot_api_server.bat <path-to-telegram-bot-api.exe> <api_id> <api_hash>

REM Window 2 — FileToLink Python Telegram bot
scripts\start_bot.bat

REM Window 3 — FastAPI
scripts\start_api.bat

REM Window 4 — Tailscale Funnel
tailscale funnel 8000

REM Window 5 — verify
curl -i http://127.0.0.1:8000/health
```

The scripts run in the foreground of their own terminal and hide nothing; the
bot and API print the resolved database and file-storage paths at startup, so a
misconfiguration is visible immediately. On Linux/macOS (or WSL2 for every
window) run the same commands directly: `python run.py --telegram` /
`python run.py --api`.

After starting, compare the **Window 2 and Window 3 startup banners**: their
database and file-storage paths must match. A mismatch is the classic "bot
stores here, API serves there" failure.

---

## 10. Stopping & restarting the system

Shut down in reverse order — **4, then 3, then 2, then 1** — with `Ctrl+C` in
each window. Stopping Window 1 while 2/3 still run does not break existing
downloads (serving never touches the Local Bot API work dir); it only stops new
files from being received.

Restart behavior (verified on the real deployment):

- **FastAPI restart — always safe.** It is stateless (fresh rate limiter,
  records re-read from SQLite); links keep working apart from the brief
  port-rebind gap.
- **Bot restart — safe.** Long polling resumes; adopted files and records
  persist. Updates still queued in the Local Bot API Server are re-delivered.
- **Local Bot API Server restart — safe for existing links.** They are served
  from `FILE_STORAGE_DIR`, never from its work dir, which may even be wiped
  between restarts (files not yet adopted at that moment are lost to ingestion;
  no link ever depended on them).
- **A missing owned file — `404`, never `500`, and never a burned download.**
  A link `active` in the database but missing on disk answers `404` (no
  download slot consumed); putting the bytes back makes the same public link
  work again immediately, with no restart and no database edit.

### After a Windows restart

No auto-start is installed (by design). Start
`scripts\start_bot_api_server.bat`, then `scripts\start_bot.bat`, then
`scripts\start_api.bat`, then `tailscale funnel 8000`; compare the Window 2/3
banners, then `curl /health` and fetch one old link.

### What a component failure looks like

| Stopped | Existing links | New uploads | Public links | Local links |
|---|---|---|---|---|
| Window 1 (Local Bot API) | keep working | fail (bot cannot fetch the file) | unaffected | unaffected |
| Window 2 (bot) | keep working | fail until restarted (queued updates re-delivered) | unaffected | unaffected |
| Window 3 (FastAPI) | unavailable while down; files/database untouched | ingested and stored, but no link served yet | fail | fail |
| Window 4 (Funnel) | unaffected | unaffected | fail (`BASE_URL` unreachable) | unaffected |

Expect these. A dependency failure is not a bug: each component reports its own
failure (Window 2 exits with an actionable message when Window 1 is missing,
has a wrong token, or is unreachable) and recovers by starting the missing
window again.

---

## 11. Health checking

Window 3 exposes `GET /health`:

```bash
curl -i http://127.0.0.1:8000/health
# 200 {"status":"ok","database":"ok","storage":"ok"}
# 503 {"status":"error","database":"ok","storage":"error"}
```

Publicly:

```bash
curl -i https://desktop-b633ve8.tail20d174.ts.net/health
```

Expected healthy response:

```json
{
  "status": "ok",
  "database": "ok",
  "storage": "ok"
}
```

- `200` = this process is serving, the configured SQLite file opens and carries
  the schema, and `FILE_STORAGE_DIR` exists or can be created.
- `503` names the failing component — and nothing else: no secrets, no paths,
  no rows.
- It **never touches a record**, so checking it cannot burn a `max_downloads`
  slot (a real download can). Use it instead of fetching a link to test Window 3.
- Independent of Telegram and Window 1 by design: serving links needs neither.
- It is rate limited like any other public route, so polling it more than
  `RATE_LIMIT_REQUESTS` times per window returns `429`.

---

## 12. Creating a file link

In real mode, a user sends a file to the bot in Telegram; the bot replies with
the public link. In mock mode, use `send <path>`:

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

The URL is `{BASE_URL}/f/{public_id}`. With the API running it **actually
serves the file**.

### Owner commands

```
/start             greet + usage
/help              help text
/delete <public_id>   permanently disable one of your links
/info   <public_id>   show status, downloads, and expiration
```

- **Ownership is enforced server-side**: the requesting Telegram user id is
  compared against the record's stored `user_id`. Only the owner can delete or
  inspect a link.
- `/delete` is idempotent: deleting an already-deleted link gives a friendly
  "already deleted" reply, and the FileToLink-owned file and its link directory
  are then removed.
- `/info` shows the file name, size, status, download count, max downloads,
  expiration, and the public URL — and never exposes `telegram_file_id`,
  `source_path`, database ids, or any secrets.
- Non-owner and unknown-id requests get a generic reply that reveals no private
  metadata.

See [Appendix A — Link management](#appendix-a--link-management) for expiry,
download limits, and lifecycle detail.

---

## 13. Downloading a file

```bash
curl -i http://localhost:8000/f/t9obSSQq        # 200 + file bytes
curl -i http://localhost:8000/f/doesNotExist    # 404
curl -i http://localhost:8000/health            # 200
```

Over the public Internet:

```bash
curl -I https://desktop-b633ve8.tail20d174.ts.net/f/<public_id>       # metadata
curl -o out.pdf https://desktop-b633ve8.tail20d174.ts.net/f/<public_id>
```

Behavior:

- `GET /f/{public_id}` returns `200` with the complete file, `206` for a valid
  single byte range, `416` for an unsatisfiable range, and `410 Gone` for
  deleted, expired, or download-exhausted links.
- The endpoint increments `download_count` once per successful GET (range GETs
  count once; HEAD never counts).
- `HEAD` mirrors the matching `GET` status and headers (`200`/`206`/`416`) with
  no body and **does not consume a download slot**.
- Files stream in 64 KB chunks (never fully read into memory); a range seeks
  and reads only the requested bytes.
- Stored MIME metadata is sanitised and files are always sent with
  `Content-Disposition: attachment`.

---

## 14. Public access

Public access is provided by **Tailscale Funnel**. The funnel exposes:

```
https://desktop-b633ve8.tail20d174.ts.net/     →     http://127.0.0.1:8000
```

The public file URL format is:

```
https://desktop-b633ve8.tail20d174.ts.net/f/{public_id}
```

Health endpoint:

```
https://desktop-b633ve8.tail20d174.ts.net/health
```

Terms used below: `BASE_URL` is what the bot puts into generated links, and the
funnel maps its public hostname to FastAPI's loopback port.

### Commands

```bash
tailscale funnel 8000          # start (foreground; Ctrl+C stops it)
tailscale funnel status        # is anything published?
tailscale funnel status --json # the exact mapping ("Proxy": "http://127.0.0.1:8000")
tailscale funnel off           # stop publishing
```

- **Running?** `tailscale funnel status --json` shows a `Foreground` entry whose
  `Proxy` is `http://127.0.0.1:8000` — this is the authoritative check. Plain
  `tailscale funnel status` is not: a working foreground funnel was observed to
  print `No serve config` as well, so never read that line as "public links are
  down".
- **`BASE_URL` is what makes the funnel useful.** Generated links are
  `{BASE_URL}/f/{public_id}`, so with the default `BASE_URL=http://localhost:8000`
  links are *local-only* even while the funnel is up. Set `BASE_URL` to the
  funnel host shown by `tailscale funnel status` to hand out publicly usable
  links, then check the local URL first when a public link fails.
- Changing `BASE_URL` affects only newly generated links: stored files and
  public ids are untouched, and the id is generated independently of the
  hostname (never hard-code your Tailscale hostname in the project — keep it in
  `.env`).
- Funnel publishes **`:8000` only**. Public links work only while Window 3 *and*
  Window 4 are up; stopping Window 4 leaves local links working.

### The Local Bot API Server stays private

The Local Bot API Server listens on `127.0.0.1:8081` (plain HTTP) and must
**NOT** be exposed publicly:

- **Do not expose port 8081 publicly.**
- **Funnel should point to FastAPI on port 8000.**
- **Public clients only interact with the FastAPI public endpoint.**

The funnel is deliberately left in the **foreground** (no `--bg`, no service).

---

## 15. Storage ownership

Telegram's Local Bot API Server manages the files in its own work directory and
may evict them at any time. A link that pointed there could silently break — a
400+ MB MKV was once successfully stored and served, then vanished from the
server's directory while the database record stayed active. FileToLink therefore
takes ownership of every received file.

### Telegram staging vs FileToLink durable storage

- **Telegram staging — `telegram_bot_data/` (`TELEGRAM_BOT_WORK_DIR`).** The
  Local Bot API Server's own working directory. It is **disposable** and may be
  cleaned up or wiped at any time. It is an ingestion source only — it is
  **not** durable storage.
- **FileToLink durable storage — `data/storage/` (`FILE_STORAGE_DIR`).**
  FileToLink's own copy of every received file, laid out one directory per link:
  `data/storage/<public_id>/<filename>`. This is the durable location.

FileToLink transfers the file into owned storage **before** it considers the
file durable and before any link exists. The public download endpoint serves the
owned file, so downloads do **not** depend on Telegram's temporary staging file
remaining present. If the staging directory disappears, existing links are
unaffected; only files that had not yet been adopted at that moment are lost to
ingestion.

This is **local storage, not cloud storage.** The bytes never leave the machine,
and FileToLink holds a copy of every file it links to, so reserve disk space
accordingly (a 400 MB upload needs 400 MB in `FILE_STORAGE_DIR`).

### How the transfer works

- **A move when it is safe**: if the Telegram file and FileToLink storage are on
  the same filesystem (the usual case on one Windows drive) the file is moved
  with an atomic rename — instant, and with no second copy of a large file.
  Otherwise (different drive, or a rename that is not permitted) it is
  **streamed** in 1 MiB chunks into a `.part` file, size-verified, moved into
  place, and only then is the source removed. The whole file is never read into
  memory.
- **Never loses the file**: the source is deleted only after the destination is
  fully written and verified; if the transfer fails, the source stays put and
  the partial destination is removed.
- **No orphans**: if the record cannot be written after a successful transfer,
  the adopted file is rolled back, so no unreferenced file is left in storage.

### Deleting an owned file

`/delete` soft-deletes the link first (it stops being downloadable immediately),
then removes the FileToLink-owned file and its link directory. Cleanup never
touches anything outside `FILE_STORAGE_DIR` — in particular never the Local Bot
API Server's files — and if the filesystem removal fails the deletion still
succeeds (the failure is logged internally, never exposed and never turned into
an API error). Expired or fully-downloaded links keep their file; removing it
stays an explicit action (there is no background cleanup daemon).

---

## 16. Backup

The application state is exactly two things, and they are one logical unit
(`source_path` in the database points into the storage tree):

1. **`data/files.db`** — all metadata: links, owners, counters, lifecycle state
2. **`data/storage/`** — the bytes those links point to

### One-command backup

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
- `backups/` is git-ignored. **No scheduler is installed** — run it by hand, or
  wire it into Windows Task Scheduler yourself if you want it automatic.

### Backup by hand

To back up safely, stop Window 2 and Window 3 first, or use SQLite's online
backup for a live-consistent copy, then copy the storage directory:

```bash
sqlite3 data/files.db ".backup 'backup/files.db'"
cp -r data/storage backup/storage
```

### What each half alone gives you

- database without files → links answer `404` (record active, bytes missing)
- files without database → nothing is served (no records); the storage is
  unreferenced and can be deleted

---

## 17. Restore & recovery

**Restore both halves together and links survive — wherever they land.** Copy
both halves back *together* while Window 2 and Window 3 are stopped:
`files.db` over `DATABASE_PATH`, `storage/` over `FILE_STORAGE_DIR`.

The storage layout is keyed by the public id
(`data/storage/<public_id>/<filename>`), so after restoring the database and the
storage directory together, each link is resolved through its public id. This
holds even when the restore is to a different folder, a different drive, or
another machine: the absolute paths recorded at ingest time are no longer
required, and no SQL needs rewriting. `/delete` uses the same resolution, so
cleanup also works after a moved restore.

There is **no automated restore system** — restore is a manual copy of the two
halves as described here.

### Recovery scenarios

- **Telegram staging files disappear** (`telegram_bot_data/` wiped): existing
  links keep working, because they are served from `FILE_STORAGE_DIR`, never the
  staging directory. Files not yet adopted at that moment are lost to ingestion;
  no link ever depended on them, so no public link breaks.
- **A durable owned file is missing**: the link answers `404` (record active,
  bytes missing) without burning a download slot. Put the bytes back and the
  same public link works again immediately — no restart and no database edit.
- **Crashed/interrupted ingestion**: can leave an unreferenced file in storage
  (e.g. a transfer that succeeded but whose record was never written). It has no
  public id in the database, so it is never served and is safe to delete; a
  partially copied `.part` file is likewise never served. There is no background
  cleanup daemon: expired or fully-downloaded links keep their bytes until an
  owner deletes the link.

### Verifying a restore

After restoring, compare the Window 2 and Window 3 startup banners (their
database and file-storage paths must match), then:

```bash
curl -i http://127.0.0.1:8000/health          # 200
curl -i http://127.0.0.1:8000/f/<public_id>   # 200, expected size
```

---

## 18. Security considerations

Everything below is free/open-source and local — no paid service, no Redis, no
external infrastructure.

### Rate limiting

- `GET` and `HEAD /f/{public_id}` are limited **per client identity**: by
  default `RATE_LIMIT_REQUESTS` requests per `RATE_LIMIT_WINDOW_SECONDS` (60 per
  60 s). Over the limit returns **`429 Too Many Requests`** with a
  `Retry-After` header. Disable with `RATE_LIMIT_ENABLED=false`.
- The limiter is a small in-process sliding window
  (`app/utils/ratelimit.py`) — appropriate for this single-process,
  self-hosted app. If you ever run several worker processes, each keeps its own
  window (limits become per-worker).
- The limit is checked **before any lookup**, so probing for public ids is
  throttled too. Keys are **client identities only — never link ids**, so
  random/invalid ids cannot grow per-file state. Tracked keys are capped by
  `RATE_LIMIT_MAX_KEYS` (least-recently-used eviction), so memory cannot grow
  without bound from attacker-controlled addresses.

### Client identity / proxy trust

By default the limiter keys on the **actual TCP peer address**, and
`X-Forwarded-For` is ignored — a client cannot spoof its identity. Behind the
funnel every visitor arrives from `127.0.0.1`, so per-visitor rate limiting
needs `TRUST_PROXY_HEADERS=true` and `TRUSTED_PROXIES=127.0.0.1` (with the
`X-Forwarded-For` the funnel adds); otherwise all visitors share one loopback
bucket. The forwarded address is trusted **only** when the direct peer is a
listed proxy. Configuration validation refuses `TRUST_PROXY_HEADERS=true` with
an empty `TRUSTED_PROXIES`, and rejects invalid IPs.

### Response security headers

Every download response (including errors and 429s) carries
`X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none';
sandbox`, and `Referrer-Policy: no-referrer`. Files are always sent with
`Content-Disposition: attachment`, so range requests and normal downloads are
unaffected.

### MIME / content hardening

Stored MIME metadata is untrusted, so it is sanitised (`app/utils/mime.py`):
well-formed `type/subtype` values are preserved, while missing, empty, or
malformed values (including anything containing control characters that could
inject headers) fall back to `application/octet-stream`. This only guarantees a
safe *header* — it is not content inspection.

### Request hardening

- The public-id path parameter is length-checked (exactly 8 chars) before the
  format regex, so oversized paths are rejected cheaply.
- Malformed/oversized/multi-range headers are rejected as `416` without opening
  the file; parsing stays bounded and files are streamed in 64 KB chunks (never
  fully read into memory).
- `HEAD` mirrors the matching `GET` status and headers (`200`/`206`/`416`) with
  no body and **without consuming a download slot**.

### Host binding

`HOST` defaults to `127.0.0.1` (localhost only). Binding to a non-loopback
address (e.g. `0.0.0.0`) exposes the API beyond your machine; the server prints
a warning, and since links are public URLs anyone holding one can download, do
this only on a trusted network.

### LocalFileSource (mock mode)

`LocalFileSource` (mock mode) has **no path containment**, by design:
`source_path` is set only by the local operator via `send <path>` (or tests),
never from untrusted remote input, so serving operator-chosen local files is the
intended local-testing behavior. Real Telegram mode uses `StorageFileSource`,
which only ever serves files inside `FILE_STORAGE_DIR` and rejects
traversal/symlink escapes (`TelegramFileSource` enforces the same rules for
records that still point at the Local Bot API work directory).

### Deployment security notes

- Only the FastAPI server needs public exposure, and only through Tailscale
  Funnel. `HOST` stays `127.0.0.1` (the funnel connects from localhost); the
  server warns if bound wider. Never funnel/forward the Local Bot API port 8081
  — it speaks plain HTTP and is for this machine only.
- Data-at-rest protection is the Windows user profile's file permissions: keep
  `DATABASE_PATH`/`FILE_STORAGE_DIR` inside the user profile on shared machines.
- Secrets (`BOT_TOKEN`, `TELEGRAM_API_ID/HASH`) live only in the untracked
  `.env`; the bot's logging redacts the token (see
  [Operational notes](#21-operational-notes)).

---

## 19. Testing

```bash
pytest
```

Phase 20 result: **371 tests passed, 2 skipped, 2 warnings.**

The suite is hermetic: it runs against the shipped defaults regardless of the
deployment `.env` (a shared fixture isolates FileToLink environment variables
per test), so results are identical on a fresh clone and on an operator machine.
Symlink tests skip where Windows lacks the privilege.

Coverage includes:

- **Bot handlers** — `/start`, `/help`, unknown commands, plain text,
  empty/absent messages, file receiving (full metadata display, missing
  name/size/MIME/timestamp, captions, size formatting, missing-file errors).
- **Database** — schema creation, create/retrieve/update/invalidate/delete,
  defaults, indexes, service-layer persistence, handler + transport
  integration, the handler's no-service path, and the `max_downloads` migration
  keeping existing records intact.
- **Public ids** — format, uniqueness, collision retry, exhaustion.
- **URL generation** — format, normalization, validation.
- **API** — valid download with headers + counter, invalid/unknown → 404,
  deleted/expired → 410, missing source → 404, full mock-send → HTTP loop,
  schema migration, `Range`/`HEAD`/lifecycle behavior.
- **Real Telegram transport** — PTB update → normalized models (incl. missing
  mime/name/captions), source containment, symlink/traversal rejection, config
  validation, application wiring, and the simulated document flow storing +
  serving bytes — all offline, no token or server needed.
- **File ownership** — move vs. streamed copy, size verification, source
  removal only after destination verification, partial-destination cleanup,
  unreadable/directory/missing sources, duplicate names, path-traversal
  filenames, bounded-chunk streaming of a multi-megabyte file, rollback when
  the record cannot be written, owned-file deletion + cleanup failure, and
  real-mode API serving/Range/HEAD/lifecycle behavior on owned files.
- **Link management** — expiration, download limits (including a real
  concurrency test with >N parallel GETs against `max_downloads=N`), deletion
  idempotency, owner-only `/delete` and `/info`, metadata privacy,
  unsatisfiable ranges not consuming slots.
- **Reliability & operations** — FastAPI/bot/Local Bot API restarts, a wiped
  staging work dir, live-consistent SQLite backup, restoring database +
  storage together (including to a different location), counters/limits across
  restarts, a missing owned file giving 404 without burning a download and
  recovering when the bytes return, orphaned and partial `.part` files never
  being served, interrupted ingestion leaving no broken active record, and
  expired/deleted records staying terminal across a restart.
- **Operations (Phase 19)** — health-endpoint semantics and its refusal to
  consume a download slot, token redaction reaching root handlers, quieted
  request-URL loggers, the credential-vs-connectivity startup messages, invalid
  `LOCAL_BOT_API_URL` and unusable `DATABASE_PATH` failing fast without a
  traceback, and the backup script (both halves present, byte-identical
  storage, no `.env`/staging data, no silent overwrite, and a restore that
  still serves the links).

Because handlers are transport-free, the same tests pass in both mock and real
modes.

---

## 20. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Bot exits: "Cannot reach the Local Bot API Server" | Window 1 not running, or `LOCAL_BOT_API_URL` wrong — start Window 1 first. The failure class is printed (`NetworkError`, …) |
| Bot exits: "rejected BOT_TOKEN" | The server answered and refused the credentials — the token is wrong or was revoked at @BotFather. Not a connectivity problem |
| Bot exits: "LOCAL_BOT_API_URL must be an absolute http(s) URL" | Typo in `.env` (missing scheme/port) — validated at startup instead of surfacing as "unreachable" |
| `run.py --api` exits: "Cannot open the SQLite database at …" | `DATABASE_PATH` is a directory, an unreadable location, or a missing drive |
| `/health` → 503 | The named component is unusable: `database` (path/moved file) or `storage` (missing/unwritable `FILE_STORAGE_DIR`) |
| `tailscale funnel status` → "No serve config" | Not proof the funnel is down — a working *foreground* funnel prints this line too. Check `tailscale funnel status --json`: no `Foreground` entry means Window 4 is not publishing (run `tailscale funnel 8000`; public links down, local links fine) |
| Bot exits listing `BOT_TOKEN`/`TELEGRAM_API_*` | `.env` incomplete for real mode; mock mode still works (`python run.py`) |
| `run.py --api`: bind error 10048 | Port already in use — another API instance is running, or `PORT` conflicts |
| Links 404 after a restart | Bot and API resolved different `DATABASE_PATH`/`FILE_STORAGE_DIR` — compare the startup banners |
| Link active but GET 404 | Owned file missing (storage moved/restored incompletely) — restore the bytes, or check the startup banners |
| `410 Gone` | Terminal by design: deleted, expired, or download limit consumed |
| Works locally, dead publicly | `BASE_URL` ≠ funnel URL, Window 4 funnel not started, or Window 3 not running |

### Diagnostics playbook

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

---

## 21. Operational notes

This section is what to check before leaving FileToLink running for hours or
days, and what each component failure looks like. Everything runs for one
operator on one Windows machine.

### Dependency graph (verified on the real deployment)

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

- **Serving an existing link needs Window 3 only.** Windows 1 and 2 can be down;
  downloads never touch the Local Bot API work directory.
- **New Telegram ingestion needs Windows 1 + 2.**
- **Public access needs Windows 3 + 4.** Local access needs Window 3.
- Window 4 publishes **:8000 only** — never funnel :8081.

### Logging (console only, secret-free)

- **No log files are created anywhere** — every process logs to the console of
  its own window. There is nothing to rotate and no logging framework beyond the
  standard library; use the window's scrollback, or redirect with `>` if you
  want a file (your choice, not the app's).
- **`BOT_TOKEN` never reaches the log.** The Local Bot API request URL embeds
  the token (`<LOCAL_BOT_API_URL>/bot<TOKEN>/<method>`), and `httpx` logs every
  request URL at `INFO` — which is exactly how earlier real runs printed the
  token. `app/utils/logging_setup.py` now (a) raises the `httpx`/`httpcore`
  loggers to `WARNING` and (b) attaches a redaction filter to the process's root
  handlers, so even a token appearing in a third-party record at any level is
  masked as `<redacted>` before it is written.
- Startup banners print configuration that is *not* secret (bot mode, database
  path, storage path); configuration errors never include secret values.

### Deliberately not installed

No Windows service, no Task Scheduler entry, no background funnel, no log
rotation, no monitoring, no containers, no Redis/PostgreSQL, no cloud storage.
The app is a bot process, an API process, a SQLite file, and a storage
directory.

---

## 22. Current limitations

- **No cloud durability.** Files live on the operator's machine; there is no
  cloud storage or replication. If the disk dies and there is no backup, the
  bytes are gone.
- **No automated restore.** Backup is one command (`scripts/backup.py`), but
  restore is a manual copy of the two halves while Windows 2 and 3 are stopped.
  No restore automation is implemented.
- **Single byte ranges only** (no multipart/byteranges); malformed or
  unsatisfiable ranges return `416`.
- **Per-process rate limiting, not per-link.** Rate limiting is per-client and
  in-process; there is no signed-URL or per-user authentication beyond the owner
  check on `/delete` and `/info`. Public links are bearer URLs — anyone holding
  one can download.
- **No background cleanup.** Expired or fully-downloaded links keep their
  FileToLink-owned file until the owner deletes the link; the file is never
  served once the link is inactive.
- **No scheduler / auto-start.** Backups and process startup are manual by
  design.
- **Telegram mode needs the Local Bot API Server.** Real ingestion requires
  that separate binary and credentials; mock mode remains the default for
  development.
- Link expiration, download limits, and deletion are per-link database
  settings.
- A range-size cap is deliberately **not** implemented: a range can never return
  more than the file itself (no amplification), and full large-file downloads
  are the project's purpose — request frequency is what the rate limiter covers.

---

## 23. Release verification status

- **Phases 1–20 complete and validated**; Phase 21 adds release documentation.
- **Latest commit:** `3edad46` — "fix(release): correct stale funnel guidance
  and ignore staging dir". Git status is clean and `main` is up to date with
  `origin/main`.
- **Test suite (Phase 20):** 371 passed, 2 skipped, 2 warnings.
- **Performance and reliability testing:** completed.
- **Large-file testing:** completed successfully. A test file of approximately
  **205 MB** was stored, served locally, served publicly, downloaded completely,
  downloaded using `Range` requests, and verified by SHA-256:

  ```
  1FEC5192D42AE8B7E5212607585A2C3164C4FF53AC27D869111DEBDEBE993226
  ```

### Real-world end-to-end verification (the decisive test)

A **fresh PDF** uploaded through Telegram was downloaded from the public
FileToLink URL:

- **Public ID:** `dSgb5xCX`
- **Size:** `2,294,697 bytes`

It was tested from:

1. A PC through the public Internet
2. A normal phone
3. A phone **without** Tailscale installed
4. Mobile Internet

The phone successfully started and completed the download. The complete
real-world path is therefore verified:

```
Telegram → FileToLink → durable storage → FastAPI
        → Tailscale Funnel → public HTTPS → normal mobile phone
        → successful download
```

This verification is part of the release record and must not be removed or
invalidated.

### Earlier verified milestones

| Phase | Verification |
|---|---|
| 14B | File ownership: FileToLink keeps its own copy of every received file; downloads no longer depend on Telegram's staging. |
| 15 | Deployment readiness: reproducible startup, restart, and backup for the same Windows/local, ₹0-cost deployment. |
| 18 | Real-machine restart/ownership/staging-loss checks on a real DOCX (public id `ylb6n993`, 46,101 bytes) — link survived bot restart, FastAPI restart, Local Bot API restart, and destruction of the staging work dir. |
| 19 | Public path verified on the real deployment with a PDF (97,328 bytes): public `HEAD`, full download, and an off-network phone on mobile data. |
| 20 | Full system/release validation: 371 passed, 2 skipped, 2 warnings; ~205 MB large-file SHA-256 verified. |
| 21 | Release documentation finalized; real-world E2E download from a normal phone confirmed (`dSgb5xCX`, 2,294,697 bytes). |

---

## Appendix A — Link management

Every stored link can carry two optional settings: an expiration time and a
maximum number of downloads. Both are stored per record (`expires_at`,
`max_downloads`) and are optional — when omitted the link lives forever with no
download limit.

### Expiration

- `expires_at` is a Unix timestamp (UTC). `NULL` means the link never expires; a
  future timestamp means active; a past timestamp means expired.
- Expiration is **lazy**: no background worker runs. The first request after the
  deadline marks the link `expired` and returns `410 Gone` — no bytes are served
  and the download counter is not incremented.

### Maximum download counts

- `max_downloads = NULL` → unlimited downloads.
- `max_downloads = 0` → no downloads allowed (always `410`).
- `max_downloads = N` → at most `N` successful GET requests.
- Each GET (including a range GET) consumes **exactly one** download slot;
  `HEAD` consumes none.
- The slot is reserved with a **single atomic conditional UPDATE**, so
  concurrent requests can never exceed the limit (no check-then-act race). A
  request that finds the limit reached returns `410` without serving bytes or
  incrementing the counter.
- The GET that consumes the final permitted slot immediately transitions the
  link `active → expired` in the same atomic operation, so `/info` shows the
  real state right away. If a reserved slot cannot be served (missing source
  file, unsatisfiable range), the slot is released and the transition is rolled
  back, so failed requests do not burn a download.

### Deletion

- Deleting a link sets `status = 'deleted'` and prevents all future downloads
  (`410 Gone`). Metadata is preserved.
- The FileToLink-owned file (and its link directory) is then removed — see
  [Storage ownership](#15-storage-ownership). A mock-mode/operator file outside
  FileToLink storage is never touched, and a filesystem cleanup failure never
  turns a successful deletion into an error.
- Deletion is idempotent and terminal: a deleted link is never reactivated.

### Lifecycle statuses

`active` → `deleted` (owner deletes) or `active` → `expired` (time passed or
final download consumed). No other transitions exist: deleted or expired links
stay that way.

### Creating links with expiration/limits

The storage layer accepts optional `expires_at` and `max_downloads` when a file
is saved (`FileService.save_document` / `FileRepository.create`); omitting them
creates a permanent, unlimited link. There is no interactive configuration flow.

### HTTP Range requests

`GET /f/{public_id}` supports standard single byte ranges
(`Accept-Ranges: bytes`):

- No `Range` header → `200` with the complete file.
- `Range: bytes=0-99` / `bytes=100-` / `bytes=-100` → `206 Partial Content` with
  `Content-Range: bytes <start>-<end>/<total>`, `Content-Length` of the range,
  `Content-Type`, and `Content-Disposition`. Only the requested bytes are
  streamed (seek + bounded 64 KB reads — never a whole-file read).
- Malformed or unsatisfiable ranges (e.g. `bytes=999-1000` on a small file,
  `bytes=100-50`, bad syntax, multi-range) → `416` with
  `Content-Range: bytes */<total>`. Multi-range responses are not implemented.
- `HEAD /f/{public_id}` returns the same metadata headers with no body and does
  not touch the download counter; 404/410 behavior is preserved.

---

## Appendix B — Database schema

- **Engine:** Python's built-in `sqlite3` — local file, zero cost, no server.
- **Location:** `data/files.db` by default; configurable via `DATABASE_PATH`.
  The directory is created automatically on first run.
- **Files table:** `id`, `public_id` (UNIQUE, written together with the row),
  `telegram_file_id`, `telegram_message_id`, `user_id`, `file_name`, `file_size`,
  `mime_type`, `created_at` (epoch seconds), `expires_at` (NULL = never),
  `download_count` (default 0), `max_downloads` (NULL = unlimited), `status`
  (`active`/`deleted`/`expired`, default `active`). Indexed on `public_id`,
  `telegram_file_id`, and `status`.
- **Migration:** pre-existing databases (without `max_downloads`) get the column
  added automatically on startup; existing records receive `max_downloads = NULL`
  (unlimited) and are otherwise untouched.
- **`source_path`** is the FileToLink-owned path in real mode. Records created
  before file ownership still point at the Local Bot API Server's work directory
  (which the server may have evicted); re-send those files through the bot to get
  FileToLink-owned copies.
- **Layering:** `app/database/` owns all SQL (connection + schema + repository).
  `app/services/files.py` turns normalized file messages into stored records. Bot
  handlers and the mock transport never touch raw SQL.
- **Inspect it:** `sqlite3 data/files.db 'SELECT * FROM files;'`

---

## Appendix C — Public IDs & links

### Public IDs

- Every stored file gets an 8-character public id from `app/utils/public_ids.py`
  — generated with Python's `secrets` (CSPRNG), URL-safe (`A-Z a-z 0-9 _ -`),
  never sequential, timestamps, or Telegram file ids.
- The UNIQUE index on `public_id` is the source of truth: on the astronomically
  unlikely collision, `FileService` generates a fresh id and retries (bounded,
  10 attempts) — another file's id is never overwritten.

### Public links (URLs)

- **Format**: `{BASE_URL}/f/{public_id}` — e.g.
  `http://localhost:8000/f/t9obSSQq`, or publicly
  `https://desktop-b633ve8.tail20d174.ts.net/f/dSgb5xCX`. The public id is used
  unchanged; internal row ids and Telegram file ids are never exposed.
- **`BASE_URL`** comes from the environment (`.env`), defaulting to
  `http://localhost:8000` for local development. It must include a scheme;
  trailing slashes and surrounding whitespace are tolerated.
- **Construction** lives in `app/utils/urls.py` (`generate_file_url`), kept out
  of the repository, transports, and SQL. Inputs are validated (public id format,
  base URL scheme).
- **Flow**: `FileService.save_document()` → record with public id →
  `generate_file_url(base_url, public_id)` → handler reply.
- **The URL is live**: `GET /f/{public_id}` serves the stored bytes — `200` for a
  full download, `206` for a valid `Range`, `416` for an unsatisfiable range, and
  `HEAD` for metadata only. Deleted, expired, or download-exhausted links return
  `410 Gone`.

### How the mock works

`app/bot/mock.py` builds normalized `Update` objects — from text lines or from
local files via `send <path>` — and feeds them into `handle_update()`, the exact
function the real bot calls. There is no network, no token, and nothing to leak.
The real transport (`app/bot/telegram.py`) does the same normalization for live
Telegram updates, so both transports share 100% of the downstream logic.
