"""In-process, thread-safe rate limiting for the public download endpoints.

Zero-cost by design: no Redis, no external service — a small sliding-window
log kept in process memory. Two properties matter for abuse resistance:

- **keys are client identities (IP addresses), never link ids**, so an
  attacker inventing random public ids cannot grow any per-file state;
- **the number of tracked keys is capped** (``max_keys``); once the cap is
  reached the least-recently-used key is evicted, so memory cannot grow
  without bound from attacker-controlled addresses.

Intended for this single-process, self-hosted application. If the API is ever
run as several worker processes, each process keeps its own window (limits
become per-worker) — an accepted limitation, documented in the README.
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque


class RateLimiter:
    """Sliding-window limiter: at most ``limit`` hits per ``window_seconds``.

    ``check()`` is safe to call from multiple threads concurrently.
    """

    def __init__(self, limit: int, window_seconds: int, max_keys: int) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if window_seconds < 1:
            raise ValueError("window_seconds must be >= 1")
        if max_keys < 1:
            raise ValueError("max_keys must be >= 1")
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._hits: "OrderedDict[str, deque[float]]" = OrderedDict()
        self._lock = threading.Lock()

    def check(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """Record one hit for ``key``. Returns ``(allowed, retry_after_seconds)``.

        ``retry_after`` is ``0`` when the request is allowed, otherwise the
        whole number of seconds until the oldest counted hit leaves the
        window (always ``>= 1``) — suitable for a ``Retry-After`` header.
        """
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_seconds
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = deque()
                self._hits[key] = hits
            else:
                self._hits.move_to_end(key)
                # Drop hits that have aged out of the window.
                while hits and hits[0] <= cutoff:
                    hits.popleft()

            if len(hits) >= self.limit:
                retry_after = max(
                    1, math.ceil(hits[0] + self.window_seconds - current)
                )
                return False, retry_after

            hits.append(current)
            # Bound memory: evict the least-recently-used key if over the cap.
            while len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)
            return True, 0

    def __len__(self) -> int:
        """Number of tracked keys (always ``<= max_keys``)."""
        with self._lock:
            return len(self._hits)
