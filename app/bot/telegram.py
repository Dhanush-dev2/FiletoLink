"""Real Telegram transport: python-telegram-bot → Local Bot API Server.

This is the only module that touches python-telegram-bot. It:

- connects to a self-hosted Local Bot API Server (``--local``) via
  ``base_url`` + ``local_mode=True`` and long polling,
- normalizes PTB updates into our framework-agnostic models
  (:mod:`app.bot.models`) so the existing handlers/services/API are reused
  unchanged,
- resolves the absolute local file path for documents via ``get_file`` and
  passes it to :class:`~app.services.files.FileService` as ``source_path``
  (the Local Bot API Server already stored the bytes locally — nothing is
  re-downloaded through the remote Telegram URL).

Run with: ``python run.py --telegram`` (or ``BOT_MODE=real`` + config).
"""
import asyncio
import logging
import os
import sys
from dataclasses import replace

import telegram
from telegram import Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from ..config import Settings, get_settings
from ..services.files import FileService, make_file_service
from .handlers import handle_update
from .models import (
    Document as NormalizedDocument,
    Message as NormalizedMessage,
    Update as NormalizedUpdate,
    User as NormalizedUser,
)

logger = logging.getLogger(__name__)


# ── update normalization ──────────────────────────────────────────────────


def normalize_update(update: telegram.Update) -> NormalizedUpdate:
    """Translate a PTB update into our framework-agnostic model."""
    ptb_message = update.message
    if ptb_message is None:
        return NormalizedUpdate(update_id=update.update_id, message=None)

    user = None
    if ptb_message.from_user is not None:
        user = NormalizedUser(
            id=ptb_message.from_user.id,
            username=ptb_message.from_user.username,
            first_name=ptb_message.from_user.first_name,
        )

    document = None
    if ptb_message.document is not None:
        doc = ptb_message.document
        document = NormalizedDocument(
            file_id=doc.file_id,
            file_name=doc.file_name,
            file_size=doc.file_size,
            mime_type=doc.mime_type,
        )

    date = None
    if ptb_message.date is not None:
        date = int(ptb_message.date.timestamp())

    return NormalizedUpdate(
        update_id=update.update_id,
        message=NormalizedMessage(
            message_id=ptb_message.message_id,
            # Files take priority over captions inside handle_update; the
            # caption is exposed as text for text-only messages.
            text=ptb_message.text or ptb_message.caption,
            user=user,
            document=document,
            date=date,
        ),
    )


async def _resolve_local_path(bot: Bot, file_id: str) -> str:
    """Return the absolute local path of a received file.

    With a Local Bot API Server running in ``--local`` mode, ``get_file``
    returns the absolute path of the already-stored file — no download.
    """
    tg_file = await bot.get_file(file_id)
    path = tg_file.file_path
    if not path or not os.path.isabs(path):
        raise ValueError(
            "Local Bot API Server returned a non-local file path "
            "(start it with the --local flag)"
        )
    return path


# ── handlers (reuse the framework-agnostic logic) ─────────────────────────


async def handle_message_update(
    update: telegram.Update,
    context: telegram.ext.ContextTypes.DEFAULT_TYPE,
    file_service: FileService,
) -> None:
    """Route a message update (files or text) through the existing pipeline."""
    normalized = normalize_update(update)
    message = normalized.message
    if message is not None and message.document is not None:
        try:
            local_path = await _resolve_local_path(
                context.bot, message.document.file_id
            )
        except (telegram.error.TelegramError, ValueError) as exc:
            logger.error("Could not resolve local file path: %s", exc)
            await update.effective_message.reply_text(
                "❌ I couldn't access that file's local path. "
                "Make sure the Local Bot API Server is running with --local."
            )
            return
        normalized = replace(
            normalized, message=replace(message, source_path=local_path)
        )

    reply = handle_update(normalized, file_service)
    await update.effective_message.reply_text(reply)


async def handle_command_update(
    update: telegram.Update,
    context: telegram.ext.ContextTypes.DEFAULT_TYPE,
) -> None:
    """Route /start and /help through the existing normalized handler."""
    reply = handle_update(normalize_update(update))
    await update.effective_message.reply_text(reply)


# ── application wiring ─────────────────────────────────────────────────────


def build_application(
    file_service: FileService | None = None,
    *,
    token: str | None = None,
    base_url: str | None = None,
    local_mode: bool = True,
) -> Application:
    """Build the PTB Application wired to the Local Bot API Server.

    ``token``/``base_url`` default to the configured BOT_TOKEN and
    ``{LOCAL_BOT_API_URL}/bot``. No network access happens here.
    """
    settings = get_settings()
    token = token or settings.bot_token
    if not token:
        raise ValueError("BOT_TOKEN is required to build the real transport")
    base_url = base_url or f"{settings.local_bot_api_url}/bot"
    if file_service is None:
        file_service = make_file_service()

    application = (
        Application.builder()
        .token(token)
        .base_url(base_url)
        .local_mode(local_mode)
        .build()
    )

    application.add_handler(CommandHandler("start", handle_command_update))
    application.add_handler(CommandHandler("help", handle_command_update))
    application.add_handler(
        MessageHandler(
            filters.TEXT | filters.Document.ALL,
            lambda update, context: handle_message_update(
                update, context, file_service
            ),
        )
    )
    return application


# ── startup / config validation ───────────────────────────────────────────


def validate_real_config(settings: Settings | None = None) -> list[str]:
    """Return a list of human-readable configuration errors (empty = OK)."""
    settings = settings or get_settings()
    errors: list[str] = []
    if not settings.bot_token:
        errors.append(
            "BOT_TOKEN is not set — create a bot with @BotFather and add the "
            "token to .env"
        )
    if not settings.telegram_api_id:
        errors.append(
            "TELEGRAM_API_ID is not set — get it (free) from "
            "https://my.telegram.org"
        )
    if not settings.telegram_api_hash:
        errors.append(
            "TELEGRAM_API_HASH is not set — get it (free) from "
            "https://my.telegram.org"
        )
    return errors


def _ping_local_server(url: str, token: str) -> bool:
    """Check the Local Bot API Server is reachable (getMe). No secrets logged."""

    async def _ping() -> None:
        async with Bot(token, base_url=f"{url}/bot", local_mode=True) as bot:
            await bot.get_me()

    try:
        asyncio.run(_ping())
        return True
    except Exception as exc:  # noqa: BLE001 - any failure = unreachable
        logger.info("Local Bot API Server ping failed: %s", exc.__class__.__name__)
        return False


def run_telegram(settings: Settings | None = None) -> None:
    """Start the real bot (validates config, then long-polls)."""
    settings = settings or get_settings()

    errors = validate_real_config(settings)
    if errors:
        for error in errors:
            print(f"❌ {error}")
        print("\nMock mode still works: python run.py")
        sys.exit(1)

    if not _ping_local_server(settings.local_bot_api_url, settings.bot_token):
        print(
            f"❌ Cannot reach the Local Bot API Server at "
            f"{settings.local_bot_api_url}.\n"
            "   Start it with --local first (see README → Local Bot API "
            "Server setup), then retry."
        )
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info(
        "Starting real Telegram bot (long polling) against %s",
        settings.local_bot_api_url,
    )
    application = build_application(file_service=make_file_service())
    application.run_polling(allowed_updates=telegram.Update.MESSAGE)


if __name__ == "__main__":
    run_telegram()