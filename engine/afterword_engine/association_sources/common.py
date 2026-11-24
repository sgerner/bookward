"""Shared text normalization and pinned HTTPS transport for association providers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import httpx

from ..associations import record_association_request
from ..http_bounds import stream_json_async
from ..identity import book_identity
from ..security import redact_url, resolve_public_target, sanitize_pinned_error_message


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
            unpinned_url = client.build_request("GET", original, params=params).url
            public_request = client.build_request("GET", redact_url(unpinned_url))
            try:
                response, payload = await stream_json_async(
                    client,
                    "GET",
                    pinned,
                    params=params,
                    headers={"Host": host_header},
                    extensions={"sni_hostname": hostname},
                    max_bytes=max_bytes,
                    content_type_error=f"{source_label} returned an unsupported content type",
                    size_error=f"{source_label} response is too large",
                    public_request=public_request,
                )
            except httpx.RequestError as exc:
                sanitized = sanitize_pinned_error_message(
                    exc,
                    address=address,
                    hostname=hostname,
                )
                try:
                    rewritten = type(exc)(sanitized, request=public_request)
                except TypeError:
                    rewritten = httpx.RequestError(sanitized, request=public_request)
                raise rewritten from None
            if require_mapping and not isinstance(payload, Mapping):
                raise ValueError(f"{source_label} response must be an object")
            if on_response is not None:
                on_response(response)
    except Exception:
        record_association_request(provider, request_key, "error")
        raise
    record_association_request(provider, request_key, "ok")
    return payload, response.headers
