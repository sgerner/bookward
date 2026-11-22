"""Conservative identities used to keep read books out of recommendations."""

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import urlparse

from .isbn import isbn_parts


_UNABRIDGED = re.compile(r"\s*\(\s*unabridged\s*\)\s*$", re.IGNORECASE)
_AUDIO_SERIES = re.compile(r"\s*:\s*[^:]+,\s*book\s+\d+\s*$", re.IGNORECASE)
_PARENTHETICAL_SERIES = re.compile(
    r"\s*\([^()]*?(?:#\s*\d+|\bbook\s+\d+|\bvol(?:ume)?\s+\d+)\s*\)\s*$",
    re.IGNORECASE,
)
_TRAILING_PAREN = re.compile(r"\s*\([^()]{2,80}\)\s*$")
_TITLE_SEPARATOR = re.compile(r"\s+(?:-|–|—)\s+|\s*:\s*")
_AUTHOR_SPLIT = re.compile(r"\s*(?:,|;|&|\band\b)\s*", re.IGNORECASE)
_AUTHOR_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "vi"}

_DERIVATIVE_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "summary",
        re.compile(
            r"\b(?:summar(?:y|ies)|cliff\s*notes|cliffs\s*notes|spark\s*notes|"
            r"instaread|blinkist|supersummary|bookcaps|quickread|key\s+takeaways)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "study_guide",
        re.compile(
            r"\b(?:study\s+guides?|study\s+aids?|student\s+guides?|teacher'?s?\s+guides?|"
            r"reading\s+guides?|reader'?s?\s+guides?|discussion\s+guides?|book\s+club\s+guides?|"
            r"literature\s+guides?|book\s+notes|study\s+notes)\b"
            r"|\b(?:summary|review)\s+(?:and|&)\s+analysis\b"
            r"|\b(?:detailed|critical|comprehensive|character)\s+analysis\b"
            r"|^\s*(?:a\s+)?(?:guide|analysis|review|notes|overview)\s+(?:of|to|for|on)\b"
            r"|\([^()]*\b(?:analysis|study|discussion\s+questions)\b[^()]*\)",
            re.IGNORECASE,
        ),
    ),
    (
        "workbook",
        re.compile(r"\b(?:workbooks?|exercise\s+books?)\b", re.IGNORECASE),
    ),
    (
        "companion",
        re.compile(
            r"\b(?:companions?|unofficial\s+guides?|unauthorized\s+(?:guide|companion|biography)|"
            r"trivia\s+(?:and|&|book|guide|questions))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "box_set",
        re.compile(
            r"\b(?:box\s*sets?|boxed\s+sets?|omnibus|complete\s+collection|"
            r"[2-9]\s*-?\s*in\s*-?\s*1|(?:two|three|four)\s+in\s+one)\b"
            r"|\b\d+\s*-?\s*book\s+(?:set|bundle|collection|pack)\b"
            r"|\b(?:books|volumes|vols\.?)\s+\d+\s*(?:-|–|—|to|through)\s*\d+\b"
            r"|#\s*\d+\s*(?:-|–|—)\s*\d+\b",
            re.IGNORECASE,
        ),
    ),
)

_FOREIGN_TITLE_LANGUAGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "es",
        re.compile(
            r"\b(?:spanish\s+(?:edition|language|version)|edici[oó]n\s+en\s+espa[nñ]ol|"
            r"versi[oó]n\s+en\s+espa[nñ]ol|en\s+espa[nñ]ol|in\s+spanish)\b"
            r"|[\(\[\-–—:]\s*(?:spanish|espa[nñ]ol)\s*[\)\]]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "fr",
        re.compile(
            r"\b(?:french\s+(?:edition|language|version)|[eé]dition\s+fran[cç]aise|"
            r"version\s+fran[cç]aise|en\s+fran[cç]ais|in\s+french)\b"
            r"|[\(\[\-–—:]\s*(?:french|fran[cç]ais)\s*[\)\]]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "de",
        re.compile(
            r"\b(?:german\s+(?:edition|language|version)|deutsche\s+ausgabe|"
            r"auf\s+deutsch|in\s+german)\b"
            r"|[\(\[\-–—:]\s*(?:german|deutsch)\s*[\)\]]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "it",
        re.compile(
            r"\b(?:italian\s+(?:edition|language|version)|edizione\s+italiana|in\s+italian)\b"
            r"|[\(\[\-–—:]\s*(?:italian|italiano)\s*[\)\]]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "pt",
        re.compile(
            r"\b(?:portuguese\s+(?:edition|language|version)|edi[cç][aã]o\s+em\s+portugu[eê]s|"
            r"in\s+portuguese)\b"
            r"|[\(\[\-–—:]\s*(?:portuguese|portugu[eê]s)\s*[\)\]]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "ru",
        re.compile(
            r"\b(?:russian\s+(?:edition|language|version)|in\s+russian)\b"
            r"|[\(\[\-–—:]\s*russian\s*[\)\]]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "ja",
        re.compile(
            r"\b(?:japanese\s+(?:edition|language|version)|in\s+japanese)\b"
            r"|[\(\[\-–—:]\s*japanese\s*[\)\]]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "zh",
        re.compile(
            r"\b(?:chinese\s+(?:edition|language|version)|in\s+chinese)\b"
            r"|[\(\[\-–—:]\s*chinese\s*[\)\]]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "ko",
        re.compile(
            r"\b(?:korean\s+(?:edition|language|version)|in\s+korean)\b"
            r"|[\(\[\-–—:]\s*korean\s*[\)\]]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "nl",
        re.compile(
            r"\b(?:dutch\s+(?:edition|language|version)|nederlandse\s+editie|in\s+dutch)\b"
            r"|[\(\[\-–—:]\s*(?:dutch|nederlands)\s*[\)\]]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "pl",
        re.compile(
            r"\b(?:polish\s+(?:edition|language|version)|wydanie\s+polskie|in\s+polish)\b"
            r"|[\(\[\-–—:]\s*(?:polish|polski)\s*[\)\]]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "sv",
        re.compile(
            r"\b(?:swedish\s+(?:edition|language|version)|svensk\s+utg[aå]va|in\s+swedish)\b"
            r"|[\(\[\-–—:]\s*(?:swedish|svenska)\s*[\)\]]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "tr",
        re.compile(
            r"\b(?:turkish\s+(?:edition|language|version)|t[uü]rk[cç]e\s+bask[iı]|in\s+turkish)\b"
            r"|[\(\[\-–—:]\s*(?:turkish|t[uü]rk[cç]e)\s*[\)\]]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "ar",
        re.compile(
            r"\b(?:arabic\s+(?:edition|language|version)|in\s+arabic)\b"
            r"|[\(\[\-–—:]\s*arabic\s*[\)\]]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "he",
        re.compile(
            r"\b(?:hebrew\s+(?:edition|language|version)|in\s+hebrew)\b"
            r"|[\(\[\-–—:]\s*hebrew\s*[\)\]]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "foreign",
        re.compile(r"\b(?:foreign|translated)\s+(?:language\s+)?edition\b", re.IGNORECASE),
    ),
)

_LANGUAGE_CODE_ALIASES: dict[str, str] = {
    "en": "en",
    "eng": "en",
    "english": "en",
    "es": "es",
    "spa": "es",
    "spanish": "es",
    "fr": "fr",
    "fre": "fr",
    "fra": "fr",
    "french": "fr",
    "de": "de",
    "ger": "de",
    "deu": "de",
    "german": "de",
    "it": "it",
    "ita": "it",
    "italian": "it",
    "pt": "pt",
    "por": "pt",
    "portuguese": "pt",
    "ru": "ru",
    "rus": "ru",
    "russian": "ru",
    "ja": "ja",
    "jpn": "ja",
    "japanese": "ja",
    "zh": "zh",
    "chi": "zh",
    "zho": "zh",
    "chinese": "zh",
    "ko": "ko",
    "kor": "ko",
    "korean": "ko",
    "nl": "nl",
    "dut": "nl",
    "nld": "nl",
    "dutch": "nl",
    "pl": "pl",
    "pol": "pl",
    "polish": "pl",
    "sv": "sv",
    "swe": "sv",
    "swedish": "sv",
    "tr": "tr",
    "tur": "tr",
    "turkish": "tr",
    "ar": "ar",
    "ara": "ar",
    "arabic": "ar",
    "he": "he",
    "heb": "he",
    "hebrew": "he",
    "da": "da",
    "dan": "da",
    "no": "no",
    "nor": "no",
    "nob": "no",
    "nno": "no",
    "fi": "fi",
    "fin": "fi",
    "cs": "cs",
    "cze": "cs",
    "ces": "cs",
    "el": "el",
    "gre": "el",
    "ell": "el",
    "hu": "hu",
    "hun": "hu",
    "ro": "ro",
    "rum": "ro",
    "ron": "ro",
    "uk": "uk",
    "ukr": "uk",
    "ca": "ca",
    "cat": "ca",
    "id": "id",
    "ind": "id",
    "vi": "vi",
    "vie": "vi",
    "th": "th",
    "tha": "th",
    "hi": "hi",
    "hin": "hi",
}
_IGNORED_LANGUAGE_CODES = {"", "und", "mul", "zxx", "mis", "unknown", "none", "null"}
_DERIVATIVE_SUBJECT_PATTERN = re.compile(
    r"^(?:study\s+aids?(?:\s*/.*)?|book\s+notes|cliff\s*notes|cliffs\s*notes|spark\s*notes)$",
    re.IGNORECASE,
)
_ONLY_DERIVATIVE_SUBJECT_PATTERN = re.compile(
    r"^(?:study\s+guides?|workbooks?|summaries)$",
    re.IGNORECASE,
)
_OPEN_LIBRARY_WORK = re.compile(
    r"(?:^|/)works/(OL\d+W)(?:\.json)?(?:[/?#]|$)", re.IGNORECASE
)
_ARTICLE_LED_SUBTITLE = re.compile(r"^(?:a|an|the)\b", re.IGNORECASE)
_NUMBERED_SERIES_SUBTITLE = re.compile(
    r"^(?:a|an|the)\s+(?:one|two|three|four|five|first|second|third|fourth|fifth)\b",
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


def _title_without_format_suffix(title: object) -> str:
    value = unicodedata.normalize("NFKC", str(title or "")).strip()
    value = _UNABRIDGED.sub("", value).strip()
    value = _PARENTHETICAL_SERIES.sub("", value).strip()
    value = _AUDIO_SERIES.sub("", value).strip()
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
    while len(tokens) > 1 and tokens[-1] in _AUTHOR_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def book_author_identity_key(value: object) -> str:
    """Return a normalized author key for bounded catalog lookups."""

    return _author_match_key(value)


def _author_tokens(value: object) -> list[str]:
    tokens = canonical_part(value).split()
    while len(tokens) > 1 and tokens[-1] in _AUTHOR_SUFFIXES:
        tokens.pop()
    return tokens


def _split_author_values(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(item) for item in value if item]
    else:
        raw_items = [str(value or "")]
    parts: list[str] = []
    for raw in raw_items:
        for piece in _AUTHOR_SPLIT.split(raw):
            cleaned = piece.strip()
            if cleaned:
                parts.append(cleaned)
    return parts


def _single_author_matches(left: object, right: object) -> bool:
    key_a = book_author_identity_key(left)
    key_b = book_author_identity_key(right)
    if not key_a or not key_b:
        return False
    norm_a = canonical_part(left)
    norm_b = canonical_part(right)
    if key_a == key_b or (norm_a and norm_a == norm_b):
        return True
    parts_a = _author_tokens(left)
    parts_b = _author_tokens(right)
    return bool(
        len(parts_a) >= 2
        and len(parts_b) >= 2
        and parts_a[-1] == parts_b[-1]
        and parts_a[0][0] == parts_b[0][0]
        and (
            parts_a[0] == parts_b[0]
            or len(parts_a[0]) == 1
            or len(parts_b[0]) == 1
        )
    )


def _single_author_similarity(left: object, right: object) -> float:
    key_a = book_author_identity_key(left)
    key_b = book_author_identity_key(right)
    if not key_a or not key_b:
        return 0.0
    norm_a = canonical_part(left)
    norm_b = canonical_part(right)
    if key_a == key_b or (norm_a and norm_a == norm_b):
        return 1.0
    parts_a = _author_tokens(left)
    parts_b = _author_tokens(right)
    if not parts_a or not parts_b:
        return 0.0
    token_a, token_b = set(parts_a), set(parts_b)
    shared = token_a & token_b
    overlap = len(shared) / max(1, len(token_a | token_b))
    if _single_author_matches(left, right):
        return max(overlap, 0.85)
    surname_match = len(parts_a) >= 2 and len(parts_b) >= 2 and parts_a[-1] == parts_b[-1]
    conflicting_given_names = (
        len(parts_a) >= 2
        and len(parts_b) >= 2
        and parts_a[0] != parts_b[0]
        and len(parts_a[0]) > 1
        and len(parts_b[0]) > 1
    )
    if conflicting_given_names:
        return 0.55 if surname_match else min(0.55, overlap)
    surname_score = 0.55 if surname_match else 0.0
    return max(overlap, surname_score)


def catalog_author_matches(wanted: object, candidates: object) -> bool:
    """Match an author by full identity, shared given name + surname, or single-char initial + surname."""

    wanted_parts = _split_author_values(wanted)
    candidate_parts = _split_author_values(candidates)
    for wanted_author in wanted_parts:
        for candidate_author in candidate_parts:
            if _single_author_matches(wanted_author, candidate_author):
                return True
    return False


def catalog_author_similarity(left: object, right: object) -> float:
    """Return conservative author similarity shared across catalog matchers."""

    left_parts = _split_author_values(left)
    right_parts = _split_author_values(right)
    if not left_parts or not right_parts:
        return 0.0
    best = 0.0
    if isinstance(left, str) and isinstance(right, str):
        best = _single_author_similarity(left, right)
    for left_author in left_parts:
        for right_author in right_parts:
            best = max(best, _single_author_similarity(left_author, right_author))
    return best


def _non_latin_scripts(value: object) -> set[str]:
    scripts: set[str] = set()
    for char in unicodedata.normalize("NFKC", str(value or "")):
        if not char.isalpha():
            continue
        code = ord(char)
        if 0x0400 <= code <= 0x052F:
            scripts.add("cyrillic")
        elif (
            0x3040 <= code <= 0x30FF
            or 0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF
        ):
            scripts.add("cjk")
        elif 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF:
            scripts.add("hangul")
        elif 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F:
            scripts.add("arabic")
        elif 0x0590 <= code <= 0x05FF:
            scripts.add("hebrew")
        elif 0x0900 <= code <= 0x097F:
            scripts.add("devanagari")
        elif 0x0E00 <= code <= 0x0E7F:
            scripts.add("thai")
        elif 0x0370 <= code <= 0x03FF:
            scripts.add("greek")
    return scripts


def catalog_edition_markers(title: object) -> tuple[set[str], set[str]]:
    """Return derivative-work categories and explicit foreign-edition language codes."""

    raw = unicodedata.normalize("NFKC", str(title or "")).strip()
    if not raw:
        return set(), set()
    derivatives = {
        category
        for category, pattern in _DERIVATIVE_CATEGORY_PATTERNS
        if pattern.search(raw)
    }
    foreign_languages = {
        lang
        for lang, pattern in _FOREIGN_TITLE_LANGUAGE_PATTERNS
        if pattern.search(raw)
    }
    return derivatives, foreign_languages


def _strip_foreign_edition_suffix(title: object) -> str:
    raw = _title_without_format_suffix(title)
    for _lang, pattern in _FOREIGN_TITLE_LANGUAGE_PATTERNS:
        raw = pattern.sub("", raw).strip()
    raw = re.sub(r"\s*\(\s*\)\s*|\s*\[\s*\]\s*", " ", raw).strip(" -–—:()[]")
    return raw


def catalog_title_allowed(requested_title: object, catalog_title: object) -> bool:
    """Reject unrequested summaries, study guides, workbooks, companions, box sets, and foreign editions."""

    req_deriv, req_langs = catalog_edition_markers(requested_title)
    cat_deriv, cat_langs = catalog_edition_markers(catalog_title)
    if req_deriv != cat_deriv:
        return False
    if cat_langs - req_langs:
        return False
    if _non_latin_scripts(catalog_title) - _non_latin_scripts(requested_title):
        return False
    return True


def catalog_derivative_subjects_conflict(requested_title: object, raw_subjects: object) -> bool:
    """Return whether catalog subjects mark a derivative study guide not requested by the title."""

    req_deriv, _ = catalog_edition_markers(requested_title)
    if req_deriv & {"summary", "study_guide", "workbook", "companion"}:
        return False
    if isinstance(raw_subjects, str):
        items: list[object] = [raw_subjects]
    elif isinstance(raw_subjects, (list, tuple, set)):
        items = list(raw_subjects)
    else:
        return False
    cleaned: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("name") or item.get("subject") or item.get("value") or item.get("text") or ""
        text = " ".join(unicodedata.normalize("NFKC", str(item or "")).split()).strip()
        if text:
            cleaned.append(text)
    if not cleaned:
        return False
    if any(_DERIVATIVE_SUBJECT_PATTERN.match(subject) for subject in cleaned):
        return True
    return all(
        _DERIVATIVE_SUBJECT_PATTERN.match(subject) or _ONLY_DERIVATIVE_SUBJECT_PATTERN.match(subject)
        for subject in cleaned
    )


def catalog_language_codes(catalog_languages: object) -> set[str]:
    """Normalize provider language values to canonical lowercase ISO codes."""

    codes: set[str] = set()

    def collect(value: object) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for key in ("key", "code", "language", "value"):
                if value.get(key):
                    collect(value[key])
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                collect(item)
            return
        raw = unicodedata.normalize("NFKC", str(value)).strip()
        if not raw:
            return
        for token in re.split(r"[\s,;|]+", raw):
            cleaned = token.strip().casefold()
            if cleaned.startswith("/languages/"):
                cleaned = cleaned[len("/languages/") :]
            cleaned = cleaned.strip("/").replace("_", "-")
            primary = cleaned.split("-", 1)[0]
            if primary in _IGNORED_LANGUAGE_CODES:
                continue
            codes.add(_LANGUAGE_CODE_ALIASES.get(primary, primary))

    collect(catalog_languages)
    return codes


def catalog_language_allowed(requested_title: object, catalog_languages: object) -> bool:
    """Allow non-English catalog records only when the candidate title requests that edition."""

    codes = catalog_language_codes(catalog_languages)
    if not codes:
        return True
    _req_deriv, req_langs = catalog_edition_markers(requested_title)
    specific_langs = req_langs - {"foreign"}
    if specific_langs:
        return bool(codes & specific_langs)
    if "foreign" in req_langs:
        return bool(codes - {"en"})
    if _non_latin_scripts(requested_title):
        return True
    return "en" in codes


def _string_title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    token_a = set(a.split())
    token_b = set(b.split())
    shared = token_a & token_b
    overlap = len(shared) / max(1, len(token_a | token_b))
    return max(overlap, SequenceMatcher(None, a, b).ratio())


def catalog_title_similarity(left: object, right: object) -> float:
    """Score title similarity after enforcing derivative and foreign-edition gates."""

    if not catalog_title_allowed(left, right):
        return 0.0
    a = canonical_part(left)
    b = canonical_part(right)
    score = _string_title_similarity(a, b)
    if score == 1.0:
        return 1.0
    stripped_a = canonical_part(_title_without_format_suffix(left))
    stripped_b = canonical_part(_title_without_format_suffix(right))
    if stripped_a != a or stripped_b != b:
        score = max(score, _string_title_similarity(stripped_a, stripped_b))
    _, req_langs = catalog_edition_markers(left)
    _, cat_langs = catalog_edition_markers(right)
    if req_langs or cat_langs:
        lang_stripped_a = canonical_part(_strip_foreign_edition_suffix(left))
        lang_stripped_b = canonical_part(_strip_foreign_edition_suffix(right))
        if lang_stripped_a and lang_stripped_b:
            score = max(score, _string_title_similarity(lang_stripped_a, lang_stripped_b))
    # Catalogs often append a parenthetical series name without a number,
    # e.g. "The Fellowship of the Ring (Lord of the Rings)".
    raw_left = str(left or "").strip()
    raw_right = str(right or "").strip()
    if bool(_TRAILING_PAREN.search(raw_left)) != bool(_TRAILING_PAREN.search(raw_right)):
        base_a = canonical_part(_TRAILING_PAREN.sub("", raw_left))
        base_b = canonical_part(_TRAILING_PAREN.sub("", raw_right))
        if len(base_a.split()) >= 2 and len(base_b.split()) >= 2:
            score = max(score, _string_title_similarity(base_a, base_b))
    return score


def catalog_title_matches(wanted_title: object, candidate_title: object) -> bool:
    """Require an edition-compatible normalized title match for catalog enrichment."""

    if not catalog_title_allowed(wanted_title, candidate_title):
        return False
    wanted = book_catalog_title_identity_key(wanted_title)
    normalized = book_catalog_title_identity_key(candidate_title)
    if wanted and normalized and wanted == normalized:
        return True
    _, req_langs = catalog_edition_markers(wanted_title)
    _, cat_langs = catalog_edition_markers(candidate_title)
    if req_langs or cat_langs:
        lang_wanted = canonical_part(_strip_foreign_edition_suffix(wanted_title))
        lang_candidate = canonical_part(_strip_foreign_edition_suffix(candidate_title))
        if lang_wanted and lang_candidate and lang_wanted == lang_candidate:
            return True
    raw_left = str(wanted_title or "").strip()
    raw_right = str(candidate_title or "").strip()
    if bool(_TRAILING_PAREN.search(raw_left)) != bool(_TRAILING_PAREN.search(raw_right)):
        base_a = canonical_part(_TRAILING_PAREN.sub("", raw_left))
        base_b = canonical_part(_TRAILING_PAREN.sub("", raw_right))
        if len(base_a.split()) >= 2 and base_a == base_b:
            return True
    return False


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
    full = canonical_part(title)
    keys = {full} if full else set()
    parts = _TITLE_SEPARATOR.split(title, maxsplit=1)
    if len(parts) == 2:
        prefix = canonical_part(parts[0])
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
                if _author_match_key(_row_value(read, "author")) in {"", "unknown author"}
                for key in _title_match_keys(_row_value(read, "title"))
            ),
        )

    def matches(self, candidate) -> bool:
        """Return whether a provider result identifies a known read."""

        if book_row_identity_match_keys(candidate) & self.match_keys:
            return True
        author_key = _author_match_key(_row_value(candidate, "author"))
        if author_key and author_key != "unknown author":
            return bool(
                _title_match_keys(_row_value(candidate, "title"))
                & self.unknown_author_title_keys
            )
        return bool(
            _title_match_keys(_row_value(candidate, "title")) & self.title_keys
        )


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
