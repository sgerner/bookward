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


def test_default_timeout_and_ssrf_guards_block_untrusted_urls(monkeypatch) -> None:
    import socket
    import httpx
    import pytest
    import afterword_engine.llm_reranker as reranker_module
    from afterword_engine.config import Settings, settings
    from afterword_engine.llm_reranker import probe_llm_reranker, validate_llm_api_url
    from afterword_engine.main import LLMRerankSettingsIn

    assert Settings().llm_rerank_timeout_seconds == 8.0
    monkeypatch.setattr(settings, "llm_rerank_allowed_hosts", "")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (
                    host
                    if host in {"127.0.0.1", "169.254.169.254"}
                    else "93.184.216.34",
                    port or 80,
                ),
            )
        ],
    )

    for blocked in (
        "https://",
        "http://llm.example/v1/chat/completions",
        "http://93.184.216.34/v1/chat/completions",
        "http://127.0.0.1:11434/v1/chat/completions",
        "http://169.254.169.254/latest/meta-data/",
        "https://localhost:8443/v1",
    ):
        with pytest.raises(ValueError):
            LLMRerankSettingsIn(
                enabled=True,
                api_url=blocked,
                model="gpt-4o-mini",
            ).validate_values()
        with pytest.raises(ValueError):
            validate_llm_api_url(blocked)

    http_called = False

    class ForbiddenClient:
        def __init__(self, *args, **kwargs) -> None:
            nonlocal http_called
            http_called = True

    monkeypatch.setattr(reranker_module.httpx, "Client", ForbiddenClient)

    candidates = [{"id": 1, "score": 80.0}, {"id": 2, "score": 78.0}]
    for blocked in (
        "https://",
        "http://llm.example/v1/chat/completions",
        "http://127.0.0.1:11434/v1",
        "http://169.254.169.254/v1",
    ):
        sync_items, sync_diag = rerank_with_llm_sync(
            [],
            candidates,
            api_url=blocked,
            api_key="sk-secret-key",
            model="gpt-4o-mini",
        )
        assert sync_diag["applied"] is False
        assert sync_diag["reason"] == "fallback"
        assert "sk-secret-key" not in sync_diag.get("error", "")
        assert [item["id"] for item in sync_items] == [1, 2]
        with pytest.raises(ValueError) as exc_info:
            probe_llm_reranker(
                api_url=blocked,
                api_key="sk-secret-key",
                model="gpt-4o-mini",
            )
        assert "sk-secret-key" not in str(exc_info.value)
    assert http_called is False

    # Keyless target preparation permits public HTTP only when no key is attached.
    target_url, host_headers, ext = reranker_module._prepare_llm_request_target(
        "http://llm.example/v1", has_api_key=False
    )
    assert target_url == "http://93.184.216.34/v1/chat/completions"
    assert host_headers == {"Host": "llm.example"}
    assert ext is None

    # Allowlisting an internal host permits it and configures httpx safely while still blocking redirects.
    monkeypatch.setattr(settings, "llm_rerank_allowed_hosts", "127.0.0.1,ollama")
    LLMRerankSettingsIn(
        enabled=True,
        api_url="http://127.0.0.1:11434/v1",
        model="llama3.2",
    ).validate_values()
    assert (
        validate_llm_api_url("http://ollama:11434/v1")
        == "http://ollama:11434/v1/chat/completions"
    )

    client_kwargs: list[dict] = []

    class RedirectingClient:
        def __init__(self, *args, **kwargs) -> None:
            client_kwargs.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def post(self, url: str, **kwargs):
            return httpx.Response(
                302,
                headers={"location": "http://169.254.169.254/latest/meta-data/"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(reranker_module.httpx, "Client", RedirectingClient)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    _, redirect_diag = rerank_with_llm_sync(
        [],
        candidates,
        api_url="https://llm.example/v1/chat/completions",
        api_key="sk-test",
        model="gpt-4o-mini",
    )
    assert client_kwargs[0]["follow_redirects"] is False
    assert client_kwargs[0]["trust_env"] is False
    assert redirect_diag["applied"] is False
    assert "Redirect" in redirect_diag["error"]


def _setup_temp_engine_db(tmp_path: Path, monkeypatch):
    import socket
    from afterword_engine import llm_reranker, profiles
    from afterword_engine.config import settings
    from afterword_engine.database import initialize

    monkeypatch.setattr(settings, "db", str(tmp_path / "llm_test.db"))
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "llm_rerank_allowed_hosts", "")
    monkeypatch.setattr(settings, "llm_rerank_top_k", 24)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (
                    host
                    if host in {"127.0.0.1", "169.254.169.254"}
                    else "93.184.216.34",
                    port or 443,
                ),
            )
        ],
    )
    llm_reranker.clear_llm_rerank_state(clear_all=True)
    initialize()
    profiles.initialize_auth()


def _seed_llm_settings(
    *,
    enabled: bool = True,
    api_url: str = "https://llm.example/v1/chat/completions",
    api_key: str = "sk-live-secret-token",
    model: str = "gpt-4o-mini",
) -> None:
    from afterword_engine.database import transaction
    from afterword_engine.secrets import seal

    with transaction() as con:
        for key, value, secret in (
            ("llm_rerank_enabled", "1" if enabled else "0", 0),
            ("llm_rerank_provider", "openai", 0),
            ("llm_rerank_api_url", api_url, 0),
            ("llm_rerank_model", model, 0),
            ("llm_rerank_api_key", seal(api_key) if api_key else "", 1),
        ):
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=excluded.secret",
                (key, value, secret),
            )


def test_overview_and_recommendations_release_db_lock_during_llm_and_allow_backup(
    tmp_path: Path, monkeypatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import httpx
    from fastapi.testclient import TestClient
    from auth_helpers import authenticated_headers
    import afterword_engine.llm_reranker as reranker_module
    import afterword_engine.main as main_module
    from afterword_engine.backups import create_backup
    from afterword_engine.database import database_lock
    from afterword_engine.tenancy import profile_database_path

    _setup_temp_engine_db(tmp_path, monkeypatch)
    _seed_llm_settings()

    in_flight = threading.Event()
    allow_finish = threading.Event()
    observed_lock_connections: list[int] = []

    class SlowLLMClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def post(self, url: str, **kwargs):
            lock = database_lock(profile_database_path())
            observed_lock_connections.append(lock._connections)
            in_flight.set()
            assert allow_finish.wait(timeout=5.0)
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"adjustments": [{"id": 1, "delta": 0.8, "reason": "Strong fit"}]}
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(reranker_module.httpx, "Client", SlowLLMClient)

    with ThreadPoolExecutor(max_workers=2) as pool:
        overview_future = pool.submit(main_module.overview_payload, recommendation_limit=24)
        assert in_flight.wait(timeout=5.0)
        # While the LLM HTTP call is active, zero _LockedConnections are held so backup succeeds immediately.
        backup_record = create_backup()
        assert backup_record["id"].startswith("bookward-")
        allow_finish.set()
        overview = overview_future.result(timeout=5.0)

    assert observed_lock_connections == [0]
    assert overview["settings"]["llm_rerank"]["last_status"] == "succeeded"

    # Verify GET /api/recommendations also holds zero _LockedConnections during live LLM calls.
    reranker_module.clear_llm_rerank_state(clear_all=True)
    in_flight.clear()
    allow_finish.clear()
    observed_lock_connections.clear()

    headers = authenticated_headers()
    client = TestClient(main_module.app, headers=headers)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rec_future = pool.submit(client.get, "/api/recommendations?status=recommended&limit=8&offset=0")
        assert in_flight.wait(timeout=5.0)
        second_backup = create_backup()
        assert second_backup["id"].startswith("bookward-")
        allow_finish.set()
        rec_resp = rec_future.result(timeout=5.0)

    assert rec_resp.status_code == 200
    assert observed_lock_connections == [0]


def test_slate_cache_reused_across_views_and_skips_offset_past_top_k(
    tmp_path: Path, monkeypatch
) -> None:
    import httpx
    from fastapi.testclient import TestClient
    from auth_helpers import authenticated_headers
    import afterword_engine.llm_reranker as reranker_module
    import afterword_engine.main as main_module
    from afterword_engine.database import rows, transaction

    _setup_temp_engine_db(tmp_path, monkeypatch)
    _seed_llm_settings()

    # Seed 30 recommended candidates with close scores so +1.1 promotes #2 to #1.
    source_id = rows("SELECT id FROM sources LIMIT 1")[0]["id"]
    with transaction() as con:
        con.execute("DELETE FROM candidates")
        for idx in range(30):
            con.execute(
                "INSERT INTO candidates(source_id,title,author,normalized_key,description,genres,score,status) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    source_id,
                    f"Speculative Book {idx}",
                    f"Author {idx}",
                    f"speculative-book-{idx}::author-{idx}",
                    "Thoughtful contemporary sci-fi novel.",
                    '["Science Fiction"]',
                    round(95.0 - idx * 0.5, 2),
                    "recommended",
                ),
            )

    top_ids = [
        item["id"]
        for item in rows("SELECT id FROM candidates WHERE status='recommended' ORDER BY score DESC LIMIT 2")
    ]
    llm_calls = 0

    class CountingClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def post(self, url: str, **kwargs):
            nonlocal llm_calls
            llm_calls += 1
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "adjustments": [
                                        {"id": top_ids[1], "delta": 1.1, "reason": "Boosted second item"}
                                    ]
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(reranker_module.httpx, "Client", CountingClient)

    # Pagination past top_k (offset >= 24) and zero-head requests make zero LLM calls and reuse 1 connection.
    opened_connections = []
    original_connect = main_module.connect

    def capture_connect():
        con = original_connect()
        opened_connections.append(con)
        return con

    monkeypatch.setattr(main_module, "connect", capture_connect)
    deep_page, _ = main_module.tracked_recommendations(
        connection=opened_connections[0] if opened_connections else None,
        status="recommended",
        limit=8,
        offset=24,
    )
    assert len(deep_page) >= 6
    assert llm_calls == 0

    opened_connections.clear()
    with main_module.connect() as con:
        zero_head, _ = main_module.tracked_recommendations(
            connection=con,
            recommended_limit=0,
        )
    assert isinstance(zero_head, list)
    assert llm_calls == 0
    assert len(opened_connections) == 1

    # First overview load populates the slate cache; second load (e.g. /?view=settings) reuses cache & 1 connection.
    first_overview = main_module.overview_payload(recommendation_limit=24)
    assert llm_calls == 1
    assert first_overview["recommendations"][0]["id"] == top_ids[1]

    opened_connections.clear()
    second_overview = main_module.overview_payload(recommendation_limit=24)
    assert llm_calls == 1
    assert len(opened_connections) == 1
    assert second_overview["recommendations"][0]["id"] == top_ids[1]

    # Recording reader feedback invalidates the cached slate so the next load refreshes.
    headers = authenticated_headers()
    client = TestClient(main_module.app, headers=headers)
    fb = client.post(
        f"/api/recommendations/{top_ids[0]}/feedback",
        json={"action": "save"},
    )
    assert fb.status_code == 200

    main_module.overview_payload(recommendation_limit=24)
    assert llm_calls == 2

    # Per-profile cache entries are bounded in memory.
    profile_key = reranker_module.current_llm_profile_key()
    for i in range(reranker_module.MAX_CACHE_ENTRIES_PER_PROFILE + 5):
        reranker_module.set_cached_slate_payload(
            profile_key,
            f"key-{i}",
            {"mode": "adjustments", "entries": []},
        )
    assert (
        len(reranker_module._SLATE_CACHE[profile_key])
        <= reranker_module.MAX_CACHE_ENTRIES_PER_PROFILE
    )


def test_circuit_breaker_persists_redacted_errors_and_serves_base_rankings_per_profile(
    tmp_path: Path, monkeypatch
) -> None:
    import httpx
    import afterword_engine.llm_reranker as reranker_module
    import afterword_engine.main as main_module
    from afterword_engine.database import initialize
    from afterword_engine.tenancy import profile_scope

    _setup_temp_engine_db(tmp_path, monkeypatch)
    secret_key = "sk-super-sensitive-credential"
    _seed_llm_settings(api_key=secret_key)

    llm_calls = 0

    class FailingClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def post(self, url: str, **kwargs):
            nonlocal llm_calls
            llm_calls += 1
            return httpx.Response(
                401,
                json={
                    "error": {
                        "message": f"Invalid key {secret_key} at https://llm.example/v1?api_key={secret_key}"
                    }
                },
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(reranker_module.httpx, "Client", FailingClient)

    first = main_module.overview_payload(recommendation_limit=24)
    assert llm_calls == 1
    llm_state = first["settings"]["llm_rerank"]
    assert llm_state["last_status"] == "failed"
    assert llm_state["last_attempt_at"]
    assert llm_state["cooldown_until"]
    assert "401" in llm_state["last_error"]
    assert secret_key not in llm_state["last_error"]
    assert "[redacted]" in llm_state["last_error"]

    # Second load hits open circuit breaker: zero HTTP calls and single DB connection reused.
    connections = []
    original_connect = main_module.connect

    def capture_connect():
        con = original_connect()
        connections.append(con)
        return con

    monkeypatch.setattr(main_module, "connect", capture_connect)
    second = main_module.overview_payload(recommendation_limit=24)
    assert llm_calls == 1
    assert len(connections) == 1
    assert len(second["recommendations"]) == 4

    # Another profile has isolated circuit-breaker and status state.
    other_profile = "a" * 32
    with profile_scope(other_profile):
        initialize()
        other_settings = main_module.safe_llm_rerank_settings()
        assert other_settings["last_status"] == ""
        assert other_settings["cooldown_until"] == ""


def test_llm_settings_save_and_test_endpoints_verify_live_responses_and_skip_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    import inspect
    import httpx
    from fastapi.testclient import TestClient
    from auth_helpers import authenticated_headers
    import afterword_engine.llm_reranker as reranker_module
    import afterword_engine.main as main_module

    assert not inspect.iscoroutinefunction(reranker_module.probe_llm_reranker)
    assert not inspect.iscoroutinefunction(main_module.update_llm_rerank_settings)
    assert not inspect.iscoroutinefunction(main_module.test_llm_rerank_settings)

    _setup_temp_engine_db(tmp_path, monkeypatch)
    probe_calls = 0
    should_fail = False
    captured_auth_headers: list[str] = []

    class ProbeSyncClient:
        def __init__(self, *args, **kwargs) -> None:
            assert kwargs.get("follow_redirects") is False
            assert kwargs.get("trust_env") is False

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def post(self, url: str, **kwargs):
            nonlocal probe_calls
            probe_calls += 1
            captured_auth_headers.append(kwargs.get("headers", {}).get("Authorization", ""))
            if should_fail:
                return httpx.Response(
                    401,
                    json={"error": {"message": "Incorrect API key sk-bad-secret"}},
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"adjustments": []}'}}]},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(reranker_module.httpx, "Client", ProbeSyncClient)
    headers = authenticated_headers()
    client = TestClient(main_module.app, headers=headers)
    if True:
        # Saving disabled LLM settings skips live probes.
        disabled_resp = client.put(
            "/api/settings/llm-rerank",
            json={
                "enabled": False,
                "provider": "openai",
                "api_url": "https://llm.example/v1/chat/completions",
                "model": "gpt-4o-mini",
                "api_key": "sk-saved",
            },
        )
        assert disabled_resp.status_code == 200
        assert disabled_resp.json()["checked"] is False
        assert probe_calls == 0

        # SSRF and cleartext public HTTP URLs are rejected before probing or saving.
        for rejected_url in (
            "http://169.254.169.254/v1/chat/completions",
            "http://llm.example/v1/chat/completions",
        ):
            bad_resp = client.put(
                "/api/settings/llm-rerank",
                json={
                    "enabled": True,
                    "provider": "custom",
                    "api_url": rejected_url,
                    "model": "gpt-4o-mini",
                    "api_key": "sk-saved",
                },
            )
            assert bad_resp.status_code == 400
            bad_test = client.post(
                "/api/settings/llm-rerank/test",
                json={
                    "provider": "custom",
                    "api_url": rejected_url,
                    "model": "gpt-4o-mini",
                    "api_key": "sk-saved",
                },
            )
            assert bad_test.status_code == 400
        assert probe_calls == 0

        # Enabling when upstream probe fails returns 502 with redacted error and does not enable reranking.
        should_fail = True
        failed_save = client.put(
            "/api/settings/llm-rerank",
            json={
                "enabled": True,
                "provider": "openai",
                "api_url": "https://llm.example/v1/chat/completions",
                "model": "gpt-4o-mini",
                "api_key": "sk-bad-secret",
            },
        )
        assert failed_save.status_code == 502
        assert "sk-bad-secret" not in failed_save.json()["detail"]
        assert probe_calls == 1
        assert main_module.safe_llm_rerank_settings()["enabled"] is False

        # Enabling with healthy upstream verifies live response and persists succeeded status.
        should_fail = False
        enabled_save = client.put(
            "/api/settings/llm-rerank",
            json={
                "enabled": True,
                "provider": "openai",
                "api_url": "https://llm.example/v1/chat/completions",
                "model": "gpt-4o-mini",
                "api_key": "sk-good-secret",
            },
        )
        assert enabled_save.status_code == 200
        body = enabled_save.json()
        assert body["checked"] is True
        assert body["llm_rerank"]["enabled"] is True
        assert body["llm_rerank"]["last_status"] == "succeeded"
        assert body["llm_rerank"]["last_attempt_at"]
        assert probe_calls == 2

        # Re-saving unchanged enabled settings skips redundant live probes.
        unchanged_save = client.put(
            "/api/settings/llm-rerank",
            json={
                "enabled": True,
                "provider": "openai",
                "api_url": "https://llm.example/v1/chat/completions",
                "model": "gpt-4o-mini",
            },
        )
        assert unchanged_save.status_code == 200
        assert unchanged_save.json()["checked"] is False
        assert probe_calls == 2

        # Changing model on the SAME normalized URL/origin & provider with blank key reuses stored key.
        same_origin_model_change = client.put(
            "/api/settings/llm-rerank",
            json={
                "enabled": True,
                "provider": "openai",
                "api_url": "https://llm.example/v1",
                "model": "gpt-4o",
            },
        )
        assert same_origin_model_change.status_code == 200
        assert same_origin_model_change.json()["checked"] is True
        assert probe_calls == 3
        assert captured_auth_headers[-1] == "Bearer sk-good-secret"

        # Explicit connection test with unchanged URL & blank key reuses stored key.
        test_ok = client.post(
            "/api/settings/llm-rerank/test",
            json={
                "provider": "openai",
                "api_url": "https://llm.example/v1/chat/completions",
                "model": "gpt-4o",
            },
        )
        assert test_ok.status_code == 200
        assert test_ok.json()["ok"] is True
        assert probe_calls == 4
        assert captured_auth_headers[-1] == "Bearer sk-good-secret"


def test_changed_url_or_provider_with_blank_key_never_sends_stored_or_env_key(
    tmp_path: Path, monkeypatch
) -> None:
    import httpx
    from fastapi.testclient import TestClient
    from auth_helpers import authenticated_headers
    import afterword_engine.llm_reranker as reranker_module
    import afterword_engine.main as main_module
    from afterword_engine.config import settings

    _setup_temp_engine_db(tmp_path, monkeypatch)
    sent_requests: list[dict] = []

    class RecordingClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def post(self, url: str, **kwargs):
            sent_requests.append({"url": url, "headers": dict(kwargs.get("headers") or {})})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"adjustments": []}'}}]},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(reranker_module.httpx, "Client", RecordingClient)
    headers = authenticated_headers()
    client = TestClient(main_module.app, headers=headers)

    # 1. Stored profile key: changing URL or provider with blank key is rejected without probing.
    _seed_llm_settings(
        enabled=True,
        api_url="https://llm.example/v1/chat/completions",
        api_key="sk-stored-profile-secret",
        model="gpt-4o-mini",
    )

    changed_url_save = client.put(
        "/api/settings/llm-rerank",
        json={
            "enabled": True,
            "provider": "openai",
            "api_url": "https://attacker.example/v1/chat/completions",
            "model": "gpt-4o-mini",
            "api_key": "",
        },
    )
    assert changed_url_save.status_code == 400
    assert sent_requests == []
    assert (
        main_module.safe_llm_rerank_settings()["api_url"]
        == "https://llm.example/v1/chat/completions"
    )

    changed_url_test = client.post(
        "/api/settings/llm-rerank/test",
        json={
            "provider": "openai",
            "api_url": "https://attacker.example/v1/chat/completions",
            "model": "gpt-4o-mini",
            "api_key": "",
        },
    )
    assert changed_url_test.status_code == 400
    assert sent_requests == []

    changed_provider_save = client.put(
        "/api/settings/llm-rerank",
        json={
            "enabled": True,
            "provider": "groq",
            "api_url": "https://llm.example/v1/chat/completions",
            "model": "gpt-4o-mini",
        },
    )
    assert changed_provider_save.status_code == 400
    assert sent_requests == []

    # Saving disabled settings with a changed URL and blank key drops the stored key instead of rebinding it.
    disabled_url_change = client.put(
        "/api/settings/llm-rerank",
        json={
            "enabled": False,
            "provider": "custom",
            "api_url": "https://attacker.example/v1/chat/completions",
            "model": "gpt-4o-mini",
        },
    )
    assert disabled_url_change.status_code == 200
    assert disabled_url_change.json()["llm_rerank"]["api_key_set"] is False
    assert sent_requests == []

    followup_test = client.post(
        "/api/settings/llm-rerank/test",
        json={
            "provider": "custom",
            "api_url": "https://attacker.example/v1/chat/completions",
            "model": "gpt-4o-mini",
        },
    )
    assert followup_test.status_code == 400
    assert sent_requests == []

    # 2. Deployment env key: never sent to a custom URL on save, test, or serving; reused only on matching default URL.
    (tmp_path / "env_profile").mkdir(parents=True, exist_ok=True)
    _setup_temp_engine_db(tmp_path / "env_profile", monkeypatch)
    client = TestClient(main_module.app, headers=authenticated_headers())
    monkeypatch.setattr(
        settings, "llm_rerank_api_url", "https://api.openai.com/v1/chat/completions"
    )
    monkeypatch.setattr(settings, "llm_rerank_api_key", "sk-deployment-env-secret")

    env_hijack_save = client.put(
        "/api/settings/llm-rerank",
        json={
            "enabled": True,
            "provider": "",
            "api_url": "https://attacker.example/v1/chat/completions",
            "model": "gpt-4o-mini",
        },
    )
    assert env_hijack_save.status_code == 400
    assert sent_requests == []

    env_hijack_test = client.post(
        "/api/settings/llm-rerank/test",
        json={
            "provider": "",
            "api_url": "https://attacker.example/v1/chat/completions",
            "model": "gpt-4o-mini",
        },
    )
    assert env_hijack_test.status_code == 400
    assert sent_requests == []

    # Even if a custom URL is present in profile settings without a profile key, effective settings omit env key.
    _seed_llm_settings(
        enabled=True,
        api_url="https://attacker.example/v1/chat/completions",
        api_key="",
        model="gpt-4o-mini",
    )
    assert main_module.effective_llm_rerank_settings()["api_key"] == ""

    # Fresh profile using the exact default origin & provider can reuse the operator env key.
    (tmp_path / "env_default_ok").mkdir(parents=True, exist_ok=True)
    _setup_temp_engine_db(tmp_path / "env_default_ok", monkeypatch)
    client = TestClient(main_module.app, headers=authenticated_headers())
    monkeypatch.setattr(
        settings, "llm_rerank_api_url", "https://api.openai.com/v1/chat/completions"
    )
    monkeypatch.setattr(settings, "llm_rerank_api_key", "sk-deployment-env-secret")
    env_default_test = client.post(
        "/api/settings/llm-rerank/test",
        json={
            "provider": "",
            "api_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
        },
    )
    assert env_default_test.status_code == 200
    assert len(sent_requests) == 1
    assert sent_requests[0]["headers"]["Authorization"] == "Bearer sk-deployment-env-secret"
    assert sent_requests[0]["headers"]["Host"] == "api.openai.com"


def test_empty_payload_trips_circuit_breaker_while_empty_adjustments_list_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    import httpx
    import afterword_engine.llm_reranker as reranker_module
    import afterword_engine.main as main_module

    _setup_temp_engine_db(tmp_path, monkeypatch)
    _seed_llm_settings()

    response_content = '{"adjustments": []}'
    llm_calls = 0

    class PayloadClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def post(self, url: str, **kwargs):
            nonlocal llm_calls
            llm_calls += 1
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": response_content}}]},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(reranker_module.httpx, "Client", PayloadClient)

    # Valid no-op schema `{"adjustments": []}` succeeds, caches, and leaves circuit breaker closed.
    noop_overview = main_module.overview_payload(recommendation_limit=24)
    assert llm_calls == 1
    assert noop_overview["settings"]["llm_rerank"]["last_status"] == "succeeded"
    assert noop_overview["settings"]["llm_rerank"]["cooldown_until"] == ""
    cached_noop = main_module.overview_payload(recommendation_limit=24)
    assert llm_calls == 1
    assert len(cached_noop["recommendations"]) == len(noop_overview["recommendations"])

    # Schema-noncompliant `{}` (missing both adjustments and rankings) trips the 300s circuit breaker.
    reranker_module.clear_llm_rerank_state(clear_all=True)
    response_content = "{}"
    broken_overview = main_module.overview_payload(recommendation_limit=24)
    assert llm_calls == 2
    llm_state = broken_overview["settings"]["llm_rerank"]
    assert llm_state["last_status"] == "failed"
    assert "adjustments or rankings" in llm_state["last_error"]
    assert llm_state["cooldown_until"] != ""

    # Next request short-circuits via open circuit breaker without calling upstream again.
    after_trip = main_module.overview_payload(recommendation_limit=24)
    assert llm_calls == 2
    assert len(after_trip["recommendations"]) == len(broken_overview["recommendations"])


def test_request_target_pins_resolved_ip_and_hmac_isolates_api_keys_and_modes(
    monkeypatch,
) -> None:
    import socket
    import httpx
    import pytest
    import afterword_engine.llm_reranker as reranker_module
    from afterword_engine.config import settings

    monkeypatch.setattr(settings, "llm_rerank_allowed_hosts", "")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))
        ],
    )

    with pytest.raises(ValueError):
        reranker_module.validate_llm_api_url("https://llm.example/v1/../admin")

    captured: list[dict] = []

    class InspectingSyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def post(self, url: str, **kwargs):
            captured.append({"url": url, **kwargs})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"adjustments": []}'}}]},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(reranker_module.httpx, "Client", InspectingSyncClient)
    probe_res = reranker_module.probe_llm_reranker(
        api_url="https://llm.example:8443/v1",
        api_key="sk-probe-key",
        model="gpt-4o-mini",
    )
    assert probe_res == {"ok": True, "checked": True, "model": "gpt-4o-mini"}
    assert captured[-1]["url"] == "https://93.184.216.34:8443/v1/chat/completions"
    assert captured[-1]["headers"]["Host"] == "llm.example:8443"
    assert captured[-1]["extensions"] == {"sni_hostname": "llm.example"}

    # Allowlisted internal hosts permit HTTP and use the operator-configured host directly without SNI override.
    monkeypatch.setattr(settings, "llm_rerank_allowed_hosts", "ollama")
    reranker_module.probe_llm_reranker(
        api_url="http://ollama:11434/v1",
        api_key="sk-local",
        model="llama3.2",
    )
    assert captured[-1]["url"] == "http://ollama:11434/v1/chat/completions"
    assert "Host" not in captured[-1]["headers"]
    assert "extensions" not in captured[-1]

    # HMAC-SHA256 fingerprints isolate slate cache keys and circuit-breaker state across API keys and modes.
    k1 = reranker_module.build_slate_cache_key(
        messages=[{"role": "user", "content": "{}"}],
        interaction_events=None,
        api_url="https://llm.example/v1",
        api_key="sk-key-one",
        model="gpt-4o-mini",
        top_k=24,
        max_delta=1.2,
        mode="cloud",
    )
    k2 = reranker_module.build_slate_cache_key(
        messages=[{"role": "user", "content": "{}"}],
        interaction_events=None,
        api_url="https://llm.example/v1",
        api_key="sk-key-two",
        model="gpt-4o-mini",
        top_k=24,
        max_delta=1.2,
        mode="cloud",
    )
    k3_mode = reranker_module.build_slate_cache_key(
        messages=[{"role": "user", "content": "{}"}],
        interaction_events=None,
        api_url="https://llm.example/v1",
        api_key="sk-key-one",
        model="gpt-4o-mini",
        top_k=24,
        max_delta=1.2,
        mode="ollama",
    )
    assert k1 != k2
    assert k1 != k3_mode
    assert "sk-key-one" not in k1

    reranker_module.trip_circuit_breaker(
        "profile-hmac-test",
        api_url="https://llm.example/v1",
        api_key="sk-key-one",
        model="gpt-4o-mini",
        error="HTTP 401",
        mode="cloud",
    )
    assert (
        reranker_module.get_circuit_breaker_state(
            "profile-hmac-test",
            api_url="https://llm.example/v1",
            api_key="sk-key-one",
            model="gpt-4o-mini",
            mode="cloud",
        )
        is not None
    )
    assert (
        reranker_module.get_circuit_breaker_state(
            "profile-hmac-test",
            api_url="https://llm.example/v1",
            api_key="sk-key-two",
            model="gpt-4o-mini",
            mode="cloud",
        )
        is None
    )
    reranker_module.trip_circuit_breaker(
        "profile-hmac-test",
        api_url="https://llm.example/v1",
        api_key="sk-key-one",
        model="gpt-4o-mini",
        error="HTTP 401",
        mode="cloud",
    )
    assert (
        reranker_module.get_circuit_breaker_state(
            "profile-hmac-test",
            api_url="https://llm.example/v1",
            api_key="sk-key-one",
            model="gpt-4o-mini",
            mode="ollama",
        )
        is None
    )
