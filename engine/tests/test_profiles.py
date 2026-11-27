from pathlib import Path
import asyncio
from datetime import timedelta
import hashlib
import logging
import sqlite3
import uuid
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
from afterword_engine.database import (
    MIGRATIONS,
    RestoreEpochError,
    bind_restore_epoch,
    connect,
    current_restore_epoch,
    database_lock,
    initialize,
    transaction,
)
import afterword_engine.jobs as jobs
from afterword_engine.jobs import enqueue_job, worker_loop
from afterword_engine.main import app, queue_goodreads_rss_if_due
from afterword_engine.secrets import installation_key, installation_key_path, seal, unseal
from afterword_engine.tenancy import current_profile_id, profile_database_path, profile_scope

OWNER_PASSWORD = f"owner-fixture-{uuid.uuid4().hex}"  # ggignore
WRONG_PASSWORD = f"wrong-fixture-{uuid.uuid4().hex}"  # ggignore


@pytest.fixture()
def identity_store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "afterword.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    profiles.initialize_auth()
    if profiles.account_count() == 0:
        admin = profiles.create_first_admin("owner", OWNER_PASSWORD)
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


def test_multi_ip_login_isolation_429_retry_after_single_warning_and_argon_skip(
    identity_store, monkeypatch, caplog
):
    from datetime import timedelta
    from afterword_engine import auth_admin

    proxy_secret = f"svc-{uuid.uuid4().hex}"
    monkeypatch.setattr(settings, "service_secret", proxy_secret)

    real_hasher = profiles.PASSWORD_HASHER

    class CountingPasswordHasher:
        def __init__(self):
            self.verify_calls = 0

        def verify(self, encoded, password):
            self.verify_calls += 1
            return real_hasher.verify(encoded, password)

        def __getattr__(self, name):
            return getattr(real_hasher, name)

    counting_hasher = CountingPasswordHasher()
    monkeypatch.setattr(profiles, "PASSWORD_HASHER", counting_hasher)

    # Seed an expired failure (>24h ago) to verify pruning during checks.
    with profiles.auth_connect() as con:
        con.execute(
            "INSERT INTO login_attempts(normalized_username,remote_address,succeeded,attempted_at) "
            "VALUES(?,?,0,?)",
            ("owner", "192.0.2.55", profiles.iso(profiles.utc_now() - timedelta(days=2))),
        )

    service_headers = {"x-bookward-service": proxy_secret}
    client = TestClient(app)

    # 10 failed logins spread across 203.0.113.1 .. 203.0.113.10 never lock out another IP.
    for octet in range(1, 11):
        failed = client.post(
            "/auth/login",
            json={"username": "owner", "password": WRONG_PASSWORD},
            headers={**service_headers, "x-bookward-client-ip": f"203.0.113.{octet}"},
        )
        assert failed.status_code == 401

    allowed = client.post(
        "/auth/login",
        json={"username": "owner", "password": OWNER_PASSWORD},
        headers={**service_headers, "x-bookward-client-ip": "198.51.100.42"},
    )
    assert allowed.status_code == 200

    with profiles.auth_connect() as con:
        expired = con.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE remote_address='192.0.2.55'"
        ).fetchone()[0]
    assert expired == 0

    # Trigger a 10-failure lockout on 203.0.113.1 (already has 1 failed attempt -> 9 more).
    caplog.clear()
    for _ in range(9):
        failed = client.post(
            "/auth/login",
            json={"username": "owner", "password": WRONG_PASSWORD},
            headers={**service_headers, "x-bookward-client-ip": "203.0.113.1"},
        )
        assert failed.status_code == 401

    calls_before_lockout = counting_hasher.verify_calls
    eleventh = client.post(
        "/auth/login",
        json={"username": "owner", "password": OWNER_PASSWORD},
        headers={**service_headers, "x-bookward-client-ip": "203.0.113.1"},
    )
    twelfth = client.post(
        "/auth/login",
        json={"username": "owner", "password": WRONG_PASSWORD},
        headers={**service_headers, "x-bookward-client-ip": "203.0.113.1"},
    )

    assert eleventh.status_code == 429
    assert twelfth.status_code == 429
    assert int(eleventh.headers["retry-after"]) > 0
    assert "Wait" in eleventh.json()["detail"]
    # Argon2id verification is skipped while locked out.
    assert counting_hasher.verify_calls == calls_before_lockout
    # Exactly one warning log is emitted for the lockout window, with the client IP truncated to /24.
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "afterword_engine.profiles" and record.levelname == "WARNING"
    ]
    assert len(warnings) == 1
    assert "203.0.113.0/24" in warnings[0]
    assert "203.0.113.1" not in warnings[0]

    # IPv6 lockouts truncate the logged address to /48 instead of logging the full host address.
    caplog.clear()
    for _ in range(10):
        assert client.post(
            "/auth/login",
            json={"username": "owner", "password": WRONG_PASSWORD},
            headers={**service_headers, "x-bookward-client-ip": "2001:db8:abcd:0012::42"},
        ).status_code == 401
    ipv6_warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "afterword_engine.profiles" and record.levelname == "WARNING"
    ]
    assert len(ipv6_warnings) == 1
    assert "2001:db8:abcd::/48" in ipv6_warnings[0]
    assert "2001:db8:abcd:12::42" not in ipv6_warnings[0]

    # Legitimate sign-in from 198.51.100.42 still succeeds while 203.0.113.1 is locked out.
    trusted_ok = client.post(
        "/auth/login",
        json={"username": "owner", "password": OWNER_PASSWORD},
        headers={**service_headers, "x-bookward-client-ip": "198.51.100.42"},
    )
    assert trusted_ok.status_code == 200

    # CLI unlock clears the lockout without revoking active sessions and logs at WARNING without level forcing.
    caplog.clear()
    auth_admin.main(["unlock", "owner", "--ip", "203.0.113.1"])
    ip_unlock_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "afterword_engine.profiles" and record.levelname == "WARNING"
    ]
    assert len(ip_unlock_logs) == 1
    assert f"admin_id=cli target_account_id={identity_store['id']} cleared_attempts=10" in ip_unlock_logs[0]
    assert profiles.resolve_session(trusted_ok.json()["session_token"]) is not None
    recovered = client.post(
        "/auth/login",
        json={"username": "owner", "password": OWNER_PASSWORD},
        headers={**service_headers, "x-bookward-client-ip": "203.0.113.1"},
    )
    assert recovered.status_code == 200

    # Newlines in attacker-supplied usernames are stripped from rate-limit warning logs.
    caplog.clear()
    for _ in range(10):
        assert client.post(
            "/auth/login",
            json={"username": "spoof\r\nadmin", "password": WRONG_PASSWORD},
            headers={**service_headers, "x-bookward-client-ip": "203.0.113.55"},
        ).status_code == 401
    injected_warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "afterword_engine.profiles" and record.levelname == "WARNING"
    ]
    assert len(injected_warnings) == 1
    assert "spoofadmin" in injected_warnings[0]
    assert "\n" not in injected_warnings[0]
    assert "\r" not in injected_warnings[0]
    assert "\\n" not in injected_warnings[0]
    assert "\\r" not in injected_warnings[0]


def test_untrusted_callers_cannot_spoof_forwarded_client_ip_and_share_peer_bucket_without_secret(
    identity_store, monkeypatch, caplog
):
    from afterword_engine.main import _warn_if_shared_rate_limit_bucket

    monkeypatch.setattr(settings, "service_secret", "")

    caplog.clear()
    _warn_if_shared_rate_limit_bucket()
    startup_warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "afterword_engine.main" and record.levelname == "WARNING"
    ]
    assert len(startup_warnings) == 1
    assert "AFTERWORD_SERVICE_SECRET is not set" in startup_warnings[0]

    # Without a service secret, non-loopback callers share one peer-IP bucket even if x-bookward-client-ip rotates.
    untrusted_client = TestClient(app, client=("203.0.113.77", 50000))
    for index in range(10):
        resp = untrusted_client.post(
            "/auth/login",
            json={"username": "owner", "password": WRONG_PASSWORD},
            headers={"x-bookward-client-ip": f"198.51.100.{index + 1}"},
        )
        assert resp.status_code == 401
    blocked = untrusted_client.post(
        "/auth/login",
        json={"username": "owner", "password": OWNER_PASSWORD},
        headers={"x-bookward-client-ip": "198.51.100.250"},
    )
    assert blocked.status_code == 429

    # Fail closed on loopback too: without service_secret, loopback processes cannot spoof x-bookward-client-ip
    # and all attempts from 127.0.0.1 share the single 127.0.0.1 bucket.
    loopback_client = TestClient(app, client=("127.0.0.1", 50000))
    for index in range(10):
        resp = loopback_client.post(
            "/auth/login",
            json={"username": "owner", "password": WRONG_PASSWORD},
            headers={"x-bookward-client-ip": f"192.0.2.{index + 1}"},
        )
        assert resp.status_code == 401
    loopback_blocked = loopback_client.post(
        "/auth/login",
        json={"username": "owner", "password": OWNER_PASSWORD},
        headers={"x-bookward-client-ip": "198.51.100.88"},
    )
    assert loopback_blocked.status_code == 429


def test_forced_password_change_uses_isolated_session_bucket_and_admin_unlock_recovers(
    identity_store, monkeypatch, capsys, caplog
):
    import uvicorn.config
    from afterword_engine import auth_admin

    proxy_secret = f"svc-{uuid.uuid4().hex}"
    temp_password = f"temp-{uuid.uuid4().hex}"  # ggignore
    personal_password = f"personal-{uuid.uuid4().hex}"  # ggignore
    reset_password_val = f"reset-{uuid.uuid4().hex}"  # ggignore
    next_password = f"next-{uuid.uuid4().hex}"  # ggignore

    monkeypatch.setattr(settings, "service_secret", proxy_secret)
    headers = {"x-bookward-service": proxy_secret, "x-bookward-client-ip": "203.0.113.20"}
    admin_session, _ = profiles.create_session(identity_store["id"])

    # Confirm plain uvicorn leaves unconfigured application loggers inheriting the root WARNING threshold.
    assert "afterword_engine" not in uvicorn.config.LOGGING_CONFIG["loggers"]
    assert logging.getLogger("afterword_engine.profiles").getEffectiveLevel() == logging.WARNING

    client = TestClient(app)
    created = client.post(
        "/admin/accounts",
        json={
            "username": "first-login-reader",
            "password": temp_password,
            "display_name": "First Login Reader",
            "role": "user",
        },
        headers={**headers, "x-bookward-session": admin_session},
    )
    assert created.status_code == 200
    reader_id = created.json()["id"]

    # Reader signs in before external noise saturates their IP bucket.
    reader_login = client.post(
        "/auth/login",
        json={"username": "first-login-reader", "password": temp_password},
        headers=headers,
    )
    assert reader_login.status_code == 200
    assert reader_login.json()["account"]["must_change_password"] is True
    reader_session = reader_login.json()["session_token"]

    # External noise locks out public /auth/login for first-login-reader on the same IP.
    for _ in range(10):
        assert client.post(
            "/auth/login",
            json={"username": "first-login-reader", "password": WRONG_PASSWORD},
            headers=headers,
        ).status_code == 401
    assert client.post(
        "/auth/login",
        json={"username": "first-login-reader", "password": temp_password},
        headers=headers,
    ).status_code == 429

    # Active session still updates the mandatory temporary password via its session bucket.
    changed = client.post(
        "/auth/password/change",
        json={
            "current_password": temp_password,
            "new_password": personal_password,
        },
        headers={**headers, "x-bookward-session": reader_session},
    )
    assert changed.status_code == 200
    new_reader_session = changed.json()["session_token"]

    # Non-admin cannot call admin unlock, and unknown target returns 404 without an audit log.
    assert client.post(
        f"/admin/accounts/{reader_id}/unlock",
        headers={**headers, "x-bookward-session": new_reader_session},
    ).status_code == 403
    caplog.clear()
    missing_unlock = client.post(
        "/admin/accounts/nonexistent-account-id/unlock",
        headers={**headers, "x-bookward-session": admin_session},
    )
    assert missing_unlock.status_code == 404
    assert [
        record for record in caplog.records
        if record.name == "afterword_engine.profiles"
    ] == []

    # Admin sees lockout metadata in /admin/accounts, unlocks the account, and emits a WARNING audit log
    # under the default logger threshold (without caplog.at_level forcing).
    accounts = client.get(
        "/admin/accounts",
        headers={**headers, "x-bookward-session": admin_session},
    ).json()["accounts"]
    reader_entry = next(item for item in accounts if item["id"] == reader_id)
    assert reader_entry["locked_bucket_count"] == 1
    assert reader_entry["lockout_retry_after_seconds"] > 0

    caplog.clear()
    unlocked = client.post(
        f"/admin/accounts/{reader_id}/unlock",
        headers={**headers, "x-bookward-session": admin_session},
    )
    assert unlocked.status_code == 200
    assert unlocked.json()["cleared_attempts"] == 10
    unlock_audit_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "afterword_engine.profiles" and record.levelname == "WARNING"
    ]
    assert len(unlock_audit_logs) == 1
    assert f"admin_id={identity_store['id']}" in unlock_audit_logs[0]
    assert f"target_account_id={reader_id}" in unlock_audit_logs[0]
    assert "cleared_attempts=10" in unlock_audit_logs[0]

    relogin = client.post(
        "/auth/login",
        json={"username": "first-login-reader", "password": personal_password},
        headers=headers,
    )
    assert relogin.status_code == 200

    # Lock again, then verify password reset also clears failed login attempts.
    for _ in range(10):
        assert client.post(
            "/auth/login",
            json={"username": "first-login-reader", "password": WRONG_PASSWORD},
            headers=headers,
        ).status_code == 401
    assert client.post(
        "/auth/login",
        json={"username": "first-login-reader", "password": personal_password},
        headers=headers,
    ).status_code == 429

    reset_resp = client.post(
        f"/admin/accounts/{reader_id}/password",
        json={"password": reset_password_val},
        headers={**headers, "x-bookward-session": admin_session},
    )
    assert reset_resp.status_code == 200
    after_reset = client.post(
        "/auth/login",
        json={"username": "first-login-reader", "password": reset_password_val},
        headers=headers,
    )
    assert after_reset.status_code == 200

    # Independent session password-change bucket rate-limits after 10 bad current passwords.
    fresh_session = after_reset.json()["session_token"]
    fresh_session_hash = hashlib.sha256(fresh_session.encode()).hexdigest()
    caplog.clear()
    for _ in range(10):
        bad_change = client.post(
            "/auth/password/change",
            json={"current_password": WRONG_PASSWORD, "new_password": next_password},
            headers={**headers, "x-bookward-session": fresh_session},
        )
        assert bad_change.status_code == 401
    session_locked = client.post(
        "/auth/password/change",
        json={"current_password": reset_password_val, "new_password": next_password},
        headers={**headers, "x-bookward-session": fresh_session},
    )
    assert session_locked.status_code == 429
    assert int(session_locked.headers["retry-after"]) > 0
    session_warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "afterword_engine.profiles" and record.levelname == "WARNING"
    ]
    assert len(session_warnings) == 1
    assert "authenticated-session" in session_warnings[0]
    assert fresh_session_hash not in session_warnings[0]

    # Public login remains unaffected by session-bucket failures, and CLI --all clears remaining buckets
    # while emitting a WARNING audit log without caplog.at_level forcing.
    assert client.post(
        "/auth/login",
        json={"username": "first-login-reader", "password": reset_password_val},
        headers=headers,
    ).status_code == 200
    caplog.clear()
    auth_admin.main(["unlock", "--all"])
    assert "Cleared" in capsys.readouterr().out
    cli_audit_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "afterword_engine.profiles" and record.levelname == "WARNING"
    ]
    assert len(cli_audit_logs) == 1
    assert "admin_id=cli target_account_id=all cleared_attempts=20" in cli_audit_logs[0]


def test_password_change_enforces_ip_bucket_across_multiple_sessions(
    identity_store, monkeypatch, caplog
):
    proxy_secret = f"svc-{uuid.uuid4().hex}"
    current_password = f"curr-{uuid.uuid4().hex}"  # ggignore
    updated_password = f"updated-{uuid.uuid4().hex}"  # ggignore

    monkeypatch.setattr(settings, "service_secret", proxy_secret)
    reader = profiles.create_account("multi-session-reader", current_password)
    session_a, _ = profiles.create_session(reader["id"])
    session_b, _ = profiles.create_session(reader["id"])
    session_c, _ = profiles.create_session(reader["id"])

    same_ip_headers = {"x-bookward-service": proxy_secret, "x-bookward-client-ip": "198.51.100.91"}
    other_ip_headers = {"x-bookward-service": proxy_secret, "x-bookward-client-ip": "198.51.100.92"}

    client = TestClient(app)
    caplog.clear()
    for active_session in (session_a, session_b):
        for _ in range(5):
            assert client.post(
                "/auth/password/change",
                json={"current_password": WRONG_PASSWORD, "new_password": updated_password},
                headers={**same_ip_headers, "x-bookward-session": active_session},
            ).status_code == 401

    ip_locked = client.post(
        "/auth/password/change",
        json={"current_password": current_password, "new_password": updated_password},
        headers={**same_ip_headers, "x-bookward-session": session_c},
    )
    assert ip_locked.status_code == 429
    pwchange_warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "afterword_engine.profiles" and record.levelname == "WARNING"
    ]
    assert len(pwchange_warnings) == 1
    assert "pwchange:198.51.100.0/24" in pwchange_warnings[0]
    assert "198.51.100.91" not in pwchange_warnings[0]

    # Another client IP using session_c is not blocked by 198.51.100.91's password-change IP bucket.
    other_ip_ok = client.post(
        "/auth/password/change",
        json={"current_password": current_password, "new_password": updated_password},
        headers={**other_ip_headers, "x-bookward-session": session_c},
    )
    assert other_ip_ok.status_code == 200


def test_concurrent_login_failures_enforce_exact_limit_without_db_lock_errors(
    identity_store, monkeypatch, caplog
):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from argon2 import PasswordHasher

    # Use lightweight Argon2 parameters so 16 threads race simultaneously onto Phase 3.
    fast_hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
    monkeypatch.setattr(profiles, "PASSWORD_HASHER", fast_hasher)
    monkeypatch.setattr(profiles, "DUMMY_PASSWORD_HASH", fast_hasher.hash(f"dummy-{uuid.uuid4().hex}"))
    concurrent_user = profiles.create_account("race-reader", f"race-{uuid.uuid4().hex}")

    worker_count = 16
    barrier = threading.Barrier(worker_count)

    def attempt_bad_login():
        barrier.wait(timeout=5)
        try:
            result = profiles.authenticate_local(
                concurrent_user["username"],
                WRONG_PASSWORD,
                "203.0.113.200",
            )
            return ("none" if result is None else "ok")
        except profiles.LoginRateLimitError:
            return "rate_limited"

    caplog.clear()
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        outcomes = list(pool.map(lambda _: attempt_bad_login(), range(worker_count)))

    assert outcomes.count("none") == profiles.LOGIN_FAILURE_LIMIT
    assert outcomes.count("rate_limited") == worker_count - profiles.LOGIN_FAILURE_LIMIT

    with profiles.auth_connect() as con:
        stored_failures = con.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE normalized_username=? AND remote_address='203.0.113.200' AND succeeded=0",
            (concurrent_user["username"],),
        ).fetchone()[0]
    assert stored_failures == profiles.LOGIN_FAILURE_LIMIT

    race_warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "afterword_engine.profiles" and record.levelname == "WARNING"
    ]
    assert len(race_warnings) == 1
    assert "203.0.113.0/24" in race_warnings[0]
    assert "203.0.113.200" not in race_warnings[0]


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


def test_multi_profile_worker_interleaves_second_profile_score_after_first_50_row_audit_batch(
    identity_store, monkeypatch
):
    from afterword_engine import main as engine_main
    from afterword_engine import quality as quality_module

    alice = profiles.create_account("audit-alice", "audit-alice-password-long")
    bob = profiles.create_account("score-bob", "score-bob-password-long")

    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
        with transaction() as con:
            source_id = con.execute(
                "SELECT id FROM sources WHERE url='builtin://upcoming'"
            ).fetchone()[0]
            for index in range(300):
                cid = con.execute(
                    "INSERT INTO candidates(title,author,source_url,source_id,normalized_key) "
                    "VALUES(?,?,?,?,?)",
                    (
                        f"Alice Sweep Book {index}",
                        "Alice Author",
                        "https://example.com/alice",
                        source_id,
                        f"alice sweep book {index}",
                    ),
                ).lastrowid
                con.execute(
                    "UPDATE candidate_quality SET quality_status='accepted',audit_version=?,audited_at='2026-01-01 00:00:00' "
                    "WHERE candidate_id=?",
                    (quality_module.QUALITY_VERSION, cid),
                )
        quality_module.configure_candidate_quality_sweep(mode="full")

    with profile_scope(bob["profile_id"]):
        initialize(seed_demo=False)
        bob_score_job_id = enqueue_job("score", dedupe=True)

    monkeypatch.setattr(
        profiles,
        "profile_ids",
        lambda: [alice["profile_id"], bob["profile_id"]],
    )

    audited_per_turn: list[int] = []
    execution_order: list[tuple[str, str]] = []
    stop = asyncio.Event()

    async def fake_audit_one(candidate, *_args, **_kwargs):
        return {
            "candidate_id": int(candidate["id"]),
            "quality_status": "accepted",
            "quality_score": 0.9,
            "flags": [],
            "provider": "openlibrary",
            "provider_id": f"/works/OL{candidate['id']}",
            "work_id": f"/works/OL{candidate['id']}",
            "title_match": 1.0,
            "author_match": 1.0,
        }

    async def fake_score_all(*_args, **_kwargs):
        return 1

    monkeypatch.setattr(quality_module, "_audit_one", fake_audit_one)
    monkeypatch.setattr(engine_main, "score_all", fake_score_all)

    async def tracked_handler(kind: str):
        pid = current_profile_id()
        execution_order.append((pid, kind))
        result = await engine_main.handle_job(kind)
        if pid == alice["profile_id"] and kind == "candidate_quality":
            audited_per_turn.append(int(result["audited"]))
        if len(execution_order) >= 3:
            stop.set()
        return result

    asyncio.run(worker_loop(tracked_handler, stop))

    # Alice runs one 50-row batch, then Bob immediately runs its score job before Alice's next batch.
    assert execution_order[0] == (alice["profile_id"], "candidate_quality")
    assert execution_order[1] == (bob["profile_id"], "score")
    assert audited_per_turn[0] == quality_module.QUALITY_AUDIT_BATCH_SIZE
    with profile_scope(bob["profile_id"]), connect() as con:
        bob_status = con.execute(
            "SELECT status FROM jobs WHERE id=?", (bob_score_job_id,)
        ).fetchone()["status"]
    assert bob_status == "complete"


def test_worker_recovers_interrupted_audit_job_and_skips_committed_subbatches(
    identity_store, monkeypatch
):
    from afterword_engine import main as engine_main
    from afterword_engine import quality as quality_module

    reader = profiles.create_account("recover-reader", "recover-reader-password-long")
    candidate_ids: list[int] = []
    with profile_scope(reader["profile_id"]):
        initialize(seed_demo=False)
        with transaction() as con:
            source_id = con.execute(
                "SELECT id FROM sources WHERE url='builtin://upcoming'"
            ).fetchone()[0]
            for index in range(65):
                cid = con.execute(
                    "INSERT INTO candidates(title,author,source_url,source_id,normalized_key) "
                    "VALUES(?,?,?,?,?)",
                    (
                        f"Recoverable Sweep {index}",
                        "Sweep Writer",
                        "https://example.com/recover",
                        source_id,
                        f"recoverable sweep {index}",
                    ),
                ).lastrowid
                candidate_ids.append(cid)
                con.execute(
                    "UPDATE candidate_quality SET quality_status='accepted',audit_version=?,audited_at='2026-01-01 00:00:00' "
                    "WHERE candidate_id=?",
                    (quality_module.QUALITY_VERSION, cid),
                )
        sweep = quality_module.configure_candidate_quality_sweep(mode="full")
        initial_job_id = sweep["job_id"]
        with transaction() as con:
            con.execute(
                "UPDATE jobs SET status='running',started_at='2000-01-01T00:00:00+00:00',"
                "heartbeat_at='2000-01-01T00:00:00+00:00',lease_token='crashed-worker',attempts=1 "
                "WHERE id=?",
                (initial_job_id,),
            )

    monkeypatch.setattr(profiles, "profile_ids", lambda: [reader["profile_id"]])
    monkeypatch.setattr(engine_main, "score_all", lambda *_a, **_k: asyncio.sleep(0, result=0))

    seen_candidates: list[int] = []
    crashed_once = False

    async def crashing_then_healthy_audit_one(candidate, *_args, **_kwargs):
        nonlocal crashed_once
        cid = int(candidate["id"])
        # Crash at candidate index 20 (row 21) on the first run after two 10-row sub-batches commit.
        if not crashed_once and cid == candidate_ids[20]:
            crashed_once = True
            raise RuntimeError("Simulated mid-pass crash at row 21")
        seen_candidates.append(cid)
        return {
            "candidate_id": cid,
            "quality_status": "accepted",
            "quality_score": 0.92,
            "flags": [],
            "provider": "openlibrary",
            "provider_id": f"/works/OL{cid}",
            "work_id": f"/works/OL{cid}",
            "title_match": 1.0,
            "author_match": 1.0,
        }

    monkeypatch.setattr(quality_module, "_audit_one", crashing_then_healthy_audit_one)

    # First attempt crashes at row 21 after committing 20 rows (two 10-row sub-batches).
    with profile_scope(reader["profile_id"]):
        with pytest.raises(RuntimeError, match="Simulated mid-pass crash at row 21"):
            asyncio.run(engine_main.handle_job("candidate_quality"))
        committed_after_crash = [
            item["candidate_id"]
            for item in database.rows(
                "SELECT candidate_id FROM candidate_quality WHERE julianday(audited_at)>=julianday(?)",
                (sweep["started_at"],),
            )
        ]
        assert committed_after_crash == candidate_ids[:20]

    # Worker reaps the expired lease and resumes from row 21 without repeating rows 1..20.
    seen_candidates.clear()
    stop = asyncio.Event()

    async def handler(kind: str):
        result = await engine_main.handle_job(kind)
        if kind == "candidate_quality" and result["remaining"] == 0:
            stop.set()
        return result

    asyncio.run(worker_loop(handler, stop))

    assert seen_candidates == candidate_ids[20:]
    with profile_scope(reader["profile_id"]):
        summary = quality_module.quality_summary()
    assert summary["audit"]["status"] == "complete"
    assert summary["audit"]["remaining"] == 0


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
    owner = profiles.create_first_admin("owner", OWNER_PASSWORD)
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
    owner = profiles.create_first_admin("owner", OWNER_PASSWORD)
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


def test_event_loop_timer_stays_responsive_during_exclusive_profile_lock(
    identity_store, monkeypatch
):
    monkeypatch.setattr("afterword_engine.main.SCHEDULER_INITIAL_DELAY_SECONDS", 0.01)
    monkeypatch.setattr("afterword_engine.main.SCHEDULER_POLL_SECONDS", 0.1)
    alice = profiles.create_account("timer-alice", "timer-alice-password-long")
    bob = profiles.create_account("timer-bob", "timer-bob-password-long")
    for account, kind in ((alice, "alice_job"), (bob, "bob_job")):
        with profile_scope(account["profile_id"]):
            initialize(seed_demo=False)
            enqueue_job(kind)

    alice_path = profile_database_path(alice["profile_id"])
    lock_acquired = Event()
    release_lock = Event()

    def hold_exclusive_lock():
        with database_lock(alice_path):
            lock_acquired.set()
            release_lock.wait(timeout=5)

    def contended_alice_read():
        with profile_scope(alice["profile_id"]), connect() as con:
            return con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    from afterword_engine.main import (
        digest_scheduler_loop,
        goodreads_rss_scheduler_loop,
        source_scheduler_loop,
    )

    handled: list[tuple[str | None, str]] = []
    bob_done = asyncio.Event()
    alice_done = asyncio.Event()
    stop = asyncio.Event()

    async def handler(kind: str):
        owner = current_profile_id()
        handled.append((owner, kind))
        if kind == "bob_job":
            bob_done.set()
        elif kind == "alice_job":
            alice_done.set()
            stop.set()
        return {"kind": kind}

    async def run_scenario():
        lock_thread = Thread(target=hold_exclusive_lock, daemon=True)
        lock_thread.start()
        assert await asyncio.to_thread(lock_acquired.wait, 2)

        tick_durations: list[float] = []
        measure_ticks = True

        async def timer_probe():
            loop = asyncio.get_running_loop()
            while measure_ticks:
                started_at = loop.time()
                await asyncio.sleep(0.05)
                tick_durations.append(loop.time() - started_at)

        background_tasks = [
            asyncio.create_task(worker_loop(handler, stop)),
            asyncio.create_task(source_scheduler_loop(stop)),
            asyncio.create_task(digest_scheduler_loop(stop)),
            asyncio.create_task(goodreads_rss_scheduler_loop(stop)),
            asyncio.create_task(backups.backup_scheduler_loop(stop)),
        ]
        probe_task = asyncio.create_task(timer_probe())
        alice_read_task = asyncio.create_task(asyncio.to_thread(contended_alice_read))
        try:
            await asyncio.wait_for(bob_done.wait(), timeout=2)
            # Hold the exclusive lock across at least one full 0.5s worker poll.
            await asyncio.sleep(0.65)
            assert not alice_done.is_set()
            assert not alice_read_task.done()
            assert len(tick_durations) >= 10
            assert max(tick_durations) < 0.25

            release_lock.set()
            # Locked profiles must skip without PROFILE_RETRY_SECONDS (60s) backoff.
            await asyncio.wait_for(alice_done.wait(), timeout=2)
            assert await asyncio.wait_for(alice_read_task, timeout=2) >= 1
        finally:
            release_lock.set()
            measure_ticks = False
            stop.set()
            await asyncio.gather(probe_task, *background_tasks, return_exceptions=True)
            await asyncio.to_thread(lock_thread.join, 2)

    asyncio.run(run_scenario())
    assert [
        item for item in handled if item[1] in {"bob_job", "alice_job"}
    ] == [
        (bob["profile_id"], "bob_job"),
        (alice["profile_id"], "alice_job"),
    ]


def test_try_shared_releases_condition_before_yielding(identity_store):
    alice = profiles.create_account("try-shared-alice", "try-shared-password-long")
    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
    alice_path = profile_database_path(alice["profile_id"])
    lock = database_lock(alice_path)

    with lock.try_shared() as acquired:
        assert acquired is True
        assert not lock._condition._is_owned()
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(lock.is_locked).result(timeout=2) is False

    lock_held = Event()
    release_lock = Event()

    def hold_exclusive():
        with lock:
            lock_held.set()
            release_lock.wait(timeout=5)

    holder = Thread(target=hold_exclusive, daemon=True)
    holder.start()
    try:
        assert lock_held.wait(timeout=2)
        with lock.try_shared() as acquired:
            assert acquired is False
            assert not lock._condition._is_owned()
            with ThreadPoolExecutor(max_workers=1) as executor:
                assert executor.submit(lock.is_locked).result(timeout=2) is True
    finally:
        release_lock.set()
        holder.join(timeout=2)


def test_worker_heartbeat_logs_deferred_lease_renewal_when_profile_locked(
    identity_store, monkeypatch, caplog
):
    import logging

    monkeypatch.setattr(jobs, "JOB_HEARTBEAT_SECONDS", 0.02)
    alice = profiles.create_account("hb-alice", "hb-alice-password-long")
    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
        job_id = enqueue_job("read_metadata")

    status, claimed_job, lease_token, claimed_epoch = jobs._claim_profile_job(
        alice["profile_id"],
        time.monotonic(),
        {alice["profile_id"]},
        {alice["profile_id"]: 0},
        {},
        {alice["profile_id"]: time.monotonic()},
        {},
        set(),
    )
    assert (
        status == "claimed"
        and claimed_job is not None
        and lease_token is not None
        and claimed_epoch == 0
    )
    assert claimed_job["id"] == job_id

    alice_path = profile_database_path(alice["profile_id"])
    lock_held = Event()
    release_lock = Event()
    hb_stop = Event()

    def hold_exclusive():
        with database_lock(alice_path):
            lock_held.set()
            release_lock.wait(timeout=5)

    holder = Thread(target=hold_exclusive, daemon=True)
    holder.start()
    heartbeat = Thread(
        target=jobs._heartbeat,
        args=(alice["profile_id"], job_id, lease_token, hb_stop),
        daemon=True,
    )
    try:
        assert lock_held.wait(timeout=2)
        with caplog.at_level(logging.INFO, logger="afterword_engine.jobs"):
            heartbeat.start()
            deadline = time.monotonic() + 2.0
            while (
                f"Deferred lease renewal for job {job_id}" not in caplog.text
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            hb_stop.set()
            heartbeat.join(timeout=2)
    finally:
        hb_stop.set()
        release_lock.set()
        holder.join(timeout=2)

    assert (
        f"Deferred lease renewal for job {job_id} while profile database lock is held"
        in caplog.text
    )


def test_deferred_heartbeat_cancels_live_job_at_lease_expiry_before_duplicate_reap(
    identity_store, monkeypatch, caplog
):
    import logging

    monkeypatch.setattr(jobs, "JOB_LEASE_SECONDS", 0.12)
    monkeypatch.setattr(jobs, "JOB_HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(jobs, "JOB_DEFERRED_HEARTBEAT_RETRY_SECONDS", 0.02)
    monkeypatch.setattr(jobs, "JOB_REAPER_SECONDS", 0.02)

    alice = profiles.create_account("hb-bound-alice", "hb-bound-password-long")
    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
        job_id = enqueue_job("lease_bound_check")

    alice_path = profile_database_path(alice["profile_id"])
    first_started = Event()
    first_cancelled = Event()
    lock_held = Event()
    release_lock = Event()
    stop = asyncio.Event()
    active_count = 0
    max_concurrent = 0
    attempts_seen = 0

    def hold_long_exclusive_lock():
        with database_lock(alice_path):
            lock_held.set()
            release_lock.wait(timeout=5)

    async def handler(kind: str):
        nonlocal active_count, max_concurrent, attempts_seen
        assert kind == "lease_bound_check"
        attempts_seen += 1
        active_count += 1
        max_concurrent = max(max_concurrent, active_count)
        try:
            if attempts_seen == 1:
                first_started.set()
                await asyncio.Event().wait()
            stop.set()
            return {"attempt": attempts_seen}
        except asyncio.CancelledError:
            first_cancelled.set()
            raise
        finally:
            active_count -= 1

    async def run_scenario():
        worker = asyncio.create_task(worker_loop(handler, stop))
        holder = Thread(target=hold_long_exclusive_lock, daemon=True)
        try:
            assert await asyncio.to_thread(first_started.wait, 2)
            holder.start()
            assert await asyncio.to_thread(lock_held.wait, 2)
            # Hold the lock past JOB_LEASE_SECONDS (0.12s); _heartbeat must
            # cancel the live handler before releasing the lock so the reaper
            # can never start a second copy concurrently.
            assert await asyncio.to_thread(first_cancelled.wait, 2)
            with profile_scope(alice["profile_id"]):
                assert database_lock(alice_path).is_locked()
            release_lock.set()
            await asyncio.to_thread(holder.join, 2)
            await asyncio.wait_for(worker, timeout=3)
        finally:
            release_lock.set()
            stop.set()
            await asyncio.gather(worker, return_exceptions=True)
            if holder.is_alive():
                await asyncio.to_thread(holder.join, 2)

    with caplog.at_level(logging.WARNING, logger="afterword_engine.jobs"):
        asyncio.run(run_scenario())

    assert (
        f"Lease expired for job {job_id} while profile database lock was held; cancelling active handler"
        in caplog.text
    )
    assert max_concurrent == 1
    assert attempts_seen == 2
    with profile_scope(alice["profile_id"]), connect() as con:
        final_job = con.execute(
            "SELECT status,attempts,result FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert final_job["status"] == "complete"
    assert final_job["attempts"] == 2
    assert final_job["result"] == '{"attempt": 2}'


def test_quiesce_requeues_active_worker_job_and_times_out_on_stuck_connection(
    identity_store,
):
    alice = profiles.create_account("quiesce-alice", "quiesce-alice-password-long")
    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
        job_id = enqueue_job("quiesce_job")

    alice_path = profile_database_path(alice["profile_id"])
    lock = database_lock(alice_path)
    job_started = Event()
    job_cancelled = Event()
    quiesced_and_locked = Event()
    stop = asyncio.Event()

    async def blocking_async_handler(kind: str):
        assert kind == "quiesce_job"
        job_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            job_cancelled.set()
            raise

    def run_quiesce_and_exclusive():
        with lock.quiesce(timeout=2.0) as deadline, lock.exclusive(deadline):
            quiesced_and_locked.set()

    async def run_quiesce_scenario():
        worker = asyncio.create_task(worker_loop(blocking_async_handler, stop))
        try:
            assert await asyncio.to_thread(job_started.wait, 2)
            # Pause worker from re-claiming once restore finishes so we can inspect the row.
            stop.set()
            await asyncio.to_thread(run_quiesce_and_exclusive)
            assert job_cancelled.is_set()
            assert quiesced_and_locked.is_set()
            await asyncio.wait_for(worker, timeout=2)
        finally:
            stop.set()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run_quiesce_scenario())
    with profile_scope(alice["profile_id"]), connect() as con:
        requeued = con.execute(
            "SELECT status,attempts,lease_token FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert requeued["status"] == "queued"
    assert requeued["attempts"] == 0
    assert requeued["lease_token"] is None

    # A stuck open connection must cause lock.exclusive(deadline) to fail with BackupError
    # after the quiescence timeout and leave the profile lock cleanly released.
    with profile_scope(alice["profile_id"]):
        stuck = connect()
        try:
            with pytest.raises(
                backups.BackupError, match="could not be safely closed for restore"
            ):
                with lock.quiesce(timeout=0.05) as deadline, lock.exclusive(deadline):
                    pass
            assert not lock.is_locked()
            assert not lock.is_restoring()
        finally:
            stuck.close()


def test_real_handle_job_offloads_sync_db_work_and_supports_cancellation(
    identity_store, monkeypatch
):
    from afterword_engine.main import handle_job

    alice = profiles.create_account("real-job-alice", "real-job-alice-password-long")
    bob = profiles.create_account("real-job-bob", "real-job-bob-password-long")
    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,rating,isbn,source) VALUES(?,?,?,?,?)",
                (
                    "The Left Hand of Darkness",
                    "Ursula K. Le Guin",
                    5,
                    "9780441478125",
                    "test",
                ),
            )
        alice_job_id = enqueue_job("read_metadata")

    with profile_scope(bob["profile_id"]):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,rating,isbn,source) VALUES(?,?,?,?,?)",
                ("Kindred", "Octavia E. Butler", 5, "9780807083697", "test"),
            )
        bob_job_id = enqueue_job("read_metadata")

    alice_path = profile_database_path(alice["profile_id"])
    alice_lookup_entered = Event()
    release_alice_lookup = asyncio.Event()
    cancel_lookup_entered = Event()
    lock_acquired = Event()
    release_lock = Event()
    pause_cancel_lookup = False

    async def fake_resolve_book_metadata(title, author, **_kwargs):
        if title == "The Left Hand of Darkness":
            alice_lookup_entered.set()
            await release_alice_lookup.wait()
        elif pause_cancel_lookup and title == "Parable of the Sower":
            cancel_lookup_entered.set()
            await asyncio.Event().wait()
        return {}

    monkeypatch.setattr(
        "afterword_engine.ingestion.resolve_book_metadata",
        fake_resolve_book_metadata,
    )

    def hold_exclusive_lock():
        with database_lock(alice_path):
            lock_acquired.set()
            release_lock.wait(timeout=5)

    def job_status(profile_id: str, job_id: str) -> str:
        with profile_scope(profile_id), connect() as con:
            found = con.execute(
                "SELECT status FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            return str(found["status"]) if found else ""

    async def run_real_worker():
        nonlocal pause_cancel_lookup
        stop = asyncio.Event()
        worker = asyncio.create_task(worker_loop(handle_job, stop))
        tick_durations: list[float] = []
        measure_ticks = True

        async def timer_probe():
            loop = asyncio.get_running_loop()
            while measure_ticks:
                started_at = loop.time()
                await asyncio.sleep(0.05)
                tick_durations.append(loop.time() - started_at)

        probe_task = asyncio.create_task(timer_probe())
        lock_thread = Thread(target=hold_exclusive_lock, daemon=True)
        try:
            assert await asyncio.to_thread(alice_lookup_entered.wait, 3)
            # Lock Alice's DB while handle_job("read_metadata") is mid-HTTP lookup,
            # then let the lookup return so _persist_read_metadata_attempt hits
            # database_lock(alice_path). It must wait in a worker thread rather
            # than blocking the event loop.
            lock_thread.start()
            assert await asyncio.to_thread(lock_acquired.wait, 2)
            release_alice_lookup.set()

            await asyncio.sleep(0.35)
            assert len(tick_durations) >= 5
            assert max(tick_durations) < 0.25

            release_lock.set()
            await asyncio.to_thread(lock_thread.join, 2)

            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                alice_state = await asyncio.to_thread(
                    job_status, alice["profile_id"], alice_job_id
                )
                bob_state = await asyncio.to_thread(
                    job_status, bob["profile_id"], bob_job_id
                )
                if alice_state == "complete" and bob_state == "complete":
                    break
                await asyncio.sleep(0.05)
            assert alice_state == "complete"
            assert bob_state == "complete"

            # Verify cancelling an in-flight worker task cancels handle_job cleanly
            # and immediately returns the interrupted job to queued without waiting
            # for the 300s lease reaper.
            pause_cancel_lookup = True
            with profile_scope(bob["profile_id"]):
                with transaction() as con:
                    con.execute(
                        "INSERT INTO reads(title,author,rating,isbn,source) VALUES(?,?,?,?,?)",
                        (
                            "Parable of the Sower",
                            "Octavia E. Butler",
                            5,
                            "9780446675505",
                            "test",
                        ),
                    )
                cancelled_job_id = enqueue_job("read_metadata")

            assert await asyncio.to_thread(cancel_lookup_entered.wait, 3)
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
            with profile_scope(bob["profile_id"]), connect() as con:
                cancelled_row = con.execute(
                    "SELECT status,attempts,lease_token FROM jobs WHERE id=?",
                    (cancelled_job_id,),
                ).fetchone()
            assert cancelled_row["status"] == "queued"
            assert cancelled_row["attempts"] == 0
            assert cancelled_row["lease_token"] is None
        finally:
            release_lock.set()
            release_alice_lookup.set()
            measure_ticks = False
            stop.set()
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await asyncio.gather(probe_task, return_exceptions=True)
            if lock_thread.is_alive():
                await asyncio.to_thread(lock_thread.join, 2)

    asyncio.run(run_real_worker())


def test_heartbeat_zero_row_update_cancels_handler_and_resets_ready_profiles_on_restore(
    identity_store, monkeypatch
):
    monkeypatch.setattr(jobs, "JOB_HEARTBEAT_SECONDS", 0.02)
    with profile_scope("legacy"):
        initialize(seed_demo=False)
        snapshot = backups.create_backup()
        first_job_id = enqueue_job("disappearing_lease")

    init_calls: list[str] = []
    original_initialize = database.initialize

    def spy_initialize(*, seed_demo=True):
        init_calls.append(current_profile_id() or "legacy")
        return original_initialize(seed_demo=seed_demo)

    monkeypatch.setattr(database, "initialize", spy_initialize)

    stop = asyncio.Event()
    first_started = asyncio.Event()
    first_cancelled = asyncio.Event()
    never_complete = asyncio.Event()
    second_done = asyncio.Event()

    async def handler(kind):
        if kind == "disappearing_lease":
            first_started.set()
            try:
                await never_complete.wait()
            except asyncio.CancelledError:
                first_cancelled.set()
                raise
        if kind == "after_restore":
            second_done.set()
            stop.set()
            return {"after_restore": True}
        return {}

    async def run():
        worker = asyncio.create_task(worker_loop(handler, stop))
        try:
            await asyncio.wait_for(first_started.wait(), timeout=2)
            assert init_calls == ["legacy"]
            with profile_scope("legacy"), transaction() as con:
                con.execute("DELETE FROM jobs WHERE id=?", (first_job_id,))
            await asyncio.wait_for(first_cancelled.wait(), timeout=2)

            with profile_scope("legacy"):
                await asyncio.to_thread(backups.restore_backup, snapshot["id"])
                second_job_id = enqueue_job("after_restore")

            await asyncio.wait_for(second_done.wait(), timeout=2)
            await asyncio.wait_for(worker, timeout=2)
            return second_job_id
        finally:
            stop.set()
            never_complete.set()

    second_job_id = asyncio.run(run())

    with profile_scope("legacy"), connect() as con:
        second_job = con.execute(
            "SELECT status,result FROM jobs WHERE id=?", (second_job_id,)
        ).fetchone()
    assert first_cancelled.is_set()
    assert init_calls.count("legacy") >= 2
    assert second_job["status"] == "complete"
    assert second_job["result"] == '{"after_restore": true}'


def test_heartbeat_defers_lease_renewal_while_profile_database_lock_is_held(
    identity_store, monkeypatch, caplog
):
    monkeypatch.setattr(jobs, "JOB_HEARTBEAT_SECONDS", 0.02)
    with profile_scope("legacy"):
        initialize(seed_demo=False)
        job_id = enqueue_job("deferred_heartbeat")
        with transaction() as con:
            con.execute(
                "UPDATE jobs SET status='running',started_at='2026-01-01T00:00:00+00:00',"
                "heartbeat_at='2026-01-01T00:00:00+00:00',lease_token='lease-1',attempts=1 "
                "WHERE id=?",
                (job_id,),
            )
        legacy_path = profile_database_path()

    lock_held = Event()
    release_lock = Event()

    def hold_exclusive_lock():
        with database_lock(legacy_path):
            lock_held.set()
            release_lock.wait(timeout=5)

    holder = ThreadPoolExecutor(max_workers=1)
    future = holder.submit(hold_exclusive_lock)
    assert lock_held.wait(timeout=2)

    stop = Event()
    lost_lease = Event()
    caplog.set_level("INFO", logger=jobs.LOGGER.name)
    hb_thread = Thread(
        target=jobs._heartbeat,
        args=("legacy", job_id, "lease-1", stop, lost_lease.set, 0),
        daemon=True,
    )
    try:
        hb_thread.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if any(
                "Deferred lease renewal for job" in record.getMessage()
                for record in caplog.records
            ):
                break
            time.sleep(0.01)
        assert any(
            "Deferred lease renewal for job" in record.getMessage()
            for record in caplog.records
        )
        assert not lost_lease.is_set()

        # Once the exclusive lock releases, heartbeat renews heartbeat_at cleanly.
        release_lock.set()
        future.result(timeout=2)
        deadline = time.monotonic() + 1.0
        renewed = None
        while time.monotonic() < deadline:
            with profile_scope("legacy"), connect() as con:
                renewed = con.execute(
                    "SELECT heartbeat_at FROM jobs WHERE id=?", (job_id,)
                ).fetchone()[0]
            if renewed and renewed != "2026-01-01T00:00:00+00:00":
                break
            time.sleep(0.01)
        assert renewed is not None and renewed != "2026-01-01T00:00:00+00:00"
        assert not lost_lease.is_set()
    finally:
        stop.set()
        release_lock.set()
        hb_thread.join(timeout=2)
        holder.shutdown(wait=True)


def test_live_profile_restore_fences_and_quiesces_without_blocking_other_tenants(
    identity_store,
):
    from datetime import datetime, timezone

    alice = profiles.create_account("tenant-alice", "tenant-alice-password-long")
    bob = profiles.create_account("tenant-bob", "tenant-bob-password-long")
    sync_time = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)

    for account, rss_url in (
        (alice, "https://www.goodreads.com/review/list_rss/alice"),
        (bob, "https://www.goodreads.com/review/list_rss/bob"),
    ):
        with profile_scope(account["profile_id"]):
            initialize(seed_demo=False)
            with transaction() as con:
                con.execute(
                    "INSERT OR REPLACE INTO settings(key,value,secret) VALUES('goodreads_rss_url',?,0)",
                    (rss_url,),
                )

    with profile_scope(alice["profile_id"]):
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES('Alice snapshot read','Alice','test')"
            )
            con.execute(
                "INSERT INTO jobs(id,kind,status) VALUES('alice-snapshot-queued','alice_snapshot','queued')"
            )
        alice_backup = backups.create_backup()
        with transaction() as con:
            con.execute("DELETE FROM jobs WHERE id='alice-snapshot-queued'")
        alice_live_job = enqueue_job("alice_active")

    with profile_scope(bob["profile_id"]):
        bob_live_job = enqueue_job("bob_active")

    alice_started = asyncio.Event()
    alice_cancelled = asyncio.Event()
    alice_snapshot_done = asyncio.Event()
    alice_hold = asyncio.Event()
    bob_started = asyncio.Event()
    bob_release = asyncio.Event()
    alice_stop = asyncio.Event()
    bob_stop = asyncio.Event()

    async def alice_handler(kind):
        if current_profile_id() != alice["profile_id"]:
            await asyncio.sleep(0.05)
            return {}
        if kind == "alice_snapshot":
            alice_snapshot_done.set()
            return {"restored_pending": True}
        assert kind == "alice_active"
        alice_started.set()
        try:
            await alice_hold.wait()
        except asyncio.CancelledError:
            alice_cancelled.set()
            raise
        return {"owner": "alice"}

    async def bob_handler(kind):
        assert current_profile_id() == bob["profile_id"]
        assert kind == "bob_active"
        bob_started.set()
        await bob_release.wait()
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES('Bob concurrent read','Bob','test')"
            )
        bob_stop.set()
        return {"owner": "bob"}

    async def run():
        # Run dedicated profile-filtered workers concurrently so Alice's active job
        # and Bob's active job are both in flight when Alice restores.
        orig_profile_ids = profiles.profile_ids
        profiles.profile_ids = lambda: [alice["profile_id"], bob["profile_id"]]
        try:
            alice_worker = asyncio.create_task(worker_loop(alice_handler, alice_stop))
            await asyncio.wait_for(alice_started.wait(), timeout=2)

            profiles.profile_ids = lambda: [bob["profile_id"]]
            bob_worker = asyncio.create_task(worker_loop(bob_handler, bob_stop))
            await asyncio.wait_for(bob_started.wait(), timeout=2)
            profiles.profile_ids = lambda: [alice["profile_id"], bob["profile_id"]]

            # While Alice's quiescence barrier is active, Alice's scheduler pauses
            # while Bob's scheduler still queues work normally.
            with profile_scope(alice["profile_id"]):
                alice_lock = database_lock()
            with alice_lock.quiesce():
                await asyncio.wait_for(alice_cancelled.wait(), timeout=2)
                with profile_scope(alice["profile_id"]):
                    assert queue_goodreads_rss_if_due(sync_time) is None
                with profile_scope(bob["profile_id"]):
                    bob_scheduled = queue_goodreads_rss_if_due(sync_time)
                    assert bob_scheduled is not None

            # Capture a pre-restore epoch fence for Alice before swapping Alice's DB.
            with profile_scope(alice["profile_id"]):
                pre_restore_epoch = current_restore_epoch()
                assert pre_restore_epoch == 0
                await asyncio.to_thread(backups.restore_backup, alice_backup["id"])
                with bind_restore_epoch(pre_restore_epoch):
                    with pytest.raises(RestoreEpochError):
                        with transaction() as con:
                            con.execute(
                                "INSERT INTO reads(title,author,source) VALUES('Late Alice write','Alice','test')"
                            )

            # Bob's active job was never canceled or fenced by Alice's restore,
            # and Alice's pending queued job from the snapshot executes cleanly.
            await asyncio.wait_for(alice_snapshot_done.wait(), timeout=2)
            bob_release.set()
            await asyncio.wait_for(bob_worker, timeout=2)
            alice_stop.set()
            await asyncio.wait_for(alice_worker, timeout=2)
        finally:
            profiles.profile_ids = orig_profile_ids
            alice_stop.set()
            bob_stop.set()
            alice_hold.set()
            bob_release.set()

    asyncio.run(run())

    with profile_scope(alice["profile_id"]), connect() as con:
        alice_reads = {
            item[0] for item in con.execute("SELECT title FROM reads").fetchall()
        }
        alice_jobs = con.execute(
            "SELECT id,status FROM jobs WHERE status IN ('queued','running')"
        ).fetchall()
        restored_job = con.execute(
            "SELECT status,result FROM jobs WHERE id='alice-snapshot-queued'"
        ).fetchone()
        alice_epoch = current_restore_epoch()
    assert alice_reads == {"Alice snapshot read"}
    assert alice_jobs == []
    assert restored_job["status"] == "complete"
    assert restored_job["result"] == '{"restored_pending": true}'
    assert alice_epoch == 1
    assert alice_live_job is not None

    with profile_scope(bob["profile_id"]), connect() as con:
        bob_reads = {
            item[0] for item in con.execute("SELECT title FROM reads").fetchall()
        }
        bob_job = con.execute(
            "SELECT status,result FROM jobs WHERE id=?", (bob_live_job,)
        ).fetchone()
        bob_epoch = current_restore_epoch()
    assert "Bob concurrent read" in bob_reads
    assert bob_job["status"] == "complete"
    assert bob_job["result"] == '{"owner": "bob"}'
    assert bob_epoch == 0


def test_worker_loop_does_not_block_event_loop_when_profile_lock_is_held(identity_store):
    alice = profiles.create_account("locked-alice", "locked-alice-password-with-length")
    bob = profiles.create_account("unlocked-bob", "unlocked-bob-password-with-length")
    for account in (alice, bob):
        with profile_scope(account["profile_id"]):
            initialize(seed_demo=False)

    with profile_scope(alice["profile_id"]):
        enqueue_job("alice_blocked")
        alice_path = profile_database_path()
    with profile_scope(bob["profile_id"]):
        bob_job_id = enqueue_job("bob_unblocked")

    writer_held = Event()
    release_writer = Event()

    def hold_alice_writer():
        with database_lock(alice_path):
            writer_held.set()
            release_writer.wait(timeout=5)

    holder = ThreadPoolExecutor(max_workers=1)
    future = holder.submit(hold_alice_writer)
    assert writer_held.wait(timeout=2)

    bob_done = asyncio.Event()
    stop = asyncio.Event()
    max_tick_gap = 0.0

    async def monitor_loop():
        nonlocal max_tick_gap
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now_mono = time.monotonic()
            max_tick_gap = max(max_tick_gap, now_mono - last)
            last = now_mono

    async def handler(kind):
        if kind == "bob_unblocked":
            bob_done.set()
            return {"bob": True}
        stop.set()
        return {"alice": True}

    async def run():
        orig_profile_ids = profiles.profile_ids
        profiles.profile_ids = lambda: [alice["profile_id"], bob["profile_id"]]
        monitor = asyncio.create_task(monitor_loop())
        worker = asyncio.create_task(worker_loop(handler, stop))
        try:
            await asyncio.wait_for(bob_done.wait(), timeout=2)
            # Event loop remained responsive while Alice's exclusive writer lock was held.
            assert max_tick_gap < 0.25
            release_writer.set()
            await asyncio.wait_for(worker, timeout=2)
        finally:
            profiles.profile_ids = orig_profile_ids
            release_writer.set()
            stop.set()
            await asyncio.gather(monitor, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release_writer.set()
        future.result(timeout=2)
        holder.shutdown(wait=True)

    with profile_scope(bob["profile_id"]), connect() as con:
        assert (
            con.execute("SELECT status FROM jobs WHERE id=?", (bob_job_id,)).fetchone()[0]
            == "complete"
        )


def test_lifespan_legacy_recovery_failure_is_guarded_and_old_schema_safety_snapshot_recovers(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "legacy-startup.db"
    monkeypatch.setattr(settings, "db", str(database_path))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)

    profiles.initialize_auth()
    admin = profiles.create_first_admin(
        "startup-admin", "startup-admin-password-with-length"
    )
    alice = profiles.create_account(
        "startup-alice", "startup-alice-password-with-length"
    )
    alice_session, _ = profiles.create_session(alice["id"])
    admin_session, _ = profiles.create_session(admin["id"])

    with profile_scope("legacy"):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES('Legacy safety read','Author','manual')"
            )
        legacy_safety = backups.create_backup()
        broken_journal = backups._restore_journal_path()
        broken_journal.write_text('{"safety_backup":"bookward-20260101T000000Z-missing0","selected_backup":"bookward-20260101T000000Z-missing0"}')

    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO reads(title,author,source) VALUES('Alice healthy read','Alice','manual')"
            )

    # 1. Broken legacy restore journal logs an error during lifespan startup without
    #    crashing /api/health or healthy secondary tenant profiles.
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        alice_overview = client.get(
            "/api/overview", headers={"x-bookward-session": alice_session}
        )
        assert alice_overview.status_code == 200
        assert {item["title"] for item in alice_overview.json()["history"]} == {
            "Alice healthy read"
        }

    # 2. Old-schema safety snapshot (missing newer tables and missing jobs / settings.updated_at)
    #    recovers cleanly during lifespan startup and migrates to the latest schema.
    with profile_scope("legacy"):
        safety_file = backups.backup_directory() / f"{legacy_safety['id']}.sqlite3"
        with sqlite3.connect(safety_file) as con:
            con.execute("DROP INDEX IF EXISTS idx_goodreads_shelf_items_title_author")
            con.execute("DROP INDEX IF EXISTS idx_goodreads_shelf_items_shelf")
            con.execute("DROP TABLE IF EXISTS goodreads_shelf_items")
            con.execute("DROP INDEX IF EXISTS idx_jobs_status_heartbeat")
            con.execute("DROP TABLE IF EXISTS jobs")
            existing_settings = con.execute(
                "SELECT key,value,secret FROM settings"
            ).fetchall()
            con.execute("DROP TABLE settings")
            con.execute(
                "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, secret INTEGER NOT NULL DEFAULT 0)"
            )
            con.executemany(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,?)",
                existing_settings,
            )
            con.execute("DELETE FROM schema_migrations WHERE version >= 21")
            con.commit()
        backups._write_restore_journal(safety_file, safety_file)

    with TestClient(app) as client:
        legacy_overview = client.get(
            "/api/overview", headers={"x-bookward-session": admin_session}
        )
        assert legacy_overview.status_code == 200
        assert "Legacy safety read" in {
            item["title"] for item in legacy_overview.json()["history"]
        }

    with profile_scope("legacy"), connect() as con:
        assert not backups._restore_journal_path().exists()
        assert (
            con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == MIGRATIONS[-1][0]
        )
        assert current_restore_epoch() >= 1


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


def test_populated_auth_registry_upgrades_transactions_sessions_and_accounts_on_startup(
    identity_store,
):
    reader = profiles.create_account("upgrade-reader", "upgrade-reader-password-long")
    sso_reader = profiles.create_oidc_account(
        "https://identity.example", "upgrade-sso-sub", "upgrade@example.test", "Upgrade SSO"
    )
    reader_session, _ = profiles.create_session(reader["id"])
    other_session, _ = profiles.create_session(reader["id"])
    sso_session, _ = profiles.create_session(sso_reader["id"])

    future_expiry = profiles.iso(profiles.utc_now() + timedelta(minutes=9))
    created_stamp = "2026-10-01T12:00:00+00:00"
    login_state_hash = hashlib.sha256(b"preupgrade-login-state").hexdigest()
    login_csrf_hash = hashlib.sha256(b"preupgrade-login-csrf").hexdigest()
    link_state_hash = hashlib.sha256(b"preupgrade-link-state").hexdigest()
    link_csrf_hash = hashlib.sha256(b"preupgrade-link-csrf").hexdigest()
    reader_session_hash = hashlib.sha256(reader_session.encode()).hexdigest()

    # Downgrade a populated auth registry to the pre-PR schema on `main`:
    # - `accounts` without `has_local_username`
    # - `auth_sessions` without `enrollment_verified_at`
    # - `auth_transactions` with `CHECK(purpose IN ('login','link'))` and populated in-flight rows
    with profiles.auth_connect() as con:
        con.execute("ALTER TABLE accounts DROP COLUMN has_local_username")
        con.execute("ALTER TABLE auth_sessions DROP COLUMN enrollment_verified_at")
        con.execute("DROP TABLE auth_transactions")
        con.execute(
            """
            CREATE TABLE auth_transactions (
                state_hash TEXT PRIMARY KEY,
                csrf_hash TEXT NOT NULL,
                nonce TEXT NOT NULL,
                code_verifier TEXT NOT NULL,
                purpose TEXT NOT NULL CHECK(purpose IN ('login','link')),
                account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
                session_hash TEXT,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        con.execute(
            """
            INSERT INTO auth_transactions(
                state_hash, csrf_hash, nonce, code_verifier, purpose, account_id, session_hash, expires_at, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?), (?,?,?,?,?,?,?,?,?)
            """,
            (
                login_state_hash,
                login_csrf_hash,
                "login-nonce",
                "login-verifier",
                "login",
                None,
                None,
                future_expiry,
                created_stamp,
            )
            + (
                link_state_hash,
                link_csrf_hash,
                "link-nonce",
                "link-verifier",
                "link",
                reader["id"],
                reader_session_hash,
                future_expiry,
                created_stamp,
            ),
        )

    # Run startup initialization twice to verify both populated migration and idempotence.
    profiles.initialize_auth()
    profiles.initialize_auth()

    with profiles.auth_connect() as con:
        legacy_table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_transactions_legacy'"
        ).fetchone()
        table_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='auth_transactions'"
        ).fetchone()[0]
        rows = con.execute(
            "SELECT state_hash, purpose, account_id, session_hash, created_at FROM auth_transactions"
        ).fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                """
                INSERT INTO auth_transactions(
                    state_hash, csrf_hash, nonce, code_verifier, purpose, expires_at
                ) VALUES ('bad-state', 'bad-csrf', 'n', 'v', 'bogus', ?)
                """,
                (future_expiry,),
            )

    assert legacy_table is None
    assert "'enroll'" in table_sql
    assert len(rows) == 2
    assert profiles.account_by_id(reader["id"])["has_local_username"] is True
    assert profiles.account_by_id(sso_reader["id"])["has_local_username"] is False

    # Pre-upgrade login and session-bound link transactions survive intact and enforce session binding.
    consumed_login = profiles.consume_oidc_transaction(
        "preupgrade-login-state", "preupgrade-login-csrf"
    )
    assert consumed_login is not None
    assert consumed_login["purpose"] == "login"
    assert consumed_login["nonce"] == "login-nonce"
    assert consumed_login["created_at"] == created_stamp

    assert (
        profiles.consume_oidc_transaction(
            "preupgrade-link-state", "preupgrade-link-csrf", other_session
        )
        is None
    )
    consumed_link = profiles.consume_oidc_transaction(
        "preupgrade-link-state", "preupgrade-link-csrf", reader_session
    )
    assert consumed_link is not None
    assert consumed_link["purpose"] == "link"
    assert consumed_link["account_id"] == reader["id"]
    assert consumed_link["session_hash"] == reader_session_hash

    # Newly supported `enroll` transactions and `ON DELETE CASCADE` work after the rebuild.
    profiles.create_oidc_transaction(
        state="postupgrade-enroll-state",
        csrf="postupgrade-enroll-csrf",
        nonce="enroll-nonce",
        verifier="enroll-verifier",
        purpose="enroll",
        account_id=sso_reader["id"],
        session_token=sso_session,
    )
    consumed_enroll = profiles.consume_oidc_transaction(
        "postupgrade-enroll-state", "postupgrade-enroll-csrf", sso_session
    )
    assert consumed_enroll is not None
    assert consumed_enroll["purpose"] == "enroll"
    assert consumed_enroll["account_id"] == sso_reader["id"]

    profiles.create_oidc_transaction(
        state="cascade-enroll-state",
        csrf="cascade-enroll-csrf",
        nonce="cascade-nonce",
        verifier="cascade-verifier",
        purpose="enroll",
        account_id=sso_reader["id"],
        session_token=sso_session,
    )
    with profiles.auth_connect() as con:
        con.execute("DELETE FROM accounts WHERE id=?", (sso_reader["id"],))
        remaining = con.execute(
            "SELECT COUNT(*) FROM auth_transactions WHERE account_id=?", (sso_reader["id"],)
        ).fetchone()[0]
    assert remaining == 0

    # Also cover upgrading an even older pre-`session_hash` table containing a populated login row
    # alongside a dangling `account_id` row from before foreign-key enforcement.
    orphan_login_state = hashlib.sha256(b"legacy-nosession-login").hexdigest()
    orphan_login_csrf = hashlib.sha256(b"legacy-nosession-csrf").hexdigest()
    with profiles.auth_connect() as con:
        con.execute("DROP TABLE auth_transactions")
        con.execute(
            """
            CREATE TABLE auth_transactions (
                state_hash TEXT PRIMARY KEY,
                csrf_hash TEXT NOT NULL,
                nonce TEXT NOT NULL,
                code_verifier TEXT NOT NULL,
                purpose TEXT NOT NULL,
                account_id TEXT,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        con.execute(
            """
            INSERT INTO auth_transactions(
                state_hash, csrf_hash, nonce, code_verifier, purpose, account_id, expires_at
            ) VALUES (?,?,?,?,?,?,?), (?,?,?,?,?,?,?)
            """,
            (
                orphan_login_state,
                orphan_login_csrf,
                "legacy-nonce",
                "legacy-verifier",
                "login",
                None,
                future_expiry,
            )
            + (
                hashlib.sha256(b"dangling-link").hexdigest(),
                hashlib.sha256(b"dangling-csrf").hexdigest(),
                "dangling-nonce",
                "dangling-verifier",
                "link",
                "deleted-account-id",
                future_expiry,
            ),
        )

    profiles.initialize_auth()

    recovered_login = profiles.consume_oidc_transaction(
        "legacy-nosession-login", "legacy-nosession-csrf"
    )
    assert recovered_login is not None
    assert recovered_login["purpose"] == "login"
    assert recovered_login["session_hash"] is None
    with profiles.auth_connect() as con:
        assert con.execute("SELECT COUNT(*) FROM auth_transactions").fetchone()[0] == 0


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


def test_auth_registry_key_caches_disk_secret_and_auth_connect_creates_0600_file(
    tmp_path, monkeypatch, caplog
):
    import logging
    import os
    import stat
    from cryptography.fernet import Fernet
    import afterword_engine.secrets as engine_secrets

    db_one = tmp_path / "one" / "afterword.db"
    db_two = tmp_path / "two" / "afterword.db"
    monkeypatch.setattr(settings, "db", str(db_one))
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    engine_secrets.clear_auth_registry_key_cache()

    # Creating a brand-new registry file enforces 0600 and propagates chmod failures.
    unwritable_chmod_db = tmp_path / "chmod-new-fail" / "afterword.db"
    with monkeypatch.context() as m:
        m.setattr(settings, "db", str(unwritable_chmod_db))
        original_chmod = Path.chmod

        def failing_new_chmod(self: Path, mode: int, *, follow_symlinks: bool = True):
            if self == profiles.auth_path():
                raise PermissionError("chmod blocked on new file")
            return original_chmod(self, mode, follow_symlinks=follow_symlinks)

        m.setattr(Path, "chmod", failing_new_chmod)
        with pytest.raises(PermissionError, match="chmod blocked on new file"):
            profiles.auth_connect()
        assert not profiles.auth_path().exists()

    previous_umask = os.umask(0)
    try:
        with profiles.auth_connect() as con:
            con.execute("SELECT 1").fetchone()
            inside_con = con
    finally:
        os.umask(previous_umask)

    registry = profiles.auth_path()
    assert stat.S_IMODE(registry.stat().st_mode) == 0o600
    with pytest.raises(sqlite3.ProgrammingError):
        inside_con.execute("SELECT 1")

    registry.chmod(0o644)
    with profiles.auth_connect() as con:
        con.execute("SELECT 1").fetchone()
    assert stat.S_IMODE(registry.stat().st_mode) == 0o600

    # Pre-existing registry files owned by another uid or on bind mounts log a warning
    # and continue rather than turning every auth request into a 500.
    profiles.initialize_auth()
    reader = profiles.create_account("chmod-reader", "chmod-reader-password-long")
    with profile_scope(reader["profile_id"]):
        initialize(seed_demo=False)
    reader_session, _ = profiles.create_session(reader["id"])
    reader_token = profiles.create_profile_api_token(reader["profile_id"], "Chmod token")["token"]

    with monkeypatch.context() as m:
        original_chmod = Path.chmod

        def failing_existing_chmod(self: Path, mode: int, *, follow_symlinks: bool = True):
            if self == registry:
                raise PermissionError("Operation not permitted")
            return original_chmod(self, mode, follow_symlinks=follow_symlinks)

        m.setattr(Path, "chmod", failing_existing_chmod)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="afterword_engine.profiles"):
            with profiles.auth_connect() as con:
                assert con.execute("SELECT 1").fetchone()[0] == 1
            with TestClient(app) as client:
                assert (
                    client.get(
                        "/api/v1/me", headers={"Authorization": f"Bearer {reader_token}"}
                    ).status_code
                    == 200
                )
                assert (
                    client.get(
                        "/auth/me", headers={"x-bookward-session": reader_session}
                    ).status_code
                    == 200
                )
        chmod_warnings = [
            record.message
            for record in caplog.records
            if "Could not restrict permissions on existing auth registry" in record.message
        ]
        assert len(chmod_warnings) == 1

    engine_secrets.clear_auth_registry_key_cache()
    disk_reads = 0
    original_generated_key = engine_secrets._generated_key

    def counting_generated_key(path: Path) -> bytes:
        nonlocal disk_reads
        disk_reads += 1
        return original_generated_key(path)

    monkeypatch.setattr(engine_secrets, "_generated_key", counting_generated_key)

    first = engine_secrets.auth_registry_key()
    second = engine_secrets.auth_registry_key()
    assert first == second
    assert disk_reads == 1

    env_key = Fernet.generate_key().decode()
    monkeypatch.setenv("AFTERWORD_SECRET_KEY", env_key)
    assert engine_secrets.auth_registry_key() == env_key.encode()
    assert disk_reads == 1

    monkeypatch.delenv("AFTERWORD_SECRET_KEY")
    assert engine_secrets.auth_registry_key() == first
    assert disk_reads == 1

    # Atomic file replacement (os.replace) refreshes the cached secret.
    key_path = db_one.with_name("secret.key")
    replacement_file = db_one.with_name("secret.key.next")
    replacement_key = Fernet.generate_key()
    replacement_file.write_bytes(replacement_key)
    replacement_file.replace(key_path)
    assert engine_secrets.auth_registry_key() == replacement_key
    assert disk_reads == 2
    assert engine_secrets.auth_registry_key() == replacement_key
    assert disk_reads == 2

    # Switching settings.db refreshes automatically.
    monkeypatch.setattr(settings, "db", str(db_two))
    other_key = engine_secrets.auth_registry_key()
    assert other_key != replacement_key
    assert disk_reads == 3


def test_api_token_digest_cache_is_bounded_hmac_keyed_and_excludes_invalid_tokens(
    identity_store, monkeypatch
):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.hmac import HMAC
    from afterword_engine.api_tokens import hash_api_token
    from afterword_engine.secrets import auth_registry_key

    profiles.invalidate_auth_caches(clear_all=True)
    reader = profiles.create_account("cache-reader", "cache-reader-password-long")
    created_a = profiles.create_profile_api_token(reader["profile_id"], "Token A")
    created_b = profiles.create_profile_api_token(reader["profile_id"], "Token B")
    created_c = profiles.create_profile_api_token(reader["profile_id"], "Token C")
    token_a = created_a["token"]

    # Invalid tokens never enter the verified digest cache.
    invalid_token = generate_api_token()
    assert profiles.authenticate_api_token(invalid_token) is None
    assert len(profiles._VERIFIED_TOKEN_DIGESTS) == 0

    derivations = 0

    def counting_hash(raw: str) -> str:
        nonlocal derivations
        derivations += 1
        return hash_api_token(raw)

    monkeypatch.setattr(profiles, "hash_api_token", counting_hash)

    assert profiles.authenticate_api_token(token_a)["id"] == created_a["id"]
    assert derivations == 1

    expected_mac = HMAC(auth_registry_key(), hashes.SHA256())
    expected_mac.update(token_a.encode("utf-8"))
    expected_hmac_key = expected_mac.finalize().hex()
    assert list(profiles._VERIFIED_TOKEN_DIGESTS.keys()) == [expected_hmac_key]
    cached_entry = profiles._VERIFIED_TOKEN_DIGESTS[expected_hmac_key]
    assert token_a not in profiles._VERIFIED_TOKEN_DIGESTS
    assert token_a not in repr(cached_entry)

    # Rotating the installation signing secret changes the derived HMAC cache key.
    with monkeypatch.context() as m:
        m.setenv("AFTERWORD_SECRET_KEY", Fernet.generate_key().decode())
        assert profiles._token_cache_key(token_a) != expected_hmac_key

    # Repeat authentications hit the digest cache without recomputing PBKDF2.
    for _ in range(10):
        verified = profiles.authenticate_api_token(token_a)
        assert verified["profile_id"] == reader["profile_id"]
    assert derivations == 1
    with profiles.auth_connect() as con:
        last_used = con.execute(
            "SELECT last_used_at FROM api_tokens WHERE id=?", (created_a["id"],)
        ).fetchone()["last_used_at"]
    assert last_used is not None

    # Bounded LRU eviction keeps at most TOKEN_CACHE_MAX_ENTRIES entries.
    monkeypatch.setattr(profiles, "TOKEN_CACHE_MAX_ENTRIES", 2)
    assert profiles.authenticate_api_token(created_b["token"]) is not None
    assert profiles.authenticate_api_token(token_a) is not None  # Refresh A as MRU
    assert profiles.authenticate_api_token(created_c["token"]) is not None  # Evicts B
    assert len(profiles._VERIFIED_TOKEN_DIGESTS) == 2
    assert profiles.has_cached_api_token(token_a) is True
    assert profiles.has_cached_api_token(created_c["token"]) is True
    assert profiles.has_cached_api_token(created_b["token"]) is False


def test_http_cached_token_revocation_and_active_account_requirement(
    identity_store, tmp_path, monkeypatch
):
    from afterword_engine.api_tokens import hash_api_token

    reader = profiles.create_account("revoke-reader", "revoke-reader-password-long")
    with profile_scope(reader["profile_id"]):
        initialize(seed_demo=False)
    reader_session, _ = profiles.create_session(reader["id"])

    first = profiles.create_profile_api_token(reader["profile_id"], "First")
    second = profiles.create_profile_api_token(reader["profile_id"], "Second")
    third = profiles.create_profile_api_token(reader["profile_id"], "Third")
    status_token = profiles.create_profile_api_token(reader["profile_id"], "Status")
    uncached_status_token = profiles.create_profile_api_token(
        reader["profile_id"], "Uncached status"
    )

    derivations = 0

    def counting_hash(raw: str) -> str:
        nonlocal derivations
        derivations += 1
        return hash_api_token(raw)

    monkeypatch.setattr(profiles, "hash_api_token", counting_hash)

    with TestClient(app) as client:
        # Warm first token over HTTP and verify repeat call hits cache, then revoke over HTTP.
        res = client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {first['token']}"}
        )
        assert res.status_code == 200
        assert res.json()["profile_id"] == reader["profile_id"]
        assert profiles.has_cached_api_token(first["token"]) is True
        derivations_after_warm = derivations

        res_cached = client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {first['token']}"}
        )
        assert res_cached.status_code == 200
        assert derivations == derivations_after_warm

        revoke_res = client.post(
            f"/api/tokens/{first['id']}/revoke",
            headers={"x-bookward-session": reader_session},
        )
        assert revoke_res.status_code == 200
        assert profiles.has_cached_api_token(first["token"]) is False

        rejected = client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {first['token']}"}
        )
        assert rejected.status_code == 401
        assert rejected.headers.get("cache-control") == "no-store"

        # Warm second token and revoke via DELETE /api/settings/api-tokens/{id}.
        assert (
            client.get(
                "/api/v1/me", headers={"Authorization": f"Bearer {second['token']}"}
            ).status_code
            == 200
        )
        assert profiles.has_cached_api_token(second["token"]) is True
        delete_res = client.delete(
            f"/api/settings/api-tokens/{second['id']}",
            headers={"x-bookward-session": reader_session},
        )
        assert delete_res.status_code == 200
        assert (
            client.get(
                "/api/v1/me", headers={"Authorization": f"Bearer {second['token']}"}
            ).status_code
            == 401
        )

        # Out-of-band SQLite revocation while still cached in memory is rejected on live SELECT.
        assert (
            client.get(
                "/api/v1/me", headers={"Authorization": f"Bearer {third['token']}"}
            ).status_code
            == 200
        )
        assert profiles.has_cached_api_token(third["token"]) is True
        with profiles.auth_connect() as con:
            con.execute(
                "UPDATE api_tokens SET revoked_at=CURRENT_TIMESTAMP WHERE id=?",
                (third["id"],),
            )
        assert profiles.has_cached_api_token(third["token"]) is True
        assert (
            client.get(
                "/api/v1/me", headers={"Authorization": f"Bearer {third['token']}"}
            ).status_code
            == 401
        )
        assert profiles.has_cached_api_token(third["token"]) is False

        # Password updates and resets immediately drop cached token entries while
        # personal API tokens survive password rotation (same as main).
        assert (
            client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {status_token['token']}"},
            ).status_code
            == 200
        )
        assert profiles.has_cached_api_token(status_token["token"]) is True
        profiles.update_password(reader["id"], "updated-reader-password-long")
        assert profiles.has_cached_api_token(status_token["token"]) is False
        assert (
            client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {status_token['token']}"},
            ).status_code
            == 200
        )
        assert profiles.has_cached_api_token(status_token["token"]) is True
        profiles.reset_password(reader["id"], "reset-reader-password-long-enough")
        assert profiles.has_cached_api_token(status_token["token"]) is False

        # Requiring active account status: even if profiles.status remains 'active' and
        # api_tokens.revoked_at is NULL, disabling accounts.status rejects both cached
        # and uncached tokens over HTTP and direct calls.
        assert (
            client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {status_token['token']}"},
            ).status_code
            == 200
        )
        assert profiles.has_cached_api_token(status_token["token"]) is True
        with profiles.auth_connect() as con:
            con.execute(
                "UPDATE accounts SET status='disabled' WHERE id=?", (reader["id"],)
            )
        assert (
            client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {status_token['token']}"},
            ).status_code
            == 401
        )
        assert profiles.has_cached_api_token(status_token["token"]) is False
        assert (
            client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {uncached_status_token['token']}"},
            ).status_code
            == 401
        )
        assert profiles.authenticate_api_token(uncached_status_token["token"]) is None

        with profiles.auth_connect() as con:
            con.execute(
                "UPDATE accounts SET status='active' WHERE id=?", (reader["id"],)
            )
        assert (
            client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {status_token['token']}"},
            ).status_code
            == 200
        )

        # Standard set_account_status('disabled') and restore_auth_backup also invalidate.
        profiles.set_account_status(reader["id"], "disabled")
        assert profiles.has_cached_api_token(status_token["token"]) is False
        assert (
            client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {status_token['token']}"},
            ).status_code
            == 401
        )

    other = profiles.create_account("restore-invalidate", "restore-invalidate-password")
    before_restore = profiles.create_profile_api_token(other["profile_id"], "Snapshot token")
    snapshot = backups.create_auth_backup()
    assert profiles.authenticate_api_token(before_restore["token"]) is not None
    assert profiles.has_cached_api_token(before_restore["token"]) is True
    backups.restore_auth_backup(snapshot["id"])
    assert profiles.has_cached_api_token(before_restore["token"]) is False
    assert profiles.authenticate_api_token(before_restore["token"]) is None

    # Pre-setup legacy profile (no account row yet) still authenticates active legacy tokens,
    # and create_first_admin invalidates pre-account cached entries.
    fresh_db = tmp_path / "pre-setup" / "afterword.db"
    monkeypatch.setattr(settings, "db", str(fresh_db))
    monkeypatch.delenv("AFTERWORD_AUTH_PASSWORD", raising=False)
    profiles.invalidate_auth_caches(clear_all=True)
    profiles.initialize_auth()
    legacy_token = profiles.create_profile_api_token("legacy", "Pre-setup legacy")
    assert profiles.authenticate_api_token(legacy_token["token"])["profile_id"] == "legacy"
    assert profiles.has_cached_api_token(legacy_token["token"]) is True
    admin = profiles.create_first_admin("bootstrap-admin", "bootstrap-admin-password")
    assert profiles.has_cached_api_token(legacy_token["token"]) is False
    assert profiles.authenticate_api_token(legacy_token["token"])["profile_id"] == "legacy"
    with profiles.auth_connect() as con:
        con.execute("UPDATE accounts SET status='disabled' WHERE id=?", (admin["id"],))
    assert profiles.authenticate_api_token(legacy_token["token"]) is None


def test_legacy_token_upgraded_between_lookups_still_authenticates(
    identity_store, monkeypatch
):
    from afterword_engine.api_tokens import hash_api_token

    reader = profiles.create_account("legacy-gap", "legacy-gap-password-long")
    created = profiles.create_profile_api_token(reader["profile_id"], "Legacy gap")
    raw_token = created["token"]
    token_id = created["id"]
    upgraded_hash = hash_api_token(raw_token)
    legacy_digest = legacy_hash_api_token(raw_token)
    with profiles.auth_connect() as con:
        con.execute(
            "UPDATE api_tokens SET token_hash=? WHERE id=?", (legacy_digest, token_id)
        )
    profiles.invalidate_auth_caches(clear_all=True)

    real_auth_connect = profiles.auth_connect
    upgraded = []

    def upgrade_before_legacy_lookup(statement):
        # Simulate another request finishing the legacy upgrade right after this
        # request's lookup of the upgraded hash and before its legacy lookup.
        if legacy_digest in statement and not upgraded:
            upgraded.append(True)
            other = sqlite3.connect(profiles.auth_path(), timeout=5)
            other.execute(
                "UPDATE api_tokens SET token_hash=? WHERE id=?", (upgraded_hash, token_id)
            )
            other.commit()
            other.close()

    def traced_auth_connect(*args, **kwargs):
        con = real_auth_connect(*args, **kwargs)
        con.set_trace_callback(upgrade_before_legacy_lookup)
        return con

    monkeypatch.setattr(profiles, "auth_connect", traced_auth_connect)
    result = profiles.authenticate_api_token(raw_token)

    assert upgraded
    assert result is not None and result["id"] == token_id


def test_concurrent_legacy_api_token_upgrade_guards_old_token_hash(
    identity_store, monkeypatch
):
    from afterword_engine.api_tokens import hash_api_token

    reader = profiles.create_account("legacy-upgrade", "legacy-upgrade-password-long")
    created = profiles.create_profile_api_token(reader["profile_id"], "Legacy upgrade")
    raw_token = created["token"]
    token_id = created["id"]
    expected_upgraded_hash = hash_api_token(raw_token)
    legacy_digest = legacy_hash_api_token(raw_token)

    with profiles.auth_connect() as con:
        con.execute(
            "UPDATE api_tokens SET token_hash=?,last_used_at=NULL WHERE id=?",
            (legacy_digest, token_id),
        )
    profiles.invalidate_auth_caches(clear_all=True)

    barrier = Barrier(4)

    def upgrade_worker():
        barrier.wait(timeout=5)
        return profiles.authenticate_api_token(raw_token)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(upgrade_worker) for _ in range(4)]
        results = [future.result(timeout=10) for future in futures]

    assert all(
        item is not None and item["id"] == token_id and item["token_hash"] == expected_upgraded_hash
        for item in results
    )
    with profiles.auth_connect() as con:
        stored = con.execute(
            "SELECT token_hash,last_used_at FROM api_tokens WHERE id=?", (token_id,)
        ).fetchone()
    assert stored["token_hash"] == expected_upgraded_hash
    assert stored["last_used_at"] is not None

    # Verify WHERE id=? AND token_hash=<legacy_hash> prevents overwriting a concurrently
    # rotated hash if token_hash changes between the read SELECT and BEGIN IMMEDIATE.
    second = profiles.create_profile_api_token(reader["profile_id"], "Legacy race")
    second_raw = second["token"]
    second_id = second["id"]
    rotated_raw = generate_api_token()
    rotated_hash = hash_api_token(rotated_raw)

    with profiles.auth_connect() as con:
        con.execute(
            "UPDATE api_tokens SET token_hash=?,last_used_at=NULL WHERE id=?",
            (legacy_hash_api_token(second_raw), second_id),
        )
    profiles.invalidate_auth_caches(clear_all=True)

    original_complete = profiles._complete_api_token_auth

    def racing_complete(con, **kwargs):
        with profiles.auth_connect() as other_con:
            other_con.execute(
                "UPDATE api_tokens SET token_hash=? WHERE id=?",
                (rotated_hash, second_id),
            )
            other_con.commit()
        return original_complete(con, **kwargs)

    monkeypatch.setattr(profiles, "_complete_api_token_auth", racing_complete)
    assert profiles.authenticate_api_token(second_raw) is None
    assert profiles.has_cached_api_token(second_raw) is False
    with profiles.auth_connect() as con:
        preserved = con.execute(
            "SELECT token_hash FROM api_tokens WHERE id=?", (second_id,)
        ).fetchone()["token_hash"]
    assert preserved == rotated_hash


def test_concurrent_api_token_derivations_are_capped_without_blocking_loop_or_sessions(
    identity_store, monkeypatch
):
    import threading
    from afterword_engine.api_tokens import hash_api_token

    readers = [
        profiles.create_account(f"api-reader-{idx}", f"api-reader-{idx}-password-long")
        for idx in range(9)
    ]
    for reader in readers:
        with profile_scope(reader["profile_id"]):
            initialize(seed_demo=False)
    session_token, _ = profiles.create_session(readers[0]["id"])
    valid_tokens = [
        profiles.create_profile_api_token(readers[idx]["profile_id"], f"Client {idx}")["token"]
        for idx in range(8)
    ]
    cached_bypass_token = profiles.create_profile_api_token(
        readers[8]["profile_id"], "Warm client"
    )["token"]
    assert profiles.authenticate_api_token(cached_bypass_token) is not None
    assert profiles.has_cached_api_token(cached_bypass_token) is True

    assert not hasattr(profiles, "_TOKEN_DERIVATION_SEMAPHORE")
    invalid_tokens = [generate_api_token() for _ in range(8)]
    concurrency_cap = profiles.TOKEN_DERIVATION_CONCURRENCY

    # Verify lifespan primes secret.key at startup and has_cached_api_token runs off the event loop.
    startup_db = Path(settings.db).parent / "startup-prime" / "afterword.db"
    with monkeypatch.context() as m:
        import afterword_engine.secrets as engine_secrets

        m.setattr(settings, "db", str(startup_db))
        m.delenv("AFTERWORD_SECRET_KEY", raising=False)
        engine_secrets.clear_auth_registry_key_cache()
        assert not startup_db.with_name("secret.key").exists()
        with TestClient(app):
            assert startup_db.with_name("secret.key").is_file()
            assert engine_secrets._AUTH_REGISTRY_KEY_CACHE is not None

    original_has_cached = profiles.has_cached_api_token
    has_cached_threads: list[int] = []

    def tracking_has_cached(token: str | None) -> bool:
        has_cached_threads.append(threading.get_ident())
        return original_has_cached(token)

    monkeypatch.setattr(profiles, "has_cached_api_token", tracking_has_cached)

    async def exercise_batch(batch: list[str], expected_status: int) -> int:
        active_derivations = 0
        peak_derivations = 0
        counter_lock = threading.Lock()
        slots_filled = Event()
        release_derivations = Event()

        def gated_hash_api_token(token: str) -> str:
            nonlocal active_derivations, peak_derivations
            with counter_lock:
                active_derivations += 1
                peak_derivations = max(peak_derivations, active_derivations)
                if active_derivations >= concurrency_cap:
                    slots_filled.set()
            try:
                assert release_derivations.wait(timeout=5)
                return hash_api_token(token)
            finally:
                with counter_lock:
                    active_derivations -= 1

        monkeypatch.setattr(profiles, "hash_api_token", gated_hash_api_token)
        for token in valid_tokens:
            profiles.invalidate_auth_caches(cache_key=profiles._token_cache_key(token))

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            pending = [
                asyncio.create_task(
                    client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
                )
                for token in batch
            ]
            try:
                assert await asyncio.to_thread(slots_filled.wait, 5)
                assert peak_derivations == concurrency_cap

                # While every uncached derivation slot is blocked in worker threads,
                # the asyncio event loop, /api/health, /auth/me, and cached /api/v1/me
                # requests all complete without waiting on the derivation limiter.
                await asyncio.sleep(0)
                health_res = await client.get("/api/health")
                assert health_res.status_code == 200

                me_res = await client.get(
                    "/auth/me", headers={"x-bookward-session": session_token}
                )
                assert me_res.status_code == 200

                cached_res = await client.get(
                    "/api/v1/me",
                    headers={"Authorization": f"Bearer {cached_bypass_token}"},
                )
                assert cached_res.status_code == 200
            finally:
                release_derivations.set()

            responses = await asyncio.gather(*pending)
            assert all(item.status_code == expected_status for item in responses)
        return peak_derivations

    async def check_off_loop_and_bounded_timeout() -> None:
        event_loop_thread_id = threading.get_ident()
        has_cached_threads.clear()

        slots_filled = Event()
        release_derivations = Event()
        filler_lock = threading.Lock()
        active_fillers = 0

        def blocking_hash_api_token(token: str) -> str:
            nonlocal active_fillers
            with filler_lock:
                active_fillers += 1
                if active_fillers >= concurrency_cap:
                    slots_filled.set()
            try:
                assert release_derivations.wait(timeout=5)
                return hash_api_token(token)
            finally:
                with filler_lock:
                    active_fillers -= 1

        monkeypatch.setattr(profiles, "hash_api_token", blocking_hash_api_token)
        monkeypatch.setattr(profiles, "TOKEN_DERIVATION_WAIT_SECONDS", 0.05)

        fillers = [generate_api_token() for _ in range(concurrency_cap)]
        overflow_cold = valid_tokens[0]
        profiles.invalidate_auth_caches(cache_key=profiles._token_cache_key(overflow_cold))

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            held_tasks = [
                asyncio.create_task(
                    client.get("/api/v1/me", headers={"Authorization": f"Bearer {bad}"})
                )
                for bad in fillers
            ]
            try:
                assert await asyncio.to_thread(slots_filled.wait, 5)
                # Give all concurrency_cap filler tasks a turn to acquire slots.
                for _ in range(5):
                    await asyncio.sleep(0)

                overflow_res = await client.get(
                    "/api/v1/me",
                    headers={"Authorization": f"Bearer {overflow_cold}"},
                )
                assert overflow_res.status_code == 503
                assert overflow_res.headers.get("cache-control") == "no-store"
                assert "profile_id" not in overflow_res.text

                cached_res = await client.get(
                    "/api/v1/me",
                    headers={"Authorization": f"Bearer {cached_bypass_token}"},
                )
                assert cached_res.status_code == 200
            finally:
                release_derivations.set()

            held_responses = await asyncio.gather(*held_tasks)
            assert all(res.status_code == 401 for res in held_responses)

            # Semaphore permits were cleanly preserved after timeout: cold valid token now succeeds.
            recovered_res = await client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {overflow_cold}"},
            )
            assert recovered_res.status_code == 200

        assert has_cached_threads
        assert all(tid != event_loop_thread_id for tid in has_cached_threads)

    assert asyncio.run(exercise_batch(valid_tokens, 200)) <= concurrency_cap
    assert asyncio.run(exercise_batch(invalid_tokens, 401)) <= concurrency_cap
    for bad in invalid_tokens:
        assert profiles.has_cached_api_token(bad) is False
    asyncio.run(check_off_loop_and_bounded_timeout())


def test_locked_auth_registry_fails_closed_for_cached_and_uncached_requests(
    identity_store, monkeypatch
):
    reader = profiles.create_account("locked-reader", "locked-reader-password-long")
    with profile_scope(reader["profile_id"]):
        initialize(seed_demo=False)
    session_token, _ = profiles.create_session(reader["id"])
    cached_token = profiles.create_profile_api_token(reader["profile_id"], "Cached")["token"]
    uncached_token = profiles.create_profile_api_token(reader["profile_id"], "Uncached")["token"]

    assert profiles.authenticate_api_token(cached_token) is not None
    assert profiles.has_cached_api_token(cached_token) is True
    assert profiles.has_cached_api_token(uncached_token) is False

    monkeypatch.setattr(profiles, "AUTH_BUSY_TIMEOUT_MS", 50)

    with TestClient(app, raise_server_exceptions=False) as client:
        locker = sqlite3.connect(profiles.auth_path(), timeout=5, isolation_level=None)
        try:
            locker.execute("BEGIN EXCLUSIVE")
            health_res = client.get("/api/health")
            assert health_res.status_code == 200

            cached_res = client.get(
                "/api/v1/me", headers={"Authorization": f"Bearer {cached_token}"}
            )
            assert cached_res.status_code == 500
            assert "profile_id" not in cached_res.text

            uncached_res = client.get(
                "/api/v1/me", headers={"Authorization": f"Bearer {uncached_token}"}
            )
            assert uncached_res.status_code == 500
            assert profiles.has_cached_api_token(uncached_token) is False

            session_res = client.get(
                "/auth/me", headers={"x-bookward-session": session_token}
            )
            assert session_res.status_code == 500
            assert "profile_id" not in session_res.text
        finally:
            locker.rollback()
            locker.close()

    with TestClient(app) as client:
        assert (
            client.get(
                "/api/v1/me", headers={"Authorization": f"Bearer {cached_token}"}
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/api/v1/me", headers={"Authorization": f"Bearer {uncached_token}"}
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/auth/me", headers={"x-bookward-session": session_token}
            ).status_code
            == 200
        )


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
    with profile_scope(sso_reader["profile_id"]):
        initialize(seed_demo=False)
    sso_api_token = profiles.create_profile_api_token(
        sso_reader["profile_id"], "SSO Personal Token"
    )["token"]
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
        assert mismatch_auth["max_age"] == ["600"]
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

        # Missing auth_time is also rejected during intent=enroll callback (mandatory freshness proof).
        missing_auth_time_start = client.get(
            "/auth/oidc/start?intent=enroll",
            headers={"x-bookward-session": enroll_session},
        )
        missing_auth_time_query = parse_qs(
            urlparse(missing_auth_time_start.json()["authorization_url"]).query
        )
        assert missing_auth_time_query["prompt"] == ["login"]
        assert missing_auth_time_query["max_age"] == ["600"]
        token_specs["missing-auth-time-code"] = {
            "sub": "enroll-subject-1",
            "nonce": missing_auth_time_query["nonce"][0],
        }
        missing_auth_time_callback = client.get(
            "/auth/oidc/callback",
            params={"code": "missing-auth-time-code", "state": missing_auth_time_query["state"][0]},
            headers={
                "x-bookward-session": enroll_session,
                "x-oidc-state": missing_auth_time_start.json()["csrf"],
            },
        )
        assert missing_auth_time_callback.status_code == 401
        assert (
            client.get("/auth/identities", headers={"x-bookward-session": enroll_session}).json()[
                "enrollment_verified"
            ]
            is False
        )

        def confirm_enrollment(code_name: str) -> None:
            start = client.get(
                "/auth/oidc/start?intent=enroll",
                headers={"x-bookward-session": enroll_session},
            )
            assert start.status_code == 200
            auth_query = parse_qs(urlparse(start.json()["authorization_url"]).query)
            assert auth_query["prompt"] == ["login"]
            assert auth_query["max_age"] == ["600"]
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

        # Warm the personal API token cache before enrollment and verify commit drops the cached digest.
        assert (
            client.get(
                "/api/v1/me", headers={"Authorization": f"Bearer {sso_api_token}"}
            ).status_code
            == 200
        )
        assert profiles.has_cached_api_token(sso_api_token) is True

        enrolled = client.post(
            "/auth/password/change",
            headers={"x-bookward-session": enroll_session},
            json={"username": "chosen-reader", "new_password": "chosen-reader-password-long"},
        )
        assert enrolled.status_code == 200
        assert enrolled.json()["username"] == "chosen-reader"
        assert enrolled.json()["has_local_username"] is True
        assert profiles.has_cached_api_token(sso_api_token) is False
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

        # Personal API tokens survive local credential enrollment and admin password resets
        # while browser sessions are revoked (matching #281).
        token_after_enroll = client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {sso_api_token}"}
        )
        assert token_after_enroll.status_code == 200
        assert token_after_enroll.json()["profile_id"] == sso_reader["profile_id"]
        assert profiles.has_cached_api_token(sso_api_token) is True

        admin_reset_after_enroll = client.post(
            f"/admin/accounts/{sso_reader['id']}/password",
            headers={"x-bookward-session": admin_session},
            json={"password": "admin-reset-enrolled-password-long"},
        )
        assert admin_reset_after_enroll.status_code == 200
        assert profiles.has_cached_api_token(sso_api_token) is False
        assert client.get("/auth/me", headers={"x-bookward-session": rotated_session}).status_code == 401
        token_after_reset = client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {sso_api_token}"}
        )
        assert token_after_reset.status_code == 200
        assert token_after_reset.json()["profile_id"] == sso_reader["profile_id"]
        assert profiles.has_cached_api_token(sso_api_token) is True


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
    integrity_token = profiles.create_profile_api_token(
        sso_reader["profile_id"], "Integrity Token"
    )["token"]
    assert profiles.authenticate_api_token(integrity_token) is not None
    assert profiles.has_cached_api_token(integrity_token) is True
    original_username = sso_reader["username"]
    enroll_session, _ = profiles.create_session(sso_reader["id"])
    sibling_session, _ = profiles.create_session(sso_reader["id"])
    assert profiles.mark_session_enrollment_verified(enroll_session) is True

    with pytest.raises(sqlite3.IntegrityError):
        profiles.update_password(
            sso_reader["id"], "candidate-password-long", username="Already-Taken"
        )
    assert profiles.has_cached_api_token(integrity_token) is True

    with TestClient(app) as client:
        conflict = client.post(
            "/auth/password/change",
            headers={"x-bookward-session": enroll_session},
            json={"username": "Already-Taken", "new_password": "candidate-password-long"},
        )
        assert conflict.status_code == 409
        assert profiles.has_cached_api_token(integrity_token) is True

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
        assert profiles.has_cached_api_token(integrity_token) is False
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


def test_lease_expiry_cancellation_preserves_attempt_count_when_lock_releases_immediately(
    identity_store, monkeypatch
):
    monkeypatch.setattr(jobs, "JOB_LEASE_SECONDS", 0.08)
    monkeypatch.setattr(jobs, "JOB_HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(jobs, "JOB_DEFERRED_HEARTBEAT_RETRY_SECONDS", 0.02)
    monkeypatch.setattr(jobs, "JOB_REAPER_SECONDS", 0.02)
    monkeypatch.setattr(jobs, "JOB_MAX_ATTEMPTS", 2)

    alice = profiles.create_account("hb-race-alice", "hb-race-password-long")
    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
        job_id = enqueue_job("lease_race_check")

    alice_path = profile_database_path(alice["profile_id"])
    first_started = Event()
    lock_held = Event()
    release_lock = Event()
    stop = asyncio.Event()
    attempts_seen = 0

    def hold_exclusive_lock():
        with database_lock(alice_path):
            lock_held.set()
            release_lock.wait(timeout=5)

    holder = Thread(target=hold_exclusive_lock, daemon=True)

    async def handler(kind: str):
        nonlocal attempts_seen
        assert kind == "lease_race_check"
        attempts_seen += 1
        try:
            if attempts_seen == 1:
                first_started.set()
                await asyncio.Event().wait()
            stop.set()
            return {"attempt": attempts_seen}
        except asyncio.CancelledError:
            # Release the exclusive lock *before* CancelledError unwinds to worker_loop
            # so database_lock().try_shared() is guaranteed unlocked during cleanup.
            release_lock.set()
            holder.join(timeout=2)
            raise

    async def run_scenario():
        worker = asyncio.create_task(worker_loop(handler, stop))
        try:
            assert await asyncio.to_thread(first_started.wait, 2)
            holder.start()
            assert await asyncio.to_thread(lock_held.wait, 2)
            await asyncio.wait_for(worker, timeout=3)
        finally:
            release_lock.set()
            stop.set()
            await asyncio.gather(worker, return_exceptions=True)
            if holder.is_alive():
                await asyncio.to_thread(holder.join, 2)

    asyncio.run(run_scenario())
    assert attempts_seen == 2
    with profile_scope(alice["profile_id"]), connect() as con:
        final_job = con.execute(
            "SELECT status,attempts,result FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert final_job["status"] == "complete"
    assert final_job["attempts"] == 2
    assert final_job["result"] == '{"attempt": 2}'

    # Also verify JOB_MAX_ATTEMPTS exhaustion marks the job failed instead of requeuing
    # when the lock releases immediately during lease-expiry cancellation.
    monkeypatch.setattr(jobs, "JOB_MAX_ATTEMPTS", 1)
    with profile_scope(alice["profile_id"]):
        exhausted_job_id = enqueue_job("lease_exhaust_check")

    exhausted_started = Event()
    exhausted_lock_held = Event()
    exhausted_release_lock = Event()
    exhausted_stop = asyncio.Event()
    exhausted_attempts = 0

    def hold_second_lock():
        with database_lock(alice_path):
            exhausted_lock_held.set()
            exhausted_release_lock.wait(timeout=5)

    second_holder = Thread(target=hold_second_lock, daemon=True)

    async def exhausting_handler(kind: str):
        nonlocal exhausted_attempts
        assert kind == "lease_exhaust_check"
        exhausted_attempts += 1
        try:
            exhausted_started.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            exhausted_release_lock.set()
            second_holder.join(timeout=2)
            raise

    async def run_exhaustion_scenario():
        worker = asyncio.create_task(worker_loop(exhausting_handler, exhausted_stop))
        try:
            assert await asyncio.to_thread(exhausted_started.wait, 2)
            second_holder.start()
            assert await asyncio.to_thread(exhausted_lock_held.wait, 2)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                def check_state():
                    with profile_scope(alice["profile_id"]), connect() as con:
                        return con.execute(
                            "SELECT status,attempts,error FROM jobs WHERE id=?",
                            (exhausted_job_id,),
                        ).fetchone()

                state = await asyncio.to_thread(check_state)
                if state and state["status"] == "failed":
                    exhausted_stop.set()
                    break
                await asyncio.sleep(0.02)
            await asyncio.wait_for(worker, timeout=2)
        finally:
            exhausted_release_lock.set()
            exhausted_stop.set()
            await asyncio.gather(worker, return_exceptions=True)
            if second_holder.is_alive():
                await asyncio.to_thread(second_holder.join, 2)

    asyncio.run(run_exhaustion_scenario())
    assert exhausted_attempts == 1
    with profile_scope(alice["profile_id"]), connect() as con:
        exhausted_row = con.execute(
            "SELECT status,attempts,error FROM jobs WHERE id=?", (exhausted_job_id,)
        ).fetchone()
    assert exhausted_row["status"] == "failed"
    assert exhausted_row["attempts"] == 1
    assert exhausted_row["error"] == "Job lease expired after maximum attempts"
