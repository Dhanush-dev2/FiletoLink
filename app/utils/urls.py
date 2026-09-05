"""Public file URL construction.

Keeps URL building out of the database repository, Telegram transports,
and unrelated handlers: callers pass a base URL and a validated public id
and get ``{BASE_URL}/f/{public_id}`` back.
"""
from .public_ids import is_valid_public_id

FILE_PATH_PREFIX = "/f/"


def generate_file_url(base_url: str, public_id: str) -> str:
    """Build a public download URL: ``{base_url}/f/{public_id}``.

    Raises :class:`ValueError` for malformed input:
    - public id that is not the canonical 8-character URL-safe format
    - empty base URL
    - base URL without a scheme (e.g. ``localhost:8000``)
    Leading/trailing whitespace and trailing slashes on the base URL are
    tolerated and normalized.
    """
    if not is_valid_public_id(public_id):
        raise ValueError(f"invalid public id: {public_id!r}")

    base = base_url.strip()
    if not base:
        raise ValueError("base URL must not be empty")
    if "://" not in base:
        raise ValueError(
            "base URL must include a scheme, e.g. 'http://localhost:8000'"
        )

    return f"{base.rstrip('/')}{FILE_PATH_PREFIX}{public_id}"