"""Invariants for the private causal representation comparison."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
import hashlib
import json

import numpy as np
import pytest

ENGINE = Path(__file__).parents[1]
sys.path.insert(0, str(ENGINE / "scripts"))
import evaluate_embedding_representations as study
from evaluate_ordinal_interests import history_components
from afterword_engine.ingestion import _read_metadata_identity_hash
from afterword_engine.covers import METADATA_GENRE_LIMIT
from afterword_engine.subjects import normalize_subjects


def test_instruction_prefix_is_applied_only_to_the_query_format():
    instruction = "Retrieve books with similar reader appeal."
    assert study.instructed_query("A Book by Someone", instruction) == (
        "Instruct: Retrieve books with similar reader appeal.\n"
        "Query: A Book by Someone"
    )


def test_identity_only_view_is_labeled_as_cache_reference_and_uses_title_author_fallback():
    reads = [
        {"id": 1, "title": "Verified title", "author": "A", "rating": 5},
        {"id": 2, "title": "Fallback title", "author": "B", "rating": 2},
    ]
    verified = {
        "read_id": 1,
        "verified_work_id": "works:/verified",
        "identity_hash": _read_metadata_identity_hash(reads[0]),
        "description": "An adventure among hidden islands.",
        "genres": '["Adventure", "Fantasy"]',
        "description_kind": "synopsis",
    }
    stale = {
        "read_id": 2,
        "verified_work_id": "works:/stale",
        "identity_hash": "0" * 64,
        "description": "Unverified evidence must be ignored.",
        "genres": '["Mystery"]',
    }
    rows = [
        (date(2020, 1, 1), reads[0], np.ones(2, dtype=np.float32)),
        (date(2020, 1, 2), reads[1], np.ones(2, dtype=np.float32)),
    ]
    views = study.build_text_views(
        {"reads": reads, "read_metadata": [verified, stale]}, rows
    )
    assert views["metadata_verification_mode"] == "identity_only"
    assert views["metadata_mask"].tolist() == [True, False]
    assert views["description_mask"].tolist() == [True, False]
    assert views["subjects_mask"].tolist() == [True, False]
    assert "hidden islands" in views["verified_metadata_text"][0]
    assert views["verified_metadata_text"][1] == views["title_author_text"][1]
    assert "rating" not in views["verified_metadata_text"][0].lower()
    assert views["invalid_metadata_rows"] == 1


def test_strict_provenance_requires_synopsis_and_hashes_all_production_genres():
    reads = [
        {"id": 1, "title": "Verified title", "author": "A", "rating": 5},
        {"id": 2, "title": "Opening title", "author": "B", "rating": 4},
        {"id": 3, "title": "Tampered title", "author": "C", "rating": 3},
    ]
    genres = [f"Genre Topic {index:02d}" for index in range(1, 13)]
    metadata_rows = []
    supplement_rows = []
    provenance_rows = []
    for item, description, description_kind, row_genres in (
        (reads[0], "A verified synopsis about a hidden city.", "synopsis", genres),
        (reads[1], "An opening sentence that is not a synopsis.", "opening_sentence", []),
        (reads[2], "", "", genres),
    ):
        read_id = item["id"]
        work_id = f"/works/OL{read_id}W"
        metadata = {
            "read_id": read_id,
            "verified_work_id": work_id,
            "identity_provider": "openlibrary",
            "identity_provider_id": work_id,
            "identity_hash": _read_metadata_identity_hash(item),
            "description": description,
            "description_kind": description_kind,
            "genres": json.dumps(row_genres, ensure_ascii=False, separators=(",", ":")),
        }
        fields = {}
        if description:
            fields["description"] = {
                "provider": "openlibrary",
                "provider_id": work_id,
                "kind": description_kind,
                "source_field": "description",
                "value_sha256": hashlib.sha256(description.encode("utf-8")).hexdigest(),
            }
            provenance_rows.append({
                "entity_type": "read", "entity_id": read_id, "field": "description",
                "provider": "openlibrary", "provider_id": work_id, "confidence": 1.0,
            })
        if row_genres:
            normalized_twelve = normalize_subjects(row_genres, limit=METADATA_GENRE_LIMIT)
            subject_hash = hashlib.sha256(json.dumps(
                normalized_twelve, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest()
            if read_id == 3:
                subject_hash = "0" * 64
            fields["genres"] = {
                "provider": "openlibrary",
                "provider_id": work_id,
                "kind": "subjects",
                "source_field": "subject",
                "value_sha256": subject_hash,
                "sources": [{
                    "provider": "openlibrary", "provider_id": work_id,
                    "kind": "subjects", "source_field": "subject",
                }],
            }
            provenance_rows.append({
                "entity_type": "read", "entity_id": read_id, "field": "genres",
                "provider": "openlibrary", "provider_id": work_id, "confidence": 1.0,
            })
        metadata_rows.append(metadata)
        supplement_rows.append({**metadata, "metadata_provenance": {"fields": fields}})
    records = [
        (date(2020, 1, index + 1), item, np.ones(2, dtype=np.float32))
        for index, item in enumerate(reads)
    ]
    views = study.build_text_views(
        {"reads": reads, "read_metadata": metadata_rows}, records,
        provenance_supplement={
            "read_metadata": supplement_rows,
            "metadata_field_provenance": provenance_rows,
        },
    )
    assert views["metadata_mask"].tolist() == [True, False, False]
    assert views["description_mask"].tolist() == [True, False, False]
    assert views["subjects_mask"].tolist() == [True, False, False]
    assert "Genre Topic 08" in views["verified_metadata_text"][0]
    assert "Genre Topic 09" not in views["verified_metadata_text"][0]
    assert views["verified_metadata_text"][1:] == views["title_author_text"][1:]
    assert views["metadata_sources"]["opening_sentence_fields_rejected"] == 1
    assert views["metadata_sources"]["unverified_subject_fields_rejected"] == 1


def test_cached_vectors_reembed_only_exact_text_hash_mismatches():
    old_texts = ["unchanged", "old input", "also unchanged"]
    new_texts = ["unchanged", "new input", "also unchanged"]
    cached = np.arange(6, dtype=np.float32).reshape(3, 2)
    calls = []

    def encode(texts):
        calls.extend(texts)
        return np.full((len(texts), 2), 9.0, dtype=np.float32)

    result, counts = study._reuse_cached_vectors(
        new_texts, old_texts, cached, dimensions=2, encoder=encode
    )
    assert calls == ["new input"]
    np.testing.assert_array_equal(result[[0, 2]], cached[[0, 2]])
    np.testing.assert_array_equal(result[1], np.array([9.0, 9.0], dtype=np.float32))
    assert counts == {"rows": 3, "reused_rows": 2, "freshly_encoded_rows": 1}


def test_metadata_coverage_union_includes_subject_only_and_description_only_rows():
    reads = [
        {"id": 1, "title": "Description", "author": "A", "rating": 5},
        {"id": 2, "title": "Subjects", "author": "B", "rating": 4},
        {"id": 3, "title": "Both", "author": "C", "rating": 3},
        {"id": 4, "title": "Fallback", "author": "D", "rating": 2},
    ]
    metadata = []
    for item, description, genres in (
        (reads[0], "A verified synopsis.", "[]"),
        (reads[1], "", '["Fantasy"]'),
        (reads[2], "Another verified synopsis.", '["Mystery"]'),
        (reads[3], "", "[]"),
    ):
        metadata.append({
            "read_id": item["id"],
            "verified_work_id": f"works:/{item['id']}",
            "identity_hash": _read_metadata_identity_hash(item),
            "description": description,
            "genres": genres,
            "description_kind": "synopsis",
        })
    rows = [
        (date(2020, 1, index + 1), item, np.ones(2, dtype=np.float32))
        for index, item in enumerate(reads)
    ]
    views = study.build_text_views({"reads": reads, "read_metadata": metadata}, rows)
    union = views["description_mask"] | views["subjects_mask"]
    np.testing.assert_array_equal(views["metadata_mask"], union)
    assert int(views["metadata_mask"].sum()) == 3
    assert int(views["description_mask"].sum()) == 2
    assert int(views["subjects_mask"].sum()) == 2
    assert int(views["metadata_mask"].sum()) >= int(views["description_mask"].sum())
    assert int(views["metadata_mask"].sum()) >= int(views["subjects_mask"].sum())


def test_sparse_similarity_components_match_dense_reference():
    rng = np.random.default_rng(20261002)
    history = []
    for index in range(110):
        history.append({
            "id": index + 1,
            "title": f"History {index}",
            "author": f"Author {index % 13}",
            "rating": float((index % 5) + 1),
            "read_at": (date(2010, 1, 1) + timedelta(days=index)).isoformat(),
        })
    query = [
        {"id": 1001 + index, "title": f"Query {index}",
         "author": f"Author {index % 7}", "rating": 5.0,
         "read_at": "2020-01-01"}
        for index in range(4)
    ]
    history_vectors = study.unit_rows(rng.normal(size=(len(history), 24)).astype(np.float32))
    query_vectors = study.unit_rows(rng.normal(size=(len(query), 24)).astype(np.float32))
    similarities = query_vectors @ history_vectors.T
    expected_features, expected_scores = history_components(
        history, history_vectors, query, query_vectors
    )
    features, scores = study.history_components_from_similarities(
        history, query, similarities
    )
    np.testing.assert_allclose(features, expected_features, rtol=1e-6, atol=3e-7)
    np.testing.assert_array_equal(np.round(scores, 1), np.round(expected_scores, 1))


def test_tfidf_fits_vocabulary_only_on_the_training_prefix():
    train = ["knownword familiartitle"]
    all_text = ["knownword familiartitle", "qzxvplmfuture"]
    matrix, info = study.tfidf_vectors(train, all_text)
    assert matrix[0].nnz > 0
    assert matrix[1].nnz == 0
    assert info["train_documents"] == 1
    assert info["zero_rows"] == 1


def test_capped_residual_uses_causal_prefix_and_never_exceeds_fixed_cap():
    rng = np.random.default_rng(17)
    current = np.linspace(40, 60, 100)
    features = rng.normal(size=(100, 4))
    ratings = np.clip(1 + 4 * (current - 40) / 20 + 0.4 * features[:, 0], 1, 5)
    folds = [{"train_stop": 50, "evaluation_start": 50, "evaluation_stop": 100}]
    values = study.capped_residual_predictions(
        current, ratings, [features], folds
    )
    cap = min(5.0, 0.25 * float(current[:50].std()))
    np.testing.assert_array_equal(values[:50], current[:50])
    assert np.max(np.abs(values[50:] - current[50:])) <= cap + 0.051
    assert np.isfinite(values).all()


def test_development_oof_mask_excludes_validation_and_later_targets():
    train = np.arange(12) < 7
    mask = study.development_oof_mask(train, first_oof_start=3)
    np.testing.assert_array_equal(np.flatnonzero(mask), np.arange(3, 7))


def test_unique_cache_size_does_not_count_huggingface_snapshot_symlinks(tmp_path):
    blob = tmp_path / "blob"
    blob.write_bytes(b"model-bytes")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "model.onnx").symlink_to(blob)
    assert study.unique_file_bytes(tmp_path) == len(b"model-bytes")


@pytest.mark.parametrize("missing_name", [
    "strict-amendment.json",
    "method-amendment.json",
    "provenance-supplement.json",
])
def test_build_experiment_preflights_strict_inputs_before_model_calls(
    tmp_path, monkeypatch, missing_name
):
    paths = {
        name: tmp_path / name
        for name in (
            "corpus.json", "features.npz", "feature-manifest.json", "protocol.json",
            "strict-amendment.json", "method-amendment.json", "provenance-supplement.json",
            "prior-vectors.npz", "prior-aggregate.json", "fresh-corpus.json",
            "fresh-vectors.npz", "fresh-manifest.json",
        )
    }
    for name, path in paths.items():
        if name != missing_name:
            path.write_bytes(b"placeholder")

    model_calls = []
    monkeypatch.setattr(study, "ollama_vectors", lambda *args, **kwargs: model_calls.append("qwen"))
    monkeypatch.setattr(study, "fastembed_reuse_views", lambda *args, **kwargs: model_calls.append("bge"))
    output = tmp_path / "private-output"
    cache = tmp_path / "model-cache"

    with pytest.raises(FileNotFoundError, match=missing_name):
        study.build_experiment(
            corpus_path=paths["corpus.json"],
            feature_path=paths["features.npz"],
            feature_manifest_path=paths["feature-manifest.json"],
            protocol_path=paths["protocol.json"],
            strict_amendment_path=paths["strict-amendment.json"],
            method_amendment_path=paths["method-amendment.json"],
            provenance_supplement_path=paths["provenance-supplement.json"],
            prior_vectors_path=paths["prior-vectors.npz"],
            prior_aggregate_path=paths["prior-aggregate.json"],
            fresh_corpus_path=paths["fresh-corpus.json"],
            fresh_vectors_path=paths["fresh-vectors.npz"],
            fresh_manifest_path=paths["fresh-manifest.json"],
            private_output=output,
            bge_cache_dir=cache,
        )

    assert model_calls == []
    assert not output.exists()
    assert not cache.exists()


def test_public_cli_requires_the_frozen_provenance_inputs(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "evaluate_embedding_representations.py",
        "--corpus", "/private/corpus.json",
        "--features", "/private/features.npz",
        "--feature-manifest", "/private/features-manifest.json",
        "--protocol", "/private/protocol.json",
        "--strict-amendment", "/private/strict-amendment.json",
        "--method-amendment", "/private/method-amendment.json",
        "--prior-vectors", "/private/prior-vectors.npz",
        "--prior-aggregate", "/private/prior-aggregate.json",
        "--fresh-corpus", "/private/fresh-corpus.json",
        "--fresh-vectors", "/private/fresh-vectors.npz",
        "--fresh-manifest", "/private/fresh-manifest.json",
        "--private-output", "/private/output",
        "--bge-cache-dir", "/private/model-cache",
    ])
    model_calls = []
    monkeypatch.setattr(study, "ollama_vectors", lambda *args, **kwargs: model_calls.append("qwen"))
    monkeypatch.setattr(study, "fastembed_reuse_views", lambda *args, **kwargs: model_calls.append("bge"))

    with pytest.raises(SystemExit) as exc_info:
        study.main()

    assert exc_info.value.code == 2
    assert "--provenance-supplement" in capsys.readouterr().err
    assert model_calls == []
