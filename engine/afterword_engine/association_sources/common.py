"""Shared text normalization for external association providers."""

from __future__ import annotations

from ..identity import book_identity


def clean(value: object, limit: int = 500) -> str:
    """Collapse provider text whitespace and enforce a field length limit."""

    return " ".join(str(value or "").split()).strip()[:limit]


def title_key(value: object) -> str:
    """Return the normalized title component of a book identity."""

    return book_identity(value, "").split("\x1f", 1)[0]
