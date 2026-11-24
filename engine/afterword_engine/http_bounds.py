"""Shared streaming HTTP helpers with header and decompressed-byte bounds."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
import json
from typing import Any

import httpx

SOURCE_CONTENT_TYPES: tuple[str, ...] = (
    "html",
    "xml",
    "rss",
    "atom",
    "json",
    "javascript",
    "text/plain",
)
JSON_CONTENT_TYPES: tuple[str, ...] = (
    "json",
    "javascript",
    "text/plain",
)

_OMIT = object()


class ResponseBoundsError(ValueError):
    """Base error raised when an upstream response violates header or byte bounds."""


class ResponseTooLargeError(ResponseBoundsError):
    """Raised when response headers or decompressed stream bytes exceed max_bytes."""


class UnsupportedContentTypeError(ResponseBoundsError):
    """Raised when a response content-type header is outside allowed_content_types."""


class InvalidContentLengthError(ResponseBoundsError):
    """Raised when a response sends a negative or malformed Content-Length header."""


def _declared_content_lengths(headers: Any) -> list[str]:
    if headers is None:
        return []
    get_list = getattr(headers, "get_list", None)
    if callable(get_list):
        return [str(value) for value in get_list("content-length") if str(value).strip()]
    raw = headers.get("content-length")
    if raw is None or not str(raw).strip():
        return []
    return [str(raw)]


def validate_response_headers(
    response: Any,
    *,
    max_bytes: int,
    allowed_content_types: Sequence[str] | None = None,
    allow_missing_content_type: bool = False,
    content_type_error: str = "Response returned an unsupported content type",
    size_error: str = "Response is too large",
) -> str:
    """Validate Content-Type and Content-Length headers before reading body chunks."""

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    headers = getattr(response, "headers", None)
    raw_content_type = headers.get("content-type", "") if headers is not None else ""
    content_type = str(raw_content_type or "").lower()

    if allowed_content_types is not None:
        if not content_type:
            if not allow_missing_content_type:
                raise UnsupportedContentTypeError(content_type_error)
        elif not any(kind in content_type for kind in allowed_content_types):
            raise UnsupportedContentTypeError(content_type_error)

    for raw_length in _declared_content_lengths(headers):
        for part in raw_length.split(","):
            token = part.strip()
            if not token:
                continue
            try:
                declared = int(token)
            except ValueError as exc:
                raise InvalidContentLengthError("Invalid Content-Length header") from exc
            if declared < 0:
                raise InvalidContentLengthError("Invalid Content-Length header")
            if declared > max_bytes:
                raise ResponseTooLargeError(size_error)

    return content_type


async def read_bounded_bytes_async(
    response: Any,
    *,
    max_bytes: int,
    allowed_content_types: Sequence[str] | None = None,
    allow_missing_content_type: bool = False,
    content_type_error: str = "Response returned an unsupported content type",
    size_error: str = "Response is too large",
) -> bytes:
    """Validate headers and accumulate decompressed chunks up to max_bytes."""

    validate_response_headers(
        response,
        max_bytes=max_bytes,
        allowed_content_types=allowed_content_types,
        allow_missing_content_type=allow_missing_content_type,
        content_type_error=content_type_error,
        size_error=size_error,
    )
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        size += len(chunk)
        if size > max_bytes:
            raise ResponseTooLargeError(size_error)
        chunks.append(chunk)
    return b"".join(chunks)


def read_bounded_bytes_sync(
    response: Any,
    *,
    max_bytes: int,
    allowed_content_types: Sequence[str] | None = None,
    allow_missing_content_type: bool = False,
    content_type_error: str = "Response returned an unsupported content type",
    size_error: str = "Response is too large",
) -> bytes:
    """Synchronous variant of read_bounded_bytes_async using iter_bytes()."""

    validate_response_headers(
        response,
        max_bytes=max_bytes,
        allowed_content_types=allowed_content_types,
        allow_missing_content_type=allow_missing_content_type,
        content_type_error=content_type_error,
        size_error=size_error,
    )
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        size += len(chunk)
        if size > max_bytes:
            raise ResponseTooLargeError(size_error)
        chunks.append(chunk)
    return b"".join(chunks)


async def read_bounded_json_async(
    response: Any,
    *,
    max_bytes: int,
    allowed_content_types: Sequence[str] | None = JSON_CONTENT_TYPES,
    allow_missing_content_type: bool = True,
    content_type_error: str = "Response returned an unsupported content type",
    size_error: str = "Response is too large",
) -> Any:
    """Read a bounded async response stream and parse JSON."""

    body = await read_bounded_bytes_async(
        response,
        max_bytes=max_bytes,
        allowed_content_types=allowed_content_types,
        allow_missing_content_type=allow_missing_content_type,
        content_type_error=content_type_error,
        size_error=size_error,
    )
    return json.loads(body)


def read_bounded_json_sync(
    response: Any,
    *,
    max_bytes: int,
    allowed_content_types: Sequence[str] | None = JSON_CONTENT_TYPES,
    allow_missing_content_type: bool = True,
    content_type_error: str = "Response returned an unsupported content type",
    size_error: str = "Response is too large",
) -> Any:
    """Read a bounded sync response stream and parse JSON."""

    body = read_bounded_bytes_sync(
        response,
        max_bytes=max_bytes,
        allowed_content_types=allowed_content_types,
        allow_missing_content_type=allow_missing_content_type,
        content_type_error=content_type_error,
        size_error=size_error,
    )
    return json.loads(body)


async def iter_bounded_bytes_async(
    response: Any,
    *,
    max_bytes: int,
    allowed_content_types: Sequence[str] | None = None,
    allow_missing_content_type: bool = True,
    content_type_error: str = "Response returned an unsupported content type",
    size_error: str = "Stream response is too large",
) -> AsyncIterator[bytes]:
    """Yield decompressed chunks from an open stream up to max_bytes."""

    validate_response_headers(
        response,
        max_bytes=max_bytes,
        allowed_content_types=allowed_content_types,
        allow_missing_content_type=allow_missing_content_type,
        content_type_error=content_type_error,
        size_error=size_error,
    )
    size = 0
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        size += len(chunk)
        if size > max_bytes:
            raise ResponseTooLargeError(size_error)
        yield chunk


async def stream_bytes_async(
    client: httpx.AsyncClient,
    method: str,
    url: httpx.URL | str,
    *,
    max_bytes: int,
    allowed_content_types: Sequence[str] | None = None,
    allow_missing_content_type: bool = False,
    content_type_error: str = "Response returned an unsupported content type",
    size_error: str = "Response is too large",
    raise_for_status: bool = True,
    auth: Any = _OMIT,
    follow_redirects: Any = _OMIT,
    **request_kwargs: Any,
) -> tuple[Any, bytes]:
    """Build and send an async streaming request, always closing in finally."""

    request = client.build_request(method, url, **request_kwargs)
    send_kwargs: dict[str, Any] = {"stream": True}
    if auth is not _OMIT:
        send_kwargs["auth"] = auth
    if follow_redirects is not _OMIT:
        send_kwargs["follow_redirects"] = follow_redirects
    response = await client.send(request, **send_kwargs)
    try:
        if raise_for_status:
            response.raise_for_status()
        elif getattr(response, "is_error", False):
            return response, b""
        body = await read_bounded_bytes_async(
            response,
            max_bytes=max_bytes,
            allowed_content_types=allowed_content_types,
            allow_missing_content_type=allow_missing_content_type,
            content_type_error=content_type_error,
            size_error=size_error,
        )
        return response, body
    finally:
        await response.aclose()


def stream_bytes_sync(
    client: httpx.Client,
    method: str,
    url: httpx.URL | str,
    *,
    max_bytes: int,
    allowed_content_types: Sequence[str] | None = None,
    allow_missing_content_type: bool = False,
    content_type_error: str = "Response returned an unsupported content type",
    size_error: str = "Response is too large",
    raise_for_status: bool = True,
    auth: Any = _OMIT,
    follow_redirects: Any = _OMIT,
    **request_kwargs: Any,
) -> tuple[Any, bytes]:
    """Build and send a sync streaming request, always closing in finally."""

    request = client.build_request(method, url, **request_kwargs)
    send_kwargs: dict[str, Any] = {"stream": True}
    if auth is not _OMIT:
        send_kwargs["auth"] = auth
    if follow_redirects is not _OMIT:
        send_kwargs["follow_redirects"] = follow_redirects
    response = client.send(request, **send_kwargs)
    try:
        if raise_for_status:
            response.raise_for_status()
        elif getattr(response, "is_error", False):
            return response, b""
        body = read_bounded_bytes_sync(
            response,
            max_bytes=max_bytes,
            allowed_content_types=allowed_content_types,
            allow_missing_content_type=allow_missing_content_type,
            content_type_error=content_type_error,
            size_error=size_error,
        )
        return response, body
    finally:
        response.close()


async def stream_json_async(
    client: httpx.AsyncClient,
    method: str,
    url: httpx.URL | str,
    *,
    max_bytes: int,
    allowed_content_types: Sequence[str] | None = JSON_CONTENT_TYPES,
    allow_missing_content_type: bool = True,
    content_type_error: str = "Response returned an unsupported content type",
    size_error: str = "Response is too large",
    auth: Any = _OMIT,
    follow_redirects: Any = _OMIT,
    **request_kwargs: Any,
) -> tuple[Any, Any]:
    """Stream an async response with byte bounds and decode JSON."""

    response, body = await stream_bytes_async(
        client,
        method,
        url,
        max_bytes=max_bytes,
        allowed_content_types=allowed_content_types,
        allow_missing_content_type=allow_missing_content_type,
        content_type_error=content_type_error,
        size_error=size_error,
        raise_for_status=True,
        auth=auth,
        follow_redirects=follow_redirects,
        **request_kwargs,
    )
    return response, json.loads(body)


def stream_json_sync(
    client: httpx.Client,
    method: str,
    url: httpx.URL | str,
    *,
    max_bytes: int,
    allowed_content_types: Sequence[str] | None = JSON_CONTENT_TYPES,
    allow_missing_content_type: bool = True,
    content_type_error: str = "Response returned an unsupported content type",
    size_error: str = "Response is too large",
    auth: Any = _OMIT,
    follow_redirects: Any = _OMIT,
    **request_kwargs: Any,
) -> tuple[Any, Any]:
    """Stream a sync response with byte bounds and decode JSON."""

    response, body = stream_bytes_sync(
        client,
        method,
        url,
        max_bytes=max_bytes,
        allowed_content_types=allowed_content_types,
        allow_missing_content_type=allow_missing_content_type,
        content_type_error=content_type_error,
        size_error=size_error,
        raise_for_status=True,
        auth=auth,
        follow_redirects=follow_redirects,
        **request_kwargs,
    )
    return response, json.loads(body)


_DECODED_BODY_STRIP_HEADERS = frozenset(
    {"content-encoding", "transfer-encoding", "content-length"}
)


def materialize_response(response: Any, body: bytes) -> httpx.Response:
    """Build a closed, decoded httpx.Response using only public httpx APIs.

    ``aiter_bytes()`` and ``iter_bytes()`` already decode transport compression,
    so ``Content-Encoding``, ``Transfer-Encoding``, and wire ``Content-Length``
    headers are omitted before passing ``content=body`` to ``httpx.Response`` to
    prevent double-decompression while avoiding private ``response._content``
    writes.
    """

    raw_headers = getattr(response, "headers", None)
    if isinstance(raw_headers, httpx.Headers):
        header_items = raw_headers.multi_items()
    elif isinstance(raw_headers, Mapping):
        header_items = list(raw_headers.items())
    else:
        header_items = []
    filtered_headers = httpx.Headers(
        [
            (key, value)
            for key, value in header_items
            if key.lower() not in _DECODED_BODY_STRIP_HEADERS
        ]
    )
    kwargs: dict[str, Any] = {
        "headers": filtered_headers,
        "content": body,
    }
    extensions = getattr(response, "extensions", None)
    if isinstance(extensions, Mapping):
        kwargs["extensions"] = dict(extensions)
    try:
        request = response.request
    except RuntimeError:
        request = None
    if isinstance(request, httpx.Request):
        kwargs["request"] = request
    return httpx.Response(int(response.status_code), **kwargs)


async def stream_response_async(
    client: httpx.AsyncClient,
    method: str,
    url: httpx.URL | str,
    *,
    max_bytes: int,
    allowed_content_types: Sequence[str] | None = None,
    allow_missing_content_type: bool = True,
    content_type_error: str = "Response returned an unsupported content type",
    size_error: str = "Response is too large",
    raise_for_status: bool = False,
    auth: Any = _OMIT,
    follow_redirects: Any = _OMIT,
    **request_kwargs: Any,
) -> httpx.Response:
    """Stream a bounded response, close the wire stream, and return a decoded httpx.Response."""

    response, body = await stream_bytes_async(
        client,
        method,
        url,
        max_bytes=max_bytes,
        allowed_content_types=allowed_content_types,
        allow_missing_content_type=allow_missing_content_type,
        content_type_error=content_type_error,
        size_error=size_error,
        raise_for_status=raise_for_status,
        auth=auth,
        follow_redirects=follow_redirects,
        **request_kwargs,
    )
    return materialize_response(response, body)
