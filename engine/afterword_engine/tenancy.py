"""Request-local routing to a profile's private SQLite database."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import re

from .config import settings

_profile_id: ContextVar[str | None] = ContextVar("bookward_profile_id", default=None)
_database_override: ContextVar[Path | None] = ContextVar("bookward_database_override", default=None)
_PROFILE_ID = re.compile(r"^[0-9a-f]{32}$")


def current_profile_id() -> str | None:
    return _profile_id.get()


@contextmanager
def profile_scope(profile_id: str):
    token = _profile_id.set(profile_id)
    try:
        yield
    finally:
        _profile_id.reset(token)


def profile_database_path(profile_id: str | None = None) -> Path:
    override = _database_override.get()
    if override is not None:
        return override
    selected = profile_id if profile_id is not None else current_profile_id()
    if selected is None or selected == "legacy":
        return Path(settings.db)
    if not _PROFILE_ID.fullmatch(selected):
        raise ValueError("Invalid profile identifier")
    root = Path(settings.db).parent / "profiles"
    return root / f"{selected}.sqlite3"


@contextmanager
def database_path_override(path: str | Path):
    token = _database_override.set(Path(path))
    try:
        yield
    finally:
        _database_override.reset(token)
