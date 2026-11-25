"""Request-local routing to a profile's private SQLite database."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import re

from .config import settings


@dataclass
class _RestoreEpochBinding:
    path: str
    generation: int | None = None
    epoch: int | None = None


_profile_id: ContextVar[str | None] = ContextVar("bookward_profile_id", default=None)
_session_id: ContextVar[str | None] = ContextVar("bookward_session_id", default=None)
_database_override: ContextVar[Path | None] = ContextVar("bookward_database_override", default=None)
_restore_epoch_fence: ContextVar[_RestoreEpochBinding | None] = ContextVar(
    "bookward_restore_epoch_fence", default=None
)
_restore_fence_bypass: ContextVar[bool] = ContextVar("bookward_restore_fence_bypass", default=False)
_PROFILE_ID = re.compile(r"^[0-9a-f]{32}$")


def current_profile_id() -> str | None:
    return _profile_id.get()


def current_session_id() -> str | None:
    return _session_id.get()


@contextmanager
def profile_scope(profile_id: str):
    token = _profile_id.set(profile_id)
    try:
        yield
    finally:
        _profile_id.reset(token)


@contextmanager
def session_scope(session_id: str | None):
    token = _session_id.set(session_id or None)
    try:
        yield
    finally:
        _session_id.reset(token)


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


def active_restore_epoch_binding(
    path: str | Path | None = None,
) -> _RestoreEpochBinding | None:
    fence = _restore_epoch_fence.get()
    if fence is None:
        return None
    target = str(Path(path) if path is not None else profile_database_path())
    return fence if fence.path == target else None


def bound_restore_epoch(path: str | Path | None = None) -> int | None:
    binding = active_restore_epoch_binding(path)
    return binding.epoch if binding is not None else None


@contextmanager
def restore_epoch_scope(epoch: int, path: str | Path | None = None):
    target = str(Path(path) if path is not None else profile_database_path())
    binding = _RestoreEpochBinding(path=target, generation=None, epoch=max(0, int(epoch)))
    token = _restore_epoch_fence.set(binding)
    try:
        yield binding
    finally:
        _restore_epoch_fence.reset(token)


@contextmanager
def restore_epoch_latch_scope(
    *,
    generation: int,
    epoch: int | None = None,
    path: str | Path | None = None,
):
    target = str(Path(path) if path is not None else profile_database_path())
    binding = _RestoreEpochBinding(
        path=target,
        generation=int(generation),
        epoch=None if epoch is None else max(0, int(epoch)),
    )
    token = _restore_epoch_fence.set(binding)
    try:
        yield binding
    finally:
        _restore_epoch_fence.reset(token)


@contextmanager
def unbind_restore_epoch():
    token = _restore_epoch_fence.set(None)
    try:
        yield
    finally:
        _restore_epoch_fence.reset(token)


def restore_fence_bypassed() -> bool:
    return _database_override.get() is not None or _restore_fence_bypass.get()


@contextmanager
def allow_restore_database_access():
    token = _restore_fence_bypass.set(True)
    try:
        yield
    finally:
        _restore_fence_bypass.reset(token)
