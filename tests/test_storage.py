"""Phase 14B tests: FileToLink ownership of received files.

The Local Bot API Server stores received files in a directory it manages and
may evict them, so FileToLink must take ownership of the bytes *before* a
public link exists. These tests cover the transfer service, the ingestion
order (transfer → verify → record → reply), rollback on failure, and the
existing download/Range/HEAD/lifecycle behaviour on FileToLink-owned files.

No real Telegram credentials or network access are used: the Local Bot API
Server is simulated with the same fakes as ``tests/test_telegram.py``.
"""
import hashlib
import os
import pathlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.bot.handlers import handle_update
from app.bot.models import Document, Message, Update, User
from app.bot.telegram import handle_message_update
from app.config import Settings
from app.database.connection import initialize
from app.database.files import FileRepository
from app.services.file_sources import FileSourceError, StorageFileSource
from app.services.files import FileService, make_file_service
from app.services.storage import (
    CHUNK_SIZE,
    PART_SUFFIX,
    FileStorage,
    StorageError,
    safe_file_name,
)
from tests.test_telegram import (
    FakeBot,
    bind,
    make_context,
    make_ptb_document,
    make_ptb_message,
    make_ptb_update,
    run_async,
)

USER = User(id=42, username="alice", first_name="Alice")

CONTENT = b"owned bytes\n"


# ── helpers ────────────────────────────────────────────────────────────────

def make_message(
    path: str | None,
    *,
    file_name: str | None = "note.txt",
    file_size: int | None = len(CONTENT),
    mime_type: str | None = "text/plain",
    user: User | None = USER,
) -> Message:
    return Message(
        message_id=7,
        user=user,
        document=Document(
            file_id="FILE1",
            file_name=file_name,
            file_size=file_size,
            mime_type=mime_type,
        ),
        date=1_700_000_000,
        source_path=path,
    )


def make_source(directory, name: str = "note.txt", content: bytes = CONTENT) -> pathlib.Path:
    """A stand-in for a file the Local Bot API Server stored in its work dir."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    return path


def storage_files(root) -> list[str]:
    """Every regular file under ``root`` (relative paths), sorted."""
    root = pathlib.Path(root)
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def owning_service(tmp_path, database=":memory:", root_name="storage"):
    """A FileService that takes ownership into ``tmp_path/storage``."""
    repo = FileRepository(initialize(database))
    service = FileService(repo, FileStorage(tmp_path / root_name))
    return repo, service, tmp_path / root_name


def force_streaming_copy(monkeypatch):
    """Make the transfer believe source and destination are on different devices."""
    monkeypatch.setattr("app.services.storage._same_device", lambda *_: False)


class GuardedReader:
    """Wraps a reader and fails on any whole-file (unbounded) read.

    Used to prove the streaming copy reads fixed-size chunks: a ``read()`` with
    no size, a negative size, or one larger than the chunk size raises.
    """

    def __init__(self, handle, recorded: list[int], max_chunk: int = CHUNK_SIZE):
        self._handle = handle
        self._recorded = recorded
        self._max_chunk = max_chunk

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0 or size > self._max_chunk:
            raise AssertionError(f"whole-file read attempted: read({size!r})")
        data = self._handle.read(size)
        self._recorded.append(len(data))
        return data

    def __enter__(self) -> "GuardedReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self._handle.close()

    def close(self) -> None:
        self._handle.close()

    def fileno(self) -> int:
        return self._handle.fileno()


def guard_reads(monkeypatch, target: pathlib.Path, recorded: list[int]) -> None:
    real_open = pathlib.Path.open

    def guarded(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if "r" in mode and self.resolve() == target.resolve():
            return GuardedReader(handle, recorded)
        return handle

    monkeypatch.setattr(pathlib.Path, "open", guarded)


def deny_open(monkeypatch, target: pathlib.Path) -> None:
    """Make ``Path.open`` raise PermissionError for ``target`` only."""
    real_open = pathlib.Path.open

    def guarded(self, *args, **kwargs):
        if self.resolve() == target.resolve():
            raise PermissionError(f"simulated denial: {self}")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", guarded)


# ── configuration ──────────────────────────────────────────────────────────

def test_file_storage_dir_defaults_beside_the_database():
    settings = Settings()
    assert settings.file_storage_dir.replace("\\", "/").endswith("data/storage")
    assert settings.file_storage_dir != settings.telegram_bot_work_dir


def test_file_storage_dir_env_is_honored(monkeypatch, tmp_path):
    monkeypatch.setenv("FILE_STORAGE_DIR", "var/owned")
    settings = Settings()
    assert settings.file_storage_dir.replace("\\", "/").endswith("var/owned")
    monkeypatch.setenv("FILE_STORAGE_DIR", str(tmp_path))
    assert Settings().file_storage_dir == str(tmp_path)


def test_file_storage_dir_must_differ_from_telegram_work_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("FILE_STORAGE_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="FILE_STORAGE_DIR"):
        Settings()


# ── safe filenames ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("report.pdf", "report.pdf"),
        ("../../evil.txt", "evil.txt"),
        ("..\\..\\evil.txt", "evil.txt"),
        ("/etc/passwd", "passwd"),
        ("C:/windows/system32/x.dll", "x.dll"),
        ("", "file"),
        (None, "file"),
        ("...", "file"),
        ("..", "file"),
        ("a\x00b.txt", "ab.txt"),
        ("a\nb.txt", "ab.txt"),
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),   # Windows treats "con.txt" as the CON device
        ("console.txt", "console.txt"),
    ],
)
def test_safe_file_name_sanitises_remote_names(raw, expected):
    assert safe_file_name(raw) == expected


# ── A/B/C/D: successful transfer ───────────────────────────────────────────

def test_adopt_moves_file_into_owned_storage(tmp_path):
    telegram = tmp_path / "telegram_bot_data"
    source = make_source(telegram)
    storage = FileStorage(tmp_path / "storage")

    owned = storage.adopt(source, public_id="AbCdEfGh", file_name="note.txt")

    # N. the returned path is inside FileToLink storage, keyed by public id.
    assert pathlib.Path(owned) == tmp_path / "storage" / "AbCdEfGh" / "note.txt"
    assert storage.owns(owned)
    # B. the destination exists and is a regular file ...
    assert pathlib.Path(owned).is_file()
    # C. ... with exactly the source's contents.
    assert pathlib.Path(owned).read_bytes() == CONTENT
    # D. the Telegram-managed source is gone (a move, not a duplicate).
    assert not source.exists()


def test_adopt_uses_an_atomic_move_on_the_same_device(tmp_path, monkeypatch):
    """No streaming copy happens when a plain move is possible."""

    def no_copy(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("a copy was made even though a move was possible")

    monkeypatch.setattr("app.services.storage.shutil.copyfileobj", no_copy)
    source = make_source(tmp_path / "telegram_bot_data")
    storage = FileStorage(tmp_path / "storage")

    owned = storage.adopt(source, public_id="AbCdEfGh", file_name="note.txt")

    assert pathlib.Path(owned).read_bytes() == CONTENT
    assert not source.exists()


def test_adopt_streams_a_copy_when_a_move_is_impossible(tmp_path, monkeypatch):
    force_streaming_copy(monkeypatch)
    source = make_source(tmp_path / "telegram_bot_data")
    storage = FileStorage(tmp_path / "storage")

    owned = storage.adopt(source, public_id="AbCdEfGh", file_name="note.txt")

    assert pathlib.Path(owned).read_bytes() == CONTENT
    # The source is removed only after the copy is written and verified.
    assert not source.exists()
    assert storage_files(storage.root) == [os.path.join("AbCdEfGh", "note.txt")]


def test_adopt_creates_the_storage_directory_lazily(tmp_path):
    storage = FileStorage(tmp_path / "storage")
    assert not storage.root.exists()
    source = make_source(tmp_path / "telegram_bot_data")
    storage.adopt(source, public_id="AbCdEfGh", file_name="note.txt")
    assert (storage.root / "AbCdEfGh").is_dir()


def test_adopt_is_idempotent_for_an_already_owned_file(tmp_path):
    storage = FileStorage(tmp_path / "storage")
    owned = storage.adopt(
        make_source(tmp_path / "telegram_bot_data"),
        public_id="AbCdEfGh",
        file_name="note.txt",
    )
    # Adopting the owned file again returns it untouched (never deletes it).
    again = storage.adopt(owned, public_id="AbCdEfGh", file_name="note.txt")
    assert again == owned
    assert pathlib.Path(again).read_bytes() == CONTENT


def test_adopt_rejects_an_invalid_public_id(tmp_path):
    storage = FileStorage(tmp_path / "storage")
    source = make_source(tmp_path / "telegram_bot_data")
    with pytest.raises(StorageError):
        storage.adopt(source, public_id="../../escape", file_name="note.txt")
    assert source.exists()


# ── E/F: failure handling ──────────────────────────────────────────────────

def test_failed_transfer_keeps_the_source(tmp_path, monkeypatch):
    force_streaming_copy(monkeypatch)
    source = make_source(tmp_path / "telegram_bot_data")
    storage = FileStorage(tmp_path / "storage")

    def boom(src, dst, length=0):
        dst.write(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr("app.services.storage.shutil.copyfileobj", boom)

    with pytest.raises(StorageError):
        storage.adopt(source, public_id="AbCdEfGh", file_name="note.txt")

    # E. the source is untouched (no ownership transfer completed) ...
    assert source.read_bytes() == CONTENT


def test_failed_transfer_cleans_up_every_partial_destination(tmp_path, monkeypatch):
    force_streaming_copy(monkeypatch)
    source = make_source(tmp_path / "telegram_bot_data")
    storage = FileStorage(tmp_path / "storage")

    def boom(src, dst, length=0):
        dst.write(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr("app.services.storage.shutil.copyfileobj", boom)

    with pytest.raises(StorageError):
        storage.adopt(source, public_id="AbCdEfGh", file_name="note.txt")

    # F. no destination file, no .part leftover, no empty link directory.
    assert not (storage.root / "AbCdEfGh").exists()
    assert storage_files(storage.root) == []
    assert list(storage.root.rglob(f"*{PART_SUFFIX}")) == []


def test_copy_size_mismatch_removes_the_destination(tmp_path, monkeypatch):
    """A truncated copy must never become the served file."""
    force_streaming_copy(monkeypatch)
    source = make_source(tmp_path / "telegram_bot_data")
    storage = FileStorage(tmp_path / "storage")

    def truncating(src, dst, length=0):
        data = src.read(4)  # deliberately short
        dst.write(data)

    monkeypatch.setattr("app.services.storage.shutil.copyfileobj", truncating)
    with pytest.raises(StorageError):
        storage.adopt(source, public_id="AbCdEfGh", file_name="note.txt")
    assert storage_files(storage.root) == []
    assert source.read_bytes() == CONTENT


# ── G/H: invalid sources ───────────────────────────────────────────────────

def test_adopt_rejects_an_unreadable_source(tmp_path, monkeypatch):
    source = make_source(tmp_path / "telegram_bot_data")
    deny_open(monkeypatch, source)
    storage = FileStorage(tmp_path / "storage")

    with pytest.raises(StorageError):
        storage.adopt(source, public_id="AbCdEfGh", file_name="note.txt")
    assert source.exists()  # the source is never removed when adoption fails
    assert storage_files(storage.root) == []


def test_adopt_rejects_a_directory(tmp_path):
    directory = tmp_path / "telegram_bot_data"
    directory.mkdir()
    storage = FileStorage(tmp_path / "storage")

    with pytest.raises(StorageError):
        storage.adopt(directory, public_id="AbCdEfGh", file_name="note.txt")
    assert storage_files(storage.root) == []


def test_adopt_rejects_a_missing_source(tmp_path):
    storage = FileStorage(tmp_path / "storage")
    with pytest.raises(StorageError):
        storage.adopt(
            tmp_path / "gone.mkv", public_id="AbCdEfGh", file_name="movie.mkv"
        )
    assert storage_files(storage.root) == []


# ── I/J: names and traversal ───────────────────────────────────────────────

def test_duplicate_file_names_do_not_overwrite_each_other(tmp_path):
    storage = FileStorage(tmp_path / "storage")
    telegram = tmp_path / "telegram_bot_data"
    first = make_source(telegram, "note.txt", b"first\n")
    first_owned = storage.adopt(first, public_id="AbCdEfGh", file_name="note.txt")

    second = make_source(telegram, "note.txt", b"second\n")
    second_owned = storage.adopt(second, public_id="AbCdEfGh", file_name="note.txt")

    assert pathlib.Path(first_owned).name == "note.txt"
    assert pathlib.Path(second_owned).name == "note-1.txt"
    assert pathlib.Path(first_owned).read_bytes() == b"first\n"
    assert pathlib.Path(second_owned).read_bytes() == b"second\n"


def test_adopt_keeps_a_traversing_filename_inside_storage(tmp_path):
    storage = FileStorage(tmp_path / "storage")
    source = make_source(tmp_path / "telegram_bot_data")

    owned = storage.adopt(
        source, public_id="AbCdEfGh", file_name="../../../../etc/passwd"
    )

    owned = pathlib.Path(owned).resolve()
    assert owned.parent == (storage.root / "AbCdEfGh").resolve()
    assert owned.name == "passwd"
    assert storage.owns(owned)          # never escaped the storage root


# ── K: large files are streamed, never loaded into RAM ─────────────────────

def test_large_copy_is_streamed_in_bounded_chunks(tmp_path, monkeypatch):
    force_streaming_copy(monkeypatch)
    size = 3 * CHUNK_SIZE + 123  # ~3 MiB: enough chunks to prove streaming
    payload = bytes((i * 31 + 7) % 256 for i in range(4096))
    content = (payload * ((size // len(payload)) + 1))[:size]
    source = make_source(tmp_path / "telegram_bot_data", "big.mkv", content)

    reads: list[int] = []
    guard_reads(monkeypatch, source, reads)

    storage = FileStorage(tmp_path / "storage")
    owned = storage.adopt(source, public_id="AbCdEfGh", file_name="big.mkv")

    assert pathlib.Path(owned).stat().st_size == size
    assert (
        hashlib.sha256(pathlib.Path(owned).read_bytes()).digest()
        == hashlib.sha256(content).digest()
    )
    assert len(reads) >= 4                    # streamed, in several chunks
    assert max(reads) <= CHUNK_SIZE           # never a whole-file read
    assert sum(reads) == size                 # every byte read exactly once
    assert not source.exists()


# ── cleanup ────────────────────────────────────────────────────────────────

def test_remove_owned_deletes_the_file_and_its_directory(tmp_path):
    storage = FileStorage(tmp_path / "storage")
    owned = storage.adopt(
        make_source(tmp_path / "telegram_bot_data"),
        public_id="AbCdEfGh",
        file_name="note.txt",
    )
    assert storage.remove_owned(owned) is True
    assert not pathlib.Path(owned).exists()
    assert not (storage.root / "AbCdEfGh").exists()  # empty dir pruned
    assert storage.root.exists()                      # root kept
    assert storage.remove_owned(owned) is True         # idempotent


def test_remove_owned_never_touches_paths_outside_storage(tmp_path):
    storage = FileStorage(tmp_path / "storage")
    storage.root.mkdir(parents=True)
    telegram_file = make_source(tmp_path / "telegram_bot_data")
    assert storage.remove_owned(telegram_file) is False
    assert telegram_file.exists()
    assert storage.remove_owned(storage.root) is False  # never the root
    assert storage.root.exists()


def test_remove_owned_reports_failure_without_raising(tmp_path, monkeypatch):
    storage = FileStorage(tmp_path / "storage")
    owned = storage.adopt(
        make_source(tmp_path / "telegram_bot_data"),
        public_id="AbCdEfGh",
        file_name="note.txt",
    )
    real_unlink = pathlib.Path.unlink

    def guarded(self, *args, **kwargs):
        if self.resolve() == pathlib.Path(owned).resolve():
            raise PermissionError("simulated lock")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", guarded)
    # Cleanup failures are logged and reported, never raised at the caller.
    assert storage.remove_owned(owned) is False


# ── service layer: ownership before the record ─────────────────────────────

def test_save_document_takes_ownership_and_records_the_owned_path(tmp_path):
    repo, service, root = owning_service(tmp_path)
    telegram = tmp_path / "telegram_bot_data"
    source = make_source(telegram)

    record = service.save_document(make_message(str(source)))

    # N. source_path points inside FileToLink storage ...
    assert pathlib.Path(record.source_path).resolve() == (
        root / record.public_id / "note.txt"
    ).resolve()
    # ... and the Telegram-managed file has been taken over (not duplicated).
    assert not source.exists()
    assert pathlib.Path(record.source_path).read_bytes() == CONTENT
    assert repo.get_by_public_id(record.public_id).source_path == record.source_path


def test_save_document_preserves_all_metadata_when_owning(tmp_path):
    _repo, service, _root = owning_service(tmp_path)
    source = make_source(tmp_path / "telegram_bot_data")

    record = service.save_document(make_message(str(source)))

    assert record.public_id is not None and len(record.public_id) == 8
    assert record.telegram_file_id == "FILE1"
    assert record.telegram_message_id == 7
    assert record.user_id == 42
    assert record.file_name == "note.txt"
    assert record.file_size == len(CONTENT)
    assert record.mime_type == "text/plain"
    assert record.created_at == 1_700_000_000
    assert record.expires_at is None
    assert record.max_downloads is None
    assert record.status == "active"
    assert record.download_count == 0
    assert record.source_path


def test_save_document_keeps_expiration_and_download_limit(tmp_path):
    _repo, service, _root = owning_service(tmp_path)
    source = make_source(tmp_path / "telegram_bot_data")

    record = service.save_document(
        make_message(str(source)), expires_at=2_000_000_000, max_downloads=3
    )

    assert record.expires_at == 2_000_000_000
    assert record.max_downloads == 3


def test_save_document_without_storage_records_the_given_path(tmp_path):
    """Mock mode is unchanged: the operator's own path is recorded as-is."""
    path = make_source(tmp_path)
    repo = FileRepository(initialize(":memory:"))
    service = FileService(repo)

    record = service.save_document(make_message(str(path)))

    assert record.source_path == str(path)
    assert path.exists()


def test_save_document_requires_a_source_when_owning(tmp_path):
    repo, service, root = owning_service(tmp_path)
    with pytest.raises(StorageError):
        service.save_document(make_message(None))
    assert repo.get_by_id(1) is None
    assert storage_files(root) == []


def test_failed_transfer_creates_no_record_and_no_public_id(tmp_path):
    """M. A file that cannot be adopted must not become an active link."""
    repo, service, root = owning_service(tmp_path)
    directory = tmp_path / "telegram_bot_data"
    directory.mkdir()

    with pytest.raises(StorageError):
        service.save_document(make_message(str(directory)))

    assert repo.get_by_id(1) is None
    assert repo.get_by_public_id("AbCdEfGh") is None
    assert storage_files(root) == []


def test_database_failure_after_transfer_rolls_the_file_back(tmp_path, monkeypatch):
    """L. Transfer ok + INSERT fails ⇒ no orphan file, no active record."""
    repo, service, root = owning_service(tmp_path)
    source = make_source(tmp_path / "telegram_bot_data")

    def boom(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "create", boom)

    with pytest.raises(sqlite3.OperationalError):
        service.save_document(make_message(str(source)))

    assert repo.get_by_id(1) is None
    assert storage_files(root) == []  # the adopted file was rolled back


def test_public_id_collision_rekeys_the_owned_file(tmp_path, monkeypatch):
    repo, service, root = owning_service(tmp_path)
    source = make_source(tmp_path / "telegram_bot_data")

    ids = iter(["COLLIDES", "FRESH123"])
    monkeypatch.setattr("app.services.files.generate_public_id", lambda: next(ids))

    real_create = repo.create
    calls = {"n": 0}

    def flaky_create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.IntegrityError("UNIQUE constraint failed: files.public_id")
        return real_create(**kwargs)

    monkeypatch.setattr(repo, "create", flaky_create)

    record = service.save_document(make_message(str(source)))

    assert record.public_id == "FRESH123"
    assert pathlib.Path(record.source_path).parent.name == "FRESH123"
    assert pathlib.Path(record.source_path).read_bytes() == CONTENT
    assert not (root / "COLLIDES").exists()  # the orphan key is gone
    assert not source.exists()               # the file itself was never lost
    assert calls["n"] == 2


def test_make_file_service_can_be_built_with_storage(tmp_path):
    service = make_file_service(":memory:", FileStorage(tmp_path / "storage"))
    assert service.storage is not None


# ── S: delete removes the owned file, never a Telegram one ─────────────────

def test_delete_removes_the_owned_file(tmp_path):
    repo, service, root = owning_service(tmp_path)
    record = service.save_document(
        make_message(str(make_source(tmp_path / "telegram_bot_data")))
    )

    deleted = service.delete_link(record.public_id)

    assert deleted.status == "deleted"
    assert not pathlib.Path(record.source_path).exists()
    assert storage_files(root) == []
    assert repo.get_by_public_id(record.public_id).status == "deleted"


def test_delete_is_idempotent_with_storage(tmp_path):
    _repo, service, _root = owning_service(tmp_path)
    record = service.save_document(
        make_message(str(make_source(tmp_path / "telegram_bot_data")))
    )
    assert service.delete_link(record.public_id).status == "deleted"
    assert service.delete_link(record.public_id).status == "deleted"


def test_delete_without_storage_leaves_the_operators_file_alone(tmp_path):
    path = make_source(tmp_path)
    repo = FileRepository(initialize(":memory:"))
    service = FileService(repo)
    record = service.save_document(make_message(str(path)))

    service.delete_link(record.public_id)

    assert path.exists()


def test_delete_cleanup_failure_still_deletes_the_link(tmp_path, monkeypatch):
    repo, service, root = owning_service(tmp_path)
    record = service.save_document(
        make_message(str(make_source(tmp_path / "telegram_bot_data")))
    )
    real_unlink = pathlib.Path.unlink

    def guarded(self, *args, **kwargs):
        if self.resolve() == pathlib.Path(record.source_path).resolve():
            raise PermissionError("simulated lock")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", guarded)

    reply = handle_update(
        Update(
            update_id=1,
            message=Message(
                message_id=1, text=f"/delete {record.public_id}", user=USER
            ),
        ),
        file_service=service,
    )

    # A filesystem failure must not turn a successful soft deletion into an error.
    assert "Link deleted" in reply
    assert str(tmp_path) not in reply            # never expose a path
    assert repo.get_by_public_id(record.public_id).status == "deleted"
    assert pathlib.Path(record.source_path).exists()  # left for diagnosis
    assert storage_files(root) == [os.path.join(record.public_id, "note.txt")]


# ── file source: FileToLink storage containment ────────────────────────────

def _record(source_path: str | None):
    from app.database.records import FileRecord

    return FileRecord(
        id=1, public_id="AbCdEfGh", telegram_file_id="F1", telegram_message_id=7,
        user_id=42, file_name="note.txt", file_size=len(CONTENT),
        mime_type="text/plain", created_at=1_700_000_000, expires_at=None,
        download_count=0, max_downloads=None, status="active",
        source_path=source_path,
    )


def test_storage_source_serves_owned_files(tmp_path):
    storage = FileStorage(tmp_path / "storage")
    owned = storage.adopt(
        make_source(tmp_path / "telegram_bot_data"),
        public_id="AbCdEfGh",
        file_name="note.txt",
    )
    with StorageFileSource(storage.root).open(_record(owned)) as fh:
        assert fh.read() == CONTENT


def test_storage_source_rejects_paths_outside_storage(tmp_path):
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    outside = make_source(tmp_path / "telegram_bot_data")
    source = StorageFileSource(storage_root)
    for path in (str(outside), str(storage_root / ".." / "telegram_bot_data" / "note.txt")):
        with pytest.raises(FileSourceError):
            source.open(_record(path))
    with pytest.raises(FileSourceError):
        source.open(_record(None))


# ── Telegram ingestion order ───────────────────────────────────────────────

def test_telegram_flow_records_the_owned_path(tmp_path):
    repo, service, root = owning_service(tmp_path)
    telegram = tmp_path / "telegram_bot_data"
    source = make_source(telegram, "movie.mkv", b"\x00" * 2048)

    bot = FakeBot(local_path=str(source))
    update = bind(
        make_ptb_update(
            make_ptb_message(document=make_ptb_document(file_name="movie.mkv"))
        ),
        bot,
    )

    run_async(handle_message_update(update, make_context(bot), service))

    reply = bot.sent[0]
    assert "File received" in reply and "/f/" in reply
    assert str(tmp_path) not in reply              # never a filesystem path

    record = repo.get_by_id(1)
    assert record.source_path.startswith(str(root))
    assert root in pathlib.Path(record.source_path).parents
    assert not source.exists()                      # taken over from Telegram
    assert pathlib.Path(record.source_path).read_bytes() == b"\x00" * 2048


def test_telegram_flow_transfer_failure_replies_error_and_stores_nothing(tmp_path):
    """M/N. A failed transfer ⇒ no record, no public id, no download URL."""
    repo, service, root = owning_service(tmp_path)
    telegram = tmp_path / "telegram_bot_data"
    telegram.mkdir()  # a directory: not a regular file, so adoption must fail

    bot = FakeBot(local_path=str(telegram))
    update = bind(
        make_ptb_update(make_ptb_message(document=make_ptb_document())), bot
    )

    run_async(handle_message_update(update, make_context(bot), service))

    assert bot.sent and "❌" in bot.sent[0]
    assert "/f/" not in bot.sent[0]                 # no successful download URL
    assert str(tmp_path) not in bot.sent[0]
    assert repo.get_by_id(1) is None
    assert storage_files(root) == []


def test_telegram_flow_database_failure_replies_error_and_leaves_no_orphan(
    tmp_path, monkeypatch
):
    """L. Transfer ok + INSERT fails at the transport level."""
    repo, service, root = owning_service(tmp_path)
    source = make_source(tmp_path / "telegram_bot_data")

    def boom(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "create", boom)

    bot = FakeBot(local_path=str(source))
    update = bind(
        make_ptb_update(make_ptb_message(document=make_ptb_document())), bot
    )

    run_async(handle_message_update(update, make_context(bot), service))

    assert bot.sent and "❌" in bot.sent[0]
    assert "/f/" not in bot.sent[0]
    assert repo.get_by_id(1) is None
    assert storage_files(root) == []


# ── O/P/Q/R/S/T: the existing endpoints on owned files ─────────────────────

def _real_settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("BOT_MODE", "real")
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("FILE_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    return Settings()


def test_real_mode_api_serves_owned_files(tmp_path, monkeypatch):
    settings = _real_settings(tmp_path, monkeypatch)
    repo, service, root = owning_service(tmp_path, root_name="storage")
    record = service.save_document(
        make_message(str(make_source(tmp_path / "telegram_bot_data")))
    )
    client = TestClient(create_app(repository=repo, settings=settings))
    url = f"/f/{record.public_id}"

    get = client.get(url)                                # O. download endpoint
    assert get.status_code == 200 and get.content == CONTENT
    assert get.headers["accept-ranges"] == "bytes"

    part = client.get(url, headers={"Range": "bytes=0-4"})   # P. Range endpoint
    assert part.status_code == 206 and part.content == CONTENT[:5]
    assert part.headers["content-range"] == f"bytes 0-4/{len(CONTENT)}"

    head = client.head(url)                              # Q. HEAD endpoint
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == str(len(CONTENT))

    assert service.get_link(record.public_id) is not None  # R. still resolvable
    assert root.exists()


def test_real_mode_api_does_not_serve_telegram_managed_files(tmp_path, monkeypatch):
    """The API only serves our own storage, never the Local Bot API work dir."""
    settings = _real_settings(tmp_path, monkeypatch)
    telegram = tmp_path / "telegram_bot_data"
    source = make_source(telegram)
    repo = FileRepository(initialize(":memory:"))
    record = FileService(repo).save_document(make_message(str(source)))

    client = TestClient(create_app(repository=repo, settings=settings))
    assert client.get(f"/f/{record.public_id}").status_code == 404


def test_real_mode_lifecycle_and_delete_cleanup(tmp_path, monkeypatch):
    settings = _real_settings(tmp_path, monkeypatch)
    repo, service, root = owning_service(tmp_path, root_name="storage")
    record = service.save_document(
        make_message(str(make_source(tmp_path / "telegram_bot_data"))),
        max_downloads=2,
    )
    client = TestClient(create_app(repository=repo, settings=settings))
    url = f"/f/{record.public_id}"

    info = handle_update(                                 # R. /info
        Update(
            update_id=1,
            message=Message(
                message_id=1, text=f"/info {record.public_id}", user=USER
            ),
        ),
        file_service=service,
    )
    assert "note.txt" in info and "active" in info and url in info
    assert "0 / 2" in info

    assert client.get(url).status_code == 200             # T. first of two
    reply = handle_update(                                # S. /delete
        Update(
            update_id=1,
            message=Message(
                message_id=1, text=f"/delete {record.public_id}", user=USER
            ),
        ),
        file_service=service,
    )
    assert "Link deleted" in reply
    assert client.get(url).status_code == 410             # no longer downloadable
    assert client.head(url).status_code == 410
    assert not pathlib.Path(record.source_path).exists()  # owned file removed
    assert storage_files(root) == []


def test_expired_owned_link_is_not_served_and_keeps_its_file_for_cleanup(
    tmp_path, monkeypatch
):
    settings = _real_settings(tmp_path, monkeypatch)
    repo, service, _root = owning_service(tmp_path, root_name="storage")
    record = service.save_document(
        make_message(str(make_source(tmp_path / "telegram_bot_data"))),
        expires_at=1,           # long past
        max_downloads=1,
    )
    client = TestClient(create_app(repository=repo, settings=settings))

    assert client.get(f"/f/{record.public_id}").status_code == 410
    assert repo.get_by_public_id(record.public_id).status == "expired"
    # Expiration never serves bytes, and cleanup stays an explicit action
    # (no background daemon in this phase): the owned file is still there.
    assert pathlib.Path(record.source_path).exists()
