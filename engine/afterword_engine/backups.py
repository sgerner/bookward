"""Private, WAL-safe SQLite backups and guarded in-place recovery."""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

from .config import settings
from .database import (
    MIGRATIONS,
    RESTORE_EPOCH_KEY,
    TOMBSTONE_SUFFIX,
    _LockedConnection,
    _restrict_database_files,
    connect,
    database_lock,
    is_database_deleted,
    read_restore_epoch,
    record_restore_epoch,
    recorded_restore_epoch,
    transaction,
    update_tombstone_marker,
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
from .secrets import clear_auth_registry_key_cache, installation_key, installation_key_path
from .security import safe_error_message
from .tenancy import (
    ProfileDeletedError,
    allow_restore_database_access,
    current_profile_id,
    profile_database_path,
    profile_scope,
    unbind_restore_epoch,
)

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
BACKUP_INITIAL_DELAY_SECONDS = 5
BACKUP_POLL_SECONDS = 60
_AUTH_BACKUP_LOCK = threading.RLock()
INCREMENTAL_VACUUM_STEP_PAGES = 4_096
MIN_VACUUM_FREE_SPACE_BYTES = 16 * 1024 * 1024
RESTORE_FENCE_DETAIL = (
    "Profile database is unavailable because an interrupted restore has not been "
    "recovered. Restore a valid backup or ask an administrator to clear the "
    "restore journal."
)
_RESTORE_JOURNAL_ERRORS: dict[str, str] = {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_hex(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _lockout_notice_username_sha256(key: object) -> str:
    raw = str(key)
    if not raw.startswith("lockout_notice:"):
        return ""
    rest = raw[len("lockout_notice:") :]
    length_str, sep, remainder = rest.partition(":")
    if not sep or not length_str.isdigit():
        return ""
    length = int(length_str)
    if len(remainder) <= length or remainder[length] != ":":
        return ""
    return _sha256_hex(remainder[:length])


class BackupError(ValueError):
    """A backup or restore could not be completed safely."""


class RecoveryHoldError(BackupError):
    """Clearing or overriding an active recovery hold requires confirmation."""


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


def _recovery_hold_value() -> str | bool | None:
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
    except (sqlite3.Error, OSError):
        LOGGER.warning(
            "Could not read profile recovery hold status; failing closed",
            exc_info=True,
        )
        return True


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


def _auth_recovery_hold_value() -> str | bool | None:
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
    except (sqlite3.Error, OSError):
        LOGGER.warning(
            "Could not read identity-registry recovery hold status; failing closed",
            exc_info=True,
        )
        return True
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
    with _AUTH_BACKUP_LOCK, database_lock(path):
        if not path.is_file():
            raise BackupError("The Bookward identity registry is not available.")
        con = sqlite3.connect(path, timeout=30)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM auth_meta WHERE key=?", (AUTH_BACKUP_HOLD_KEY,))
            con.commit()
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()
    return identity_backup_status()


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size if not path.is_symlink() and path.is_file() else 0
    except OSError:
        return 0


def database_storage_metrics(
    backups: list[dict] | None = None,
    *,
    skip_database: bool = False,
) -> dict:
    db_path = profile_database_path()
    if is_database_deleted(db_path):
        raise ProfileDeletedError("Profile has been deleted")
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
    if skip_database or not db_path.is_file() or _has_restore_journal():
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
    except (sqlite3.Error, OSError):
        return default_metrics


def backup_status() -> dict:
    backups = list_backups()
    last_error = None
    hold_value = None
    hold_active = False
    status_read_failed = False
    journal = _restore_journal_path()
    fenced = _has_restore_journal(journal)
    if fenced:
        last_error = _RESTORE_JOURNAL_ERRORS.get(str(journal)) or RESTORE_FENCE_DETAIL
    else:
        path = profile_database_path()
        try:
            if path.is_file():
                with connect() as con:
                    stored = dict(
                        con.execute(
                            "SELECT key,value FROM settings WHERE key IN (?,?)",
                            (LAST_ERROR_KEY, RECOVERY_HOLD_KEY),
                        ).fetchall()
                    )
                    last_error = stored.get(LAST_ERROR_KEY) or None
                    hold_value = (stored.get(RECOVERY_HOLD_KEY) or "").strip() or None
                    hold_active = bool(hold_value)
        except (sqlite3.Error, OSError):
            last_error = "Backup status is temporarily unavailable."
            hold_active = True
            status_read_failed = True
    storage = database_storage_metrics(
        backups, skip_database=fenced or status_read_failed
    )
    interval = backup_interval_hours()
    last_success = backups[0]["created_at"] if backups else None
    next_backup = None
    if interval and last_success and not hold_active and not fenced:
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
        **storage,
    }


def maintenance_is_due(now: datetime | None = None) -> bool:
    db_path = profile_database_path()
    if is_database_deleted(db_path):
        raise ProfileDeletedError("Profile has been deleted")
    if not db_path.is_file() or _has_restore_journal():
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    try:
        with connect() as con:
            hold_row = con.execute(
                "SELECT value FROM settings WHERE key=?",
                (RECOVERY_HOLD_KEY,),
            ).fetchone()
            if hold_row and (hold_row["value"] or "").strip():
                return False
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
    except (sqlite3.Error, OSError, ValueError):
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
    hold_read_failed = False
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
    except (sqlite3.Error, OSError):
        state[AUTH_BACKUP_ERROR_KEY] = "Identity backup status is temporarily unavailable."
        hold_read_failed = True

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
        "recovery_hold": hold_read_failed or bool(hold_value),
        "recovery_hold_at": hold_value,
        "last_success_at": state.get(AUTH_BACKUP_SUCCESS_KEY)
        or (backups[0]["created_at"] if backups else None),
        "last_attempt_at": state.get(AUTH_BACKUP_ATTEMPT_KEY),
        "last_restore_at": state.get(AUTH_BACKUP_RESTORE_KEY),
        "last_error": state.get(AUTH_BACKUP_ERROR_KEY) or None,
        "backups": backups,
    }


def _pending_auth_scrubs_path() -> Path:
    from .profiles import auth_path

    registry = auth_path()
    return registry.with_name(f".{registry.name}.scrub-pending.json")


def _load_pending_auth_backup_scrubs_unlocked() -> list[dict[str, str]]:
    by_account: dict[str, dict[str, str]] = {}
    journal = _pending_auth_scrubs_path()
    if journal.is_file() and not journal.is_symlink():
        try:
            raw = json.loads(journal.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    acct = str(item.get("account_id") or "").strip()
                    prof = str(item.get("profile_id") or "").strip()
                    uhash = str(item.get("username_sha256") or "").strip()
                    if acct and prof:
                        by_account[acct] = {
                            "account_id": acct,
                            "profile_id": prof,
                            "username_sha256": uhash,
                        }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            LOGGER.warning("Could not read pending auth-backup scrub journal")

    profiles_dir = Path(settings.db).parent / "profiles"
    if profiles_dir.is_dir() and not profiles_dir.is_symlink():
        try:
            for marker in profiles_dir.iterdir():
                if (
                    marker.is_symlink()
                    or not marker.is_file()
                    or not marker.name.endswith(f".sqlite3{TOMBSTONE_SUFFIX}")
                ):
                    continue
                try:
                    payload = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict) or not payload.get("scrub_pending"):
                    continue
                acct = str(payload.get("account_id") or "").strip()
                prof = str(payload.get("profile_id") or "").strip()
                uhash = str(payload.get("username_sha256") or "").strip()
                if acct and prof and acct not in by_account:
                    by_account[acct] = {
                        "account_id": acct,
                        "profile_id": prof,
                        "username_sha256": uhash,
                    }
        except OSError:
            pass
    return list(by_account.values())


def _save_pending_auth_backup_scrubs_unlocked(entries: list[dict[str, str]]) -> None:
    journal = _pending_auth_scrubs_path()
    if not entries:
        journal.unlink(missing_ok=True)
        return
    journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=".bookward-auth-scrub-pending-", suffix=".tmp", dir=journal.parent
    )
    os.fchmod(descriptor, 0o600)
    temporary = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(entries, sort_keys=True).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, journal)
        os.chmod(journal, 0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def has_pending_auth_backup_scrubs() -> bool:
    with _AUTH_BACKUP_LOCK:
        return bool(_load_pending_auth_backup_scrubs_unlocked())


def pending_auth_backup_scrub_account_ids() -> set[str]:
    with _AUTH_BACKUP_LOCK:
        return {
            item["account_id"]
            for item in _load_pending_auth_backup_scrubs_unlocked()
        }


def record_pending_auth_backup_scrub(
    account_id: str, profile_id: str, normalized_username: str
) -> str:
    username_sha256 = _sha256_hex(normalized_username)
    with _AUTH_BACKUP_LOCK:
        entries = [
            item
            for item in _load_pending_auth_backup_scrubs_unlocked()
            if item["account_id"] != account_id
        ]
        entries.append(
            {
                "account_id": account_id,
                "profile_id": profile_id,
                "username_sha256": username_sha256,
            }
        )
        _save_pending_auth_backup_scrubs_unlocked(entries)
        if profile_id != "legacy":
            update_tombstone_marker(
                profile_id,
                scrub_pending=True,
                account_id=account_id,
                username_sha256=username_sha256,
            )
    return username_sha256


def complete_auth_backup_scrub(
    account_id: str,
    profile_id: str,
    normalized_username: str = "",
    *,
    username_sha256: str = "",
    create_snapshot: bool = True,
) -> None:
    with _AUTH_BACKUP_LOCK:
        scrub_account_from_auth_backups(
            account_id,
            profile_id,
            normalized_username,
            username_sha256=username_sha256,
        )
        if create_snapshot and not auth_recovery_hold_active():
            _create_auth_backup_unlocked(prune=True)
        remaining = [
            item
            for item in _load_pending_auth_backup_scrubs_unlocked()
            if item["account_id"] != account_id
        ]
        _save_pending_auth_backup_scrubs_unlocked(remaining)
        if profile_id != "legacy":
            update_tombstone_marker(profile_id, scrub_pending=False)


def retry_pending_auth_backup_scrubs(*, create_snapshot: bool = True) -> int:
    from .profiles import auth_path

    with _AUTH_BACKUP_LOCK:
        entries = _load_pending_auth_backup_scrubs_unlocked()
        if not entries:
            return 0
        live_account_ids: set[str] = set()
        registry = auth_path()
        if registry.is_file():
            con = sqlite3.connect(
                registry.resolve().as_uri() + "?mode=ro", uri=True, timeout=30
            )
            try:
                placeholders = ",".join("?" for _ in entries)
                live_account_ids = {
                    str(row[0])
                    for row in con.execute(
                        f"SELECT id FROM accounts WHERE id IN ({placeholders})",
                        [item["account_id"] for item in entries],
                    )
                }
            except sqlite3.Error:
                pass
            finally:
                con.close()
        ready_entries = [
            item for item in entries if item["account_id"] not in live_account_ids
        ]
        still_pending = [
            item for item in entries if item["account_id"] in live_account_ids
        ]
        if not ready_entries:
            return 0
        try:
            for item in ready_entries:
                scrub_account_from_auth_backups(
                    item["account_id"],
                    item["profile_id"],
                    username_sha256=item.get("username_sha256", ""),
                )
            if create_snapshot and not still_pending and not auth_recovery_hold_active():
                _create_auth_backup_unlocked(prune=True)
        except (BackupError, OSError) as exc:
            _record_auth_backup_state(error=exc)
            raise
        _save_pending_auth_backup_scrubs_unlocked(still_pending)
        for item in ready_entries:
            if item["profile_id"] != "legacy":
                update_tombstone_marker(item["profile_id"], scrub_pending=False)
        return len(ready_entries)


def create_auth_backup(*, prune: bool = True) -> dict:
    """Snapshot the account/profile registry alongside personal database backups."""
    with _AUTH_BACKUP_LOCK:
        if _load_pending_auth_backup_scrubs_unlocked():
            retry_pending_auth_backup_scrubs(create_snapshot=False)
        return _create_auth_backup_unlocked(prune=prune)


def _create_auth_backup_unlocked(*, prune: bool = True) -> dict:
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


def scrub_account_from_auth_backups(
    account_id: str,
    profile_id: str,
    normalized_username: str = "",
    *,
    username_sha256: str = "",
) -> int:
    """Remove an erased account's rows and vacuum older auth-registry snapshots."""

    target_hash = username_sha256 or (
        _sha256_hex(normalized_username) if normalized_username else ""
    )
    with _AUTH_BACKUP_LOCK:
        directory = auth_backup_directory()
        if directory.is_symlink():
            raise BackupError("The identity-registry backup directory is invalid.")
        if not directory.exists():
            return 0
        _ensure_private_directory(directory)
        entries = list(directory.iterdir())

        scrubbed = 0
        deferred_error: Exception | None = None
        for snapshot_path in entries:
            if (
                snapshot_path.is_symlink()
                or not snapshot_path.is_file()
                or snapshot_path.suffix != ".sqlite3"
                or not AUTH_BACKUP_ID.fullmatch(snapshot_path.stem)
            ):
                continue
            staged = None
            con = None
            vacuum_con = None
            try:
                orig_stat = snapshot_path.stat()
                descriptor, temp_name = tempfile.mkstemp(
                    prefix=".bookward-auth-scrub-", suffix=".tmp", dir=directory
                )
                os.fchmod(descriptor, 0o600)
                os.close(descriptor)
                staged = Path(temp_name)
                shutil.copyfile(snapshot_path, staged)
                os.chmod(staged, 0o600)

                con = sqlite3.connect(staged, timeout=30)
                con.create_function("sha256_hex", 1, _sha256_hex, deterministic=True)
                con.create_function(
                    "lockout_username_sha256",
                    1,
                    _lockout_notice_username_sha256,
                    deterministic=True,
                )
                con.execute("PRAGMA foreign_keys=ON")
                con.execute("PRAGMA busy_timeout=5000")
                con.execute("PRAGMA secure_delete=ON")
                tables = {
                    item[0]
                    for item in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                changed = 0
                con.execute("BEGIN IMMEDIATE")
                if "login_attempts" in tables:
                    if normalized_username:
                        changed += max(
                            0,
                            con.execute(
                                "DELETE FROM login_attempts WHERE normalized_username=?",
                                (normalized_username,),
                            ).rowcount,
                        )
                    elif target_hash:
                        changed += max(
                            0,
                            con.execute(
                                "DELETE FROM login_attempts WHERE sha256_hex(normalized_username)=?",
                                (target_hash,),
                            ).rowcount,
                        )
                if "oidc_identities" in tables:
                    changed += max(
                        0,
                        con.execute(
                            "DELETE FROM oidc_identities WHERE account_id=?",
                            (account_id,),
                        ).rowcount,
                    )
                if "auth_sessions" in tables:
                    changed += max(
                        0,
                        con.execute(
                            "DELETE FROM auth_sessions WHERE account_id=?",
                            (account_id,),
                        ).rowcount,
                    )
                if "auth_transactions" in tables:
                    changed += max(
                        0,
                        con.execute(
                            "DELETE FROM auth_transactions WHERE account_id=?",
                            (account_id,),
                        ).rowcount,
                    )
                if "accounts" in tables:
                    changed += max(
                        0,
                        con.execute(
                            "DELETE FROM accounts WHERE id=?",
                            (account_id,),
                        ).rowcount,
                    )
                if "auth_meta" in tables:
                    changed += max(
                        0,
                        con.execute(
                            "DELETE FROM auth_meta WHERE key=?",
                            (f"erasure_pending:{account_id}",),
                        ).rowcount,
                    )
                    if normalized_username:
                        notice_prefix = (
                            f"lockout_notice:{len(normalized_username)}:{normalized_username}:"
                        )
                        notice_end = (
                            f"lockout_notice:{len(normalized_username)}:{normalized_username};"
                        )
                        changed += max(
                            0,
                            con.execute(
                                "DELETE FROM auth_meta WHERE key>=? AND key<?",
                                (notice_prefix, notice_end),
                            ).rowcount,
                        )
                    elif target_hash:
                        changed += max(
                            0,
                            con.execute(
                                "DELETE FROM auth_meta WHERE key>='lockout_notice:' AND key<'lockout_notice;' "
                                "AND lockout_username_sha256(key)=?",
                                (target_hash,),
                            ).rowcount,
                        )
                if "api_tokens" in tables:
                    changed += max(
                        0,
                        con.execute(
                            "DELETE FROM api_tokens WHERE profile_id=?",
                            (profile_id,),
                        ).rowcount,
                    )
                if "profiles" in tables:
                    if profile_id != "legacy":
                        has_other_account = (
                            "accounts" in tables
                            and con.execute(
                                "SELECT 1 FROM accounts WHERE profile_id=? LIMIT 1",
                                (profile_id,),
                            ).fetchone()
                            is not None
                        )
                        if not has_other_account:
                            changed += max(
                                0,
                                con.execute(
                                    "DELETE FROM profiles WHERE id=?",
                                    (profile_id,),
                                ).rowcount,
                            )
                    else:
                        has_legacy_account = (
                            "accounts" in tables
                            and con.execute(
                                "SELECT 1 FROM accounts WHERE profile_id='legacy' LIMIT 1"
                            ).fetchone()
                            is not None
                        )
                        if not has_legacy_account:
                            row_state = con.execute(
                                "SELECT status,display_name FROM profiles WHERE id='legacy'"
                            ).fetchone()
                            if row_state and (
                                row_state[0] != "disabled"
                                or row_state[1] != "My profile"
                            ):
                                changed += max(
                                    0,
                                    con.execute(
                                        "UPDATE profiles SET status='disabled',display_name='My profile' WHERE id='legacy'"
                                    ).rowcount,
                                )
                con.commit()
                con.close()
                con = None

                if changed > 0:
                    vacuum_con = sqlite3.connect(staged, timeout=30, isolation_level=None)
                    vacuum_con.execute("PRAGMA busy_timeout=5000")
                    vacuum_con.execute("VACUUM")
                    vacuum_con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    integrity = vacuum_con.execute("PRAGMA integrity_check").fetchone()
                    if (
                        not integrity
                        or integrity[0] != "ok"
                        or vacuum_con.execute("PRAGMA foreign_key_check").fetchone()
                    ):
                        raise BackupError("Scrubbed identity-registry snapshot failed integrity check.")
                    vacuum_con.close()
                    vacuum_con = None
                    with staged.open("rb") as handle:
                        os.fsync(handle.fileno())
                    os.chmod(staged, 0o600)
                    os.utime(staged, ns=(orig_stat.st_atime_ns, orig_stat.st_mtime_ns))
                    os.replace(staged, snapshot_path)
                    staged = None
                    scrubbed += 1
            except Exception as exc:
                LOGGER.warning(
                    "Removing unreadable identity-registry snapshot %s during account erasure",
                    snapshot_path.name,
                )
                try:
                    snapshot_path.unlink(missing_ok=True)
                    _remove_sidecars(snapshot_path)
                except OSError as unlink_exc:
                    LOGGER.exception(
                        "Could not remove damaged identity-registry snapshot %s",
                        snapshot_path.name,
                    )
                    if deferred_error is None:
                        deferred_error = unlink_exc
                else:
                    # Only fail the scrub if snapshot_path still exists (e.g. a
                    # transient copy/chmod/replace error where unlink was mocked or blocked).
                    if snapshot_path.exists() and deferred_error is None:
                        deferred_error = exc
            finally:
                if con is not None:
                    con.close()
                if vacuum_con is not None:
                    vacuum_con.close()
                if staged is not None:
                    staged.unlink(missing_ok=True)
                    _remove_sidecars(staged)

        if scrubbed > 0:
            try:
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        if deferred_error is not None:
            raise BackupError("Could not scrub all identity-registry snapshots.") from deferred_error
        return scrubbed


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
    from .profiles import invalidate_auth_caches

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
    invalidate_auth_caches(clear_all=True)
    clear_auth_registry_key_cache()
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
    with _AUTH_BACKUP_LOCK:
        return _restore_auth_backup_unlocked(backup_id)


def _restore_auth_backup_unlocked(backup_id: str) -> dict:
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
                "DELETE FROM auth_meta WHERE key IN ('setup_token_hash', ?) OR (key>='lockout_notice:' AND key<'lockout_notice;')",
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


def _prune_backups(
    *,
    preserve_ids: set[str] | None = None,
    expire_before: datetime | None = None,
) -> None:
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
    if preserve_ids:
        protected_ids.update(preserve_ids)
    retained = list_backups()
    keep = []
    expired = []
    retention = backup_retention_count()
    for index, item in enumerate(retained):
        if item["id"] in protected_ids:
            keep.append(item)
            continue
        created_at = datetime.fromisoformat(item["created_at"])
        if index >= retention or (
            expire_before is not None and created_at <= expire_before
        ):
            expired.append(item)
        else:
            keep.append(item)
    retained_ids = {item["id"] for item in keep} | protected_ids
    for old in expired:
        if old["id"] in retained_ids:
            continue
        try:
            path = _backup_path(old["id"])
            path.unlink()
            _key_sidecar(path).unlink(missing_ok=True)
        except (OSError, BackupError):
            LOGGER.warning("Could not remove an expired Bookward backup")
    directory = backup_directory()
    if not directory.is_dir():
        return
    for sidecar in directory.glob("bookward-*.secret.key"):
        backup_id = sidecar.name.removesuffix(".secret.key")
        if backup_id not in retained_ids and not sidecar.is_symlink():
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("Could not remove an orphaned Bookward key recovery file")


def purge_profile_backups(profile_id: str | None = None) -> int:
    """Remove all snapshots and generated-key sidecars for one profile."""

    configured = str(settings.backup_dir or "").strip()
    base = Path(configured) if configured else Path(settings.db).parent / "backups"
    selected = profile_id if profile_id is not None else (current_profile_id() or "legacy")
    removed = 0
    first_error: OSError | None = None
    if selected == "legacy":
        if not base.is_dir() or base.is_symlink():
            return 0
        for entry in list(base.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                continue
            name = entry.name
            if (
                (name.startswith("bookward-") and not name.startswith("bookward-auth-"))
                or name.startswith(".bookward-backup-")
                or name.startswith(".bookward-key-")
            ):
                try:
                    entry.unlink(missing_ok=True)
                    removed += 1
                except OSError as exc:
                    LOGGER.warning("Could not remove a legacy backup file")
                    if first_error is None:
                        first_error = exc
        if first_error is not None:
            raise first_error
        return removed

    if not re.fullmatch(r"[0-9a-f]{32}", selected):
        raise ValueError("Invalid profile identifier")
    directory = base / selected
    if directory.is_symlink():
        directory.unlink(missing_ok=True)
        return 1
    if not directory.is_dir():
        return 0
    try:
        if directory.resolve(strict=True).parent != base.resolve(strict=True):
            raise BackupError("Profile backup directory resolved outside backup root.")
    except FileNotFoundError:
        return 0
    for entry in list(directory.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            try:
                shutil.rmtree(entry)
            except OSError as exc:
                LOGGER.warning("Could not remove a profile backup subdirectory")
                if first_error is None:
                    first_error = exc
        else:
            try:
                entry.unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                LOGGER.warning("Could not remove a profile backup file")
                if first_error is None:
                    first_error = exc
    if first_error is not None:
        raise first_error
    try:
        directory.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        shutil.rmtree(directory)
    return removed


def _reclaim_incremental_pages_step(
    live_path: Path,
    *,
    max_pages: int = INCREMENTAL_VACUUM_STEP_PAGES,
) -> int:
    """Reclaim up to `max_pages` free pages online without exclusive `database_lock`."""

    if is_database_deleted(live_path):
        raise ProfileDeletedError("Profile has been deleted")
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
        if not is_database_deleted(live_path):
            _restrict_database_files()


def _checkpoint_wal_unlocked(live_path: Path, *, mode: str = "PASSIVE") -> None:
    if is_database_deleted(live_path):
        raise ProfileDeletedError("Profile has been deleted")
    if not live_path.is_file() or live_path.is_symlink() or _has_restore_journal():
        return
    checkpoint_mode = mode if mode in {"PASSIVE", "TRUNCATE"} else "PASSIVE"
    raw = None
    try:
        raw = sqlite3.connect(
            live_path,
            timeout=30,
            isolation_level=None,
            factory=_LockedConnection,
        )
        raw.execute(f"PRAGMA wal_checkpoint({checkpoint_mode})").fetchone()
    finally:
        if raw is not None:
            raw.close()
        if not is_database_deleted(live_path):
            _restrict_database_files()


def _run_maintenance_if_initialized(*, now: datetime | None = None) -> dict | None:
    live_path = profile_database_path()
    if is_database_deleted(live_path):
        raise ProfileDeletedError("Profile has been deleted")
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

    live_path = profile_database_path()
    if is_database_deleted(live_path):
        raise ProfileDeletedError("Profile has been deleted")
    if _has_restore_journal():
        return {}
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
        if is_database_deleted(live_path):
            raise ProfileDeletedError("Profile has been deleted")
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
    except ProfileDeletedError:
        raise
    except BackupError:
        raise
    except Exception as exc:
        detail = safe_error_message(exc, limit=200)
        LOGGER.exception("Bookward storage compaction failed")
        raise BackupError(f"Storage compaction failed: {detail}") from exc


def create_backup(*, confirm_clear_hold: bool = False) -> dict:
    try:
        with database_lock(profile_database_path()):
            if _has_restore_journal():
                raise BackupError(
                    "Backup cannot run while an interrupted restore is awaiting recovery."
                )
            hold_was_active = recovery_hold_active()
            if hold_was_active and not confirm_clear_hold:
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
            if not hold_was_active:
                _prune_backups()
        return _backup_record(path)
    except (RecoveryHoldError, ProfileDeletedError):
        raise
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


def _restore_journal_path(target: str | Path | None = None) -> Path:
    if target is None:
        path = profile_database_path()
    elif isinstance(target, Path):
        path = target
    else:
        path = profile_database_path(target)
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
    except ProfileDeletedError:
        raise
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
        if key_mutated:
            clear_auth_registry_key_cache()
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
                        con.execute(
                            "DELETE FROM settings WHERE key IN (?,?)",
                            (LAST_ERROR_KEY, RECOVERY_HOLD_KEY),
                        )
                    _clear_restore_journal()
                replaced = False
                _prune_backups(preserve_ids={backup_id, safety_backup.stem})
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
    except ProfileDeletedError:
        raise
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
    scrubbed_pending = False
    if has_pending_auth_backup_scrubs():
        retry_pending_auth_backup_scrubs()
        scrubbed_pending = True
    if backup_interval_hours() <= 0:
        return None
    auth_hold = _auth_recovery_hold_value()
    if isinstance(auth_hold, str):
        LOGGER.warning(
            "Scheduled identity-registry backup skipped while recovery hold is active; "
            "new account-registry changes are not backed up until a snapshot is restored or the hold is dismissed"
        )
        return None
    if auth_hold is True:
        LOGGER.warning(
            "Scheduled identity-registry backup skipped because recovery hold status could not be read"
        )
        return None
    if scrubbed_pending:
        return None
    if auth_backup_is_due():
        return create_auth_backup()
    return None


def _run_scheduled_profile_backup(profile_id: str) -> dict | None:
    with profile_scope(profile_id):
        if _has_restore_journal() or database_lock().is_locked():
            return None
        interval_enabled = backup_interval_hours() > 0
        profile_hold = _recovery_hold_value() if interval_enabled else None
        if isinstance(profile_hold, str):
            LOGGER.warning(
                "Scheduled Bookward backup skipped for profile %s while recovery hold is active; "
                "new profile data is not backed up until a snapshot is restored or the hold is dismissed",
                profile_id,
            )
            return None
        if profile_hold is True:
            LOGGER.warning(
                "Scheduled Bookward backup skipped for profile %s because recovery hold status could not be read",
                profile_id,
            )
            return None
        try:
            if maintenance_is_due():
                run_scheduled_maintenance()
        except ProfileDeletedError:
            raise
        except Exception:
            LOGGER.exception(
                "Scheduled Bookward storage maintenance failed for profile %s", profile_id
            )
        if not interval_enabled or not backup_is_due():
            return None
        return create_backup()


async def backup_scheduler_loop(stop: asyncio.Event) -> None:
    """Create hourly-polled backups without depending on host cron."""

    try:
        await asyncio.wait_for(stop.wait(), timeout=BACKUP_INITIAL_DELAY_SECONDS)
    except TimeoutError:
        pass
    while not stop.is_set():
        from .profiles import profile_ids, recover_incomplete_account_erasures

        try:
            await asyncio.to_thread(recover_incomplete_account_erasures)
        except Exception:
            LOGGER.exception("Scheduled completion of tombstoned account erasure failed")
        try:
            await asyncio.to_thread(_run_scheduled_auth_backup)
        except Exception:
            LOGGER.exception("Scheduled identity-registry backup failed")
        try:
            active_profiles = await asyncio.to_thread(profile_ids)
        except Exception:
            LOGGER.exception("Could not enumerate profiles for scheduled backups")
            active_profiles = []
        active_set = set(active_profiles)
        for profile_id in active_profiles:
            if stop.is_set():
                break
            try:
                await asyncio.to_thread(_run_scheduled_profile_backup, profile_id)
            except ProfileDeletedError:
                continue
            except Exception:
                LOGGER.exception("Scheduled Bookward backup failed for profile %s", profile_id)
        try:
            all_profiles = await asyncio.to_thread(profile_ids, False)
        except Exception:
            LOGGER.exception("Could not enumerate disabled profiles for backup pruning")
            all_profiles = []
        disabled_profiles = [
            profile_id
            for profile_id in all_profiles
            if profile_id not in active_set
        ]
        interval = backup_interval_hours()
        expire_before = (
            _utc_now()
            - timedelta(hours=interval * backup_retention_count())
            if interval > 0
            else None
        )
        for profile_id in disabled_profiles:
            try:
                with profile_scope(profile_id):
                    if not backup_directory().is_dir():
                        continue

                    def _prune_disabled():
                        db_path = profile_database_path()
                        with database_lock(db_path):
                            try:
                                db_missing = (
                                    not db_path.is_file() or db_path.stat().st_size == 0
                                )
                            except OSError:
                                LOGGER.warning(
                                    "Scheduled Bookward backup pruning skipped for disabled profile %s "
                                    "because profile database status could not be read",
                                    profile_id,
                                )
                                return
                            if db_missing:
                                if list_backups():
                                    LOGGER.warning(
                                        "Scheduled Bookward backup pruning skipped for disabled profile %s "
                                        "while profile database is missing and surviving snapshots exist",
                                        profile_id,
                                    )
                                return
                            if recovery_hold_active():
                                return
                            _prune_backups(expire_before=expire_before)

                    await asyncio.to_thread(_prune_disabled)
            except ProfileDeletedError:
                continue
            except Exception:
                LOGGER.exception(
                    "Scheduled Bookward backup pruning failed for disabled profile %s",
                    profile_id,
                )
        try:
            await asyncio.wait_for(stop.wait(), timeout=BACKUP_POLL_SECONDS)
        except TimeoutError:
            pass
