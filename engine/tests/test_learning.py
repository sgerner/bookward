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
)
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
    assert run["policy_version"] == "rating-neighborhood-v1"
    assert run["candidate_count"] == len(recommendations)
    assert impressions["count"] == len(recommendations)
    assert row(
        "SELECT COUNT(*) count FROM recommendation_impressions WHERE run_id=? AND propensity=1",
        (run_id,),
    )["count"] == len(recommendations)

    with TestClient(app) as client:
        response = client.get("/api/recommendations")
    assert response.status_code == 200
    header_run = response.headers["x-bookward-recommendation-run"]
    assert row("SELECT id FROM recommendation_runs WHERE id=?", (header_run,))


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
