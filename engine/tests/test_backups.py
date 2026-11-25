import asyncio
import logging
import sqlite3
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from auth_helpers import authenticated_headers
from cryptography.fernet import Fernet

from afterword_engine import backups, database as db_module, profiles, scoring
from afterword_engine.config import settings
from afterword_engine.database import (
    MIGRATIONS,
    RestoreEpochError,
    bind_restore_epoch,
    connect,
    current_restore_epoch,
    database_lock,
    initialize,
    private_setting,
    row,
    rows,
    transaction,
)
from afterword_engine.embeddings import content_hash, vector_blob
from afterword_engine.jobs import enqueue_job, worker_loop
from afterword_engine.main import app, handle_job, lifespan, queue_goodreads_rss_if_due
from afterword_engine.secrets import installation_key_path, seal
from afterword_engine.tenancy import profile_database_path, profile_scope


@pytest.fixture()
def database(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "afterword.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.setattr(settings, "backup_interval_hours", 24)
    monkeypatch.setattr(settings, "backup_retention_count", 7)
    initialize()
    return tmp_path


def test_backup_is_wal_safe_private_and_excludes_the_key(database):
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("WAL snapshot", "Reader", "test"),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("backup_test_secret", seal("keep this encrypted")),
        )

    wal_connection = sqlite3.connect(settings.db)
    wal_connection.execute("PRAGMA journal_mode=WAL")
    wal_connection.execute(
        "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
        ("Committed in WAL", "Reader", "test"),
    )
    wal_connection.commit()
    assert Path(f"{settings.db}-wal").exists()
    try:
        backup = backups.create_backup()
    finally:
        wal_connection.close()
    snapshot = database / "backups" / f"{backup['id']}.sqlite3"
    assert snapshot.is_file()
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert stat.S_IMODE(snapshot.parent.stat().st_mode) == 0o700
    assert not (database / "backups" / "secret.key").exists()
    key_sidecar = snapshot.with_name(f"{backup['id']}.secret.key")
    assert key_sidecar.is_file()
    assert stat.S_IMODE(key_sidecar.stat().st_mode) == 0o600
    with sqlite3.connect(snapshot) as con:
        assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert con.execute("SELECT title FROM reads WHERE title='WAL snapshot'").fetchone() == ("WAL snapshot",)
        assert con.execute("SELECT title FROM reads WHERE title='Committed in WAL'").fetchone() == ("Committed in WAL",)
        encrypted = con.execute(
            "SELECT value FROM settings WHERE key='backup_test_secret'"
        ).fetchone()[0]
        assert encrypted.startswith("fernet:")
        assert "keep this encrypted" not in encrypted

    assert private_setting("backup_test_secret") == "keep this encrypted"


def test_backup_failure_keeps_a_specific_safe_diagnostic(database, monkeypatch):
    monkeypatch.setattr(
        backups,
        "_create_snapshot_unlocked",
        lambda: (_ for _ in ()).throw(OSError("backup disk is full")),
    )

    with pytest.raises(backups.BackupError, match="backup disk is full"):
        backups.create_backup()

    assert backups.backup_status()["last_error"] == "Backup failed: backup disk is full"


def test_restore_makes_a_safety_snapshot_and_recovers_all_rows(database):
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("Saved before restore", "Reader", "test"),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("restore_test_secret", seal("same installation key")),
        )
    selected = backups.create_backup()

    # Simulate losing the application-data volume while retaining the separate
    # backup volume. A new install gets a different generated Fernet key.
    Path(settings.db).unlink()
    Path(f"{settings.db}-wal").unlink(missing_ok=True)
    Path(f"{settings.db}-shm").unlink(missing_ok=True)
    (database / "secret.key").unlink()
    initialize()
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("New installation", "Reader", "test"),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("fresh_install_secret", seal("new installation key")),
        )

    result = backups.restore_backup(selected["id"])

    assert result["restored_backup"] == selected["id"]
    assert result["safety_backup"]["id"] != selected["id"]
    assert row("SELECT 1 FROM reads WHERE title='Saved before restore'") is not None
    assert row("SELECT 1 FROM reads WHERE title='New installation'") is None
    assert private_setting("restore_test_secret") == "same installation key"
    with sqlite3.connect(database / "backups" / f"{result['safety_backup']['id']}.sqlite3") as con:
        assert con.execute(
            "SELECT 1 FROM reads WHERE title='New installation'"
        ).fetchone() == (1,)


def test_restore_rejects_a_non_bookward_file_without_changing_live_data(database, caplog):
    (database / "backups").mkdir(exist_ok=True)
    invalid_id = "bookward-20260101T000000Z-deadbeef"
    invalid = database / "backups" / f"{invalid_id}.sqlite3"
    with sqlite3.connect(invalid) as con:
        con.execute("CREATE TABLE unrelated (value TEXT)")
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("Keep me", "Reader", "test"),
        )

    with caplog.at_level(logging.ERROR, logger="afterword_engine.backups"):
        with pytest.raises(backups.BackupError, match="not a Bookward database backup") as exc_info:
            backups.restore_backup(invalid_id)

    assert exc_info.value.__cause__ is None
    assert caplog.records == []
    assert backups.backup_status()["last_error"] == (
        "Restore failed; the previous database remains active. "
        "This file is not a Bookward database backup."
    )
    assert row("SELECT 1 FROM reads WHERE title='Keep me'") is not None


def test_backup_id_traversal_cannot_select_a_file_outside_backup_directory(database):
    outside = database / "outside.sqlite3"
    outside.write_bytes(b"do not open")

    with pytest.raises(backups.BackupError, match="valid Bookward backup"):
        backups._backup_path("../outside")

    assert outside.read_bytes() == b"do not open"


def test_restore_rejects_symlinked_snapshot_and_key_files(database):
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("Preserve live state", "Reader", "test"),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("symlink_test_secret", seal("expected key")),
        )
    backup = backups.create_backup()
    snapshot = database / "backups" / f"{backup['id']}.sqlite3"
    key_sidecar = snapshot.with_name(f"{backup['id']}.secret.key")

    outside_snapshot = database / "outside.sqlite3"
    outside_snapshot.write_bytes(snapshot.read_bytes())
    snapshot.unlink()
    snapshot.symlink_to(outside_snapshot)
    assert backup["id"] not in {item["id"] for item in backups.list_backups()}
    with pytest.raises(backups.BackupError, match="no longer available"):
        backups.restore_backup(backup["id"])

    snapshot.unlink()
    snapshot.write_bytes(outside_snapshot.read_bytes())
    snapshot.chmod(0o600)
    outside_key = database / "outside.key"
    original_key = key_sidecar.read_bytes()
    outside_key.write_bytes(original_key)
    key_sidecar.unlink()
    key_sidecar.symlink_to(outside_key)
    with pytest.raises(backups.BackupError, match="no generated encryption key"):
        backups.restore_backup(backup["id"])

    assert outside_key.read_bytes() == original_key
    assert row("SELECT 1 FROM reads WHERE title='Preserve live state'") is not None
    assert private_setting("symlink_test_secret") == "expected key"


def test_restore_rejects_a_mismatched_generated_key_before_replacement(database):
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("Live data", "Reader", "test"),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("key_check_secret", seal("expected key")),
        )
    backup = backups.create_backup()
    snapshot = database / "backups" / f"{backup['id']}.sqlite3"
    key_sidecar = snapshot.with_name(f"{backup['id']}.secret.key")
    original_key = key_sidecar.read_bytes()
    key_sidecar.write_bytes(Fernet.generate_key())
    key_sidecar.chmod(0o600)

    with pytest.raises(backups.BackupError, match="does not match"):
        backups.restore_backup(backup["id"])

    assert row("SELECT 1 FROM reads WHERE title='Live data'") is not None
    key_sidecar.write_bytes(original_key)


def _prepare_distinct_backup_and_live_state(database: Path) -> dict:
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("Selected snapshot", "Reader", "test"),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("selected_secret", seal("selected key")),
        )
    selected = backups.create_backup()

    Path(settings.db).unlink()
    Path(f"{settings.db}-wal").unlink(missing_ok=True)
    Path(f"{settings.db}-shm").unlink(missing_ok=True)
    (database / "secret.key").unlink()
    initialize()
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("Live before restore", "Reader", "test"),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("live_secret", seal("live key")),
        )
    return selected


def test_restore_rolls_back_snapshot_and_key_on_post_swap_backup_error(
    database, monkeypatch, caplog
):
    selected = _prepare_distinct_backup_and_live_state(database)
    real_clear_journal = backups._clear_restore_journal
    primary_error = backups.BackupError("post-swap check failed")
    calls = 0

    def fail_once_after_swap():
        nonlocal calls
        calls += 1
        if calls == 1:
            assert row("SELECT 1 FROM reads WHERE title='Selected snapshot'") is not None
            assert private_setting("selected_secret") == "selected key"
            raise primary_error
        real_clear_journal()

    monkeypatch.setattr(backups, "_clear_restore_journal", fail_once_after_swap)

    with caplog.at_level(logging.ERROR, logger="afterword_engine.backups"):
        with pytest.raises(backups.BackupError) as exc_info:
            backups.restore_backup(selected["id"])

    assert exc_info.value is primary_error
    assert exc_info.value.__cause__ is None
    assert caplog.records == []
    assert not backups._restore_journal_path().exists()
    assert row("SELECT 1 FROM reads WHERE title='Live before restore'") is not None
    assert row("SELECT 1 FROM reads WHERE title='Selected snapshot'") is None
    assert private_setting("live_secret") == "live key"
    assert backups.backup_status()["last_error"] == (
        "Restore failed; the previous database remains active. post-swap check failed"
    )


def test_restore_rolls_back_and_chains_unexpected_post_swap_exception(
    database, monkeypatch, caplog
):
    selected = _prepare_distinct_backup_and_live_state(database)
    real_clear_journal = backups._clear_restore_journal
    unexpected = RuntimeError("journal sync boom")
    calls = 0

    def fail_once_after_swap():
        nonlocal calls
        calls += 1
        if calls == 1:
            assert row("SELECT 1 FROM reads WHERE title='Selected snapshot'") is not None
            assert private_setting("selected_secret") == "selected key"
            raise unexpected
        real_clear_journal()

    monkeypatch.setattr(backups, "_clear_restore_journal", fail_once_after_swap)

    with caplog.at_level(logging.ERROR, logger="afterword_engine.backups"):
        with pytest.raises(backups.BackupError) as exc_info:
            backups.restore_backup(selected["id"])

    expected_message = (
        "Restore failed; the previous database remains active. journal sync boom"
    )
    assert str(exc_info.value) == expected_message
    assert exc_info.value.__cause__ is unexpected
    assert [record.getMessage() for record in caplog.records] == [
        "Bookward restore failed"
    ]
    assert caplog.records[0].exc_info[1] is unexpected
    assert not backups._restore_journal_path().exists()
    assert row("SELECT 1 FROM reads WHERE title='Live before restore'") is not None
    assert row("SELECT 1 FROM reads WHERE title='Selected snapshot'") is None
    assert private_setting("live_secret") == "live key"
    assert backups.backup_status()["last_error"] == expected_message


def test_restore_secondary_rollback_failure_chains_error_and_cleans_unswapped_stage(
    database, monkeypatch, caplog
):
    selected = _prepare_distinct_backup_and_live_state(database)
    real_copy_to_stage = backups._copy_to_stage
    primary_error = backups.BackupError("active database could not close")
    staged_paths: list[Path] = []
    copy_calls = 0

    def copy_or_fail_rollback(source: Path, directory: Path) -> Path:
        nonlocal copy_calls
        copy_calls += 1
        if copy_calls == 1:
            staged = real_copy_to_stage(source, directory)
            staged_paths.append(staged)
            return staged
        raise OSError("rollback stage copy failed")

    def fail_live_swap(staged: Path, key_source: Path | None = None) -> None:
        Path(f"{staged}-wal").write_bytes(b"unswapped wal")
        raise primary_error

    monkeypatch.setattr(backups, "_copy_to_stage", copy_or_fail_rollback)
    monkeypatch.setattr(backups, "_replace_live_database", fail_live_swap)

    with caplog.at_level(logging.ERROR, logger="afterword_engine.backups"):
        with pytest.raises(backups.BackupError) as exc_info:
            backups.restore_backup(selected["id"])

    expected_message = (
        "Restore failed; automatic rollback did not complete. "
        "active database could not close"
    )
    assert exc_info.value is not primary_error
    assert str(exc_info.value) == expected_message
    assert exc_info.value.__cause__ is primary_error
    assert [record.getMessage() for record in caplog.records] == [
        "Failed to roll back an unsuccessful Bookward restore"
    ]
    assert backups.backup_status()["last_error"] == expected_message
    assert len(staged_paths) == 1
    assert not staged_paths[0].exists()
    assert not Path(f"{staged_paths[0]}-wal").exists()


def test_restore_missing_safety_backup_marks_rollback_failed(
    database, monkeypatch, caplog
):
    selected = _prepare_distinct_backup_and_live_state(database)
    primary_error = backups.BackupError("post-swap check failed")

    monkeypatch.setattr(backups, "_create_snapshot_unlocked", lambda: None)
    monkeypatch.setattr(backups, "_write_restore_journal", lambda *_args: None)
    monkeypatch.setattr(
        backups,
        "_clear_restore_journal",
        lambda: (_ for _ in ()).throw(primary_error),
    )

    with caplog.at_level(logging.ERROR, logger="afterword_engine.backups"):
        with pytest.raises(backups.BackupError) as exc_info:
            backups.restore_backup(selected["id"])

    expected_message = (
        "Restore failed; automatic rollback did not complete. post-swap check failed"
    )
    assert exc_info.value is not primary_error
    assert str(exc_info.value) == expected_message
    assert exc_info.value.__cause__ is primary_error
    assert caplog.records == []
    assert backups.backup_status()["last_error"] == expected_message


class _SimulatedCrash(BaseException):
    """Bypass except Exception blocks to model an abrupt process crash."""


@pytest.mark.parametrize("damage_mode", ["torn_bytes", "missing_file"])
def test_startup_recovery_rolls_back_an_interrupted_database_and_key_swap(
    database, damage_mode
):
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("Safety state", "Reader", "test"),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("recovery_test_secret", seal("safety key")),
        )
    safety = backups.create_backup()

    Path(settings.db).unlink()
    Path(f"{settings.db}-wal").unlink(missing_ok=True)
    Path(f"{settings.db}-shm").unlink(missing_ok=True)
    (database / "secret.key").unlink()
    initialize()
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("Partial restore", "Reader", "test"),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("partial_secret", seal("partial key")),
        )

    live_db = Path(settings.db)
    if damage_mode == "torn_bytes":
        live_db.write_bytes(b"SQLite format 3\x00torn-header-and-corrupt-pages")
        Path(f"{settings.db}-wal").write_bytes(b"corrupt-wal-frames")
        Path(f"{settings.db}-shm").write_bytes(b"corrupt-shm")
    else:
        live_db.unlink()
        Path(f"{settings.db}-wal").write_bytes(b"orphaned-wal")
        Path(f"{settings.db}-shm").unlink(missing_ok=True)

    backups._write_restore_journal(
        database / "backups" / f"{safety['id']}.sqlite3",
        database / "backups" / f"{safety['id']}.sqlite3",
    )
    journal_path = backups._restore_journal_path()
    assert journal_path.is_file()
    assert stat.S_IMODE(journal_path.stat().st_mode) == 0o600
    (database / "secret.key").unlink()

    assert backups.recover_interrupted_restore() is True
    assert not journal_path.exists()
    assert not backups._restore_journal_key_path(journal_path).exists()
    assert stat.S_IMODE(live_db.stat().st_mode) == 0o600
    assert stat.S_IMODE((database / "secret.key").stat().st_mode) == 0o600
    assert row("SELECT 1 FROM reads WHERE title='Safety state'") is not None
    assert row("SELECT 1 FROM reads WHERE title='Partial restore'") is None
    assert private_setting("recovery_test_secret") == "safety key"
    assert (
        backups.backup_status()["last_error"]
        == "An interrupted restore was rolled back to its safety backup."
    )


@pytest.mark.parametrize(
    "crash_step",
    [
        "before_journal",
        "after_journal_before_swap",
        "mid_swap",
        "after_swap_before_journal_clear",
    ],
)
def test_restore_backup_crash_injection_across_each_step(
    database, monkeypatch, crash_step
):
    from afterword_engine.database import connect

    # Create a target backup under a distinct generated key.
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("Target backup read", "Reader", "test"),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("restore_step_secret", seal("target-secret-value")),
        )
    target = backups.create_backup()

    # Reinitialize live profile with a new key and live state before restoring.
    Path(settings.db).unlink()
    Path(f"{settings.db}-wal").unlink(missing_ok=True)
    Path(f"{settings.db}-shm").unlink(missing_ok=True)
    (database / "secret.key").unlink()
    initialize()
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("Live pre-restore read", "Reader", "test"),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("restore_step_secret", seal("live-secret-value")),
        )
    expected_live_key = (database / "secret.key").read_bytes()

    with monkeypatch.context() as mp:
        if crash_step == "before_journal":
            mp.setattr(
                backups,
                "_write_restore_journal",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    _SimulatedCrash("before journal")
                ),
            )
        elif crash_step == "after_journal_before_swap":
            original_write_journal = backups._write_restore_journal

            def write_then_crash(*args, **kwargs):
                original_write_journal(*args, **kwargs)
                raise _SimulatedCrash("after journal before swap")

            mp.setattr(backups, "_write_restore_journal", write_then_crash)
        elif crash_step == "mid_swap":
            def crash_mid_swap(staged, key_source=None, **_kwargs):
                assert key_source is not None
                (database / "secret.key").write_bytes(key_source.read_bytes())
                Path(settings.db).write_bytes(b"SQLite format 3\x00torn-mid-swap-pages")
                Path(f"{settings.db}-wal").write_bytes(b"corrupt-mid-swap-wal")
                staged.unlink(missing_ok=True)
                raise _SimulatedCrash("mid swap after key replace")

            mp.setattr(backups, "_replace_live_database", crash_mid_swap)
        elif crash_step == "after_swap_before_journal_clear":
            mp.setattr(
                backups,
                "_clear_restore_journal",
                lambda: (_ for _ in ()).throw(
                    _SimulatedCrash("after swap before clear")
                ),
            )

        with pytest.raises(_SimulatedCrash):
            backups.restore_backup(target["id"])

    if crash_step == "before_journal":
        assert backups.has_restore_journal() is False
        assert backups.recover_interrupted_restore() is False
    else:
        assert backups.has_restore_journal() is True
        with pytest.raises(sqlite3.OperationalError, match="interrupted restore"):
            connect()
        assert backups.recover_interrupted_restore() is True
        assert backups.has_restore_journal() is False

    assert (database / "secret.key").read_bytes() == expected_live_key
    assert row("SELECT 1 FROM reads WHERE title='Live pre-restore read'") is not None
    assert row("SELECT 1 FROM reads WHERE title='Target backup read'") is None
    assert private_setting("restore_step_secret") == "live-secret-value"


def test_mid_swap_crash_without_safety_key_sidecar_prevents_key_database_mismatch(
    database, monkeypatch
):
    # Case 1: safety snapshot's .secret.key sidecar is missing, so rollback falls
    # back to the pre-swap key staged alongside the restore journal.
    with transaction() as con:
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("sidecar_fallback_secret", seal("target-key-secret")),
        )
    target = backups.create_backup()

    Path(settings.db).unlink()
    Path(f"{settings.db}-wal").unlink(missing_ok=True)
    Path(f"{settings.db}-shm").unlink(missing_ok=True)
    (database / "secret.key").unlink()
    initialize()
    with transaction() as con:
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("sidecar_fallback_secret", seal("original-live-secret")),
        )
    original_key_bytes = (database / "secret.key").read_bytes()

    def crash_after_losing_safety_sidecar(staged, key_source=None, **_kwargs):
        journal = backups._restore_journal_path()
        safety_id, _, _ = backups._read_restore_journal_meta(journal)
        safety_sidecar = database / "backups" / f"{safety_id}.secret.key"
        safety_sidecar.unlink()
        assert key_source is not None
        (database / "secret.key").write_bytes(key_source.read_bytes())
        Path(settings.db).write_bytes(b"corrupt-mid-swap-db")
        staged.unlink(missing_ok=True)
        raise _SimulatedCrash("mid swap with missing safety sidecar")

    with monkeypatch.context() as mp:
        mp.setattr(backups, "_replace_live_database", crash_after_losing_safety_sidecar)
        with pytest.raises(_SimulatedCrash):
            backups.restore_backup(target["id"])

    assert (database / "secret.key").read_bytes() != original_key_bytes
    assert backups.recover_interrupted_restore() is True
    assert (database / "secret.key").read_bytes() == original_key_bytes
    assert private_setting("sidecar_fallback_secret") == "original-live-secret"

    # Case 2: configured AFTERWORD_SECRET_KEY means _restore_key_sidecar returns None
    # (no sidecar on safety snapshot); rollback clears any stray generated secret.key.
    configured_key = Fernet.generate_key().decode()
    with monkeypatch.context() as mp:
        mp.setenv("AFTERWORD_SECRET_KEY", configured_key)
        (database / "secret.key").unlink(missing_ok=True)
        with transaction() as con:
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,1) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("sidecar_fallback_secret", seal("configured-env-secret")),
            )
        assert backups.recovery_hold_active() is True
        env_safety = backups.create_backup(confirm_clear_hold=True)
        env_safety_path = database / "backups" / f"{env_safety['id']}.sqlite3"
        assert not (database / "backups" / f"{env_safety['id']}.secret.key").exists()

        backups._write_restore_journal(env_safety_path, env_safety_path)
        (database / "secret.key").write_bytes(Fernet.generate_key())
        Path(settings.db).write_bytes(b"corrupt-env-key-db")

        assert backups.recover_interrupted_restore() is True
        assert not (database / "secret.key").exists()
        assert private_setting("sidecar_fallback_secret") == "configured-env-secret"


@pytest.mark.parametrize(
    ("error_message", "error_code"),
    [
        ("disk I/O error", sqlite3.SQLITE_IOERR),
        ("unable to open database file", sqlite3.SQLITE_CANTOPEN),
    ],
)
def test_rollback_mode_tolerates_non_lock_sqlite_operational_errors(
    database, monkeypatch, error_message, error_code
):
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("Rollback operational error row", "Reader", "test"),
        )
    safety = backups.create_backup()
    safety_path = database / "backups" / f"{safety['id']}.sqlite3"
    backups._write_restore_journal(safety_path, safety_path)
    Path(f"{settings.db}-wal").write_bytes(b"damaged-wal")

    real_connect = backups.sqlite3.connect
    live_resolved = Path(settings.db).resolve()

    def connect_failing_live(target, *args, **kwargs):
        if not kwargs.get("uri") and Path(target).resolve() == live_resolved:
            err = sqlite3.OperationalError(error_message)
            err.sqlite_errorcode = error_code
            raise err
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(backups.sqlite3, "connect", connect_failing_live)
    assert backups.recover_interrupted_restore() is True
    assert not backups.has_restore_journal()
    assert not Path(f"{settings.db}-wal").exists()
    monkeypatch.setattr(backups.sqlite3, "connect", real_connect)
    assert row("SELECT 1 FROM reads WHERE title='Rollback operational error row'") is not None


def test_interrupted_restore_rejects_corrupt_journal_locks_and_missing_safety_snapshot(
    database,
):
    safety = backups.create_backup()
    safety_snapshot = database / "backups" / f"{safety['id']}.sqlite3"
    journal_path = backups._restore_journal_path()

    # 1. Corrupt journal JSON surfaces a journal error and keeps the fence.
    journal_path.write_text("{corrupt-json", encoding="utf-8")
    journal_path.chmod(0o600)
    with pytest.raises(backups.BackupError, match="journal is invalid"):
        backups.recover_interrupted_restore()
    assert journal_path.is_file()
    assert backups.backup_status()["last_error"] == "The interrupted restore journal is invalid."

    # 2. Active external SQLite lock is rejected even in rollback mode.
    backups._write_restore_journal(safety_snapshot, safety_snapshot)
    external_lock = sqlite3.connect(settings.db)
    external_lock.execute("BEGIN IMMEDIATE")
    external_lock.execute(
        "INSERT INTO reads(title,author,source) VALUES('Locked row','External','test')"
    )
    try:
        with pytest.raises(
            backups.BackupError, match="could not be safely closed for restore"
        ):
            backups.recover_interrupted_restore()
        assert journal_path.is_file()
        assert (
            backups.backup_status()["last_error"]
            == "The active database could not be safely closed for restore."
        )
    finally:
        external_lock.rollback()
        external_lock.close()

    # 3. Missing safety snapshot preserves availability BackupError and keeps the fence.
    safety_snapshot.unlink()
    with pytest.raises(backups.BackupError, match="no longer available"):
        backups.recover_interrupted_restore()
    assert backups.backup_status()["last_error"] == "That backup is no longer available."
    backups._clear_restore_journal()


def test_fenced_profile_blocks_sqlite_access_and_recovers_by_restoring_another_backup_via_middleware(
    database,
):
    import asyncio
    from afterword_engine.database import connect
    from afterword_engine.tenancy import database_path_override

    headers = authenticated_headers()
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("Recoverable alternate snapshot", "Reader", "test"),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("alternate_backup_secret", seal("alternate-backup-key")),
        )
    visible_backup = backups.create_backup()

    missing_safety_id = "bookward-20260101T000000Z-12345678"
    journal_path = backups._restore_journal_path()
    journal_path.write_text(
        f'{{"safety_backup": "{missing_safety_id}", "selected_backup": "{visible_backup["id"]}"}}',
        encoding="utf-8",
    )
    journal_path.chmod(0o600)

    live_db = Path(settings.db)
    sentinel = b"SQLite format 3\x00unrecovered-partial-bytes"
    live_db.write_bytes(sentinel)
    (database / "secret.key").write_bytes(Fernet.generate_key())
    Path(f"{settings.db}-wal").unlink(missing_ok=True)
    Path(f"{settings.db}-shm").unlink(missing_ok=True)

    with pytest.raises(sqlite3.OperationalError, match="interrupted restore"):
        connect()
    with pytest.raises(sqlite3.OperationalError, match="interrupted restore"):
        transaction().__enter__()
    with pytest.raises(backups.BackupError, match="interrupted restore"):
        backups.create_backup()

    # Staged migrations under database_path_override still bypass the journal fence.
    staged = database / "staged-migration.sqlite3"
    with database_path_override(staged):
        initialize(seed_demo=False)
    assert staged.is_file()

    # Scheduled backup loop must not create snapshots while fenced.
    stop = asyncio.Event()

    async def stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    async def run_scheduler():
        stopper = asyncio.create_task(stop_soon())
        await backups.backup_scheduler_loop(stop)
        await stopper

    asyncio.run(run_scheduler())
    assert [item["id"] for item in backups.list_backups()] == [visible_backup["id"]]

    client = TestClient(app, headers=headers)
    assert client.get("/api/health").status_code == 200
    assert client.get("/auth/config").status_code == 200
    assert client.get("/auth/me").status_code == 200

    overview_resp = client.get("/api/overview")
    assert overview_resp.status_code == 503
    assert overview_resp.json()["detail"] == backups.RESTORE_FENCE_DETAIL
    assert str(database) not in overview_resp.text

    sync_resp = client.post("/api/sync", json={})
    assert sync_resp.status_code == 503

    create_resp = client.post("/api/backups/create", json={})
    assert create_resp.status_code == 503

    backups_resp = client.get("/api/backups")
    assert backups_resp.status_code == 200
    payload = backups_resp.json()
    assert [item["id"] for item in payload["backups"]] == [visible_backup["id"]]
    assert payload["last_error"] == "That backup is no longer available."
    assert str(database) not in backups_resp.text
    assert "secret.key" not in backups_resp.text

    # Zero bytes or sidecars were written to the fenced live database before restore.
    assert live_db.read_bytes() == sentinel
    assert not Path(f"{settings.db}-wal").exists()

    # Restoring a missing backup fails closed and leaves the fence intact.
    bad_restore = client.post(
        "/api/backups/bookward-20260101T000000Z-ffffffff/restore", json={}
    )
    assert bad_restore.status_code == 400
    assert backups.has_restore_journal() is True
    assert client.get("/api/overview").status_code == 503

    # Restoring a valid backup passes through the middleware fence and recovers the profile.
    recover_resp = client.post(f"/api/backups/{visible_backup['id']}/restore", json={})
    assert recover_resp.status_code == 200
    assert recover_resp.json()["restored_backup"] == visible_backup["id"]
    assert backups.has_restore_journal() is False

    recovered_overview = client.get("/api/overview")
    assert recovered_overview.status_code == 200
    assert "Recoverable alternate snapshot" in {
        item["title"] for item in recovered_overview.json()["history"]
    }
    assert private_setting("alternate_backup_secret") == "alternate-backup-key"


def test_admin_can_delete_stuck_restore_journal_and_middleware_skips_thread_hop_when_clear(
    database, monkeypatch
):
    from afterword_engine import main as main_module
    from afterword_engine import profiles

    admin_headers = authenticated_headers()
    reader = profiles.create_account("journal-reader", "journal-reader-password-long")
    reader_session, _ = profiles.create_session(reader["id"])
    reader_headers = {"x-bookward-session": reader_session}

    journal_path = backups._restore_journal_path()
    journal_path.write_text(
        '{"safety_backup": "bookward-20260101T000000Z-deadbeef"}', encoding="utf-8"
    )
    journal_path.chmod(0o600)

    client = TestClient(app)
    assert client.get("/api/overview", headers=admin_headers).status_code == 503
    assert (
        client.delete(
            "/admin/profiles/legacy/restore-journal", headers=reader_headers
        ).status_code
        == 403
    )
    assert backups.has_restore_journal("legacy") is True

    assert (
        client.delete(
            "/admin/profiles/00000000000000000000000000000000/restore-journal",
            headers=admin_headers,
        ).status_code
        == 404
    )

    cleared = client.delete(
        "/admin/profiles/legacy/restore-journal", headers=admin_headers
    )
    assert cleared.status_code == 200
    assert cleared.json() == {"cleared": True, "profile_id": "legacy"}
    assert backups.has_restore_journal("legacy") is False

    # When no journal exists, session and API-token requests must not hop via asyncio.to_thread.
    api_token = profiles.create_profile_api_token("legacy", "Fast path check")["token"]
    real_to_thread = main_module.asyncio.to_thread
    recovery_thread_calls = 0

    async def spy_to_thread(func, /, *args, **kwargs):
        nonlocal recovery_thread_calls
        if func is main_module._recover_profile_for_request:
            recovery_thread_calls += 1
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(main_module.asyncio, "to_thread", spy_to_thread)
    assert client.get("/api/overview", headers=admin_headers).status_code == 200
    assert (
        client.get(
            "/api/v1/overview", headers={"authorization": f"Bearer {api_token}"}
        ).status_code
        == 200
    )
    assert recovery_thread_calls == 0


def test_prune_backups_retains_safety_snapshot_referenced_by_restore_journal(
    database, monkeypatch
):
    monkeypatch.setattr(settings, "backup_retention_count", 1)
    safety = backups.create_backup()
    newer_one = backups.create_backup()
    newer_two = backups.create_backup()
    # Re-create safety snapshot as an older timestamped entry to verify retention protection.
    old_id = "bookward-20200101T000000Z-abcdef01"
    old_snapshot = database / "backups" / f"{old_id}.sqlite3"
    old_sidecar = database / "backups" / f"{old_id}.secret.key"
    latest_id = backups.list_backups()[0]["id"]
    latest_snapshot = database / "backups" / f"{latest_id}.sqlite3"
    latest_sidecar = database / "backups" / f"{latest_id}.secret.key"
    old_snapshot.write_bytes(latest_snapshot.read_bytes())
    old_snapshot.chmod(0o600)
    old_sidecar.write_bytes(latest_sidecar.read_bytes())
    old_sidecar.chmod(0o600)

    backups._write_restore_journal(old_snapshot, latest_snapshot)
    try:
        backups._prune_backups()
        remaining_ids = {item["id"] for item in backups.list_backups()}
        assert old_id in remaining_ids
        assert latest_id in remaining_ids
        assert old_snapshot.is_file()
        assert old_sidecar.is_file()
    finally:
        backups._clear_restore_journal()

    backups._prune_backups()
    assert len(backups.list_backups()) == 1
    assert not old_snapshot.exists()
    assert not old_sidecar.exists()
    _ = (safety, newer_one, newer_two)


def test_restore_backup_in_process_rollback_replaces_damaged_live_database(
    database, monkeypatch
):
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("Target snapshot row", "Reader", "test"),
        )
    target = backups.create_backup()

    with transaction() as con:
        con.execute("DELETE FROM reads WHERE title='Target snapshot row'")
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("Pre-restore live row", "Reader", "test"),
        )

    original_replace = backups._replace_live_database
    calls = 0

    def corrupt_on_initial_swap(
        staged, key_source=None, *, rollback=False, clear_generated_key=False
    ):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert rollback is False
            Path(settings.db).write_bytes(b"corrupt-mid-swap-bytes")
            raise OSError(28, "No space left on device")
        assert rollback is True
        return original_replace(
            staged,
            key_source,
            rollback=rollback,
            clear_generated_key=clear_generated_key,
        )

    monkeypatch.setattr(backups, "_replace_live_database", corrupt_on_initial_swap)
    with pytest.raises(backups.BackupError, match="previous database remains active"):
        backups.restore_backup(target["id"])

    assert calls == 2
    assert not backups._restore_journal_path().exists()
    assert row("SELECT 1 FROM reads WHERE title='Pre-restore live row'") is not None


def test_backup_scheduler_interval_and_retention(database, monkeypatch):
    monkeypatch.setattr(settings, "backup_retention_count", 2)
    assert backups.backup_is_due()
    first = backups.create_backup()
    latest = datetime.fromisoformat(first["created_at"])
    assert not backups.backup_is_due(latest + timedelta(hours=23, minutes=59))
    assert backups.backup_is_due(latest + timedelta(hours=24))

    backups.create_backup()
    backups.create_backup()
    assert len(backups.list_backups()) == 2
    monkeypatch.setattr(settings, "backup_interval_hours", 0)
    assert not backups.backup_is_due(datetime.now(timezone.utc) + timedelta(days=5))


def test_backup_api_lists_only_safe_metadata_and_restores_selected_snapshot(database):
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("API recovery", "Reader", "test"),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("api_recovery_secret", seal("api secret")),
        )

    client = TestClient(app, headers=authenticated_headers())
    created = client.post("/api/backups/create", json={})
    assert created.status_code == 200
    backup = created.json()["backup"]
    status_response = client.get("/api/backups")
    assert status_response.status_code == 200
    status_payload = status_response.json()
    assert status_payload["last_success_at"]
    assert status_payload["backups"][0]["id"] == backup["id"]
    assert "secret.key" not in status_response.text
    assert "api secret" not in status_response.text

    with transaction() as con:
        con.execute("DELETE FROM reads WHERE title='API recovery'")
    restored = client.post(f"/api/backups/{backup['id']}/restore", json={})
    assert restored.status_code == 200
    assert restored.json()["restored_backup"] == backup["id"]
    assert row("SELECT 1 FROM reads WHERE title='API recovery'") is not None
    assert private_setting("api_recovery_secret") == "api secret"


def _backdate_profile_snapshot(backup_id: str, target_time: datetime) -> str:
    stamp = target_time.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = backup_id.rsplit("-", 1)[-1]
    aged_id = f"bookward-{stamp}-{suffix}"
    directory = backups.backup_directory()
    old_db = directory / f"{backup_id}.sqlite3"
    new_db = directory / f"{aged_id}.sqlite3"
    old_db.rename(new_db)
    old_key = directory / f"{backup_id}.secret.key"
    if old_key.exists():
        old_key.rename(directory / f"{aged_id}.secret.key")
    return aged_id


def _backdate_auth_snapshot(backup_id: str, target_time: datetime) -> str:
    stamp = target_time.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = backup_id.rsplit("-", 1)[-1]
    aged_id = f"bookward-auth-{stamp}-{suffix}"
    directory = backups.auth_backup_directory()
    (directory / f"{backup_id}.sqlite3").rename(directory / f"{aged_id}.sqlite3")
    return aged_id


def _wipe_live_data_volume(data_root: Path) -> None:
    for item in list(data_root.iterdir()):
        if item.name == "backups":
            continue
        if item.is_dir():
            for nested in list(item.rglob("*")):
                if nested.is_file() or nested.is_symlink():
                    nested.unlink()
            for nested in sorted(item.rglob("*"), reverse=True):
                if nested.is_dir():
                    nested.rmdir()
            item.rmdir()
        else:
            item.unlink()


@pytest.mark.parametrize("retention", [1, 7, 100])
def test_fresh_volume_boot_with_25h_snapshots_pauses_scheduler_across_retention(
    tmp_path: Path, monkeypatch, caplog, retention: int
):
    monkeypatch.setattr(settings, "db", str(tmp_path / "afterword.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.setattr(settings, "backup_interval_hours", 24)
    monkeypatch.setattr(settings, "backup_retention_count", retention)
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    monkeypatch.delenv("AFTERWORD_AUTH_PASSWORD", raising=False)

    profiles.initialize_auth()
    profiles.create_first_admin("owner", "owner-password-with-enough-length")
    alice = profiles.create_account("alice", "alice-password-with-enough-length")

    aged_at = datetime.now(timezone.utc) - timedelta(hours=25)
    with profile_scope("legacy"):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
                ("Owner real book", "Author", "test"),
            )
        legacy_snap = _backdate_profile_snapshot(
            backups.create_backup()["id"], aged_at
        )

    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
                ("Alice real book", "Author", "test"),
            )
        alice_snap = _backdate_profile_snapshot(
            backups.create_backup()["id"], aged_at
        )

    auth_snap = _backdate_auth_snapshot(backups.create_auth_backup()["id"], aged_at)

    # Simulate starting on an empty /data mount before restoring auth.sqlite3.
    _wipe_live_data_volume(tmp_path)

    backups_root = (tmp_path / "backups").resolve()
    real_connect = sqlite3.connect
    opened_snapshots = []

    def tracking_connect(database_arg, *args, **kwargs):
        text = str(database_arg)
        if str(backups_root) in text:
            opened_snapshots.append(text)
        return real_connect(database_arg, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    profiles.initialize_auth()
    with profile_scope("legacy"):
        initialize()
    assert opened_snapshots == []
    monkeypatch.setattr(sqlite3, "connect", real_connect)

    assert backups.auth_recovery_hold_active() is True
    assert backups.auth_backup_is_due() is False
    with profile_scope("legacy"):
        assert backups.recovery_hold_active() is True
        assert backups.backup_is_due() is False
        assert backups.backup_status()["next_backup_at"] is None
        assert [item["id"] for item in backups.list_backups()] == [legacy_snap]

    # Run backup_scheduler_loop through two poll cycles: verify warnings log each cycle and nothing is pruned.
    real_wait_for = asyncio.wait_for

    async def fast_scheduler_cycles():
        stop = asyncio.Event()
        waits = 0

        async def quick_wait(coro, timeout):
            nonlocal waits
            waits += 1
            if waits in (1, 2):
                coro.close()
                raise TimeoutError
            stop.set()
            return await real_wait_for(coro, timeout=1)

        monkeypatch.setattr(asyncio, "wait_for", quick_wait)
        try:
            await backups.backup_scheduler_loop(stop)
        finally:
            monkeypatch.setattr(asyncio, "wait_for", real_wait_for)

    with caplog.at_level(logging.WARNING, logger="afterword_engine.backups"):
        asyncio.run(fast_scheduler_cycles())

    warning_messages = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert (
        sum(
            "Scheduled identity-registry backup skipped while recovery hold is active"
            in msg
            for msg in warning_messages
        )
        == 2
    )
    assert (
        sum(
            "Scheduled Bookward backup skipped for profile legacy while recovery hold is active"
            in msg
            for msg in warning_messages
        )
        == 2
    )

    # Simulate repeated container restarts against the already-initialized /data volume.
    profiles.initialize_auth()
    with profile_scope("legacy"):
        initialize()
    assert backups.auth_backup_is_due() is False
    with profile_scope("legacy"):
        assert backups.backup_is_due() is False
        assert [item["id"] for item in backups.list_backups()] == [legacy_snap]
    with profile_scope(alice["profile_id"]):
        assert [item["id"] for item in backups.list_backups()] == [alice_snap]
    assert [
        item["id"] for item in backups.identity_backup_status()["backups"]
    ] == [auth_snap]


def test_multi_profile_recovery_hold_restore_and_dismissal_preserve_retention_one(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(settings, "db", str(tmp_path / "afterword.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.setattr(settings, "backup_interval_hours", 24)
    monkeypatch.setattr(settings, "backup_retention_count", 1)
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    monkeypatch.delenv("AFTERWORD_AUTH_PASSWORD", raising=False)

    profiles.initialize_auth()
    profiles.create_first_admin("owner", "owner-password-with-enough-length")
    alice = profiles.create_account("alice", "alice-password-with-enough-length")
    bob = profiles.create_account("bob", "bob-password-with-enough-length")

    aged_at = datetime.now(timezone.utc) - timedelta(hours=25)
    older_at = datetime.now(timezone.utc) - timedelta(hours=50)

    monkeypatch.setattr(settings, "backup_retention_count", 7)
    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
                ("Alice older snapshot", "Author", "test"),
            )
        alice_older = _backdate_profile_snapshot(
            backups.create_backup()["id"], older_at
        )
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
                ("Alice newest snapshot", "Author", "test"),
            )
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
                ("alice_secret", seal("alice-secret-value")),
            )
        alice_newest = _backdate_profile_snapshot(
            backups.create_backup()["id"], aged_at
        )

    with profile_scope(bob["profile_id"]):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
                ("Bob surviving snapshot", "Author", "test"),
            )
        bob_snap = _backdate_profile_snapshot(
            backups.create_backup()["id"], aged_at
        )

    auth_snap = _backdate_auth_snapshot(backups.create_auth_backup()["id"], aged_at)
    monkeypatch.setattr(settings, "backup_retention_count", 1)

    # Lose /data completely, restore auth registry offline, then boot profiles.
    _wipe_live_data_volume(tmp_path)
    auth_restore = backups.restore_auth_backup(auth_snap)
    assert auth_restore["restored"] is True
    assert backups.auth_recovery_hold_active() is False
    assert (backups.auth_backup_directory() / f"{auth_snap}.sqlite3").is_file()
    # Offline restore on empty /data must not let the 5s startup poll immediately prune auth_snap.
    assert backups.auth_backup_is_due() is False
    assert backups.auth_backup_is_due(
        datetime.now(timezone.utc) + timedelta(hours=24, seconds=1)
    ) is True

    profiles.initialize_auth()
    carol = profiles.create_account("carol", "carol-password-with-enough-length")
    for pid in profiles.profile_ids():
        with profile_scope(pid):
            initialize(seed_demo=pid == "legacy")

    # Carol has no prior snapshots, so scheduled backups work immediately without a hold.
    with profile_scope(carol["profile_id"]):
        assert backups.recovery_hold_active() is False
        assert backups.backup_is_due() is True

    # Alice sees newest pre-disaster snapshot at backups[0]; restore clears hold and keeps snapshot under retention=1.
    with profile_scope(alice["profile_id"]):
        alice_status = backups.backup_status()
        assert alice_status["recovery_hold"] is True
        assert [item["id"] for item in alice_status["backups"]] == [
            alice_newest,
            alice_older,
        ]
        restored = backups.restore_backup(alice_newest)
        assert restored["restored_backup"] == alice_newest
        assert backups.recovery_hold_active() is False
        retained_ids = {item["id"] for item in backups.list_backups()}
        assert alice_newest in retained_ids
        assert restored["safety_backup"]["id"] in retained_ids
        assert (
            backups.backup_directory() / f"{alice_newest}.secret.key"
        ).is_file()
        assert private_setting("alice_secret") == "alice-secret-value"
        assert (
            row("SELECT 1 FROM reads WHERE title='Alice newest snapshot'")
            is not None
        )
        assert backups.backup_is_due(
            datetime.now(timezone.utc) + timedelta(hours=24, seconds=1)
        ) is True

    # Bob's hold remains active until Bob explicitly confirms dismissal or manual backup.
    with profile_scope(bob["profile_id"]):
        assert backups.recovery_hold_active() is True
        assert backups.backup_is_due() is False
        with pytest.raises(backups.RecoveryHoldError):
            backups.create_backup()
        with pytest.raises(backups.RecoveryHoldError):
            backups.dismiss_recovery_hold(confirm=False)
        assert backups.backup_status()["last_error"] is None
        assert [item["id"] for item in backups.list_backups()] == [bob_snap]

        dismissed = backups.dismiss_recovery_hold(confirm=True)
        assert dismissed["recovery_hold"] is False
        assert backups.backup_is_due() is True


def test_recovery_hold_api_exposes_safe_metadata_and_requires_confirmation(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(settings, "db", str(tmp_path / "afterword.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.setattr(settings, "backup_interval_hours", 24)
    monkeypatch.setattr(settings, "backup_retention_count", 1)
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    monkeypatch.delenv("AFTERWORD_AUTH_PASSWORD", raising=False)

    profiles.initialize_auth()
    profiles.create_first_admin("owner", "owner-password-with-enough-length")
    with profile_scope("legacy"):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
                ("api_hold_secret", seal("top-secret-token")),
            )
        aged = _backdate_profile_snapshot(
            backups.create_backup()["id"],
            datetime.now(timezone.utc) - timedelta(hours=25),
        )
    aged_auth = _backdate_auth_snapshot(
        backups.create_auth_backup()["id"],
        datetime.now(timezone.utc) - timedelta(hours=25),
    )

    _wipe_live_data_volume(tmp_path)
    profiles.initialize_auth()
    admin = profiles.create_first_admin("new-admin", "new-admin-password-long")
    reader = profiles.create_account("new-reader", "new-reader-password-long")
    with profile_scope("legacy"):
        initialize()
    with profile_scope(reader["profile_id"]):
        initialize(seed_demo=False)

    admin_session, _ = profiles.create_session(admin["id"])
    reader_session, _ = profiles.create_session(reader["id"])

    client = TestClient(app)
    admin_headers = {"x-bookward-session": admin_session}
    reader_headers = {"x-bookward-session": reader_session}

    profile_status = client.get("/api/backups", headers=admin_headers)
    assert profile_status.status_code == 200
    payload = profile_status.json()
    assert payload["recovery_hold"] is True
    assert payload["backups"][0]["id"] == aged
    assert str(tmp_path) not in profile_status.text
    assert "secret.key" not in profile_status.text
    assert "top-secret-token" not in profile_status.text

    blocked_create = client.post("/api/backups/create", json={}, headers=admin_headers)
    assert blocked_create.status_code == 409
    assert [item["id"] for item in backups.list_backups()] == [aged]

    blocked_dismiss = client.post(
        "/api/backups/recovery-hold/dismiss",
        json={"confirm": False},
        headers=admin_headers,
    )
    assert blocked_dismiss.status_code == 400

    confirmed_create = client.post(
        "/api/backups/create",
        json={"confirm_clear_hold": True},
        headers=admin_headers,
    )
    assert confirmed_create.status_code == 200
    confirmed_payload = confirmed_create.json()
    assert confirmed_payload["status"]["recovery_hold"] is False
    assert [item["id"] for item in confirmed_payload["status"]["backups"]] == [
        confirmed_payload["backup"]["id"],
        aged,
    ]
    assert (backups.backup_directory() / f"{aged}.secret.key").is_file()

    auth_status = client.get("/admin/auth-backups", headers=admin_headers)
    assert auth_status.status_code == 200
    auth_payload = auth_status.json()
    assert auth_payload["recovery_hold"] is True
    assert auth_payload["backups"][0]["id"] == aged_auth
    assert str(tmp_path) not in auth_status.text

    assert (
        client.post(
            "/admin/auth-backups/recovery-hold/dismiss",
            json={"confirm": True},
            headers=reader_headers,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/admin/auth-backups/recovery-hold/dismiss",
            json={"confirm": False},
            headers=admin_headers,
        ).status_code
        == 400
    )
    dismissed_auth = client.post(
        "/admin/auth-backups/recovery-hold/dismiss",
        json={"confirm": True},
        headers=admin_headers,
    )
    assert dismissed_auth.status_code == 200
    assert dismissed_auth.json()["status"]["recovery_hold"] is False


def test_confirmed_create_skips_prune_when_clearing_hold_and_prunes_oldest_on_next_backup(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(settings, "db", str(tmp_path / "afterword.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.setattr(settings, "backup_interval_hours", 24)
    monkeypatch.setattr(settings, "backup_retention_count", 1)
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    monkeypatch.delenv("AFTERWORD_AUTH_PASSWORD", raising=False)

    initialize(seed_demo=False)
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("Pre-disaster read", "Author", "test"),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("pre_hold_secret", seal("pre-disaster-key-check")),
        )
    pre_disaster = _backdate_profile_snapshot(
        backups.create_backup()["id"],
        datetime.now(timezone.utc) - timedelta(hours=25),
    )

    _wipe_live_data_volume(tmp_path)
    initialize(seed_demo=False)
    assert backups.recovery_hold_active() is True

    # Verify create_backup checks recovery_hold_active() only once (under database_lock).
    hold_checks = 0
    real_hold_active = backups.recovery_hold_active

    def counting_hold_active():
        nonlocal hold_checks
        hold_checks += 1
        return real_hold_active()

    monkeypatch.setattr(backups, "recovery_hold_active", counting_hold_active)
    with pytest.raises(backups.RecoveryHoldError):
        backups.create_backup()
    assert hold_checks == 1

    hold_checks = 0
    # Under retention=1, confirming Create backup now must NOT delete pre_disaster.
    clearing = backups.create_backup(confirm_clear_hold=True)
    assert hold_checks == 1
    monkeypatch.setattr(backups, "recovery_hold_active", real_hold_active)
    assert backups.recovery_hold_active() is False
    assert [item["id"] for item in backups.list_backups()] == [
        clearing["id"],
        pre_disaster,
    ]
    assert (backups.backup_directory() / f"{pre_disaster}.secret.key").is_file()
    assert (backups.backup_directory() / f"{clearing['id']}.secret.key").is_file()

    # Under retention=2, a subsequent normal backup evicts only the oldest snapshot (pre_disaster).
    clearing_aged = _backdate_profile_snapshot(
        clearing["id"],
        datetime.now(timezone.utc) - timedelta(hours=1),
    )
    monkeypatch.setattr(settings, "backup_retention_count", 2)
    subsequent = backups.create_backup()
    assert [item["id"] for item in backups.list_backups()] == [
        subsequent["id"],
        clearing_aged,
    ]
    assert not (backups.backup_directory() / f"{pre_disaster}.sqlite3").exists()
    assert not (backups.backup_directory() / f"{pre_disaster}.secret.key").exists()
    assert (backups.backup_directory() / f"{clearing_aged}.secret.key").is_file()
    assert (backups.backup_directory() / f"{subsequent['id']}.secret.key").is_file()


def test_recovery_hold_checks_fail_closed_on_sqlite_errors(
    tmp_path: Path, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "db", str(tmp_path / "afterword.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.setattr(settings, "backup_interval_hours", 24)
    monkeypatch.setattr(settings, "backup_retention_count", 1)
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    monkeypatch.delenv("AFTERWORD_AUTH_PASSWORD", raising=False)

    profiles.initialize_auth()
    profiles.create_first_admin("owner", "owner-password-with-enough-length")
    with profile_scope("legacy"):
        initialize(seed_demo=False)
        legacy_snap = _backdate_profile_snapshot(
            backups.create_backup()["id"],
            datetime.now(timezone.utc) - timedelta(hours=25),
        )
    auth_snap = _backdate_auth_snapshot(
        backups.create_auth_backup()["id"],
        datetime.now(timezone.utc) - timedelta(hours=25),
    )

    assert backups.backup_is_due() is True
    assert backups.auth_backup_is_due() is True
    healthy_profile_status = backups.backup_status()
    assert healthy_profile_status["recovery_hold"] is False
    assert healthy_profile_status["next_backup_at"] is not None
    assert backups.identity_backup_status()["recovery_hold"] is False

    real_profile_connect = backups.connect
    real_sqlite_connect = sqlite3.connect

    def locked_profile_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    auth_file_resolved = str(profiles.auth_path().resolve())

    def locked_auth_sqlite_connect(target, *args, **kwargs):
        if auth_file_resolved in str(target):
            raise sqlite3.OperationalError("database is locked")
        return real_sqlite_connect(target, *args, **kwargs)

    monkeypatch.setattr(backups, "connect", locked_profile_connect)
    monkeypatch.setattr(sqlite3, "connect", locked_auth_sqlite_connect)

    assert backups._recovery_hold_value() is True
    assert backups.recovery_hold_active() is True
    assert backups.backup_is_due() is False
    locked_profile_status = backups.backup_status()
    assert locked_profile_status["recovery_hold"] is True
    assert locked_profile_status["recovery_hold_at"] is None
    assert locked_profile_status["next_backup_at"] is None
    assert locked_profile_status["last_error"] == "Backup status is temporarily unavailable."

    assert backups._auth_recovery_hold_value() is True
    assert backups.auth_recovery_hold_active() is True
    assert backups.auth_backup_is_due() is False
    locked_auth_status = backups.identity_backup_status()
    assert locked_auth_status["recovery_hold"] is True
    assert locked_auth_status["recovery_hold_at"] is None
    assert (
        locked_auth_status["last_error"]
        == "Identity backup status is temporarily unavailable."
    )

    real_wait_for = asyncio.wait_for

    async def locked_scheduler_cycle():
        stop = asyncio.Event()
        waits = 0

        async def quick_wait(coro, timeout):
            nonlocal waits
            waits += 1
            if waits == 1:
                coro.close()
                raise TimeoutError
            stop.set()
            return await real_wait_for(coro, timeout=1)

        monkeypatch.setattr(asyncio, "wait_for", quick_wait)
        try:
            await backups.backup_scheduler_loop(stop)
        finally:
            monkeypatch.setattr(asyncio, "wait_for", real_wait_for)

    # Allow profile_ids() to return ["legacy"] so both auth and profile transient-lock log paths execute.
    monkeypatch.setattr(profiles, "profile_ids", lambda: ["legacy"])
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="afterword_engine.backups"):
        asyncio.run(locked_scheduler_cycle())

    transient_messages = [record.getMessage() for record in caplog.records]
    assert any(
        "Scheduled identity-registry backup skipped because recovery hold status could not be read"
        in msg
        for msg in transient_messages
    )
    assert any(
        "Scheduled Bookward backup skipped for profile legacy because recovery hold status could not be read"
        in msg
        for msg in transient_messages
    )
    assert not any("recovery hold is active" in msg for msg in transient_messages)

    monkeypatch.setattr(backups, "connect", real_profile_connect)
    monkeypatch.setattr(sqlite3, "connect", real_sqlite_connect)
    assert [item["id"] for item in backups.list_backups()] == [legacy_snap]
    assert [
        item["id"] for item in backups.identity_backup_status()["backups"]
    ] == [auth_snap]

    # Also verify real on-disk SQLite corruption fails closed in both predicates and status readers.
    for suffix in ("", "-wal", "-shm"):
        Path(f"{settings.db}{suffix}").unlink(missing_ok=True)
        Path(f"{profiles.auth_path()}{suffix}").unlink(missing_ok=True)
    Path(settings.db).write_bytes(b"not a valid sqlite file")
    profiles.auth_path().write_bytes(b"not a valid sqlite file")

    assert backups._recovery_hold_value() is True
    assert backups._auth_recovery_hold_value() is True
    assert backups.backup_is_due() is False
    assert backups.auth_backup_is_due() is False
    corrupt_profile_status = backups.backup_status()
    assert corrupt_profile_status["recovery_hold"] is True
    assert corrupt_profile_status["recovery_hold_at"] is None
    assert corrupt_profile_status["next_backup_at"] is None
    corrupt_auth_status = backups.identity_backup_status()
    assert corrupt_auth_status["recovery_hold"] is True
    assert corrupt_auth_status["recovery_hold_at"] is None


def test_populated_databases_without_meta_tables_do_not_trip_recovery_hold(
    tmp_path: Path, monkeypatch
):
    from afterword_engine.database import SCHEMA

    monkeypatch.setattr(settings, "db", str(tmp_path / "afterword.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    monkeypatch.delenv("AFTERWORD_AUTH_PASSWORD", raising=False)

    profiles.initialize_auth()
    profiles.create_first_admin("owner", "owner-password-with-enough-length")
    initialize(seed_demo=False)
    backups.create_backup()
    backups.create_auth_backup()

    # Replace live profile DB with a populated v1 database that predates schema_migrations.
    for suffix in ("", "-wal", "-shm"):
        Path(f"{settings.db}{suffix}").unlink(missing_ok=True)
    with sqlite3.connect(settings.db) as con:
        con.executescript(SCHEMA)
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            ("Existing book", "Author", "test"),
        )
        con.commit()
    with sqlite3.connect(profiles.auth_path()) as con:
        con.execute("DROP TABLE auth_meta")
        con.commit()

    profiles.initialize_auth()
    initialize(seed_demo=False)

    assert backups.auth_recovery_hold_active() is False
    assert backups.recovery_hold_active() is False
    assert row("SELECT 1 FROM reads WHERE title='Existing book'") is not None


def test_dismiss_auth_hold_uses_database_lock_and_lifespan_recovers_before_auth_init(
    tmp_path: Path, monkeypatch
):
    import afterword_engine.main as main_mod

    monkeypatch.setattr(settings, "db", str(tmp_path / "afterword.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    monkeypatch.delenv("AFTERWORD_AUTH_PASSWORD", raising=False)

    profiles.initialize_auth()
    profiles.create_first_admin("owner", "owner-password-with-enough-length")
    auth_snap = backups.create_auth_backup()["id"]
    _wipe_live_data_volume(tmp_path)

    # If an interrupted legacy restore fails during lifespan startup, legacy
    # recover_interrupted_restore() runs before initialize_auth() and skips
    # initializing the legacy profile while #290's startup isolation lets auth boot.
    fail_events: list[tuple[str, str | None]] = []
    real_init_auth = profiles.initialize_auth
    real_init_db = main_mod.initialize

    async def noop_worker(_handler, stop):
        await stop.wait()

    async def noop_loop(stop):
        await stop.wait()

    def fail_interrupted_restore():
        from afterword_engine.tenancy import current_profile_id

        fail_events.append(("recover", current_profile_id()))
        raise backups.BackupError("simulated interrupted restore failure")

    def record_fail_init_auth():
        fail_events.append(("initialize_auth", None))
        return real_init_auth()

    def record_fail_init_db():
        from afterword_engine.tenancy import current_profile_id

        fail_events.append(("initialize_db", current_profile_id()))
        return real_init_db()

    monkeypatch.setattr(main_mod, "worker_loop", noop_worker)
    monkeypatch.setattr(main_mod, "source_scheduler_loop", noop_loop)
    monkeypatch.setattr(main_mod, "digest_scheduler_loop", noop_loop)
    monkeypatch.setattr(main_mod, "goodreads_rss_scheduler_loop", noop_loop)
    monkeypatch.setattr(main_mod, "backup_scheduler_loop", noop_loop)
    monkeypatch.setattr(
        main_mod, "recover_interrupted_restore", fail_interrupted_restore
    )
    monkeypatch.setattr(profiles, "initialize_auth", record_fail_init_auth)
    monkeypatch.setattr(main_mod, "initialize", record_fail_init_db)

    async def run_failing_lifespan():
        async with lifespan(app):
            pass

    asyncio.run(run_failing_lifespan())
    assert fail_events == [("recover", "legacy"), ("initialize_auth", None)]
    assert not (tmp_path / "afterword.db").exists()

    _wipe_live_data_volume(tmp_path)
    monkeypatch.setattr(main_mod, "initialize", real_init_db)

    # Verify ordering on normal startup: recover_interrupted_restore("legacy") runs before initialize_auth().
    events: list[tuple[str, str | None]] = []

    def record_recover():
        from afterword_engine.tenancy import current_profile_id

        events.append(("recover", current_profile_id()))
        return False

    def record_init_auth():
        events.append(("initialize_auth", None))
        return real_init_auth()

    monkeypatch.setattr(main_mod, "recover_interrupted_restore", record_recover)
    monkeypatch.setattr(profiles, "initialize_auth", record_init_auth)

    async def run_healthy_lifespan():
        async with lifespan(app):
            pass

    asyncio.run(run_healthy_lifespan())
    assert events[:2] == [("recover", "legacy"), ("initialize_auth", None)]
    assert backups.auth_recovery_hold_active() is True
    assert [
        item["id"] for item in backups.identity_backup_status()["backups"]
    ] == [auth_snap]

    # Verify dismiss_auth_recovery_hold acquires database_lock(auth_path()).
    locked_paths: list[Path] = []
    real_database_lock = backups.database_lock

    class TrackingLockWrapper:
        def __init__(self, path):
            self._path = Path(path)
            self._lock = real_database_lock(path)

        def __enter__(self):
            self._lock.__enter__()
            locked_paths.append(self._path)
            return self._lock

        def __exit__(self, exc_type, exc_val, exc_tb):
            return self._lock.__exit__(exc_type, exc_val, exc_tb)

    monkeypatch.setattr(backups, "database_lock", TrackingLockWrapper)
    dismissed = backups.dismiss_auth_recovery_hold(confirm=True)
    assert dismissed["recovery_hold"] is False
    assert locked_paths == [profiles.auth_path()]


def test_restore_while_rebuild_embeddings_awaits_vectors_preserves_restored_embeddings(
    database, monkeypatch
):
    profiles.initialize_auth()
    reads_list = rows("SELECT * FROM reads ORDER BY id")
    candidates_list = rows("SELECT * FROM candidates ORDER BY id")
    expected_total = len(reads_list) + len(candidates_list)
    assert expected_total >= 8

    with transaction() as con:
        con.execute("DELETE FROM embeddings")
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value,secret) VALUES('embedding_backend','ollama',0)"
        )
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value,secret) VALUES('embedding_model','nomic-embed-text',0)"
        )
        for entity_type, items in (("read", reads_list), ("candidate", candidates_list)):
            for item in items:
                vector = [0.25] * 4
                con.execute(
                    "INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        item["id"],
                        "ollama",
                        "nomic-embed-text",
                        vector_blob(vector),
                        len(vector),
                        content_hash(scoring.document(item)),
                    ),
                )
    snapshot = backups.create_backup()

    with transaction() as con:
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value,secret) VALUES('embedding_backend','openai',0)"
        )
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value,secret) VALUES('embedding_model','text-embedding-3-small',0)"
        )

    awaiting_vectors = asyncio.Event()
    release_vectors = asyncio.Event()

    class SlowEmbedder:
        name = "openai"
        model = "text-embedding-3-small"

        async def embed(self, texts):
            awaiting_vectors.set()
            await release_vectors.wait()
            return [[0.75] * 4 for _ in texts]

    monkeypatch.setattr(scoring, "get_embedder", lambda *_args, **_kwargs: SlowEmbedder())

    # 1. Worker-managed rebuild_embeddings is canceled during restore quiescence.
    enqueue_job("rebuild_embeddings", dedupe=True)
    stop = asyncio.Event()

    async def run_worker_restore():
        worker = asyncio.create_task(worker_loop(handle_job, stop))
        await asyncio.wait_for(awaiting_vectors.wait(), timeout=2)
        await asyncio.to_thread(backups.restore_backup, snapshot["id"])
        stop.set()
        release_vectors.set()
        await asyncio.wait_for(worker, timeout=2)

    asyncio.run(run_worker_restore())

    stored = rows("SELECT entity_type,entity_id,backend,model FROM embeddings")
    assert len(stored) == expected_total
    assert {(item["backend"], item["model"]) for item in stored} == {
        ("ollama", "nomic-embed-text")
    }
    assert private_setting("embedding_backend") == "ollama"
    assert current_restore_epoch() == 1
    assert rows("SELECT id FROM jobs WHERE status IN ('queued','running')") == []

    # 2. Even if an unfenced coroutine/thread resumes from await after restore swaps
    #    an older snapshot (epoch 0 -> 2), transaction() blocks all writes by epoch.
    awaiting_vectors = asyncio.Event()
    release_vectors = asyncio.Event()
    with transaction() as con:
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value,secret) VALUES('embedding_backend','openai',0)"
        )
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value,secret) VALUES('embedding_model','text-embedding-3-small',0)"
        )

    async def run_late_resumed_rebuild():
        rebuild_task = asyncio.create_task(handle_job("rebuild_embeddings"))
        await asyncio.wait_for(awaiting_vectors.wait(), timeout=2)
        await asyncio.to_thread(backups.restore_backup, snapshot["id"])
        release_vectors.set()
        with pytest.raises(RestoreEpochError):
            await asyncio.wait_for(rebuild_task, timeout=2)

    asyncio.run(run_late_resumed_rebuild())

    stored_after = rows("SELECT entity_type,entity_id,backend,model FROM embeddings")
    assert len(stored_after) == expected_total
    assert {(item["backend"], item["model"]) for item in stored_after} == {
        ("ollama", "nomic-embed-text")
    }
    assert current_restore_epoch() == 2


def test_restore_and_startup_recovery_preserve_queued_jobs_and_requeue_running_rows(database):
    today = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
    created_iso = today.isoformat()
    with transaction() as con:
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value,secret) VALUES('goodreads_rss_url','https://www.goodreads.com/review/list_rss/1',0)"
        )
        con.execute(
            "INSERT INTO jobs(id,kind,status,created_at) VALUES('job-queued','digest:manual','queued',?)",
            (created_iso,),
        )
        con.execute(
            "INSERT INTO jobs(id,kind,status,started_at,heartbeat_at,lease_token,attempts,created_at) "
            "VALUES('job-running','goodreads_rss','running',?,?,?,1,?)",
            (created_iso, created_iso, "stale-lease", created_iso),
        )
        con.execute(
            "INSERT INTO jobs(id,kind,status,result,finished_at,created_at) "
            "VALUES('job-complete','score','complete','{\"scored\": 4}',?,?)",
            (created_iso, created_iso),
        )
        con.execute(
            "INSERT INTO jobs(id,kind,status,error,attempts,finished_at,created_at) "
            "VALUES('job-failed','sync','failed','network timeout',3,?,?)",
            (created_iso, created_iso),
        )
    snapshot = backups.create_backup()

    with transaction() as con:
        con.execute("DELETE FROM jobs")
        con.execute(
            "INSERT INTO jobs(id,kind,status,created_at) VALUES('safety-queued','sync','queued',?)",
            (created_iso,),
        )
        con.execute(
            "INSERT INTO jobs(id,kind,status,started_at,lease_token,attempts,created_at) "
            "VALUES('safety-running','score','running',?,'live-lease',1,?)",
            (created_iso, created_iso),
        )
        con.execute(
            "INSERT INTO jobs(id,kind,status,result,finished_at,created_at) "
            "VALUES('safety-complete','goodreads_rss','complete','{}',?,?)",
            (created_iso, created_iso),
        )
        con.execute(
            "INSERT INTO jobs(id,kind,status,error,attempts,finished_at,created_at) "
            "VALUES('safety-failed','digest','failed','smtp down',3,?,?)",
            (created_iso, created_iso),
        )

    result = backups.restore_backup(snapshot["id"])
    assert current_restore_epoch() == 1
    restored_jobs = {
        item["id"]: item
        for item in rows(
            "SELECT id,status,started_at,heartbeat_at,lease_token,attempts FROM jobs"
        )
    }
    assert {job_id: item["status"] for job_id, item in restored_jobs.items()} == {
        "job-queued": "queued",
        "job-running": "queued",
        "job-complete": "complete",
        "job-failed": "failed",
    }
    assert restored_jobs["job-running"]["started_at"] is None
    assert restored_jobs["job-running"]["heartbeat_at"] is None
    assert restored_jobs["job-running"]["lease_token"] is None
    assert restored_jobs["job-running"]["attempts"] == 0
    # Requeuing the interrupted goodreads_rss row preserves pending work without
    # waiting for a dead lease to expire.
    scheduled_job_id = queue_goodreads_rss_if_due(today)
    assert scheduled_job_id == "job-running"

    # Simulate an interrupted restore rolling back to safety_backup on startup.
    safety_path = database / "backups" / f"{result['safety_backup']['id']}.sqlite3"
    backups._write_restore_journal(safety_path, safety_path)
    assert backups.recover_interrupted_restore() is True
    assert current_restore_epoch() == 2
    recovered_jobs = {
        item["id"]: item
        for item in rows(
            "SELECT id,status,started_at,heartbeat_at,lease_token,attempts FROM jobs"
        )
    }
    assert {job_id: item["status"] for job_id, item in recovered_jobs.items()} == {
        "safety-queued": "queued",
        "safety-running": "queued",
        "safety-complete": "complete",
        "safety-failed": "failed",
    }
    assert recovered_jobs["safety-running"]["started_at"] is None
    assert recovered_jobs["safety-running"]["heartbeat_at"] is None
    assert recovered_jobs["safety-running"]["lease_token"] is None
    assert recovered_jobs["safety-running"]["attempts"] == 0


def test_failed_restore_prechecks_preserve_active_jobs_and_restore_epoch(database):
    profiles.initialize_auth()
    with transaction() as con:
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("precheck_secret", seal("live secret")),
        )
    valid_backup = backups.create_backup()
    key_sidecar = database / "backups" / f"{valid_backup['id']}.secret.key"
    original_key = key_sidecar.read_bytes()
    key_sidecar.write_bytes(Fernet.generate_key())
    key_sidecar.chmod(0o600)

    corrupt_id = "bookward-20260102T000000Z-badbad00"
    corrupt_snapshot = database / "backups" / f"{corrupt_id}.sqlite3"
    with sqlite3.connect(corrupt_snapshot) as con:
        con.execute("CREATE TABLE not_bookward (id INTEGER)")

    job_id = enqueue_job("keep_running")
    started = asyncio.Event()
    release = asyncio.Event()
    stop = asyncio.Event()
    cancelled = False

    async def active_handler(kind):
        nonlocal cancelled
        assert kind == "keep_running"
        started.set()
        try:
            await release.wait()
            stop.set()
            return {"preserved": True}
        except asyncio.CancelledError:
            cancelled = True
            raise

    async def run():
        worker = asyncio.create_task(worker_loop(active_handler, stop))
        await asyncio.wait_for(started.wait(), timeout=2)
        try:
            for bad_id in ("../invalid", corrupt_id, valid_backup["id"]):
                with pytest.raises(backups.BackupError):
                    await asyncio.to_thread(backups.restore_backup, bad_id)
            assert cancelled is False
            assert current_restore_epoch() == 0
        finally:
            release.set()
            await asyncio.wait_for(worker, timeout=2)
            key_sidecar.write_bytes(original_key)

    asyncio.run(run())

    assert cancelled is False
    assert current_restore_epoch() == 0
    finished = row("SELECT status,result FROM jobs WHERE id=?", (job_id,))
    assert finished == {"status": "complete", "result": '{"preserved": true}'}


def test_restore_lock_acquire_timeout_on_stuck_connection_clears_restoring_state(
    database, monkeypatch
):
    profiles.initialize_auth()
    monkeypatch.setattr(db_module, "RESTORE_QUIESCENCE_TIMEOUT_SECONDS", 0.05)
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES('Live read','Reader','test')"
        )
    snapshot = backups.create_backup()
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES('After snapshot','Reader','test')"
        )

    # Direct acquire(deadline) timeout raises BackupError and wakes waiting readers.
    lock = database_lock()
    held_ready = threading.Event()
    release_held = threading.Event()

    def hold_open_connection():
        con = connect()
        try:
            held_ready.set()
            release_held.wait(timeout=5)
        finally:
            con.close()

    holder = threading.Thread(target=hold_open_connection, daemon=True)
    holder.start()
    assert held_ready.wait(timeout=2)
    try:
        with pytest.raises(
            backups.BackupError, match="could not be safely closed for restore"
        ):
            lock.acquire(time.monotonic() + 0.03)
        assert not lock.is_locked()

        # Full restore_backup times out at quiesce's deadline, resets _restoring_count,
        # and leaves worker_loop able to process jobs once the stuck connection closes.
        stop = asyncio.Event()
        job_done = asyncio.Event()

        async def handler(kind):
            assert kind == "post_timeout_job"
            job_done.set()
            stop.set()
            return {"ok": True}

        async def run():
            worker = asyncio.create_task(worker_loop(handler, stop))
            try:
                started_at = time.monotonic()
                with pytest.raises(
                    backups.BackupError, match="could not be safely closed for restore"
                ):
                    await asyncio.to_thread(backups.restore_backup, snapshot["id"])
                assert time.monotonic() - started_at < 1.5
                assert not lock.is_restoring()
                assert not lock.is_locked()
                assert current_restore_epoch() == 0

                release_held.set()
                await asyncio.to_thread(holder.join, 2.0)
                job_id = enqueue_job("post_timeout_job")
                await asyncio.wait_for(job_done.wait(), timeout=2)
                await asyncio.wait_for(worker, timeout=2)
                return job_id
            finally:
                stop.set()
                release_held.set()

        completed_job_id = asyncio.run(run())
    finally:
        release_held.set()
        holder.join(timeout=2)

    assert row("SELECT 1 FROM reads WHERE title='After snapshot'") is not None
    assert row("SELECT status FROM jobs WHERE id=?", (completed_job_id,)) == {
        "status": "complete"
    }


def test_quiesce_timeout_with_live_jobs_finalizes_status_or_fences_post_swap_writes(
    database, monkeypatch
):
    profiles.initialize_auth()
    monkeypatch.setattr(db_module, "RESTORE_QUIESCENCE_TIMEOUT_SECONDS", 0.05)
    snapshot = backups.create_backup()

    # 1 & 2: Handler outlives quiesce while holding an open connection so restore fails
    #        with BackupError; once the handler finishes (complete or exception) after
    #        job_aborted was set, worker_loop records terminal status instead of leaving
    #        the row stuck in 'running'.
    for mode in ("complete", "fail"):
        job_id = enqueue_job(f"uninterruptible_{mode}")
        in_handler = asyncio.Event()
        finish_handler = threading.Event()
        stop = asyncio.Event()

        def blocking_db_work():
            with connect() as con:
                con.execute("SELECT 1").fetchone()
                finish_handler.wait(timeout=5)

        async def uninterruptible_handler(kind):
            in_handler.set()
            try:
                await asyncio.shield(asyncio.to_thread(blocking_db_work))
            except asyncio.CancelledError:
                await asyncio.to_thread(finish_handler.wait, 5.0)
            stop.set()
            if kind.endswith("_fail"):
                raise RuntimeError("boom after abort")
            return {"mode": mode}

        async def run_case():
            worker = asyncio.create_task(worker_loop(uninterruptible_handler, stop))
            try:
                await asyncio.wait_for(in_handler.wait(), timeout=2)
                with pytest.raises(backups.BackupError):
                    await asyncio.to_thread(backups.restore_backup, snapshot["id"])
                finish_handler.set()
                await asyncio.wait_for(worker, timeout=2)
            finally:
                finish_handler.set()
                stop.set()

        asyncio.run(run_case())
        recorded = row("SELECT status,result,error FROM jobs WHERE id=?", (job_id,))
        if mode == "complete":
            assert recorded["status"] == "complete"
            assert recorded["result"] == '{"mode": "complete"}'
        else:
            assert recorded["status"] == "failed"
            assert "boom after abort" in (recorded["error"] or "")

    # 3: Connectionless handler ignores cancellation past quiesce(0.05) so restore swaps
    #    the DB to epoch 1 while the job is still running; late writes raise
    #    RestoreEpochError and completion does not touch the restored DB.
    job_id = enqueue_job("late_after_swap")
    started = asyncio.Event()
    allow_resume = asyncio.Event()
    late_epoch_error = asyncio.Event()
    stop = asyncio.Event()

    async def connectionless_uninterruptible(kind):
        started.set()
        try:
            await allow_resume.wait()
        except asyncio.CancelledError:
            await allow_resume.wait()
        try:
            with transaction() as con:
                con.execute(
                    "INSERT INTO reads(title,author,source) VALUES('Stale post-quiesce','Reader','test')"
                )
        except RestoreEpochError:
            late_epoch_error.set()
        stop.set()
        return {"late": True}

    async def run_post_swap():
        worker = asyncio.create_task(worker_loop(connectionless_uninterruptible, stop))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            await asyncio.to_thread(backups.restore_backup, snapshot["id"])
            assert current_restore_epoch() == 1
            allow_resume.set()
            await asyncio.wait_for(worker, timeout=2)
        finally:
            allow_resume.set()
            stop.set()

    asyncio.run(run_post_swap())
    assert late_epoch_error.is_set()
    assert row("SELECT 1 FROM reads WHERE title='Stale post-quiesce'") is None
    assert row("SELECT id FROM jobs WHERE id=?", (job_id,)) is None

    # 4: Connectionless handler outlives quiesce(0.05) while restore fails in
    #    _replace_live_database and rolls back to safety_backup: the job's row
    #    (captured as 'running' in safety_backup) is requeued instead of deleted,
    #    the stale run's write/completion is fenced, and worker_loop completes
    #    the requeued job on the rolled-back epoch.
    rollback_job_id = enqueue_job("outlived_rollback")
    first_run_started = asyncio.Event()
    release_first_run = asyncio.Event()
    stale_write_fenced = asyncio.Event()
    requeued_run_done = asyncio.Event()
    stop = asyncio.Event()
    run_number = 0
    real_replace = backups._replace_live_database
    swap_calls = 0

    def fail_primary_swap(staged: Path, key_source: Path | None = None, **kwargs) -> None:
        nonlocal swap_calls
        swap_calls += 1
        if swap_calls == 1:
            raise OSError("simulated swap failure after quiesce timeout")
        real_replace(staged, key_source, **kwargs)

    monkeypatch.setattr(backups, "_replace_live_database", fail_primary_swap)

    async def rollback_outliving_handler(kind):
        nonlocal run_number
        assert kind == "outlived_rollback"
        run_number += 1
        if run_number == 1:
            first_run_started.set()
            try:
                await release_first_run.wait()
            except asyncio.CancelledError:
                await release_first_run.wait()
            try:
                with transaction() as con:
                    con.execute(
                        "INSERT INTO reads(title,author,source) VALUES('Stale rollback write','Reader','test')"
                    )
            except RestoreEpochError:
                stale_write_fenced.set()
            return {"stale": True}
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES('Requeued after rollback','Reader','test')"
            )
        requeued_run_done.set()
        stop.set()
        return {"requeued": True}

    async def run_rollback_outlived():
        worker = asyncio.create_task(worker_loop(rollback_outliving_handler, stop))
        try:
            await asyncio.wait_for(first_run_started.wait(), timeout=2)
            with pytest.raises(backups.BackupError, match="previous database remains active"):
                await asyncio.to_thread(backups.restore_backup, snapshot["id"])
            assert swap_calls == 2
            assert current_restore_epoch() == 2
            requeued_state = row(
                "SELECT status,lease_token,attempts FROM jobs WHERE id=?",
                (rollback_job_id,),
            )
            assert requeued_state == {
                "status": "queued",
                "lease_token": None,
                "attempts": 0,
            }
            release_first_run.set()
            await asyncio.wait_for(requeued_run_done.wait(), timeout=2)
            await asyncio.wait_for(worker, timeout=2)
        finally:
            release_first_run.set()
            stop.set()

    try:
        asyncio.run(run_rollback_outlived())
    finally:
        monkeypatch.setattr(backups, "_replace_live_database", real_replace)

    assert stale_write_fenced.is_set()
    assert row("SELECT 1 FROM reads WHERE title='Stale rollback write'") is None
    assert row("SELECT 1 FROM reads WHERE title='Requeued after rollback'") is not None
    final_job = row("SELECT status,result,attempts FROM jobs WHERE id=?", (rollback_job_id,))
    assert final_job == {
        "status": "complete",
        "result": '{"requeued": true}',
        "attempts": 1,
    }


def test_rollback_after_replace_failure_advances_epoch_migrates_schema_and_preserves_queued_jobs(
    database, monkeypatch
):
    selected = _prepare_distinct_backup_and_live_state(database)
    created_iso = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc).isoformat()
    with transaction() as con:
        con.execute(
            "INSERT INTO jobs(id,kind,status,created_at) VALUES('live-queued','sync','queued',?)",
            (created_iso,),
        )
        con.execute(
            "INSERT INTO jobs(id,kind,status,started_at,lease_token,attempts,created_at) "
            "VALUES('live-running','score','running',?,'stale-lease',1,?)",
            (created_iso, created_iso),
        )

    pre_restore_epoch = current_restore_epoch()
    assert pre_restore_epoch == 0

    # Simulate an older-schema safety snapshot and a pre-rename swap failure.
    real_create_snapshot = backups._create_snapshot_unlocked
    real_replace = backups._replace_live_database

    def create_older_schema_safety_snapshot() -> Path:
        safety_path = real_create_snapshot()
        with sqlite3.connect(safety_path) as con:
            con.execute("DROP INDEX IF EXISTS idx_goodreads_shelf_items_title_author")
            con.execute("DROP INDEX IF EXISTS idx_goodreads_shelf_items_shelf")
            con.execute("DROP TABLE IF EXISTS goodreads_shelf_items")
            con.execute("DELETE FROM schema_migrations WHERE version IN (24, 25, 26)")
            con.commit()
        return safety_path

    calls = 0

    def fail_first_swap(
        staged: Path, key_source: Path | None = None, **kwargs
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated pre-rename swap failure")
        real_replace(staged, key_source, **kwargs)

    monkeypatch.setattr(
        backups, "_create_snapshot_unlocked", create_older_schema_safety_snapshot
    )
    monkeypatch.setattr(backups, "_replace_live_database", fail_first_swap)

    with pytest.raises(backups.BackupError, match="previous database remains active"):
        backups.restore_backup(selected["id"])

    assert calls == 2
    assert current_restore_epoch() == 1
    with bind_restore_epoch(pre_restore_epoch):
        with pytest.raises(RestoreEpochError):
            with transaction() as con:
                con.execute(
                    "INSERT INTO reads(title,author,source) VALUES('Fenced pre-rollback','Reader','test')"
                )
    assert row("SELECT 1 FROM reads WHERE title='Live before restore'") is not None
    assert row("SELECT 1 FROM reads WHERE title='Fenced pre-rollback'") is None
    rolled_back_jobs = {
        item["id"]: item
        for item in rows("SELECT id,status,lease_token,attempts FROM jobs")
    }
    assert {job_id: item["status"] for job_id, item in rolled_back_jobs.items()} == {
        "live-queued": "queued",
        "live-running": "queued",
    }
    assert rolled_back_jobs["live-running"]["lease_token"] is None
    assert rolled_back_jobs["live-running"]["attempts"] == 0
    assert row("SELECT MAX(version) AS v FROM schema_migrations")["v"] == MIGRATIONS[-1][0]
    assert (
        row(
            "SELECT 1 AS ok FROM sqlite_master WHERE type='table' AND name='goodreads_shelf_items'"
        )
        is not None
    )

    # Also verify post-rename failure inside _replace_live_database advances rollback
    # epoch past the staged snapshot's epoch.
    selected_path = backups._backup_path(selected["id"])
    con = sqlite3.connect(selected_path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value,secret) VALUES('restore_epoch','4',0)"
        )
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()

    post_rename_calls = 0

    def fail_after_rename(
        staged: Path, key_source: Path | None = None, **kwargs
    ) -> None:
        nonlocal post_rename_calls
        post_rename_calls += 1
        real_replace(staged, key_source, **kwargs)
        if post_rename_calls == 1:
            raise OSError("simulated post-rename directory fsync failure")

    monkeypatch.setattr(backups, "_create_snapshot_unlocked", real_create_snapshot)
    monkeypatch.setattr(backups, "_replace_live_database", fail_after_rename)

    with pytest.raises(backups.BackupError, match="previous database remains active"):
        backups.restore_backup(selected["id"])

    assert post_rename_calls == 2
    # Staged snapshot carried epoch 5 (max(1, 4) + 1), so rollback advances to epoch 6.
    assert current_restore_epoch() == 6
    for stale_epoch in (1, 5):
        with bind_restore_epoch(stale_epoch):
            with pytest.raises(RestoreEpochError):
                with transaction() as con:
                    con.execute(
                        "INSERT INTO reads(title,author,source) VALUES('Stale epoch write','Reader','test')"
                    )


def test_in_flight_api_writes_during_restore_return_conflict_without_mutating_restored_db(
    database, monkeypatch
):
    headers = authenticated_headers()
    with transaction() as con:
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value,secret) "
            "VALUES('librarr_url','http://snapshot.librarr.local:8080',0)"
        )
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value,secret) "
            "VALUES('embedding_backend','ollama',0)"
        )
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value,secret) "
            "VALUES('embedding_model','nomic-embed-text',0)"
        )
    snapshot = backups.create_backup()

    with transaction() as con:
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value,secret) "
            "VALUES('librarr_url','http://stale-live.librarr.local:8080',0)"
        )

    from afterword_engine import main as main_module

    async def run():
        health_entered = asyncio.Event()
        release_health = asyncio.Event()

        class PausingEmbedder:
            model = "mxbai-embed-large"

            async def health(self):
                health_entered.set()
                await release_health.wait()
                return {"ok": True, "checked": True, "model": self.model}

        monkeypatch.setattr(
            main_module, "get_embedder", lambda *_args, **_kwargs: PausingEmbedder()
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", headers=headers
        ) as client:
            settings_task = asyncio.create_task(
                client.put(
                    "/api/settings",
                    json={
                        "embedding_backend": "ollama",
                        "embedding_model": "mxbai-embed-large",
                        "embedding_url": "http://127.0.0.1:11434",
                    },
                )
            )
            await asyncio.wait_for(health_entered.wait(), timeout=2)

            restore_resp = await client.post(
                f"/api/backups/{snapshot['id']}/restore", json={}
            )
            assert restore_resp.status_code == 200

            release_health.set()
            conflict_resp = await asyncio.wait_for(settings_task, timeout=2)
            assert conflict_resp.status_code == 409
            assert "restored" in conflict_resp.json()["detail"].lower()

            # A fresh post-restore write succeeds on the new epoch.
            fresh_resp = await client.put(
                "/api/settings",
                json={"librarr_media_type": "ebook"},
            )
            assert fresh_resp.status_code == 200

    asyncio.run(run())

    assert private_setting("librarr_url") == "http://snapshot.librarr.local:8080"
    assert private_setting("embedding_backend") == "ollama"
    assert private_setting("librarr_media_type") == "ebook"
    assert current_restore_epoch() == 1


def test_unexpected_recovery_error_returns_static_503_detail(database, monkeypatch):
    headers = authenticated_headers()
    journal_path = backups._restore_journal_path()
    journal_path.write_text(
        '{"safety_backup": "bookward-20260101T000000Z-deadbeef"}', encoding="utf-8"
    )
    journal_path.chmod(0o600)
    try:
        monkeypatch.setattr(
            "afterword_engine.main.recover_interrupted_restore",
            lambda: (_ for _ in ()).throw(
                RuntimeError("unexpected internal traceback detail")
            ),
        )
        client = TestClient(app, headers=headers)
        resp = client.get("/api/overview")
        assert resp.status_code == 503
        assert resp.json()["detail"] == backups.RESTORE_FENCE_DETAIL
        assert "unexpected internal traceback detail" not in resp.text
    finally:
        journal_path.unlink(missing_ok=True)


def test_backup_scheduler_loop_runs_due_checks_off_event_loop_thread(
    database, monkeypatch
):
    profiles.initialize_auth()
    alice = profiles.create_account("sched-alice", "sched-alice-password-with-length")
    bob = profiles.create_account("sched-bob", "sched-bob-password-with-length")
    from afterword_engine.tenancy import profile_scope

    for profile_id in ("legacy", alice["profile_id"], bob["profile_id"]):
        with profile_scope(profile_id):
            initialize(seed_demo=False)

    loop_thread_id: int | None = None
    observed_threads: dict[str, set[int]] = {
        "auth_due": set(),
        "profile_ids": set(),
        "profile_due": set(),
    }
    checked_profiles: list[str] = []
    stop = asyncio.Event()

    orig_profile_ids = profiles.profile_ids

    def spy_auth_due(now=None):
        observed_threads["auth_due"].add(threading.get_ident())
        return False

    def spy_profile_ids(*args, **kwargs):
        observed_threads["profile_ids"].add(threading.get_ident())
        return orig_profile_ids(*args, **kwargs)

    def spy_backup_is_due(now=None):
        observed_threads["profile_due"].add(threading.get_ident())
        from afterword_engine.tenancy import current_profile_id

        checked_profiles.append(current_profile_id() or "legacy")
        if len(checked_profiles) >= 2:
            stop.set()
        return False

    monkeypatch.setattr(backups, "auth_backup_is_due", spy_auth_due)
    monkeypatch.setattr(profiles, "profile_ids", spy_profile_ids)
    monkeypatch.setattr(backups, "backup_is_due", spy_backup_is_due)

    # Hold an exclusive lock on Alice so backup_scheduler_loop skips Alice without blocking.
    alice_lock = database_lock(backups.profile_database_path(alice["profile_id"]))
    locked_ready = threading.Event()
    release_lock = threading.Event()

    def hold_alice():
        with alice_lock:
            locked_ready.set()
            release_lock.wait(timeout=5)

    holder = threading.Thread(target=hold_alice, daemon=True)
    holder.start()
    assert locked_ready.wait(timeout=2)

    orig_wait_for = asyncio.wait_for

    async def fast_initial_wait(coro, timeout):
        return await orig_wait_for(coro, 0.01 if timeout == 5 else timeout)

    monkeypatch.setattr(backups.asyncio, "wait_for", fast_initial_wait)

    async def run():
        nonlocal loop_thread_id
        loop_thread_id = threading.get_ident()
        await orig_wait_for(backups.backup_scheduler_loop(stop), timeout=2)

    try:
        asyncio.run(run())
    finally:
        stop.set()
        release_lock.set()
        holder.join(timeout=2)

    assert loop_thread_id is not None
    assert observed_threads["auth_due"] and loop_thread_id not in observed_threads["auth_due"]
    assert observed_threads["profile_ids"] and loop_thread_id not in observed_threads["profile_ids"]
    assert observed_threads["profile_due"] and loop_thread_id not in observed_threads["profile_due"]
    assert alice["profile_id"] not in checked_profiles
    assert set(checked_profiles) == {"legacy", bob["profile_id"]}
