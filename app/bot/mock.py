"""Mock Telegram transport.

Simulates the Telegram Bot API locally so the project runs with zero
configuration and no ``BOT_TOKEN``. It feeds normalized updates straight
into the same handlers the real integration will use later, which is why
this milestone works without creating a bot or exposing any credentials.

Usage (interactive chat):
    python -m app.bot.mock

    or, via the project entry point:

    python run.py
"""
from ..config import get_settings
from ..utils.console import ensure_utf8_stdout
from .handlers import handle_update
from .models import Message, Update, User

_BOT_FACE = "🤖"


def reply_to(text: str, user: User | None = None) -> str:
    """Route one user message through the bot and return the reply text."""
    update = Update(
        update_id=1,
        message=Message(message_id=1, text=text, user=user),
    )
    return handle_update(update)


def repl() -> None:
    """Interactive chat loop that simulates a Telegram client."""
    ensure_utf8_stdout()
    settings = get_settings()
    print(f"{_BOT_FACE} {settings.bot_username} — mock Telegram bot (mode: {settings.bot_mode})")
    print("No bot token required. Type /start, /help, or any message.")
    print("Type 'quit' or press Ctrl+C to exit.\n")

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
        print(f"{_BOT_FACE} {reply_to(line)}\n")


if __name__ == "__main__":
    repl()