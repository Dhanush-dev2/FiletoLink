"""Tests for the FastAPI download endpoint."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.bot.models import Document, Message, Update, User
from app.database.connection import initialize
from app.database.files import FileRepository
from app.services.file_sources import LocalFileSource
from app.services.files import FileService

USER = User(id=42, username="alice", first_name="Alice")

CONTENT = b"hello world\n"


def make_message(path: str | None = None) -> Message:
    return Message(
        message_id=7,
        user=USER,
        document=Document(
            file_id="FILE1",
            file_name="note.txt",
            file_size=len(CONTENT),
            mime_type="text/plain",
        ),
        date=1_700_000_000,
        source_path=path,
    )


@pytest.fixture
def stored_file(tmp_path) -> tuple[FileRepository, FileService, str]:
    """Repo + service with one stored file backed by a real temp file."""
    path = tmp_path / "note.txt"
    path.write_bytes(CONTENT)
    repo = FileRepository(initialize(":memory:"))
    service = FileService(repo)
    service.save_document(make_message(str(path)))
    return repo, service, str(path)


@pytest.fixture
def client(stored_file) -> TestClient:
    repo, _service, _path = stored_file
    return TestClient(create_app(repository=repo, file_source=LocalFileSource()))


def _public_id_of(repo: FileRepository) -> str:
    return repo.get_by_id(1).public_id


def test_valid_link_returns_file(client, stored_file):
    repo, _service, _path = stored_file
    resp = client.get(f"/f/{_public_id_of(repo)}")
    assert resp.status_code == 200
    assert resp.content == CONTENT
    assert resp.headers["content-type"].startswith("text/plain")
    assert "attachment" in resp.headers["content-disposition"]
    assert "note.txt" in resp.headers["content-disposition"]


def test_download_increments_counter(client, stored_file):
    repo, _service, _path = stored_file
    pid = _public_id_of(repo)
    assert repo.get_by_id(1).download_count == 0
    client.get(f"/f/{pid}")
    client.get(f"/f/{pid}")
    assert repo.get_by_id(1).download_count == 2


def test_invalid_public_id_returns_404(client):
    assert client.get("/f/notvalid!").status_code == 404
    assert client.get("/f/short").status_code == 404


def test_unknown_public_id_returns_404(client):
    assert client.get("/f/AbCdEfGh").status_code == 404


def test_deleted_link_returns_410(client, stored_file):
    repo, _service, _path = stored_file
    repo.set_status(repo.get_by_id(1).id, "deleted")
    resp = client.get(f"/f/{_public_id_of(repo)}")
    assert resp.status_code == 410


def test_expired_link_returns_410(client, stored_file):
    repo, _service, _path = stored_file
    repo.update(repo.get_by_id(1).id, status="expired")
    resp = client.get(f"/f/{_public_id_of(repo)}")
    assert resp.status_code == 410


def test_missing_source_file_returns_404(stored_file, tmp_path):
    repo, _service, path = stored_file
    # Remove the file the record points at.
    import os

    os.unlink(path)
    client = TestClient(create_app(repository=repo, file_source=LocalFileSource()))
    assert client.get(f"/f/{_public_id_of(repo)}").status_code == 404


def test_record_without_source_path_returns_404(tmp_path):
    repo = FileRepository(initialize(":memory:"))
    service = FileService(repo)
    service.save_document(make_message(path=None))  # no local source
    client = TestClient(create_app(repository=repo, file_source=LocalFileSource()))
    assert client.get(f"/f/{_public_id_of(repo)}").status_code == 404


def test_mock_send_produces_working_link(tmp_path):
    """Full loop: mock send → URL → HTTP download of the original bytes."""
    from app.bot.mock import send_local_file

    src = tmp_path / "report.csv"
    src.write_bytes(b"a,b,c\n1,2,3\n")

    repo = FileRepository(initialize(":memory:"))
    service = FileService(repo)
    reply = send_local_file(str(src), user=USER, file_service=service, base_url="http://localhost:8000")

    import re

    match = re.search(r"http://localhost:8000/f/([A-Za-z0-9_-]{8})", reply)
    assert match is not None

    client = TestClient(create_app(repository=repo, file_source=LocalFileSource()))
    resp = client.get(f"/f/{match.group(1)}")
    assert resp.status_code == 200
    assert resp.content == b"a,b,c\n1,2,3\n"


def test_api_factory_defaults_to_configured_database():
    # create_app() with no args must construct without error.
    app = create_app()
    assert app is not None


# ── migration ──────────────────────────────────────────────────────────────


def test_migration_adds_source_path_column(tmp_path):
    """A database created before source_path existed gets the column."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT,
            telegram_file_id TEXT NOT NULL,
            telegram_message_id INTEGER NOT NULL,
            user_id INTEGER,
            file_name TEXT,
            file_size INTEGER,
            mime_type TEXT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER,
            download_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active'
        );
        """
    )
    conn.commit()
    conn.close()

    migrated = initialize(db)
    columns = {
        row["name"] for row in migrated.execute("PRAGMA table_info(files)")
    }
    assert "source_path" in columns