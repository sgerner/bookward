"""Conservative identities used to keep read books out of recommendations."""

import re
import unicodedata


_UNABRIDGED = re.compile(r"\s*\(\s*unabridged\s*\)\s*$", re.IGNORECASE)
_AUDIO_SERIES = re.compile(r"\s*:\s*[^:]+,\s*book\s+\d+\s*$", re.IGNORECASE)
_PARENTHETICAL_SERIES = re.compile(
    r"\s*\([^()]*?(?:#\s*\d+|\bbook\s+\d+|\bvol(?:ume)?\s+\d+)\s*\)\s*$",
    re.IGNORECASE,
)
_TITLE_SEPARATOR = re.compile(r"\s+-\s+|\s*:\s*")
_AUTHOR_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "vi"}


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


def _author_match_key(value: object) -> str:
    """Normalize harmless generational suffix differences in author names."""

    tokens = _canonical_author(value).split()
    while len(tokens) > 1 and tokens[-1] in _AUTHOR_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _title_match_keys(value: object) -> set[str]:
    """Return the full title and a safe subtitle-free comparison key.

    Catalogs frequently disagree about whether a subtitle is included. The
    prefix is only accepted for titles with at least three words, which avoids
    collapsing short series titles such as ``Dune`` and ``Dune: Messiah``.
    """

    title = _title_without_format_suffix(value)
    full = _canonical_part(title)
    keys = {full} if full else set()
    parts = _TITLE_SEPARATOR.split(title, maxsplit=1)
    if len(parts) == 2:
        prefix = _canonical_part(parts[0])
        if len(prefix.split()) >= 3:
            keys.add(prefix)
    return keys


def book_identity(title: object, author: object) -> str:
    """Return a conservative, deterministic title/author identity."""

    return f"{_canonical_part(_title_without_format_suffix(title))}\x1f{_canonical_author(author)}"


def book_identity_matches(
    left_title: object,
    left_author: object,
    right_title: object,
    right_author: object,
) -> bool:
    """Return whether two catalog rows likely describe the same work.

    Exact identities remain the primary path. The fallback handles the two
    common Goodreads/catalog variants that otherwise leak read books back into
    discovery: generational author suffixes and an omitted subtitle.
    """

    if book_identity(left_title, left_author) == book_identity(right_title, right_author):
        return True
    if _author_match_key(left_author) != _author_match_key(right_author):
        return False
    left_titles = _title_match_keys(left_title)
    right_titles = _title_match_keys(right_title)
    return bool(left_titles & right_titles)
