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
from .database import MIGRATIONS, connect, database_lock, transaction
from .secrets import installation_key, installation_key_path
from .security import safe_error_message
from .tenancy import current_profile_id, profile_database_path

LOGGER = logging.getLogger(__name__)
BACKUP_ID = re.compile(r"^bookward-(\d{8}T\d{6}Z)-([0-9a-f]{8})$")
AUTH_BACKUP_ID = re.compile(r"^bookward-auth-(\d{8}T\d{6}Z)-([0-9a-f]{8})$")
BACKUP_INTERVAL_MAX_HOURS = 24 * 30
BACKUP_RETENTION_MAX = 100
LAST_ERROR_KEY = "backup_last_error"
RECOVERY_HOLD_KEY = "backup_recovery_hold"
RESTORE_JOURNAL_NAME = ".bookward-restore-in-progress.json"
AUTH_BACKUP_ERROR_KEY = "auth_backup_last_error"
AUTH_BACKUP_ATTEMPT_KEY = "auth_backup_last_attempt_at"
AUTH_BACKUP_SUCCESS_KEY = "auth_backup_last_success_at"
AUTH_BACKUP_RESTORE_KEY = "auth_backup_last_restore_at"
AUTH_BACKUP_HOLD_KEY = "auth_backup_recovery_hold"


class BackupError(ValueError):
    """A backup or restore could not be completed safely."""


class RecoveryHoldError(BackupError):
    """Clearing or overriding an active recovery hold requires confirmation."""


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


def _auth_id_time(backup_id: str) -> datetime | None:
    match = AUTH_BACKUP_ID.fullmatch(backup_id)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def has_auth_backups() -> bool:
    directory = auth_backup_directory()
    if not directory.is_dir():
        return False
    try:
        for path in directory.iterdir():
            if path.is_symlink() or path.suffix != ".sqlite3":
                continue
            if _auth_id_time(path.stem) and path.is_file():
                return True
    except OSError:
        LOGGER.exception("Could not inspect the identity-registry backup directory")
    return False


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


def _recovery_hold_value() -> str | None:
    path = profile_database_path()
    if not path.is_file():
        return None
    try:
        with connect() as con:
            found = con.execute(
                "SELECT value FROM settings WHERE key=?", (RECOVERY_HOLD_KEY,)
            ).fetchone()
            value = (found["value"] or "").strip() if found else ""
            return value or None
    except sqlite3.Error:
        return None


def recovery_hold_active() -> bool:
    return bool(_recovery_hold_value())


def dismiss_recovery_hold(*, confirm: bool = False) -> dict:
    if not confirm:
        raise RecoveryHoldError(
            "Confirm keeping the current database before resuming scheduled backups."
        )
    with database_lock(profile_database_path()):
        with transaction() as con:
            con.execute("DELETE FROM settings WHERE key=?", (RECOVERY_HOLD_KEY,))
    return backup_status()


def _auth_recovery_hold_value() -> str | None:
    from .profiles import auth_path

    registry = auth_path()
    if not registry.is_file():
        return None
    con = None
    try:
        con = sqlite3.connect(
            registry.resolve().as_uri() + "?mode=ro", uri=True, timeout=30
        )
        found = con.execute(
            "SELECT value FROM auth_meta WHERE key=?", (AUTH_BACKUP_HOLD_KEY,)
        ).fetchone()
        value = (found[0] or "").strip() if found else ""
        return value or None
    except sqlite3.Error:
        LOGGER.exception("Could not read identity-registry recovery hold status")
        return None
    finally:
        if con is not None:
            con.close()


def auth_recovery_hold_active() -> bool:
    return bool(_auth_recovery_hold_value())


def dismiss_auth_recovery_hold(*, confirm: bool = False) -> dict:
    if not confirm:
        raise RecoveryHoldError(
            "Confirm keeping the current account registry before resuming scheduled backups."
        )
    from .profiles import auth_path

    path = auth_path()
    if not path.is_file():
        raise BackupError("The Bookward identity registry is not available.")
    con = sqlite3.connect(path, timeout=30)
    try:
        con.execute("DELETE FROM auth_meta WHERE key=?", (AUTH_BACKUP_HOLD_KEY,))
        con.commit()
    finally:
        con.close()
    return identity_backup_status()


def backup_status() -> dict:
    backups = list_backups()
    last_error = None
    hold_value = None
    try:
        with connect() as con:
            stored = dict(
                con.execute(
                    "SELECT key,value FROM settings WHERE key IN (?,?)",
                    (LAST_ERROR_KEY, RECOVERY_HOLD_KEY),
                ).fetchall()
            )
            last_error = stored.get(LAST_ERROR_KEY) or None
            hold_value = (stored.get(RECOVERY_HOLD_KEY) or "").strip() or None
    except sqlite3.Error:
        last_error = "Backup status is temporarily unavailable."
    hold_active = bool(hold_value)
    interval = backup_interval_hours()
    last_success = backups[0]["created_at"] if backups else None
    next_backup = None
    if interval and last_success and not hold_active:
        parsed = datetime.fromisoformat(last_success)
        next_backup = (parsed + timedelta(hours=interval)).isoformat()
    return {
        "enabled": interval > 0,
        "interval_hours": interval,
        "retention_count": backup_retention_count(),
        "recovery_hold": hold_active,
        "recovery_hold_at": hold_value,
        "last_success_at": last_success,
        "last_error": last_error,
        "next_backup_at": next_backup,
        "backups": backups,
    }


def backup_is_due(now: datetime | None = None) -> bool:
    interval = backup_interval_hours()
    if interval <= 0:
        return False
    if recovery_hold_active():
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
    if auth_recovery_hold_active():
        return False
    directory = auth_backup_directory()
    if not directory.is_dir():
        return True
    snapshots = []
    for path in directory.iterdir():
        if path.is_symlink() or path.suffix != ".sqlite3" or not path.is_file():
            continue
        created = _auth_id_time(path.stem)
        if created is not None:
            snapshots.append(created)
    if not snapshots:
        return True
    latest = max(snapshots)
    from .profiles import auth_path

    registry = auth_path()
    if registry.is_file():
        con = None
        try:
            con = sqlite3.connect(
                registry.resolve().as_uri() + "?mode=ro", uri=True, timeout=30
            )
            restored = con.execute(
                "SELECT value FROM auth_meta WHERE key=?",
                (AUTH_BACKUP_RESTORE_KEY,),
            ).fetchone()
            if restored and restored[0]:
                restored_at = datetime.fromisoformat(restored[0])
                if restored_at.tzinfo is None:
                    restored_at = restored_at.replace(tzinfo=timezone.utc)
                latest = max(latest, restored_at.astimezone(timezone.utc))
        except (sqlite3.Error, ValueError):
            pass
        finally:
            if con is not None:
                con.close()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc) - latest >= timedelta(hours=interval)


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
                        "SELECT key,value FROM auth_meta WHERE key IN (?,?,?,?,?)",
                        (
                            AUTH_BACKUP_ERROR_KEY,
                            AUTH_BACKUP_ATTEMPT_KEY,
                            AUTH_BACKUP_SUCCESS_KEY,
                            AUTH_BACKUP_RESTORE_KEY,
                            AUTH_BACKUP_HOLD_KEY,
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
    hold_value = (state.get(AUTH_BACKUP_HOLD_KEY) or "").strip() or None
    return {
        "enabled": backup_interval_hours() > 0,
        "interval_hours": backup_interval_hours(),
        "retention_count": backup_retention_count(),
        "recovery_hold": bool(hold_value),
        "recovery_hold_at": hold_value,
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
            con.execute(
                "DELETE FROM auth_meta WHERE key IN ('setup_token_hash', ?)",
                (AUTH_BACKUP_HOLD_KEY,),
            )
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


def _prune_backups(*, preserve_ids: set[str] | None = None) -> None:
    retained = list_backups()
    retained_ids = {item["id"] for item in retained[: backup_retention_count()]}
    if preserve_ids:
        retained_ids.update(preserve_ids)
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


def create_backup(*, confirm_clear_hold: bool = False) -> dict:
    if recovery_hold_active() and not confirm_clear_hold:
        raise RecoveryHoldError(
            "Scheduled backups are paused to protect existing snapshots. Restore a backup or confirm keeping the current database first."
        )
    try:
        with database_lock(profile_database_path()):
            if recovery_hold_active() and not confirm_clear_hold:
                raise RecoveryHoldError(
                    "Scheduled backups are paused to protect existing snapshots. Restore a backup or confirm keeping the current database first."
                )
            path = _create_snapshot_unlocked()
            with transaction() as con:
                con.execute("DELETE FROM settings WHERE key=?", (RECOVERY_HOLD_KEY,))
                con.execute(
                    "INSERT INTO settings(key,value,secret) VALUES(?,?,0) "
                    "ON CONFLICT(key) DO UPDATE SET value='',secret=0,"
                    "updated_at=CURRENT_TIMESTAMP",
                    (LAST_ERROR_KEY, ""),
                )
            _prune_backups()
        return _backup_record(path)
    except RecoveryHoldError:
        raise
    except BackupError as exc:
        _set_last_error(f"Backup failed: {safe_error_message(exc, limit=240)}")
        raise
    except Exception as exc:
        detail = safe_error_message(exc, limit=200)
        LOGGER.exception("Bookward backup failed")
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


def _restore_journal_path() -> Path:
    path = profile_database_path()
    return path.parent / f".{path.stem}{RESTORE_JOURNAL_NAME}"


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
        with database_lock(profile_database_path()):
            staged = _copy_to_stage(safety_path, profile_database_path().parent)
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
            key_path = installation_key_path()
            os.replace(staged_key, key_path)
            os.chmod(key_path, 0o600)
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
    live_path = profile_database_path()
    try:
        with database_lock(live_path):
            _validate_snapshot(source)
            selected_key = _restore_key_sidecar(source)
            _validate_snapshot_key(source, selected_key)
            staged = _copy_to_stage(source, live_path.parent)

            # Apply this release's migrations on the staged copy first. If
            # staging fails, neither the live database nor key is touched.
            try:
                from .database import initialize
                from .tenancy import database_path_override

                with database_path_override(staged):
                    initialize(seed_demo=False)
            except Exception:
                raise
            _validate_snapshot(staged)

            safety_backup = _create_snapshot_unlocked()
            _write_restore_journal(safety_backup, source)
            replaced = True
            _replace_live_database(staged, selected_key)
            staged = None
            with transaction() as con:
                con.execute(
                    "DELETE FROM settings WHERE key IN (?,?)",
                    (LAST_ERROR_KEY, RECOVERY_HOLD_KEY),
                )
            _clear_restore_journal()
            replaced = False
            _prune_backups(preserve_ids={backup_id, safety_backup.stem})
        return {
            "restored_backup": backup_id,
            "safety_backup": _backup_record(safety_backup),
        }
    except Exception as exc:
        if not isinstance(exc, BackupError):
            LOGGER.exception("Bookward restore failed")
        rollback_failed = False
        if replaced:
            if safety_backup:
                try:
                    with database_lock(live_path):
                        rollback = _copy_to_stage(safety_backup, live_path.parent)
                        _replace_live_database(rollback, _restore_key_sidecar(safety_backup))
                        _clear_restore_journal()
                except Exception:
                    rollback_failed = True
                    LOGGER.exception("Failed to roll back an unsuccessful Bookward restore")
            else:
                rollback_failed = True
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


async def backup_scheduler_loop(stop: asyncio.Event) -> None:
    """Create hourly-polled backups without depending on host cron."""

    try:
        await asyncio.wait_for(stop.wait(), timeout=5)
    except TimeoutError:
        pass
    while not stop.is_set():
        from .profiles import profile_ids
        from .tenancy import profile_scope
        try:
            if auth_backup_is_due():
                await asyncio.to_thread(create_auth_backup)
        except Exception:
            LOGGER.exception("Scheduled identity-registry backup failed")
        for profile_id in profile_ids():
            try:
                with profile_scope(profile_id):
                    if backup_is_due():
                        await asyncio.to_thread(create_backup)
            except Exception:
                LOGGER.exception("Scheduled Bookward backup failed for profile %s", profile_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except TimeoutError:
            pass
