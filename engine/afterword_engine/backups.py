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
from .database import (
    MIGRATIONS,
    connect,
    database_lock,
    _LockedConnection,
    read_restore_epoch,
    record_restore_epoch,
    recorded_restore_epoch,
    RESTORE_EPOCH_KEY,
    _restrict_database_files,
    transaction,
)
from .decision_evidence import (
    EVIDENCE_HIGH_WATERMARK_RATIO,
    EVIDENCE_LOW_WATERMARK_RATIO,
    MAX_STORED_EVIDENCE_BYTES,
    evidence_budget_summary,
    is_evidence_rotation_enabled,
    is_storage_retention_enabled,
)
from .maintenance import (
    MAINTENANCE_INTERVAL_HOURS,
    MAINTENANCE_LAST_RUN_KEY,
    run_storage_maintenance,
)
from .secrets import installation_key, installation_key_path
from .security import safe_error_message
from .tenancy import (
    allow_restore_database_access,
    current_profile_id,
    profile_database_path,
    unbind_restore_epoch,
)

LOGGER = logging.getLogger(__name__)
BACKUP_ID = re.compile(r"^bookward-(\d{8}T\d{6}Z)-([0-9a-f]{8})$")
AUTH_BACKUP_ID = re.compile(r"^bookward-auth-(\d{8}T\d{6}Z)-([0-9a-f]{8})$")
BACKUP_INTERVAL_MAX_HOURS = 24 * 30
BACKUP_RETENTION_MAX = 100
LAST_ERROR_KEY = "backup_last_error"
RESTORE_JOURNAL_NAME = ".bookward-restore-in-progress.json"
AUTH_BACKUP_ERROR_KEY = "auth_backup_last_error"
AUTH_BACKUP_ATTEMPT_KEY = "auth_backup_last_attempt_at"
AUTH_BACKUP_SUCCESS_KEY = "auth_backup_last_success_at"
AUTH_BACKUP_RESTORE_KEY = "auth_backup_last_restore_at"
INCREMENTAL_VACUUM_STEP_PAGES = 4_096
MIN_VACUUM_FREE_SPACE_BYTES = 16 * 1024 * 1024
RESTORE_FENCE_DETAIL = (
    "Profile database is unavailable because an interrupted restore has not been "
    "recovered. Restore a valid backup or ask an administrator to clear the "
    "restore journal."
)
_RESTORE_JOURNAL_ERRORS: dict[str, str] = {}


class BackupError(ValueError):
    """A backup or restore could not be completed safely."""


def _has_restore_journal(journal: Path | None = None) -> bool:
    target = journal if journal is not None else _restore_journal_path()
    try:
        os.lstat(target)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def has_restore_journal(profile_id: str | None = None) -> bool:
    return _has_restore_journal(_restore_journal_path(profile_id))


def backup_directory() -> Path:
    configured = str(settings.backup_dir or "").strip()
    base = Path(configured) if configured else Path(settings.db).parent / "backups"
    profile_id = current_profile_id() or "legacy"
    return base if profile_id == "legacy" else base / profile_id


def auth_backup_directory() -> Path:
    configured = str(settings.backup_dir or "").strip()
    base = Path(configured) if configured else Path(settings.db).parent / "backups"
    return base / "auth-registry"


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
    journal = _restore_journal_path()
    if _has_restore_journal(journal):
        if message:
            _RESTORE_JOURNAL_ERRORS[str(journal)] = message[:300]
        else:
            _RESTORE_JOURNAL_ERRORS.pop(str(journal), None)
        return
    try:
        with database_lock(profile_database_path()).try_shared() as acquired:
            if not acquired:
                LOGGER.warning("Skipped persisting backup status while profile database is locked")
                return
            with unbind_restore_epoch(), transaction() as con:
                con.execute(
                    "INSERT INTO settings(key,value,secret) VALUES(?,?,0) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=0,"
                    "updated_at=CURRENT_TIMESTAMP",
                    (LAST_ERROR_KEY, message[:300]),
                )
    except Exception:
        LOGGER.exception("Could not persist backup status")


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size if not path.is_symlink() and path.is_file() else 0
    except OSError:
        return 0


def database_storage_metrics(backups: list[dict] | None = None) -> dict:
    db_path = profile_database_path()
    wal_path = Path(f"{db_path}-wal")
    database_bytes = _file_size(db_path)
    wal_bytes = _file_size(wal_path)
    live_bytes = database_bytes + wal_bytes
    items = list_backups() if backups is None else backups
    backup_total_bytes = sum(int(item.get("size_bytes") or 0) for item in items)
    quota_bytes = int(MAX_STORED_EVIDENCE_BYTES)
    default_metrics = {
        "retention_enabled": False,
        "evidence_rotation_enabled": False,
        "database_size_bytes": database_bytes,
        "wal_size_bytes": wal_bytes,
        "live_size_bytes": live_bytes,
        "reclaimable_bytes": 0,
        "backup_total_size_bytes": backup_total_bytes,
        "auto_vacuum_mode": "none",
        "evidence_used_bytes": 0,
        "evidence_quota_bytes": quota_bytes,
        "evidence_high_watermark_bytes": int(quota_bytes * EVIDENCE_HIGH_WATERMARK_RATIO),
        "evidence_low_watermark_bytes": int(quota_bytes * EVIDENCE_LOW_WATERMARK_RATIO),
        "evidence_usage_ratio": 0.0,
        "evidence_status": "healthy",
        "evidence_complete_runs": 0,
        "evidence_incomplete_runs": 0,
    }
    if not db_path.is_file() or _has_restore_journal():
        return default_metrics
    try:
        with connect() as con:
            page_size = int(con.execute("PRAGMA page_size").fetchone()[0] or 4096)
            freelist_count = int(con.execute("PRAGMA freelist_count").fetchone()[0] or 0)
            auto_vacuum_raw = int(con.execute("PRAGMA auto_vacuum").fetchone()[0] or 0)
            auto_vacuum_mode = {
                0: "none",
                1: "full",
                2: "incremental",
            }.get(auto_vacuum_raw, "none")
            evidence = evidence_budget_summary(con)
            return {
                "retention_enabled": is_storage_retention_enabled(con),
                "evidence_rotation_enabled": is_evidence_rotation_enabled(con),
                "database_size_bytes": database_bytes,
                "wal_size_bytes": wal_bytes,
                "live_size_bytes": live_bytes,
                "reclaimable_bytes": max(0, freelist_count * page_size),
                "backup_total_size_bytes": backup_total_bytes,
                "auto_vacuum_mode": auto_vacuum_mode,
                "evidence_used_bytes": int(evidence["used_bytes"]),
                "evidence_quota_bytes": int(evidence["quota_bytes"]),
                "evidence_high_watermark_bytes": int(evidence["high_watermark_bytes"]),
                "evidence_low_watermark_bytes": int(evidence["low_watermark_bytes"]),
                "evidence_usage_ratio": float(evidence["usage_ratio"]),
                "evidence_status": str(evidence["status"]),
                "evidence_complete_runs": int(evidence["complete_runs"]),
                "evidence_incomplete_runs": int(evidence["incomplete_runs"]),
            }
    except sqlite3.Error:
        return default_metrics


def backup_status() -> dict:
    backups = list_backups()
    last_error = None
    journal = _restore_journal_path()
    if _has_restore_journal(journal):
        last_error = _RESTORE_JOURNAL_ERRORS.get(str(journal)) or RESTORE_FENCE_DETAIL
    else:
        try:
            with connect() as con:
                found = con.execute(
                    "SELECT value FROM settings WHERE key=?", (LAST_ERROR_KEY,)
                ).fetchone()
                last_error = found["value"] if found else None
        except sqlite3.Error:
            last_error = "Backup status is temporarily unavailable."
    storage = database_storage_metrics(backups)
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
        **storage,
    }


def maintenance_is_due(now: datetime | None = None) -> bool:
    db_path = profile_database_path()
    if not db_path.is_file() or _has_restore_journal():
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    try:
        with connect() as con:
            if not is_storage_retention_enabled(con):
                return False
            row = con.execute(
                "SELECT value FROM settings WHERE key=?",
                (MAINTENANCE_LAST_RUN_KEY,),
            ).fetchone()
            if not row or not row["value"]:
                return True
            last_run = datetime.fromisoformat(str(row["value"]).replace("Z", "+00:00"))
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=timezone.utc)
    except (sqlite3.Error, ValueError):
        LOGGER.warning(
            "Could not inspect storage maintenance schedule; failing closed",
            exc_info=True,
        )
        return False
    return current.astimezone(timezone.utc) - last_run.astimezone(timezone.utc) >= timedelta(
        hours=MAINTENANCE_INTERVAL_HOURS
    )


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


def auth_backup_is_due(now: datetime | None = None) -> bool:
    interval = backup_interval_hours()
    if interval <= 0:
        return False
    directory = auth_backup_directory()
    if not directory.is_dir():
        return True
    snapshots = []
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file() or not AUTH_BACKUP_ID.fullmatch(path.stem):
            continue
        match = AUTH_BACKUP_ID.fullmatch(path.stem)
        try:
            snapshots.append(datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    if not snapshots:
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc) - max(snapshots) >= timedelta(hours=interval)


def _record_auth_backup_state(*, attempted_at=None, success_at=None, restore_at=None, error=None) -> None:
    """Persist safe backup telemetry without making it part of profile data."""

    from .profiles import auth_path

    path = auth_path()
    if not path.is_file():
        return
    values = {}
    if attempted_at is not None:
        values[AUTH_BACKUP_ATTEMPT_KEY] = attempted_at
    if success_at is not None:
        values[AUTH_BACKUP_SUCCESS_KEY] = success_at
        values[AUTH_BACKUP_ERROR_KEY] = ""
    if restore_at is not None:
        values[AUTH_BACKUP_RESTORE_KEY] = restore_at
        values[AUTH_BACKUP_ERROR_KEY] = ""
    elif error is not None:
        values[AUTH_BACKUP_ERROR_KEY] = safe_error_message(error, limit=300)
    try:
        con = sqlite3.connect(path, timeout=30)
        try:
            for key, value in values.items():
                con.execute(
                    "INSERT INTO auth_meta(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        LOGGER.exception("Could not persist identity-registry backup status")


def identity_backup_status() -> dict:
    """Return identity-registry backup metadata without exposing its contents."""

    from .profiles import auth_path

    state = {}
    registry = auth_path()
    try:
        if registry.is_file():
            con = sqlite3.connect(registry.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
            try:
                state = dict(
                    con.execute(
                        "SELECT key,value FROM auth_meta WHERE key IN (?,?,?,?)",
                        (
                            AUTH_BACKUP_ERROR_KEY,
                            AUTH_BACKUP_ATTEMPT_KEY,
                            AUTH_BACKUP_SUCCESS_KEY,
                            AUTH_BACKUP_RESTORE_KEY,
                        ),
                    ).fetchall()
                )
            finally:
                con.close()
    except sqlite3.Error:
        state[AUTH_BACKUP_ERROR_KEY] = "Identity backup status is temporarily unavailable."

    directory = auth_backup_directory()
    backups = []
    try:
        if directory.is_dir():
            for path in directory.iterdir():
                if path.is_symlink() or not path.is_file() or not AUTH_BACKUP_ID.fullmatch(path.stem):
                    continue
                match = AUTH_BACKUP_ID.fullmatch(path.stem)
                try:
                    created = datetime.strptime(
                        match.group(1), "%Y%m%dT%H%M%SZ"
                    ).replace(tzinfo=timezone.utc)
                    backups.append(
                        {
                            "id": path.stem,
                            "created_at": created.isoformat(),
                            "size_bytes": path.stat().st_size,
                        }
                    )
                except (OSError, ValueError):
                    continue
    except OSError:
        if not state.get(AUTH_BACKUP_ERROR_KEY):
            state[AUTH_BACKUP_ERROR_KEY] = "Identity backup files are temporarily unavailable."
    backups.sort(key=lambda item: item["created_at"], reverse=True)
    return {
        "enabled": backup_interval_hours() > 0,
        "interval_hours": backup_interval_hours(),
        "retention_count": backup_retention_count(),
        "last_success_at": state.get(AUTH_BACKUP_SUCCESS_KEY)
        or (backups[0]["created_at"] if backups else None),
        "last_attempt_at": state.get(AUTH_BACKUP_ATTEMPT_KEY),
        "last_restore_at": state.get(AUTH_BACKUP_RESTORE_KEY),
        "last_error": state.get(AUTH_BACKUP_ERROR_KEY) or None,
        "backups": backups,
    }


def create_auth_backup(*, prune: bool = True) -> dict:
    """Snapshot the account/profile registry alongside personal database backups."""
    from .profiles import auth_path

    source_path = auth_path()
    if not source_path.is_file():
        raise BackupError("The Bookward identity registry is not available for backup.")
    attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _record_auth_backup_state(attempted_at=attempted_at)
    directory = auth_backup_directory()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_id = f"bookward-auth-{stamp}-{uuid.uuid4().hex[:8]}"
    destination = directory / f"{backup_id}.sqlite3"
    temporary = None
    source = target = None
    try:
        _ensure_private_directory(directory)
        descriptor, name = tempfile.mkstemp(prefix=".bookward-auth-", suffix=".tmp", dir=directory)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        source = sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        target = sqlite3.connect(temporary, timeout=30)
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok" or target.execute("PRAGMA foreign_key_check").fetchone():
            raise BackupError("The identity registry backup could not be verified.")
        target.commit()
        target.close(); target = None
        source.close(); source = None
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if prune:
            retention = backup_retention_count()
            snapshots = sorted(
                (path for path in directory.iterdir() if path.is_file() and not path.is_symlink() and AUTH_BACKUP_ID.fullmatch(path.stem)),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            for old in snapshots[retention:]:
                old.unlink(missing_ok=True)
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _record_auth_backup_state(success_at=created_at)
        return {"id": backup_id, "created_at": created_at, "size_bytes": destination.stat().st_size}
    except Exception as exc:
        _record_auth_backup_state(error=exc)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                LOGGER.exception("Could not remove an incomplete identity-registry backup")
        raise
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()


def _validate_auth_snapshot(path: Path) -> None:
    con = None
    try:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        integrity = con.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok" or con.execute("PRAGMA foreign_key_check").fetchone():
            raise BackupError("That identity-registry snapshot is damaged.")
        tables = {
            item[0]
            for item in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {"auth_meta", "profiles", "accounts", "oidc_identities", "auth_sessions", "api_tokens"}
        if not required <= tables:
            raise BackupError("That file is not a Bookward identity-registry backup.")
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise BackupError("That identity-registry snapshot is damaged.") from exc
    finally:
        if con is not None:
            con.close()


def _replace_auth_registry(staged: Path, live: Path, on_replace=None) -> None:
    if live.is_file():
        checkpoint = sqlite3.connect(live, timeout=30)
        try:
            result = checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result and result[0] != 0:
                raise BackupError("The active identity registry could not be safely closed for restore.")
        finally:
            checkpoint.close()
    _remove_sidecars(live)
    os.replace(staged, live)
    if on_replace is not None:
        on_replace()
    os.chmod(live, 0o600)
    directory_fd = os.open(live.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def restore_auth_backup(backup_id: str) -> dict:
    """Restore a staged registry, retaining a pre-restore safety snapshot.

    This is an operator recovery action and must only run with the engine
    stopped. It is intentionally not exposed as a browser route.
    """
    if not AUTH_BACKUP_ID.fullmatch(backup_id):
        raise BackupError("Choose a valid identity-registry snapshot.")
    directory = auth_backup_directory()
    path = directory / f"{backup_id}.sqlite3"
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
        raise BackupError("That identity-registry snapshot is unavailable.")
    _validate_auth_snapshot(path)
    from .profiles import auth_path

    live = auth_path()
    live.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(
        prefix=".bookward-auth-restore-", suffix=".sqlite3", dir=live.parent
    )
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    staged = Path(name)
    replaced = False
    safety_backup = None

    def mark_replaced():
        nonlocal replaced
        replaced = True

    try:
        shutil.copyfile(path, staged)
        os.chmod(staged, 0o600)
        con = sqlite3.connect(staged, timeout=30)
        try:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE auth_sessions SET revoked_at=COALESCE(revoked_at,CURRENT_TIMESTAMP)")
            con.execute("UPDATE api_tokens SET revoked_at=COALESCE(revoked_at,CURRENT_TIMESTAMP)")
            con.execute("DELETE FROM auth_transactions")
            con.execute("DELETE FROM login_attempts")
            con.execute("DELETE FROM auth_meta WHERE key='setup_token_hash'")
            con.commit()
            integrity = con.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok" or con.execute("PRAGMA foreign_key_check").fetchone():
                raise BackupError("The restored identity registry failed integrity checks.")
        finally:
            con.close()
        with staged.open("rb") as handle:
            os.fsync(handle.fileno())
        # Protect the selected snapshot from retention pruning before taking
        # the safety copy, and preserve the operator safety copy itself.
        safety_backup = create_auth_backup(prune=False) if live.is_file() else None
        _replace_auth_registry(staged, live, on_replace=mark_replaced)
        _record_auth_backup_state(restore_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return {"restored": True, "sessions_revoked": True, "api_tokens_revoked": True}
    except Exception as exc:
        if replaced and safety_backup:
            safety_path = directory / f"{safety_backup['id']}.sqlite3"
            rollback = None
            try:
                rollback = _copy_to_stage(safety_path, live.parent)
                _replace_auth_registry(rollback, live)
                rollback = None
            except Exception:
                LOGGER.exception("Failed to roll back an unsuccessful identity-registry restore")
            finally:
                if rollback is not None:
                    rollback.unlink(missing_ok=True)
        _record_auth_backup_state(error=exc)
        raise
    finally:
        staged.unlink(missing_ok=True)
        _remove_sidecars(staged)


def _create_snapshot_unlocked() -> Path:
    if _has_restore_journal():
        raise BackupError(
            "Backup cannot run while an interrupted restore is awaiting recovery."
        )
    source_path = profile_database_path()
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
    journal = _restore_journal_path()
    protected_ids: set[str] = set()
    if _has_restore_journal(journal):
        try:
            safety_id, selected_id, _ = _read_restore_journal_meta(journal)
        except Exception:
            # Never prune snapshots when an active restore journal cannot be
            # inspected; retaining extra files is safer than losing rollback data.
            return
        protected_ids.add(safety_id)
        if selected_id:
            protected_ids.add(selected_id)
    retained = list_backups()
    retained_ids = (
        {item["id"] for item in retained[: backup_retention_count()]} | protected_ids
    )
    for old in retained[backup_retention_count() :]:
        if old["id"] in retained_ids:
            continue
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


def _reclaim_incremental_pages_step(
    live_path: Path,
    *,
    max_pages: int = INCREMENTAL_VACUUM_STEP_PAGES,
) -> int:
    """Reclaim up to `max_pages` free pages online without exclusive `database_lock`."""

    if not live_path.is_file() or live_path.is_symlink() or _has_restore_journal():
        return 0
    con = None
    try:
        con = sqlite3.connect(
            live_path,
            timeout=10,
            isolation_level=None,
            factory=_LockedConnection,
        )
        con.execute("PRAGMA busy_timeout=5000")
        auto_vacuum = int(con.execute("PRAGMA auto_vacuum").fetchone()[0] or 0)
        if auto_vacuum != 2:
            return 0
        before_free = int(con.execute("PRAGMA freelist_count").fetchone()[0] or 0)
        if before_free <= 0:
            return 0
        con.execute(f"PRAGMA incremental_vacuum({max(1, int(max_pages))})").fetchall()
        after_free = int(con.execute("PRAGMA freelist_count").fetchone()[0] or 0)
        return max(0, before_free - after_free)
    finally:
        if con is not None:
            con.close()
        _restrict_database_files()


def _checkpoint_wal_unlocked(live_path: Path, *, mode: str = "PASSIVE") -> None:
    if not live_path.is_file() or live_path.is_symlink() or _has_restore_journal():
        return
    checkpoint_mode = mode if mode in {"PASSIVE", "TRUNCATE"} else "PASSIVE"
    raw = None
    try:
        raw = sqlite3.connect(live_path, timeout=30, isolation_level=None)
        raw.execute(f"PRAGMA wal_checkpoint({checkpoint_mode})").fetchone()
    finally:
        if raw is not None:
            raw.close()
        _restrict_database_files()


def _run_maintenance_if_initialized(*, now: datetime | None = None) -> dict | None:
    live_path = profile_database_path()
    if not live_path.is_file() or live_path.is_symlink() or _has_restore_journal():
        return None
    with connect() as con:
        has_table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recommendation_evidence_budget'"
        ).fetchone()
    if not has_table:
        return None
    return run_storage_maintenance(now=now)


def run_scheduled_maintenance(*, now: datetime | None = None) -> dict:
    """Run batched retention outside `database_lock` and reclaim bounded free pages."""

    if _has_restore_journal():
        return {}
    live_path = profile_database_path()
    maintenance = _run_maintenance_if_initialized(now=now) or {}
    if maintenance and not maintenance.get("skipped") and not maintenance.get("truncated"):
        reclaimed_pages = _reclaim_incremental_pages_step(
            live_path, max_pages=INCREMENTAL_VACUUM_STEP_PAGES
        )
        if reclaimed_pages > 0:
            _checkpoint_wal_unlocked(live_path, mode="PASSIVE")
    return maintenance


def compact_database() -> dict:
    """Compact unused SQLite pages and truncate the WAL without deleting rows."""

    live_path = profile_database_path()
    try:
        if _has_restore_journal():
            raise BackupError(
                "Storage compaction cannot run while an interrupted restore is awaiting recovery."
            )
        if not live_path.is_file() or live_path.is_symlink():
            raise BackupError("The Bookward database is not available for compaction.")
        before_bytes = _file_size(live_path) + _file_size(Path(f"{live_path}-wal"))
        with connect() as con:
            auto_vacuum = int(con.execute("PRAGMA auto_vacuum").fetchone()[0] or 0)

        migrated_auto_vacuum = False
        if auto_vacuum == 2:
            # Drain free pages in bounded steps outside `database_lock` so
            # concurrent read/write requests can interleave freely.
            while (
                _reclaim_incremental_pages_step(
                    live_path, max_pages=INCREMENTAL_VACUUM_STEP_PAGES
                )
                >= INCREMENTAL_VACUUM_STEP_PAGES
            ):
                pass
            with database_lock(live_path):
                if _has_restore_journal():
                    raise BackupError(
                        "Storage compaction cannot run while an interrupted restore is awaiting recovery."
                    )
                raw = None
                try:
                    raw = sqlite3.connect(live_path, timeout=30, isolation_level=None)
                    raw.execute(
                        f"PRAGMA incremental_vacuum({INCREMENTAL_VACUUM_STEP_PAGES})"
                    ).fetchall()
                    raw.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                finally:
                    if raw is not None:
                        raw.close()
                    _restrict_database_files()
        else:
            with database_lock(live_path):
                if _has_restore_journal():
                    raise BackupError(
                        "Storage compaction cannot run while an interrupted restore is awaiting recovery."
                    )
                if not live_path.is_file() or live_path.is_symlink():
                    raise BackupError(
                        "The Bookward database is not available for compaction."
                    )
                raw = None
                try:
                    raw = sqlite3.connect(live_path, timeout=30, isolation_level=None)
                    current_auto_vacuum = int(
                        raw.execute("PRAGMA auto_vacuum").fetchone()[0] or 0
                    )
                    if current_auto_vacuum != 2:
                        current_bytes = _file_size(live_path) + _file_size(
                            Path(f"{live_path}-wal")
                        )
                        required_free = max(
                            MIN_VACUUM_FREE_SPACE_BYTES, current_bytes * 2
                        )
                        available_free = shutil.disk_usage(live_path.parent).free
                        if available_free < required_free:
                            raise BackupError(
                                "Not enough free disk space to migrate this database to incremental compaction."
                            )
                        raw.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                        raw.execute("PRAGMA auto_vacuum=INCREMENTAL")
                        raw.execute("VACUUM")
                        raw.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                        migrated_auto_vacuum = True
                    else:
                        raw.execute("PRAGMA incremental_vacuum").fetchall()
                        raw.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                finally:
                    if raw is not None:
                        raw.close()
                    _restrict_database_files()
        status = backup_status()
        after_bytes = int(status.get("live_size_bytes", 0))
        return {
            "before_bytes": before_bytes,
            "after_bytes": after_bytes,
            "reclaimed_bytes": max(0, before_bytes - after_bytes),
            "migrated_auto_vacuum": migrated_auto_vacuum,
            "auto_vacuum_mode": status.get("auto_vacuum_mode", "incremental"),
            "status": status,
        }
    except BackupError:
        raise
    except Exception as exc:
        detail = safe_error_message(exc, limit=200)
        LOGGER.exception("Bookward storage compaction failed")
        raise BackupError(f"Storage compaction failed: {detail}") from exc


def create_backup() -> dict:
    try:
        with database_lock(profile_database_path()):
            if _has_restore_journal():
                raise BackupError(
                    "Backup cannot run while an interrupted restore is awaiting recovery."
                )
            path = _create_snapshot_unlocked()
            _set_last_error("")
            _prune_backups()
        return _backup_record(path)
    except BackupError as exc:
        if not _has_restore_journal():
            _set_last_error(f"Backup failed: {safe_error_message(exc, limit=240)}")
        raise
    except Exception as exc:
        detail = safe_error_message(exc, limit=200)
        LOGGER.exception("Bookward backup failed")
        if not _has_restore_journal():
            _set_last_error(f"Backup failed: {detail}")
        raise BackupError(f"Backup failed: {detail}") from exc


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


def _restore_journal_path(profile_id: str | None = None) -> Path:
    path = (
        profile_database_path()
        if profile_id is None
        else profile_database_path(profile_id)
    )
    return path.parent / f".{path.stem}{RESTORE_JOURNAL_NAME}"


def _restore_journal_key_path(journal: Path | None = None) -> Path:
    target = journal if journal is not None else _restore_journal_path()
    return target.with_name(f"{target.name}.key")


def _write_restore_journal(
    safety_backup: Path, selected_backup: Path, preserve_live_key: bool = True
) -> None:
    journal = _restore_journal_path()
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal_key = _restore_journal_key_path(journal)
    live_key = installation_key_path()
    had_key_file = bool(
        preserve_live_key and live_key.is_file() and not live_key.is_symlink()
    )
    if had_key_file:
        key_fd, key_tmp_name = tempfile.mkstemp(
            prefix=".bookward-restore-key-", suffix=".tmp", dir=journal.parent
        )
        os.fchmod(key_fd, 0o600)
        key_tmp = Path(key_tmp_name)
        try:
            with os.fdopen(key_fd, "wb") as handle:
                handle.write(live_key.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(key_tmp, journal_key)
        except Exception:
            key_tmp.unlink(missing_ok=True)
            raise
    else:
        journal_key.unlink(missing_ok=True)
    payload = json.dumps(
        {
            "safety_backup": safety_backup.stem,
            "selected_backup": selected_backup.stem,
            "had_key_file": had_key_file,
        }
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
        _RESTORE_JOURNAL_ERRORS.pop(str(journal), None)
        directory_fd = os.open(journal.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        journal_key.unlink(missing_ok=True)
        raise


def _clear_restore_journal() -> None:
    journal = _restore_journal_path()
    journal.unlink(missing_ok=True)
    _restore_journal_key_path(journal).unlink(missing_ok=True)
    _RESTORE_JOURNAL_ERRORS.pop(str(journal), None)
    if journal.parent.is_dir():
        directory_fd = os.open(journal.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def clear_interrupted_restore_journal() -> bool:
    with database_lock(profile_database_path()):
        existed = _has_restore_journal()
        _clear_restore_journal()
        return existed


def _read_restore_journal_meta(journal: Path) -> tuple[str, str | None, bool]:
    if journal.is_symlink() or not journal.is_file():
        raise BackupError("The interrupted restore journal is invalid.")
    try:
        payload = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("The interrupted restore journal is invalid.") from exc
    if not isinstance(payload, dict):
        raise BackupError("The interrupted restore journal is invalid.")
    safety_id = payload.get("safety_backup")
    if not isinstance(safety_id, str) or not safety_id.strip():
        raise BackupError("The interrupted restore journal is invalid.")
    selected_id = payload.get("selected_backup")
    had_key_file = bool(payload.get("had_key_file", False))
    return (
        safety_id,
        selected_id if isinstance(selected_id, str) and selected_id else None,
        had_key_file,
    )


def _validate_key_file(path: Path) -> Path:
    try:
        key = path.read_bytes().strip()
        Fernet(key)
    except (OSError, ValueError) as exc:
        raise BackupError("This backup's generated encryption key is invalid.") from exc
    return path


def _rollback_key_for_snapshot(
    safety_path: Path, journal: Path, had_key_file: bool
) -> tuple[Path | None, bool]:
    sidecar = _key_sidecar(safety_path)
    if not sidecar.is_symlink() and sidecar.is_file():
        _validate_key_file(sidecar)
        _validate_snapshot_key(
            safety_path, None if _has_configured_key() else sidecar
        )
        return sidecar, False
    journal_key = _restore_journal_key_path(journal)
    if not journal_key.is_symlink() and journal_key.is_file():
        _validate_key_file(journal_key)
        _validate_snapshot_key(
            safety_path, None if _has_configured_key() else journal_key
        )
        return journal_key, False
    key_source = _restore_key_sidecar(safety_path)
    _validate_snapshot_key(safety_path, key_source)
    return key_source, key_source is None and not had_key_file


def recover_interrupted_restore() -> bool:
    """Roll back a database/key pair if the engine stopped during restore."""

    journal = _restore_journal_path()
    if not _has_restore_journal(journal):
        return False
    try:
        live_path = profile_database_path()
        lock = database_lock(live_path)
        with unbind_restore_epoch(), lock.quiesce() as deadline, lock.exclusive(deadline):
            journal = _restore_journal_path()
            if not _has_restore_journal(journal):
                return False
            safety_id, _, had_key_file = _read_restore_journal_meta(journal)
            safety_path = _backup_path(safety_id)
            _validate_snapshot(safety_path)
            safety_key, clear_key = _rollback_key_for_snapshot(
                safety_path, journal, had_key_file
            )
            live_parent = live_path.parent
            live_parent.mkdir(parents=True, exist_ok=True)
            staged, next_epoch = _stage_restored_database(
                safety_path,
                live_parent,
                minimum_epoch=_read_file_restore_epoch(live_path),
                migrate=True,
            )
            try:
                if clear_key:
                    _replace_live_database(
                        staged, safety_key, rollback=True, clear_generated_key=True
                    )
                else:
                    _replace_live_database(staged, safety_key, rollback=True)
                staged = None
            finally:
                if staged is not None:
                    staged.unlink(missing_ok=True)
                    _remove_sidecars(staged)
            record_restore_epoch(next_epoch, live_path)
            with allow_restore_database_access():
                _clear_restore_journal()
                _set_last_error("An interrupted restore was rolled back to its safety backup.")
        return True
    except BackupError as exc:
        _set_last_error(safe_error_message(exc, limit=200))
        LOGGER.exception("Could not recover an interrupted Bookward restore")
        raise
    except Exception as exc:
        message = (
            "An interrupted restore needs its safety backup and matching encryption key."
        )
        _set_last_error(message)
        LOGGER.exception("Could not recover an interrupted Bookward restore")
        raise BackupError(message) from exc


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


def _is_sqlite_lock_error(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and (code & 0xFF) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }:
        return True
    lowered = str(exc).casefold()
    return "locked" in lowered or "busy" in lowered


def _is_lock_backup_error(exc: BackupError) -> bool:
    return str(exc) == "The active database could not be safely closed for restore."


def _replace_live_database(
    staged: Path,
    key_source: Path | None = None,
    *,
    rollback: bool = False,
    clear_generated_key: bool = False,
) -> None:
    live = profile_database_path()
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
    key_path = installation_key_path()
    previous_key: bytes | None = None
    had_existing_key = False
    key_mutated = False
    db_replaced = False
    try:
        if live.is_symlink():
            raise BackupError("The active database path is invalid.")
        if not rollback or live.exists():
            checkpoint = None
            try:
                checkpoint = sqlite3.connect(live, timeout=0.1 if rollback else 30)
                result = checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if result and result[0] != 0:
                    raise BackupError(
                        "The active database could not be safely closed for restore."
                    )
            except BackupError:
                raise
            except sqlite3.DatabaseError as exc:
                if _is_sqlite_lock_error(exc):
                    raise BackupError(
                        "The active database could not be safely closed for restore."
                    ) from exc
                if not rollback:
                    raise
            finally:
                if checkpoint is not None:
                    checkpoint.close()
        _remove_sidecars(live)
        if staged_key is not None or (rollback and clear_generated_key):
            if key_path.is_symlink():
                raise BackupError("The active database path is invalid.")
            if key_path.is_file():
                had_existing_key = True
                previous_key = key_path.read_bytes()
        if staged_key is not None:
            os.replace(staged_key, key_path)
            staged_key = None
            key_mutated = True
            os.chmod(key_path, 0o600)
        elif rollback and clear_generated_key:
            key_path.unlink(missing_ok=True)
            key_mutated = True
        os.replace(staged, live)
        db_replaced = True
        os.chmod(live, 0o600)
        directory_fd = os.open(live.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if key_mutated and not db_replaced:
            try:
                if had_existing_key and previous_key is not None:
                    restore_fd, restore_name = tempfile.mkstemp(
                        prefix=".bookward-key-rollback-",
                        suffix=".tmp",
                        dir=live.parent,
                    )
                    os.fchmod(restore_fd, 0o600)
                    restore_tmp = Path(restore_name)
                    try:
                        with os.fdopen(restore_fd, "wb") as handle:
                            handle.write(previous_key)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(restore_tmp, key_path)
                        os.chmod(key_path, 0o600)
                    except Exception:
                        restore_tmp.unlink(missing_ok=True)
                else:
                    key_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.exception("Failed to restore previous installation key")
        raise
    finally:
        if staged_key is not None:
            staged_key.unlink(missing_ok=True)


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


def _read_file_restore_epoch(path: Path) -> int:
    baseline = recorded_restore_epoch(path)
    if not path.is_file():
        return baseline
    con = None
    try:
        con = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30
        )
        return max(baseline, read_restore_epoch(con))
    except (OSError, sqlite3.Error):
        return baseline
    finally:
        if con is not None:
            con.close()


def _stage_restored_database(
    source: Path,
    directory: Path,
    *,
    minimum_epoch: int,
    migrate: bool = True,
) -> tuple[Path, int]:
    staged = _copy_to_stage(source, directory)
    try:
        if migrate:
            from .database import initialize
            from .tenancy import database_path_override

            with database_path_override(staged):
                initialize(seed_demo=False)
        con = sqlite3.connect(staged, timeout=30)
        try:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("BEGIN IMMEDIATE")
            tables = {
                item[0]
                for item in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "jobs" in tables:
                job_columns = {
                    item[1] for item in con.execute("PRAGMA table_info(jobs)")
                }
                if {"started_at", "heartbeat_at", "lease_token", "attempts"} <= job_columns:
                    con.execute(
                        "UPDATE jobs SET status='queued', started_at=NULL, "
                        "heartbeat_at=NULL, lease_token=NULL, "
                        "attempts=CASE WHEN COALESCE(attempts, 0) > 0 THEN attempts - 1 ELSE 0 END "
                        "WHERE status='running'"
                    )
                elif "started_at" in job_columns:
                    con.execute(
                        "UPDATE jobs SET status='queued', started_at=NULL "
                        "WHERE status='running'"
                    )
                else:
                    con.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            con.execute(
                "CREATE TABLE IF NOT EXISTS settings ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                "secret INTEGER NOT NULL DEFAULT 0, "
                "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            staged_epoch = read_restore_epoch(con)
            next_epoch = max(minimum_epoch, staged_epoch) + 1
            setting_columns = {
                item[1] for item in con.execute("PRAGMA table_info(settings)")
            }
            if "updated_at" in setting_columns:
                con.execute(
                    "INSERT INTO settings(key,value,secret) VALUES(?,?,0) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=0,"
                    "updated_at=CURRENT_TIMESTAMP",
                    (RESTORE_EPOCH_KEY, str(next_epoch)),
                )
            else:
                con.execute(
                    "INSERT INTO settings(key,value,secret) VALUES(?,?,0) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=0",
                    (RESTORE_EPOCH_KEY, str(next_epoch)),
                )
            con.commit()
            mode = con.execute("PRAGMA journal_mode").fetchone()
            if mode and str(mode[0]).lower() == "wal":
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                con.execute("PRAGMA journal_mode=DELETE")
        finally:
            con.close()
        _remove_sidecars(staged)
        os.chmod(staged, 0o600)
        with staged.open("rb") as handle:
            os.fsync(handle.fileno())
        _validate_snapshot(staged)
        return staged, next_epoch
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
    rollback_failed = False
    had_unrecoverable_journal = False
    live_path = profile_database_path()
    lock = database_lock(live_path)
    try:
        _validate_snapshot(source)
        selected_key = _restore_key_sidecar(source)
        _validate_snapshot_key(source, selected_key)
        with unbind_restore_epoch(), lock.quiesce() as deadline, lock.exclusive(deadline):
            try:
                if _has_restore_journal():
                    try:
                        recover_interrupted_restore()
                    except BackupError as exc:
                        if _is_lock_backup_error(exc):
                            raise
                        had_unrecoverable_journal = True
                source = _backup_path(backup_id)
                _validate_snapshot(source)
                selected_key = _restore_key_sidecar(source)
                _validate_snapshot_key(source, selected_key)
                live_path.parent.mkdir(parents=True, exist_ok=True)
                # Apply this release's migrations, requeue any orphaned running
                # jobs while keeping queued work intact, and advance
                # restore_epoch on the staged copy first. If staging fails,
                # neither the live database nor key is touched.
                staged, next_epoch = _stage_restored_database(
                    source,
                    live_path.parent,
                    minimum_epoch=_read_file_restore_epoch(live_path),
                    migrate=True,
                )

                if had_unrecoverable_journal:
                    safety_backup = source
                    _write_restore_journal(safety_backup, source, False)
                    replaced = True
                    if selected_key is None:
                        _replace_live_database(
                            staged,
                            selected_key,
                            rollback=True,
                            clear_generated_key=True,
                        )
                    else:
                        _replace_live_database(staged, selected_key, rollback=True)
                else:
                    safety_backup = _create_snapshot_unlocked()
                    _write_restore_journal(safety_backup, source)
                    replaced = True
                    _replace_live_database(staged, selected_key)
                staged = None
                record_restore_epoch(next_epoch, live_path)
                with allow_restore_database_access():
                    with transaction() as con:
                        con.execute("DELETE FROM settings WHERE key=?", (LAST_ERROR_KEY,))
                    _clear_restore_journal()
                replaced = False
                _prune_backups()
            except Exception:
                if replaced:
                    if safety_backup:
                        rollback = None
                        try:
                            journal = _restore_journal_path()
                            had_key_file = False
                            if _has_restore_journal(journal):
                                try:
                                    _, _, had_key_file = _read_restore_journal_meta(journal)
                                except BackupError:
                                    had_key_file = False
                            safety_key, clear_key = _rollback_key_for_snapshot(
                                safety_backup, journal, had_key_file
                            )
                            rollback, rollback_epoch = _stage_restored_database(
                                safety_backup,
                                live_path.parent,
                                minimum_epoch=_read_file_restore_epoch(live_path),
                                migrate=True,
                            )
                            if clear_key:
                                _replace_live_database(
                                    rollback,
                                    safety_key,
                                    rollback=True,
                                    clear_generated_key=True,
                                )
                            else:
                                _replace_live_database(
                                    rollback,
                                    safety_key,
                                    rollback=True,
                                )
                            rollback = None
                            record_restore_epoch(rollback_epoch, live_path)
                            _clear_restore_journal()
                        except Exception:
                            rollback_failed = True
                            LOGGER.exception(
                                "Failed to roll back an unsuccessful Bookward restore"
                            )
                        finally:
                            if rollback is not None:
                                rollback.unlink(missing_ok=True)
                                _remove_sidecars(rollback)
                    else:
                        rollback_failed = True
                raise
        return {
            "restored_backup": backup_id,
            "safety_backup": _backup_record(safety_backup),
        }
    except Exception as exc:
        if not isinstance(exc, BackupError):
            LOGGER.exception("Bookward restore failed")
        detail = safe_error_message(exc, limit=200)
        if rollback_failed:
            message = f"Restore failed; automatic rollback did not complete. {detail}"
        else:
            message = f"Restore failed; the previous database remains active. {detail}"
        _set_last_error(message)
        if rollback_failed or not isinstance(exc, BackupError):
            raise BackupError(message) from exc
        raise exc
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
            _remove_sidecars(staged)


def _run_scheduled_auth_backup() -> dict | None:
    if auth_backup_is_due():
        return create_auth_backup()
    return None


def _run_scheduled_profile_backup(profile_id: str) -> dict | None:
    from .tenancy import profile_scope

    with profile_scope(profile_id):
        if _has_restore_journal() or database_lock().is_locked():
            return None
        try:
            if maintenance_is_due():
                run_scheduled_maintenance()
        except Exception:
            LOGGER.exception(
                "Scheduled Bookward storage maintenance failed for profile %s", profile_id
            )
        if not backup_is_due():
            return None
        return create_backup()


async def backup_scheduler_loop(stop: asyncio.Event) -> None:
    """Create hourly-polled backups without depending on host cron."""

    try:
        await asyncio.wait_for(stop.wait(), timeout=5)
    except TimeoutError:
        pass
    while not stop.is_set():
        from .profiles import profile_ids

        try:
            await asyncio.to_thread(_run_scheduled_auth_backup)
        except Exception:
            LOGGER.exception("Scheduled identity-registry backup failed")
        try:
            active_profiles = await asyncio.to_thread(profile_ids)
        except Exception:
            LOGGER.exception("Could not enumerate profiles for scheduled backups")
            active_profiles = []
        for profile_id in active_profiles:
            if stop.is_set():
                break
            try:
                await asyncio.to_thread(_run_scheduled_profile_backup, profile_id)
            except Exception:
                LOGGER.exception("Scheduled Bookward backup failed for profile %s", profile_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except TimeoutError:
            pass
