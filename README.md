# Telegram File-to-Link Bot

A zero-cost Telegram bot that receives files from users and generates a public
link that can be used to access/download those files.

> **Status — Milestone 6 (end-to-end local download, mock transport).**
> The bot runs locally on a **mock Telegram integration**: no `BOT_TOKEN`, no
> external services, ₹0 cost. `/start` and `/help` work; sending a file yields
> an acknowledgment with its metadata, persists it to **SQLite**, assigns a
> secure **public id**, and replies with a **download URL**
> (`{BASE_URL}/f/{public_id}`) that now **actually serves the file** through
> a FastAPI endpoint (`python run.py --api`).

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
                  SQLite (data/files.db) ← app/database + app/services
                       │
                       ▼
                  Download endpoint (Phase 5+)
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
| `BOT_TOKEN`    | *(unset)*              | Real bot token (later phase). Not required now |
| `BOT_USERNAME` | `file_to_link_bot`     | Placeholder bot username                       |
| `BOT_MODE`     | `mock` (auto)          | `mock` or `real` (later phase)                 |
| `BASE_URL`     | `http://localhost:8000`| Base URL for generated links (`{BASE_URL}/f/{id}`) || `HOST`     | `127.0.0.1`            | API host (Phase 5+)                            |
| `PORT`     | `8000`                 | API port (Phase 5+)                            |
| `DATABASE_PATH` | `data/files.db`  | SQLite database file (relative to project root) |

## Running the bot

```bash
python run.py
```

or directly:

```bash
python -m app.bot.mock
```

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

- The endpoint increments `download_count` on every successful download.
- The file source is abstracted (`app/services/file_sources.py`): today it
  reads local paths recorded by the mock transport; the real Telegram
  integration will plug in a source that fetches bytes via the Bot API.
- HTTP Range Requests (seeking/resume) and refined streaming come in later
  phases.

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

## Database

- **Engine**: Python's built-in `sqlite3` — local file, zero cost, no server.
- **Location**: `data/files.db` by default; configurable via `DATABASE_PATH`.
  The directory is created automatically on first run.
- **Files table**: `id`, `public_id` (UNIQUE, filled in on save),
  `telegram_file_id`, `telegram_message_id`, `user_id`, `file_name`,
  `file_size`, `mime_type`, `created_at` (epoch seconds), `expires_at`
  (NULL = never), `download_count` (default 0), `status`
  (`active`/`deleted`/`expired`, default `active`). Indexed on `public_id`,
  `telegram_file_id`, and `status`.
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
schema migration). Because handlers are transport-free, the same tests will
pass once the real Telegram integration lands.

## Testing

```bash
pytest
```

The tests cover the bot handlers: `/start`, `/help`, unknown commands,
plain text, and empty/absent messages. Because handlers are transport-free,
the same tests will pass once the real Telegram integration lands.

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
- ⚠️ The generated URL is display-only at this phase: visiting it does not
  serve the file yet. The `GET /f/{public_id}` download endpoint is the
  next phase.

## How the mock works

`app/bot/mock.py` builds normalized `Update` objects — from text lines or
from local files via `send <path>` — and feeds them into `handle_update()`,
the exact function the real bot will call. There is no network, no token,
and nothing to leak.

## Milestone plan (phase-by-phase)

1. ✅ Bot scaffold + mock transport (`/start`, `/help`)
2. ✅ File receiving (documents) + metadata extraction
3. ✅ SQLite database layer
4. ✅ Unique public IDs
5. ✅ Link generation
6. ✅ Download endpoint
7. ⏳ Telegram Local Bot API Server (large files)
7. Telegram Local Bot API Server (large files)
8. Streaming
9. HTTP Range Requests
10. Link management (expiration, counters, deletion)
11. Security (rate limiting, validation)
12. Error handling
13. Logging
14. Testing
15. Free deployment options

Each milestone ends in a working, testable state. The real Telegram bot will
be connected after the core project is finalized.

## Limitations (current milestone)

- Mock transport only — no real Telegram traffic; mock file ids are
  fabricated locally.
- Files are served from local paths recorded by the mock (`source_path`);
  records without a local path (or with a deleted file) return 404. The
  real Telegram file source comes with the real integration.
- No HTTP Range Requests, refined streaming, expiration handling, link
  management, or rate limiting yet.