"""Conservative identities used to keep read books out of recommendations."""

import re
import unicodedata
from urllib.parse import urlparse

from .isbn import isbn_parts


_UNABRIDGED = re.compile(r"\s*\(\s*unabridged\s*\)\s*$", re.IGNORECASE)
_AUDIO_SERIES = re.compile(r"\s*:\s*[^:]+,\s*book\s+\d+\s*$", re.IGNORECASE)
_PARENTHETICAL_SERIES = re.compile(
    r"\s*\([^()]*?(?:#\s*\d+|\bbook\s+\d+|\bvol(?:ume)?\s+\d+)\s*\)\s*$",
    re.IGNORECASE,
)
_TITLE_SEPARATOR = re.compile(r"\s+(?:-|–|—)\s+|\s*:\s*")
_AUTHOR_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "vi"}
_OPEN_LIBRARY_WORK = re.compile(
    r"(?:^|/)works/(OL\d+W)(?:\.json)?(?:[/?#]|$)", re.IGNORECASE
)
_ARTICLE_LED_SUBTITLE = re.compile(r"^(?:a|an|the)\b", re.IGNORECASE)
_NUMBERED_SERIES_SUBTITLE = re.compile(
    r"^(?:a|an|the)\s+(?:one|two|three|four|five|first|second|third|fourth|fifth)\b",
    re.IGNORECASE,
)


def _row_value(item, key: str, default=""):
    """Read a field from either a dictionary or sqlite3.Row."""

    try:
        return item[key]
    except (IndexError, KeyError, TypeError):
        return default


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


def book_author_identity_key(value: object) -> str:
    """Return a normalized author key for bounded catalog lookups."""

    return _author_match_key(value)


def _title_match_keys(value: object) -> set[str]:
    """Return the full title and a safe subtitle-free comparison key.

    Catalogs frequently disagree about whether a subtitle is included. The
    prefix is accepted for subtitle-shaped, article-led extensions when the
    base has at least three words, or two words with one unusually distinctive
    long word. Number-led series titles such as ``The Lord of the Rings: The
    Two Towers`` are kept distinct, as are short series titles such as
    ``Dune`` and ``Dune: Messiah``.
    """

    title = _title_without_format_suffix(value)
    full = _canonical_part(title)
    keys = {full} if full else set()
    parts = _TITLE_SEPARATOR.split(title, maxsplit=1)
    if len(parts) == 2:
        prefix = _canonical_part(parts[0])
        subtitle = parts[1].strip()
        words = prefix.split()
        distinctive_two_word_title = (
            len(words) == 2 and any(len(word) >= 9 for word in words)
        )
        subtitle_shaped = bool(_ARTICLE_LED_SUBTITLE.match(subtitle)) and not bool(
            _NUMBERED_SERIES_SUBTITLE.match(subtitle)
        )
        if subtitle_shaped and (len(words) >= 3 or distinctive_two_word_title):
            keys.add(prefix)
    return keys


def _isbn_match_keys(item) -> set[tuple[str, str]]:
    """Return validated, edition-level identifiers for a catalog row."""

    keys: set[tuple[str, str]] = set()
    for field in (
        "isbn",
        "isbn10",
        "isbn13",
        "quality_isbn10",
        "quality_isbn13",
    ):
        value = _row_value(item, field)
        if not value:
            continue
        isbn13, _isbn10 = isbn_parts(value)
        if isbn13:
            # isbn_parts maps a valid ISBN-10 to its ISBN-13 equivalent, so
            # both representations share one key without relying on titles.
            keys.add(("isbn", isbn13))
    return keys


def _work_match_keys(item) -> set[tuple[str, str, str]]:
    """Return provider-scoped work IDs, ignoring opaque unscoped identifiers.

    Work IDs only match when both rows identify the same catalog provider. An
    Open Library work URL is self-identifying; other IDs need an explicit
    provider so unrelated providers cannot collide on short opaque IDs.
    """

    values = (
        _row_value(item, "quality_work_id"),
        _row_value(item, "work_id"),
        _row_value(item, "openlibrary_work_id"),
    )
    raw = next((str(value).strip() for value in values if value), "")
    source_url = str(_row_value(item, "source_url") or "").strip()
    if not raw and _OPEN_LIBRARY_WORK.search(source_url):
        raw = source_url
    if not raw:
        return set()

    match = _OPEN_LIBRARY_WORK.search(raw)
    provider = str(
        _row_value(item, "quality_provider")
        or _row_value(item, "work_id_provider")
        or _row_value(item, "provider")
        or ""
    ).strip().casefold().replace(" ", "_")
    if match:
        provider, identifier = "openlibrary", match.group(1).upper()
    else:
        identifier = raw
        if raw.startswith(("http://", "https://")):
            parsed = urlparse(raw)
            identifier = parsed.path.rstrip("/") or parsed.netloc
        identifier = identifier.strip().rstrip("/")
        if not provider or not identifier:
            return set()
    return {("work", provider, identifier)}


def book_row_identity_match_keys(item) -> set[tuple[str, ...]]:
    """Return title and stable catalog keys for a read or candidate row."""

    keys: set[tuple[str, ...]] = set(
        book_identity_match_keys(
            _row_value(item, "title"), _row_value(item, "author")
        )
    )
    keys.update(_isbn_match_keys(item))
    keys.update(_work_match_keys(item))
    return keys


def book_openlibrary_work_id(item) -> str:
    """Return a normalized Open Library work URL when the row has one."""

    for kind, provider, identifier in _work_match_keys(item):
        if kind == "work" and provider == "openlibrary":
            return f"/works/{identifier}"
    return ""


def book_identity_match_keys(title: object, author: object) -> set[tuple[str, str]]:
    """Return indexed keys used by the conservative same-work matcher."""

    author_key = _author_match_key(author)
    return {(author_key, title_key) for title_key in _title_match_keys(title)}


def book_identity_match_index(items) -> set[tuple[str, ...]]:
    """Build a lookup index for a collection of title/author rows."""

    index: set[tuple[str, ...]] = set()
    for item in items:
        index.update(book_row_identity_match_keys(item))
    return index


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
