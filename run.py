"""Project entry point.

Starts the bot in the configured mode. The default is the mock transport
(no BOT_TOKEN and no network); ``--telegram`` (or ``BOT_MODE=real``) starts
the real Telegram bot, which needs a Local Bot API Server and credentials.

Use ``python run.py --api`` to start the FastAPI download server instead.
"""
import argparse

from app.bot import mock
from app.bot.mock import repl
from app.config import get_settings
from app.utils.console import ensure_utf8_stdout


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="File-to-Link Bot: mock Telegram bot or download API.",
    )
    parser.add_argument(
        "--api",
        action="store_true",
        help="run the FastAPI download server (GET /f/{public_id}) ",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="run the real Telegram bot (Local Bot API Server + long "
        "polling); requires BOT_TOKEN, TELEGRAM_API_ID/HASH and a running "
        "local server",
    )
    return parser.parse_args()


def run_api() -> None:
    """Start the download API with the configured host/port."""
    import uvicorn

    from app.api.app import create_app

    settings = get_settings()
    print(
        f"▶ Starting download API on http://{settings.host}:{settings.port} "
        f"(BASE_URL: {settings.base_url})"
    )
    uvicorn.run(
        create_app(),
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


def run_bot() -> None:
    settings = get_settings()
    print(f"▶ Starting {settings.bot_username} in {settings.bot_mode.upper()} mode")
    if not settings.is_mock:
        print(
            f"⚠️  BOT_MODE={settings.bot_mode!r} is not recognised — "
            "starting the mock transport. Use BOT_MODE=real (with a "
            "BOT_TOKEN) for the real Telegram bot."
        )
    mock.repl()


def main() -> None:
    ensure_utf8_stdout()
    args = _parse_args()
    if args.api:
        run_api()
        return
    if args.telegram:
        from app.bot.telegram import run_telegram

        run_telegram()
        return

    settings = get_settings()
    if settings.bot_mode == "real":
        # Explicitly configured for the real bot (BOT_MODE=real): start it.
        from app.bot.telegram import run_telegram

        run_telegram()
        return

    # Safe default: mock transport, no token/network required.
    run_bot()


if __name__ == "__main__":
    main()