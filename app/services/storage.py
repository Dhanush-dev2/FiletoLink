"""FileToLink-owned storage: taking ownership of a received file.

Telegram (with a Local Bot API Server in ``--local`` mode) hands us the
absolute path of a file the *server* stored in its own work directory. That
directory is not ours: the server may evict files from it at any time, which
turned a working link into a dead one (Phase 14).

So before a public link is created, FileToLink takes ownership of the bytes:
:class:`FileStorage` transfers the source file into FileToLink storage
(``FILE_STORAGE_DIR``, default ``data/storage``) and returns the
FileToLink-owned path that is then recorded as ``source_path``.

Layout::

    <FILE_STORAGE_DIR>/<public_id>/<filename>

One directory per link, so a link's bytes are derivable from its public id and
two links can never collide with (or overwrite) each other's file.

Guarantees:

- the source is validated first (regular, readable file — never a directory);
- destination names are sanitised to a safe basename and made unique, so a
  remote filename can never traverse out of the storage root;
- the source and destination are on the same device -> an atomic ``os.replace``
  move, no duplicate of a 400+ MB file; otherwise a *streamed* copy (fixed
  1 MiB chunks, never a whole-file read into RAM) into a ``.part`` file that is
  only moved into place once written and size-verified;
- the source is removed only *after* the destination is fully written and
  verified, so a failure can never lose the file;
- a failed transfer leaves nothing behind: the partial destination is removed.

The storage root is created lazily, on the first transfer.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from ..config import get_settings
from ..utils.public_ids import is_valid_public_id

logger = logging.getLogger(__name__)

# Streamed copies move the file in 1 MiB chunks: a 400+ MB file is never
# loaded into memory, and the chunk size is big enough to keep copies fast.
CHUNK_SIZE = 1024 * 1024

# A streamed copy writes here first, then renames onto the final name, so a
# partially written file is never visible as the destination.
PART_SUFFIX = ".part"

# Bounded attempts to find an unused destination name inside a link directory.
MAX_NAME_ATTEMPTS = 1_000

_FALLBACK_NAME = "file"

# Characters that are unsafe in a filename (path separators, Windows-reserved
# characters, control characters that could break anything downstream).
_UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')

# Windows device names: a file literally called "CON" (etc.) cannot be used.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)


class StorageError(RuntimeError):
    """Raised when a file cannot be taken into FileToLink-owned storage.

    The message is written for internal logs only: callers map it to a generic
    user-facing error, so no filesystem path is ever exposed.
    """


def safe_file_name(name: str | None, fallback: str = _FALLBACK_NAME) -> str:
    """Return a safe, single-component file name for storage.

    The name comes from the message sender and is therefore untrusted: any
    directory component (``/`` or ``\\``), reserved character, or control
    character is dropped, so the result can only ever name a file *inside* the
    link's own directory. Empty/degenerate names fall back to ``file``.
    """
    if not name:
        return fallback
    # Keep only the last component (defence in depth against traversal) ...
    candidate = name.replace("\\", "/").rsplit("/", 1)[-1]
    # ... then drop anything else that is not safe in a file name.
    candidate = _UNSAFE_NAME_CHARS.sub("", candidate).strip().strip(".")
    if not candidate or candidate in {".", ".."}:
        return fallback
    if candidate.split(".")[0].upper() in _RESERVED_NAMES:
        candidate = f"_{candidate}"
    return candidate[:255]


def _same_device(source: Path, dest_dir: Path) -> bool:
    """Whether ``source`` and ``dest_dir`` live on the same filesystem.

    Same device ⇒ a move (rename) is possible: atomic, instant, and free of an
    extra full copy of the file's bytes.
    """
    try:
        return os.stat(source).st_dev == os.stat(dest_dir).st_dev
    except OSError:
        return False


class FileStorage:
    """FileToLink's own storage for the files it serves.

    ``root`` defaults to the configured ``FILE_STORAGE_DIR``. The directory is
    created when needed (on the first transfer), never in the constructor.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            root = get_settings().file_storage_dir
        self.root = Path(root).resolve()

    # ── queries ────────────────────────────────────────────────────────────

    def owns(self, path: str | Path) -> bool:
        """Whether ``path`` is a file inside FileToLink storage (not the root).

        Resolves symlinks first, so a link pointing out of the storage root is
        not considered owned. Used to make sure cleanup never touches a
        Telegram-managed file.
        """
        try:
            resolved = Path(path).resolve(strict=False)
        except OSError:
            return False
        return resolved.is_relative_to(self.root) and resolved != self.root

    def owner_dir(self, public_id: str) -> Path:
        """The per-link directory that holds this link's file."""
        return self.root / public_id

    # ── taking ownership ───────────────────────────────────────────────────

    def adopt(
        self,
        source_path: str | Path,
        *,
        public_id: str,
        file_name: str | None = None,
    ) -> str:
        """Transfer ``source_path`` into FileToLink storage; return its new path.

        The returned path is the FileToLink-owned absolute path to record as
        ``source_path``. Raises :class:`StorageError` (and leaves the source
        untouched) when the file cannot be validated or transferred.

        A file that is already inside the storage root is returned unchanged
        (adoption is idempotent), so re-adopting an owned file never removes or
        duplicates it.
        """
        if not is_valid_public_id(public_id):
            raise StorageError("refusing to store a file under an invalid public id")
        try:
            source = Path(source_path).resolve(strict=True)
        except OSError as exc:
            raise StorageError("source file is missing or unreachable") from exc
        if not source.is_file():
            raise StorageError("source path is not a regular file")
        if self.owns(source):
            return str(source)  # already ours — nothing to transfer
        # Validate readability before creating anything, so a failure cannot
        # leave a destination behind.
        self._ensure_readable(source)

        name = self._unique_name(public_id, safe_file_name(file_name))
        dest_dir = self.owner_dir(public_id)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError("could not create the storage directory") from exc

        dest = dest_dir / name
        try:
            self._transfer(source, dest)
        except BaseException:
            # The transfer cleaned up after itself; drop the now-empty link
            # directory too, so a failed adoption leaves nothing at all behind.
            self._prune(dest_dir)
            raise

        # Verify before the path is recorded or served: the durable invariant is
        # "ACTIVE record ⇒ a valid FileToLink-owned file exists".
        if not dest.is_file():
            raise StorageError("destination file was not created")
        return str(dest)

    def rekey(self, public_id: str, new_public_id: str) -> str | None:
        """Move an owned file to a new public id's directory; path or None.

        Only needed for the astronomically unlikely public-id collision
        (48-bit CSPRNG id + UNIQUE index): the file has already been adopted
        under its candidate id, so it is re-keyed instead of re-transferred.
        Returns None when the move is not possible, leaving everything as is.
        """
        if not is_valid_public_id(new_public_id):
            return None
        old_dir = self.owner_dir(public_id)
        new_dir = self.owner_dir(new_public_id)
        if new_dir.exists():
            return None
        try:
            os.rename(old_dir, new_dir)
        except OSError:
            return None
        files = [entry for entry in new_dir.iterdir() if entry.is_file()]
        if len(files) != 1:
            # Unexpected layout: put it back exactly as it was.
            try:
                os.rename(new_dir, old_dir)
            except OSError:
                pass
            return None
        return str(files[0])

    # ── cleanup ────────────────────────────────────────────────────────────

    def remove_owned(self, path: str | Path) -> bool:
        """Delete one FileToLink-owned file (and its now-empty link directory).

        Returns True when the file is gone (or was already gone) and False when
        the path is not ours or the deletion failed. Never raises: cleanup must
        not fail the API call that triggered it, so failures are logged
        internally (with the path, for diagnosis) and reported as False.
        Anything outside the storage root — in particular Telegram-managed
        files — is never touched.
        """
        try:
            target = Path(path).resolve(strict=False)
        except OSError:
            return False
        if not self.owns(target):
            return False
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "could not delete FileToLink-owned file %s: %s",
                target,
                exc.__class__.__name__,
            )
            return False
        self._prune(target.parent)
        return True

    def _prune(self, directory: Path) -> None:
        """Remove an empty per-link directory (never the storage root)."""
        try:
            resolved = directory.resolve(strict=False)
        except OSError:
            return
        if resolved == self.root or not resolved.is_relative_to(self.root):
            return
        try:
            if not any(resolved.iterdir()):
                resolved.rmdir()
        except OSError:
            pass  # not empty / in use: leaving it is harmless

    # ── internals ──────────────────────────────────────────────────────────

    @staticmethod
    def _ensure_readable(source: Path) -> None:
        """Fail early when the source cannot be opened for reading."""
        try:
            with source.open("rb"):
                pass
        except OSError as exc:
            raise StorageError("source file is not readable") from exc

    def _unique_name(self, public_id: str, name: str) -> str:
        """First unused file name inside the link's directory.

        Two files with the same name can only ever meet here (a link's
        directory holds one file per public id), and the second one is stored
        alongside the first instead of overwriting it.
        """
        directory = self.owner_dir(public_id)
        if not (directory / name).exists():
            return name
        stem, suffix = os.path.splitext(name)
        for counter in range(1, MAX_NAME_ATTEMPTS):
            candidate = f"{stem}-{counter}{suffix}"
            if not (directory / candidate).exists():
                return candidate
        raise StorageError("could not find an unused file name in storage")

    def _transfer(self, source: Path, dest: Path) -> None:
        """Move the file into storage, or stream a copy when a move is unsafe.

        A move is preferred: it is atomic, instant, and never duplicates the
        file. It is only safe on the same device (``os.replace`` raises across
        devices), so the fallback is a verified streaming copy.
        """
        if _same_device(source, dest.parent):
            try:
                os.replace(source, dest)
                return
            except OSError as exc:
                logger.debug(
                    "could not move %s into storage (%s); streaming a copy instead",
                    source,
                    exc.__class__.__name__,
                )
        try:
            self._copy_streaming(source, dest)
        except StorageError:
            raise
        except OSError as exc:
            # The partial destination is already cleaned up; report a storage
            # failure (never a raw OSError) so callers map it to a clean error.
            raise StorageError("could not copy the file into storage") from exc

    def _copy_streaming(self, source: Path, dest: Path) -> None:
        """Copy ``source`` to ``dest`` in chunks, verify, then remove ``source``.

        The whole file is never read into memory: ``shutil.copyfileobj`` moves
        it in fixed-size chunks. The copy lands in a ``.part`` file that only
        replaces the destination once it is complete and its size matches the
        source, so a partial file can never be served. On any failure the
        partial destination is deleted and the source is left intact — losing
        the file is the one outcome this must never produce.
        """
        tmp = dest.with_name(dest.name + PART_SUFFIX)
        # Never touch a destination that was already there: this transfer only
        # cleans up what it created itself.
        pre_existing = dest.exists()
        try:
            with source.open("rb") as src, tmp.open("wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK_SIZE)
                dst.flush()
                os.fsync(dst.fileno())
            if tmp.stat().st_size != source.stat().st_size:
                raise StorageError("copy verification failed: size mismatch")
            os.replace(tmp, dest)
        except BaseException:
            # Clean up whatever this failed transfer produced.
            leftovers = (tmp,) if pre_existing else (tmp, dest)
            for leftover in leftovers:
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    logger.warning("could not remove partial storage file %s", leftover)
            raise
        # The destination is written and verified — only now is the source
        # allowed to disappear. A failure to remove it leaves a harmless
        # duplicate (the Local Bot API Server re-fetches what it needs).
        try:
            source.unlink()
        except OSError as exc:
            logger.warning(
                "owned copy written but the source file could not be removed: %s",
                exc.__class__.__name__,
            )
