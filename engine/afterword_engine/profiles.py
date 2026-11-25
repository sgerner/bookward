"""Installation-wide identity, session, and API-token registry.

Recommendation data lives in a separate SQLite file per profile. This module
stores only login identity and the opaque mapping that selects that file.
"""

from collections import OrderedDict, defaultdict
from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import logging
import math
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.hmac import HMAC

from .api_tokens import (
    TOKEN_MAX_LENGTH,
    TOKEN_PREFIX,
    generate_api_token,
    hash_api_token,
    legacy_hash_api_token,
    token_prefix,
)
from .config import settings
from .secrets import auth_registry_key

LOGGER = logging.getLogger(__name__)
SESSION_IDLE_HOURS = 24
SESSION_ABSOLUTE_DAYS = 7
LOGIN_FAILURE_LIMIT = 10
LOGIN_LOCKOUT_WINDOW = timedelta(minutes=15)
LOGIN_ATTEMPT_RETENTION = timedelta(days=1)
LOCKOUT_NOTICE_PREFIX = "lockout_notice:"
AUTH_BUSY_TIMEOUT_MS = 5000
TOKEN_CACHE_MAX_ENTRIES = 1024
TOKEN_DERIVATION_CONCURRENCY = max(1, min(2, (os.cpu_count() or 2) // 2))
TOKEN_DERIVATION_WAIT_SECONDS = 5.0
PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
DUMMY_PASSWORD_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


def format_lockout_message(retry_after_seconds: int) -> str:
    seconds = max(1, int(retry_after_seconds))
    if seconds >= 60:
        minutes = (seconds + 59) // 60
        unit = "minute" if minutes == 1 else "minutes"
        wait = f"{minutes} {unit}"
    else:
        unit = "second" if seconds == 1 else "seconds"
        wait = f"{seconds} {unit}"
    return f"Too many failed password attempts. Wait {wait} before trying again."


class LoginRateLimitError(PermissionError):
    """Raised when a username and client/session bucket has reached the failure limit."""

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        super().__init__(format_lockout_message(self.retry_after_seconds))

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


class _AuthConnection(sqlite3.Connection):
    """Close identity-registry connections deterministically on context exit."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def auth_connect(*, timeout: float = 10):
    path = auth_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    created = False
    if not path.exists():
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            created = True
        except FileExistsError:
            pass
    busy_ms = max(1, int(AUTH_BUSY_TIMEOUT_MS))
    connect_timeout = timeout if AUTH_BUSY_TIMEOUT_MS == 5000 else max(0.001, busy_ms / 1000)
    con = sqlite3.connect(path, timeout=connect_timeout, factory=_AuthConnection)
    try:
        try:
            path.chmod(0o600)
            if _CHMOD_WARNED_IDENTITIES:
                identity = _registry_file_identity(path)
                if identity is not None:
                    with _AUTH_CACHE_LOCK:
                        _CHMOD_WARNED_IDENTITIES.discard(identity)
        except OSError as exc:
            if created:
                raise
            identity = _registry_file_identity(path)
            should_warn = identity is None
            if identity is not None:
                with _AUTH_CACHE_LOCK:
                    if identity not in _CHMOD_WARNED_IDENTITIES:
                        _CHMOD_WARNED_IDENTITIES.add(identity)
                        should_warn = True
            if should_warn:
                LOGGER.warning(
                    "Could not restrict permissions on existing auth registry %s: %s",
                    path,
                    exc,
                )
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute(f"PRAGMA busy_timeout={busy_ms}")
        return con
    except Exception:
        con.close()
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise


@dataclass(frozen=True)
class _CachedTokenDigest:
    token_hash: str
    token_id: int
    profile_id: str
    account_id: str | None


_AUTH_CACHE_LOCK = threading.Lock()
_CACHE_REGISTRY_IDENTITY: tuple[str, int, int] | None = None
_CHMOD_WARNED_IDENTITIES: set[tuple[str, int, int]] = set()
_VERIFIED_TOKEN_DIGESTS: OrderedDict[str, _CachedTokenDigest] = OrderedDict()


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
        _CACHE_REGISTRY_IDENTITY = identity


def is_valid_api_token_format(token: str | None) -> bool:
    return bool(
        token
        and len(token) <= TOKEN_MAX_LENGTH
        and token.startswith(TOKEN_PREFIX)
        and not any(character.isspace() for character in token)
    )


def _token_cache_key(token: str) -> str:
    mac = HMAC(auth_registry_key(), hashes.SHA256())
    mac.update(token.encode("utf-8"))
    return mac.finalize().hex()


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
    clear_all: bool = False,
) -> None:
    """Immediately drop cached API token digests matching the invalidated scope."""
    global _CACHE_REGISTRY_IDENTITY
    with _AUTH_CACHE_LOCK:
        if clear_all:
            _VERIFIED_TOKEN_DIGESTS.clear()
            _CHMOD_WARNED_IDENTITIES.clear()
            _CACHE_REGISTRY_IDENTITY = _registry_file_identity()
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
    invalidate_auth_caches(profile_id="legacy")
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
            "SELECT normalized_username FROM accounts WHERE id=? AND status='active'",
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
        con.execute(
            "DELETE FROM login_attempts WHERE normalized_username=? AND ("
            "(remote_address>='session:' AND remote_address<'session;') OR "
            "(remote_address>='pwchange:' AND remote_address<'pwchange;')"
            ") AND succeeded=0",
            (target["normalized_username"],),
        )
        _clear_account_lockout_notices(con, target["normalized_username"], password_change_only=True)
    invalidate_auth_caches(account_id=account_id)


def reset_password(account_id: str, password: str) -> None:
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Use a password between 12 and 1024 characters.")
    password_hash = PASSWORD_HASHER.hash(password)
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        target = con.execute(
            "SELECT normalized_username FROM accounts WHERE id=? AND status='active'",
            (account_id,),
        ).fetchone()
        if not target:
            con.rollback()
            raise LookupError("Account not found.")
        con.execute(
            "UPDATE accounts SET password_hash=?,must_change_password=1,auth_version=auth_version+1,updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='active'", (password_hash, account_id)
        )
        con.execute("UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE account_id=? AND revoked_at IS NULL", (account_id,))
        con.execute(
            "DELETE FROM login_attempts WHERE normalized_username=? AND succeeded=0",
            (target["normalized_username"],),
        )
        _clear_account_lockout_notices(con, target["normalized_username"])
    invalidate_auth_caches(account_id=account_id)


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


def _sanitize_log_field(value: object) -> str:
    return str(value).replace("\r", "").replace("\n", "")


def _login_bucket(remote_address: str, scope: Literal["login", "session"] = "login") -> str:
    if scope == "session":
        session_digest = (remote_address or "").removeprefix("session:").strip()[:64] or "unknown"
        return f"session:{session_digest}"
    cleaned = (remote_address or "").strip()
    while cleaned.lower().startswith(("session:", "pwchange:")):
        prefix_len = 8 if cleaned.lower().startswith("session:") else 9
        cleaned = cleaned[prefix_len:].strip()
    return cleaned[:80] or "unknown"


def _password_change_ip_bucket(client_address: str) -> str | None:
    if not (client_address or "").strip():
        return None
    return f"pwchange:{_login_bucket(client_address, 'login')}"


def _log_rate_limited_verification(
    scope: Literal["login", "session"],
    normalized_username: str,
    bucket_label: str,
    retry_after: int,
) -> None:
    safe_username = _sanitize_log_field(normalized_username)
    safe_bucket = _sanitize_log_field(bucket_label)
    LOGGER.warning(
        "Rate-limited %s password verification for username=%r bucket=%r after %d failed attempts (retry_after=%ds)",
        scope,
        safe_username,
        safe_bucket,
        LOGIN_FAILURE_LIMIT,
        retry_after,
    )


def _redact_ip_for_log(raw_address: str) -> str:
    candidate = _login_bucket(raw_address, "login")
    if candidate == "unknown":
        return "unknown"
    host = candidate
    if host.startswith("[") and "]" in host:
        host_part, _, remainder = host[1:].partition("]")
        if not remainder or (remainder.startswith(":") and remainder[1:].isdigit()):
            host = host_part
    elif host.count(":") == 1:
        host_part, port_part = host.split(":", 1)
        if port_part.isdigit():
            host = host_part
    try:
        parsed = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return "non-ip-peer"
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    if isinstance(parsed, ipaddress.IPv4Address):
        return str(ipaddress.ip_network(f"{parsed}/24", strict=False))
    return str(ipaddress.ip_network(f"{parsed}/48", strict=False))


def _emit_rate_limit_warning(
    scope: Literal["login", "session"],
    normalized_username: str,
    log_bucket_index: int,
    remote_address: str,
    client_address: str,
    retry_after: int,
) -> None:
    if scope == "session":
        bucket_label = (
            "authenticated-session"
            if log_bucket_index == 0
            else f"pwchange:{_redact_ip_for_log(client_address)}"
        )
    else:
        bucket_label = _redact_ip_for_log(remote_address)
    _log_rate_limited_verification(scope, normalized_username, bucket_label, retry_after)


def _log_account_unlock(admin_id: str, target_account_id: str, cleared_attempts: int) -> None:
    LOGGER.warning(
        "Account unlock completed: admin_id=%s target_account_id=%s cleared_attempts=%d",
        _sanitize_log_field(admin_id),
        _sanitize_log_field(target_account_id),
        int(cleared_attempts),
    )


def _lockout_notice_key(normalized_username: str, bucket: str) -> str:
    return f"{LOCKOUT_NOTICE_PREFIX}{len(normalized_username)}:{normalized_username}:{bucket}"


def _clear_account_lockout_notices(
    con, normalized_username: str, *, password_change_only: bool = False
) -> None:
    prefix = f"{LOCKOUT_NOTICE_PREFIX}{len(normalized_username)}:{normalized_username}:"
    if password_change_only:
        con.execute(
            "DELETE FROM auth_meta WHERE (key>=? AND key<?) OR (key>=? AND key<?)",
            (f"{prefix}session:", f"{prefix}session;", f"{prefix}pwchange:", f"{prefix}pwchange;"),
        )
    else:
        end = f"{LOCKOUT_NOTICE_PREFIX}{len(normalized_username)}:{normalized_username};"
        con.execute("DELETE FROM auth_meta WHERE key>=? AND key<?", (prefix, end))


def _clear_all_lockout_notices(con) -> None:
    con.execute("DELETE FROM auth_meta WHERE key>='lockout_notice:' AND key<'lockout_notice;'")


def _parse_attempt_time(raw: str, fallback: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(raw).replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _retry_after_from_attempt(tenth_attempt_raw: str, now_dt: datetime) -> tuple[int, str]:
    tenth_dt = _parse_attempt_time(tenth_attempt_raw, now_dt)
    unlock_at = tenth_dt + LOGIN_LOCKOUT_WINDOW
    remaining = math.ceil((unlock_at - now_dt).total_seconds())
    retry_after = max(1, min(int(LOGIN_LOCKOUT_WINDOW.total_seconds()), remaining))
    return retry_after, iso(unlock_at)


def accounts_list():
    now_dt = utc_now()
    cutoff = iso(now_dt - LOGIN_LOCKOUT_WINDOW)
    with auth_connect() as con:
        account_rows = con.execute("SELECT * FROM accounts ORDER BY created_at,username").fetchall()
        attempt_rows = con.execute(
            "SELECT normalized_username,remote_address,attempted_at FROM login_attempts "
            "WHERE succeeded=0 AND attempted_at>? ORDER BY attempted_at DESC,id DESC",
            (cutoff,),
        ).fetchall()
    buckets_by_user: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for attempt in attempt_rows:
        buckets_by_user[attempt["normalized_username"]][attempt["remote_address"]].append(
            attempt["attempted_at"]
        )
    result = []
    for row in account_rows:
        item = {key: value for key, value in _account(row).items() if key != "auth_version"}
        user_buckets = buckets_by_user.get(row["normalized_username"], {})
        locked_count = 0
        max_retry_after = 0
        total_recent = 0
        for attempts in user_buckets.values():
            total_recent += len(attempts)
            if len(attempts) >= LOGIN_FAILURE_LIMIT:
                locked_count += 1
                retry_after, _ = _retry_after_from_attempt(
                    attempts[LOGIN_FAILURE_LIMIT - 1], now_dt
                )
                max_retry_after = max(max_retry_after, retry_after)
        item["recent_failed_attempts"] = total_recent
        item["locked_bucket_count"] = locked_count
        item["lockout_retry_after_seconds"] = max_retry_after
        result.append(item)
    return result


def unlock_account(account_id: str, *, admin_id: str = "cli") -> dict:
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        target = con.execute(
            "SELECT id,username,normalized_username FROM accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        if not target:
            con.rollback()
            raise LookupError("Account not found.")
        cleared = con.execute(
            "DELETE FROM login_attempts WHERE normalized_username=? AND succeeded=0",
            (target["normalized_username"],),
        ).rowcount
        _clear_account_lockout_notices(con, target["normalized_username"])
        con.commit()
    _log_account_unlock(admin_id, target["id"], int(cleared))
    return {
        "unlocked": True,
        "account_id": target["id"],
        "username": target["username"],
        "cleared_attempts": int(cleared),
    }


def unlock_username(
    username: str, remote_address: str | None = None, *, admin_id: str = "cli"
) -> dict:
    clean = username.strip()
    if not clean:
        raise ValueError("Enter a valid username.")
    normalized = normalize_username(clean)
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        target = con.execute(
            "SELECT id,username,normalized_username FROM accounts WHERE normalized_username=? OR id=?",
            (normalized, clean),
        ).fetchone()
        if not target:
            con.rollback()
            raise LookupError(f"Account not found: {clean}")
        account_norm = target["normalized_username"]
        if remote_address is not None and remote_address.strip():
            bucket = _login_bucket(remote_address, "login")
            pwchange_bucket = f"pwchange:{bucket}"
            cleared = con.execute(
                "DELETE FROM login_attempts WHERE normalized_username=? AND remote_address IN (?,?) AND succeeded=0",
                (account_norm, bucket, pwchange_bucket),
            ).rowcount
            con.execute(
                "DELETE FROM auth_meta WHERE key IN (?,?)",
                (
                    _lockout_notice_key(account_norm, bucket),
                    _lockout_notice_key(account_norm, pwchange_bucket),
                ),
            )
        else:
            cleared = con.execute(
                "DELETE FROM login_attempts WHERE normalized_username=? AND succeeded=0",
                (account_norm,),
            ).rowcount
            _clear_account_lockout_notices(con, account_norm)
        con.commit()
    _log_account_unlock(admin_id, target["id"], int(cleared))
    return {
        "unlocked": True,
        "account_id": target["id"],
        "username": target["username"],
        "cleared_attempts": int(cleared),
    }


def unlock_all_accounts(*, admin_id: str = "cli") -> dict:
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        cleared = con.execute("DELETE FROM login_attempts WHERE succeeded=0").rowcount
        _clear_all_lockout_notices(con)
        con.commit()
    _log_account_unlock(admin_id, "all", int(cleared))
    return {"unlocked": True, "cleared_attempts": int(cleared)}


def _inspect_rate_limit_buckets(
    con,
    normalized: str,
    buckets: list[str],
    cutoff: str,
    now_dt: datetime,
    now: str,
) -> tuple[dict[str, list], tuple[int, int | None] | None]:
    rows_by_bucket: dict[str, list] = {}
    locked_retry_after = 0
    first_unlogged_index: int | None = None
    is_locked = False
    for idx, bucket_key in enumerate(buckets):
        recent_rows = con.execute(
            "SELECT attempted_at FROM login_attempts WHERE normalized_username=? AND remote_address=? "
            "AND succeeded=0 AND attempted_at>? ORDER BY attempted_at DESC,id DESC LIMIT ?",
            (normalized, bucket_key, cutoff, LOGIN_FAILURE_LIMIT),
        ).fetchall()
        rows_by_bucket[bucket_key] = recent_rows
        if len(recent_rows) >= LOGIN_FAILURE_LIMIT:
            is_locked = True
            retry_after, unlock_iso = _retry_after_from_attempt(
                recent_rows[LOGIN_FAILURE_LIMIT - 1]["attempted_at"], now_dt
            )
            locked_retry_after = max(locked_retry_after, retry_after)
            notice_key = _lockout_notice_key(normalized, bucket_key)
            existing_notice = con.execute(
                "SELECT value FROM auth_meta WHERE key=?", (notice_key,)
            ).fetchone()
            if existing_notice is None or existing_notice["value"] <= now:
                con.execute(
                    "INSERT INTO auth_meta(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (notice_key, unlock_iso),
                )
                if first_unlogged_index is None:
                    first_unlogged_index = idx
    if is_locked:
        return rows_by_bucket, (locked_retry_after, first_unlogged_index)
    return rows_by_bucket, None


def authenticate_local(
    username: str,
    password: str,
    remote_address: str = "",
    *,
    scope: Literal["login", "session"] = "login",
    client_address: str = "",
):
    normalized = normalize_username(username)
    primary_bucket = _login_bucket(remote_address, scope)
    buckets = [primary_bucket]
    if scope == "session":
        pwchange_bucket = _password_change_ip_bucket(client_address)
        if pwchange_bucket:
            buckets.append(pwchange_bucket)

    now_dt = utc_now()
    now = iso(now_dt)
    cutoff = iso(now_dt - LOGIN_LOCKOUT_WINDOW)
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "DELETE FROM login_attempts WHERE attempted_at<?",
            (iso(now_dt - LOGIN_ATTEMPT_RETENTION),),
        )
        con.execute(
            "DELETE FROM auth_meta WHERE key>='lockout_notice:' AND key<'lockout_notice;' AND value<=?",
            (now,),
        )
        _, locked_info = _inspect_rate_limit_buckets(
            con, normalized, buckets, cutoff, now_dt, now
        )
        if locked_info is not None:
            retry_after, log_bucket_index = locked_info
            con.commit()
            if log_bucket_index is not None:
                _emit_rate_limit_warning(
                    scope, normalized, log_bucket_index, remote_address, client_address, retry_after
                )
            raise LoginRateLimitError(retry_after)
        found = con.execute(
            "SELECT * FROM accounts WHERE normalized_username=? AND status='active'",
            (normalized,),
        ).fetchone()
        con.commit()

    valid = False
    rehashed: str | None = None
    if found and found["password_hash"]:
        try:
            valid = PASSWORD_HASHER.verify(found["password_hash"], password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            valid = False
        if valid and PASSWORD_HASHER.check_needs_rehash(found["password_hash"]):
            rehashed = PASSWORD_HASHER.hash(password)
    else:
        try:
            PASSWORD_HASHER.verify(DUMMY_PASSWORD_HASH, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            pass

    now_dt = utc_now()
    now = iso(now_dt)
    cutoff = iso(now_dt - LOGIN_LOCKOUT_WINDOW)
    log_bucket_index: int | None = None
    retry_after = 0
    with auth_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        rows_by_bucket, locked_info = _inspect_rate_limit_buckets(
            con, normalized, buckets, cutoff, now_dt, now
        )
        if locked_info is not None:
            retry_after, log_bucket_index = locked_info
            con.commit()
            if log_bucket_index is not None:
                _emit_rate_limit_warning(
                    scope, normalized, log_bucket_index, remote_address, client_address, retry_after
                )
            raise LoginRateLimitError(retry_after)

        current_found = None
        if valid and found is not None:
            current_found = con.execute(
                "SELECT * FROM accounts WHERE id=? AND status='active'",
                (found["id"],),
            ).fetchone()
            if not current_found or current_found["auth_version"] != found["auth_version"]:
                valid = False

        if valid and current_found is not None:
            if rehashed is not None:
                con.execute(
                    "UPDATE accounts SET password_hash=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (rehashed, current_found["id"]),
                )
            for bucket_key in buckets:
                con.execute(
                    "INSERT INTO login_attempts(normalized_username,remote_address,succeeded,attempted_at) VALUES(?,?,1,?)",
                    (normalized, bucket_key, now),
                )
                con.execute(
                    "DELETE FROM login_attempts WHERE normalized_username=? AND remote_address=? AND succeeded=0",
                    (normalized, bucket_key),
                )
                con.execute(
                    "DELETE FROM auth_meta WHERE key=?",
                    (_lockout_notice_key(normalized, bucket_key),),
                )
            con.commit()
            return _account(current_found)

        for idx, bucket_key in enumerate(buckets):
            con.execute(
                "INSERT INTO login_attempts(normalized_username,remote_address,succeeded,attempted_at) VALUES(?,?,0,?)",
                (normalized, bucket_key, now),
            )
            recent_rows = rows_by_bucket[bucket_key]
            if len(recent_rows) + 1 >= LOGIN_FAILURE_LIMIT:
                tenth_raw = (
                    recent_rows[LOGIN_FAILURE_LIMIT - 2]["attempted_at"]
                    if LOGIN_FAILURE_LIMIT >= 2 and len(recent_rows) >= LOGIN_FAILURE_LIMIT - 1
                    else now
                )
                bucket_retry_after, unlock_iso = _retry_after_from_attempt(tenth_raw, now_dt)
                retry_after = max(retry_after, bucket_retry_after)
                con.execute(
                    "INSERT INTO auth_meta(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (_lockout_notice_key(normalized, bucket_key), unlock_iso),
                )
                if log_bucket_index is None:
                    log_bucket_index = idx
        con.commit()
    if log_bucket_index is not None:
        _emit_rate_limit_warning(
            scope, normalized, log_bucket_index, remote_address, client_address, retry_after
        )
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
        account["session_token_hash"] = found["token_hash"]
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
    "SELECT t.id,t.name,t.profile_id,t.token_hash,a.id AS account_id "
    "FROM api_tokens t "
    "JOIN profiles p ON p.id=t.profile_id "
    "LEFT JOIN accounts a ON a.profile_id=p.id "
    "WHERE t.token_hash=? AND t.revoked_at IS NULL "
    "AND p.status='active' AND COALESCE(a.status,'active')='active'"
)


def authenticate_api_token(token: str):
    """Verify a personal API token against live registry state.

    Verified PBKDF2 digests are cached in memory by ``HMAC-SHA256`` so repeat
    requests avoid recomputing the 600,000-iteration derivation, while every
    call still queries ``auth.sqlite3`` to enforce immediate token revocation,
    active profile status, and active owning-account status (when an account
    exists for the profile).
    """
    if not is_valid_api_token_format(token):
        return None
    cache_key = _token_cache_key(token)
    cached = _get_cached_token_digest(cache_key)
    current_hash: str | None = cached.token_hash if cached is not None else None
    legacy_hash: str | None = None

    if current_hash is not None:
        with auth_connect() as con:
            found = con.execute(_ACTIVE_TOKEN_QUERY, (current_hash,)).fetchone()
            if found is not None:
                return _complete_api_token_auth(
                    con,
                    found=found,
                    cache_key=cache_key,
                    resolved_hash=current_hash,
                    expected_hash=current_hash,
                    needs_upgrade=False,
                )
            invalidate_auth_caches(cache_key=cache_key)
            legacy_candidate = legacy_hash_api_token(token)
            if con.execute(_ACTIVE_TOKEN_QUERY, (legacy_candidate,)).fetchone() is None:
                return None
            current_hash = None

    current_hash = hash_api_token(token)
    legacy_hash = legacy_hash_api_token(token)

    with auth_connect() as con:
        found = con.execute(_ACTIVE_TOKEN_QUERY, (current_hash,)).fetchone()
        needs_upgrade = False
        expected_hash = current_hash
        if found is None:
            found = con.execute(_ACTIVE_TOKEN_QUERY, (legacy_hash,)).fetchone()
            if found is not None:
                expected_hash = legacy_hash
                needs_upgrade = legacy_hash != current_hash
            else:
                # A concurrent legacy upgrade may have committed between the two
                # lookups, so look for the upgraded hash once more before failing.
                found = con.execute(_ACTIVE_TOKEN_QUERY, (current_hash,)).fetchone()
                if found is None:
                    return None
        return _complete_api_token_auth(
            con,
            found=found,
            cache_key=cache_key,
            resolved_hash=current_hash,
            expected_hash=expected_hash,
            needs_upgrade=needs_upgrade,
        )


def _complete_api_token_auth(
    con: sqlite3.Connection,
    *,
    found: sqlite3.Row,
    cache_key: str,
    resolved_hash: str,
    expected_hash: str,
    needs_upgrade: bool,
) -> dict | None:
    con.execute("BEGIN IMMEDIATE")
    if needs_upgrade:
        updated = con.execute(
            "UPDATE api_tokens SET token_hash=?,last_used_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND token_hash=? AND revoked_at IS NULL "
            "AND EXISTS ("
            "SELECT 1 FROM profiles p LEFT JOIN accounts a ON a.profile_id=p.id "
            "WHERE p.id=? AND p.status='active' AND COALESCE(a.status,'active')='active'"
            ")",
            (resolved_hash, found["id"], expected_hash, found["profile_id"]),
        ).rowcount
        if not updated:
            # Another caller may have completed the identical legacy upgrade
            # between the read SELECT and BEGIN IMMEDIATE.
            updated = con.execute(
                "UPDATE api_tokens SET last_used_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND token_hash=? AND revoked_at IS NULL "
                "AND EXISTS ("
                "SELECT 1 FROM profiles p LEFT JOIN accounts a ON a.profile_id=p.id "
                "WHERE p.id=? AND p.status='active' AND COALESCE(a.status,'active')='active'"
                ")",
                (found["id"], resolved_hash, found["profile_id"]),
            ).rowcount
    else:
        updated = con.execute(
            "UPDATE api_tokens SET last_used_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND token_hash=? AND revoked_at IS NULL "
            "AND EXISTS ("
            "SELECT 1 FROM profiles p LEFT JOIN accounts a ON a.profile_id=p.id "
            "WHERE p.id=? AND p.status='active' AND COALESCE(a.status,'active')='active'"
            ")",
            (found["id"], expected_hash, found["profile_id"]),
        ).rowcount
    if not updated:
        con.rollback()
        invalidate_auth_caches(
            cache_key=cache_key,
            token_id=int(found["id"]),
            profile_id=found["profile_id"],
        )
        return None
    account_id = found["account_id"]
    if account_id is None:
        owner = con.execute(
            "SELECT id FROM accounts WHERE profile_id=?", (found["profile_id"],)
        ).fetchone()
        account_id = owner["id"] if owner is not None else None
    con.commit()
    _store_cached_token_digest(
        cache_key,
        _CachedTokenDigest(
            token_hash=resolved_hash,
            token_id=int(found["id"]),
            profile_id=found["profile_id"],
            account_id=account_id,
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
