"""Shared text normalization and pinned HTTPS transport for association providers."""

from __future__ import annotations

from typing import Any, Callable, Mapping

import httpx

from ..associations import record_association_request
from ..identity import book_identity
from ..security import resolve_public_target


DEFAULT_USER_AGENT = "Bookward/0.1 (+self-hosted book recommender)"


def clean(value: object, limit: int = 500) -> str:
    """Collapse provider text whitespace and enforce a field length limit."""

    return " ".join(str(value or "").split()).strip()[:limit]


def title_key(value: object) -> str:
    """Return the normalized title component of a book identity."""

    return book_identity(value, "").split("\x1f", 1)[0]


async def fetch_pinned_association_json(
    url: httpx.URL | str,
    *,
    provider: str,
    request_key: str,
    source_label: str,
    params: Mapping[str, Any] | None = None,
    timeout_seconds: float,
    max_bytes: int,
    user_agent: str = DEFAULT_USER_AGENT,
    require_mapping: bool = True,
    on_response: Callable[[httpx.Response], None] | None = None,
) -> tuple[Any, httpx.Headers]:
    """Fetch JSON over SSRF-pinned HTTPS and record association request telemetry."""

    original = httpx.URL(url)
    _, address, hostname = resolve_public_target(str(original))
    pinned = original.copy_with(host=address)
    host_header = hostname if original.port in (None, 443) else f"{hostname}:{original.port}"
    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": user_agent},
        ) as client:
            response = await client.request(
                "GET",
                pinned,
                params=params,
                headers={"Host": host_header},
                extensions={"sni_hostname": hostname},
            )
            response.raise_for_status()
            if len(response.content) > max_bytes:
                raise ValueError(f"{source_label} response is too large")
            payload = response.json()
            if require_mapping and not isinstance(payload, Mapping):
                raise ValueError(f"{source_label} response must be an object")
            if on_response is not None:
                on_response(response)
    except Exception:
        record_association_request(provider, request_key, "error")
        raise
    record_association_request(provider, request_key, "ok")
    return payload, response.headers
