"""Application configuration, sourced from environment variables.

Values may be set in a local ``.env`` file (loaded by python-dotenv) or
exported in the shell. The real ``.env`` is never committed; see
``.env.example`` for the supported variables.
"""
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# Project root: app/config/settings.py -> parents[0]=config, [1]=app, [2]=root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

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
        # bot_mode: "mock" (default, no token required) or "real".
        self.bot_mode: str = (
            _get_optional("BOT_MODE") or _default_mode(self.bot_token)
        ).lower()
        self.base_url: str = (_get_optional("BASE_URL") or "http://localhost:8000").rstrip("/")
        self.host: str = _get_optional("HOST") or "127.0.0.1"
        self.port: int = _get_int("PORT", 8000)
        self.database_path: str = self._resolve_database_path()
        # Real Telegram (BOT_MODE=real): credentials and Local Bot API Server.
        self.telegram_api_id: str | None = _get_optional("TELEGRAM_API_ID")
        self.telegram_api_hash: str | None = _get_optional("TELEGRAM_API_HASH")
        self.local_bot_api_url: str = (
            _get_optional("LOCAL_BOT_API_URL") or "http://127.0.0.1:8081"
        ).rstrip("/")
        self.telegram_bot_work_dir: str = self._resolve_work_dir()

    def _resolve_database_path(self) -> str:
        """SQLite location: DATABASE_PATH env var or data/files.db under the
        project root. Relative paths resolve against the project root."""
        raw = _get_optional("DATABASE_PATH")
        if not raw:
            return str(PROJECT_ROOT / "data" / "files.db")
        path = Path(raw)
        return str(path if path.is_absolute() else PROJECT_ROOT / path)

    def _resolve_work_dir(self) -> str:
        """Local Bot API Server working directory (--dir).

        TELEGRAM_BOT_WORK_DIR env var, or telegram_bot_data/ under the
        project root. Relative paths resolve against the project root.
        """
        raw = _get_optional("TELEGRAM_BOT_WORK_DIR")
        if not raw:
            return str(PROJECT_ROOT / "telegram_bot_data")
        path = Path(raw)
        return str(path if path.is_absolute() else PROJECT_ROOT / path)

    @property
    def is_mock(self) -> bool:
        return self.bot_mode == "mock"


@lru_cache
def get_settings() -> Settings:
    """Return the (cached) application settings."""
    return Settings()