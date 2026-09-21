"""Phase 19: long-running operations & deployment hardening.

Deterministic tests for the operational surface added or fixed in this phase —
no Telegram, no Tailscale, no internet, no real ``.env``, no live services:

- ``GET /health`` semantics (200 healthy / 503 with the failing component),
  and the guarantee that checking health never consumes a download slot;
- secret-free logging: the URL loggers are quieted and a token-shaped string
  can never reach a root handler;
- startup fail-fast messages (including the new ``LOCAL_BOT_API_URL`` check
  and an unusable ``DATABASE_PATH`` reported without a traceback);
- ``scripts/backup.py``: one consistent snapshot of the database + owned
  storage, no ``.env``, no staging directory, no silent overwrite, and a
  restore that still serves the links.
"""
import hashlib
import io
import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import scripts.backup as backup_module
from app.api.app import create_app
from app.bot.models import Document, Message, User
from app.config import Settings, get_settings
from app.database.connection import initialize
from app.database.files import FileRepository
from app.services.files import make_file_service
from app.services.storage import FileStorage
from app.utils.logging_setup import (
    REDACTED,
    SecretRedactionFilter,
    configure_logging,
    redact_secrets,
)

CONTENT = b"phase19 operational payload \x00\x01"
USER = User(id=11, username="ops", first_name="Ops")
TOKEN = "123456:AAFakeTokenUsedForRedactionTests1234567"

# ── helpers ─────────────────────────────────────────────────────────────────


def _deploy_env(tmp_path, monkeypatch) -> tuple[Path, Path, Path]:
    """Point the deployment at tmp_path exactly as a real ``.env`` would."""
    db = tmp_path / "data" / "files.db"
    storage = tmp_path / "data" / "storage"
    work = tmp_path / "telegram_bot_data"
    monkeypatch.setenv("BOT_MODE", "real")
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("FILE_STORAGE_DIR", str(storage))
    monkeypatch.setenv("TELEGRAM_BOT_WORK_DIR", str(work))
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    return db, storage, work


def _message(source_path: Path, *, name: str, size: int) -> Message:
    return Message(
        message_id=1,
        user=USER,
        document=Document(
            file_id="FILE1",
            file_name=name,
            file_size=size,
            mime_type="application/octet-stream",
        ),
        date=1_700_000_000,
        source_path=str(source_path),
    )


def _ingest(
    db: Path,
    storage: Path,
    work: Path,
    *,
    name: str = "clip.bin",
    content: bytes = CONTENT,
    max_downloads: int | None = None,
):
    """Receive one file through the real-mode service (ownership transfer)."""
    work.mkdir(parents=True, exist_ok=True)
    source = work / name
    source.write_bytes(content)
    service = make_file_service(db, FileStorage(storage))
    return service.save_document(
        _message(source, name=name, size=len(content)),
        max_downloads=max_downloads,
    )


def _client() -> TestClient:
    """Build the API the way ``python run.py --api`` would."""
    get_settings.cache_clear()
    return TestClient(create_app())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── GET /health ─────────────────────────────────────────────────────────────


def test_health_reports_ok_for_a_healthy_deployment(tmp_path, monkeypatch):
    _deploy_env(tmp_path, monkeypatch)

    response = _client().get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "storage": "ok"}
    # The same hardening as every download response, and nothing else.
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_health_reports_a_missing_database_as_503(tmp_path, monkeypatch):
    """A database that became unusable after startup (moved restore, wrong
    path) is reported on 503 — with the component named, nothing more."""
    _deploy_env(tmp_path, monkeypatch)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path))  # a directory, not a file
    settings = Settings()
    get_settings.cache_clear()

    app = create_app(
        repository=FileRepository(initialize(":memory:")), settings=settings
    )
    response = TestClient(app).get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "error", "storage": "ok"}


def test_health_reports_an_unusable_storage_root_as_503(tmp_path, monkeypatch):
    _deploy_env(tmp_path, monkeypatch)
    collision = tmp_path / "storage-is-a-file"
    collision.write_bytes(b"not a directory")
    monkeypatch.setenv("FILE_STORAGE_DIR", str(collision))

    response = _client().get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "database": "ok",
        "storage": "error",
    }


def test_health_never_consumes_a_download_slot(tmp_path, monkeypatch):
    """Operators check /health instead of fetching a link — so it must not
    burn a max_downloads slot (the reason the endpoint exists at all)."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    record = _ingest(db, storage, work, max_downloads=1)
    client = _client()

    for _ in range(5):
        assert client.get("/health").status_code == 200

    assert client.get(f"/f/{record.public_id}").status_code == 200
    assert client.get(f"/f/{record.public_id}").status_code == 410  # the one slot


def test_health_does_not_expose_configuration_or_data(tmp_path, monkeypatch):
    """The body is component states only: no paths, no credentials, no rows."""
    db, storage, _work = _deploy_env(tmp_path, monkeypatch)
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    _ingest(db, storage, tmp_path / "telegram_bot_data")

    response = _client().get("/health")

    assert set(response.json()) == {"status", "database", "storage"}
    body = response.text
    for secret in (TOKEN, str(db), str(storage), "clip.bin"):
        assert secret not in body


# ── logging: secrets never reach the console ────────────────────────────────


def test_redact_secrets_masks_token_shapes_only():
    message = f"HTTP Request: POST http://127.0.0.1:8081/bot{TOKEN}/getUpdates"
    assert TOKEN not in redact_secrets(message)
    assert "getUpdates" in redact_secrets(message)
    # Ordinary operational lines are untouched.
    for text in ("port 8000", "clip.bin", "12:30", "abc:def", "data/storage"):
        assert redact_secrets(text) == text


def test_configure_logging_quiets_the_url_loggers():
    """httpx logs every request URL — including ``/bot<TOKEN>/...`` — at INFO."""
    configure_logging()

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_configured_output_redacts_a_token_reaching_a_root_handler():
    """Even a token logged at WARNING/ERROR by any library is masked, because
    every root handler carries the redaction filter."""
    configure_logging()
    root = logging.getLogger()
    assert any(
        isinstance(f, SecretRedactionFilter)
        for handler in root.handlers
        for f in handler.filters
    )

    stream = io.StringIO()
    handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
    originals = [(h, h.stream) for h in handlers]
    logger = logging.getLogger("app.ops.redaction.probe")
    previous_level = logger.level
    try:
        for handler, _ in originals:
            handler.setStream(stream)
        logger.setLevel(logging.INFO)
        logger.info("HTTP Request: GET http://127.0.0.1:8081/bot%s/getMe", TOKEN)
    finally:
        logger.setLevel(previous_level)
        for handler, original in originals:
            handler.setStream(original)

    output = stream.getvalue()
    assert TOKEN not in output
    assert REDACTED in output
    assert "getMe" in output  # the diagnostically useful part survives


def test_run_telegram_explains_a_rejected_token_without_printing_it(
    monkeypatch, capsys
):
    """A wrong/revoked BOT_TOKEN must not be reported as 'cannot reach the
    server' (which sends the operator to the wrong window)."""
    from app.bot import telegram as telegram_module

    monkeypatch.setenv("BOT_MODE", "real")
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "deadbeef")
    monkeypatch.setattr(
        telegram_module, "_ping_local_server", lambda url, token: (False, "InvalidToken")
    )

    with pytest.raises(SystemExit) as excinfo:
        telegram_module.run_telegram()

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "rejected BOT_TOKEN" in out
    assert "credential problem" in out
    assert TOKEN not in out


def test_run_telegram_explains_an_unreachable_local_server(monkeypatch, capsys):
    from app.bot import telegram as telegram_module

    monkeypatch.setenv("BOT_MODE", "real")
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "deadbeef")
    monkeypatch.setattr(
        telegram_module, "_ping_local_server", lambda url, token: (False, "NetworkError")
    )

    with pytest.raises(SystemExit):
        telegram_module.run_telegram()

    out = capsys.readouterr().out
    assert "Cannot reach the Local Bot API Server" in out
    assert "Window 1" in out or "Start it with --local" in out
    assert TOKEN not in out


# ── startup fail-fast ───────────────────────────────────────────────────────


def test_invalid_local_bot_api_url_fails_fast(monkeypatch):
    monkeypatch.setenv("LOCAL_BOT_API_URL", "not-a-url")

    with pytest.raises(ValueError, match="LOCAL_BOT_API_URL"):
        Settings()


def test_local_bot_api_url_still_accepts_the_documented_forms(monkeypatch):
    for url in ("http://127.0.0.1:8081", "https://bot-api.internal:8443/"):
        monkeypatch.setenv("LOCAL_BOT_API_URL", url)
        assert Settings().local_bot_api_url == url.rstrip("/")


def test_run_reports_an_unusable_database_path_without_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """``DATABASE_PATH`` pointing at a directory (or an unreadable location)
    must fail fast with one actionable line, not a stack trace."""
    import run as run_module

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path))  # a directory
    monkeypatch.setattr(sys, "argv", ["run.py", "--api"])
    get_settings.cache_clear()

    with pytest.raises(SystemExit) as excinfo:
        run_module.main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Cannot open the SQLite database" in captured.out
    assert "DATABASE_PATH" in captured.out
    assert "Traceback" not in captured.err


def test_run_still_rejects_invalid_configuration_without_a_traceback(
    monkeypatch, capsys
):
    import run as run_module

    monkeypatch.setenv("BOT_MODE", "invalid-mode")
    monkeypatch.setattr(sys, "argv", ["run.py", "--api"])
    get_settings.cache_clear()

    with pytest.raises(SystemExit) as excinfo:
        run_module.main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Invalid configuration" in captured.out
    assert "BOT_MODE" in captured.out
    assert "Traceback" not in captured.err


# ── backup script ───────────────────────────────────────────────────────────


def _backup(tmp_path, monkeypatch, **kwargs):
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    record = _ingest(db, storage, work)
    dest = tmp_path / "backups" / "snap"
    summary = backup_module.create_backup(db, storage, work, dest, **kwargs)
    return db, storage, work, record, dest, summary


def test_backup_contains_both_halves_with_matching_bytes(tmp_path, monkeypatch):
    db, storage, work, record, dest, summary = _backup(tmp_path, monkeypatch)

    # The database half opens read-only and still knows the link.
    backup_db = dest / "files.db"
    restored = FileRepository(initialize(str(backup_db))).get_by_public_id(
        record.public_id
    )
    assert restored is not None
    assert restored.status == "active"
    # The storage half is byte-identical to the owned file.
    assert summary["records"] == 1
    assert summary["active_records"] == 1
    assert _sha256(dest / "storage" / record.public_id / "clip.bin") == _sha256(
        storage / record.public_id / "clip.bin"
    )


def test_backup_excludes_secrets_and_local_bot_api_staging(tmp_path, monkeypatch):
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text("BOT_TOKEN=must-never-be-backed-up\n")
    work.mkdir(parents=True, exist_ok=True)
    (work / "staged.bin").write_bytes(b"staging, not application state")
    _ingest(db, storage, work)

    dest = tmp_path / "backups" / "snap"
    backup_module.create_backup(db, storage, work, dest)

    assert not (dest / ".env").exists()
    assert not (dest / "telegram_bot_data").exists()
    for path in dest.rglob("*"):
        if path.is_file():
            assert b"must-never-be-backed-up" not in path.read_bytes()


def test_backup_refuses_to_overwrite_a_non_empty_destination(tmp_path, monkeypatch):
    db, storage, work, _record, dest, _summary = _backup(tmp_path, monkeypatch)

    with pytest.raises(backup_module.BackupError) as excinfo:
        backup_module.create_backup(db, storage, work, dest)

    assert "refusing to overwrite" in str(excinfo.value)
    assert excinfo.value.exit_code == backup_module.EXIT_REFUSED
    # ... and an explicit --force is allowed to proceed.
    summary = backup_module.create_backup(db, storage, work, dest, force=True)
    assert summary["records"] == 1


def test_backup_refuses_a_storage_dir_that_contains_env(tmp_path, monkeypatch):
    """A misconfigured FILE_STORAGE_DIR pointing at the project root would copy
    secrets — refuse instead."""
    db, _storage, work = _deploy_env(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text("BOT_TOKEN=secret\n")
    initialize(db)

    with pytest.raises(backup_module.BackupError) as excinfo:
        backup_module.create_backup(db, tmp_path, work, tmp_path / "backups" / "snap")

    assert ".env" in str(excinfo.value)
    assert excinfo.value.exit_code == backup_module.EXIT_REFUSED


def test_backup_refuses_a_staging_dir_inside_storage(tmp_path, monkeypatch):
    db, storage, _work = _deploy_env(tmp_path, monkeypatch)
    initialize(db)
    nested_work = storage / "telegram_bot_data"

    with pytest.raises(backup_module.BackupError) as excinfo:
        backup_module.create_backup(db, storage, nested_work, tmp_path / "snap")

    assert "staging" in str(excinfo.value)


def test_backup_without_a_database_fails_clearly(tmp_path, monkeypatch):
    _deploy_env(tmp_path, monkeypatch)

    with pytest.raises(backup_module.BackupError, match="no database"):
        backup_module.create_backup(
            tmp_path / "data" / "missing.db",
            tmp_path / "data" / "storage",
            tmp_path / "telegram_bot_data",
            tmp_path / "backups" / "snap",
        )


def test_backup_manifest_records_contents_and_exclusions(tmp_path, monkeypatch):
    _db, _storage, _work, _record, dest, _summary = _backup(tmp_path, monkeypatch)

    manifest = (dest / "MANIFEST.txt").read_text(encoding="utf-8")
    assert "records: 1 (active: 1)" in manifest
    assert "files.db" in manifest and "storage/" in manifest
    assert ".env" in manifest  # documented as excluded
    assert str(tmp_path) not in manifest  # no absolute machine paths


def test_restored_backup_serves_the_links_from_a_new_location(tmp_path, monkeypatch):
    """The script's output is a usable restore: point the deployment at the
    backup copy and the links still serve."""
    _db, _storage, _work, record, dest, _summary = _backup(tmp_path, monkeypatch)

    monkeypatch.setenv("DATABASE_PATH", str(dest / "files.db"))
    monkeypatch.setenv("FILE_STORAGE_DIR", str(dest / "storage"))

    client = _client()
    response = client.get(f"/f/{record.public_id}")
    assert response.status_code == 200
    assert response.content == CONTENT


def test_backup_of_a_live_database_is_consistent_while_the_api_serves(
    tmp_path, monkeypatch
):
    """The snapshot uses SQLite's online backup API, so it stays consistent
    while the API is actively reading (no need to stop windows first)."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    record = _ingest(db, storage, work, max_downloads=1)
    client = _client()
    assert client.get(f"/f/{record.public_id}").status_code == 200  # counter written

    dest = tmp_path / "backups" / "live"
    backup_module.create_backup(db, storage, work, dest)

    snapshot = FileRepository(initialize(str(dest / "files.db"))).get_by_public_id(
        record.public_id
    )
    assert snapshot is not None
    assert snapshot.download_count == 1
    assert snapshot.status == "expired"


def test_cli_reports_success_and_a_clear_failure(tmp_path, monkeypatch, capsys):
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    _ingest(db, storage, work)

    assert backup_module.main(["--dest", str(tmp_path / "backups" / "cli")]) == 0
    out = capsys.readouterr().out
    assert "Backup complete" in out
    assert "restore" in out.lower()

    # A second run into the same destination is refused, not silent.
    assert (
        backup_module.main(["--dest", str(tmp_path / "backups" / "cli")])
        == backup_module.EXIT_REFUSED
    )
    assert "refusing to overwrite" in capsys.readouterr().out


def test_backup_leaves_the_live_deployment_untouched(tmp_path, monkeypatch):
    db, storage, work, record, dest, _summary = _backup(tmp_path, monkeypatch)
    assert dest.is_dir()

    # The live halves are still exactly where they were, and still serve.
    assert db.is_file()
    assert (storage / record.public_id / "clip.bin").is_file()
    assert _client().get(f"/f/{record.public_id}").status_code == 200
