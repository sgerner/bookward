"""Small, dependency-free helpers for validating and recovering ISBNs."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse


def canonical_isbn(value: object) -> str:
    """Return a checksum-valid ISBN-10/13, or an empty string."""

    raw = re.sub(r"[^0-9Xx]", "", str(value or ""))
    if len(raw) == 13 and raw.isdigit():
        total = sum(
            (1 if index % 2 == 0 else 3) * int(char)
            for index, char in enumerate(raw)
        )
        return raw if total % 10 == 0 else ""
    if len(raw) == 10 and re.fullmatch(r"[0-9]{9}[0-9Xx]", raw):
        total = sum(
            (10 - index) * (10 if char.upper() == "X" else int(char))
            for index, char in enumerate(raw)
        )
        if total % 11 != 0:
            return ""
        prefix = "978" + raw[:9]
        check = (
            10
            - sum(
                (1 if index % 2 == 0 else 3) * int(char)
                for index, char in enumerate(prefix)
            )
            % 10
        ) % 10
        return prefix + str(check)
    return ""


def isbn_parts(*values: object) -> tuple[str, str]:
    """Return the first valid ISBN-13 and ISBN-10 represented by ``values``.

    ISBN-10 values also supply their equivalent ISBN-13 so callers can use a
    single stable lookup key while retaining the source's original edition
    identifier.
    """

    isbn13 = ""
    isbn10 = ""
    pending = list(values)
    while pending:
        value = pending.pop(0)
        if isinstance(value, (list, tuple, set)):
            pending[0:0] = list(value)
            continue
        raw = re.sub(r"[^0-9Xx]", "", str(value or ""))
        if len(raw) == 10 and not isbn10 and canonical_isbn(raw):
            isbn10 = raw.upper()
            isbn13 = isbn13 or canonical_isbn(raw)
        elif len(raw) == 13 and not isbn13 and canonical_isbn(raw):
            isbn13 = raw
    return isbn13, isbn10


_AMAZON_PRODUCT_RE = re.compile(
    r"/(?:dp|gp/(?:product|aw/d|aw/dp))/([^/?#]+)",
    re.IGNORECASE,
)


def isbn_parts_from_amazon_url(value: object) -> tuple[str, str]:
    """Recover a checksum-valid ISBN from a standard Amazon product URL."""

    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return "", ""
    host = (parsed.hostname or "").casefold().rstrip(".")
    # Amazon's regional domains all contain an ``amazon`` label.  Restrict
    # extraction to those hosts so an unrelated URL cannot turn an arbitrary
    # path segment into source identity evidence.
    if "amazon" not in host.split("."):
        return "", ""
    path = unquote(parsed.path)
    for match in _AMAZON_PRODUCT_RE.finditer(path):
        isbn13, isbn10 = isbn_parts(match.group(1))
        if isbn13 or isbn10:
            return isbn13, isbn10
    query = parse_qs(parsed.query, keep_blank_values=False)
    for key, values in query.items():
        if key.casefold() not in {"isbn", "isbn13", "isbn10", "asin"}:
            continue
        for value in values:
            isbn13, isbn10 = isbn_parts(value)
            if isbn13 or isbn10:
                return isbn13, isbn10
    return "", ""


def isbn_parts_from_source(values: object = (), source_url: object = "") -> tuple[str, str]:
    """Normalize payload ISBN fields, falling back to an Amazon URL."""

    isbn13, isbn10 = isbn_parts(values)
    url_isbn13, url_isbn10 = isbn_parts_from_amazon_url(source_url)
    return isbn13 or url_isbn13, isbn10 or url_isbn10
