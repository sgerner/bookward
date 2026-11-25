import os
import re
import threading
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
from .config import settings
from .tenancy import current_profile_id

_AUTH_REGISTRY_KEY_LOCK = threading.Lock()
_AUTH_REGISTRY_KEY_CACHE: tuple[tuple[str, int, int, int, int, int], bytes] | None = None


def clear_auth_registry_key_cache() -> None:
    """Drop the cached local registry signing secret."""
    global _AUTH_REGISTRY_KEY_CACHE
    with _AUTH_REGISTRY_KEY_LOCK:
        _AUTH_REGISTRY_KEY_CACHE = None


def installation_key_path() -> Path:
    """Return the generated key path for the active profile.

    The legacy profile keeps the pre-profile ``secret.key`` location so an
    upgrade can still decrypt existing secrets. Additional profiles have a
    distinct key beside their database.
    """
    profile_id = current_profile_id()
    database = Path(settings.db)
    if profile_id and profile_id != "legacy":
        if not re.fullmatch(r"[0-9a-f]{32}", profile_id):
            raise ValueError("Invalid profile identifier")
        database = database.parent / "profiles" / f"{profile_id}.sqlite3"
        return database.with_name(f"{database.name}.secret.key")
    return database.with_name("secret.key")

def _generated_key(path: Path) -> bytes:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle: handle.write(Fernet.generate_key())
        except FileExistsError: pass
    return path.read_bytes().strip()


def installation_key():
    configured = os.getenv("AFTERWORD_SECRET_KEY", "").encode()
    if configured: return configured
    return _generated_key(installation_key_path())


def _key_signature(path: Path) -> tuple[str, int, int, int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (
        os.path.abspath(path),
        st.st_dev,
        st.st_ino,
        st.st_mtime_ns,
        st.st_ctime_ns,
        st.st_size,
    )


def auth_registry_key():
    """Use a stable installation key for digests stored in the shared registry."""
    global _AUTH_REGISTRY_KEY_CACHE
    configured = os.getenv("AFTERWORD_SECRET_KEY", "").encode()
    if configured:
        return configured
    path = Path(settings.db).with_name("secret.key")
    with _AUTH_REGISTRY_KEY_LOCK:
        signature = _key_signature(path)
        if (
            signature is not None
            and _AUTH_REGISTRY_KEY_CACHE is not None
            and _AUTH_REGISTRY_KEY_CACHE[0] == signature
        ):
            return _AUTH_REGISTRY_KEY_CACHE[1]
        key = _generated_key(path)
        signature = _key_signature(path)
        _AUTH_REGISTRY_KEY_CACHE = (signature, key) if signature is not None else None
        return key

def _fernet(): return Fernet(installation_key())

def seal(value: str): return "fernet:" + _fernet().encrypt(value.encode()).decode()

def unseal(value: str):
    if not value.startswith("fernet:"): return value
    try: return _fernet().decrypt(value.removeprefix("fernet:").encode()).decode()
    except InvalidToken as exc: raise RuntimeError("Stored secret cannot be decrypted with the configured key") from exc
