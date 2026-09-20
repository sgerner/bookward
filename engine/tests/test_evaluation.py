"""Guard against leakage and metric errors in the offline ranking study."""
import importlib.util
from pathlib import Path

import numpy as np
spec = importlib.util.spec_from_file_location(
    "evaluate_ranking", Path(__file__).parents[1] / "scripts" / "evaluate_ranking.py"
)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def test_ranking_metrics_handle_ties_and_reversed_order():
    labels = np.array([True, True, False, False])
    assert evaluation.auc(labels, np.array([4, 3, 2, 1])) == 1
    assert evaluation.auc(labels, np.array([1, 2, 3, 4])) == 0
    assert evaluation.auc(labels, np.ones(4)) == .5
    assert evaluation.metrics(labels, np.array([4, 3, 2, 1]))["ndcg_at_20"] == 1


def test_read_dates_normalize_rss_and_iso_to_utc():
    assert evaluation.read_time("2026-08-05") == evaluation.read_time("Wed, 5 Aug 2026 00:00:00 +0000")
    assert evaluation.read_time("2026-08-04T17:00:00-07:00") == evaluation.read_time("2026-08-05")
    assert evaluation.read_time("not a date") is None


def test_prepare_deduplicates_works_and_rejects_stale_vectors():
    reads, embeddings = [], []
    for i in range(104):
        item = {"id": i, "title": f"Distinct title {i}", "author": "Writer", "rating": 4,
                "read_at": f"2020-01-{i % 28 + 1:02d}"}
        reads.append(item)
    reads[101].update(title=reads[0]["title"] + " (Series, #1)", read_at="2026-01-01")
    reads[102]["rating"] = None
    for item in reads:
        embeddings.append({"entity_type": "read", "entity_id": item["id"], "backend": "test",
                           "model": "fixed", "content_hash": evaluation.content_hash(evaluation.document(item)),
                           "dimensions": 2, "vector": np.array([1, 0], dtype=np.float32).tobytes()})
    embeddings[103]["content_hash"] = "stale"
    records, selected, excluded = evaluation.prepare({"reads": reads, "embeddings": embeddings}, None, None)
    assert len(records) == 101
    assert selected == ("test", "fixed")
    assert excluded == {"unrated": 1, "undated": 0, "missing_or_stale_vector": 1, "duplicate_work": 1}
    assert 101 not in {item["id"] for _, item, _ in records}


def test_feedback_uses_latest_explicit_event_and_only_prior_unrelated_reads(monkeypatch):
    vector = np.array([1, 0], dtype=np.float32)
    candidate = {"id": 9, "title": "Candidate", "author": "Writer", "status": "rejected"}
    data = {"candidates": [candidate], "feedback": [
        {"id": 1, "candidate_id": 9, "action": "reject", "created_at": "2025-06-01"},
        {"id": 2, "candidate_id": 9, "action": "save", "created_at": "2025-06-01"},
    ], "embeddings": [{"entity_type": "candidate", "entity_id": 9, "backend": "test", "model": "fixed",
                         "content_hash": evaluation.content_hash(evaluation.document(candidate)),
                         "vector": vector.tobytes()}]}
    records = [(evaluation.read_time(date), {"id": i, "title": title, "author": "Writer", "rating": 5}, vector)
               for i, date, title in [(1, "2020-01-01", "Prior"), (2, "2020-01-02", "Candidate"),
                                      (3, "2025-06-01", "Same day"), (4, "2026-01-01", "Future")]]

    def check_history(reads, *args):
        assert [r["id"] for r in reads] == [1]
        return [{"score": 60}]

    monkeypatch.setattr("afterword_engine.ranking.rank_candidates", check_history)
    result = evaluation.feedback_check(data, records, ("test", "fixed"))
    assert result["usable_candidates"] == 1
    assert result["saved"] == 1  # workflow status is not a negative label
