"""FastAPI application: the public download endpoint.

HTTP concerns live here and stay separate from bot handlers, transports,
the repository, and id/URL generation. The endpoint only talks to the
repository (for the record) and a :class:`FileSource` (for the bytes);
it contains no raw SQL.

Run it with:
    python run.py --api
    # or: uvicorn --factory app.api.app:create_app --host 127.0.0.1 --port 8000
"""
from typing import BinaryIO, Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
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

CHUNK_SIZE = 64 * 1024


def _iter_chunks(stream: BinaryIO, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
    """Yield the stream in fixed-size chunks without loading it into RAM."""
    try:
        while chunk := stream.read(chunk_size):
            yield chunk
    finally:
        stream.close()


def _serve_record(
    record: FileRecord, file_source: FileSource
) -> StreamingResponse:
    """Open the file's bytes through the source and stream them out."""
    try:
        stream = file_source.open(record)
    except FileSourceError as exc:
        raise HTTPException(
            status_code=404, detail="File content is not available"
        ) from exc

    filename = quote(record.file_name or "download", safe="")
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"}
    return StreamingResponse(
        _iter_chunks(stream),
        media_type=record.mime_type,
        headers=headers,
    )


def create_app(
    repository: FileRepository | None = None,
    file_source: FileSource | None = None,
) -> FastAPI:
    """Build the API. Defaults to the configured database and a local source.

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

    @app.get("/f/{public_id}")
    def download(public_id: str) -> StreamingResponse:
        # 1. Validate the public id.
        if not is_valid_public_id(public_id):
            raise HTTPException(status_code=404, detail="File not found")

        # 2. Look up the record.
        record = repository.get_by_public_id(public_id)
        if record is None:
            raise HTTPException(status_code=404, detail="File not found")

        # 3. Only active records are downloadable.
        if record.status != "active":
            raise HTTPException(
                status_code=410, detail="This link is no longer available"
            )

        # 4. Increment the download counter (Phase 6; full counters later).
        repository.update(record.id, download_count=record.download_count + 1)

        # 5. Resolve the bytes and serve them.
        return _serve_record(record, file_source)

    return app