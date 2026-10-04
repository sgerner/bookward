from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from afterword_engine.publication_era_signal import (
    MAX_SCORE_ADJUSTMENT,
    _EraModel,
    fit_publication_era_signal,
)
from afterword_engine.ranking import rank_candidates


def _history(*, era_effect: bool, count: int = 120):
    rows = []
    vectors = []
    era_years = list(range(1920, 2021, 5))
    start = date(2021, 1, 1)
    for index in range(count):
        year = era_years[index % len(era_years)]
        quality = (2.4, 2.9, 3.3, 3.7, 4.0)[(index * 7) % 5]
        favored = year <= 1950 or year >= 1990
        residual = (0.85 if favored else -0.85) if era_effect else 0.0
        rating = min(5.0, max(1.0, quality + residual))
        rows.append({
            "id": index + 1,
            "author": f"author-{index % 30}",
            "rating": rating,
            "read_at": (start + timedelta(days=index)).isoformat(),
            "first_publication_year": year,
            "baseline_score": 50.0 + 10.0 * (quality - 3.0),
        })
        vectors.append([float(index), 1.0])
    return rows, vectors


def _baseline(history_rows, history_vectors, query_rows, query_vectors):
    # This fixture is deliberately an existing-score oracle.  Its history
    # arguments are still checked below to exercise the temporal contract.
    return [row["baseline_score"] for row in query_rows]


def test_nonlinear_era_residual_activates_only_after_all_gated_folds() -> None:
    rows, vectors = _history(era_effect=True)
    rows[0]["first_publication_year"] = None
    calls = []

    def baseline(history_rows, history_vectors, query_rows, query_vectors):
        if calls:
            query_start = date.fromisoformat(query_rows[0]["read_at"][:10])
            assert all(date.fromisoformat(row["read_at"][:10]) < query_start for row in history_rows)
        else:
            # Seed scores are training-only and intentionally use the history
            # snapshot immediately before the first held-out fold.
            first_validation_day = date.fromisoformat(rows[52]["read_at"][:10])
            assert all(date.fromisoformat(row["read_at"][:10]) < first_validation_day for row in history_rows)
        calls.append((len(history_rows), len(query_rows), any(
            row.get("first_publication_year") is None for row in history_rows
        )))
        return _baseline(history_rows, history_vectors, query_rows, query_vectors)

    fit = fit_publication_era_signal(rows, vectors, baseline)

    assert fit.activated
    assert fit.diagnostics["reason"] == "all_three_folds_passed"
    assert len(fit.diagnostics["folds"]) == 3
    assert all(fold["status"] == "passed" for fold in fit.diagnostics["folds"])
    assert len(calls) == 4  # one seed score pass plus three expanding folds
    assert any(has_missing_publication_year for _, _, has_missing_publication_year in calls[1:])
    early = fit.adjustment(1935)
    middle = fit.adjustment(1975)
    late = fit.adjustment(2005)
    assert early > 0
    assert middle < 0
    assert late > 0
    assert all(abs(fit.adjustment(year)) <= MAX_SCORE_ADJUSTMENT for year in (1935, 1975, 2005))


def test_no_era_residual_and_sparse_confounding_abstain() -> None:
    rows, vectors = _history(era_effect=False)
    no_effect = fit_publication_era_signal(rows, vectors, _baseline)
    assert not no_effect.activated
    assert all(no_effect.adjustment(year) == 0 for year in (1930, 1970, 2000))

    sparse_rows, sparse_vectors = _history(era_effect=False)
    for index, row in enumerate(sparse_rows):
        row["first_publication_year"] = 1940 if index < 60 else 1990
    sparse = fit_publication_era_signal(sparse_rows, sparse_vectors, _baseline)
    assert not sparse.activated
    assert sparse.diagnostics["reason"] == "all_fold_confidence_gate_not_met"


def test_baseline_explains_year_association_and_shuffled_labels_abstain() -> None:
    rows, vectors = _history(era_effect=False)
    for index, row in enumerate(rows):
        year = row["first_publication_year"]
        favored = year <= 1950 or year >= 1990
        row["rating"] = 4.6 if favored else 2.4
        row["baseline_score"] = 50.0 + 10.0 * (row["rating"] - 3.0)
    explained = fit_publication_era_signal(rows, vectors, _baseline)
    assert not explained.activated
    assert all(explained.adjustment(year) == 0 for year in (1930, 1975, 2010))

    shuffled_rows, shuffled_vectors = _history(era_effect=True)
    years = [row["first_publication_year"] for row in shuffled_rows]
    permutation = np.random.default_rng(981).permutation(len(years))
    for index, row in enumerate(shuffled_rows):
        row["first_publication_year"] = years[int(permutation[index])]
    shuffled = fit_publication_era_signal(shuffled_rows, shuffled_vectors, _baseline)
    assert not shuffled.activated


def test_one_author_repeated_days_and_single_outlier_abstain() -> None:
    rows, vectors = _history(era_effect=True)
    for row in rows:
        row["author"] = "Only One Author"
    one_author = fit_publication_era_signal(rows, vectors, _baseline)
    assert not one_author.activated

    repeated_rows, repeated_vectors = _history(era_effect=True)
    start = date(2021, 1, 1)
    for index, row in enumerate(repeated_rows):
        row["read_at"] = (start + timedelta(days=index // 24)).isoformat()
    repeated_days = fit_publication_era_signal(repeated_rows, repeated_vectors, _baseline)
    assert not repeated_days.activated

    outlier_rows, outlier_vectors = _history(era_effect=False)
    outlier_rows[57]["rating"] = 5.0
    outlier = fit_publication_era_signal(outlier_rows, outlier_vectors, _baseline)
    assert not outlier.activated


def test_missing_year_is_neutral_and_year_is_not_extrapolated() -> None:
    rows, vectors = _history(era_effect=True)
    fit = fit_publication_era_signal(rows, vectors, _baseline)
    assert fit.activated
    assert fit.adjustment(None) == 0.0
    assert fit.adjustment("1935") == 0.0
    assert fit.adjustment(2100) == 0.0


def test_real_ranker_scores_can_support_a_bounded_nonlinear_residual() -> None:
    rng = np.random.default_rng(4021)
    years = list(range(1920, 2021, 5))
    rows = []
    vectors = []
    for index in range(128):
        category = (index * 7) % 10
        year = years[(index * 13) % len(years)]
        quality = 2.2 + 0.2 * category
        era_residual = 0.8 if year <= 1950 or year >= 1990 else -0.8
        rows.append({
            "id": index + 1,
            "title": f"Ranker fixture {index}",
            "author": f"Fixture Author {index % 32}",
            "rating": min(5.0, max(1.0, quality + era_residual)),
            "read_at": (date(2022, 1, 1) + timedelta(days=index)).isoformat(),
            "first_publication_year": year,
        })
        vector = np.zeros(10, dtype=np.float64)
        vector[category] = 1.0
        vector += rng.normal(0.0, 0.07, size=10)
        vector /= np.linalg.norm(vector)
        vectors.append(vector.tolist())

    def current_ranker(history_rows, history_vectors, query_rows, query_vectors):
        ranked = rank_candidates(
            list(history_rows), list(history_vectors), list(query_rows), list(query_vectors),
            exclude_candidate_identity=True,
        )
        return [row["score"] for row in ranked]

    fit = fit_publication_era_signal(rows, vectors, current_ranker)

    assert fit.activated
    assert fit.adjustment(1930) > 0
    assert fit.adjustment(1975) < 0
    assert fit.adjustment(2010) > 0
    assert max(abs(fit.adjustment(year)) for year in (1930, 1975, 2010)) <= 2.0


def test_interpolation_across_unsupported_publication_gap_is_neutral() -> None:
    days = tuple(date(2020, 1, 1) + timedelta(days=index) for index in range(20))
    model = _EraModel(
        years=np.asarray([1920] * 10 + [2020] * 10, dtype=float),
        residuals=np.asarray([0.5] * 10 + [-0.5] * 10, dtype=float),
        authors=tuple(f"author-{index}" for index in range(20)),
        days=days,
    )
    correction, detail = model.estimate(1970)
    assert correction == 0.0
    assert detail["reason"] == "unsupported_local_gap"


def test_verified_work_aliases_cannot_duplicate_calibration_or_baseline_history():
    rows, vectors = _history(era_effect=True)
    for index, row in enumerate(rows):
        row['publication_work_id'] = f'/works/OL{index + 1}W'
    original = fit_publication_era_signal(rows, vectors, _baseline)
    aliases = [dict(row, id=row['id'] + 1000, title='Different edition title', rating=1)
               for row in rows]
    duplicated = fit_publication_era_signal(rows + aliases, vectors + vectors, _baseline)
    assert duplicated.diagnostics == original.diagnostics
    assert duplicated.activated == original.activated
    assert duplicated.adjustment(1930) == original.adjustment(1930)


def test_supported_date_formats_always_normalize_to_utc_days():
    from afterword_engine.publication_era_signal import _day_value
    from datetime import datetime, timezone
    expected = date(2022, 1, 1)
    for value in ('2022-01-01', '2022/01/01', 'Sat, 01 Jan 2022 00:00:00 GMT',
                  '2021-12-31T19:00:00-05:00', datetime(2022, 1, 1, tzinfo=timezone.utc), expected):
        result = _day_value(value)
        assert result == expected
        assert type(result) is date
    assert _day_value('2022-01-01garbage') is None
