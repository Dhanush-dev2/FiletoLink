"""HTTP byte-range parsing (RFC 7233), single range only.

Supported forms (``Range: bytes=...``):

- explicit:  ``bytes=0-99``
- open-ended: ``bytes=100-``
- suffix:     ``bytes=-100``

Behavior contract (documented for tests):

- No ``Range`` header or a non-``bytes`` unit → ``None`` (full response).
- Satisfiable single range → inclusive ``(start, end)``.
- Multi-range (``bytes=0-1,3-4``) → :class:`UnsatisfiableRange` (416):
  multipart/byteranges is deliberately not implemented.
- Malformed or impossible ranges → :class:`UnsatisfiableRange` (416),
  e.g. non-numeric bounds, no ``-``, ``start > end``, ``start >= size``,
  or an empty/zero suffix.
- An end beyond EOF is clamped to the last byte (206, not 416).
"""
from __future__ import annotations


class UnsatisfiableRange(ValueError):
    """The Range header is malformed or cannot be satisfied (→ HTTP 416)."""


def parse_range_header(range_header: str | None, total_size: int) -> tuple[int, int] | None:
    """Parse a single byte range against ``total_size``.

    Returns inclusive ``(start, end)``, ``None`` when the request carries no
    applicable range, or raises :class:`UnsatisfiableRange`.
    """
    if not range_header:
        return None
    if not range_header.startswith("bytes="):
        return None  # unknown unit: ignore the header (RFC 7233 §3.1)
    spec = range_header[len("bytes="):].strip()

    if "," in spec:
        raise UnsatisfiableRange("multi-range requests are not supported")
    if total_size <= 0:
        raise UnsatisfiableRange("file has no bytes to range over")

    dash = spec.find("-")
    if dash == -1:
        raise UnsatisfiableRange("range must contain '-'")
    start_str = spec[:dash].strip()
    end_str = spec[dash + 1:].strip()

    if start_str == "" and end_str == "":
        raise UnsatisfiableRange("empty range")

    if start_str == "":
        # Suffix range: the last N bytes.
        try:
            suffix = int(end_str)
        except ValueError:
            raise UnsatisfiableRange("non-numeric suffix range") from None
        if suffix <= 0:
            raise UnsatisfiableRange("suffix length must be positive")
        length = min(suffix, total_size)
        return (total_size - length, total_size - 1)

    try:
        start = int(start_str)
    except ValueError:
        raise UnsatisfiableRange("non-numeric start") from None

    if end_str == "":
        end = total_size - 1
    else:
        try:
            end = int(end_str)
        except ValueError:
            raise UnsatisfiableRange("non-numeric end") from None

    if start < 0 or end < 0:
        raise UnsatisfiableRange("negative bounds")
    if start > end:
        raise UnsatisfiableRange("start is greater than end")
    if start >= total_size:
        raise UnsatisfiableRange("start is beyond the end of the file")

    end = min(end, total_size - 1)
    return (start, end)