import numpy as np
import pytest

from afterword_engine.ranking import rank_candidates


def book(book_id, title, author, rating=None):
    value = {"id": book_id, "title": title, "author": author}
    if rating is not None:
        value["rating"] = rating
    return value


def test_positive_neighbor_ranks_candidate_and_explains_match():
    reads = [book(1, "Loved", "Writer", 5), book(2, "Disliked", "Other", 1)]
    candidates = [book(10, "Near loved", "New"), book(11, "Near disliked", "New")]
    result = rank_candidates(reads, [[1, 0], [-1, 0]], candidates, [[1, 0], [-1, 0]])
    assert result[0]["id"] == 10
    assert result[0]["score"] > result[1]["score"]
    assert any("Loved" in text for text in result[0]["explanation"])
    assert any("Disliked" in text for text in result[1]["explanation"])


def test_empty_history_uses_54_fallback_and_three_star_is_neutral():
    candidate = book(10, "Candidate", "New")
    assert rank_candidates([], [], [candidate], [[1, 0]])[0]["score"] == 54.0
    three_star = rank_candidates([book(1, "Middle", "Writer", 3)], [[1, 0]], [candidate], [[1, 0]])
    assert three_star[0]["score"] == 54.0


def test_author_shrinkage_mixes_history_and_is_not_exact_match_bonus():
    reads = [book(1, "A", "Writer", 5), book(2, "B", "Writer", 1)]
    result = rank_candidates(reads, [[1, 0], [1, 0]], [book(10, "C", "Writer")], [[1, 0]])[0]
    assert result["score"] == 66.0
    assert any("mixed ratings" in text for text in result["explanation"])


def test_batching_and_duplicate_reads_are_deterministic():
    reads = [book(2, "Same", "Author", 5), book(1, "Same", "Author", 1)]
    candidates = [book(i, f"Candidate {i}", "New") for i in range(80)]
    vectors = [[float(i % 7), 1.0] for i in range(80)]
    first = rank_candidates(reads, [[1, 0], [1, 0]], candidates, vectors)
    second = rank_candidates(reads, [[1, 0], [1, 0]], candidates, vectors)
    assert [(x["id"], x["score"]) for x in first] == [(x["id"], x["score"]) for x in second]
    assert len(first) == 80
    single = rank_candidates([reads[1]], [[1, 0]], candidates, vectors)
    assert [r["score"] for r in first] == [r["score"] for r in single]


def test_invalid_or_mismatched_vectors_fail_cleanly():
    with pytest.raises(ValueError):
        rank_candidates([book(1, "A", "A", 5)], [[1, 0]], [book(2, "B", "B")], [[1, 0, 0]])
    with pytest.raises(ValueError):
        rank_candidates([book(1, "A", "A", 5)], [[np.nan, 0]], [book(2, "B", "B")], [[1, 0]])


def test_formula_parity_no_negative_neighbors_and_zero_vectors_stay_finite():
    reads = [book(1, "Loved", "Writer", 5)]
    result = rank_candidates(reads, np.asarray([[1.0, 0.0]]), [book(2, "Candidate", "New",)], np.asarray([[1.0, 0.0]]))[0]
    # 42 + 48*1 + 3.5*(5-3) + 0 author delta.
    assert result["score"] == 97.0
    zero = rank_candidates([], np.empty((0, 2)), [book(3, "Zero", "New")], np.asarray([[0.0, 0.0]]))[0]
    assert zero["score"] == 54.0
    assert np.isfinite(zero["score"])


def test_kernel_rating_ramps_in_for_short_history():
    reads = [book(1, "Loved", "A", 5), book(2, "Disliked", "B", 1)]
    candidates = [book(3, "Near loved", "C"), book(4, "Near disliked", "D")]
    ranked = rank_candidates(reads, [[1, 0], [0, 1]], candidates, [[1, 0], [0, 1]])
    assert [item["score"] for item in ranked] == [97.4, 10.6]


def test_kernel_does_not_infer_a_rating_without_positive_similarity():
    reads = [book(i, f"Read {i}", "Writer", 5 if i < 41 else 1) for i in range(1, 42)]
    result = rank_candidates(reads, [[1, 0]] * len(reads), [book(50, "Orthogonal", "New")], [[0, 1]])
    assert result[0]["score"] == 42.0
