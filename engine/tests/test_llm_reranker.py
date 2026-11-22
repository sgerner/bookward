import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afterword_engine.llm_reranker import (
    apply_llm_rerank_adjustments,
    apply_llm_rerank_order,
    build_rerank_prompt,
    rerank_with_llm_sync,
)


def test_build_rerank_prompt_grounds_only_input_candidates() -> None:
    reads = [
        {"title": "Dune", "author": "Frank Herbert", "rating": 5.0},
        {"title": "Bad Book", "author": "Some Author", "rating": 1.0},
    ]
    candidates = [
        {"id": 10, "title": "Hyperion", "author": "Dan Simmons", "genres": ["Sci-Fi"], "score": 82.0},
        {"id": 11, "title": "Foundation", "author": "Isaac Asimov", "genres": ["Sci-Fi"], "score": 80.0},
    ]
    messages = build_rerank_prompt(reads, candidates, top_k=24)
    assert len(messages) == 2
    assert "Hyperion" in messages[1]["content"]
    assert "Dune" in messages[1]["content"]


def test_build_rerank_prompt_zero_data_leakage() -> None:
    reads = [{"title": "Dune", "author": "Frank Herbert", "rating": 5.0}]
    candidates = [
        {"id": 10, "title": "Hyperion", "author": "Dan Simmons", "genres": ["Sci-Fi"], "score": 82.0},
        {"id": 11, "title": "Foundation", "author": "Isaac Asimov", "genres": ["Sci-Fi"], "score": 80.0},
    ]
    interaction_events = [
        # Candidate 10 is in the slate -> MUST be excluded to avoid leakage!
        {"candidate_id": 10, "title": "Hyperion", "author": "Dan Simmons", "event_type": "save"},
        # Candidate 99 is NOT in the slate -> should be included as history
        {"candidate_id": 99, "title": "Neuromancer", "author": "William Gibson", "event_type": "save"},
        # Candidate 11 is in the slate -> MUST be excluded!
        {"candidate_id": 11, "title": "Foundation", "author": "Isaac Asimov", "event_type": "reject"},
        # Candidate 100 is NOT in the slate -> should be included
        {"candidate_id": 100, "title": "Generic Romance", "author": "Anon", "event_type": "reject"},
    ]
    messages = build_rerank_prompt(reads, candidates, interaction_events=interaction_events, top_k=24)
    payload = json.loads(messages[1]["content"])

    saved_titles = [b["title"] for b in payload["recent_discovery_saved_books"]]
    rejected_titles = [b["title"] for b in payload["recent_discovery_rejected_books"]]

    # Candidate 99 & 100 are present
    assert "Neuromancer" in saved_titles
    assert "Generic Romance" in rejected_titles

    # Candidate 10 & 11 are strictly excluded from history
    assert "Hyperion" not in saved_titles
    assert "Foundation" not in rejected_titles


def test_apply_llm_rerank_adjustments_bounds_deltas_and_sorts() -> None:
    candidates = [
        {"id": 10, "title": "First", "score": 80.0, "explanation": []},
        {"id": 11, "title": "Second", "score": 79.5, "explanation": []},
        {"id": 12, "title": "Third", "score": 75.0, "explanation": []},
    ]
    adjustments = [
        {"id": 999, "delta": 5.0, "reason": "Hallucinated"},
        {"id": 11, "delta": 1.0, "reason": "Contemporary speculative match"},
        {"id": 10, "delta": -1.5, "reason": "Older classic canon"},
    ]
    reranked, diag = apply_llm_rerank_adjustments(candidates, adjustments, max_delta=1.2)
    assert diag["applied"] is True
    assert diag["adjusted_count"] == 2

    # id 11 was 79.5 + 1.0 = 80.5 (now rank 1)
    # id 10 was 80.0 - 1.2 (clipped from -1.5) = 78.8 (now rank 2)
    # id 12 was 75.0 + 0 = 75.0 (rank 3)
    assert [item["id"] for item in reranked] == [11, 10, 12]
    assert reranked[0]["score"] == 80.5
    assert reranked[1]["score"] == 78.8
    assert "LLM delta (+1.00): Contemporary speculative match" in reranked[0]["explanation"]
    assert "LLM delta (-1.20): Older classic canon" in reranked[1]["explanation"]


def test_apply_llm_rerank_order_backward_compatibility() -> None:
    candidates = [
        {"id": 10, "title": "First", "score": 80.0, "explanation": []},
        {"id": 11, "title": "Second", "score": 78.0, "explanation": []},
    ]
    reranked, diag = apply_llm_rerank_order(
        candidates,
        [
            {"id": 999, "reason": "Hallucinated"},
            {"id": 11, "reason": "Strong thematic fit"},
            {"id": 10, "reason": "Good secondary pick"},
        ],
        top_k=24,
        max_nudge=4.0,
    )
    assert diag["applied"] is True
    assert [item["id"] for item in reranked] == [11, 10]
    assert reranked[0]["score"] == 82.0
    assert reranked[1]["score"] == 76.0
    assert "LLM rerank: Strong thematic fit" in reranked[0]["explanation"]


def test_rerank_with_llm_sync_falls_back_when_unconfigured() -> None:
    candidates = [{"id": 1, "score": 75.0}, {"id": 2, "score": 70.0}]
    res, diag = rerank_with_llm_sync([], candidates, api_key="")
    assert diag == {"applied": False, "reason": "not_configured"}
    assert [item["id"] for item in res] == [1, 2]


def test_normalize_chat_url() -> None:
    from afterword_engine.llm_reranker import _normalize_chat_url

    assert _normalize_chat_url("https://api.openai.com/v1") == "https://api.openai.com/v1/chat/completions"
    assert _normalize_chat_url("https://api.openai.com/v1/") == "https://api.openai.com/v1/chat/completions"
    assert _normalize_chat_url("https://generativelanguage.googleapis.com/v1beta/openai") == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert _normalize_chat_url("https://api.openai.com/v1/chat/completions") == "https://api.openai.com/v1/chat/completions"


def test_normalize_models_dev_catalog() -> None:
    from afterword_engine.main import _normalize_models_dev_catalog

    providers = _normalize_models_dev_catalog(
        {
            "google": {
                "name": "Google",
                "api": None,
                "npm": "@ai-sdk/google",
                "models": {
                    "gemini-2.5-flash": {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
                },
            },
            "sambanova": {
                "name": "SambaNova",
                "api": "https://api.sambanova.ai/v1",
                "npm": "@ai-sdk/openai-compatible",
                "models": {
                    "Meta-Llama-3.3-70B": {"id": "Meta-Llama-3.3-70B", "name": "Llama 3.3 70B"},
                },
            },
            "empty-provider": {
                "name": "Empty",
                "models": {},
            },
        }
    )
    assert [p["id"] for p in providers] == ["google", "sambanova"]
    assert providers[0]["api"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert providers[1]["api"] == "https://api.sambanova.ai/v1/chat/completions"


def test_llm_models_catalog_fetches_and_caches(monkeypatch) -> None:
    import asyncio
    import afterword_engine.main as main_module

    calls = 0

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "openai": {
                    "name": "OpenAI",
                    "api": "https://api.openai.com/v1",
                    "models": {"gpt-4o-mini": {"id": "gpt-4o-mini", "name": "GPT-4o mini"}},
                }
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str):
            nonlocal calls
            calls += 1
            return FakeResponse()

    main_module._MODELS_DEV_CACHE["providers"] = []
    main_module._MODELS_DEV_CACHE["expires_at"] = 0.0
    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeClient)

    first = asyncio.run(main_module.llm_models_catalog())
    second = asyncio.run(main_module.llm_models_catalog())
    assert calls == 1
    assert first["cached"] is False
    assert second["cached"] is True
    assert first["providers"][0]["id"] == "openai"


def test_extract_llm_json_handles_think_tags_fences_and_prose() -> None:
    from afterword_engine.llm_reranker import extract_llm_json
    import pytest

    fenced = '<think>\nEvaluating slate...\n</think>\nHere is the result:\n```json\n{"adjustments": [{"id": 10, "delta": 0.7, "reason": "Speculative match"}]}\n```'
    assert extract_llm_json(fenced) == {
        "adjustments": [{"id": 10, "delta": 0.7, "reason": "Speculative match"}]
    }

    embedded = 'Note: model lacks json_object flag -> {"adjustments": [{"id": 11, "delta": -0.8, "reason": "Canon"}]} trailing text'
    assert extract_llm_json(embedded) == {
        "adjustments": [{"id": 11, "delta": -0.8, "reason": "Canon"}]
    }

    with pytest.raises(ValueError, match="valid JSON"):
        extract_llm_json("No structured output available here.")


def test_ollama_mode_saves_keyless_discovers_offline_and_reranks_recommendations(
    tmp_path: Path, monkeypatch
) -> None:
    import httpx
    import respx
    from fastapi.testclient import TestClient
    from auth_helpers import authenticated_headers
    from afterword_engine import profiles
    from afterword_engine.config import settings
    from afterword_engine.database import initialize, rows, transaction
    import afterword_engine.main as main_module

    monkeypatch.setattr(settings, "db", str(tmp_path / "ollama_test.db"))
    monkeypatch.setattr(settings, "llm_rerank_allowed_hosts", "ollama")
    initialize()
    profiles.initialize_auth()

    with transaction() as con:
        con.execute("UPDATE candidates SET status='recommended', score=80.0 WHERE id=1")
        con.execute("UPDATE candidates SET status='recommended', score=79.5 WHERE id=2")

    main_module._MODELS_DEV_CACHE["providers"] = []
    main_module._MODELS_DEV_CACHE["expires_at"] = 0.0

    with respx.mock(assert_all_called=False) as mock_http:
        mock_http.get("https://models.dev/api.json").mock(
            side_effect=httpx.ConnectError("offline host")
        )
        tags_route = mock_http.get("http://ollama:11434/api/tags").mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "qwen2.5:7b", "model": "qwen2.5:7b"},
                        {"name": "llama3.2:3b", "model": "llama3.2:3b"},
                    ]
                },
            )
        )
        chat_route = mock_http.post("http://ollama:11434/api/chat").mock(
            return_value=httpx.Response(
                200,
                json={
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "adjustments": [
                                    {"id": 2, "delta": 1.1, "reason": "Strong local match"},
                                    {"id": 1, "delta": -0.9, "reason": "Older classic"},
                                ]
                            }
                        ),
                    }
                },
            )
        )

        with TestClient(main_module.app, headers=authenticated_headers()) as client:
            # Cloud catalog fails offline, but local Ollama discovery and saving still succeed
            cloud_resp = client.get("/api/llm/models")
            assert cloud_resp.status_code == 502

            ollama_models = client.get(
                "/api/llm/models", params={"mode": "ollama", "api_url": "http://ollama:11434"}
            )
            assert ollama_models.status_code == 200
            assert [m["id"] for m in ollama_models.json()["models"]] == [
                "llama3.2:3b",
                "qwen2.5:7b",
            ]
            assert tags_route.called
            assert "authorization" not in tags_route.calls[0].request.headers

            save_resp = client.put(
                "/api/settings/llm-rerank",
                json={
                    "enabled": True,
                    "mode": "ollama",
                    "api_url": "",
                    "model": "qwen2.5:7b",
                    "api_key": "",
                },
            )
            assert save_resp.status_code == 200, save_resp.text
            saved_body = save_resp.json()
            assert saved_body["llm_rerank"]["enabled"] is True
            assert saved_body["llm_rerank"]["mode"] == "ollama"
            assert saved_body["llm_rerank"]["api_url"] == "http://ollama:11434"
            assert saved_body["llm_rerank"]["api_key_set"] is False
            assert saved_body["health"]["checked"] is True
            assert "authorization" not in chat_route.calls[0].request.headers

            overview = client.get("/api/overview")
            assert overview.status_code == 200
            recs = [
                r
                for r in overview.json()["recommendations"]
                if r["id"] in {1, 2}
            ]
            assert [r["id"] for r in recs] == [2, 1]
            assert any("Strong local match" in exp for exp in recs[0]["explanation"])


def test_openai_compatible_mode_keyless_and_optional_key_lifecycle(
    tmp_path: Path, monkeypatch
) -> None:
    import httpx
    import respx
    from fastapi.testclient import TestClient
    from auth_helpers import authenticated_headers
    from afterword_engine import profiles
    from afterword_engine.config import settings
    from afterword_engine.database import initialize, transaction
    import afterword_engine.main as main_module

    monkeypatch.setattr(settings, "db", str(tmp_path / "openai_compat.db"))
    monkeypatch.setattr(settings, "llm_rerank_allowed_hosts", "ollama,vllm")
    initialize()
    profiles.initialize_auth()

    with transaction() as con:
        con.execute("UPDATE candidates SET status='recommended', score=81.0 WHERE id=1")
        con.execute("UPDATE candidates SET status='recommended', score=80.5 WHERE id=2")

    with respx.mock(assert_all_called=False) as mock_http:
        models_route = mock_http.get("http://vllm:8000/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": "Qwen/Qwen2.5-7B-Instruct", "object": "model"}],
                },
            )
        )
        chat_route = mock_http.post("http://vllm:8000/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "adjustments": [
                                            {"id": 2, "delta": 1.0, "reason": "Contemporary sci-fi"}
                                        ]
                                    }
                                )
                            }
                        }
                    ]
                },
            )
        )

        with TestClient(main_module.app, headers=authenticated_headers()) as client:
            disc = client.get(
                "/api/llm/models",
                params={"mode": "openai-compatible", "api_url": "http://vllm:8000/v1"},
            )
            assert disc.status_code == 200
            assert disc.json()["models"] == [
                {"id": "Qwen/Qwen2.5-7B-Instruct", "name": "Qwen/Qwen2.5-7B-Instruct"}
            ]
            assert "authorization" not in models_route.calls[-1].request.headers

            # Keyless save & recommendation reranking omits Authorization header
            saved = client.put(
                "/api/settings/llm-rerank",
                json={
                    "enabled": True,
                    "mode": "openai-compatible",
                    "api_url": "http://vllm:8000",
                    "model": "Qwen/Qwen2.5-7B-Instruct",
                },
            )
            assert saved.status_code == 200
            assert saved.json()["llm_rerank"]["api_key_set"] is False
            assert "authorization" not in chat_route.calls[-1].request.headers

            overview = client.get("/api/overview")
            assert overview.status_code == 200
            recs = [r for r in overview.json()["recommendations"] if r["id"] in {1, 2}]
            assert [r["id"] for r in recs] == [2, 1]
            assert "authorization" not in chat_route.calls[-1].request.headers

            # Supply optional API key, keep it on blank save, then clear it while staying enabled
            with_key = client.put(
                "/api/settings/llm-rerank",
                json={
                    "enabled": True,
                    "mode": "openai-compatible",
                    "api_url": "http://vllm:8000/v1",
                    "model": "Qwen/Qwen2.5-7B-Instruct",
                    "api_key": "local-vllm-secret",
                },
            )
            assert with_key.status_code == 200
            assert with_key.json()["llm_rerank"]["api_key_set"] is True
            assert (
                chat_route.calls[-1].request.headers.get("authorization")
                == "Bearer local-vllm-secret"
            )

            kept_key = client.put(
                "/api/settings/llm-rerank",
                json={
                    "enabled": True,
                    "mode": "openai-compatible",
                    "api_url": "http://vllm:8000/v1",
                    "model": "Qwen/Qwen2.5-7B-Instruct",
                    "api_key": "",
                },
            )
            assert kept_key.status_code == 200
            assert kept_key.json()["llm_rerank"]["api_key_set"] is True

            cleared_key = client.put(
                "/api/settings/llm-rerank",
                json={
                    "enabled": True,
                    "mode": "openai-compatible",
                    "api_url": "http://vllm:8000/v1",
                    "model": "Qwen/Qwen2.5-7B-Instruct",
                    "clear_api_key": True,
                },
            )
            assert cleared_key.status_code == 200
            assert cleared_key.json()["llm_rerank"]["api_key_set"] is False
            assert "authorization" not in chat_route.calls[-1].request.headers


def test_local_reranker_plain_schema_fallback_and_unparseable_output(monkeypatch) -> None:
    import httpx
    import respx
    from afterword_engine.config import settings

    monkeypatch.setattr(settings, "llm_rerank_allowed_hosts", "ollama,llamacpp")
    candidates = [
        {"id": 10, "title": "Classic", "score": 80.0, "explanation": []},
        {"id": 11, "title": "Modern", "score": 79.4, "explanation": []},
    ]

    seen_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen_payloads.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "response_format is not supported"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '<think>Checking...</think>\n```json\n{"adjustments": [{"id": 11, "delta": 1.0, "reason": "Fresh match"}]}\n```'
                        }
                    }
                ]
            },
        )

    with respx.mock:
        respx.post("http://llamacpp:8080/v1/chat/completions").mock(side_effect=handler)
        reranked, diag = rerank_with_llm_sync(
            [],
            candidates,
            mode="openai-compatible",
            api_url="http://llamacpp:8080/v1",
            model="local-gguf",
        )
    assert len(seen_payloads) == 2
    assert "response_format" in seen_payloads[0]
    assert "response_format" not in seen_payloads[1]
    assert diag["applied"] is True
    assert [c["id"] for c in reranked] == [11, 10]

    # Unparseable output keeps base rankings safely
    with respx.mock:
        respx.post("http://llamacpp:8080/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": "I cannot produce JSON right now."}}]},
            )
        )
        fallback_items, fallback_diag = rerank_with_llm_sync(
            [],
            candidates,
            mode="openai-compatible",
            api_url="http://llamacpp:8080/v1",
            model="local-gguf",
        )
    assert fallback_diag == {"applied": False, "reason": "fallback"}
    assert [c["id"] for c in fallback_items] == [10, 11]


def test_ssrf_blocks_unapproved_private_loopback_and_link_local_before_socket(
    tmp_path: Path, monkeypatch
) -> None:
    import socket
    import httpx
    import pytest
    from fastapi.testclient import TestClient
    from auth_helpers import authenticated_headers
    from afterword_engine import profiles
    from afterword_engine.config import settings
    from afterword_engine.database import initialize
    import afterword_engine.llm_reranker as reranker_module
    import afterword_engine.main as main_module

    monkeypatch.setattr(settings, "db", str(tmp_path / "ssrf_test.db"))
    monkeypatch.setattr(settings, "llm_rerank_allowed_hosts", "ollama")
    initialize()
    profiles.initialize_auth()

    real_getaddrinfo = socket.getaddrinfo

    def controlled_getaddrinfo(host, port, *args, **kwargs):
        if host == "unapproved-internal":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.18.0.9", port or 80))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", controlled_getaddrinfo)

    client_init_kwargs: list[dict] = []

    class GuardedAsyncClient:
        def __init__(self, *args, **kwargs):
            client_init_kwargs.append(kwargs)
            raise AssertionError("Outbound AsyncClient should never be instantiated for blocked SSRF URLs")

    class GuardedSyncClient:
        def __init__(self, *args, **kwargs):
            client_init_kwargs.append(kwargs)
            raise AssertionError("Outbound sync Client should never be instantiated for blocked SSRF URLs")

    monkeypatch.setattr(reranker_module.httpx, "AsyncClient", GuardedAsyncClient)
    monkeypatch.setattr(reranker_module.httpx, "Client", GuardedSyncClient)

    blocked_urls = [
        "http://127.0.0.1:11434",
        "http://localhost:11434",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.8:8000/v1",
        "http://192.168.1.20:8080/v1",
        "http://unapproved-internal:8000/v1",
    ]

    with TestClient(main_module.app, headers=authenticated_headers()) as client:
        for bad_url in blocked_urls:
            save_res = client.put(
                "/api/settings/llm-rerank",
                json={
                    "enabled": True,
                    "mode": "openai-compatible",
                    "api_url": bad_url,
                    "model": "test-model",
                },
            )
            assert save_res.status_code == 400, (bad_url, save_res.text)

            disc_res = client.get(
                "/api/llm/models",
                params={"mode": "ollama", "api_url": bad_url},
            )
            assert disc_res.status_code == 400, (bad_url, disc_res.text)

            test_res = client.post(
                "/api/llm/test",
                json={"mode": "ollama", "api_url": bad_url, "model": "qwen2.5:7b"},
            )
            assert test_res.status_code == 400, (bad_url, test_res.text)

            items, diag = rerank_with_llm_sync(
                [],
                [{"id": 1, "score": 80.0}, {"id": 2, "score": 79.0}],
                mode="ollama",
                api_url=bad_url,
                model="qwen2.5:7b",
            )
            assert diag == {"applied": False, "reason": "fallback"}
            assert [c["id"] for c in items] == [1, 2]

    assert client_init_kwargs == []


def test_outbound_clients_disable_redirects_and_env_proxies(monkeypatch) -> None:
    from afterword_engine.config import settings
    import afterword_engine.llm_reranker as reranker_module

    monkeypatch.setattr(settings, "llm_rerank_allowed_hosts", "ollama")
    captured_kwargs: list[dict] = []

    class InspectSyncClient:
        def __init__(self, *args, **kwargs):
            captured_kwargs.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, headers=None, json=None):
            class Resp:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return {"message": {"content": '{"adjustments": []}'}}

            return Resp()

    monkeypatch.setattr(reranker_module.httpx, "Client", InspectSyncClient)
    _, diag = rerank_with_llm_sync(
        [],
        [{"id": 1, "score": 80.0}, {"id": 2, "score": 79.0}],
        mode="ollama",
        api_url="http://ollama:11434",
        model="qwen2.5:7b",
    )
    assert diag["applied"] is True
    assert captured_kwargs[0]["follow_redirects"] is False
    assert captured_kwargs[0]["trust_env"] is False


def test_existing_cloud_setup_with_encrypted_key_loads_reranks_and_clears(
    tmp_path: Path, monkeypatch
) -> None:
    import socket
    import httpx
    import respx
    from fastapi.testclient import TestClient
    from auth_helpers import authenticated_headers
    from afterword_engine import profiles
    from afterword_engine.config import settings
    from afterword_engine.database import initialize, transaction
    from afterword_engine.secrets import seal
    import afterword_engine.main as main_module

    monkeypatch.setattr(settings, "db", str(tmp_path / "cloud_compat.db"))
    initialize()
    profiles.initialize_auth()

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )

    with transaction() as con:
        con.execute("UPDATE candidates SET status='recommended', score=82.0 WHERE id=1")
        con.execute("UPDATE candidates SET status='recommended', score=81.5 WHERE id=2")
        # Legacy rows saved prior to llm_rerank_mode migration
        for key, value, secret in (
            ("llm_rerank_enabled", "1", 0),
            ("llm_rerank_provider", "openai", 0),
            ("llm_rerank_api_url", "https://api.openai.com/v1/chat/completions", 0),
            ("llm_rerank_model", "gpt-4o-mini", 0),
            ("llm_rerank_api_key", seal("sk-encrypted-cloud-key"), 1),
        ):
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=excluded.secret",
                (key, value, secret),
            )

    with respx.mock(assert_all_called=False) as mock_http:
        cloud_route = mock_http.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"adjustments": [{"id": 2, "delta": 1.0, "reason": "Cloud boost"}]}
                                )
                            }
                        }
                    ]
                },
            )
        )

        with TestClient(main_module.app, headers=authenticated_headers()) as client:
            loaded = client.get("/api/settings/llm-rerank")
            assert loaded.status_code == 200
            assert loaded.json() == {
                "enabled": True,
                "mode": "cloud",
                "provider": "openai",
                "api_url": "https://api.openai.com/v1/chat/completions",
                "model": "gpt-4o-mini",
                "api_key_set": True,
            }

            overview = client.get("/api/overview")
            assert overview.status_code == 200
            recs = [r for r in overview.json()["recommendations"] if r["id"] in {1, 2}]
            assert [r["id"] for r in recs] == [2, 1]
            assert (
                cloud_route.calls[-1].request.headers.get("authorization")
                == "Bearer sk-encrypted-cloud-key"
            )

            # Cloud mode still rejects clearing key while enabled
            bad_clear = client.put(
                "/api/settings/llm-rerank",
                json={
                    "enabled": True,
                    "mode": "cloud",
                    "provider": "openai",
                    "api_url": "https://api.openai.com/v1/chat/completions",
                    "model": "gpt-4o-mini",
                    "clear_api_key": True,
                },
            )
            assert bad_clear.status_code == 400

            # Disabling and clearing credentials succeeds
            cleared = client.put(
                "/api/settings/llm-rerank",
                json={
                    "enabled": False,
                    "mode": "cloud",
                    "provider": "openai",
                    "api_url": "https://api.openai.com/v1/chat/completions",
                    "model": "gpt-4o-mini",
                    "clear_api_key": True,
                },
            )
            assert cleared.status_code == 200
            assert cleared.json()["llm_rerank"]["enabled"] is False
            assert cleared.json()["llm_rerank"]["api_key_set"] is False


def test_validated_llm_target_url_rejects_traversal_and_canonicalizes_components() -> None:
    import pytest
    from afterword_engine.llm_reranker import (
        _build_validated_llm_target_url,
        validate_llm_rerank_url,
    )

    allowed = {"ollama", "vllm"}
    assert (
        _build_validated_llm_target_url(
            "http://OLLAMA.:11434/api/chat",
            mode="ollama",
            endpoint="models",
            allowed_hosts=allowed,
        )
        == "http://ollama:11434/api/tags"
    )
    assert (
        _build_validated_llm_target_url(
            "http://ollama:11434/proxy/v1",
            mode="ollama",
            endpoint="chat",
            allowed_hosts=allowed,
        )
        == "http://ollama:11434/proxy/api/chat"
    )
    assert (
        _build_validated_llm_target_url(
            "http://vllm:8000",
            mode="openai-compatible",
            endpoint="chat",
            allowed_hosts=allowed,
        )
        == "http://vllm:8000/v1/chat/completions"
    )
    assert (
        _build_validated_llm_target_url(
            "http://vllm:8000/v1/chat/completions",
            mode="openai-compatible",
            endpoint="models",
            allowed_hosts=allowed,
        )
        == "http://vllm:8000/v1/models"
    )

    malformed_urls = [
        "http://ollama:11434/../../admin",
        "http://ollama:11434/%2e%2e/admin",
        "http://ollama:11434\\@127.0.0.1",
        "http://user:secret@ollama:11434",
        "http://ollama:11434/v1?redirect=http://127.0.0.1",
        "http://ollama:11434/v1#frag",
        "http://ollama:99999/v1",
    ]
    for bad in malformed_urls:
        with pytest.raises(ValueError):
            validate_llm_rerank_url(bad, allowed)
        with pytest.raises(ValueError):
            _build_validated_llm_target_url(
                bad, mode="ollama", endpoint="chat", allowed_hosts=allowed
            )

