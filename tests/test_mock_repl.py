"""Tests for the interactive mock REPL command path.

The Phase 10 audit found that ``repl()`` routed plain command lines through
``reply_to()`` *without* a ``FileService``, so ``/info`` and ``/delete``
reported that no database was available even though ``send`` had just stored
a file. These tests drive the real ``repl()`` loop (with ``input()`` and the
service factory monkeypatched) and the underlying ``reply_to()`` dispatch,
and confirm ownership is still enforced rather than bypassed.
"""
import builtins

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.bot import mock
from app.bot.models import User
from app.services.file_sources import LocalFileSource
from app.services.files import make_file_service

# A different Telegram identity for ownership checks.
OTHER_USER = User(id=999, username="mallory", first_name="Mallory")

CONTENT = b"hello from the mock repl\n"


def _store_file(tmp_path, service) -> str:
    """Send one file through the mock transport; return its public id."""
    src = tmp_path / "note.txt"
    src.write_bytes(CONTENT)
    mock.send_local_file(str(src), file_service=service)
    return service.repository.get_by_id(1).public_id


# ── actual interactive REPL path ───────────────────────────────────────────


def test_repl_info_and_delete_use_the_shared_file_service(
    monkeypatch, tmp_path, capsys
):
    """`send` → `/info` → `/delete` all run through one REPL against one DB."""
    src = tmp_path / "note.txt"
    src.write_bytes(CONTENT)
    service = make_file_service(":memory:")

    def scripted_input():
        # Yielding lets the REPL finish processing `send` before we read the
        # generated public id for the following commands.
        yield f"send {src}"
        public_id = service.repository.get_by_id(1).public_id
        yield f"/info {public_id}"
        yield f"/delete {public_id}"
        yield "quit"

    lines = scripted_input()
    monkeypatch.setattr(mock, "make_file_service", lambda *a, **k: service)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(lines))

    mock.repl()

    out = capsys.readouterr().out
    public_id = service.repository.get_by_id(1).public_id

    # A. file created through the mock workflow, persisted to the shared DB.
    assert "Stored in database" in out
    assert service.repository.get_by_id(1).file_name == "note.txt"

    # C/D. /info through the same REPL path returns real link details.
    assert "Link info" in out
    assert "note.txt" in out
    assert public_id in out
    assert "needs a database" not in out

    # E/F. /delete through the same REPL path succeeds and is terminal.
    assert "Link deleted" in out
    assert service.repository.get_by_id(1).status == "deleted"

    # G. The deleted link is refused by the API with 410 Gone.
    client = TestClient(
        create_app(repository=service.repository, file_source=LocalFileSource())
    )
    assert client.get(f"/f/{public_id}").status_code == 410


# ── reply_to dispatch (the same path the REPL now uses) ────────────────────


def test_reply_to_info_shows_link_details(tmp_path):
    service = make_file_service(":memory:")
    public_id = _store_file(tmp_path, service)

    reply = mock.reply_to(f"/info {public_id}", file_service=service)

    assert "Link info" in reply
    assert "note.txt" in reply
    assert "active" in reply
    assert "unlimited" in reply
    assert f"/f/{public_id}" in reply


def test_reply_to_delete_succeeds_for_creating_user(tmp_path):
    service = make_file_service(":memory:")
    public_id = _store_file(tmp_path, service)

    reply = mock.reply_to(f"/delete {public_id}", file_service=service)

    assert "Link deleted" in reply
    assert service.repository.get_by_id(1).status == "deleted"


def test_reply_to_delete_is_idempotent(tmp_path):
    service = make_file_service(":memory:")
    public_id = _store_file(tmp_path, service)

    assert "Link deleted" in mock.reply_to(
        f"/delete {public_id}", file_service=service
    )
    assert "already deleted" in mock.reply_to(
        f"/delete {public_id}", file_service=service
    ).lower()


def test_reply_to_ownership_is_not_bypassed(tmp_path):
    """A different mock identity cannot inspect or delete someone else's link."""
    service = make_file_service(":memory:")
    public_id = _store_file(tmp_path, service)

    info = mock.reply_to(f"/info {public_id}", user=OTHER_USER, file_service=service)
    delete = mock.reply_to(
        f"/delete {public_id}", user=OTHER_USER, file_service=service
    )

    assert "created" in info.lower()
    assert "created" in delete.lower()
    # No private metadata leaks to the other user.
    assert "note.txt" not in info
    assert f"/f/{public_id}" not in info
    # The link is untouched.
    assert service.repository.get_by_id(1).status == "active"


def test_reply_to_unknown_public_id_is_handled(tmp_path):
    service = make_file_service(":memory:")
    _store_file(tmp_path, service)

    reply = mock.reply_to("/info AbCdEfGh", file_service=service)
    assert "couldn't find" in reply.lower()


def test_reply_to_without_service_stays_graceful():
    """Backward compatible: no service still yields a friendly reply."""
    reply = mock.reply_to("/info AbCdEfGh")
    assert reply
    assert "database" in reply.lower()
