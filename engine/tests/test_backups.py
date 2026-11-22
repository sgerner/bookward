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

from afterword_engine import backups, profiles, scoring
from afterword_engine.config import settings
from afterword_engine.database import (
    RestoreEpochError,
    current_restore_epoch,
    initialize,
    private_setting,
    row,
    rows,
    transaction,
)
from afterword_engine.embeddings import content_hash, vector_blob
from afterword_engine.jobs import enqueue_job, worker_loop
from afterword_engine.main import app, handle_job, queue_goodreads_rss_if_due
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


def test_restore_and_startup_recovery_scrub_unfinished_jobs_and_keep_terminal_rows(database):
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
        item["id"]: item["status"] for item in rows("SELECT id,status FROM jobs")
    }
    assert restored_jobs == {
        "job-complete": "complete",
        "job-failed": "failed",
    }
    # Scrubbing the running goodreads_rss row deleted it instead of marking it failed,
    # so the scheduler queues a fresh job cleanly rather than replaying a stale row.
    fresh_job_id = queue_goodreads_rss_if_due(today)
    assert fresh_job_id is not None
    assert fresh_job_id != "job-running"

    # Simulate an interrupted restore rolling back to safety_backup on startup.
    safety_path = database / "backups" / f"{result['safety_backup']['id']}.sqlite3"
    backups._write_restore_journal(safety_path, safety_path)
    assert backups.recover_interrupted_restore() is True
    assert current_restore_epoch() == 2
    recovered_jobs = {
        item["id"]: item["status"] for item in rows("SELECT id,status FROM jobs")
    }
    assert recovered_jobs == {
        "safety-complete": "complete",
        "safety-failed": "failed",
    }


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
