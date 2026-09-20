"""Phase 15: deployment readiness — fresh-clone behavior, restart recovery,
and the configuration surface.

These tests pin what a fresh deployment relies on:

- required directories are created automatically (the SQLite parent on
  ``initialize``; FileToLink storage on the first transfer — never earlier,
  so a fresh clone accumulates no unnecessary directories);
- restarting a component does not lose links: records and owned files are
  durable, so the bot, the API, and the Local Bot API Server can each be
  restarted (or wiped, in the server's case) independently;
- the environment surface is documented: every variable ``Settings`` reads
  appears in ``.env.example``, and relative paths resolve against the
  project root;
- startup reports the resolved durable paths without leaking secrets.
"""
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.bot.models import Document, Message, User
from app.config.settings import PROJECT_ROOT
from app.config import Settings
from app.database.connection import initialize
from app.services.files import make_file_service
from app.services.storage import FileStorage
from app.utils.public_ids import generate_public_id
from app.utils.urls import generate_file_url
from tests.conftest import SETTINGS_ENV_VARS

CONTENT = b"deployment fixture payload"

USER = User(id=42, username="alice", first_name="Alice")


def make_message(source_path: str) -> Message:
    return Message(
        message_id=7,
        user=USER,
        document=Document(
            file_id="FILE1",
            file_name="clip.mp4",
            file_size=len(CONTENT),
            mime_type="video/mp4",
        ),
        date=1_700_000_000,
        source_path=source_path,
    )


def _deploy_env(tmp_path, monkeypatch) -> tuple[Path, Path, Path]:
    """Point DATABASE_PATH/FILE_STORAGE_DIR/TELEGRAM_BOT_WORK_DIR at tmp_path,
    as a real deployment would via .env (real mode, rate limiting off)."""
    db = tmp_path / "data" / "files.db"
    storage = tmp_path / "data" / "storage"
    work = tmp_path / "telegram_bot_data"
    monkeypatch.setenv("BOT_MODE", "real")
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("FILE_STORAGE_DIR", str(storage))
    monkeypatch.setenv("TELEGRAM_BOT_WORK_DIR", str(work))
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    return db, storage, work


def _ingest(db: Path, storage: Path, work: Path, monkeypatch) -> str:
    """First-boot ingestion: receive one file through the real-mode service."""
    work.mkdir(parents=True)
    source = work / "clip.mp4"
    source.write_bytes(CONTENT)
    service = make_file_service(db, FileStorage(storage))
    record = service.save_document(make_message(str(source)))
    assert not source.exists()  # ownership transferred out of the work dir
    return record.public_id


# ── directory initialization on a fresh deployment ─────────────────────────


def test_database_parent_directory_created_automatically(tmp_path):
    """A fresh clone has no data/ directory; the first run creates the
    database's parent directories by itself."""
    db = tmp_path / "fresh" / "nested" / "files.db"
    assert not db.parent.exists()
    conn = initialize(db)
    try:
        assert db.exists()
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    finally:
        conn.close()


def test_storage_root_created_on_first_transfer_only(tmp_path):
    """FileToLink storage is created lazily: constructing FileStorage creates
    nothing (no unnecessary directories); the first transfer creates the
    storage root and the per-link directory."""
    root = tmp_path / "fresh" / "storage"
    storage = FileStorage(root)
    assert not root.exists()

    source = tmp_path / "incoming.bin"
    source.write_bytes(CONTENT)
    public_id = generate_public_id()

    owned = storage.adopt(source, public_id=public_id, file_name="incoming.bin")

    assert Path(owned) == root / public_id / "incoming.bin"
    assert root.exists()
    assert not (tmp_path / "fresh" / "telegram_bot_data").exists()  # never ours


# ── restart / recovery ──────────────────────────────────────────────────────


def test_api_restart_keeps_links_downloadable(tmp_path, monkeypatch):
    """Restarting FastAPI (a fresh process: fresh connection, fresh rate
    limiter, env-driven wiring) must not lose links."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    public_id = _ingest(db, storage, work, monkeypatch)

    # "Restart": build the app exactly like `python run.py --api` would —
    # nothing injected, everything resolved from the environment.
    client = TestClient(create_app())
    resp = client.get(f"/f/{public_id}")
    assert resp.status_code == 200
    assert resp.content == CONTENT
    assert client.head(f"/f/{public_id}").status_code == 200


def test_bot_restart_keeps_links_manageable(tmp_path, monkeypatch):
    """Restarting the bot (a fresh FileService over the same database and
    storage) keeps records usable, including lifecycle commands."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    public_id = _ingest(db, storage, work, monkeypatch)

    restarted = make_file_service(db, FileStorage(storage))
    record = restarted.get_link(public_id)
    assert record is not None
    assert record.status == "active"

    url = generate_file_url("https://machine.tailnet-name.ts.net", public_id)
    assert url.endswith(f"/f/{public_id}")

    assert restarted.delete_link(public_id) is not None
    assert restarted.get_link(public_id).status == "deleted"
    assert not (storage / public_id).exists()  # owned file cleaned up


def test_local_bot_api_work_dir_is_disposable(tmp_path, monkeypatch):
    """The Local Bot API Server may wipe its work dir at any time (restart,
    eviction): FileToLink-owned links must keep working regardless."""
    db, storage, work = _deploy_env(tmp_path, monkeypatch)
    public_id = _ingest(db, storage, work, monkeypatch)

    shutil.rmtree(work)  # the server's storage was never application storage

    client = TestClient(create_app())
    resp = client.get(f"/f/{public_id}")
    assert resp.status_code == 200
    assert resp.content == CONTENT


# ── configuration surface ───────────────────────────────────────────────────


def test_relative_config_paths_resolve_against_project_root(monkeypatch):
    """Deployment overrides may be relative; they resolve against the project
    root (and resolving them creates no directories)."""
    monkeypatch.setenv("DATABASE_PATH", "data/deploy-check.db")
    monkeypatch.setenv("FILE_STORAGE_DIR", "data/deploy-check-storage")
    monkeypatch.setenv("TELEGRAM_BOT_WORK_DIR", "deploy-check-tgdata")

    settings = Settings()

    assert Path(settings.database_path) == PROJECT_ROOT / "data" / "deploy-check.db"
    assert Path(settings.file_storage_dir) == (
        PROJECT_ROOT / "data" / "deploy-check-storage"
    )
    assert Path(settings.telegram_bot_work_dir) == (
        PROJECT_ROOT / "deploy-check-tgdata"
    )
    assert not (PROJECT_ROOT / "data" / "deploy-check-storage").exists()


def test_env_example_documents_every_settings_variable():
    """Every environment variable Settings reads must be documented in
    .env.example, so the deployment surface cannot silently drift."""
    text = Path(PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    documented: set[str] = set()
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):  # commented-out (optional) variables count
            stripped = stripped.lstrip("#").strip()
        name, sep, _ = stripped.partition("=")
        if sep and name.strip().isidentifier() and name.strip() == name.strip():
            documented.add(name.strip())

    for variable in SETTINGS_ENV_VARS:
        assert variable in documented, (
            f"{variable} is read by Settings but missing from .env.example"
        )


# ── startup banner ──────────────────────────────────────────────────────────


def test_runtime_paths_banner_reports_paths_not_secrets(
    tmp_path, monkeypatch, capsys
):
    """The startup banner shows the resolved durable paths so operators can
    spot a bot/API storage mismatch — and never prints secret values."""
    monkeypatch.setenv("BOT_TOKEN", "123456:SUPERSECRET")
    monkeypatch.setenv("BOT_MODE", "real")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "files.db"))
    monkeypatch.setenv("FILE_STORAGE_DIR", str(tmp_path / "storage"))

    import run as run_module

    run_module.print_runtime_paths(Settings())

    out = capsys.readouterr().out
    assert str(tmp_path / "files.db") in out
    assert str(tmp_path / "storage") in out
    assert "real" in out
    assert "SUPERSECRET" not in out
