"""File repository: SQL access to the ``files`` table.

This is the only place raw SQL lives. Handlers and transports talk to this
repository (usually through :class:`app.services.files.FileService`).
"""
import sqlite3
import time

from .records import FileRecord

_ALLOWED_STATUSES = {"active", "deleted", "expired"}

# Sentinel distinguishing "field not provided" from an explicit None value.
_UNSET = object()

_SELECT_COLUMNS = (
    "id, public_id, telegram_file_id, telegram_message_id, user_id, "
    "file_name, file_size, mime_type, created_at, expires_at, "
    "download_count, status"
)


def _row_to_record(row: sqlite3.Row) -> FileRecord:
    return FileRecord(**dict(row))


class FileRepository:
    """CRUD operations on file records."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def create(
        self,
        *,
        telegram_file_id: str,
        telegram_message_id: int,
        user_id: int | None,
        file_name: str | None = None,
        file_size: int | None = None,
        mime_type: str | None = None,
        created_at: int | None = None,
    ) -> FileRecord:
        """Insert a new file record and return it. ``created_at`` defaults to now."""
        created_at = int(time.time()) if created_at is None else created_at
        try:
            cur = self._conn.execute(
                "INSERT INTO files "
                "(telegram_file_id, telegram_message_id, user_id, file_name, "
                " file_size, mime_type, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    telegram_file_id,
                    telegram_message_id,
                    user_id,
                    file_name,
                    file_size,
                    mime_type,
                    created_at,
                ),
            )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise
        return self.get_by_id(cur.lastrowid)

    def get_by_id(self, record_id: int) -> FileRecord | None:
        """Retrieve a record by its row id, or None when missing."""
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM files WHERE id = ?", (record_id,)
        ).fetchone()
        return _row_to_record(row) if row else None

    def get_by_telegram_file_id(self, telegram_file_id: str) -> FileRecord | None:
        """Retrieve the most recent record for a Telegram file id, or None."""
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM files "
            "WHERE telegram_file_id = ? ORDER BY id DESC LIMIT 1",
            (telegram_file_id,),
        ).fetchone()
        return _row_to_record(row) if row else None

    def update(
        self,
        record_id: int,
        *,
        public_id: str | None = _UNSET,
        file_name: str | None = _UNSET,
        file_size: int | None = _UNSET,
        mime_type: str | None = _UNSET,
        expires_at: int | None = _UNSET,
        download_count: int | None = _UNSET,
        status: str = _UNSET,
    ) -> FileRecord | None:
        """Update only the provided fields; returns the updated record or None."""
        fields: dict[str, object] = {}
        for name, value in (
            ("public_id", public_id),
            ("file_name", file_name),
            ("file_size", file_size),
            ("mime_type", mime_type),
            ("expires_at", expires_at),
            ("download_count", download_count),
            ("status", status),
        ):
            if value is not _UNSET:
                if name == "status" and value not in _ALLOWED_STATUSES:
                    raise ValueError(f"invalid status: {value!r}")
                fields[name] = value

        if not fields:
            return self.get_by_id(record_id)

        assignments = ", ".join(f"{name} = ?" for name in fields)
        try:
            self._conn.execute(
                f"UPDATE files SET {assignments} WHERE id = ?",
                (*fields.values(), record_id),
            )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()  # keep the connection usable for retries
            raise
        return self.get_by_id(record_id)

    def set_status(self, record_id: int, status: str) -> FileRecord | None:
        """Invalidate/restore a record via its status; returns it or None."""
        return self.update(record_id, status=status)

    def delete(self, record_id: int) -> bool:
        """Hard-delete a record; returns True when a row was removed."""
        try:
            cur = self._conn.execute("DELETE FROM files WHERE id = ?", (record_id,))
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise
        return cur.rowcount > 0