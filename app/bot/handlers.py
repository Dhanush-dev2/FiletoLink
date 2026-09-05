"""Framework-agnostic bot logic.

Every handler receives a normalized :class:`~app.bot.models.Update` and
returns the reply text to send back. Neither the mock transport nor the
future real Telegram integration needs to know how the other works.
"""
import time
from datetime import datetime, timezone

from ..config import get_settings
from ..database.records import FileRecord
from ..services.files import FileService
from ..utils.public_ids import is_valid_public_id
from ..utils.urls import generate_file_url
from .models import Message, Update

BOT_NAME = "File-to-Link Bot"
UNNAMED_FILE = "unnamed"

START_TEXT = (
    "👋 Hi{name}! I'm the {bot}, a zero-cost bot that turns files into "
    "public links anyone can use to download them.\n\n"
    "Commands:\n"
    "/start — show this message\n"
    "/help — show help\n"
    "/delete <public_id> — remove one of your links\n"
    "/info <public_id> — show details about one of your links\n\n"
    "📤 Send me a file and I'll give you a public download link. "
    "Links can expire or be limited to a number of downloads, and you "
    "can delete them any time."
)

HELP_TEXT = (
    "📚 Help — {bot}\n\n"
    "Commands:\n"
    "/start — welcome message\n"
    "/help — this help text\n"
    "/delete <public_id> — permanently disable one of your links\n"
    "/info <public_id> — show status, download count, and expiration "
    "for one of your links\n\n"
    "📤 Send me a file — I'll store it and give you a public download "
    "link.\n\n"
    "Links are private to you: only you can delete or inspect them. "
    "Expired, deleted, or fully-downloaded links return HTTP 410.\n\n"
    "I currently run on a local mock Telegram transport, so no bot token "
    "is required."
)


def format_size(num_bytes: int | None) -> str:
    """Human-readable size, e.g. ``512 B``, ``4.2 MB``."""
    if num_bytes is None:
        return "unknown"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # unreachable; satisfies type checkers


def format_timestamp(epoch: int) -> str:
    """Format a Unix timestamp as UTC, e.g. ``2023-11-14 22:13:20 UTC``."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _greeting_name(update: Update) -> str:
    user = update.message.user if update.message else None
    if user and user.first_name:
        return f" {user.first_name}"
    return ""


def _command_argument(text: str) -> str | None:
    """Return the argument after a bot command, or None when absent."""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else None


def _find_owned_link(
    public_id: str,
    user_id: int | None,
    file_service: FileService | None,
) -> tuple[FileRecord | None, str | None]:
    """Look up a link and verify ownership for a management command.

    Returns ``(record, None)`` when the caller may proceed, otherwise
    ``(None, error_reply)``. Ownership is checked server-side against the
    stored ``user_id`` — never against anything the user claims — and a
    rejection reveals no private metadata.
    """
    if file_service is None:
        return None, (
            "⚠️ Link management needs a database connection, which isn't "
            "available right now."
        )
    record = file_service.get_link(public_id)
    if record is None:
        return None, "I couldn't find a link with that public id."
    if user_id is None or record.user_id != user_id:
        return None, "You can only manage links you created."
    return record, None


def _handle_delete(message: Message, file_service: FileService | None) -> str:
    """/delete <public_id>: owner-only, idempotent link removal."""
    argument = _command_argument(message.text or "")
    if not argument:
        return (
            "🗑️ Usage: /delete <public_id> — permanently disable one of "
            "your download links."
        )
    if not is_valid_public_id(argument):
        return (
            "❌ That doesn't look like a valid public id — it's the "
            "8-character code at the end of your download link."
        )
    user_id = message.user.id if message.user is not None else None
    record, error = _find_owned_link(argument, user_id, file_service)
    if error is not None:
        return error
    if record.status == "deleted":
        return "ℹ️ That link is already deleted."
    file_service.delete_link(argument)
    return f"🗑️ Link deleted: {argument}. It can no longer be downloaded."


def _format_link_info(record: FileRecord, base_url: str) -> str:
    """Public-facing info for the link owner. Never exposes internal
    identifiers (telegram_file_id, source_path, database id) or secrets."""
    if record.expires_at is None:
        expires = "never"
    else:
        expires = format_timestamp(record.expires_at)
        if record.expires_at <= int(time.time()):
            expires += " (past)"
    max_downloads = (
        "unlimited" if record.max_downloads is None else str(record.max_downloads)
    )
    lines = [
        "ℹ️ Link info",
        "",
        f"📁 File: {record.file_name or UNNAMED_FILE}",
        f"📦 Size: {format_size(record.file_size)}",
        f"📊 Status: {record.status}",
        f"⬇️ Downloads: {record.download_count} / {max_downloads}",
        f"⏳ Expires: {expires}",
    ]
    if record.public_id is not None:
        url = generate_file_url(base_url, record.public_id)
        lines.append("")
        lines.append("🔗 Link:")
        lines.append(url)
    return "\n".join(lines)


def _handle_info(
    message: Message,
    file_service: FileService | None,
    base_url: str | None,
) -> str:
    """/info <public_id>: owner-only link details."""
    argument = _command_argument(message.text or "")
    if not argument:
        return (
            "ℹ️ Usage: /info <public_id> — show details about one of your "
            "links (status, downloads, expiration)."
        )
    if not is_valid_public_id(argument):
        return (
            "❌ That doesn't look like a valid public id — it's the "
            "8-character code at the end of your download link."
        )
    user_id = message.user.id if message.user is not None else None
    record, error = _find_owned_link(argument, user_id, file_service)
    if error is not None:
        return error
    return _format_link_info(record, base_url or get_settings().base_url)


def _handle_document(
    message: Message,
    file_service: FileService | None = None,
    base_url: str | None = None,
) -> str:
    """Acknowledge a received file and display its metadata + download URL.

    When a ``file_service`` is provided the metadata is persisted to SQLite
    and the reply notes the record id, public id, and generated download
    URL. The URL is generated but does not serve files yet.
    """
    doc = message.document
    lines = [
        "✅ File received",
        "",
        f"📁 {doc.file_name or UNNAMED_FILE}",
        f"📦 {format_size(doc.file_size)}",
    ]
    if doc.mime_type:
        lines.append(f"🏷️ Type: {doc.mime_type}")
    lines.append("")
    lines.append(f"🆔 File ID: {doc.file_id}")
    lines.append(f"#️⃣ Message ID: {message.message_id}")
    if message.user is not None:
        lines.append(f"👤 User ID: {message.user.id}")
    if message.date is not None:
        lines.append(f"🕒 Received: {format_timestamp(message.date)}")
    if file_service is not None:
        record = file_service.save_document(message)
        lines.append(f"💾 Stored in database (record #{record.id})")
        if record.public_id is not None:
            lines.append(f"🔑 Public ID: {record.public_id}")
            url = generate_file_url(base_url or get_settings().base_url, record.public_id)
            lines.append("")
            lines.append("🔗 Download:")
            lines.append(url)
    return "\n".join(lines)


def handle_update(
    update: Update,
    file_service: FileService | None = None,
    base_url: str | None = None,
) -> str:
    """Route one update through the bot and return the reply text.

    ``file_service`` is optional: when provided, received files are
    persisted to SQLite. ``base_url`` overrides the configured BASE_URL
    used for the generated download link.
    """
    message: Message | None = update.message
    if message is None:
        return (
            "🤔 I need a message to work with. "
            "Send /help to see what I can do."
        )
    if message.document is not None:
        return _handle_document(message, file_service, base_url)
    if message.text is None:
        return (
            "🤔 I can only read text messages and files for now. "
            "Send /help to see what I can do."
        )

    text = message.text.strip()
    first_word = text.split(maxsplit=1)[0].lower() if text else ""

    if first_word == "/start":
        return START_TEXT.format(
            name=_greeting_name(update), bot=BOT_NAME
        )
    if first_word == "/help":
        return HELP_TEXT.format(bot=BOT_NAME)
    if first_word == "/delete":
        return _handle_delete(message, file_service)
    if first_word == "/info":
        return _handle_info(message, file_service, base_url)
    if text.startswith("/"):
        return (
            f"❓ Unknown command: {text}\n\n"
            "Use /help to see what I can do."
        )
    return (
        "📩 I got your message, but I only turn *files* into links.\n\n"
        "Send me a file and I'll give you a public download link."
    )