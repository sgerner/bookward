"""Private, WAL-safe SQLite backups and guarded in-place recovery."""

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

from .config import settings
from .database import DATABASE_LOCK, MIGRATIONS, connect, transaction
from .secrets import installation_key

LOGGER = logging.getLogger(__name__)
BACKUP_ID = re.compile(r"^bookward-(\d{8}T\d{6}Z)-([0-9a-f]{8})$")
BACKUP_INTERVAL_MAX_HOURS = 24 * 30
BACKUP_RETENTION_MAX = 100
LAST_ERROR_KEY = "backup_last_error"
RESTORE_JOURNAL_NAME = ".bookward-restore-in-progress.json"


class BackupError(ValueError):
    """A backup or restore could not be completed safely."""


def backup_directory() -> Path:
    configured = str(settings.backup_dir or "").strip()
    return Path(configured) if configured else Path(settings.db).parent / "backups"


def backup_interval_hours() -> int:
    try:
        value = int(settings.backup_interval_hours)
    except (TypeError, ValueError):
        value = 24
    return max(0, min(BACKUP_INTERVAL_MAX_HOURS, value))


def backup_retention_count() -> int:
    try:
        value = int(settings.backup_retention_count)
    except (TypeError, ValueError):
        value = 7
    return max(1, min(BACKUP_RETENTION_MAX, value))


def _ensure_private_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError as exc:
        raise BackupError("The backup directory permissions could not be secured.") from exc


def _id_time(backup_id: str) -> datetime | None:
    match = BACKUP_ID.fullmatch(backup_id)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _backup_path(backup_id: str) -> Path:
    if not _id_time(backup_id):
        raise BackupError("Choose a valid Bookward backup.")
    directory = backup_directory()
    _ensure_private_directory(directory)
    try:
        resolved_directory = directory.resolve(strict=True)
        # Resolve requests against directory entries instead of turning the
        # request value into a filesystem path. This keeps malformed or
        # manipulated IDs from influencing path traversal, even if the ID
        # validation above is changed in the future.
        for candidate in directory.iterdir():
            if candidate.name != f"{backup_id}.sqlite3":
                continue
            if candidate.is_symlink() or not candidate.is_file():
                break
            resolved = candidate.resolve(strict=True)
            if resolved.parent != resolved_directory:
                break
            return resolved
    except OSError:
        pass
    raise BackupError("That backup is no longer available.")


def _key_sidecar(backup_path: Path) -> Path:
    return backup_path.with_name(f"{backup_path.stem}.secret.key")


def _has_configured_key() -> bool:
    return bool(os.getenv("AFTERWORD_SECRET_KEY", ""))


def _write_generated_key_sidecar(backup_path: Path) -> None:
    """Keep the generated Fernet key beside its snapshot, mode 0600."""

    if _has_configured_key():
        return
    key = installation_key()
    Fernet(key)
    sidecar = _key_sidecar(backup_path)
    descriptor, name = tempfile.mkstemp(
        prefix=".bookward-key-", suffix=".tmp", dir=sidecar.parent
    )
    os.fchmod(descriptor, 0o600)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, sidecar)
        os.chmod(sidecar, 0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def list_backups() -> list[dict]:
    directory = backup_directory()
    if not directory.is_dir():
        return []
    try:
        _ensure_private_directory(directory)
    except (OSError, BackupError):
        LOGGER.exception("Could not inspect the private Bookward backup directory")
        return []
    found = []
    try:
        paths = list(directory.iterdir())
    except OSError:
        LOGGER.exception("Could not inspect the private Bookward backup directory")
        return []
    for path in paths:
        if path.is_symlink() or path.suffix != ".sqlite3":
            continue
        backup_id = path.stem
        created_at = _id_time(backup_id)
        if not created_at or not path.is_file():
            continue
        try:
            details = {
                "id": backup_id,
                "created_at": created_at.isoformat(),
                "size_bytes": path.stat().st_size,
            }
            found.append(
                (
                    created_at.isoformat(),
                    path.stat().st_mtime_ns,
                    details,
                )
            )
        except OSError:
            continue
    return [item[2] for item in sorted(found, reverse=True)]


def _set_last_error(message: str) -> None:
    try:
        with transaction() as con:
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,0) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=0,"
                "updated_at=CURRENT_TIMESTAMP",
                (LAST_ERROR_KEY, message[:300]),
            )
    except Exception:
        LOGGER.exception("Could not persist backup status")


def backup_status() -> dict:
    backups = list_backups()
    last_error = None
    try:
        with connect() as con:
            found = con.execute(
                "SELECT value FROM settings WHERE key=?", (LAST_ERROR_KEY,)
            ).fetchone()
            last_error = found["value"] if found else None
    except sqlite3.Error:
        last_error = "Backup status is temporarily unavailable."
    interval = backup_interval_hours()
    last_success = backups[0]["created_at"] if backups else None
    next_backup = None
    if interval and last_success:
        parsed = datetime.fromisoformat(last_success)
        next_backup = (parsed + timedelta(hours=interval)).isoformat()
    return {
        "enabled": interval > 0,
        "interval_hours": interval,
        "retention_count": backup_retention_count(),
        "last_success_at": last_success,
        "last_error": last_error,
        "next_backup_at": next_backup,
        "backups": backups,
    }


def backup_is_due(now: datetime | None = None) -> bool:
    interval = backup_interval_hours()
    if interval <= 0:
        return False
    backups = list_backups()
    if not backups:
        return True
    latest = datetime.fromisoformat(backups[0]["created_at"])
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc) - latest >= timedelta(hours=interval)


def _create_snapshot_unlocked() -> Path:
    source_path = Path(settings.db)
    if not source_path.is_file():
        raise BackupError("The Bookward database is not available for backup.")
    directory = backup_directory()
    _ensure_private_directory(directory)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_id = f"bookward-{stamp}-{uuid.uuid4().hex[:8]}"
    destination = directory / f"{backup_id}.sqlite3"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".bookward-backup-", suffix=".tmp", dir=directory
    )
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    temporary = Path(temporary_name)
    source = target = None
    try:
        source_uri = source_path.resolve().as_uri() + "?mode=ro"
        source = sqlite3.connect(source_uri, uri=True, timeout=30)
        target = sqlite3.connect(temporary, timeout=30)
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise BackupError("SQLite could not verify the completed backup.")
        if target.execute("PRAGMA foreign_key_check").fetchone():
            raise BackupError("The database has unresolved references; backup stopped.")
        target.commit()
        target.close()
        target = None
        source.close()
        source = None
        os.chmod(temporary, 0o600)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        _write_generated_key_sidecar(destination)
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return destination
    except Exception:
        LOGGER.exception("SQLite backup creation failed")
        destination.unlink(missing_ok=True)
        _key_sidecar(destination).unlink(missing_ok=True)
        raise
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
        temporary.unlink(missing_ok=True)


def _backup_record(path: Path) -> dict:
    backup_id = path.stem
    created_at = _id_time(backup_id)
    if not created_at:
        raise BackupError("The completed backup name is invalid.")
    return {
        "id": backup_id,
        "created_at": created_at.isoformat(),
        "size_bytes": path.stat().st_size,
    }


def _prune_backups() -> None:
    retained = list_backups()
    retained_ids = {item["id"] for item in retained[: backup_retention_count()]}
    for old in retained[backup_retention_count() :]:
        try:
            path = _backup_path(old["id"])
            path.unlink()
            _key_sidecar(path).unlink(missing_ok=True)
        except (OSError, BackupError):
            LOGGER.warning("Could not remove an expired Bookward backup")
    directory = backup_directory()
    for sidecar in directory.glob("bookward-*.secret.key"):
        backup_id = sidecar.name.removesuffix(".secret.key")
        if backup_id not in retained_ids and not sidecar.is_symlink():
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("Could not remove an orphaned Bookward key recovery file")


def create_backup() -> dict:
    try:
        with DATABASE_LOCK:
            path = _create_snapshot_unlocked()
            _set_last_error("")
            _prune_backups()
        return _backup_record(path)
    except BackupError:
        _set_last_error("Backup failed; check the engine logs for details.")
        raise
    except Exception as exc:
        _set_last_error("Backup failed; check the engine logs for details.")
        raise BackupError("Backup failed; check the engine logs for details.") from exc


def _validate_snapshot(path: Path) -> int:
    con = None
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=30)
        integrity = con.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise BackupError("This file is not a valid Bookward database backup.")
        if con.execute("PRAGMA foreign_key_check").fetchone():
            raise BackupError("This backup contains unresolved database references.")
        table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if not table:
            raise BackupError("This file is not a Bookward database backup.")
        versions = [
            item[0]
            for item in con.execute("SELECT version FROM schema_migrations")
        ]
        if not versions:
            raise BackupError("This backup has no Bookward schema version.")
        version = max(versions)
        if version > MIGRATIONS[-1][0]:
            raise BackupError(
                "This backup was made by a newer Bookward version. Upgrade Bookward before restoring it."
            )
        return version
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise BackupError("This file is not a readable Bookward database backup.") from exc
    finally:
        if con is not None:
            con.close()


def _remove_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _restore_journal_path() -> Path:
    return Path(settings.db).parent / RESTORE_JOURNAL_NAME


def _write_restore_journal(safety_backup: Path, selected_backup: Path) -> None:
    journal = _restore_journal_path()
    payload = json.dumps(
        {"safety_backup": safety_backup.stem, "selected_backup": selected_backup.stem}
    ).encode()
    descriptor, name = tempfile.mkstemp(prefix=".bookward-restore-", suffix=".tmp", dir=journal.parent)
    os.fchmod(descriptor, 0o600)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, journal)
        directory_fd = os.open(journal.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _clear_restore_journal() -> None:
    journal = _restore_journal_path()
    journal.unlink(missing_ok=True)
    directory_fd = os.open(journal.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def recover_interrupted_restore() -> bool:
    """Roll back a database/key pair if the engine stopped during restore."""

    journal = _restore_journal_path()
    if not journal.exists():
        return False
    try:
        payload = json.loads(journal.read_text())
        safety_id = payload["safety_backup"]
        safety_path = _backup_path(safety_id)
        _validate_snapshot(safety_path)
        safety_key = _restore_key_sidecar(safety_path)
        _validate_snapshot_key(safety_path, safety_key)
        with DATABASE_LOCK:
            staged = _copy_to_stage(safety_path, Path(settings.db).parent)
            try:
                _replace_live_database(staged, safety_key)
            except Exception:
                staged.unlink(missing_ok=True)
                _remove_sidecars(staged)
                raise
            _clear_restore_journal()
            _set_last_error("An interrupted restore was rolled back to its safety backup.")
        return True
    except Exception as exc:
        LOGGER.exception("Could not recover an interrupted Bookward restore")
        raise BackupError(
            "An interrupted restore needs its safety backup and matching encryption key."
        ) from exc


def _restore_key_sidecar(backup_path: Path) -> Path | None:
    if _has_configured_key():
        return None
    sidecar = _key_sidecar(backup_path)
    if sidecar.is_symlink() or not sidecar.is_file():
        raise BackupError(
            "This backup has no generated encryption key. Restore it with the original data volume or configured key."
        )
    try:
        key = sidecar.read_bytes().strip()
        Fernet(key)
        sidecar.chmod(0o600)
    except (OSError, ValueError) as exc:
        raise BackupError("This backup's generated encryption key is invalid.") from exc
    return sidecar


def _validate_snapshot_key(backup_path: Path, key_sidecar: Path | None) -> None:
    """Catch mismatched recovery keys before touching the live installation."""

    con = None
    try:
        raw_key = (
            key_sidecar.read_bytes().strip()
            if key_sidecar is not None
            else os.getenv("AFTERWORD_SECRET_KEY", "").encode()
        )
        cipher = Fernet(raw_key)
        uri = backup_path.resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=30)
        tables = {
            item[0]
            for item in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        queries = []
        if "settings" in tables:
            queries.append(
                "SELECT value FROM settings WHERE secret=1 AND value LIKE 'fernet:%'"
            )
        if "llm_connections" in tables:
            queries.append(
                "SELECT secret FROM llm_connections WHERE secret LIKE 'fernet:%'"
            )
        for query in queries:
            for (value,) in con.execute(query):
                cipher.decrypt(value.removeprefix("fernet:").encode())
    except (InvalidToken, ValueError, OSError, sqlite3.Error) as exc:
        raise BackupError(
            "The encryption key for this backup is missing or does not match its stored secrets."
        ) from exc
    finally:
        if con is not None:
            con.close()


def _replace_live_database(staged: Path, key_source: Path | None = None) -> None:
    live = Path(settings.db)
    staged_key = None
    if key_source is not None:
        descriptor, name = tempfile.mkstemp(
            prefix=".bookward-key-restore-", suffix=".tmp", dir=live.parent
        )
        os.fchmod(descriptor, 0o600)
        staged_key = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(key_source.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            staged_key.unlink(missing_ok=True)
            raise
    try:
        checkpoint = sqlite3.connect(live, timeout=30)
        try:
            result = checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result and result[0] != 0:
                raise BackupError("The active database could not be safely closed for restore.")
        finally:
            checkpoint.close()
        _remove_sidecars(live)
        if staged_key is not None:
            os.replace(staged_key, live.with_name("secret.key"))
            os.chmod(live.with_name("secret.key"), 0o600)
        os.replace(staged, live)
        os.chmod(live, 0o600)
        directory_fd = os.open(live.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if staged_key is not None:
            staged_key.unlink(missing_ok=True)
        raise


def _copy_to_stage(source: Path, directory: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".bookward-restore-", suffix=".sqlite3", dir=directory)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    staged = Path(name)
    try:
        shutil.copyfile(source, staged)
        os.chmod(staged, 0o600)
        with staged.open("rb") as handle:
            os.fsync(handle.fileno())
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        _remove_sidecars(staged)
        raise


def restore_backup(backup_id: str) -> dict:
    source = _backup_path(backup_id)
    selected_key = None
    safety_backup = None
    staged = None
    replaced = False
    live_path = Path(settings.db)
    try:
        with DATABASE_LOCK:
            _validate_snapshot(source)
            selected_key = _restore_key_sidecar(source)
            _validate_snapshot_key(source, selected_key)
            staged = _copy_to_stage(source, live_path.parent)

            # Apply this release's migrations on the staged copy first. If
            # staging fails, neither the live database nor key is touched.
            original_db_path = settings.db
            settings.db = str(staged)
            try:
                from .database import initialize

                initialize()
            finally:
                settings.db = original_db_path
            _validate_snapshot(staged)

            safety_backup = _create_snapshot_unlocked()
            _write_restore_journal(safety_backup, source)
            replaced = True
            _replace_live_database(staged, selected_key)
            staged = None
            with transaction() as con:
                con.execute("DELETE FROM settings WHERE key=?", (LAST_ERROR_KEY,))
            _clear_restore_journal()
            replaced = False
            _prune_backups()
        return {
            "restored_backup": backup_id,
            "safety_backup": _backup_record(safety_backup),
        }
    except BackupError as exc:
        if replaced and safety_backup:
            try:
                with DATABASE_LOCK:
                    rollback = _copy_to_stage(safety_backup, live_path.parent)
                    _replace_live_database(rollback, _restore_key_sidecar(safety_backup))
                    _clear_restore_journal()
            except Exception:
                LOGGER.exception("Failed to roll back an unsuccessful Bookward restore")
        _set_last_error("Restore failed; the previous database remains active.")
        raise exc
    except Exception as exc:
        LOGGER.exception("Bookward restore failed")
        if replaced and safety_backup:
            try:
                with DATABASE_LOCK:
                    rollback = _copy_to_stage(safety_backup, live_path.parent)
                    _replace_live_database(rollback, _restore_key_sidecar(safety_backup))
                    _clear_restore_journal()
            except Exception:
                LOGGER.exception("Failed to roll back an unsuccessful Bookward restore")
        _set_last_error("Restore failed; the previous database remains active.")
        raise BackupError(
            "Restore failed; the previous database remains active. Check the engine logs for details."
        ) from exc
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
            _remove_sidecars(staged)


async def backup_scheduler_loop(stop: asyncio.Event) -> None:
    """Create hourly-polled backups without depending on host cron."""

    try:
        await asyncio.wait_for(stop.wait(), timeout=5)
    except TimeoutError:
        pass
    while not stop.is_set():
        try:
            if backup_is_due():
                await asyncio.to_thread(create_backup)
        except Exception:
            LOGGER.exception("Scheduled Bookward backup failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except TimeoutError:
            pass
