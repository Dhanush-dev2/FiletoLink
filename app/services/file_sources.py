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