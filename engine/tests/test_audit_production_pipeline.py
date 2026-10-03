import base64
import hashlib
import importlib.util
from pathlib import Path

import numpy as np
import pytest

from afterword_engine.embeddings import content_hash
from afterword_engine.scoring import document

SPEC = importlib.util.spec_from_file_location("audit_production_pipeline", Path(__file__).resolve().parents[1] / "scripts" / "audit_production_pipeline.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def fixture_snapshot():
    reads = [
        {"id": 1, "title": "Loved", "author": "One", "rating": 5},
        {"id": 2, "title": "Disliked", "author": "Two", "rating": 1},
    ]
    candidates = [
        {"id": 10, "title": "Better match", "author": "Three", "source_id": 1, "genres": "[]", "description": "", "status": "recommended", "score": 10},
        {"id": 11, "title": "Other match", "author": "Four", "source_id": 1, "genres": "[]", "description": "", "status": "recommended", "score": 90},
    ]
    vectors = {}
    for kind, items, values in (("read", reads, [[1, 0], [1, 0]]), ("candidate", candidates, [[.8, .6], [.2, np.sqrt(.96)]])):
        for item, value in zip(items, values):
            vectors[kind, item["id"]] = {"entity_type": kind, "entity_id": item["id"], "backend": "local", "model": "hashing-768", "vector": base64.b64encode(np.asarray(value, dtype=np.float32).tobytes()).decode(), "dimensions": 2, "content_hash": content_hash(document(item))}
    return {
        "reads": reads, "candidates": candidates,
        "sources": [{"id": 1, "name": "Source", "enabled": 1, "weight": 1}],
        "candidate_quality": [{"candidate_id": item["id"], "quality_status": "accepted", "quality_score": 1, "metadata_confidence": .45} for item in candidates],
        "embeddings": list(vectors.values()),
        "settings": [{"key": "embedding_backend", "value": "local"}, {"key": "embedding_model", "value": "hashing-768"}],
    }


def test_source_removal_precedes_confidence_and_neutralization_is_pure():
    snapshot = fixture_snapshot()
    reads = snapshot["reads"]
    read_vectors = [np.array([1, 0], dtype=np.float32)] * 2
    candidate = {"id": 3, "title": "Target", "author": "Other", "description": "", "genres": "[]", "catalog_confidence": 1, "source_weight": .6}
    query = np.array([.5, np.sqrt(.75)], dtype=np.float32)
    scores = {arm: audit._rank_recomputed(arm, reads=reads, read_vectors=read_vectors, candidate_rows=[candidate], candidate_vectors=[query])[0] for arm in ("current", "source_neutral", "metadata_neutral")}
    # Raw preference score=54, source penalty=-2, confidence=.45.
    assert scores["current"]["score"] == 50.9
    assert scores["source_neutral"]["score"] == 51.8
    assert scores["metadata_neutral"]["score"] == 52.0
    assert candidate["catalog_confidence"] == 1
    assert candidate["source_weight"] == .6


def test_inactive_interaction_ablation_preserves_recomputed_sql_order():
    snapshot = fixture_snapshot()
    with audit._fixture_connection(snapshot) as con:
        base = audit.recommendation_list(con, status="recommended", limit=None)
        assert [row["id"] for row in base] == [11, 10]
        report, coverage = audit._recomputed_arms(snapshot, con, scope="recommended", persisted_base=base, events=[], now=audit._as_datetime("2026-10-02T00:00:00Z"), backend="local", model="hashing-768", runtime={})
    assert coverage["common_complete_recompute_candidates"] == 2
    assert report["persisted_to_recomputed_drift"]["changed_candidates"] == 2
    assert report["arms"]["interaction_off"]["rank_movement_vs_recomputed_current"]["moved_candidates"] == 0
    assert report["arms"]["slate_off"]["rank_movement_vs_recomputed_current"]["moved_candidates"] == 0


def test_stale_cache_omitted_equally_and_never_promotes_new_rows():
    snapshot = fixture_snapshot()
    snapshot["embeddings"][-1]["content_hash"] = "stale"
    snapshot["candidates"][0]["status"] = "new"
    with audit._fixture_connection(snapshot) as con:
        base = audit.recommendation_list(con, status="recommended", limit=None)
        report, coverage = audit._recomputed_arms(snapshot, con, scope="recommended", persisted_base=base, events=[], now=audit._as_datetime("2026-10-02T00:00:00Z"), backend="local", model="hashing-768", runtime={})
    assert coverage["new_candidates_not_promoted"] == 1
    assert coverage["dropped_recommended_candidates"] == 1
    assert report["status"] == "unidentifiable_from_cached_vectors"
    assert report["arms"] == {}


def test_reference_requires_identical_order_not_just_identical_scores():
    rows = [{"id": 1, "score": 50, "status": "recommended"}, {"id": 2, "score": 50, "status": "recommended"}]
    generated = {"base": rows, "personalized": rows, "final": list(reversed(rows)), "learning": {}, "slate": {}}
    reference = {"base": rows, "personalized": rows, "final": rows, "interaction_diagnostics": {}, "slate_diagnostics": {}}
    result = audit._compare_reference(generated, reference)
    assert result["exact"] is False
    assert result["stages"]["final"]["mismatched_rows"] == 2


def test_score_changes_join_by_candidate_id_after_reranking():
    left = [{"id": 1, "score": 50}, {"id": 2, "score": 49}]
    right = [{"id": 2, "score": 51}, {"id": 1, "score": 50}]
    result = audit._score_change_summary(left, right)
    assert result["changed_candidates"] == 1
    assert result["maximum_absolute_delta"] == 2


def test_metadata_slices_use_original_confidence():
    left = [{"id": 1, "score": 50, "metadata_confidence": .3}]
    right = [{"id": 1, "score": 52, "metadata_confidence": 1}]
    assert audit._metadata_slices(left, right) == {"0.25-0.50": {"candidates": 1, "mean_score_delta": 2, "changed_candidates": 1}}


def test_cache_correction_keeps_original_event_id_for_supersession():
    snapshot = fixture_snapshot()
    event = {"id": 10, "candidate_id": 11, **{key: snapshot["candidates"][1][key] for key in ("title", "author", "description", "genres")}}
    with audit._fixture_connection(snapshot) as con:
        base = audit.recommendation_list(con, status="recommended", limit=None)
        buggy, _ = audit._cache_items(base, [event], con)
        corrected, _ = audit._cache_items(base, [event], con, corrected_event_key=True)
    assert 10 not in buggy
    assert set(corrected) == {10, 11}
    assert event["id"] == 10


def test_identity_and_quality_gates_survive_score_ablation():
    snapshot = fixture_snapshot()
    snapshot["candidates"][0].update(title="Loved", author="One")
    snapshot["candidate_quality"][1]["quality_status"] = "pending"
    with audit._fixture_connection(snapshot) as con:
        assert audit._score_eligible_candidates(con) == []



def test_source_hash_guard_requires_all_allowlisted_v1_files_and_rejects_paths():
    package = Path(audit.__file__).resolve().parents[1] / "afterword_engine"
    complete = {
        name: hashlib.sha256((package / name).read_bytes()).hexdigest()
        for name in audit.RUNTIME_SOURCE_FILES_V1
    }
    assert audit._source_hash_status(complete) == {
        "exact": True,
        "provided_count": 10,
        "expected_count": 10,
        "key_set_exact": True,
    }

    partial = dict(list(complete.items())[:-1])
    partial_status = audit._source_hash_status(partial)
    assert partial_status["exact"] is False
    assert partial_status["key_set_exact"] is False
    assert partial_status["provided_count"] == 9

    traversal = {**complete, "../../etc/passwd": complete["main.py"]}
    traversal_status = audit._source_hash_status(traversal)
    assert traversal_status["exact"] is False
    assert traversal_status["key_set_exact"] is False
    assert traversal_status["provided_count"] == 11


def test_report_uses_only_explicit_captured_commit_or_unknown():
    snapshot = fixture_snapshot()
    snapshot.update(
        exported_at_utc="2026-10-02T16:43:58.094Z",
        serving_reference={},
        runtime={},
        source_hashes={},
    )
    assert audit._run_snapshot(snapshot)["production_commit"] == "unknown"

    future_commit = "a" * 40
    snapshot["production_commit"] = future_commit
    assert audit._run_snapshot(snapshot)["production_commit"] == future_commit

    del snapshot["production_commit"]
    snapshot["runtime"] = {"git_commit": "b" * 64}
    assert audit._run_snapshot(snapshot)["production_commit"] == "b" * 64

    snapshot["runtime"] = {"git_commit": "not-a-commit"}
    assert audit._run_snapshot(snapshot)["production_commit"] == "unknown"
