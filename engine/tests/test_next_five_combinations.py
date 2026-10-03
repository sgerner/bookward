import importlib.util
from pathlib import Path

import numpy as np
import pytest


SPEC = importlib.util.spec_from_file_location(
    "next_five_combinations", Path(__file__).parents[1] / "scripts/evaluate_next_five_combinations.py"
)
study = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(study)


def test_selection_cannot_consult_future_labels_or_scores():
    y = np.asarray([1, 2, 3, 4, 5] * 8, dtype=float)
    current = y * 2
    candidate = y.copy()
    manifest = {"development_folds": [
        {"evaluation_start": 5, "evaluation_stop": 10},
        {"evaluation_start": 10, "evaluation_stop": 15},
        {"evaluation_start": 15, "evaluation_stop": 20},
    ]}
    result = study.selection({"ratings": y}, {"current": current, "candidate": candidate}, manifest)
    changed_y, changed_scores = y.copy(), candidate.copy()
    changed_y[20:] = 6 - changed_y[20:]
    changed_scores[20:] = np.nan
    assert study.selection({"ratings": changed_y},
                           {"current": current, "candidate": changed_scores}, manifest) == result


def test_bootstrap_is_paired_and_keeps_zero_delta_for_identical_scores():
    y = np.asarray([1, 4, 2, 5, 3] * 5, dtype=float)
    groups = np.repeat(np.arange(5), 5)
    scores = np.arange(len(y), dtype=float)
    result = study.grouped_bootstrap(y, scores, scores, groups, repetitions=20)
    assert result["valid_replicates"] == 20
    assert result["balanced_delta_ci95"] == [0.0, 0.0]
    assert result["group_count"] == 5


def test_score_join_rejects_different_ids_even_when_lengths_match(tmp_path):
    features = tmp_path / "features.npz"
    predictions = tmp_path / "scores.npz"
    np.savez(features, read_ids=[1, 2], current=[50.0, 60.0])
    np.savez(predictions, read_ids=[2, 1], selected_heads=[55.0, 65.0])
    with pytest.raises(ValueError, match="identity-aligned"):
        study.load_scores(features, [predictions])


def test_unknown_fit_prefix_does_not_hide_missing_evaluation_predictions():
    values = np.asarray([np.nan] * 5 + [1, 2, 3, 4, 5], dtype=float)
    manifest = {"development_folds": [{"evaluation_start": 5, "evaluation_stop": 10}]}
    y = np.asarray([1, 2, 3, 4, 5] * 2)
    study.selection({"ratings": y}, {"current": y, "candidate": values}, manifest)
    values[6] = np.nan
    with pytest.raises(ValueError, match="Missing OOF"):
        study.selection({"ratings": y}, {"current": y, "candidate": values}, manifest)


def test_aligned_pair_and_current_blend_use_serving_rounding(tmp_path):
    features = tmp_path / "features.npz"
    predictions = tmp_path / "scores.npz"
    masks = {name: np.asarray([value]) for name, value in
             (("train_mask", True), ("validation_mask", False), ("later_mask", False),
              ("oof_selection_mask", False))}
    np.savez(features, read_ids=[1], current=[50.0], **masks)
    np.savez(predictions, read_ids=[1], aligned_heads=[60.0], aligned_metric=[70.0], **masks)
    _, scores = study.load_scores(features, [predictions])
    assert scores["current75_heads25"].tolist() == [52.5]
    assert scores["pair_heads_metric"].tolist() == [65.0]


def test_same_id_score_archive_cannot_change_day_cuts(tmp_path):
    features, predictions = tmp_path / "features.npz", tmp_path / "scores.npz"
    masks = {name: np.asarray([value]) for name, value in
             (("train_mask", True), ("validation_mask", False), ("later_mask", False),
              ("oof_selection_mask", False))}
    np.savez(features, read_ids=[1], current=[50.0], **masks)
    masks["validation_mask"] = np.asarray([True])
    np.savez(predictions, read_ids=[1], selected_heads=[50.0], **masks)
    with pytest.raises(ValueError, match="split mask"):
        study.load_scores(features, [predictions])


def test_fold_cannot_cut_a_utc_day():
    features = {"utc_day": np.asarray([1, 1, 2, 3, 4]),
                "train_mask": np.asarray([1, 1, 1, 0, 0], dtype=bool),
                "validation_mask": np.asarray([0, 0, 0, 1, 0], dtype=bool),
                "later_mask": np.asarray([0, 0, 0, 0, 1], dtype=bool),
                "oof_selection_mask": np.asarray([0, 1, 1, 0, 0], dtype=bool)}
    manifest = {"development_folds": [{"train_stop": 1, "evaluation_start": 1, "evaluation_stop": 3}]}
    with pytest.raises(ValueError, match="UTC day"):
        study.validate_folds(features, manifest)


def test_three_star_is_neither_tail():
    result = study.metrics(np.asarray([1, 2, 3, 4, 5]), np.asarray([10, 20, 30, 40, 50]))
    assert result["high_n"] == 2
    assert result["low_n"] == 2
    assert result["balanced_auc"] == 1.0
