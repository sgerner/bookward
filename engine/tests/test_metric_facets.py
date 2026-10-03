"""Synthetic invariants for constrained metric learning and grounded facets."""
import importlib.util
from pathlib import Path

import numpy as np
import pytest

from afterword_engine.ranking import rank_candidates


SCRIPT = Path(__file__).parents[1] / "scripts" / "evaluate_metric_facets.py"
spec = importlib.util.spec_from_file_location("evaluate_metric_facets", SCRIPT)
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


def test_supervised_metric_uses_only_the_training_prefix():
    rng = np.random.default_rng(20261002)
    vectors = rng.normal(size=(90, 16))
    ratings = rng.integers(1, 6, size=90).astype(float)
    train = np.arange(70)

    first = evaluator.fit_supervised_metric(vectors, ratings, train, components=4)
    changed_vectors = vectors.copy()
    changed_ratings = ratings.copy()
    changed_vectors[70:] = rng.normal(100, 30, size=changed_vectors[70:].shape)
    changed_ratings[70:] = np.tile([1, 5], 10)
    second = evaluator.fit_supervised_metric(changed_vectors, changed_ratings, train, components=4)

    np.testing.assert_array_equal(first["direction"], second["direction"])
    assert first["train_count"] == 70
    assert np.count_nonzero(first["direction"]) <= 16


def test_supervised_metric_ignores_poisoned_held_out_labels():
    rng = np.random.default_rng(20261003)
    vectors = rng.normal(size=(88, 20))
    ratings = rng.integers(1, 6, size=88).astype(float)
    train = np.arange(72)
    reference = evaluator.fit_supervised_metric(vectors, ratings, train, components=4)

    poisoned = ratings.copy()
    poisoned[72:] = np.nan
    changed_vectors = vectors.copy()
    changed_vectors[72:] = rng.normal(1000, 200, size=changed_vectors[72:].shape)
    actual = evaluator.fit_supervised_metric(changed_vectors, poisoned, train, components=4)

    np.testing.assert_array_equal(reference["direction"], actual["direction"])


def test_rank_one_metric_is_exact_cosine_at_zero_and_stays_unit_length():
    rng = np.random.default_rng(8)
    vectors = rng.normal(size=(13, 24)).astype(np.float32)
    direction = rng.normal(size=24)
    unit = evaluator._unit_rows(vectors).astype(np.float32)

    unchanged = evaluator.metric_vectors(vectors, direction, 0.0)
    adjusted = evaluator.metric_vectors(vectors, direction, 0.05)
    np.testing.assert_allclose(unchanged, unit, atol=1e-7)
    np.testing.assert_allclose(np.linalg.norm(adjusted, axis=1), 1.0, atol=1e-6)
    assert not np.allclose(adjusted, unit)


def test_nonzero_metric_matches_rank_one_psd_cosine_formula():
    rng = np.random.default_rng(901)
    vectors = rng.normal(size=(17, 11)).astype(np.float32)
    direction = rng.normal(size=11)
    reference = evaluator.cosine_similarity_matrix(vectors)
    direct = evaluator.cosine_similarity_matrix(
        evaluator.metric_vectors(vectors, direction, 0.05)
    )
    calculated = evaluator.make_metric_similarity(reference, vectors, direction, 0.05)

    np.testing.assert_allclose(calculated, direct, atol=2e-6, rtol=2e-6)
    np.testing.assert_allclose(np.diag(calculated), np.ones(len(vectors)), atol=2e-6)


def test_cross_query_history_cosines_normalize_both_views_independently():
    rng = np.random.default_rng(902)
    query = rng.normal(size=(9, 13)).astype(np.float32)
    history = rng.normal(size=(15, 13)).astype(np.float32)
    expected = evaluator._servable_unit_rows(query) @ evaluator._servable_unit_rows(history).T

    actual = evaluator.cosine_cross_similarity_matrix(query, history)

    np.testing.assert_array_equal(actual, expected)
    assert actual.shape == (9, 15)


def test_causal_cross_similarity_matches_serving_query_history_batch():
    from datetime import date

    history = [
        {"id": 9, "title": "Earlier 9", "author": "Writer 9", "rating": 5,
         "read_at": "2020-01-01"},
        {"id": 2, "title": "Earlier 2", "author": "Writer 2", "rating": 1,
         "read_at": "2020-01-02"},
        {"id": 5, "title": "Earlier 5", "author": "Writer 5", "rating": 4,
         "read_at": "2020-01-02"},
    ]
    query = {"id": 20, "title": "Query", "author": "Query writer", "rating": 5,
             "read_at": "2020-01-03"}
    history_vectors = np.asarray([[1, 0], [0, 1], [1, 1]], dtype=np.float32)
    query_vectors = np.asarray([[0, 1], [1, 0], [-1, 1], [-1, 0]], dtype=np.float32)
    records = [
        (date(2020, 1, 1), history[0], history_vectors[0]),
        (date(2020, 1, 2), history[1], history_vectors[1]),
        (date(2020, 1, 2), history[2], history_vectors[2]),
        (date(2020, 1, 3), query, query_vectors[3]),
    ]
    similarity = evaluator.causal_cross_similarity_matrix(
        records, query_vectors, np.vstack([history_vectors, [1, 0]])
    )
    replay = evaluator.score_records_from_similarity(records, [3], similarity)
    expected = rank_candidates(history, history_vectors, [query], query_vectors[3:])[0]["score"]

    np.testing.assert_array_equal(replay, [expected])
    assert similarity[1, 2] == 0.0  # same-day history is unavailable to row 1


def test_lambda_zero_similarity_scores_match_serving_ranker():
    rng = np.random.default_rng(21)
    history = []
    vectors = []
    for index in range(40):
        history.append({
            "id": index + 1,
            "title": f"History {index}",
            "author": f"Writer {index % 9}",
            "rating": [1, 2, 3, 4, 5][index % 5],
            "read_at": f"2020-01-{(index % 27) + 1:02d}",
        })
        vectors.append(rng.normal(size=12).astype(np.float32))
    candidate = {
        "id": 1000,
        "title": "Query",
        "author": "Writer 2",
        "rating": 5,
        "read_at": "2020-02-01",
        "source_weight": 1.1,
        "catalog_confidence": 0.8,
        "description": "A short synopsis",
        "genres": '["Mystery"]',
    }
    query = rng.normal(size=12).astype(np.float32)
    unit_history = evaluator._unit_rows(vectors)
    unit_query = evaluator._unit_rows([query])[0]
    similarity = unit_history @ unit_query

    expected = rank_candidates(history, vectors, [candidate], [query])[0]["score"]
    actual = evaluator.score_from_similarities(history, candidate, similarity)

    assert actual == expected


def test_all_negative_cosines_match_serving_ranker_fallback():
    history = [{
        "id": index + 1, "title": f"Positive {index}", "author": f"Writer {index}",
        "rating": 5, "read_at": "2020-01-01",
    } for index in range(8)]
    vectors = [np.asarray([1.0, 0.0], dtype=np.float32) for _ in history]
    candidate = {"id": 100, "title": "Query", "author": "New writer", "rating": 5,
                 "read_at": "2020-01-02"}
    query = np.asarray([-1.0, 0.0], dtype=np.float32)
    negative_cosines = np.full(len(history), -1.0)

    expected = rank_candidates(history, vectors, [candidate], [query])[0]["score"]
    actual = evaluator.score_from_similarities(history, candidate, negative_cosines)

    assert actual == expected


def test_similarity_replay_hides_every_read_from_the_query_day():
    from datetime import date

    history_row = {"id": 1, "title": "Prior", "author": "A", "rating": 5,
                   "read_at": "2020-01-01"}
    same_day_row = {"id": 2, "title": "Same day", "author": "B", "rating": 1,
                    "read_at": "2020-01-02"}
    query = {"id": 3, "title": "Query", "author": "C", "rating": 5,
             "read_at": "2020-01-02"}
    vectors = np.asarray([[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]], dtype=np.float32)
    records = [
        (date(2020, 1, 1), history_row, vectors[0]),
        (date(2020, 1, 2), same_day_row, vectors[1]),
        (date(2020, 1, 2), query, vectors[2]),
    ]
    similarities = evaluator._unit_rows(vectors) @ evaluator._unit_rows(vectors).T

    actual = evaluator.score_records_from_similarity(records, [2], similarities)
    reference = evaluator.score_from_similarities([history_row], query, similarities[2, [0]])

    np.testing.assert_array_equal(actual, [reference])


def test_similarity_replay_uses_serving_id_order_for_cosine_ties():
    from datetime import date

    vectors = np.tile(np.asarray([[1.0, 0.0]], dtype=np.float32), (42, 1))
    records = []
    records.append((date(2020, 1, 1), {
        "id": 41, "title": "Late id", "author": "Writer 41", "rating": 5,
        "read_at": "2020-01-01",
    }, vectors[0]))
    for idx in range(40):
        records.append((date(2020, 1, 2), {
            "id": idx + 1, "title": f"Earlier id {idx + 1}", "author": f"Writer {idx + 1}",
            "rating": 1 if idx < 20 else 5, "read_at": "2020-01-02",
        }, vectors[idx + 1]))
    query = {"id": 100, "title": "Query", "author": "Query writer", "rating": 5,
             "read_at": "2020-01-03"}
    records.append((date(2020, 1, 3), query, vectors[-1]))
    similarities = np.ones((len(records), len(records)), dtype=np.float32)
    chronological_history = [row[1] for row in records[:-1]]
    expected = rank_candidates(
        chronological_history,
        [row[2] for row in records[:-1]],
        [query],
        [records[-1][2]],
    )[0]["score"]

    actual = evaluator.score_records_from_similarity(records, [len(records) - 1], similarities)

    np.testing.assert_array_equal(actual, [expected])


def test_facets_keep_literal_evidence_and_leave_unsupported_fields_unknown():
    description = "During World War II, a detective protects her family."
    source = {
        "source": "Open Library",
        "source_reference": "private-record:17",
        "document_sha256": "a" * 64,
    }
    facets = evaluator.extract_grounded_facets(
        description=description,
        description_provenance={"source": "Open Library", "source_reference": "private-record:17",
                              "kind": "synopsis",
                              "document_sha256": "a" * 64},
        subjects=["Mystery", "Families"],
        subjects_provenance={**source, "source_field": "verified_subjects"},
    )

    assert facets["values"]["setting_time"] == ["past"]
    assert facets["values"]["synopsis_topics"] == ["crime_investigation", "family_relationship", "war_conflict"]
    evidence = facets["evidence"]["setting_time"]["past"][0]
    assert evidence["evidence_text"] == "During World War II"
    assert description[slice(*evidence["span"])] == evidence["evidence_text"]
    assert evidence["source_reference"] == "private-record:17"
    assert facets["evidence"]["catalog_subjects"][0]["evidence_text"] == "Mystery"

    empty = evaluator.extract_grounded_facets(
        description=None, description_provenance=None, subjects=None, subjects_provenance=None,
    )
    assert empty["values"] == {
        "catalog_subjects": "unknown",
        "synopsis_topics": "unknown",
        "setting_time": "unknown",
    }
    assert not any(empty["evidence"].values())

    unverified = evaluator.extract_grounded_facets(
        description="A future story about a murder.",
        description_provenance={"kind": "synopsis"},
        subjects=["Mystery"], subjects_provenance=None,
    )
    assert unverified["values"]["synopsis_topics"] == "unknown"
    assert unverified["values"]["catalog_subjects"] == "unknown"


def test_setting_time_requires_explicit_setting_or_temporal_context():
    provenance = {"source": "catalog", "source_reference": "private-record:21",
                  "kind": "synopsis", "document_sha256": "b" * 64}
    ambiguous = evaluator.extract_grounded_facets(
        description="The future of economics is uncertain; a historical novelist protects her family.",
        description_provenance=provenance, subjects=None, subjects_provenance=None,
    )
    assert ambiguous["values"]["setting_time"] == "unknown"

    explicit_text = "Set in a post-apocalyptic future, a family searches for shelter."
    explicit = evaluator.extract_grounded_facets(
        description=explicit_text,
        description_provenance={**provenance,
                                "document_sha256": evaluator.hashlib.sha256(
                                    explicit_text.encode()).hexdigest()},
        subjects=None, subjects_provenance=None,
    )
    assert explicit["values"]["setting_time"] == ["future"]
    evidence = explicit["evidence"]["setting_time"]["future"][0]
    assert explicit_text[slice(*evidence["span"])] == evidence["evidence_text"]
    assert evidence["evidence_text"].casefold() == "set in a post-apocalyptic future"


def test_metadata_field_provenance_requires_matching_identity_value_and_provider(monkeypatch):
    description = "Set in the 19th century, a detective solves a case."
    digest = evaluator.hashlib.sha256(description.encode()).hexdigest()
    read = {"id": 31}
    monkeypatch.setattr(evaluator, "_read_metadata_identity_hash", lambda value: "identity-ok")
    original = {"read_id": 31, "identity_hash": "identity-ok", "description": description,
                "description_kind": "synopsis"}
    supplemented = {
        "read_id": 31,
        "metadata_provenance": {"fields": {"description": {
            "kind": "synopsis", "provider": "catalog", "provider_id": "work:31",
            "source_field": "description", "value_sha256": digest,
        }}},
    }
    source_rows = [{"entity_type": "read", "entity_id": "31", "field": "description",
                    "provider": "catalog", "provider_id": "work:31", "confidence": 1.0}]
    provenance = evaluator._verified_field_provenance(
        read, original, supplemented, source_rows, "description")
    assert provenance["source_reference"] == "catalog:work:31"
    assert provenance["document_sha256"] == digest

    mismatched = [dict(source_rows[0], provider_id="other-work")]
    assert evaluator._verified_field_provenance(
        read, original, supplemented, mismatched, "description") is None
    opening = dict(original, description_kind="opening_sentence")
    assert evaluator._verified_field_provenance(
        read, opening, supplemented, source_rows, "description") is None


@pytest.mark.parametrize("subject_count", [9, 10, 11, 12])
def test_genre_provenance_hash_uses_production_twelve_subject_limit(monkeypatch, subject_count):
    genres = [f"Subject {index}" for index in range(1, subject_count + 1)]
    normalized = evaluator.normalize_subjects(
        genres, limit=evaluator.METADATA_GENRE_LIMIT)
    digest = evaluator.hashlib.sha256(evaluator.json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    read = {"id": 401}
    monkeypatch.setattr(evaluator, "_read_metadata_identity_hash", lambda value: "identity-ok")
    original = {
        "read_id": 401,
        "identity_hash": "identity-ok",
        "genres": genres,
    }
    source = {
        "provider": "catalog",
        "provider_id": "work:401",
        "source_field": "subjects",
    }
    supplemented = {
        "read_id": 401,
        "metadata_provenance": {"fields": {"genres": {
            "value_sha256": digest,
            "sources": [source],
        }}},
    }
    source_rows = [{
        "entity_type": "read", "entity_id": "401", "field": "genres",
        "provider": "catalog", "provider_id": "work:401", "confidence": 1.0,
    }]

    provenance = evaluator._verified_field_provenance(
        read, original, supplemented, source_rows, "genres")

    assert provenance is not None
    assert provenance["document_sha256"] == digest
    assert evaluator.normalize_subjects(
        genres, limit=evaluator.FACET_SUBJECT_FEATURE_LIMIT) == genres[:8]


def test_genre_provenance_rejects_a_tampered_twelfth_subject(monkeypatch):
    genres = [f"Subject {index}" for index in range(1, 13)]
    normalized = evaluator.normalize_subjects(
        genres, limit=evaluator.METADATA_GENRE_LIMIT)
    digest = evaluator.hashlib.sha256(evaluator.json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    read = {"id": 402}
    monkeypatch.setattr(evaluator, "_read_metadata_identity_hash", lambda value: "identity-ok")
    original = {"read_id": 402, "identity_hash": "identity-ok", "genres": genres}
    source = {"provider": "catalog", "provider_id": "work:402"}
    supplemented = {
        "read_id": 402,
        "metadata_provenance": {"fields": {"genres": {
            "value_sha256": digest, "sources": [source],
        }}},
    }
    source_rows = [{
        "entity_type": "read", "entity_id": "402", "field": "genres",
        "provider": "catalog", "provider_id": "work:402", "confidence": 1.0,
    }]
    tampered = dict(original, genres=genres[:-1] + ["Tampered twelfth subject"])

    assert evaluator._verified_field_provenance(
        read, tampered, supplemented, source_rows, "genres") is None


def test_grounded_records_verify_twelve_genres_but_keep_eight_as_features(tmp_path, monkeypatch):
    from datetime import date

    genres = [f"Subject {index}" for index in range(1, 13)]
    digest = evaluator.hashlib.sha256(evaluator.json.dumps(
        evaluator.normalize_subjects(genres, limit=evaluator.METADATA_GENRE_LIMIT),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    read = {"id": 403}
    monkeypatch.setattr(evaluator, "_read_metadata_identity_hash", lambda value: "identity-ok")
    original = {
        "read_id": 403,
        "identity_hash": "identity-ok",
        "genres": genres,
    }
    source = {
        "provider": "catalog",
        "provider_id": "work:403",
        "source_field": "subjects",
    }
    supplemented = {
        **original,
        "metadata_provenance": {"fields": {"genres": {
            "value_sha256": digest,
            "sources": [source],
        }}},
    }
    supplement_path = tmp_path / "provenance.json"
    supplement_path.write_text(evaluator.json.dumps({
        "read_metadata": [supplemented],
        "metadata_field_provenance": [{
            "entity_type": "read", "entity_id": "403", "field": "genres",
            "provider": "catalog", "provider_id": "work:403", "confidence": 1.0,
        }],
    }), encoding="utf-8")

    facets, _, summary = evaluator._grounded_facets_for_records(
        {"read_metadata": [original]}, supplement_path,
        [(date(2020, 1, 1), read, np.asarray([1.0, 0.0]))],
    )

    assert summary["eligible_verified_subject_count"] == 1
    assert facets[0]["values"]["catalog_subjects"] == [
        evaluator._normalize_subject(value) for value in genres[:8]
    ]
    assert len(facets[0]["evidence"]["catalog_subjects"]) == 8


def test_metric_facets_registration_locks_production_verification_and_feature_caps():
    facets = evaluator.preregistration_metadata()["facets"]

    assert facets["subject_provenance_normalization_limit"] == 12
    assert facets["subject_feature_limit"] == 8


def test_prior_recency_cluster_combo_adds_both_standardized_signals(monkeypatch):
    count = 8
    features = {
        "ratings": np.ones(count),
        "train_mask": np.asarray([1, 1, 1, 1, 1, 0, 0, 0], dtype=bool),
        "validation_mask": np.asarray([0, 0, 0, 0, 0, 1, 0, 0], dtype=bool),
        "later_mask": np.asarray([0, 0, 0, 0, 0, 0, 1, 1], dtype=bool),
        "recency": np.zeros(count),
        "cluster3": np.zeros(count),
        "ordinal_expected": np.zeros(count),
    }

    def fake_zscores(_features, name, _train_stop, evaluation_indexes, *, first_fold=False):
        value = {"recency": 1.0, "cluster3": 2.0, "ordinal_expected": -1.0}[name]
        return (np.full(len(evaluation_indexes), value), {"mean": 0.0, "sd": 1.0, "training_n": 4})

    monkeypatch.setattr(evaluator, "_signal_zscores", fake_zscores)
    folds = [
        {"train_stop": 2, "evaluation_start": 2, "evaluation_stop": 3},
        {"train_stop": 3, "evaluation_start": 3, "evaluation_stop": 4},
        {"train_stop": 4, "evaluation_start": 4, "evaluation_stop": 5},
    ]
    alignments = {"metric": {"validation_later_from_oof": {"reference_sd": 10.0}}}
    for fold in folds:
        alignments["metric"][f"oof_fold_{fold['evaluation_start']}_{fold['evaluation_stop']}"] = {
            "reference_sd": 10.0,
        }
    values, _ = evaluator._prior_signal_scores(
        features, {"metric": np.full(count, 50.0)}, alignments, folds)

    np.testing.assert_array_equal(values["metric_plus_recency"][2:5], [52.5, 52.5, 52.5])
    np.testing.assert_array_equal(values["metric_plus_cluster3"][2:5], [55.0, 55.0, 55.0])
    np.testing.assert_array_equal(values["metric_plus_recency_cluster3"][2:5],
                                  [57.5, 57.5, 57.5])
    assert values["metric_plus_recency_cluster3"][5] == 57.5


def test_facet_vocabulary_is_training_only_and_unknown_pairs_fall_back():
    facets = [
        evaluator.extract_grounded_facets(
            description="A future detective investigates a mystery.",
            description_provenance={"source": "catalog", "source_reference": "work:1", "kind": "synopsis"},
            subjects=["Mystery"], subjects_provenance={"source": "catalog", "source_reference": "work:1"},
        ),
        evaluator.extract_grounded_facets(
            description="A detective investigates a case.",
            description_provenance={"source": "catalog", "source_reference": "work:2", "kind": "synopsis"},
            subjects=["Mystery"], subjects_provenance={"source": "catalog", "source_reference": "work:2"},
        ),
        evaluator.extract_grounded_facets(
            description="", description_provenance=None,
            subjects=["Previously unseen subject"], subjects_provenance={"source": "catalog", "source_reference": "work:3"},
        ),
        evaluator.extract_grounded_facets(
            description=None, description_provenance=None, subjects=None, subjects_provenance=None,
        ),
    ]
    train = np.array([0, 1])
    facet_sim, observed, summary = evaluator.facet_similarity_matrix(facets, train)

    assert summary["training_subject_vocabulary_size"] == 1
    assert facet_sim[0, 1] > 0.0
    assert not observed[2, 0]  # Held-out subject was not in the training vocabulary.
    assert not observed[3, 0]  # Unsupported facets remain unknown.
    reference = np.full((4, 4), 0.3, dtype=np.float32)
    reference[2, 0] = 1.000001
    reference[0, 1] = 0.9
    facet_sim[0, 1] = 1.0
    blended = evaluator.blend_facet_similarity(reference, facet_sim, observed, 0.25)
    assert blended[2, 0] == reference[2, 0]
    assert blended[0, 1] == 1.0  # known agreement is added then clipped


def test_invalid_facet_weight_and_metric_inputs_fail_closed():
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        evaluator.blend_facet_similarity(np.eye(2), np.eye(2), np.eye(2, dtype=bool), 1.1)
    with pytest.raises(ValueError, match="integer stars"):
        evaluator.fit_supervised_metric(np.eye(9), [1, 2, 3, 4, 5, 1, 4.5, 3, 4], np.arange(8), components=4)


def test_preregistration_grid_is_small_fixed_and_aggregate_safe():
    registration = evaluator.preregistration_metadata()

    assert registration["metric"]["pca_components"] == [4, 8]
    assert registration["metric"]["lambdas"] == [0.0, 0.025, 0.05]
    assert registration["facets"]["blend_weights"] == [0.1, 0.25]
    assert "unknown values carry no feature" in registration["facets"]["unknown_policy"]
    assert "exact matched span" in registration["facets"]["evidence_policy"]
