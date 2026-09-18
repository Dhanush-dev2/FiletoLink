"""HTTP security policy for the download API.

Kept separate from the endpoint so the header policy and the client-identity
(rate-limit key) trust model live in one obvious place.

Header choice is deliberately minimal and download-friendly:

- ``X-Content-Type-Options: nosniff`` — stops a browser from content-sniffing
  a download into a different type;
- ``Content-Security-Policy: default-src 'none'; sandbox`` — neutralises the
  response if a browser ever renders it inline instead of downloading;
- ``Referrer-Policy: no-referrer`` — the link URL is the only credential, so
  never leak it to third parties via the ``Referer`` header.

Files are always sent with ``Content-Disposition: attachment`` (see the
endpoint), so these headers do not interfere with normal downloads, range
requests, or explicit Content-Disposition handling.
"""
from __future__ import annotations

from fastapi import Request

from ..config import Settings

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Referrer-Policy": "no-referrer",
}


def client_key(request: Request, settings: Settings) -> str:
    """Return the rate-limit identity (key) for a request.

    Trust model: the **direct TCP peer address** is used by default. The
    ``X-Forwarded-For`` header is only consulted when
    ``TRUST_PROXY_HEADERS`` is enabled **and** the direct peer is listed in
    ``TRUSTED_PROXIES``. A client therefore cannot spoof its identity unless
    it connects from an explicitly trusted proxy; configuration validation
    refuses ``TRUST_PROXY_HEADERS`` without a non-empty ``TRUSTED_PROXIES``.
    """
    direct = request.client.host if request.client else "unknown"
    if settings.trust_proxy_headers and direct in settings.trusted_proxies:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",", 1)[0].strip()
            if first:
                return first
    return direct
