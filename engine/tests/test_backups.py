import logging
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth_helpers import authenticated_headers
from cryptography.fernet import Fernet

from afterword_engine import backups
from afterword_engine.config import settings
from afterword_engine.database import initialize, private_setting, row, transaction
from afterword_engine.main import app
from afterword_engine.secrets import seal


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
        env_safety = backups.create_backup()
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


def test_backups_and_restores_never_delay_health_or_other_profiles_while_same_profile_waits(
    database, monkeypatch
):
    import asyncio
    from threading import Event

    import httpx

    from afterword_engine import profiles
    from afterword_engine.database import database_lock
    from afterword_engine.jobs import worker_loop
    from afterword_engine.tenancy import profile_database_path, profile_scope

    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    profiles.initialize_auth()
    if profiles.account_count() == 0:
        profiles.create_first_admin("backup-owner", "backup-owner-password-long")
    alice = profiles.create_account("backup-alice", "backup-alice-password-long")
    bob = profiles.create_account("backup-bob", "backup-bob-password-long")
    alice_session, _ = profiles.create_session(alice["id"])
    bob_session, _ = profiles.create_session(bob["id"])
    alice_headers = {"x-bookward-session": alice_session}
    bob_headers = {"x-bookward-session": bob_session}

    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,rating,source) VALUES(?,?,?,?)",
                ("Alice snapshot book", "Ursula K. Le Guin", 5, "test"),
            )
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
                ("alice_secret", seal("alice-key-material")),
            )
            alice_source_id = con.execute(
                "INSERT INTO sources(name,url,enabled,weight) VALUES(?,?,1,1)",
                ("Alice Source", "https://example.test/alice-feed"),
            ).lastrowid
            alice_candidate_id = con.execute(
                "INSERT INTO candidates(source_id,title,author,normalized_key,status) "
                "VALUES(?,?,?,?,?)",
                (
                    alice_source_id,
                    "Write during staging",
                    "Concurrent Reader",
                    "write during staging::concurrent reader",
                    "saved",
                ),
            ).lastrowid

    with profile_scope(bob["profile_id"]):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,rating,source) VALUES(?,?,?,?)",
                ("Bob independent book", "Octavia E. Butler", 5, "test"),
            )
            bob_source_id = con.execute(
                "INSERT INTO sources(name,url,enabled,weight) VALUES(?,?,1,1)",
                ("Bob Source", "https://example.test/bob-feed"),
            ).lastrowid
            bob_candidate_id = con.execute(
                "INSERT INTO candidates(source_id,title,author,normalized_key,status) "
                "VALUES(?,?,?,?,?)",
                (
                    bob_source_id,
                    "Bob concurrent write",
                    "Octavia E. Butler",
                    "bob concurrent write::octavia e. butler",
                    "saved",
                ),
            ).lastrowid

    alice_path = profile_database_path(alice["profile_id"])
    original_snapshot = backups._create_snapshot_unlocked
    original_copy_to_stage = backups._copy_to_stage
    original_replace_live = backups._replace_live_database

    pause_backup = False
    backup_entered = Event()
    release_backup = Event()

    def controlled_snapshot():
        if pause_backup and profile_database_path() == alice_path:
            backup_entered.set()
            assert release_backup.wait(timeout=5)
        return original_snapshot()

    pause_staging = False
    staging_entered = Event()
    release_staging = Event()

    def controlled_copy_to_stage(source, directory):
        staged = original_copy_to_stage(source, directory)
        if pause_staging and directory == alice_path.parent:
            staging_entered.set()
            assert release_staging.wait(timeout=5)
        return staged

    pause_cutover = False
    cutover_entered = Event()
    release_cutover = Event()

    def controlled_replace_live(staged, key_source=None, **kwargs):
        if pause_cutover and profile_database_path() == alice_path:
            cutover_entered.set()
            assert release_cutover.wait(timeout=5)
        return original_replace_live(staged, key_source, **kwargs)

    async def fake_resolve_book_metadata(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(
        "afterword_engine.ingestion.resolve_book_metadata",
        fake_resolve_book_metadata,
    )
    monkeypatch.setattr(backups, "_create_snapshot_unlocked", controlled_snapshot)
    monkeypatch.setattr(backups, "_copy_to_stage", controlled_copy_to_stage)
    monkeypatch.setattr(backups, "_replace_live_database", controlled_replace_live)

    from afterword_engine import main as main_module
    from afterword_engine.main import handle_job

    rater_loads = 0
    original_rater_inputs = main_module._load_reading_history_rater_inputs

    def counting_rater_inputs():
        nonlocal rater_loads
        rater_loads += 1
        return original_rater_inputs()

    monkeypatch.setattr(
        main_module, "_load_reading_history_rater_inputs", counting_rater_inputs
    )

    async def run_integration():
        nonlocal pause_backup, pause_staging, pause_cutover, rater_loads
        stop = asyncio.Event()
        worker = asyncio.create_task(worker_loop(handle_job, stop))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            try:
                # 1. Shared-read backup on Alice must not block health, Bob, or Alice reads.
                pause_backup = True
                create_task = asyncio.create_task(
                    client.post("/api/backups/create", json={}, headers=alice_headers)
                )
                assert await asyncio.to_thread(backup_entered.wait, 3)
                assert not database_lock(alice_path).is_locked()

                (
                    health_res,
                    bob_overview,
                    alice_overview,
                    alice_scores,
                    empty_page_scores,
                ) = await asyncio.wait_for(
                    asyncio.gather(
                        client.get("/api/health"),
                        client.get("/api/overview", headers=bob_headers),
                        client.get("/api/overview", headers=alice_headers),
                        client.get(
                            "/api/reading-history/scores", headers=alice_headers
                        ),
                        client.get(
                            "/api/reading-history/scores?offset=100",
                            headers=alice_headers,
                        ),
                    ),
                    timeout=2,
                )
                assert health_res.status_code == 200
                assert health_res.json() == {"ok": True}
                assert bob_overview.status_code == 200
                assert alice_overview.status_code == 200
                assert alice_scores.status_code == 200
                assert len(alice_scores.json()) == 1
                assert empty_page_scores.status_code == 200
                assert empty_page_scores.json() == []
                # Only the non-empty page should load rated_reads / embedder inputs.
                assert rater_loads == 1

                pause_backup = False
                release_backup.set()
                created = await asyncio.wait_for(create_task, timeout=5)
                assert created.status_code == 200
                backup_id = created.json()["backup"]["id"]

                with profile_scope(alice["profile_id"]), transaction() as con:
                    con.execute(
                        "INSERT INTO reads(title,author,rating,source) VALUES(?,?,?,?)",
                        ("Transient post-backup book", "Temporary Author", 4, "test"),
                    )

                # 2. Restore on Alice holds the exclusive profile lock across staging
                #    and cutover so live writes arriving during staging wait and commit
                #    onto the restored database instead of being silently discarded.
                pause_staging = True
                pause_cutover = True
                restore_task = asyncio.create_task(
                    client.post(
                        f"/api/backups/{backup_id}/restore",
                        json={},
                        headers=alice_headers,
                    )
                )
                assert await asyncio.to_thread(staging_entered.wait, 3)
                assert database_lock(alice_path).is_locked()

                # Start a live write and contended reads on Alice during staging.
                staged_alice_write = asyncio.create_task(
                    client.post(
                        f"/api/recommendations/{alice_candidate_id}/read",
                        json={"rating": 5},
                        headers=alice_headers,
                    )
                )
                contended_scores = asyncio.create_task(
                    client.get("/api/reading-history/scores", headers=alice_headers)
                )
                contended_overview = asyncio.create_task(
                    client.get("/api/overview", headers=alice_headers)
                )

                staged_health, staged_bob, staged_bob_write = await asyncio.wait_for(
                    asyncio.gather(
                        client.get("/api/health"),
                        client.get("/api/overview", headers=bob_headers),
                        client.post(
                            f"/api/recommendations/{bob_candidate_id}/read",
                            json={"rating": 4},
                            headers=bob_headers,
                        ),
                    ),
                    timeout=2,
                )
                assert staged_health.status_code == 200
                assert staged_bob.status_code == 200
                assert staged_bob_write.status_code == 200
                assert not staged_alice_write.done()
                assert not contended_scores.done()
                assert not contended_overview.done()

                pause_staging = False
                release_staging.set()
                assert await asyncio.to_thread(cutover_entered.wait, 5)
                assert database_lock(alice_path).is_locked()

                # Let worker_loop complete a 0.5s poll while Alice is exclusively locked.
                await asyncio.sleep(0.6)
                assert not staged_alice_write.done()
                assert not contended_scores.done()
                assert not contended_overview.done()

                loop = asyncio.get_running_loop()
                started = loop.time()
                cutover_health, cutover_bob_overview, cutover_bob_scores = (
                    await asyncio.wait_for(
                        asyncio.gather(
                            client.get("/api/health"),
                            client.get("/api/overview", headers=bob_headers),
                            client.get(
                                "/api/reading-history/scores", headers=bob_headers
                            ),
                        ),
                        timeout=2,
                    )
                )
                elapsed = loop.time() - started
                assert elapsed < 2.0
                assert cutover_health.status_code == 200
                assert cutover_bob_overview.status_code == 200
                assert cutover_bob_scores.status_code == 200
                assert {
                    item["title"] for item in cutover_bob_overview.json()["history"]
                } == {"Bob independent book", "Bob concurrent write"}

                pause_cutover = False
                release_cutover.set()
                restored_res, write_res, scores_res, overview_res = (
                    await asyncio.wait_for(
                        asyncio.gather(
                            restore_task,
                            staged_alice_write,
                            contended_scores,
                            contended_overview,
                        ),
                        timeout=5,
                    )
                )
                assert restored_res.status_code == 200
                assert write_res.status_code == 200
                assert scores_res.status_code == 200
                assert overview_res.status_code == 200

                final_overview = await asyncio.wait_for(
                    client.get("/api/overview", headers=alice_headers),
                    timeout=2,
                )
                assert final_overview.status_code == 200
                final_titles = {
                    item["title"] for item in final_overview.json()["history"]
                }
                assert "Transient post-backup book" not in final_titles
                assert final_titles == {
                    "Alice snapshot book",
                    "Write during staging",
                }
            finally:
                release_backup.set()
                release_staging.set()
                release_cutover.set()
                stop.set()
                await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run_integration())
    with profile_scope(alice["profile_id"]):
        assert private_setting("alice_secret") == "alice-key-material"


def test_backup_and_restore_failures_leave_profile_unlocked_and_writable(
    database, monkeypatch
):
    from afterword_engine.database import database_lock
    from afterword_engine.tenancy import profile_database_path

    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    live_path = profile_database_path()
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,rating,source) VALUES(?,?,?,?)",
            ("Baseline book", "Ursula K. Le Guin", 5, "test"),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("failure_test_secret", seal("keep-writable")),
        )

    baseline = backups.create_backup()

    def assert_unlocked_and_writable(marker_title: str) -> None:
        assert not database_lock(live_path).is_locked()
        with backups._profile_backup_lock(live_path).try_acquire() as acquired:
            assert acquired is True
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,rating,source) VALUES(?,?,?,?)",
                (marker_title, "Recovery Check", 4, "test"),
            )
        assert row("SELECT 1 FROM reads WHERE title=?", (marker_title,)) is not None

    # 1. Failure inside create_backup releases both backup and shared DB locks.
    original_snapshot = backups._create_snapshot_unlocked
    monkeypatch.setattr(
        backups,
        "_create_snapshot_unlocked",
        lambda: (_ for _ in ()).throw(RuntimeError("injected snapshot failure")),
    )
    with pytest.raises(backups.BackupError, match="injected snapshot failure"):
        backups.create_backup()
    monkeypatch.setattr(backups, "_create_snapshot_unlocked", original_snapshot)
    assert_unlocked_and_writable("Writable after backup failure")

    # 2. Failure during restore staging releases exclusive DB lock and leaves live DB intact.
    original_finalize = backups._finalize_staged_database
    monkeypatch.setattr(
        backups,
        "_finalize_staged_database",
        lambda _staged: (_ for _ in ()).throw(
            backups.BackupError("injected staging failure")
        ),
    )
    with pytest.raises(backups.BackupError, match="injected staging failure"):
        backups.restore_backup(baseline["id"])
    monkeypatch.setattr(backups, "_finalize_staged_database", original_finalize)
    assert (
        row("SELECT 1 FROM reads WHERE title='Writable after backup failure'")
        is not None
    )
    assert_unlocked_and_writable("Writable after staging failure")

    # 3. Failure during cutover after replacing live DB rolls back inside the lock
    #    and leaves the profile unlocked and writable.
    original_clear_journal = backups._clear_restore_journal
    clear_calls = 0

    def fail_first_journal_clear():
        nonlocal clear_calls
        clear_calls += 1
        if clear_calls == 1:
            raise OSError("injected cutover failure")
        original_clear_journal()

    monkeypatch.setattr(backups, "_clear_restore_journal", fail_first_journal_clear)
    with pytest.raises(backups.BackupError, match="previous database remains active"):
        backups.restore_backup(baseline["id"])
    monkeypatch.setattr(backups, "_clear_restore_journal", original_clear_journal)

    assert clear_calls == 2
    assert (
        row("SELECT 1 FROM reads WHERE title='Writable after staging failure'")
        is not None
    )
    assert private_setting("failure_test_secret") == "keep-writable"
    assert_unlocked_and_writable("Writable after cutover rollback")
    assert backups.create_backup()["id"]


def test_cross_profile_restore_key_swap_does_not_corrupt_concurrent_backup_sidecar(
    database, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from cryptography.fernet import Fernet

    from afterword_engine import profiles
    from afterword_engine.secrets import installation_key, installation_key_path
    from afterword_engine.tenancy import profile_scope

    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    profiles.initialize_auth()
    if profiles.account_count() == 0:
        profiles.create_first_admin("key-owner", "key-owner-password-long")
    bob = profiles.create_account("key-bob", "key-bob-password-long")

    with profile_scope("legacy"):
        legacy_key_path = installation_key_path()
        with transaction() as con:
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,1) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=1",
                ("legacy_secret", seal("legacy-secret-value")),
            )

    with profile_scope(bob["profile_id"]):
        initialize(seed_demo=False)
        bob_key_path = installation_key_path()
        assert bob_key_path != legacy_key_path
        assert bob_key_path.name == f"{bob['profile_id']}.sqlite3.secret.key"
        with transaction() as con:
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
                ("bob_secret", seal("bob-original-secret")),
            )
        bob_v1 = backups.create_backup()

        # Rotate Bob's per-profile key and secret so restoring bob_v1 swaps Bob's key file.
        rotated_key = Fernet.generate_key()
        bob_key_path.write_bytes(rotated_key)
        assert installation_key() == rotated_key
        with transaction() as con:
            con.execute(
                "UPDATE settings SET value=? WHERE key='bob_secret'",
                (seal("bob-rotated-secret"),),
            )

    original_write_sidecar = backups._write_generated_key_sidecar
    legacy_sidecar_ready = Event()
    allow_legacy_sidecar = Event()

    def coordinated_write_sidecar(backup_path):
        if installation_key_path() == legacy_key_path:
            legacy_sidecar_ready.set()
            assert allow_legacy_sidecar.wait(timeout=5)
        return original_write_sidecar(backup_path)

    monkeypatch.setattr(
        backups, "_write_generated_key_sidecar", coordinated_write_sidecar
    )

    def backup_legacy():
        with profile_scope("legacy"):
            return backups.create_backup()

    def restore_bob():
        with profile_scope(bob["profile_id"]):
            return backups.restore_backup(bob_v1["id"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        legacy_future = executor.submit(backup_legacy)
        assert legacy_sidecar_ready.wait(timeout=3)
        bob_future = executor.submit(restore_bob)
        assert bob_future.result(timeout=5)["restored_backup"] == bob_v1["id"]
        allow_legacy_sidecar.set()
        legacy_backup = legacy_future.result(timeout=5)

    with profile_scope("legacy"):
        assert private_setting("legacy_secret") == "legacy-secret-value"
        sidecar = backups.backup_directory() / f"{legacy_backup['id']}.secret.key"
        assert sidecar.read_bytes().strip() == installation_key()

    with profile_scope(bob["profile_id"]):
        assert private_setting("bob_secret") == "bob-original-secret"


def test_concurrent_backups_and_restores_serialize_per_profile(database, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from afterword_engine.database import database_lock
    from afterword_engine.tenancy import profile_database_path

    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,rating,source) VALUES(?,?,?,?)",
            ("Serialized state", "Author", 5, "test"),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("serialized_secret", seal("keep-sidecar-aligned")),
        )
    baseline = backups.create_backup()

    monkeypatch.setattr(settings, "backup_retention_count", 6)

    def run_mixed_op(index: int):
        if index % 3 == 0:
            return backups.create_backup()["id"]
        if index % 3 == 1:
            return backups.restore_backup(baseline["id"])["restored_backup"]
        for write_idx in range(3):
            with transaction() as con:
                con.execute(
                    "INSERT INTO reads(title,author,rating,source) VALUES(?,?,?,?)",
                    (f"Concurrent write {index}-{write_idx}", "Writer", 4, "test"),
                )
        return f"writes-{index}"

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(run_mixed_op, idx) for idx in range(6)]
        for future in futures:
            assert future.result(timeout=15)

    monkeypatch.setattr(settings, "backup_retention_count", 2)

    def run_prune_or_write(index: int):
        if index % 2 == 0:
            return backups.create_backup()["id"]
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,rating,source) VALUES(?,?,?,?)",
                (f"Prune concurrent write {index}", "Writer", 5, "test"),
            )
        return f"prune-write-{index}"

    with ThreadPoolExecutor(max_workers=6) as executor:
        prune_futures = [executor.submit(run_prune_or_write, idx) for idx in range(6)]
        for future in prune_futures:
            assert future.result(timeout=15)

    assert not database_lock(profile_database_path()).is_locked()
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,rating,source) VALUES(?,?,?,?)",
            ("Final post-concurrency write", "Author", 5, "test"),
        )
    assert (
        row("SELECT 1 FROM reads WHERE title='Final post-concurrency write'")
        is not None
    )

    retained = backups.list_backups()
    assert len(retained) == 2
    retained_ids = {item["id"] for item in retained}
    backup_dir = database / "backups"
    sidecar_ids = {
        path.name.removesuffix(".secret.key")
        for path in backup_dir.glob("bookward-*.secret.key")
    }
    assert sidecar_ids == retained_ids
    assert private_setting("serialized_secret") == "keep-sidecar-aligned"


def test_job_trigger_routes_run_off_event_loop_during_restore_cutover(
    database, monkeypatch
):
    import asyncio
    from threading import Event

    import httpx

    from afterword_engine import profiles
    from afterword_engine.database import database_lock
    from afterword_engine.tenancy import profile_database_path, profile_scope

    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    profiles.initialize_auth()
    if profiles.account_count() == 0:
        profiles.create_first_admin("route-owner", "route-owner-password-long")
    alice = profiles.create_account("route-alice", "route-alice-password-long")
    bob = profiles.create_account("route-bob", "route-bob-password-long")
    alice_session, _ = profiles.create_session(alice["id"])
    bob_session, _ = profiles.create_session(bob["id"])
    alice_headers = {"x-bookward-session": alice_session}
    bob_headers = {"x-bookward-session": bob_session}

    for account in (alice, bob):
        with profile_scope(account["profile_id"]):
            initialize(seed_demo=False)

    with profile_scope(alice["profile_id"]):
        backup = backups.create_backup()

    alice_path = profile_database_path(alice["profile_id"])
    original_replace_live = backups._replace_live_database
    cutover_entered = Event()
    release_cutover = Event()

    def pause_at_cutover(staged, key_source=None, **kwargs):
        if profile_database_path() == alice_path:
            cutover_entered.set()
            assert release_cutover.wait(timeout=5)
        return original_replace_live(staged, key_source, **kwargs)

    monkeypatch.setattr(backups, "_replace_live_database", pause_at_cutover)

    async def run_scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            restore_task = asyncio.create_task(
                client.post(
                    f"/api/backups/{backup['id']}/restore",
                    json={},
                    headers=alice_headers,
                )
            )
            try:
                assert await asyncio.to_thread(cutover_entered.wait, 3)
                assert database_lock(alice_path).is_locked()

                alice_job_tasks = [
                    asyncio.create_task(
                        client.post(route, json={}, headers=alice_headers)
                    )
                    for route in (
                        "/api/sync",
                        "/api/score",
                        "/api/candidates/audit",
                        "/api/embeddings/rebuild",
                    )
                ]
                await asyncio.sleep(0.05)
                assert all(not task.done() for task in alice_job_tasks)

                # If any of the 4 routes ran enqueue_job() synchronously on the
                # event loop, health and Bob's job triggers would stall until cutover.
                loop = asyncio.get_running_loop()
                started = loop.time()
                health_res, bob_sync_res, bob_score_res = await asyncio.wait_for(
                    asyncio.gather(
                        client.get("/api/health"),
                        client.post("/api/sync", json={}, headers=bob_headers),
                        client.post("/api/score", json={}, headers=bob_headers),
                    ),
                    timeout=1.5,
                )
                assert loop.time() - started < 1.5
                assert health_res.status_code == 200
                assert bob_sync_res.status_code == 200
                assert bob_score_res.status_code == 200
                assert all(not task.done() for task in alice_job_tasks)

                release_cutover.set()
                restore_res, *alice_responses = await asyncio.wait_for(
                    asyncio.gather(restore_task, *alice_job_tasks),
                    timeout=5,
                )
                assert restore_res.status_code == 200
                for response in alice_responses:
                    assert response.status_code == 200
                    assert response.json()["job_id"]
            finally:
                release_cutover.set()
                await asyncio.gather(restore_task, return_exceptions=True)

    asyncio.run(run_scenario())


def test_backup_scheduler_offloads_maintenance_and_skips_locked_profiles(
    database, monkeypatch, caplog
):
    import asyncio
    from threading import Event, Thread, get_ident

    from afterword_engine import profiles
    from afterword_engine.database import connect, database_lock
    from afterword_engine.tenancy import (
        current_profile_id,
        profile_database_path,
        profile_scope,
    )

    monkeypatch.setattr(settings, "retention_enabled", True)
    monkeypatch.setattr(settings, "backup_interval_hours", 24)
    profiles.initialize_auth()
    if profiles.account_count() == 0:
        profiles.create_first_admin("maint-owner", "maint-owner-password-with-enough-length")
    alice = profiles.create_account("maint-alice", "maint-alice-password-with-enough-length")
    bob = profiles.create_account("maint-bob", "maint-bob-password-with-enough-length")
    carol = profiles.create_account("maint-carol", "maint-carol-password-with-enough-length")

    for account in (alice, bob, carol):
        with profile_scope(account["profile_id"]):
            initialize(seed_demo=False)
            with transaction() as con:
                con.execute(
                    "INSERT INTO jobs(id, kind, status, created_at, finished_at) "
                    "VALUES('aged-job', 'refresh', 'complete', '2025-01-01 00:00:00', '2025-01-01 00:01:00')"
                )

    alice_path = profile_database_path(alice["profile_id"])
    lock_acquired = Event()
    release_lock = Event()

    def hold_alice_lock() -> None:
        with database_lock(alice_path):
            lock_acquired.set()
            release_lock.wait(timeout=10)

    observed_threads: dict[str, list[int]] = {"due": [], "run": []}
    original_due = backups.maintenance_is_due
    original_run = backups.run_scheduled_maintenance
    stop = asyncio.Event()

    def tracked_due(*args, **kwargs):
        observed_threads["due"].append(get_ident())
        return original_due(*args, **kwargs)

    def tracked_run(*args, **kwargs):
        observed_threads["run"].append(get_ident())
        if current_profile_id() == carol["profile_id"]:
            raise RuntimeError("simulated carol maintenance failure")
        return original_run(*args, **kwargs)

    monkeypatch.setattr(backups, "maintenance_is_due", tracked_due)
    monkeypatch.setattr(backups, "run_scheduled_maintenance", tracked_run)

    original_profile_backup = backups._run_scheduled_profile_backup

    def tracked_profile_backup(profile_id: str):
        result = original_profile_backup(profile_id)
        if profile_id == carol["profile_id"]:
            stop.set()
        return result

    monkeypatch.setattr(backups, "_run_scheduled_profile_backup", tracked_profile_backup)

    async def run_scheduler():
        loop_thread_id = get_ident()
        lock_thread = Thread(target=hold_alice_lock, daemon=True)
        lock_thread.start()
        try:
            assert await asyncio.to_thread(lock_acquired.wait, 2)
            await asyncio.wait_for(backups.backup_scheduler_loop(stop), timeout=10)
            return loop_thread_id
        finally:
            release_lock.set()
            stop.set()
            await asyncio.to_thread(lock_thread.join, 2)

    caplog.set_level(logging.ERROR)
    loop_tid = asyncio.run(run_scheduler())

    assert observed_threads["due"]
    assert observed_threads["run"]
    assert all(tid != loop_tid for tid in observed_threads["due"])
    assert all(tid != loop_tid for tid in observed_threads["run"])

    # Locked Alice was skipped without pruning or blocking; unlocked Bob had aged job pruned;
    # Carol logged maintenance failure yet still received her scheduled backup.
    with profile_scope(alice["profile_id"]), connect() as con:
        assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert backups.list_backups() == []
    with profile_scope(bob["profile_id"]), connect() as con:
        assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert len(backups.list_backups()) == 1
    with profile_scope(carol["profile_id"]), connect() as con:
        assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert len(backups.list_backups()) == 1
    assert any(
        f"Scheduled Bookward storage maintenance failed for profile {carol['profile_id']}"
        in record.message
        for record in caplog.records
    )
