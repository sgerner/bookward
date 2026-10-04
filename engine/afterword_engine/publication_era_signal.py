"""Conservative, user-specific publication-era score adjustment.

The signal is an optional correction on top of the existing rank score.  It is
fit only when chronological held-out folds show a positive paired improvement
under both rating-day and author resampling.  Missing or unsupported years are
neutral.  The module is deliberately pure so callers can supply the existing
ranker as ``baseline_score`` without coupling this experiment to production
ranking.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re
import re
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from .identity import book_identity


GAUSSIAN_BANDWIDTH_YEARS = 10.0
MAX_CALIBRATION_ROWS = 128
MAX_SCORE_ADJUSTMENT = 2.0
MIN_SEED_ROWS = 40
MIN_BASELINE_CALIBRATION_ROWS = 24
MIN_ERA_ROWS = 24
MIN_DISTINCT_ERA_YEARS = 8
MIN_LOCAL_ESS = 8.0
MIN_LOCAL_AUTHORS = 8
MIN_LOCAL_DAYS = 8
LOCAL_WEIGHT_FRACTION = 0.05
MIN_FOLD_ROWS = 12
MIN_FOLD_GROUPS = 8
MIN_FOLD_AUTHOR_COVERAGE = 0.80
BOOTSTRAP_REPLICATES = 2_000
BOOTSTRAP_SEED = 20261003


BaselineScorer = Callable[
    [Sequence[Mapping[str, Any]], Sequence[Any], Sequence[Mapping[str, Any]], Sequence[Any]],
    Sequence[float] | Sequence[Mapping[str, Any]],
]


@dataclass(frozen=True)
class _Calibration:
    intercept: float
    slope: float

    def predict(self, score: float | np.ndarray) -> float | np.ndarray:
        return np.clip(self.intercept + self.slope * np.asarray(score), 1.0, 5.0)


@dataclass(frozen=True)
class _EraModel:
    years: np.ndarray
    residuals: np.ndarray
    authors: tuple[str | None, ...]
    days: tuple[date, ...]

    def estimate(self, year: int) -> tuple[float, dict[str, Any]]:
        """Estimate a local star residual and a conservative cluster interval."""
        base = {
            "year": year,
            "effective_sample_size": 0.0,
            "local_authors": 0,
            "local_days": 0,
            "correction_stars": 0.0,
            "correction_interval_stars": [0.0, 0.0],
            "supported": False,
            "reason": "unsupported_extrapolation",
        }
        if not len(self.years) or year < float(self.years.min()) or year > float(self.years.max()):
            return 0.0, base
        nearest_distance = float(np.min(np.abs(self.years - float(year))))
        if nearest_distance > 2.0 * GAUSSIAN_BANDWIDTH_YEARS:
            return 0.0, {**base, "nearest_year_distance": nearest_distance, "reason": "unsupported_local_gap"}

        distances = (self.years - float(year)) / GAUSSIAN_BANDWIDTH_YEARS
        weights = np.exp(-0.5 * distances * distances)
        peak = float(weights.max()) if len(weights) else 0.0
        local = weights >= peak * LOCAL_WEIGHT_FRACTION if peak > 0 else np.zeros(len(weights), bool)
        local_authors = {
            self.authors[i] for i in np.flatnonzero(local) if self.authors[i]
        }
        local_days = {self.days[i] for i in np.flatnonzero(local)}
        sum_weights = float(weights.sum())
        sum_sq_weights = float(np.dot(weights, weights))
        ess = sum_weights * sum_weights / sum_sq_weights if sum_sq_weights > 0 else 0.0
        details = {
            **base,
            "nearest_year_distance": nearest_distance,
            "effective_sample_size": float(ess),
            "local_authors": len(local_authors),
            "local_days": len(local_days),
            "reason": "low_local_support",
        }
        if (ess < MIN_LOCAL_ESS or len(local_authors) < MIN_LOCAL_AUTHORS
                or len(local_days) < MIN_LOCAL_DAYS):
            return 0.0, details

        correction = float(np.dot(weights, self.residuals) / sum_weights)
        centered = self.residuals - correction
        row_variance = float(np.dot(weights, centered * centered) / sum_weights)
        row_se = math.sqrt(max(0.0, row_variance) / max(ess, 1.0))

        # Treat days and authors as the independent units.  The larger cluster
        # standard error is used so repeated ratings do not create false
        # precision merely by sharing a day or author.
        def cluster_se(groups: Sequence[str]) -> float:
            influence: dict[str, float] = defaultdict(float)
            for index, (group, weight) in enumerate(zip(groups, weights)):
                if group:
                    influence[group] += float(weight) * float(centered[index]) / sum_weights
            count = len(influence)
            if count < 2:
                return math.inf
            correction_factor = count / (count - 1)
            return math.sqrt(correction_factor * sum(value * value for value in influence.values()))

        day_groups = [value.isoformat() for value in self.days]
        author_groups = [value or "__unknown_author__" for value in self.authors]
        se = max(row_se, cluster_se(day_groups), cluster_se(author_groups))
        lower, upper = correction - 1.96 * se, correction + 1.96 * se
        if not (lower > 0 or upper < 0):
            return 0.0, {
                **details,
                "correction_stars": correction,
                "correction_interval_stars": [float(lower), float(upper)],
                "reason": "correction_uncertainty_includes_zero",
            }
        return correction, {
            **details,
            "correction_stars": correction,
            "correction_interval_stars": [float(lower), float(upper)],
            "supported": True,
            "reason": "supported",
        }


@dataclass(frozen=True)
class PublicationEraFit:
    """Fit result. ``adjustment(year)`` returns score points, or zero neutrally."""

    diagnostics: Mapping[str, Any]
    _model: _EraModel | None = None
    _calibration: _Calibration | None = None
    activated: bool = False

    def adjustment(self, year: object) -> float:
        parsed = _trusted_year(year)
        if not self.activated or self._model is None or self._calibration is None or parsed is None:
            return 0.0
        correction_stars, detail = self._model.estimate(parsed)
        if not detail["supported"] or self._calibration.slope <= 0:
            return 0.0
        score_points = correction_stars / self._calibration.slope
        return float(np.clip(score_points, -MAX_SCORE_ADJUSTMENT, MAX_SCORE_ADJUSTMENT))

    def __call__(self, year: object) -> float:
        return self.adjustment(year)


def _trusted_year(value: object) -> int | None:
    # Production callers pass a normalized integer from verified work-level
    # metadata.  Do not silently parse loose strings or fractional values here.
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        return None
    year = int(value)
    return year if 1000 <= year <= 2100 else None


def _rating(row: Mapping[str, Any]) -> float | None:
    try:
        value = float(row.get("rating"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 1.0 <= value <= 5.0 else None


def _day_value(value: object) -> date | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.strptime(text, "%Y/%m/%d").date()
            except ValueError:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                    try:
                        parsed = parsedate_to_datetime(text)
                    except (TypeError, ValueError, IndexError, OverflowError):
                        return None
                else:
                    try:
                        parsed = datetime.combine(date.fromisoformat(text), datetime.min.time(), tzinfo=timezone.utc)
                    except ValueError:
                        return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date()


def _row_day(row: Mapping[str, Any]) -> date | None:
    for key in ("rated_at", "read_at", "rating_day", "day", "utc_day"):
        if key in row:
            parsed = _day_value(row.get(key))
            if parsed is not None:
                return parsed
    return None


def _author(row: Mapping[str, Any]) -> str | None:
    value = str(row.get("author") or "").strip()
    return book_identity("", value) if value else None


def _calibration(scores: Sequence[float], ratings: Sequence[float]) -> _Calibration:
    x = np.asarray(scores, dtype=np.float64)
    y = np.asarray(ratings, dtype=np.float64)
    if not len(x):
        return _Calibration(3.0, 0.0)
    x_mean, y_mean = float(x.mean()), float(y.mean())
    variance = float(np.dot(x - x_mean, x - x_mean))
    slope = float(np.dot(x - x_mean, y - y_mean) / variance) if variance > 1e-12 else 0.0
    # A rank score that runs opposite to ratings is not reinterpreted as
    # evidence for an era correction.  Keep only a nonnegative calibration.
    slope = max(0.0, slope)
    return _Calibration(y_mean - slope * x_mean, slope)


def _extract_scores(output: object, query_rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    if isinstance(output, Mapping):
        if "scores" in output:
            output = output["scores"]
        else:
            values = []
            for row in query_rows:
                key = row.get("id")
                if key not in output:
                    raise ValueError("baseline score mapping has no query id")
                values.append(output[key])
            output = values
    values: list[float] = []
    for value in output:  # type: ignore[union-attr]
        if isinstance(value, Mapping):
            value = value.get("score")
        score = float(value)
        if not math.isfinite(score):
            raise ValueError("baseline scorer returned a non-finite score")
        values.append(score)
    if len(values) != len(query_rows):
        raise ValueError("baseline scorer must return one score per query row")
    return np.asarray(values, dtype=np.float64)


def _call_baseline(
    baseline_score: BaselineScorer,
    rows: Sequence[Mapping[str, Any]],
    vectors: Sequence[Any],
    history_indices: Sequence[int],
    query_indices: Sequence[int],
) -> np.ndarray:
    history_rows = [rows[index] for index in history_indices]
    history_vectors = [vectors[index] for index in history_indices]
    query_rows = [rows[index] for index in query_indices]
    query_vectors = [vectors[index] for index in query_indices]
    output = baseline_score(history_rows, history_vectors, query_rows, query_vectors)
    return _extract_scores(output, query_rows)


def _spread_sample(indices: Sequence[int], maximum: int) -> list[int]:
    if len(indices) <= maximum:
        return list(indices)
    positions = np.linspace(0, len(indices) - 1, maximum, dtype=np.int64)
    return [indices[int(position)] for position in positions]


def _boundaries(rows: Sequence[Mapping[str, Any]], target_indices: Sequence[int]) -> list[int]:
    """Return row offsets for a 40% seed and three whole-day test blocks."""
    groups: list[list[int]] = []
    for index in target_indices:
        day = _row_day(rows[index])
        if not groups or _row_day(rows[groups[-1][0]]) != day:
            groups.append([])
        groups[-1].append(index)
    if len(groups) < 4:
        return []
    cumulative = np.cumsum([len(group) for group in groups]).tolist()
    total = len(target_indices)
    chosen: list[int] = [0]
    for fraction in (0.40, 0.60, 0.80, 1.0):
        target = total * fraction
        candidates = [value for value in cumulative if value > chosen[-1]]
        if fraction == 1.0:
            boundary = total
        elif not candidates:
            return []
        else:
            boundary = min(candidates, key=lambda value: (abs(value - target), value))
        chosen.append(boundary)
    if any(chosen[index] >= chosen[index + 1] for index in range(len(chosen) - 1)):
        return []
    return chosen


def _history_before(rows: Sequence[Mapping[str, Any]], cutoff: date) -> list[int]:
    # Unknown-date labels cannot be proven earlier than a held-out query, so
    # they are never admitted into chronological callback history.
    return [
        index for index, row in enumerate(rows)
        if _rating(row) is not None and (day := _row_day(row)) is not None and day < cutoff
    ]


def _residuals_for_training(
    scores: np.ndarray,
    ratings: np.ndarray,
    indices: Sequence[int],
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[int], np.ndarray]:
    """Day-LOO calibrated residuals for a training partition."""
    result_indices: list[int] = []
    residuals: list[float] = []
    by_day: dict[date, list[int]] = defaultdict(list)
    for index in indices:
        if (day := _row_day(rows[index])) is not None:
            by_day[day].append(index)
    for day in sorted(by_day):
        day_indices = by_day[day]
        calibration_indices = [index for index in indices if _row_day(rows[index]) != day]
        if len(calibration_indices) < MIN_BASELINE_CALIBRATION_ROWS:
            continue
        calibrator = _calibration(scores[calibration_indices], ratings[calibration_indices])
        for index in day_indices:
            prediction = float(calibrator.predict(scores[index]))
            year = _trusted_year(rows[index].get("first_publication_year"))
            if year is None or year > day.year:
                continue
            result_indices.append(index)
            residuals.append(float(ratings[index] - prediction))
    return result_indices, np.asarray(residuals, dtype=np.float64)


def _make_model(
    residual_indices: Sequence[int], residuals: np.ndarray, rows: Sequence[Mapping[str, Any]]
) -> tuple[_EraModel | None, str]:
    selected: list[int] = []
    years: list[int] = []
    authors: list[str | None] = []
    days: list[date] = []
    values: list[float] = []
    for index, residual in zip(residual_indices, residuals):
        row = rows[index]
        year = _trusted_year(row.get("first_publication_year"))
        day = _row_day(row)
        author = _author(row)
        if (year is None or day is None or year > day.year or author is None
                or not math.isfinite(float(residual))):
            continue
        selected.append(index)
        years.append(year)
        authors.append(author)
        days.append(day)
        values.append(float(residual))
    if len(years) < MIN_ERA_ROWS:
        return None, "low_dated_era_history"
    if len(set(years)) < MIN_DISTINCT_ERA_YEARS:
        return None, "low_era_diversity"
    return _EraModel(
        np.asarray(years, dtype=np.float64), np.asarray(values, dtype=np.float64),
        tuple(authors), tuple(days),
    ), "supported"


def _cluster_lower_bound(
    improvements: np.ndarray,
    groups: Sequence[str | None],
    *,
    seed: int,
) -> tuple[float | None, int]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for value, group in zip(improvements, groups):
        if group:
            grouped[group].append(float(value))
    means = np.asarray([np.mean(values) for _group, values in sorted(grouped.items())], dtype=np.float64)
    if len(means) < MIN_FOLD_GROUPS:
        return None, len(means)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(means), size=(BOOTSTRAP_REPLICATES, len(means)))
    bootstrap_means = means[draws].mean(axis=1)
    return float(np.quantile(bootstrap_means, 0.05, method="linear")), len(means)


def _neutral(reason: str, diagnostics: Mapping[str, Any] | None = None) -> PublicationEraFit:
    detail = dict(diagnostics or {})
    detail.setdefault("status", "abstained")
    detail["reason"] = reason
    return PublicationEraFit(detail)


def fit_publication_era_signal(
    rated_rows: Sequence[Mapping[str, Any]],
    vectors: Sequence[Any],
    baseline_score: BaselineScorer,
    *,
    max_rows: int = MAX_CALIBRATION_ROWS,
) -> PublicationEraFit:
    """Fit a gated Gaussian-10-year era correction on rated history.

    ``baseline_score(history_rows, history_vectors, query_rows, query_vectors)``
    must return one existing-engine score per query (or rows containing a
    ``score`` key).  The seed is training-only: its scores use the history
    snapshot before the first held-out block and the callback must leave each
    query identity out.  Every held-out block is scored against only dated,
    rated history strictly before its first query day.  The input can be much
    larger than ``max_rows``: only calibration targets are sampled, while the
    full earlier history remains available to each callback invocation.
    """
    if len(rated_rows) != len(vectors):
        raise ValueError("rated rows and vectors must have equal lengths")
    if not callable(baseline_score):
        raise TypeError("baseline_score must be callable")
    if max_rows < 1:
        raise ValueError("max_rows must be positive")
    limit = min(int(max_rows), MAX_CALIBRATION_ROWS)
    # A second title/edition of the same verified work cannot supply another
    # copy of its rating to either calibration or held-out baseline history.
    rows = []
    vector_list = []
    seen_work_ids = set()
    for row, vector in zip(rated_rows, vectors):
        work_id = str(row.get("publication_work_id") or row.get("openlibrary_work_id") or "")
        verified_work = bool(re.fullmatch(r"/works/OL[0-9]+W", work_id))
        if verified_work and work_id in seen_work_ids:
            continue
        if verified_work:
            seen_work_ids.add(work_id)
        rows.append(row)
        vector_list.append(vector)
    chronological = sorted(
        (index for index, row in enumerate(rows) if _rating(row) is not None and _row_day(row) is not None),
        key=lambda index: (_row_day(rows[index]), index),
    )
    targets = _spread_sample(chronological, limit)
    diagnostics: dict[str, Any] = {
        "status": "abstained",
        "reason": "insufficient_chronological_rows",
        "protocol": {
            "kernel": "Gaussian",
            "bandwidth_years": GAUSSIAN_BANDWIDTH_YEARS,
        "max_calibration_rows": limit,
            "folds": 3,
            "seed_fraction": 0.40,
            "seed_scores_are_training_only_leave_one_identity_out": True,
            "heldout_history": "strictly_before_first_day_of_fold",
            "fold_fractions": [0.20, 0.20, 0.20],
            "maximum_score_adjustment": MAX_SCORE_ADJUSTMENT,
            "correction_scale": "invert fitted nonnegative score-to-rating slope",
            "minimum_fold_author_coverage": MIN_FOLD_AUTHOR_COVERAGE,
            "minimum_seed_rows": MIN_SEED_ROWS,
            "minimum_baseline_calibration_rows": MIN_BASELINE_CALIBRATION_ROWS,
            "minimum_era_rows": MIN_ERA_ROWS,
            "minimum_distinct_era_years": MIN_DISTINCT_ERA_YEARS,
            "minimum_local_effective_sample_size": MIN_LOCAL_ESS,
            "minimum_local_authors": MIN_LOCAL_AUTHORS,
            "minimum_local_days": MIN_LOCAL_DAYS,
            "local_weight_floor_fraction": LOCAL_WEIGHT_FRACTION,
            "maximum_distance_to_local_year": 2.0 * GAUSSIAN_BANDWIDTH_YEARS,
            "minimum_fold_rows": MIN_FOLD_ROWS,
            "minimum_fold_supported_rows": MIN_FOLD_GROUPS,
            "minimum_fold_day_and_author_groups": MIN_FOLD_GROUPS,
            "paired_lower_quantile": 0.05,
            "bootstrap_replicates": BOOTSTRAP_REPLICATES,
            "correction_interval_z": 1.96,
            "paired_bound": "one-sided 95% cluster bootstrap by day and author",
        },
        "input_rows": len(rows),
        "dated_rated_history_rows": len(chronological),
        "calibration_targets": len(targets),
        "calibration_target_years": sum(
            _trusted_year(rows[index].get("first_publication_year")) is not None for index in targets
        ),
        "folds": [],
    }
    boundaries = _boundaries(rows, targets)
    if len(targets) < MIN_SEED_ROWS or not boundaries:
        return _neutral("insufficient_chronological_rows", diagnostics)

    # The first 40% is a seed block.  The three later blocks are expanding
    # held-out folds, each scored at its start-of-block history snapshot.
    blocks = [targets[boundaries[i]:boundaries[i + 1]] for i in range(4)]
    if len(blocks[0]) < MIN_SEED_ROWS:
        return _neutral("insufficient_seed_rows", diagnostics)
    score_by_index: dict[int, float] = {}
    try:
        for block_number, block in enumerate(blocks):
            if not block:
                return _neutral("empty_chronological_fold", diagnostics)
            first_query_day = _row_day(rows[block[0]])
            if first_query_day is None:
                return _neutral("unknown_fold_day", diagnostics)
            # Seed scores are training-only calibration evidence.  Score them
            # against the history available immediately before the first
            # held-out fold, with the caller's leave-one-identity-out baseline;
            # this avoids an empty-history constant score for the seed.  Seed
            # labels are not described as causal predictions.  Every held-out
            # block still uses only history before its own first day.
            history_cutoff = (
                _row_day(rows[blocks[1][0]]) if block_number == 0 else first_query_day
            )
            if history_cutoff is None:
                return _neutral("unknown_fold_day", diagnostics)
            history_indices = _history_before(rows, history_cutoff)
            block_scores = _call_baseline(
                baseline_score, rows, vector_list, history_indices, block
            )
            score_by_index.update(zip(block, block_scores.tolist()))
    except Exception as exc:
        diagnostics["baseline_error"] = type(exc).__name__
        return _neutral("baseline_scoring_failed", diagnostics)

    target_ratings = np.asarray([float(_rating(rows[index])) for index in targets], dtype=np.float64)
    target_scores = np.asarray([score_by_index[index] for index in targets], dtype=np.float64)
    row_scores = np.full(len(rows), np.nan, dtype=np.float64)
    row_ratings = np.full(len(rows), np.nan, dtype=np.float64)
    for index, score, rating in zip(targets, target_scores, target_ratings):
        row_scores[index] = score
        row_ratings[index] = rating
    target_position = {index: position for position, index in enumerate(targets)}
    fold_results: list[dict[str, Any]] = []
    all_folds_pass = True
    for fold_number in range(3):
        train_indices = [index for block in blocks[:fold_number + 1] for index in block]
        validation_indices = blocks[fold_number + 1]
        if len(train_indices) < MIN_BASELINE_CALIBRATION_ROWS:
            fold_results.append({"fold": fold_number + 1, "status": "abstained", "reason": "low_training_rows"})
            all_folds_pass = False
            continue
        train_positions = [target_position[index] for index in train_indices]
        validation_positions = [target_position[index] for index in validation_indices]
        train_score_values = target_scores[train_positions]
        train_rating_values = target_ratings[train_positions]
        calibrator = _calibration(train_score_values, train_rating_values)
        residual_indices, residual_values = _residuals_for_training(
            row_scores, row_ratings, train_indices, rows
        )
        model, model_reason = _make_model(residual_indices, residual_values, rows)
        result: dict[str, Any] = {
            "fold": fold_number + 1,
            "training_rows": len(train_indices),
            "validation_rows": len(validation_indices),
            "calibration_slope_stars_per_score_point": calibrator.slope,
            "era_training_rows": len(residual_values),
            "era_model_reason": model_reason,
        }
        if model is None or calibrator.slope <= 0:
            result.update({"status": "abstained", "reason": model_reason if model is None else "nonpositive_calibration_slope"})
            fold_results.append(result)
            all_folds_pass = False
            continue

        improvements: list[float] = []
        paired_days: list[str | None] = []
        paired_authors: list[str | None] = []
        support_count = 0
        for position, index in zip(validation_positions, validation_indices):
            score = round(float(target_scores[position]), 1)
            year = _trusted_year(rows[index].get("first_publication_year"))
            base_prediction = float(calibrator.predict(score))
            delta_score = 0.0
            if year is not None and (day := _row_day(rows[index])) is not None and year <= day.year:
                residual, local = model.estimate(year)
                if local["supported"]:
                    delta_score = float(np.clip(
                        residual / calibrator.slope,
                        -MAX_SCORE_ADJUSTMENT,
                        MAX_SCORE_ADJUSTMENT,
                    ))
                    support_count += 1
            adjusted_score = float(np.clip(
                score + delta_score,
                0.0,
                100.0,
            ))
            adjusted_score = round(adjusted_score, 1)
            corrected_prediction = float(calibrator.predict(adjusted_score))
            actual = float(_rating(rows[index]))
            improvements.append((actual - base_prediction) ** 2 - (actual - corrected_prediction) ** 2)
            paired_days.append(_row_day(rows[index]).isoformat() if _row_day(rows[index]) else None)
            paired_authors.append(_author(rows[index]))

        improvement_array = np.asarray(improvements, dtype=np.float64)
        day_lower, day_group_count = _cluster_lower_bound(
            improvement_array, paired_days, seed=BOOTSTRAP_SEED + fold_number * 2
        )
        author_lower, author_group_count = _cluster_lower_bound(
            improvement_array, [author or "__unknown_author__" for author in paired_authors], seed=BOOTSTRAP_SEED + fold_number * 2 + 1
        )
        mean_improvement = float(improvement_array.mean()) if len(improvement_array) else 0.0
        author_coverage = sum(bool(value) for value in paired_authors) / max(1, len(paired_authors))
        passed = (
            len(validation_indices) >= MIN_FOLD_ROWS
            and mean_improvement > 0
            and author_coverage >= MIN_FOLD_AUTHOR_COVERAGE
            and support_count >= MIN_FOLD_GROUPS
            and day_lower is not None and day_lower > 0
            and author_lower is not None and author_lower > 0
        )
        result.update({
            "supported_validation_rows": support_count,
            "validation_author_coverage": author_coverage,
            "mean_paired_mse_improvement_stars_squared": mean_improvement,
            "day_cluster_lower_bound": day_lower,
            "author_cluster_lower_bound": author_lower,
            "day_groups": day_group_count,
            "author_groups": author_group_count,
            "status": "passed" if passed else "abstained",
            "reason": "positive_paired_bounds" if passed else "heldout_improvement_not_confidently_positive",
        })
        fold_results.append(result)
        all_folds_pass = all_folds_pass and passed

    diagnostics["folds"] = fold_results
    if len(fold_results) != 3 or not all_folds_pass:
        diagnostics["reason"] = "all_fold_confidence_gate_not_met"
        return _neutral("all_fold_confidence_gate_not_met", diagnostics)

    final_calibration = _calibration(target_scores, target_ratings)
    final_indices, final_residuals = _residuals_for_training(
        row_scores, row_ratings, targets, rows
    )
    final_model, final_reason = _make_model(final_indices, final_residuals, rows)
    if final_model is None or final_calibration.slope <= 0:
        diagnostics["reason"] = final_reason if final_model is None else "nonpositive_calibration_slope"
        return _neutral(diagnostics["reason"], diagnostics)
    diagnostics["status"] = "activated"
    diagnostics["reason"] = "all_three_folds_passed"
    diagnostics["final_calibration_slope_stars_per_score_point"] = final_calibration.slope
    diagnostics["final_era_training_rows"] = len(final_residuals)
    diagnostics["final_supported_year_range"] = [int(final_model.years.min()), int(final_model.years.max())]
    return PublicationEraFit(diagnostics, final_model, final_calibration, True)


__all__ = [
    "PublicationEraFit",
    "fit_publication_era_signal",
    "GAUSSIAN_BANDWIDTH_YEARS",
    "MAX_CALIBRATION_ROWS",
    "MAX_SCORE_ADJUSTMENT",
]
