import os
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
from .config import settings

def _fernet():
    configured = os.getenv("AFTERWORD_SECRET_KEY", "").encode()
    if configured: return Fernet(configured)
    path = Path(settings.db).with_name("secret.key")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle: handle.write(Fernet.generate_key())
        except FileExistsError: pass
    return Fernet(path.read_bytes().strip())

def seal(value: str): return "fernet:" + _fernet().encrypt(value.encode()).decode()

def unseal(value: str):
    if not value.startswith("fernet:"): return value
    try: return _fernet().decrypt(value.removeprefix("fernet:").encode()).decode()
    except InvalidToken as exc: raise RuntimeError("Stored secret cannot be decrypted with the configured key") from exc
