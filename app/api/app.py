"""FastAPI application: the public download endpoint.

HTTP concerns live here and stay separate from bot handlers, transports,
the repository, and id/URL generation. The endpoint only talks to the
repository (for the record) and a :class:`FileSource` (for the bytes);
it contains no raw SQL.

Byte-range behavior (RFC 7233, single range):

- no Range header → ``200`` with the full file + ``Accept-Ranges: bytes``
- valid ``Range: bytes=...`` → ``206`` with ``Content-Range`` and only the
  requested bytes (seek + bounded reads, never a whole-file read)
- malformed/unsatisfiable ranges → ``416`` with ``Content-Range: bytes */N``
- ``HEAD`` is served by the same route (Starlette adds HEAD to GET routes)
  with identical metadata headers, no body, and no download counter change

Run it with:
    python run.py --api
    # or: uvicorn --factory app.api.app:create_app --host 127.0.0.1 --port 8000
"""
import os
from typing import BinaryIO, Iterator

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from urllib.parse import quote

from ..config import get_settings
from ..database.connection import initialize
from ..database.files import FileRepository
from ..database.records import FileRecord
from ..services.file_sources import (
    FileSource,
    FileSourceError,
    LocalFileSource,
    TelegramFileSource,
)
from ..utils.public_ids import is_valid_public_id
from ..utils.ranges import UnsatisfiableRange, parse_range_header

CHUNK_SIZE = 64 * 1024


def _iter_chunks(stream: BinaryIO, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
    """Yield the stream in fixed-size chunks without loading it into RAM."""
    try:
        while chunk := stream.read(chunk_size):
            yield chunk
    finally:
        stream.close()


def _iter_range(
    stream: BinaryIO, start: int, end: int, chunk_size: int = CHUNK_SIZE
) -> Iterator[bytes]:
    """Yield exactly the bytes in the inclusive ``[start, end]`` range.

    Seeks to ``start``, then reads only ``end - start + 1`` bytes in
    bounded chunks — never a whole-file read.
    """
    stream.seek(start)
    remaining = end - start + 1
    try:
        while remaining > 0:
            chunk = stream.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        stream.close()


def _stream_size(stream: BinaryIO) -> int | None:
    """Best-effort byte size of an open stream via fstat (position unchanged)."""
    try:
        return os.fstat(stream.fileno()).st_size
    except (OSError, ValueError, AttributeError):
        return None


def _filename_header(record: FileRecord) -> str:
    return (
        "attachment; filename*=UTF-8''"
        f"{quote(record.file_name or 'download', safe='')}"
    )


def _content_headers(
    record: FileRecord,
    *,
    content_length: int,
    content_range: str | None = None,
) -> dict[str, str]:
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
    }
    if record.mime_type:
        headers["Content-Type"] = record.mime_type
    headers["Content-Disposition"] = _filename_header(record)
    if content_range:
        headers["Content-Range"] = content_range
    return headers


def _error(
    status: int, detail: str, *, is_head: bool, headers: dict[str, str] | None = None
) -> Response:
    """JSON error for GET; header-only (empty body) error for HEAD."""
    if is_head:
        return Response(status_code=status, headers=headers or {})
    return JSONResponse(
        status_code=status, content={"detail": detail}, headers=headers or {}
    )


def _head_response(record: FileRecord) -> Response:
    headers: dict[str, str] = {"Accept-Ranges": "bytes"}
    if record.file_size is not None:
        headers["Content-Length"] = str(record.file_size)
    if record.mime_type:
        headers["Content-Type"] = record.mime_type
    headers["Content-Disposition"] = _filename_header(record)
    return Response(status_code=200, headers=headers)


def _get_response(
    record: FileRecord,
    file_source: FileSource,
    range_header: str | None,
) -> Response:
    """Serve the full file (200) or the requested byte range (206/416)."""
    try:
        stream = file_source.open(record)
    except FileSourceError as exc:
        raise HTTPException(
            status_code=404, detail="File content is not available"
        ) from exc

    total = record.file_size
    if total is None:
        total = _stream_size(stream)
    if total is None:
        # Size unknown: serve the whole stream without range support.
        return StreamingResponse(
            _iter_chunks(stream),
            headers={
                "Accept-Ranges": "bytes",
                "Content-Disposition": _filename_header(record),
            },
        )

    try:
        rng = parse_range_header(range_header, total)
    except UnsatisfiableRange:
        stream.close()
        return _error(
            416,
            "Requested range not satisfiable",
            is_head=False,
            headers={"Content-Range": f"bytes */{total}"},
        )

    if rng is None:
        return StreamingResponse(
            _iter_chunks(stream),
            headers=_content_headers(record, content_length=total),
        )

    start, end = rng
    length = end - start + 1
    return StreamingResponse(
        _iter_range(stream, start, end),
        status_code=206,
        headers=_content_headers(
            record,
            content_length=length,
            content_range=f"bytes {start}-{end}/{total}",
        ),
    )


def create_app(
    repository: FileRepository | None = None,
    file_source: FileSource | None = None,
) -> FastAPI:
    """Build the API. Defaults to the configured database and a file source.

    Tests inject an in-memory repository and a source of their choosing.
    """
    if repository is None:
        repository = FileRepository(initialize())
    if file_source is None:
        # Real mode serves the Local Bot API Server's files (work-dir
        # enforced); mock mode serves the paths the mock transport recorded.
        settings = get_settings()
        file_source = (
            TelegramFileSource() if settings.bot_mode == "real" else LocalFileSource()
        )

    app = FastAPI(title="File-to-Link API")

    def handle_download(public_id: str, request: Request) -> Response:
        # Shared by GET and HEAD; the branch below only alters
        # headers/body/counter.
        is_head = request.method == "HEAD"

        # 1. Validate the public id.
        if not is_valid_public_id(public_id):
            return _error(404, "File not found", is_head=is_head)

        # 2. Look up the record.
        record = repository.get_by_public_id(public_id)
        if record is None:
            return _error(404, "File not found", is_head=is_head)

        # 3. Only active records are downloadable.
        if record.status != "active":
            return _error(
                410, "This link is no longer available", is_head=is_head
            )

        # 4. HEAD: metadata headers only — never counts as a download.
        if is_head:
            return _head_response(record)

        # 5. One logical download request → one counter increment
        #    (range GETs and streamed chunks do not multiply it).
        repository.update(record.id, download_count=record.download_count + 1)

        # 6. Resolve the bytes and serve them (full or ranged).
        return _get_response(record, file_source, request.headers.get("range"))

    @app.get("/f/{public_id}")
    def download(public_id: str, request: Request) -> Response:
        return handle_download(public_id, request)

    @app.head("/f/{public_id}")
    def download_head(public_id: str, request: Request) -> Response:
        return handle_download(public_id, request)

    return app