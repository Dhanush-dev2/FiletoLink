"""Application services for file handling.

This layer sits between the (framework-agnostic) bot handlers and the
database repository: it knows how to turn a normalized file message into
a stored record — including generating and assigning its unique public id —
keeping raw SQL out of handlers and transports.
"""
import sqlite3
from pathlib import Path

from ..bot.models import Message
from ..database.connection import initialize
from ..database.files import FileRecord, FileRepository
from ..utils.public_ids import generate_public_id

# Bounded retries for the (astronomically unlikely) public id collision.
MAX_PUBLIC_ID_ATTEMPTS = 10


class PublicIdError(RuntimeError):
    """Raised when a public id could not be assigned after retries."""


class FileService:
    """Turns normalized file messages into stored database records."""

    def __init__(self, repository: FileRepository) -> None:
        self.repository = repository

    def save_document(
        self,
        message: Message,
        *,
        expires_at: int | None = None,
        max_downloads: int | None = None,
    ) -> FileRecord:
        """Persist a file message and return the stored record.

        ``expires_at`` (NULL = never) and ``max_downloads`` (NULL =
        unlimited) are optional link-management settings; when omitted the
        link lives forever with no download limit.

        Flow: save metadata → generate public id → store it (retrying on
        the unlikely UNIQUE collision) → return the record.
        """
        if message.document is None:
            raise ValueError("message has no document")
        doc = message.document
        record = self.repository.create(
            telegram_file_id=doc.file_id,
            telegram_message_id=message.message_id,
            user_id=message.user.id if message.user is not None else None,
            file_name=doc.file_name,
            file_size=doc.file_size,
            mime_type=doc.mime_type,
            created_at=message.date,
            source_path=message.source_path,
            expires_at=expires_at,
            max_downloads=max_downloads,
        )
        return self._assign_public_id(record.id)

    def get_link(self, public_id: str) -> FileRecord | None:
        """Retrieve a record by its public id (used by /info and /delete)."""
        return self.repository.get_by_public_id(public_id)

    def delete_link(self, public_id: str) -> FileRecord | None:
        """Idempotently mark a link deleted; returns the record or None.

        Deletion only flips ``status`` to ``deleted`` — metadata is
        preserved and the physical source file is left untouched.
        """
        record = self.repository.get_by_public_id(public_id)
        if record is None:
            return None
        return self.repository.set_status(record.id, "deleted")

    def _assign_public_id(self, record_id: int) -> FileRecord:
        """Attach a fresh public id, retrying if it already exists.

        The UNIQUE index on ``public_id`` is the source of truth: on a
        collision (IntegrityError) a new id is generated and the update is
        retried — an existing file's id is never overwritten.
        """
        for _ in range(MAX_PUBLIC_ID_ATTEMPTS):
            public_id = generate_public_id()
            try:
                record = self.repository.update(record_id, public_id=public_id)
            except sqlite3.IntegrityError:
                continue  # collision — try again with a fresh id
            assert record is not None, "update returned None for existing row"
            return record
        raise PublicIdError(
            f"could not assign a unique public id after "
            f"{MAX_PUBLIC_ID_ATTEMPTS} attempts"
        )


def make_file_service(database: str | Path | None = None) -> FileService:
    """Build a :class:`FileService` backed by the given database.

    ``None`` uses the configured ``DATABASE_PATH``; ``":memory:"`` gives an
    in-memory database (handy for tests). Schema is created if missing.
    """
    return FileService(FileRepository(initialize(database)))