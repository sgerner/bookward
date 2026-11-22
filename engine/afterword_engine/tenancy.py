"""Request-local routing to a profile's private SQLite database."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import re

from .config import settings

_profile_id: ContextVar[str | None] = ContextVar("bookward_profile_id", default=None)
_database_override: ContextVar[Path | None] = ContextVar("bookward_database_override", default=None)
_restore_epoch_fence: ContextVar[tuple[str, int] | None] = ContextVar(
    "bookward_restore_epoch_fence", default=None
)
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


def bound_restore_epoch(path: str | Path | None = None) -> int | None:
    fence = _restore_epoch_fence.get()
    if fence is None:
        return None
    target = str(Path(path) if path is not None else profile_database_path())
    bound_path, epoch = fence
    return epoch if bound_path == target else None


@contextmanager
def restore_epoch_scope(epoch: int, path: str | Path | None = None):
    target = str(Path(path) if path is not None else profile_database_path())
    token = _restore_epoch_fence.set((target, max(0, int(epoch))))
    try:
        yield
    finally:
        _restore_epoch_fence.reset(token)


@contextmanager
def unbind_restore_epoch():
    token = _restore_epoch_fence.set(None)
    try:
        yield
    finally:
        _restore_epoch_fence.reset(token)
