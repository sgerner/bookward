from pathlib import Path
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
import base64
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from afterword_engine import backups, profiles
from afterword_engine.api_tokens import generate_api_token, legacy_hash_api_token
from afterword_engine.config import settings
from afterword_engine.database import MIGRATIONS, connect, database_lock, initialize, transaction
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
            "/api/reading-list", headers={"x-bookward-session": owner_session}
        ).json()
        alice_shortlist = client.get(
            "/api/reading-list", headers={"x-bookward-session": alice_session}
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
        alice_list = client.get("/api/v1/reading-list", headers=alice_headers)
        bob_list = client.get("/api/v1/reading-list", headers=bob_headers)
        alice_job = client.get("/api/jobs/alice-private-job", headers={"x-bookward-session": profiles.create_session(alice["id"])[0]})
        foreign_job = client.get("/api/jobs/alice-private-job", headers={"x-bookward-session": profiles.create_session(bob["id"])[0]})
        bob_job = client.get("/api/jobs/bob-private-job", headers={"x-bookward-session": profiles.create_session(bob["id"])[0]})
        progress = client.put(
            "/api/v1/reading-list/1",
            json={"status": "finished", "rating": 5},
            headers=alice_headers,
        )
        bob_list_after_update = client.get("/api/v1/reading-list", headers=bob_headers)

    assert response.status_code == 200
    assert response.json()["profile_id"] == alice["profile_id"]
    assert conflict.status_code == 401
    assert [item["title"] for item in alice_list.json()] == ["Alice API shortlist"]
    assert [item["title"] for item in bob_list.json()] == ["Bob API shortlist"]
    assert alice_job.status_code == 200
    assert foreign_job.status_code == 404
    assert bob_job.status_code == 200
    assert progress.status_code == 200
    assert [item["title"] for item in bob_list_after_update.json()] == ["Bob API shortlist"]
    with profile_scope(alice["profile_id"]), connect() as con:
        assert con.execute("SELECT rating FROM reads WHERE title='Alice API shortlist'").fetchone()[0] == 5
    with profile_scope(bob["profile_id"]), connect() as con:
        assert con.execute("SELECT COUNT(*) FROM reading_progress").fetchone()[0] == 0


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
            replay = client.get(
                "/auth/oidc/callback",
                params={"code": code, "state": authorization["state"][0]},
                headers={"x-oidc-state": start.json()["csrf"]},
            )
            assert replay.status_code == 400

    assert profile_ids[0] != "legacy"
    assert profile_ids[0] == profile_ids[1]


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
