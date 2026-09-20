"""Creation and verification helpers for Bookward API tokens.

The token value is intentionally only available at creation time.  The
database keeps a one-way digest and a short prefix that is safe to display in
the Settings screen when a user needs to identify a token later.
"""

import hashlib
import secrets


TOKEN_PREFIX = "bkw_"


def generate_api_token() -> str:
    """Return a high-entropy token suitable for the Authorization header."""

    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_api_token(token: str) -> str:
    """Hash a token for storage and lookup without retaining the secret."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    """Return the stable, non-secret identifier shown in token listings."""

    return token[:12]
