"""Application services for file handling.

This layer sits between the (framework-agnostic) bot handlers and the
database repository: it knows how to turn a normalized file message into
a stored record, keeping raw SQL out of handlers and transports.
"""
from pathlib import Path

from ..bot.models import Message
from ..database.connection import initialize
from ..database.files import FileRecord, FileRepository


class FileService:
    """Turns normalized file messages into stored database records."""

    def __init__(self, repository: FileRepository) -> None:
        self.repository = repository

    def save_document(self, message: Message) -> FileRecord:
        """Persist the metadata of a file message and return the record."""
        if message.document is None:
            raise ValueError("message has no document")
        doc = message.document
        return self.repository.create(
            telegram_file_id=doc.file_id,
            telegram_message_id=message.message_id,
            user_id=message.user.id if message.user is not None else None,
            file_name=doc.file_name,
            file_size=doc.file_size,
            mime_type=doc.mime_type,
            created_at=message.date,
        )


def make_file_service(database: str | Path | None = None) -> FileService:
    """Build a :class:`FileService` backed by the given database.

    ``None`` uses the configured ``DATABASE_PATH``; ``":memory:"`` gives an
    in-memory database (handy for tests). Schema is created if missing.
    """
    return FileService(FileRepository(initialize(database)))