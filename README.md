# Telegram File-to-Link Bot

A zero-cost Telegram bot that receives files from users and generates a public
link that can be used to access/download those files.

> **Status — Milestone 2 (file receiving, mock transport).**
> The bot runs locally on a **mock Telegram integration**: no `BOT_TOKEN`, no
> external services, ₹0 cost. `/start` and `/help` work, and sending a file
> yields an acknowledgment with its metadata (file id, name, size, MIME type,
> message id, user id, timestamp). The database, link generation, and the
> download endpoint arrive in later phases.

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
                  SQLite (Phase 3+)   File handling (Phase 2+)
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
| `BASE_URL`     | `http://localhost:8000`| Public base URL for generated links (Phase 5+) |
| `HOST`         | `127.0.0.1`            | API host (Phase 5+)                            |
| `PORT`         | `8000`                 | API port (Phase 5+)                            |

## Running the bot

```bash
python run.py
```

or directly:

```bash
python -m app.bot.mock
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
```

`send <path>` reads the local file for its name/size, guesses the MIME type
from the extension, and fabricates a mock Telegram file id. Missing or
unreadable files get a friendly error.

## Testing

```bash
pytest
```

The tests cover the bot handlers (`/start`, `/help`, unknown commands, plain
text, empty/absent messages) and file receiving (full metadata display,
missing name/size/MIME type/timestamp, captions with documents, size
formatting, mock file-document extraction, and missing-file errors). Because
handlers are transport-free, the same tests will pass once the real Telegram
integration lands.

## Testing

```bash
pytest
```

The tests cover the bot handlers: `/start`, `/help`, unknown commands,
plain text, and empty/absent messages. Because handlers are transport-free,
the same tests will pass once the real Telegram integration lands.

## How the mock works

`app/bot/mock.py` builds normalized `Update` objects — from text lines or
from local files via `send <path>` — and feeds them into `handle_update()`,
the exact function the real bot will call. There is no network, no token,
and nothing to leak.

## Milestone plan (phase-by-phase)

1. ✅ Bot scaffold + mock transport (`/start`, `/help`)
2. ✅ File receiving (documents) + metadata extraction
3. ⏳ SQLite database layer
4. Unique public IDs
5. Link generation
6. Download endpoint
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
- Files are acknowledged with metadata only — no storage, no public links.
- No database, links, or download endpoint yet.
- No rate limiting or link management yet.