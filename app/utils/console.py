"""Console helpers.

Telegram-style output uses emoji, but Windows consoles often default to a
legacy codepage (e.g. cp1252) that cannot encode them, crashing the app.
This forces UTF-8 output where supported.
"""
import sys


def ensure_utf8_stdout() -> None:
    """Reconfigure stdout to UTF-8 when the runtime supports it."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        # Non-TTY or old runtime — leave as-is rather than crash.
        pass