"""Tests for Phase 11 abuse protection (rate limiting)."""
import asyncio
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.bot.models import Document, Message, User
from app.config import Settings
from app.database.connection import initialize
from app.database.files import FileRepository
from app.services.file_sources import LocalFileSource
from app.services.files import FileService
from app.utils.ratelimit import RateLimiter

USER = User(id=42, username="alice", first_name="Alice")
CONTENT = b"hello world\n"


def _make_setup(
    tmp_path,
    *,
    rate_limiter: RateLimiter | None = None,
    settings: Settings | None = None,
):
    """Return (app, repo, public_id) with one stored file."""
    path = tmp_path / "note.txt"
    path.write_bytes(CONTENT)
    repo = FileRepository(initialize(":memory:"))
    FileService(repo).save_document(
        Message(
            message_id=7,
            user=USER,
            document=Document(
                file_id="FILE1",
                file_name="note.txt",
                file_size=len(CONTENT),
                mime_type="text/plain",
            ),
            date=1_700_000_000,
            source_path=str(path),
        )
    )
    app = create_app(
        repository=repo,
        file_source=LocalFileSource(),
        rate_limiter=rate_limiter,
        settings=settings,
    )
    return app, repo, repo.get_by_id(1).public_id


# ── RateLimiter unit behavior ──────────────────────────────────────────────


def test_allows_up_to_limit_then_rejects():
    limiter = RateLimiter(limit=3, window_seconds=60, max_keys=10)
    assert [limiter.check("k", now=0.0)[0] for _ in range(3)] == [True, True, True]
    allowed, retry_after = limiter.check("k", now=0.0)
    assert allowed is False
    assert retry_after >= 1


def test_window_slides_and_recovers():
    limiter = RateLimiter(limit=2, window_seconds=10, max_keys=10)
    assert limiter.check("k", now=0.0)[0] is True
    assert limiter.check("k", now=0.0)[0] is True
    assert limiter.check("k", now=0.0)[0] is False
    # Once the window has passed, the old hits age out.
    assert limiter.check("k", now=10.0)[0] is True


def test_keys_are_independent():
    limiter = RateLimiter(limit=1, window_seconds=60, max_keys=10)
    assert limiter.check("client-a", now=0.0)[0] is True
    assert limiter.check("client-b", now=0.0)[0] is True  # separate bucket
    assert limiter.check("client-a", now=0.0)[0] is False


def test_tracked_keys_are_bounded():
    """Many attacker-controlled keys cannot grow memory without bound."""
    limiter = RateLimiter(limit=1, window_seconds=60, max_keys=5)
    for i in range(500):
        limiter.check(f"ip-{i}", now=0.0)
    assert len(limiter) <= 5


def test_concurrent_checks_never_exceed_the_limit():
    limiter = RateLimiter(limit=5, window_seconds=60, max_keys=100)
    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(25)

    def worker() -> None:
        barrier.wait()  # maximise contention
        allowed, _ = limiter.check("same-key", now=1_000.0)
        with lock:
            results.append(allowed)

    threads = [threading.Thread(target=worker) for _ in range(25)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 25
    assert sum(results) == 5  # exactly the limit, never more


def test_invalid_constructor_arguments_rejected():
    with pytest.raises(ValueError):
        RateLimiter(limit=0, window_seconds=60, max_keys=10)
    with pytest.raises(ValueError):
        RateLimiter(limit=1, window_seconds=0, max_keys=10)
    with pytest.raises(ValueError):
        RateLimiter(limit=1, window_seconds=60, max_keys=0)


# ── API-level behavior ─────────────────────────────────────────────────────


def test_api_allows_normal_requests_under_the_limit(tmp_path):
    app, _repo, pid = _make_setup(
        tmp_path, rate_limiter=RateLimiter(limit=5, window_seconds=60, max_keys=10)
    )
    client = TestClient(app)
    for _ in range(5):
        assert client.get(f"/f/{pid}").status_code == 200


def test_api_returns_429_with_retry_after(tmp_path):
    app, _repo, pid = _make_setup(
        tmp_path, rate_limiter=RateLimiter(limit=2, window_seconds=30, max_keys=10)
    )
    client = TestClient(app)
    assert client.get(f"/f/{pid}").status_code == 200
    assert client.get(f"/f/{pid}").status_code == 200

    resp = client.get(f"/f/{pid}")
    assert resp.status_code == 429
    assert "retry-after" in resp.headers
    assert 1 <= int(resp.headers["retry-after"]) <= 30
    # The 429 is JSON and still carries the security headers.
    assert resp.json()["detail"]
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_head_is_rate_limited_too(tmp_path):
    app, _repo, pid = _make_setup(
        tmp_path, rate_limiter=RateLimiter(limit=2, window_seconds=30, max_keys=10)
    )
    client = TestClient(app)
    assert client.head(f"/f/{pid}").status_code == 200
    assert client.head(f"/f/{pid}").status_code == 200
    resp = client.head(f"/f/{pid}")
    assert resp.status_code == 429
    assert "retry-after" in resp.headers


def test_invalid_ids_are_throttled_without_per_file_state(tmp_path):
    """Probing unknown ids is throttled, but creates no per-id limiter state."""
    limiter = RateLimiter(limit=3, window_seconds=60, max_keys=10)
    app, _repo, _pid = _make_setup(tmp_path, rate_limiter=limiter)
    client = TestClient(app)

    # Three distinct unknown (but well-formed) ids are allowed through → 404.
    assert [client.get(f"/f/AbCdEf{i}x").status_code for i in range(3)] == [
        404,
        404,
        404,
    ]
    # The fourth request is throttled regardless of the id.
    assert client.get("/f/AbCdEf9x").status_code == 429
    # Only the client identity is tracked — never the (attacker-chosen) ids.
    assert len(limiter) == 1


def test_independent_clients_get_independent_buckets(tmp_path):
    app, _repo, pid = _make_setup(
        tmp_path, rate_limiter=RateLimiter(limit=1, window_seconds=60, max_keys=10)
    )

    async def run() -> list[int]:
        transport_a = httpx.ASGITransport(app=app, client=("10.0.0.1", 1234))
        transport_b = httpx.ASGITransport(app=app, client=("10.0.0.2", 1234))
        async with (
            httpx.AsyncClient(transport=transport_a, base_url="http://test") as a,
            httpx.AsyncClient(transport=transport_b, base_url="http://test") as b,
        ):
            first_a = await a.get(f"/f/{pid}")
            first_b = await b.get(f"/f/{pid}")
            second_a = await a.get(f"/f/{pid}")
        return [first_a.status_code, first_b.status_code, second_a.status_code]

    assert asyncio.run(run()) == [200, 200, 429]


def test_rate_limiting_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    settings = Settings()
    assert settings.rate_limit_enabled is False

    app, _repo, pid = _make_setup(tmp_path, settings=settings)
    client = TestClient(app)
    for _ in range(10):
        assert client.get(f"/f/{pid}").status_code == 200
