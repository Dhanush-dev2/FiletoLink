"""Tests for the framework-agnostic bot handlers."""
from app.bot.handlers import handle_update
from app.bot.models import Message, Update, User

USER = User(id=42, username="alice", first_name="Alice")


def make_update(text: str) -> Update:
    return Update(
        update_id=1,
        message=Message(message_id=1, text=text, user=USER),
    )


def test_start_reply_welcomes_and_lists_commands():
    reply = handle_update(make_update("/start"))
    assert "File-to-Link Bot" in reply
    assert "/start" in reply
    assert "/help" in reply
    assert "Alice" in reply  # greets by first name


def test_start_without_first_name_is_still_valid():
    update = Update(
        update_id=1,
        message=Message(message_id=1, text="/start", user=User(id=7)),
    )
    reply = handle_update(update)
    assert "File-to-Link Bot" in reply
    assert "Hi!" in reply


def test_help_reply_lists_commands():
    reply = handle_update(make_update("/help"))
    assert "/start" in reply
    assert "/help" in reply


def test_unknown_command_reports_error():
    reply = handle_update(make_update("/bogus"))
    assert "Unknown command" in reply
    assert "/bogus" in reply


def test_plain_message_explains_file_support_is_coming():
    reply = handle_update(make_update("hello there"))
    assert "file" in reply.lower()


def test_message_without_text_gets_guidance():
    update = Update(update_id=1, message=Message(message_id=1))
    reply = handle_update(update)
    assert "/help" in reply


def test_update_without_message_gets_guidance():
    reply = handle_update(Update(update_id=1, message=None))
    assert reply  # non-empty, no crash