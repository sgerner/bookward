"""Candidate identity, metadata quality, and catalog enrichment.

Source feeds are useful discovery signals, but they are not authoritative book
records.  This module keeps the ingest boundary conservative: malformed rows
are rejected, plausible rows without a catalog match are quarantined, and
accepted rows carry a stable provider/work/ISBN match when one is available.

The catalog lookups are deliberately best-effort.  A provider outage must not
erase a candidate that a reader explicitly saved, so the audit records the
failure and leaves the row in quarantine for a later retry.
"""

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import json
import logging
import re
import threading
import time
import unicodedata
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
import httpx

from .covers import (
    GOOGLE_BOOKS_SEARCH,
    METADATA_GENRE_LIMIT,
    METADATA_NORMALIZATION_VERSION,
    OPEN_LIBRARY_SEARCH,
    _CATALOG_PROVIDER_FAILURE_STATUSES,
    _bounded_source_payload,
    _catalog_genres,
    _clean_metadata_text,
    _field_provenance,
    _isbn_identifier_evidence,
    _isbn_provenance_values,
    _openlibrary_publication_date,
    _openlibrary_request,
    _publication_date,
    _query_isbn_matches,
    _utc_now,
    is_weak_cover_url,
    metadata_client,
    safe_cover_url,
)
from .database import connect, persist_metadata_field_provenance, private_setting, rows, transaction
from .identity import (
    ReadIdentityIndex,
    _AUTHOR_SUFFIXES,
    _title_without_format_suffix,
    book_author_identity_key,
    book_identity,
    book_identity_match_index,
    book_row_identity_match_keys,
    canonical_part,
)
from .isbn import canonical_isbn, isbn_parts, isbn_parts_from_source


LOGGER = logging.getLogger(__name__)
QUALITY_VERSION = "candidate-quality-v6"
_NON_CATALOG_PROVIDERS = frozenset({"source", "penguinrandomhouse"})
_POLLUTION_FLAGS = frozenset(
    {
        "catalog_homonym_author",
        "catalog_derivative_edition",
        "catalog_foreign_language",
    }
)
APPLE_BOOKS_SEARCH = "https://itunes.apple.com/search"
CATALOG_CONCURRENCY = 8
CATALOG_LIMIT = 10
CATALOG_PROVIDER_RETRY_HOURS = 24
CATALOG_QUARANTINE_RETRY_DAYS = 14
QUALITY_AUDIT_BATCH_SIZE = 50
QUALITY_AUDIT_SUBBATCH_SIZE = 10
QUALITY_AUDIT_TIME_BUDGET_SECONDS = 30.0
_audit_monotonic = time.monotonic
_AUDIT_TIMESTAMP_LOCK = threading.Lock()
_LAST_AUDIT_TIMESTAMP_US = 0


def _utc_audit_timestamp() -> str:
    """Return a strictly increasing naive UTC timestamp with >=1ms step for SQLite julianday()."""

    global _LAST_AUDIT_TIMESTAMP_US
    epoch_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
    with _AUDIT_TIMESTAMP_LOCK:
        if epoch_us < _LAST_AUDIT_TIMESTAMP_US + 1_000:
            epoch_us = _LAST_AUDIT_TIMESTAMP_US + 1_000
        _LAST_AUDIT_TIMESTAMP_US = epoch_us
    resolved = datetime.fromtimestamp(epoch_us / 1_000_000, tz=timezone.utc).replace(tzinfo=None)
    return resolved.isoformat(sep=" ", timespec="microseconds")

_TRAILING_PAREN = re.compile(r"\s*\([^()]{2,80}\)\s*$")
_AUTHOR_SPLIT = re.compile(r"\s*(?:,|;|&|\band\b)\s*", re.IGNORECASE)
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
_FOREIGN_TITLE_LANGUAGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (lang, re.compile(pattern, re.IGNORECASE))
    for lang, pattern in (
        ("es", r"\b(?:spanish\s+(?:edition|language|version)|edici[oó]n\s+en\s+espa[nñ]ol|versi[oó]n\s+en\s+espa[nñ]ol|en\s+espa[nñ]ol|in\s+spanish)\b|[\(\[\-–—:]\s*(?:spanish|espa[nñ]ol)\s*[\)\]]?\s*$"),
        ("fr", r"\b(?:french\s+(?:edition|language|version)|[eé]dition\s+fran[cç]aise|version\s+fran[cç]aise|en\s+fran[cç]ais|in\s+french)\b|[\(\[\-–—:]\s*(?:french|fran[cç]ais)\s*[\)\]]?\s*$"),
        ("de", r"\b(?:german\s+(?:edition|language|version)|deutsche\s+ausgabe|auf\s+deutsch|in\s+german)\b|[\(\[\-–—:]\s*(?:german|deutsch)\s*[\)\]]?\s*$"),
        ("it", r"\b(?:italian\s+(?:edition|language|version)|edizione\s+italiana|in\s+italian)\b|[\(\[\-–—:]\s*(?:italian|italiano)\s*[\)\]]?\s*$"),
        ("pt", r"\b(?:portuguese\s+(?:edition|language|version)|edi[cç][aã]o\s+em\s+portugu[eê]s|in\s+portuguese)\b|[\(\[\-–—:]\s*(?:portuguese|portugu[eê]s)\s*[\)\]]?\s*$"),
        ("ru", r"\b(?:russian\s+(?:edition|language|version)|in\s+russian)\b|[\(\[\-–—:]\s*russian\s*[\)\]]?\s*$"),
        ("ja", r"\b(?:japanese\s+(?:edition|language|version)|in\s+japanese)\b|[\(\[\-–—:]\s*japanese\s*[\)\]]?\s*$"),
        ("zh", r"\b(?:chinese\s+(?:edition|language|version)|in\s+chinese)\b|[\(\[\-–—:]\s*chinese\s*[\)\]]?\s*$"),
        ("ko", r"\b(?:korean\s+(?:edition|language|version)|in\s+korean)\b|[\(\[\-–—:]\s*korean\s*[\)\]]?\s*$"),
        ("nl", r"\b(?:dutch\s+(?:edition|language|version)|nederlandse\s+editie|in\s+dutch)\b|[\(\[\-–—:]\s*(?:dutch|nederlands)\s*[\)\]]?\s*$"),
        ("pl", r"\b(?:polish\s+(?:edition|language|version)|wydanie\s+polskie|in\s+polish)\b|[\(\[\-–—:]\s*(?:polish|polski)\s*[\)\]]?\s*$"),
        ("sv", r"\b(?:swedish\s+(?:edition|language|version)|svensk\s+utg[aå]va|in\s+swedish)\b|[\(\[\-–—:]\s*(?:swedish|svenska)\s*[\)\]]?\s*$"),
        ("tr", r"\b(?:turkish\s+(?:edition|language|version)|t[uü]rk[cç]e\s+bask[iı]|in\s+turkish)\b|[\(\[\-–—:]\s*(?:turkish|t[uü]rk[cç]e)\s*[\)\]]?\s*$"),
        ("ar", r"\b(?:arabic\s+(?:edition|language|version)|in\s+arabic)\b|[\(\[\-–—:]\s*arabic\s*[\)\]]?\s*$"),
        ("he", r"\b(?:hebrew\s+(?:edition|language|version)|in\s+hebrew)\b|[\(\[\-–—:]\s*hebrew\s*[\)\]]?\s*$"),
        ("foreign", r"\b(?:foreign|translated)\s+(?:language\s+)?edition\b"),
    )
)
_LANGUAGE_CODE_ALIASES: dict[str, str] = {
    alias: canonical
    for canonical, aliases in (
        ("en", ("en", "eng", "english")),
        ("es", ("es", "spa", "spanish")),
        ("fr", ("fr", "fre", "fra", "french")),
        ("de", ("de", "ger", "deu", "german")),
        ("it", ("it", "ita", "italian")),
        ("pt", ("pt", "por", "portuguese")),
        ("ru", ("ru", "rus", "russian")),
        ("ja", ("ja", "jpn", "japanese")),
        ("zh", ("zh", "chi", "zho", "chinese")),
        ("ko", ("ko", "kor", "korean")),
        ("nl", ("nl", "dut", "nld", "dutch")),
        ("pl", ("pl", "pol", "polish")),
        ("sv", ("sv", "swe", "swedish")),
        ("tr", ("tr", "tur", "turkish")),
        ("ar", ("ar", "ara", "arabic")),
        ("he", ("he", "heb", "hebrew")),
        ("cs", ("cs", "cze", "ces")),
        ("el", ("el", "gre", "ell")),
        ("hu", ("hu", "hun")),
        ("ro", ("ro", "rum", "ron")),
        ("uk", ("uk", "ukr")),
        ("ca", ("ca", "cat")),
        ("id", ("id", "ind")),
        ("vi", ("vi", "vie")),
        ("th", ("th", "tha")),
        ("hi", ("hi", "hin")),
    )
    for alias in aliases
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
UNKNOWN_AUTHOR_RE = re.compile(
    r"^(?:unknown(?:\s+author)?|n/?a|none|null|various(?:\s+authors?)?|anonymous|staff)$",
    re.IGNORECASE,
)
NOISE_TITLE_RE = re.compile(
    r"^(?:untitled|unknown|new releases?|coming soon|book list|books?|n/?a|none|null)$",
    re.IGNORECASE,
)


def _text(value: object, limit: int) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(value.strip().split())[:limit]


def _tokens(value: object) -> set[str]:
    return set(canonical_part(value).split())


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


def _author_similarity(left: object, right: object) -> float:
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


def _author_has_homonym_conflict(left: object, right: object) -> bool:
    if _author_similarity(left, right) >= 0.65:
        return False
    for left_author in _split_author_values(left):
        parts_a = _author_tokens(left_author)
        if len(parts_a) < 2 or len(parts_a[0]) <= 1:
            continue
        for right_author in _split_author_values(right):
            parts_b = _author_tokens(right_author)
            if (
                len(parts_b) >= 2
                and len(parts_b[0]) > 1
                and parts_a[-1] == parts_b[-1]
                and parts_a[0] != parts_b[0]
            ):
                return True
    return False


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


def _title_similarity(left: object, right: object) -> float:
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
    raw_left = str(left or "").strip()
    raw_right = str(right or "").strip()
    if bool(_TRAILING_PAREN.search(raw_left)) != bool(_TRAILING_PAREN.search(raw_right)):
        base_a = canonical_part(_TRAILING_PAREN.sub("", raw_left))
        base_b = canonical_part(_TRAILING_PAREN.sub("", raw_right))
        if len(base_a.split()) >= 2 and len(base_b.split()) >= 2:
            score = max(score, _string_title_similarity(base_a, base_b))
    return score


def _catalog_item_targets_work(
    requested_title: object,
    requested_author: object,
    catalog_title: object,
    catalog_author: object,
    *,
    isbn_matched: bool = False,
) -> bool:
    if isbn_matched:
        return True
    if _author_similarity(requested_author, catalog_author) < 0.65:
        return False
    req_key = canonical_part(_title_without_format_suffix(requested_title))
    if not req_key:
        return False
    cat_full = canonical_part(catalog_title)
    cat_stripped = canonical_part(_strip_foreign_edition_suffix(catalog_title))
    cat_base = canonical_part(_TRAILING_PAREN.sub("", str(catalog_title or "").strip()))
    return bool(
        _string_title_similarity(req_key, cat_stripped) >= 0.82
        or _string_title_similarity(req_key, cat_base) >= 0.82
        or (len(req_key) >= 4 and f" {req_key} " in f" {cat_full} ")
    )


def _record_item_pollution_flags(
    pollution_flags: set[str] | None,
    *,
    requested_title: object,
    requested_author: object,
    catalog_title: object,
    catalog_author: object,
    isbn_matched: bool = False,
) -> None:
    if pollution_flags is None:
        return
    req_deriv, req_langs = catalog_edition_markers(requested_title)
    cat_deriv, cat_langs = catalog_edition_markers(catalog_title)
    if _catalog_item_targets_work(
        requested_title,
        requested_author,
        catalog_title,
        catalog_author,
        isbn_matched=isbn_matched,
    ):
        if req_deriv != cat_deriv:
            pollution_flags.add("catalog_derivative_edition")
        if (cat_langs - req_langs) or (_non_latin_scripts(catalog_title) - _non_latin_scripts(requested_title)):
            pollution_flags.add("catalog_foreign_language")
    req_key = canonical_part(_title_without_format_suffix(requested_title))
    cat_key = canonical_part(_title_without_format_suffix(catalog_title))
    if _string_title_similarity(req_key, cat_key) >= 0.82 and _author_has_homonym_conflict(
        requested_author, catalog_author
    ):
        pollution_flags.add("catalog_homonym_author")


def _is_accepted_match(match: dict[str, Any] | None) -> bool:
    return bool(
        match
        and match.get("quality_score", 0) >= 0.82
        and match.get("title_match", 0) >= 0.82
        and match.get("author_match", 0) >= 0.65
    )


def _first_isbn(values: object) -> tuple[str, str]:
    return isbn_parts(values)


def local_flags(candidate: dict[str, Any]) -> list[str]:
    """Return deterministic defects that do not require a network lookup."""

    title = _text(candidate.get("title"), 500)
    author = _text(candidate.get("author"), 300)
    flags: list[str] = []
    if len(title) < 2:
        flags.append("missing_title")
    elif NOISE_TITLE_RE.fullmatch(title):
        flags.append("noise_title")
    if len(author) < 2:
        flags.append("missing_author")
    elif UNKNOWN_AUTHOR_RE.fullmatch(author):
        flags.append("unknown_author")
    if title and author and canonical_part(title) == canonical_part(author):
        flags.append("title_equals_author")
    if title and ("http://" in title.casefold() or "https://" in title.casefold()):
        flags.append("title_contains_url")
    if len(_tokens(title)) < 1:
        flags.append("empty_title_tokens")
    source_url = str(candidate.get("source_url") or "").strip()
    if source_url and not source_url.startswith(("https://", "http://", "association://")):
        flags.append("invalid_source_url")
    return sorted(set(flags))


def _provider_match(
    *,
    provider: str,
    provider_id: str,
    work_id: str,
    title: str,
    author: str,
    catalog_title: str,
    catalog_author: str,
    isbn13: str = "",
    isbn10: str = "",
    description: str = "",
    description_kind: str = "",
    description_source_field: str = "",
    opening_sentence: str = "",
    release_date: str = "",
    release_date_kind: str = "",
    release_date_source_field: str = "",
    cover_url: str = "",
    genres: list[str] | None = None,
    source_payload: object = None,
    identifier_evidence: str = "not_applicable",
    identifier_verified: bool = False,
) -> dict[str, Any]:
    title_match = _title_similarity(title, catalog_title)
    author_match = _author_similarity(author, catalog_author)
    confidence = min(1.0, (title_match * 0.65) + (author_match * 0.35))
    if title_match >= 0.98 and author_match >= 0.98:
        confidence = 1.0
    cleaned_description = _clean_metadata_text(description, 4000)
    normalized_genres = _catalog_genres(genres, limit=METADATA_GENRE_LIMIT)
    description_content_quality_heuristic = 0.0
    if cleaned_description:
        length_quality = min(1.0, len(cleaned_description) / 600.0)
        description_content_quality_heuristic = round(
            (0.5 + 0.5 * length_quality)
            if description_kind == "synopsis"
            else 0.5 * length_quality,
            4,
        )
    if release_date:
        normalized_release_date, inferred_date_kind = _publication_date(release_date)
        if normalized_release_date:
            release_date = normalized_release_date
            release_date_kind = release_date_kind or inferred_date_kind or ""
        else:
            release_date = ""
            release_date_kind = ""
    fields: dict[str, Any] = {}
    if cleaned_description:
        fields["description"] = _field_provenance(
            provider,
            provider_id,
            cleaned_description,
            kind=description_kind,
            source_field=description_source_field,
        )
        fields["description"]["identity_quality_score"] = round(confidence, 4)
        fields["description"]["content_quality_heuristic"] = description_content_quality_heuristic
        fields["description"]["content_quality_note"] = "Heuristic from field type and normalized character length; not a calibrated probability."
    if normalized_genres:
        fields["genres"] = _field_provenance(
            provider, provider_id, normalized_genres, kind="subjects",
            source_field="subject" if provider == "openlibrary" else "categories",
        )
        fields["genres"]["identity_quality_score"] = round(confidence, 4)
    if cover_url:
        fields["cover_url"] = _field_provenance(
            provider, provider_id, cover_url, kind="cover",
            source_field="cover_i" if provider == "openlibrary" else "imageLinks",
        )
    if release_date:
        fields["release_date"] = _field_provenance(
            provider, provider_id, release_date, kind=release_date_kind or "date",
            source_field=release_date_source_field,
        )
    return {
        "provider": provider,
        "provider_id": provider_id,
        "work_id": work_id,
        "isbn13": isbn13,
        "isbn10": isbn10,
        "identifier_evidence": identifier_evidence,
        "identifier_verified": bool(identifier_verified),
        "title_match": round(title_match, 4),
        "author_match": round(author_match, 4),
        "quality_score": round(confidence, 4),
        "identity_quality_score": round(confidence, 4),
        "catalog_title": _text(catalog_title, 500),
        "catalog_author": _text(catalog_author, 300),
        "description": cleaned_description,
        "description_kind": description_kind,
        "description_source_field": description_source_field,
        "description_opening_sentence": _clean_metadata_text(opening_sentence, 1500),
        "description_provider": provider if cleaned_description else "",
        "description_provider_id": provider_id if cleaned_description else "",
        "description_content_quality_heuristic": description_content_quality_heuristic,
        "release_date": _text(release_date, 32),
        "release_date_kind": release_date_kind if release_date else "",
        "release_date_source_field": release_date_source_field if release_date else "",
        "cover_url": cover_url,
        "genres": normalized_genres,
        "genres_provider": provider if normalized_genres else "",
        "genres_provider_id": provider_id if normalized_genres else "",
        "cover_provider": provider if cover_url else "",
        "cover_provider_id": provider_id if cover_url else "",
        "release_date_provider": provider if release_date else "",
        "release_date_provider_id": provider_id if release_date else "",
        "metadata_provenance": {
            "fetched_at": _utc_now(),
            "normalization_version": METADATA_NORMALIZATION_VERSION,
            "fields": fields,
            "fetch_trace": [],
            "source_payloads": [
                {"provider": provider, "provider_id": provider_id, "payload": _bounded_source_payload(source_payload)}
            ] if source_payload else [],
        },
    }


async def _fetch_catalog_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    provider: str,
    query_kind: str,
    params: Mapping[str, Any],
    trace: list[dict[str, Any]] | None = None,
    request: Callable[..., Awaitable[httpx.Response]] | None = None,
    rate_limit_statuses: Collection[int] = frozenset(),
) -> tuple[Any, dict[str, Any]] | None:
    fetch_trace: dict[str, Any] = {
        "provider": provider,
        "fetched_at": _utc_now(),
        "query_kind": query_kind,
        "status": "request_error",
    }
    if trace is not None:
        trace.append(fetch_trace)
    try:
        response = (
            await request(client, url, params=params)
            if request is not None
            else await client.get(url, params=params)
        )
        fetch_trace["http_status"] = response.status_code
        if response.is_error:
            fetch_trace["status"] = (
                "rate_limited" if response.status_code in rate_limit_statuses else "http_error"
            )
            return None
        try:
            payload = response.json()
        except ValueError:
            fetch_trace["status"] = "invalid_json"
            return None
    except Exception as exc:
        fetch_trace["error_type"] = type(exc).__name__
        return None
    return payload, fetch_trace


def _select_best_provider_match(
    candidates: Iterable[dict[str, Any]],
    fetch_trace: dict[str, Any],
    *,
    isbn: str = "",
    mismatched_identifiers: int = 0,
    include_identifier_trace: bool = True,
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    matched_count = 0
    for match in candidates:
        matched_count += 1
        if best is None or (
            _is_accepted_match(match),
            bool(match.get("identifier_verified")),
            match.get("identifier_evidence") != "mismatch",
            match["quality_score"],
            match["description_kind"] == "synopsis",
            match["description_content_quality_heuristic"],
            len(match["genres"]),
        ) > (
            _is_accepted_match(best),
            bool(best.get("identifier_verified")),
            best.get("identifier_evidence") != "mismatch",
            best["quality_score"],
            best["description_kind"] == "synopsis",
            best["description_content_quality_heuristic"],
            len(best["genres"]),
        ):
            best = match
    fetch_trace["matched_count"] = matched_count
    if include_identifier_trace:
        fetch_trace["mismatched_identifier_count"] = mismatched_identifiers
    if best:
        fetch_trace["status"] = "matched"
        fetch_trace["provider_id"] = best["provider_id"]
        fetch_trace["available_fields"] = [
            field for field in ("description", "genres", "cover_url", "release_date") if best.get(field)
        ]
        if include_identifier_trace:
            source_payloads = best["metadata_provenance"]["source_payloads"]
            identifiers = source_payloads[0]["payload"].get("identifiers", []) if source_payloads else []
            evidence, verified = _isbn_identifier_evidence(isbn, identifiers)
            fetch_trace["identifier_evidence"] = evidence
            fetch_trace["identifier_verified"] = verified
        best["metadata_provenance"]["fetch_trace"] = [fetch_trace]
    else:
        fetch_trace["status"] = "no_match"
        if include_identifier_trace and isbn:
            fetch_trace["identifier_evidence"] = "mismatch" if mismatched_identifiers else "no_result"
            fetch_trace["identifier_verified"] = False
    return best


async def _open_library_match(
    title: str,
    author: str,
    client: httpx.AsyncClient,
    *,
    isbn: str = "",
    expected_isbn: str = "",
    trace: list[dict[str, Any]] | None = None,
    pollution_flags: set[str] | None = None,
) -> dict[str, Any] | None:
    query_title = _title_without_format_suffix(title) or title
    params = {
        ("isbn" if isbn else "title"): isbn or query_title[:500],
        **({} if isbn else {"author": author[:300]}),
        "limit": CATALOG_LIMIT,
        "fields": "key,title,author_name,cover_i,first_publish_year,first_publish_date,first_sentence,description,subject,isbn,isbn13,edition_key,language",
    }
    fetched = await _fetch_catalog_json(
        client,
        OPEN_LIBRARY_SEARCH,
        provider="openlibrary",
        query_kind="isbn" if isbn else "title_author",
        params=params,
        trace=trace,
        request=_openlibrary_request,
        rate_limit_statuses=frozenset(),
    )
    if fetched is None:
        return None
    payload, fetch_trace = fetched
    docs = payload.get("docs", []) if isinstance(payload, dict) else []
    target_isbn = isbn or expected_isbn
    candidates: list[dict[str, Any]] = []
    mismatched_identifiers = 0
    for doc in docs if isinstance(docs, list) else []:
        if not isinstance(doc, dict):
            continue
        raw_identifiers = [doc.get("isbn13"), doc.get("isbn"), doc.get("isbn10")]
        if isbn and not _query_isbn_matches(isbn, raw_identifiers):
            mismatched_identifiers += 1
            continue
        authors = doc.get("author_name") or []
        if isinstance(authors, str):
            authors = [authors]
        catalog_author = ", ".join(str(value) for value in authors if value)
        catalog_title = str(doc.get("title") or "")
        if not catalog_language_allowed(title, doc.get("language")):
            if pollution_flags is not None and _catalog_item_targets_work(
                title, author, catalog_title, catalog_author, isbn_matched=bool(isbn)
            ):
                pollution_flags.add("catalog_foreign_language")
            continue
        doc_evidence, doc_verified = _isbn_identifier_evidence(target_isbn, raw_identifiers)
        if not doc_verified and catalog_derivative_subjects_conflict(title, doc.get("subject")):
            if pollution_flags is not None and _catalog_item_targets_work(
                title, author, catalog_title, catalog_author, isbn_matched=bool(isbn)
            ):
                pollution_flags.add("catalog_derivative_edition")
            continue
        _record_item_pollution_flags(
            pollution_flags,
            requested_title=title,
            requested_author=author,
            catalog_title=catalog_title,
            catalog_author=catalog_author,
            isbn_matched=bool(isbn),
        )
        projected_identifiers = _isbn_provenance_values(target_isbn, raw_identifiers)
        isbn13, isbn10 = _first_isbn(projected_identifiers)
        cover = ""
        if str(doc.get("cover_i") or "").isdigit():
            cover = f"https://covers.openlibrary.org/b/id/{int(doc['cover_i'])}-L.jpg"
        synopsis = _clean_metadata_text(doc.get("description"), 4000)
        opening_sentence = _clean_metadata_text(doc.get("first_sentence"), 1500)
        description = synopsis or opening_sentence
        description_kind = "synopsis" if synopsis else "opening_sentence" if opening_sentence else ""
        _parsed_release_date, release_date_kind, release_date_source_field = _openlibrary_publication_date(doc)
        release_date = _text(doc.get(release_date_source_field), 32) if release_date_kind else ""
        provider_id = str(doc.get("key") or "")
        genres = _catalog_genres(doc.get("subject"), limit=METADATA_GENRE_LIMIT)
        source_payload = {
            "key": provider_id,
            "title": _clean_metadata_text(doc.get("title"), 500),
            "authors": [_clean_metadata_text(value, 300) for value in authors if value],
            "description": synopsis,
            "opening_sentence": opening_sentence,
            "genres": genres,
            "identifiers": projected_identifiers,
            "publication_date": _clean_metadata_text(doc.get("first_publish_date") or doc.get("first_publish_year"), 40),
            "publication_date_source_field": release_date_source_field if release_date else "",
            "cover_id": str(doc.get("cover_i") or "")[:40],
        }
        match = _provider_match(
            provider="openlibrary",
            provider_id=provider_id,
            work_id=provider_id,
            title=title,
            author=author,
            catalog_title=str(doc.get("title") or ""),
            catalog_author=catalog_author,
            isbn13=isbn13,
            isbn10=isbn10,
            description=description,
            description_kind=description_kind,
            description_source_field="description" if synopsis else "first_sentence" if opening_sentence else "",
            opening_sentence=opening_sentence,
            release_date=release_date or "",
            release_date_kind=release_date_kind or "",
            release_date_source_field=release_date_source_field if release_date else "",
            cover_url=safe_cover_url(cover),
            genres=genres,
            source_payload=source_payload,
            identifier_evidence=doc_evidence,
            identifier_verified=doc_verified,
        )
        candidates.append(match)
    return _select_best_provider_match(
        candidates,
        fetch_trace,
        isbn=isbn,
        mismatched_identifiers=mismatched_identifiers,
    )


async def _google_books_match(
    title: str,
    author: str,
    client: httpx.AsyncClient,
    *,
    isbn: str = "",
    expected_isbn: str = "",
    trace: list[dict[str, Any]] | None = None,
    google_books_api_key: str = "",
    pollution_flags: set[str] | None = None,
) -> dict[str, Any] | None:
    query_title = _title_without_format_suffix(title) or title
    params = {
        "q": f"isbn:{isbn}" if isbn else f"intitle:{query_title[:300]} inauthor:{author[:200]}",
        "maxResults": CATALOG_LIMIT,
    }
    if google_books_api_key:
        # Do not include the request URL in fetch traces or exception text.
        params["key"] = google_books_api_key
    fetched = await _fetch_catalog_json(
        client,
        GOOGLE_BOOKS_SEARCH,
        provider="google_books",
        query_kind="isbn" if isbn else "title_author",
        params=params,
        trace=trace,
        rate_limit_statuses={429},
    )
    if fetched is None:
        return None
    payload, fetch_trace = fetched
    target_isbn = isbn or expected_isbn
    candidates: list[dict[str, Any]] = []
    mismatched_identifiers = 0
    for item in payload.get("items", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        info = item.get("volumeInfo") or {}
        if not isinstance(info, dict):
            continue
        authors = info.get("authors") or []
        if isinstance(authors, str):
            authors = [authors]
        catalog_author = ", ".join(str(value) for value in authors if value)
        catalog_title = str(info.get("title") or "")
        raw_identifiers = info.get("industryIdentifiers", [])
        if not isinstance(raw_identifiers, list):
            raw_identifiers = []
        identifiers = [
            value.get("identifier") for value in raw_identifiers
            if isinstance(value, dict) and value.get("type") in {"ISBN_10", "ISBN_13"}
        ]
        if isbn and not _query_isbn_matches(isbn, identifiers):
            mismatched_identifiers += 1
            continue
        if not catalog_language_allowed(title, info.get("language")):
            if pollution_flags is not None and _catalog_item_targets_work(
                title, author, catalog_title, catalog_author, isbn_matched=bool(isbn)
            ):
                pollution_flags.add("catalog_foreign_language")
            continue
        doc_evidence, doc_verified = _isbn_identifier_evidence(target_isbn, identifiers)
        if not doc_verified and catalog_derivative_subjects_conflict(title, info.get("categories")):
            if pollution_flags is not None and _catalog_item_targets_work(
                title, author, catalog_title, catalog_author, isbn_matched=bool(isbn)
            ):
                pollution_flags.add("catalog_derivative_edition")
            continue
        _record_item_pollution_flags(
            pollution_flags,
            requested_title=title,
            requested_author=author,
            catalog_title=catalog_title,
            catalog_author=catalog_author,
            isbn_matched=bool(isbn),
        )
        projected_identifiers = _isbn_provenance_values(target_isbn, identifiers)
        isbn13, isbn10 = _first_isbn(projected_identifiers)
        image_links = info.get("imageLinks") or {}
        image = ""
        if isinstance(image_links, dict):
            for key in ("extraLarge", "large", "medium", "thumbnail", "smallThumbnail"):
                if image_links.get(key):
                    image = safe_cover_url(str(image_links[key]).replace("http://", "https://"))
                    if image:
                        break
        description = _clean_metadata_text(info.get("description"), 4000)
        provider_id = str(item.get("id") or "")
        genres = _catalog_genres(info.get("categories"), limit=METADATA_GENRE_LIMIT)
        raw_release_date = _text(info.get("publishedDate"), 32)
        _parsed_release_date, release_date_kind = _publication_date(raw_release_date)
        release_date = raw_release_date if release_date_kind else ""
        source_payload = {
            "id": provider_id,
            "title": _clean_metadata_text(info.get("title"), 500),
            "authors": [_clean_metadata_text(value, 300) for value in authors if value],
            "description": description,
            "genres": genres,
            "identifiers": projected_identifiers,
            "publication_date": _clean_metadata_text(info.get("publishedDate"), 40),
            "publication_date_source_field": "publishedDate" if release_date else "",
            "cover_url": image,
        }
        match = _provider_match(
            provider="google_books",
            provider_id=provider_id,
            work_id=provider_id,
            title=title,
            author=author,
            catalog_title=str(info.get("title") or ""),
            catalog_author=", ".join(str(value) for value in authors if value),
            isbn13=isbn13,
            isbn10=isbn10,
            description=description,
            description_kind="synopsis" if description else "",
            description_source_field="description" if description else "",
            release_date=release_date,
            release_date_kind=release_date_kind or "",
            release_date_source_field="publishedDate" if release_date else "",
            cover_url=image,
            genres=genres,
            source_payload=source_payload,
            identifier_evidence=doc_evidence,
            identifier_verified=doc_verified,
        )
        candidates.append(match)
    return _select_best_provider_match(
        candidates,
        fetch_trace,
        isbn=isbn,
        mismatched_identifiers=mismatched_identifiers,
    )


async def _apple_books_match(
    title: str,
    author: str,
    client: httpx.AsyncClient,
    *,
    isbn: str = "",
    media: str = "ebook",
    trace: list[dict[str, Any]] | None = None,
    pollution_flags: set[str] | None = None,
) -> dict[str, Any] | None:
    if isbn:
        return None
    query_title = _title_without_format_suffix(title) or title
    term = " ".join(part for part in (query_title[:200], author[:150]) if part).strip()
    if not term:
        return None
    fetched = await _fetch_catalog_json(
        client,
        APPLE_BOOKS_SEARCH,
        provider="apple_books",
        query_kind=f"title_author_{media}",
        params={"media": media, "term": term, "limit": 5, "country": "us"},
        trace=trace,
        rate_limit_statuses={403, 429},
    )
    if fetched is None:
        return None
    payload, fetch_trace = fetched
    candidates: list[dict[str, Any]] = []
    for item in payload.get("results", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        track_id = str(item.get("trackId") or "").strip()
        catalog_title = str(item.get("trackName") or "").strip()
        catalog_author = str(item.get("artistName") or "").strip()
        if not track_id or not catalog_title:
            continue
        if not catalog_language_allowed(title, item.get("language") or item.get("languageCodesISO2A")):
            if pollution_flags is not None and _catalog_item_targets_work(
                title, author, catalog_title, catalog_author
            ):
                pollution_flags.add("catalog_foreign_language")
            continue
        if catalog_derivative_subjects_conflict(title, item.get("genres")):
            if pollution_flags is not None and _catalog_item_targets_work(
                title, author, catalog_title, catalog_author
            ):
                pollution_flags.add("catalog_derivative_edition")
            continue
        _record_item_pollution_flags(
            pollution_flags,
            requested_title=title,
            requested_author=author,
            catalog_title=catalog_title,
            catalog_author=catalog_author,
        )
        description = _clean_metadata_text(item.get("description"))
        description_kind = "synopsis" if description else ""
        release_date, release_date_kind = _publication_date(item.get("releaseDate"))
        raw_cover = str(item.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
        cover = safe_cover_url(raw_cover)
        genres = _catalog_genres(
            [g for g in (item.get("genres") or []) if str(g).casefold() != "books"],
            limit=METADATA_GENRE_LIMIT,
        )
        source_payload = {
            "trackId": item.get("trackId"),
            "trackName": _clean_metadata_text(catalog_title, 500),
            "artistName": _clean_metadata_text(catalog_author, 300),
            "description": description,
            "genres": genres,
            "releaseDate": _clean_metadata_text(item.get("releaseDate"), 40),
            "artworkUrl100": raw_cover,
            "media": media,
        }
        match = _provider_match(
            provider="apple_books",
            provider_id=track_id,
            work_id="",
            title=title,
            author=author,
            catalog_title=catalog_title,
            catalog_author=catalog_author,
            description=description,
            description_kind=description_kind,
            description_source_field="description" if description else "",
            release_date=release_date or "",
            release_date_kind=release_date_kind or "",
            release_date_source_field="releaseDate" if release_date else "",
            cover_url=cover,
            genres=genres,
            source_payload=source_payload,
        )
        candidates.append(match)
    return _select_best_provider_match(
        candidates,
        fetch_trace,
        include_identifier_trace=False,
    )


async def resolve_catalog_match(
    title: str,
    author: str,
    *,
    client: httpx.AsyncClient,
    cache: dict[tuple[str, ...], asyncio.Task[dict[str, Any] | None]],
    isbn13: str = "",
    isbn10: str = "",
    isbn: str = "",
    google_books_api_key: str = "",
    existing_fields_complete: bool = False,
    rate_limited_providers: set[str] | None = None,
    pollution_flags_out: set[str] | None = None,
) -> dict[str, Any] | None:
    """Resolve identity first, then choose metadata independently per field."""

    source_isbn13, source_isbn10 = isbn_parts(isbn13, isbn10, isbn)
    key = (
        canonical_part(title),
        canonical_part(author),
        source_isbn13,
        source_isbn10,
        bool(google_books_api_key),
        bool(existing_fields_complete),
    )
    existing = cache.get(key)
    if existing is not None:
        cached_result = await existing
        if pollution_flags_out is not None:
            pollution_flags_out.update(getattr(existing, "_pollution_flags", ()))
        return cached_result

    def accepted(match: dict[str, Any] | None) -> bool:
        return bool(
            match
            and match.get("quality_score", 0) >= 0.82
            and match.get("title_match", 0) >= 0.82
            and match.get("author_match", 0) >= 0.65
        )

    detected_pollution: set[str] = set()

    async def lookup():
        fetch_trace: list[dict[str, Any]] = []
        matches: list[dict[str, Any]] = []
        query_isbn = source_isbn13 or source_isbn10

        def _record_rate_limits(traces: list[dict[str, Any]] | None = None):
            if rate_limited_providers is None:
                return
            for trace in traces if traces is not None else fetch_trace:
                if trace.get("status") == "rate_limited" and trace.get("provider"):
                    rate_limited_providers.add(str(trace["provider"]))

        async def _call_provider(lookup_provider, *, isbn_arg: str = ""):
            provider_name = "openlibrary" if lookup_provider is _open_library_match else "google_books"
            if rate_limited_providers is not None and provider_name in rate_limited_providers:
                return None, [
                    {
                        "provider": provider_name,
                        "fetched_at": _utc_now(),
                        "query_kind": "isbn" if isbn_arg else "title_author",
                        "status": "rate_limited",
                        "http_status": 429,
                        "skipped": True,
                    }
                ]
            kwargs: dict[str, Any] = (
                {"google_books_api_key": google_books_api_key}
                if lookup_provider is _google_books_match
                else {}
            )
            if query_isbn and not isbn_arg:
                kwargs["expected_isbn"] = query_isbn
            provider_traces: list[dict[str, Any]] = []
            result = await lookup_provider(
                title,
                author,
                client,
                isbn=isbn_arg,
                trace=provider_traces,
                pollution_flags=detected_pollution,
                **kwargs,
            )
            _record_rate_limits(provider_traces)
            return result, provider_traces

        if query_isbn:
            # Query both providers concurrently so a strong identity record with
            # sparse fields does not hide useful verified metadata from the
            # other and Open Library's 1 req/s lock never stalls Google Books.
            stage1_results = await asyncio.gather(
                _call_provider(_open_library_match, isbn_arg=query_isbn),
                _call_provider(_google_books_match, isbn_arg=query_isbn),
            )
            for match, provider_traces in stage1_results:
                fetch_trace.extend(provider_traces)
                if match:
                    matches.append(match)

        def has_full_field_coverage(values: list[dict[str, Any]]) -> bool:
            return (
                any(value.get("description_kind") == "synopsis" for value in values)
                and any(value.get("genres") for value in values)
                and any(value.get("cover_url") for value in values)
                and any(value.get("release_date") for value in values)
            )

        if not query_isbn or (
            not has_full_field_coverage([value for value in matches if accepted(value)])
            and not existing_fields_complete
        ):
            unavailable_providers = {
                str(trace.get("provider") or "")
                for trace in fetch_trace
                if trace.get("query_kind") == "isbn"
                and trace.get("status") in _CATALOG_PROVIDER_FAILURE_STATUSES
            }
            stage2_providers = [
                lookup_provider
                for lookup_provider in (_open_library_match, _google_books_match)
                if not (
                    query_isbn
                    and ("openlibrary" if lookup_provider is _open_library_match else "google_books")
                    in unavailable_providers
                )
            ]
            if stage2_providers:
                stage2_results = await asyncio.gather(
                    *(_call_provider(lookup_provider) for lookup_provider in stage2_providers)
                )
                for match, provider_traces in stage2_results:
                    fetch_trace.extend(provider_traces)
                    if match:
                        matches.append(match)

        primary_unavailable = any(
            trace.get("provider") in {"openlibrary", "google_books"}
            and trace.get("status") in _CATALOG_PROVIDER_FAILURE_STATUSES
            for trace in fetch_trace
        )
        if (
            not any(accepted(value) for value in matches)
            and (not query_isbn or primary_unavailable)
            and (rate_limited_providers is None or "apple_books" not in rate_limited_providers)
        ):
            apple_match = await _apple_books_match(
                title,
                author,
                client,
                media="ebook",
                trace=fetch_trace,
                pollution_flags=detected_pollution,
            )
            _record_rate_limits()
            if apple_match:
                matches.append(apple_match)
            if not accepted(apple_match) and (
                rate_limited_providers is None or "apple_books" not in rate_limited_providers
            ):
                audio_match = await _apple_books_match(
                    title,
                    author,
                    client,
                    media="audiobook",
                    trace=fetch_trace,
                    pollution_flags=detected_pollution,
                )
                _record_rate_limits()
                if audio_match:
                    matches.append(audio_match)

        provider_unavailable = any(
            trace.get("status") in _CATALOG_PROVIDER_FAILURE_STATUSES
            for trace in fetch_trace
        )
        provider_rate_limited = any(
            trace.get("status") == "rate_limited" for trace in fetch_trace
        )

        if not matches:
            if provider_unavailable:
                return {
                    "provider_lookup_unavailable": True,
                    "provider_rate_limited": provider_rate_limited,
                    "quality_score": 0.0,
                    "title_match": 0.0,
                    "author_match": 0.0,
                    "metadata_provenance": {
                        "fetched_at": _utc_now(),
                        "normalization_version": METADATA_NORMALIZATION_VERSION,
                        "fields": {},
                        "fetch_trace": fetch_trace,
                        "source_payloads": [],
                    },
                }
            return None

        # De-duplicate an ISBN and a text result for the same provider record,
        # retaining whichever parsed record supplies more useful fields while
        # preserving exact identifier verification for that record.
        by_record: dict[tuple[str, str], dict[str, Any]] = {}
        for match in matches:
            record_key = (str(match.get("provider") or ""), str(match.get("provider_id") or ""))
            prior = by_record.get(record_key)
            rank = (
                accepted(match),
                match.get("identifier_evidence") != "mismatch",
                match.get("description_kind") == "synopsis",
                len(str(match.get("description") or "")),
                len(match.get("genres") or []),
                bool(match.get("cover_url")),
                bool(match.get("release_date")),
                bool(match.get("identifier_verified")),
            )
            prior_rank = (
                accepted(prior),
                prior.get("identifier_evidence") != "mismatch",
                prior.get("description_kind") == "synopsis",
                len(str(prior.get("description") or "")),
                len(prior.get("genres") or []),
                bool(prior.get("cover_url")),
                bool(prior.get("release_date")),
                bool(prior.get("identifier_verified")),
            ) if prior else None
            if prior is None or rank > prior_rank:
                merged = dict(match)
                if prior and prior.get("identifier_verified") and not merged.get("identifier_verified"):
                    merged["identifier_verified"] = True
                    merged["identifier_evidence"] = "matched"
                by_record[record_key] = merged
            elif prior and match.get("identifier_verified") and not prior.get("identifier_verified"):
                prior = dict(prior)
                prior["identifier_verified"] = True
                prior["identifier_evidence"] = "matched"
                by_record[record_key] = prior
        records = list(by_record.values())
        verified_records = [record for record in records if accepted(record)]
        identity_record = max(
            records,
            key=lambda record: (
                accepted(record),
                bool(record.get("identifier_verified")),
                record.get("identifier_evidence") != "mismatch",
                record.get("quality_score", 0),
            ),
        )
        metadata_records = verified_records or []
        if not metadata_records:
            identity_record = dict(identity_record)
            identity_record["metadata_provenance"] = {
                "fetched_at": _utc_now(),
                "normalization_version": METADATA_NORMALIZATION_VERSION,
                "fields": {},
                "fetch_trace": fetch_trace,
                "source_payloads": [],
            }
            identity_record["provider_lookup_unavailable"] = provider_unavailable
            identity_record["provider_rate_limited"] = provider_rate_limited
            if detected_pollution:
                identity_record["pollution_flags"] = sorted(detected_pollution)
            return identity_record

        has_isbn_verified = bool(
            query_isbn and any(record.get("identifier_verified") for record in metadata_records)
        )
        description_records = [record for record in metadata_records if record.get("description")]
        description_record = max(
            description_records,
            key=lambda record: (
                record.get("description_kind") == "synopsis",
                bool(record.get("identifier_verified")),
                record.get("identifier_evidence") != "mismatch",
                record.get("quality_score", 0),
                len(str(record.get("description") or "")),
            ),
            default=None,
        )
        description = str(description_record.get("description") or "") if description_record else ""
        eligible_genre_records = sorted(
            (
                record
                for record in metadata_records
                if not (has_isbn_verified and record.get("identifier_evidence") == "mismatch")
            ),
            key=lambda record: (
                not bool(record.get("identifier_verified")),
                -float(record.get("quality_score", 0) or 0.0),
            ),
        )
        genres: list[str] = []
        genre_sources: list[dict[str, str]] = []
        for record in eligible_genre_records:
            before = len(genres)
            genres = _catalog_genres(genres + list(record.get("genres") or []), limit=METADATA_GENRE_LIMIT)
            if len(genres) > before:
                provider = str(record.get("provider") or "")
                genre_sources.append({
                    "provider": provider,
                    "provider_id": str(record.get("provider_id") or ""),
                    "kind": "subjects",
                    "source_field": "subject" if provider == "openlibrary" else "categories",
                    "normalization_version": METADATA_NORMALIZATION_VERSION,
                })
        cover_record = next(
            (
                record
                for record in sorted(
                    metadata_records,
                    key=lambda record: (
                        not bool(record.get("identifier_verified")),
                        record.get("identifier_evidence") == "mismatch",
                        -float(record.get("quality_score", 0) or 0.0),
                    ),
                )
                if record.get("cover_url")
            ),
            None,
        )
        date_rank = {"day": 3, "month": 2, "year": 1}
        eligible_date_records = (
            record
            for record in metadata_records
            if record.get("release_date")
            and not (has_isbn_verified and record.get("identifier_evidence") == "mismatch")
        )
        date_record = max(
            eligible_date_records,
            key=lambda record: (
                bool(record.get("identifier_verified")),
                float(record.get("quality_score", 0) or 0.0),
                date_rank.get(
                    record.get("release_date_kind") or _publication_date_kind(record.get("release_date")), 0
                ),
                len(str(record.get("release_date") or "")),
            ),
            default=None,
        )

        result = dict(identity_record)
        result.update({
            "description": description,
            "description_kind": description_record.get("description_kind", "") if description_record else "",
            "description_source_field": description_record.get("description_source_field", "") if description_record else "",
            "description_opening_sentence": description_record.get("description_opening_sentence", "") if description_record else "",
            "description_provider": description_record.get("provider", "") if description_record else "",
            "description_provider_id": description_record.get("provider_id", "") if description_record else "",
            "description_content_quality_heuristic": description_record.get("description_content_quality_heuristic", 0.0) if description_record else 0.0,
            "description_candidate": {
                "provider": description_record.get("provider", ""),
                "provider_id": description_record.get("provider_id", ""),
                "text": description,
                "kind": description_record.get("description_kind", ""),
                "source_field": description_record.get("description_source_field", ""),
                "opening_sentence": description_record.get("description_opening_sentence", ""),
                "content_quality_heuristic": description_record.get("description_content_quality_heuristic", 0.0),
                "content_quality_note": "Heuristic from field type and normalized character length; not a calibrated probability.",
            } if description_record else {},
            "genres": genres,
            "genres_provider": genre_sources[0]["provider"] if genre_sources else "",
            "genres_provider_id": genre_sources[0]["provider_id"] if genre_sources else "",
            "cover_url": cover_record.get("cover_url", "") if cover_record else "",
            "cover_provider": cover_record.get("provider", "") if cover_record else "",
            "cover_provider_id": cover_record.get("provider_id", "") if cover_record else "",
            "release_date": date_record.get("release_date", "") if date_record else "",
            "release_date_kind": date_record.get("release_date_kind", "") if date_record else "",
            "release_date_source_field": date_record.get("release_date_source_field", "") if date_record else "",
            "release_date_provider": date_record.get("provider", "") if date_record else "",
            "release_date_provider_id": date_record.get("provider_id", "") if date_record else "",
            "metadata_provider": (
                description_record.get("provider", "") if description_record
                else genre_sources[0]["provider"] if genre_sources
                else cover_record.get("provider", "") if cover_record
                else date_record.get("provider", "") if date_record
                else ""
            ),
            "metadata_provider_id": (
                description_record.get("provider_id", "") if description_record
                else genre_sources[0]["provider_id"] if genre_sources
                else cover_record.get("provider_id", "") if cover_record
                else date_record.get("provider_id", "") if date_record
                else ""
            ),
        })
        fields: dict[str, Any] = {}
        if description_record:
            fields["description"] = _field_provenance(
                str(description_record.get("provider") or ""),
                str(description_record.get("provider_id") or ""),
                description,
                kind=str(description_record.get("description_kind") or ""),
                source_field=str(description_record.get("description_source_field") or ""),
            )
            fields["description"]["identity_quality_score"] = description_record.get("quality_score", 0.0)
            fields["description"]["content_quality_heuristic"] = description_record.get("description_content_quality_heuristic", 0.0)
            fields["description"]["content_quality_note"] = "Heuristic from field type and normalized character length; not a calibrated probability."
        if genre_sources:
            fields["genres"] = {
                "provider": genre_sources[0]["provider"],
                "provider_id": genre_sources[0]["provider_id"],
                "sources": genre_sources,
                "kind": "subjects",
                "source_field": genre_sources[0]["source_field"],
                "value_sha256": _field_provenance("", "", genres)["value_sha256"],
                "normalization_version": METADATA_NORMALIZATION_VERSION,
            }
        if cover_record:
            fields["cover_url"] = _field_provenance(
                str(cover_record.get("provider") or ""), str(cover_record.get("provider_id") or ""),
                cover_record.get("cover_url", ""), kind="cover",
                source_field="cover_i" if cover_record.get("provider") == "openlibrary" else "imageLinks",
            )
        if date_record:
            fields["release_date"] = _field_provenance(
                str(date_record.get("provider") or ""), str(date_record.get("provider_id") or ""),
                date_record.get("release_date", ""),
                kind=str(date_record.get("release_date_kind") or "date"),
                source_field=str(date_record.get("release_date_source_field") or ""),
            )
        result["provider_lookup_unavailable"] = provider_unavailable
        result["provider_rate_limited"] = provider_rate_limited
        result["metadata_provenance"] = {
            "fetched_at": _utc_now(),
            "normalization_version": METADATA_NORMALIZATION_VERSION,
            "fields": fields,
            "fetch_trace": fetch_trace,
            "source_payloads": [
                source
                for record in metadata_records
                for source in record.get("metadata_provenance", {}).get("source_payloads", [])
            ],
        }
        return result

    task = asyncio.create_task(lookup())
    setattr(task, "_pollution_flags", detected_pollution)
    cache[key] = task
    resolved = await task
    if pollution_flags_out is not None:
        pollution_flags_out.update(detected_pollution)
    return resolved


def _publication_date_kind(value: object) -> str:
    from .covers import _publication_date

    _, kind = _publication_date(value)
    return str(kind or "")


async def _audit_one(
    candidate: dict[str, Any],
    client: httpx.AsyncClient,
    cache,
    google_books_api_key: str = "",
    rate_limited_providers: set[str] | None = None,
):
    flags = local_flags(candidate)
    source_isbn13, source_isbn10 = isbn_parts_from_source(
        [candidate.get("isbn13"), candidate.get("isbn10"), candidate.get("isbn")],
        candidate.get("source_url", ""),
    )
    source_identifiers = {"isbn13": source_isbn13, "isbn10": source_isbn10}
    if any(flag in flags for flag in ("missing_title", "noise_title", "missing_author", "unknown_author", "title_equals_author", "title_contains_url")):
        return {
            "candidate_id": int(candidate["id"]),
            "quality_status": "rejected",
            "quality_score": 0.0,
            "flags": flags,
            **source_identifiers,
        }
    pollution_flags: set[str] = set()
    match = await resolve_catalog_match(
        candidate["title"],
        candidate["author"],
        client=client,
        cache=cache,
        isbn13=source_isbn13,
        isbn10=source_isbn10,
        google_books_api_key=google_books_api_key,
        existing_fields_complete=_candidate_fields_complete(candidate),
        rate_limited_providers=rate_limited_providers,
        pollution_flags_out=pollution_flags,
    )
    if isinstance(match, dict):
        if isinstance(match.get("pollution_flags"), list):
            pollution_flags.update(str(item) for item in match["pollution_flags"] if item)
        if match.get("catalog_title") or match.get("catalog_author"):
            _record_item_pollution_flags(
                pollution_flags,
                requested_title=candidate["title"],
                requested_author=candidate["author"],
                catalog_title=match.get("catalog_title", ""),
                catalog_author=match.get("catalog_author", ""),
            )
        elif (
            float(match.get("title_match", 0) or 0) >= 0.82
            and 0.5 <= float(match.get("author_match", 0) or 0) <= 0.55
        ):
            pollution_flags.add("catalog_homonym_author")

    accepted = _is_accepted_match(match)
    if accepted and match is not None:
        accepted_flags = list(flags)
        if match.get("provider_lookup_unavailable"):
            accepted_flags.append("catalog_provider_unavailable")
            if match.get("provider_rate_limited"):
                accepted_flags.append("catalog_provider_rate_limited")
        return {
            "candidate_id": int(candidate["id"]),
            "quality_status": "accepted",
            "quality_score": match["quality_score"],
            **match,
            "flags": sorted(set(accepted_flags)),
            "isbn13": source_isbn13 or match.get("isbn13", ""),
            "isbn10": source_isbn10 or match.get("isbn10", ""),
        }

    if not match or match.get("provider_lookup_unavailable"):
        unavailable = bool(match and match.get("provider_lookup_unavailable"))
        failure_flags = ["catalog_provider_unavailable"]
        if match and match.get("provider_rate_limited"):
            failure_flags.append("catalog_provider_rate_limited")
        return {
            "candidate_id": int(candidate["id"]),
            "quality_status": "quarantine",
            "quality_score": 0.0,
            "provider_lookup_unavailable": unavailable,
            "flags": sorted(
                set(
                    flags
                    + (
                        failure_flags
                        if unavailable
                        else ["catalog_unmatched", *(pollution_flags & _POLLUTION_FLAGS)]
                    )
                )
            ),
            **({"metadata_provenance": match.get("metadata_provenance", {})} if unavailable else {}),
            **source_identifiers,
        }
    return {
        "candidate_id": int(candidate["id"]),
        "quality_status": "quarantine",
        "quality_score": match["quality_score"],
        "title_match": match["title_match"],
        "author_match": match["author_match"],
        "flags": sorted(
            set(flags + ["catalog_match_ambiguous", *(pollution_flags & _POLLUTION_FLAGS)])
        ),
        "provider": "",
        "provider_id": "",
        "work_id": "",
        **source_identifiers,
    }


def _candidate_fields_complete(candidate: dict[str, Any]) -> bool:
    if "catalog_provider_unavailable" in str(candidate.get("_flags_json") or ""):
        return False
    prior_version = str(candidate.get("_audit_version") or "")
    if prior_version and prior_version != QUALITY_VERSION and (
        candidate.get("_has_catalog_provenance") or candidate.get("_metadata_provider")
    ):
        return False
    genres = candidate.get("genres") or []
    if isinstance(genres, str):
        try:
            genres = json.loads(genres)
        except (TypeError, ValueError, json.JSONDecodeError):
            genres = [genres] if genres.strip() else []
    genres = _catalog_genres(genres, limit=METADATA_GENRE_LIMIT)
    cover_url = str(candidate.get("cover_url") or "")
    return bool(
        _clean_metadata_text(candidate.get("description"))
        and genres
        and str(candidate.get("release_date") or "").strip()
        and cover_url
        and not is_weak_cover_url(cover_url)
        and safe_cover_url(cover_url, str(candidate.get("source_url") or ""))
    )


def _attach_catalog_provenance_flags(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not candidates:
        return candidates
    catalog_ids: set[int] = set()
    candidate_ids = [int(item["id"]) for item in candidates]
    for start in range(0, len(candidate_ids), 400):
        batch = candidate_ids[start : start + 400]
        placeholders = ",".join("?" for _ in batch)
        for item in rows(
            "SELECT DISTINCT entity_id FROM metadata_field_provenance "
            f"WHERE entity_type='candidate' AND entity_id IN ({placeholders}) "
            "AND provider NOT IN ('source','penguinrandomhouse')",
            tuple(batch),
        ):
            catalog_ids.add(int(item["entity_id"]))
    for candidate in candidates:
        candidate["_has_catalog_provenance"] = int(candidate["id"]) in catalog_ids
    return candidates


def candidate_quality_audit_eligibility(
    now: datetime | None = None,
    *,
    include_quarantined: bool = False,
    quarantine_before: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Return the shared SQL predicate and parameters for a due bounded audit."""

    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    outage_retry_before = (
        reference_time - timedelta(hours=CATALOG_PROVIDER_RETRY_HOURS)
    ).isoformat(timespec="seconds")
    if include_quarantined:
        quarantine_cutoff = quarantine_before or reference_time.isoformat(timespec="microseconds")
    else:
        quarantine_cutoff = (
            reference_time - timedelta(days=CATALOG_QUARANTINE_RETRY_DAYS)
        ).isoformat(timespec="seconds")
    predicate = (
        "COALESCE(q.audit_version,'')!='builtin-curated-v1' AND ("
        "COALESCE(q.quality_status,'pending')='pending' "
        "OR q.audit_version='legacy-pending-audit-v1' "
        "OR (q.quality_status IN ('accepted','quarantine') "
        "AND COALESCE(q.audit_version,'')!=? "
        "AND (COALESCE(c.isbn13,'')!='' OR COALESCE(c.isbn10,'')!='' "
        "OR COALESCE(q.isbn13,'')!='' OR COALESCE(q.isbn10,'')!='' "
        "OR LOWER(COALESCE(c.source_url,'')) LIKE '%amazon.%' "
        "OR (q.quality_status='accepted' AND ("
        "COALESCE(q.provider,'')!='' OR COALESCE(q.metadata_provider,'')!='' "
        "OR EXISTS(SELECT 1 FROM metadata_field_provenance mfp "
        "WHERE mfp.entity_type='candidate' AND mfp.entity_id=c.id "
        "AND mfp.provider NOT IN ('source','penguinrandomhouse')))))) "
        "OR (q.quality_status IN ('accepted','quarantine') "
        "AND q.flags_json LIKE '%catalog_provider_unavailable%' "
        "AND julianday(COALESCE(q.audited_at,q.updated_at))<=julianday(?)) "
        "OR (q.quality_status='quarantine' "
        "AND julianday(COALESCE(q.audited_at,q.updated_at))<=julianday(?)))"
    )
    return predicate, (QUALITY_VERSION, outage_retry_before, quarantine_cutoff)


def _due_audit_reason(
    candidate: Mapping[str, Any],
    *,
    include_quarantined: bool = False,
) -> str | None:
    audit_version = str(candidate.get("audit_version") or "")
    if audit_version == "builtin-curated-v1":
        return None
    status = str(candidate.get("quality_status") or "pending")
    if status == "pending":
        return "pending"
    if audit_version == "legacy-pending-audit-v1":
        return "legacy_pending"
    try:
        flags = json.loads(candidate.get("flags_json") or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        flags = []
    has_outage_flag = isinstance(flags, list) and "catalog_provider_unavailable" in flags
    c_isbn13 = str(candidate.get("c_isbn13") or "").strip()
    c_isbn10 = str(candidate.get("c_isbn10") or "").strip()
    q_isbn13 = str(candidate.get("q_isbn13") or "").strip()
    q_isbn10 = str(candidate.get("q_isbn10") or "").strip()
    has_raw_isbn = bool(c_isbn13 or c_isbn10 or q_isbn13 or q_isbn10)
    valid_isbn = any(
        isbn_parts_from_source(
            [c_isbn13 or q_isbn13, c_isbn10 or q_isbn10],
            str(candidate.get("source_url") or ""),
        )
    )
    corrupt_isbn_only = has_raw_isbn and not valid_isbn
    outage_due = bool(candidate.get("outage_due"))
    quarantine_due = bool(candidate.get("quarantine_due"))

    if status in {"accepted", "quarantine"} and audit_version == QUALITY_VERSION and has_outage_flag:
        if outage_due or (status == "quarantine" and include_quarantined and quarantine_due):
            return "outage_retry"
        return None
    if status in {"accepted", "quarantine"} and audit_version != QUALITY_VERSION:
        if valid_isbn:
            return "prior_version_isbn"
        if corrupt_isbn_only:
            return None
        has_catalog_attribution = bool(
            str(candidate.get("q_provider") or "").strip()
            or str(candidate.get("q_metadata_provider") or "").strip()
            or candidate.get("has_catalog_provenance")
        )
        if status == "accepted" and has_catalog_attribution:
            return "prior_version_catalog"
        if status == "quarantine" and has_outage_flag and outage_due:
            return "outage_retry"
        if status == "quarantine" and quarantine_due:
            return "quarantine_retry"
        return None
    if status == "quarantine" and audit_version == QUALITY_VERSION:
        if corrupt_isbn_only:
            return None
        if quarantine_due:
            return "quarantine_retry"
    return None


def _due_candidate_quality_page_rows(
    predicate: str,
    params: tuple[str, ...],
    *,
    page_size: int,
    last_id: int,
    audited_before: str | None = None,
) -> list[dict[str, Any]]:
    _, outage_retry_before, quarantine_cutoff = params
    extra_where = ""
    extra_params: tuple[str, ...] = ()
    if audited_before:
        extra_where = "AND (q.audited_at IS NULL OR julianday(q.audited_at)<julianday(?)) "
        extra_params = (audited_before,)
    return rows(
        "SELECT c.id,c.isbn13 AS c_isbn13,c.isbn10 AS c_isbn10,c.source_url,"
        "q.quality_status,q.audit_version,q.flags_json,"
        "q.isbn13 AS q_isbn13,q.isbn10 AS q_isbn10,"
        "q.provider AS q_provider,q.metadata_provider AS q_metadata_provider,"
        "CASE WHEN julianday(COALESCE(q.audited_at,q.updated_at))<=julianday(?) THEN 1 ELSE 0 END AS outage_due,"
        "CASE WHEN julianday(COALESCE(q.audited_at,q.updated_at))<=julianday(?) THEN 1 ELSE 0 END AS quarantine_due,"
        "EXISTS(SELECT 1 FROM metadata_field_provenance mfp "
        "WHERE mfp.entity_type='candidate' AND mfp.entity_id=c.id "
        "AND mfp.provider NOT IN ('source','penguinrandomhouse')) AS has_catalog_provenance "
        "FROM candidates c LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
        "LEFT JOIN sources s ON s.id=c.source_id "
        f"WHERE c.id>? AND c.status!='rejected' AND (COALESCE(s.enabled,1)=1 OR c.status IN ('saved','imported')) AND ({predicate}) {extra_where}"
        "ORDER BY c.id LIMIT ?",
        (outage_retry_before, quarantine_cutoff, last_id, *params, *extra_params, page_size),
    )


def _due_candidate_quality_breakdown(
    *,
    now: datetime | None = None,
    include_quarantined: bool = False,
    quarantine_before: str | None = None,
    audited_before: str | None = None,
) -> tuple[int, dict[str, int]]:
    predicate, params = candidate_quality_audit_eligibility(
        now,
        include_quarantined=include_quarantined,
        quarantine_before=quarantine_before,
    )
    page_size = 250
    last_id = 0
    total = 0
    breakdown: dict[str, int] = {}
    while True:
        page = _due_candidate_quality_page_rows(
            predicate,
            params,
            page_size=page_size,
            last_id=last_id,
            audited_before=audited_before,
        )
        if not page:
            break
        for candidate in page:
            reason = _due_audit_reason(candidate, include_quarantined=include_quarantined)
            if reason:
                total += 1
                breakdown[reason] = breakdown.get(reason, 0) + 1
        last_id = int(page[-1]["id"])
        if len(page) < page_size:
            break
    return total, breakdown


def count_candidate_quality_audit_candidates(
    *,
    now: datetime | None = None,
    include_quarantined: bool = False,
    quarantine_before: str | None = None,
    audited_before: str | None = None,
) -> int:
    """Return the exact number of candidates currently due for quality recovery."""

    total, _ = _due_candidate_quality_breakdown(
        now=now,
        include_quarantined=include_quarantined,
        quarantine_before=quarantine_before,
        audited_before=audited_before,
    )
    return total


def candidate_quality_audit_candidates(
    limit: int = QUALITY_AUDIT_BATCH_SIZE,
    *,
    now: datetime | None = None,
    include_quarantined: bool = False,
    quarantine_before: str | None = None,
    audited_before: str | None = None,
) -> list[dict[str, Any]]:
    """Select a bounded batch due for quality audit."""

    batch_limit = max(1, min(int(limit), QUALITY_AUDIT_BATCH_SIZE))
    predicate, params = candidate_quality_audit_eligibility(
        now,
        include_quarantined=include_quarantined,
        quarantine_before=quarantine_before,
    )
    page_size = max(100, batch_limit * 4)
    selected_ids: list[int] = []
    last_id = 0
    while len(selected_ids) < batch_limit:
        page = _due_candidate_quality_page_rows(
            predicate,
            params,
            page_size=page_size,
            last_id=last_id,
            audited_before=audited_before,
        )
        if not page:
            break
        for candidate in page:
            if _due_audit_reason(candidate, include_quarantined=include_quarantined):
                selected_ids.append(int(candidate["id"]))
            if len(selected_ids) >= batch_limit:
                break
        last_id = int(page[-1]["id"])
        if len(page) < page_size:
            break
    if not selected_ids:
        return []
    placeholders = ",".join("?" for _ in selected_ids)
    selected = rows(
        "SELECT c.*,s.url AS source_root,q.quality_status AS _quality_status,"
        "q.audit_version AS _audit_version,q.flags_json AS _flags_json,"
        "q.provider AS _provider,q.provider_id AS _provider_id,"
        "q.metadata_provider AS _metadata_provider,"
        "q.metadata_provider_id AS _metadata_provider_id FROM candidates c "
        "LEFT JOIN sources s ON s.id=c.source_id "
        "LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
        f"WHERE c.id IN ({placeholders}) ORDER BY c.id",
        tuple(selected_ids),
    )
    return _attach_catalog_provenance_flags(selected)


def has_candidate_quality_audit_candidates(
    *,
    now: datetime | None = None,
    include_quarantined: bool = False,
    quarantine_before: str | None = None,
    audited_before: str | None = None,
) -> bool:
    """Return whether any candidate is due for a bounded quality audit."""

    return bool(
        candidate_quality_audit_candidates(
            limit=1,
            now=now,
            include_quarantined=include_quarantined,
            quarantine_before=quarantine_before,
            audited_before=audited_before,
        )
    )


def _provenance_payload_genres(raw_payload: object) -> list[str]:
    try:
        parsed = json.loads(str(raw_payload or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, dict):
        return []
    payload = parsed.get("payload")
    if isinstance(payload, dict) and payload.get("genres"):
        return _catalog_genres(payload.get("genres"), limit=METADATA_GENRE_LIMIT)
    if parsed.get("genres"):
        return _catalog_genres(parsed.get("genres"), limit=METADATA_GENRE_LIMIT)
    return []


def _plan_store_result(con, candidate: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    candidate_id = int(candidate["id"])
    status = str(result.get("quality_status", "quarantine") or "quarantine")
    raw_flags = result.get("flags", [])
    flags = [str(item) for item in raw_flags] if isinstance(raw_flags, list) else []
    transient_outage = (
        "catalog_provider_unavailable" in flags
        or bool(result.get("provider_lookup_unavailable"))
    )
    if transient_outage and "catalog_provider_unavailable" not in flags:
        flags = sorted(set([*flags, "catalog_provider_unavailable"]))
    has_explicit_pollution = bool(_POLLUTION_FLAGS & set(flags)) and not transient_outage

    prior_quality = con.execute(
        "SELECT quality_status,quality_score,flags_json,provider,provider_id,work_id,"
        "isbn13,isbn10,title_match,author_match,audit_version,audited_at,metadata_provider,"
        "metadata_provider_id FROM candidate_quality WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()
    prior_status = str(
        (prior_quality["quality_status"] if prior_quality else candidate.get("_quality_status")) or ""
    )
    prior_audit_version = str(
        candidate.get("_audit_version")
        or (prior_quality["audit_version"] if prior_quality else "")
        or ""
    )
    try:
        prior_flags = json.loads(
            str(
                (prior_quality["flags_json"] if prior_quality else candidate.get("_flags_json"))
                or "[]"
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        prior_flags = []
    had_prior_outage = isinstance(prior_flags, list) and "catalog_provider_unavailable" in prior_flags
    existing_provider = str(
        candidate.get("_metadata_provider")
        or (prior_quality["metadata_provider"] if prior_quality else "")
        or ""
    )
    existing_provider_id = str(
        candidate.get("_metadata_provider_id")
        or (prior_quality["metadata_provider_id"] if prior_quality else "")
        or ""
    )
    is_prior_version = bool(prior_audit_version and prior_audit_version != QUALITY_VERSION)
    can_reconcile_catalog_fields = (is_prior_version or had_prior_outage) and not transient_outage

    provenance_rows = con.execute(
        "SELECT field,provider,provider_id,source_payload FROM metadata_field_provenance "
        "WHERE entity_type='candidate' AND entity_id=?",
        (candidate_id,),
    ).fetchall()
    source_provenance_fields: set[str] = set()
    catalog_provenance_fields: set[str] = set()
    catalog_recorded_genres: list[str] = []
    source_recorded_genres: list[str] = []
    for prov_row in provenance_rows:
        field_name = str(prov_row["field"] or "")
        provider_name = str(prov_row["provider"] or "")
        if provider_name in _NON_CATALOG_PROVIDERS:
            source_provenance_fields.add(field_name)
            if field_name == "genres":
                source_recorded_genres.extend(_provenance_payload_genres(prov_row["source_payload"]))
        else:
            catalog_provenance_fields.add(field_name)
            if field_name == "genres":
                catalog_recorded_genres.extend(_provenance_payload_genres(prov_row["source_payload"]))

    existing_description = _clean_metadata_text(candidate.get("description"), 4000)
    existing_cover = str(candidate.get("cover_url") or "")
    existing_release_date = str(candidate.get("release_date") or "").strip()
    existing_date_kind = str(candidate.get("date_kind") or "")
    raw_existing_genres = candidate.get("genres", "[]")
    if isinstance(raw_existing_genres, str):
        try:
            raw_existing_genres = json.loads(raw_existing_genres)
        except (TypeError, ValueError):
            raw_existing_genres = [raw_existing_genres] if raw_existing_genres.strip() else []
    normalized_existing_genres = _catalog_genres(raw_existing_genres, limit=METADATA_GENRE_LIMIT)

    is_catalog_description = (
        "description" not in source_provenance_fields
        and "description" in catalog_provenance_fields
    )
    is_catalog_cover = (
        "cover_url" not in source_provenance_fields and "cover_url" in catalog_provenance_fields
    )
    is_catalog_date = "release_date" not in source_provenance_fields and (
        "release_date" in catalog_provenance_fields or existing_date_kind == "catalog"
    )
    if "genres" in catalog_provenance_fields:
        if catalog_recorded_genres:
            catalog_genre_keys = {genre.casefold() for genre in catalog_recorded_genres}
            source_genre_keys = {genre.casefold() for genre in source_recorded_genres}
            preserved_source_genres = [
                genre
                for genre in normalized_existing_genres
                if genre.casefold() not in catalog_genre_keys or genre.casefold() in source_genre_keys
            ]
        elif "genres" in source_provenance_fields:
            preserved_source_genres = normalized_existing_genres
        else:
            preserved_source_genres = []
    else:
        preserved_source_genres = normalized_existing_genres

    if status != "accepted":
        has_prior_verified_acceptance = bool(
            prior_quality is not None
            and prior_status == "accepted"
            and (
                str(prior_quality["provider"] or "").strip()
                or str(prior_quality["metadata_provider"] or "").strip()
                or prior_quality["audited_at"] is not None
                or catalog_provenance_fields
            )
        )
        if transient_outage and has_prior_verified_acceptance:
            return {
                "branch": "preserve_accepted_outage",
                "effective_status": "accepted",
                "flags": flags,
                "prior_quality": prior_quality,
                "existing_provider": existing_provider,
                "existing_provider_id": existing_provider_id,
                "any_candidate_change": False,
                "cleared_fields": [],
            }
        clear_description = bool(has_explicit_pollution and is_catalog_description and existing_description)
        clear_cover = bool(has_explicit_pollution and is_catalog_cover and existing_cover)
        clear_date = bool(has_explicit_pollution and is_catalog_date and existing_release_date)
        clear_genres = bool(has_explicit_pollution and preserved_source_genres != normalized_existing_genres)
        cleared_fields = [
            name
            for name, cleared in (
                ("description", clear_description),
                ("cover_url", clear_cover),
                ("release_date", clear_date),
                ("genres", clear_genres),
            )
            if cleared
        ]
        keep_existing_provider = transient_outage or not has_explicit_pollution
        return {
            "branch": "non_accepted",
            "effective_status": status,
            "flags": flags,
            "next_metadata_provider": existing_provider if keep_existing_provider else "",
            "next_metadata_provider_id": existing_provider_id if keep_existing_provider else "",
            "clear_description": clear_description,
            "clear_cover": clear_cover,
            "clear_date": clear_date,
            "clear_genres": clear_genres,
            "preserved_source_genres": preserved_source_genres,
            "delete_catalog_provenance": bool(has_explicit_pollution and catalog_provenance_fields),
            "any_candidate_change": False,
            "cleared_fields": cleared_fields,
        }

    description = _text(result.get("description"), 4000)
    description_candidate = result.get("description_candidate")
    if not isinstance(description_candidate, dict):
        description_candidate = {}
    can_upgrade_opening_sentence = bool(
        existing_description
        and description_candidate.get("kind") == "synopsis"
        and description_candidate.get("opening_sentence")
        and existing_description == _clean_metadata_text(description_candidate.get("opening_sentence"), 1500)
        and existing_provider
        and existing_provider == str(description_candidate.get("provider") or "")
        and existing_provider_id == str(description_candidate.get("provider_id") or "")
        and description
    )
    if transient_outage and existing_description:
        store_description = False
        next_description = existing_description
    elif is_catalog_description:
        if description:
            store_description = description != existing_description or can_upgrade_opening_sentence
            next_description = description
        elif can_reconcile_catalog_fields and existing_description:
            store_description = True
            next_description = ""
        else:
            store_description = False
            next_description = existing_description
    else:
        store_description = bool(description and (not existing_description or can_upgrade_opening_sentence))
        next_description = description if store_description else existing_description

    cover_url = str(result.get("cover_url") or "")
    if transient_outage and is_catalog_cover and existing_cover:
        store_cover = False
        next_cover = existing_cover
    elif is_catalog_cover:
        if cover_url:
            store_cover = cover_url != existing_cover
            next_cover = cover_url
        elif can_reconcile_catalog_fields and existing_cover:
            store_cover = True
            next_cover = ""
        else:
            store_cover = False
            next_cover = existing_cover
    else:
        store_cover = bool(
            cover_url
            and (
                not existing_cover
                or "/b/isbn/" in existing_cover
                or existing_cover.startswith("https://placehold.co/")
            )
        )
        next_cover = cover_url if store_cover else existing_cover

    release_date = _text(result.get("release_date"), 32)
    next_release_date: str | None = existing_release_date or None
    next_release_date_kind = existing_date_kind
    store_release_date = False
    if is_catalog_date:
        if release_date and not (transient_outage and existing_release_date):
            candidate_date_kind = str(result.get("release_date_kind") or "catalog")
            if release_date != existing_release_date or candidate_date_kind != existing_date_kind:
                store_release_date = True
                next_release_date = release_date
                next_release_date_kind = candidate_date_kind
        elif can_reconcile_catalog_fields and existing_release_date:
            store_release_date = True
            next_release_date = None
            next_release_date_kind = "unknown"
    elif release_date and not existing_release_date:
        store_release_date = True
        next_release_date = release_date
        next_release_date_kind = str(result.get("release_date_kind") or "catalog")

    result_genres = _catalog_genres(result.get("genres", []), limit=METADATA_GENRE_LIMIT)
    if transient_outage:
        merged_genres = (
            _catalog_genres(normalized_existing_genres + result_genres, limit=METADATA_GENRE_LIMIT)
            if result_genres
            else normalized_existing_genres
        )
    else:
        genre_base = (
            preserved_source_genres
            if "genres" in catalog_provenance_fields and (can_reconcile_catalog_fields or result_genres)
            else normalized_existing_genres
        )
        merged_genres = _catalog_genres(genre_base + result_genres, limit=METADATA_GENRE_LIMIT)
    store_genres = merged_genres != normalized_existing_genres

    cleared_fields: list[str] = []
    if store_description and not next_description and existing_description:
        cleared_fields.append("description")
    if store_cover and not next_cover and existing_cover:
        cleared_fields.append("cover_url")
    if store_release_date and not next_release_date and existing_release_date:
        cleared_fields.append("release_date")
    if store_genres and len(merged_genres) < len(normalized_existing_genres):
        cleared_fields.append("genres")

    return {
        "branch": "accepted",
        "effective_status": "accepted",
        "flags": flags,
        "transient_outage": transient_outage,
        "can_reconcile_catalog_fields": can_reconcile_catalog_fields,
        "existing_provider": existing_provider,
        "existing_provider_id": existing_provider_id,
        "catalog_provenance_fields": catalog_provenance_fields,
        "is_catalog_description": is_catalog_description,
        "is_catalog_cover": is_catalog_cover,
        "is_catalog_date": is_catalog_date,
        "description": description,
        "store_description": store_description,
        "next_description": next_description,
        "store_cover": store_cover,
        "next_cover": next_cover,
        "store_release_date": store_release_date,
        "next_release_date": next_release_date,
        "next_release_date_kind": next_release_date_kind,
        "result_genres": result_genres,
        "merged_genres": merged_genres,
        "store_genres": store_genres,
        "any_candidate_change": bool(
            store_description or store_cover or store_release_date or store_genres
        ),
        "cleared_fields": cleared_fields,
    }


def _google_books_outage_retry_status(
    result: Mapping[str, Any],
    flags: list[str] | tuple[str, ...],
) -> str:
    if not any(
        flag in flags
        for flag in ("catalog_provider_unavailable", "catalog_provider_rate_limited")
    ):
        return ""
    provenance = result.get("metadata_provenance")
    traces = (
        provenance.get("fetch_trace", [])
        if isinstance(provenance, dict)
        else []
    )
    if not isinstance(traces, list):
        return ""
    retry_status = ""
    for trace in traces:
        if not isinstance(trace, dict) or trace.get("provider") != "google_books":
            continue
        trace_status = trace.get("status")
        http_status = trace.get("http_status")
        if trace_status in {
            "rate_limited",
            "server_error",
            "request_error",
            "timeout",
            "provider_unavailable",
        } or (
            isinstance(http_status, int)
            and (http_status in {408, 429} or http_status >= 500)
        ):
            return "google_books_transient"
        if trace_status == "credential_error" or (
            isinstance(http_status, int) and http_status in {400, 401, 403}
        ):
            retry_status = "google_books_error"
    return retry_status


def _store_result(con, candidate: dict[str, Any], result: dict[str, Any]) -> bool:
    candidate_id = int(candidate["id"])
    plan = _plan_store_result(con, candidate, result)
    flags = plan["flags"]
    audited_at = _utc_audit_timestamp()

    if plan["branch"] == "preserve_accepted_outage":
        con.execute(
            """UPDATE candidate_quality SET
                flags_json=?,
                isbn13=CASE WHEN isbn13!='' THEN isbn13 ELSE ? END,
                isbn10=CASE WHEN isbn10!='' THEN isbn10 ELSE ? END,
                audit_version=?,
                audited_at=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE candidate_id=?""",
            (
                json.dumps(flags, separators=(",", ":")),
                str(result.get("isbn13", "")),
                str(result.get("isbn10", "")),
                QUALITY_VERSION,
                audited_at,
                candidate_id,
            ),
        )
        return False

    if plan["branch"] == "non_accepted":
        status = plan["effective_status"]
        quarantine_retry_status = (
            _google_books_outage_retry_status(result, flags)
            if status == "quarantine"
            else ""
        )
        con.execute(
            """INSERT INTO candidate_quality(
                candidate_id,quality_status,quality_score,flags_json,provider,provider_id,
                work_id,isbn13,isbn10,title_match,author_match,audit_version,metadata_provider,
                metadata_provider_id,metadata_retry_status,audited_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(candidate_id) DO UPDATE SET
                quality_status=excluded.quality_status,
                quality_score=excluded.quality_score,
                flags_json=excluded.flags_json,
                provider=excluded.provider,
                provider_id=excluded.provider_id,
                work_id=excluded.work_id,
                isbn13=excluded.isbn13,
                isbn10=excluded.isbn10,
                title_match=excluded.title_match,
                author_match=excluded.author_match,
                audit_version=excluded.audit_version,
                metadata_provider=excluded.metadata_provider,
                metadata_provider_id=excluded.metadata_provider_id,
                metadata_retry_status=excluded.metadata_retry_status,
                audited_at=excluded.audited_at,
                updated_at=CURRENT_TIMESTAMP""",
            (
                candidate_id,
                status,
                float(result.get("quality_score", 0) or 0),
                json.dumps(flags, separators=(",", ":")),
                str(result.get("provider", "")),
                str(result.get("provider_id", "")),
                str(result.get("work_id", "")),
                str(result.get("isbn13", "")),
                str(result.get("isbn10", "")),
                float(result.get("title_match", 0) or 0),
                float(result.get("author_match", 0) or 0),
                QUALITY_VERSION,
                plan["next_metadata_provider"],
                plan["next_metadata_provider_id"],
                quarantine_retry_status,
                audited_at,
            ),
        )
        if status == "rejected" and candidate.get("status") in {"new", "recommended"}:
            con.execute(
                "UPDATE candidates SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (candidate_id,),
            )
        if (
            plan["clear_description"]
            or plan["clear_cover"]
            or plan["clear_date"]
            or plan["clear_genres"]
        ):
            cleaned_genres_json = json.dumps(
                plan["preserved_source_genres"], ensure_ascii=False, separators=(",", ":")
            )
            con.execute(
                """UPDATE candidates SET
                    description=CASE WHEN ?=1 THEN '' ELSE description END,
                    cover_url=CASE WHEN ?=1 THEN '' ELSE cover_url END,
                    release_date=CASE WHEN ?=1 THEN NULL ELSE release_date END,
                    date_kind=CASE WHEN ?=1 THEN 'unknown' ELSE date_kind END,
                    genres=CASE WHEN ?=1 THEN ? ELSE genres END,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (
                    int(plan["clear_description"]),
                    int(plan["clear_cover"]),
                    int(plan["clear_date"]),
                    int(plan["clear_date"]),
                    int(plan["clear_genres"]),
                    cleaned_genres_json,
                    candidate_id,
                ),
            )
        if plan["delete_catalog_provenance"]:
            con.execute(
                "DELETE FROM metadata_field_provenance "
                "WHERE entity_type='candidate' AND entity_id=? "
                "AND provider NOT IN ('source','penguinrandomhouse')",
                (candidate_id,),
            )
        return False

    store_description = plan["store_description"]
    next_description = plan["next_description"]
    store_cover = plan["store_cover"]
    next_cover = plan["next_cover"]
    store_release_date = plan["store_release_date"]
    next_release_date = plan["next_release_date"]
    next_release_date_kind = plan["next_release_date_kind"]
    result_genres = plan["result_genres"]
    merged_genres = plan["merged_genres"]
    store_genres = plan["store_genres"]
    is_catalog_description = plan["is_catalog_description"]
    is_catalog_cover = plan["is_catalog_cover"]
    is_catalog_date = plan["is_catalog_date"]
    catalog_provenance_fields = plan["catalog_provenance_fields"]
    can_reconcile_catalog_fields = plan["can_reconcile_catalog_fields"]
    existing_provider = plan["existing_provider"]
    existing_provider_id = plan["existing_provider_id"]
    description = plan["description"]
    genres_json = json.dumps(merged_genres, ensure_ascii=False, separators=(",", ":"))

    selected_fields = []
    if store_description and next_description:
        selected_fields.append(("description", result.get("description_provider"), result.get("description_provider_id")))
    if store_genres and result_genres:
        selected_fields.append(("genres", result.get("genres_provider"), result.get("genres_provider_id")))
    if store_cover and next_cover:
        selected_fields.append(("cover_url", result.get("cover_provider"), result.get("cover_provider_id")))
    if store_release_date and next_release_date:
        selected_fields.append(("release_date", result.get("release_date_provider"), result.get("release_date_provider_id")))

    if store_description:
        metadata_provider = str(result.get("description_provider") or "") if next_description else ""
        metadata_provider_id = str(result.get("description_provider_id") or "") if next_description else ""
    elif is_catalog_description and description and not plan["transient_outage"]:
        metadata_provider = str(result.get("description_provider") or existing_provider)
        metadata_provider_id = str(result.get("description_provider_id") or existing_provider_id)
    else:
        metadata_provider = existing_provider
        metadata_provider_id = existing_provider_id

    con.execute(
        """INSERT INTO candidate_quality(
            candidate_id,quality_status,quality_score,flags_json,provider,provider_id,
            work_id,isbn13,isbn10,title_match,author_match,audit_version,metadata_provider,
            metadata_provider_id,metadata_retry_status,audited_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(candidate_id) DO UPDATE SET
            quality_status=excluded.quality_status,
            quality_score=excluded.quality_score,
            flags_json=excluded.flags_json,
            provider=excluded.provider,
            provider_id=excluded.provider_id,
            work_id=excluded.work_id,
            isbn13=excluded.isbn13,
            isbn10=excluded.isbn10,
            title_match=excluded.title_match,
            author_match=excluded.author_match,
            audit_version=excluded.audit_version,
            metadata_provider=excluded.metadata_provider,
            metadata_provider_id=excluded.metadata_provider_id,
            metadata_retry_status=CASE WHEN excluded.quality_status='accepted' THEN metadata_retry_status ELSE excluded.metadata_retry_status END,
            audited_at=excluded.audited_at,
            updated_at=CURRENT_TIMESTAMP""",
        (
            candidate_id,
            "accepted",
            float(result.get("quality_score", 0) or 0),
            json.dumps(flags, separators=(",", ":")),
            str(result.get("provider", "")),
            str(result.get("provider_id", "")),
            str(result.get("work_id", "")),
            str(result.get("isbn13", "")),
            str(result.get("isbn10", "")),
            float(result.get("title_match", 0) or 0),
            float(result.get("author_match", 0) or 0),
            QUALITY_VERSION,
            metadata_provider,
            metadata_provider_id,
            "",
            audited_at,
        ),
    )

    any_candidate_change = plan["any_candidate_change"]
    if any_candidate_change:
        con.execute(
            """UPDATE candidates SET
                description=CASE WHEN ?=1 THEN ? ELSE description END,
                cover_url=CASE WHEN ?=1 THEN ? ELSE cover_url END,
                release_date=CASE WHEN ?=1 THEN ? ELSE release_date END,
                date_kind=CASE WHEN ?=1 THEN ? ELSE date_kind END,
                genres=CASE WHEN ?=1 THEN ? ELSE genres END,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (
                int(store_description), next_description,
                int(store_cover), next_cover,
                int(store_release_date), next_release_date,
                int(store_release_date), next_release_date_kind,
                int(store_genres), genres_json,
                candidate_id,
            ),
        )

    provenance_refresh_fields = set()
    if store_description or (is_catalog_description and can_reconcile_catalog_fields):
        provenance_refresh_fields.add("description")
    if store_genres or ("genres" in catalog_provenance_fields and can_reconcile_catalog_fields):
        provenance_refresh_fields.add("genres")
    if store_cover or (is_catalog_cover and can_reconcile_catalog_fields):
        provenance_refresh_fields.add("cover_url")
    if store_release_date or (is_catalog_date and can_reconcile_catalog_fields):
        provenance_refresh_fields.add("release_date")

    for field in provenance_refresh_fields:
        con.execute(
            "DELETE FROM metadata_field_provenance "
            "WHERE entity_type='candidate' AND entity_id=? AND field=? "
            "AND provider NOT IN ('source','penguinrandomhouse')",
            (candidate_id, field),
        )

    field_provider_keys = {
        "description": ("description_provider", "description_provider_id"),
        "genres": ("genres_provider", "genres_provider_id"),
        "cover_url": ("cover_provider", "cover_provider_id"),
        "release_date": ("release_date_provider", "release_date_provider_id"),
    }
    provenance_fields_to_write = list(selected_fields)
    if can_reconcile_catalog_fields:
        existing_selected = {field for field, _, _ in provenance_fields_to_write}
        if "description" in provenance_refresh_fields and next_description and is_catalog_description and "description" not in existing_selected:
            provenance_fields_to_write.append(("description", result.get("description_provider"), result.get("description_provider_id")))
        if "genres" in provenance_refresh_fields and result_genres and "genres" not in existing_selected:
            provenance_fields_to_write.append(("genres", result.get("genres_provider"), result.get("genres_provider_id")))
        if "cover_url" in provenance_refresh_fields and next_cover and is_catalog_cover and "cover_url" not in existing_selected:
            provenance_fields_to_write.append(("cover_url", result.get("cover_provider"), result.get("cover_provider_id")))
        if "release_date" in provenance_refresh_fields and next_release_date and is_catalog_date and "release_date" not in existing_selected:
            provenance_fields_to_write.append(("release_date", result.get("release_date_provider"), result.get("release_date_provider_id")))

    for field, provider, provider_id in provenance_fields_to_write:
        provider_key, provider_id_key = field_provider_keys[field]
        persist_metadata_field_provenance(
            con,
            "candidate",
            candidate_id,
            field,
            result,
            default_provider=str(provider or result.get(provider_key) or ""),
            default_provider_id=str(provider_id or result.get(provider_id_key) or ""),
            default_confidence=float(result.get("quality_score", 0) or 0),
        )
    return any_candidate_change


def _dedupe_and_hide(con) -> int:
    """Reject read overlaps and active duplicates of saved/imported books."""

    changed = 0

    def reject_candidates(candidate_ids, flag):
        nonlocal changed
        ids = sorted(set(int(candidate_id) for candidate_id in candidate_ids))
        if not ids:
            return
        con.executemany(
            "UPDATE candidates SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE id=?",
            ((candidate_id,) for candidate_id in ids),
        )
        encoded_flag = json.dumps([flag])
        con.executemany(
            "UPDATE candidate_quality SET quality_status='rejected',flags_json=?,"
            "updated_at=CURRENT_TIMESTAMP WHERE candidate_id=?",
            ((encoded_flag, candidate_id) for candidate_id in ids),
        )
        changed += len(ids)

    active = con.execute(
        """SELECT c.id,c.title,c.author,c.isbn13,c.isbn10,
        q.work_id AS quality_work_id,q.provider AS quality_provider,
        q.isbn13 AS quality_isbn13,q.isbn10 AS quality_isbn10
        FROM candidates c
        JOIN candidate_quality q ON q.candidate_id=c.id
        WHERE c.status IN ('new','recommended')"""
    ).fetchall()
    read_index = ReadIdentityIndex.from_reads(
        con.execute(
            """SELECT r.title,r.author,r.isbn,r.openlibrary_work_id,
            m.verified_work_id AS quality_work_id,m.identity_provider AS quality_provider
            FROM reads r LEFT JOIN read_metadata m ON m.read_id=r.id"""
        ).fetchall()
    )
    overlap = [item for item in active if read_index.matches(item)]
    reject_candidates((item["id"] for item in overlap), "read_overlap")
    overlap_ids = {item["id"] for item in overlap}

    shortlisted_keys = book_identity_match_index(
        con.execute(
            """SELECT c.title,c.author,c.isbn13,c.isbn10,
            q.work_id AS quality_work_id,q.provider AS quality_provider,
            q.isbn13 AS quality_isbn13,q.isbn10 AS quality_isbn10
            FROM candidates c
            LEFT JOIN candidate_quality q ON q.candidate_id=c.id
            WHERE c.status IN ('saved','imported')"""
        ).fetchall()
    )
    shortlisted_overlap = [
        item for item in active
        if item["id"] not in overlap_ids
        and book_row_identity_match_keys(item) & shortlisted_keys
    ]
    reject_candidates((item["id"] for item in shortlisted_overlap), "shortlisted_overlap")

    accepted = con.execute(
        """SELECT c.id,c.title,c.author,c.description,c.cover_url,c.score,
        c.isbn13,c.isbn10,q.quality_score,q.work_id AS quality_work_id,
        q.provider AS quality_provider,q.isbn13 AS quality_isbn13,
        q.isbn10 AS quality_isbn10
        FROM candidates c JOIN candidate_quality q ON q.candidate_id=c.id
        WHERE c.status IN ('new','recommended') AND q.quality_status='accepted'"""
    ).fetchall()
    ordered = sorted(
        accepted,
        key=lambda item: (
            -float(item["quality_score"] or 0),
            -int(bool(item["description"])),
            -int(bool(item["cover_url"])),
            -float(item["score"] or 0),
            int(item["id"]),
        ),
    )
    seen_keys = set()
    duplicate_ids = []
    for item in ordered:
        keys = book_row_identity_match_keys(item)
        if keys & seen_keys:
            duplicate_ids.append(int(item["id"]))
            continue
        seen_keys.update(keys)
    reject_candidates(duplicate_ids, "duplicate_identity")
    return changed


def _update_running_audit_job_progress(progress: float) -> None:
    clamped = max(0.0, min(0.99, round(float(progress), 4)))
    with transaction() as con:
        con.execute(
            "UPDATE jobs SET progress=? "
            "WHERE status='running' AND kind IN ('candidate_quality','candidate_quality_recovery')",
            (clamped,),
        )


async def audit_candidates(
    *,
    only_pending: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
    count_only: bool = False,
    retry_quarantined: bool = False,
    quarantine_before: str | None = None,
    audited_before: str | None = None,
    max_duration_seconds: float | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Audit and enrich candidates, returning a deterministic quality report."""

    if count_only:
        due_total, breakdown = _due_candidate_quality_breakdown(
            include_quarantined=retry_quarantined,
            quarantine_before=quarantine_before,
            audited_before=audited_before,
        )
        if only_pending:
            effective_limit = (
                max(1, min(int(limit), QUALITY_AUDIT_BATCH_SIZE))
                if limit is not None
                else QUALITY_AUDIT_BATCH_SIZE
            )
            selected_count = min(due_total, effective_limit)
        else:
            selected_count = (
                min(due_total, max(1, int(limit))) if limit is not None else due_total
            )
        remaining_count = max(0, due_total - selected_count)
        return {
            "count_only": True,
            "dry_run": True,
            "only_pending": only_pending,
            "retry_quarantined": retry_quarantined,
            "due": due_total,
            "selected": selected_count,
            "remaining": remaining_count,
            "breakdown": breakdown,
            "audited": 0,
            "accepted": 0,
            "quarantine": 0,
            "rejected": 0,
            "deduped": 0,
            "enriched": 0,
            "quality_version": QUALITY_VERSION,
        }

    if only_pending or retry_quarantined:
        candidates = candidate_quality_audit_candidates(
            limit=limit if limit is not None else QUALITY_AUDIT_BATCH_SIZE,
            include_quarantined=retry_quarantined,
            quarantine_before=quarantine_before,
            audited_before=audited_before,
        )
    else:
        query = (
            "SELECT c.*,s.url AS source_root,q.quality_status AS _quality_status,"
            "q.audit_version AS _audit_version,q.flags_json AS _flags_json,"
            "q.provider AS _provider,q.provider_id AS _provider_id,"
            "q.metadata_provider AS _metadata_provider,"
            "q.metadata_provider_id AS _metadata_provider_id FROM candidates c "
            "LEFT JOIN sources s ON s.id=c.source_id "
            "LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
            "WHERE c.status!='rejected'"
        )
        params: list[object] = []
        if audited_before:
            query += " AND (q.audited_at IS NULL OR julianday(q.audited_at)<julianday(?))"
            params.append(audited_before)
        query += " ORDER BY c.id"
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(1, int(limit)))
        candidates = _attach_catalog_provenance_flags(rows(query, tuple(params)))

    if not candidates:
        if dry_run:
            return {
                "dry_run": True,
                "audited": 0,
                "accepted": 0,
                "quarantine": 0,
                "rejected": 0,
                "deduped": 0,
                "enriched": 0,
                "would_enrich": 0,
                "would_clear": 0,
                "remaining": 0,
                "timed_out": False,
                "elapsed_seconds": 0.0,
                "quality_version": QUALITY_VERSION,
            }
        with transaction() as con:
            deduped = _dedupe_and_hide(con)
        return {
            "audited": 0,
            "accepted": 0,
            "quarantine": 0,
            "rejected": 0,
            "deduped": deduped,
            "enriched": 0,
            "remaining": 0,
            "timed_out": False,
            "elapsed_seconds": 0.0,
            "quality_version": QUALITY_VERSION,
        }

    effective_max_duration: float | None
    if max_duration_seconds is not None:
        effective_max_duration = max(0.0, float(max_duration_seconds))
    elif only_pending or retry_quarantined:
        effective_max_duration = QUALITY_AUDIT_TIME_BUDGET_SECONDS
    else:
        effective_max_duration = None

    started_at = _audit_monotonic()
    semaphore = asyncio.Semaphore(CATALOG_CONCURRENCY)
    cache: dict[tuple[str, ...], asyncio.Task[dict[str, Any] | None]] = {}
    rate_limited_providers: set[str] = set()
    google_books_api_key = private_setting("association_google_books_api_key", "").strip()
    counts = {"accepted": 0, "quarantine": 0, "rejected": 0}
    enriched = 0
    would_enrich = 0
    would_clear = 0
    audited_count = 0
    timed_out = False
    total_selected = len(candidates)

    async with metadata_client() as client:
        async def run(candidate):
            async with semaphore:
                return await _audit_one(
                    candidate,
                    client,
                    cache,
                    google_books_api_key,
                    rate_limited_providers=rate_limited_providers,
                )

        for start in range(0, total_selected, QUALITY_AUDIT_SUBBATCH_SIZE):
            if (
                start > 0
                and effective_max_duration is not None
                and (_audit_monotonic() - started_at) >= effective_max_duration
            ):
                timed_out = True
                break
            chunk = candidates[start : start + QUALITY_AUDIT_SUBBATCH_SIZE]
            chunk_results = await asyncio.gather(*(run(candidate) for candidate in chunk))
            audited_count += len(chunk)
            if dry_run:
                with connect() as con:
                    for candidate, result in zip(chunk, chunk_results):
                        plan = _plan_store_result(con, candidate, result)
                        eff_status = plan["effective_status"]
                        counts[eff_status] = counts.get(eff_status, 0) + 1
                        if plan["any_candidate_change"]:
                            would_enrich += 1
                        if plan["cleared_fields"]:
                            would_clear += 1
            else:
                with transaction() as con:
                    for candidate, result in zip(chunk, chunk_results):
                        plan = _plan_store_result(con, candidate, result)
                        eff_status = plan["effective_status"]
                        counts[eff_status] = counts.get(eff_status, 0) + 1
                        if _store_result(con, candidate, result):
                            enriched += 1
                _update_running_audit_job_progress(audited_count / max(1, total_selected))

            elapsed_so_far = max(0.0, _audit_monotonic() - started_at)
            snapshot = {
                "audited": audited_count,
                "selected": total_selected,
                "accepted": counts["accepted"],
                "quarantine": counts["quarantine"],
                "rejected": counts["rejected"],
                "enriched": would_enrich if dry_run else enriched,
                "elapsed_seconds": round(elapsed_so_far, 3),
                "dry_run": dry_run,
            }
            if progress_callback is not None:
                progress_callback(snapshot)
            LOGGER.info(
                "Candidate quality audit progress: %d/%d audited (accepted=%d quarantine=%d rejected=%d elapsed=%.2fs dry_run=%s)",
                audited_count,
                total_selected,
                counts["accepted"],
                counts["quarantine"],
                counts["rejected"],
                elapsed_so_far,
                dry_run,
            )

    elapsed = round(max(0.0, _audit_monotonic() - started_at), 3)
    if dry_run:
        due_total = (
            count_candidate_quality_audit_candidates(
                include_quarantined=retry_quarantined,
                quarantine_before=quarantine_before,
                audited_before=audited_before,
            )
            if (only_pending or retry_quarantined)
            else total_selected
        )
        return {
            "dry_run": True,
            "audited": audited_count,
            **counts,
            "deduped": 0,
            "enriched": 0,
            "would_enrich": would_enrich,
            "would_clear": would_clear,
            "remaining": max(0, due_total - audited_count),
            "timed_out": timed_out,
            "elapsed_seconds": elapsed,
            "quality_version": QUALITY_VERSION,
        }

    with transaction() as con:
        deduped = _dedupe_and_hide(con)
    remaining = (
        count_candidate_quality_audit_candidates(
            include_quarantined=retry_quarantined,
            quarantine_before=quarantine_before,
            audited_before=audited_before,
        )
        if (only_pending or retry_quarantined)
        else 0
    )
    return {
        "audited": audited_count,
        **counts,
        "deduped": deduped,
        "enriched": enriched,
        "remaining": remaining,
        "timed_out": timed_out,
        "elapsed_seconds": elapsed,
        "quality_version": QUALITY_VERSION,
    }


def quality_summary() -> dict[str, Any]:
    summary = rows(
        "SELECT q.quality_status,COUNT(*) AS count FROM candidate_quality q "
        "GROUP BY q.quality_status ORDER BY q.quality_status"
    )
    identifiers = rows(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN isbn13!='' THEN 1 ELSE 0 END) AS with_isbn13, "
        "SUM(CASE WHEN provider!='' THEN 1 ELSE 0 END) AS with_catalog_match "
        "FROM candidate_quality"
    )[0]
    flag_counts: dict[str, int] = {}
    for item in rows("SELECT flags_json FROM candidate_quality"):
        try:
            flags = json.loads(item.get("flags_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            flags = []
        for flag in flags if isinstance(flags, list) else []:
            flag_counts[str(flag)] = flag_counts.get(str(flag), 0) + 1
    due_total, due_breakdown = _due_candidate_quality_breakdown()
    return {
        "statuses": summary,
        "identifiers": identifiers,
        "flags": dict(sorted(flag_counts.items(), key=lambda item: (-item[1], item[0]))),
        "due": due_total,
        "due_breakdown": due_breakdown,
        "quarantine_retry_days": CATALOG_QUARANTINE_RETRY_DAYS,
        "version": QUALITY_VERSION,
    }
