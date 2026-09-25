import csv
import io
import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from afterword_engine.config import settings
from afterword_engine.database import initialize, row, transaction
from afterword_engine.ingestion import import_goodreads_csv
from afterword_engine.learning import (
    attribute_read_outcomes,
    create_recommendation_run,
    record_event_in_connection,
)
from afterword_engine.interaction_personalization import load_interaction_events
from afterword_engine.main import app, tracked_recommendations


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
    assert run["policy_version"] == "rating-kernel-recency-interaction-v1"
    assert run["candidate_count"] == len(recommendations)
    assert impressions["count"] == len(recommendations)
    assert row(
        "SELECT COUNT(*) count FROM recommendation_impressions WHERE run_id=? AND propensity=1",
        (run_id,),
    )["count"] == len(recommendations)
    assert json.loads(run["metadata"])["interaction_learning"]["mode"] == "confidence_gated_live"

    with TestClient(app) as client:
        response = client.get("/api/recommendations")
    assert response.status_code == 200
    header_run = response.headers["x-bookward-recommendation-run"]
    assert row("SELECT id FROM recommendation_runs WHERE id=?", (header_run,))


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
    assert run["policy_version"] == "rating-kernel-recency-interaction-v1"
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


def test_session_scoped_learning_keeps_browser_preferences_separate(database):
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

    reader_a_events = load_interaction_events(session_id="session-reader-a")
    reader_b_events = load_interaction_events(session_id="session-reader-b")

    assert [event["candidate_id"] for event in reader_a_events] == [reader_a_id]
    assert [event["candidate_id"] for event in reader_b_events] == [reader_b_id]


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
    with TestClient(app) as client:
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
    with TestClient(app) as client:
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

    with TestClient(app) as client:
        invalid = client.post(
            f"/api/recommendations/{candidate_id}/feedback",
            json={"action": "reject", "run_id": "missing-run-1"},
        )
    assert invalid.status_code == 400
    assert row("SELECT COUNT(*) count FROM feedback WHERE action='reject'")["count"] == 0


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
