from pathlib import Path
import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Thread

import pytest
import base64
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from afterword_engine import backups, database, profiles
from afterword_engine.api_tokens import generate_api_token, legacy_hash_api_token
from afterword_engine.config import settings
from afterword_engine.database import MIGRATIONS, connect, database_lock, initialize, transaction
import afterword_engine.jobs as jobs
from afterword_engine.jobs import enqueue_job, worker_loop
from afterword_engine.main import app
from afterword_engine.secrets import installation_key, installation_key_path, seal, unseal
from afterword_engine.tenancy import current_profile_id, profile_database_path, profile_scope


@pytest.fixture()
def identity_store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "afterword.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    profiles.initialize_auth()
    if profiles.account_count() == 0:
        admin = profiles.create_first_admin(
            "owner", "owner-password-with-enough-length"
        )
    else:
        with profiles.auth_connect() as con:
            admin_row = con.execute(
                "SELECT id FROM accounts WHERE profile_id='legacy' AND role='admin' "
                "AND status='active' ORDER BY created_at,id LIMIT 1"
            ).fetchone()
        assert admin_row is not None
        admin = profiles.account_by_id(admin_row["id"])
    initialize(seed_demo=False)
    return admin


def test_private_api_selects_data_from_the_authenticated_profile(identity_store):
    alice = profiles.create_account("alice", "alice-password-with-enough-length")
    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
                ("Alice private read", "Alice", "test"),
            )
            source_id = con.execute(
                "SELECT id FROM sources WHERE url='builtin://upcoming'"
            ).fetchone()[0]
            con.execute(
                "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
                "VALUES('Alice shortlist','Alice',90,'saved',?,?)",
                (source_id, "alice shortlist"),
            )
    with profile_scope("legacy"):
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
                ("Owner private read", "Owner", "test"),
            )
            source_id = con.execute(
                "SELECT id FROM sources WHERE url='builtin://upcoming'"
            ).fetchone()[0]
            con.execute(
                "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
                "VALUES('Owner shortlist','Owner',90,'saved',?,?)",
                (source_id, "owner shortlist"),
            )
    owner_session, _ = profiles.create_session(identity_store["id"])
    alice_session, _ = profiles.create_session(alice["id"])

    with TestClient(app) as client:
        owner_history = client.get(
            "/api/overview", headers={"x-bookward-session": owner_session}
        ).json()["history"]
        alice_history = client.get(
            "/api/overview", headers={"x-bookward-session": alice_session}
        ).json()["history"]
        owner_shortlist = client.get(
            "/api/recommendations?status=saved",
            headers={"x-bookward-session": owner_session},
        ).json()
        alice_shortlist = client.get(
            "/api/recommendations?status=saved",
            headers={"x-bookward-session": alice_session},
        ).json()

    assert "Owner private read" in {item["title"] for item in owner_history}
    assert "Alice private read" not in {item["title"] for item in owner_history}
    assert [item["title"] for item in alice_history] == ["Alice private read"]
    assert [item["title"] for item in owner_shortlist] == ["Owner shortlist"]
    assert [item["title"] for item in alice_shortlist] == ["Alice shortlist"]
    assert profile_database_path("legacy") != profile_database_path(alice["profile_id"])


def test_first_admin_setup_uses_only_credentials_and_is_available_once(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "first-admin.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.delenv("AFTERWORD_AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("AFTERWORD_AUTH_USERNAME", raising=False)
    profiles.initialize_auth()

    assert profiles.setup_required() is True
    with profiles.auth_connect() as con:
        con.execute(
            "INSERT INTO auth_meta(key,value) VALUES('setup_token_hash','obsolete-digest')"
        )
    profiles.initialize_auth()
    with profiles.auth_connect() as con:
        assert con.execute(
            "SELECT 1 FROM auth_meta WHERE key='setup_token_hash'"
        ).fetchone() is None

    account = profiles.create_first_admin(
        "first-reader", "first-reader-password-long"
    )

    assert account["role"] == "admin"
    assert account["profile_id"] == "legacy"
    assert profiles.setup_required() is False
    with pytest.raises(PermissionError, match="already completed"):
        profiles.create_first_admin("second-reader", "second-reader-password-long")
    assert profiles.account_count() == 1


def test_concurrent_first_admin_setup_creates_exactly_one_account(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "concurrent-first-admin.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.delenv("AFTERWORD_AUTH_PASSWORD", raising=False)
    profiles.initialize_auth()

    class FastPasswordHasher:
        def hash(self, password):
            return f"test-hash:{password}"

    monkeypatch.setattr(profiles, "PASSWORD_HASHER", FastPasswordHasher())
    validate_credentials = profiles.validate_credentials
    both_prechecked = Barrier(2)

    def validate_together(username, password):
        result = validate_credentials(username, password)
        both_prechecked.wait(timeout=5)
        return result

    monkeypatch.setattr(profiles, "validate_credentials", validate_together)

    def attempt(username):
        try:
            account = profiles.create_first_admin(username, "first-admin-password-long")
            return ("created", account["username"])
        except PermissionError:
            return ("closed", username)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ("concurrent-one", "concurrent-two")))

    assert [result[0] for result in results].count("created") == 1
    assert [result[0] for result in results].count("closed") == 1
    assert profiles.account_count() == 1


def test_auth_setup_api_needs_no_token_and_closes_after_first_account(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "setup-api.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.delenv("AFTERWORD_AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("AFTERWORD_AUTH_USERNAME", raising=False)
    profiles.initialize_auth()

    with TestClient(app) as client:
        created = client.post(
            "/auth/setup",
            json={
                "username": "web-owner",
                "password": "web-owner-password-long",
            },
        )
        config = client.get("/auth/config")
        repeated = client.post(
            "/auth/setup",
            json={
                "username": "late-owner",
                "password": "late-owner-password-long",
            },
        )

    assert created.status_code == 200
    assert created.json()["account"]["role"] == "admin"
    assert config.json()["setup_required"] is False
    assert config.json()["local_login_enabled"] is True
    assert repeated.status_code == 403


def test_admin_provisioned_local_login_opens_a_new_profile(identity_store):
    with profile_scope("legacy"):
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
                ("Owner only", "Owner", "test"),
            )
    owner_session, _ = profiles.create_session(identity_store["id"])

    with TestClient(app, headers={"x-bookward-session": owner_session}) as client:
        created = client.post(
            "/admin/accounts",
            json={
                "username": "reader",
                "password": "temporary-password-123",
                "display_name": "Reader",
                "role": "user",
            },
        )
        assert created.status_code == 200
        profile_id = created.json()["profile_id"]
        assert profile_id != "legacy"
        login = client.post(
            "/auth/login",
            json={"username": "reader", "password": "temporary-password-123"},
        )
        assert login.status_code == 200
        assert login.json()["account"]["profile_id"] == profile_id
        assert login.json()["account"]["must_change_password"] is True
        history = client.get(
            "/api/overview",
            headers={"x-bookward-session": login.json()["session_token"]},
        ).json()["history"]

    assert history == []


def test_password_reset_and_change_revoke_old_sessions(identity_store):
    reader = profiles.create_account("password-reader", "initial-reader-password-long")
    first_session, _ = profiles.create_session(reader["id"])
    second_session, _ = profiles.create_session(reader["id"])

    profiles.reset_password(reader["id"], "temporary-reader-password-long")

    assert profiles.resolve_session(first_session) is None
    assert profiles.resolve_session(second_session) is None
    reset_login = profiles.authenticate_local(
        "password-reader", "temporary-reader-password-long"
    )
    assert reset_login["must_change_password"] is True
    changed_session, _ = profiles.create_session(reader["id"])
    profiles.update_password(reader["id"], "personal-reader-password-long")

    assert profiles.resolve_session(changed_session) is None
    changed_login = profiles.authenticate_local(
        "password-reader", "personal-reader-password-long"
    )
    assert changed_login["must_change_password"] is False


def test_background_worker_processes_each_profiles_jobs_in_its_own_scope(identity_store):
    alice = profiles.create_account("job-alice", "job-alice-password-long")
    bob = profiles.create_account("job-bob", "job-bob-password-long")
    job_ids = {}
    for account, label in ((alice, "alice"), (bob, "bob")):
        with profile_scope(account["profile_id"]):
            initialize(seed_demo=False)
            job_ids[label] = enqueue_job("profile_acceptance", dedupe=True)
            assert enqueue_job("profile_acceptance", dedupe=True) == job_ids[label]

    stop = asyncio.Event()
    processed_profiles = []

    async def handler(kind):
        processed_profiles.append(current_profile_id())
        if len(processed_profiles) == 2:
            stop.set()
        return {"kind": kind}

    asyncio.run(worker_loop(handler, stop))

    assert set(processed_profiles) == {alice["profile_id"], bob["profile_id"]}
    for account, label in ((alice, "alice"), (bob, "bob")):
        with profile_scope(account["profile_id"]), connect() as con:
            status = con.execute("SELECT status FROM jobs WHERE id=?", (job_ids[label],)).fetchone()[0]
        assert status == "complete"


def test_workers_do_not_requeue_a_live_job_claimed_by_another_worker(identity_store):
    with profile_scope("legacy"):
        initialize(seed_demo=False)
        job_id = enqueue_job("single_execution")

    stop = asyncio.Event()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(kind):
        nonlocal calls
        assert kind == "single_execution"
        calls += 1
        started.set()
        await release.wait()
        stop.set()
        return {"calls": calls}

    async def run_workers():
        workers = [
            asyncio.create_task(worker_loop(handler, stop)),
            asyncio.create_task(worker_loop(handler, stop)),
        ]
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            await asyncio.sleep(0.05)
            assert calls == 1
        finally:
            release.set()
        await asyncio.wait_for(asyncio.gather(*workers), timeout=2)

    asyncio.run(run_workers())

    with profile_scope("legacy"), connect() as con:
        job = con.execute(
            "SELECT status,lease_token,heartbeat_at FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert calls == 1
    assert job["status"] == "complete"
    assert job["lease_token"] is None
    assert job["heartbeat_at"] is None


@pytest.mark.parametrize(
    "started_at",
    [None, "not-a-date", "2000-01-01T00:00:00+00:00"],
)
def test_worker_recovers_jobs_with_expired_leases(identity_store, started_at):
    with profile_scope("legacy"):
        initialize(seed_demo=False)
        job_id = enqueue_job("recover_expired")
        with transaction() as con:
            con.execute(
                "UPDATE jobs SET status='running',started_at=?,lease_token='dead-worker' WHERE id=?",
                (started_at, job_id),
            )

    stop = asyncio.Event()
    recovered = []

    async def handler(kind):
        recovered.append(kind)
        stop.set()
        return {"recovered": True}

    asyncio.run(worker_loop(handler, stop))

    with profile_scope("legacy"), connect() as con:
        job = con.execute(
            "SELECT status,result,lease_token FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert recovered == ["recover_expired"]
    assert job["status"] == "complete"
    assert job["result"] == '{"recovered": true}'
    assert job["lease_token"] is None


def test_worker_preserves_a_recent_lease_owned_by_another_worker(identity_store):
    with profile_scope("legacy"):
        initialize(seed_demo=False)
        job_id = enqueue_job("recover_on_startup")
        claimed_at = jobs.now()
        with transaction() as con:
            con.execute(
                "UPDATE jobs SET status='running',started_at=?,heartbeat_at=?,"
                "lease_token='previous-process',attempts=1 WHERE id=?",
                (claimed_at, claimed_at, job_id),
            )

    stop = asyncio.Event()
    handled = []

    async def handler(kind):
        handled.append(kind)
        return {}

    async def stop_worker():
        await asyncio.sleep(0.05)
        stop.set()

    async def run():
        stopper = asyncio.create_task(stop_worker())
        await worker_loop(handler, stop)
        await stopper

    asyncio.run(run())

    with profile_scope("legacy"), connect() as con:
        job = con.execute(
            "SELECT status,lease_token,attempts FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert handled == []
    assert job["status"] == "running"
    assert job["lease_token"] == "previous-process"
    assert job["attempts"] == 1


def test_worker_fails_jobs_after_the_retry_limit(identity_store):
    with profile_scope("legacy"):
        initialize(seed_demo=False)
        job_id = enqueue_job("exhausted_lease")
        claimed_at = "2000-01-01T00:00:00+00:00"
        with transaction() as con:
            con.execute(
                "UPDATE jobs SET status='running',started_at=?,heartbeat_at=?,"
                "lease_token='last-process',attempts=? WHERE id=?",
                (claimed_at, claimed_at, jobs.JOB_MAX_ATTEMPTS, job_id),
            )

    stop = asyncio.Event()
    handled = []

    async def handler(kind):
        handled.append(kind)
        return {}

    async def stop_worker():
        await asyncio.sleep(0.05)
        stop.set()

    async def run():
        stopper = asyncio.create_task(stop_worker())
        await worker_loop(handler, stop)
        await stopper

    asyncio.run(run())

    with profile_scope("legacy"), connect() as con:
        job = con.execute(
            "SELECT status,error,lease_token FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert handled == []
    assert job["status"] == "failed"
    assert job["error"] == "Job lease expired after maximum attempts"
    assert job["lease_token"] is None


def test_worker_skips_an_unmigratable_profile_and_keeps_serving_others(
    identity_store, monkeypatch
):
    broken_profile = "a" * 32
    broken_path = profile_database_path(broken_profile)
    broken_path.parent.mkdir(parents=True, exist_ok=True)
    broken_path.write_bytes(b"not a sqlite database")
    with profile_scope("legacy"):
        initialize(seed_demo=False)
        job_id = enqueue_job("healthy_profile_job")

    original_initialize = database.initialize

    def fail_one_profile(*, seed_demo=True):
        if current_profile_id() == broken_profile:
            raise database.sqlite3.DatabaseError("profile migration failed")
        return original_initialize(seed_demo=seed_demo)

    monkeypatch.setattr(database, "initialize", fail_one_profile)
    monkeypatch.setattr(profiles, "profile_ids", lambda: [broken_profile, "legacy"])
    stop = asyncio.Event()
    handled = []

    async def handler(kind):
        handled.append(kind)
        stop.set()
        return {"ok": True}

    asyncio.run(worker_loop(handler, stop))

    with profile_scope("legacy"), connect() as con:
        job = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert handled == ["healthy_profile_job"]
    assert job["status"] == "complete"


@pytest.mark.parametrize("fenced_target", ["legacy", "secondary"])
def test_lifespan_boots_when_legacy_or_secondary_profile_has_unrecoverable_restore_journal(
    tmp_path: Path, monkeypatch, fenced_target
):
    monkeypatch.setattr(settings, "db", str(tmp_path / "lifespan-fence.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    profiles.initialize_auth()
    owner = profiles.create_first_admin("owner", "owner-password-with-enough-length")
    alice = profiles.create_account("alice", "alice-password-with-enough-length")

    with profile_scope("legacy"):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
                ("Legacy healthy read", "Owner", "test"),
            )

    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
                ("Alice healthy read", "Alice", "test"),
            )

    fenced_profile_id = "legacy" if fenced_target == "legacy" else alice["profile_id"]
    healthy_profile_id = alice["profile_id"] if fenced_target == "legacy" else "legacy"
    fenced_account = owner if fenced_target == "legacy" else alice
    healthy_account = alice if fenced_target == "legacy" else owner

    with profile_scope(fenced_profile_id):
        journal = backups._restore_journal_path()
        journal.write_text(
            '{"safety_backup": "bookward-20260101T000000Z-deadbeef", "selected_backup": "bookward-20260101T000000Z-deadbeef"}',
            encoding="utf-8",
        )
        journal.chmod(0o600)
        db_file = profile_database_path()
        db_file.write_bytes(b"corrupt-unrecovered-bytes")

    owner_session, _ = profiles.create_session(owner["id"])
    fenced_session, _ = profiles.create_session(fenced_account["id"])
    healthy_session, _ = profiles.create_session(healthy_account["id"])

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert (
            client.get("/auth/me", headers={"x-bookward-session": fenced_session}).status_code
            == 200
        )
        assert (
            client.get(
                "/admin/auth-backups", headers={"x-bookward-session": owner_session}
            ).status_code
            == 200
        )
        fenced_backups = client.get(
            "/api/backups", headers={"x-bookward-session": fenced_session}
        )
        assert fenced_backups.status_code == 200
        assert fenced_backups.json()["last_error"] == "That backup is no longer available."

        fenced_overview = client.get(
            "/api/overview", headers={"x-bookward-session": fenced_session}
        )
        assert fenced_overview.status_code == 503
        assert fenced_overview.json()["detail"] == backups.RESTORE_FENCE_DETAIL

        healthy_overview = client.get(
            "/api/overview", headers={"x-bookward-session": healthy_session}
        )
        assert healthy_overview.status_code == 200
        expected_title = (
            "Alice healthy read" if healthy_profile_id != "legacy" else "Legacy healthy read"
        )
        assert expected_title in {
            item["title"] for item in healthy_overview.json()["history"]
        }

    with profile_scope(fenced_profile_id):
        assert backups._restore_journal_path().is_file()
        assert profile_database_path().read_bytes() == b"corrupt-unrecovered-bytes"


def test_transient_startup_lock_recovers_on_request_and_preserves_later_writes(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(settings, "db", str(tmp_path / "transient-lock.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    profiles.initialize_auth()
    owner = profiles.create_first_admin("owner", "owner-password-with-enough-length")
    api_token = profiles.create_profile_api_token("legacy", "Recovery check")["token"]

    with profile_scope("legacy"):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
                ("Safety baseline read", "Owner", "test"),
            )
        safety = backups.create_backup()
        safety_path = tmp_path / "backups" / f"{safety['id']}.sqlite3"
        with transaction() as con:
            con.execute("DELETE FROM reads WHERE title='Safety baseline read'")
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
                ("Unfinished restore state", "Owner", "test"),
            )
        backups._write_restore_journal(safety_path, safety_path)

    owner_session, _ = profiles.create_session(owner["id"])
    external_lock = sqlite3.connect(settings.db)
    external_lock.execute("BEGIN IMMEDIATE")
    external_lock.execute(
        "INSERT INTO reads(title,author,source) VALUES('External lock row','Ext','test')"
    )
    try:
        with TestClient(app) as client:
            locked_resp = client.get(
                "/api/overview", headers={"x-bookward-session": owner_session}
            )
            assert locked_resp.status_code == 503
            assert locked_resp.json()["detail"] == backups.RESTORE_FENCE_DETAIL

            external_lock.rollback()
            external_lock.close()
            external_lock = None

            # Once the lock clears, an API token or session request completes
            # rollback before serving.
            v1_resp = client.get(
                "/api/v1/overview",
                headers={"authorization": f"Bearer {api_token}"},
            )
            assert v1_resp.status_code == 200
            titles = {item["title"] for item in v1_resp.json()["history"]}
            assert "Safety baseline read" in titles
            assert "Unfinished restore state" not in titles

            with profile_scope("legacy"):
                assert not backups._restore_journal_path().exists()
                with transaction() as con:
                    con.execute(
                        "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
                        ("Write after recovery", "Owner", "test"),
                    )
    finally:
        if external_lock is not None:
            external_lock.close()

    # Restarting the engine must preserve post-recovery writes.
    with TestClient(app) as client:
        restarted = client.get(
            "/api/overview", headers={"x-bookward-session": owner_session}
        )
        assert restarted.status_code == 200
        restarted_titles = {item["title"] for item in restarted.json()["history"]}
        assert "Safety baseline read" in restarted_titles
        assert "Write after recovery" in restarted_titles


def test_worker_loop_completes_interrupted_restore_once_transient_lock_clears(
    identity_store, monkeypatch
):
    monkeypatch.setattr(jobs, "PROFILE_RETRY_SECONDS", 0.05)
    alice = profiles.create_account("worker-recover", "worker-recover-password-long")
    profile_id = alice["profile_id"]
    with profile_scope(profile_id):
        initialize(seed_demo=False)
        job_id = enqueue_job("post_recovery_job")
        safety = backups.create_backup()
        safety_path = backups.backup_directory() / f"{safety['id']}.sqlite3"
        profile_database_path().write_bytes(b"corrupt-partial-restore")
        backups._write_restore_journal(safety_path, safety_path)

    original_recover = backups.recover_interrupted_restore
    attempts = 0

    def transient_lock_then_recover():
        nonlocal attempts
        if current_profile_id() == profile_id:
            attempts += 1
            if attempts == 1:
                raise backups.BackupError(
                    "The active database could not be safely closed for restore."
                )
        return original_recover()

    monkeypatch.setattr(backups, "recover_interrupted_restore", transient_lock_then_recover)
    stop = asyncio.Event()
    handled = []

    async def handler(kind):
        handled.append((current_profile_id(), kind))
        stop.set()
        return {"recovered_by": "worker"}

    asyncio.run(worker_loop(handler, stop))

    assert attempts >= 2
    assert handled == [(profile_id, "post_recovery_job")]
    with profile_scope(profile_id):
        assert not backups._restore_journal_path().exists()
        with connect() as con:
            job = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "complete"


def test_lease_reaper_preserves_a_lease_renewed_after_its_read(identity_store, monkeypatch):
    with profile_scope("legacy"):
        initialize(seed_demo=False)
        job_id = enqueue_job("renewed_during_reap")
        with transaction() as con:
            con.execute(
                "UPDATE jobs SET status='running',started_at=?,heartbeat_at=?,"
                "lease_token='renewed-worker',attempts=1 WHERE id=?",
                ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", job_id),
            )

    original_rows = jobs.rows

    def renew_after_read(query, params=()):
        result = original_rows(query, params)
        if "FROM jobs WHERE status='running'" in query:
            with profile_scope("legacy"), transaction() as con:
                con.execute(
                    "UPDATE jobs SET heartbeat_at=? WHERE id=?",
                    (jobs.now(), job_id),
                )
        return result

    monkeypatch.setattr(jobs, "rows", renew_after_read)
    with profile_scope("legacy"):
        recovered = jobs._reap_abandoned_jobs()

    with profile_scope("legacy"), connect() as con:
        job = con.execute(
            "SELECT status,lease_token,heartbeat_at FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert recovered == 0
    assert job["status"] == "running"
    assert job["lease_token"] == "renewed-worker"
    assert job["heartbeat_at"] != "2000-01-01T00:00:00+00:00"


def test_idle_worker_does_not_open_write_transactions_for_lease_reaping(
    identity_store, monkeypatch
):
    calls = 0
    original_transaction = jobs.transaction

    def count_transactions():
        nonlocal calls
        calls += 1
        return original_transaction()

    monkeypatch.setattr(jobs, "transaction", count_transactions)
    stop = asyncio.Event()

    async def stop_worker():
        await asyncio.sleep(0.05)
        stop.set()

    async def run():
        stopper = asyncio.create_task(stop_worker())
        await worker_loop(lambda _kind: asyncio.sleep(0), stop)
        await stopper

    asyncio.run(run())

    assert calls == 0


def test_expired_worker_cannot_overwrite_the_reclaimed_job_result(identity_store):
    with profile_scope("legacy"):
        initialize(seed_demo=False)
        job_id = enqueue_job("lease_fencing")

    stop = asyncio.Event()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    calls = 0

    async def handler(kind):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()
            return {"worker": "expired"}
        second_started.set()
        stop.set()
        return {"worker": "reclaimed"}

    async def run_workers():
        first_worker = asyncio.create_task(worker_loop(handler, stop))
        await asyncio.wait_for(first_started.wait(), timeout=2)
        with profile_scope("legacy"), transaction() as con:
            con.execute(
                "UPDATE jobs SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (job_id,),
            )
        second_worker = asyncio.create_task(worker_loop(handler, stop))
        try:
            await asyncio.wait_for(second_started.wait(), timeout=2)
            await asyncio.wait_for(second_worker, timeout=2)
        finally:
            release_first.set()
        await asyncio.wait_for(first_worker, timeout=2)

    asyncio.run(run_workers())

    with profile_scope("legacy"), connect() as con:
        job = con.execute(
            "SELECT status,result,lease_token FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert calls == 2
    assert job["status"] == "complete"
    assert job["result"] == '{"worker": "reclaimed"}'
    assert job["lease_token"] is None


def test_job_heartbeat_survives_a_blocked_worker_event_loop(identity_store, monkeypatch):
    monkeypatch.setattr("afterword_engine.jobs.JOB_LEASE_SECONDS", 0.12)
    monkeypatch.setattr("afterword_engine.jobs.JOB_HEARTBEAT_SECONDS", 0.02)
    with profile_scope("legacy"):
        initialize(seed_demo=False)
        job_id = enqueue_job("blocked_handler")

    handler_started = Event()
    main_worker_finished = Event()
    competitor_finished = Event()
    duplicates = []
    competitor_errors = []
    main_stop = asyncio.Event()

    async def main_handler(kind):
        assert kind == "blocked_handler"
        handler_started.set()
        # Deliberately block the main worker's event loop for longer than its
        # lease. A loop-bound heartbeat would be unable to renew during this.
        time.sleep(0.75)
        main_worker_finished.set()
        main_stop.set()
        return {"worker": "original"}

    def run_competing_worker():
        if not handler_started.wait(timeout=2):
            competitor_errors.append("original handler did not start")
            competitor_finished.set()
            return

        async def run():
            stop = asyncio.Event()

            async def stop_when_original_finishes():
                while not main_worker_finished.wait(timeout=0):
                    await asyncio.sleep(0.01)
                stop.set()

            async def duplicate_handler(kind):
                duplicates.append(kind)
                stop.set()
                return {"worker": "duplicate"}

            monitor = asyncio.create_task(stop_when_original_finishes())
            try:
                await worker_loop(duplicate_handler, stop)
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)

        try:
            asyncio.run(run())
        except Exception as exc:
            competitor_errors.append(str(exc))
        finally:
            competitor_finished.set()

    competitor = Thread(target=run_competing_worker, daemon=True)
    competitor.start()
    asyncio.run(worker_loop(main_handler, main_stop))
    assert competitor_finished.wait(timeout=2)
    competitor.join(timeout=2)

    with profile_scope("legacy"), connect() as con:
        job = con.execute(
            "SELECT status,result FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert competitor_errors == []
    assert duplicates == []
    assert job["status"] == "complete"
    assert job["result"] == '{"worker": "original"}'


def test_profile_database_connections_lock_independently(identity_store):
    alice = profiles.create_account("lock-alice", "lock-alice-password-long")
    bob = profiles.create_account("lock-bob", "lock-bob-password-long")
    for account in (alice, bob):
        with profile_scope(account["profile_id"]):
            initialize(seed_demo=False)

    alice_path = profile_database_path(alice["profile_id"])
    bob_path = profile_database_path(bob["profile_id"])
    assert database_lock(alice_path) is database_lock(alice_path)
    assert database_lock(alice_path) is not database_lock(bob_path)

    opened_together = Barrier(2)

    def open_profile(profile_id):
        with profile_scope(profile_id), connect() as con:
            opened_together.wait(timeout=5)
            return con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            executor.submit(open_profile, account["profile_id"])
            for account in (alice, bob)
        ]
        assert [result.result(timeout=6) for result in results] == [0, 0]


def test_connections_within_one_profile_can_run_concurrently(identity_store):
    alice = profiles.create_account("parallel-alice", "parallel-alice-password-long")
    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
    both_open = Barrier(2)

    def read_profile():
        with profile_scope(alice["profile_id"]), connect() as con:
            both_open.wait(timeout=5)
            return con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(read_profile) for _ in range(2)]
        assert [result.result(timeout=6) for result in results] == [0, 0]


def test_exclusive_profile_lock_waits_for_open_connections(identity_store):
    alice = profiles.create_account("restore-alice", "restore-alice-password-long")
    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
        path = profile_database_path()
        connection = connect()
        lock_attempted = Event()
        lock_acquired = Event()

        def restore_lock():
            lock_attempted.set()
            with database_lock(path):
                lock_acquired.set()

        executor = ThreadPoolExecutor(max_workers=1)
        restore = executor.submit(restore_lock)
        try:
            assert lock_attempted.wait(timeout=2)
            assert not lock_acquired.wait(timeout=0.05)
        finally:
            connection.close()
            executor.shutdown(wait=True)
        assert lock_acquired.wait(timeout=2)
        restore.result(timeout=2)


def test_existing_install_upgrade_preserves_legacy_data_password_and_api_token(tmp_path, monkeypatch):
    database_path = tmp_path / "upgrade.db"
    monkeypatch.setattr(settings, "db", str(database_path))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    monkeypatch.setenv("AFTERWORD_AUTH_USERNAME", "old-owner")
    monkeypatch.setenv("AFTERWORD_AUTH_PASSWORD", "short-old-password")

    with profile_scope("legacy"):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,rating,source) VALUES(?,?,?,?)",
                ("Existing private read", "Upgrade Test", 5, "manual"),
            )
            token = generate_api_token()
            con.execute(
                "INSERT INTO api_tokens(id,name,token_prefix,token_hash) VALUES(1,?,?,?)",
                ("Existing integration", token[:12], legacy_hash_api_token(token)),
            )

    profiles.initialize_auth()

    with TestClient(app) as client:
        login = client.post(
            "/auth/login",
            json={"username": "old-owner", "password": "short-old-password"},
        )
        assert login.status_code == 200
        assert login.json()["account"]["profile_id"] == "legacy"
        history = client.get(
            "/api/overview",
            headers={"x-bookward-session": login.json()["session_token"]},
        )
        assert "Existing private read" in {
            item["title"] for item in history.json()["history"]
        }
        public = client.get(
            "/api/v1/overview", headers={"authorization": f"Bearer {token}"}
        )

    assert public.status_code == 200
    assert "Existing private read" in {
        item["title"] for item in public.json()["history"]
    }
    with profiles.auth_connect() as con:
        migrated = con.execute(
            "SELECT profile_id,token_hash FROM api_tokens WHERE token_prefix=?",
            (token[:12],),
        ).fetchone()
    assert migrated["profile_id"] == "legacy"
    assert migrated["token_hash"] != legacy_hash_api_token(token)


def test_same_email_on_different_oidc_subjects_creates_distinct_profiles(identity_store):
    first = profiles.create_oidc_account(
        "https://identity.example", "subject-one", "same@example.test", "Reader One"
    )
    second = profiles.create_oidc_account(
        "https://identity.example", "subject-two", "same@example.test", "Reader Two"
    )

    assert first["id"] != second["id"]
    assert first["profile_id"] != second["profile_id"]


def test_oidc_link_transaction_is_bound_to_the_exact_session(identity_store):
    session, _ = profiles.create_session(identity_store["id"])
    replacement_session, _ = profiles.create_session(identity_store["id"])
    fields = dict(
        state="one-time-state",
        csrf="one-time-csrf",
        nonce="nonce",
        verifier="verifier",
        purpose="link",
        account_id=identity_store["id"],
    )
    profiles.create_oidc_transaction(**fields, session_token=session)

    assert profiles.consume_oidc_transaction(
        "one-time-state", "one-time-csrf", replacement_session
    ) is None
    consumed = profiles.consume_oidc_transaction(
        "one-time-state", "one-time-csrf", session
    )
    assert consumed["account_id"] == identity_store["id"]
    assert profiles.consume_oidc_transaction(
        "one-time-state", "one-time-csrf", session
    ) is None


def test_oidc_login_transaction_is_not_tied_to_an_existing_session(identity_store):
    profiles.create_oidc_transaction(
        state="login-state",
        csrf="login-csrf",
        nonce="nonce",
        verifier="verifier",
        purpose="login",
        account_id=None,
    )

    consumed = profiles.consume_oidc_transaction("login-state", "login-csrf")
    assert consumed["purpose"] == "login"


def test_existing_oidc_transactions_gain_the_session_binding_column(identity_store):
    with profiles.auth_connect() as con:
        con.execute("DROP TABLE auth_transactions")
        con.execute(
            "CREATE TABLE auth_transactions (state_hash TEXT PRIMARY KEY, csrf_hash TEXT NOT NULL, "
            "nonce TEXT NOT NULL, code_verifier TEXT NOT NULL, purpose TEXT NOT NULL, "
            "account_id TEXT, expires_at TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )

    profiles.initialize_auth()

    with profiles.auth_connect() as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(auth_transactions)")}
    assert "session_hash" in columns


def test_profile_api_token_authentication_uses_the_shared_registry_key(identity_store):
    alice = profiles.create_account("alice", "alice-password-with-enough-length")
    bob = profiles.create_account("bob", "bob-password-with-enough-length")
    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
        with transaction() as con:
            source_id = con.execute(
                "SELECT id FROM sources WHERE url='builtin://upcoming'"
            ).fetchone()[0]
            con.execute(
                "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
                "VALUES('Alice API shortlist','Alice',90,'saved',?,?)",
                (source_id, "alice api shortlist"),
            )
            con.execute(
                "INSERT INTO jobs(id,kind,status) VALUES('alice-private-job','sync','complete')"
            )
        token = profiles.create_profile_api_token(alice["profile_id"], "Alice API")["token"]
    with profile_scope(bob["profile_id"]):
        initialize(seed_demo=False)
        with transaction() as con:
            source_id = con.execute(
                "SELECT id FROM sources WHERE url='builtin://upcoming'"
            ).fetchone()[0]
            con.execute(
                "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
                "VALUES('Bob API shortlist','Bob',90,'saved',?,?)",
                (source_id, "bob api shortlist"),
            )
            con.execute(
                "INSERT INTO jobs(id,kind,status) VALUES('bob-private-job','sync','complete')"
            )
        other_token = profiles.create_profile_api_token(bob["profile_id"], "Bob API")["token"]

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/me", headers={"authorization": f"Bearer {token}"}
        )
        conflict = client.get(
            "/api/v1/me",
            headers={
                "authorization": f"Bearer {token}",
                "x-api-key": other_token,
            },
        )
        alice_headers = {"authorization": f"Bearer {token}"}
        bob_headers = {"authorization": f"Bearer {other_token}"}
        alice_list = client.get(
            "/api/v1/recommendations?status=saved", headers=alice_headers
        )
        bob_list = client.get(
            "/api/v1/recommendations?status=saved", headers=bob_headers
        )
        alice_job = client.get("/api/jobs/alice-private-job", headers={"x-bookward-session": profiles.create_session(alice["id"])[0]})
        foreign_job = client.get("/api/jobs/alice-private-job", headers={"x-bookward-session": profiles.create_session(bob["id"])[0]})
        bob_job = client.get("/api/jobs/bob-private-job", headers={"x-bookward-session": profiles.create_session(bob["id"])[0]})

    assert response.status_code == 200
    assert response.json()["profile_id"] == alice["profile_id"]
    assert conflict.status_code == 401
    assert [item["title"] for item in alice_list.json()] == ["Alice API shortlist"]
    assert [item["title"] for item in bob_list.json()] == ["Bob API shortlist"]
    assert alice_job.status_code == 200
    assert foreign_job.status_code == 404
    assert bob_job.status_code == 200


@respx.mock
def test_oidc_login_routes_a_stable_identity_to_its_profile(identity_store, monkeypatch):
    issuer = "https://identity.example"
    client_id = "bookward-client"
    monkeypatch.setattr(settings, "oidc_issuer", issuer)
    monkeypatch.setattr(settings, "oidc_client_id", client_id)
    monkeypatch.setattr(settings, "oidc_client_secret", "test-client-secret")
    monkeypatch.setattr(settings, "oidc_redirect_uri", "https://bookward.example/auth/oidc/callback")
    monkeypatch.setattr(settings, "oidc_auto_provision", True)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private_key.public_key().public_numbers()

    def encoded_integer(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    respx.get(f"{issuer}/.well-known/openid-configuration").mock(
        return_value=httpx.Response(
            200,
            json={
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/authorize?tenant=books",
                "token_endpoint": f"{issuer}/token",
                "jwks_uri": f"{issuer}/jwks",
                "token_endpoint_auth_methods_supported": ["client_secret_post"],
            },
        )
    )
    respx.get(f"{issuer}/jwks").mock(
        return_value=httpx.Response(
            200,
            json={
                "keys": [
                    {
                        "kty": "RSA",
                        "use": "sig",
                        "alg": "RS256",
                        "kid": "test-signing-key",
                        "n": encoded_integer(public.n),
                        "e": encoded_integer(public.e),
                    }
                ]
            },
        )
    )
    nonces = {}

    def issue_token(request):
        form = parse_qs(request.content.decode())
        assert form["client_id"] == [client_id]
        assert form["client_secret"] == ["test-client-secret"]
        assert "authorization" not in request.headers
        code = form["code"][0]
        now = int(time.time())
        signed = jwt.encode(
            {
                "iss": issuer,
                "sub": "stable-subject-123",
                "aud": client_id,
                "iat": now,
                "exp": now + 600,
                "nonce": nonces[code],
                "email": "reader@example.test",
                "email_verified": True,
                "name": "Reader",
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "test-signing-key"},
        )
        return httpx.Response(200, json={"id_token": signed})

    respx.post(f"{issuer}/token").mock(side_effect=issue_token)
    profile_ids = []
    with TestClient(app) as client:
        for index in range(2):
            code = f"code-{index}"
            start = client.get("/auth/oidc/start")
            assert start.status_code == 200
            authorization = parse_qs(urlparse(start.json()["authorization_url"]).query)
            assert authorization["code_challenge_method"] == ["S256"]
            assert "tenant=books" in start.json()["authorization_url"]
            nonces[code] = authorization["nonce"][0]
            callback = client.get(
                "/auth/oidc/callback",
                params={"code": code, "state": authorization["state"][0]},
                headers={"x-oidc-state": start.json()["csrf"]},
            )
            assert callback.status_code == 200
            assert callback.json()["account"]["must_change_password"] is False
            profile_ids.append(callback.json()["account"]["profile_id"])
            if index == 0:
                missing_profile_db = profile_database_path(profile_ids[0])
                missing_profile_db.unlink()
                for suffix in ("-wal", "-shm", "-journal"):
                    Path(f"{missing_profile_db}{suffix}").unlink(missing_ok=True)
            replay = client.get(
                "/auth/oidc/callback",
                params={"code": code, "state": authorization["state"][0]},
                headers={"x-oidc-state": start.json()["csrf"]},
            )
            assert replay.status_code == 400

    assert profile_ids[0] != "legacy"
    assert profile_ids[0] == profile_ids[1]
    with profile_scope(profile_ids[0]), connect() as con:
        assert con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(MIGRATIONS)


@pytest.mark.parametrize(
    ("invalid_claim", "expected_status"),
    [("state", 400), ("nonce", 401), ("issuer", 401), ("audience", 401), ("expiry", 401)],
)
@respx.mock
def test_oidc_callback_rejects_invalid_state_and_id_token_claims(
    identity_store, monkeypatch, invalid_claim, expected_status
):
    issuer = "https://identity.example"
    client_id = "bookward-client"
    monkeypatch.setattr(settings, "oidc_issuer", issuer)
    monkeypatch.setattr(settings, "oidc_client_id", client_id)
    monkeypatch.setattr(settings, "oidc_client_secret", "test-client-secret")
    monkeypatch.setattr(settings, "oidc_redirect_uri", "https://bookward.example/auth/oidc/callback")

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private_key.public_key().public_numbers()

    def encoded_integer(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    respx.get(f"{issuer}/.well-known/openid-configuration").mock(
        return_value=httpx.Response(
            200,
            json={
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/token",
                "jwks_uri": f"{issuer}/jwks",
            },
        )
    )
    respx.get(f"{issuer}/jwks").mock(
        return_value=httpx.Response(
            200,
            json={
                "keys": [{
                    "kty": "RSA", "use": "sig", "alg": "RS256", "kid": "bad-claim-key",
                    "n": encoded_integer(public.n), "e": encoded_integer(public.e),
                }],
            },
        )
    )
    expected_nonce = ""

    def issue_token(request):
        now = int(time.time())
        claims = {
            "iss": issuer,
            "sub": "invalid-claim-subject",
            "aud": client_id,
            "iat": now,
            "exp": now + 600,
            "nonce": expected_nonce,
        }
        if invalid_claim == "nonce":
            claims["nonce"] = "wrong-nonce"
        elif invalid_claim == "issuer":
            claims["iss"] = "https://attacker.example"
        elif invalid_claim == "audience":
            claims["aud"] = "different-client"
        elif invalid_claim == "expiry":
            claims["exp"] = now - 600
        signed = jwt.encode(
            claims, private_key, algorithm="RS256", headers={"kid": "bad-claim-key"}
        )
        return httpx.Response(200, json={"id_token": signed})

    token_route = respx.post(f"{issuer}/token").mock(side_effect=issue_token)
    with TestClient(app) as client:
        start = client.get("/auth/oidc/start")
        authorization = parse_qs(urlparse(start.json()["authorization_url"]).query)
        expected_nonce = authorization["nonce"][0]
        supplied_state = authorization["state"][0]
        if invalid_claim == "state":
            supplied_state = "incorrect-state"
        callback = client.get(
            "/auth/oidc/callback",
            params={"code": "bad-claims-code", "state": supplied_state},
            headers={"x-oidc-state": start.json()["csrf"]},
        )

    assert callback.status_code == expected_status
    assert token_route.called is (invalid_claim != "state")


def test_generated_profile_keys_and_backups_do_not_replace_another_profiles_key(identity_store):
    alice = profiles.create_account("alice", "alice-password-with-enough-length")
    bob = profiles.create_account("bob", "bob-password-with-enough-length")
    backup_ids = {}
    keys = {}
    for account, label in ((alice, "alice"), (bob, "bob")):
        with profile_scope(account["profile_id"]):
            initialize(seed_demo=False)
            with transaction() as con:
                con.execute(
                    "INSERT INTO settings(key,value,secret) VALUES(?,?,1)",
                    ("profile_secret", seal(f"{label} secret")),
                )
            keys[label] = installation_key()
            backup_ids[label] = backups.create_backup()["id"]
            assert installation_key_path().is_file()

    assert keys["alice"] != keys["bob"]
    bob_key_path = profile_database_path(bob["profile_id"]).with_name(
        f"{profile_database_path(bob['profile_id']).name}.secret.key"
    )
    bob_key_before_restore = bob_key_path.read_bytes()

    with profile_scope(alice["profile_id"]):
        # Simulate losing the profile data volume and its generated key.
        database = profile_database_path()
        database.unlink()
        for suffix in ("-wal", "-shm"):
            Path(f"{database}{suffix}").unlink(missing_ok=True)
        installation_key_path().unlink()
        initialize(seed_demo=False)
        seal("replacement secret")
        backups.restore_backup(backup_ids["alice"])
        with connect() as con:
            encrypted = con.execute(
                "SELECT value FROM settings WHERE key='profile_secret'"
            ).fetchone()[0]
        assert unseal(encrypted) == "alice secret"

    assert bob_key_path.read_bytes() == bob_key_before_restore
    with profile_scope(bob["profile_id"]):
        with connect() as con:
            encrypted = con.execute(
                "SELECT value FROM settings WHERE key='profile_secret'"
            ).fetchone()[0]
        assert unseal(encrypted) == "bob secret"


def test_identity_registry_restore_revokes_restored_sessions_and_tokens(identity_store):
    reader = profiles.create_account("backup-reader", "backup-reader-password-long")
    token_before_backup = profiles.create_profile_api_token(
        reader["profile_id"], "Before backup"
    )["token"]
    session_before_backup, _ = profiles.create_session(reader["id"])
    snapshot = backups.create_auth_backup()

    later_reader = profiles.create_account("later-reader", "later-reader-password-long")
    token_after_backup = profiles.create_profile_api_token(
        later_reader["profile_id"], "After backup"
    )["token"]
    session_after_backup, _ = profiles.create_session(later_reader["id"])

    restored = backups.restore_auth_backup(snapshot["id"])

    assert restored == {
        "restored": True,
        "sessions_revoked": True,
        "api_tokens_revoked": True,
    }
    assert profiles.resolve_session(session_before_backup) is None
    assert profiles.resolve_session(session_after_backup) is None
    assert profiles.authenticate_api_token(token_before_backup) is None
    assert profiles.authenticate_api_token(token_after_backup) is None
    assert profiles.account_by_id(later_reader["id"]) is None
    safety_snapshots = [
        item for item in backups.identity_backup_status()["backups"]
        if item["id"] != snapshot["id"]
    ]
    assert safety_snapshots
    assert (backups.auth_backup_directory() / f"{snapshot['id']}.sqlite3").is_file()
    safety_path = backups.auth_backup_directory() / f"{safety_snapshots[0]['id']}.sqlite3"
    with sqlite3.connect(safety_path) as con:
        assert con.execute(
            "SELECT 1 FROM accounts WHERE id=?", (later_reader["id"],)
        ).fetchone() == (1,)


def test_identity_backup_failure_is_persisted_in_status(identity_store, monkeypatch):
    def fail_directory(_directory):
        raise OSError("backup storage is unavailable")

    monkeypatch.setattr(backups, "_ensure_private_directory", fail_directory)
    with pytest.raises(OSError, match="backup storage is unavailable"):
        backups.create_auth_backup()

    status = backups.identity_backup_status()
    assert status["last_attempt_at"]
    assert status["last_error"] == "backup storage is unavailable"


def test_identity_restore_rolls_back_after_replace_failure_and_keeps_selected_snapshot(
    identity_store, monkeypatch
):
    reader = profiles.create_account("restore-safety-reader", "restore-safety-password-long")
    selected = backups.create_auth_backup()
    later_reader = profiles.create_account("restore-safety-later", "restore-safety-password-long")
    monkeypatch.setattr(settings, "backup_retention_count", 1)
    replace_registry = backups._replace_auth_registry
    calls = 0

    def fail_after_first_rename(staged, live, on_replace=None):
        nonlocal calls
        calls += 1
        replace_registry(staged, live, on_replace=on_replace)
        if calls == 1:
            raise OSError("injected directory sync failure")

    monkeypatch.setattr(backups, "_replace_auth_registry", fail_after_first_rename)
    with pytest.raises(OSError, match="injected directory sync failure"):
        backups.restore_auth_backup(selected["id"])

    assert calls == 2
    assert profiles.account_by_id(reader["id"]) is not None
    assert profiles.account_by_id(later_reader["id"]) is not None
    assert (backups.auth_backup_directory() / f"{selected['id']}.sqlite3").is_file()


def test_identity_backup_status_is_admin_only_and_reports_metadata(identity_store):
    backup = backups.create_auth_backup()
    reader = profiles.create_account("backup-status-reader", "backup-status-password-long")
    admin_session, _ = profiles.create_session(identity_store["id"])
    reader_session, _ = profiles.create_session(reader["id"])

    with TestClient(app) as client:
        admin = client.get(
            "/admin/auth-backups", headers={"x-bookward-session": admin_session}
        )
        regular = client.get(
            "/admin/auth-backups", headers={"x-bookward-session": reader_session}
        )
        anonymous = client.get("/admin/auth-backups")

    assert admin.status_code == 200
    status = admin.json()
    assert status["enabled"] is True
    assert isinstance(status["interval_hours"], int)
    assert status["last_success_at"]
    assert any(item["id"] == backup["id"] for item in status["backups"])
    assert all(set(item) == {"id", "created_at", "size_bytes"} for item in status["backups"])
    assert regular.status_code == 403
    assert anonymous.status_code == 401


def test_reenabling_profile_applies_migrations_accumulated_while_disabled(identity_store):
    feedback_migration = next(
        version for version, script in MIGRATIONS
        if "ALTER TABLE feedback ADD COLUMN previous_status" in script
    )
    reader = profiles.create_account("returning-reader", "temporary-password-123")
    profile_id = reader["profile_id"]
    with profile_scope(profile_id):
        initialize(seed_demo=False)
        # Model a profile that was disabled during the feedback schema upgrade.
        with connect() as con:
            con.execute("ALTER TABLE feedback DROP COLUMN undone_at")
            con.execute("ALTER TABLE feedback DROP COLUMN previous_status")
            con.execute("DELETE FROM schema_migrations WHERE version=?", (feedback_migration,))
    profiles.set_account_status(reader["id"], "disabled")
    admin_session, _ = profiles.create_session(identity_store["id"])

    with TestClient(app, headers={"x-bookward-session": admin_session}) as client:
        response = client.put(
            f"/admin/accounts/{reader['id']}/status", json={"status": "active"}
        )
    assert response.status_code == 200
    with profile_scope(profile_id), connect() as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(feedback)")}
        applied = {row[0] for row in con.execute("SELECT version FROM schema_migrations")}
    assert {"previous_status", "undone_at"} <= columns
    assert feedback_migration in applied


@respx.mock
def test_oidc_account_enrolls_unique_local_username_and_blocks_lockout_unlink(
    identity_store, monkeypatch
):
    issuer = "https://identity.example"
    client_id = "bookward-client"
    monkeypatch.setattr(settings, "oidc_issuer", issuer)
    monkeypatch.setattr(settings, "oidc_client_id", client_id)
    monkeypatch.setattr(settings, "oidc_client_secret", "test-client-secret")
    monkeypatch.setattr(settings, "oidc_redirect_uri", "https://bookward.example/auth/oidc/callback")

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private_key.public_key().public_numbers()

    def encoded_integer(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    respx.get(f"{issuer}/.well-known/openid-configuration").mock(
        return_value=httpx.Response(
            200,
            json={
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/token",
                "jwks_uri": f"{issuer}/jwks",
            },
        )
    )
    respx.get(f"{issuer}/jwks").mock(
        return_value=httpx.Response(
            200,
            json={
                "keys": [
                    {
                        "kty": "RSA",
                        "use": "sig",
                        "alg": "RS256",
                        "kid": "enroll-signing-key",
                        "n": encoded_integer(public.n),
                        "e": encoded_integer(public.e),
                    }
                ]
            },
        )
    )
    token_specs: dict[str, dict] = {}

    def issue_token(request):
        code = parse_qs(request.content.decode())["code"][0]
        spec = token_specs[code]
        now = int(time.time())
        claims = {
            "iss": issuer,
            "sub": spec["sub"],
            "aud": client_id,
            "iat": now,
            "exp": now + 600,
            "nonce": spec["nonce"],
            "email": "sso@example.test",
            "email_verified": True,
            "name": "SSO Reader",
        }
        if "auth_time" in spec:
            claims["auth_time"] = spec["auth_time"]
        signed = jwt.encode(
            claims, private_key, algorithm="RS256", headers={"kid": "enroll-signing-key"}
        )
        return httpx.Response(200, json={"id_token": signed})

    respx.post(f"{issuer}/token").mock(side_effect=issue_token)

    sso_reader = profiles.create_oidc_account(
        issuer, "enroll-subject-1", "sso@example.test", "SSO Reader"
    )
    existing_local = profiles.create_account("taken-name", "taken-reader-password-long")
    assert sso_reader["has_local_username"] is False
    assert existing_local["has_local_username"] is True

    admin_session, _ = profiles.create_session(identity_store["id"])
    sso_session, _ = profiles.create_session(sso_reader["id"])
    enroll_session, _ = profiles.create_session(sso_reader["id"])
    local_only_session, _ = profiles.create_session(existing_local["id"])

    with TestClient(app) as client:
        me = client.get("/auth/me", headers={"x-bookward-session": sso_session})
        assert me.status_code == 200
        assert me.json()["has_local_username"] is False

        methods = client.get("/auth/identities", headers={"x-bookward-session": sso_session})
        assert methods.status_code == 200
        assert methods.json()["has_local_username"] is False
        assert methods.json()["password_enabled"] is False
        assert methods.json()["enrollment_verified"] is False
        identity_id = methods.json()["oidc_identities"][0]["id"]

        admin_list = client.get("/admin/accounts", headers={"x-bookward-session": admin_session})
        assert admin_list.status_code == 200
        sso_entry = next(item for item in admin_list.json()["accounts"] if item["id"] == sso_reader["id"])
        assert sso_entry["has_local_username"] is False

        # Sole SSO identity cannot be unlinked before local username + password exist.
        unlink_without_password = client.delete(
            f"/auth/identities/{identity_id}",
            headers={"x-bookward-session": sso_session},
        )
        assert unlink_without_password.status_code == 400

        # Simulate a row that had password_hash set while still owning an oidc: surrogate username.
        with profiles.auth_connect() as con:
            con.execute(
                "UPDATE accounts SET password_hash=? WHERE id=?",
                (profiles.PASSWORD_HASHER.hash("legacy-unusable-password-long"), sso_reader["id"]),
            )
        assert profiles.authenticate_local(sso_reader["username"], "legacy-unusable-password-long") is None
        unlink_with_surrogate_username = client.delete(
            f"/auth/identities/{identity_id}",
            headers={"x-bookward-session": sso_session},
        )
        assert unlink_with_surrogate_username.status_code == 400

        # A freshly minted login session (< 10 minutes old) is NOT enough to attach local credentials:
        # enrollment requires an explicit intent=enroll OIDC round trip.
        unconfirmed_fresh_attempt = client.post(
            "/auth/password/change",
            headers={"x-bookward-session": enroll_session},
            json={"username": "chosen-reader", "new_password": "chosen-reader-password-long"},
        )
        assert unconfirmed_fresh_attempt.status_code == 401

        # intent=enroll start requires an authenticated session and a linked OIDC identity.
        assert client.get("/auth/oidc/start?intent=enroll").status_code == 401
        assert (
            client.get(
                "/auth/oidc/start?intent=enroll",
                headers={"x-bookward-session": local_only_session},
            ).status_code
            == 400
        )

        # Mismatched session cannot redeem another session's enroll transaction.
        mismatch_start = client.get(
            "/auth/oidc/start?intent=enroll",
            headers={"x-bookward-session": enroll_session},
        )
        assert mismatch_start.status_code == 200
        mismatch_auth = parse_qs(urlparse(mismatch_start.json()["authorization_url"]).query)
        assert mismatch_auth["prompt"] == ["login"]
        mismatch_callback = client.get(
            "/auth/oidc/callback",
            params={"code": "unused-code", "state": mismatch_auth["state"][0]},
            headers={
                "x-bookward-session": sso_session,
                "x-oidc-state": mismatch_start.json()["csrf"],
            },
        )
        assert mismatch_callback.status_code == 400

        # Unlinked subject cannot verify enrollment for this account.
        wrong_sub_start = client.get(
            "/auth/oidc/start?intent=enroll",
            headers={"x-bookward-session": enroll_session},
        )
        wrong_sub_auth = parse_qs(urlparse(wrong_sub_start.json()["authorization_url"]).query)
        token_specs["wrong-sub-code"] = {
            "sub": "different-subject",
            "nonce": wrong_sub_auth["nonce"][0],
            "auth_time": int(time.time()),
        }
        wrong_sub_callback = client.get(
            "/auth/oidc/callback",
            params={"code": "wrong-sub-code", "state": wrong_sub_auth["state"][0]},
            headers={
                "x-bookward-session": enroll_session,
                "x-oidc-state": wrong_sub_start.json()["csrf"],
            },
        )
        assert wrong_sub_callback.status_code == 403

        # Stale auth_time (> 10 minutes old) is rejected even during intent=enroll callback.
        stale_auth_start = client.get(
            "/auth/oidc/start?intent=enroll",
            headers={"x-bookward-session": enroll_session},
        )
        stale_auth_query = parse_qs(urlparse(stale_auth_start.json()["authorization_url"]).query)
        token_specs["stale-auth-code"] = {
            "sub": "enroll-subject-1",
            "nonce": stale_auth_query["nonce"][0],
            "auth_time": int(time.time()) - 900,
        }
        stale_auth_callback = client.get(
            "/auth/oidc/callback",
            params={"code": "stale-auth-code", "state": stale_auth_query["state"][0]},
            headers={
                "x-bookward-session": enroll_session,
                "x-oidc-state": stale_auth_start.json()["csrf"],
            },
        )
        assert stale_auth_callback.status_code == 401

        def confirm_enrollment(code_name: str) -> None:
            start = client.get(
                "/auth/oidc/start?intent=enroll",
                headers={"x-bookward-session": enroll_session},
            )
            assert start.status_code == 200
            auth_query = parse_qs(urlparse(start.json()["authorization_url"]).query)
            assert auth_query["prompt"] == ["login"]
            token_specs[code_name] = {
                "sub": "enroll-subject-1",
                "nonce": auth_query["nonce"][0],
                "auth_time": int(time.time()),
            }
            callback = client.get(
                "/auth/oidc/callback",
                params={"code": code_name, "state": auth_query["state"][0]},
                headers={
                    "x-bookward-session": enroll_session,
                    "x-oidc-state": start.json()["csrf"],
                },
            )
            assert callback.status_code == 200
            assert callback.json()["enroll_verified"] is True

        confirm_enrollment("enroll-code-1")
        assert (
            client.get("/auth/identities", headers={"x-bookward-session": enroll_session}).json()[
                "enrollment_verified"
            ]
            is True
        )
        # Verification is scoped to the presenting session, not sibling sessions.
        assert (
            client.get("/auth/identities", headers={"x-bookward-session": sso_session}).json()[
                "enrollment_verified"
            ]
            is False
        )
        assert (
            client.post(
                "/auth/password/change",
                headers={"x-bookward-session": sso_session},
                json={"username": "chosen-reader", "new_password": "chosen-reader-password-long"},
            ).status_code
            == 401
        )

        # Expired enrollment verification (> 10 minutes old) is rejected.
        with profiles.auth_connect() as con:
            con.execute(
                "UPDATE auth_sessions SET enrollment_verified_at='2000-01-01T00:00:00+00:00' "
                "WHERE account_id=?",
                (sso_reader["id"],),
            )
        expired_attempt = client.post(
            "/auth/password/change",
            headers={"x-bookward-session": enroll_session},
            json={"username": "chosen-reader", "new_password": "chosen-reader-password-long"},
        )
        assert expired_attempt.status_code == 401

        confirm_enrollment("enroll-code-2")

        missing_username = client.post(
            "/auth/password/change",
            headers={"x-bookward-session": enroll_session},
            json={"new_password": "chosen-reader-password-long"},
        )
        assert missing_username.status_code == 400

        reserved_prefix = client.post(
            "/auth/password/change",
            headers={"x-bookward-session": enroll_session},
            json={"username": "OIDC:spoofed", "new_password": "chosen-reader-password-long"},
        )
        assert reserved_prefix.status_code == 400

        duplicate_username = client.post(
            "/auth/password/change",
            headers={"x-bookward-session": enroll_session},
            json={"username": "Taken-Name", "new_password": "chosen-reader-password-long"},
        )
        assert duplicate_username.status_code == 409

        enrolled = client.post(
            "/auth/password/change",
            headers={"x-bookward-session": enroll_session},
            json={"username": "chosen-reader", "new_password": "chosen-reader-password-long"},
        )
        assert enrolled.status_code == 200
        assert enrolled.json()["username"] == "chosen-reader"
        assert enrolled.json()["has_local_username"] is True
        rotated_session = enrolled.json()["session_token"]

        # Enrolling local credentials bumps auth_version and revokes all prior sessions.
        assert profiles.resolve_session(sso_session) is None
        assert profiles.resolve_session(enroll_session) is None
        assert client.get("/auth/me", headers={"x-bookward-session": sso_session}).status_code == 401
        assert client.get("/auth/me", headers={"x-bookward-session": enroll_session}).status_code == 401
        assert (
            client.get("/auth/identities", headers={"x-bookward-session": rotated_session}).json()[
                "enrollment_verified"
            ]
            is False
        )

        unlinked = client.delete(
            f"/auth/identities/{identity_id}",
            headers={"x-bookward-session": rotated_session},
        )
        assert unlinked.status_code == 200

        local_login = client.post(
            "/auth/login",
            json={"username": "chosen-reader", "password": "chosen-reader-password-long"},
        )
        assert local_login.status_code == 200
        assert local_login.json()["account"]["profile_id"] == sso_reader["profile_id"]


def test_legacy_oidc_prefixed_local_account_migrates_and_retains_login_and_reset(identity_store):
    issuer = "https://identity.example"
    surrogate_username = profiles.oidc_surrogate_username(issuer, "surrogate-subject")
    legacy_password_hash = profiles.PASSWORD_HASHER.hash("legacy-local-password-123")
    surrogate_password_hash = profiles.PASSWORD_HASHER.hash("unusable-surrogate-password-123")

    for profile_id in ("b" * 32, "c" * 32, "d" * 32):
        with profiles.auth_connect() as con:
            con.execute("INSERT INTO profiles(id,display_name) VALUES(?,?)", (profile_id, profile_id))
        with profile_scope(profile_id):
            initialize(seed_demo=False)

    # Simulate pre-migration accounts schema (before has_local_username existed) where a local
    # account legitimately used an 'oidc:'-prefixed username alongside true OIDC surrogate rows.
    with profiles.auth_connect() as con:
        con.execute("ALTER TABLE accounts DROP COLUMN has_local_username")
        con.execute(
            """
            INSERT INTO accounts(
                id, profile_id, username, normalized_username, display_name, role, status, password_hash
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                "legacy-local-id",
                "b" * 32,
                "oidc:legacy-reader",
                "oidc:legacy-reader",
                "Legacy OIDC-Prefixed Local",
                "user",
                "active",
                legacy_password_hash,
            ),
        )
        con.execute(
            "INSERT INTO oidc_identities(account_id, issuer, subject, email) VALUES (?,?,?,?)",
            ("legacy-local-id", issuer, "linked-non-surrogate-sub", "legacy@example.test"),
        )
        con.execute(
            """
            INSERT INTO accounts(
                id, profile_id, username, normalized_username, display_name, role, status, password_hash
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                "surrogate-id",
                "c" * 32,
                surrogate_username,
                surrogate_username,
                "Surrogate OIDC",
                "user",
                "active",
                surrogate_password_hash,
            ),
        )
        con.execute(
            "INSERT INTO oidc_identities(account_id, issuer, subject, email) VALUES (?,?,?,?)",
            ("surrogate-id", issuer, "surrogate-subject", "surrogate@example.test"),
        )
        con.execute(
            """
            INSERT INTO accounts(
                id, profile_id, username, normalized_username, display_name, role, status, password_hash
            ) VALUES (?,?,?,?,?,?,?,NULL)
            """,
            (
                "passwordless-oidc-id",
                "d" * 32,
                "oidc:passwordless",
                "oidc:passwordless",
                "Passwordless OIDC",
                "user",
                "active",
            ),
        )

    profiles.initialize_auth()

    assert profiles.account_by_id("legacy-local-id")["has_local_username"] is True
    assert profiles.account_by_id("surrogate-id")["has_local_username"] is False
    assert profiles.account_by_id("passwordless-oidc-id")["has_local_username"] is False
    assert profiles.authenticate_local(surrogate_username, "unusable-surrogate-password-123") is None

    admin_session, _ = profiles.create_session(identity_store["id"])
    with TestClient(app) as client:
        # Existing local account with 'oidc:' prefix can still sign in locally.
        login = client.post(
            "/auth/login",
            json={"username": "OIDC:Legacy-Reader", "password": "legacy-local-password-123"},
        )
        assert login.status_code == 200
        assert login.json()["account"]["username"] == "oidc:legacy-reader"
        assert login.json()["account"]["has_local_username"] is True
        legacy_session = login.json()["session_token"]

        # Self-service password change works without renaming the legacy username.
        changed = client.post(
            "/auth/password/change",
            headers={"x-bookward-session": legacy_session},
            json={
                "current_password": "legacy-local-password-123",
                "new_password": "updated-legacy-password-456",
            },
        )
        assert changed.status_code == 200
        assert changed.json()["username"] == "oidc:legacy-reader"
        assert changed.json()["has_local_username"] is True

        # Admin password reset continues to work for legacy local accounts.
        admin_reset = client.post(
            "/admin/accounts/legacy-local-id/password",
            headers={"x-bookward-session": admin_session},
            json={"password": "admin-temporary-password-789"},
        )
        assert admin_reset.status_code == 200
        assert profiles.account_by_id("legacy-local-id")["must_change_password"] is True

        reset_login = client.post(
            "/auth/login",
            json={"username": "oidc:legacy-reader", "password": "admin-temporary-password-789"},
        )
        assert reset_login.status_code == 200
        personal_update = client.post(
            "/auth/password/change",
            headers={"x-bookward-session": reset_login.json()["session_token"]},
            json={
                "current_password": "admin-temporary-password-789",
                "new_password": "final-legacy-password-999",
            },
        )
        assert personal_update.status_code == 200
        final_session = personal_update.json()["session_token"]

        # Linked OIDC identity can be unlinked because local login is genuinely active.
        identities = client.get(
            "/auth/identities", headers={"x-bookward-session": final_session}
        ).json()
        assert identities["has_local_username"] is True
        assert identities["password_enabled"] is True
        unlink = client.delete(
            f"/auth/identities/{identities['oidc_identities'][0]['id']}",
            headers={"x-bookward-session": final_session},
        )
        assert unlink.status_code == 200


def test_update_password_integrity_error_rolls_back_without_revoking_sessions(identity_store):
    profiles.create_account("already-taken", "already-taken-password-long")
    sso_reader = profiles.create_oidc_account(
        "https://identity.example", "integrity-subject", "integrity@example.test", "Integrity Reader"
    )
    original_username = sso_reader["username"]
    enroll_session, _ = profiles.create_session(sso_reader["id"])
    sibling_session, _ = profiles.create_session(sso_reader["id"])
    assert profiles.mark_session_enrollment_verified(enroll_session) is True

    with pytest.raises(sqlite3.IntegrityError):
        profiles.update_password(
            sso_reader["id"], "candidate-password-long", username="Already-Taken"
        )

    with TestClient(app) as client:
        conflict = client.post(
            "/auth/password/change",
            headers={"x-bookward-session": enroll_session},
            json={"username": "Already-Taken", "new_password": "candidate-password-long"},
        )
        assert conflict.status_code == 409

        # Neither the account row nor active sessions were mutated or revoked on conflict.
        unchanged = profiles.account_by_id(sso_reader["id"])
        assert unchanged["username"] == original_username
        assert unchanged["has_local_username"] is False
        with profiles.auth_connect() as con:
            raw = con.execute(
                "SELECT password_hash, auth_version FROM accounts WHERE id=?",
                (sso_reader["id"],),
            ).fetchone()
        assert raw["password_hash"] is None
        assert raw["auth_version"] == 1
        assert profiles.resolve_session(sibling_session) is not None
        verified_session = profiles.resolve_session(enroll_session)
        assert verified_session is not None
        assert verified_session["enrollment_verified"] is True

        # Retrying immediately with a unique username succeeds on the still-verified session.
        retry = client.post(
            "/auth/password/change",
            headers={"x-bookward-session": enroll_session},
            json={"username": "available-reader", "new_password": "candidate-password-long"},
        )
        assert retry.status_code == 200
        assert retry.json()["username"] == "available-reader"
        assert retry.json()["has_local_username"] is True
        assert profiles.resolve_session(sibling_session) is None
        assert profiles.resolve_session(enroll_session) is None


def test_admin_password_reset_blocks_sso_only_accounts_and_rejects_reserved_usernames(identity_store):
    sso_reader = profiles.create_oidc_account(
        "https://identity.example", "admin-reset-subject", "sso-admin@example.test", "SSO Managed"
    )
    admin_session, _ = profiles.create_session(identity_store["id"])

    with TestClient(app, headers={"x-bookward-session": admin_session}) as client:
        reserved_create = client.post(
            "/admin/accounts",
            json={
                "username": "oidc:manual",
                "password": "temporary-password-123",
                "display_name": "Reserved",
                "role": "user",
            },
        )
        assert reserved_create.status_code == 400

        blocked_reset = client.post(
            f"/admin/accounts/{sso_reader['id']}/password",
            json={"password": "temporary-password-123"},
        )
        assert blocked_reset.status_code == 400

    reloaded = profiles.account_by_id(sso_reader["id"])
    assert reloaded["must_change_password"] is False

