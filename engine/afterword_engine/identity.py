"""Conservative identities used to keep read books out of recommendations."""

import re
import unicodedata


_UNABRIDGED = re.compile(r"\s*\(\s*unabridged\s*\)\s*$", re.IGNORECASE)
_AUDIO_SERIES = re.compile(r"\s*:\s*[^:]+,\s*book\s+\d+\s*$", re.IGNORECASE)
_PARENTHETICAL_SERIES = re.compile(
    r"\s*\([^()]*?(?:#\s*\d+|\bbook\s+\d+|\bvol(?:ume)?\s+\d+)\s*\)\s*$",
    re.IGNORECASE,
)


def _title_without_format_suffix(title: object) -> str:
    value = unicodedata.normalize("NFKC", str(title or "")).strip()
    value = _UNABRIDGED.sub("", value).strip()
    value = _PARENTHETICAL_SERIES.sub("", value).strip()
    value = _AUDIO_SERIES.sub("", value).strip()
    return value


def _canonical_part(value: object) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # Treat punctuation as spacing. This makes initials such as "N. K." and
    # "N K" equivalent without merging adjacent substantive words.
    chars = [char if (char.isalnum() or char.isspace()) else " " for char in value]
    return " ".join("".join(chars).split())


def _canonical_author(value: object) -> str:
    tokens = _canonical_part(value).split()
    # Initials are equivalent with or without separators: R.F. Kuang and
    # R. F. Kuang both become "rf kuang".
    initials = []
    while tokens and len(tokens[0]) == 1:
        initials.append(tokens.pop(0))
    if initials:
        tokens.insert(0, "".join(initials))
    return " ".join(tokens)


def book_identity(title: object, author: object) -> str:
    """Return a conservative, deterministic title/author identity."""

    return f"{_canonical_part(_title_without_format_suffix(title))}\x1f{_canonical_author(author)}"
