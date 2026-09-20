"""Creation and verification helpers for Bookward API tokens.

The token value is intentionally only available at creation time.  The
database keeps a one-way digest and a short prefix that is safe to display in
the Settings screen when a user needs to identify a token later.
"""

import hashlib
import secrets
from .secrets import installation_key


TOKEN_PREFIX = "bkw_"
TOKEN_HASH_SALT = b"bookward-api-token-v1:"
TOKEN_HASH_ITERATIONS = 600_000


def generate_api_token() -> str:
    """Return a high-entropy token suitable for the Authorization header."""

    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_api_token(token: str) -> str:
    """Derive a slow, installation-bound digest without retaining the token."""

    return hashlib.pbkdf2_hmac(
        "sha256",
        token.encode("utf-8"),
        TOKEN_HASH_SALT + installation_key(),
        TOKEN_HASH_ITERATIONS,
    ).hex()


def legacy_hash_api_token(token: str) -> str:
    """Return the SHA-256 digest used before installation-bound hashing."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    """Return the stable, non-secret identifier shown in token listings."""

    return token[:12]
