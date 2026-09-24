"""Normalize subject labels before they reach storage, embeddings, or readers."""

from __future__ import annotations

import json
import re
import unicodedata


_CLASSIFICATION_PREFIX = re.compile(r"^\s*\d{2,3}\.\d{1,3}\s+")
_WHITESPACE = re.compile(r"\s+")
_HTML_TAG = re.compile(r"<[^>]*>")
_NOISE_SUBJECTS = {
    "new york times bestseller",
    "new york times best seller",
    "nyt bestseller",
    "nyt best seller",
    "open library staff picks",
    "staff picks",
}


def _subject_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(
        "".join(char if char.isalnum() else " " for char in normalized).split()
    )


def normalize_subjects(value: object, limit: int = 8) -> list[str]:
    """Return concise topical labels, dropping catalog codes and list metadata."""

    if limit <= 0:
        return []

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            values = [value]
        else:
            values = decoded if isinstance(decoded, (list, tuple)) else [decoded]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        return []

    subjects: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if isinstance(raw, dict):
            raw = raw.get("name") or raw.get("subject") or raw.get("value") or raw.get("text") or ""
        subject = unicodedata.normalize("NFKC", str(raw or ""))
        subject = _WHITESPACE.sub(" ", _HTML_TAG.sub(" ", subject)).strip(" \t\r\n,;|")
        subject = subject[:160].strip()
        if not subject or subject.casefold().startswith("nyt:"):
            continue
        subject = _CLASSIFICATION_PREFIX.sub("", subject).strip()
        if not subject or len(subject) > 80:
            continue
        key = _subject_key(subject)
        if not key or key in _NOISE_SUBJECTS or key in seen:
            continue
        if len(key) < 2 or all(token.isdigit() for token in key.split()):
            continue
        seen.add(key)
        subjects.append(subject)
        if len(subjects) >= max(0, limit):
            break
    return subjects
