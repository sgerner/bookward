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
from afterword_engine.llm_shadow import run_shadow_policy
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
