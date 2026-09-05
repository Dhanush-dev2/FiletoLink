"""Tests for Phase 10 link management: expiration, download limits,
deletion, /delete, /info, ownership, migration, and concurrency."""
import asyncio
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.bot.handlers import handle_update
from app.bot.models import Document, Message, Update, User
from app.database.connection import initialize
from app.database.files import FileRepository
from app.services.file_sources import LocalFileSource
from app.services.files import FileService

USER = User(id=42, username="alice", first_name="Alice")
OTHER_USER = User(id=999, username="mallory", first_name="Mallory")

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


def make_command(text: str, user: User = USER) -> Update:
    return Update(
        update_id=1,
        message=Message(message_id=1, text=text, user=user),
    )


@pytest.fixture
def repo(tmp_path) -> FileRepository:
    """In-memory repo with one stored file backed by a real temp file."""
    path = tmp_path / "note.txt"
    path.write_bytes(CONTENT)
    repository = FileRepository(initialize(":memory:"))
    FileService(repository).save_document(make_message(str(path)))
    return repository


@pytest.fixture
def service(repo) -> FileService:
    return FileService(repo)


@pytest.fixture
def client(repo) -> TestClient:
    return TestClient(create_app(repository=repo, file_source=LocalFileSource()))


def _pid(repo: FileRepository) -> str:
    return repo.get_by_id(1).public_id


def _url(repo: FileRepository) -> str:
    return f"/f/{_pid(repo)}"


def _past() -> int:
    return int(time.time()) - 60


def _future() -> int:
    return int(time.time()) + 3600


# ── A/B/C/D. expiration ────────────────────────────────────────────────────


def test_no_expiration_link_is_downloadable(repo, client):
    assert repo.get_by_id(1).expires_at is None  # NULL = never expires
    resp = client.get(_url(repo))
    assert resp.status_code == 200
    assert resp.content == CONTENT


def test_future_expiration_link_is_downloadable(repo, client):
    repo.update(1, expires_at=_future())
    resp = client.get(_url(repo))
    assert resp.status_code == 200
    assert resp.content == CONTENT


def test_past_expiration_returns_410_marks_expired_no_bytes_no_counter(repo, client):
    repo.update(1, expires_at=_past())
    resp = client.get(_url(repo))
    assert resp.status_code == 410
    assert CONTENT not in resp.content  # no file bytes served
    assert repo.get_by_id(1).status == "expired"
    assert repo.get_by_id(1).download_count == 0


def test_expiration_is_lazy_no_background_worker(repo, client):
    """The record stays 'active' until first access; the GET triggers it."""
    repo.update(1, expires_at=_past())
    assert repo.get_by_id(1).status == "active"
    assert client.get(_url(repo)).status_code == 410
    assert repo.get_by_id(1).status == "expired"


def test_expired_link_stays_expired_on_later_requests(repo, client):
    repo.update(1, expires_at=_past())
    assert client.get(_url(repo)).status_code == 410
    assert client.get(_url(repo)).status_code == 410  # never reactivates
    assert repo.get_by_id(1).status == "expired"


# ── E/F/G/H. download limits ───────────────────────────────────────────────


def test_unlimited_downloads_null_max(repo, client):
    assert repo.get_by_id(1).max_downloads is None  # NULL = unlimited
    for _ in range(3):
        assert client.get(_url(repo)).status_code == 200
    record = repo.get_by_id(1)
    assert record.download_count == 3
    assert record.status == "active"  # unlimited links never expire via limit


def test_max_downloads_zero_rejects_everything(repo, client):
    repo.update(1, max_downloads=0)
    resp = client.get(_url(repo))
    assert resp.status_code == 410
    assert CONTENT not in resp.content
    assert repo.get_by_id(1).download_count == 0


def test_max_downloads_one(repo, client):
    repo.update(1, max_downloads=1)
    assert client.get(_url(repo)).status_code == 200
    assert repo.get_by_id(1).download_count == 1
    resp = client.get(_url(repo))
    assert resp.status_code == 410
    assert repo.get_by_id(1).download_count == 1


def test_final_download_transitions_status_to_expired(repo, client):
    """The successful GET that reaches max_downloads also flips the status
    to 'expired' in the same atomic operation — no separate check."""
    repo.update(1, max_downloads=1)
    resp = client.get(_url(repo))
    assert resp.status_code == 200
    record = repo.get_by_id(1)
    assert record.download_count == 1
    assert record.status == "expired"  # immediate, not on a later request
    assert client.get(_url(repo)).status_code == 410


def test_max_downloads_n_exactly_n_then_410(repo, client):
    n = 4
    repo.update(1, max_downloads=n)
    for _ in range(n):
        assert client.get(_url(repo)).status_code == 200
    assert repo.get_by_id(1).download_count == n
    resp = client.get(_url(repo))
    assert resp.status_code == 410
    assert repo.get_by_id(1).download_count == n


def test_exhausted_link_terminalized_as_expired(repo, client):
    """Exhausted links reuse the existing 'expired' terminal status."""
    repo.update(1, max_downloads=1)
    assert client.get(_url(repo)).status_code == 200  # final permitted download
    assert repo.get_by_id(1).status == "expired"       # already terminal now
    assert client.get(_url(repo)).status_code == 410


# ── I/J/K. HEAD and Range interplay ────────────────────────────────────────


def test_head_does_not_consume_slot(repo, client):
    repo.update(1, max_downloads=1)
    resp = client.head(_url(repo))
    assert resp.status_code == 200
    assert repo.get_by_id(1).download_count == 0
    # The slot is still available for a real download.
    assert client.get(_url(repo)).status_code == 200


def test_head_on_expired_link_returns_410(repo, client):
    repo.update(1, expires_at=_past())
    resp = client.head(_url(repo))
    assert resp.status_code == 410
    assert repo.get_by_id(1).download_count == 0


def test_head_on_exhausted_link_returns_410(repo, client):
    repo.update(1, max_downloads=1)
    client.get(_url(repo))  # consume the only slot
    assert client.head(_url(repo)).status_code == 410


def test_range_get_consumes_exactly_one_slot(repo, client):
    repo.update(1, max_downloads=1)
    resp = client.get(_url(repo), headers={"Range": "bytes=0-4"})
    assert resp.status_code == 206
    assert resp.content == CONTENT[:5]
    assert repo.get_by_id(1).download_count == 1
    assert client.get(_url(repo)).status_code == 410  # limit exhausted


def test_expired_link_with_range_returns_410_not_206(repo, client):
    repo.update(1, expires_at=_past())
    resp = client.get(_url(repo), headers={"Range": "bytes=0-4"})
    assert resp.status_code == 410
    assert "content-range" not in resp.headers
    assert repo.get_by_id(1).download_count == 0


def test_deleted_link_with_range_returns_410_not_206(repo, client):
    repo.set_status(1, "deleted")
    resp = client.get(_url(repo), headers={"Range": "bytes=0-4"})
    assert resp.status_code == 410
    assert "content-range" not in resp.headers


def test_unsatisfiable_range_does_not_consume_slot(repo, client):
    repo.update(1, max_downloads=1)
    resp = client.get(_url(repo), headers={"Range": "bytes=99-100"})
    assert resp.status_code == 416
    assert repo.get_by_id(1).download_count == 0
    # The slot was released, so a real download still works.
    assert client.get(_url(repo)).status_code == 200


def test_missing_source_file_releases_reserved_slot(repo, client, tmp_path):
    repo.update(1, max_downloads=1)
    import os

    os.unlink(tmp_path / "note.txt")
    assert client.get(_url(repo)).status_code == 404
    assert repo.get_by_id(1).download_count == 0  # slot rolled back


# ── L/M. deletion + idempotency (HTTP + handler) ───────────────────────────


def test_deleted_link_returns_410(repo, client):
    repo.set_status(1, "deleted")
    assert client.get(_url(repo)).status_code == 410
    assert client.head(_url(repo)).status_code == 410


def test_delete_via_command_is_idempotent(repo, service):
    first = handle_update(make_command(f"/delete {_pid(repo)}"), file_service=service)
    assert "Link deleted" in first
    assert repo.get_by_id(1).status == "deleted"

    second = handle_update(make_command(f"/delete {_pid(repo)}"), file_service=service)
    assert "already deleted" in second.lower()
    assert repo.get_by_id(1).status == "deleted"

    # Deleted links never come back.
    client = TestClient(create_app(repository=repo, file_source=LocalFileSource()))
    assert client.get(_url(repo)).status_code == 410


# ── N/O. /delete ownership ─────────────────────────────────────────────────


def test_owner_can_delete(repo, service):
    reply = handle_update(make_command(f"/delete {_pid(repo)}"), file_service=service)
    assert "Link deleted" in reply
    assert _pid(repo) in reply
    assert repo.get_by_id(1).status == "deleted"


def test_non_owner_cannot_delete(repo, service):
    reply = handle_update(
        make_command(f"/delete {_pid(repo)}", user=OTHER_USER), file_service=service
    )
    assert "created" in reply.lower()
    assert repo.get_by_id(1).status == "active"
    # No private metadata is revealed.
    assert "note.txt" not in reply
    assert "FILE1" not in reply
    assert _url(repo) not in reply


def test_delete_without_user_is_rejected(repo, service):
    update = Update(update_id=1, message=Message(message_id=1, text=f"/delete {_pid(repo)}"))
    reply = handle_update(update, file_service=service)
    assert "created" in reply.lower()
    assert repo.get_by_id(1).status == "active"


# ── P/Q/T. /info ownership + privacy ───────────────────────────────────────


def test_owner_info_shows_public_details(repo, service):
    repo.update(1, max_downloads=5, expires_at=_future())
    reply = handle_update(make_command(f"/info {_pid(repo)}"), file_service=service)
    assert "note.txt" in reply            # file name
    assert "12 B" in reply                # file size
    assert "active" in reply              # status
    assert "0 / 5" in reply               # downloads / max
    assert "Expires:" in reply            # expiration
    assert _url(repo) in reply            # public URL


def test_owner_info_shows_unlimited_and_never(repo, service):
    reply = handle_update(make_command(f"/info {_pid(repo)}"), file_service=service)
    assert "unlimited" in reply
    assert "never" in reply


def test_info_reports_expired_immediately_after_final_download(repo, service, client):
    repo.update(1, max_downloads=1)
    client.get(_url(repo))  # final permitted download
    reply = handle_update(make_command(f"/info {_pid(repo)}"), file_service=service)
    assert "expired" in reply
    assert "1 / 1" in reply


def test_non_owner_info_is_rejected_without_metadata(repo, service):
    reply = handle_update(
        make_command(f"/info {_pid(repo)}", user=OTHER_USER), file_service=service
    )
    assert "created" in reply.lower()
    assert "note.txt" not in reply
    assert "FILE1" not in reply
    assert _url(repo) not in reply


def test_info_never_leaks_private_metadata(repo, service):
    reply = handle_update(make_command(f"/info {_pid(repo)}"), file_service=service)
    assert "FILE1" not in reply            # telegram_file_id
    assert "source_path" not in reply      # source path
    assert "record #" not in reply         # database id
    assert "BOT_TOKEN" not in reply
    assert "TELEGRAM_API_ID" not in reply
    assert "TELEGRAM_API_HASH" not in reply


# ── R/S. unknown / invalid ids ─────────────────────────────────────────────


def test_unknown_public_id_management_commands(repo, service):
    for command in ("/delete AbCdEfGh", "/info AbCdEfGh"):
        reply = handle_update(make_command(command), file_service=service)
        assert "couldn't find" in reply.lower()


def test_invalid_public_id_management_commands(repo, service):
    for command in ("/delete not-valid!!", "/delete short", "/info nope", "/info !!!"):
        reply = handle_update(make_command(command), file_service=service)
        assert "valid public id" in reply.lower()


def test_management_commands_require_an_argument(repo, service):
    for command in ("/delete", "/info"):
        reply = handle_update(make_command(command), file_service=service)
        assert "usage" in reply.lower()


def test_management_commands_without_service_are_graceful():
    reply = handle_update(make_command("/delete AbCdEfGh"))
    assert reply  # no crash, friendly text


# ── repository lifecycle methods ───────────────────────────────────────────


def test_reserve_download_slot_until_limit(repo):
    repo.update(1, max_downloads=2)
    assert repo.reserve_download_slot(1) is True
    assert repo.reserve_download_slot(1) is True
    assert repo.reserve_download_slot(1) is False
    record = repo.get_by_id(1)
    assert record.download_count == 2
    assert record.status == "expired"  # final slot terminalized the link


def test_reserve_download_slot_rejects_expired(repo):
    repo.update(1, expires_at=_past())
    assert repo.reserve_download_slot(1) is False
    assert repo.get_by_id(1).download_count == 0


def test_reserve_download_slot_rejects_deleted(repo):
    repo.set_status(1, "deleted")
    assert repo.reserve_download_slot(1) is False


def test_release_download_slot_restores(repo):
    repo.update(1, max_downloads=1)
    assert repo.reserve_download_slot(1) is True
    repo.release_download_slot(1)
    assert repo.get_by_id(1).download_count == 0
    assert repo.reserve_download_slot(1) is True


def test_release_never_goes_negative(repo):
    repo.release_download_slot(1)
    assert repo.get_by_id(1).download_count == 0


def test_expire_if_due_only_flips_active_due_records(repo):
    repo.update(1, expires_at=_past())
    repo.expire_if_due(1)
    assert repo.get_by_id(1).status == "expired"

    # Reset and confirm a future expiry is left alone.
    repo.update(1, status="active", expires_at=_future())
    repo.expire_if_due(1)
    assert repo.get_by_id(1).status == "active"

    # A deleted record is never reactivated by expire_if_due.
    repo.set_status(1, "deleted")
    repo.expire_if_due(1)
    assert repo.get_by_id(1).status == "deleted"


def test_mark_exhausted_only_flips_active_at_limit(repo):
    repo.update(1, max_downloads=1)
    repo.mark_exhausted(1)
    assert repo.get_by_id(1).status == "active"  # not at the limit yet

    repo.update(1, download_count=1)  # simulate the limit being reached
    repo.mark_exhausted(1)
    assert repo.get_by_id(1).status == "expired"


# ── service layer ──────────────────────────────────────────────────────────


def test_save_document_accepts_expiration_and_limit(tmp_path):
    path = tmp_path / "note.txt"
    path.write_bytes(CONTENT)
    repo = FileRepository(initialize(":memory:"))
    record = FileService(repo).save_document(
        make_message(str(path)), expires_at=1_900_000_000, max_downloads=3
    )
    assert record.expires_at == 1_900_000_000
    assert record.max_downloads == 3
    assert repo.get_by_id(record.id).max_downloads == 3


def test_repository_create_accepts_expiration_and_limit(repo):
    record = repo.create(
        telegram_file_id="F2",
        telegram_message_id=9,
        user_id=1,
        expires_at=1_900_000_000,
        max_downloads=7,
    )
    assert record.expires_at == 1_900_000_000
    assert record.max_downloads == 7


def test_update_can_set_and_clear_max_downloads(repo):
    repo.update(1, max_downloads=5)
    assert repo.get_by_id(1).max_downloads == 5
    repo.update(1, max_downloads=None)  # back to unlimited
    assert repo.get_by_id(1).max_downloads is None


def test_negative_max_downloads_rejected(repo):
    with pytest.raises(ValueError):
        repo.update(1, max_downloads=-1)
    with pytest.raises(ValueError):
        repo.create(telegram_file_id="F", telegram_message_id=1, user_id=1, max_downloads=-1)


def test_get_link_and_delete_link(repo, service):
    pid = _pid(repo)
    assert service.get_link(pid).id == 1
    assert service.get_link("AbCdEfGh") is None
    assert service.delete_link(pid).status == "deleted"
    assert repo.get_by_id(1).status == "deleted"
    assert service.delete_link("AbCdEfGh") is None


# ── U/V. migration ─────────────────────────────────────────────────────────

# The Phase 9 files table (before max_downloads existed).
PHASE9_SCHEMA = """
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
        CHECK (status IN ('active', 'deleted', 'expired')),
    source_path TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_files_public_id ON files (public_id);
CREATE INDEX IF NOT EXISTS idx_files_telegram_file_id ON files (telegram_file_id);
CREATE INDEX IF NOT EXISTS idx_files_status ON files (status);
"""


def test_migration_adds_max_downloads_and_keeps_records(tmp_path):
    db = tmp_path / "phase9.db"
    conn = sqlite3.connect(db)
    conn.executescript(PHASE9_SCHEMA)
    conn.execute(
        "INSERT INTO files (public_id, telegram_file_id, telegram_message_id, "
        "user_id, file_name, file_size, mime_type, created_at, expires_at, "
        "download_count, status, source_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "AbCdEfGh", "FILE1", 7, 42, "note.txt", 12, "text/plain",
            1_700_000_000, 1_800_000_000, 3, "active", "/tmp/note.txt",
        ),
    )
    conn.commit()
    conn.close()

    migrated = initialize(db)
    columns = {row["name"] for row in migrated.execute("PRAGMA table_info(files)")}
    assert "max_downloads" in columns

    row = migrated.execute("SELECT * FROM files WHERE id = 1").fetchone()
    # Existing records receive max_downloads = NULL (unlimited) and their
    # data is left intact.
    assert row["max_downloads"] is None
    assert row["telegram_file_id"] == "FILE1"
    assert row["public_id"] == "AbCdEfGh"
    assert row["file_name"] == "note.txt"
    assert row["download_count"] == 3
    assert row["expires_at"] == 1_800_000_000
    assert row["status"] == "active"
    assert row["source_path"] == "/tmp/note.txt"
    migrated.close()


def test_fresh_schema_includes_max_downloads():
    conn = initialize(":memory:")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(files)")}
    assert "max_downloads" in columns
    assert "expires_at" in columns


# ── W. concurrency (REQUIRED) ──────────────────────────────────────────────


def test_concurrent_gets_respect_download_limit(tmp_path):
    """max_downloads=5: 12 concurrent GETs → exactly 5 succeed, 7 → 410.

    Real concurrency through the ASGI app (sync endpoints run in a
    threadpool), so the conditional UPDATE must prevent overshoot.
    """
    path = tmp_path / "note.txt"
    path.write_bytes(CONTENT)
    repo = FileRepository(initialize(":memory:"))
    FileService(repo).save_document(make_message(str(path)), max_downloads=5)
    pid = repo.get_by_id(1).public_id

    app = create_app(repository=repo, file_source=LocalFileSource())
    transport = httpx.ASGITransport(app=app)

    async def run() -> list[int]:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            responses = await asyncio.gather(
                *(client.get(f"/f/{pid}") for _ in range(12))
            )
        return [r.status_code for r in responses]

    statuses = asyncio.run(run())
    assert statuses.count(200) == 5
    assert statuses.count(410) == 7
    record = repo.get_by_id(1)
    assert record.download_count == 5
    assert record.status == "expired"  # final slot terminalized the link