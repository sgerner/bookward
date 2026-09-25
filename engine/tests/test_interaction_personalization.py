from datetime import datetime, timedelta, timezone

from afterword_engine.interaction_personalization import personalize_recommendations


NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)


def interaction(number, event_type, subject, *, value=None, days_ago=0):
    occurred = (NOW - timedelta(days=days_ago)).isoformat()
    return {
        "id": number,
        "candidate_id": number,
        "event_type": event_type,
        "value": value,
        "occurred_at": occurred,
        "title": f"Interaction book {number}",
        "author": f"Writer {number}",
        "genres": [subject],
    }


def candidate(number, subject, score, *, status="recommended", confidence=1.0):
    return {
        "id": number,
        "title": f"Candidate {number}",
        "author": f"Candidate writer {number}",
        "genres": [subject],
        "score": score,
        "status": status,
        "metadata_confidence": confidence,
        "explanation": [],
    }


def split_preference_events(count=6):
    events = [
        interaction(i, "save", "Solar myth")
        for i in range(1, count + 1)
    ]
    events.extend(
        interaction(count + i, "reject", "Dark thriller")
        for i in range(1, count + 1)
    )
    return events


def test_confident_interactions_adjust_and_reorder_live_recommendations():
    recommendations = [
        candidate(101, "Dark thriller", 50),
        candidate(102, "Solar myth", 49),
        candidate(103, "Solar myth", 49, status="saved"),
    ]

    ranked, diagnostics = personalize_recommendations(
        recommendations, split_preference_events(), now=NOW
    )

    assert [item["id"] for item in ranked] == [102, 101, 103]
    assert ranked[0]["score"] > 49
    assert ranked[1]["score"] < 50
    assert ranked[2]["score"] == 49
    assert diagnostics == {
        "applied": True,
        "observed_books": 12,
        "qualified_features": 2,
        "semantic_evidence_books": 0,
        "semantic_adjusted_candidates": 0,
        "adjusted_candidates": 2,
        "max_score_adjustment": 2.0,
    }
    assert "saved, imported, or positively rated" in ranked[0]["explanation"][-1]


def test_weak_or_unobserved_signals_leave_champion_order_unchanged():
    events = split_preference_events(count=5)
    events.extend(
        {
            **interaction(100 + i, "visible", "Solar myth"),
            "title": f"Unclicked book {i}",
        }
        for i in range(20)
    )
    recommendations = [
        candidate(101, "Dark thriller", 50),
        candidate(102, "Solar myth", 49),
    ]

    ranked, diagnostics = personalize_recommendations(
        recommendations, events, now=NOW
    )

    assert [item["id"] for item in ranked] == [101, 102]
    assert [item["score"] for item in ranked] == [50, 49]
    assert diagnostics["applied"] is False
    assert diagnostics["observed_books"] == 10
    assert diagnostics["adjusted_candidates"] == 0


def test_latest_restore_and_neutral_rating_clear_older_actions():
    events = split_preference_events()
    # The first rejection is later restored, and the first save is later
    # replaced by a neutral three-star read. Neither old label remains active.
    events.extend(
        [
            {
                **interaction(13, "restore", "Dark thriller"),
                "title": "Interaction book 7",
                "author": "Writer 7",
            },
            {
                **interaction(14, "read", "Solar myth", value=3),
                "title": "Interaction book 1",
                "author": "Writer 1",
            },
        ]
    )

    ranked, diagnostics = personalize_recommendations(
        [candidate(101, "Solar myth", 50), candidate(102, "Dark thriller", 50)],
        events,
        now=NOW,
    )

    assert diagnostics["observed_books"] == 10
    assert diagnostics["applied"] is False
    assert [item["score"] for item in ranked] == [50, 50]


def test_older_interactions_decay_out():
    events = [
        interaction(i, "save", "Solar myth", days_ago=2_900)
        for i in range(1, 7)
    ]
    events.extend(
        interaction(6 + i, "reject", "Dark thriller", days_ago=2_900)
        for i in range(1, 7)
    )

    ranked, diagnostics = personalize_recommendations(
        [
            candidate(101, "Solar myth", 50, confidence=0.4),
            candidate(102, "Dark thriller", 50, confidence=0.4),
        ],
        events,
        now=NOW,
    )

    assert diagnostics["applied"] is False
    assert [item["score"] for item in ranked] == [50, 50]


def test_sparse_candidate_metadata_scales_down_adjustment():
    ranked, diagnostics = personalize_recommendations(
        [candidate(101, "Solar myth", 50, confidence=0.25)],
        split_preference_events(),
        now=NOW,
    )

    assert diagnostics["applied"] is True
    assert ranked[0]["score"] == 50.5
    assert diagnostics["max_score_adjustment"] == 0.5


def test_five_star_and_one_star_attributed_reads_are_preference_signals():
    events = [
        interaction(i, "read", "Solar myth", value=5)
        for i in range(1, 7)
    ]
    events.extend(
        interaction(6 + i, "read", "Dark thriller", value=1)
        for i in range(1, 7)
    )

    ranked, diagnostics = personalize_recommendations(
        [candidate(101, "Dark thriller", 50), candidate(102, "Solar myth", 50)],
        events,
        now=NOW,
    )

    assert diagnostics["applied"] is True
    assert ranked[0]["id"] == 102
    assert ranked[0]["score"] > ranked[1]["score"]


def test_semantic_neighbor_learning_generalizes_beyond_repeated_subjects():
    events = []
    vectors = {}
    for index in range(6):
        saved = interaction(index + 1, "save", f"Unique saved topic {index}")
        rejected = interaction(index + 7, "reject", f"Unique rejected topic {index}")
        events.extend((saved, rejected))
        vectors[index + 1] = [1.0, index * 0.01]
        vectors[index + 7] = [-1.0, index * 0.01]
    candidates = [
        candidate(101, "Never seen label", 50),
        candidate(102, "Another unseen label", 50),
    ]

    ranked, diagnostics = personalize_recommendations(
        candidates,
        events,
        now=NOW,
        interaction_vectors=vectors,
        candidate_vectors={101: [1.0, 0.0], 102: [-1.0, 0.0]},
    )

    assert [item["id"] for item in ranked] == [101, 102]
    assert ranked[0]["score"] == 54.0
    assert ranked[1]["score"] == 46.0
    assert diagnostics["qualified_features"] == 0
    assert diagnostics["semantic_evidence_books"] == 12
    assert diagnostics["semantic_adjusted_candidates"] == 2
