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
    assert "subject solar myth" in ranked[0]["explanation"][-1]
    assert "subject dark thriller" in ranked[1]["explanation"][-1]


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
    assert ranked[0]["score"] == 53.2
    assert ranked[1]["score"] == 46.8
    assert diagnostics["qualified_features"] == 0
    assert diagnostics["semantic_evidence_books"] == 12
    assert diagnostics["semantic_adjusted_candidates"] == 2


def test_mature_history_learns_empirical_bayes_era_and_diminishing_subjects():
    events = []
    for index in range(10):
        events.append(
            {
                **interaction(index + 1, "save", f"Unique save {index}"),
                "release_date": "2025-05-01",
                "genres": ["Time travel", "Speculative", "Audiobooks", "Fiction literature"],
            }
        )
    for index in range(10):
        events.append(
            {
                **interaction(index + 11, "reject", f"Unique reject {index}"),
                "release_date": "1932-01-01",
                "genres": [f"Unique reject {index}", "Audiobooks"],
            }
        )

    candidates = [
        {
            **candidate(201, "Unseen genre", 50),
            "release_date": "1925-01-01",
            "genres": ["Audiobooks", "Fiction literature"],
        },
        {
            **candidate(202, "Time travel", 50),
            "release_date": "2026-01-01",
            "genres": ["Time travel", "Speculative", "Audiobooks"],
        },
    ]

    ranked, diagnostics = personalize_recommendations(candidates, events, now=NOW)

    assert diagnostics["applied"] is True
    assert diagnostics["observed_books"] == 20
    assert [item["id"] for item in ranked] == [202, 201]
    assert ranked[0]["score"] > 50
    assert ranked[1]["score"] < 50
    assert any("publication era" in text for text in ranked[1]["explanation"])


def test_mature_semantic_pooling_uses_asymmetric_positive_floor():
    events = []
    vectors = {}
    for index in range(10):
        saved = interaction(index + 1, "save", f"Saved topic {index}")
        rejected = interaction(index + 11, "reject", f"Rejected topic {index}")
        events.extend((saved, rejected))
        vectors[index + 1] = [1.0, 0.0]
        vectors[index + 11] = [0.0, 1.0]

    # Candidate 301 has cosine 0.48 with positive seeds (< 0.52 positive floor -> ignored)
    # Candidate 302 has cosine 0.48 with negative seeds (>= 0.45 negative floor -> penalized)
    candidates = [
        candidate(301, "Unseen A", 50),
        candidate(302, "Unseen B", 50),
    ]
    cand_vectors = {
        301: [0.48, -0.877268],
        302: [-0.877268, 0.48],
    }

    ranked, diagnostics = personalize_recommendations(
        candidates,
        events,
        now=NOW,
        interaction_vectors=vectors,
        candidate_vectors=cand_vectors,
    )

    by_id = {item["id"]: item for item in ranked}
    assert by_id[301]["score"] == 50
    assert by_id[302]["score"] < 50
    assert diagnostics["semantic_evidence_books"] == 20


def test_to_read_priors_apply_bounded_035_author_and_subject_boosts_without_polluting_observations():
    shelf_items = [
        {
            "id": 1,
            "shelf": "to-read",
            "title": "Project Hail Mary",
            "author": "Andy Weir",
            "genres": ["Hard science fiction", "Audiobooks"],
            "release_date": "2021-05-04",
        },
        {
            "id": 2,
            "shelf": "currently-reading",
            "title": "Piranesi",
            "author": "Susanna Clarke",
            "genres": ["Fantasy"],
        },
    ]
    candidates = [
        {
            **candidate(401, "Unrelated mystery", 70.0),
            "author": "Other Writer",
        },
        {
            **candidate(402, "Hard science fiction", 70.0),
            "author": "Novel Sci-Fi Author",
        },
        {
            **candidate(403, "Unrelated thriller", 70.0),
            "author": "Andy Weir",
        },
        {
            # Era-only match (2021 -> 2020_plus) and currently-reading author must NOT get to-read boost
            **candidate(404, "Fantasy", 70.0),
            "author": "Susanna Clarke",
            "release_date": "2024-01-01",
        },
    ]

    ranked, diagnostics = personalize_recommendations(
        candidates,
        [],
        now=NOW,
        shelf_items=shelf_items,
    )

    by_id = {item["id"]: item for item in ranked}
    assert by_id[401]["score"] == 70.0
    assert by_id[402]["score"] == round(70.0 + 0.35, 1)
    assert by_id[403]["score"] == round(70.0 + 0.35, 1)
    assert by_id[404]["score"] == 70.0
    assert diagnostics["observed_books"] == 0
    assert diagnostics["qualified_features"] == 2
    assert diagnostics["adjusted_candidates"] == 2
    assert diagnostics["max_score_adjustment"] == round(0.35, 1)
    assert "Raised using your Goodreads to-read shelf (subject hard science fiction)" in by_id[402]["explanation"]
    assert "Raised using your Goodreads to-read shelf (author Andy Weir)" in by_id[403]["explanation"]


def test_to_read_priors_never_override_explicit_save_or_reject_events():
    shelf_items = [
        {
            "id": 1,
            "shelf": "to-read",
            "title": "Queued Thriller",
            "author": "Rejected Author",
            "genres": ["Dark thriller"],
        },
        {
            "id": 2,
            "shelf": "to-read",
            "title": "Queued Solar Book",
            "author": "Loved Author",
            "genres": ["Solar myth"],
        },
        {
            "id": 3,
            "shelf": "to-read",
            "title": "Explicitly Deferred Queue Book",
            "author": "Deferred Author",
            "genres": ["Deferred genre"],
        },
        {
            "id": 4,
            "shelf": "to-read",
            "title": "Fresh Queue Discovery",
            "author": "Fresh Author",
            "genres": ["First contact"],
        },
    ]
    events = split_preference_events(count=6)
    events.extend(
        [
            {
                **interaction(50, "reject", "Noir"),
                "title": "Single Rejected Author Book",
                "author": "Rejected Author",
            },
            {
                **interaction(51, "maybe_later", "Deferred genre"),
                "title": "Explicitly Deferred Queue Book",
                "author": "Deferred Author",
            },
        ]
    )

    candidates = [
        # Explicit confident save on Solar myth (+2.0) must stay authoritative, not shrunk to +0.35
        candidate(501, "Solar myth", 50.0),
        # Explicit confident reject on Dark thriller (-2.0) + rejected author must stay negative
        {**candidate(502, "Dark thriller", 50.0), "author": "Rejected Author"},
        # Single reject on Rejected Author blocks +0.35 author prior even on a neutral genre
        {**candidate(503, "Neutral topic", 50.0), "author": "Rejected Author"},
        # Explicit maybe_later on the exact queued book suppresses its shelf prior
        {**candidate(504, "Deferred genre", 50.0), "author": "Deferred Author"},
        # Unconflicted to-read item receives bounded +0.35 prior
        {**candidate(505, "First contact", 50.0), "author": "Other Writer"},
    ]

    baseline_ranked, _ = personalize_recommendations(candidates, events, now=NOW)
    baseline_by_id = {item["id"]: item for item in baseline_ranked}

    ranked, _ = personalize_recommendations(
        candidates,
        events,
        now=NOW,
        shelf_items=shelf_items,
    )

    by_id = {item["id"]: item for item in ranked}
    assert by_id[501]["score"] == baseline_by_id[501]["score"] > 50.0
    assert "saved, imported, or positively rated" in by_id[501]["explanation"][-1]
    assert by_id[502]["score"] == baseline_by_id[502]["score"] < 50.0
    assert by_id[503]["score"] == 50.0
    assert by_id[504]["score"] == 50.0
    assert by_id[505]["score"] == round(50.0 + 0.35, 1)


def test_shelf_prior_benchmark_gate_passes_aligned_and_abstains_on_regression_or_inconclusive():
    from afterword_engine.interaction_personalization import evaluate_shelf_prior_gate

    aligned_shelf = [
        {
            "id": 1,
            "shelf": "to-read",
            "title": "Queued Space Opera",
            "author": "Queue Writer",
            "genres": ["Space opera"],
            "added_at": (NOW - timedelta(days=30)).isoformat(),
        }
    ]

    # 1. Single-class / empty benchmark is inconclusive and abstains in strict mode.
    only_saves = [interaction(1, "save", "Space opera", days_ago=5)]
    inconclusive = evaluate_shelf_prior_gate(only_saves, aligned_shelf, now=NOW)
    assert inconclusive["status"] == "inconclusive"
    assert inconclusive["passed"] is False
    assert inconclusive["abstained"] is True

    strict_ranked, strict_diag = personalize_recommendations(
        [candidate(601, "Space opera", 60.0)],
        only_saves,
        now=NOW,
        shelf_items=aligned_shelf,
        gate_mode="strict",
    )
    assert strict_ranked[0]["score"] == 60.0
    assert strict_diag["adjusted_candidates"] == 0

    # 2. Aligned history improves both LOO AUC and chronological Prequential AUC -> passes gate!
    aligned_events = [
        interaction(1, "save", "Space opera", days_ago=10),
        interaction(2, "reject", "Cozy mystery", days_ago=8),
        interaction(3, "save", "Space opera", days_ago=5),
        interaction(4, "reject", "Historical romance", days_ago=2),
    ]
    passed_gate = evaluate_shelf_prior_gate(aligned_events, aligned_shelf, now=NOW)
    assert passed_gate["status"] == "passed"
    assert passed_gate["passed"] is True
    assert passed_gate["abstained"] is False
    assert passed_gate["loo_challenger_auc"] >= passed_gate["loo_baseline_auc"]
    assert passed_gate["prequential_challenger_auc"] >= passed_gate["prequential_baseline_auc"]

    # 3. Contradictory shelf (to-read matches genres the user rejected in Discovery) regresses
    # held-out LOO / Prequential AUC -> live personalize_recommendations abstains automatically!
    contradictory_shelf = [
        {
            "id": 10,
            "shelf": "to-read",
            "title": "Old Stale Queue Item",
            "author": "Stale Writer",
            "genres": ["Cozy mystery"],
            "added_at": (NOW - timedelta(days=60)).isoformat(),
        },
        {
            "id": 11,
            "shelf": "to-read",
            "title": "Second Queue Item",
            "author": "Fresh Writer",
            "genres": ["Cyberpunk"],
            "added_at": (NOW - timedelta(days=60)).isoformat(),
        },
    ]
    regressed_gate = evaluate_shelf_prior_gate(aligned_events, contradictory_shelf, now=NOW)
    assert regressed_gate["status"] == "regressed"
    assert regressed_gate["passed"] is False
    assert regressed_gate["abstained"] is True

    abstained_ranked, abstained_diag = personalize_recommendations(
        [candidate(602, "Cyberpunk", 60.0)],
        aligned_events,
        now=NOW,
        shelf_items=contradictory_shelf,
    )
    assert abstained_ranked[0]["score"] == 60.0
    assert abstained_diag["adjusted_candidates"] == 0


def test_tracked_recommendations_persists_and_replays_to_read_priors_in_decision_evidence(
    tmp_path, monkeypatch
):
    import json as json_mod
    import sqlite3
    from afterword_engine.config import settings
    from afterword_engine.database import connect, initialize, normalize_key
    from afterword_engine.decision_evidence import load_decision_evidence
    from afterword_engine.main import tracked_recommendations

    db_path = tmp_path / "evidence_shelf_priors.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT OR IGNORE INTO sources(id, name, url, kind, enabled) "
            "VALUES(999, 'Curated', 'https://example.com/rss', 'rss', 1)"
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, source, title, author, isbn) "
            "VALUES('to-read', 'goodreads_csv', 'Project Hail Mary', 'Andy Weir', '9780593135204')"
        )
        for cid, title, author, genres, score in [
            # Queued candidate itself: excluded from Discovery, but supplies catalog genres for shelf prior!
            (701, "Project Hail Mary", "Andy Weir", ["Hard science fiction"], 85.0),
            # Novel candidate matching to-read subject -> boosted from 75.0 to 75.4, overtaking 703!
            (702, "Delta-V", "Daniel Suarez", ["Hard science fiction"], 75.0),
            # Higher raw decimal tie-break check: 75.2 stays below 75.4 after prior
            (703, "Ordinary Drama", "Plain Author", ["Domestic drama"], 75.2),
        ]:
            con.execute(
                "INSERT INTO candidates(id, source_id, title, author, genres, status, score, explanation, normalized_key) "
                "VALUES(?, 999, ?, ?, ?, 'recommended', ?, '[\"Base match\"]', ?)",
                (cid, title, author, json_mod.dumps(genres), score, normalize_key(title, author)),
            )
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted', metadata_confidence=1.0 "
                "WHERE candidate_id=?",
                (cid,),
            )
        con.commit()
    finally:
        con.close()

    served, run_id = tracked_recommendations(status="recommended", limit=10)
    assert [item["id"] for item in served] == [702, 703]
    assert served[0]["score"] == round(75.0 + 0.35, 1)
    assert served[1]["score"] == 75.2

    with connect() as con:
        evidence = load_decision_evidence(con, run_id)
    assert evidence is not None
    decision = evidence["decision"]
    replayed, _ = personalize_recommendations(
        decision["pools"]["base"],
        decision["interaction_profile"],
        now=datetime.fromisoformat(decision["request_context"]["ranking_at"]),
    )
    assert [(item["id"], item["score"]) for item in replayed] == [
        (item["id"], item["score"]) for item in decision["pools"]["personalized"]
    ]

