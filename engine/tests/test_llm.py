import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from afterword_engine.config import settings
from afterword_engine.database import initialize, row, rows, transaction
from afterword_engine.llm import (
    AnthropicMessagesClient,
    OpenAICompatibleClient,
    OpenAIResponsesClient,
    LLMError,
    validate_rankings,
)
from afterword_engine.llm_shadow import run_shadow_policy, safe_policies
from afterword_engine import main as main_module
from afterword_engine.main import app, recommendation_list


RANKING = {
    "rankings": [
        {"candidate_id": 1, "score": 91, "confidence": 0.8, "reason_codes": ["positive_neighbor"]},
        {"candidate_id": 2, "score": 42, "confidence": 0.6, "reason_codes": []},
    ]
}


@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "test.db")
    initialize()
    return settings.db


def test_validate_rankings_requires_exact_candidate_set():
    valid = validate_rankings(RANKING, {1, 2})
    assert valid[0]["score"] == 91.0
    with pytest.raises(LLMError, match="duplicate"):
        validate_rankings({"rankings": [RANKING["rankings"][0], RANKING["rankings"][0]]}, {1, 2})
    with pytest.raises(LLMError, match="unknown"):
        validate_rankings({"rankings": [{**RANKING["rankings"][0], "candidate_id": 99}, RANKING["rankings"][1]]}, {1, 2})
    with pytest.raises(LLMError, match="finite"):
        validate_rankings({"rankings": [{**RANKING["rankings"][0], "score": float("nan")}, RANKING["rankings"][1]]}, {1, 2})


@respx.mock
def test_openai_responses_client_uses_structured_output():
    route = respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(200, json={"output_text": json.dumps(RANKING), "usage": {"input_tokens": 11, "output_tokens": 7}})
    )
    result = asyncio.run(OpenAIResponsesClient("https://api.openai.com/v1", "gpt-test", "secret").rank("prompt", {1, 2}))
    assert result.input_tokens == 11 and result.output_tokens == 7
    assert route.calls[0].request.headers["authorization"] == "Bearer secret"
    body = json.loads(route.calls[0].request.content)
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["strict"] is True


@respx.mock
def test_openai_compatible_client_parses_chat_completion():
    route = respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(RANKING)}}]})
    )
    result = asyncio.run(OpenAICompatibleClient("https://llm.example/v1", "model", "key").rank("prompt", {1, 2}))
    assert [item["candidate_id"] for item in result.rankings] == [1, 2]
    assert json.loads(route.calls[0].request.content)["response_format"]["type"] == "json_schema"


@respx.mock
def test_anthropic_messages_client_parses_content_blocks():
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(RANKING)}], "usage": {"input_tokens": 9, "output_tokens": 5}})
    )
    result = asyncio.run(AnthropicMessagesClient("https://api.anthropic.com/v1", "claude-test", "key").rank("prompt", {1, 2}))
    assert result.output_tokens == 5
    assert route.calls[0].request.headers["x-api-key"] == "key"


@respx.mock
def test_shadow_run_is_persisted_without_changing_visible_scores(database):
    candidates = rows("SELECT id,score FROM candidates WHERE status='recommended' ORDER BY score DESC LIMIT 2")
    assert len(candidates) == 2
    ids = [item["id"] for item in candidates]
    response = {"rankings": [
        {"candidate_id": ids[1], "score": 90, "confidence": 0.9, "reason_codes": ["fit"]},
        {"candidate_id": ids[0], "score": 30, "confidence": 0.4, "reason_codes": ["uncertain"]},
    ]}
    respx.post("https://api.openai.com/v1/responses").mock(return_value=httpx.Response(200, json={"output_text": json.dumps(response)}))
    with transaction() as con:
        connection_id = con.execute(
            "INSERT INTO llm_connections(name,provider_id,model_id,endpoint,secret) VALUES(?,?,?,?,?)",
            ("Test", "openai", "gpt-test", "https://api.openai.com/v1", ""),
        ).lastrowid
        # The test does not exercise secret decryption; use the API path for that
        # concern separately below.
        policy_id = con.execute(
            "INSERT INTO llm_policies(name,connection_id,top_k) VALUES(?,?,?)",
            ("Shadow", connection_id, 2),
        ).lastrowid
    before = recommendation_list()
    result = asyncio.run(run_shadow_policy(policy_id))
    assert result["status"] == "complete"
    run = row("SELECT * FROM llm_runs WHERE id=?", (result["run_id"],))
    assert run["status"] == "complete" and row("SELECT COUNT(*) count FROM llm_scores WHERE run_id=?", (result["run_id"],))["count"] == 2
    after = recommendation_list()
    assert [(item["id"], item["score"]) for item in after] == [(item["id"], item["score"]) for item in before]


def test_connection_api_encrypts_key_and_never_returns_it(database):
    with TestClient(app) as client:
        response = client.post("/api/llm/connections", json={"name": "OpenAI", "provider_id": "openai", "model_id": "gpt-test", "api_key": "super-secret"})
        assert response.status_code == 200
        connection_id = response.json()["id"]
        assert "api_key" not in response.json()["connection"]
        listed = client.get("/api/llm/connections").json()["connections"]
        assert listed[0]["id"] == connection_id
        stored = row("SELECT secret FROM llm_connections WHERE id=?", (connection_id,))["secret"]
        assert stored.startswith("fernet:") and "super-secret" not in stored
        assert client.post("/api/llm/policies", json={"name": "Shadow", "connection_id": connection_id, "top_k": 5}).status_code == 200


def test_connection_api_provisions_default_shadow_policy(database):
    with TestClient(app) as client:
        response = client.post(
            "/api/llm/connections",
            json={
                "name": "OpenAI",
                "provider_id": "openai",
                "model_id": "gpt-test",
                "api_key": "super-secret",
            },
        )
        assert response.status_code == 200
        connection_id = response.json()["id"]
        policy = response.json()["policy"]
        assert policy["connection_id"] == connection_id
        assert policy["enabled"] == 1
        assert policy["top_k"] == 20
        assert client.get("/api/llm/policies").json()["policies"]


def test_claude_connection_picker_applies_reasoning_to_default_policy(database):
    with TestClient(app) as client:
        response = client.post(
            "/api/llm/connections",
            json={
                "name": "Claude subscription",
                "provider_id": "anthropic",
                "model_id": "claude-sonnet-4-5",
                "auth_type": "claude_code",
                "oauth_token": "oauth-secret",
                "reasoning_effort": "xhigh",
            },
        )
        assert response.status_code == 200
        assert response.json()["connection"]["model_id"] == "claude-sonnet-4-5"
        assert response.json()["policy"]["reasoning_effort"] == "xhigh"
        policy = row("SELECT reasoning_effort FROM llm_policies WHERE connection_id=?", (response.json()["id"],))
        assert policy["reasoning_effort"] == "xhigh"


def test_subscription_policy_settings_updates_model_and_reasoning(database):
    with transaction() as con:
        connection_id = con.execute(
            "INSERT INTO llm_connections(name,provider_id,model_id,endpoint,auth_type,secret) VALUES(?,?,?,?,?,?)",
            ("ChatGPT", "openai", "gpt-5.6-terra", "https://api.openai.com/v1", "openai_codex", ""),
        ).lastrowid
        policy_id = con.execute(
            "INSERT INTO llm_policies(name,connection_id,reasoning_effort) VALUES(?,?,?)",
            ("ChatGPT shadow", connection_id, "medium"),
        ).lastrowid
    with TestClient(app) as client:
        response = client.put(
            f"/api/llm/policies/{policy_id}/settings",
            json={"model_id": "gpt-5.6-luna", "reasoning_effort": "xhigh"},
        )
    assert response.status_code == 200
    assert response.json()["policy"]["model_id"] == "gpt-5.6-luna"
    assert response.json()["policy"]["reasoning_effort"] == "xhigh"


def test_policy_listing_backfills_connections_created_before_auto_provisioning(database):
    with transaction() as con:
        connection_id = con.execute(
            "INSERT INTO llm_connections(name,provider_id,model_id,endpoint,secret) VALUES(?,?,?,?,?)",
            ("Legacy", "openai", "gpt-test", "https://api.openai.com/v1", ""),
        ).lastrowid
    policies = safe_policies()
    assert any(policy["connection_id"] == connection_id for policy in policies)


def test_authenticated_device_status_provisions_connection_and_policy(database, monkeypatch):
    class AuthenticatedManager:
        def status(self):
            return {
                "status": "authenticated",
                "authenticated": True,
                "account": {"email": "reader@example.com"},
            }

    monkeypatch.setattr(main_module, "codex_login_manager", lambda: AuthenticatedManager())
    with TestClient(app) as client:
        first = client.get("/api/llm/openai/device/status")
        second = client.get("/api/llm/openai/device/status")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["connection"]["auth_type"] == "openai_codex"
    assert first.json()["policy"]["connection_id"] == first.json()["connection"]["id"]
    assert second.json()["connection"]["id"] == first.json()["connection"]["id"]
    assert row("SELECT COUNT(*) count FROM llm_connections WHERE auth_type='openai_codex'")["count"] == 1
    assert row("SELECT COUNT(*) count FROM llm_policies")["count"] == 1
