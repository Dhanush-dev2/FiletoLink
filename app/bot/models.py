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
class Message:
    message_id: int
    text: str | None = None
    user: User | None = None
    # `document` (file info) will be added when file receiving lands.


@dataclass(frozen=True)
class Update:
    update_id: int
    message: Message | None = None