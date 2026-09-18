"""Phase 12 regression + hardening tests.

Covers the three defects found during the Phase 12 system audit (all fixed):

1. an unreadable source file raised a raw ``OSError`` out of the endpoint
   (HTTP 500) instead of a clean ``404``;
2. ``send <directory>`` in the mock REPL stored a link whose bytes could
   never be served (a dead link);
3. ``HEAD`` diverged from the matching ``GET`` for a missing source (200 vs
   404) and when ``file_size`` was stored as NULL (advertised
   ``Content-Length: 0`` vs the real size).

It also adds the boundary/failure-mode coverage the audit matrix calls for:
zero-byte files, the 64 KB chunk boundary, range boundaries, error-body
leakage, concurrent final-download/rate-limit behaviour, and clean startup
failure on invalid configuration.
"""
import asyncio
import pathlib
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.bot import mock
from app.bot.handlers import handle_update
from app.bot.models import Document, Message, Update, User
from app.database.connection import initialize
from app.database.files import FileRepository
from app.database.records import FileRecord
from app.services.file_sources import (
    FileSourceError,
    LocalFileSource,
    TelegramFileSource,
)
from app.services.files import FileService, make_file_service
from app.utils.ratelimit import RateLimiter

USER = User(id=42, username="alice", first_name="Alice")
OTHER_USER = User(id=999, username="mallory", first_name="Mallory")
CHUNK = 64 * 1024


def _record(source_path: str | None, *, file_size: int | None = 10) -> FileRecord:
    return FileRecord(
        id=1, public_id="AbCdEfGh", telegram_file_id="FILE1",
        telegram_message_id=7, user_id=42, file_name="note.txt",
        file_size=file_size, mime_type="text/plain",
        created_at=1_700_000_000, expires_at=None, download_count=0,
        max_downloads=None, status="active", source_path=source_path,
    )


def _store(tmp_path, content=b"abcdefghij", *, name="f.bin", size=None, mime="text/plain"):
    """Store a real file; return (repo, public_id, path)."""
    path = tmp_path / name
    path.write_bytes(content)
    repo = FileRepository(initialize(":memory:"))
    repo.create(
        telegram_file_id="FILE1", telegram_message_id=7, user_id=42,
        file_name=name, file_size=(len(content) if size is None else size),
        mime_type=mime, created_at=1_700_000_000, source_path=str(path),
    )
    repo.update(1, public_id="AbCdEfGh")
    return repo, "AbCdEfGh", path


def _client(repo) -> TestClient:
    return TestClient(create_app(repository=repo, file_source=LocalFileSource()))


# ── Fix 1: unreadable source → 404, never an unhandled OSError ─────────────


def _deny_open(monkeypatch, target: pathlib.Path) -> None:
    """Make ``Path.open`` raise PermissionError for ``target`` only."""
    real_open = pathlib.Path.open

    def guarded(self, *args, **kwargs):
        if self.resolve() == target.resolve():
            raise PermissionError(f"simulated denial: {self}")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", guarded)


def test_local_file_source_wraps_os_error_as_file_source_error(monkeypatch, tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"x")
    _deny_open(monkeypatch, path)
    with pytest.raises(FileSourceError):
        LocalFileSource().open(_record(str(path)))


def test_telegram_file_source_wraps_os_error_as_file_source_error(monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    path = work / "in.txt"
    path.write_bytes(b"x")
    _deny_open(monkeypatch, path)
    with pytest.raises(FileSourceError):
        TelegramFileSource(work).open(_record(str(path)))


def test_unreadable_source_returns_404_on_get_and_head(monkeypatch, tmp_path):
    """Before the fix this raised PermissionError out of the endpoint (500)."""
    repo, pid, path = _store(tmp_path)
    _deny_open(monkeypatch, path)
    client = _client(repo)
    assert client.get(f"/f/{pid}").status_code == 404
    assert client.head(f"/f/{pid}").status_code == 404


# ── Fix 2: mock send of a directory must not create a dead link ────────────


def test_local_file_document_rejects_directory(tmp_path):
    with pytest.raises(IsADirectoryError):
        mock.local_file_document(str(tmp_path))


def test_send_directory_is_rejected_and_not_stored(tmp_path):
    service = make_file_service(":memory:")
    with pytest.raises(OSError):
        mock.send_local_file(str(tmp_path), file_service=service)
    assert service.repository.get_by_id(1) is None


def test_send_missing_path_still_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        mock.send_local_file(str(tmp_path / "nope.txt"), file_service=make_file_service(":memory:"))


# ── Fix 3: HEAD mirrors GET ────────────────────────────────────────────────


def test_head_missing_source_returns_404_like_get(tmp_path):
    repo = FileRepository(initialize(":memory:"))
    repo.create(telegram_file_id="FILE1", telegram_message_id=7, user_id=42,
                file_name="note.txt", file_size=10, mime_type="text/plain",
                created_at=1_700_000_000, source_path=str(tmp_path / "gone.txt"))
    repo.update(1, public_id="AbCdEfGh")
    client = _client(repo)
    assert client.get("/f/AbCdEfGh").status_code == 404
    assert client.head("/f/AbCdEfGh").status_code == 404


def test_head_content_length_matches_get_when_file_size_is_null(tmp_path):
    repo, pid, _path = _store(tmp_path, content=b"0123456789", size=10)
    repo.update(1, file_size=None)  # stored size unknown (e.g. Telegram omitted it)
    client = _client(repo)
    get = client.get(f"/f/{pid}")
    head = client.head(f"/f/{pid}")
    assert get.status_code == head.status_code == 200
    assert head.content == b""  # still no body
    assert head.headers["content-length"] == get.headers["content-length"] == "10"


def test_head_missing_source_does_not_consume_a_slot(tmp_path):
    repo = FileRepository(initialize(":memory:"))
    repo.create(telegram_file_id="FILE1", telegram_message_id=7, user_id=42,
                file_name="note.txt", file_size=10, mime_type="text/plain",
                created_at=1_700_000_000, source_path=str(tmp_path / "gone.txt"))
    repo.update(1, public_id="AbCdEfGh", max_downloads=1)
    assert _client(repo).head("/f/AbCdEfGh").status_code == 404
    assert repo.get_by_id(1).download_count == 0


# ── Edge-case matrix: files, ranges, leakage ───────────────────────────────


def test_zero_byte_file_download_and_head(tmp_path):
    repo, pid, _p = _store(tmp_path, content=b"", name="empty.bin", mime="application/octet-stream")
    client = _client(repo)
    get = client.get(f"/f/{pid}")
    head = client.head(f"/f/{pid}")
    assert get.status_code == 200 and get.content == b""
    assert get.headers["content-length"] == "0"
    assert head.status_code == 200 and head.headers["content-length"] == "0"
    # Any range over an empty body is unsatisfiable.
    assert client.get(f"/f/{pid}", headers={"Range": "bytes=0-"}).status_code == 416
    assert client.head(f"/f/{pid}", headers={"Range": "bytes=0-"}).status_code == 416


@pytest.mark.parametrize("size", [1, 1023, CHUNK - 1, CHUNK, CHUNK + 1, 2 * CHUNK + 7])
def test_file_integrity_across_chunk_boundary(size, tmp_path):
    import hashlib

    content = bytes((i * 7 + 3) % 256 for i in range(size))
    repo, pid, _p = _store(tmp_path, content=content, name=f"b{size}.bin",
                           mime="application/octet-stream")
    client = _client(repo)
    full = client.get(f"/f/{pid}")
    assert full.status_code == 200
    assert hashlib.sha256(full.content).digest() == hashlib.sha256(content).digest()

    if size > 1:
        rng = client.get(f"/f/{pid}", headers={"Range": f"bytes=1-{size - 1}"})
        assert rng.status_code == 206
        assert rng.content == content[1:size]


def _range_case(repo, pid, header, size):
    client = _client(repo)
    resp = client.get(f"/f/{pid}", headers={"Range": header})
    return resp.status_code, resp.content


@pytest.mark.parametrize(
    "header, expected_status, expected_slice",
    [
        ("bytes=0-0", 206, (0, 1)),
        ("bytes=0-9", 206, (0, 10)),
        ("bytes=9-9", 206, (9, 10)),
        ("bytes=10-10", 416, None),   # start == N
        ("bytes=11-12", 416, None),   # start > N
        ("bytes=-1", 206, (9, 10)),
        ("bytes=-10", 206, (0, 10)),
        ("bytes=-11", 206, (0, 10)),  # suffix longer than the file clamps
        ("bytes=0-", 206, (0, 10)),
        ("bytes=9-", 206, (9, 10)),
        ("bytes=0-1,3-4", 416, None),  # multi-range unsupported
        ("bytes=abc-def", 416, None),
        ("bytes=99999999999999999999-", 416, None),
    ],
)
def test_range_boundary_matrix(header, expected_status, expected_slice, tmp_path):
    content = b"0123456789"
    repo, pid, _p = _store(tmp_path, content=content, name="ten.bin",
                           mime="application/octet-stream")
    status, body = _range_case(repo, pid, header, len(content))
    assert status == expected_status
    if expected_slice is not None:
        assert body == content[expected_slice[0]:expected_slice[1]]


def test_security_headers_present_on_missing_source_404(tmp_path):
    """Phase 11 claimed headers on every response; this path used to omit them."""
    repo = FileRepository(initialize(":memory:"))
    repo.create(telegram_file_id="FILE1", telegram_message_id=7, user_id=42,
                file_name="note.txt", file_size=10, mime_type="text/plain",
                created_at=1_700_000_000, source_path=str(tmp_path / "gone.txt"))
    repo.update(1, public_id="AbCdEfGh")
    client = _client(repo)
    for resp in (client.get("/f/AbCdEfGh"), client.head("/f/AbCdEfGh")):
        assert resp.status_code == 404
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in resp.headers["content-security-policy"]
        assert resp.headers["referrer-policy"] == "no-referrer"


def test_error_responses_do_not_leak_internal_details(tmp_path):
    repo = FileRepository(initialize(":memory:"))
    repo.create(telegram_file_id="SECRETFILE", telegram_message_id=7, user_id=42,
                file_name="note.txt", file_size=10, mime_type="text/plain",
                created_at=1_700_000_000, source_path=str(tmp_path / "missing.txt"))
    repo.update(1, public_id="AbCdEfGh")
    client = _client(repo)

    bodies = [
        client.get("/f/AbCdEfGh").text,                 # 404 (missing source)
        client.get("/f/doesNotExist").text,             # 404 (unknown)
        client.get("/f/AbCdEfGh", headers={"Range": "bytes=9-9"}).text,  # 416
    ]
    repo.set_status(1, "deleted")
    bodies.append(client.get("/f/AbCdEfGh").text)       # 410

    for body in bodies:
        assert str(tmp_path) not in body          # no filesystem path
        assert "SECRETFILE" not in body           # no telegram file id
        assert "Traceback" not in body            # no stack trace
        assert "note.txt" not in body             # no metadata

    # The errors above all still carry the security headers.
    assert _client(repo).get("/f/doesNotExist").headers["x-content-type-options"] == "nosniff"


# ── Concurrency ────────────────────────────────────────────────────────────


def _parallel_get(app, pid, count, client_ip=None):
    async def run():
        kwargs = {"app": app}
        if client_ip is not None:
            kwargs["client"] = (client_ip, 1234)
        transport = httpx.ASGITransport(**kwargs)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(*(client.get(f"/f/{pid}") for _ in range(count)))
        return [r.status_code for r in responses]

    return asyncio.run(run())


def test_simultaneous_final_download_attempts_serve_exactly_one(tmp_path):
    repo, pid, _p = _store(tmp_path, content=b"x" * 100, name="final.bin")
    repo.update(1, max_downloads=1)
    statuses = _parallel_get(create_app(repository=repo, file_source=LocalFileSource()), pid, 20)
    assert statuses.count(200) == 1
    assert statuses.count(410) == 19
    assert set(statuses) <= {200, 410}
    assert repo.get_by_id(1).download_count == 1


def test_concurrent_rate_limited_requests_allow_exactly_the_limit(tmp_path):
    repo, pid, _p = _store(tmp_path)
    app = create_app(
        repository=repo,
        file_source=LocalFileSource(),
        rate_limiter=RateLimiter(limit=5, window_seconds=60, max_keys=100),
    )
    statuses = _parallel_get(app, pid, 20)
    assert statuses.count(200) == 5
    assert statuses.count(429) == 15


def test_concurrent_repeated_deletion_is_safe(tmp_path):
    repo, pid, _p = _store(tmp_path)
    service = FileService(repo)

    def delete():
        return handle_update(
            Update(update_id=1, message=Message(message_id=1, text=f"/delete {pid}", user=USER)),
            file_service=service,
        )

    import threading

    replies: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        reply = delete()
        with lock:
            replies.append(reply)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The link ends deleted exactly once and never resurrects. Deletion stays
    # idempotent under the race: every caller gets a valid outcome (more than
    # one may report success, all reporting the same terminal state) and none
    # reveals private metadata or a cross-user error.
    assert repo.get_by_id(1).status == "deleted"
    assert len(replies) == 8
    assert all(("Link deleted" in r) or ("already deleted" in r.lower()) for r in replies)
    assert all("note.txt" not in r and "FILE1" not in r for r in replies)


# ── Configuration / startup ────────────────────────────────────────────────


def test_run_main_exits_cleanly_on_invalid_config(monkeypatch, capsys):
    import run as run_module
    from app.config import get_settings

    monkeypatch.setenv("BOT_MODE", "bogus")
    monkeypatch.setattr(sys, "argv", ["run.py", "--api"])
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as excinfo:
            run_module.main()
        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "Invalid configuration" in out
        assert "bogus" in out
        assert "BOT_TOKEN" not in out
    finally:
        get_settings.cache_clear()


def test_mock_mode_works_without_secrets(monkeypatch, tmp_path):
    from app.config import Settings

    for name in ("BOT_TOKEN", "TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.bot_mode == "mock"
    assert settings.is_mock
