"""SQLite connection management.

The database file lives in the project's ``data/`` directory by default and
its location is configurable via ``DATABASE_PATH`` (see
:class:`app.config.settings.Settings`). ``":memory:"`` is supported for
tests. Everything runs locally — no external database server, ₹0 cost.
"""
import sqlite3
from pathlib import Path

from ..config import get_settings

# SQLite schema for the files table. `public_id` is prepared (UNIQUE, NULL
# until generated in Phase 4); no public links are generated yet.
SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT,
    telegram_file_id TEXT NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    user_id INTEGER,
    file_name TEXT,
    file_size INTEGER,
    mime_type TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    download_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'deleted', 'expired'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_files_public_id ON files (public_id);
CREATE INDEX IF NOT EXISTS idx_files_telegram_file_id ON files (telegram_file_id);
CREATE INDEX IF NOT EXISTS idx_files_status ON files (status);
"""


def connect(database: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection, creating the parent directory when needed.

    ``database=None`` uses the configured ``DATABASE_PATH``;
    ``":memory:"`` returns an in-memory database (tests).
    """
    if database is None:
        database = get_settings().database_path
    database = str(database)
    if database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Create the tables and indexes if they do not exist yet."""
    conn.executescript(SCHEMA)
    conn.commit()


def initialize(database: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection and ensure the schema exists. Idempotent."""
    conn = connect(database)
    initialize_schema(conn)
    return conn