"""Installation-wide identity, session, and API-token registry.

Recommendation data lives in a separate SQLite file per profile. This module
stores only login identity and the opaque mapping that selects that file.
"""

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import threading
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
from .database import _database_setup_lock, database_lock
from .secrets import auth_registry_key

LOGGER = logging.getLogger(__name__)
SESSION_IDLE_HOURS = 24
SESSION_ABSOLUTE_DAYS = 7
ACTIVITY_UPDATE_INTERVAL_SECONDS = 60
TOKEN_CACHE_MAX_ENTRIES = 1024
ACTIVITY_CACHE_MAX_ENTRIES = 4096
TOKEN_DERIVATION_CONCURRENCY = max(1, min(2, (os.cpu_count() or 2) // 2))
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


def auth_lock(path: str | Path | None = None):
    """Return the process lock coordinating connections and restores for auth.sqlite3."""
    target = os.path.abspath(path if path is not None else auth_path())
    return database_lock(target)


@contextmanager
def auth_shared_lock(path: str | Path | None = None):
    """Hold a shared reader/backup connection slot without blocking other readers."""
    lock = auth_lock(path)
    owner = lock.acquire_connection()
    try:
        yield
    finally:
        lock.release_connection(owner)


def restrict_auth_files(path: Path | None = None) -> None:
    """Keep the identity registry and SQLite WAL sidecars mode 0600."""
    target = path if path is not None else auth_path()
    for candidate in (target, Path(f"{target}-wal"), Path(f"{target}-shm")):
        try:
            candidate.chmod(0o600)
        except OSError:
            continue


class _AuthConnection(sqlite3.Connection):
    """Close identity-registry connections deterministically and guard restores."""

    def __init__(self, *args, **kwargs):
        raw_target = args[0] if args else None
        target_path = Path(raw_target) if isinstance(raw_target, (str, Path)) else auth_path()
        self._database_lock = auth_lock(target_path)
        self._database_lock_owner = self._database_lock.acquire_connection()
        self._database_lock_held = True
        try:
            if not target_path.exists():
                try:
                    descriptor = os.open(target_path, os.O_CREAT | os.O_RDWR, 0o600)
                    os.close(descriptor)
                except OSError:
                    pass
            super().__init__(*args, **kwargs)
        except Exception:
            self._database_lock_held = False
            self._database_lock.release_connection(self._database_lock_owner)
            raise

    def close(self):
        held = getattr(self, "_database_lock_held", False)
        if held:
            self._database_lock_held = False
        try:
            super().close()
        finally:
            if held:
                self._database_lock.release_connection(self._database_lock_owner)

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


_WAL_READY_IDENTITY: tuple[str, int, int] | None = None


def _wal_files_ready(path: Path, identity: tuple[str, int, int] | None) -> bool:
    if identity is None or identity != _WAL_READY_IDENTITY:
        return False
    shm = Path(f"{path}-shm")
    try:
        return (shm.stat().st_mode & 0o777) == 0o600
    except OSError:
        return False


def auth_connect(*, timeout: float = 10):
    global _WAL_READY_IDENTITY
    path = auth_path()
    identity = _registry_file_identity(path)
    if identity is None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    con = sqlite3.connect(
        path, timeout=timeout, check_same_thread=False, factory=_AuthConnection
    )
    try:
        con.row_factory = sqlite3.Row
        busy_ms = max(5000, int(timeout * 1000))
        if not _wal_files_ready(path, identity):
            with _database_setup_lock(Path(os.path.abspath(path))):
                identity = _registry_file_identity(path)
                if not _wal_files_ready(path, identity):
                    restrict_auth_files(path)
                    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
                    if str(mode).lower() != "wal":
                        con.execute("PRAGMA journal_mode=WAL")
                    restrict_auth_files(path)
                    _WAL_READY_IDENTITY = _registry_file_identity(path)
        con.execute("PRAGMA foreign_keys=ON")
        con.execute(f"PRAGMA busy_timeout={busy_ms}")
        return con
    except Exception:
        con.close()
        raise


@dataclass(frozen=True)
class _CachedTokenDigest:
    token_hash: str
    token_id: int
    profile_id: str
    account_id: str | None


@dataclass
class _ActivitySlot:
    claimed_at: float
    db_timestamp: str
    committed: bool
    owner_id: str
    account_id: str | None = None


_AUTH_CACHE_LOCK = threading.Lock()
_CACHE_REGISTRY_IDENTITY: tuple[str, int, int] | None = None
_VERIFIED_TOKEN_DIGESTS: OrderedDict[str, _CachedTokenDigest] = OrderedDict()
_SESSION_ACTIVITY_WRITES: OrderedDict[str, _ActivitySlot] = OrderedDict()
_TOKEN_ACTIVITY_WRITES: OrderedDict[int, _ActivitySlot] = OrderedDict()
_TOKEN_DERIVATION_SEMAPHORE = threading.BoundedSemaphore(TOKEN_DERIVATION_CONCURRENCY)


def _registry_file_identity(path: Path | None = None) -> tuple[str, int, int] | None:
    target = path if path is not None else auth_path()
    try:
        st = target.stat()
    except OSError:
        return None
    return (os.path.abspath(target), st.st_dev, st.st_ino)


def _sync_cache_registry_identity_unlocked() -> None:
    global _CACHE_REGISTRY_IDENTITY
    identity = _registry_file_identity()
    if identity != _CACHE_REGISTRY_IDENTITY:
        _VERIFIED_TOKEN_DIGESTS.clear()
        _SESSION_ACTIVITY_WRITES.clear()
        _TOKEN_ACTIVITY_WRITES.clear()
        _CACHE_REGISTRY_IDENTITY = identity


def is_valid_api_token_format(token: str | None) -> bool:
    return bool(
        token
        and len(token) <= TOKEN_MAX_LENGTH
        and token.startswith(TOKEN_PREFIX)
        and not any(character.isspace() for character in token)
    )


def _token_cache_key(token: str) -> str:
    return hmac.new(
        auth_registry_key(), token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def has_cached_api_token(token: str | None) -> bool:
    """Return True if a token's HMAC key is currently in the verified digest cache."""
    if not is_valid_api_token_format(token):
        return False
    cache_key = _token_cache_key(token)
    with _AUTH_CACHE_LOCK:
        _sync_cache_registry_identity_unlocked()
        return cache_key in _VERIFIED_TOKEN_DIGESTS


def _get_cached_token_digest(cache_key: str) -> _CachedTokenDigest | None:
    with _AUTH_CACHE_LOCK:
        _sync_cache_registry_identity_unlocked()
        entry = _VERIFIED_TOKEN_DIGESTS.get(cache_key)
        if entry is not None:
            _VERIFIED_TOKEN_DIGESTS.move_to_end(cache_key)
        return entry


def _store_cached_token_digest(cache_key: str, entry: _CachedTokenDigest) -> None:
    with _AUTH_CACHE_LOCK:
        _sync_cache_registry_identity_unlocked()
        _VERIFIED_TOKEN_DIGESTS[cache_key] = entry
        _VERIFIED_TOKEN_DIGESTS.move_to_end(cache_key)
        while len(_VERIFIED_TOKEN_DIGESTS) > TOKEN_CACHE_MAX_ENTRIES:
            _VERIFIED_TOKEN_DIGESTS.popitem(last=False)


def invalidate_auth_caches(
    *,
    account_id: str | None = None,
    profile_id: str | None = None,
    token_id: int | None = None,
    cache_key: str | None = None,
    session_digest: str | None = None,
    clear_all: bool = False,
) -> None:
    """Immediately drop cached token digests and activity throttle state."""
    global _CACHE_REGISTRY_IDENTITY, _WAL_READY_IDENTITY
    with _AUTH_CACHE_LOCK:
        if clear_all:
            _VERIFIED_TOKEN_DIGESTS.clear()
            _SESSION_ACTIVITY_WRITES.clear()
            _TOKEN_ACTIVITY_WRITES.clear()
            _CACHE_REGISTRY_IDENTITY = _registry_file_identity()
            _WAL_READY_IDENTITY = None
            return
        if cache_key is not None:
            _VERIFIED_TOKEN_DIGESTS.pop(cache_key, None)
        if token_id is not None or profile_id is not None or account_id is not None:
            stale_keys = [
                key
                for key, item in _VERIFIED_TOKEN_DIGESTS.items()
                if (token_id is not None and item.token_id == token_id)
                or (profile_id is not None and item.profile_id == profile_id)
                or (account_id is not None and item.account_id == account_id)
            ]
            for key in stale_keys:
                _VERIFIED_TOKEN_DIGESTS.pop(key, None)
            stale_tokens = [
                tid
                for tid, slot in _TOKEN_ACTIVITY_WRITES.items()
                if (token_id is not None and tid == token_id)
                or (profile_id is not None and slot.owner_id == profile_id)
                or (account_id is not None and slot.account_id == account_id)
            ]
            for tid in stale_tokens:
                _TOKEN_ACTIVITY_WRITES.pop(tid, None)
        if session_digest is not None:
            _SESSION_ACTIVITY_WRITES.pop(session_digest, None)
        if account_id is not None:
            stale_sessions = [
                digest
                for digest, slot in _SESSION_ACTIVITY_WRITES.items()
                if slot.owner_id == account_id
            ]
            for digest in stale_sessions:
                _SESSION_ACTIVITY_WRITES.pop(digest, None)


def _parse_stored_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _claim_activity_slot(
    table: OrderedDict,
    key,
    *,
    now: datetime,
    current_db_timestamp: str | None,
    next_db_timestamp: str,
    owner_id: str,
    account_id: str | None = None,
    force: bool = False,
) -> tuple[bool, _ActivitySlot | None]:
    now_ts = now.timestamp()
    with _AUTH_CACHE_LOCK:
        _sync_cache_registry_identity_unlocked()
        previous = table.get(key)
        if not force and previous is not None:
            if not previous.committed:
                if 0 <= (now_ts - previous.claimed_at) < ACTIVITY_UPDATE_INTERVAL_SECONDS:
                    return False, previous
            elif (
                current_db_timestamp == previous.db_timestamp
                and 0 <= (now_ts - previous.claimed_at) < ACTIVITY_UPDATE_INTERVAL_SECONDS
            ):
                return False, previous
            elif current_db_timestamp is not None:
                parsed_db = _parse_stored_utc(current_db_timestamp)
                if (
                    parsed_db is not None
                    and 0 <= (now - parsed_db).total_seconds() < ACTIVITY_UPDATE_INTERVAL_SECONDS
                ):
                    return False, previous
        prior_copy = (
            _ActivitySlot(
                claimed_at=previous.claimed_at,
                db_timestamp=previous.db_timestamp,
                committed=previous.committed,
                owner_id=previous.owner_id,
                account_id=previous.account_id,
            )
            if previous is not None
            else None
        )
        table[key] = _ActivitySlot(
            claimed_at=now_ts,
            db_timestamp=next_db_timestamp,
            committed=False,
            owner_id=owner_id,
            account_id=account_id,
        )
        table.move_to_end(key)
        while len(table) > ACTIVITY_CACHE_MAX_ENTRIES:
            table.popitem(last=False)
        return True, prior_copy


def _finalize_activity_slot(
    table: OrderedDict,
    key,
    claimed_at: float,
    previous: _ActivitySlot | None,
    *,
    committed: bool,
) -> None:
    with _AUTH_CACHE_LOCK:
        slot = table.get(key)
        if slot is None or slot.claimed_at != claimed_at:
            return
        if committed:
            slot.committed = True
        elif previous is None:
            table.pop(key, None)
        else:
            table[key] = previous


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


def initialize_auth() -> None:
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
        # Older releases stored a setup-token digest here. First-admin setup
        # is now an atomic first-visitor claim, so remove any obsolete digest.
        con.execute("DELETE FROM auth_meta WHERE key='setup_token_hash'")


def setup_required() -> bool:
    with auth_connect() as con:
        return con.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is None


def create_first_admin(username: str, password: str):
    # Avoid an expensive Argon2 hash on every setup request after the account
    # exists, then re-check under a write lock to make concurrent first visits
    # mutually exclusive.
    with auth_connect() as con:
        if con.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
            raise PermissionError("Initial account setup has already completed.")
    clean, password_hash = validate_credentials(username, password)
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
            con.rollback()
            raise PermissionError("Initial account setup has already completed.")
        account_id = uuid.uuid4().hex
        con.execute(
            "INSERT INTO accounts(id,profile_id,username,normalized_username,display_name,role,password_hash) "
            "VALUES(?,?,?,?,?,'admin',?)",
            (account_id, "legacy", clean, normalize_username(clean), clean, password_hash),
        )
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
        target = con.execute(
            "SELECT profile_id FROM accounts WHERE id=? AND status='active'",
            (account_id,),
        ).fetchone()
        if not target:
            con.rollback()
            raise LookupError("Account not found.")
        con.execute(
            "UPDATE accounts SET password_hash=?,must_change_password=0,auth_version=auth_version+1,updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='active'",
            (password_hash, account_id),
        )
        con.execute(
            "UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE account_id=? AND revoked_at IS NULL",
            (account_id,),
        )
    invalidate_auth_caches(account_id=account_id, profile_id=target["profile_id"])


def reset_password(account_id: str, password: str) -> None:
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Use a password between 12 and 1024 characters.")
    password_hash = PASSWORD_HASHER.hash(password)
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        target = con.execute(
            "SELECT profile_id FROM accounts WHERE id=? AND status='active'",
            (account_id,),
        ).fetchone()
        if not target:
            con.rollback()
            raise LookupError("Account not found.")
        con.execute(
            "UPDATE accounts SET password_hash=?,must_change_password=1,auth_version=auth_version+1,updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='active'",
            (password_hash, account_id),
        )
        con.execute(
            "UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE account_id=? AND revoked_at IS NULL",
            (account_id,),
        )
    invalidate_auth_caches(account_id=account_id, profile_id=target["profile_id"])


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
        found = con.execute(
            "SELECT a.*,s.token_hash,s.created_at session_created_at,s.last_seen_at,"
            "s.idle_expires_at,s.absolute_expires_at,s.auth_version session_auth_version "
            "FROM auth_sessions s JOIN accounts a ON a.id=s.account_id "
            "JOIN profiles p ON p.id=a.profile_id "
            "WHERE s.token_hash=? AND s.revoked_at IS NULL AND a.status='active' AND p.status='active'",
            (digest,),
        ).fetchone()
        if not found or found["auth_version"] != found["session_auth_version"]:
            return None
        if now.isoformat() >= found["idle_expires_at"] or now.isoformat() >= found["absolute_expires_at"]:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE auth_sessions SET revoked_at=COALESCE(revoked_at,?) WHERE token_hash=?",
                (iso(now), digest),
            )
            con.commit()
            invalidate_auth_caches(session_digest=digest)
            return None

        absolute_dt = datetime.fromisoformat(found["absolute_expires_at"])
        idle_dt = datetime.fromisoformat(found["idle_expires_at"])
        near_idle_expiry = (
            idle_dt < absolute_dt
            and (idle_dt - now) <= timedelta(seconds=ACTIVITY_UPDATE_INTERVAL_SECONDS)
        )
        next_seen = iso(now)
        should_write, prior_slot = _claim_activity_slot(
            _SESSION_ACTIVITY_WRITES,
            digest,
            now=now,
            current_db_timestamp=found["last_seen_at"],
            next_db_timestamp=next_seen,
            owner_id=found["id"],
            account_id=found["id"],
            force=near_idle_expiry,
        )
        if should_write:
            claimed_at = now.timestamp()
            committed = False
            try:
                next_idle = min(
                    now + timedelta(hours=SESSION_IDLE_HOURS), absolute_dt
                ).isoformat(timespec="seconds")
                con.execute("BEGIN IMMEDIATE")
                updated = con.execute(
                    "UPDATE auth_sessions SET last_seen_at=?,idle_expires_at=? "
                    "WHERE token_hash=? AND revoked_at IS NULL "
                    "AND auth_version=(SELECT auth_version FROM accounts WHERE id=? AND status='active') "
                    "AND EXISTS (SELECT 1 FROM profiles WHERE id=? AND status='active')",
                    (next_seen, next_idle, digest, found["id"], found["profile_id"]),
                ).rowcount
                if not updated:
                    con.rollback()
                    invalidate_auth_caches(session_digest=digest)
                    return None
                con.commit()
                committed = True
            finally:
                _finalize_activity_slot(
                    _SESSION_ACTIVITY_WRITES,
                    digest,
                    claimed_at,
                    prior_slot,
                    committed=committed,
                )
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
        invalidate_auth_caches(session_digest=digest)


def set_account_status(account_id: str, status: str):
    if status not in {"active", "disabled"}:
        raise ValueError("Invalid account status.")
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        target = con.execute(
            "SELECT role,status,profile_id FROM accounts WHERE id=?", (account_id,)
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
        con.execute("UPDATE profiles SET status=? WHERE id=?", (status, target["profile_id"]))
        con.execute("UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE account_id=? AND revoked_at IS NULL", (account_id,))
        if status == "disabled":
            con.execute(
                "UPDATE api_tokens SET revoked_at=COALESCE(revoked_at,CURRENT_TIMESTAMP) "
                "WHERE profile_id=?",
                (target["profile_id"],),
            )
    invalidate_auth_caches(account_id=account_id, profile_id=target["profile_id"])


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
    invalidate_auth_caches(profile_id=profile_id, token_id=token_id)


_ACTIVE_TOKEN_QUERY = (
    "SELECT t.id,t.name,t.profile_id,t.token_hash,t.last_used_at,a.id AS account_id "
    "FROM api_tokens t "
    "JOIN profiles p ON p.id=t.profile_id "
    "LEFT JOIN accounts a ON a.profile_id=p.id "
    "WHERE t.token_hash=? AND t.revoked_at IS NULL "
    "AND p.status='active' AND COALESCE(a.status,'active')='active'"
)


def authenticate_api_token(token: str):
    if not is_valid_api_token_format(token):
        return None
    cache_key = _token_cache_key(token)
    cached = _get_cached_token_digest(cache_key)
    current_hash: str | None = cached.token_hash if cached is not None else None
    legacy_hash: str | None = None
    needs_upgrade = False

    if current_hash is not None:
        with auth_connect() as con:
            found = con.execute(_ACTIVE_TOKEN_QUERY, (current_hash,)).fetchone()
            if found is None:
                invalidate_auth_caches(cache_key=cache_key)
                legacy_candidate = legacy_hash_api_token(token)
                if con.execute(_ACTIVE_TOKEN_QUERY, (legacy_candidate,)).fetchone() is None:
                    return None
                current_hash = None
            else:
                return _complete_api_token_auth(
                    con,
                    found=found,
                    cache_key=cache_key,
                    resolved_hash=current_hash,
                    needs_upgrade=False,
                )

    with _TOKEN_DERIVATION_SEMAPHORE:
        rechecked = _get_cached_token_digest(cache_key)
        if rechecked is not None:
            current_hash = rechecked.token_hash
        else:
            current_hash = hash_api_token(token)
        legacy_hash = legacy_hash_api_token(token)

    with auth_connect() as con:
        found = con.execute(_ACTIVE_TOKEN_QUERY, (current_hash,)).fetchone()
        if found is None:
            found = con.execute(_ACTIVE_TOKEN_QUERY, (legacy_hash,)).fetchone()
            if found is None:
                return None
            needs_upgrade = legacy_hash != current_hash
        return _complete_api_token_auth(
            con,
            found=found,
            cache_key=cache_key,
            resolved_hash=current_hash,
            needs_upgrade=needs_upgrade,
        )


def _complete_api_token_auth(
    con: sqlite3.Connection,
    *,
    found: sqlite3.Row,
    cache_key: str,
    resolved_hash: str,
    needs_upgrade: bool,
) -> dict | None:
    now = utc_now()
    next_used = now.strftime("%Y-%m-%d %H:%M:%S")
    first_use = found["last_used_at"] is None
    should_write, prior_slot = _claim_activity_slot(
        _TOKEN_ACTIVITY_WRITES,
        int(found["id"]),
        now=now,
        current_db_timestamp=found["last_used_at"],
        next_db_timestamp=next_used,
        owner_id=found["profile_id"],
        account_id=found["account_id"],
        force=first_use or needs_upgrade,
    )
    if should_write:
        claimed_at = now.timestamp()
        committed = False
        try:
            con.execute("BEGIN IMMEDIATE")
            if needs_upgrade:
                updated = con.execute(
                    "UPDATE api_tokens SET token_hash=?,last_used_at=? "
                    "WHERE id=? AND revoked_at IS NULL "
                    "AND EXISTS ("
                    "SELECT 1 FROM profiles p LEFT JOIN accounts a ON a.profile_id=p.id "
                    "WHERE p.id=? AND p.status='active' AND COALESCE(a.status,'active')='active'"
                    ")",
                    (resolved_hash, next_used, found["id"], found["profile_id"]),
                ).rowcount
            else:
                updated = con.execute(
                    "UPDATE api_tokens SET last_used_at=? "
                    "WHERE id=? AND revoked_at IS NULL "
                    "AND EXISTS ("
                    "SELECT 1 FROM profiles p LEFT JOIN accounts a ON a.profile_id=p.id "
                    "WHERE p.id=? AND p.status='active' AND COALESCE(a.status,'active')='active'"
                    ")",
                    (next_used, found["id"], found["profile_id"]),
                ).rowcount
            if not updated:
                con.rollback()
                invalidate_auth_caches(
                    cache_key=cache_key,
                    token_id=int(found["id"]),
                    profile_id=found["profile_id"],
                )
                return None
            con.commit()
            committed = True
        finally:
            _finalize_activity_slot(
                _TOKEN_ACTIVITY_WRITES,
                int(found["id"]),
                claimed_at,
                prior_slot,
                committed=committed,
            )

    _store_cached_token_digest(
        cache_key,
        _CachedTokenDigest(
            token_hash=resolved_hash,
            token_id=int(found["id"]),
            profile_id=found["profile_id"],
            account_id=found["account_id"],
        ),
    )
    return {
        "id": found["id"],
        "name": found["name"],
        "profile_id": found["profile_id"],
        "token_hash": resolved_hash,
    }


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
