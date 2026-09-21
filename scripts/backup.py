"""FileToLink backup: one consistent snapshot of the two halves of app state.

The application state is exactly two things, and they are one logical unit
(``source_path`` in the database points into the storage tree):

- the SQLite database (``DATABASE_PATH``) — links, owners, counters, lifecycle
- FileToLink-owned storage (``FILE_STORAGE_DIR``) — the bytes those links serve

This script copies both, together, into a new timestamped directory:

.. code-block:: text

    backups/20260921-120000/
        files.db        consistent snapshot of the live database
        storage/        FileToLink-owned files (public-id keyed layout)
        MANIFEST.txt    what this snapshot contains (no absolute paths)

It deliberately copies **nothing else** — in particular not ``.env`` (secrets)
and not the Local Bot API Server's work directory (disposable staging, not
application state). It never overwrites an existing backup silently: a
non-empty destination is refused unless ``--force`` is given.

Usage (from the project root, Windows or Linux):

.. code-block:: text

    .venv\\Scripts\\python.exe scripts\\backup.py
    .venv\\Scripts\\python.exe scripts\\backup.py --dest D:\\filetolink-backups\\weekly
    .venv\\Scripts\\python.exe scripts\\backup.py --force

Restore = copy both halves back together (see README → Backup & restore):
``files.db`` over ``DATABASE_PATH`` and ``storage/`` over ``FILE_STORAGE_DIR``
while Window 2 and Window 3 are stopped.

The database snapshot uses SQLite's online backup API, so it is consistent
even while the bot and the API are running (which is the normal operator
situation). No scheduler and no service are installed by this phase — run it
by hand, or wire it into Task Scheduler yourself later.
"""

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a plain script (``python scripts/backup.py``) by making the
# project root importable — the app's settings resolution must match the
# running deployment exactly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.config.settings import PROJECT_ROOT  # noqa: E402
from app.utils.console import ensure_utf8_stdout  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2


class BackupError(Exception):
    """A backup that must not proceed (or failed), with an operator-facing message."""

    def __init__(self, message: str, exit_code: int = EXIT_FAILED) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _count_storage_files(root: Path) -> tuple[int, int]:
    """Return (file count, total bytes) under ``root`` (0, 0 when absent)."""
    if not root.is_dir():
        return 0, 0
    count = 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            count += 1
            total += path.stat().st_size
    return count, total


def _backup_database(source_db: Path, dest_db: Path) -> None:
    """Write a transactionally consistent copy of ``source_db`` to ``dest_db``.

    Uses SQLite's online backup API against a read-only connection, so a
    running bot/API cannot produce a torn snapshot and the live database is
    never modified.
    """
    try:
        source = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BackupError(
            f"cannot open the database at {source_db} ({exc}) — check "
            "DATABASE_PATH in .env"
        ) from None
    try:
        dest = sqlite3.connect(dest_db)
        try:
            source.backup(dest)
        finally:
            dest.close()
    except sqlite3.Error as exc:
        raise BackupError(f"database backup failed: {exc}") from None
    finally:
        source.close()


def _verify_database(backup_db: Path) -> tuple[int, int]:
    """Open the snapshot read-only and return (records, active records)."""
    try:
        conn = sqlite3.connect(f"file:{backup_db.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM files WHERE status = 'active'"
            ).fetchone()[0]
            return int(rows), int(active)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BackupError(
            f"the database snapshot is not readable ({exc}) — the backup is "
            "incomplete and must not be trusted"
        ) from None


def _check_destination(dest: Path, *, force: bool) -> None:
    if not dest.exists():
        return
    if not dest.is_dir():
        raise BackupError(
            f"{dest} exists and is not a directory — choose another destination",
            exit_code=EXIT_REFUSED,
        )
    if any(dest.iterdir()) and not force:
        raise BackupError(
            f"{dest} already exists and is not empty — refusing to overwrite a "
            "backup silently (use --force to write into it anyway)",
            exit_code=EXIT_REFUSED,
        )


def _check_safety(database_path: Path, storage_dir: Path, work_dir: Path, dest: Path) -> None:
    """Refuse layouts where a backup would copy secrets or staging data."""
    if storage_dir == PROJECT_ROOT or (storage_dir / ".env").exists():
        raise BackupError(
            f"FILE_STORAGE_DIR ({storage_dir}) contains .env — refusing to copy "
            "secrets into a backup; point FILE_STORAGE_DIR at a dedicated "
            "directory (the default is data/storage)",
            exit_code=EXIT_REFUSED,
        )
    if storage_dir == work_dir:
        raise BackupError(
            f"FILE_STORAGE_DIR and TELEGRAM_BOT_WORK_DIR are both {storage_dir} — "
            "the Local Bot API staging directory must never be backed up",
            exit_code=EXIT_REFUSED,
        )
    if work_dir.is_relative_to(storage_dir):
        raise BackupError(
            f"TELEGRAM_BOT_WORK_DIR ({work_dir}) is inside FILE_STORAGE_DIR "
            f"({storage_dir}) — refusing to copy the Local Bot API staging "
            "directory into a backup",
            exit_code=EXIT_REFUSED,
        )
    for name, path in (("destination", dest),):
        if path.is_relative_to(storage_dir) or path.is_relative_to(work_dir):
            raise BackupError(
                f"the {name} ({path}) is inside the storage/staging tree — "
                "choose a destination outside it"
            )
    if dest == database_path:
        raise BackupError("the destination must not be the live database file")


def create_backup(
    database_path: str | Path,
    storage_dir: str | Path,
    work_dir: str | Path,
    dest: str | Path,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, object]:
    """Create one backup and return a summary dict.

    Raises :class:`BackupError` (with an operator-facing message) instead of
    leaving a half-written backup: the destination is only reported as a
    backup once both halves are in place and the database snapshot verified.
    """
    database_path = Path(database_path)
    storage_dir = Path(storage_dir)
    work_dir = Path(work_dir)
    dest = Path(dest)
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    _check_safety(database_path, storage_dir, work_dir, dest)
    _check_destination(dest, force=force)

    if not database_path.is_file():
        raise BackupError(
            f"no database at {database_path} — nothing to back up (check "
            "DATABASE_PATH in .env)"
        )

    dest.mkdir(parents=True, exist_ok=True)
    backup_db = dest / "files.db"
    if backup_db.exists():
        backup_db.unlink()  # only reachable with --force
    _backup_database(database_path, backup_db)
    records, active = _verify_database(backup_db)

    dest_storage = dest / "storage"
    storage_present = storage_dir.is_dir()
    if storage_present:
        if dest_storage.exists():
            shutil.rmtree(dest_storage)  # only reachable with --force
        shutil.copytree(storage_dir, dest_storage)
    storage_files, storage_bytes = _count_storage_files(dest_storage)

    (dest / "MANIFEST.txt").write_text(
        "\n".join(
            [
                "FileToLink backup",
                f"created_at: {timestamp.isoformat(timespec='seconds')}",
                f"database: files.db ({backup_db.stat().st_size} bytes)",
                f"records: {records} (active: {active})",
                f"storage: storage/ ({storage_files} files, {storage_bytes} bytes)",
                "excluded: .env, TELEGRAM_BOT_WORK_DIR (Local Bot API staging), logs",
                "restore: copy files.db over DATABASE_PATH and storage/ over "
                "FILE_STORAGE_DIR together, with the bot and the API stopped",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    return {
        "dest": dest,
        "database": backup_db,
        "records": records,
        "active_records": active,
        "storage_files": storage_files,
        "storage_bytes": storage_bytes,
        "storage_present": storage_present,
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts/backup.py",
        description=(
            "Back up the FileToLink database and its owned file storage together."
        ),
    )
    parser.add_argument(
        "--dest",
        default=None,
        help=(
            "backup directory (default: backups/<UTC timestamp> under the "
            "project root)"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="write into an existing backup directory (never silent)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Windows consoles often use a legacy codepage that cannot encode the
    # status emoji below (observed: cp1252 UnicodeEncodeError when piped).
    ensure_utf8_stdout()
    args = _parse_args(argv)
    settings = get_settings()
    dest = args.dest or (
        PROJECT_ROOT
        / "backups"
        / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    )

    print("▶ FileToLink backup")
    print(f"   database:     {settings.database_path}")
    print(f"   file storage: {settings.file_storage_dir}")
    print(f"   destination:  {dest}")

    try:
        summary = create_backup(
            settings.database_path,
            settings.file_storage_dir,
            settings.telegram_bot_work_dir,
            dest,
            force=args.force,
        )
    except BackupError as exc:
        print(f"❌ Backup failed: {exc}")
        return exc.exit_code
    except OSError as exc:
        print(f"❌ Backup failed: {exc}")
        return EXIT_FAILED

    if not summary["storage_present"]:
        print(
            "⚠️  FILE_STORAGE_DIR does not exist yet — the snapshot has no storage "
            "half (expected only before the first ingest)."
        )
    print(
        f"✅ Backup complete: {summary['records']} records "
        f"({summary['active_records']} active), "
        f"{summary['storage_files']} storage files "
        f"({summary['storage_bytes']} bytes) → {summary['dest']}"
    )
    print(
        "   Restore: stop Window 2 and Window 3, then copy files.db over "
        "DATABASE_PATH and storage/ over FILE_STORAGE_DIR together."
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
