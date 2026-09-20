"""Shared pytest configuration.

Deployment reproducibility (Phase 15): a real deployment keeps a populated
``.env`` (``BOT_MODE=real``, ``BASE_URL``, ``TELEGRAM_API_*``, ...) and
``app.config`` loads it via ``load_dotenv()`` at import time. Several tests
assert the *shipped defaults* (e.g. ``BOT_MODE=mock``,
``BASE_URL=http://localhost:8000``), which only holds when those variables
are absent — otherwise test results would depend on whatever the operator's
machine happens to run. The suite must be hermetic: identical results on a
fresh clone and on an operator machine with a fully configured ``.env``.

The autouse fixture below removes every FileToLink environment variable for
the duration of each test and drops the cached :class:`~app.config.Settings`,
so tests always start from the documented defaults. Tests that need specific
values set them explicitly with ``monkeypatch.setenv`` (applied after this
fixture, so explicit values still win), and the real process environment is
restored afterwards — nothing here touches the operator's ``.env`` file.
"""
import pytest

from app.config import get_settings

# Every environment variable app/config/settings.py reads. Keep in sync with
# that module (test_deployment.py guards that .env.example documents them).
SETTINGS_ENV_VARS = (
    "BOT_TOKEN",
    "BOT_USERNAME",
    "BOT_MODE",
    "BASE_URL",
    "HOST",
    "PORT",
    "DATABASE_PATH",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "LOCAL_BOT_API_URL",
    "TELEGRAM_BOT_WORK_DIR",
    "FILE_STORAGE_DIR",
    "TELEGRAM_GET_FILE_READ_TIMEOUT_SECONDS",
    "RATE_LIMIT_ENABLED",
    "RATE_LIMIT_REQUESTS",
    "RATE_LIMIT_WINDOW_SECONDS",
    "RATE_LIMIT_MAX_KEYS",
    "TRUST_PROXY_HEADERS",
    "TRUSTED_PROXIES",
)


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch):
    """Run each test against the shipped defaults, not the deployment .env."""
    for name in SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
