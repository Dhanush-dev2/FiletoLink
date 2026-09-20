"""Phase 17: performance-regression properties (deterministic, no speed numbers).

Machine-specific MB/s numbers do not belong in the test suite. These tests
pin the structural properties the Phase 17 measurements relied on:

- a full download streams in bounded chunks: server-side Python memory
  during the response stays far below the file size (measured with
  ``tracemalloc`` while driving the ASGI app directly and discarding body
  chunks as they arrive — a normal HTTP client would buffer the whole body
  on its side and mask the measurement);
- a ``Range`` response reads only the requested bytes from the file — never
  the whole file (veri

fied with a byte-counting file source);
- ``HEAD`` resolves metadata without reading any file bytes at all.

All three run against the real ASGI app and storage source, so a regression
toward whole-file buffering or eager reads would fail them.
"""
import asyncio
import hashlib
import os
import tracemalloc

import httpx

from app.api.app import create_app
from app.bot.models import Document, Message, User
from app.database.connection import initialize
from app.database.files import FileRepository
from app.services.file_sources import LocalFileSource
from app.services.files import FileService

SIZE = 8 * 1024 * 1024  # 8 MiB — large enough that buffering would show


def _store(tmp_path, content: bytes):
    path = tmp_path / "big.bin"
    path.write_bytes(content)
    repo = FileRepository(initialize(":memory:"))
    message = Message(
        message_id=7,
        user=User(id=42, username="alice", first_name="Alice"),
        document=Document(
            file_id="FILE1",
            file_name="big.bin",
            file_size=len(content),
            mime_type="application/octet-stream",
        ),
        date=1_700_000_000,
        source_path=str(path),
    )
    record = FileService(repo).save_document(message)
    return repo, record.public_id


# ── full download: bounded memory ───────────────────────────────────────────


def test_full_download_memory_stays_bounded(tmp_path):
    """The endpoint must stream, never buffer the file whole.

    The ASGI app is driven directly: every ``http.response.body`` chunk is
    hashed and immediately discarded, so the only memory that can accumulate
    inside the traced region is the application's own. tracemalloc traces
    all threads of the process, including FastAPI's threadpool where the
    sync endpoint runs. Buffering the whole 8 MiB file (or even a sizeable
    fraction of it) would blow the bound.
    """
    content = os.urandom(SIZE)
    expected = hashlib.sha256(content).hexdigest()
    repo, pid = _store(tmp_path, content)
    app = create_app(repository=repo, file_source=LocalFileSource())

    async def run():
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/f/{pid}",
            "raw_path": f"/f/{pid}".encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        request_sent = False

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            # Never report a disconnect (that would cancel the streaming
            # response, like a client hanging up): block until the framework
            # cancels this listener after the response completes.
            await asyncio.Event().wait()

        state = {"status": None, "received": 0, "hasher": hashlib.sha256()}

        async def send(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                state["received"] += len(body)
                state["hasher"].update(body)  # discard: never accumulate

        tracemalloc.start()
        try:
            await app(scope, receive, send)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return state["status"], state["received"], state["hasher"].hexdigest(), peak

    status, received, digest, peak = asyncio.run(run())

    assert status == 200
    assert received == SIZE
    assert digest == expected
    # Bounded buffers (64 KiB chunks) vs. proportional memory (>= 8 MiB):
    # 1 MiB leaves headroom for a few in-flight chunks yet fails loudly if
    # the file (or a large fraction of it) is ever materialized at once.
    assert peak < SIZE / 8


# ── range / HEAD: never read beyond what is needed ──────────────────────────


class _CountingReader:
    """BinaryIO proxy counting every byte actually read from disk."""

    def __init__(self, stream, counter):
        self._stream = stream
        self._counter = counter

    def read(self, n=-1):
        data = self._stream.read(n)
        self._counter[0] += len(data)
        return data

    def seek(self, *args):
        return self._stream.seek(*args)

    def fileno(self):
        return self._stream.fileno()

    def close(self):
        self._stream.close()


class CountingSource:
    """FileSource wrapper that totals bytes read across all opens."""

    def __init__(self):
        self.inner = LocalFileSource()
        self.bytes_read = [0]

    def open(self, record):
        return _CountingReader(self.inner.open(record), self.bytes_read)


def _request_fn(repo, source):
    app = create_app(repository=repo, file_source=source)
    transport = httpx.ASGITransport(app=app)

    def request(method, url, **kwargs):
        async def go():
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(go())

    return request


def test_range_reads_only_requested_bytes(tmp_path):
    """A middle 1 MiB range must seek and read exactly that window — the
    whole file must never stream through the endpoint."""
    content = os.urandom(SIZE)
    repo, pid = _store(tmp_path, content)
    source = CountingSource()

    start, end = 3_145_728, 4_194_303  # the second middle 1 MiB
    response = _request_fn(
        repo, source
    )("GET", f"/f/{pid}", headers={"Range": f"bytes={start}-{end}"})

    assert response.status_code == 206
    assert response.content == content[start : end + 1]
    assert response.headers["content-range"] == f"bytes {start}-{end}/{SIZE}"
    assert source.bytes_read[0] == end - start + 1  # exactly the range


def test_head_reads_no_file_bytes(tmp_path):
    """HEAD resolves metadata (size via fstat) without reading file bytes."""
    content = os.urandom(SIZE)
    repo, pid = _store(tmp_path, content)
    source = CountingSource()

    response = _request_fn(repo, source)("HEAD", f"/f/{pid}")

    assert response.status_code == 200
    assert response.headers["content-length"] == str(SIZE)
    assert response.content == b""
    assert source.bytes_read[0] == 0
