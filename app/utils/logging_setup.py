"""Process logging configuration — one place, secret-free, console-only.

Why this module exists (Phase 19): the real bot talks to the Local Bot API
Server through URLs of the form ``<LOCAL_BOT_API_URL>/bot<BOT_TOKEN>/<method>``.
``httpx`` logs every request URL at ``INFO`` (``HTTP Request: POST http://...``)
and ``httpcore`` does the same at ``DEBUG``, so a plain
``logging.basicConfig(level=INFO)`` printed the bot token to the console —
observed on the real deployment before this module existed.

Two independent protections, because a token in a log file is a live
credential:

1. **Suppress the root cause**: the ``httpx``/``httpcore`` loggers are raised to
   ``WARNING``, so request URLs (and therefore tokens) are never formatted.
2. **Redact anything that still contains a token**: a filter on every root
   handler masks bot-token shapes, so even a third-party library logging the
   URL at WARNING/ERROR — or a future dependency — cannot leak it.

Logging stays console-only (stdout/stderr of the window each process runs in):
no log files, no rotation, no logging framework beyond the standard library.
"""

import logging
import re

# A Telegram bot token looks like "<bot id>:<secret>" — digits, a colon, and a
# long URL-safe secret. Only that shape is masked, so ordinary log lines (file
# names, ports, paths, timestamps) are untouched. No leading word boundary:
# in a request URL the token follows "/bot" directly, so there is no boundary
# between "t" and the first digit.
_TOKEN_PATTERN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")

REDACTED = "<redacted>"

# Loggers that include the full request URL (and thus the bot token) in their
# output. Telegram's own logger logs method names/bodies, not URLs, and stays
# at INFO for operational visibility.
_URL_LOGGERS = ("httpx", "httpcore")


def redact_secrets(message: str) -> str:
    """Return ``message`` with every bot-token-shaped substring masked."""
    return _TOKEN_PATTERN.sub(REDACTED, message)


class SecretRedactionFilter(logging.Filter):
    """Mask token-shaped substrings in a record before it is formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # never let redaction break logging itself
            return True
        redacted = redact_secrets(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """Configure process logging: readable console output, never a token.

    Idempotent and safe to call from every entry point (``run.py``, the
    Telegram transport when started directly). Filtering happens on the root
    handlers, so records propagated from child loggers (uvicorn, telegram,
    httpx, our own modules) are covered too.
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for name in _URL_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
            handler.addFilter(SecretRedactionFilter())
