"""FastAPI application: the public download endpoint.

HTTP concerns live here and stay separate from bot handlers, transports,
the repository, and id/URL generation. The endpoint only talks to the
repository (for the record) and a :class:`FileSource` (for the bytes);
it contains no raw SQL.

Link lifecycle (Phase 10):

- expiration (``expires_at`` in the past) → lazy ``expired`` status, ``410``
- deleted (``status = 'deleted'``) → ``410``
- exhausted (``download_count >= max_downloads``) → ``410``; exhausted
  links are terminalized with the existing ``expired`` status
- one download slot is reserved per GET with a single atomic conditional
  UPDATE, so concurrent requests can never exceed ``max_downloads``
- ``HEAD`` returns metadata without reserving a slot

Byte-range behavior (RFC 7233, single range):

- no Range header → ``200`` with the full file + ``Accept-Ranges: bytes``
- valid ``Range: bytes=...`` → ``206`` with ``Content-Range`` and only the
  requested bytes (seek + bounded reads, never a whole-file read)
- malformed/unsatisfiable ranges → ``416`` with ``Content-Range: bytes */N``
- a valid range GET consumes exactly one download slot

Abuse protection & hardening (Phase 11):

- ``GET``/``HEAD`` are rate limited per **client identity** (see
  :mod:`app.utils.ratelimit`) *before* any lookup, so probing for public ids
  is throttled too; over the limit → ``429`` with ``Retry-After``. Only
  client identities are tracked, never link ids.
- security headers are attached to every response (:mod:`app.api.security`);
  stored MIME metadata is sanitised (:mod:`app.utils.mime`) with an
  ``application/octet-stream`` fallback;
- ``HEAD`` mirrors the matching GET status/headers (``200``/``206``/``416``)
  with no body and without consuming a download slot.

Run it with:
    python run.py --api
    # or: uvicorn --factory app.api.app:create_app --host 127.0.0.1 --port 8000
"""
import os
import time
from typing import BinaryIO, Iterator
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import Settings, get_settings
from ..database.connection import initialize
from ..database.files import FileRepository
from ..database.records import FileRecord
from ..services.file_sources import (
    FileSource,
    FileSourceError,
    LocalFileSource,
    TelegramFileSource,
)
from ..utils.mime import resolve_content_type
from ..utils.public_ids import PUBLIC_ID_LENGTH, is_valid_public_id
from ..utils.ratelimit import RateLimiter
from ..utils.ranges import UnsatisfiableRange, parse_range_header
from .security import SECURITY_HEADERS, client_key

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


def _headers(**extra: str) -> dict[str, str]:
    """Security headers plus any response-specific headers."""
    return {**SECURITY_HEADERS, **extra}


def _content_headers(
    record: FileRecord,
    *,
    content_length: int,
    content_range: str | None = None,
) -> dict[str, str]:
    headers = _headers(
        **{
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Type": resolve_content_type(record.mime_type),
            "Content-Disposition": _filename_header(record),
        }
    )
    if content_range:
        headers["Content-Range"] = content_range
    return headers


def _error(
    status: int, detail: str, *, is_head: bool, headers: dict[str, str] | None = None
) -> Response:
    """JSON error for GET; header-only (empty body) error for HEAD."""
    response_headers = _headers(**(headers or {}))
    if is_head:
        return Response(status_code=status, headers=response_headers)
    return JSONResponse(
        status_code=status, content={"detail": detail}, headers=response_headers
    )


def _head_response(
    record: FileRecord,
    file_source: FileSource,
    range_header: str | None = None,
) -> Response:
    """Metadata-only response mirroring what the matching GET would return.

    Like GET it verifies the bytes are actually available (opens and
    immediately closes the source), so a missing or unreadable file gives
    the same ``404`` as GET, and the size (hence ``Content-Length``) is
    resolved the same way when the record stores no ``file_size``. It never
    sends a body and never reserves a download slot, and with a valid
    ``Range`` it returns ``206`` (or ``416`` when unsatisfiable) exactly like
    GET.
    """
    try:
        stream = file_source.open(record)
    except FileSourceError:
        # Return through _error (not HTTPException) so the security headers
        # are still attached to this 404.
        return _error(404, "File content is not available", is_head=True)
    try:
        total = record.file_size
        if total is None:
            total = _stream_size(stream)
    finally:
        stream.close()

    if total is not None:
        try:
            rng = parse_range_header(range_header, total)
        except UnsatisfiableRange:
            return Response(
                status_code=416,
                headers=_headers(**{"Content-Range": f"bytes */{total}"}),
            )
        if rng is not None:
            start, end = rng
            return Response(
                status_code=206,
                headers=_headers(
                    **{
                        "Accept-Ranges": "bytes",
                        "Content-Length": str(end - start + 1),
                        "Content-Type": resolve_content_type(record.mime_type),
                        "Content-Disposition": _filename_header(record),
                        "Content-Range": f"bytes {start}-{end}/{total}",
                    }
                ),
            )

    headers = _headers(
        **{
            "Accept-Ranges": "bytes",
            "Content-Type": resolve_content_type(record.mime_type),
            "Content-Disposition": _filename_header(record),
        }
    )
    if total is not None:
        headers["Content-Length"] = str(total)
    return Response(status_code=200, headers=headers)


def _get_response(
    record: FileRecord,
    file_source: FileSource,
    range_header: str | None,
) -> Response:
    """Serve the full file (200) or the requested byte range (206/416)."""
    try:
        stream = file_source.open(record)
    except FileSourceError:
        # Return through _error (not HTTPException) so the security headers
        # are still attached to this 404.
        return _error(404, "File content is not available", is_head=False)

    total = record.file_size
    if total is None:
        total = _stream_size(stream)
    if total is None:
        # Size unknown: serve the whole stream without range support.
        return StreamingResponse(
            _iter_chunks(stream),
            headers=_headers(
                **{
                    "Accept-Ranges": "bytes",
                    "Content-Type": resolve_content_type(record.mime_type),
                    "Content-Disposition": _filename_header(record),
                }
            ),
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
    rate_limiter: RateLimiter | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Build the API. Defaults to the configured database and a file source.

    Tests inject an in-memory repository, a source of their choosing, and/or
    a pre-built rate limiter.
    """
    settings = settings or get_settings()
    if repository is None:
        repository = FileRepository(initialize())
    if file_source is None:
        # Real mode serves the Local Bot API Server's files (work-dir
        # enforced); mock mode serves the paths the mock transport recorded.
        file_source = (
            TelegramFileSource() if settings.bot_mode == "real" else LocalFileSource()
        )
    if rate_limiter is None and settings.rate_limit_enabled:
        rate_limiter = RateLimiter(
            limit=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
            max_keys=settings.rate_limit_max_keys,
        )

    app = FastAPI(title="File-to-Link API")

    def handle_download(public_id: str, request: Request) -> Response:
        # Shared by GET and HEAD; the branch below only alters
        # headers/body/counter.
        is_head = request.method == "HEAD"

        # 0. Abuse protection: per-client rate limit, before any validation
        #    or lookup, so probing for public ids is throttled as well. Only
        #    client identities are tracked (never link ids), so invalid ids
        #    cannot grow per-file state.
        if rate_limiter is not None:
            allowed, retry_after = rate_limiter.check(client_key(request, settings))
            if not allowed:
                return _error(
                    429,
                    "Too many requests",
                    is_head=is_head,
                    headers={"Retry-After": str(retry_after)},
                )

        now = int(time.time())

        # 1. Validate the public id (cheap length check first, so an
        #    oversized path parameter is rejected without scanning it).
        if len(public_id) != PUBLIC_ID_LENGTH or not is_valid_public_id(public_id):
            return _error(404, "File not found", is_head=is_head)

        # 2. Look up the record.
        record = repository.get_by_public_id(public_id)
        if record is None:
            return _error(404, "File not found", is_head=is_head)

        # 3. Lazy expiration: a link whose expires_at has passed is
        #    terminalized on first access (no background worker needed).
        if record.expires_at is not None and record.expires_at <= now:
            repository.expire_if_due(record.id, now)
            return _error(410, "This link has expired", is_head=is_head)

        # 4. Only active records are downloadable (deleted / expired).
        if record.status != "active":
            return _error(
                410, "This link is no longer available", is_head=is_head
            )

        # 5. HEAD: metadata headers only — never reserves a slot. Still,
        #    a link whose count already reached the limit (e.g.
        #    max_downloads = 0) is terminal: detect it via the derived
        #    condition and terminalize it like GET would.
        if is_head:
            if (
                record.max_downloads is not None
                and record.download_count >= record.max_downloads
            ):
                repository.mark_exhausted(record.id)
                return _error(
                    410, "This link is no longer available", is_head=is_head
                )
            return _head_response(
                record, file_source, request.headers.get("range")
            )

        # 6. Atomically reserve one download slot. The single conditional
        #    UPDATE checks status, expiration, and the download limit, so
        #    concurrent requests cannot exceed max_downloads (no
        #    check-then-act race). Range GETs count as exactly one slot.
        if not repository.reserve_download_slot(record.id, now):
            current = repository.get_by_id(record.id)
            if (
                current is not None
                and current.expires_at is not None
                and current.expires_at <= now
            ):
                repository.expire_if_due(current.id, now)
                return _error(410, "This link has expired", is_head=is_head)
            if current is not None and current.status == "active":
                # Active but unreservable ⇒ its download limit is exhausted;
                # terminalize it with the existing 'expired' status.
                repository.mark_exhausted(record.id)
            return _error(
                410, "This link is no longer available", is_head=is_head
            )

        # 7. Serve the bytes. If nothing can be served (missing source →
        #    404, unsatisfiable range → 416) the reserved slot is released
        #    so failed requests do not burn a download.
        response = _get_response(record, file_source, request.headers.get("range"))
        if response.status_code >= 400:
            repository.release_download_slot(record.id)
        return response

    @app.get("/f/{public_id}")
    def download(public_id: str, request: Request) -> Response:
        return handle_download(public_id, request)

    @app.head("/f/{public_id}")
    def download_head(public_id: str, request: Request) -> Response:
        return handle_download(public_id, request)

    return app
