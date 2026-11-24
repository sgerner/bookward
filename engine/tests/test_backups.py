import asyncio
import logging
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth_helpers import authenticated_headers
from cryptography.fernet import Fernet

from afterword_engine import backups, profiles
from afterword_engine.config import settings
from afterword_engine.database import initialize, private_setting, row, transaction
from afterword_engine.main import app, lifespan
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


def test_startup_recovery_rolls_back_an_interrupted_database_and_key_swap(database):
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
    backups._write_restore_journal(
        database / "backups" / f"{safety['id']}.sqlite3",
        database / "backups" / f"{safety['id']}.sqlite3",
    )
    (database / "secret.key").unlink()

    assert backups.recover_interrupted_restore() is True
    assert not (database / backups.RESTORE_JOURNAL_NAME).exists()
    assert row("SELECT 1 FROM reads WHERE title='Safety state'") is not None
    assert row("SELECT 1 FROM reads WHERE title='Partial restore'") is None
    assert private_setting("recovery_test_secret") == "safety key"


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

    # Under retention=1, confirming Create backup now must NOT delete pre_disaster.
    clearing = backups.create_backup(confirm_clear_hold=True)
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
    assert backups._auth_recovery_hold_value() is True
    assert backups.auth_recovery_hold_active() is True
    assert backups.auth_backup_is_due() is False

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

    asyncio.run(locked_scheduler_cycle())

    monkeypatch.setattr(backups, "connect", real_profile_connect)
    monkeypatch.setattr(sqlite3, "connect", real_sqlite_connect)
    assert [item["id"] for item in backups.list_backups()] == [legacy_snap]
    assert [
        item["id"] for item in backups.identity_backup_status()["backups"]
    ] == [auth_snap]

    # Also verify real on-disk SQLite corruption fails closed without mocking connect.
    for suffix in ("", "-wal", "-shm"):
        Path(f"{settings.db}{suffix}").unlink(missing_ok=True)
        Path(f"{profiles.auth_path()}{suffix}").unlink(missing_ok=True)
    Path(settings.db).write_bytes(b"not a valid sqlite file")
    profiles.auth_path().write_bytes(b"not a valid sqlite file")

    assert backups._recovery_hold_value() is True
    assert backups._auth_recovery_hold_value() is True
    assert backups.backup_is_due() is False
    assert backups.auth_backup_is_due() is False


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

    # If an interrupted legacy restore fails during lifespan startup, initialize_auth()
    # must not run first and set an auth recovery hold on a freshly created registry.
    def fail_interrupted_restore():
        raise backups.BackupError("simulated interrupted restore failure")

    monkeypatch.setattr(
        main_mod, "recover_interrupted_restore", fail_interrupted_restore
    )

    async def run_failing_lifespan():
        async with lifespan(app):
            pass

    with pytest.raises(backups.BackupError, match="simulated interrupted restore"):
        asyncio.run(run_failing_lifespan())
    assert not profiles.auth_path().exists()

    # Verify ordering on normal startup: recover_interrupted_restore("legacy") runs before initialize_auth().
    events: list[tuple[str, str | None]] = []
    real_init_auth = profiles.initialize_auth

    def record_recover():
        from afterword_engine.tenancy import current_profile_id

        events.append(("recover", current_profile_id()))
        return False

    def record_init_auth():
        events.append(("initialize_auth", None))
        return real_init_auth()

    async def noop_worker(_handler, stop):
        await stop.wait()

    async def noop_loop(stop):
        await stop.wait()

    monkeypatch.setattr(main_mod, "recover_interrupted_restore", record_recover)
    monkeypatch.setattr(profiles, "initialize_auth", record_init_auth)
    monkeypatch.setattr(main_mod, "worker_loop", noop_worker)
    monkeypatch.setattr(main_mod, "source_scheduler_loop", noop_loop)
    monkeypatch.setattr(main_mod, "digest_scheduler_loop", noop_loop)
    monkeypatch.setattr(main_mod, "goodreads_rss_scheduler_loop", noop_loop)
    monkeypatch.setattr(main_mod, "backup_scheduler_loop", noop_loop)

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