import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_pipeline_history.py"
SPEC = importlib.util.spec_from_file_location("audit_pipeline_history", SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def test_event_exposure_order_uses_strict_utc_timestamp_and_day_rules():
    # Offset timestamps are compared after UTC normalization.
    later, kind = audit._is_later_event_than_exposure(
        {"occurred_at": "2026-10-02T00:16:00+01:00", "metadata": "{}"},
        {"presented_at": "2026-10-01T23:15:00Z"},
    )
    assert later is True
    assert kind == "timestamp_strictly_after"

    same_instant, kind = audit._is_later_event_than_exposure(
        {"occurred_at": "2026-10-02T00:15:00+01:00", "metadata": "{}"},
        {"presented_at": "2026-10-01T23:15:00Z"},
    )
    assert same_instant is False
    assert kind == "same_instant"

    same_day, kind = audit._is_later_event_than_exposure(
        {"occurred_at": "2026-10-01", "metadata": '{"time_precision":"day"}'},
        {"presented_at": "2026-10-01T23:59:00Z"},
    )
    assert same_day is False
    assert kind == "same_utc_day"

    next_day, kind = audit._is_later_event_than_exposure(
        {"occurred_at": "2026/10/02", "metadata": '{"time_precision":"day"}'},
        {"presented_at": "2026-10-01T23:59:00Z"},
    )
    assert next_day is True
    assert kind == "day_strictly_after"


def test_aggregate_keeps_unobserved_unknown_and_reports_replay_gaps():
    data = {
        "exported_at_utc": "2026-10-02T16:40:55Z",
        "schema_version": 18,
        "integrity_ok": True,
        "foreign_key_violations": 0,
        "runtime": {"exploration_enabled": False},
        "recommendation_runs": [
            {
                "id": "private-run-a", "policy": "rating-neighborhood",
                "policy_version": "p1", "candidate_count": 2,
                "created_at": "2026-10-01T12:00:00Z",
                "metadata": '{"interaction_learning":{"applied":true,"observed_books":4}}',
            },
            {
                "id": "private-run-b", "policy": "rating-neighborhood",
                "policy_version": "p1", "candidate_count": 1,
                "created_at": "2026-10-02T12:00:00Z",
                "metadata": '{"interaction_learning":{"applied":false,"observed_books":0}}',
            },
        ],
        "recommendation_impressions": [
            {"id": 11, "run_id": "private-run-a", "candidate_id": 9, "rank": 1,
             "score": 77.2, "propensity": 1.0, "presented_at": "2026-10-01T12:00:00Z",
             "visible_at": "2026-10-01T12:00:03Z"},
            {"id": 12, "run_id": "private-run-a", "candidate_id": 10, "rank": 2,
             "score": 70.0, "propensity": 1.0, "presented_at": "2026-10-01T12:00:00Z",
             "visible_at": None},
            {"id": 13, "run_id": "private-run-b", "candidate_id": 9, "rank": 1,
             "score": 78.0, "propensity": 1.0, "presented_at": "2026-10-02T12:00:00Z",
             "visible_at": "2026-10-02T12:00:02Z"},
        ],
        "recommendation_events": [
            {"id": 20, "event_key": "action-one", "event_type": "save", "candidate_id": 9,
             "run_id": "private-run-a", "created_at": "2026-10-01T13:00:00Z",
             "occurred_at": "2026-10-01T12:30:00Z", "metadata": "{}"},
            {"id": 21, "event_key": "action-two", "event_type": "reject", "candidate_id": 10,
             "run_id": "private-run-a", "created_at": "2026-10-03T13:00:00Z",
             "occurred_at": "2026-10-03T12:30:00Z", "metadata": "{}"},
        ],
        "recommendation_outcomes": [
            {"id": 1, "impression_id": 11, "event_id": 20, "label": 1.0,
             "label_kind": "save", "confidence": 1.0, "attributed_at": "2026-10-01T13:00:00Z"},
        ],
        "candidates": [{"id": 9, "updated_at": "2026-10-03T00:00:00Z"}],
        "candidate_quality": [],
        "embeddings": [],
        "settings": [],
        "feedback": [],
    }

    report = audit.build_report(data)
    assert report["counts"]["runs"] == 2
    assert report["counts"]["distinct_served_candidates"] == 2
    assert report["counts"]["impressions_with_any_recorded_outcome"] == 1
    assert report["counts"]["impressions_without_recorded_outcome_unknown"] == 2
    assert report["counts"]["unrepresented_rank_slots_within_declared_candidate_counts"] == 0
    assert report["exposure_telemetry"]["visible_impressions"] == 2
    assert report["exposure_telemetry"]["visible_distinct_candidates"] == 1
    assert report["exposure_telemetry"]["propensity_counts"] == {"1": 3}
    assert report["interaction_asof_coverage"]["runs_with_at_least_one_action_row_created_strictly_before_run"] == 1
    assert report["pipeline_replayability"]["exact_historical_pipeline_replay_identifiable"] is False
    assert report["pipeline_replayability"]["logged_final_rank_and_score_are_available"] is True
    assert report["outcome_telemetry"]["unobserved_impressions_are_unknown"] is True
    assert report["outcome_telemetry"]["distinct_outcome_impressions"] == 1
    assert report["outcome_telemetry"]["distinct_outcome_candidates"] == 1
    assert report["outcome_telemetry"]["distinct_outcome_runs"] == 1
    assert report["outcome_telemetry"]["label_class_counts"] == {"positive": 1}
    assert report["outcome_telemetry"]["visible_impressions_with_recorded_outcome"] == 1
    assert report["outcome_telemetry"]["event_vs_presentation_time"] == {"timestamp_strictly_after": 1}

    # The report must never serialize row identifiers or private identity text.
    serialized = str(report)
    for private_value in ("private-run-a", "private-run-b", "candidate_id", "action-one"):
        assert private_value not in serialized


def test_event_created_at_must_strictly_precede_run_for_asof_availability():
    data = {
        "recommendation_runs": [
            {"id": "run-a", "policy_version": "v", "created_at": "2026-10-02T00:00:00Z",
             "candidate_count": 0, "metadata": "{}"},
            {"id": "run-b", "policy_version": "v", "created_at": "2026-10-02T01:00:00Z",
             "candidate_count": 0, "metadata": "{}"},
        ],
        "recommendation_impressions": [],
        "recommendation_events": [
            {"id": 1, "event_key": "before", "event_type": "save",
             "created_at": "2026-10-02T00:00:00Z"},
        ],
        "recommendation_outcomes": [],
        "feedback": [],
    }
    coverage = audit.build_report(data)["interaction_asof_coverage"]
    assert coverage["runs_with_at_least_one_action_row_created_strictly_before_run"] == 1


def test_current_reference_is_aggregate_and_never_claimed_as_historical_replay():
    reference = {
        "recommended": {
            "scope": "recommended",
            "clock_utc": "2026-10-02T16:40:00Z",
            "base": [{"id": "private-a", "score": 50.0}, {"id": "private-b", "score": 40.0}],
            "personalized": [{"id": "private-a", "score": 51.0}, {"id": "private-b", "score": 40.0}],
            "final": [{"id": "private-b", "score": 40.0}, {"id": "private-a", "score": 51.0}],
            "vectors": [[0.1, 0.2]],
            "interaction_vectors": [],
            "interaction_diagnostics": {"applied": True, "adjusted_candidates": 1},
            "slate_diagnostics": {"policy_version": "mmr-v1", "reordered_items": 2},
        },
    }
    report = audit._current_reference_report(reference)
    scope = report["scopes"]["recommended"]
    assert report["historical_outcome_or_ablation_evidence"] is False
    assert scope["rows_with_final_serving_vector"] == 1
    assert scope["base_to_personalized_score_rows_changed"] == 1
    assert scope["base_to_final_candidate_order_changed"] is True
    assert scope["historical_run_replay"] is False
    serialized = str(report)
    assert "private-a" not in serialized
    assert "private-b" not in serialized


def test_empty_export_does_not_vacuously_claim_complete_replay():
    report = audit.build_report({})
    assert report["pipeline_replayability"]["exact_historical_pipeline_replay_identifiable"] is False
    assert "no observed historical cohort to replay" in report["pipeline_replayability"]["reasons_not_identifiable"]
