#!/usr/bin/env python3
"""Aggregate historical ranking-pipeline evidence without exposing row data.

The report intentionally contains counts and coverage only. It distinguishes
what was logged for a served run from inputs that would be needed to replay the
complete pipeline as of that run.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping


LEARNING_EVENT_TYPES = {
    "save", "reject", "maybe_later", "restore", "read", "librarr_import",
}
FEEDBACK_EVENT_PREFIX = "feedback:"
DATE_ONLY_RE = re.compile(r"^\d{4}(?:-|/)\d{2}(?:-|/)\d{2}$")


def _rows(data: Mapping[str, Any], table: str) -> list[dict[str, Any]]:
    values = data.get(table, [])
    if not isinstance(values, list):
        return []
    return [dict(row) for row in values if isinstance(row, Mapping)]


def _parse_time(value: Any) -> tuple[datetime | None, bool, bool]:
    """Return (UTC time, day precision, timezone was explicit).

    Naive database timestamps are interpreted as UTC, matching the engine's
    timestamp convention. The third value preserves whether that assumption
    was needed so aggregate reports can show its coverage.
    """

    if isinstance(value, datetime):
        parsed = value
        day_precision = False
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
        day_precision = True
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        day_precision = bool(DATE_ONLY_RE.fullmatch(raw))
        try:
            if day_precision and "/" in raw:
                parsed = datetime.strptime(raw, "%Y/%m/%d")
            else:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None, day_precision, False
    else:
        return None, False, False

    explicit_zone = parsed.tzinfo is not None
    parsed = parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
    return parsed, day_precision, explicit_zone


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value:
        try:
            result = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(result) if isinstance(result, Mapping) else {}
    return {}


def _is_later_event_than_exposure(
    event: Mapping[str, Any], impression: Mapping[str, Any]
) -> tuple[bool | None, str]:
    event_time, parsed_day_precision, _ = _parse_time(event.get("occurred_at"))
    exposure_time, _, _ = _parse_time(impression.get("presented_at"))
    if event_time is None or exposure_time is None:
        return None, "missing_or_invalid_time"

    event_metadata = _metadata(event.get("metadata"))
    day_precision = (
        parsed_day_precision
        or str(event_metadata.get("time_precision") or "").casefold() == "day"
    )
    if day_precision:
        if event_time.date() > exposure_time.date():
            return True, "day_strictly_after"
        if event_time.date() == exposure_time.date():
            return False, "same_utc_day"
        return False, "day_before"

    if event_time > exposure_time:
        return True, "timestamp_strictly_after"
    if event_time == exposure_time:
        return False, "same_instant"
    return False, "timestamp_before"


def _event_sources(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return action-log rows that can feed the learner, without identities."""

    events = _rows(data, "recommendation_events")
    result = [
        {"created_at": row.get("created_at"), "event_type": row.get("event_type")}
        for row in events
        if str(row.get("event_type") or "").casefold() in LEARNING_EVENT_TYPES
    ]

    # Legacy feedback participates in learning only when its equivalent
    # recommendation event has not already been recorded.
    event_keys = {str(row.get("event_key") or "") for row in events}
    for row in _rows(data, "feedback"):
        action = str(row.get("action") or "").casefold()
        event_key = f"{FEEDBACK_EVENT_PREFIX}{row.get('id')}"
        if action in {"save", "reject", "maybe_later", "restore"} and event_key not in event_keys:
            result.append({"created_at": row.get("created_at"), "event_type": action})
    return result


def _counter_report(values: Counter[str]) -> dict[str, int]:
    return {key: values[key] for key in sorted(values)}


def _median(values: list[int]) -> float | None:
    return float(statistics.median(values)) if values else None


def _finite_score(row: Mapping[str, Any]) -> float | None:
    try:
        value = float(row.get("score"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _current_reference_report(reference: Any) -> dict[str, Any]:
    """Summarize same-snapshot serving helper outputs without returning rows."""

    if not isinstance(reference, Mapping):
        return {"present": False}

    scopes: dict[str, Any] = {}
    for scope_name in ("recommended", "all"):
        scope = reference.get(scope_name)
        if not isinstance(scope, Mapping):
            continue
        stages = {
            stage: [row for row in scope.get(stage, []) if isinstance(row, Mapping)]
            for stage in ("base", "personalized", "final")
        }
        by_stage = {
            stage: {str(row.get("id")): row for row in rows if row.get("id") is not None}
            for stage, rows in stages.items()
        }
        base_ids = [str(row.get("id")) for row in stages["base"] if row.get("id") is not None]
        personalized_ids = [
            str(row.get("id")) for row in stages["personalized"] if row.get("id") is not None
        ]
        final_ids = [str(row.get("id")) for row in stages["final"] if row.get("id") is not None]

        adjusted = []
        score_abs_deltas = []
        for candidate_id, base_row in by_stage["base"].items():
            current = by_stage["personalized"].get(candidate_id)
            if current is None:
                continue
            before = _finite_score(base_row)
            after = _finite_score(current)
            if before is None or after is None:
                continue
            delta = after - before
            score_abs_deltas.append(abs(delta))
            if abs(delta) >= 0.05:
                adjusted.append(candidate_id)

        top_k = min(20, len(base_ids), len(final_ids))
        slate_diag = scope.get("slate_diagnostics")
        if not isinstance(slate_diag, Mapping):
            slate_diag = {}
        interaction_diag = scope.get("interaction_diagnostics")
        if not isinstance(interaction_diag, Mapping):
            interaction_diag = {}
        safe_diag_keys = (
            "applied", "author_repeat_swaps", "max_books_per_author",
            "max_displacement", "max_score_sacrifice", "near_duplicate_cosine",
            "policy_version", "reordered_items", "similarity_swaps", "slate_size",
        )
        safe_interaction_keys = (
            "adjusted_candidates", "applied", "max_score_adjustment",
            "observed_books", "qualified_features", "semantic_adjusted_candidates",
            "semantic_evidence_books",
        )
        scopes[scope_name] = {
            "capture_time_utc": scope.get("clock_utc"),
            "scope_label": scope.get("scope"),
            "candidate_rows_by_stage": {stage: len(rows) for stage, rows in stages.items()},
            "candidate_ids_common_to_all_stages": len(
                set(by_stage["base"]) & set(by_stage["personalized"]) & set(by_stage["final"])
            ),
            "rows_with_final_serving_vector": len(
                scope.get("vectors", []) if isinstance(scope.get("vectors"), list) else []
            ),
            "rows_with_interaction_vector": len(
                scope.get("interaction_vectors", [])
                if isinstance(scope.get("interaction_vectors"), list) else []
            ),
            "base_to_personalized_score_rows_comparable": len(score_abs_deltas),
            "base_to_personalized_score_rows_changed": len(adjusted),
            "max_absolute_base_to_personalized_score_delta": max(score_abs_deltas, default=0.0),
            "mean_absolute_base_to_personalized_score_delta": (
                statistics.mean(score_abs_deltas) if score_abs_deltas else 0.0
            ),
            "base_to_final_top20_candidate_overlap_count": len(
                set(base_ids[:top_k]) & set(final_ids[:top_k])
            ),
            "base_to_final_candidate_order_changed": base_ids != final_ids,
            "interaction_diagnostics": {
                key: interaction_diag[key]
                for key in safe_interaction_keys if key in interaction_diag
            },
            "slate_diagnostics": {
                key: slate_diag[key] for key in safe_diag_keys if key in slate_diag
            },
            "historical_run_replay": False,
        }
    return {
        "present": True,
        "kind": "read_only_same_snapshot_serving_reference",
        "historical_outcome_or_ablation_evidence": False,
        "scopes": scopes,
    }


def build_report(data: Mapping[str, Any]) -> dict[str, Any]:
    """Build a privacy-safe telemetry coverage and replayability report."""

    runs = _rows(data, "recommendation_runs")
    impressions = _rows(data, "recommendation_impressions")
    outcomes = _rows(data, "recommendation_outcomes")
    events = _rows(data, "recommendation_events")
    candidates = _rows(data, "candidates")
    candidate_quality = _rows(data, "candidate_quality")
    embeddings = _rows(data, "embeddings")

    run_by_id = {str(row.get("id")): row for row in runs if row.get("id") is not None}
    candidate_by_id = {
        str(row.get("id")): row for row in candidates if row.get("id") is not None
    }
    quality_by_candidate = {
        str(row.get("candidate_id")): row
        for row in candidate_quality if row.get("candidate_id") is not None
    }
    impression_by_id = {
        str(row.get("id")): row for row in impressions if row.get("id") is not None
    }
    event_by_id = {
        str(row.get("id")): row for row in events if row.get("id") is not None
    }

    impressions_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for impression in impressions:
        if impression.get("run_id") is not None:
            impressions_by_run[str(impression["run_id"])].append(impression)

    outcomes_by_impression: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for outcome in outcomes:
        if outcome.get("impression_id") is not None:
            outcomes_by_impression[str(outcome["impression_id"])].append(outcome)

    policy_versions = Counter(str(row.get("policy_version") or "missing") for row in runs)
    policies = Counter(str(row.get("policy") or "missing") for row in runs)
    metadata_modes: Counter[str] = Counter()
    learning_applied = 0
    observed_books: list[int] = []
    adjusted_candidates: list[int] = []
    qualified_features: list[int] = []
    runs_with_learning_metadata = 0
    for run in runs:
        metadata = _metadata(run.get("metadata"))
        learning = metadata.get("interaction_learning")
        if isinstance(learning, Mapping):
            runs_with_learning_metadata += 1
            if learning.get("applied") is True:
                learning_applied += 1
            if learning.get("mode") is not None:
                metadata_modes[str(learning["mode"])] += 1
            for key, values in (
                ("observed_books", observed_books),
                ("adjusted_candidates", adjusted_candidates),
                ("qualified_features", qualified_features),
            ):
                try:
                    number = int(learning.get(key))
                except (TypeError, ValueError):
                    continue
                if number >= 0:
                    values.append(number)

    propensity_counts: Counter[str] = Counter()
    rank_invalid = duplicate_ranks = 0
    impressions_by_run_rank: dict[str, set[int]] = defaultdict(set)
    seen_run_candidate: set[tuple[str, str]] = set()
    duplicate_run_candidates = 0
    distinct_candidates: set[str] = set()
    visible = 0
    visible_impression_ids: set[str] = set()
    visible_run_ids: set[str] = set()
    visible_candidate_ids: set[str] = set()
    visible_missing_presentation_time = 0
    visible_before_presentation = 0
    current_candidate_rows = 0
    candidate_rows_updated_after_run = 0
    quality_rows = 0
    quality_rows_updated_after_run = 0
    outcome_linked_impressions = 0
    label_kinds: Counter[str] = Counter()
    confidence_counts: Counter[str] = Counter()
    for impression in impressions:
        run_id = str(impression.get("run_id") or "")
        candidate_id = str(impression.get("candidate_id") or "")
        if candidate_id:
            distinct_candidates.add(candidate_id)
        pair = (run_id, candidate_id)
        if pair in seen_run_candidate:
            duplicate_run_candidates += 1
        seen_run_candidate.add(pair)

        propensity = impression.get("propensity")
        try:
            number = float(propensity)
            p_key = (f"{number:.6g}" if math.isfinite(number) else "invalid")
            if not math.isfinite(number) or not (0 < number <= 1):
                p_key = "invalid"
        except (TypeError, ValueError):
            p_key = "missing_or_invalid"
        propensity_counts[p_key] += 1

        try:
            rank = int(impression.get("rank"))
            if rank <= 0 or str(rank) != str(impression.get("rank")):
                rank_invalid += 1
            elif rank in impressions_by_run_rank[run_id]:
                duplicate_ranks += 1
            else:
                impressions_by_run_rank[run_id].add(rank)
        except (TypeError, ValueError):
            rank_invalid += 1

        visible_time, _, _ = _parse_time(impression.get("visible_at"))
        presented_time, _, _ = _parse_time(impression.get("presented_at"))
        if impression.get("visible_at") not in (None, ""):
            visible += 1
            if impression.get("id") is not None:
                visible_impression_ids.add(str(impression["id"]))
            if impression.get("run_id") is not None:
                visible_run_ids.add(str(impression["run_id"]))
            if impression.get("candidate_id") is not None:
                visible_candidate_ids.add(str(impression["candidate_id"]))
            if visible_time is None or presented_time is None:
                visible_missing_presentation_time += 1
            elif visible_time < presented_time:
                visible_before_presentation += 1

        candidate = candidate_by_id.get(candidate_id)
        run = run_by_id.get(run_id, {})
        run_time, _, _ = _parse_time(run.get("created_at"))
        if candidate:
            current_candidate_rows += 1
            updated_at, _, _ = _parse_time(candidate.get("updated_at"))
            if run_time and updated_at and updated_at > run_time:
                candidate_rows_updated_after_run += 1
        quality = quality_by_candidate.get(candidate_id)
        if quality:
            quality_rows += 1
            quality_updated, _, _ = _parse_time(quality.get("updated_at"))
            if run_time and quality_updated and quality_updated > run_time:
                quality_rows_updated_after_run += 1

        if outcomes_by_impression.get(str(impression.get("id"))):
            outcome_linked_impressions += 1

    labels_by_exposure_time: Counter[str] = Counter()
    label_class_counts: Counter[str] = Counter()
    label_classes_by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    outcome_links_missing_event = 0
    outcome_links_missing_impression = 0
    outcome_event_created_after_run = 0
    outcome_event_created_time_unknown = 0
    outcome_run_ids: set[str] = set()
    outcome_candidate_ids: set[str] = set()
    outcome_impression_ids: set[str] = set()
    visible_outcome_impression_ids: set[str] = set()
    kind_run_ids: dict[str, set[str]] = defaultdict(set)
    kind_candidate_ids: dict[str, set[str]] = defaultdict(set)
    for outcome in outcomes:
        kind = str(outcome.get("label_kind") or "missing")
        label_kinds[kind] += 1
        try:
            label_value = float(outcome.get("label"))
            if not math.isfinite(label_value) or not 0 <= label_value <= 1:
                label_class = "invalid_or_missing"
            elif label_value > 0.5:
                label_class = "positive"
            elif label_value < 0.5:
                label_class = "negative"
            else:
                label_class = "neutral"
        except (TypeError, ValueError):
            label_class = "invalid_or_missing"
        label_class_counts[label_class] += 1
        label_classes_by_kind[kind][label_class] += 1
        try:
            confidence = float(outcome.get("confidence"))
            confidence_counts[f"{confidence:.3g}" if math.isfinite(confidence) else "invalid"] += 1
        except (TypeError, ValueError):
            confidence_counts["missing_or_invalid"] += 1

        impression = impression_by_id.get(str(outcome.get("impression_id")))
        event = event_by_id.get(str(outcome.get("event_id")))
        if impression is None:
            outcome_links_missing_impression += 1
        else:
            impression_id = str(impression.get("id"))
            outcome_impression_ids.add(impression_id)
            run_id = str(impression.get("run_id") or "")
            candidate_id = str(impression.get("candidate_id") or "")
            if run_id:
                outcome_run_ids.add(run_id)
                kind_run_ids[kind].add(run_id)
            if candidate_id:
                outcome_candidate_ids.add(candidate_id)
                kind_candidate_ids[kind].add(candidate_id)
            if impression_id in visible_impression_ids:
                visible_outcome_impression_ids.add(impression_id)
        if event is None:
            outcome_links_missing_event += 1
        if impression is not None and event is not None:
            _, chronology = _is_later_event_than_exposure(event, impression)
            labels_by_exposure_time[chronology] += 1
            run = run_by_id.get(str(impression.get("run_id")), {})
            created_time, _, _ = _parse_time(event.get("created_at"))
            run_time, _, _ = _parse_time(run.get("created_at"))
            if created_time is None or run_time is None:
                outcome_event_created_time_unknown += 1
            elif created_time > run_time:
                outcome_event_created_after_run += 1

    # Action rows persisted before a run are evidence that some source events
    # could have been read then. They are not a complete profile snapshot:
    # title/author/metadata, event value history and vector cache state mutate.
    action_sources = _event_sources(data)
    prior_source_events_per_run: list[int] = []
    runs_with_prior_action_source = 0
    source_events_with_known_creation_time = 0
    for source in action_sources:
        created, _, _ = _parse_time(source.get("created_at"))
        if created is not None:
            source_events_with_known_creation_time += 1
    for run in runs:
        run_time, _, _ = _parse_time(run.get("created_at"))
        if run_time is None:
            continue
        available = 0
        for source in action_sources:
            created, _, _ = _parse_time(source.get("created_at"))
            # Strictly earlier avoids claiming availability for equal timestamps.
            if created is not None and created < run_time:
                available += 1
        prior_source_events_per_run.append(available)
        if available:
            runs_with_prior_action_source += 1

    runs_missing_impression_rows = sum(
        1 for run in runs if not impressions_by_run.get(str(run.get("id")))
    )
    candidate_count_mismatch = 0
    run_impression_count_missing = 0
    runs_with_rank_gaps_within_declared_count = 0
    unrepresented_rank_slots = 0
    for run in runs:
        expected = run.get("candidate_count")
        actual = len(impressions_by_run.get(str(run.get("id")), []))
        try:
            expected_count = int(expected)
            if expected_count != actual:
                candidate_count_mismatch += 1
            ranks = impressions_by_run_rank.get(str(run.get("id")), set())
            represented_slots = sum(1 for rank in ranks if 1 <= rank <= expected_count)
            missing_slots = max(0, expected_count - represented_slots)
            if missing_slots:
                runs_with_rank_gaps_within_declared_count += 1
                unrepresented_rank_slots += missing_slots
        except (TypeError, ValueError):
            run_impression_count_missing += 1

    table_names = {key for key, value in data.items() if isinstance(value, list)}
    snapshot_table_names = {
        "candidate_snapshots", "recommendation_candidate_snapshots",
        "candidate_versions", "candidate_history",
    }
    stage_table_names = {
        "recommendation_stage_scores", "pipeline_stage_scores",
        "candidate_score_components", "score_components",
    }
    snapshot_table_present = bool(table_names & snapshot_table_names)
    stage_table_present = bool(table_names & stage_table_names)

    # The served score and rank are recorded per impression; no component
    # decomposition or per-run build/feature fingerprint exists in this export.
    served_score_coverage = sum(
        1 for row in impressions if row.get("score") is not None
    )
    run_lineage_fields = {
        "code_version", "build_id", "git_commit", "source_hash",
        "pipeline_fingerprint", "model_version", "feature_version",
    }
    runs_with_lineage = sum(
        1 for run in runs if any(run.get(field) not in (None, "") for field in run_lineage_fields)
    )
    runs_with_feature_snapshot = sum(
        1 for run in runs
        if any(run.get(field) not in (None, "") for field in (
            "feature_snapshot", "profile_snapshot", "input_snapshot_hash",
            "candidate_snapshot_hash", "feature_asof_at",
        ))
    )
    impressions_with_stage_scores = sum(
        1 for row in impressions
        if any(row.get(field) not in (None, "") for field in (
            "stage_scores", "score_components", "base_score", "personalization_delta",
        ))
    )

    counts = {
        "runs": len(runs),
        "impressions": len(impressions),
        "distinct_served_candidates": len(distinct_candidates),
        "outcome_rows": len(outcomes),
        "impressions_with_any_recorded_outcome": outcome_linked_impressions,
        "impressions_without_recorded_outcome_unknown": max(0, len(impressions) - outcome_linked_impressions),
        "duplicate_run_candidate_rows": duplicate_run_candidates,
        "duplicate_rank_rows": duplicate_ranks,
        "invalid_rank_rows": rank_invalid,
        "runs_without_impressions": runs_missing_impression_rows,
        "runs_with_candidate_count_mismatch": candidate_count_mismatch,
        "runs_missing_candidate_count": run_impression_count_missing,
        "declared_candidate_count_total": sum(
            int(row["candidate_count"])
            for row in runs
            if isinstance(row.get("candidate_count"), (int, float))
            and math.isfinite(float(row["candidate_count"]))
        ),
        "runs_with_rank_gaps_within_declared_candidate_count": runs_with_rank_gaps_within_declared_count,
        "unrepresented_rank_slots_within_declared_candidate_counts": unrepresented_rank_slots,
    }

    exact_replay_reasons = []
    has_complete_candidate_snapshot_hashes = bool(impressions) and all(
        row.get("candidate_snapshot_hash") for row in impressions
    )
    if not snapshot_table_present and not has_complete_candidate_snapshot_hashes:
        exact_replay_reasons.append("no per-run candidate-pool or candidate-state snapshots")
    if not impressions or (not stage_table_present and impressions_with_stage_scores != len(impressions)):
        exact_replay_reasons.append("no per-candidate stage-score decomposition")
    if not runs or runs_with_feature_snapshot != len(runs):
        exact_replay_reasons.append("no per-run pre-action feature/profile snapshot or as-of watermark")
    if not runs or runs_with_lineage != len(runs):
        exact_replay_reasons.append("no per-run code, model, and feature lineage fingerprint")
    if not runs or not impressions:
        exact_replay_reasons.append("no observed historical cohort to replay")
    # Exact pipeline ablations also need a candidate universe and feature state
    # that match the served timestamp, not only current cache rows.
    exact_historical_replay_identifiable = not exact_replay_reasons

    settings_keys = sorted(
        str(row.get("key")) for row in _rows(data, "settings") if row.get("key") is not None
    )
    embedding_models = sorted({
        f"{row.get('backend') or 'missing'}/{row.get('model') or 'missing'}"
        for row in embeddings
    })

    return {
        "export": {
            "exported_at_utc": data.get("exported_at_utc"),
            "schema_version": data.get("schema_version"),
            "integrity_ok": data.get("integrity_ok"),
            "foreign_key_violations": data.get("foreign_key_violations"),
            "source_hash_files": len(data.get("source_hashes", {}))
            if isinstance(data.get("source_hashes"), Mapping) else 0,
        },
        "counts": counts,
        "run_telemetry": {
            "policy_counts": _counter_report(policies),
            "policy_version_counts": _counter_report(policy_versions),
            "runs_with_interaction_learning_metadata": runs_with_learning_metadata,
            "runs_with_applied_interaction_adjustment": learning_applied,
            "interaction_learning_mode_counts": _counter_report(metadata_modes),
            "median_observed_books_in_run_metadata": _median(observed_books),
            "median_adjusted_candidates_in_run_metadata": _median(adjusted_candidates),
            "median_qualified_features_in_run_metadata": _median(qualified_features),
            "runs_with_per_run_code_or_model_lineage": runs_with_lineage,
            "runs_with_preaction_feature_snapshot": runs_with_feature_snapshot,
        },
        "exposure_telemetry": {
            "visible_impressions": visible,
            "visible_distinct_candidates": len(visible_candidate_ids),
            "runs_with_any_visible_impression": len(visible_run_ids),
            "impressions_without_visible_timestamp_unknown_visibility": max(0, len(impressions) - visible),
            "visible_rows_missing_or_invalid_time": visible_missing_presentation_time,
            "visible_before_presented_rows": visible_before_presentation,
            "propensity_counts": _counter_report(propensity_counts),
            "runtime_exploration_enabled": (
                bool(_metadata(data.get("runtime", {})).get("exploration_enabled"))
                if isinstance(data.get("runtime"), Mapping) else None
            ),
            "impressions_with_logged_final_served_score": served_score_coverage,
            "candidate_rows_currently_joinable": current_candidate_rows,
            "candidate_rows_with_current_updated_at_after_run": candidate_rows_updated_after_run,
            "candidate_quality_rows_currently_joinable": quality_rows,
            "candidate_quality_rows_with_current_updated_at_after_run": quality_rows_updated_after_run,
            "current_embedding_model_families": embedding_models,
            "current_settings_key_names": settings_keys,
        },
        "outcome_telemetry": {
            "label_kind_counts": _counter_report(label_kinds),
            "label_class_counts": _counter_report(label_class_counts),
            "label_class_counts_by_kind": {
                kind: _counter_report(values)
                for kind, values in sorted(label_classes_by_kind.items())
            },
            "confidence_counts": _counter_report(confidence_counts),
            "distinct_outcome_impressions": len(outcome_impression_ids),
            "distinct_outcome_candidates": len(outcome_candidate_ids),
            "distinct_outcome_runs": len(outcome_run_ids),
            "outcome_rows_by_kind_distinct_candidates": {
                kind: len(kind_candidate_ids[kind]) for kind in sorted(kind_candidate_ids)
            },
            "outcome_rows_by_kind_distinct_runs": {
                kind: len(kind_run_ids[kind]) for kind in sorted(kind_run_ids)
            },
            "visible_impressions_with_recorded_outcome": len(visible_outcome_impression_ids),
            "visible_impressions_without_recorded_outcome_unknown": max(
                0, len(visible_impression_ids) - len(visible_outcome_impression_ids)
            ),
            "runs_with_any_recorded_outcome": len(outcome_run_ids),
            "outcome_event_missing_links": outcome_links_missing_event,
            "outcome_impression_missing_links": outcome_links_missing_impression,
            "event_vs_presentation_time": _counter_report(labels_by_exposure_time),
            "outcome_events_created_after_their_run": outcome_event_created_after_run,
            "outcome_event_or_run_creation_time_unknown": outcome_event_created_time_unknown,
            "unobserved_impressions_are_unknown": True,
            "outcome_values_are_immutable_history": False,
        },
        "interaction_asof_coverage": {
            "source_action_rows": len(action_sources),
            "source_action_rows_with_creation_time": source_events_with_known_creation_time,
            "runs_with_at_least_one_action_row_created_strictly_before_run": runs_with_prior_action_source,
            "median_prior_action_rows_per_timestamped_run": _median(prior_source_events_per_run),
            "feature_snapshot_reconstructible_from_these_rows": False,
            "limitations": [
                "created_at shows when source rows became available, but no profile/event watermark is stored per run",
                "action values and outcome attribution can be updated in place",
                "candidate title, author, subjects, descriptions, and quality fields have current values only",
                "embedding rows and settings are current cache state, not immutable as-of-run inputs",
                "candidate updated_at is a current row timestamp and does not isolate score_all execution or preserve prior scores",
            ],
        },
        "current_serving_reference": _current_reference_report(data.get("serving_reference")),
        "pipeline_replayability": {
            "candidate_snapshot_table_present": snapshot_table_present,
            "stage_score_table_present": stage_table_present,
            "impressions_with_stage_score_fields": impressions_with_stage_scores,
            "exact_historical_pipeline_replay_identifiable": exact_historical_replay_identifiable,
            "reasons_not_identifiable": exact_replay_reasons,
            "logged_final_rank_and_score_are_available": served_score_coverage == len(impressions),
            "current_candidate_rows_are_historical_snapshots": False,
            "source_hashes_are_recorded_per_run": False,
            "score_all_execution_history_available_in_export": bool(_rows(data, "jobs")),
            "job_history_note": "A jobs table exists in the codebase but was not included in this allowlisted capture. Completion times alone would not reconstruct candidate/profile/cache inputs or stage scores.",
            "score_cache_asof_state_exactly_reconstructible": False,
        },
        "interpretation": {
            "logged_rank_outcomes": "factual outcomes on actually served logged ranks; descriptive only",
            "sparse_propensity_support": "deterministic propensities do not identify alternate-policy effects",
            "pipeline_ablation_utility": "not historically identifiable without per-run inputs, feature availability, and component scores",
            "unseen_or_unacted_impressions": "unknown, never negative",
            "dependence": "one installation with repeated correlated runs; run-clustered metrics are descriptive and do not create independent trials or reader-level replication",
        },
    }


def _write_private_json(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Private allowlisted JSON export")
    parser.add_argument("--output", type=Path, help="Write aggregate JSON with mode 0600")
    args = parser.parse_args()
    data = json.loads(args.source.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        parser.error("source must contain a JSON object of table arrays")
    report = build_report(data)
    if args.output:
        _write_private_json(args.output, report)
    else:
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
