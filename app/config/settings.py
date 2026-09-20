"""Application configuration, sourced from environment variables.

Values may be set in a local ``.env`` file (loaded by python-dotenv) or
exported in the shell. The real ``.env`` is never committed; see
``.env.example`` for the supported variables.

Invalid or unsafe configuration fails fast: :class:`Settings` raises
``ValueError`` from its constructor. Error messages never include secret
values (tokens, API id/hash), only the offending variable and its value for
non-secret variables.
"""
import ipaddress
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

# Project root: app/config/settings.py -> parents[0]=config, [1]=app, [2]=root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Load variables from a .env file if present. Harmless when absent.
load_dotenv()

VALID_BOT_MODES = ("mock", "real")

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _get_optional(name: str) -> str | None:
    """Return the env var as a stripped string, or None when unset/empty."""
    value = os.getenv(name)
    return value.strip() if value else None


def _get_int(name: str, default: int) -> int:
    """Return an integer env var, or ``default`` when unset. Fail fast on junk."""
    raw = _get_optional(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def _get_bool(name: str, default: bool) -> bool:
    """Return a boolean env var (1/true/yes/on, 0/false/no/off), or default."""
    raw = _get_optional(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def _get_list(name: str) -> list[str]:
    """Return a comma-separated env var as a list of non-empty items."""
    raw = _get_optional(name)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


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
        self.base_url: str = (
            _get_optional("BASE_URL") or "http://localhost:8000"
        ).rstrip("/")
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
        # Phase 14B: FileToLink's *own* storage for received files. The Local
        # Bot API Server may clean up its work dir at any time, so the bytes
        # of an active link must live under this directory instead.
        self.file_storage_dir: str = self._resolve_file_storage_dir()
        # Phase 14A: read timeout for the Local Bot API ``getFile`` request.
        # Large files (hundreds of MB) can make the request take far longer
        # than PTB's small default, so it gets its own configurable timeout.
        # This applies ONLY to file retrieval, never to polling/getUpdates.
        self.telegram_get_file_read_timeout_seconds: int = _get_int(
            "TELEGRAM_GET_FILE_READ_TIMEOUT_SECONDS", 300
        )
        # Phase 11: abuse protection (public download endpoint) + proxy trust.
        self.rate_limit_enabled: bool = _get_bool("RATE_LIMIT_ENABLED", True)
        self.rate_limit_requests: int = _get_int("RATE_LIMIT_REQUESTS", 60)
        self.rate_limit_window_seconds: int = _get_int("RATE_LIMIT_WINDOW_SECONDS", 60)
        self.rate_limit_max_keys: int = _get_int("RATE_LIMIT_MAX_KEYS", 10_000)
        self.trust_proxy_headers: bool = _get_bool("TRUST_PROXY_HEADERS", False)
        self.trusted_proxies: frozenset[str] = self._resolve_trusted_proxies()
        self._validate()

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

    def _resolve_file_storage_dir(self) -> str:
        """FileToLink-owned file storage (Phase 14B).

        FILE_STORAGE_DIR env var, or ``data/storage`` under the project root.
        Deliberately *not* ``TELEGRAM_BOT_WORK_DIR``: that directory is managed
        by the Local Bot API Server (which may evict files at any time), while
        this one is owned by FileToLink and must survive as long as a link does.
        Relative paths resolve against the project root.
        """
        raw = _get_optional("FILE_STORAGE_DIR")
        if not raw:
            return str(PROJECT_ROOT / "data" / "storage")
        path = Path(raw)
        return str(path if path.is_absolute() else PROJECT_ROOT / path)

    def _resolve_trusted_proxies(self) -> frozenset[str]:
        """Parse TRUSTED_PROXIES into a set of validated IP address strings."""
        proxies: set[str] = set()
        for entry in _get_list("TRUSTED_PROXIES"):
            try:
                proxies.add(str(ipaddress.ip_address(entry)))
            except ValueError:
                raise ValueError(
                    f"TRUSTED_PROXIES contains an invalid IP address: {entry!r}"
                ) from None
        return frozenset(proxies)

    def _validate(self) -> None:
        """Fail fast on unsupported or unsafe security-sensitive values."""
        if self.bot_mode not in VALID_BOT_MODES:
            raise ValueError(
                f"BOT_MODE must be one of {VALID_BOT_MODES}, got {self.bot_mode!r}"
            )
        parsed = urlsplit(self.base_url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "BASE_URL must be an absolute http(s) URL, "
                f"got {self.base_url!r}"
            )
        if not self.host or any(ch in self.host for ch in "/ \t"):
            raise ValueError(
                f"HOST must be a host name or IP address, got {self.host!r}"
            )
        if not 1 <= self.port <= 65535:
            raise ValueError(f"PORT must be between 1 and 65535, got {self.port}")
        if self.rate_limit_requests < 1:
            raise ValueError("RATE_LIMIT_REQUESTS must be >= 1")
        if self.rate_limit_window_seconds < 1:
            raise ValueError("RATE_LIMIT_WINDOW_SECONDS must be >= 1")
        if self.rate_limit_max_keys < 1:
            raise ValueError("RATE_LIMIT_MAX_KEYS must be >= 1")
        if self.telegram_get_file_read_timeout_seconds < 1:
            raise ValueError(
                "TELEGRAM_GET_FILE_READ_TIMEOUT_SECONDS must be >= 1"
            )
        if self._same_path(self.file_storage_dir, self.telegram_bot_work_dir):
            raise ValueError(
                "FILE_STORAGE_DIR must not be TELEGRAM_BOT_WORK_DIR: FileToLink "
                "storage is its own permanent copy, while the Local Bot API "
                "Server may clean up its work directory at any time"
            )
        if self.trust_proxy_headers and not self.trusted_proxies:
            raise ValueError(
                "TRUST_PROXY_HEADERS is enabled but TRUSTED_PROXIES is empty; "
                "list the proxy IP addresses whose X-Forwarded-For may be trusted"
            )

    @staticmethod
    def _same_path(first: str, second: str) -> bool:
        """Whether two configured directories resolve to the same location."""
        try:
            return Path(first).resolve() == Path(second).resolve()
        except OSError:  # unreadable/odd path: fall back to a lexical compare
            return os.path.normcase(first) == os.path.normcase(second)

    @property
    def is_mock(self) -> bool:
        return self.bot_mode == "mock"


@lru_cache
def get_settings() -> Settings:
    """Return the (cached) application settings."""
    return Settings()
