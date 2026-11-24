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

    def controlled_replace_live(staged, key_source=None):
        if pause_cutover and profile_database_path() == alice_path:
            cutover_entered.set()
            assert release_cutover.wait(timeout=5)
        return original_replace_live(staged, key_source)

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
