"""Real Telegram transport: python-telegram-bot → Local Bot API Server.

This is the only module that touches python-telegram-bot. It:

- connects to a self-hosted Local Bot API Server (``--local``) via
  ``base_url`` + ``local_mode=True`` and long polling,
- normalizes PTB updates into our framework-agnostic models
  (:mod:`app.bot.models`) so the existing handlers/services/API are reused
  unchanged,
- resolves the absolute local file path for documents via ``get_file``
  (the Local Bot API Server already stored the bytes locally — nothing is
  re-downloaded through the remote Telegram URL).

Ownership (Phase 14B): the file service this transport builds carries a
:class:`~app.services.storage.FileStorage`, so the received file is transferred
into FileToLink-owned storage and *that* path is recorded — the Local Bot API
Server may clean up its own work directory at any time, so no link may depend
on it. Order: ``get_file`` → transfer/verify → record + public id → reply. If
the transfer fails, no record, no public id and no download URL are created and
the user gets an error reply instead.

Run with: ``python run.py --telegram`` (or ``BOT_MODE=real`` + config).
"""
import asyncio
import logging
import os
import sqlite3
import sys
from dataclasses import replace

import telegram
from telegram import Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from ..config import Settings, get_settings
from ..services.files import FileService, PublicIdError, make_file_service
from ..services.storage import FileStorage, StorageError
from ..utils.logging_setup import configure_logging
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


async def _resolve_local_path(
    bot: Bot,
    file_id: str,
    *,
    read_timeout: float | None = None,
) -> str:
    """Return the absolute local path of a received file.

    With a Local Bot API Server running in ``--local`` mode, ``get_file``
    returns the absolute path of the already-stored file — no download.

    ``getFile`` gets its own long read timeout (Phase 14A): storing a
    multi-hundred-MB file can take well past PTB's small default, and the
    request must not time out while the local server finishes. The timeout
    defaults to :attr:`Settings.telegram_get_file_read_timeout_seconds` and
    applies only to this call — polling/getUpdates timeouts are untouched.
    """
    if read_timeout is None:
        read_timeout = get_settings().telegram_get_file_read_timeout_seconds
    tg_file = await bot.get_file(file_id, read_timeout=read_timeout)
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

    try:
        reply = handle_update(normalized, file_service)
    except StorageError:
        # The file could not be taken into FileToLink-owned storage: no record,
        # no public id, no download URL — tell the user and stop. Details stay
        # in the log (they may contain filesystem paths).
        logger.exception("Could not take ownership of the received file")
        reply = (
            "❌ I couldn't copy that file into my own storage, so I did not "
            "create a link. Check the free space where FileToLink stores "
            "files (FILE_STORAGE_DIR) and send it again."
        )
    except (PublicIdError, sqlite3.Error):
        # The link could not be recorded (a public id could not be reserved or
        # the INSERT failed). Anything already adopted was rolled back by the
        # service, so this is a clean failure with no orphan file.
        logger.exception("Could not record the received file")
        reply = (
            "❌ I couldn't save that file's link, so no download URL was "
            "created. Please try again."
        )
    await update.effective_message.reply_text(reply)


async def handle_command_update(
    update: telegram.Update,
    context: telegram.ext.ContextTypes.DEFAULT_TYPE,
    file_service: FileService | None = None,
) -> None:
    """Route bot commands through the existing normalized handler.

    /start and /help need no service; /delete and /info receive the
    file service so ownership checks can run against the database.
    """
    reply = handle_update(normalize_update(update), file_service=file_service)
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
        # Real ingestion takes ownership of the bytes (Phase 14B).
        file_service = make_file_service(storage=FileStorage())

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
        CommandHandler(
            "delete",
            lambda update, context: handle_command_update(
                update, context, file_service
            ),
        )
    )
    application.add_handler(
        CommandHandler(
            "info",
            lambda update, context: handle_command_update(
                update, context, file_service
            ),
        )
    )
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


# Errors that mean the server answered but rejected our credentials — a very
# different operator action from "the server is not running" (Phase 19: an
# expired/incorrect BOT_TOKEN used to be reported as "cannot reach the server",
# pointing the operator at the wrong window).
_CREDENTIAL_ERRORS = frozenset({"InvalidToken", "Unauthorized", "Forbidden"})


def _ping_local_server(url: str, token: str) -> tuple[bool, str | None]:
    """Check the Local Bot API Server is reachable *and* accepts our token.

    Returns ``(ok, failure)`` where ``failure`` is the exception *class name*
    (never the message, which can embed the token, and never the token).
    """

    async def _ping() -> None:
        async with Bot(token, base_url=f"{url}/bot", local_mode=True) as bot:
            await bot.get_me()

    try:
        asyncio.run(_ping())
        return True, None
    except Exception as exc:  # noqa: BLE001 - any failure = not usable
        failure = exc.__class__.__name__
        logger.info("Local Bot API Server ping failed: %s", failure)
        return False, failure


def run_telegram(settings: Settings | None = None) -> None:
    """Start the real bot (validates config, then long-polls)."""
    settings = settings or get_settings()

    errors = validate_real_config(settings)
    if errors:
        for error in errors:
            print(f"❌ {error}")
        print("\nMock mode still works: python run.py")
        sys.exit(1)

    reachable, failure = _ping_local_server(
        settings.local_bot_api_url, settings.bot_token
    )
    if not reachable:
        if failure in _CREDENTIAL_ERRORS:
            print(
                f"❌ The Local Bot API Server at {settings.local_bot_api_url} "
                f"rejected BOT_TOKEN ({failure}).\n"
                "   Check that BOT_TOKEN in .env is the current token for this "
                "bot — @BotFather can revoke and re-issue it. (This is a "
                "credential problem, not a connectivity one.)"
            )
        else:
            print(
                f"❌ Cannot reach the Local Bot API Server at "
                f"{settings.local_bot_api_url} ({failure}).\n"
                "   Start it with --local first (see README → Local Bot API "
                "Server setup), then retry."
            )
        sys.exit(1)

    # Secret-free console logging: request URLs are suppressed and tokens are
    # redacted (the Local Bot API URL embeds BOT_TOKEN).
    configure_logging()
    logger.info(
        "Starting real Telegram bot (long polling) against %s",
        settings.local_bot_api_url,
    )
    application = build_application(
        file_service=make_file_service(storage=FileStorage())
    )
    application.run_polling(allowed_updates=telegram.Update.MESSAGE)


if __name__ == "__main__":
    run_telegram()