"""Focused invariants for the label-blind reading-experience pilot."""
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest


ENGINE = Path(__file__).parents[1]
sys.path.insert(0, str(ENGINE / "scripts"))
import evaluate_experience as experience
from afterword_engine.ingestion import _read_metadata_identity_hash
from afterword_engine.subjects import normalize_subjects


def _verified(text: str) -> dict:
    import hashlib

    return {
        "kind": "synopsis",
        "source": "openlibrary",
        "source_reference": "openlibrary:/works/OL1W",
        "document_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def test_extractor_only_uses_exact_literal_cues_from_verified_synopsis():
    synopsis = "A propulsive, fast-paced mystery."
    extracted = experience.extract_experience(synopsis, _verified(synopsis))

    assert extracted["values"]["pace_fast"] == 1
    assert extracted["known"]["pace"] == 1
    assert extracted["known"]["tone"] == 0
    assert [item["evidence_text"] for item in extracted["evidence"]["pace_fast"]] == [
        "fast-paced", "propulsive",
    ]

    unverified = experience.extract_experience(
        synopsis, {**_verified(synopsis), "source_reference": ""})
    assert not any(unverified["values"].values())
    assert not any(unverified["known"].values())


def test_extractor_leaves_negated_cues_unknown_instead_of_imputing_opposite():
    synopsis = "This is not a fast-paced story and is not hopeful."
    extracted = experience.extract_experience(synopsis, _verified(synopsis))

    assert not any(extracted["values"].values())
    assert not any(extracted["known"].values())


def test_extractor_does_not_expand_the_frozen_lexicon_to_synonyms():
    synopsis = "A funny, hilarious, warm story that readers will love."
    extracted = experience.extract_experience(synopsis, _verified(synopsis))

    assert not any(extracted["values"].values())
    assert not any(extracted["known"].values())


def test_source_coverage_gate_stops_when_registered_thresholds_fail():
    result = experience.coverage_gate({
        "any_supported_experience_cue": 45,
        "experience_group_coverage": {"pace": 10, "tone": 33, "density": 1, "structure": 2},
        "experience_group_coverage_initial_300": {"pace": 3, "tone": 7, "density": 0, "structure": 0},
    })

    assert result["passed"] is False
    assert result["supported_groups"] == ["tone"]
    assert result["checks"] == {
        "minimum_any_supported_cue": False,
        "minimum_supported_groups": False,
        "minimum_initial_prefix_support": False,
    }


def test_disabled_score_archive_is_id_aligned_current_fallback_without_labels(tmp_path):
    features = tmp_path / "features.npz"
    ids = np.array([12, 4, 99], dtype=np.int64)
    current = np.array([62.5, 41.0, 78.25], dtype=np.float64)
    train = np.array([True, False, False])
    validation = np.array([False, True, False])
    later = np.array([False, False, True])
    oof = np.array([True, True, False])
    np.savez_compressed(
        features,
        read_ids=ids,
        current=current,
        train_mask=train,
        validation_mask=validation,
        later_mask=later,
        oof_selection_mask=oof,
        ratings=np.array([5.0, 1.0, 4.0]),
    )

    output = experience._write_disabled_score_archive(features, tmp_path)
    assert output.stat().st_mode & 0o777 == 0o600
    with np.load(output, allow_pickle=False) as archive:
        assert set(archive.files) == {
            "read_ids", "current", "train_mask", "validation_mask", "later_mask",
            "oof_selection_mask", "score__current", "score__experience_disabled_fallback",
        }
        np.testing.assert_array_equal(archive["read_ids"], ids)
        np.testing.assert_array_equal(archive["current"], current)
        np.testing.assert_array_equal(archive["score__current"], current)
        np.testing.assert_array_equal(archive["score__experience_disabled_fallback"], current)


def test_subject_verification_hashes_all_twelve_but_topic_features_stay_capped_at_eight():
    read = {"id": 17, "title": "A private fixture", "author": "Fixture Author", "isbn": ""}
    genres = [f"subject {index}" for index in range(1, 12)] + ["Fiction"]
    original = {
        "read_id": read["id"],
        "identity_hash": _read_metadata_identity_hash(read),
        "genres": genres,
    }
    normalized_twelve = normalize_subjects(
        genres, limit=experience.grounded.METADATA_GENRE_LIMIT
    )
    value_hash = hashlib.sha256(json.dumps(
        normalized_twelve, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    provider = {"provider": "openlibrary", "provider_id": "/works/OL17W", "source_field": "subjects"}
    supplemented = {
        "read_id": read["id"],
        "metadata_provenance": {
            "fields": {"genres": {"value_sha256": value_hash, "sources": [provider]}}
        },
    }
    field_rows = [{
        "entity_type": "read", "entity_id": read["id"], "field": "genres",
        "confidence": 1.0, "provider": provider["provider"],
        "provider_id": provider["provider_id"],
    }]

    verified = experience.grounded._verified_field_provenance(
        read, original, supplemented, field_rows, "genres"
    )
    assert verified
    assert "fiction" not in {
        value.casefold() for value in normalize_subjects(
            genres, limit=experience.grounded.FACET_SUBJECT_FEATURE_LIMIT
        )
    }
    assert experience._stratum(
        {"description_provenance": {"kind": "synopsis"},
         "subjects_provenance": verified, "original": original},
        {"known": {}},
    ) == "rich_fiction"

    tampered = {**original, "genres": [*genres[:-1], "tampered twelfth subject"]}
    assert experience.grounded._verified_field_provenance(
        read, tampered, supplemented, field_rows, "genres"
    ) is None


def test_correction_freeze_locks_prior_sample_before_pilot_outputs(tmp_path):
    prior = tmp_path / "prior"
    prior.mkdir()
    protocol_path = prior / "experience-protocol-private.json"
    report_path = prior / "experience-pilot-report-private.json"
    evidence_path = prior / "experience-pilot-evidence-private.json"
    protocol_path.write_text(json.dumps({
        "pilot": {"label_blind": True}, "schema_sha256": "old-schema",
        "advance_gate": {"minimum_any_supported_cue": 100},
    }))
    report_path.write_text(json.dumps({
        "protocol_sha256": experience.sha256_file(protocol_path),
        "pilot": {"labels_accessed": False, "feature_manifest_sha256": "features",
                  "corpus_sha256": "corpus", "provenance_sha256": "provenance"},
        "coverage_gate": {"passed": False}, "appeal_model_fitted": False,
    }))
    evidence_path.write_text(json.dumps({
        "rows": [{"read_id": value, "stratum": "unknown_genre"}
                 for value in range(1, experience.TARGET_SAMPLE_SIZE + 1)]
    }))
    out = tmp_path / "corrected"

    protocol = experience.freeze_protocol(out, correction_from=prior)
    lock = json.loads((out / "experience-frozen-sample-lock-private.json").read_text())
    amendment = json.loads((out / "experience-protocol-amendment-private.json").read_text())

    assert protocol["source_rules"]["subject_value_hash_normalization_limit"] == 12
    assert protocol["source_rules"]["subject_feature_limit"] == 8
    assert amendment["status"] == "frozen_before_corrected_outputs"
    assert [row["read_id"] for row in lock["rows"]] == list(
        range(1, experience.TARGET_SAMPLE_SIZE + 1)
    )
    assert not (out / "experience-pilot-report-private.json").exists()
