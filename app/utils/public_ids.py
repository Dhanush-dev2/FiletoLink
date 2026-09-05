"""Public identifier generation for file links.

IDs are generated with :mod:`secrets` (a CSPRNG), so they are
cryptographically unpredictable — never sequential, never derived from
row ids, timestamps, or Telegram file ids.
"""
import re
import secrets

# 6 random bytes → 8 URL-safe characters (48 bits of entropy). Compact
# enough for URLs, and collisions are astronomically unlikely; callers
# still handle the residual risk by retrying on the UNIQUE constraint.
PUBLIC_ID_BYTES = 6

# Canonical public id shape: exactly 8 URL-safe characters.
PUBLIC_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8}$")


def generate_public_id() -> str:
    """Return a new cryptographically unpredictable, URL-safe public id.

    Alphabet: ``A-Z a-z 0-9 _ -`` (no padding), always 8 characters.
    """
    return secrets.token_urlsafe(PUBLIC_ID_BYTES)


def is_valid_public_id(public_id: str) -> bool:
    """Whether ``public_id`` matches the canonical URL-safe format."""
    return bool(PUBLIC_ID_PATTERN.fullmatch(public_id))