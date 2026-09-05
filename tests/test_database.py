"""Tests for the SQLite database layer, repository, and file service."""
import sqlite3

import pytest

from app.bot.handlers import handle_update
from app.bot.models import Document, Message, Update, User
from app.database.connection import initialize
from app.database.files import FileRepository
from app.database.records import FileRecord
from app.services.files import FileService, make_file_service

USER = User(id=42, username="alice", first_name="Alice")


@pytest.fixture
def repo() -> FileRepository:
    return FileRepository(initialize(":memory:"))


def make_args(**overrides):
    args = dict(
        telegram_file_id="FILE1",
        telegram_message_id=5,
        user_id=42,
        file_name="a.pdf",
        file_size=1000,
        mime_type="application/pdf",
        created_at=1_700_000_000,
    )
    args.update(overrides)
    return args


def make_document_update(record_args=None, text=None, user=USER) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=7,
            text=text,
            user=user,
            document=Document(
                file_id="FILE1",
                file_name="a.pdf",
                file_size=1000,
                mime_type="application/pdf",
            ),
            date=1_700_000_000,
        ),
    )


# ── connection / schema ───────────────────────────────────────────────────


def test_initialize_creates_files_table():
    conn = initialize(":memory:")
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='files'"
    ).fetchone()
    assert row is not None


def test_initialize_is_idempotent():
    from app.database.connection import initialize_schema

    conn = initialize(":memory:")
    initialize_schema(conn)  # running the schema twice must not raise
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM sqlite_master WHERE type='index'"
    ).fetchone()["c"]
    assert count >= 3  # public_id (unique), telegram_file_id, status


# ── repository: create / retrieve ──────────────────────────────────────────


def test_create_sets_defaults(repo):
    record = repo.create(**make_args(created_at=None))
    assert isinstance(record, FileRecord)
    assert record.id == 1
    assert record.status == "active"
    assert record.download_count == 0
    assert record.public_id is None
    assert record.expires_at is None
    assert record.created_at > 0  # defaulted to now


def test_create_and_get_by_id_roundtrip(repo):
    record = repo.create(**make_args())
    fetched = repo.get_by_id(record.id)
    assert fetched == record
    assert fetched.telegram_file_id == "FILE1"
    assert fetched.telegram_message_id == 5
    assert fetched.user_id == 42
    assert fetched.file_name == "a.pdf"
    assert fetched.file_size == 1000
    assert fetched.mime_type == "application/pdf"
    assert fetched.created_at == 1_700_000_000


def test_create_with_minimal_fields(repo):
    record = repo.create(
        telegram_file_id="F2", telegram_message_id=1, user_id=None
    )
    assert record.user_id is None
    assert record.file_name is None
    assert record.file_size is None
    assert record.mime_type is None


def test_get_by_id_missing_returns_none(repo):
    assert repo.get_by_id(999) is None


def test_get_by_telegram_file_id_returns_latest(repo):
    repo.create(**make_args(telegram_file_id="SAME"))
    second = repo.create(**make_args(telegram_file_id="SAME", file_name="b.pdf"))
    fetched = repo.get_by_telegram_file_id("SAME")
    assert fetched.id == second.id
    assert fetched.file_name == "b.pdf"


def test_get_by_telegram_file_id_missing_returns_none(repo):
    assert repo.get_by_telegram_file_id("NOPE") is None


# ── repository: update / invalidate / delete ───────────────────────────────


def test_update_changes_provided_fields_only(repo):
    record = repo.create(**make_args())
    updated = repo.update(
        record.id,
        download_count=3,
        expires_at=1_800_000_000,
        status="expired",
    )
    assert updated.download_count == 3
    assert updated.expires_at == 1_800_000_000
    assert updated.status == "expired"
    # Untouched fields keep their values.
    assert updated.file_name == "a.pdf"
    assert updated.telegram_file_id == "FILE1"


def test_update_with_no_fields_returns_record(repo):
    record = repo.create(**make_args())
    assert repo.update(record.id) == record


def test_update_can_set_public_id(repo):
    record = repo.create(**make_args())
    updated = repo.update(record.id, public_id="Ab7Kx92")
    assert updated.public_id == "Ab7Kx92"


def test_set_status_deleted(repo):
    record = repo.create(**make_args())
    updated = repo.set_status(record.id, "deleted")
    assert updated.status == "deleted"


def test_invalid_status_rejected(repo):
    record = repo.create(**make_args())
    with pytest.raises(ValueError):
        repo.update(record.id, status="bogus")


def test_delete_removes_record(repo):
    record = repo.create(**make_args())
    assert repo.delete(record.id) is True
    assert repo.get_by_id(record.id) is None
    assert repo.delete(record.id) is False


# ── service layer ──────────────────────────────────────────────────────────


def test_save_document_persists_metadata(repo):
    service = FileService(repo)
    record = service.save_document(make_document_update().message)
    assert record.id == 1
    assert record.telegram_file_id == "FILE1"
    assert record.user_id == 42
    assert record.file_name == "a.pdf"
    assert record.file_size == 1000
    assert record.mime_type == "application/pdf"
    assert record.created_at == 1_700_000_000
    assert repo.get_by_id(record.id) == record


def test_save_document_without_document_raises(repo):
    service = FileService(repo)
    with pytest.raises(ValueError):
        service.save_document(Message(message_id=1, text="hi", user=USER))


def test_make_file_service_in_memory():
    service = make_file_service(":memory:")
    record = service.save_document(make_document_update().message)
    assert record.id == 1


# ── handler + transport integration ────────────────────────────────────────


def test_handler_persists_when_service_given(repo):
    service = FileService(repo)
    reply = handle_update(make_document_update(), file_service=service)
    assert "File received" in reply
    assert "Stored in database (record #1)" in reply
    assert repo.get_by_id(1) is not None


def test_handler_without_service_does_not_persist():
    reply = handle_update(make_document_update())
    assert "Stored in database" not in reply


def test_mock_send_persists_with_service(tmp_path):
    path = tmp_path / "note.txt"
    path.write_bytes(b"hello world\n")
    repo = FileRepository(initialize(":memory:"))
    service = FileService(repo)
    reply = _send_via_mock(str(path), service)
    assert "Stored in database (record #1)" in reply
    record = repo.get_by_id(1)
    assert record.file_name == "note.txt"
    assert record.file_size == 12


def _send_via_mock(path: str, service: FileService) -> str:
    from app.bot.mock import send_local_file

    return send_local_file(path, user=USER, file_service=service)