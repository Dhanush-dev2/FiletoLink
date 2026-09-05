"""Tests for public file URL generation and BASE_URL configuration."""
import pytest

from app.bot.handlers import handle_update
from app.bot.models import Document, Message, Update, User
from app.config import Settings
from app.database.connection import initialize
from app.database.files import FileRepository
from app.services.files import FileService
from app.utils.public_ids import is_valid_public_id
from app.utils.urls import generate_file_url

USER = User(id=42, username="alice", first_name="Alice")

BASE = "http://localhost:8000"


def make_document_update() -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=7,
            user=USER,
            document=Document(
                file_id="FILE1",
                file_name="a.pdf",
                file_size=1000,
                mime_type="application/pdf",
            ),
            date=1_700_000_000,
        ),
    )


def make_service() -> FileService:
    return FileService(FileRepository(initialize(":memory:")))


# ── generation ─────────────────────────────────────────────────────────────


def test_url_generated_from_valid_public_id():
    assert (
        generate_file_url(BASE, "t9obSSQq") == "http://localhost:8000/f/t9obSSQq"
    )


def test_url_has_correct_path_prefix():
    url = generate_file_url(BASE, "t9obSSQq")
    assert url.startswith("http://localhost:8000/f/")
    assert url.count("/f/") == 1


def test_url_uses_given_base_url():
    url = generate_file_url("https://files.example.org", "t9obSSQq")
    assert url.startswith("https://files.example.org/f/")


def test_base_url_with_trailing_slash_normalized():
    assert generate_file_url("http://localhost:8000/", "t9obSSQq") == (
        "http://localhost:8000/f/t9obSSQq"
    )


def test_base_url_with_whitespace_normalized():
    url = generate_file_url("  http://localhost:8000/  ", "t9obSSQq")
    assert url == "http://localhost:8000/f/t9obSSQq"


def test_public_id_preserved_exactly():
    for pid in ("t9obSSQq", "A8pQ2zYx", "ABCDefgh", "a-b_c-D1"):
        assert generate_file_url(BASE, pid).endswith(f"/f/{pid}")


def test_invalid_public_id_rejected():
    for bad in ("short", "has spaces", "with/slash", "toolong12345", ""):
        with pytest.raises(ValueError):
            generate_file_url(BASE, bad)


def test_empty_base_url_rejected():
    with pytest.raises(ValueError):
        generate_file_url("", "t9obSSQq")
    with pytest.raises(ValueError):
        generate_file_url("   ", "t9obSSQq")


def test_base_url_without_scheme_rejected():
    with pytest.raises(ValueError):
        generate_file_url("localhost:8000", "t9obSSQq")


def test_is_valid_public_id():
    assert is_valid_public_id("t9obSSQq")
    assert not is_valid_public_id("t9obSSQ")
    assert not is_valid_public_id("t9obSSQ!")
    assert not is_valid_public_id("T9OB SSQq")


# ── stored-file integration ────────────────────────────────────────────────


def test_url_generated_for_stored_file():
    service = make_service()
    record = service.save_document(make_document_update().message)
    url = generate_file_url(BASE, record.public_id)
    assert url.startswith("http://localhost:8000/f/")
    assert url.endswith(record.public_id)


def test_handler_reply_contains_generated_url():
    service = make_service()
    reply = handle_update(make_document_update(), file_service=service)
    assert "🔗 Download:" in reply
    assert "http://localhost:8000/f/" in reply


def test_handler_uses_custom_base_url():
    service = make_service()
    reply = handle_update(
        make_document_update(),
        file_service=service,
        base_url="https://cdn.example.test",
    )
    assert "https://cdn.example.test/f/" in reply
    assert "http://localhost:8000/f/" not in reply


def test_handler_without_service_has_no_url():
    reply = handle_update(make_document_update())
    assert "Download:" not in reply


# ── configuration ──────────────────────────────────────────────────────────


def test_settings_default_base_url():
    assert Settings().base_url == "http://localhost:8000"


def test_settings_loads_base_url(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://example.com/")
    assert Settings().base_url == "https://example.com"


def test_settings_base_url_falls_back_when_blank(monkeypatch):
    monkeypatch.setenv("BASE_URL", "   ")
    assert Settings().base_url == "http://localhost:8000"