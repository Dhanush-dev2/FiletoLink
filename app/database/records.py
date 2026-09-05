"""Row model for the ``files`` table (database layer)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class FileRecord:
    """One row of the ``files`` table.

    ``public_id`` is NULL until link generation lands (Phase 4).
    ``expires_at`` NULL means the link never expires.
    ``status`` is one of ``active`` / ``deleted`` / ``expired``.
    Timestamps are Unix epoch seconds (UTC).
    """

    id: int
    public_id: str | None
    telegram_file_id: str
    telegram_message_id: int
    user_id: int | None
    file_name: str | None
    file_size: int | None
    mime_type: str | None
    created_at: int
    expires_at: int | None
    download_count: int
    status: str
    source_path: str | None