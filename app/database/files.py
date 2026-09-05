"""File repository: SQL access to the ``files`` table.

This is the only place raw SQL lives. Handlers and transports talk to this
repository (usually through :class:`app.services.files.FileService`).

Threading: the download API runs its sync endpoints in a threadpool, so the
single shared ``sqlite3`` connection is used from several threads. pysqlite
does not serialize concurrent ``execute()`` calls on one connection (that is
what ``check_same_thread=False`` means), so every database operation here is
guarded by a lock. Each operation is a single short statement + commit, and
SQLite's own locking still applies between processes/connections.
"""
import sqlite3
import threading
import time
from contextlib import contextmanager

from .records import FileRecord

_ALLOWED_STATUSES = {"active", "deleted", "expired"}

# Sentinel distinguishing "field not provided" from an explicit None value.
_UNSET = object()

_SELECT_COLUMNS = (
    "id, public_id, telegram_file_id, telegram_message_id, user_id, "
    "file_name, file_size, mime_type, created_at, expires_at, "
    "download_count, max_downloads, status, source_path"
)


def _row_to_record(row: sqlite3.Row) -> FileRecord:
    return FileRecord(**dict(row))


class FileRepository:
    """CRUD operations on file records."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._lock = threading.RLock()

    @contextmanager
    def _transaction(self):
        """Run one statement and commit; roll back and re-raise on SQL error.

        The lock serializes access to the shared connection so concurrent
        threadpool requests cannot corrupt pysqlite's statement state.
        """
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()  # keep the connection usable for retries
                raise

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
        source_path: str | None = None,
        expires_at: int | None = None,
        max_downloads: int | None = None,
    ) -> FileRecord:
        """Insert a new file record and return it. ``created_at`` defaults to now.

        ``expires_at`` (NULL = never) and ``max_downloads`` (NULL = unlimited)
        are optional link-management settings.
        """
        if max_downloads is not None and max_downloads < 0:
            raise ValueError(f"max_downloads must be >= 0, got {max_downloads!r}")
        created_at = int(time.time()) if created_at is None else created_at
        with self._transaction() as conn:
            cur = conn.execute(
                "INSERT INTO files "
                "(telegram_file_id, telegram_message_id, user_id, file_name, "
                " file_size, mime_type, created_at, expires_at, max_downloads, "
                " source_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    telegram_file_id,
                    telegram_message_id,
                    user_id,
                    file_name,
                    file_size,
                    mime_type,
                    created_at,
                    expires_at,
                    max_downloads,
                    source_path,
                ),
            )
        return self.get_by_id(cur.lastrowid)

    def get_by_id(self, record_id: int) -> FileRecord | None:
        """Retrieve a record by its row id, or None when missing."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM files WHERE id = ?", (record_id,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def get_by_telegram_file_id(self, telegram_file_id: str) -> FileRecord | None:
        """Retrieve the most recent record for a Telegram file id, or None."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM files "
                "WHERE telegram_file_id = ? ORDER BY id DESC LIMIT 1",
                (telegram_file_id,),
            ).fetchone()
        return _row_to_record(row) if row else None

    def get_by_public_id(self, public_id: str) -> FileRecord | None:
        """Retrieve the record for a public id, or None when unknown."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM files WHERE public_id = ?",
                (public_id,),
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
        max_downloads: int | None = _UNSET,
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
            ("max_downloads", max_downloads),
            ("status", status),
        ):
            if value is not _UNSET:
                if name == "status" and value not in _ALLOWED_STATUSES:
                    raise ValueError(f"invalid status: {value!r}")
                if name == "max_downloads" and value is not None and value < 0:
                    raise ValueError(f"max_downloads must be >= 0, got {value!r}")
                fields[name] = value

        if not fields:
            return self.get_by_id(record_id)

        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._transaction() as conn:
            conn.execute(
                f"UPDATE files SET {assignments} WHERE id = ?",
                (*fields.values(), record_id),
            )
        return self.get_by_id(record_id)

    def set_status(self, record_id: int, status: str) -> FileRecord | None:
        """Invalidate/restore a record via its status; returns it or None."""
        return self.update(record_id, status=status)

    # ── link lifecycle (expiration + download limits) ──────────────────────

    def reserve_download_slot(self, record_id: int, now: int | None = None) -> bool:
        """Atomically reserve one download slot, or return False.

        One conditional UPDATE enforces every gate in a single statement, so
        there is no check-then-act race: the record must be ``active``, its
        ``expires_at`` must not have passed, and (when set) its download
        count must still be below ``max_downloads``. Concurrent requests can
        therefore never exceed the limit.

        When the increment reaches ``max_downloads`` (the final permitted
        download), the same statement terminalizes the record with the
        existing ``expired`` status — the ``active → expired`` transition
        happens immediately, atomically, with no separate read/check.
        """
        now = int(time.time()) if now is None else now
        with self._transaction() as conn:
            cur = conn.execute(
                "UPDATE files SET "
                "  download_count = download_count + 1, "
                "  status = CASE "
                "    WHEN max_downloads IS NOT NULL "
                "         AND download_count + 1 >= max_downloads "
                "    THEN 'expired' ELSE status END "
                "WHERE id = ? AND status = 'active' "
                "  AND (expires_at IS NULL OR expires_at > ?) "
                "  AND (max_downloads IS NULL OR download_count < max_downloads)",
                (record_id, now),
            )
        return cur.rowcount == 1

    def release_download_slot(
        self, record_id: int, now: int | None = None
    ) -> None:
        """Undo a reservation that could not be fulfilled (e.g. the source
        file is missing), so failed requests do not burn a download.

        If the reservation had terminalized the link (it consumed the final
        slot) but nothing was actually served, the ``expired`` status is
        reverted to ``active`` — but only when the counter drops back below
        the limit and the link is not time-expired. A genuinely finished or
        time-expired link is never reactivated; the ``download_count > 0``
        guard keeps the counter from going negative. All of this happens in
        one atomic statement.
        """
        now = int(time.time()) if now is None else now
        with self._transaction() as conn:
            conn.execute(
                "UPDATE files SET "
                "  download_count = download_count - 1, "
                "  status = CASE "
                "    WHEN status = 'expired' "
                "         AND max_downloads IS NOT NULL "
                "         AND download_count - 1 < max_downloads "
                "         AND (expires_at IS NULL OR expires_at > ?) "
                "    THEN 'active' ELSE status END "
                "WHERE id = ? AND download_count > 0",
                (now, record_id),
            )

    def expire_if_due(self, record_id: int, now: int | None = None) -> FileRecord | None:
        """Lazily mark a record ``expired`` when its ``expires_at`` passed.

        No background worker is used: the transition happens on access. The
        conditional UPDATE is idempotent and only flips ``active`` records.
        """
        now = int(time.time()) if now is None else now
        with self._transaction() as conn:
            conn.execute(
                "UPDATE files SET status = 'expired' "
                "WHERE id = ? AND status = 'active' "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                (record_id, now),
            )
        return self.get_by_id(record_id)

    def mark_exhausted(self, record_id: int) -> FileRecord | None:
        """Terminalize a link whose download limit has been reached.

        Reuses the existing ``expired`` terminal status (no new status is
        introduced): the record is flipped only when it is still ``active``
        and its download count is at the limit, so it is safe under
        concurrency and never reactivates anything.
        """
        with self._transaction() as conn:
            conn.execute(
                "UPDATE files SET status = 'expired' "
                "WHERE id = ? AND status = 'active' "
                "AND max_downloads IS NOT NULL "
                "AND download_count >= max_downloads",
                (record_id,),
            )
        return self.get_by_id(record_id)

    def delete(self, record_id: int) -> bool:
        """Hard-delete a record; returns True when a row was removed."""
        with self._transaction() as conn:
            cur = conn.execute("DELETE FROM files WHERE id = ?", (record_id,))
        return cur.rowcount > 0