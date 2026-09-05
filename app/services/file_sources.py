"""File source abstraction: resolving a database record to its bytes.

The database stores *metadata* about files, not the bytes themselves.
This layer decides where bytes actually come from:

- :class:`LocalFileSource` serves bytes from the local path recorded on
  the record (how the mock transport works).
- The real Telegram integration will provide a source that fetches bytes
  through the Bot API (``getFile``), without changing the endpoint.

The download endpoint only depends on :class:`FileSource` — never on the
concrete source or raw SQL.
"""
from pathlib import Path
from typing import BinaryIO, Protocol

from ..database.records import FileRecord


class FileSourceError(RuntimeError):
    """Raised when a file's bytes cannot be resolved or read."""


class FileSource(Protocol):
    """Resolves a database record to an open binary stream of its bytes."""

    def open(self, record: FileRecord) -> BinaryIO:
        """Return an open, binary, readable stream. Caller closes it."""
        ...


class LocalFileSource:
    """Serves bytes from a local path (``record.source_path``).

    The mock transport records where each received file came from. Files
    received before this source existed (or from other sources) simply
    have no local path and raise :class:`FileSourceError`.
    """

    def open(self, record: FileRecord) -> BinaryIO:
        if not record.source_path:
            raise FileSourceError("record has no local source path")
        path = Path(record.source_path)
        if not path.is_file():
            raise FileSourceError(f"source file missing: {path}")
        return path.open("rb")


class TelegramFileSource:
    """Serves files stored by the Local Bot API Server (``--local``).

    ``record.source_path`` is the absolute local path the server returned
    via ``get_file``. For security, only files physically inside the
    configured :attr:`work_dir` are accessible:

    - paths that escape the directory (``../``, absolute paths elsewhere)
      are rejected;
    - symlinks are resolved first, so a link inside the directory that
      points outside it is also rejected;
    - filesystem paths are never exposed to users (callers map
      :class:`FileSourceError` to a generic error).
    """

    def __init__(self, work_dir: str | Path | None = None) -> None:
        from ..config import get_settings

        if work_dir is None:
            work_dir = get_settings().telegram_bot_work_dir
        self.work_dir = Path(work_dir).resolve()

    def open(self, record: FileRecord) -> BinaryIO:
        if not record.source_path:
            raise FileSourceError("record has no source path")

        target = Path(record.source_path)
        try:
            # Follows symlinks: a link escaping the work dir is caught below.
            resolved = target.resolve(strict=False)
        except OSError as exc:
            raise FileSourceError("could not resolve source path") from exc

        if not resolved.is_relative_to(self.work_dir):
            raise FileSourceError(
                "source path is outside the allowed work directory"
            )
        if not resolved.is_file():
            raise FileSourceError(f"source file missing: {resolved}")
        return resolved.open("rb")