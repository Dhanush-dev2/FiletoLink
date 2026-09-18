"""HTTP ``Content-Type`` sanitisation for downloads.

Stored MIME metadata originates from the upload source (Telegram) and is
therefore untrusted: it must never be able to produce an invalid or
injectable HTTP response header. Only well-formed ``type/subtype`` values
(with optional simple ``;key=value`` parameters) are passed through;
anything missing or malformed falls back to ``application/octet-stream``.

The character classes deliberately exclude control characters (CR, LF, NUL,
...), so a value that matches cannot break or inject a header. This is not a
content-inspection system — it only guarantees the *header* is safe.
"""
from __future__ import annotations

import re

FALLBACK_CONTENT_TYPE = "application/octet-stream"

# type/subtype, then zero or more ;token=token or ;token="quoted" parameters.
_CONTENT_TYPE_RE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]{1,127}/[A-Za-z0-9!#$&^_.+-]{1,127}"
    r"(?:\s*;\s*[A-Za-z0-9!#$&^_.+-]{1,64}="
    r'(?:[A-Za-z0-9!#$&^_.+-]{1,128}|"[^"\x00-\x1f\x7f]{0,128}"))*$'
)


def is_valid_content_type(value: str) -> bool:
    """Whether ``value`` is a safe, well-formed media type."""
    return bool(_CONTENT_TYPE_RE.fullmatch(value))


def resolve_content_type(mime_type: str | None) -> str:
    """Return a safe ``Content-Type`` for untrusted stored MIME metadata.

    Missing/empty or malformed values return
    :data:`FALLBACK_CONTENT_TYPE`; valid values are returned unchanged.
    """
    if not mime_type:
        return FALLBACK_CONTENT_TYPE
    value = mime_type.strip()
    if _CONTENT_TYPE_RE.fullmatch(value):
        return value
    return FALLBACK_CONTENT_TYPE
