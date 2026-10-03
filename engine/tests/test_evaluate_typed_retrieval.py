"""Protocol tests for the private typed-retrieval evaluator."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys

import httpx
import numpy as np
import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import evaluate_typed_retrieval as study


class ImmediateLimiter:
    async def wait(self):
        return None


def record(read_id: int, title: str, author: str, day: datetime, rating: int = 5):
    return (
        day,
        {
            "id": read_id,
            "title": title,
            "author": author,
            "rating": rating,
            "read_at": day.isoformat(),
        },
        np.asarray([1.0, 0.0], dtype=np.float32),
    )


def work(work_id: str, title: str, author: str, **kwargs) -> study.Work:
    return study.Work(
        work_id=work_id,
        title=title,
        author=author,
        source_url=f"https://openlibrary.org{work_id}",
        **kwargs,
    )


def test_work_and_author_keys_accept_only_open_library_catalog_paths():
    assert study._work_key("/works/OL123W.json") == "/works/OL123W"
    assert study._work_key("https://www.openlibrary.org/works/OL123w") == "/works/OL123W"
    assert study._work_key("https://openlibrary.org.evil.example/works/OL123W") == ""
    assert study._work_key("/books/OL123M") == ""
    assert study._author_key("/authors/OL99A.json") == "/authors/OL99A"


def test_series_edges_require_an_explicit_catalog_series_field():
    inferred_only = study._work_from_catalog(
        {
            "key": "/works/OL1W",
            "title": "The Example Cycle: Book One",
            "author_name": ["Writer One"],
            "author_key": ["/authors/OL10A"],
        },
        source="subject_works",
    )
    explicit = study._work_from_catalog(
        {
            "key": "/works/OL2W",
            "title": "Another Book",
            "author_name": ["Writer Two"],
            "series": ["The Example Cycle"],
        },
        source="subject_works",
    )

    assert inferred_only is not None and inferred_only.series_keys == set()
    assert explicit is not None and explicit.series_keys == {"the example cycle"}


def test_description_projection_accepts_synopsis_text_only():
    common = {
        "title": "A Catalog Work",
        "author_name": ["Catalog Writer"],
        "first_sentence": ["Opening sentence must not become the synopsis."],
    }
    plain = study._work_from_catalog(
        {**common, "key": "/works/OL31W", "description": "Actual synopsis."},
        source="subject_works",
    )
    mapped = study._work_from_catalog(
        {
            **common,
            "key": "/works/OL32W",
            "description": {"type": "/type/text", "value": "Mapped synopsis."},
        },
        source="subject_works",
    )
    opening_only = study._work_from_catalog(
        {**common, "key": "/works/OL33W"},
        source="subject_works",
    )
    malformed = study._work_from_catalog(
        {
            **common,
            "key": "/works/OL34W",
            "description": {"type": "/type/text", "value": {"nested": "not text"}},
        },
        source="subject_works",
    )

    assert plain is not None and plain.description == "Actual synopsis."
    assert mapped is not None and mapped.description == "Mapped synopsis."
    assert opening_only is not None and opening_only.description == ""
    assert malformed is not None and malformed.description == ""


def test_seed_policies_use_only_strictly_earlier_high_rated_reads():
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    history = [
        record(index + 1, f"Book {index}", f"Writer {index}", start + timedelta(days=index))
        for index in range(6)
    ]
    boundary = date(2020, 1, 7)
    history.append(record(7, "Boundary Book", "Boundary Writer", datetime(2020, 1, 7, tzinfo=timezone.utc)))

    current = study.choose_seed_reads(history, "current", boundary_day=boundary)
    recent = study.choose_seed_reads(history, "recent", boundary_day=boundary)
    mixed = study.choose_seed_reads(history, "mixed", boundary_day=boundary)

    assert [item["title"] for item in current] == ["Book 0", "Book 1", "Book 2", "Book 3"]
    assert [item["title"] for item in recent] == ["Book 5", "Book 4", "Book 3", "Book 2"]
    assert [item["title"] for item in mixed] == ["Book 0", "Book 5", "Book 1", "Book 4"]
    assert all(datetime.fromisoformat(item["read_at"]).date() < boundary for item in current + recent + mixed)


def test_candidate_gate_excludes_seen_and_known_post_cutoff_works():
    cutoff = date(2020, 3, 1)
    seen = work("/works/OL20W", "Already Read", "Writer", first_publish_year=2010)
    future = work("/works/OL21W", "Future Book", "Writer", first_publish_year=2021)
    fresh = work("/works/OL22W", "New Candidate", "Another Writer", first_publish_year=2019)
    prior_reads = [{"title": "Already Read", "author": "Writer"}]

    assert study.eligible_candidate(seen, prior_reads, cutoff_day=cutoff) == (False, "already_read")
    assert study.eligible_candidate(future, prior_reads, cutoff_day=cutoff) == (False, "published_after_cutoff")
    assert study.eligible_candidate(fresh, prior_reads, cutoff_day=cutoff) == (True, "eligible")


def test_failed_public_request_consumes_budget_without_retry(monkeypatch):
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(503, json={"error": "temporary"}, request=request)

    monkeypatch.setattr(
        study,
        "resolve_public_target",
        lambda _url: ("https://openlibrary.org", "93.184.216.34", "openlibrary.org"),
    )
    client = study.OpenLibraryProbeClient(
        request_budget=1,
        rate_limiter=ImmediateLimiter(),
        transport=httpx.MockTransport(handler),
    )

    async def attempt_twice():
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_json("/search.json", {"title": "synthetic"})
        with pytest.raises(study.BudgetExhausted):
            await client.get_json("/search.json", {"title": "synthetic"})

    asyncio.run(attempt_twice())

    assert len(observed) == 1
    assert client.requests == 1
    assert client.statuses["HTTPStatusError"] == 1
    assert client.budget_blocked == 1
    assert client.aggregate()["automatic_retries"] == 0


def test_public_target_resolution_failure_is_counted_and_consumes_budget(monkeypatch):
    monkeypatch.setattr(
        study,
        "resolve_public_target",
        lambda _url: (_ for _ in ()).throw(ValueError("synthetic resolution failure")),
    )
    client = study.OpenLibraryProbeClient(
        request_budget=1,
        rate_limiter=ImmediateLimiter(),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}, request=request)),
    )

    with pytest.raises(ValueError, match="synthetic resolution failure"):
        asyncio.run(client.get_json("/search.json"))

    assert client.requests == 1
    assert client.statuses["ValueError"] == 1
    assert client.aggregate()["requests_remaining"] == 0


def test_typed_scores_use_verified_relations_and_never_title_only_joins():
    seed = work(
        "/works/OL100W",
        "A Read",
        "Known Writer",
        author_keys={"/authors/OL10A"},
        subjects={"history": "History"},
    )
    same_author = work(
        "/works/OL101W",
        "Another Book",
        "Known Writer",
        author_keys={"/authors/OL10A"},
    )
    title_only = work("/works/OL102W", "Same Future Title", "Other Writer")
    capture = study.Capture(
        name="typed_current",
        policy="current",
        seeds=[],
        seed_works={seed.work_id: seed},
        eligible={same_author.work_id: same_author, title_only.work_id: title_only},
    )

    scores = study.one_hop_scores(capture)
    assert scores[same_author.work_id] > 0
    assert scores.get(title_only.work_id, 0) == 0
    assert study.match_method(
        {"title": "Same Future Title", "author": "Catalog Author"},
        {"title": "Same Future Title", "author": "Different Author"},
    ) is None
    assert study.match_method(
        {"title": "Same Future Title", "author": "Catalog Author"},
        {"title": "Same Future Title", "author": "Catalog Author"},
    ) == "exact_title_author"


def test_diffusion_is_a_reordering_of_the_captured_typed_graph():
    seed = work("/works/OL200W", "Read", "Writer", author_keys={"/authors/OL20A"})
    candidate = work("/works/OL201W", "Candidate", "Writer", author_keys={"/authors/OL20A"})
    capture = study.Capture(
        name="typed_current",
        policy="current",
        seeds=[],
        seed_works={seed.work_id: seed},
        eligible={candidate.work_id: candidate},
    )

    assert set(study.diffusion_scores(capture, 0.6)) == {candidate.work_id}
    assert study.ranked_pool(capture, "diffusion", alpha=0.6) == [candidate]


def test_private_capture_round_trip_preserves_the_exact_pool_and_no_ratings():
    seed = work("/works/OL300W", "Earlier Read", "Writer", author_keys={"/authors/OL30A"})
    candidate = work(
        "/works/OL301W",
        "Eligible Candidate",
        "Writer",
        author_keys={"/authors/OL30A"},
        subjects={"history": "History"},
        series_keys={"explicit cycle"},
        sources={"author_works"},
    )
    capture = study.Capture(
        name="typed_current",
        policy="current",
        seeds=[{"id": 1, "title": "Earlier Read", "author": "Writer", "rating": 5, "read_at": "2020-01-01"}],
        works={seed.work_id: seed, candidate.work_id: candidate},
        seed_works={seed.work_id: seed},
        eligible={candidate.work_id: candidate},
        relation_degree_hints={("author", "/authors/OL30A"): 3},
    )

    serialized = study._private_capture_record(capture)
    restored = study._capture_from_private_record(serialized)

    assert "rating" not in serialized["seed_reads"][0]
    assert list(restored.eligible) == [candidate.work_id]
    assert restored.seed_works[seed.work_id].author_keys == {"/authors/OL30A"}
    assert restored.eligible[candidate.work_id].series_keys == {"explicit cycle"}
    assert restored.relation_degree_hints[("author", "/authors/OL30A")] == 3
