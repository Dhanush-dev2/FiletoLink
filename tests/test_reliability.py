"""Phase 18: reliability & operations.

These tests pin what an operator relies on across restarts, backups, and
recovery — without a running Local Bot API Server, and without touching a real
deployment. They replay the phase's 16 scenarios at the component level:

1. FastAPI restart      — fresh process/connection, records re-read from SQLite
2. bot restart          — fresh FileService over the same database + storage
3. Local Bot API restart— existing links never depend on the server's work dir
4. staging loss         — wiping the server's work dir does not break links
5. FileToLink restart   — re-ingestion after a restart works and old links hold
6. SQLite backup        — a live-consistent snapshot restores cleanly
7. db + storage restore — restoring both halves together preserves links
8. db/storage consistency— an active record always has its owned file
9. stored file missing  — 404, no download consumed, recovers when replaced
10. orphaned storage file— never served, never touched by unrelated deletion
11. partial ``.part``    — an interrupted copy is never served
12. download across restart— counters/limits persist and stay exact
13. interrupted ingestion— no broken active record, no orphan on a clean failure
14. expired/deleted     — terminal states survive a restart
15. public links        — preserved by a backup restore, wherever it lands
16. no manual DB surgery— a moved storage root is resolved automatically

Everything runs against the shipped defaults (the shared ``hermetic_environment``
fixture in ``conftest.py`` clears the deployment ``.env``).
"""
import asyncio
import hashlib
import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.bot.models import Document, Message, User
from app.config import get_settings
from app.database.connection import initialize
from app.database.files import FileRepository
from app.services.file_sources import FileSourceError, StorageFileSource
from app.services.files import FileService, make_file_service
from app.services.storage import FileStorage, StorageError

CONTENT = b"phase18 reliability payload \x00\x01\x02" * 64
USER = User(id=7, username="ops", first_name="Ops")


# ── helpers ─────────────────────────────────────────────────────────────────


def _deploy_env(tmp_path, monkeypatch) -> tuple[Path, Path, Path]:
    """Point the whole deployment at tmp_path, as a real ``.env`` would."""
    db = tmp_path / "data" / "files.db"
    storage = tmp_path / "data" / "storage"
    work = tmp_path / "telegram_bot_data"
    monkeypatch.setenv("BOT_MODE", "real")
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("FILE_STORAGE_DIR", str(storage))
    monkeypatch.setenv("TELEGRAM_BOT_WORK_DIR", str(work))
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    return db, storage, work


def _message(source_path: Path, *, name: str, size: int | None = None) -> Message:
    return Message(
        message_id=1,
        user=USER,
        document=Document(
            file_id="FILE1",
            file_name=name,
            file_size=len(CONTENT) if size is None else size,
            mime_type="application/octet-stream",
        ),
        date=1_700_000_000,
        source_path=str(source_path),
    )


def _ingest(
    db: Path,
    storage: Path,
    work: Path,
    *,
    name: str = "clip.bin",
    content: bytes = CONTENT,
    expires_at: int | None = None,
    max_downloads: int | None = None,
):
    """Receive one file through the real-mode service (ownership transfer)."""
    work.mkdir(parents=True, exist_ok=True)
    source = work / name
    source.write_bytes(content)
    service = make_file_service(db, FileStorage(storage))
    return service.save_document(
        _message(source, name=name, size=len(content)),
        expires_at=expires_at,
        max_downloads=max_downloads,
    )


def _client() -> TestClient:
    """Build an API client exactly like ``python run.py --api`` would."""
    get_settings.cache_clear()  # re-read the environment (a "restart")
    return TestClient(create_app())


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parallel_get(app, pid: str, count: int) -> list[int]:
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            responses = await asyncio.gather(
                *(client.get(f"/f/{pid}") for _ in range(count))
            )
        return [r.status_code for r in responses]

    return asyncio.run(run())


def _backup(db: Path, dest: Path) -> None:
    """Live-consistent SQLite copy, exactly like ``sqlite3 .backup``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(db)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


# ── 1/12: FastAPI restart, counters + limits ────────────────────────────────


def test_fastapi_restart_preserves_counters_and_limits(tmp_path, monkeypatch):
    """A restart is stateless: the download counter and the limit live in
    SQLite and keep being enforced exactly."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    record = _ingest(db, storage, work, max_downloads=2)
    pid = record.public_id

    assert _client().get(f"/f/{pid}").status_code == 200
    repository = FileRepository(initialize(db))
    assert repository.get_by_id(record.id).download_count == 1

    restart = _client()  # restarted FastAPI: fresh connection, fresh limiter
    second = restart.get(f"/f/{pid}")
    assert second.status_code == 200
    assert _sha256(second.content) == _sha256(CONTENT)
    assert repository.get_by_id(record.id).download_count == 2
    assert repository.get_by_id(record.id).status == "expired"

    assert restart.get(f"/f/{pid}").status_code == 410  # limit stayed exact


def test_concurrent_downloads_across_restart_cannot_exceed_the_limit(
    tmp_path, monkeypatch
):
    """max_downloads=3 with parallel GETs genuinely oversubscribed: exactly
    three succeed, even though requests arrive from two app instances."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    record = _ingest(db, storage, work, max_downloads=3)
    pid = record.public_id

    first = create_app()
    second = create_app()  # a second instance, as after a restart
    statuses = _parallel_get(first, pid, 4) + _parallel_get(second, pid, 4)

    assert statuses.count(200) == 3
    assert set(statuses) <= {200, 410}
    assert FileRepository(initialize(db)).get_by_id(record.id).download_count == 3


# ── 2/5: bot restart ────────────────────────────────────────────────────────


def test_bot_restart_ingests_new_files_and_keeps_old_links(tmp_path, monkeypatch):
    """After a bot restart, old links still serve and a fresh upload works."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    first = _ingest(db, storage, work, name="first.bin")
    assert _client().get(f"/f/{first.public_id}").status_code == 200

    # Restart the bot: a fresh FileService over the same db + storage.
    second = _ingest(db, storage, work, name="second.bin")

    assert FileRepository(initialize(db)).get_by_public_id(first.public_id) is not None
    assert _client().get(f"/f/{first.public_id}").status_code == 200
    assert _client().get(f"/f/{second.public_id}").status_code == 200
    assert Path(second.source_path).is_file()


# ── 3/4: Local Bot API restart / staging loss ───────────────────────────────


def test_local_bot_api_restart_and_staging_loss(tmp_path, monkeypatch):
    """The server's work dir is disposable: wiping it (restart/eviction) keeps
    every owned link working, and re-ingestion into a fresh work dir works."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    old = _ingest(db, storage, work, name="old.bin")

    shutil.rmtree(work)  # e.g. the Local Bot API Server restarts with a clean --dir
    assert not work.exists()
    assert _client().get(f"/f/{old.public_id}").status_code == 200

    # The server comes back and re-stages a new file.
    new = _ingest(db, storage, work, name="new.bin")
    assert Path(new.source_path).is_file()
    assert _client().get(f"/f/{new.public_id}").status_code == 200


# ── 6/7/8/15: backup + restore ──────────────────────────────────────────────


def test_sqlite_backup_is_consistent_and_restores_the_record(tmp_path, monkeypatch):
    """A ``sqlite3 .backup``-style snapshot of the live database opens cleanly
    and still holds the link metadata."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    record = _ingest(db, storage, work)

    backup_db = tmp_path / "backup" / "files.db"
    _backup(db, backup_db)

    restored = FileRepository(initialize(backup_db)).get_by_public_id(
        record.public_id
    )
    assert restored is not None
    assert restored.status == "active"
    assert restored.source_path == record.source_path


def test_restoring_the_storage_half_preserves_public_links(tmp_path, monkeypatch):
    """If the storage directory is lost and restored from backup (to the same
    path), every link still serves — the database half was untouched."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    record = _ingest(db, storage, work)

    backup = tmp_path / "backup"
    _backup(db, backup / "files.db")
    shutil.copytree(storage, backup / "storage")

    shutil.rmtree(storage)  # the storage half was lost
    shutil.copytree(backup / "storage", storage)  # ... and restored

    response = _client().get(f"/f/{record.public_id}")
    assert response.status_code == 200
    assert _sha256(response.content) == _sha256(CONTENT)


def test_backup_restore_to_a_different_location_needs_no_database_surgery(
    tmp_path, monkeypatch
):
    """The heart of Phase 18: restoring db + storage *together* to a different
    folder/drive/machine must keep every public link working.

    ``source_path`` records an absolute path from ingest time, so after a move
    that path no longer exists. The public-id-keyed layout
    (``<storage>/<public_id>/<filename>``) identifies the file instead, so no
    manual SQL rewrite is needed.
    """
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    record = _ingest(db, storage, work)

    restored_db = tmp_path / "restored" / "files.db"
    restored_storage = tmp_path / "restored" / "storage"
    _backup(db, restored_db)
    shutil.copytree(storage, restored_storage)
    # The disaster case: the original bytes are gone, only the restore remains.
    shutil.rmtree(storage)

    monkeypatch.setenv("DATABASE_PATH", str(restored_db))
    monkeypatch.setenv("FILE_STORAGE_DIR", str(restored_storage))

    # The old absolute source_path does not exist under the new root.
    get_settings.cache_clear()
    assert not Path(record.source_path).exists()

    client = TestClient(create_app())
    assert client.head(f"/f/{record.public_id}").status_code == 200
    response = client.get(f"/f/{record.public_id}")
    assert response.status_code == 200
    assert _sha256(response.content) == _sha256(CONTENT)

    # The restored copy — not the (deleted) original — is what was served.
    assert Path(record.source_path) != (
        restored_storage / record.public_id / "clip.bin"
    )
    assert (restored_storage / record.public_id / "clip.bin").is_file()


def test_deletion_after_a_moved_restore_cleans_up_the_owned_file(
    tmp_path, monkeypatch
):
    """Deletion resolves the file by public id too, so a link restored to a
    new location is actually removed (no manual database surgery)."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    record = _ingest(db, storage, work)

    restored_db = tmp_path / "restored" / "files.db"
    restored_storage = tmp_path / "restored" / "storage"
    _backup(db, restored_db)
    shutil.copytree(storage, restored_storage)
    monkeypatch.setenv("DATABASE_PATH", str(restored_db))
    monkeypatch.setenv("FILE_STORAGE_DIR", str(restored_storage))

    service = make_file_service(restored_db, FileStorage(restored_storage))
    assert service.delete_link(record.public_id).status == "deleted"
    assert not (restored_storage / record.public_id).exists()
    assert service.get_link(record.public_id).status == "deleted"


def test_active_record_always_has_its_owned_file(tmp_path, monkeypatch):
    """The durable invariant, re-checked after a restart: every active record
    resolves to a readable owned file inside the storage root."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    for name in ("a.bin", "b.bin", "c.bin"):
        _ingest(db, storage, work, name=name)

    conn = initialize(db)
    try:
        rows = conn.execute("SELECT * FROM files").fetchall()
    finally:
        conn.close()
    assert len(rows) == 3
    repository = FileRepository(initialize(db))
    source = StorageFileSource(storage)
    for row in rows:
        record = repository.get_by_id(row["id"])
        assert record.status == "active"
        with source.open(record) as handle:
            assert handle.read() == CONTENT


# ── 9: a stored file is missing ─────────────────────────────────────────────


def test_missing_stored_file_is_404_and_recovers_when_replaced(
    tmp_path, monkeypatch
):
    """A missing owned file never burns a download and never 500s; restoring
    the bytes makes the same public link work again."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    record = _ingest(db, storage, work, max_downloads=3)
    pid = record.public_id
    owned = Path(record.source_path)

    owned.unlink()  # e.g. the storage half of a restore is incomplete

    client = _client()
    assert client.get(f"/f/{pid}").status_code == 404
    assert client.head(f"/f/{pid}").status_code == 404
    repository = FileRepository(initialize(db))
    assert repository.get_by_id(record.id).download_count == 0  # slot released

    owned.parent.mkdir(parents=True, exist_ok=True)
    owned.write_bytes(CONTENT)  # ~ file restored onto disk

    response = client.get(f"/f/{pid}")
    assert response.status_code == 200
    assert _sha256(response.content) == _sha256(CONTENT)


# ── 10/11: orphaned and partial storage files ──────────────────────────────


def test_orphaned_storage_file_is_never_served_or_touched(tmp_path, monkeypatch):
    """An unreferenced file in storage has no record, so nothing can serve it,
    and deleting an unrelated link never touches it."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    record = _ingest(db, storage, work)

    orphan = storage / "orphanAA" / "leftover.bin"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"unreferenced")

    assert _client().get("/f/orphanAA").status_code == 404

    make_file_service(db, FileStorage(storage)).delete_link(record.public_id)
    assert orphan.is_file()  # unrelated cleanup left it alone
    assert orphan.read_bytes() == b"unreferenced"


def test_partial_part_file_is_never_served(tmp_path, monkeypatch):
    """A ``.part`` file is an interrupted copy, not owned content: it is never
    served even when the real destination is gone, and a successful transfer
    never leaves one behind."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    record = _ingest(db, storage, work)
    pid = record.public_id
    owned = Path(record.source_path)

    owned.unlink()
    (owned.parent / (owned.name + ".part")).write_bytes(b"half a file")

    assert _client().get(f"/f/{pid}").status_code == 404

    # A normal transfer leaves no .part behind in its own link directory.
    clean = _ingest(db, storage, work, name="clean.bin")
    assert not list((storage / clean.public_id).glob("*.part"))


# ── 13: interrupted / failed ingestion ─────────────────────────────────────


def test_failed_ingestion_leaves_no_active_record(tmp_path, monkeypatch):
    """A transfer failure or a failed INSERT produces no link at all — the
    invariant "active ⇒ owned bytes exist" is never violated."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    work.mkdir(parents=True)
    source = work / "broken.bin"
    source.write_bytes(CONTENT)

    service = make_file_service(db, FileStorage(storage))

    # (a) the file cannot be taken into storage at all
    def fail_adopt(*args, **kwargs):
        raise StorageError("simulated transfer failure")

    monkeypatch.setattr(service.storage, "adopt", fail_adopt)
    with pytest.raises(StorageError):
        service.save_document(_message(source, name="broken.bin"))

    # (b) the transfer succeeds but the record cannot be written → rollback
    service2 = make_file_service(db, FileStorage(storage))

    def fail_insert(**kwargs):
        raise sqlite3.OperationalError("simulated database failure")

    monkeypatch.setattr(service2.repository, "create", fail_insert)
    with pytest.raises(sqlite3.Error):
        service2.save_document(_message(source, name="broken.bin"))

    repository = FileRepository(initialize(db))
    assert repository.get_by_id(1) is None          # no record was created
    assert not list(storage.rglob("broken.bin"))     # no orphan left behind


def test_crash_between_adopt_and_insert_creates_no_active_record(
    tmp_path, monkeypatch
):
    """A process killed after the file moved but before the record was written
    leaves an unreferenced file — never a link without bytes. The user simply
    re-sends (Telegram re-delivers the file to the local server)."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    work.mkdir(parents=True)
    source = work / "interrupted.bin"
    source.write_bytes(CONTENT)

    service = make_file_service(db, FileStorage(storage))
    pid = service._new_public_id()
    owned = service.storage.adopt(
        str(source), public_id=pid, file_name="interrupted.bin"
    )
    # ... crash: nothing else happens, no INSERT.

    assert Path(owned).is_file()                       # orphaned bytes exist
    assert not source.exists()                         # moved out of staging
    assert service.get_link(pid) is None               # but no record
    assert _client().get(f"/f/{pid}").status_code == 404


# ── 14: expired / deleted stay safe ────────────────────────────────────────


def test_expired_and_deleted_records_stay_safe_across_restart(
    tmp_path, monkeypatch
):
    """Terminal lifecycle states survive a restart: expired and deleted links
    still answer 410 and are never reactivated."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    expired = _ingest(db, storage, work, name="expired.bin", expires_at=1)
    deleted = _ingest(db, storage, work, name="deleted.bin")

    assert _client().get(f"/f/{expired.public_id}").status_code == 410
    make_file_service(db, FileStorage(storage)).delete_link(deleted.public_id)

    restart = _client()
    assert restart.get(f"/f/{expired.public_id}").status_code == 410
    assert restart.get(f"/f/{deleted.public_id}").status_code == 410
    assert restart.head(f"/f/{deleted.public_id}").status_code == 410
