"""Small, defensive client for the optional Librarr integration.

The engine owns the Librarr API key, so the browser never talks to Librarr
directly.  Keeping the client here also makes the media-type behaviour shared
by wishlist imports and the interactive search flow.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .security import validate_service_url

MEDIA_TYPES = frozenset({"ebook", "audiobook"})


def normalize_media_type(value: str | None) -> str:
    media_type = str(value or "audiobook").strip().lower()
    if media_type not in MEDIA_TYPES:
        raise ValueError("Librarr media type must be ebook or audiobook")
    return media_type


def _service(config: dict[str, Any]) -> tuple[str, dict[str, str]]:
    base_url = str(config.get("librarr_url", "")).strip().rstrip("/")
    api_key = str(config.get("librarr_api_key", "")).strip()
    if not base_url or not api_key:
        raise ValueError("Connect Librarr first")
    allowed_hosts = {
        host.strip().lower()
        for host in str(config.get("librarr_allowed_hosts", "")).split(",")
        if host.strip()
    }
    validate_service_url(base_url, allowed_hosts)
    return base_url, {"X-Api-Key": api_key}


def _results(payload: Any) -> list[dict[str, Any]]:
    """Extract a bounded list from the response shapes used by Librarr versions."""

    values: Any = payload
    if isinstance(payload, dict):
        for key in ("results", "items", "books", "data"):
            if key in payload:
                values = payload[key]
                break
    if not isinstance(values, list):
        return []
    # Do not reflect arbitrary nested payloads into the browser.  A generous
    # cap still keeps a broken/hostile service from making the page enormous.
    output: list[dict[str, Any]] = []
    for value in values[:50]:
        if not isinstance(value, dict):
            continue
        try:
            if len(json.dumps(value, separators=(",", ":"))) > 25_000:
                continue
        except (TypeError, ValueError):
            continue
        output.append(value)
    return output


async def search(config: dict[str, Any], query: str, media_type: str) -> dict[str, Any]:
    media = normalize_media_type(media_type)
    clean_query = str(query).strip()
    if len(clean_query) < 2 or len(clean_query) > 200:
        raise ValueError("Search must be between 2 and 200 characters")
    base_url, headers = _service(config)
    endpoint = "/api/search/audiobooks" if media == "audiobook" else "/api/search"
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        response = await client.get(f"{base_url}{endpoint}", params={"q": clean_query}, headers=headers)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("Librarr returned an invalid search response") from exc
    return {"results": _results(payload), "media_type": media}


async def download(config: dict[str, Any], result: dict[str, Any], media_type: str, idempotency_key: str) -> dict[str, Any]:
    media = normalize_media_type(media_type)
    if not result:
        raise ValueError("Choose a Librarr search result first")
    # Validate and bound the forwarded object before it reaches the service.
    try:
        encoded = json.dumps(result, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Librarr search result is not valid JSON") from exc
    if len(encoded) > 100_000:
        raise ValueError("Librarr search result is too large")
    base_url, headers = _service(config)
    endpoint = "/api/download/audiobook" if media == "audiobook" else "/api/download"
    request_headers = {**headers, "Idempotency-Key": idempotency_key}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        response = await client.post(f"{base_url}{endpoint}", headers=request_headers, json=result)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            payload = {}
    return payload if isinstance(payload, dict) else {"result": payload}
