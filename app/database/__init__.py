"""Database layer: SQLite connection handling and schema initialization.

The database is deliberately isolated from Telegram/mock transport, bot
handlers, and API code. Transport layers use :class:`app.services.files.FileService`
(which uses the repository in :mod:`app.database.files`); no raw SQL
leaks into handlers.
"""
from .connection import connect, initialize, initialize_schema
from .files import FileRepository
from .records import FileRecord

__all__ = [
    "connect",
    "initialize",
    "initialize_schema",
    "FileRepository",
    "FileRecord",
]