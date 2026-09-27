"""Installation-wide identity, session, and API-token registry.

Recommendation data lives in a separate SQLite file per profile. This module
stores only login identity and the opaque mapping that selects that file.
"""

import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .api_tokens import (
    TOKEN_MAX_LENGTH,
    TOKEN_PREFIX,
    generate_api_token,
    hash_api_token,
    legacy_hash_api_token,
    token_prefix,
)
from .config import settings

LOGGER = logging.getLogger(__name__)
SESSION_IDLE_HOURS = 24
SESSION_ABSOLUTE_DAYS = 7
PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
DUMMY_PASSWORD_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL UNIQUE REFERENCES profiles(id),
    username TEXT NOT NULL,
    normalized_username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin','user')),
    password_hash TEXT,
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK(must_change_password IN (0,1)),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
    auth_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS oidc_identities (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(issuer, subject)
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    auth_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    idle_expires_at TEXT NOT NULL,
    absolute_expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_account ON auth_sessions(account_id, revoked_at);
CREATE TABLE IF NOT EXISTS auth_transactions (
    state_hash TEXT PRIMARY KEY,
    csrf_hash TEXT NOT NULL,
    nonce TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN ('login','link')),
    account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
    session_hash TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY,
    normalized_username TEXT NOT NULL,
    remote_address TEXT NOT NULL,
    succeeded INTEGER NOT NULL CHECK(succeeded IN (0,1)),
    attempted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_lookup
    ON login_attempts(normalized_username, remote_address, attempted_at);
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_prefix TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_profile_api_tokens
    ON api_tokens(profile_id, revoked_at, created_at DESC);
"""


def auth_path() -> Path:
    return Path(f"{settings.db}.auth.sqlite3")


def auth_connect():
    path = auth_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return con


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def normalize_username(value: str) -> str:
    return value.strip().casefold()


def validate_credentials(username: str, password: str) -> tuple[str, str]:
    clean_username = username.strip()
    if not clean_username or len(clean_username) > 160 or any(ord(ch) < 32 for ch in clean_username):
        raise ValueError("Enter a valid username.")
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Use a password between 12 and 1024 characters.")
    return clean_username, PASSWORD_HASHER.hash(password)


def initialize_auth() -> str | None:
    """Create the identity store and establish the existing-data profile."""
    with auth_connect() as con:
        con.executescript(AUTH_SCHEMA)
        account_columns = {row[1] for row in con.execute("PRAGMA table_info(accounts)")}
        if "must_change_password" not in account_columns:
            con.execute("ALTER TABLE accounts ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
        transaction_columns = {row[1] for row in con.execute("PRAGMA table_info(auth_transactions)")}
        if "session_hash" not in transaction_columns:
            con.execute("ALTER TABLE auth_transactions ADD COLUMN session_hash TEXT")
        con.execute(
            "INSERT OR IGNORE INTO profiles(id,display_name) VALUES('legacy','My profile')"
        )
        count = con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        supplied_password = os.getenv("AFTERWORD_AUTH_PASSWORD", "")
        supplied_username = os.getenv("AFTERWORD_AUTH_USERNAME", "bookward")
        setup_token = None
        if count == 0 and supplied_password:
            # Existing Basic-auth deployments may already use a shorter
            # password. Preserve that credential during migration; new local
            # passwords still follow the stronger minimum.
            clean = supplied_username.strip()
            if not clean or len(clean) > 160 or len(supplied_password) > 1024:
                raise ValueError("The existing Basic-auth credentials are invalid.")
            encoded = PASSWORD_HASHER.hash(supplied_password)
            con.execute(
                "INSERT INTO accounts(id,profile_id,username,normalized_username,display_name,role,password_hash) "
                "VALUES(?,?,?,?,?,'admin',?)",
                (uuid.uuid4().hex, "legacy", clean, normalize_username(clean), clean, encoded),
            )
            con.execute("DELETE FROM auth_meta WHERE key='setup_token_hash'")
        elif count == 0 and not con.execute(
            "SELECT 1 FROM auth_meta WHERE key='setup_token_hash'"
        ).fetchone():
            setup_token = secrets.token_urlsafe(32)
            con.execute(
                "INSERT INTO auth_meta(key,value) VALUES('setup_token_hash',?)",
                (hashlib.sha256(setup_token.encode()).hexdigest(),),
            )
        return setup_token


def setup_required() -> bool:
    with auth_connect() as con:
        return con.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is None


def issue_setup_token() -> str:
    token = secrets.token_urlsafe(32)
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
            con.rollback()
            raise PermissionError("An administrator account already exists.")
        con.execute(
            "INSERT INTO auth_meta(key,value) VALUES('setup_token_hash',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (hashlib.sha256(token.encode()).hexdigest(),),
        )
    return token


def create_first_admin(setup_token: str, username: str, password: str):
    clean, password_hash = validate_credentials(username, password)
    supplied_hash = hashlib.sha256(setup_token.encode()).hexdigest()
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        stored = con.execute(
            "SELECT value FROM auth_meta WHERE key='setup_token_hash'"
        ).fetchone()
        if not stored or not hmac.compare_digest(supplied_hash, stored[0]):
            con.rollback()
            raise PermissionError("The setup token is invalid or has already been used.")
        if con.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
            con.rollback()
            raise PermissionError("Initial account setup has already completed.")
        account_id = uuid.uuid4().hex
        con.execute(
            "INSERT INTO accounts(id,profile_id,username,normalized_username,display_name,role,password_hash) "
            "VALUES(?,?,?,?,?,'admin',?)",
            (account_id, "legacy", clean, normalize_username(clean), clean, password_hash),
        )
        con.execute("DELETE FROM auth_meta WHERE key='setup_token_hash'")
    return account_by_id(account_id)


def create_account(username: str, password: str, display_name: str = "", role: str = "user"):
    if role not in {"user", "admin"}:
        raise ValueError("Choose a valid account role.")
    clean, password_hash = validate_credentials(username, password)
    account_id = uuid.uuid4().hex
    profile_id = uuid.uuid4().hex
    display = (display_name.strip() or clean)[:160]
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO profiles(id,display_name) VALUES(?,?)", (profile_id, display)
        )
        con.execute(
            "INSERT INTO accounts(id,profile_id,username,normalized_username,display_name,role,password_hash,must_change_password) "
            "VALUES(?,?,?,?,?,?,?,1)",
            (account_id, profile_id, clean, normalize_username(clean), display, role, password_hash),
        )
    return account_by_id(account_id)


def update_password(account_id: str, password: str) -> None:
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Use a password between 12 and 1024 characters.")
    password_hash = PASSWORD_HASHER.hash(password)
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        updated = con.execute(
            "UPDATE accounts SET password_hash=?,must_change_password=0,auth_version=auth_version+1,updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='active'",
            (password_hash, account_id),
        ).rowcount
        if not updated:
            con.rollback()
            raise LookupError("Account not found.")
        con.execute(
            "UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE account_id=? AND revoked_at IS NULL",
            (account_id,),
        )


def reset_password(account_id: str, password: str) -> None:
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Use a password between 12 and 1024 characters.")
    password_hash = PASSWORD_HASHER.hash(password)
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        updated = con.execute(
            "UPDATE accounts SET password_hash=?,must_change_password=1,auth_version=auth_version+1,updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='active'", (password_hash, account_id)
        ).rowcount
        if not updated:
            con.rollback()
            raise LookupError("Account not found.")
        con.execute("UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE account_id=? AND revoked_at IS NULL", (account_id,))


def _account(row) -> dict:
    return {
        "id": row["id"],
        "profile_id": row["profile_id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "status": row["status"],
        "auth_version": row["auth_version"],
        "must_change_password": bool(row["must_change_password"]),
    }


def account_by_id(account_id: str):
    with auth_connect() as con:
        found = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return _account(found) if found else None


def accounts_list():
    with auth_connect() as con:
        return [
            {key: value for key, value in _account(row).items() if key != "auth_version"}
            for row in con.execute("SELECT * FROM accounts ORDER BY created_at,username")
        ]


def authenticate_local(username: str, password: str, remote_address: str = ""):
    normalized = normalize_username(username)
    now = iso(utc_now())
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        recent = con.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE normalized_username=? AND remote_address=? "
            "AND succeeded=0 AND attempted_at>=?",
            (normalized, remote_address[:80], iso(utc_now() - timedelta(minutes=15))),
        ).fetchone()[0]
        if recent >= 10:
            con.rollback()
            return None
        found = con.execute(
            "SELECT * FROM accounts WHERE normalized_username=? AND status='active'",
            (normalized,),
        ).fetchone()
        valid = False
        con.execute("DELETE FROM login_attempts WHERE attempted_at<?", (iso(utc_now() - timedelta(days=1)),))
        if found and found["password_hash"]:
            try:
                valid = PASSWORD_HASHER.verify(found["password_hash"], password)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                valid = False
            if valid and PASSWORD_HASHER.check_needs_rehash(found["password_hash"]):
                con.execute(
                    "UPDATE accounts SET password_hash=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (PASSWORD_HASHER.hash(password), found["id"]),
                )
        else:
            try:
                PASSWORD_HASHER.verify(DUMMY_PASSWORD_HASH, password)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                pass
        con.execute(
            "INSERT INTO login_attempts(normalized_username,remote_address,succeeded,attempted_at) VALUES(?,?,?,?)",
            (normalized, remote_address[:80], 1 if valid else 0, now),
        )
        if valid:
            con.execute(
                "DELETE FROM login_attempts WHERE normalized_username=? AND remote_address=? AND succeeded=0",
                (normalized, remote_address[:80]),
            )
            con.commit()
            return _account(found)
    return None


def create_session(account_id: str) -> tuple[str, dict]:
    raw = secrets.token_urlsafe(48)
    now = utc_now()
    with auth_connect() as con:
        found = con.execute(
            "SELECT a.* FROM accounts a JOIN profiles p ON p.id=a.profile_id "
            "WHERE a.id=? AND a.status='active' AND p.status='active'", (account_id,)
        ).fetchone()
        if not found:
            raise LookupError("Account is unavailable.")
        con.execute(
            "INSERT INTO auth_sessions(token_hash,account_id,auth_version,created_at,last_seen_at,idle_expires_at,absolute_expires_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                hashlib.sha256(raw.encode()).hexdigest(), account_id,
                found["auth_version"], iso(now), iso(now),
                iso(now + timedelta(hours=SESSION_IDLE_HOURS)),
                iso(now + timedelta(days=SESSION_ABSOLUTE_DAYS)),
            ),
        )
        return raw, _account(found)


def resolve_session(raw: str | None):
    if not raw or len(raw) > 256:
        return None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    now = utc_now()
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        found = con.execute(
            "SELECT a.*,s.token_hash,s.created_at session_created_at,s.idle_expires_at,s.absolute_expires_at,s.auth_version session_auth_version "
            "FROM auth_sessions s JOIN accounts a ON a.id=s.account_id "
            "JOIN profiles p ON p.id=a.profile_id "
            "WHERE s.token_hash=? AND s.revoked_at IS NULL AND a.status='active' AND p.status='active'",
            (digest,),
        ).fetchone()
        if not found or found["auth_version"] != found["session_auth_version"]:
            con.rollback()
            return None
        if now.isoformat() >= found["idle_expires_at"] or now.isoformat() >= found["absolute_expires_at"]:
            con.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=?", (iso(now), digest))
            con.commit()
            return None
        con.execute(
            "UPDATE auth_sessions SET last_seen_at=?,idle_expires_at=? WHERE token_hash=?",
            (iso(now), min(now + timedelta(hours=SESSION_IDLE_HOURS), datetime.fromisoformat(found["absolute_expires_at"])).isoformat(timespec="seconds"), digest),
        )
        con.commit()
        account = _account(found)
        account["session_created_at"] = found["session_created_at"]
        return account


def revoke_session(raw: str | None):
    if raw:
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with auth_connect() as con:
            con.execute(
                "UPDATE auth_sessions SET revoked_at=COALESCE(revoked_at,CURRENT_TIMESTAMP) WHERE token_hash=?",
                (digest,),
            )


def set_account_status(account_id: str, status: str):
    if status not in {"active", "disabled"}:
        raise ValueError("Invalid account status.")
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        target = con.execute(
            "SELECT role,status FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not target:
            con.rollback()
            raise LookupError("Account not found.")
        if target["role"] == "admin" and target["status"] == "active" and status == "disabled":
            active_admins = con.execute(
                "SELECT COUNT(*) FROM accounts WHERE role='admin' AND status='active'"
            ).fetchone()[0]
            if active_admins <= 1:
                con.rollback()
                raise ValueError("The installation must retain an active administrator")
        changed = con.execute(
            "UPDATE accounts SET status=?,auth_version=auth_version+1,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, account_id),
        ).rowcount
        if not changed:
            con.rollback()
            raise LookupError("Account not found.")
        con.execute("UPDATE profiles SET status=? WHERE id=(SELECT profile_id FROM accounts WHERE id=?)", (status, account_id))
        con.execute("UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE account_id=? AND revoked_at IS NULL", (account_id,))
        if status == "disabled":
            con.execute(
                "UPDATE api_tokens SET revoked_at=COALESCE(revoked_at,CURRENT_TIMESTAMP) "
                "WHERE profile_id=(SELECT profile_id FROM accounts WHERE id=?)", (account_id,)
            )


def link_oidc_identity(account_id: str, issuer: str, subject: str, email: str = "") -> None:
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT account_id FROM oidc_identities WHERE issuer=? AND subject=?",
            (issuer, subject),
        ).fetchone()
        if existing and existing["account_id"] != account_id:
            con.rollback()
            raise PermissionError("This identity is already linked to another account.")
        con.execute(
            "INSERT OR IGNORE INTO oidc_identities(account_id,issuer,subject,email) VALUES(?,?,?,?)",
            (account_id, issuer, subject, email[:320]),
        )


def auth_methods(account_id: str):
    with auth_connect() as con:
        account = con.execute("SELECT password_hash FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not account:
            raise LookupError("Account not found.")
        identities = [dict(row) for row in con.execute(
            "SELECT id,issuer,email,created_at FROM oidc_identities WHERE account_id=? ORDER BY id",
            (account_id,),
        )]
        return {"password_enabled": bool(account["password_hash"]), "oidc_identities": identities}


def unlink_oidc_identity(account_id: str, identity_id: int):
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        account = con.execute("SELECT password_hash FROM accounts WHERE id=?", (account_id,)).fetchone()
        identity = con.execute(
            "SELECT id FROM oidc_identities WHERE id=? AND account_id=?", (identity_id, account_id)
        ).fetchone()
        others = con.execute(
            "SELECT COUNT(*) FROM oidc_identities WHERE account_id=? AND id!=?", (account_id, identity_id)
        ).fetchone()[0]
        if not account or not identity:
            con.rollback()
            raise LookupError("Login method not found.")
        if not account["password_hash"] and not others:
            con.rollback()
            raise ValueError("Add another login method before removing this one.")
        con.execute("DELETE FROM oidc_identities WHERE id=? AND account_id=?", (identity_id, account_id))


def account_for_oidc_identity(issuer: str, subject: str):
    with auth_connect() as con:
        found = con.execute(
            "SELECT a.* FROM oidc_identities i JOIN accounts a ON a.id=i.account_id "
            "JOIN profiles p ON p.id=a.profile_id WHERE i.issuer=? AND i.subject=? "
            "AND a.status='active' AND p.status='active'",
            (issuer, subject),
        ).fetchone()
        return _account(found) if found else None


def create_oidc_account(issuer: str, subject: str, email: str = "", display_name: str = ""):
    profile_id = uuid.uuid4().hex
    account_id = uuid.uuid4().hex
    display = (display_name.strip() or email.strip() or "SSO user")[:160]
    stable_login = "oidc:" + hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()[:40]
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT a.*,p.status profile_status FROM oidc_identities i "
            "JOIN accounts a ON a.id=i.account_id JOIN profiles p ON p.id=a.profile_id "
            "WHERE i.issuer=? AND i.subject=?", (issuer, subject)
        ).fetchone()
        if existing:
            con.rollback()
            if existing["status"] != "active" or existing["profile_status"] != "active":
                raise PermissionError("This account is disabled.")
            return _account(existing)
        con.execute("INSERT INTO profiles(id,display_name) VALUES(?,?)", (profile_id, display))
        con.execute(
            "INSERT INTO accounts(id,profile_id,username,normalized_username,display_name,role,password_hash) "
            "VALUES(?,?,?,?,?,'user',NULL)",
            (account_id, profile_id, stable_login, stable_login, display),
        )
        con.execute(
            "INSERT INTO oidc_identities(account_id,issuer,subject,email) VALUES(?,?,?,?)",
            (account_id, issuer, subject, email[:320]),
        )
        found = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return _account(found)


def create_oidc_transaction(*, state: str, csrf: str, nonce: str, verifier: str,
                            purpose: str, account_id: str | None,
                            session_token: str | None = None):
    if (purpose not in {"login", "link"} or (purpose == "link") != bool(account_id)
            or (purpose == "link") != bool(session_token)):
        raise ValueError("Invalid identity transaction.")
    session_hash = hashlib.sha256(session_token.encode()).hexdigest() if session_token else None
    with auth_connect() as con:
        con.execute("DELETE FROM auth_transactions WHERE expires_at<=?", (iso(utc_now()),))
        if con.execute("SELECT COUNT(*) FROM auth_transactions").fetchone()[0] >= 1000:
            raise RuntimeError("Too many pending SSO sign-ins")
        con.execute(
            "INSERT INTO auth_transactions(state_hash,csrf_hash,nonce,code_verifier,purpose,account_id,session_hash,expires_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                hashlib.sha256(state.encode()).hexdigest(),
                hashlib.sha256(csrf.encode()).hexdigest(), nonce, verifier,
                purpose, account_id, session_hash, iso(utc_now() + timedelta(minutes=10)),
            ),
        )


def consume_oidc_transaction(state: str, csrf: str, session_token: str | None = None):
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    csrf_hash = hashlib.sha256(csrf.encode()).hexdigest()
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        found = con.execute(
            "SELECT * FROM auth_transactions WHERE state_hash=? AND csrf_hash=? AND expires_at>?",
            (state_hash, csrf_hash, iso(utc_now())),
        ).fetchone()
        if not found:
            con.rollback()
            return None
        if found["purpose"] == "link":
            presented = hashlib.sha256(session_token.encode()).hexdigest() if session_token else ""
            if not found["session_hash"] or not hmac.compare_digest(found["session_hash"], presented):
                con.rollback()
                return None
        con.execute("DELETE FROM auth_transactions WHERE state_hash=?", (state_hash,))
        con.commit()
        return dict(found)


def create_profile_api_token(profile_id: str, name: str):
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 120:
        raise ValueError("Token name must contain 1 to 120 characters.")
    raw = generate_api_token()
    with auth_connect() as con:
        cursor = con.execute(
            "INSERT INTO api_tokens(profile_id,name,token_prefix,token_hash) VALUES(?,?,?,?)",
            (profile_id, clean_name, token_prefix(raw), hash_api_token(raw)),
        )
        found = con.execute(
            "SELECT id,name,token_prefix,created_at,last_used_at,revoked_at FROM api_tokens WHERE id=?",
            (cursor.lastrowid,),
        ).fetchone()
    return {**dict(found), "token": raw}


def list_profile_api_tokens(profile_id: str):
    with auth_connect() as con:
        return [dict(row) for row in con.execute(
            "SELECT id,name,token_prefix,created_at,last_used_at,revoked_at FROM api_tokens "
            "WHERE profile_id=? ORDER BY created_at DESC,id DESC", (profile_id,)
        )]


def revoke_profile_api_token(profile_id: str, token_id: int):
    with auth_connect() as con:
        changed = con.execute(
            "UPDATE api_tokens SET revoked_at=COALESCE(revoked_at,CURRENT_TIMESTAMP) "
            "WHERE id=? AND profile_id=?", (token_id, profile_id)
        ).rowcount
    if not changed:
        raise LookupError("API token not found.")


def authenticate_api_token(token: str):
    if (not token or len(token) > TOKEN_MAX_LENGTH or not token.startswith(TOKEN_PREFIX)
            or any(character.isspace() for character in token)):
        return None
    current_hash = hash_api_token(token)
    legacy_hash = legacy_hash_api_token(token)
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        found = con.execute(
            "SELECT t.id,t.name,t.profile_id,t.token_hash FROM api_tokens t "
            "JOIN profiles p ON p.id=t.profile_id WHERE t.token_hash=? AND t.revoked_at IS NULL AND p.status='active'",
            (current_hash,),
        ).fetchone()
        if not found:
            found = con.execute(
                "SELECT t.id,t.name,t.profile_id,t.token_hash FROM api_tokens t "
                "JOIN profiles p ON p.id=t.profile_id WHERE t.token_hash=? AND t.revoked_at IS NULL AND p.status='active'",
                (legacy_hash,),
            ).fetchone()
            if found:
                con.execute("UPDATE api_tokens SET token_hash=? WHERE id=?", (current_hash, found["id"]))
        if not found:
            con.rollback()
            return None
        con.execute("UPDATE api_tokens SET last_used_at=CURRENT_TIMESTAMP WHERE id=?", (found["id"],))
        con.commit()
        return dict(found)


def migrate_legacy_api_tokens(legacy_rows: list[dict]):
    with auth_connect() as con:
        if con.execute("SELECT 1 FROM auth_meta WHERE key='legacy_api_tokens_migrated'").fetchone():
            return
        for token in legacy_rows:
            con.execute(
                "INSERT OR IGNORE INTO api_tokens(id,profile_id,name,token_prefix,token_hash,created_at,last_used_at,revoked_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    token["id"], "legacy", token["name"], token["token_prefix"],
                    token["token_hash"], token["created_at"], token["last_used_at"], token["revoked_at"],
                ),
            )
        con.execute("INSERT INTO auth_meta(key,value) VALUES('legacy_api_tokens_migrated','1')")


def account_count() -> int:
    with auth_connect() as con:
        return int(con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])


def profile_ids(active_only: bool = True) -> list[str]:
    with auth_connect() as con:
        where = "WHERE status='active'" if active_only else ""
        return [row[0] for row in con.execute(f"SELECT id FROM profiles {where} ORDER BY created_at,id")]
