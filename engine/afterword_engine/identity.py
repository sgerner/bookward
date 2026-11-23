"""Conservative identities used to keep read books out of recommendations."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlparse

from .isbn import isbn_parts


_UNABRIDGED = re.compile(r"\s*\(\s*unabridged\s*\)\s*$", re.IGNORECASE)
_SERIES_WORD_NUMBER = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty(?:\s*-\s*(?:one|two|three|four|five|six|seven|eight|nine))?|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"i{1,3}|iv|vi{0,3}|ix|x)"
)
_SERIES_NUMBER = rf"(?:\d+|{_SERIES_WORD_NUMBER})"
_AUDIO_SERIES = re.compile(
    rf"(?:\s*:\s*|\s+(?:-|–|—)\s+)[^:]+,\s*(?:book|vol(?:ume)?\.?)\s+#?\s*{_SERIES_NUMBER}\s*$",
    re.IGNORECASE,
)
_COLON_BOOK_OF_SERIES = re.compile(
    rf"^(.*?)(?:\s*:\s*|\s+(?:-|–|—)\s+)(?:book|vol(?:ume)?\.?)\s+({_SERIES_NUMBER})\s+of\s+([^:()]+?)\s*$",
    re.IGNORECASE,
)
_PARENTHETICAL_SERIES = re.compile(
    rf"\s*\([^()]*?(?:#\s*\d+|\bbook\s+#?\s*{_SERIES_NUMBER}|\bvol(?:ume)?\.?\s+#?\s*{_SERIES_NUMBER})\s*\)\s*$",
    re.IGNORECASE,
)
_TRAILING_PAREN = re.compile(r"^(.*?)\s*\(([^()]+)\)\s*$")
_TITLE_SEPARATOR = re.compile(r"\s+(?:-|–|—)\s+|\s*:\s*")
_RIGHT_TITLE_SEPARATOR = re.compile(r"^(.*)(?:\s+(?:-|–|—)\s+|\s*:\s*)(.+?)\s*$")
_AUTHOR_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "vi", "vii", "viii"}
_AUTHOR_ROLE_NOTE = re.compile(r"\s*\((?:goodreads\s+)?author\)", re.IGNORECASE)
_AUTHOR_SEPARATOR = re.compile(
    r"\s*(?:,|;|&|/|\band\b|\bwith\b)\s*",
    re.IGNORECASE,
)
_NON_AUTHOR_KEYS = {
    "",
    "unknown",
    "unknown author",
    "various",
    "various authors",
    "anonymous",
    "n a",
    "na",
    "none",
    "null",
    "et al",
}
_SERIES_LABEL_NOUNS = {
    "series",
    "saga",
    "trilogy",
    "duology",
    "quartet",
    "quintet",
    "cycle",
    "chronicles",
    "chronicle",
    "sequence",
}
_OPEN_LIBRARY_WORK = re.compile(
    r"(?:^|/)works/(OL\d+W)(?:\.json)?(?:[/?#]|$)", re.IGNORECASE
)
_ARTICLE_LED_SUBTITLE = re.compile(r"^(?:a|an|the)\b", re.IGNORECASE)
_NUMBERED_SERIES_SUBTITLE = re.compile(
    r"^(?:(?:a|an|the)\s+(?:one|two|three|four|five|six|seven|eight|nine|ten|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\b"
    r"|(?:book|part|vol(?:ume)?\.?|act|episode|chapter)\s+"
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"i{1,3}|iv|vi{0,3}|ix|x)\b)",
    re.IGNORECASE,
)
_PLURAL_COLLECTION_SUBTITLE = re.compile(
    r"^(?:\d+|two|three|four|five|six|seven|eight|nine|ten)\s+",
    re.IGNORECASE,
)
_EXPLICIT_GENRE_SUBTITLE = re.compile(
    r"^(?:"
    r"(?!the\b)(?:(?:a|an)\s+)?(?:[a-z0-9]+\s+){0,3}"
    r"(?:novel|novella|thriller|suspense|mystery|romance|whodunit|memoir|memoirs|biography|autobiography|fable)"
    r"(?:\s+(?:of|in|from|about|for)\b.*)?"
    r"|(?:a|an)\s+(?:[a-z0-9]+\s+){0,3}history(?:\s+(?:of|in|from)\b.*)?"
    r"|(?:(?:a|an)\s+(?:collection\s+of\s+)?|(?:short|selected|new\s+and\s+selected|personal|collected)\s+)?"
    r"(?:stories|essays|poems|poetry)(?:\s+(?:of|in|from|on|about)\b.*)?"
    r"|(?:a|an)\s+(?:[a-z0-9]+\s+){0,2}guide\s+to\b.*"
    r")$",
    re.IGNORECASE,
)
_EXPLICIT_EDITION_SUBTITLE = re.compile(
    r"^(?:"
    r"(?:[a-z0-9]+\s+){0,6}edition"
    r"|unabridged|abridged|audiobook|audio|large\s+print"
    r"|(?:trade\s+|mass\s+market\s+)?paperback|hardcover|ebook|kindle"
    r"|(?:movie|tv|television|film|media|netflix)\s+tie\s+in"
    r"|tie\s+in"
    r"|illustrated|annotated|revised|expanded|revised\s+and\s+expanded"
    r"|(?:the\s+)?complete\s+and\s+uncut|author\s+s\s+preferred\s+text"
    r"|\d+(?:st|nd|rd|th)\s+anniversary"
    r")$",
    re.IGNORECASE,
)
_TITLE_ALIAS_GROUPS = (
    ("1984", "nineteen eighty four"),
    ("2001", "two thousand one", "two thousand and one"),
    ("catch 22", "catch twenty two"),
    ("fahrenheit 451", "fahrenheit four hundred fifty one"),
    ("slaughterhouse 5", "slaughterhouse five"),
)
_TITLE_ALIASES: dict[str, tuple[str, ...]] = {
    alias: group
    for group in _TITLE_ALIAS_GROUPS
    for alias in group
}


def _row_value(item, key: str, default=""):
    """Read a field from either a dictionary or sqlite3.Row."""

    try:
        return item[key]
    except (IndexError, KeyError, TypeError):
        return default


def normalize_key(title: str, author: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", f"{title} {author}".lower()).strip()


def _without_leading_article(text: str) -> str:
    words = text.split()
    if len(words) >= 2 and words[0] in {"a", "an", "the"}:
        return " ".join(words[1:])
    return text


def _strip_repeated_series_parenthetical(value: str) -> str:
    match = _TRAILING_PAREN.match(value)
    if not match:
        return value
    base_raw = match.group(1).strip()
    paren_raw = match.group(2).strip()
    base_norm = canonical_part(base_raw)
    paren_norm = canonical_part(paren_raw)
    if not base_norm or not paren_norm:
        return value
    base_core = _without_leading_article(base_norm)
    paren_core = _without_leading_article(paren_norm)
    if paren_norm == base_norm or paren_core == base_core:
        return base_raw
    paren_words = paren_core.split()
    if (
        len(paren_words) >= 2
        and paren_words[-1] in _SERIES_LABEL_NOUNS
        and " ".join(paren_words[:-1]) == base_core
    ):
        return base_raw
    return value


def _strip_colon_book_of_series(value: str) -> str:
    match = _COLON_BOOK_OF_SERIES.match(value)
    if not match:
        return value
    base_raw = match.group(1).strip()
    number_raw = canonical_part(match.group(2))
    series_raw = match.group(3).strip()
    base_core = _without_leading_article(canonical_part(base_raw))
    series_core = _without_leading_article(canonical_part(series_raw))
    if not base_core or not series_core:
        return value
    if base_core == series_core and number_raw not in {"1", "one", "first", "i"}:
        return value
    return base_raw


def _title_without_format_suffix(title: object) -> str:
    value = unicodedata.normalize("NFKC", str(title or "")).strip()
    while value:
        updated = _UNABRIDGED.sub("", value).strip()
        updated = _PARENTHETICAL_SERIES.sub("", updated).strip()
        updated = _strip_repeated_series_parenthetical(updated).strip()
        updated = _AUDIO_SERIES.sub("", updated).strip()
        updated = _strip_colon_book_of_series(updated).strip()
        if updated == value:
            break
        value = updated
    return value


def canonical_part(value: object) -> str:
    """Canonicalize text into stable lowercase alphanumeric word tokens."""

    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # Treat punctuation as spacing. This makes initials such as "N. K." and
    # "N K" equivalent without merging adjacent substantive words.
    chars = [char if (char.isalnum() or char.isspace()) else " " for char in value]
    return " ".join("".join(chars).split())


def book_catalog_title_identity_key(value: object) -> str:
    """Return a full catalog title key after removing only known format suffixes.

    The suffix rules cover parenthetical series numbering and common audiobook
    labels. The function deliberately keeps all substantive title and subtitle
    words, so ``Dune`` and ``Dune: Messiah`` remain distinct.
    """

    return canonical_part(_title_without_format_suffix(value))


def _canonical_author(value: object) -> str:
    tokens = canonical_part(value).split()
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
    while len(tokens) >= 2 and tokens[-2:] == ["et", "al"]:
        tokens = tokens[:-2]
    while tokens and tokens[-1] in _AUTHOR_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def book_author_identity_key(value: object) -> str:
    """Return a normalized author key for bounded catalog lookups."""

    return _author_match_key(value)


def book_author_identity_keys(*values: object) -> set[str]:
    """Return normalized contributor keys for primary and secondary author fields."""

    keys: set[str] = set()
    for raw_value in values:
        text = unicodedata.normalize("NFKC", str(raw_value or "")).strip()
        if not text:
            continue
        text = _AUTHOR_ROLE_NOTE.sub("", text).strip()
        if not text:
            continue
        segments = [part.strip() for part in _AUTHOR_SEPARATOR.split(text) if part.strip()]
        normalized_segments = [
            key
            for segment in segments
            if (key := _author_match_key(segment)) and key not in _NON_AUTHOR_KEYS
        ]
        if not normalized_segments:
            continue
        if len(normalized_segments) == 1:
            keys.add(normalized_segments[0])
            continue
        full_contributors = [
            key for key in normalized_segments if len(key.split()) >= 2
        ]
        if full_contributors:
            keys.update(full_contributors)
        else:
            fallback = _author_match_key(text)
            if fallback and fallback not in _NON_AUTHOR_KEYS:
                keys.add(fallback)
    return keys


def book_row_author_identity_keys(item) -> set[str]:
    """Return normalized contributor keys for a read, shelf, or candidate row."""

    return book_author_identity_keys(
        _row_value(item, "author"),
        _row_value(item, "additional_authors"),
    )


def _is_explicit_genre_or_edition_subtitle(subtitle: str) -> bool:
    raw = subtitle.strip()
    if not raw or _TITLE_SEPARATOR.search(raw):
        return False
    if _NUMBERED_SERIES_SUBTITLE.match(raw):
        return False
    sub = canonical_part(raw)
    if not sub:
        return False
    if _EXPLICIT_EDITION_SUBTITLE.match(sub):
        return True
    if _PLURAL_COLLECTION_SUBTITLE.match(sub):
        return False
    return bool(_EXPLICIT_GENRE_SUBTITLE.match(sub))


def _title_match_keys(value: object) -> set[str]:
    """Return the full title and a safe subtitle-free comparison key.

    Catalogs frequently disagree about whether a subtitle is included. The
    prefix is accepted for subtitle-shaped, article-led extensions when the
    base has at least three words, or two words with one unusually distinctive
    long word, and for explicit genre or edition subtitles on shorter titles.
    Number-led series titles such as ``The Lord of the Rings: The Two Towers``
    are kept distinct, as are short series titles such as ``Dune`` and ``Dune:
    Messiah``.
    """

    title = _title_without_format_suffix(value)
    if not title:
        return set()
    keys: set[str] = set()
    variants = [title]

    current = title
    while current:
        paren_match = _TRAILING_PAREN.match(current)
        if paren_match and _is_explicit_genre_or_edition_subtitle(paren_match.group(2)):
            stripped = _title_without_format_suffix(paren_match.group(1))
            if stripped and stripped != current:
                variants.append(stripped)
                current = stripped
                continue
        right_match = _RIGHT_TITLE_SEPARATOR.match(current)
        if right_match and _is_explicit_genre_or_edition_subtitle(right_match.group(2)):
            stripped = _title_without_format_suffix(right_match.group(1))
            if stripped and stripped != current:
                variants.append(stripped)
                current = stripped
                continue
        break

    for variant in variants:
        normalized = canonical_part(variant)
        if normalized:
            keys.add(normalized)
        parts = _TITLE_SEPARATOR.split(variant, maxsplit=1)
        if len(parts) == 2:
            prefix = canonical_part(_title_without_format_suffix(parts[0]))
            subtitle = parts[1].strip()
            words = prefix.split()
            distinctive_two_word_title = (
                len(words) == 2 and any(len(word) >= 9 for word in words)
            )
            subtitle_shaped = (
                bool(_ARTICLE_LED_SUBTITLE.match(subtitle))
                and not bool(_NUMBERED_SERIES_SUBTITLE.match(subtitle))
            )
            if prefix and (
                (subtitle_shaped and (len(words) >= 3 or distinctive_two_word_title))
                or _is_explicit_genre_or_edition_subtitle(subtitle)
            ):
                keys.add(prefix)

    for key in list(keys):
        keys.update(_TITLE_ALIASES.get(key, ()))
        words = key.split()
        if len(words) >= 4 and words[0] in {"a", "an", "the"}:
            keys.add(" ".join(words[1:]))
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
            _row_value(item, "title"),
            _row_value(item, "author"),
            _row_value(item, "additional_authors"),
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


def read_metadata_identity_hash(read) -> str:
    """Fingerprint the read's bibliographic identity and verified work key."""

    isbn = _row_value(read, "isbn")
    isbn13, isbn10 = isbn_parts(isbn)
    identity = {
        "title": normalize_key(str(_row_value(read, "title") or ""), ""),
        "author": normalize_key("", str(_row_value(read, "author") or "")),
        "source_isbn": re.sub(r"[^0-9xX]", "", str(isbn or "")).upper(),
        "isbn13": isbn13,
        "isbn10": isbn10,
        "openlibrary_work_id": book_openlibrary_work_id(read),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _is_unknown_author_row(item) -> bool:
    if book_row_author_identity_keys(item):
        return False
    raw_primary = canonical_part(_row_value(item, "author"))
    raw_secondary = canonical_part(_row_value(item, "additional_authors"))
    return raw_primary in {"", "unknown author"} and not raw_secondary


def book_identity_match_keys(
    title: object,
    author: object,
    additional_authors: object = "",
) -> set[tuple[str, str]]:
    """Return indexed keys used by the conservative same-work matcher."""

    title_keys = _title_match_keys(title)
    if not title_keys:
        return set()
    author_keys = book_author_identity_keys(author, additional_authors)
    if author_keys:
        return {
            (author_key, title_key)
            for author_key in author_keys
            for title_key in title_keys
        }
    raw_primary = canonical_part(author)
    raw_secondary = canonical_part(additional_authors)
    if raw_primary in {"", "unknown author"} and not raw_secondary:
        return {(raw_primary, title_key) for title_key in title_keys}
    return set()


def book_identity_match_index(items) -> set[tuple[str, ...]]:
    """Build a lookup index for a collection of title/author rows."""

    index: set[tuple[str, ...]] = set()
    for item in items:
        index.update(book_row_identity_match_keys(item))
    return index


@dataclass(frozen=True, slots=True)
class ReadIdentityIndex:
    """Indexed read identities for filtering provider-discovered candidates.

    Known authors use the same title/author aliases, provider-scoped work IDs,
    and validated ISBN keys as serving eligibility. A title-only fallback is
    retained when either side has no author or the exact ``Unknown author``
    placeholder, where the available history or provider identity cannot
    support a safer comparison.
    """

    match_keys: frozenset[tuple[str, ...]]
    title_keys: frozenset[str]
    unknown_author_title_keys: frozenset[str]

    @classmethod
    def from_reads(cls, reads) -> "ReadIdentityIndex":
        rows = list(reads)
        return cls(
            match_keys=frozenset(book_identity_match_index(rows)),
            title_keys=frozenset(
                key
                for read in rows
                for key in _title_match_keys(_row_value(read, "title"))
            ),
            unknown_author_title_keys=frozenset(
                key
                for read in rows
                if _is_unknown_author_row(read)
                for key in _title_match_keys(_row_value(read, "title"))
            ),
        )

    def matches(self, candidate) -> bool:
        """Return whether a provider result identifies a known read."""

        if book_row_identity_match_keys(candidate) & self.match_keys:
            return True
        if _is_unknown_author_row(candidate):
            return bool(
                _title_match_keys(_row_value(candidate, "title")) & self.title_keys
            )
        if book_row_author_identity_keys(candidate):
            return bool(
                _title_match_keys(_row_value(candidate, "title"))
                & self.unknown_author_title_keys
            )
        return False


def book_identity(title: object, author: object) -> str:
    """Return a conservative, deterministic title/author identity."""

    return f"{book_catalog_title_identity_key(title)}\x1f{_canonical_author(author)}"


def book_identity_matches(
    left_title: object,
    left_author: object,
    right_title: object,
    right_author: object,
) -> bool:
    """Return whether two catalog rows likely describe the same work.

    Exact identities remain the primary path. The fallback handles the
    common Goodreads/catalog variants that otherwise leak read books back into
    discovery: generational author suffixes, shared contributors, and an
    omitted subtitle.
    """

    if book_identity(left_title, left_author) == book_identity(right_title, right_author):
        return True
    left_authors = book_author_identity_keys(left_author)
    right_authors = book_author_identity_keys(right_author)
    if not left_authors or not right_authors or not (left_authors & right_authors):
        return False
    left_titles = _title_match_keys(left_title)
    right_titles = _title_match_keys(right_title)
    return bool(left_titles & right_titles)
