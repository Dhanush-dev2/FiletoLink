"""Tests for the real Telegram transport — all offline (no network/token/server).

PTB update objects are constructed locally; the Local Bot API Server is
simulated with a fake bot that returns a local file path.
"""
import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import telegram

from app.bot.models import Document, Message, Update, User
from app.bot.telegram import (
    build_application,
    handle_command_update,
    handle_message_update,
    normalize_update,
    validate_real_config,
)
from app.config import Settings
from app.database.connection import initialize
from app.database.files import FileRepository
from app.database.records import FileRecord
from app.services.file_sources import FileSourceError, TelegramFileSource
from app.services.files import FileService, make_file_service

USER = User(id=42, username="alice", first_name="Alice")


# ── helpers to build PTB objects locally ──────────────────────────────────


def make_ptb_document(
    file_id: str = "FILE1",
    file_name: str | None = "a.pdf",
    file_size: int | None = 1000,
    mime_type: str | None = "application/pdf",
) -> telegram.Document:
    return telegram.Document(
        file_id=file_id,
        file_unique_id="u" + file_id,
        file_name=file_name,
        file_size=file_size,
        mime_type=mime_type,
    )


def make_ptb_message(
    *,
    message_id: int = 7,
    text: str | None = None,
    caption: str | None = None,
    document: telegram.Document | None = None,
    user: telegram.User | None = None,
) -> telegram.Message:
    return telegram.Message(
        message_id=message_id,
        date=datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc),
        chat=telegram.Chat(id=1, type="private"),
        from_user=user
        or telegram.User(id=42, first_name="Alice", username="alice", is_bot=False),
        text=text,
        caption=caption,
        document=document,
    )


def make_ptb_update(message: telegram.Message | None = None) -> telegram.Update:
    return telegram.Update(update_id=99, message=message)


class FakeBot:
    """Minimal bot stub: get_file returns a local path, replies get captured."""

    def __init__(self, local_path: str | None = None, error: Exception | None = None):
        self.local_path = local_path
        self.error = error
        self.sent: list[str] = []

    async def get_file(self, file_id: str) -> SimpleNamespace:
        if self.error is not None:
            raise self.error
        return SimpleNamespace(file_path=self.local_path)

    async def send_message(self, chat_id, text, **kwargs) -> SimpleNamespace:
        self.sent.append(text)
        return SimpleNamespace(message_id=1)


def make_context(bot: FakeBot) -> SimpleNamespace:
    return SimpleNamespace(bot=bot)


def bind(update: telegram.Update, bot: FakeBot) -> telegram.Update:
    """Attach a bot to the update's message so reply_text works offline."""
    update.effective_message.set_bot(bot)
    return update


def run_async(coro):
    return asyncio.run(coro)


def make_record(source_path: str | None, work_dir: str) -> FileRecord:
    return FileRecord(
        id=1,
        public_id=None,
        telegram_file_id="F1",
        telegram_message_id=7,
        user_id=42,
        file_name="a.pdf",
        file_size=4,
        mime_type="application/pdf",
        created_at=1_700_000_000,
        expires_at=None,
        download_count=0,
        max_downloads=None,
        status="active",
        source_path=source_path,
    )


# ── normalization ──────────────────────────────────────────────────────────


def test_normalize_document_message():
    normalized = normalize_update(
        make_ptb_update(make_ptb_message(document=make_ptb_document()))
    )
    assert normalized.update_id == 99
    msg = normalized.message
    assert msg.message_id == 7
    assert msg.user == USER
    assert msg.document.file_id == "FILE1"
    assert msg.document.file_name == "a.pdf"
    assert msg.document.file_size == 1000
    assert msg.document.mime_type == "application/pdf"
    assert msg.date == 1_700_000_000  # 2023-11-14T22:13:20Z


def test_normalize_missing_mime_type():
    msg = make_ptb_message(document=make_ptb_document(mime_type=None))
    assert normalize_update(make_ptb_update(msg)).message.document.mime_type is None


def test_normalize_missing_filename():
    msg = make_ptb_message(document=make_ptb_document(file_name=None))
    assert normalize_update(make_ptb_update(msg)).message.document.file_name is None


def test_normalize_caption_becomes_text():
    msg = make_ptb_message(
        document=make_ptb_document(), caption="look at this"
    )
    normalized = normalize_update(make_ptb_update(msg))
    assert normalized.message.text == "look at this"
    assert normalized.message.document is not None  # file still takes priority


def test_normalize_text_message():
    msg = make_ptb_message(text="/start")
    normalized = normalize_update(make_ptb_update(msg))
    assert normalized.message.text == "/start"
    assert normalized.message.document is None


def test_normalize_update_without_message():
    normalized = normalize_update(make_ptb_update(message=None))
    assert normalized.message is None


# ── TelegramFileSource security ────────────────────────────────────────────


def test_source_serves_file_inside_work_dir(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    target = work_dir / "file.txt"
    target.write_bytes(b"hello telegram\n")

    source = TelegramFileSource(work_dir)
    with source.open(make_record(str(target), str(work_dir))) as fh:
        assert fh.read() == b"hello telegram\n"


def test_source_rejects_path_outside_work_dir(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"secret")

    source = TelegramFileSource(work_dir)
    with pytest.raises(FileSourceError):
        source.open(make_record(str(outside), str(work_dir)))


def test_source_rejects_traversal(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    escape = str((work_dir / ".." / "secret.txt").resolve())
    with open(escape, "w") as fh:
        fh.write("secret")

    source = TelegramFileSource(work_dir)
    with pytest.raises(FileSourceError):
        source.open(make_record(escape, str(work_dir)))


def test_source_rejects_missing_file(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    source = TelegramFileSource(work_dir)
    with pytest.raises(FileSourceError):
        source.open(make_record(str(work_dir / "nope.txt"), str(work_dir)))


def test_source_rejects_record_without_path(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    source = TelegramFileSource(work_dir)
    with pytest.raises(FileSourceError):
        source.open(make_record(None, str(work_dir)))


def test_source_rejects_symlink_escaping_work_dir(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"secret")
    link = work_dir / "innocent.txt"
    try:
        os.symlink(outside, link)
    except OSError:  # Windows without privileges — skip
        pytest.skip("symlinks not available in this environment")

    source = TelegramFileSource(work_dir)
    with pytest.raises(FileSourceError):
        source.open(make_record(str(link), str(work_dir)))


def test_source_allows_symlink_inside_work_dir(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    real = work_dir / "real.txt"
    real.write_bytes(b"ok")
    link = work_dir / "alias.txt"
    try:
        os.symlink(real, link)
    except OSError:
        pytest.skip("symlinks not available in this environment")

    source = TelegramFileSource(work_dir)
    with source.open(make_record(str(link), str(work_dir))) as fh:
        assert fh.read() == b"ok"


# ── config ─────────────────────────────────────────────────────────────────


def test_settings_telegram_defaults():
    s = Settings()
    assert s.local_bot_api_url == "http://127.0.0.1:8081"
    assert s.telegram_bot_work_dir.endswith("telegram_bot_data")


def test_settings_loads_telegram_vars(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "deadbeef")
    monkeypatch.setenv("LOCAL_BOT_API_URL", "http://127.0.0.1:9999/")
    monkeypatch.setenv("TELEGRAM_BOT_WORK_DIR", "custom_dir")
    s = Settings()
    assert s.bot_token == "123:abc"
    assert s.telegram_api_id == "12345"
    assert s.telegram_api_hash == "deadbeef"
    assert s.local_bot_api_url == "http://127.0.0.1:9999"  # trailing slash stripped
    assert s.telegram_bot_work_dir.endswith("custom_dir")


def test_validate_real_config_reports_missing(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    errors = validate_real_config(Settings())
    assert len(errors) == 3
    assert any("BOT_TOKEN" in e for e in errors)
    assert any("TELEGRAM_API_ID" in e for e in errors)
    assert any("TELEGRAM_API_HASH" in e for e in errors)


def test_validate_real_config_ok(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "deadbeef")
    assert validate_real_config(Settings()) == []


# ── application wiring ─────────────────────────────────────────────────────


def test_build_application_requires_token():
    # get_settings() is cached, so pass no token and rely on the env being
    # unset on this machine.
    with pytest.raises(ValueError):
        build_application(file_service=make_file_service(":memory:"))


def test_build_application_registers_handlers():
    app = build_application(
        token="123:abc",
        file_service=make_file_service(":memory:"),
        base_url="http://127.0.0.1:8081/bot",
    )
    handlers = [h for group in app.handlers.values() for h in group]
    # /start, /help, /delete, /info + the message handler.
    assert len(handlers) == 5
    from telegram.ext import CommandHandler, MessageHandler

    assert sum(isinstance(h, CommandHandler) for h in handlers) == 4
    assert sum(isinstance(h, MessageHandler) for h in handlers) == 1


# ── real flow (simulated Local Bot API Server) ─────────────────────────────


def test_document_flow_stores_record_and_serves_bytes(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    source_file = work_dir / "report.pdf"
    source_file.write_bytes(b"%PDF-1.4 fake")

    repo = FileRepository(initialize(":memory:"))
    service = FileService(repo)
    bot = FakeBot(local_path=str(source_file))
    update = make_ptb_update(make_ptb_message(document=make_ptb_document()))

    run_async(handle_message_update(bind(update, bot), make_context(bot), service))

    assert bot.sent, "bot should have replied"
    reply = bot.sent[0]
    assert "File received" in reply
    assert "http://localhost:8000/f/" in reply

    record = repo.get_by_id(1)
    assert record is not None
    assert record.source_path == str(source_file)
    assert record.public_id is not None

    # The stored record is served by TelegramFileSource from the work dir.
    source = TelegramFileSource(work_dir)
    with source.open(record) as fh:
        assert fh.read() == b"%PDF-1.4 fake"


def test_document_flow_without_local_path_replies_error(tmp_path):
    repo = FileRepository(initialize(":memory:"))
    service = FileService(repo)
    bot = FakeBot(local_path="remote/path/to/file")  # not an absolute local path
    update = make_ptb_update(make_ptb_message(document=make_ptb_document()))

    run_async(handle_message_update(bind(update, bot), make_context(bot), service))

    assert bot.sent
    assert "❌" in bot.sent[0]
    assert repo.get_by_id(1) is None  # nothing stored


def test_document_flow_handles_telegram_error(tmp_path):
    repo = FileRepository(initialize(":memory:"))
    service = FileService(repo)
    bot = FakeBot(error=telegram.error.NetworkError("boom"))
    update = make_ptb_update(make_ptb_message(document=make_ptb_document()))

    run_async(handle_message_update(bind(update, bot), make_context(bot), service))

    assert bot.sent
    assert "❌" in bot.sent[0]
    assert repo.get_by_id(1) is None


def test_command_flow_routes_through_normalized_handlers():
    bot = FakeBot()
    update = make_ptb_update(make_ptb_message(text="/start"))
    update.effective_message.set_bot(bot)

    run_async(handle_command_update(update, make_context(bot)))

    assert bot.sent
    assert "File-to-Link Bot" in bot.sent[0]


def test_text_message_flow_replies_guidance():
    repo = FileRepository(initialize(":memory:"))
    service = FileService(repo)
    bot = FakeBot()
    update = make_ptb_update(make_ptb_message(text="hello there"))

    run_async(handle_message_update(bind(update, bot), make_context(bot), service))

    assert bot.sent
    assert "file" in bot.sent[0].lower()
    assert repo.get_by_id(1) is None