"""Project entry point.

Starts the bot in the configured mode. Until the real Telegram integration
lands (a later phase), this launches the mock transport so the project runs
with no BOT_TOKEN and no external services.
"""
from app.bot.mock import repl
from app.config import get_settings
from app.utils.console import ensure_utf8_stdout


def main() -> None:
    ensure_utf8_stdout()
    settings = get_settings()
    print(f"▶ Starting {settings.bot_username} in {settings.bot_mode.upper()} mode")
    if not settings.is_mock:
        print(
            "⚠️  Real Telegram polling is not implemented yet "
            "(it lands in a later phase) — falling back to mock transport."
        )
    repl()


if __name__ == "__main__":
    main()