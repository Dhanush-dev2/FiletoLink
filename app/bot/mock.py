"""Mock Telegram transport.

Simulates the Telegram Bot API locally so the project runs with zero
configuration and no ``BOT_TOKEN``. It feeds normalized updates straight
into the same handlers the real integration will use later, which is why
this milestone works without creating a bot or exposing any credentials.

Usage (interactive chat):
    python -m app.bot.mock

    or, via the project entry point:

    python run.py

In the chat loop:
    /start, /help          — bot commands
    any text               — a plain user message
    send <path>            — simulate sending a local file to the bot
"""
import base64
import itertools
import mimetypes
import os
import time

from ..config import get_settings
from ..services.files import FileService, make_file_service
from ..utils.console import ensure_utf8_stdout
from .handlers import handle_update
from .models import Document, Message, Update, User

_BOT_FACE = "🤖"

# Simulated sender for the mock chat loop.
MOCK_USER = User(id=123456789, username="mock_user", first_name="Mock")

_message_ids = itertools.count(1)


def _fake_file_id() -> str:
    """A Telegram-like file id (mock only; random on every call)."""
    raw = base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=")
    return f"BQAD{raw}"


def reply_to(text: str, user: User = MOCK_USER) -> str:
    """Route one plain user message through the bot and return the reply."""
    update = Update(
        update_id=1,
        message=Message(
            message_id=next(_message_ids),
            text=text,
            user=user,
            date=int(time.time()),
        ),
    )
    return handle_update(update)


def local_file_document(path: str) -> Document:
    """Build a normalized :class:`Document` from a local file path.

    Reads the file's name and size, guesses the MIME type from the
    extension, and fabricates a mock file id (real Telegram file ids are
    only available once the real integration lands).
    """
    file_name = os.path.basename(path)
    file_size = os.path.getsize(path)
    mime_type, _ = mimetypes.guess_type(path)
    return Document(
        file_id=_fake_file_id(),
        file_name=file_name,
        file_size=file_size,
        mime_type=mime_type,
    )


def send_local_file(
    path: str,
    user: User = MOCK_USER,
    file_service: FileService | None = None,
) -> str:
    """Simulate a user sending a local file; return the bot's reply.

    When no ``file_service`` is given, an ephemeral in-memory database is
    used (tests). Raises :class:`FileNotFoundError` (or another OSError)
    when the path cannot be read — the REPL turns that into a friendly
    message.
    """
    if file_service is None:
        file_service = make_file_service(":memory:")
    update = Update(
        update_id=1,
        message=Message(
            message_id=next(_message_ids),
            user=user,
            document=local_file_document(path),
            date=int(time.time()),
        ),
    )
    return handle_update(update, file_service=file_service)


def _print_reply(reply: str) -> None:
    print(f"{_BOT_FACE} {reply}\n")


def repl() -> None:
    """Interactive chat loop that simulates a Telegram client."""
    ensure_utf8_stdout()
    settings = get_settings()
    print(f"{_BOT_FACE} {settings.bot_username} — mock Telegram bot (mode: {settings.bot_mode})")
    print("No bot token required. Type /start, /help, any message, or:")
    print("  send <path>   — simulate sending a local file to the bot")
    print(f"💾 Files are stored in SQLite: {settings.database_path}")
    print("Type 'quit' or press Ctrl+C to exit.\n")

    file_service = make_file_service()

    while True:
        try:
            line = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{_BOT_FACE} Bye!")
            return
        if not line:
            continue
        if line.lower() in {"quit", "exit", "q"}:
            print(f"{_BOT_FACE} Bye!")
            return

        head, _, rest = line.partition(" ")
        if head.lower() == "send":
            path = rest.strip().strip('"')
            if not path:
                print(f"{_BOT_FACE} 📎 Usage: send <path> — simulate sending a file.\n")
                continue
            try:
                _print_reply(send_local_file(path, file_service=file_service))
            except FileNotFoundError:
                print(f"{_BOT_FACE} ❌ No such file: {path}\n")
            except OSError as exc:
                print(f"{_BOT_FACE} ❌ Could not read {path}: {exc}\n")
            continue

        _print_reply(reply_to(line))


if __name__ == "__main__":
    repl()