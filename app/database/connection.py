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
    max_downloads INTEGER,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'deleted', 'expired')),
    source_path TEXT
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
    # check_same_thread=False: FastAPI runs sync endpoints in a threadpool,
    # so the connection is used from request threads, not just the creating
    # thread. SQLite serializes access internally; transactions here are
    # short (single execute + commit), which keeps this safe for a local app.
    conn = sqlite3.connect(database, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, declaration: str
) -> None:
    """ALTER TABLE migration for databases created before the column existed."""
    columns = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Create the tables and indexes if they do not exist yet.

    Also applies lightweight migrations for pre-existing databases.
    """
    conn.executescript(SCHEMA)
    _add_column_if_missing(conn, "files", "source_path", "TEXT")
    _add_column_if_missing(conn, "files", "max_downloads", "INTEGER")
    conn.commit()


def initialize(database: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection and ensure the schema exists. Idempotent."""
    conn = connect(database)
    initialize_schema(conn)
    return conn