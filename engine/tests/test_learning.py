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
from afterword_engine.database import connect, initialize, row, rows, transaction
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
            "INSERT INTO candidates(title,author,status,source_id,normalized_key) VALUES(?,?,'recommended',?,?)",
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


def test_overview_logs_at_most_24_recommended_and_excludes_non_discovery_from_exploration(
    database,
):
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        con.execute("DELETE FROM candidates")
        for index in range(30):
            cid = con.execute(
                "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
                "VALUES(?,?,?,'recommended',?,?)",
                (
                    f"Discovery Book {index + 1}",
                    f"Author {index + 1}",
                    round(99.0 - index * 0.5, 2),
                    source_id,
                    f"discovery-book-{index + 1}",
                ),
            ).lastrowid
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted' WHERE candidate_id=?",
                (cid,),
            )
        for status, count in (("saved", 3), ("imported", 2), ("new", 2)):
            for idx in range(count):
                cid = con.execute(
                    "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        f"{status.title()} Book {idx + 1}",
                        f"{status.title()} Author {idx + 1}",
                        75.0 - idx,
                        status,
                        source_id,
                        f"{status}-book-{idx + 1}",
                    ),
                ).lastrowid
                con.execute(
                    "UPDATE candidate_quality SET quality_status='accepted' WHERE candidate_id=?",
                    (cid,),
                )

    prev_enabled = settings.exploration_enabled
    prev_epsilon = settings.exploration_epsilon
    prev_top_k = settings.exploration_stable_top_k
    settings.exploration_enabled = True
    settings.exploration_epsilon = 0.25
    settings.exploration_stable_top_k = 8
    try:
        with TestClient(app, headers=authenticated_headers()) as client:
            response = client.get("/api/overview?recommendation_limit=24")
        assert response.status_code == 200
        overview = response.json()
        assert overview["recommendations_has_more"] is True
        returned_statuses = [item["status"] for item in overview["recommendations"]]
        assert returned_statuses.count("recommended") == 24
        assert "saved" in returned_statuses
        assert "imported" in returned_statuses
        assert "new" in returned_statuses

        run_id = overview["recommendation_run_id"]
        assert run_id
        with connect() as con:
            logged = [
                dict(item)
                for item in con.execute(
                    "SELECT i.rank,i.propensity,c.status "
                    "FROM recommendation_impressions i "
                    "JOIN candidates c ON c.id=i.candidate_id "
                    "WHERE i.run_id=? ORDER BY i.rank",
                    (run_id,),
                ).fetchall()
            ]
        assert len(logged) == 24
        assert [item["rank"] for item in logged] == list(range(1, 25))
        assert all(item["status"] == "recommended" for item in logged)
        # Full discovery pool has 30 recommended books; stable_top_k=8 leaves tail_size=22.
        # Appended saved/imported/new items (7 rows) must not inflate tail_size to 29,
        # and post-slice truncation (24 - 8 = 16) must not shrink tail_size to 16.
        expected_in_place = (1.0 - 0.25) + (0.25 / 22.0)
        expected_displaced = 0.25 / 22.0
        assert all(item["propensity"] == pytest.approx(1.0) for item in logged[:8])
        assert all(
            item["propensity"]
            in (pytest.approx(expected_in_place), pytest.approx(expected_displaced))
            for item in logged[8:]
        )
    finally:
        settings.exploration_enabled = prev_enabled
        settings.exploration_epsilon = prev_epsilon
        settings.exploration_stable_top_k = prev_top_k


def test_paginated_discovery_logs_global_ranks_and_has_more_header(database):
    from afterword_engine.decision_evidence import load_decision_evidence

    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        con.execute("DELETE FROM candidates")
        for index in range(35):
            cid = con.execute(
                "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
                "VALUES(?,?,?,'recommended',?,?)",
                (
                    f"Paged Book {index + 1}",
                    f"Paged Author {index + 1}",
                    round(99.0 - index * 0.5, 2),
                    source_id,
                    f"paged-book-{index + 1}",
                ),
            ).lastrowid
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted' WHERE candidate_id=?",
                (cid,),
            )
        saved_id = con.execute(
            "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
            "VALUES('Shortlisted Extra','Reader',88.0,'saved',?,?)",
            (source_id, "shortlisted-extra"),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted' WHERE candidate_id=?",
            (saved_id,),
        )

    prev_enabled = settings.exploration_enabled
    prev_epsilon = settings.exploration_epsilon
    prev_top_k = settings.exploration_stable_top_k
    settings.exploration_enabled = True
    settings.exploration_epsilon = 0.20
    settings.exploration_stable_top_k = 8
    try:
        with TestClient(app, headers=authenticated_headers()) as client:
            page = client.get(
                "/api/recommendations?status=recommended&limit=8&offset=24"
            )
            assert page.status_code == 200
            assert page.headers.get("X-Bookward-Has-More") == "true"
            run_id = page.headers.get("X-Bookward-Recommendation-Run")
            assert run_id
            items = page.json()
            assert len(items) == 8
            assert all(item["recommendation_run_id"] == run_id for item in items)

            tail_page = client.get(
                "/api/recommendations?status=recommended&limit=8&offset=32"
            )
            assert tail_page.status_code == 200
            assert tail_page.headers.get("X-Bookward-Has-More") == "false"
            tail_run_id = tail_page.headers.get("X-Bookward-Recommendation-Run")
            assert tail_run_id
            assert len(tail_page.json()) == 3

        expected_in_place = (1.0 - 0.20) + (0.20 / (35 - 8))
        expected_displaced = 0.20 / (35 - 8)
        with connect() as con:
            impressions = [
                dict(item)
                for item in con.execute(
                    "SELECT rank,propensity FROM recommendation_impressions "
                    "WHERE run_id=? ORDER BY rank",
                    (run_id,),
                ).fetchall()
            ]
            evidence = load_decision_evidence(con, run_id)
            tail_ranks = [
                item["rank"]
                for item in con.execute(
                    "SELECT rank FROM recommendation_impressions "
                    "WHERE run_id=? ORDER BY rank",
                    (tail_run_id,),
                ).fetchall()
            ]
        assert [item["rank"] for item in impressions] == list(range(25, 33))
        assert all(
            item["propensity"]
            in (pytest.approx(expected_in_place), pytest.approx(expected_displaced))
            for item in impressions
        )
        assert [item["rank"] for item in evidence["served_exposures"]] == list(
            range(25, 33)
        )
        assert tail_ranks == [33, 34, 35]
    finally:
        settings.exploration_enabled = prev_enabled
        settings.exploration_epsilon = prev_epsilon
        settings.exploration_stable_top_k = prev_top_k


def test_non_discovery_queries_and_settings_endpoints_insert_zero_runs(database):
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        saved_id = con.execute(
            "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
            "VALUES('Saved Item','Author S',85,'saved',?,?)",
            (source_id, "saved-item-author-s"),
        ).lastrowid
        rejected_id = con.execute(
            "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
            "VALUES('Rejected Item','Author R',60,'rejected',?,?)",
            (source_id, "rejected-item-author-r"),
        ).lastrowid
        con.execute(
            "INSERT INTO feedback(candidate_id,action) VALUES(?,'reject')",
            (rejected_id,),
        )
        later_id = con.execute(
            "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
            "VALUES('Later Item','Author L',70,'maybe_later',?,?)",
            (source_id, "later-item-author-l"),
        ).lastrowid
        for cid in (saved_id, rejected_id, later_id):
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted' WHERE candidate_id=?",
                (cid,),
            )

    initial_runs = row("SELECT COUNT(*) count FROM recommendation_runs")["count"]
    with TestClient(app, headers=authenticated_headers()) as client:
        for status in ("saved", "decisions", "rejected", "maybe_later"):
            res = client.get(f"/api/recommendations?status={status}&limit=8&offset=0")
            assert res.status_code == 200
            assert res.headers.get("X-Bookward-Has-More") == "false"
            assert res.headers.get("X-Bookward-Recommendation-Run") is None
            assert all(item["recommendation_run_id"] == "" for item in res.json())

        settings_res = client.get("/api/settings")
        assert settings_res.status_code == 200
        assert "embedding_backend" in settings_res.json()

        sources_res = client.get("/api/sources")
        assert sources_res.status_code == 200
        default_source = next(item for item in sources_res.json() if item["is_default"])
        toggle_res = client.put(f"/api/sources/{default_source['id']}/toggle", json={})
        assert toggle_res.status_code == 200

        put_settings = client.put(
            "/api/settings", json={"librarr_media_type": "ebook"}
        )
        assert put_settings.status_code == 200

        untracked_overview = client.get(
            "/api/overview?recommendation_limit=24&track_impressions=false"
        )
        assert untracked_overview.status_code == 200
        assert untracked_overview.json()["recommendation_run_id"] == ""

    final_runs = row("SELECT COUNT(*) count FROM recommendation_runs")["count"]
    assert final_runs == initial_runs


def test_shortlist_read_attribution_credits_discovery_exposure_over_non_discovery_runs(
    database,
):
    recommendations, discovery_run_id = tracked_recommendations(status="recommended")
    manual_book = recommendations[0]
    imported_book = recommendations[1]

    with TestClient(app, headers=authenticated_headers()) as client:
        for book in (manual_book, imported_book):
            save_res = client.post(
                f"/api/recommendations/{book['id']}/feedback",
                json={"action": "save", "run_id": discovery_run_id},
            )
            assert save_res.status_code == 200

        # Querying shortlist must create zero runs.
        shortlist_res = client.get("/api/recommendations?status=saved&limit=8&offset=0")
        assert shortlist_res.status_code == 200
        assert shortlist_res.headers.get("X-Bookward-Recommendation-Run") is None

        # Simulate a legacy shortlist run with newer presented_at and visible_at timestamps.
        with transaction() as con:
            con.execute(
                "INSERT INTO recommendation_runs(id,policy,policy_version,session_id,candidate_count,metadata,created_at) "
                "VALUES('legacy-saved-run','legacy','v0','',2,'{\"status\":\"saved\"}','2098-06-01T12:00:00+00:00')"
            )
            for idx, book in enumerate((manual_book, imported_book), start=1):
                con.execute(
                    "INSERT INTO recommendation_impressions(run_id,candidate_id,rank,score,propensity,presented_at,visible_at) "
                    "VALUES('legacy-saved-run',?,?,80.0,1.0,'2098-06-01T12:00:00+00:00','2098-06-01T12:05:00+00:00')",
                    (book["id"], idx),
                )
            # Ensure discovery exposures precede the manual read timestamp and legacy run.
            con.execute(
                "UPDATE recommendation_impressions SET presented_at='2025-01-01T00:00:00+00:00', "
                "visible_at='2025-01-01T00:01:00+00:00' WHERE run_id=?",
                (discovery_run_id,),
            )

        read_res = client.post(
            f"/api/recommendations/{manual_book['id']}/read", json={"rating": 5}
        )
        assert read_res.status_code == 200

    manual_outcome = row(
        "SELECT i.run_id,i.rank,o.label,o.label_kind "
        "FROM recommendation_outcomes o "
        "JOIN recommendation_impressions i ON i.id=o.impression_id "
        "JOIN recommendation_events e ON e.id=o.event_id "
        "WHERE e.candidate_id=? AND e.source='manual_read'",
        (manual_book["id"],),
    )
    assert manual_outcome == {
        "run_id": discovery_run_id,
        "rank": 1,
        "label": 1.0,
        "label_kind": "rating_positive",
    }

    csv_payload = (
        "Title,Author,My Rating,Date Read,Exclusive Shelf\n"
        f"{imported_book['title']},{imported_book['author']},5,2099/01/02,read\n"
    ).encode("utf-8")
    assert import_goodreads_csv(csv_payload) == 1
    imported_outcome = row(
        "SELECT i.run_id,i.rank,o.label,o.label_kind "
        "FROM recommendation_outcomes o "
        "JOIN recommendation_impressions i ON i.id=o.impression_id "
        "JOIN recommendation_events e ON e.id=o.event_id "
        "WHERE e.candidate_id=? AND e.source='read_import'",
        (imported_book["id"],),
    )
    assert imported_outcome == {
        "run_id": discovery_run_id,
        "rank": 2,
        "label": 1.0,
        "label_kind": "rating_positive",
    }


def test_paginated_discovery_forms_stable_exploration_partition_with_exact_propensities(
    database, monkeypatch
):
    import afterword_engine.main as main_module

    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        con.execute("DELETE FROM candidates")
        for index in range(35):
            cid = con.execute(
                "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
                "VALUES(?,?,?,'recommended',?,?)",
                (
                    f"Partition Book {index + 1}",
                    f"Partition Author {index + 1}",
                    round(99.0 - index * 0.5, 2),
                    source_id,
                    f"partition-book-{index + 1}",
                ),
            ).lastrowid
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted' WHERE candidate_id=?",
                (cid,),
            )

    monkeypatch.setattr(settings, "exploration_enabled", True)
    monkeypatch.setattr(settings, "exploration_epsilon", 1.0)
    monkeypatch.setattr(settings, "exploration_stable_top_k", 8)

    with TestClient(app, headers=authenticated_headers()) as client:
        overview_res = client.get("/api/overview?recommendation_limit=24")
        assert overview_res.status_code == 200
        overview_body = overview_res.json()
        run_0 = overview_body["recommendation_run_id"]
        assert run_0
        page_0_ids = [
            item["id"]
            for item in overview_body["recommendations"]
            if item["status"] == "recommended"
        ]
        assert len(page_0_ids) == 24

        page_1_res = client.get(
            f"/api/recommendations?status=recommended&limit=8&offset=24&seed_run_id={run_0}"
        )
        assert page_1_res.status_code == 200
        assert page_1_res.headers.get("X-Bookward-Has-More") == "true"
        run_24 = page_1_res.headers.get("X-Bookward-Recommendation-Run")
        assert run_24 and run_24 != run_0
        page_1_ids = [item["id"] for item in page_1_res.json()]
        assert len(page_1_ids) == 8

        # Chaining seed_run_id from page 2's run resolves back to the root exploration seed.
        page_2_res = client.get(
            f"/api/recommendations?status=recommended&limit=8&offset=32&seed_run_id={run_24}"
        )
        assert page_2_res.status_code == 200
        assert page_2_res.headers.get("X-Bookward-Has-More") == "false"
        page_2_ids = [item["id"] for item in page_2_res.json()]
        assert len(page_2_ids) == 3

        combined = page_0_ids + page_1_ids + page_2_ids
        assert len(combined) == 35
        assert len(set(combined)) == 35

        # Window-based fallback without seed_run_id also reuses the recent discovery seed.
        window_p1 = client.get(
            "/api/recommendations?status=recommended&limit=8&offset=24"
        ).json()
        window_p2 = client.get(
            "/api/recommendations?status=recommended&limit=8&offset=32"
        ).json()
        assert [item["id"] for item in window_p1] == page_1_ids
        assert [item["id"] for item in window_p2] == page_2_ids

    # Exact marginal propensities at epsilon=1.0: top 8 = 1.0, all 27 tail slots = 1/27.
    expected_tail_propensity = 1.0 / (35 - 8)
    with connect() as con:
        p0_imps = con.execute(
            "SELECT rank,propensity FROM recommendation_impressions WHERE run_id=? ORDER BY rank",
            (run_0,),
        ).fetchall()
        p1_imps = con.execute(
            "SELECT rank,propensity FROM recommendation_impressions WHERE run_id=? ORDER BY rank",
            (run_24,),
        ).fetchall()
    assert [r["rank"] for r in p0_imps] == list(range(1, 25))
    assert all(r["propensity"] == pytest.approx(1.0) for r in p0_imps[:8])
    assert all(
        r["propensity"] == pytest.approx(expected_tail_propensity)
        for r in p0_imps[8:]
    )
    assert [r["rank"] for r in p1_imps] == list(range(25, 33))
    assert all(
        r["propensity"] == pytest.approx(expected_tail_propensity)
        for r in p1_imps
    )

    # Independent offset=0 runs draw fresh exploration permutations across refreshes.
    tail_orders = set()
    for _ in range(6):
        fresh_items, fresh_run = tracked_recommendations(
            status="recommended", limit=35, offset=0
        )
        assert fresh_run
        tail_orders.add(tuple(item["id"] for item in fresh_items[8:]))
    assert len(tail_orders) > 1

    # Personalization failure fallback branch also honors seed_run_id across pages.
    def broken_personalize(*_args, **_kwargs):
        raise RuntimeError("simulated ranking failure")

    monkeypatch.setattr(main_module, "personalize_recommendations", broken_personalize)
    fb_p0, fb_seed_run, _ = main_module.tracked_recommendations_page(
        status="recommended", limit=24, offset=0
    )
    fb_p1, _, _ = main_module.tracked_recommendations_page(
        status="recommended", limit=8, offset=24, seed_run_id=fb_seed_run
    )
    fb_p2, _, _ = main_module.tracked_recommendations_page(
        status="recommended", limit=8, offset=32, seed_run_id=fb_seed_run
    )
    fb_ids = [item["id"] for item in [*fb_p0, *fb_p1, *fb_p2]]
    assert len(fb_ids) == 35
    assert len(set(fb_ids)) == 35


def test_empty_run_id_feedback_falls_back_to_latest_discovery_impression(database):
    recommendations, discovery_run_id = tracked_recommendations(status="recommended")
    target = recommendations[0]

    with TestClient(app, headers=authenticated_headers()) as client:
        save_res = client.post(
            f"/api/recommendations/{target['id']}/feedback",
            json={"action": "save", "run_id": discovery_run_id},
        )
        assert save_res.status_code == 200

        shortlist_items = client.get(
            "/api/recommendations?status=saved&limit=8&offset=0"
        ).json()
        shortlisted = next(
            item for item in shortlist_items if item["id"] == target["id"]
        )
        assert shortlisted["recommendation_run_id"] == ""

        # Shortlist/history forms submit recommendation_run_id="" (or omit run_id).
        restore_res = client.post(
            f"/api/recommendations/{target['id']}/feedback",
            json={"action": "restore", "run_id": ""},
        )
        assert restore_res.status_code == 200

        reject_res = client.post(
            f"/api/recommendations/{target['id']}/feedback",
            json={"action": "reject", "run_id": ""},
        )
        assert reject_res.status_code == 200
        reject_decision_id = reject_res.json()["decision_id"]

    event = row(
        "SELECT id,run_id,event_type FROM recommendation_events WHERE event_key=?",
        (f"feedback:{reject_decision_id}",),
    )
    assert event["run_id"] is None
    assert event["event_type"] == "reject"
    outcome = row(
        "SELECT i.run_id,i.rank,o.label,o.label_kind,o.confidence "
        "FROM recommendation_outcomes o "
        "JOIN recommendation_impressions i ON i.id=o.impression_id "
        "WHERE o.event_id=?",
        (event["id"],),
    )
    assert outcome == {
        "run_id": discovery_run_id,
        "rank": 1,
        "label": 0.0,
        "label_kind": "explicit_feedback",
        "confidence": 0.5,
    }


def test_create_recommendation_run_guards_and_non_discovery_caller_skip(database):
    from afterword_engine.main import tracked_recommendations_page

    candidate_id = row("SELECT id FROM candidates LIMIT 1")["id"]

    for bad_status in (
        "saved",
        "decisions",
        "rejected",
        "maybe_later",
        "imported",
        "new",
        "read",
    ):
        with pytest.raises(
            ValueError, match="only be created for discovery slates"
        ):
            create_recommendation_run(
                [{"id": candidate_id, "score": 80, "status": "recommended"}],
                status=bad_status,
            )

    for bad_item_status in (
        "saved",
        "imported",
        "rejected",
        "maybe_later",
        "decisions",
        "excluded",
        "read",
    ):
        with pytest.raises(
            ValueError,
            match="may not log shortlist, imported, or archived items",
        ):
            create_recommendation_run(
                [{"id": candidate_id, "score": 80, "status": bad_item_status}],
                status="recommended",
            )

    for bad_offset in (-1, True):
        with pytest.raises(ValueError, match="non-negative integer"):
            create_recommendation_run(
                [{"id": candidate_id, "score": 80, "status": "recommended"}],
                offset=bad_offset,
            )

    before_runs = row("SELECT COUNT(*) count FROM recommendation_runs")["count"]
    for non_discovery in ("saved", "decisions", "rejected", "maybe_later"):
        _items, skipped_run_id, _more = tracked_recommendations_page(
            status=non_discovery, limit=8, offset=0
        )
        assert skipped_run_id == ""
    assert row("SELECT COUNT(*) count FROM recommendation_runs")["count"] == before_runs


def test_legacy_saved_runs_are_excluded_without_discovery_run_and_with_explicit_run_id(
    database,
):
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        only_saved_id = con.execute(
            "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
            "VALUES('Legacy Saved Only','Legacy Author',75.0,'saved',?,?)",
            (source_id, "legacy-saved-only"),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted' WHERE candidate_id=?",
            (only_saved_id,),
        )
        con.execute(
            "INSERT INTO recommendation_runs(id,policy,policy_version,session_id,candidate_count,metadata,created_at) "
            "VALUES('legacy-saved-only','legacy','v0','',1,'{\"status\":\"saved\"}','2025-01-01T00:00:00+00:00')"
        )
        con.execute(
            "INSERT INTO recommendation_impressions(run_id,candidate_id,rank,score,propensity,presented_at,visible_at) "
            "VALUES('legacy-saved-only',?,1,75.0,1.0,'2025-01-01T00:00:00+00:00','2025-01-01T00:01:00+00:00')",
            (only_saved_id,),
        )

    with TestClient(app, headers=authenticated_headers()) as client:
        # Feedback without run_id, with empty run_id, and with explicit legacy run_id
        # must all refuse to attribute an outcome to a non-discovery "saved" run.
        r1 = client.post(
            f"/api/recommendations/{only_saved_id}/feedback",
            json={"action": "reject", "run_id": ""},
        )
        assert r1.status_code == 200
        r2 = client.post(
            f"/api/recommendations/{only_saved_id}/feedback",
            json={"action": "save", "run_id": "legacy-saved-only"},
        )
        assert r2.status_code == 200
        r3 = client.post(
            f"/api/recommendations/{only_saved_id}/read",
            json={"rating": 5},
        )
        assert r3.status_code == 200

    csv_payload = (
        "Title,Author,My Rating,Date Read,Exclusive Shelf\n"
        "Legacy Saved Only,Legacy Author,5,2099/01/02,read\n"
    ).encode("utf-8")
    assert import_goodreads_csv(csv_payload) == 1
    assert (
        row(
            "SELECT COUNT(*) count FROM recommendation_outcomes o "
            "JOIN recommendation_impressions i ON i.id=o.impression_id "
            "WHERE i.candidate_id=?",
            (only_saved_id,),
        )["count"]
        == 0
    )

    # When both a discovery run and a newer legacy saved run exist, even passing
    # run_id="legacy-saved-both" falls back to the discovery impression rather than
    # crediting the non-discovery saved run.
    recommendations, discovery_run_id = tracked_recommendations(status="recommended")
    both_book = recommendations[0]
    with transaction() as con:
        con.execute(
            "INSERT INTO recommendation_runs(id,policy,policy_version,session_id,candidate_count,metadata,created_at) "
            "VALUES('legacy-saved-both','legacy','v0','',1,'{\"status\":\"saved\"}','2098-01-01T00:00:00+00:00')"
        )
        con.execute(
            "INSERT INTO recommendation_impressions(run_id,candidate_id,rank,score,propensity,presented_at,visible_at) "
            "VALUES('legacy-saved-both',?,1,75.0,1.0,'2098-01-01T00:00:00+00:00','2098-01-01T00:01:00+00:00')",
            (both_book["id"],),
        )
    with TestClient(app, headers=authenticated_headers()) as client:
        res = client.post(
            f"/api/recommendations/{both_book['id']}/feedback",
            json={"action": "save", "run_id": "legacy-saved-both"},
        )
        assert res.status_code == 200
        decision_id = res.json()["decision_id"]

    credited = row(
        "SELECT i.run_id FROM recommendation_outcomes o "
        "JOIN recommendation_impressions i ON i.id=o.impression_id "
        "JOIN recommendation_events e ON e.id=o.event_id "
        "WHERE e.event_key=?",
        (f"feedback:{decision_id}",),
    )
    assert credited == {"run_id": discovery_run_id}


def test_internal_settings_and_sources_endpoints_enforce_auth_and_mask_secrets(
    database, monkeypatch
):
    from afterword_engine.secrets import seal

    with transaction() as con:
        for key, raw in (
            ("librarr_api_key", "super-secret-librarr"),
            ("embedding_api_key", "super-secret-embed"),
            ("nyt_api_key", "super-secret-nyt"),
            ("association_google_books_api_key", "super-secret-gbooks"),
            ("llm_rerank_api_key", "super-secret-llm"),
            ("digest_discord_webhook_url", "https://discord.example/secret-webhook"),
            ("digest_smtp_password", "super-secret-smtp"),
        ):
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,1) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=1",
                (key, seal(raw)),
            )

    # Unauthenticated requests are rejected with 401.
    with TestClient(app) as unauth_client:
        for path in ("/api/settings", "/api/sources"):
            res = unauth_client.get(path)
            assert res.status_code == 401

    # When service_secret is configured, missing/wrong x-bookward-service is rejected with 401.
    auth_headers = authenticated_headers()
    monkeypatch.setattr(settings, "service_secret", "shared-engine-secret")
    with TestClient(app, headers=auth_headers) as missing_service_client:
        for path in ("/api/settings", "/api/sources"):
            res = missing_service_client.get(path)
            assert res.status_code == 401

    with TestClient(
        app,
        headers={**auth_headers, "x-bookward-service": "shared-engine-secret"},
    ) as client:
        settings_res = client.get("/api/settings")
        sources_res = client.get("/api/sources")
        overview_res = client.get("/api/overview?track_impressions=false")

    assert settings_res.status_code == 200
    assert sources_res.status_code == 200
    assert "no-store" in settings_res.headers.get("cache-control", "")
    assert "private" in settings_res.headers.get("cache-control", "")
    assert "no-store" in sources_res.headers.get("cache-control", "")
    assert "private" in sources_res.headers.get("cache-control", "")

    settings_payload = settings_res.json()
    serialized = json.dumps(settings_payload)
    assert "super-secret" not in serialized
    assert "secret-webhook" not in serialized
    assert "api_tokens" not in settings_payload
    assert settings_payload["librarr_api_key_set"] is True
    assert settings_payload["embedding_api_key_set"] is True
    assert settings_payload["nyt_api_key_set"] is True
    assert settings_payload["google_books_api_key_set"] is True
    assert settings_payload["llm_rerank"]["api_key_set"] is True
    assert settings_payload["digest"]["discord_webhook_set"] is True
    assert settings_payload["digest"]["smtp_password_set"] is True
    assert sources_res.json() == overview_res.json()["sources"]


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
            "INSERT INTO candidates(title,author,status,source_id,normalized_key) VALUES(?,?,'recommended',?,?)",
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


def test_authenticated_http_discovery_isolates_exploration_seed_per_session(
    database, monkeypatch
):
    monkeypatch.setattr(settings, "exploration_enabled", True)
    monkeypatch.setattr(settings, "exploration_epsilon", 1.0)
    monkeypatch.setattr(settings, "exploration_stable_top_k", 3)

    headers_a = authenticated_headers()
    headers_b = authenticated_headers()
    assert headers_a["x-bookward-session"] != headers_b["x-bookward-session"]

    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        con.execute("DELETE FROM candidates")
        for index in range(40):
            cid = con.execute(
                "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
                "VALUES(?,?,?,'recommended',?,?)",
                (
                    f"Session Pool {index + 1}",
                    f"Author {index + 1}",
                    round(100.0 - index, 2),
                    source_id,
                    f"session pool {index + 1}",
                ),
            ).lastrowid
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted' WHERE candidate_id=?",
                (cid,),
            )

    with TestClient(app) as client:
        token = client.post(
            "/api/settings/api-tokens",
            headers=headers_a,
            json={"name": "Session Isolation CLI"},
        ).json()["token"]
        v1_headers = {"Authorization": f"Bearer {token}"}

        ov_a = client.get(
            "/api/overview?recommendation_limit=24&decision_limit=24",
            headers=headers_a,
        )
        assert ov_a.status_code == 200
        run_a0 = ov_a.json()["recommendation_run_id"]
        ids_a0 = [
            item["id"]
            for item in ov_a.json()["recommendations"]
            if item["status"] == "recommended"
        ]
        assert run_a0

        ov_b = client.get(
            "/api/overview?recommendation_limit=24&decision_limit=24",
            headers=headers_b,
        )
        assert ov_b.status_code == 200
        run_b0 = ov_b.json()["recommendation_run_id"]
        ids_b0 = [
            item["id"]
            for item in ov_b.json()["recommendations"]
            if item["status"] == "recommended"
        ]
        assert run_b0
        assert run_b0 != run_a0

        # Session B acted more recently than Session A; an unseeded offset=24
        # request from Session A must reuse Session A's own seed (run_a0), not
        # Session B's more recent run_b0, and return a disjoint tail slice.
        page_a = client.get(
            "/api/recommendations?status=recommended&limit=8&offset=24",
            headers=headers_a,
        )
        assert page_a.status_code == 200
        run_a1 = page_a.headers["X-Bookward-Recommendation-Run"]
        ids_a1 = [item["id"] for item in page_a.json()]
        assert set(ids_a0).isdisjoint(ids_a1)

        page_b = client.get(
            "/api/recommendations?status=recommended&limit=8&offset=24",
            headers=headers_b,
        )
        assert page_b.status_code == 200
        run_b1 = page_b.headers["X-Bookward-Recommendation-Run"]
        ids_b1 = [item["id"] for item in page_b.json()]
        assert set(ids_b0).isdisjoint(ids_b1)

        # An API token caller also gets its own api-token:* session_id rather
        # than inheriting either browser session's recent seed.
        v1_page = client.get(
            "/api/v1/recommendations?status=recommended&limit=8&offset=24",
            headers=v1_headers,
        )
        assert v1_page.status_code == 200
        run_v1 = v1_page.headers["X-Bookward-Recommendation-Run"]

        # When an API token explicitly passes seed_run_id for Session A's run,
        # it reproduces Session A's exact partition while keeping its api-token session_id.
        v1_seeded = client.get(
            f"/api/v1/recommendations?status=recommended&limit=8&offset=24&seed_run_id={run_a0}",
            headers=v1_headers,
        )
        assert v1_seeded.status_code == 200
        assert [item["id"] for item in v1_seeded.json()] == ids_a1
        run_v1_seeded = v1_seeded.headers["X-Bookward-Recommendation-Run"]

    runs_by_id = {
        item["id"]: item
        for item in rows(
            "SELECT id, session_id, metadata FROM recommendation_runs WHERE id IN (?, ?, ?, ?, ?, ?)",
            (run_a0, run_b0, run_a1, run_b1, run_v1, run_v1_seeded),
        )
    }
    assert runs_by_id[run_a0]["session_id"].startswith("sess:")
    assert runs_by_id[run_b0]["session_id"].startswith("sess:")
    assert runs_by_id[run_a0]["session_id"] != runs_by_id[run_b0]["session_id"]
    assert runs_by_id[run_a1]["session_id"] == runs_by_id[run_a0]["session_id"]
    assert runs_by_id[run_b1]["session_id"] == runs_by_id[run_b0]["session_id"]
    assert runs_by_id[run_v1]["session_id"].startswith("api-token:")
    assert runs_by_id[run_v1_seeded]["session_id"] == runs_by_id[run_v1]["session_id"]

    meta_a1 = json.loads(runs_by_id[run_a1]["metadata"])
    meta_b1 = json.loads(runs_by_id[run_b1]["metadata"])
    meta_v1 = json.loads(runs_by_id[run_v1]["metadata"])
    assert meta_a1["exploration_seed"] == run_a0
    assert meta_b1["exploration_seed"] == run_b0
    assert meta_v1["exploration_seed"] == run_v1

