"""Small reproducer for the production interaction-vector ID mismatch.

Interaction event rows expose the event primary key as ``id``. The request
path currently passes those rows to a helper that treats ``id`` as the
candidate embedding key, so this fixture keeps the relevant collision tiny and
deterministic.
"""

import sqlite3

from afterword_engine.embeddings import content_hash, vector_blob
from afterword_engine.interaction_personalization import load_cached_candidate_vectors
from afterword_engine.scoring import document


def test_event_primary_key_collision_drops_both_candidate_vectors():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO settings(key,value) VALUES(?,?)",
        [("embedding_backend", "local"), ("embedding_model", "hashing-768")],
    )
    connection.execute(
        "CREATE TABLE embeddings(entity_type TEXT, entity_id INTEGER, backend TEXT, "
        "model TEXT, vector BLOB, content_hash TEXT)"
    )

    ranked = {
        "id": 1,
        "title": "Visible ranked candidate",
        "author": "Ranked author",
        "description": "ranked text",
        "genres": "[]",
        "status": "recommended",
        "score": 50.0,
    }
    interacted_candidate = {
        "title": "Old rejected item",
        "author": "Event author",
        "description": "old item text",
        "genres": "[]",
    }
    event = {
        "id": 1,  # recommendation_events.id, not candidates.id
        "candidate_id": 44,
        "event_type": "reject",
        **interacted_candidate,
    }
    for candidate_id, item, vector in (
        (1, ranked, [1.0, 0.0]),
        (44, interacted_candidate, [0.0, 1.0]),
    ):
        connection.execute(
            "INSERT INTO embeddings VALUES(?,?,?,?,?,?)",
            (
                "candidate",
                candidate_id,
                "local",
                "hashing-768",
                vector_blob(vector),
                content_hash(document(item)),
            ),
        )

    # This is the shape passed by tracked_recommendations:
    # [*ranked, *interaction_events]. The event's id=1 overwrites ranked id=1,
    # then the loader queries candidate 1 with the event book's content hash;
    # candidate 44 is never queried at all.
    loaded = load_cached_candidate_vectors([ranked, event], connection)
    assert loaded == {}

    # Re-keying the event row to candidate_id restores the expected coverage.
    normalized_event = {**event, "id": event["candidate_id"]}
    corrected = load_cached_candidate_vectors([ranked, normalized_event], connection)
    assert set(corrected) == {1, 44}
