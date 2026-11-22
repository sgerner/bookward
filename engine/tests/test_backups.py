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


def test_restore_rejects_a_non_bookward_file_without_changing_live_data(database):
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

    with pytest.raises(backups.BackupError, match="not a Bookward database backup"):
        backups.restore_backup(invalid_id)

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
    assert stat.S_IMODE(live_db.stat().st_mode) == 0o600
    assert stat.S_IMODE((database / "secret.key").stat().st_mode) == 0o600
    assert row("SELECT 1 FROM reads WHERE title='Safety state'") is not None
    assert row("SELECT 1 FROM reads WHERE title='Partial restore'") is None
    assert private_setting("recovery_test_secret") == "safety key"
    assert (
        backups.backup_status()["last_error"]
        == "An interrupted restore was rolled back to its safety backup."
    )


def test_interrupted_restore_preserves_specific_diagnostics_without_leaking_paths_or_keys(
    database, monkeypatch
):
    with transaction() as con:
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
            ("diagnostic_secret", seal("top-secret-value")),
        )
    safety = backups.create_backup()
    safety_snapshot = database / "backups" / f"{safety['id']}.sqlite3"
    safety_key_bytes = (database / "secret.key").read_bytes().decode()
    journal_path = backups._restore_journal_path()

    # 1. Corrupt journal JSON surfaces a journal diagnostic rather than key mismatch.
    journal_path.write_text("{corrupt-json", encoding="utf-8")
    journal_path.chmod(0o600)
    with pytest.raises(backups.BackupError, match="journal is corrupted") as exc_info:
        backups.recover_interrupted_restore()
    assert "matching encryption key" not in str(exc_info.value)
    assert "journal is corrupted" in (backups.backup_status()["last_error"] or "")

    # 2. Active external SQLite lock is rejected and leaves the journal intact.
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

    # 3. OS errors with host paths or key filenames are sanitized in errors and status.
    def fail_stage(_source, _directory):
        raise PermissionError(
            13,
            "Permission denied",
            str(database / "secret.key"),
        )

    with monkeypatch.context() as mp:
        mp.setattr(backups, "_copy_to_stage", fail_stage)
        with pytest.raises(backups.BackupError, match="Permission denied") as os_exc:
            backups.recover_interrupted_restore()
        status_error = backups.backup_status()["last_error"] or ""
        for surfaced in (str(os_exc.value), status_error):
            assert "Permission denied" in surfaced
            assert "matching encryption key" not in surfaced
            assert str(database) not in surfaced
            assert "secret.key" not in surfaced
            assert safety_key_bytes not in surfaced

    # 4. Missing safety snapshot preserves the exact availability BackupError.
    safety_snapshot.unlink()
    with pytest.raises(backups.BackupError, match="no longer available"):
        backups.recover_interrupted_restore()
    assert backups.backup_status()["last_error"] == "That backup is no longer available."
    journal_path.unlink(missing_ok=True)


def test_fenced_profile_blocks_sqlite_access_backups_and_writes_while_keeping_status_responsive(
    database,
):
    import asyncio
    from afterword_engine.database import connect
    from afterword_engine.tenancy import database_path_override

    headers = authenticated_headers()
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
    assert "no longer available" in overview_resp.json()["detail"]
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

    # Zero bytes or sidecars were written to the fenced live database.
    assert live_db.read_bytes() == sentinel
    assert not Path(f"{settings.db}-wal").exists()
    journal_path.unlink(missing_ok=True)


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

    def corrupt_on_initial_swap(staged, key_source=None, *, rollback=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert rollback is False
            Path(settings.db).write_bytes(b"corrupt-mid-swap-bytes")
            raise OSError(28, "No space left on device", str(settings.db))
        assert rollback is True
        return original_replace(staged, key_source, rollback=rollback)

    monkeypatch.setattr(backups, "_replace_live_database", corrupt_on_initial_swap)
    with pytest.raises(backups.BackupError, match="previous database remains active") as exc_info:
        backups.restore_backup(target["id"])

    assert str(database) not in str(exc_info.value)
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
