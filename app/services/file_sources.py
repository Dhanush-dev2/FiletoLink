"""File source abstraction: resolving a database record to its bytes.

The database stores *metadata* about files, not the bytes themselves.
This layer decides where bytes actually come from:

- :class:`LocalFileSource` serves bytes from the local path recorded on
  the record (how the mock transport works).
- :class:`StorageFileSource` serves FileToLink-owned files (Phase 14B): the
  real Telegram flow copies/moves received files into ``FILE_STORAGE_DIR``
  and records *that* path, so downloads never depend on the Local Bot API
  Server's own file storage.
- :class:`TelegramFileSource` is the equivalent containment-enforcing source
  for the Local Bot API Server's work directory (kept for that work dir, not
  used for records written since Phase 14B).

The download endpoint only depends on :class:`FileSource` — never on the
concrete source or raw SQL. Both contained sources share one implementation
(:class:`ContainedFileSource`), so the security rules cannot drift apart.
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

    Trust model (Phase 11 review): unlike :class:`TelegramFileSource`, this
    source has **no path containment**, and deliberately so. ``source_path``
    is set only by the *local operator* running the mock REPL (``send
    <path>``) or by tests — never from untrusted remote input — so serving
    any operator-chosen local file is the intended local-testing behavior.
    Real Telegram mode uses :class:`StorageFileSource` (FileToLink storage),
    which enforces the storage root. If mock mode is ever exposed to untrusted
    users, the operator has chosen to publish those specific files via their
    links.
    """

    def open(self, record: FileRecord) -> BinaryIO:
        if not record.source_path:
            raise FileSourceError("record has no local source path")
        path = Path(record.source_path)
        if not path.is_file():
            raise FileSourceError(f"source file missing: {path}")
        try:
            return path.open("rb")
        except OSError as exc:
            # The file exists but cannot be read (permissions, I/O error, ...).
            # Surface it as FileSourceError so callers map it to a clean 404
            # instead of letting a raw OSError become a 500.
            raise FileSourceError(f"source file unreadable: {path}") from exc


class ContainedFileSource:
    """Serves files that must live physically inside one root directory.

    ``record.source_path`` must resolve to a file inside :attr:`root`:

    - paths that escape the directory (``../``, absolute paths elsewhere)
      are rejected;
    - symlinks are resolved first, so a link inside the directory that
      points outside it is also rejected;
    - filesystem paths are never exposed to users (callers map
      :class:`FileSourceError` to a generic error).

    Nothing outside the configured root is ever readable through this source,
    which is what keeps a record from serving an unrelated local file.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def open(self, record: FileRecord) -> BinaryIO:
        if not record.source_path:
            raise FileSourceError("record has no source path")

        target = Path(record.source_path)
        try:
            # Follows symlinks: a link escaping the root is caught below.
            resolved = target.resolve(strict=False)
        except OSError as exc:
            raise FileSourceError("could not resolve source path") from exc

        if not resolved.is_relative_to(self.root):
            raise FileSourceError(
                "source path is outside the allowed work directory"
            )
        if not resolved.is_file():
            raise FileSourceError(f"source file missing: {resolved}")
        try:
            return resolved.open("rb")
        except OSError as exc:
            # Readable-check passed but the open still failed (permissions,
            # I/O error, ...): report it as FileSourceError (→ 404), never a
            # raw OSError (→ 500).
            raise FileSourceError("source file unreadable") from exc


class TelegramFileSource(ContainedFileSource):
    """Serves files stored by the Local Bot API Server (``--local``).

    ``record.source_path`` is the absolute local path the server returned via
    ``get_file``. Files received before Phase 14B (or adopted without a
    storage service) are still recorded there, so this source remains the
    containment-enforcing way to serve them; the work directory is the
    configured ``TELEGRAM_BOT_WORK_DIR``.
    """

    def __init__(self, work_dir: str | Path | None = None) -> None:
        from ..config import get_settings

        if work_dir is None:
            work_dir = get_settings().telegram_bot_work_dir
        super().__init__(work_dir)
        self.work_dir = self.root  # historical alias for the same root


class StorageFileSource(ContainedFileSource):
    """Serves FileToLink-owned files (Phase 14B).

    Since Phase 14B the real Telegram flow transfers each received file into
    FileToLink storage and records the path *there*, so this is the source the
    API uses in real mode. Containment is enforced against
    ``FILE_STORAGE_DIR``: only files inside FileToLink storage can ever be
    served, never the Local Bot API Server's work directory.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        from ..config import get_settings

        if root is None:
            root = get_settings().file_storage_dir
        super().__init__(root)