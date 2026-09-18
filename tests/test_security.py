"""Tests for Phase 11 security hardening: response headers, MIME handling,
request hardening, HEAD+Range semantics, and configuration validation."""
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api.app import create_app
from app.api.security import client_key
from app.bot.models import Document, Message, User
from app.config import Settings
from app.database.connection import initialize
from app.database.files import FileRepository
from app.services.file_sources import LocalFileSource
from app.services.files import FileService
from app.utils.mime import FALLBACK_CONTENT_TYPE, resolve_content_type

USER = User(id=42, username="alice", first_name="Alice")
CONTENT = b"hello world\n"  # 12 bytes


def _stored(tmp_path, mime_type, *, file_name="note.txt", content=CONTENT):
    """Store one file with the given MIME type; return (repo, public_id)."""
    path = tmp_path / file_name
    path.write_bytes(content)
    repo = FileRepository(initialize(":memory:"))
    FileService(repo).save_document(
        Message(
            message_id=7,
            user=USER,
            document=Document(
                file_id="FILE1",
                file_name=file_name,
                file_size=len(content),
                mime_type=mime_type,
            ),
            date=1_700_000_000,
            source_path=str(path),
        )
    )
    return repo, repo.get_by_id(1).public_id


def _client(repo) -> TestClient:
    return TestClient(create_app(repository=repo, file_source=LocalFileSource()))


def _assert_security_headers(resp) -> None:
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in resp.headers["content-security-policy"]


# ── security headers ───────────────────────────────────────────────────────


def test_security_headers_on_full_and_range_downloads(tmp_path):
    repo, pid = _stored(tmp_path, "text/plain")
    client = _client(repo)
    _assert_security_headers(client.get(f"/f/{pid}"))
    _assert_security_headers(
        client.get(f"/f/{pid}", headers={"Range": "bytes=0-4"})
    )


def test_security_headers_on_head(tmp_path):
    repo, pid = _stored(tmp_path, "text/plain")
    _assert_security_headers(_client(repo).head(f"/f/{pid}"))


def test_security_headers_on_errors(tmp_path):
    repo, pid = _stored(tmp_path, "text/plain")
    client = _client(repo)
    _assert_security_headers(client.get("/f/doesNotExist"))  # 404
    _assert_security_headers(
        client.get(f"/f/{pid}", headers={"Range": "bytes=99-100"})  # 416
    )
    repo.set_status(1, "deleted")
    _assert_security_headers(client.get(f"/f/{pid}"))  # 410


# ── MIME / content hardening ───────────────────────────────────────────────


def test_resolve_content_type_passthrough_and_fallback():
    assert resolve_content_type("text/plain") == "text/plain"
    assert resolve_content_type("application/pdf") == "application/pdf"
    assert (
        resolve_content_type("text/plain; charset=utf-8")
        == "text/plain; charset=utf-8"
    )
    # Missing / empty → safe fallback.
    assert resolve_content_type(None) == FALLBACK_CONTENT_TYPE
    assert resolve_content_type("") == FALLBACK_CONTENT_TYPE
    assert resolve_content_type("   ") == FALLBACK_CONTENT_TYPE
    # Malformed / injectable → safe fallback.
    assert resolve_content_type("not a mime") == FALLBACK_CONTENT_TYPE
    assert resolve_content_type("text/plain\r\nX-Evil: 1") == FALLBACK_CONTENT_TYPE
    assert resolve_content_type("<script>") == FALLBACK_CONTENT_TYPE
    assert resolve_content_type("text/") == FALLBACK_CONTENT_TYPE


def test_missing_mime_falls_back_and_still_downloads(tmp_path):
    repo, pid = _stored(tmp_path, None)
    resp = _client(repo).get(f"/f/{pid}")
    assert resp.status_code == 200
    assert resp.content == CONTENT
    assert resp.headers["content-type"] == FALLBACK_CONTENT_TYPE


def test_head_missing_mime_falls_back(tmp_path):
    repo, pid = _stored(tmp_path, None)
    resp = _client(repo).head(f"/f/{pid}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == FALLBACK_CONTENT_TYPE


def test_malformed_stored_mime_cannot_inject_headers(tmp_path):
    repo, pid = _stored(tmp_path, "text/plain\r\nX-Evil: 1")
    resp = _client(repo).get(f"/f/{pid}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == FALLBACK_CONTENT_TYPE
    assert "x-evil" not in resp.headers


def test_valid_stored_mime_is_preserved(tmp_path):
    repo, pid = _stored(tmp_path, "application/x-custom")
    resp = _client(repo).get(f"/f/{pid}")
    assert resp.headers["content-type"] == "application/x-custom"
    # Downloads stay attachment-oriented.
    assert "attachment" in resp.headers["content-disposition"]


# ── request hardening ──────────────────────────────────────────────────────


def test_oversized_path_parameter_is_rejected(tmp_path):
    repo, _pid = _stored(tmp_path, "text/plain")
    resp = _client(repo).get("/f/" + "A" * 5000)
    assert resp.status_code == 404


def test_pathological_range_is_rejected_as_416(tmp_path):
    repo, pid = _stored(tmp_path, "text/plain")
    client = _client(repo)
    for bad in (
        "bytes=" + "9" * 10000 + "-",  # absurdly long numeric bound
        "bytes=0-1,3-4",               # multi-range (unsupported)
        "bytes=99-100",                # start beyond EOF
        "bytes=abc-def",               # non-numeric
    ):
        resp = client.get(f"/f/{pid}", headers={"Range": bad})
        assert resp.status_code == 416
        assert resp.headers["content-range"] == f"bytes */{len(CONTENT)}"


def test_unsupported_methods_return_405(tmp_path):
    repo, pid = _stored(tmp_path, "text/plain")
    client = _client(repo)
    assert client.post(f"/f/{pid}").status_code == 405
    assert client.put(f"/f/{pid}").status_code == 405
    assert client.delete(f"/f/{pid}").status_code == 405


# ── HEAD + Range (Phase 11 fix) ────────────────────────────────────────────


def test_head_with_valid_range_mirrors_get_without_body(tmp_path):
    repo, pid = _stored(tmp_path, "text/plain")
    resp = _client(repo).head(f"/f/{pid}", headers={"Range": "bytes=0-4"})
    assert resp.status_code == 206
    assert resp.content == b""  # never a body
    assert resp.headers["content-range"] == f"bytes 0-4/{len(CONTENT)}"
    assert resp.headers["content-length"] == "5"
    assert repo.get_by_id(1).download_count == 0  # never a slot


def test_head_with_open_ended_range(tmp_path):
    repo, pid = _stored(tmp_path, "text/plain")
    resp = _client(repo).head(f"/f/{pid}", headers={"Range": "bytes=6-"})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes 6-{len(CONTENT) - 1}/{len(CONTENT)}"


def test_head_with_unsatisfiable_range_returns_416(tmp_path):
    repo, pid = _stored(tmp_path, "text/plain")
    resp = _client(repo).head(f"/f/{pid}", headers={"Range": "bytes=999-1000"})
    assert resp.status_code == 416
    assert resp.headers["content-range"] == f"bytes */{len(CONTENT)}"
    assert resp.content == b""


def test_head_with_range_does_not_consume_a_slot(tmp_path):
    repo, pid = _stored(tmp_path, "text/plain")
    repo.update(1, max_downloads=1)
    client = _client(repo)
    assert client.head(f"/f/{pid}", headers={"Range": "bytes=0-4"}).status_code == 206
    assert repo.get_by_id(1).download_count == 0
    # The real slot is still available afterwards.
    assert client.get(f"/f/{pid}").status_code == 200


# ── configuration validation ───────────────────────────────────────────────


def test_default_settings_are_valid():
    settings = Settings()
    assert settings.bot_mode == "mock"
    assert settings.rate_limit_enabled is True
    assert settings.rate_limit_requests == 60
    assert settings.rate_limit_window_seconds == 60
    assert settings.trust_proxy_headers is False
    assert settings.trusted_proxies == frozenset()


def test_invalid_bot_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("BOT_MODE", "bogus")
    with pytest.raises(ValueError, match="BOT_MODE"):
        Settings()


def test_base_url_must_be_absolute_http(monkeypatch):
    for bad in ("ftp://example.com", "localhost:8000", "http://"):
        monkeypatch.setenv("BASE_URL", bad)
        with pytest.raises(ValueError, match="BASE_URL"):
            Settings()


def test_port_must_be_valid_and_in_range(monkeypatch):
    for bad in ("0", "70000", "notanumber"):
        monkeypatch.setenv("PORT", bad)
        with pytest.raises(ValueError, match="PORT"):
            Settings()


def test_rate_limit_config_is_validated(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_REQUESTS", "0")
    with pytest.raises(ValueError, match="RATE_LIMIT_REQUESTS"):
        Settings()
    monkeypatch.delenv("RATE_LIMIT_REQUESTS")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "0")
    with pytest.raises(ValueError, match="RATE_LIMIT_WINDOW_SECONDS"):
        Settings()
    monkeypatch.delenv("RATE_LIMIT_WINDOW_SECONDS")
    monkeypatch.setenv("RATE_LIMIT_MAX_KEYS", "0")
    with pytest.raises(ValueError, match="RATE_LIMIT_MAX_KEYS"):
        Settings()


def test_boolean_config_rejects_junk(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "maybe")
    with pytest.raises(ValueError, match="RATE_LIMIT_ENABLED"):
        Settings()


def test_trust_proxy_headers_requires_trusted_proxies(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    with pytest.raises(ValueError, match="TRUSTED_PROXIES"):
        Settings()


def test_invalid_trusted_proxy_ip_is_rejected(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1,not-an-ip")
    with pytest.raises(ValueError, match="TRUSTED_PROXIES"):
        Settings()


def test_trusted_proxies_are_parsed_and_normalized(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1, 203.0.113.7 ")
    settings = Settings()
    assert settings.trusted_proxies == frozenset({"127.0.0.1", "203.0.113.7"})


def test_validation_errors_do_not_leak_secrets(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:SUPERSECRET")
    monkeypatch.setenv("TELEGRAM_API_HASH", "deadbeefsecret")
    monkeypatch.setenv("BOT_MODE", "bogus")
    with pytest.raises(ValueError) as excinfo:
        Settings()
    message = str(excinfo.value)
    assert "SUPERSECRET" not in message
    assert "deadbeefsecret" not in message


# ── proxy trust model for rate-limit identity ──────────────────────────────


def _request(client_host: str, forwarded: str | None = None) -> Request:
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/f/x",
            "headers": headers,
            "client": (client_host, 12345),
            "server": ("testserver", 80),
            "query_string": b"",
        }
    )


def test_client_key_uses_direct_peer_by_default(monkeypatch):
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    settings = Settings()
    # A spoofed X-Forwarded-For is ignored: identity is the TCP peer.
    assert client_key(_request("203.0.113.9", "1.2.3.4"), settings) == "203.0.113.9"


def test_forwarded_header_only_trusted_from_configured_proxy(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1,203.0.113.7")
    settings = Settings()

    # From the trusted proxy: the forwarded client address is used.
    assert client_key(_request("127.0.0.1", "198.51.100.3"), settings) == "198.51.100.3"
    # From an untrusted peer: the header is ignored.
    assert client_key(_request("198.51.100.99", "10.0.0.1"), settings) == "198.51.100.99"
