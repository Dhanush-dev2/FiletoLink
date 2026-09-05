"""Minimal, framework-agnostic representations of Telegram updates.

Bot logic only ever sees these types. The mock transport (``app/bot/mock.py``)
builds them directly; the real python-telegram-bot integration (a later
phase) will translate incoming updates into the same types. This keeps the
handlers independent of the Telegram transport.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    id: int
    username: str | None = None
    first_name: str | None = None


@dataclass(frozen=True)
class Document:
    """A file attached to a message (Telegram 'document')."""

    file_id: str
    file_name: str | None = None
    file_size: int | None = None  # bytes
    mime_type: str | None = None


@dataclass(frozen=True)
class Message:
    message_id: int
    text: str | None = None
    user: User | None = None
    document: Document | None = None
    date: int | None = None  # Unix epoch seconds, Telegram-style
    # Transport hint: where the file bytes can be read. The mock transport
    # sets this to a local path; the real Telegram integration will use its
    # own file source instead and leave this None.
    source_path: str | None = None


@dataclass(frozen=True)
class Update:
    update_id: int
    message: Message | None = None