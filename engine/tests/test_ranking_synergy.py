"""Meaningful invariants for the bounded combined-score evaluator."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "evaluate_ranking_synergy.py"
spec = importlib.util.spec_from_file_location("evaluate_ranking_synergy", SCRIPT)
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


def test_arm_blends_use_frozen_training_scalers_and_preserve_current_order():
    current = np.array([10.0, 20.0, 30.0, 40.0])
    cluster = np.array([1.0, 3.0, 5.0, 1000.0])
    signals = {
        "cluster": cluster,
        "recency": np.array([3.0, 1.0, 2.0, -500.0]),
        "local": np.array([2.0, 4.0, 3.0, 900.0]),
        "ordinal": np.array([2.0, 4.0, 3.0, 5.0]),
    }
    train = np.array([0, 1])

    scores, scalers = evaluator.build_arm_scores(current, signals, train)
    expected_current = (current - 15.0) / 5.0
    expected_cluster = (cluster - 2.0) / 1.0

    np.testing.assert_allclose(scores["current"], expected_current)
    np.testing.assert_allclose(scores["cluster"], expected_current + 0.25 * expected_cluster)
    assert scalers["cluster"] == {"mean": 2.0, "scale": 1.0}
    # Altering held-out signal values cannot change any training-fitted scale.
    changed = {name: values.copy() for name, values in signals.items()}
    changed["cluster"][2:] = [-1e9, 1e9]
    _, changed_scalers = evaluator.build_arm_scores(current, changed, train)
    assert scalers == changed_scalers
    assert np.array_equal(np.argsort(scores["current"]), np.argsort(current))


def test_validation_selection_requires_both_auc_directions_and_breaks_ties_simply():
    def result(high, low):
        return {"auc_high_4plus": high, "auc_low_2or_less_reversed": low}

    results = {
        "current": result(0.80, 0.80),
        "cluster": result(0.90, 0.78),  # Fails the low-AUC gate.
        "recency": result(0.805, 0.80),
        "local": result(0.82, 0.80),
        "ordinal": result(0.82, 0.80),
        "cluster_recency": result(0.82, 0.80),
    }

    selected, eligibility = evaluator.select_validation_arm(results)

    assert eligibility["cluster"]["qualifies"] is False
    assert selected == "local"  # Equal one-signal arms use registered order.


def test_validation_falls_back_to_current_when_every_challenger_is_ineligible():
    baseline = {"auc_high_4plus": 0.6, "auc_low_2or_less_reversed": 0.6}
    results = {name: baseline for name in evaluator.ARMS}

    selected, _ = evaluator.select_validation_arm(results)

    assert selected == "current"


def test_preregistration_metadata_does_not_claim_chronology_was_verified():
    result = evaluator.preregistration_metadata()

    assert result["fixed_grid"] is True
    assert "registered_before_scoring" not in result
    assert "not verified by the evaluator" in result["registration_verification"].lower()


def test_pair_interaction_is_the_registered_descriptive_residual():
    def result(value):
        return {"auc_high_4plus": value, "auc_low_2or_less_reversed": value}

    results = {
        "current": result(0.80),
        "cluster": result(0.81),
        "recency": result(0.805),
        "cluster_recency": result(0.8175),
    }

    assert evaluator.interaction_residual(results, "cluster_recency") == pytest.approx(0.0025)


def test_cluster_bootstrap_resamples_whole_paired_groups():
    ratings = np.tile(np.array([5.0, 1.0]), 6)
    current = np.tile(np.array([0.0, 1.0]), 6)
    candidate = np.tile(np.array([1.0, 0.0]), 6)
    groups = [np.array([2 * i, 2 * i + 1]) for i in range(6)]

    result = evaluator.paired_cluster_bootstrap(
        ratings, current, candidate, groups,
        iterations=40, seed=12, unit="synthetic author clusters",
    )

    assert result["status"] == "ok"
    assert result["valid_high_resamples"] == 40
    assert result["valid_low_resamples"] == 40
    assert result["high_auc_delta_95pct"] == [1.0, 1.0]
    assert result["low_auc_delta_95pct"] == [1.0, 1.0]


def test_calendar_blocks_are_contiguous_and_fail_closed_when_too_few():
    origin = datetime(2024, 1, 1, tzinfo=timezone.utc)
    records = [(origin + timedelta(days=day), {}, np.zeros(2)) for day in (0, 15, 30, 61)]
    groups = evaluator.calendar_block_clusters(records, np.arange(4), block_days=30)
    too_few = evaluator.paired_cluster_bootstrap(
        np.tile(np.array([5.0, 1.0]), 2),
        np.tile(np.array([0.0, 1.0]), 2),
        np.tile(np.array([1.0, 0.0]), 2),
        groups[:2], iterations=40, unit="calendar blocks",
    )

    assert [len(group) for group in groups] == [2, 1, 1]
    assert too_few["status"] == "insufficient_groups"
    assert too_few["groups"] == 2


def test_joint_score_arrays_use_one_fresh_reference_and_frozen_signal_scalers():
    fresh = np.array([10.0, 20.0, 30.0, 40.0])
    fresh_ess5 = np.array([12.0, 22.0, 32.0, 42.0])
    metadata = np.array([100.0, 200.0, 300.0, 400.0])
    metadata_ess5 = np.array([110.0, 210.0, 310.0, 410.0])
    ordinal = np.array([1.0, 2.0, 3.0, 4.0])
    cluster = np.array([-2.0, 2.0, 1.0, -1.0])
    train = np.array([0, 1])

    scores, scalers = evaluator.build_joint_followup_scores(
        fresh, fresh_ess5, metadata, metadata_ess5, ordinal, cluster, train
    )

    np.testing.assert_allclose(scores["fresh_ta_current"], [-1.0, 1.0, 3.0, 5.0])
    np.testing.assert_allclose(scores["selected_metadata_current"], [17.0, 37.0, 57.0, 77.0])
    np.testing.assert_allclose(
        scores["selected_metadata_plus_ordinal"],
        scores["selected_metadata_current"] + 0.25 * np.array([-1.0, 1.0, 3.0, 5.0]),
    )
    assert scalers["reference_train_mean"] == 15.0
    assert scalers["reference_train_scale"] == 5.0


def test_joint_score_builder_keeps_unscored_history_prefix_unavailable():
    nan = np.nan
    scores, _ = evaluator.build_joint_followup_scores(
        np.array([nan, 10.0, 20.0, 30.0]),
        np.array([nan, 11.0, 21.0, 31.0]),
        np.array([nan, 12.0, 22.0, 32.0]),
        np.array([nan, 13.0, 23.0, 33.0]),
        np.array([nan, 1.0, 2.0, 3.0]),
        np.array([nan, -1.0, 1.0, 0.0]),
        np.array([1, 2]),
    )

    assert np.isnan(scores["fresh_ta_current"][0])
    assert np.isnan(scores["fresh_ta_plus_ordinal"][0])
    assert np.isfinite(scores["selected_metadata_plus_cluster"][1:]).all()


def test_score_archive_joins_by_id_and_fails_on_duplicate_ids(tmp_path):
    path = tmp_path / "scores.npz"
    np.savez(path, read_ids=np.array(["extra", "book-b", "book-a"]), score=np.array([9.0, 2.0, 1.0]))

    np.testing.assert_array_equal(
        evaluator.align_npz_array(path, "score", ["book-a", "book-b"]),
        np.array([1.0, 2.0]),
    )
    np.savez(path, read_ids=np.array(["book-a", "book-a"]), score=np.array([1.0, 2.0]))
    with pytest.raises(ValueError, match="duplicate IDs"):
        evaluator.align_npz_array(path, "score", ["book-a", "book-b"])

    np.savez(path, read_ids=np.array(["book-a"]), score=np.array([1.0]))
    with pytest.raises(ValueError, match="missing 1 causal target IDs"):
        evaluator.align_npz_array(path, "score", ["book-a", "book-b"])


def test_joint_provenance_checks_optional_manifest_and_fresh_ess5_when_available(tmp_path):
    corpus = tmp_path / "corpus.json"
    corpus.write_text("{}\n")
    scores = tmp_path / "scores.npz"
    history = tmp_path / "history.npz"
    np.savez(scores, read_ids=np.array([1]), fresh_ta_current=np.array([50.0]))
    np.savez(history, read_ids=np.array([1]), score_current=np.array([50.0]),
             snapshot_sha256=np.asarray(evaluator.sha256_file(corpus)))

    parity = {
        arm: {"compared": 1677, "mismatches": 0, "max_error": 0.0}
        for arm in ("fresh_ta", "metadata_combined")
    }
    frozen = {
        key: {"n": 355, "mismatches": 0, "max_error": 0.0}
        for key in (
            "fresh_ta_current", "metadata_combined_current", "metadata_combined_ess5",
            "fresh_ta_ess5",
        )
    }
    preregistration = tmp_path / "joint-preregistration.json"
    preregistration.write_text('{"version":1,"fixed":true}\n')
    report = tmp_path / "report.json"
    report_data = {
        "corpus_sha256": evaluator.sha256_file(corpus),
        "score_array_sha256": evaluator.sha256_file(scores),
        "joint_preregistration_sha256": evaluator.sha256_file(preregistration),
        "selected_current_arm_on_primary_validation": "metadata_combined",
        "direct_rank_candidates_current_parity": parity,
        "full_replay_matches_frozen_validation_arrays": frozen,
    }
    report.write_text(json.dumps(report_data))

    accepted = evaluator.validate_joint_provenance(
        corpus, scores, report, history, "metadata_combined", preregistration
    )
    assert accepted["selected_view_matches_frozen_validation_arrays"] is True
    assert accepted["joint_preregistration_sha256"] == evaluator.sha256_file(preregistration)
    assert accepted["joint_preregistration_hash_verified"] is True

    for field, value, message in (
        ("selected_current_arm_on_primary_validation", "equal_vector_blend", "winner"),
        ("score_array_sha256", "wrong", "hash"),
        ("corpus_sha256", "wrong", "corpus"),
        ("joint_preregistration_sha256", "wrong", "preregistration hash"),
    ):
        corrupted = dict(report_data)
        corrupted[field] = value
        report.write_text(json.dumps(corrupted))
        with pytest.raises(ValueError, match=message):
            evaluator.validate_joint_provenance(
                corpus, scores, report, history, "metadata_combined", preregistration
            )

    corrupted = json.loads(json.dumps(report_data))
    corrupted["full_replay_matches_frozen_validation_arrays"]["fresh_ta_ess5"]["max_error"] = 0.1
    report.write_text(json.dumps(corrupted))
    with pytest.raises(ValueError, match="fresh_ta_ess5"):
        evaluator.validate_joint_provenance(
            corpus, scores, report, history, "metadata_combined", preregistration
        )

    # The optional manifest preserves legacy joint invocations, but provenance
    # must say that its hash was not checked when the caller omits the path.
    legacy = dict(report_data)
    del legacy["full_replay_matches_frozen_validation_arrays"]["fresh_ta_ess5"]
    report.write_text(json.dumps(legacy))
    accepted_legacy = evaluator.validate_joint_provenance(
        corpus, scores, report, history, "metadata_combined"
    )
    assert accepted_legacy["joint_preregistration_sha256"] is None
    assert accepted_legacy["joint_preregistration_hash_verified"] is False


def test_joint_followup_runs_by_explicit_id_and_emits_aggregate_only(tmp_path):
    count = 40
    origin = datetime(2020, 1, 1, tzinfo=timezone.utc)
    records = [
        (origin + timedelta(days=index), {"id": index, "author": f"author-{index % 7}"}, np.zeros(2))
        for index in range(count)
    ]
    items = [row[1] for row in records]
    ratings = np.tile(np.array([5.0, 1.0, 3.0, 4.0, 2.0]), 8)
    train, validation, later = np.arange(20), np.arange(20, 30), np.arange(30, 40)
    current = np.linspace(30.0, 70.0, count)
    order = np.arange(count)[::-1]
    metadata_path = tmp_path / "metadata.npz"
    np.savez(
        metadata_path,
        read_ids=np.arange(count)[order],
        fresh_ta_current=current[order],
        fresh_ta_ess5=(current + 1.0)[order],
        metadata_combined_current=(current + 2.0)[order],
        metadata_combined_ess5=(current + 3.0)[order],
        primary_isbn_identity_mask=(np.arange(count) % 2 == 0)[order],
        secondary_title_author_only_mask=(np.arange(count) % 5 == 0)[order],
    )
    history_path = tmp_path / "history.npz"
    np.savez(
        history_path,
        read_ids=np.arange(count)[order],
        score_current=current.astype(np.float32)[order],
        snapshot_sha256=np.asarray("synthetic-corpus-hash"),
    )
    history_check = evaluator.compare_history_current_archive(
        history_path, list(range(count)), current
    )
    assert history_check["count_abs_error_gt_0_05"] == 0
    assert history_check["float32_exact_mismatches"] == 0
    assert history_check["served_precision_one_decimal_mismatches"] == 0

    result = evaluator.run_joint_followup(
        records, items, ratings, train, validation, later,
        np.linspace(1.0, 5.0, count), np.sin(np.arange(count)),
        metadata_path, history_check, "metadata_combined",
        iterations=40, seed=17, block_days=1,
    )

    assert result["score_array_checks"]["joint_target_count"] == count
    assert result["score_array_checks"]["history_current_archive_comparison"][
        "served_precision_one_decimal_mismatches"
    ] == 0
    assert result["selected_metadata_arm_from_validation_only"] == "metadata_combined"
    assert result["later_exploratory"]["arms"]["selected_metadata_plus_ordinal"]["metrics"]["n"] == 10
    assert "book-" not in json.dumps(result)
