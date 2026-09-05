"""Tests for public id generation and collision-safe assignment."""
import re

import pytest

from app.bot.handlers import handle_update
from app.bot.models import Document, Message, Update, User
from app.database.connection import initialize
from app.database.files import FileRepository
from app.services.files import (
    MAX_PUBLIC_ID_ATTEMPTS,
    FileService,
    PublicIdError,
)
from app.utils.public_ids import generate_public_id

USER = User(id=42, username="alice", first_name="Alice")

PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8}$")


@pytest.fixture
def repo() -> FileRepository:
    return FileRepository(initialize(":memory:"))


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


# ── generation ────────────────────────────────────────────────────────────


def test_generated_ids_are_url_safe_and_compact():
    for _ in range(100):
        assert PUBLIC_ID_RE.match(generate_public_id()) is not None


def test_generated_ids_are_unique_in_bulk():
    ids = {generate_public_id() for _ in range(10_000)}
    assert len(ids) == 10_000


def test_generated_ids_are_not_predictable_shapes():
    # Must not look like sequential ids, timestamps, or plain row numbers.
    for _ in range(50):
        assert not generate_public_id().isdigit()


# ── assignment via service ─────────────────────────────────────────────────


def test_save_document_assigns_public_id(repo):
    service = FileService(repo)
    record = service.save_document(make_document_update().message)
    assert record.public_id is not None
    assert PUBLIC_ID_RE.match(record.public_id) is not None
    # Persisted in the database.
    assert repo.get_by_id(record.id).public_id == record.public_id


def test_distinct_files_get_distinct_public_ids(repo):
    service = FileService(repo)
    first = service.save_document(make_document_update().message)
    # Same telegram file id is fine — public ids must still differ.
    second = service.save_document(make_document_update().message)
    assert first.public_id != second.public_id


def test_collision_retries_with_fresh_id(repo, monkeypatch):
    # Pre-existing record already using the id the generator will emit.
    other = repo.create(
        telegram_file_id="OTHER",
        telegram_message_id=1,
        user_id=1,
        created_at=1_700_000_000,
    )
    repo.update(other.id, public_id="COLLIDES")

    ids = iter(["COLLIDES", "FRESH1234"])
    monkeypatch.setattr(
        "app.services.files.generate_public_id", lambda: next(ids)
    )

    service = FileService(repo)
    record = service.save_document(make_document_update().message)
    assert record.public_id == "FRESH1234"
    # The other file's id was not overwritten.
    assert repo.get_by_id(other.id).public_id == "COLLIDES"


def test_collision_exhaustion_raises(repo, monkeypatch):
    other = repo.create(
        telegram_file_id="OTHER",
        telegram_message_id=1,
        user_id=1,
        created_at=1_700_000_000,
    )
    repo.update(other.id, public_id="STUCK")

    monkeypatch.setattr(
        "app.services.files.generate_public_id", lambda: "STUCK"
    )

    service = FileService(repo)
    with pytest.raises(PublicIdError):
        service.save_document(make_document_update().message)
    # Existing file untouched.
    assert repo.get_by_id(other.id).public_id == "STUCK"
    assert MAX_PUBLIC_ID_ATTEMPTS == 10


# ── handler integration ────────────────────────────────────────────────────


def test_file_reply_includes_public_id(repo):
    service = FileService(repo)
    reply = handle_update(make_document_update(), file_service=service)
    assert "Stored in database (record #1)" in reply
    match = re.search(r"Public ID: ([A-Za-z0-9_-]{8})", reply)
    assert match is not None
    assert repo.get_by_id(1).public_id == match.group(1)


def test_file_reply_without_service_has_no_public_id():
    reply = handle_update(make_document_update())
    assert "Public ID:" not in reply
    assert "Stored in database" not in reply