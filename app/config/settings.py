"""Application configuration, sourced from environment variables.

Values may be set in a local ``.env`` file (loaded by python-dotenv) or
exported in the shell. The real ``.env`` is never committed; see
``.env.example`` for the supported variables.
"""
import os
from functools import lru_cache

from dotenv import load_dotenv

# Load variables from a .env file if present. Harmless when absent.
load_dotenv()


def _get_optional(name: str) -> str | None:
    """Return the env var as a stripped string, or None when unset/empty."""
    value = os.getenv(name)
    return value.strip() if value else None


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _default_mode(bot_token: str | None) -> str:
    """Without a token the only working mode is mock."""
    return "mock" if not bot_token else "real"


class Settings:
    """Read-only settings. Access via :func:`get_settings`."""

    def __init__(self) -> None:
        self.bot_token: str | None = _get_optional("BOT_TOKEN")
        self.bot_username: str = _get_optional("BOT_USERNAME") or "file_to_link_bot"
        # bot_mode: "mock" (default, no token required) or "real" (later phase).
        self.bot_mode: str = (
            _get_optional("BOT_MODE") or _default_mode(self.bot_token)
        ).lower()
        self.base_url: str = (_get_optional("BASE_URL") or "http://localhost:8000").rstrip("/")
        self.host: str = _get_optional("HOST") or "127.0.0.1"
        self.port: int = _get_int("PORT", 8000)

    @property
    def is_mock(self) -> bool:
        return self.bot_mode == "mock"


@lru_cache
def get_settings() -> Settings:
    """Return the (cached) application settings."""
    return Settings()