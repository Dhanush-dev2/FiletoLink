"""Framework-agnostic bot logic.

Every handler receives a normalized :class:`~app.bot.models.Update` and
returns the reply text to send back. Neither the mock transport nor the
future real Telegram integration needs to know how the other works.
"""
from datetime import datetime, timezone

from ..services.files import FileService
from .models import Message, Update

BOT_NAME = "File-to-Link Bot"
UNNAMED_FILE = "unnamed"

START_TEXT = (
    "👋 Hi{name}! I'm the {bot}, a zero-cost bot that turns files into "
    "public links anyone can use to download them.\n\n"
    "Commands:\n"
    "/start — show this message\n"
    "/help — show help\n\n"
    "📤 Send me a file and I'll show you its metadata — public download "
    "links arrive in a later phase."
)

HELP_TEXT = (
    "📚 Help — {bot}\n\n"
    "Available now:\n"
    "/start — welcome message\n"
    "/help — this help text\n"
    "📤 Send me a file — I'll acknowledge it and show its metadata\n\n"
    "Coming in later phases:\n"
    "🔗 Public download links, link expiration, and counters\n"
    "🌐 A web download endpoint with range-request support\n\n"
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


def _handle_document(message: Message, file_service: FileService | None = None) -> str:
    """Acknowledge a received file and display its metadata.

    When a ``file_service`` is provided the metadata is persisted to SQLite
    and the reply notes the record id. Public link generation is
    intentionally NOT implemented yet.
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
    return "\n".join(lines)


def handle_update(update: Update, file_service: FileService | None = None) -> str:
    """Route one update through the bot and return the reply text.

    ``file_service`` is optional: when provided, received files are
    persisted to SQLite.
    """
    message: Message | None = update.message
    if message is None:
        return (
            "🤔 I need a message to work with. "
            "Send /help to see what I can do."
        )
    if message.document is not None:
        return _handle_document(message, file_service)
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
    if text.startswith("/"):
        return (
            f"❓ Unknown command: {text}\n\n"
            "Use /help to see what I can do."
        )
    return (
        "📩 I got your message, but I only turn *files* into links.\n\n"
        "Send me a file and I'll acknowledge it and show its metadata. "
        "Public download links arrive in a later phase."
    )