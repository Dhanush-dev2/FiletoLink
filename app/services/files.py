"""Application services for file handling.

This layer sits between the (framework-agnostic) bot handlers and the
database repository: it knows how to turn a normalized file message into
a stored record — including generating and assigning its unique public id —
keeping raw SQL out of handlers and transports.

Ownership of the bytes (Phase 14B): when a
:class:`~app.services.storage.FileStorage` is configured, the service also
*takes ownership* of the incoming file. The Telegram Local Bot API Server
stores received files in a directory it manages and may clean up, so a link
must not point there. Instead:

1. generate the public id (the storage layout is keyed by it),
2. transfer the file into FileToLink-owned storage and verify the
   destination,
3. *then* create the record with the FileToLink-owned ``source_path``.

The durable invariant: an ``active`` record always has a valid,
FileToLink-owned source file. A failed transfer creates no record and no
public id; a record that cannot be written rolls the adopted file back, so no
unreferenced orphan is left in storage.
"""
import sqlite3
from pathlib import Path

from ..bot.models import Message
from ..database.connection import initialize
from ..database.files import FileRecord, FileRepository
from ..utils.public_ids import generate_public_id
from .storage import FileStorage, StorageError

# Bounded retries for the (astronomically unlikely) public id collision.
MAX_PUBLIC_ID_ATTEMPTS = 10


class PublicIdError(RuntimeError):
    """Raised when a public id could not be assigned after retries."""


class FileService:
    """Turns normalized file messages into stored database records.

    ``storage`` is optional: without one the service records the message's
    ``source_path`` as-is (mock transport / tests). With one, the file is
    taken into FileToLink-owned storage first (the real Telegram flow).
    """

    def __init__(
        self, repository: FileRepository, storage: FileStorage | None = None
    ) -> None:
        self.repository = repository
        self.storage = storage

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

        With storage configured the file is transferred into FileToLink
        storage *before* the record exists (and rolled back if the record
        cannot be written). Without storage the message's ``source_path`` is
        recorded unchanged. Either way the public id is generated first and
        stored in the same INSERT as the record.
        """
        if message.document is None:
            raise ValueError("message has no document")
        if self.storage is None:
            for _ in range(MAX_PUBLIC_ID_ATTEMPTS):
                try:
                    return self._insert(
                        message,
                        source_path=message.source_path,
                        expires_at=expires_at,
                        max_downloads=max_downloads,
                    )
                except sqlite3.IntegrityError:
                    continue  # collision — try again with a fresh id
            raise PublicIdError(
                f"could not assign a unique public id after "
                f"{MAX_PUBLIC_ID_ATTEMPTS} attempts"
            )
        return self._adopt_and_insert(
            message, expires_at=expires_at, max_downloads=max_downloads
        )

    def get_link(self, public_id: str) -> FileRecord | None:
        """Retrieve a record by its public id (used by /info and /delete)."""
        return self.repository.get_by_public_id(public_id)

    def delete_link(self, public_id: str) -> FileRecord | None:
        """Idempotently mark a link deleted; returns the record or None.

        Deletion flips ``status`` to ``deleted`` first — the link is dead from
        that moment — and only then removes the FileToLink-owned file when a
        storage service is configured and the path really is ours (a
        Telegram-managed file is never touched). A filesystem cleanup failure
        is logged and reported internally but never turns an otherwise
        successful deletion into an error, and never exposes a path.
        """
        record = self.repository.get_by_public_id(public_id)
        if record is None:
            return None
        deleted = self.repository.set_status(record.id, "deleted")
        if deleted is not None and self.storage is not None and record.source_path:
            self.storage.remove_owned(record.source_path)
        return deleted

    # ── recording ──────────────────────────────────────────────────────────

    def _adopt_and_insert(
        self,
        message: Message,
        *,
        expires_at: int | None,
        max_downloads: int | None,
    ) -> FileRecord:
        """Take ownership of the message's file, then create its record.

        Order matters: the file is transferred and verified before the record
        (and therefore the public link) exists, so a failed transfer can never
        produce an active link without bytes.
        """
        assert self.storage is not None  # guarded by save_document
        assert message.document is not None  # guarded by save_document
        source = message.source_path
        if not source:
            raise StorageError("the received file has no local source path to own")
        public_id = self._new_public_id()
        owned_path = self.storage.adopt(
            source, public_id=public_id, file_name=message.document.file_name
        )
        for _ in range(MAX_PUBLIC_ID_ATTEMPTS):
            try:
                return self._insert(
                    message,
                    public_id=public_id,
                    source_path=owned_path,
                    expires_at=expires_at,
                    max_downloads=max_downloads,
                )
            except sqlite3.IntegrityError:
                pass  # id taken in the meantime — re-key and retry below
            except Exception:
                # Transfer succeeded but the record could not be written: the
                # file is not referenced by anything, so roll it back instead of
                # leaving an uncontrolled orphan in storage.
                self.storage.remove_owned(owned_path)
                raise

            # The id was taken between the pre-check and the INSERT (a 48-bit
            # random id, so this is essentially impossible). Re-key the already
            # adopted file to a fresh id and retry: neither an orphan file nor
            # an unrestorable file is left behind.
            try:
                new_id = self._new_public_id()
            except PublicIdError:
                self.storage.remove_owned(owned_path)
                raise
            rekeyed = self.storage.rekey(public_id, new_id)
            if rekeyed is None:
                break
            public_id, owned_path = new_id, rekeyed
        self.storage.remove_owned(owned_path)
        raise PublicIdError(
            f"could not assign a unique public id after "
            f"{MAX_PUBLIC_ID_ATTEMPTS} attempts"
        )

    def _insert(
        self,
        message: Message,
        *,
        source_path: str | None,
        expires_at: int | None,
        max_downloads: int | None,
        public_id: str | None = None,
    ) -> FileRecord:
        """INSERT the record, returning it with its public id attached.

        Without a pre-generated id this generates one and retries on the
        (astronomically unlikely) UNIQUE collision; an existing file's id is
        never overwritten.
        """
        assert message.document is not None  # guarded by save_document
        doc = message.document
        if public_id is None:
            public_id = self._new_public_id()
        return self.repository.create(
            telegram_file_id=doc.file_id,
            telegram_message_id=message.message_id,
            user_id=message.user.id if message.user is not None else None,
            file_name=doc.file_name,
            file_size=doc.file_size,
            mime_type=doc.mime_type,
            created_at=message.date,
            source_path=source_path,
            expires_at=expires_at,
            max_downloads=max_downloads,
            public_id=public_id,
        )

    def _new_public_id(self) -> str:
        """A fresh public id that no stored record is using.

        The id is *generated* here but reserved by the INSERT (the UNIQUE index
        is the source of truth); this pre-check just keeps the reservation step
        from failing in practice.
        """
        for _ in range(MAX_PUBLIC_ID_ATTEMPTS):
            public_id = generate_public_id()
            if self.repository.get_by_public_id(public_id) is None:
                return public_id
        raise PublicIdError(
            f"could not assign a unique public id after "
            f"{MAX_PUBLIC_ID_ATTEMPTS} attempts"
        )


def make_file_service(
    database: str | Path | None = None,
    storage: FileStorage | None = None,
) -> FileService:
    """Build a :class:`FileService` backed by the given database.

    ``None`` uses the configured ``DATABASE_PATH``; ``":memory:"`` gives an
    in-memory database (handy for tests). Schema is created if missing. Pass a
    :class:`~app.services.storage.FileStorage` to have the service take
    ownership of received files (the real Telegram flow).
    """
    return FileService(FileRepository(initialize(database)), storage)
