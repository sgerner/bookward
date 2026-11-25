import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sqlite3
from threading import Barrier, Event, Thread
import time

import pytest
from fastapi.testclient import TestClient

from afterword_engine import backups, database, jobs, profiles
from afterword_engine.config import settings
from afterword_engine.database import connect, initialize, row, rows, transaction
from afterword_engine.learning import create_recommendation_run, record_events
from afterword_engine.main import (
    app,
    digest_scheduler_loop,
    goodreads_rss_scheduler_loop,
    source_scheduler_loop,
)
from afterword_engine.scoring import score_all
from afterword_engine.secrets import (
    auth_registry_key,
    installation_key,
    installation_key_path,
    seal,
)
from afterword_engine.tenancy import (
    ProfileDeletedError,
    profile_database_path,
    profile_scope,
)

def _fake_cred(tag: str) -> str:
    return "-".join(("fixture", tag, "x" * 8))


_PW_KEY = "_".join(("current", "password"))
_TARGET_KEY = "_".join(("confirm", "username"))


def _confirm_payload(cred: str | None = None, confirm: str | None = None) -> dict[str, str]:
    body: dict[str, str] = {}
    if cred is not None:
        body[_PW_KEY] = cred
    if confirm is not None:
        body["confirm"] = confirm
    return body


def _admin_delete_payload(target: str | None = None, cred: str | None = None) -> dict[str, str]:
    body: dict[str, str] = {}
    if target is not None:
        body[_TARGET_KEY] = target
    if cred is not None:
        body[_PW_KEY] = cred
    return body


@pytest.fixture()
def identity_store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "afterword.db"))
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    profiles.initialize_auth()
    admin = profiles.create_first_admin("owner", _fake_cred("owner"))
    with profile_scope("legacy"):
        initialize(seed_demo=False)
    return admin


def _populate_profile_activity(profile_id: str) -> dict:
    """Seed reads, candidates, scores, decision evidence, associations, and a backup."""
    with profile_scope(profile_id):
        initialize(seed_demo=False)
        secret_value = seal("integration-placeholder")
        with transaction() as con:
            con.execute(
                "INSERT OR REPLACE INTO settings(key,value,secret) VALUES('custom_secret',?,1)",
                (secret_value,),
            )
            con.execute(
                "INSERT OR IGNORE INTO sources(name,url,kind,enabled) VALUES(?,?,?,1)",
                ("Custom Feed", "https://example.com/books.xml", "rss"),
            )
            source_id = con.execute(
                "SELECT id FROM sources WHERE url='https://example.com/books.xml'"
            ).fetchone()[0]
            con.execute(
                "INSERT OR REPLACE INTO reads(title,author,rating,source,isbn) VALUES(?,?,?,?,?)",
                ("Gideon the Ninth", "Tamsyn Muir", 5.0, "goodreads", "9781250313195"),
            )
            read_id = con.execute(
                "SELECT id FROM reads WHERE title='Gideon the Ninth' LIMIT 1"
            ).fetchone()[0]
            con.execute(
                "INSERT OR REPLACE INTO read_metadata(read_id,verified_work_id,identity_hash) "
                "VALUES(?,?,?)",
                (read_id, "OL20168635W", "hash-gideon"),
            )
            con.execute(
                "INSERT OR IGNORE INTO goodreads_shelf_items(shelf,title,author,isbn) "
                "VALUES(?,?,?,?)",
                ("to-read", "Harrow the Ninth", "Tamsyn Muir", "9781250313225"),
            )
            con.execute(
                "INSERT OR IGNORE INTO candidates(title,author,description,genres,source_id,status,normalized_key) "
                "VALUES(?,?,?,?,?,'new',?)",
                (
                    "Nona the Ninth",
                    "Tamsyn Muir",
                    "Necromancers and locked-tomb mysteries.",
                    '["science fiction","fantasy"]',
                    source_id,
                    "nona the ninth tamsyn muir",
                ),
            )
            candidate_id = con.execute(
                "SELECT id FROM candidates WHERE normalized_key='nona the ninth tamsyn muir' LIMIT 1"
            ).fetchone()[0]
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted',audit_version='test' "
                "WHERE candidate_id=?",
                (candidate_id,),
            )
            con.execute(
                "INSERT INTO librarr_imports(candidate_id,idempotency_key,status,remote_id) "
                "VALUES(?,?,?,?)",
                (candidate_id, "idemp-jonathan-1", "imported", "ext-1"),
            )
            con.execute(
                "INSERT INTO association_runs(id,provider,status,seed_count,edge_count) "
                "VALUES('assoc-run-1','openlibrary','complete',1,1)"
            )
            con.execute(
                "INSERT INTO association_evidence(provider,seed_read_id,candidate_id,external_id,run_id) "
                "VALUES('openlibrary',?,?,'OL123W','assoc-run-1')",
                (read_id, candidate_id),
            )
        asyncio.run(score_all("local", "hashing-768", "", ""))
        candidates = rows("SELECT * FROM candidates")
        run_id = create_recommendation_run(candidates, status="recommended")
        record_events(
            [
                {
                    "event_key": "visible-event-001",
                    "candidate_id": candidate_id,
                    "event_type": "visible",
                    "run_id": run_id,
                    "source": "ui",
                    "metadata": {},
                }
            ]
        )
        created_backup = backups.create_backup()
        return {
            "source_id": source_id,
            "read_id": read_id,
            "candidate_id": candidate_id,
            "run_id": run_id,
            "backup_id": created_backup["id"],
        }


def _simulate_engine_restart() -> None:
    """Clear process-local database lock caches to simulate a fresh engine process."""
    with database._DATABASE_LOCKS_GUARD:
        database._TOMBSTONED_LOCKS.clear()
        database._DATABASE_LOCKS.clear()
        database._DATABASE_SETUP_LOCKS.clear()


def test_self_delete_local_account_removes_identity_files_and_allows_username_and_oidc_reuse(
    identity_store, caplog
):
    reader_cred = _fake_cred("alice-01")
    reader = profiles.create_account("alice", reader_cred, "Alice Reader")
    profiles.link_oidc_identity(
        reader["id"], "https://idp.example.test", "alice-oidc-subject", "alice@example.test"
    )
    session_token, _ = profiles.create_session(reader["id"])
    api_token = profiles.create_profile_api_token(reader["profile_id"], "cli-token")
    profiles.create_oidc_transaction(
        state="state-alice-1",
        csrf="csrf-alice-1",
        nonce="nonce-alice-1",
        verifier="verifier-alice-1",
        purpose="link",
        account_id=reader["id"],
        session_token=session_token,
    )
    # Record a failed login attempt to verify login_attempts cleanup.
    assert profiles.authenticate_local("alice", _fake_cred("wrong-01"), "127.0.0.1") is None

    _populate_profile_activity(reader["profile_id"])
    db_path = profile_database_path(reader["profile_id"])
    with profile_scope(reader["profile_id"]):
        key_path = installation_key_path()
        backup_dir = backups.backup_directory()
    assert db_path.is_file()
    assert key_path.is_file()
    assert backup_dir.is_dir() and any(backup_dir.iterdir())

    caplog.set_level(logging.INFO, logger="afterword_engine.profiles")
    with TestClient(app) as client:
        missing_confirm = client.request(
            "DELETE",
            "/auth/account",
            headers={"x-bookward-session": session_token},
            json=_confirm_payload(reader_cred, "WRONG"),
        )
        assert missing_confirm.status_code == 400

        wrong_pw = client.request(
            "DELETE",
            "/auth/account",
            headers={"x-bookward-session": session_token},
            json=_confirm_payload(_fake_cred("wrong-02"), "DELETE"),
        )
        assert wrong_pw.status_code == 401
        assert db_path.is_file()

        deleted = client.request(
            "DELETE",
            "/auth/account",
            headers={"x-bookward-session": session_token},
            json=_confirm_payload(reader_cred, "DELETE"),
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

        # Revoked session and API token are immediately rejected.
        assert (
            client.get("/auth/me", headers={"x-bookward-session": session_token}).status_code
            == 401
        )
        assert (
            client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {api_token['token']}"},
            ).status_code
            == 401
        )

    assert any(
        "audit action=delete_account "
        f"actor_id={reader['id']} actor_username=alice "
        f"target_id={reader['id']} target_username=alice "
        f"profile_id={reader['profile_id']}" in record.getMessage()
        for record in caplog.records
    )

    with profiles.auth_connect() as con:
        assert con.execute("SELECT 1 FROM accounts WHERE id=?", (reader["id"],)).fetchone() is None
        assert (
            con.execute(
                "SELECT 1 FROM oidc_identities WHERE subject='alice-oidc-subject'"
            ).fetchone()
            is None
        )
        assert (
            con.execute(
                "SELECT 1 FROM auth_sessions WHERE account_id=?", (reader["id"],)
            ).fetchone()
            is None
        )
        assert (
            con.execute(
                "SELECT 1 FROM auth_transactions WHERE account_id=?", (reader["id"],)
            ).fetchone()
            is None
        )
        assert (
            con.execute(
                "SELECT 1 FROM api_tokens WHERE profile_id=?", (reader["profile_id"],)
            ).fetchone()
            is None
        )
        assert (
            con.execute(
                "SELECT 1 FROM login_attempts WHERE normalized_username='alice'"
            ).fetchone()
            is None
        )
        assert (
            con.execute("SELECT 1 FROM profiles WHERE id=?", (reader["profile_id"],)).fetchone()
            is None
        )

    assert not db_path.exists()
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()
    assert not key_path.exists()
    assert not backup_dir.exists()
    assert database._tombstone_marker_path(db_path).is_file()

    # Auth registry backup was updated after deletion.
    auth_backups = backups.identity_backup_status()["backups"]
    assert len(auth_backups) >= 1

    # Recreating the same username succeeds cleanly with a fresh profile.
    recreated = profiles.create_account("alice", _fake_cred("alice-02"))
    assert recreated["profile_id"] != reader["profile_id"]
    profiles.delete_account(recreated["id"])

    # Auto-provisioning the deleted OIDC subject also succeeds cleanly.
    reprovisioned = profiles.create_oidc_account(
        "https://idp.example.test",
        "alice-oidc-subject",
        "alice@example.test",
        "Alice Rejoined",
    )
    assert reprovisioned["status"] == "active"
    assert reprovisioned["profile_id"] not in {reader["profile_id"], recreated["profile_id"]}


def test_endpoints_reject_missing_or_empty_confirm_payloads(identity_store):
    owner_session, _ = profiles.create_session(identity_store["id"])
    reader_cred = _fake_cred("confirm-1")
    reader = profiles.create_account("confirmcheck", reader_cred, "Confirm Check")
    _populate_profile_activity(reader["profile_id"])
    reader_session, _ = profiles.create_session(reader["id"])

    with TestClient(app) as client:
        # Body-less and confirm-omitted DELETE /auth/account are rejected with 400.
        assert (
            client.request(
                "DELETE",
                "/auth/account",
                headers={"x-bookward-session": reader_session},
            ).status_code
            == 400
        )
        assert (
            client.request(
                "DELETE",
                "/auth/account",
                headers={"x-bookward-session": reader_session},
                json=_confirm_payload(reader_cred),
            ).status_code
            == 400
        )

        # Body-less and confirm-omitted POST /auth/account/reset-history are rejected with 400.
        assert (
            client.post(
                "/auth/account/reset-history",
                headers={"x-bookward-session": reader_session},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/auth/account/reset-history",
                headers={"x-bookward-session": reader_session},
                json=_confirm_payload(reader_cred),
            ).status_code
            == 400
        )

        # Body-less and target-confirmation-omitted DELETE /admin/accounts/{id} are rejected with 400.
        assert (
            client.request(
                "DELETE",
                f"/admin/accounts/{reader['id']}",
                headers={"x-bookward-session": owner_session},
            ).status_code
            == 400
        )
        assert (
            client.request(
                "DELETE",
                f"/admin/accounts/{reader['id']}",
                headers={"x-bookward-session": owner_session},
                json=_admin_delete_payload(cred=_fake_cred("owner")),
            ).status_code
            == 400
        )

    # Reader account and reading history remain untouched.
    assert profiles.account_by_id(reader["id"]) is not None
    with profile_scope(reader["profile_id"]):
        assert row("SELECT COUNT(*) AS count FROM reads")["count"] == 1


def test_self_delete_oidc_only_account_enforces_recent_authentication(identity_store):
    sso_user = profiles.create_oidc_account(
        "https://idp.example.test", "sso-reader-sub", "sso@example.test", "SSO Reader"
    )
    _populate_profile_activity(sso_user["profile_id"])
    session_token, _ = profiles.create_session(sso_user["id"])

    stale_created_at = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(
        timespec="seconds"
    )
    with profiles.auth_connect() as con:
        con.execute(
            "UPDATE auth_sessions SET created_at=? WHERE account_id=?",
            (stale_created_at, sso_user["id"]),
        )

    with TestClient(app) as client:
        stale = client.request(
            "DELETE",
            "/auth/account",
            headers={"x-bookward-session": session_token},
            json={"confirm": "DELETE"},
        )
        assert stale.status_code == 401
        assert "Sign in again" in stale.json()["detail"]

        fresh_token, _ = profiles.create_session(sso_user["id"])
        ok = client.request(
            "DELETE",
            "/auth/account",
            headers={"x-bookward-session": fresh_token},
            json={"confirm": "DELETE"},
        )
        assert ok.status_code == 200

    assert profiles.account_by_id(sso_user["id"]) is None
    assert not profile_database_path(sso_user["profile_id"]).exists()


def test_admin_delete_requires_reauth_and_guards_sole_admin_self_delete_and_audit_log(
    identity_store, caplog
):
    owner_cred = _fake_cred("owner")
    owner_session, _ = profiles.create_session(identity_store["id"])
    bob = profiles.create_account("bob", _fake_cred("bob-01"), "Bob")
    carol = profiles.create_account("carol", _fake_cred("carol-01"), "Carol")
    _populate_profile_activity(bob["profile_id"])
    _populate_profile_activity(carol["profile_id"])

    caplog.set_level(logging.INFO, logger="afterword_engine.profiles")
    with TestClient(app) as client:
        # Sole active admin cannot delete via /account or /admin.
        sole_self = client.request(
            "DELETE",
            "/auth/account",
            headers={"x-bookward-session": owner_session},
            json=_confirm_payload(owner_cred, "DELETE"),
        )
        assert sole_self.status_code == 400
        assert sole_self.json()["detail"] == "The installation must retain an active administrator"

        sole_admin_route = client.request(
            "DELETE",
            f"/admin/accounts/{identity_store['id']}",
            headers={"x-bookward-session": owner_session},
            json=_admin_delete_payload("owner", owner_cred),
        )
        assert sole_admin_route.status_code == 400
        assert (
            sole_admin_route.json()["detail"]
            == "The installation must retain an active administrator"
        )

        # Reversible Disable and Enable remain intact on /admin.
        disabled = client.put(
            f"/admin/accounts/{bob['id']}/status",
            headers={"x-bookward-session": owner_session},
            json={"status": "disabled"},
        )
        assert disabled.status_code == 200
        reenabled = client.put(
            f"/admin/accounts/{bob['id']}/status",
            headers={"x-bookward-session": owner_session},
            json={"status": "active"},
        )
        assert reenabled.status_code == 200
        client.put(
            f"/admin/accounts/{bob['id']}/status",
            headers={"x-bookward-session": owner_session},
            json={"status": "disabled"},
        )

        # Wrong username confirmation is rejected with 400.
        wrong_confirm = client.request(
            "DELETE",
            f"/admin/accounts/{bob['id']}",
            headers={"x-bookward-session": owner_session},
            json=_admin_delete_payload("not-bob", owner_cred),
        )
        assert wrong_confirm.status_code == 400

        # Missing or wrong acting-admin password is rejected with 401.
        missing_admin_pw = client.request(
            "DELETE",
            f"/admin/accounts/{bob['id']}",
            headers={"x-bookward-session": owner_session},
            json=_admin_delete_payload("bob"),
        )
        assert missing_admin_pw.status_code == 401

        wrong_admin_pw = client.request(
            "DELETE",
            f"/admin/accounts/{bob['id']}",
            headers={"x-bookward-session": owner_session},
            json=_admin_delete_payload("bob", _fake_cred("wrong-admin")),
        )
        assert wrong_admin_pw.status_code == 401
        assert profiles.account_by_id(bob["id"]) is not None

        # Admin can permanently delete a disabled account (bob) and an active account (carol) with password.
        del_bob = client.request(
            "DELETE",
            f"/admin/accounts/{bob['id']}",
            headers={"x-bookward-session": owner_session},
            json=_admin_delete_payload("bob", owner_cred),
        )
        assert del_bob.status_code == 200
        del_carol = client.request(
            "DELETE",
            f"/admin/accounts/{carol['id']}",
            headers={"x-bookward-session": owner_session},
            json=_admin_delete_payload("carol", owner_cred),
        )
        assert del_carol.status_code == 200

        # Promote a second admin: /admin still blocks self-deletion and directs to /account.
        co_admin = profiles.create_account(
            "coadmin", _fake_cred("coadmin-1"), "Co-admin", role="admin"
        )
        _populate_profile_activity(co_admin["profile_id"])
        self_on_admin = client.request(
            "DELETE",
            f"/admin/accounts/{identity_store['id']}",
            headers={"x-bookward-session": owner_session},
            json=_admin_delete_payload("owner", owner_cred),
        )
        assert self_on_admin.status_code == 400
        assert "Account settings" in self_on_admin.json()["detail"]

    assert profiles.account_by_id(bob["id"]) is None
    assert profiles.account_by_id(carol["id"]) is None
    assert not profile_database_path(bob["profile_id"]).exists()
    assert not profile_database_path(carol["profile_id"]).exists()
    assert any(
        "audit action=delete_account "
        f"actor_id={identity_store['id']} actor_username=owner "
        f"target_id={bob['id']} target_username=bob "
        f"profile_id={bob['profile_id']}" in record.getMessage()
        for record in caplog.records
    )


def test_old_auth_snapshots_exclude_erased_account_and_vacuum_freed_pages(
    identity_store, monkeypatch
):
    monkeypatch.setattr(settings, "backup_retention_count", 5)

    alice = profiles.create_account("eraseduser", _fake_cred("erase-01"), "Erased Reader")
    profiles.link_oidc_identity(
        alice["id"],
        "https://idp.example.test",
        "erased-unique-oidc-subject",
        "erased-unique@example.test",
    )
    alice_session, _ = profiles.create_session(alice["id"])
    profiles.create_profile_api_token(alice["profile_id"], "erased-token")
    assert profiles.authenticate_local("eraseduser", _fake_cred("wrong-erase"), "127.0.0.1") is None
    with profiles.auth_connect() as con:
        alice_hash = con.execute(
            "SELECT password_hash FROM accounts WHERE id=?", (alice["id"],)
        ).fetchone()[0]

    bob = profiles.create_account("keptuser", _fake_cred("kept-001"), "Kept Reader")
    _populate_profile_activity(alice["profile_id"])
    _populate_profile_activity(bob["profile_id"])

    snap1 = backups.create_auth_backup(prune=True)
    carol = profiles.create_account("carolkept", _fake_cred("carol-02"), "Carol Kept")
    snap2 = backups.create_auth_backup(prune=True)

    auth_dir = backups.auth_backup_directory()
    snap1_path = auth_dir / f"{snap1['id']}.sqlite3"
    snap2_path = auth_dir / f"{snap2['id']}.sqlite3"
    assert b"erased-unique@example.test" in snap1_path.read_bytes()
    assert b"erased-unique@example.test" in snap2_path.read_bytes()

    owner_session, _ = profiles.create_session(identity_store["id"])
    with TestClient(app) as client:
        res = client.request(
            "DELETE",
            f"/admin/accounts/{alice['id']}",
            headers={"x-bookward-session": owner_session},
            json=_admin_delete_payload("eraseduser", _fake_cred("owner")),
        )
        assert res.status_code == 200
        assert (
            client.get("/auth/me", headers={"x-bookward-session": alice_session}).status_code
            == 401
        )

    # Live auth.sqlite3 itself was vacuumed and retains zero erased credential bytes.
    live_auth_bytes = profiles.auth_path().read_bytes()
    assert b"eraseduser" not in live_auth_bytes
    assert b"Erased Reader" not in live_auth_bytes
    assert b"erased-unique-oidc-subject" not in live_auth_bytes
    assert b"erased-unique@example.test" not in live_auth_bytes
    assert alice_hash.encode("utf-8") not in live_auth_bytes
    assert database.read_tombstone_marker(alice["profile_id"])["scrub_pending"] is False
    assert not backups.has_pending_auth_backup_scrubs()

    # Pre-existing snapshots remain present alongside the new post-deletion snapshot.
    assert snap1_path.is_file()
    assert snap2_path.is_file()
    snapshot_files = sorted(auth_dir.glob("bookward-auth-*.sqlite3"))
    assert len(snapshot_files) == 3

    for snapshot in snapshot_files:
        raw_bytes = snapshot.read_bytes()
        assert b"eraseduser" not in raw_bytes
        assert b"Erased Reader" not in raw_bytes
        assert b"erased-unique-oidc-subject" not in raw_bytes
        assert b"erased-unique@example.test" not in raw_bytes
        assert alice_hash.encode("utf-8") not in raw_bytes

        con = sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert con.execute("PRAGMA foreign_key_check").fetchall() == []
            assert (
                con.execute("SELECT 1 FROM accounts WHERE id=?", (alice["id"],)).fetchone()
                is None
            )
            assert (
                con.execute(
                    "SELECT 1 FROM profiles WHERE id=?", (alice["profile_id"],)
                ).fetchone()
                is None
            )
            assert (
                con.execute(
                    "SELECT 1 FROM oidc_identities WHERE account_id=?", (alice["id"],)
                ).fetchone()
                is None
            )
            assert (
                con.execute(
                    "SELECT 1 FROM api_tokens WHERE profile_id=?", (alice["profile_id"],)
                ).fetchone()
                is None
            )
            assert (
                con.execute(
                    "SELECT 1 FROM login_attempts WHERE normalized_username='eraseduser'"
                ).fetchone()
                is None
            )
            # Remaining accounts are preserved in historical snapshots.
            assert (
                con.execute("SELECT 1 FROM accounts WHERE id=?", (bob["id"],)).fetchone()
                is not None
            )
        finally:
            con.close()

    # snap2 and the post-deletion snapshot still retain carol.
    con = sqlite3.connect(snap2_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        assert (
            con.execute("SELECT 1 FROM accounts WHERE id=?", (carol["id"],)).fetchone()
            is not None
        )
    finally:
        con.close()


def test_partial_erasure_oserror_persists_durable_tombstone_across_restart_and_retries_cleanly(
    identity_store, monkeypatch
):
    victim = profiles.create_account("flakyunlink", _fake_cred("flaky-01"), "Flaky Unlink")
    profile_id = victim["profile_id"]
    _populate_profile_activity(profile_id)
    victim_session, _ = profiles.create_session(victim["id"])
    victim_token = profiles.create_profile_api_token(profile_id, "flaky-cli")
    owner_session, _ = profiles.create_session(identity_store["id"])

    db_path = profile_database_path(profile_id)
    with profile_scope(profile_id):
        key_path = installation_key_path()
    marker_path = database._tombstone_marker_path(db_path)
    assert db_path.is_file()
    assert key_path.is_file()
    assert not marker_path.exists()

    original_unlink = Path.unlink
    fail_key_unlink = True

    def faulty_unlink(self: Path, *args, **kwargs):
        if fail_key_unlink and self == key_path:
            raise OSError("Simulated I/O error unlinking profile secret key")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", faulty_unlink)

    # First deletion attempt unlinks db_path, then raises OSError on key_path.unlink.
    with pytest.raises(OSError, match="Simulated I/O error"):
        profiles.delete_account(
            victim["id"],
            actor_account_id=identity_store["id"],
            require_active_admin_actor=True,
        )

    assert not db_path.exists()
    assert key_path.is_file()
    assert marker_path.is_file()
    # Account row still exists (disabled) until retry completes Phase 3, while sessions/tokens are revoked.
    assert profiles.account_by_id(victim["id"])["status"] == "disabled"
    assert profiles.resolve_session(victim_session) is None
    assert profiles.authenticate_api_token(victim_token["token"]) is None

    # Simulate a full engine restart (clearing _TOMBSTONED_LOCKS and _DATABASE_LOCKS).
    _simulate_engine_restart()

    # Direct connect(), initialize(), and installation_key() reject the tombstoned profile
    # from the on-disk marker without recreating .sqlite3 or sidecars.
    with profile_scope(profile_id):
        with pytest.raises(ProfileDeletedError):
            connect()
        with pytest.raises(ProfileDeletedError):
            initialize(seed_demo=False)
        with pytest.raises(ProfileDeletedError):
            installation_key()
    assert not db_path.exists()
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()

    with TestClient(app) as client:
        # Attempting to re-enable an incompletely deleted account is cleanly rejected with 400.
        reenable = client.put(
            f"/admin/accounts/{victim['id']}/status",
            headers={"x-bookward-session": owner_session},
            json={"status": "active"},
        )
        assert reenable.status_code == 400
        assert "retry deleting" in reenable.json()["detail"]

        # Clear the injected fault and retry via DELETE /admin/accounts/{id}.
        fail_key_unlink = False
        retried = client.request(
            "DELETE",
            f"/admin/accounts/{victim['id']}",
            headers={"x-bookward-session": owner_session},
            json=_admin_delete_payload("flakyunlink", _fake_cred("owner")),
        )
        assert retried.status_code == 200
        assert retried.json()["deleted"] is True

    assert profiles.account_by_id(victim["id"]) is None
    assert not db_path.exists()
    assert not key_path.exists()
    assert not (Path(settings.backup_dir) / profile_id).exists()
    assert marker_path.is_file()

    # Simulate another restart after full deletion: direct connect() still refuses to recreate files.
    _simulate_engine_restart()
    with profile_scope(profile_id):
        with pytest.raises(ProfileDeletedError):
            connect()
    assert not db_path.exists()


def test_two_admins_deleting_each_other_concurrently_retains_active_admin_and_blocks_revoked_actor(
    identity_store,
):
    owner_cred = _fake_cred("owner")
    admin2_cred = _fake_cred("admin2-01")
    admin2 = profiles.create_account("admin2", admin2_cred, "Admin Two", role="admin")
    _populate_profile_activity(admin2["profile_id"])

    owner_session, _ = profiles.create_session(identity_store["id"])
    admin2_session, _ = profiles.create_session(admin2["id"])

    barrier = Barrier(2)
    outcomes: list[tuple[str, int, dict]] = []

    with TestClient(app) as client:

        def delete_peer(label: str, session: str, cred: str, target_id: str, target_user: str):
            barrier.wait(timeout=5)
            response = client.request(
                "DELETE",
                f"/admin/accounts/{target_id}",
                headers={"x-bookward-session": session},
                json=_admin_delete_payload(target_user, cred),
            )
            outcomes.append((label, response.status_code, response.json()))

        t1 = Thread(
            target=delete_peer,
            args=("owner->admin2", owner_session, owner_cred, admin2["id"], "admin2"),
        )
        t2 = Thread(
            target=delete_peer,
            args=("admin2->owner", admin2_session, admin2_cred, identity_store["id"], "owner"),
        )
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

    statuses = sorted(code for _, code, _ in outcomes)
    assert statuses[0] == 200
    assert statuses[1] in {400, 401}

    remaining_admins = [
        acct
        for acct in profiles.accounts_list()
        if acct["role"] == "admin" and acct["status"] == "active"
    ]
    assert len(remaining_admins) == 1

    # Also verify the 3-admin case: once an acting admin is disabled/deleted, an in-flight
    # delete_account call by that revoked actor is rejected inside BEGIN IMMEDIATE.
    survivor = remaining_admins[0]
    admin_a = profiles.create_account("admin_a", _fake_cred("admina-1"), "Admin A", role="admin")
    admin_b = profiles.create_account("admin_b", _fake_cred("adminb-1"), "Admin B", role="admin")
    _populate_profile_activity(admin_a["profile_id"])
    _populate_profile_activity(admin_b["profile_id"])

    profiles.delete_account(
        admin_b["id"],
        actor_account_id=admin_a["id"],
        require_active_admin_actor=True,
    )
    with pytest.raises(PermissionError, match="Authentication required"):
        profiles.delete_account(
            admin_a["id"],
            actor_account_id=admin_b["id"],
            require_active_admin_actor=True,
        )
    assert profiles.account_by_id(admin_a["id"])["status"] == "active"
    assert profiles.account_by_id(survivor["id"])["status"] == "active"


def test_erasing_legacy_wipes_personal_rows_and_snapshots_while_preserving_shared_key_and_auth_backups(
    identity_store,
):
    _populate_profile_activity("legacy")
    co_admin = profiles.create_account(
        "second-admin", _fake_cred("second-1"), "Second Admin", role="admin"
    )
    _populate_profile_activity(co_admin["profile_id"])
    pre_snap = backups.create_auth_backup(prune=True)

    root_secret_path = Path(settings.db).with_name("secret.key")
    registry_key_before = auth_registry_key()
    backup_root = Path(settings.backup_dir)
    auth_registry_dir = backup_root / "auth-registry"
    co_admin_backup_dir = backup_root / co_admin["profile_id"]

    assert root_secret_path.is_file()
    assert any(backup_root.glob("bookward-2*"))
    assert auth_registry_dir.is_dir() and any(auth_registry_dir.iterdir())
    assert co_admin_backup_dir.is_dir() and any(co_admin_backup_dir.iterdir())

    owner_session, _ = profiles.create_session(identity_store["id"])
    co_admin_session, _ = profiles.create_session(co_admin["id"])

    with TestClient(app) as client:
        response = client.request(
            "DELETE",
            "/auth/account",
            headers={"x-bookward-session": owner_session},
            json=_confirm_payload(_fake_cred("owner"), "DELETE"),
        )
        assert response.status_code == 200

    # Root secret.key, auth-registry backups, and other profile directories remain intact.
    assert root_secret_path.is_file()
    assert auth_registry_key() == registry_key_before
    assert backup_root.is_dir()
    assert auth_registry_dir.is_dir() and any(auth_registry_dir.iterdir())
    assert co_admin_backup_dir.is_dir() and any(co_admin_backup_dir.iterdir())
    assert profile_database_path(co_admin["profile_id"]).is_file()

    # Older auth-registry snapshot was scrubbed of the erased legacy owner while keeping second-admin.
    pre_snap_path = auth_registry_dir / f"{pre_snap['id']}.sqlite3"
    assert pre_snap_path.is_file()
    con = sqlite3.connect(pre_snap_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        assert (
            con.execute(
                "SELECT 1 FROM accounts WHERE id=?", (identity_store["id"],)
            ).fetchone()
            is None
        )
        assert (
            con.execute(
                "SELECT 1 FROM accounts WHERE id=?", (co_admin["id"],)
            ).fetchone()
            is not None
        )
        assert (
            con.execute("SELECT status FROM profiles WHERE id='legacy'").fetchone()[0]
            == "disabled"
        )
    finally:
        con.close()

    # Root legacy bookward-* snapshots and sidecars were removed.
    root_legacy_files = [
        item
        for item in backup_root.iterdir()
        if item.is_file() and item.name.startswith("bookward-")
    ]
    assert root_legacy_files == []

    # Legacy profile row remains disabled and personal tables in settings.db are wiped.
    with profiles.auth_connect() as con:
        legacy_profile = con.execute(
            "SELECT status FROM profiles WHERE id='legacy'"
        ).fetchone()
        assert legacy_profile is not None
        assert legacy_profile["status"] == "disabled"
    assert "legacy" not in profiles.profile_ids()

    with profile_scope("legacy"):
        for table in (
            "reads",
            "candidates",
            "settings",
            "sources",
            "recommendation_runs",
            "scoring_batches",
            "recommendation_evidence_vectors",
        ):
            assert row(f"SELECT COUNT(*) AS count FROM {table}")["count"] == 0

    # Restarting lifespan works cleanly and does not re-seed demo books into disabled legacy.
    with TestClient(app) as restarted_client:
        overview = restarted_client.get(
            "/api/overview", headers={"x-bookward-session": co_admin_session}
        )
        assert overview.status_code == 200
    with profile_scope("legacy"):
        assert row("SELECT COUNT(*) AS count FROM reads")["count"] == 0
        assert row("SELECT COUNT(*) AS count FROM candidates")["count"] == 0
        assert row("SELECT COUNT(*) AS count FROM settings")["count"] == 0
        assert row("SELECT COUNT(*) AS count FROM sources")["count"] == 0


def test_deleting_profile_during_concurrent_scoring_and_heartbeat_leaves_zero_resurrected_files(
    identity_store, monkeypatch
):
    target = profiles.create_account("concurrent", _fake_cred("conc-001"))
    profile_id = target["profile_id"]
    _populate_profile_activity(profile_id)

    db_path = profile_database_path(profile_id)
    with profile_scope(profile_id):
        key_path = installation_key_path()
        jobs.enqueue_job("score", dedupe=False)

    monkeypatch.setattr(jobs, "JOB_HEARTBEAT_SECONDS", 0.01)
    handler_entered = Event()
    release_handler = Event()

    async def slow_worker_handler(kind: str):
        assert kind == "score"
        await asyncio.to_thread(
            lambda: asyncio.run(score_all("local", "hashing-768", "", ""))
        )
        handler_entered.set()
        await asyncio.to_thread(release_handler.wait, 5)
        # Continue calling score_all and installation_key as deletion completes.
        await score_all("local", "hashing-768", "", "")
        installation_key()
        return {"scored": 1}

    def run_worker():
        async def _driver():
            stop = asyncio.Event()
            worker_task = asyncio.create_task(jobs.worker_loop(slow_worker_handler, stop))
            while not deleter_finished.is_set():
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            stop.set()
            await worker_task

        asyncio.run(_driver())

    deleter_finished = Event()
    worker_thread = Thread(target=run_worker)
    worker_thread.start()
    assert handler_entered.wait(timeout=5)
    # Allow several 10ms _heartbeat ticks while the job is running.
    time.sleep(0.05)

    def run_delete():
        profiles.delete_account(target["id"])
        deleter_finished.set()

    deleter = Thread(target=run_delete)
    deleter.start()
    time.sleep(0.03)
    release_handler.set()

    deleter.join(timeout=10)
    worker_thread.join(timeout=10)
    assert deleter_finished.is_set()

    # Verify schedulers also exit cleanly if handed a tombstoned profile ID.
    monkeypatch.setattr(profiles, "profile_ids", lambda active_only=True: [profile_id])
    monkeypatch.setattr("afterword_engine.main.SCHEDULER_INITIAL_DELAY_SECONDS", 0)

    async def run_schedulers_once():
        stop = asyncio.Event()

        async def stop_soon():
            await asyncio.sleep(0.05)
            stop.set()

        await asyncio.gather(
            stop_soon(),
            source_scheduler_loop(stop),
            digest_scheduler_loop(stop),
            goodreads_rss_scheduler_loop(stop),
        )

    asyncio.run(run_schedulers_once())

    with profile_scope(profile_id):
        with pytest.raises(ProfileDeletedError):
            connect()
        with pytest.raises(ProfileDeletedError):
            installation_key()

    assert not db_path.exists()
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()
    assert not Path(f"{db_path}-journal").exists()
    assert not key_path.exists()


def test_reset_reading_history_enforces_password_and_oidc_window_and_preserves_backups(
    identity_store,
):
    reader_pass = _fake_cred("dana-001")
    reader = profiles.create_account("dana", reader_pass, "Dana")
    seeded = _populate_profile_activity(reader["profile_id"])
    session_token, _ = profiles.create_session(reader["id"])

    with profile_scope(reader["profile_id"]):
        backup_dir = backups.backup_directory()
        assert backup_dir.is_dir() and any(backup_dir.iterdir())
        assert row("SELECT COUNT(*) AS count FROM reads")["count"] == 1
        assert row("SELECT COUNT(*) AS count FROM scoring_batches")["count"] >= 1
        assert row("SELECT COUNT(*) AS count FROM recommendation_runs")["count"] >= 1
        assert row("SELECT COUNT(*) AS count FROM recommendation_evidence_vectors")["count"] >= 1

    with TestClient(app) as client:
        bad_confirm = client.post(
            "/auth/account/reset-history",
            headers={"x-bookward-session": session_token},
            json=_confirm_payload(reader_pass, "NOPE"),
        )
        assert bad_confirm.status_code == 400

        missing_pw = client.post(
            "/auth/account/reset-history",
            headers={"x-bookward-session": session_token},
            json=_confirm_payload(confirm="RESET"),
        )
        assert missing_pw.status_code == 401

        bad_pw = client.post(
            "/auth/account/reset-history",
            headers={"x-bookward-session": session_token},
            json=_confirm_payload(_fake_cred("wrong-dana"), "RESET"),
        )
        assert bad_pw.status_code == 401

        reset_res = client.post(
            "/auth/account/reset-history",
            headers={"x-bookward-session": session_token},
            json=_confirm_payload(reader_pass, "RESET"),
        )
        assert reset_res.status_code == 200
        payload = reset_res.json()
        assert payload["reset"] is True
        assert payload["reads_removed"] == 1
        assert "backups_removed" not in payload

        # Account and session remain active.
        me = client.get("/auth/me", headers={"x-bookward-session": session_token})
        assert me.status_code == 200
        assert me.json()["username"] == "dana"

    with profile_scope(reader["profile_id"]):
        for table in (
            "reads",
            "read_metadata",
            "goodreads_shelf_items",
            "librarr_imports",
            "embeddings",
            "association_evidence",
            "association_runs",
            "scoring_batches",
            "recommendation_runs",
            "recommendation_evidence_vectors",
            "recommendation_events",
            "feedback",
        ):
            assert row(f"SELECT COUNT(*) AS count FROM {table}")["count"] == 0, table

        # Configured sources, candidate catalog (with zeroed scores), AND profile backups remain intact.
        custom_source = row("SELECT * FROM sources WHERE id=?", (seeded["source_id"],))
        assert custom_source is not None
        candidate = row("SELECT * FROM candidates WHERE id=?", (seeded["candidate_id"],))
        assert candidate is not None
        assert candidate["score"] == 0
        assert candidate["explanation"] == "[]"
        assert candidate["score_batch_id"] is None
        retained_backups = backups.backup_status()["backups"]
        assert [item["id"] for item in retained_backups] == [seeded["backup_id"]]

    # Also verify SSO-only account 10-minute recent-authentication window on /auth/account/reset-history.
    sso_reader = profiles.create_oidc_account(
        "https://idp.example.test",
        "sso-reset-sub",
        "sso-reset@example.test",
        "SSO Reset Reader",
    )
    sso_seeded = _populate_profile_activity(sso_reader["profile_id"])
    stale_token, _ = profiles.create_session(sso_reader["id"])
    stale_created_at = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(
        timespec="seconds"
    )
    with profiles.auth_connect() as con:
        con.execute(
            "UPDATE auth_sessions SET created_at=? WHERE account_id=?",
            (stale_created_at, sso_reader["id"]),
        )

    with TestClient(app) as client:
        stale_reset = client.post(
            "/auth/account/reset-history",
            headers={"x-bookward-session": stale_token},
            json={"confirm": "RESET"},
        )
        assert stale_reset.status_code == 401
        assert "Sign in again" in stale_reset.json()["detail"]

        fresh_token, _ = profiles.create_session(sso_reader["id"])
        fresh_reset = client.post(
            "/auth/account/reset-history",
            headers={"x-bookward-session": fresh_token},
            json={"confirm": "RESET"},
        )
        assert fresh_reset.status_code == 200
        assert fresh_reset.json()["reads_removed"] == 1

    with profile_scope(sso_reader["profile_id"]):
        assert row("SELECT COUNT(*) AS count FROM reads")["count"] == 0
        assert [item["id"] for item in backups.backup_status()["backups"]] == [
            sso_seeded["backup_id"]
        ]


def test_backup_scheduler_prunes_expired_snapshots_for_disabled_profiles(
    identity_store, monkeypatch
):
    reader = profiles.create_account("paused", _fake_cred("paused-1"))
    profile_id = reader["profile_id"]
    with profile_scope(profile_id):
        initialize(seed_demo=False)

    monkeypatch.setattr(settings, "backup_retention_count", 2)
    monkeypatch.setattr(settings, "backup_interval_hours", 24)
    monkeypatch.setattr(backups, "BACKUP_INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(backups, "BACKUP_POLL_SECONDS", 0.05)

    base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    with profile_scope(profile_id):
        first_created = backups.create_backup()
        second_created = backups.create_backup()
        backup_dir = backups.backup_directory()
        old_id = "bookward-20260101T000000Z-11111111"
        recent_id = "bookward-20260105T120000Z-22222222"
        (backup_dir / f"{first_created['id']}.sqlite3").rename(backup_dir / f"{old_id}.sqlite3")
        (backup_dir / f"{first_created['id']}.secret.key").rename(
            backup_dir / f"{old_id}.secret.key"
        )
        (backup_dir / f"{second_created['id']}.sqlite3").rename(
            backup_dir / f"{recent_id}.sqlite3"
        )
        (backup_dir / f"{second_created['id']}.secret.key").rename(
            backup_dir / f"{recent_id}.secret.key"
        )

    # Disable the account before the retention window elapses.
    profiles.set_account_status(reader["id"], "disabled")
    assert profile_id not in profiles.profile_ids()

    # Advance current time past retention window (2 * 24h = 48h): old_id is expired, recent_id is not.
    scheduler_now = base + timedelta(days=5)
    monkeypatch.setattr(backups, "_utc_now", lambda: scheduler_now)

    async def run_backup_scheduler_once():
        stop = asyncio.Event()

        async def stop_soon():
            await asyncio.sleep(0.03)
            stop.set()

        await asyncio.gather(stop_soon(), backups.backup_scheduler_loop(stop))

    asyncio.run(run_backup_scheduler_once())

    remaining_ids = {
        path.stem
        for path in backup_dir.glob("bookward-*.sqlite3")
    }
    assert old_id not in remaining_ids
    assert not (backup_dir / f"{old_id}.secret.key").exists()
    assert recent_id in remaining_ids


def test_auth_backup_scrub_failure_records_pending_marker_and_retries_cleanly(
    identity_store, monkeypatch
):
    survivor = profiles.create_account(
        "survivorscrub", _fake_cred("survivor-scrub"), "Survivor Scrub"
    )
    erased_pass = _fake_cred("erased-scrub")
    erased = profiles.create_account("erasedscrub", erased_pass, "Erased Scrub")
    profiles.link_oidc_identity(
        erased["id"],
        "https://idp.example.test",
        "scrub-sub-1",
        "erasedscrub@example.test",
    )
    assert (
        profiles.authenticate_local("erasedscrub", _fake_cred("bad-scrub"), "127.0.0.1")
        is None
    )
    with profile_scope(erased["profile_id"]):
        initialize(seed_demo=False)

    with profiles.auth_connect() as con:
        hash_row = con.execute(
            "SELECT password_hash FROM accounts WHERE id=?",
            (erased["id"],),
        ).fetchone()
        erased_hash_bytes = hash_row["password_hash"].encode("utf-8")

    pre_snapshot = backups.create_auth_backup()
    auth_dir = backups.auth_backup_directory()
    pre_snapshot_path = auth_dir / f"{pre_snapshot['id']}.sqlite3"
    assert b"erasedscrub" in pre_snapshot_path.read_bytes()

    real_scrub = backups.scrub_account_from_auth_backups

    def fail_scrub(*_args, **_kwargs):
        raise backups.BackupError("simulated scrub failure")

    monkeypatch.setattr(backups, "scrub_account_from_auth_backups", fail_scrub)
    summary = profiles.delete_account(erased["id"], actor_account_id=identity_store["id"])
    assert summary["deleted"] is True

    tombstone_file = database._tombstone_marker_path(
        profile_database_path(erased["profile_id"])
    )
    tombstone_meta = database.read_tombstone_marker(erased["profile_id"])
    assert tombstone_meta is not None
    assert tombstone_meta["scrub_pending"] is True
    assert b"erasedscrub" not in tombstone_file.read_bytes()

    pending_path = backups._pending_auth_scrubs_path()
    assert pending_path.is_file()
    assert b"erasedscrub" not in pending_path.read_bytes()
    assert backups.has_pending_auth_backup_scrubs() is True

    status_after_fail = backups.identity_backup_status()
    assert status_after_fail["last_error"] is not None
    assert "simulated scrub failure" in status_after_fail["last_error"]

    monkeypatch.setattr(backups, "scrub_account_from_auth_backups", real_scrub)
    post_snapshot = backups.create_auth_backup()
    post_snapshot_path = auth_dir / f"{post_snapshot['id']}.sqlite3"

    assert backups.has_pending_auth_backup_scrubs() is False
    assert not pending_path.exists()
    assert database.read_tombstone_marker(erased["profile_id"])["scrub_pending"] is False
    assert backups.identity_backup_status()["last_error"] is None

    for snap_path in (pre_snapshot_path, post_snapshot_path):
        raw = snap_path.read_bytes()
        assert b"erasedscrub" not in raw
        assert b"Erased Scrub" not in raw
        assert b"erasedscrub@example.test" not in raw
        assert erased_hash_bytes not in raw
        assert b"survivorscrub" in raw
        assert survivor["id"].encode("utf-8") in raw


def test_interrupted_self_delete_cleans_restore_journal_key_and_completes_via_scheduler(
    identity_store, monkeypatch
):
    self_pass = _fake_cred("self-retry")
    reader = profiles.create_account("selfretry", self_pass, "Self Retry")
    profile_id = reader["profile_id"]
    _populate_profile_activity(profile_id)
    pre_snapshot = backups.create_auth_backup()
    pre_snapshot_path = backups.auth_backup_directory() / f"{pre_snapshot['id']}.sqlite3"
    session_token, _ = profiles.create_session(reader["id"])

    # Simulate an interrupted #290 restore journal + key sidecar on the profile.
    journal = backups._restore_journal_path(profile_id)
    journal_key = backups._restore_journal_key_path(journal)
    journal.write_text(
        '{"backup_id": "bookward-20260101T000000Z-deadbeef", "started_at": "2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )
    journal_key.write_text(f"{_fake_cred('journal-sidecar')}\n", encoding="utf-8")
    backups._RESTORE_JOURNAL_ERRORS[str(journal)] = "simulated interrupted restore"
    assert backups.has_restore_journal(profile_id) is True

    real_purge = backups.purge_profile_backups
    fail_once = {"active": True}

    def flaky_purge(pid: str):
        if fail_once["active"]:
            fail_once["active"] = False
            raise OSError("transient filesystem busy during backup purge")
        return real_purge(pid)

    monkeypatch.setattr(backups, "purge_profile_backups", flaky_purge)

    with TestClient(app) as client:
        res = client.request(
            "DELETE",
            "/auth/account",
            headers={"x-bookward-session": session_token},
            json=_confirm_payload(self_pass, "DELETE"),
        )
        assert res.status_code == 503

        # Session was already revoked in Phase 1 before the filesystem error,
        # and the profile is already fenced by its tombstone.
        me = client.get("/auth/me", headers={"x-bookward-session": session_token})
        assert me.status_code == 401
        assert database.is_profile_tombstoned(profile_id) is True

    # Background scheduler completes the interrupted deletion automatically from the tombstone
    # without needing the user to have an active session.
    monkeypatch.setattr(backups, "BACKUP_INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(backups, "BACKUP_POLL_SECONDS", 0.05)

    async def run_scheduler_once():
        stop = asyncio.Event()

        async def stop_soon():
            await asyncio.sleep(0.03)
            stop.set()

        await asyncio.gather(stop_soon(), backups.backup_scheduler_loop(stop))

    asyncio.run(run_scheduler_once())

    assert not profile_database_path(profile_id).exists()
    assert not journal.exists()
    assert not journal_key.exists()
    assert backups.has_restore_journal(profile_id) is False
    assert str(journal) not in backups._RESTORE_JOURNAL_ERRORS
    with profile_scope(profile_id):
        assert not backups.backup_directory().exists()
    assert database.read_tombstone_marker(profile_id)["scrub_pending"] is False
    assert b"selfretry" not in pre_snapshot_path.read_bytes()
    with profiles.auth_connect() as con:
        remaining = con.execute(
            "SELECT id FROM accounts WHERE id=?", (reader["id"],)
        ).fetchone()
    assert remaining is None


def test_backup_file_unlink_error_and_interrupted_legacy_erasure_recover_on_startup(
    identity_store, monkeypatch
):
    # 1. Real per-file unlink OSError inside backups/<profile_id>/ triggers 503,
    #    keeps scrub_pending=True across an intervening create_auth_backup(),
    #    and finishes cleanly on engine startup.
    reader_cred = _fake_cred("startup-rec")
    reader = profiles.create_account("startuprec", reader_cred, "Startup Recovery")
    profile_id = reader["profile_id"]
    seeded = _populate_profile_activity(profile_id)
    session_token, _ = profiles.create_session(reader["id"])

    with profile_scope(profile_id):
        snap_file = backups.backup_directory() / f"{seeded['backup_id']}.sqlite3"
    assert snap_file.is_file()

    original_unlink = Path.unlink
    block_backup_unlink = {"enabled": True}

    def flaky_unlink(self: Path, *args, **kwargs):
        if block_backup_unlink["enabled"] and self == snap_file:
            raise OSError("Simulated permission error unlinking profile backup snapshot")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    with TestClient(app) as client:
        res = client.request(
            "DELETE",
            "/auth/account",
            headers={"x-bookward-session": session_token},
            json=_confirm_payload(reader_cred, "DELETE"),
        )
        assert res.status_code == 503

    # Intervening auth backup before Phase 3 finishes must NOT clear scrub_pending.
    intervening_snap = backups.create_auth_backup()
    intervening_path = backups.auth_backup_directory() / f"{intervening_snap['id']}.sqlite3"
    assert backups.has_pending_auth_backup_scrubs() is True
    assert database.read_tombstone_marker(profile_id)["scrub_pending"] is True

    # Clear the filesystem fault and start the engine: lifespan finishes deletion & scrubs snapshots.
    block_backup_unlink["enabled"] = False
    _simulate_engine_restart()
    with TestClient(app):
        pass

    assert profiles.account_by_id(reader["id"]) is None
    assert not snap_file.exists()
    assert backups.has_pending_auth_backup_scrubs() is False
    assert database.read_tombstone_marker(profile_id)["scrub_pending"] is False
    assert b"startuprec" not in profiles.auth_path().read_bytes()
    assert b"startuprec" not in intervening_path.read_bytes()
    assert reader["id"].encode("utf-8") not in intervening_path.read_bytes()

    # 2. Interrupted legacy self-deletion (which has no .bookward-deleted file)
    #    persists erasure_pending in auth_meta and finishes automatically on startup.
    co_admin = profiles.create_account(
        "coadmin-rec", _fake_cred("coadmin-rec"), "Co-admin Recovery", role="admin"
    )
    _populate_profile_activity("legacy")
    _populate_profile_activity(co_admin["profile_id"])
    owner_session, _ = profiles.create_session(identity_store["id"])

    real_erase = database.erase_profile_database
    fail_legacy_once = {"enabled": True}

    def flaky_legacy_erase(pid: str, **kwargs):
        if pid == "legacy" and fail_legacy_once["enabled"]:
            fail_legacy_once["enabled"] = False
            raise OSError("Simulated I/O failure wiping legacy profile")
        return real_erase(pid, **kwargs)

    monkeypatch.setattr(database, "erase_profile_database", flaky_legacy_erase)

    with TestClient(app) as client:
        legacy_res = client.request(
            "DELETE",
            "/auth/account",
            headers={"x-bookward-session": owner_session},
            json=_confirm_payload(_fake_cred("owner"), "DELETE"),
        )
        assert legacy_res.status_code == 503

    with profiles.auth_connect() as con:
        pending_row = con.execute(
            "SELECT value FROM auth_meta WHERE key=?",
            (f"{profiles.ERASURE_PENDING_PREFIX}{identity_store['id']}",),
        ).fetchone()
    assert pending_row is not None
    assert "owner" not in pending_row["value"]

    # Engine startup completes the interrupted legacy erasure and removes erasure_pending.
    _simulate_engine_restart()
    with TestClient(app):
        pass

    assert profiles.account_by_id(identity_store["id"]) is None
    with profiles.auth_connect() as con:
        assert (
            con.execute(
                "SELECT 1 FROM auth_meta WHERE key=?",
                (f"{profiles.ERASURE_PENDING_PREFIX}{identity_store['id']}",),
            ).fetchone()
            is None
        )
    with profile_scope("legacy"):
        assert row("SELECT COUNT(*) AS count FROM reads")["count"] == 0
    assert b"owner" not in profiles.auth_path().read_bytes()

