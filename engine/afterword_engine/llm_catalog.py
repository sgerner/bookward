"""Bounded, cached access to the Models.dev provider catalog."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import settings

MAX_CATALOG_BYTES = 12_000_000


def _model(model_id: str, raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        return None
    modalities = raw.get("modalities") if isinstance(raw.get("modalities"), dict) else {}
    inputs, outputs = modalities.get("input", []), modalities.get("output", [])
    if "text" not in inputs or "text" not in outputs or raw.get("status") == "deprecated":
        return None
    limit = raw.get("limit") if isinstance(raw.get("limit"), dict) else {}
    cost = raw.get("cost") if isinstance(raw.get("cost"), dict) else {}
    return {
        "id": str(raw.get("id") or model_id)[:300],
        "name": raw["name"][:200],
        "family": str(raw.get("family") or "")[:100],
        "reasoning": bool(raw.get("reasoning")),
        "structured_output": bool(raw.get("structured_output")),
        "tool_call": bool(raw.get("tool_call")),
        "context": int(limit.get("context") or 0),
        "output": int(limit.get("output") or 0),
        "input_cost": float(cost["input"]) if isinstance(cost.get("input"), (int, float)) else None,
        "output_cost": float(cost["output"]) if isinstance(cost.get("output"), (int, float)) else None,
    }


def normalize_catalog(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ValueError("Models.dev catalog must be an object")
    providers = []
    for provider_id, provider in raw.items():
        if not isinstance(provider_id, str) or not isinstance(provider, dict):
            continue
        models = [item for key, value in (provider.get("models") or {}).items() if (item := _model(str(key), value))]
        if not models:
            continue
        api = provider.get("api")
        providers.append({
            "id": provider_id[:100],
            "name": str(provider.get("name") or provider_id)[:200],
            "api": api[:500] if isinstance(api, str) and api.startswith("https://") else "",
            "npm": str(provider.get("npm") or "")[:200],
            "models": sorted(models, key=lambda item: item["name"].casefold()),
        })
    return sorted(providers, key=lambda item: item["name"].casefold())


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
        return payload if isinstance(payload, dict) and isinstance(payload.get("catalog"), dict) else None
    except (OSError, ValueError):
        return None


async def get_catalog(*, force: bool = False) -> dict[str, Any]:
    path = Path(settings.models_catalog_cache)
    cached = _read_cache(path)
    now = datetime.now(timezone.utc)
    if cached and not force:
        fetched = datetime.fromisoformat(str(cached.get("fetched_at", "")).replace("Z", "+00:00"))
        if now - fetched < timedelta(hours=max(1, settings.models_catalog_ttl_hours)):
            return {"providers": normalize_catalog(cached["catalog"]), "fetched_at": cached["fetched_at"], "stale": False}
    headers = {"Accept": "application/json", "User-Agent": "Bookward/0.1"}
    if cached.get("etag") if cached else None:
        headers["If-None-Match"] = cached["etag"]
    try:
        async with httpx.AsyncClient(timeout=settings.source_timeout_seconds, follow_redirects=False, trust_env=False) as client:
            response = await client.get(settings.models_catalog_url, headers=headers)
        if response.status_code == 304 and cached:
            cached["fetched_at"] = now.isoformat()
        else:
            response.raise_for_status()
            if len(response.content) > MAX_CATALOG_BYTES:
                raise ValueError("Models.dev catalog is too large")
            catalog = response.json()
            normalize_catalog(catalog)
            cached = {"fetched_at": now.isoformat(), "etag": response.headers.get("etag", ""), "catalog": catalog}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cached, separators=(",", ":")))
        path.chmod(0o600)
        return {"providers": normalize_catalog(cached["catalog"]), "fetched_at": cached["fetched_at"], "stale": False}
    except (httpx.HTTPError, OSError, ValueError, json.JSONDecodeError):
        if cached:
            return {"providers": normalize_catalog(cached["catalog"]), "fetched_at": cached["fetched_at"], "stale": True}
        raise ValueError("Models.dev catalog is temporarily unavailable")
