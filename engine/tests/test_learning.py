import asyncio
import csv
import io
import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth_helpers import authenticated_headers

from afterword_engine.config import settings
from afterword_engine.database import connect, initialize, row, transaction
from afterword_engine.decision_evidence import build_decision_evidence, load_decision_evidence
from afterword_engine.ingestion import import_goodreads_csv
from afterword_engine.learning import (
    attribute_read_outcomes,
    create_recommendation_run,
    record_event_in_connection,
)
from afterword_engine.interaction_personalization import (
    load_interaction_events,
    personalize_recommendations,
)
from afterword_engine.main import app, recommendation_list, tracked_recommendations
from afterword_engine.scoring import score_all


@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "learning.db")
    initialize()
    return settings.db


def test_recommendation_responses_create_ranked_run_and_impressions(database):
    recommendations, run_id = tracked_recommendations()
    run = row("SELECT * FROM recommendation_runs WHERE id=?", (run_id,))
    impressions = row(
        "SELECT COUNT(*) count FROM recommendation_impressions WHERE run_id=?",
        (run_id,),
    )
    assert run["policy"] == "rating-neighborhood"
    assert run["policy_version"] == "rating-kernel-recency-era-confidence-interaction-installation-slate-v2"
    assert run["candidate_count"] == len(recommendations)
    assert impressions["count"] == len(recommendations)
    assert row(
        "SELECT COUNT(*) count FROM recommendation_impressions WHERE run_id=? AND propensity=1",
        (run_id,),
    )["count"] == len(recommendations)
    learning = json.loads(run["metadata"])["interaction_learning"]
    assert learning["mode"] == "confidence_gated_live"
    assert learning["scope"] == "installation"

    with TestClient(app, headers=authenticated_headers()) as client:
        response = client.get("/api/recommendations")
    assert response.status_code == 200
    header_run = response.headers["x-bookward-recommendation-run"]
    api_run = row("SELECT metadata FROM recommendation_runs WHERE id=?", (header_run,))
    assert json.loads(api_run["metadata"])["interaction_learning"]["scope"] == "installation"


def test_confident_existing_actions_reorder_live_results_before_page_limit(database):
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        con.execute("UPDATE candidates SET status='rejected'")

        def add_book(title, author, subject, status, score=0):
            candidate_id = con.execute(
                "INSERT INTO candidates(title,author,genres,score,status,source_id,normalized_key) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    title,
                    author,
                    json.dumps([subject]),
                    score,
                    status,
                    source_id,
                    f"interaction test {title.casefold()}",
                ),
            ).lastrowid
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted',metadata_confidence=1 "
                "WHERE candidate_id=?",
                (candidate_id,),
            )
            return candidate_id

        for index in range(6):
            candidate_id = add_book(
                f"Saved solar book {index}",
                f"Solar author {index}",
                "Solar myth",
                "saved",
            )
            record_event_in_connection(
                con,
                event_key=f"learning-save-{index:04d}",
                candidate_id=candidate_id,
                event_type="save",
                source="ui",
            )
        for index in range(6):
            candidate_id = add_book(
                f"Rejected noir book {index}",
                f"Noir author {index}",
                "Dark thriller",
                "rejected",
            )
            record_event_in_connection(
                con,
                event_key=f"learning-reject-{index:04d}",
                candidate_id=candidate_id,
                event_type="reject",
                source="ui",
            )

        solar_id = add_book(
            "New solar recommendation", "New solar author", "Solar myth", "recommended", 49
        )
        add_book(
            "New noir recommendation", "New noir author", "Dark thriller", "recommended", 50
        )
        neutral_id = add_book(
            "New neutral recommendation", "New neutral author", "Quiet romance", "recommended", 49.5
        )

    recommendations, run_id = tracked_recommendations(
        status="recommended", limit=2
    )

    run = row("SELECT metadata FROM recommendation_runs WHERE id=?", (run_id,))
    learning = json.loads(run["metadata"])["interaction_learning"]
    assert [item["id"] for item in recommendations] == [solar_id, neutral_id], (
        [(item["id"], item["score"]) for item in recommendations],
        learning,
    )
    assert recommendations[0]["score"] > recommendations[1]["score"]
    run = row("SELECT policy_version,metadata FROM recommendation_runs WHERE id=?", (run_id,))
    metadata = json.loads(run["metadata"])["interaction_learning"]
    assert run["policy_version"] == "rating-kernel-recency-era-confidence-interaction-installation-slate-v2"
    assert metadata["mode"] == "confidence_gated_live"
    assert metadata["applied"] is True
    assert metadata["observed_books"] == 12


def test_legacy_feedback_is_used_without_double_counting_new_events(database):
    with transaction() as con:
        legacy_id = con.execute(
            "INSERT INTO candidates(title,author,normalized_key) VALUES(?,?,?)",
            ("Legacy save", "Reader", "legacy save reader"),
        ).lastrowid
        con.execute(
            "INSERT INTO feedback(candidate_id,action) VALUES(?, 'save')",
            (legacy_id,),
        )

        event_id = con.execute(
            "INSERT INTO candidates(title,author,normalized_key) VALUES(?,?,?)",
            ("Event reject", "Reader", "event reject reader"),
        ).lastrowid
        feedback_id = con.execute(
            "INSERT INTO feedback(candidate_id,action) VALUES(?, 'reject')",
            (event_id,),
        ).lastrowid
        record_event_in_connection(
            con,
            event_key=f"feedback:{feedback_id}",
            candidate_id=event_id,
            event_type="reject",
            source="ui",
        )

    events = load_interaction_events()

    assert len(events) == 2
    assert {(event["candidate_id"], event["event_type"]) for event in events} == {
        (legacy_id, "save"),
        (event_id, "reject"),
    }


def test_learning_aggregates_actions_across_legacy_browser_sessions(database):
    with transaction() as con:
        reader_a_id = con.execute(
            "INSERT INTO candidates(title,author,genres,normalized_key) VALUES(?,?,?,?)",
            ("Reader A favorite", "Writer A", '["Solar myth"]', "reader a favorite writer a"),
        ).lastrowid
        reader_b_id = con.execute(
            "INSERT INTO candidates(title,author,genres,normalized_key) VALUES(?,?,?,?)",
            ("Reader B favorite", "Writer B", '["Dark thriller"]', "reader b favorite writer b"),
        ).lastrowid
    run_a = create_recommendation_run(
        [{"id": reader_a_id, "score": 50}], session_id="session-reader-a"
    )
    run_b = create_recommendation_run(
        [{"id": reader_b_id, "score": 50}], session_id="session-reader-b"
    )
    with transaction() as con:
        record_event_in_connection(
            con,
            event_key="reader-a-save-0001",
            candidate_id=reader_a_id,
            event_type="save",
            run_id=run_a,
            source="ui",
        )
        record_event_in_connection(
            con,
            event_key="reader-b-save-0001",
            candidate_id=reader_b_id,
            event_type="save",
            run_id=run_b,
            source="ui",
        )

    events = load_interaction_events()
    assert {
        (event["candidate_id"], event["event_type"])
        for event in events
        if event["candidate_id"] in {reader_a_id, reader_b_id}
    } == {
        (reader_a_id, "save"),
        (reader_b_id, "save"),
    }
    _, run_id = tracked_recommendations()
    learning = json.loads(
        row("SELECT metadata FROM recommendation_runs WHERE id=?", (run_id,))["metadata"]
    )["interaction_learning"]
    assert learning["scope"] == "installation"
    assert learning["observed_books"] >= 2


def test_manual_read_marks_candidate_read_and_learns_without_a_session(database):
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        candidate_id = con.execute(
            "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
            "VALUES(?,?,20,'recommended',?,?)",
            ("Already Read", "A Reader", source_id, "already read a reader"),
        ).lastrowid
        unrated_id = con.execute(
            "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
            "VALUES(?,?,19,'recommended',?,?)",
            ("Unrated Read", "Another Reader", source_id, "unrated read another reader"),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted' WHERE candidate_id IN (?,?)",
            (candidate_id, unrated_id),
        )

    assert candidate_id in {
        item["id"] for item in recommendation_list(status="recommended", limit=None)
    }
    with TestClient(app, headers=authenticated_headers()) as client:
        response = client.post(
            f"/api/recommendations/{candidate_id}/read",
            json={"rating": 4},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "read"
    assert response.json()["rating"] == 4
    read = row(
        "SELECT title,author,rating,read_at,source FROM reads WHERE title=? AND author=?",
        ("Already Read", "A Reader"),
    )
    assert (read["title"], read["author"], read["rating"], read["source"]) == (
        "Already Read", "A Reader", 4.0, "manual"
    )
    assert read["read_at"]
    assert candidate_id not in {
        item["id"] for item in recommendation_list(status="recommended", limit=None)
    }
    event = row(
        "SELECT event_type,value,source,metadata FROM recommendation_events "
        "WHERE candidate_id=? AND source='manual_read'",
        (candidate_id,),
    )
    assert event["event_type"] == "read"
    assert event["value"] == 4
    assert json.loads(event["metadata"]) == {
        "read_source": "manual",
        "time_precision": "timestamp",
    }
    assert candidate_id in {
        event["candidate_id"] for event in load_interaction_events()
    }

    with TestClient(app, headers=authenticated_headers()) as client:
        no_rating = client.post(
            f"/api/recommendations/{candidate_id}/read",
            json={},
        )
        unrated = client.post(
            f"/api/recommendations/{unrated_id}/read",
            json={},
        )
        invalid = client.post(
            f"/api/recommendations/{candidate_id}/read",
            json={"rating": 6},
        )
    assert no_rating.status_code == 200
    assert no_rating.json()["rating"] == 4
    assert unrated.status_code == 200
    assert unrated.json()["rating"] is None
    assert invalid.status_code == 422
    assert row(
        "SELECT COUNT(*) count FROM reads WHERE title=? AND author=?",
        ("Already Read", "A Reader"),
    )["count"] == 1
    assert row(
        "SELECT rating FROM reads WHERE title=? AND author=?",
        ("Unrated Read", "Another Reader"),
    )["rating"] is None
    assert unrated_id not in {
        item["id"] for item in recommendation_list(status="recommended", limit=None)
    }


def test_telemetry_batch_is_strict_and_idempotent(database):
    recommendations, run_id = tracked_recommendations()
    candidate_id = recommendations[0]["id"]
    payload = {
        "events": [
            {
                "event_key": "visible-event-1",
                "candidate_id": candidate_id,
                "run_id": run_id,
                "event_type": "visible",
                "metadata": {"view": "discover"},
            }
        ]
    }
    with TestClient(app, headers=authenticated_headers()) as client:
        first = client.post("/api/telemetry/events", json=payload)
        second = client.post("/api/telemetry/events", json=payload)
        conflict = client.post(
            "/api/telemetry/events",
            json={
                "events": [
                    {
                        **payload["events"][0],
                        "candidate_id": recommendations[1]["id"],
                    }
                ]
            },
        )
        missing_run = client.post(
            "/api/telemetry/events",
            json={
                "events": [
                    {
                        "event_key": "visible-event-2",
                        "candidate_id": candidate_id,
                        "run_id": "missing-run-1",
                        "event_type": "visible",
                    }
                ]
            },
        )
        invalid_type = client.post(
            "/api/telemetry/events",
            json={
                "events": [
                    {
                        "event_key": "invalid-event-1",
                        "candidate_id": candidate_id,
                        "event_type": "rating",
                    }
                ]
            },
        )
    assert first.status_code == 200 and first.json() == {"accepted": 1, "duplicates": 0, "ids": [1]}
    assert second.status_code == 200 and second.json() == {"accepted": 0, "duplicates": 1, "ids": [1]}
    assert conflict.status_code == 400
    assert missing_run.status_code == 400
    assert invalid_type.status_code == 422
    assert row(
        "SELECT visible_at FROM recommendation_impressions WHERE run_id=? AND candidate_id=?",
        (run_id, candidate_id),
    )["visible_at"]
    assert row("SELECT COUNT(*) count FROM recommendation_events")["count"] == 1


def test_feedback_with_run_id_creates_explicit_outcome(database):
    recommendations, run_id = tracked_recommendations()
    candidate_id = recommendations[0]["id"]
    with TestClient(app, headers=authenticated_headers()) as client:
        response = client.post(
            f"/api/recommendations/{candidate_id}/feedback",
            json={"action": "save", "run_id": run_id},
        )
    assert response.status_code == 200
    event = row(
        "SELECT * FROM recommendation_events WHERE event_type='save' AND candidate_id=?",
        (candidate_id,),
    )
    outcome = row(
        "SELECT o.* FROM recommendation_outcomes o JOIN recommendation_events e ON e.id=o.event_id WHERE e.id=?",
        (event["id"],),
    )
    assert event["run_id"] == run_id
    assert outcome["label"] == 1
    assert outcome["label_kind"] == "explicit_feedback"
    assert outcome["confidence"] == 0.8

    with TestClient(app, headers=authenticated_headers()) as client:
        invalid = client.post(
            f"/api/recommendations/{candidate_id}/feedback",
            json={"action": "reject", "run_id": "missing-run-1"},
        )
    assert invalid.status_code == 400
    assert row("SELECT COUNT(*) count FROM feedback WHERE action='reject'")["count"] == 0


def test_maybe_later_is_persisted_as_neutral_and_can_be_undone(database):
    recommendations, run_id = tracked_recommendations()
    candidate_id = recommendations[0]["id"]

    with TestClient(app, headers=authenticated_headers()) as client:
        passed = client.post(
            f"/api/recommendations/{candidate_id}/feedback",
            json={"action": "reject", "run_id": run_id},
        )
        deferred = client.post(
            f"/api/recommendations/{candidate_id}/feedback",
            json={"action": "maybe_later", "run_id": run_id},
        )
        decision_id = deferred.json()["decision_id"]

        assert passed.status_code == 200
        assert deferred.status_code == 200
        assert deferred.json()["status"] == "maybe_later"
        assert row("SELECT status FROM candidates WHERE id=?", (candidate_id,))["status"] == "maybe_later"
        assert row(
            "SELECT action,previous_status FROM feedback WHERE id=?",
            (decision_id,),
        ) == {"action": "maybe_later", "previous_status": "rejected"}
        event = row(
            "SELECT event_type FROM recommendation_events WHERE event_key=?",
            (f"feedback:{decision_id}",),
        )
        assert event["event_type"] == "maybe_later"
        assert row(
            "SELECT COUNT(*) count FROM recommendation_outcomes WHERE event_id="
            "(SELECT id FROM recommendation_events WHERE event_key=?)",
            (f"feedback:{decision_id}",),
        )["count"] == 0
        events = [
            item for item in load_interaction_events()
            if item["candidate_id"] == candidate_id
        ]
        assert {item["event_type"] for item in events} == {"reject", "maybe_later"}
        _, neutral_learning = personalize_recommendations([], events)
        assert neutral_learning["observed_books"] == 0
        assert candidate_id not in {item["id"] for item in recommendation_list()}
        assert candidate_id in {
            item["id"] for item in recommendation_list(status="decisions")
        }

        undone = client.post(
            f"/api/recommendations/{candidate_id}/undo",
            json={"decision_id": decision_id},
        )

    assert undone.status_code == 200
    assert undone.json()["status"] == "rejected"
    assert row("SELECT undone_at FROM feedback WHERE id=?", (decision_id,))["undone_at"]
    assert row("SELECT status FROM candidates WHERE id=?", (candidate_id,))["status"] == "rejected"
    assert row(
        "SELECT event_type FROM recommendation_events WHERE event_key=?",
        (f"undo-feedback:{decision_id}",),
    )["event_type"] == "reject"
    _, passed_learning = personalize_recommendations(
        [],
        [
            item for item in load_interaction_events()
            if item["candidate_id"] == candidate_id
        ],
    )
    assert passed_learning["observed_books"] == 1


@pytest.mark.parametrize(
    ("action", "status", "expected_label"),
    [("save", "saved", 1.0), ("reject", "rejected", 0.0)],
)
def test_undo_reverses_save_and_pass_state_without_relabeling_maybe_later(
    database, action, status, expected_label
):
    recommendations, run_id = tracked_recommendations()
    candidate_id = recommendations[0]["id"]
    with TestClient(app, headers=authenticated_headers()) as client:
        decision = client.post(
            f"/api/recommendations/{candidate_id}/feedback",
            json={"action": action, "run_id": run_id},
        ).json()
        outcome = row(
            "SELECT o.label FROM recommendation_outcomes o JOIN recommendation_events e "
            "ON e.id=o.event_id WHERE e.event_key=?",
            (f"feedback:{decision['decision_id']}",),
        )
        assert outcome["label"] == expected_label
        assert row("SELECT status FROM candidates WHERE id=?", (candidate_id,))["status"] == status

        undone = client.post(
            f"/api/recommendations/{candidate_id}/undo",
            json={"decision_id": decision["decision_id"]},
        )
        stale = client.post(
            f"/api/recommendations/{candidate_id}/undo",
            json={"decision_id": decision["decision_id"]},
        )

    assert undone.status_code == 200
    assert row("SELECT status FROM candidates WHERE id=?", (candidate_id,))["status"] == recommendations[0]["status"]
    assert row(
        "SELECT event_type FROM recommendation_events WHERE event_key=?",
        (f"undo-feedback:{decision['decision_id']}",),
    )["event_type"] == "restore"
    assert row(
        "SELECT COUNT(*) count FROM recommendation_outcomes WHERE event_id="
        "(SELECT id FROM recommendation_events WHERE event_key=?)",
        (f"feedback:{decision['decision_id']}",),
    )["count"] == 0
    assert stale.status_code == 409


def test_only_the_latest_feedback_can_be_undone(database):
    recommendations, run_id = tracked_recommendations()
    candidate_id = recommendations[0]["id"]
    with TestClient(app, headers=authenticated_headers()) as client:
        first = client.post(
            f"/api/recommendations/{candidate_id}/feedback",
            json={"action": "save", "run_id": run_id},
        ).json()
        second = client.post(
            f"/api/recommendations/{candidate_id}/feedback",
            json={"action": "reject", "run_id": run_id},
        ).json()
        stale = client.post(
            f"/api/recommendations/{candidate_id}/undo",
            json={"decision_id": first["decision_id"]},
        )
        latest = client.post(
            f"/api/recommendations/{candidate_id}/undo",
            json={"decision_id": second["decision_id"]},
        )

    assert stale.status_code == 409
    assert latest.status_code == 200
    assert row("SELECT status FROM candidates WHERE id=?", (candidate_id,))["status"] == "saved"


@pytest.mark.parametrize(
    ("initial_action", "initial_status", "expected_compensation", "expected_observed"),
    [
        ("save", "saved", "save", 1),
        ("reject", "rejected", "reject", 1),
        ("maybe_later", "maybe_later", "maybe_later", 0),
    ],
)
def test_undo_reverses_restore_and_restores_compensation_signal(
    database, initial_action, initial_status, expected_compensation, expected_observed
):
    recommendations, run_id = tracked_recommendations()
    candidate_id = recommendations[0]["id"]
    with TestClient(app, headers=authenticated_headers()) as client:
        client.post(
            f"/api/recommendations/{candidate_id}/feedback",
            json={"action": initial_action, "run_id": run_id},
        )
        restored = client.post(
            f"/api/recommendations/{candidate_id}/feedback",
            json={"action": "restore"},
        )
        assert restored.status_code == 200
        restore_payload = restored.json()
        assert restore_payload["status"] == "recommended"
        assert restore_payload["decision_id"] > 0

        _, neutral_learning = personalize_recommendations(
            [],
            [
                item for item in load_interaction_events()
                if item["candidate_id"] == candidate_id
            ],
        )
        assert neutral_learning["observed_books"] == 0

        undone = client.post(
            f"/api/recommendations/{candidate_id}/undo",
            json={"decision_id": restore_payload["decision_id"]},
        )

    assert undone.status_code == 200
    assert undone.json() == {"id": candidate_id, "status": initial_status}
    assert row("SELECT status FROM candidates WHERE id=?", (candidate_id,))["status"] == initial_status
    assert row("SELECT undone_at FROM feedback WHERE id=?", (restore_payload["decision_id"],))["undone_at"]
    assert row(
        "SELECT event_type FROM recommendation_events WHERE event_key=?",
        (f"undo-feedback:{restore_payload['decision_id']}",),
    )["event_type"] == expected_compensation
    _, reverted_learning = personalize_recommendations(
        [],
        [
            item for item in load_interaction_events()
            if item["candidate_id"] == candidate_id
        ],
    )
    assert reverted_learning["observed_books"] == expected_observed


def test_restore_undo_rejects_empty_previous_status_and_completed_reads(database):
    recommendations, _ = tracked_recommendations()
    read_candidate_id = recommendations[0]["id"]
    legacy_candidate_id = recommendations[1]["id"]
    with TestClient(app, headers=authenticated_headers()) as client:
        marked = client.post(f"/api/recommendations/{read_candidate_id}/read", json={"rating": 4})
        assert marked.status_code == 200

        restored_from_read = client.post(
            f"/api/recommendations/{read_candidate_id}/feedback",
            json={"action": "restore"},
        )
        assert restored_from_read.status_code == 200
        assert "decision_id" not in restored_from_read.json()
        read_restore_id = row(
            "SELECT id FROM feedback WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
            (read_candidate_id,),
        )["id"]
        undo_read_restore = client.post(
            f"/api/recommendations/{read_candidate_id}/undo",
            json={"decision_id": read_restore_id},
        )
        assert undo_read_restore.status_code == 404

        with transaction() as con:
            legacy_decision_id = con.execute(
                "INSERT INTO feedback(candidate_id,action,previous_status) VALUES(?,?,?)",
                (legacy_candidate_id, "restore", ""),
            ).lastrowid
        undo_legacy = client.post(
            f"/api/recommendations/{legacy_candidate_id}/undo",
            json={"decision_id": legacy_decision_id},
        )
        assert undo_legacy.status_code == 404


def test_bulk_feedback_records_run_attribution_and_supports_v1_bulk_undo(database):
    recommendations, run_id = tracked_recommendations()
    batch_ids = [recommendations[0]["id"], recommendations[1]["id"]]
    with TestClient(app, headers=authenticated_headers()) as client:
        bulk = client.post(
            "/api/recommendations/bulk-feedback",
            json={"ids": batch_ids, "action": "save", "run_id": run_id},
        )
        assert bulk.status_code == 200
        payload = bulk.json()
        assert payload["updated"] == 2
        assert payload["skipped"] == 0
        assert payload["status"] == "saved"
        assert payload["ids"] == sorted(batch_ids)
        assert [item["id"] for item in payload["decisions"]] == batch_ids

        for item in payload["decisions"]:
            feedback_row = row(
                "SELECT previous_status,undone_at FROM feedback WHERE id=?",
                (item["decision_id"],),
            )
            assert feedback_row == {"previous_status": "recommended", "undone_at": None}
            outcome = row(
                "SELECT o.label,o.confidence FROM recommendation_outcomes o "
                "JOIN recommendation_events e ON e.id=o.event_id WHERE e.event_key=?",
                (f"feedback:{item['decision_id']}",),
            )
            assert outcome == {"label": 1.0, "confidence": 0.8}

        token = client.post("/api/settings/api-tokens", json={"name": "Batch Undo"}).json()["token"]
        undone = client.post(
            "/api/v1/recommendations/bulk-undo",
            headers={"Authorization": f"Bearer {token}"},
            json={"decisions": payload["decisions"]},
        )
        stale = client.post(
            "/api/recommendations/bulk-undo",
            json={"decisions": payload["decisions"]},
        )

    assert undone.status_code == 200
    assert undone.json() == {
        "undone": 2,
        "items": [{"id": candidate_id, "status": "recommended"} for candidate_id in batch_ids],
    }
    assert stale.status_code == 409
    for item in payload["decisions"]:
        assert row("SELECT status FROM candidates WHERE id=?", (item["id"],))["status"] == "recommended"
        assert row("SELECT undone_at FROM feedback WHERE id=?", (item["decision_id"],))["undone_at"]
        assert row(
            "SELECT COUNT(*) count FROM recommendation_outcomes WHERE event_id="
            "(SELECT id FROM recommendation_events WHERE event_key=?)",
            (f"feedback:{item['decision_id']}",),
        )["count"] == 0
        assert row(
            "SELECT event_type FROM recommendation_events WHERE event_key=?",
            (f"undo-feedback:{item['decision_id']}",),
        )["event_type"] == "restore"


def test_bulk_feedback_offers_no_undo_for_books_that_were_already_read(database):
    recommendations, _run_id = tracked_recommendations()
    read_id = recommendations[0]["id"]
    other_id = recommendations[1]["id"]
    single_read_id = recommendations[2]["id"]
    with transaction() as con:
        con.execute(
            "UPDATE candidates SET status='read' WHERE id IN (?,?)", (read_id, single_read_id)
        )
    with TestClient(app, headers=authenticated_headers()) as client:
        bulk = client.post(
            "/api/recommendations/bulk-feedback",
            json={"ids": [read_id, other_id], "action": "save"},
        )
        single = client.post(
            f"/api/recommendations/{single_read_id}/feedback", json={"action": "reject"}
        )
    assert bulk.status_code == 200
    assert [item["id"] for item in bulk.json()["decisions"]] == [other_id]
    assert single.status_code == 200
    assert "decision_id" not in single.json()


def test_bulk_undo_rolls_back_atomically_on_conflict(database):
    recommendations, run_id = tracked_recommendations()
    first_id, second_id, third_id, fourth_id = [item["id"] for item in recommendations[:4]]
    with TestClient(app, headers=authenticated_headers()) as client:
        first_batch = client.post(
            "/api/recommendations/bulk-feedback",
            json={"ids": [first_id, second_id], "action": "save", "run_id": run_id},
        ).json()
        client.post(
            f"/api/recommendations/{second_id}/feedback",
            json={"action": "reject", "run_id": run_id},
        )
        newer_conflict = client.post(
            "/api/recommendations/bulk-undo",
            json={"decisions": first_batch["decisions"]},
        )
        assert newer_conflict.status_code == 409
        first_decision_id = first_batch["decisions"][0]["decision_id"]
        assert row("SELECT status FROM candidates WHERE id=?", (first_id,))["status"] == "saved"
        assert row("SELECT undone_at FROM feedback WHERE id=?", (first_decision_id,))["undone_at"] is None
        assert row(
            "SELECT COUNT(*) count FROM recommendation_outcomes WHERE event_id="
            "(SELECT id FROM recommendation_events WHERE event_key=?)",
            (f"feedback:{first_decision_id}",),
        )["count"] == 1
        assert row(
            "SELECT COUNT(*) count FROM recommendation_events WHERE event_key=?",
            (f"undo-feedback:{first_decision_id}",),
        )["count"] == 0

        second_batch = client.post(
            "/api/recommendations/bulk-feedback",
            json={"ids": [third_id, fourth_id], "action": "save", "run_id": run_id},
        ).json()
        client.post(f"/api/recommendations/{fourth_id}/read", json={"rating": 5})
        status_conflict = client.post(
            "/api/recommendations/bulk-undo",
            json={"decisions": second_batch["decisions"]},
        )
        assert status_conflict.status_code == 409
        third_decision_id = second_batch["decisions"][0]["decision_id"]
        assert row("SELECT status FROM candidates WHERE id=?", (third_id,))["status"] == "saved"
        assert row("SELECT undone_at FROM feedback WHERE id=?", (third_decision_id,))["undone_at"] is None


def test_read_attribution_requires_prior_exposure_and_updates_rating(database):
    recommendations, run_id = tracked_recommendations()
    candidate_id = recommendations[0]["id"]
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,rating,read_at,source) VALUES(?,?,?,?,?)",
            (recommendations[0]["title"], recommendations[0]["author"], None, "2099-01-01", "test"),
        )
    first = attribute_read_outcomes()
    assert first["outcomes_created"] == 1
    event = row("SELECT * FROM recommendation_events WHERE event_type='read' AND candidate_id=?", (candidate_id,))
    outcome = row("SELECT * FROM recommendation_outcomes WHERE event_id=?", (event["id"],))
    assert event["run_id"] == run_id
    assert outcome["label_kind"] == "completed_unrated"
    assert outcome["confidence"] == 0.4

    with transaction() as con:
        read_id = con.execute(
            "SELECT read_id FROM recommendation_outcomes WHERE event_id=?",
            (event["id"],),
        ).fetchone()[0]
        con.execute("UPDATE reads SET rating=5 WHERE id=?", (read_id,))
    second = attribute_read_outcomes()
    assert second["outcomes_updated"] == 1
    assert row("SELECT COUNT(*) count FROM recommendation_events WHERE event_key LIKE 'read:%'")["count"] == 1
    assert row("SELECT label,label_kind FROM recommendation_outcomes WHERE event_id=?", (event["id"],)) == {
        "label": 1.0,
        "label_kind": "rating_positive",
    }

    with transaction() as con:
        con.execute(
            "INSERT INTO candidates(title,author,source_id,normalized_key) VALUES(?,?,?,?)",
            ("Future exposure", "Writer", 1, "future exposure writer"),
        )
        future_id = con.execute("SELECT id FROM candidates WHERE title='Future exposure'").fetchone()[0]
        con.execute(
            "INSERT INTO reads(title,author,rating,read_at,source) VALUES(?,?,?,?,?)",
            ("Future exposure", "Writer", 1, "2000-01-01", "test"),
        )
    future_run = create_recommendation_run([{"id": future_id, "score": 50}])
    assert attribute_read_outcomes()["reads_attributed"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_events WHERE event_key LIKE 'read:%'")["count"] == 1
    assert future_run


def test_manual_mark_read_records_completion_time_and_attributes_exposure(database):
    recommendations, _run_id = tracked_recommendations()
    candidate = recommendations[0]

    with TestClient(app, headers=authenticated_headers()) as client:
        response = client.post(
            f"/api/recommendations/{candidate['id']}/read", json={"rating": 5}
        )

    assert response.status_code == 200
    read = row("SELECT id,read_at FROM reads WHERE title=? AND author=?", (
        candidate["title"], candidate["author"]
    ))
    assert read["read_at"]
    event = row(
        "SELECT id,occurred_at FROM recommendation_events "
        "WHERE candidate_id=? AND source='manual_read'",
        (candidate["id"],),
    )
    outcome = row(
        "SELECT read_id,label,label_kind FROM recommendation_outcomes WHERE event_id=?",
        (event["id"],),
    )
    assert event["occurred_at"] == read["read_at"]
    assert outcome == {
        "read_id": read["id"],
        "label": 1.0,
        "label_kind": "rating_positive",
    }


def test_manual_mark_read_does_not_attribute_historical_completion_to_newer_exposure(database):
    recommendations, run_id = tracked_recommendations()
    candidate = recommendations[0]
    with transaction() as con:
        con.execute(
            "UPDATE recommendation_impressions SET presented_at=? WHERE run_id=? AND candidate_id=?",
            ("2099-01-01T12:00:00+00:00", run_id, candidate["id"]),
        )
        con.execute(
            "INSERT INTO reads(title,author,rating,read_at,source) VALUES(?,?,?,?,?)",
            (candidate["title"], candidate["author"], 5, "2001-01-01", "goodreads_csv"),
        )

    with TestClient(app, headers=authenticated_headers()) as client:
        response = client.post(f"/api/recommendations/{candidate['id']}/read", json={})

    assert response.status_code == 200
    event = row(
        "SELECT id,run_id,occurred_at FROM recommendation_events "
        "WHERE candidate_id=? AND source='manual_read'",
        (candidate["id"],),
    )
    assert event["run_id"] is None
    assert event["occurred_at"] == "2001-01-01"
    assert row(
        "SELECT COUNT(*) count FROM recommendation_outcomes WHERE event_id=?",
        (event["id"],),
    )["count"] == 0
    assert attribute_read_outcomes()["reads_attributed"] == 0
    assert row(
        "SELECT COUNT(*) count FROM recommendation_events WHERE event_key LIKE 'read:%'"
    )["count"] == 0


def _set_candidate_exposure_time(candidate_id: int, run_id: str, presented_at: str) -> None:
    with transaction() as con:
        con.execute(
            "UPDATE recommendation_impressions SET presented_at=?,visible_at=NULL "
            "WHERE candidate_id=? AND run_id=?",
            (presented_at, candidate_id, run_id),
        )


@pytest.mark.parametrize("source", ["manual_history", "goodreads_csv"])
@pytest.mark.parametrize("read_at", [None, "not-a-date"], ids=["missing-date", "invalid-date"])
def test_historical_imports_do_not_use_import_time_as_completion_time(database, source, read_at):
    recommendations, run_id = tracked_recommendations()
    candidate = recommendations[0]
    _set_candidate_exposure_time(candidate["id"], run_id, "2025-01-01T12:00:00+00:00")
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,rating,read_at,source,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                candidate["title"],
                candidate["author"],
                5,
                read_at,
                source,
                "2099-01-02T12:00:00+00:00",
            ),
        )

    assert attribute_read_outcomes()["reads_attributed"] == 0
    assert row(
        "SELECT COUNT(*) count FROM recommendation_events "
        "WHERE event_type='read' AND candidate_id=?",
        (candidate["id"],),
    )["count"] == 0


@pytest.mark.parametrize("source", ["manual_history", "goodreads_csv"])
def test_historical_imports_use_an_explicit_valid_completion_date(database, source):
    recommendations, run_id = tracked_recommendations()
    candidate = recommendations[0]
    _set_candidate_exposure_time(candidate["id"], run_id, "2025-01-01T12:00:00+00:00")
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,rating,read_at,source,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                candidate["title"],
                candidate["author"],
                5,
                "2025-01-02",
                source,
                "2099-01-02T12:00:00+00:00",
            ),
        )

    assert attribute_read_outcomes()["reads_attributed"] == 1
    event = row(
        "SELECT occurred_at FROM recommendation_events "
        "WHERE event_type='read' AND candidate_id=?",
        (candidate["id"],),
    )
    assert event["occurred_at"] == "2025-01-02"


def test_current_manual_read_still_uses_created_at_when_read_date_is_missing(database):
    recommendations, run_id = tracked_recommendations()
    candidate = recommendations[0]
    _set_candidate_exposure_time(candidate["id"], run_id, "2099-01-01T12:00:00+00:00")
    created_at = "2099-01-02T12:00:00+00:00"
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,rating,read_at,source,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (candidate["title"], candidate["author"], 5, None, "manual", created_at),
        )

    assert attribute_read_outcomes()["reads_attributed"] == 1
    event = row(
        "SELECT occurred_at FROM recommendation_events "
        "WHERE event_type='read' AND candidate_id=?",
        (candidate["id"],),
    )
    assert event["occurred_at"] == created_at


def test_goodreads_import_runs_read_attribution(database):
    with transaction() as con:
        source_id = con.execute("SELECT id FROM sources WHERE is_default=1 LIMIT 1").fetchone()[0]
        con.execute(
            "INSERT INTO candidates(title,author,source_id,normalized_key) VALUES(?,?,?,?)",
            ("Imported later", "A Reader", source_id, "imported later a reader"),
        )
        candidate_id = con.execute("SELECT id FROM candidates WHERE title='Imported later'").fetchone()[0]
    recommendations, run_id = tracked_recommendations()
    assert candidate_id in {item["id"] for item in recommendations}
    payload = b"Title,Author,My Rating,Date Read,Exclusive Shelf\nImported later,A Reader,2,2099/01/02,read\n"
    assert import_goodreads_csv(payload) == 1
    event = row("SELECT * FROM recommendation_events WHERE event_key LIKE 'read:%' AND candidate_id=?", (candidate_id,))
    outcome = row("SELECT * FROM recommendation_outcomes WHERE event_id=?", (event["id"],))
    assert event["run_id"] == run_id
    assert outcome["label"] == 0
    assert outcome["label_kind"] == "rating_negative"


def test_day_precision_does_not_attribute_same_day_exposure(database):
    recommendations, run_id = tracked_recommendations()
    candidate_id = recommendations[0]["id"]
    today = date.today().isoformat()
    with transaction() as con:
        con.execute(
            "UPDATE recommendation_impressions SET presented_at=? WHERE run_id=? AND candidate_id=?",
            (today + "T00:00:00+00:00", run_id, candidate_id),
        )
        con.execute(
            "INSERT INTO reads(title,author,rating,read_at,source) VALUES(?,?,?,?,?)",
            (recommendations[0]["title"], recommendations[0]["author"], 5, today, "test"),
        )
    assert attribute_read_outcomes()["reads_attributed"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_events WHERE event_type='read'")["count"] == 0


def test_unrated_manual_read_keeps_verified_isbn_for_aliases_imports_and_rescoring(database):
    isbn13 = "9780593418574"
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        con.execute("UPDATE candidates SET status='rejected'")
        marked_id = con.execute(
            "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
            "VALUES(?,?,90,'recommended',?,?)",
            ("Unreasonable Hospitality", "Will Guidara", source_id, "marked hospitality"),
        ).lastrowid
        alias_id = con.execute(
            "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
            "VALUES(?,?,89,'recommended',?,?)",
            ("Hospitality: A New Approach", "Catalog Variant", source_id, "hospitality alias"),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',isbn13=?,"
            "provider='openlibrary',work_id='/works/OL123456W' "
            "WHERE candidate_id IN (?,?)",
            (isbn13, marked_id, alias_id),
        )

    initially_visible_ids = {
        item["id"] for item in recommendation_list(status="recommended", limit=None)
    }
    assert marked_id in initially_visible_ids
    assert alias_id not in initially_visible_ids
    with TestClient(app, headers=authenticated_headers()) as client:
        response = client.post(f"/api/recommendations/{marked_id}/read", json={})
    assert response.status_code == 200
    assert response.json()["rating"] is None
    assert row(
        "SELECT isbn,rating,source,openlibrary_work_id FROM reads WHERE title=? AND author=?",
        ("Unreasonable Hospitality", "Will Guidara"),
    ) == {
        "isbn": isbn13,
        "rating": None,
        "source": "manual",
        "openlibrary_work_id": "/works/OL123456W",
    }
    assert alias_id not in {
        item["id"] for item in recommendation_list(status="recommended", limit=None)
    }

    class FakeEmbedder:
        name, model = "test", "manual-read-isbn-exclusion"

        async def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    assert asyncio.run(score_all(embedder=FakeEmbedder())) == 0

    count = import_goodreads_csv(
        b"Title,Author,My Rating,Date Read,ISBN13,Exclusive Shelf\r\n"
        b"Unreasonable Hospitality,Will Guidara,0,2022-08-12,,read\r\n"
    )
    assert count == 1
    assert row(
        "SELECT isbn,rating,source FROM reads WHERE title=? AND author=?",
        ("Unreasonable Hospitality", "Will Guidara"),
    ) == {"isbn": isbn13, "rating": None, "source": "manual"}
    assert alias_id not in {
        item["id"] for item in recommendation_list(status="recommended", limit=None)
    }
    assert asyncio.run(score_all(embedder=FakeEmbedder())) == 0



def test_manual_read_keeps_existing_isbn_rating_and_read_source(database):
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        con.execute("UPDATE candidates SET status='rejected'")
        candidate_id = con.execute(
            "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
            "VALUES(?,?,90,'recommended',?,?)",
            ("Existing Work", "A Reader", source_id, "existing work marker"),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',isbn13=? "
            "WHERE candidate_id=?",
            ("9780593418574", candidate_id),
        )
        con.execute(
            "INSERT INTO reads(title,author,rating,isbn,source) "
            "VALUES('Existing Work','A Reader',3,'9780306406157','goodreads_csv')"
        )

    with TestClient(app, headers=authenticated_headers()) as client:
        response = client.post(f"/api/recommendations/{candidate_id}/read", json={})
    assert response.status_code == 200
    assert response.json()["rating"] == 3
    assert row(
        "SELECT isbn,rating,source FROM reads WHERE title='Existing Work' AND author='A Reader'"
    ) == {"isbn": "9780306406157", "rating": 3.0, "source": "goodreads_csv"}
    assert row(
        "SELECT event_type,value,source FROM recommendation_events "
        "WHERE candidate_id=? AND source='manual_read'",
        (candidate_id,),
    ) == {"event_type": "read", "value": 3.0, "source": "manual_read"}


@pytest.mark.parametrize(
    ("quality_isbn13", "quality_isbn10", "expected_isbn"),
    [
        ("", "0593418573", "9780593418574"),
        ("9780593418575", "", None),
    ],
)
def test_manual_read_canonicalizes_valid_isbn10_and_ignores_invalid_isbn(
    database, quality_isbn13, quality_isbn10, expected_isbn
):
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        con.execute("UPDATE candidates SET status='rejected'")
        candidate_id = con.execute(
            "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
            "VALUES(?,?,90,'recommended',?,?)",
            ("Identifier Check", "A Reader", source_id, "identifier check"),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',isbn13=?,isbn10=? "
            "WHERE candidate_id=?",
            (quality_isbn13, quality_isbn10, candidate_id),
        )

    with TestClient(app, headers=authenticated_headers()) as client:
        response = client.post(f"/api/recommendations/{candidate_id}/read", json={})
    assert response.status_code == 200
    assert row(
        "SELECT isbn,rating FROM reads WHERE title='Identifier Check' AND author='A Reader'"
    ) == {"isbn": expected_isbn, "rating": None}


def test_mark_read_from_shortlist_uses_verified_isbn_persistence(database):
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        con.execute("UPDATE candidates SET status='rejected'")
        candidate_id = con.execute(
            "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
            "VALUES(?,?,90,'saved',?,?)",
            ("Finished Work", "A Reader", source_id, "finished work marker"),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',isbn10=? "
            "WHERE candidate_id=?",
            ("0593418573", candidate_id),
        )

    with TestClient(app, headers=authenticated_headers()) as client:
        response = client.post(f"/api/recommendations/{candidate_id}/read", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "read"
    assert row(
        "SELECT isbn,rating,source FROM reads WHERE title='Finished Work' AND author='A Reader'"
    ) == {"isbn": "9780593418574", "rating": None, "source": "manual"}


def _expected_evidence_budget(con) -> int:
    return int(
        con.execute(
            "SELECT COALESCE(SUM(length(payload)),0) total "
            "FROM recommendation_run_evidence WHERE payload IS NOT NULL"
        ).fetchone()["total"]
        + con.execute(
            "SELECT COALESCE(SUM(length(vector)),0) total "
            "FROM recommendation_evidence_vectors"
        ).fetchone()["total"]
        + 96
        * con.execute(
            "SELECT COUNT(*) count FROM recommendation_run_evidence_vectors"
        ).fetchone()["count"]
        + con.execute(
            "SELECT COALESCE(SUM(72+length(identity_hash)),0) total "
            "FROM recommendation_run_evidence_candidates"
        ).fetchone()["total"]
        + con.execute(
            "SELECT COALESCE(SUM(160+length(CAST(candidate_snapshot_json AS BLOB))),0) total "
            "FROM recommendation_evidence_exposures"
        ).fetchone()["total"]
        + con.execute(
            "SELECT COALESCE(SUM(128+length(CAST(event_key AS BLOB)) "
            "+length(CAST(event_type AS BLOB))+length(CAST(source AS BLOB)) "
            "+length(CAST(occurred_at AS BLOB))),0) total "
            "FROM recommendation_evidence_events"
        ).fetchone()["total"]
        + con.execute(
            "SELECT COALESCE(SUM(192+length(CAST(event_key AS BLOB)) "
            "+length(CAST(label_kind AS BLOB))+length(CAST(presented_at AS BLOB)) "
            "+length(CAST(attributed_at AS BLOB)) "
            "+COALESCE(length(CAST(visible_at AS BLOB)),0)),0) total "
            "FROM recommendation_evidence_outcomes"
        ).fetchone()["total"]
    )


def test_manual_read_reconciles_across_goodreads_csv_and_rss_syncs(database, monkeypatch):
    import afterword_engine.ingestion as ingestion_module
    from afterword_engine.ingestion import import_goodreads_rss

    recommendations, run_early = tracked_recommendations()
    candidate = recommendations[0]
    _set_candidate_exposure_time(candidate["id"], run_early, "2025-05-01T10:00:00+00:00")
    _, run_late = tracked_recommendations()
    _set_candidate_exposure_time(candidate["id"], run_late, "2025-05-10T10:00:00+00:00")

    with TestClient(app, headers=authenticated_headers()) as client:
        marked = client.post(f"/api/recommendations/{candidate['id']}/read", json={})
    assert marked.status_code == 200
    read_id = marked.json()["read_id"]
    event_key = f"manual-read:{candidate['id']}:{read_id:08d}"

    initial_event = row("SELECT * FROM recommendation_events WHERE event_key=?", (event_key,))
    assert initial_event["run_id"] == run_late
    assert initial_event["value"] is None
    assert json.loads(initial_event["metadata"]) == {
        "read_source": "manual",
        "time_precision": "timestamp",
    }

    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow(["Title", "Author", "My Rating", "Date Read", "Exclusive Shelf"])
    writer.writerow([candidate["title"], candidate["author"], "5", "2025/05/05", "read"])
    csv_bytes = csv_buf.getvalue().encode("utf-8")

    assert import_goodreads_csv(csv_bytes) == 1
    assert import_goodreads_csv(csv_bytes) == 1

    reconciled = row("SELECT * FROM recommendation_events WHERE event_key=?", (event_key,))
    assert reconciled["run_id"] == run_early
    assert reconciled["value"] == 5.0
    assert json.loads(reconciled["metadata"]) == {
        "read_source": "manual",
        "time_precision": "day",
    }
    assert row("SELECT COUNT(*) count FROM recommendation_events WHERE event_type='read'")["count"] == 1
    assert row("SELECT COUNT(*) count FROM recommendation_outcomes WHERE event_id=?", (reconciled["id"],))["count"] == 1
    outcome = row("SELECT * FROM recommendation_outcomes WHERE event_id=?", (reconciled["id"],))
    assert outcome["label"] == 1.0
    assert outcome["label_kind"] == "rating_positive"
    assert row(
        "SELECT COUNT(*) count FROM recommendation_evidence_events WHERE event_key=?",
        (event_key,),
    )["count"] == 2
    assert row(
        "SELECT COUNT(*) count FROM recommendation_evidence_outcomes WHERE event_key=?",
        (event_key,),
    )["count"] == 2
    with connect() as con:
        assert load_decision_evidence(con, run_late)["outcomes"] == []
        early_outcomes = load_decision_evidence(con, run_early)["outcomes"]
        assert len(early_outcomes) == 1
        assert early_outcomes[0]["event_key"] == event_key
        assert early_outcomes[0]["event_revision"] == 2
        assert early_outcomes[0]["label"] == 1.0
        assert early_outcomes[0]["label_kind"] == "rating_positive"

    async def fake_fetch_bytes(*_args, **_kwargs):
        return b"", "application/rss+xml"

    class HistoricalFeed:
        entries = [
            {
                "title": candidate["title"],
                "author_name": candidate["author"],
                "user_rating": "4",
                "user_read_at": "2020/01/02",
            }
        ]

    monkeypatch.setattr(ingestion_module, "fetch_bytes", fake_fetch_bytes)
    monkeypatch.setattr(ingestion_module.feedparser, "parse", lambda _content: HistoricalFeed())

    assert asyncio.run(
        import_goodreads_rss("https://www.goodreads.com/review/list_rss/reader?shelf=read")
    ) == 1
    assert asyncio.run(
        import_goodreads_rss("https://www.goodreads.com/review/list_rss/reader?shelf=read")
    ) == 1

    cleared = row("SELECT * FROM recommendation_events WHERE event_key=?", (event_key,))
    assert cleared["run_id"] is None
    assert cleared["value"] == 4.0
    assert cleared["occurred_at"] == "2020/01/02"
    assert row("SELECT COUNT(*) count FROM recommendation_outcomes WHERE event_id=?", (cleared["id"],))["count"] == 0
    # Append-only evidence rows remain intact while replay filters to the latest revision.
    assert row(
        "SELECT COUNT(*) count FROM recommendation_evidence_events WHERE event_key=?",
        (event_key,),
    )["count"] == 3
    assert row(
        "SELECT COUNT(*) count FROM recommendation_evidence_outcomes WHERE event_key=?",
        (event_key,),
    )["count"] == 2
    with connect() as con:
        assert load_decision_evidence(con, run_late)["outcomes"] == []
        assert load_decision_evidence(con, run_early)["outcomes"] == []
    with transaction() as con:
        assert (
            con.execute("SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1").fetchone()["used_bytes"]
            == _expected_evidence_budget(con)
        )


def test_goodreads_import_before_manual_read_reuses_read_event_key_and_keeps_browser_events_immutable(database):
    recommendations, run_id = tracked_recommendations()
    candidate = recommendations[0]
    _set_candidate_exposure_time(candidate["id"], run_id, "2025-01-01T10:00:00+00:00")

    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow(["Title", "Author", "My Rating", "Date Read", "Exclusive Shelf"])
    writer.writerow([candidate["title"], candidate["author"], "0", "2025/01/05", "read"])
    assert import_goodreads_csv(csv_buf.getvalue().encode("utf-8")) == 1

    read_row = row("SELECT id FROM reads WHERE title=? AND author=?", (candidate["title"], candidate["author"]))
    expected_key = f"read:{int(read_row['id']):08d}"
    assert row("SELECT COUNT(*) count FROM recommendation_events WHERE event_type='read'")["count"] == 1
    assert row("SELECT event_key,value FROM recommendation_events WHERE event_type='read'") == {
        "event_key": expected_key,
        "value": None,
    }

    with TestClient(app, headers=authenticated_headers()) as client:
        marked = client.post(
            f"/api/recommendations/{candidate['id']}/read",
            json={"rating": 4},
        )
        assert marked.status_code == 200

        accepted = client.post(
            "/api/telemetry/events",
            json={
                "events": [
                    {
                        "event_key": "immutable-browser-001",
                        "candidate_id": candidate["id"],
                        "run_id": run_id,
                        "event_type": "detail_open",
                        "metadata": {"surface": "discover"},
                    }
                ]
            },
        )
        rejected_metadata = client.post(
            "/api/telemetry/events",
            json={
                "events": [
                    {
                        "event_key": "immutable-browser-001",
                        "candidate_id": candidate["id"],
                        "run_id": run_id,
                        "event_type": "detail_open",
                        "metadata": {"surface": "saved"},
                    }
                ]
            },
        )
        rejected_value = client.post(
            "/api/telemetry/events",
            json={
                "events": [
                    {
                        "event_key": "immutable-browser-001",
                        "candidate_id": candidate["id"],
                        "run_id": run_id,
                        "event_type": "detail_open",
                        "value": 1.0,
                        "metadata": {"surface": "discover"},
                    }
                ]
            },
        )

    assert accepted.status_code == 200
    assert rejected_metadata.status_code == 400
    assert rejected_value.status_code == 400
    assert row("SELECT COUNT(*) count FROM recommendation_events WHERE event_type='read'")["count"] == 1
    assert row("SELECT event_key,value FROM recommendation_events WHERE event_type='read'") == {
        "event_key": expected_key,
        "value": 4.0,
    }
    assert row(
        "SELECT label,label_kind FROM recommendation_outcomes WHERE read_id=?",
        (read_row["id"],),
    ) == {"label": 1.0, "label_kind": "rating_positive"}


def test_goodreads_csv_and_rss_roll_back_reads_when_attribution_fails(database, monkeypatch):
    import afterword_engine.ingestion as ingestion_module
    import afterword_engine.learning as learning_module
    from afterword_engine.ingestion import import_goodreads_rss

    def fail_attribution(_con=None):
        raise RuntimeError("attribution failure")

    monkeypatch.setattr(learning_module, "attribute_read_outcomes", fail_attribution)

    with pytest.raises(RuntimeError, match="attribution failure"):
        import_goodreads_csv(
            b"Title,Author,My Rating,Date Read,Exclusive Shelf\nRollback Book,Writer,5,2025/02/01,read\n"
        )
    assert row("SELECT COUNT(*) count FROM reads WHERE title='Rollback Book'")["count"] == 0

    async def fake_fetch_bytes(*_args, **_kwargs):
        return b"", "application/rss+xml"

    class RssFeed:
        entries = [
            {
                "title": "Rollback RSS Book",
                "author_name": "Writer",
                "user_rating": "5",
                "user_read_at": "2025/02/01",
            }
        ]

    monkeypatch.setattr(ingestion_module, "fetch_bytes", fake_fetch_bytes)
    monkeypatch.setattr(ingestion_module.feedparser, "parse", lambda _content: RssFeed())
    with pytest.raises(RuntimeError, match="attribution failure"):
        asyncio.run(
            import_goodreads_rss("https://www.goodreads.com/review/list_rss/reader?shelf=read")
        )
    assert row("SELECT COUNT(*) count FROM reads WHERE title='Rollback RSS Book'")["count"] == 0


def test_read_attribution_matches_canonical_variants_and_tie_breaks_candidates(database):
    from afterword_engine.identity import read_metadata_identity_hash

    with transaction() as con:
        source_id = con.execute("SELECT id FROM sources WHERE is_default=1 LIMIT 1").fetchone()[0]
        con.execute("UPDATE candidates SET status='rejected'")

        def add_cand(
            title,
            author,
            *,
            key,
            status="recommended",
            score=80.0,
            isbn13="",
            work_id="",
            provider="",
        ):
            cand_id = con.execute(
                "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
                "VALUES(?,?,?,?,?,?)",
                (title, author, score, status, source_id, key),
            ).lastrowid
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted',isbn13=?,work_id=?,provider=? "
                "WHERE candidate_id=?",
                (isbn13, work_id, provider, cand_id),
            )
            return int(cand_id)

        # 1a. Unshown exact title/author beats shown loose subtitle variant (no false-positive attribution)
        exact_unimpressed = add_cand(
            "Project Hail Mary", "Andy Weir", key="phm-exact", score=60.0
        )
        loose_subtitle_impressed = add_cand(
            "Project Hail Mary: A Novel", "Andy Weir", key="phm-subtitle", score=99.0
        )
        # 1b. Unshown exact ISBN beats shown loose work-ID sibling
        isbn_exact_unimpressed = add_cand(
            "Unshown Exact Edition",
            "Edition Author",
            key="isbn-unshown-exact",
            isbn13="9780140449136",
            work_id="/works/OL1111111W",
            provider="openlibrary",
        )
        loose_work_impressed = add_cand(
            "Shown Sibling Edition",
            "Different Translator",
            key="work-shown-loose",
            work_id="/works/OL1111111W",
            provider="openlibrary",
        )
        # 1c. Subtitle variant matches when no exact-identity candidate exists
        subtitle_only_cand = add_cand(
            "The Dispossessed: An Ambiguous Utopia",
            "Ursula K. Le Guin",
            key="dispossessed-subtitle",
            score=82.0,
        )
        # 2. Leading article variant
        article_cand = add_cand(
            "The Left Hand of Darkness", "Ursula K. Le Guin", key="lhod-article"
        )
        # 3. Numeric alias variant
        numeric_cand = add_cand(
            "Slaughterhouse-Five", "Kurt Vonnegut", key="sh5-alias"
        )
        # 4. ISBN variant with distinct catalog title + active/score tie-breaking
        isbn_rejected = add_cand(
            "Edition Rejected",
            "Different Credit",
            key="isbn-rejected",
            status="rejected",
            score=98.0,
            isbn13="9780593418574",
        )
        isbn_lower = add_cand(
            "Edition Active Low",
            "Different Credit",
            key="isbn-low",
            status="recommended",
            score=75.0,
            isbn13="9780593418574",
        )
        isbn_higher = add_cand(
            "Edition Active High",
            "Different Credit",
            key="isbn-high",
            status="recommended",
            score=88.0,
            isbn13="9780593418574",
        )
        # 5. Open Library work variant via projected read_metadata
        work_cand = add_cand(
            "Translated Catalog Title",
            "Catalog Translator",
            key="ol-work-cand",
            work_id="/works/OL7654321W",
            provider="openlibrary",
        )

    impressed_ids = [
        loose_subtitle_impressed,
        loose_work_impressed,
        subtitle_only_cand,
        article_cand,
        numeric_cand,
        isbn_rejected,
        isbn_lower,
        isbn_higher,
        work_cand,
    ]
    run_id = create_recommendation_run(
        [{"id": cid, "score": 80} for cid in impressed_ids]
    )
    for cid in impressed_ids:
        _set_candidate_exposure_time(cid, run_id, "2025-03-01T12:00:00+00:00")
    assert exact_unimpressed not in impressed_ids
    assert isbn_exact_unimpressed not in impressed_ids

    with transaction() as con:
        con.executemany(
            "INSERT INTO reads(title,author,rating,read_at,isbn,source) VALUES(?,?,?,?,?,?)",
            [
                ("Project Hail Mary", "Andy Weir", 5, "2025-03-05", None, "goodreads_csv"),
                ("Unrelated Reader Title", "Other Credit", 5, "2025-03-05", "9780140449136", "goodreads_csv"),
                ("The Dispossessed", "Ursula K. Le Guin", 5, "2025-03-05", None, "goodreads_csv"),
                ("Left Hand of Darkness", "Ursula K. Le Guin", 5, "2025-03-05", None, "goodreads_csv"),
                ("Slaughterhouse 5", "Kurt Vonnegut Jr.", 4, "2025-03-05", None, "goodreads_csv"),
                ("Imported ISBN Edition", "Reader Credit", 5, "2025-03-05", "0593418573", "goodreads_csv"),
            ],
        )
        work_read_id = con.execute(
            "INSERT INTO reads(title,author,rating,read_at,isbn,source) VALUES(?,?,?,?,?,?)",
            ("Original Edition Title", "Original Author", 5, "2025-03-05", "9780306406157", "goodreads_csv"),
        ).lastrowid
        work_read = dict(
            con.execute("SELECT * FROM reads WHERE id=?", (work_read_id,)).fetchone()
        )
        con.execute(
            "INSERT INTO read_metadata(read_id,verified_work_id,identity_provider,identity_provider_id,identity_hash) "
            "VALUES(?,?,?,?,?)",
            (
                work_read_id,
                "/works/OL7654321W",
                "openlibrary",
                "/works/OL7654321W",
                read_metadata_identity_hash(work_read),
            ),
        )

    summary = attribute_read_outcomes()
    assert summary["outcomes_created"] == 5
    attributed_candidates = {
        int(item["candidate_id"])
        for item in [
            dict(r)
            for r in __import__("afterword_engine.database", fromlist=["rows"]).rows(
                "SELECT candidate_id FROM recommendation_events WHERE event_type='read'"
            )
        ]
    }
    assert attributed_candidates == {
        subtitle_only_cand,
        article_cand,
        numeric_cand,
        isbn_higher,
        work_cand,
    }
    assert loose_subtitle_impressed not in attributed_candidates
    assert loose_work_impressed not in attributed_candidates


def test_three_part_read_event_key_cleans_up_on_rematch_and_pre_exposure_date(database):
    with transaction() as con:
        source_id = con.execute("SELECT id FROM sources WHERE is_default=1 LIMIT 1").fetchone()[0]
        cand_a = int(
            con.execute(
                "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
                "VALUES('Candidate Alpha','Writer One',85,'recommended',?,'cand-alpha')",
                (source_id,),
            ).lastrowid
        )
        cand_b = int(
            con.execute(
                "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
                "VALUES('Candidate Beta','Writer Two',85,'recommended',?,'cand-beta')",
                (source_id,),
            ).lastrowid
        )
        for cid in (cand_a, cand_b):
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted' WHERE candidate_id=?",
                (cid,),
            )

    with connect() as con:
        evidence = build_decision_evidence(
            con,
            base_pool=[],
            personalized_pool=[],
            final_pool=[],
            interaction_events=[],
            cached_vectors={},
            ranking_metadata={},
            discovery_slate_metadata={},
            request_context={},
        )
    run_id = create_recommendation_run(
        [{"id": cand_a, "score": 85}, {"id": cand_b, "score": 85}],
        decision_evidence=evidence,
    )
    _set_candidate_exposure_time(cand_a, run_id, "2025-03-01T10:00:00+00:00")
    _set_candidate_exposure_time(cand_b, run_id, "2025-03-01T10:00:00+00:00")

    with transaction() as con:
        read_id = int(
            con.execute(
                "INSERT INTO reads(title,author,rating,read_at,source) "
                "VALUES('Candidate Alpha','Writer One',5,'2025-03-05','goodreads_csv')"
            ).lastrowid
        )
    assert attribute_read_outcomes()["outcomes_created"] == 1
    default_key = f"read:{read_id:08d}"
    three_part_key = f"read:{read_id:08d}:{cand_b}"

    # Switch read to Candidate B -> creates read:NNN:CAND_B and clears Candidate A.
    with transaction() as con:
        con.execute(
            "UPDATE reads SET title='Candidate Beta',author='Writer Two' WHERE id=?",
            (read_id,),
        )
    attribute_read_outcomes()
    assert row("SELECT run_id FROM recommendation_events WHERE event_key=?", (default_key,))["run_id"] is None
    assert row("SELECT run_id FROM recommendation_events WHERE event_key=?", (three_part_key,))["run_id"] == run_id
    assert row("SELECT COUNT(*) count FROM recommendation_outcomes WHERE read_id=?", (read_id,))["count"] == 1

    # Switch back to Candidate A -> 3-part key must be parsed and cleaned up (no double counting).
    with transaction() as con:
        con.execute(
            "UPDATE reads SET title='Candidate Alpha',author='Writer One' WHERE id=?",
            (read_id,),
        )
    attribute_read_outcomes()
    assert row("SELECT run_id FROM recommendation_events WHERE event_key=?", (three_part_key,))["run_id"] is None
    assert row("SELECT run_id FROM recommendation_events WHERE event_key=?", (default_key,))["run_id"] == run_id
    active_outcomes = __import__("afterword_engine.database", fromlist=["rows"]).rows(
        "SELECT i.candidate_id FROM recommendation_outcomes o "
        "JOIN recommendation_impressions i ON i.id=o.impression_id WHERE o.read_id=?",
        (read_id,),
    )
    assert [int(item["candidate_id"]) for item in active_outcomes] == [cand_a]
    with connect() as con:
        replayed = load_decision_evidence(con, run_id)["outcomes"]
        assert [int(item["candidate_id"]) for item in replayed] == [cand_a]

    # Re-match Candidate B then move read_at before all exposures -> 3-part key is cleared too.
    with transaction() as con:
        con.execute(
            "UPDATE reads SET title='Candidate Beta',author='Writer Two' WHERE id=?",
            (read_id,),
        )
    attribute_read_outcomes()
    with transaction() as con:
        con.execute("UPDATE reads SET read_at='2020-01-02' WHERE id=?", (read_id,))
    attribute_read_outcomes()
    assert row("SELECT run_id FROM recommendation_events WHERE event_key=?", (three_part_key,))["run_id"] is None
    assert row("SELECT COUNT(*) count FROM recommendation_outcomes WHERE read_id=?", (read_id,))["count"] == 0
    with connect() as con:
        assert load_decision_evidence(con, run_id)["outcomes"] == []


def test_undated_or_unmatched_reads_and_unlabelled_duplicate_reads_preserve_outcomes(database):
    recommendations, run_id = tracked_recommendations()
    candidate = recommendations[0]
    _set_candidate_exposure_time(candidate["id"], run_id, "2025-05-01T10:00:00+00:00")

    with transaction() as con:
        read_id = int(
            con.execute(
                "INSERT INTO reads(title,author,rating,read_at,source) VALUES(?,?,?,?,?)",
                (candidate["title"], candidate["author"], 5, "2025-05-05", "goodreads_csv"),
            ).lastrowid
        )
    assert attribute_read_outcomes()["outcomes_created"] == 1
    event_key = f"read:{read_id:08d}"
    event = row("SELECT id,run_id FROM recommendation_events WHERE event_key=?", (event_key,))
    assert event["run_id"] == run_id

    # Dropping read_at on a historical source must NOT wipe earlier attributed outcomes.
    with transaction() as con:
        con.execute("UPDATE reads SET read_at=NULL WHERE id=?", (read_id,))
    attribute_read_outcomes()
    assert row("SELECT run_id FROM recommendation_events WHERE event_key=?", (event_key,))["run_id"] == run_id
    assert row("SELECT COUNT(*) count FROM recommendation_outcomes WHERE event_id=?", (event["id"],))["count"] == 1
    with connect() as con:
        assert len(load_decision_evidence(con, run_id)["outcomes"]) == 1

    # Becoming unmatched in the current catalog must also preserve earlier attributed outcomes.
    with transaction() as con:
        con.execute(
            "UPDATE reads SET title='Unmatched Export Title',read_at='2025-05-05' WHERE id=?",
            (read_id,),
        )
    attribute_read_outcomes()
    assert row("SELECT run_id FROM recommendation_events WHERE event_key=?", (event_key,))["run_id"] == run_id
    assert row("SELECT COUNT(*) count FROM recommendation_outcomes WHERE event_id=?", (event["id"],))["count"] == 1

    # Recording a duplicate read without a label must preserve all existing outcomes regardless of label_kind.
    with transaction() as con:
        other_imp_id = int(
            con.execute(
                "SELECT id FROM recommendation_impressions WHERE run_id=? AND candidate_id=?",
                (run_id, recommendations[1]["id"]),
            ).fetchone()["id"]
        )
        con.execute(
            "INSERT INTO recommendation_outcomes(impression_id,event_id,label,label_kind,confidence,attributed_at) "
            "VALUES(?,?,1.0,'explicit_feedback',0.8,'2025-05-06T00:00:00+00:00')",
            (other_imp_id, event["id"]),
        )
        dup_result = record_event_in_connection(
            con,
            event_key=event_key,
            candidate_id=candidate["id"],
            event_type="read",
            value=5.0,
            source="read_import",
            metadata={"read_source": "goodreads_csv", "time_precision": "day"},
        )
        assert dup_result == {"id": event["id"], "duplicate": True, "outcome": "none"}
        assert (
            con.execute(
                "SELECT COUNT(*) count FROM recommendation_outcomes WHERE event_id=?",
                (event["id"],),
            ).fetchone()["count"]
            == 2
        )

        # Even explicit clear_read_outcome=True only removes READ_OUTCOME_LABEL_KINDS.
        record_event_in_connection(
            con,
            event_key=event_key,
            candidate_id=candidate["id"],
            event_type="read",
            run_id=None,
            value=5.0,
            source="read_import",
            metadata={"read_source": "goodreads_csv", "time_precision": "day"},
            clear_read_outcome=True,
        )
        remaining = [
            dict(r)
            for r in con.execute(
                "SELECT label_kind FROM recommendation_outcomes WHERE event_id=?",
                (event["id"],),
            ).fetchall()
        ]
        assert remaining == [{"label_kind": "explicit_feedback"}]


def test_retracted_explicit_outcomes_are_deleted_and_unmask_read_outcomes(database):
    from afterword_engine.evaluation import load_rows

    recommendations, run_id = tracked_recommendations()
    first_cand = recommendations[0]
    second_cand = recommendations[1]
    _set_candidate_exposure_time(first_cand["id"], run_id, "2025-04-01T10:00:00+00:00")
    _set_candidate_exposure_time(second_cand["id"], run_id, "2025-04-01T10:00:00+00:00")

    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,rating,read_at,source) VALUES(?,?,?,?,?)",
            (first_cand["title"], first_cand["author"], None, "2025-04-05", "goodreads_csv"),
        )
    assert attribute_read_outcomes()["outcomes_created"] == 1

    with TestClient(app, headers=authenticated_headers()) as client:
        rejected = client.post(
            f"/api/recommendations/{first_cand['id']}/feedback",
            json={"action": "reject", "run_id": run_id},
        )
        assert rejected.status_code == 200
        by_cand = {item["candidate_id"]: item for item in load_rows(database) if item["run_id"] == run_id}
        assert by_cand[first_cand["id"]]["label_kind"] == "explicit_feedback"
        assert by_cand[first_cand["id"]]["label"] == 0.0

        deferred = client.post(
            f"/api/recommendations/{first_cand['id']}/feedback",
            json={"action": "maybe_later", "run_id": run_id},
        )
        assert deferred.status_code == 200
        by_cand = {item["candidate_id"]: item for item in load_rows(database) if item["run_id"] == run_id}
        assert by_cand[first_cand["id"]]["label_kind"] == "completed_unrated"
        assert by_cand[first_cand["id"]]["label"] == 0.75
        # Evidence outcomes stay append-only; replay hides retracted explicit feedback.
        assert row(
            "SELECT COUNT(*) count FROM recommendation_evidence_outcomes "
            "WHERE candidate_id=? AND label_kind='explicit_feedback'",
            (first_cand["id"],),
        )["count"] == 1
        with connect() as con:
            assert [
                item["label_kind"]
                for item in load_decision_evidence(con, run_id)["outcomes"]
                if item["candidate_id"] == first_cand["id"]
            ] == ["completed_unrated"]

        saved = client.post(
            f"/api/recommendations/{first_cand['id']}/feedback",
            json={"action": "save", "run_id": run_id},
        ).json()
        assert row(
            "SELECT COUNT(*) count FROM recommendation_evidence_outcomes "
            "WHERE candidate_id=? AND label_kind='explicit_feedback'",
            (first_cand["id"],),
        )["count"] == 2
        undone = client.post(
            f"/api/recommendations/{first_cand['id']}/undo",
            json={"decision_id": saved["decision_id"]},
        )
        assert undone.status_code == 200
        assert row(
            "SELECT COUNT(*) count FROM recommendation_evidence_outcomes "
            "WHERE candidate_id=? AND label_kind='explicit_feedback'",
            (first_cand["id"],),
        )["count"] == 2
        with connect() as con:
            assert [
                item["label_kind"]
                for item in load_decision_evidence(con, run_id)["outcomes"]
                if item["candidate_id"] == first_cand["id"]
            ] == ["completed_unrated"]

        client.post(
            f"/api/recommendations/{second_cand['id']}/feedback",
            json={"action": "save", "run_id": run_id},
        )
        restored = client.post(
            f"/api/recommendations/{second_cand['id']}/feedback",
            json={"action": "restore", "run_id": run_id},
        )
        assert restored.status_code == 200
        assert row(
            "SELECT COUNT(*) count FROM recommendation_outcomes "
            "WHERE label_kind='explicit_feedback' AND event_id IN "
            "(SELECT id FROM recommendation_events WHERE candidate_id=?)",
            (second_cand["id"],),
        )["count"] == 0
        assert row(
            "SELECT COUNT(*) count FROM recommendation_evidence_outcomes "
            "WHERE candidate_id=? AND label_kind='explicit_feedback'",
            (second_cand["id"],),
        )["count"] == 1
        with connect() as con:
            assert [
                item
                for item in load_decision_evidence(con, run_id)["outcomes"]
                if item["candidate_id"] == second_cand["id"]
            ] == []

    with transaction() as con:
        assert (
            con.execute("SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1").fetchone()["used_bytes"]
            == _expected_evidence_budget(con)
        )


def test_bulk_feedback_records_previous_status_and_links_matching_run_impressions(database):
    with transaction() as con:
        source_id = con.execute("SELECT id FROM sources WHERE is_default=1 LIMIT 1").fetchone()[0]
        first_id = con.execute(
            "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
            "VALUES('Bulk Run One','Author One',91,'recommended',?,'bulk run one')",
            (source_id,),
        ).lastrowid
        second_id = con.execute(
            "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
            "VALUES('Bulk Run Two','Author Two',89,'recommended',?,'bulk run two')",
            (source_id,),
        ).lastrowid

    run_one = create_recommendation_run([{"id": first_id, "score": 91}])
    run_two = create_recommendation_run([{"id": second_id, "score": 89}])
    assert run_two != run_one

    with TestClient(app, headers=authenticated_headers()) as client:
        saved = client.post(
            "/api/recommendations/bulk-feedback",
            json={"ids": [first_id, second_id], "action": "save", "run_id": run_one},
        )
        assert saved.status_code == 200
        assert saved.json()["updated"] == 2

        first_feedback = row(
            "SELECT id,previous_status FROM feedback WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
            (first_id,),
        )
        second_feedback = row(
            "SELECT id,previous_status FROM feedback WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
            (second_id,),
        )
        assert first_feedback["previous_status"] == "recommended"
        assert second_feedback["previous_status"] == "recommended"

        first_event = row("SELECT id,run_id FROM recommendation_events WHERE event_key=?", (f"feedback:{first_feedback['id']}",))
        second_event = row("SELECT id,run_id FROM recommendation_events WHERE event_key=?", (f"feedback:{second_feedback['id']}",))
        assert first_event["run_id"] == run_one
        assert second_event["run_id"] is None

        assert row("SELECT confidence FROM recommendation_outcomes WHERE event_id=?", (first_event["id"],))["confidence"] == 0.8
        assert row("SELECT confidence FROM recommendation_outcomes WHERE event_id=?", (second_event["id"],))["confidence"] == 0.5
        assert row(
            "SELECT confidence FROM recommendation_evidence_outcomes WHERE run_id=? AND candidate_id=?",
            (run_one, first_id),
        )["confidence"] == 0.8

        restored = client.post(
            "/api/recommendations/bulk-feedback",
            json={"ids": [first_id], "action": "restore", "run_id": run_one},
        )
        assert restored.status_code == 200
        assert row(
            "SELECT COUNT(*) count FROM recommendation_outcomes WHERE event_id=?",
            (first_event["id"],),
        )["count"] == 0
        assert row(
            "SELECT COUNT(*) count FROM recommendation_evidence_outcomes WHERE candidate_id=?",
            (first_id,),
        )["count"] == 1


@pytest.mark.parametrize("read_at", [None, "not-a-date"], ids=["missing-date", "invalid-date"])
def test_storygraph_undated_imports_do_not_use_import_time_as_completion_time(database, read_at):
    recommendations, run_id = tracked_recommendations()
    candidate = recommendations[0]
    _set_candidate_exposure_time(candidate["id"], run_id, "2025-01-01T12:00:00+00:00")
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,rating,read_at,source,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                candidate["title"],
                candidate["author"],
                4.5,
                read_at,
                "storygraph_csv",
                "2099-01-02T12:00:00+00:00",
            ),
        )

    assert attribute_read_outcomes()["reads_attributed"] == 0
    assert row(
        "SELECT COUNT(*) count FROM recommendation_events "
        "WHERE event_type='read' AND candidate_id=?",
        (candidate["id"],),
    )["count"] == 0


def test_storygraph_csv_import_attributes_dated_fractional_rating_outcomes(database):
    from afterword_engine.ingestion import import_csv

    with transaction() as con:
        source_id = con.execute("SELECT id FROM sources WHERE is_default=1 LIMIT 1").fetchone()[0]
        con.execute(
            "INSERT INTO candidates(title,author,source_id,normalized_key) VALUES(?,?,?,?)",
            ("StoryGraph Pick", "Primary Writer", source_id, "storygraph pick primary writer"),
        )
        candidate_id = con.execute(
            "SELECT id FROM candidates WHERE title='StoryGraph Pick'"
        ).fetchone()[0]
    recommendations, run_id = tracked_recommendations()
    assert candidate_id in {item["id"] for item in recommendations}

    payload = (
        "Title,Authors,Read Status,Last Date Read,Dates Read,Star Rating\n"
        '"StoryGraph Pick","Primary Writer, Coauthor",read,,2099/01/02-2099/01/10,4.25\n'
    ).encode("utf-8")
    assert import_csv(payload) == {"imported": 1, "source": "storygraph_csv"}

    event = row(
        "SELECT * FROM recommendation_events WHERE event_key LIKE 'read:%' AND candidate_id=?",
        (candidate_id,),
    )
    outcome = row("SELECT * FROM recommendation_outcomes WHERE event_id=?", (event["id"],))
    assert event["run_id"] == run_id
    assert event["value"] == 4.25
    assert event["occurred_at"] == "2099/01/10"
    assert outcome["label"] == 1.0
    assert outcome["label_kind"] == "rating_positive"

