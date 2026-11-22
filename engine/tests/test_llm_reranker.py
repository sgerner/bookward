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


def test_reader_neutral_prompt_and_distinct_signals_across_opposite_profiles() -> None:
    from afterword_engine.llm_reranker import build_rerank_prompt

    candidates = [
        {
            "id": 1,
            "title": "Middlemarch",
            "author": "George Eliot",
            "genres": ["Classics", "Literary Fiction"],
            "description": "Provincial life and marriage in nineteenth-century England.",
            "score": 81.0,
        },
        {
            "id": 2,
            "title": "Beach Read",
            "author": "Emily Henry",
            "genres": ["Romance", "Contemporary"],
            "description": "Two rival writers swap genres over one summer.",
            "score": 80.5,
        },
        {
            "id": 3,
            "title": "Orbital",
            "author": "Samantha Harvey",
            "genres": ["Science Fiction", "Literary"],
            "description": "Six astronauts aboard a space station observe Earth.",
            "score": 80.0,
        },
    ]

    classics_romance_reads = [
        {"id": 1, "title": "Pride and Prejudice", "author": "Jane Austen", "rating": 5.0, "read_at": "2026-10-01"},
        {"id": 2, "title": "Neuromancer", "author": "William Gibson", "rating": 1.0, "read_at": "2026-09-15"},
    ]
    classics_romance_events = [
        {"id": 10, "candidate_id": 501, "title": "Book Lovers", "author": "Emily Henry", "genres": ["Romance"], "event_type": "save", "occurred_at": "2026-10-10T10:00:00Z"},
        {"id": 11, "candidate_id": 502, "title": "Anna Karenina", "author": "Leo Tolstoy", "genres": ["Classics"], "event_type": "librarr_import", "occurred_at": "2026-10-11T10:00:00Z"},
        {"id": 12, "candidate_id": 503, "title": "Blindsight", "author": "Peter Watts", "genres": ["Hard Sci-Fi"], "event_type": "reject", "occurred_at": "2026-10-12T10:00:00Z"},
    ]

    scifi_nonfic_reads = [
        {"id": 1, "title": "Blindsight", "author": "Peter Watts", "rating": 5.0, "read_at": "2026-10-01"},
        {"id": 2, "title": "Pride and Prejudice", "author": "Jane Austen", "rating": 1.0, "read_at": "2026-09-15"},
    ]
    scifi_nonfic_events = [
        {"id": 20, "candidate_id": 601, "title": "Children of Time", "author": "Adrian Tchaikovsky", "genres": ["Science Fiction"], "event_type": "save", "occurred_at": "2026-10-10T10:00:00Z"},
        {"id": 21, "candidate_id": 602, "title": "Empire of Pain", "author": "Patrick Radden Keefe", "genres": ["Investigative Nonfiction"], "event_type": "librarr_import", "occurred_at": "2026-10-11T10:00:00Z"},
        {"id": 22, "candidate_id": 603, "title": "Book Lovers", "author": "Emily Henry", "genres": ["Romance"], "event_type": "reject", "occurred_at": "2026-10-12T10:00:00Z"},
    ]

    msg_a = build_rerank_prompt(
        classics_romance_reads,
        candidates,
        interaction_events=classics_romance_events,
        max_delta=1.2,
    )
    msg_b = build_rerank_prompt(
        scifi_nonfic_reads,
        candidates,
        interaction_events=scifi_nonfic_events,
        max_delta=1.2,
    )

    # Identical neutral system prompt with zero hardcoded author/genre rules.
    assert msg_a[0]["content"] == msg_b[0]["content"]
    system_lower = msg_a[0]["content"].casefold()
    for banned in (
        "orwell",
        "austen",
        "tolkien",
        "le guin",
        "chekhov",
        "classic",
        "canon",
        "self-help",
        "romance",
        "thriller",
        "speculative",
        "sci-fi",
    ):
        assert banned not in system_lower
    assert "relative ranking signal" in system_lower
    assert "[-1.20, +1.20]" in msg_a[0]["content"]

    payload_a = json.loads(msg_a[1]["content"])
    payload_b = json.loads(msg_b[1]["content"])
    assert [b["title"] for b in payload_a["rated_reads_liked"]] == ["Pride and Prejudice"]
    assert [b["title"] for b in payload_a["rated_reads_disliked"]] == ["Neuromancer"]
    assert [b["title"] for b in payload_a["recent_discovery_saved_books"]] == ["Book Lovers"]
    assert [b["title"] for b in payload_a["recent_discovery_acquired_books"]] == ["Anna Karenina"]
    assert [b["title"] for b in payload_a["recent_discovery_rejected_books"]] == ["Blindsight"]

    assert [b["title"] for b in payload_b["rated_reads_liked"]] == ["Blindsight"]
    assert [b["title"] for b in payload_b["rated_reads_disliked"]] == ["Pride and Prejudice"]
    assert [b["title"] for b in payload_b["recent_discovery_saved_books"]] == ["Children of Time"]
    assert [b["title"] for b in payload_b["recent_discovery_acquired_books"]] == ["Empire of Pain"]
    assert [b["title"] for b in payload_b["recent_discovery_rejected_books"]] == ["Book Lovers"]


def test_prompt_retains_15_newest_active_items_and_collapses_per_work_decisions() -> None:
    from datetime import datetime, timedelta, timezone

    base_time = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    events_asc = []
    event_id = 1

    # Record 20 saves (Save 1 oldest .. Save 20 newest) and 20 rejects (Reject 1 oldest .. Reject 20 newest)
    for i in range(1, 21):
        events_asc.append(
            {
                "id": event_id,
                "candidate_id": 100 + i,
                "title": f"Saved Work {i}",
                "author": f"Author S{i}",
                "event_type": "save",
                "occurred_at": (base_time + timedelta(minutes=event_id)).isoformat(),
            }
        )
        event_id += 1

    for i in range(1, 21):
        events_asc.append(
            {
                "id": event_id,
                "candidate_id": 200 + i,
                "title": f"Rejected Work {i}",
                "author": f"Author R{i}",
                "event_type": "reject",
                "occurred_at": (base_time + timedelta(minutes=event_id)).isoformat(),
            }
        )
        event_id += 1

    # Add retracted/deferred/undone/superseded transitions:
    # 1. Work saved then later rejected -> must appear ONLY in rejected, never in saved.
    events_asc.append(
        {
            "id": event_id,
            "candidate_id": 301,
            "title": "Changed Mind Book",
            "author": "Flip Author",
            "event_type": "save",
            "occurred_at": (base_time + timedelta(minutes=event_id)).isoformat(),
        }
    )
    event_id += 1
    events_asc.append(
        {
            "id": event_id,
            "candidate_id": 301,
            "title": "Changed Mind Book",
            "author": "Flip Author",
            "event_type": "reject",
            "occurred_at": (base_time + timedelta(minutes=event_id)).isoformat(),
        }
    )
    event_id += 1

    # 2. Saved Work 20 later restored -> must be dropped.
    events_asc.append(
        {
            "id": event_id,
            "candidate_id": 120,
            "title": "Saved Work 20",
            "author": "Author S20",
            "event_type": "restore",
            "occurred_at": (base_time + timedelta(minutes=event_id)).isoformat(),
        }
    )
    event_id += 1

    # 3. Rejected Work 20 later deferred with maybe_later -> must be dropped.
    events_asc.append(
        {
            "id": event_id,
            "candidate_id": 220,
            "title": "Rejected Work 20",
            "author": "Author R20",
            "event_type": "maybe_later",
            "occurred_at": (base_time + timedelta(minutes=event_id)).isoformat(),
        }
    )
    event_id += 1

    # 4. Explicitly undone save -> must be dropped.
    events_asc.append(
        {
            "id": event_id,
            "candidate_id": 302,
            "title": "Undone Save Book",
            "author": "Undo Author",
            "event_type": "save",
            "undone_at": (base_time + timedelta(minutes=event_id + 1)).isoformat(),
            "occurred_at": (base_time + timedelta(minutes=event_id)).isoformat(),
        }
    )

    # Pass in DESC order exactly like load_interaction_events() returns from SQLite!
    events_desc = list(reversed(events_asc))

    # Also pass 20 liked reads where id=1..20 have ascending read_at dates, in shuffled/DESC order.
    reads = [
        {
            "id": i,
            "title": f"Rated Read {i}",
            "author": f"Reader Author {i}",
            "rating": 5.0,
            "read_at": f"2026-09-{i:02d}",
        }
        for i in range(20, 0, -1)
    ]
    candidates = [
        {"id": 901, "title": "Fresh Candidate One", "author": "New Writer A", "score": 80.0},
        {"id": 902, "title": "Fresh Candidate Two", "author": "New Writer B", "score": 79.0},
    ]

    messages = build_rerank_prompt(reads, candidates, interaction_events=events_desc, top_k=24)
    payload = json.loads(messages[1]["content"])

    saved_titles = [b["title"] for b in payload["recent_discovery_saved_books"]]
    rejected_titles = [b["title"] for b in payload["recent_discovery_rejected_books"]]
    liked_titles = [b["title"] for b in payload["rated_reads_liked"]]

    # Saved Work 20 was restored, so newest 15 active saves are Saved Work 19 down to Saved Work 5.
    assert len(saved_titles) == 15
    assert saved_titles == [f"Saved Work {i}" for i in range(19, 4, -1)]
    assert "Saved Work 20" not in saved_titles
    assert "Undone Save Book" not in saved_titles
    assert "Changed Mind Book" not in saved_titles

    # Changed Mind Book is newest reject, Rejected Work 20 was maybe_later, so Changed Mind Book + 19..6.
    assert len(rejected_titles) == 15
    assert rejected_titles[0] == "Changed Mind Book"
    assert rejected_titles[1:] == [f"Rejected Work {i}" for i in range(19, 5, -1)]
    assert "Rejected Work 20" not in rejected_titles

    # Reads retain newest 15 (Sept 20 down to Sept 6), never oldest.
    assert liked_titles == [f"Rated Read {i}" for i in range(20, 5, -1)]


def test_zero_prompt_leakage_across_aliases_subtitles_audio_isbns_and_work_ids() -> None:
    candidates = [
        {
            "id": 1,
            "title": "Nineteen Eighty-Four",
            "author": "George Orwell",
            "score": 85.0,
        },
        {
            "id": 2,
            "title": "Project Hail Mary: A Novel",
            "author": "Andy Weir",
            "score": 84.0,
        },
        {
            "id": 3,
            "title": "Leviathan Wakes",
            "author": "James S. A. Corey",
            "score": 83.0,
        },
        {
            "id": 4,
            "title": "Completely Different Edition Title",
            "author": "Frank Herbert",
            "isbn13": "9780441172719",
            "score": 82.0,
        },
        {
            "id": 5,
            "title": "Translated Work Title",
            "author": "Ursula K. Le Guin",
            "quality_work_id": "/works/OL100001W",
            "quality_provider": "openlibrary",
            "score": 81.0,
        },
        {
            "id": 6,
            "title": "Provider Work Match Candidate",
            "author": "Octavia E. Butler",
            "quality_work_id": "gb-vol-777",
            "quality_provider": "google_books",
            "score": 80.0,
        },
    ]

    reads = [
        # Numeric alias of candidate 1
        {"id": 1, "title": "1984", "author": "George Orwell", "rating": 5.0},
        # ISBN-10 equivalent of candidate 4's ISBN-13 (9780441172719 <-> 0441172717)
        {"id": 2, "title": "Dune Classic", "author": "F. Herbert", "isbn": "0441172717", "rating": 5.0},
        # Verified provider work ID matching candidate 6
        {
            "id": 3,
            "title": "Alternate Butler Edition",
            "author": "Octavia Butler",
            "verified_work_id": "gb-vol-777",
            "identity_provider": "google_books",
            "rating": 2.0,
        },
        # Unrelated control read that MUST remain
        {"id": 4, "title": "The Left Hand of Darkness", "author": "Ursula K. Le Guin", "rating": 5.0},
    ]

    events = [
        # Subtitle-free variant of candidate 2
        {"id": 1, "candidate_id": 50, "title": "Project Hail Mary", "author": "Andy Weir", "event_type": "save"},
        # Audio suffixes of candidate 3
        {"id": 2, "candidate_id": 51, "title": "Leviathan Wakes (Unabridged)", "author": "James S.A. Corey", "event_type": "save"},
        {"id": 3, "candidate_id": 52, "title": "Leviathan Wakes: The Expanse, Book 1", "author": "James S. A. Corey", "event_type": "librarr_import"},
        # Open Library work ID match to candidate 5
        {
            "id": 4,
            "candidate_id": 53,
            "title": "Original Work Title",
            "author": "U. K. Le Guin",
            "openlibrary_work_id": "OL100001W",
            "event_type": "reject",
        },
        # Unrelated control events that MUST remain
        {"id": 5, "candidate_id": 99, "title": "Station Eleven", "author": "Emily St. John Mandel", "event_type": "save"},
        {"id": 6, "candidate_id": 100, "title": "Piranesi", "author": "Susanna Clarke", "event_type": "librarr_import"},
        {"id": 7, "candidate_id": 101, "title": "Verity", "author": "Colleen Hoover", "event_type": "reject"},
    ]

    messages = build_rerank_prompt(reads, candidates, interaction_events=events, top_k=24)
    payload = json.loads(messages[1]["content"])

    assert [b["title"] for b in payload["rated_reads_liked"]] == ["The Left Hand of Darkness"]
    assert payload["rated_reads_disliked"] == []
    assert [b["title"] for b in payload["recent_discovery_saved_books"]] == ["Station Eleven"]
    assert [b["title"] for b in payload["recent_discovery_acquired_books"]] == ["Piranesi"]
    assert [b["title"] for b in payload["recent_discovery_rejected_books"]] == ["Verity"]


def test_zero_delta_preserves_diversify_swaps_and_emits_zero_plus_zero_explanations() -> None:
    import numpy as np
    from afterword_engine.discovery_slate import diversify_discovery_slate

    # Build a slate with BOTH an author-repeat overflow (3 books by Author A)
    # and a near-duplicate similarity pair (ids 4 & 5 have identical vectors).
    raw_candidates = [
        {"id": 1, "title": "Repeat One", "author": "Author A", "score": 90.0, "explanation": ["Base match"]},
        {"id": 2, "title": "Repeat Two", "author": "Author A", "score": 89.5, "explanation": ["Base match"]},
        {"id": 3, "title": "Repeat Three", "author": "Author A", "score": 89.0, "explanation": ["Base match"]},
        {"id": 4, "title": "Twin Alpha", "author": "Author B", "score": 88.5, "explanation": ["Base match"]},
        {"id": 5, "title": "Twin Beta", "author": "Author C", "score": 88.0, "explanation": ["Base match"]},
        {"id": 6, "title": "Diverse Gem", "author": "Author D", "score": 87.5, "explanation": ["Base match"]},
        {"id": 7, "title": "Second Diverse Gem", "author": "Author E", "score": 87.0, "explanation": ["Base match"]},
    ]
    vectors = {
        1: np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32),
        2: np.array([0.0, 1.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32),
        3: np.array([0.0, 0.0, 1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        4: np.array([0.0, 0.0, 0.0, 1.0, 0.0, 0.0], dtype=np.float32),
        5: np.array([0.0, 0.0, 0.0, 1.0, 0.0, 0.0], dtype=np.float32),  # cosine 1.0 with id 4
        6: np.array([0.0, 0.0, 0.0, 0.0, 1.0, 0.0], dtype=np.float32),
        7: np.array([0.0, 0.0, 0.0, 0.0, 0.0, 1.0], dtype=np.float32),
    }

    baseline_diversified, baseline_diag = diversify_discovery_slate(raw_candidates, vectors)
    assert baseline_diag["author_repeat_swaps"] >= 1
    assert baseline_diag["similarity_swaps"] >= 1

    zero_adjustments = [
        {"id": c["id"], "delta": 0.0, "reason": "No change needed"}
        for c in raw_candidates
    ] + [{"id": 1, "delta": 0.002, "reason": "Sub-cent noise"}]

    # Pipeline order: LLM adjustments FIRST, then diversify_discovery_slate
    llm_out, llm_diag = apply_llm_rerank_adjustments(raw_candidates, zero_adjustments, max_delta=1.2)
    assert llm_diag["adjusted_count"] == 0
    post_diversified, post_diag = diversify_discovery_slate(llm_out, vectors)

    assert [c["id"] for c in post_diversified] == [c["id"] for c in baseline_diversified]
    assert post_diag == baseline_diag

    # Even if apply_llm_rerank_adjustments is called directly on an already diversified slate,
    # zero-delta adjustments must preserve swap order and never emit +0.00/-0.00 explanations.
    preserved, _ = apply_llm_rerank_adjustments(baseline_diversified, zero_adjustments, max_delta=1.2)
    assert [c["id"] for c in preserved] == [c["id"] for c in baseline_diversified]
    for item in preserved:
        joined_reason = " · ".join(item["explanation"])
        assert "+0.00" not in joined_reason
        assert "-0.00" not in joined_reason
        assert "LLM delta" not in joined_reason


def test_serving_releases_db_lock_before_network_io_and_caches_per_profile(
    tmp_path: Path, monkeypatch
) -> None:
    import socket
    from fastapi.testclient import TestClient
    from afterword_engine import profiles
    from afterword_engine.config import settings
    from afterword_engine.database import database_lock, initialize, transaction
    from afterword_engine.llm_reranker import (
        MAX_CACHE_ENTRIES_PER_PROFILE,
        clear_llm_rerank_cache,
        rerank_with_llm_sync,
    )
    import afterword_engine.main as main_module
    from afterword_engine.tenancy import profile_database_path, profile_scope

    clear_llm_rerank_cache()
    monkeypatch.setattr(settings, "db", str(tmp_path / "lock-cache.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )

    profiles.initialize_auth()
    admin = profiles.create_first_admin("owner", "owner-password-with-enough-length")
    alice = profiles.create_account("alice", "alice-password-with-enough-length")
    owner_session, _ = profiles.create_session(admin["id"])
    alice_session, _ = profiles.create_session(alice["id"])

    for prof_id, fav_title in (("legacy", "Dune"), (alice["profile_id"], "Pride and Prejudice")):
        with profile_scope(prof_id):
            initialize(seed_demo=False)
            with transaction() as con:
                con.execute(
                    "INSERT INTO reads(title,author,rating,read_at,source) VALUES(?,?,?,?,?)",
                    (fav_title, "Author", 5.0, "2026-10-01", "test"),
                )
                source_id = con.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
                con.execute("UPDATE sources SET enabled=1 WHERE id=?", (source_id,))
                for idx in range(1, 5):
                    cur = con.execute(
                        "INSERT INTO candidates(title,author,score,status,source_id,normalized_key,explanation) "
                        "VALUES(?,?,?,'recommended',?,?,?)",
                        (
                            f"Candidate {idx}",
                            f"Author {idx}",
                            80.0 - idx,
                            source_id,
                            f"{prof_id}-cand-{idx}",
                            json.dumps(["Base match"]),
                        ),
                    )
                    con.execute(
                        "INSERT OR REPLACE INTO candidate_quality(candidate_id,quality_status,quality_score) "
                        "VALUES(?, 'accepted', 1.0)",
                        (cur.lastrowid,),
                    )

    http_calls: list[dict] = []

    class LockCheckingSyncClient:
        def __init__(self, *args, **kwargs) -> None:
            self.timeout = kwargs.get("timeout")
            self.follow_redirects = kwargs.get("follow_redirects")

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def post(self, url: str, *, headers: dict, json: dict):
            # Verify _LockedConnection and _ProfileDatabaseLock are NOT held during outbound HTTP!
            active_db = profile_database_path()
            lock = database_lock(active_db)
            assert lock._connections == 0
            # Verify an exclusive writer lock (like backup/restore) can acquire immediately without deadlock!
            with lock:
                pass

            payload = json
            user_content = globals()["json"].loads(payload["messages"][1]["content"])
            cands = user_content["candidates"]
            http_calls.append(
                {
                    "url": url,
                    "auth": headers.get("Authorization"),
                    "timeout": self.timeout,
                    "follow_redirects": self.follow_redirects,
                    "liked": user_content["rated_reads_liked"],
                }
            )

            class Resp:
                status_code = 200
                headers: dict[str, str] = {}

                def raise_for_status(self) -> None:
                    return None

                def json(self) -> dict:
                    return {
                        "choices": [
                            {
                                "message": {
                                    "content": globals()["json"].dumps(
                                        {
                                            "adjustments": [
                                                {
                                                    "id": cands[1]["id"],
                                                    "delta": 1.1,
                                                    "reason": "Grounded profile match",
                                                },
                                                {
                                                    "id": cands[0]["id"],
                                                    "delta": 0.0,
                                                    "reason": "Unchanged",
                                                },
                                            ]
                                        }
                                    )
                                }
                            }
                        ]
                    }

            return Resp()

    import afterword_engine.llm_reranker as reranker_module

    monkeypatch.setattr(reranker_module.httpx, "Client", LockCheckingSyncClient)

    with TestClient(main_module.app) as client:
        for session_tok, key_val in ((owner_session, "sk-owner-key"), (alice_session, "sk-alice-key")):
            res = client.put(
                "/api/settings/llm-rerank",
                headers={"x-bookward-session": session_tok},
                json={
                    "enabled": True,
                    "provider": "openai",
                    "api_url": "https://api.openai.com/v1/chat/completions",
                    "model": "gpt-4o-mini",
                    "api_key": key_val,
                },
            )
            assert res.status_code == 200

        # First /api/recommendations request for owner -> 1 outbound HTTP call, lock released before I/O
        r1 = client.get("/api/recommendations?status=recommended", headers={"x-bookward-session": owner_session})
        assert r1.status_code == 200
        assert len(http_calls) == 1
        assert http_calls[0]["timeout"] <= 15.0
        assert http_calls[0]["follow_redirects"] is False
        # Candidate 2 (78.0 + 1.1 = 79.1) moved ahead of Candidate 1 (79.0 + 0.0 = 79.0)
        r1_items = r1.json()
        assert r1_items[0]["title"] == "Candidate 2"
        assert any("LLM delta (+1.10)" in exp for exp in r1_items[0]["explanation"])
        assert all("+0.00" not in exp for item in r1_items for exp in item["explanation"])

        # Repeated /api/recommendations AND /api/overview with unchanged slate/reads/feedback -> cache hit (0 new HTTP calls!)
        r2 = client.get("/api/recommendations?status=recommended", headers={"x-bookward-session": owner_session})
        ov = client.get("/api/overview", headers={"x-bookward-session": owner_session})
        assert r2.status_code == 200
        assert ov.status_code == 200
        assert len(http_calls) == 1
        assert [item["id"] for item in r2.json()] == [item["id"] for item in r1_items]

        # Alice's profile is strictly isolated -> issues her own HTTP call with her own history
        ra = client.get("/api/recommendations?status=recommended", headers={"x-bookward-session": alice_session})
        assert ra.status_code == 200
        assert len(http_calls) == 2
        assert http_calls[1]["liked"][0]["title"] == "Pride and Prejudice"

        # Recording new feedback on owner invalidates cache key -> triggers a fresh call
        target_id = r1_items[-1]["id"]
        fb = client.post(
            f"/api/recommendations/{target_id}/feedback",
            headers={"x-bookward-session": owner_session},
            json={"action": "save"},
        )
        assert fb.status_code == 200
        r3 = client.get("/api/recommendations?status=recommended", headers={"x-bookward-session": owner_session})
        assert r3.status_code == 200
        assert len(http_calls) == 3

    # Verify bounded per-profile eviction does not evict another profile's cached entry
    calls_before_eviction = len(http_calls)
    for i in range(MAX_CACHE_ENTRIES_PER_PROFILE + 5):
        rerank_with_llm_sync(
            [{"title": f"Book {i}", "author": "Author", "rating": 5.0}],
            [{"id": 1, "title": "A", "score": 80.0}, {"id": 2, "title": "B", "score": 79.0}],
            profile_id="busy-profile",
            api_url="https://api.openai.com/v1/chat/completions",
            api_key="sk-busy",
            model="gpt-4o-mini",
        )
    with reranker_module._RERANK_CACHE_LOCK:
        assert len(reranker_module._RERANK_CACHE_BY_PROFILE["profile:busy-profile"]) == MAX_CACHE_ENTRIES_PER_PROFILE
        assert f"profile:{alice['profile_id']}" in reranker_module._RERANK_CACHE_BY_PROFILE
    assert len(http_calls) == calls_before_eviction + MAX_CACHE_ENTRIES_PER_PROFILE + 5


def test_ssrf_validation_redirect_rejection_and_host_credential_isolation(
    tmp_path: Path, monkeypatch
) -> None:
    import socket
    from fastapi.testclient import TestClient
    from afterword_engine import profiles
    from afterword_engine.config import settings
    from afterword_engine.database import initialize, transaction
    import afterword_engine.llm_reranker as reranker_module
    import afterword_engine.main as main_module
    from afterword_engine.tenancy import profile_scope

    reranker_module.clear_llm_rerank_cache()
    monkeypatch.setattr(settings, "db", str(tmp_path / "security-llm.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.setattr(settings, "llm_rerank_api_url", "https://api.openai.com/v1/chat/completions")
    monkeypatch.setattr(settings, "llm_rerank_api_key", "sk-HOST-OPERATOR-SECRET")
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)

    profiles.initialize_auth()
    admin = profiles.create_first_admin("owner", "owner-password-with-enough-length")
    tenant = profiles.create_account("tenant", "tenant-password-with-enough-length")
    with profile_scope("legacy"):
        initialize(seed_demo=False)
    with profile_scope(tenant["profile_id"]):
        initialize(seed_demo=False)
    tenant_session, _ = profiles.create_session(tenant["id"])

    def fake_dns(host: str, port: int, *args, **kwargs):
        mapping = {
            "127.0.0.1": "127.0.0.1",
            "::1": "::1",
            "10.0.0.8": "10.0.0.8",
            "192.168.1.20": "192.168.1.20",
            "169.254.169.254": "169.254.169.254",
            "rebind.example.test": "169.254.169.254",
            "custom-llm.example.com": "93.184.216.34",
            "api.openai.com": "93.184.216.34",
        }
        ip = mapping.get(host, "93.184.216.34")
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_dns)

    with TestClient(main_module.app, headers={"x-bookward-session": tenant_session}) as client:
        # 1. Host key is NEVER indicated in safe settings for a profile without its own saved key
        initial = client.get("/api/settings/llm-rerank")
        assert initial.status_code == 200
        assert initial.json()["api_key_set"] is False

        # 2. Loopback, private, link-local, metadata, and plain-HTTP URLs all fail validation
        blocked_urls = [
            "http://127.0.0.1:11434/v1/chat/completions",
            "https://127.0.0.1/v1/chat/completions",
            "https://[::1]/v1/chat/completions",
            "https://localhost/v1/chat/completions",
            "https://10.0.0.8/v1/chat/completions",
            "https://192.168.1.20/v1/chat/completions",
            "http://169.254.169.254/latest/meta-data/",
            "https://169.254.169.254/v1/chat/completions",
            "https://metadata.google.internal/computeMetadata/v1/",
            "https://rebind.example.test/v1/chat/completions",
            "http://custom-llm.example.com/v1/chat/completions",
        ]
        for bad_url in blocked_urls:
            resp = client.put(
                "/api/settings/llm-rerank",
                json={
                    "enabled": True,
                    "api_url": bad_url,
                    "model": "gpt-4o-mini",
                    "api_key": "sk-tenant-own",
                },
            )
            assert resp.status_code == 400, f"Expected 400 for {bad_url}, got {resp.status_code}"

        # 3. Custom URL without profile-owned API key MUST be rejected even though host key is set!
        no_key_custom = client.put(
            "/api/settings/llm-rerank",
            json={
                "enabled": True,
                "api_url": "https://custom-llm.example.com/v1/chat/completions",
                "model": "custom-model",
                "api_key": "",
            },
        )
        assert no_key_custom.status_code == 400
        assert "profile API key" in no_key_custom.json()["detail"]

    # 4. Even if DB settings contain a custom URL with blank profile key, host key is NEVER returned or sent
    with profile_scope(tenant["profile_id"]):
        with transaction() as con:
            con.execute(
                "INSERT OR REPLACE INTO settings(key,value,secret) VALUES('llm_rerank_enabled','1',0)"
            )
            con.execute(
                "INSERT OR REPLACE INTO settings(key,value,secret) VALUES('llm_rerank_api_url',?,0)",
                ("https://custom-llm.example.com/v1/chat/completions",),
            )
        eff = main_module.effective_llm_rerank_settings()
        safe = main_module.safe_llm_rerank_settings()
        assert eff["api_key"] == ""
        assert safe["api_key_set"] is False

    res_custom, diag_custom = reranker_module.rerank_with_llm_sync(
        [{"title": "Dune", "author": "Frank Herbert", "rating": 5.0}],
        [{"id": 1, "title": "A", "score": 80.0}, {"id": 2, "title": "B", "score": 79.0}],
        api_url="https://custom-llm.example.com/v1/chat/completions",
        api_key=None,
    )
    assert diag_custom == {"applied": False, "reason": "not_configured"}

    # 5. Outbound requests reject cross-origin redirects without following or leaking Authorization
    seen_requests: list[tuple[str, str]] = []

    class RedirectClient:
        def __init__(self, *args, **kwargs) -> None:
            assert kwargs.get("follow_redirects") is False

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def post(self, url: str, *, headers: dict, json: dict):
            seen_requests.append((url, headers.get("Authorization", "")))

            class RedirectResp:
                status_code = 307
                headers = {"location": "https://evil.example.org/steal"}

                def raise_for_status(self) -> None:
                    return None

            return RedirectResp()

    monkeypatch.setattr(reranker_module.httpx, "Client", RedirectClient)
    fallback_cands, fallback_diag = reranker_module.rerank_with_llm_sync(
        [{"title": "Dune", "author": "Frank Herbert", "rating": 5.0}],
        [{"id": 1, "title": "A", "score": 80.0}, {"id": 2, "title": "B", "score": 79.0}],
        api_url="https://custom-llm.example.com/v1/chat/completions",
        api_key="sk-tenant-own",
    )
    assert fallback_diag == {"applied": False, "reason": "fallback"}
    assert len(seen_requests) == 1
    assert seen_requests[0][0] == "https://custom-llm.example.com/v1/chat/completions"


def test_held_out_loo_and_chronological_replay_non_inferiority_across_profiles() -> None:
    from datetime import datetime, timedelta, timezone
    import numpy as np
    from afterword_engine.llm_reranker import evaluate_reranker_replay

    def grounded_profile_responder(messages: list[dict[str, str]]) -> list[dict]:
        """Deterministic grounded LLM simulator driven strictly by prompt evidence."""
        payload = json.loads(messages[1]["content"])
        pos_genres: dict[str, float] = {}
        neg_genres: dict[str, float] = {}
        pos_authors: set[str] = set()
        neg_authors: set[str] = set()

        for r in payload["rated_reads_liked"]:
            if r.get("author"):
                pos_authors.add(str(r["author"]).casefold())
        for r in payload["rated_reads_disliked"]:
            if r.get("author"):
                neg_authors.add(str(r["author"]).casefold())
        for b in payload["recent_discovery_saved_books"]:
            if b.get("author"):
                pos_authors.add(str(b["author"]).casefold())
            for g in b.get("genres") or []:
                pos_genres[g.casefold()] = pos_genres.get(g.casefold(), 0.0) + 1.0
        for b in payload["recent_discovery_acquired_books"]:
            for g in b.get("genres") or []:
                pos_genres[g.casefold()] = pos_genres.get(g.casefold(), 0.0) + 0.7
        for b in payload["recent_discovery_rejected_books"]:
            if b.get("author"):
                neg_authors.add(str(b["author"]).casefold())
            for g in b.get("genres") or []:
                neg_genres[g.casefold()] = neg_genres.get(g.casefold(), 0.0) + 1.0

        total_evidence = (
            len(payload["rated_reads_liked"])
            + len(payload["rated_reads_disliked"])
            + len(payload["recent_discovery_saved_books"])
            + len(payload["recent_discovery_acquired_books"])
            + len(payload["recent_discovery_rejected_books"])
        )
        if total_evidence < 3:
            # Sparse profile guardrail: keep all adjustments at 0.0
            return [{"id": c["id"], "delta": 0.0, "reason": "Sparse evidence"} for c in payload["candidates"]]

        out = []
        for cand in payload["candidates"]:
            author = str(cand.get("author") or "").casefold()
            genres = [str(g).casefold() for g in (cand.get("genres") or [])]
            pos_support = sum(pos_genres.get(g, 0.0) for g in genres) + (1.5 if author in pos_authors else 0.0)
            neg_support = sum(neg_genres.get(g, 0.0) for g in genres) + (1.5 if author in neg_authors else 0.0)
            net = pos_support - neg_support
            if net >= 1.5:
                out.append({"id": cand["id"], "delta": 0.9, "reason": "Matches reader history"})
            elif net <= -1.5:
                out.append({"id": cand["id"], "delta": -0.9, "reason": "Matches reader passes"})
            else:
                out.append({"id": cand["id"], "delta": 0.0, "reason": "Inconclusive"})
        return out

    personas = [
        (
            "classics_literary",
            "Nineteenth Century Classics",
            "Hard Cyberpunk",
            "George Eliot",
            "Cyber Writer",
        ),
        (
            "romance_thriller",
            "Contemporary Romance",
            "Military Sci-Fi",
            "Emily Henry",
            "Space Marine Author",
        ),
        (
            "scifi_investigative",
            "Speculative Science Fiction",
            "Cozy Mystery",
            "Adrian Tchaikovsky",
            "Cozy Detective Author",
        ),
    ]

    start_dt = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    for persona_name, liked_genre, disliked_genre, liked_author, disliked_author in personas:
        for history_size in (0, 4, 24):
            candidates = []
            cand_vectors = {}
            for idx in range(1, 25):
                is_pos = idx % 2 == 1
                cid = idx
                genre = liked_genre if is_pos else disliked_genre
                author = f"{liked_author} {idx // 3}" if is_pos else f"{disliked_author} {idx // 3}"
                # Give slightly noisy base scores so reranking has room to refine without distorting
                base_score = 78.0 + (0.4 if not is_pos else 0.0) - (idx * 0.15)
                candidates.append(
                    {
                        "id": cid,
                        "title": f"{persona_name} Book {cid}",
                        "author": author,
                        "genres": [genre],
                        "description": f"A {genre} novel.",
                        "score": round(base_score, 2),
                        "explanation": ["Base scorer"],
                    }
                )
                vec = np.array([1.0, 0.1 * (idx % 3)] if is_pos else [-1.0, 0.1 * (idx % 3)], dtype=np.float32)
                cand_vectors[cid] = vec

            reads = (
                []
                if history_size == 0
                else [
                    {
                        "id": 1,
                        "title": f"{persona_name} Seed Liked",
                        "author": f"{liked_author} 0",
                        "rating": 5.0,
                        "read_at": "2026-08-01",
                    },
                    {
                        "id": 2,
                        "title": f"{persona_name} Seed Disliked",
                        "author": f"{disliked_author} 0",
                        "rating": 1.0,
                        "read_at": "2026-08-02",
                    },
                ]
            )

            eval_count = 4 if history_size == 0 else history_size
            decisions = []
            for step in range(eval_count):
                cid = step + 1
                cand = candidates[step]
                is_pos = cid % 2 == 1
                decisions.append(
                    {
                        "id": step + 1,
                        "candidate_id": cid,
                        "title": cand["title"],
                        "author": cand["author"],
                        "genres": cand["genres"],
                        "event_type": "save" if is_pos else "reject",
                        "occurred_at": (start_dt + timedelta(days=step + 1)).isoformat(),
                    }
                )

            effective_reads = [] if history_size == 0 else reads
            effective_decisions = (
                decisions[:2] if history_size == 0 else decisions
            )

            for mode in ("leave_one_out", "chronological"):
                report = evaluate_reranker_replay(
                    reads=effective_reads,
                    candidates=candidates,
                    decisions=effective_decisions,
                    responder=grounded_profile_responder,
                    mode=mode,
                    candidate_vectors=cand_vectors,
                    interaction_vectors=cand_vectors,
                    top_k=24,
                    max_delta=1.2,
                )
                assert report["leakage_violations"] == 0, f"Leakage in {persona_name}/{history_size}/{mode}"
                assert report["non_inferior"] is True, (
                    f"Regression in {persona_name} size={history_size} mode={mode}: {report}"
                )
                if history_size == 24:
                    assert report["reranked"]["auc"] >= report["base"]["auc"]


def test_async_rerank_with_llm_caches_clamps_timeout_and_rejects_redirects(monkeypatch) -> None:
    import asyncio
    import socket
    import afterword_engine.llm_reranker as reranker_module
    from afterword_engine.llm_reranker import clear_llm_rerank_cache, rerank_with_llm

    clear_llm_rerank_cache()
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )

    calls: list[dict] = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            self.timeout = kwargs.get("timeout")
            self.follow_redirects = kwargs.get("follow_redirects")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url: str, *, headers: dict, json: dict):
            calls.append({"url": url, "timeout": self.timeout, "follow_redirects": self.follow_redirects})

            class Resp:
                status_code = 200
                headers: dict[str, str] = {}

                def raise_for_status(self) -> None:
                    return None

                def json(self) -> dict:
                    return {
                        "choices": [
                            {
                                "message": {
                                    "content": globals()["json"].dumps(
                                        {
                                            "adjustments": [
                                                {"id": 2, "delta": 0.85, "reason": "Async match"},
                                                {"id": 1, "delta": 0.0, "reason": "No op"},
                                            ]
                                        }
                                    )
                                }
                            }
                        ]
                    }

            return Resp()

    monkeypatch.setattr(reranker_module.httpx, "AsyncClient", FakeAsyncClient)

    reads = [{"title": "Dune", "author": "Frank Herbert", "rating": 5.0}]
    candidates = [
        {"id": 1, "title": "First", "author": "A", "score": 80.0, "explanation": ["Base"]},
        {"id": 2, "title": "Second", "author": "B", "score": 79.5, "explanation": ["Base"]},
    ]

    r1, d1 = asyncio.run(
        rerank_with_llm(
            reads,
            candidates,
            profile_id="async-prof",
            api_url="https://api.openai.com/v1/chat/completions",
            api_key="sk-async",
            model="gpt-4o-mini",
            timeout_seconds=99.0,  # Clamped to <= 15.0s
        )
    )
    assert d1["applied"] is True
    assert d1["cached"] is False
    assert [c["id"] for c in r1] == [2, 1]
    assert calls[0]["timeout"] <= 15.0
    assert calls[0]["follow_redirects"] is False

    r2, d2 = asyncio.run(
        rerank_with_llm(
            reads,
            candidates,
            profile_id="async-prof",
            api_url="https://api.openai.com/v1/chat/completions",
            api_key="sk-async",
            model="gpt-4o-mini",
        )
    )
    assert d2["applied"] is True
    assert d2["cached"] is True
    assert len(calls) == 1
    assert [c["id"] for c in r2] == [2, 1]


def test_cache_key_excludes_sensitive_api_key_material() -> None:
    import inspect
    from afterword_engine.llm_reranker import _build_cache_key, build_rerank_prompt

    sig = inspect.signature(_build_cache_key)
    assert "api_key" not in sig.parameters

    reads = [{"title": "Dune", "author": "Frank Herbert", "rating": 5.0}]
    candidates = [
        {"id": 1, "title": "First", "author": "A", "score": 80.0},
        {"id": 2, "title": "Second", "author": "B", "score": 79.0},
    ]
    messages = build_rerank_prompt(reads, candidates, top_k=24, max_delta=1.2)
    base_kwargs = dict(
        profile_scope="profile:alpha",
        url="https://api.openai.com/v1/chat/completions",
        model="gpt-4o-mini",
        top_k=24,
        max_delta=1.2,
        messages=messages,
        candidates=candidates,
        reads=reads,
        interaction_events=None,
    )
    k1 = _build_cache_key(**base_kwargs)
    assert k1 == _build_cache_key(**base_kwargs)
    assert k1 != _build_cache_key(**{**base_kwargs, "profile_scope": "profile:beta"})
    assert k1 != _build_cache_key(**{**base_kwargs, "model": "gpt-4.1-mini"})
    assert k1 != _build_cache_key(
        **{**base_kwargs, "url": "https://api.groq.com/openai/v1/chat/completions"}
    )
