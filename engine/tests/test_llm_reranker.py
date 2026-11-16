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
