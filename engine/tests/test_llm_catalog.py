import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from afterword_engine.config import settings
from afterword_engine.llm_catalog import get_catalog, normalize_catalog


CATALOG = {
    "openai": {
        "name": "OpenAI", "npm": "@ai-sdk/openai", "models": {
            "gpt-good": {"name": "Good", "modalities": {"input": ["text"], "output": ["text"]}, "structured_output": True, "limit": {"context": 1000}, "cost": {"input": 1, "output": 2}},
            "audio": {"name": "Audio", "modalities": {"input": ["audio"], "output": ["audio"]}},
            "old": {"name": "Old", "status": "deprecated", "modalities": {"input": ["text"], "output": ["text"]}},
        }
    }
}


def test_normalize_catalog_keeps_text_models_and_safe_fields():
    result = normalize_catalog(CATALOG)
    assert result[0]["id"] == "openai"
    assert [model["id"] for model in result[0]["models"]] == ["gpt-good"]
    assert result[0]["models"][0]["structured_output"] is True


def test_normalize_catalog_rejects_non_object():
    with pytest.raises(ValueError):
        normalize_catalog([])


@respx.mock
def test_catalog_fetch_uses_etag_and_stale_fallback(tmp_path: Path):
    previous = (settings.models_catalog_cache, settings.models_catalog_url)
    settings.models_catalog_cache = str(tmp_path / "catalog.json")
    settings.models_catalog_url = "https://models.dev/api.json?type=all"
    try:
        route = respx.get(settings.models_catalog_url).mock(return_value=httpx.Response(200, json=CATALOG, headers={"etag": '"v1"'}))
        first = asyncio.run(get_catalog(force=True))
        assert first["stale"] is False and route.called
        route.mock(return_value=httpx.Response(503))
        second = asyncio.run(get_catalog(force=True))
        assert second["stale"] is True
        assert json.loads(Path(settings.models_catalog_cache).read_text())["etag"] == '"v1"'
    finally:
        settings.models_catalog_cache, settings.models_catalog_url = previous


@respx.mock
def test_catalog_unavailable_without_cache(tmp_path: Path):
    previous = (settings.models_catalog_cache, settings.models_catalog_url)
    settings.models_catalog_cache = str(tmp_path / "missing.json")
    settings.models_catalog_url = "https://models.dev/api.json?type=all"
    try:
        respx.get(settings.models_catalog_url).mock(return_value=httpx.Response(503))
        with pytest.raises(ValueError, match="temporarily unavailable"):
            asyncio.run(get_catalog(force=True))
    finally:
        settings.models_catalog_cache, settings.models_catalog_url = previous
