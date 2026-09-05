"""Framework-agnostic bot logic.

Every handler receives a normalized :class:`~app.bot.models.Update` and
returns the reply text to send back. Neither the mock transport nor the
future real Telegram integration needs to know how the other works.
"""
from .models import Message, Update

BOT_NAME = "File-to-Link Bot"

START_TEXT = (
    "👋 Hi{name}! I'm the {bot}, a zero-cost bot that turns files into "
    "public links anyone can use to download them.\n\n"
    "Commands:\n"
    "/start — show this message\n"
    "/help — show help\n\n"
    "⚠️ File sharing is not enabled yet — it arrives in the next phase. "
    "Send /help to see what I can do today."
)

HELP_TEXT = (
    "📚 Help — {bot}\n\n"
    "Available commands:\n"
    "/start — welcome message\n"
    "/help — this help text\n\n"
    "Coming in later phases:\n"
    "📤 Upload a file and receive a public download link\n"
    "🔗 Link expiration, download counters, and deletion\n"
    "🌐 A web download endpoint with range-request support\n\n"
    "I currently run on a local mock Telegram transport, so no bot token "
    "is required."
)


def _greeting_name(update: Update) -> str:
    user = update.message.user if update.message else None
    if user and user.first_name:
        return f" {user.first_name}"
    return ""


def handle_update(update: Update) -> str:
    """Route one update through the bot and return the reply text."""
    message: Message | None = update.message
    if message is None or message.text is None:
        return (
            "🤔 I can only read text messages for now. "
            "Use /help to see what I can do."
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
        "📩 I got your message, but I only turn *files* into links — and "
        "file support lands in the next phase. Send /help to see what I "
        "can do today."
    )