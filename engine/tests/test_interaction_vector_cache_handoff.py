import json
from pathlib import Path

import pytest

from afterword_engine.config import settings
from afterword_engine.database import initialize, transaction
from afterword_engine.embeddings import content_hash, vector_blob
from afterword_engine.interaction_personalization import (
    load_cached_candidate_vectors,
    personalize_recommendations,
)
from afterword_engine.learning import record_event_in_connection
from afterword_engine.main import tracked_recommendations
from afterword_engine.scoring import document


@pytest.fixture()
def database(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "interaction-cache.db"))
    initialize(seed_demo=False)
    return settings.db


@pytest.mark.parametrize("request_context", ["overview", "recommended"])
def test_interaction_vectors_use_candidate_ids_in_both_request_contexts(
    database, monkeypatch, request_context
):
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        candidates = [
            ("Visible candidate one", "Author One", "recommended"),
            ("Visible candidate two", "Author Two", "recommended"),
            ("Hidden interacted candidate", "Author Three", "rejected"),
        ]
        candidate_ids = []
        for title, author, status in candidates:
            candidate_id = con.execute(
                "INSERT INTO candidates(title,author,genres,description,status,source_id,normalized_key) "
                "VALUES(?,?,?,? ,?,?,?)",
                (
                    title,
                    author,
                    json.dumps(["space opera"]),
                    f"Description for {title}",
                    status,
                    source_id,
                    f"cache handoff {title.casefold()}",
                ),
            ).lastrowid
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted',metadata_confidence=1 "
                "WHERE candidate_id=?",
                (candidate_id,),
            )
            candidate_ids.append(candidate_id)

        # Give the event IDs (1 and 2) the same values as visible candidate IDs,
        # while both events belong to candidate 3. Their tied timestamps ensure
        # the event primary key still determines which action is latest.
        for event_key, event_type in (
            ("cache-handoff-save", "save"),
            ("cache-handoff-reject", "reject"),
        ):
            record_event_in_connection(
                con,
                event_key=event_key,
                candidate_id=candidate_ids[2],
                event_type=event_type,
                occurred_at="2026-09-01T10:00:00+00:00",
            )
        event_ids = [
            item["id"]
            for item in con.execute(
                "SELECT id FROM recommendation_events ORDER BY id"
            ).fetchall()
        ]

        con.executemany(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [("embedding_backend", "local"), ("embedding_model", "hashing-768")],
        )
        vectors = ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0])
        for candidate_id, (title, author, _status), vector in zip(
            candidate_ids, candidates, vectors
        ):
            item = {
                "title": title,
                "author": author,
                "genres": json.dumps(["space opera"]),
                "description": f"Description for {title}",
            }
            con.execute(
                "INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) "
                "VALUES('candidate',?,'local','hashing-768',?,?,?)",
                (
                    candidate_id,
                    vector_blob(vector),
                    len(vector),
                    content_hash(document(item)),
                ),
            )

    assert candidate_ids[:2] == event_ids
    original_loader = load_cached_candidate_vectors
    original_personalizer = personalize_recommendations
    cache_observations = []
    learner_event_observations = []

    def capture_cache_items(items, connection=None):
        event_items = [item for item in items if "event_type" in item]
        loaded = original_loader(items, connection)
        cache_observations.append(
            {
                "event_items": [
                    (item["id"], item["candidate_id"], item["event_type"])
                    for item in event_items
                ],
                "loaded_ids": set(loaded),
            }
        )
        return loaded

    def capture_learner_events(ranked, events, **kwargs):
        learner_event_observations.append(
            [(item["id"], item["candidate_id"], item["event_type"]) for item in events]
        )
        return original_personalizer(ranked, events, **kwargs)

    monkeypatch.setattr(
        "afterword_engine.main.load_cached_candidate_vectors", capture_cache_items
    )
    monkeypatch.setattr(
        "afterword_engine.main.personalize_recommendations", capture_learner_events
    )

    if request_context == "overview":
        recommendations, _ = tracked_recommendations(
            limit=24, recommended_limit=24
        )
    else:
        recommendations, _ = tracked_recommendations(
            status="recommended", limit=24, offset=0
        )

    assert {item["id"] for item in recommendations} == set(candidate_ids[:2])
    assert cache_observations == [
        {
            "event_items": [
                (candidate_ids[2], candidate_ids[2], "reject"),
                (candidate_ids[2], candidate_ids[2], "save"),
            ],
            "loaded_ids": set(candidate_ids),
        }
    ]
    assert learner_event_observations == [
        [
            (event_ids[1], candidate_ids[2], "reject"),
            (event_ids[0], candidate_ids[2], "save"),
        ]
    ]
