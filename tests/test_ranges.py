"""Tests for HTTP Range Request support (RFC 7233, single range)."""
import pytest
from fastapi.testclient import TestClient

from app.api.app import CHUNK_SIZE, create_app
from app.bot.models import Document, Message, User
from app.database.connection import initialize
from app.database.files import FileRepository
from app.services.file_sources import (
    LocalFileSource,
    StorageFileSource,
    TelegramFileSource,
)
from app.services.files import FileService
from app.utils.ranges import UnsatisfiableRange, parse_range_header

USER = User(id=42, username="alice", first_name="Alice")

# 10 bytes, makes range assertions obvious: index i holds str(i).
CONTENT = b"0123456789"


def make_message(path: str, file_size: int | None = len(CONTENT)) -> Message:
    return Message(
        message_id=7,
        user=USER,
        document=Document(
            file_id="FILE1",
            file_name="digits.txt",
            file_size=file_size,
            mime_type="text/plain",
        ),
        date=1_700_000_000,
        source_path=path,
    )


@pytest.fixture
def stored(tmp_path):
    path = tmp_path / "digits.txt"
    path.write_bytes(CONTENT)
    repo = FileRepository(initialize(":memory:"))
    service = FileService(repo)
    service.save_document(make_message(str(path)))
    return repo, path


@pytest.fixture
def client(stored) -> TestClient:
    repo, _path = stored
    return TestClient(create_app(repository=repo, file_source=LocalFileSource()))


def _pid(stored) -> str:
    repo, _ = stored
    return repo.get_by_id(1).public_id


def _url(stored) -> str:
    return f"/f/{_pid(stored)}"


# ── A. existing full-download behavior ─────────────────────────────────────


def test_plain_get_returns_200_full_content(stored, client):
    resp = client.get(_url(stored))
    assert resp.status_code == 200
    assert resp.content == CONTENT
    assert resp.headers["content-length"] == "10"
    assert resp.headers["content-type"].startswith("text/plain")
    assert "attachment" in resp.headers["content-disposition"]
    assert "digits.txt" in resp.headers["content-disposition"]
    assert resp.headers["accept-ranges"] == "bytes"


# ── B/C/D/E/F/G. valid ranges ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "range_header, expected_status, expected_range, expected_body",
    [
        ("bytes=0-4", 206, "bytes 0-4/10", b"01234"),        # explicit
        ("bytes=5-9", 206, "bytes 5-9/10", b"56789"),        # middle
        ("bytes=5-", 206, "bytes 5-9/10", b"56789"),         # open-ended
        ("bytes=-5", 206, "bytes 5-9/10", b"56789"),         # suffix
        ("bytes=0-0", 206, "bytes 0-0/10", b"0"),            # single byte
        ("bytes=9-9", 206, "bytes 9-9/10", b"9"),            # last byte
        ("bytes=2-99", 206, "bytes 2-9/10", b"23456789"),    # end clamped
    ],
)
def test_valid_ranges(
    stored, client, range_header, expected_status, expected_range, expected_body
):
    resp = client.get(_url(stored), headers={"Range": range_header})
    assert resp.status_code == expected_status
    assert resp.headers["content-range"] == expected_range
    assert resp.headers["content-length"] == str(len(expected_body))
    assert resp.content == expected_body
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-type"].startswith("text/plain")


# ── H/I. invalid / unsatisfiable ranges → 416 ──────────────────────────────


@pytest.mark.parametrize(
    "range_header",
    [
        "bytes=100-50",   # start > end
        "bytes=10-20",    # start == size
        "bytes=11-",      # start beyond EOF
        "bytes=-0",       # zero suffix
        "bytes=-",        # empty suffix
        "bytes=0",        # no dash
        "bytes=",         # empty spec
        "bytes=abc-def",  # non-numeric
        "bytes=0-1,3-4",  # multi-range (not supported)
    ],
)
def test_unsatisfiable_ranges_return_416(stored, client, range_header):
    resp = client.get(_url(stored), headers={"Range": range_header})
    assert resp.status_code == 416
    assert resp.headers["content-range"] == "bytes */10"


def test_unknown_range_unit_is_ignored(stored, client):
    resp = client.get(_url(stored), headers={"Range": "items=0-5"})
    assert resp.status_code == 200
    assert resp.content == CONTENT


# ── J. HEAD ────────────────────────────────────────────────────────────────


def test_head_returns_metadata_without_body_or_counter(stored, client):
    repo, _path = stored
    resp = client.head(_url(stored))
    assert resp.status_code == 200
    assert resp.content == b""
    assert resp.headers["content-length"] == "10"
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["accept-ranges"] == "bytes"
    assert "attachment" in resp.headers["content-disposition"]
    assert repo.get_by_id(1).download_count == 0


def test_head_preserves_404_and_410(stored, client):
    repo, _path = stored
    assert client.head("/f/doesNotExist").status_code == 404
    assert client.head("/f/bad!char").status_code == 404  # invalid format
    repo.set_status(repo.get_by_id(1).id, "deleted")
    assert client.head(_url(stored)).status_code == 410


# ── K. download counter semantics ──────────────────────────────────────────


def test_counter_increments_once_per_get_and_not_for_head(stored, client):
    repo, _path = stored
    client.head(_url(stored))
    client.get(_url(stored))                                       # 1
    client.get(_url(stored), headers={"Range": "bytes=0-4"})       # 2
    client.get(_url(stored), headers={"Range": "bytes=-3"})        # 3
    assert repo.get_by_id(1).download_count == 3


# ── L. streaming behavior: no whole-file reads ─────────────────────────────


class SpyFile:
    """Wraps a real file and records every read() request."""

    def __init__(self, fh):
        self.fh = fh
        self.max_read = 0
        self.total_read = 0

    def read(self, size: int = -1) -> bytes:
        self.max_read = max(self.max_read, size)
        data = self.fh.read(size)
        self.total_read += len(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.fh.seek(offset, whence)

    def close(self) -> None:
        self.fh.close()


class SpySource(LocalFileSource):
    """LocalFileSource variant that wraps opened files with a SpyFile."""

    def __init__(self, path: str):
        self.path = path
        self.last: SpyFile | None = None

    def open(self, record):
        self.last = SpyFile(open(self.path, "rb"))
        return self.last


def test_range_download_reads_only_requested_bytes(tmp_path):
    size = 2 * 1024 * 1024  # 2 MiB
    path = tmp_path / "big.bin"
    with open(path, "wb") as fh:
        fh.write(b"\xAB" * size)

    repo = FileRepository(initialize(":memory:"))
    FileService(repo).save_document(make_message(str(path), file_size=size))
    source = SpySource(str(path))
    client = TestClient(create_app(repository=repo, file_source=source))

    resp = client.get(
        f"/f/{repo.get_by_id(1).public_id}", headers={"Range": "bytes=1000-1999"}
    )
    assert resp.status_code == 206
    assert len(resp.content) == 1000
    assert source.last is not None
    assert source.last.max_read <= CHUNK_SIZE      # bounded reads only
    assert source.last.total_read == 1000          # never read the whole file


def test_full_download_streams_in_bounded_chunks(tmp_path):
    size = 1024 * 1024
    path = tmp_path / "full.bin"
    with open(path, "wb") as fh:
        fh.write(b"\xCD" * size)

    repo = FileRepository(initialize(":memory:"))
    FileService(repo).save_document(make_message(str(path), file_size=size))
    source = SpySource(str(path))
    client = TestClient(create_app(repository=repo, file_source=source))

    resp = client.get(f"/f/{repo.get_by_id(1).public_id}")
    assert resp.status_code == 200
    assert len(resp.content) == size
    assert source.last is not None
    assert source.last.max_read <= CHUNK_SIZE      # bounded reads only
    assert source.last.total_read == size


# ── M. every file source supports ranges ───────────────────────────────────


@pytest.mark.parametrize(
    "source_cls", [LocalFileSource, TelegramFileSource, StorageFileSource]
)
def test_ranges_work_with_both_file_sources(tmp_path, source_cls):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    path = work_dir / "digits.txt"
    path.write_bytes(CONTENT)

    repo = FileRepository(initialize(":memory:"))
    FileService(repo).save_document(make_message(str(path)))
    if source_cls is TelegramFileSource:
        source = TelegramFileSource(work_dir)
    elif source_cls is StorageFileSource:
        # Phase 14B: FileToLink-owned storage is a containment root too.
        source = StorageFileSource(work_dir)
    else:
        source = LocalFileSource()
    client = TestClient(create_app(repository=repo, file_source=source))
    url = f"/f/{repo.get_by_id(1).public_id}"

    full = client.get(url)
    assert full.status_code == 200
    assert full.content == CONTENT

    part = client.get(url, headers={"Range": "bytes=2-5"})
    assert part.status_code == 206
    assert part.headers["content-range"] == "bytes 2-5/10"
    assert part.content == b"2345"


# ── parse_range_header unit tests ──────────────────────────────────────────


def test_parse_range_header_forms():
    assert parse_range_header("bytes=0-99", 100) == (0, 99)
    assert parse_range_header("bytes=5-", 10) == (5, 9)
    assert parse_range_header("bytes=-5", 10) == (5, 9)
    assert parse_range_header("bytes=-100", 10) == (0, 9)  # suffix clamps
    assert parse_range_header("bytes=0-999", 100) == (0, 99)  # end clamps
    assert parse_range_header("bytes=9-9", 10) == (9, 9)


def test_parse_range_header_none_cases():
    assert parse_range_header(None, 10) is None
    assert parse_range_header("", 10) is None
    assert parse_range_header("items=0-5", 10) is None  # unknown unit


def test_parse_range_header_unsatisfiable():
    for bad in (
        "bytes=100-50",
        "bytes=10-20",
        "bytes=-0",
        "bytes=-",
        "bytes=0",
        "bytes=",
        "bytes=abc-def",
        "bytes=0-1,3-4",
        "bytes=5-1",
    ):
        with pytest.raises(UnsatisfiableRange):
            parse_range_header(bad, 10)
    with pytest.raises(UnsatisfiableRange):
        parse_range_header("bytes=0-9", 0)  # empty file