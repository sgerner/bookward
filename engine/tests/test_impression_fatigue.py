from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afterword_engine.config import settings
from afterword_engine.database import initialize
from afterword_engine.impression_fatigue import (
    apply_impression_fatigue,
    load_unacted_visible_impression_counts,
)


def test_apply_impression_fatigue_decays_and_resorts_unacted_recommended() -> None:
    candidates = [
        {"id": 1, "title": "Stale Book", "status": "recommended", "score": 82.0},
        {"id": 2, "title": "Fresh Book", "status": "recommended", "score": 79.0},
        {"id": 3, "title": "Saved Book", "status": "saved", "score": 75.0},
    ]
    ranked, diag = apply_impression_fatigue(
        candidates,
        {1: 5, 2: 1, 3: 10},
        grace_days=1,
        penalty_per_day=1.5,
        max_penalty=9.0,
    )
    assert diag == {"applied": True, "adjusted_candidates": 1, "max_penalty": 6.0}
    assert [item["id"] for item in ranked] == [2, 1, 3]
    assert ranked[0]["score"] == 79.0
    assert "explanation" not in ranked[0]
    assert ranked[1]["score"] == 76.0
    assert ranked[1]["fatigue_penalty"] == 6.0
    assert ranked[1]["explanation"] == [
        "Lowered after appearing on several recent days without a decision"
    ]
    assert ranked[2]["score"] == 75.0


def test_apply_impression_fatigue_preserves_two_decimal_scores_and_explains_decay() -> None:
    ranked, diag = apply_impression_fatigue(
        [
            {
                "id": 1,
                "title": "Fractional Decay",
                "status": "recommended",
                "score": 82.0,
                "explanation": ['Similar to "Dune" (5★)', "From Sci-Fi Feed"],
            }
        ],
        {1: 2},
        grace_days=1,
        penalty_per_day=0.75,
        max_penalty=9.0,
    )
    assert diag == {"applied": True, "adjusted_candidates": 1, "max_penalty": 0.75}
    assert ranked[0]["score"] == 81.25
    assert ranked[0]["explanation"] == [
        'Similar to "Dune" (5★)',
        "From Sci-Fi Feed",
        "Lowered after appearing on several recent days without a decision",
    ]


def test_apply_impression_fatigue_caps_penalty_at_max() -> None:
    ranked, diag = apply_impression_fatigue(
        [{"id": 1, "status": "recommended", "score": 80.0}],
        {1: 30},
        grace_days=1,
        penalty_per_day=1.5,
        max_penalty=9.0,
    )
    assert diag["max_penalty"] == 9.0
    assert ranked[0]["score"] == 71.0


def test_load_unacted_visible_impression_counts_excludes_acted_and_old(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "fatigue.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute(
            "INSERT OR IGNORE INTO sources(id, name, url, kind, enabled) "
            "VALUES(999, 'List', 'https://example.com', 'rss', 1)"
        )
        for cid, title in [(10, "Unacted"), (11, "Acted")]:
            con.execute(
                "INSERT INTO candidates(id, source_id, title, author, status, normalized_key) "
                "VALUES(?, 999, ?, 'Author', 'recommended', ?)",
                (cid, title, f"{title.lower()}|author"),
            )
        for idx, day in enumerate(
            ["2026-11-10T10:00:00Z", "2026-11-10T18:00:00Z", "2026-11-12T10:00:00Z", "2026-09-01T10:00:00Z"],
            start=1,
        ):
            run_id = f"r{idx}"
            con.execute(
                "INSERT OR IGNORE INTO recommendation_runs(id, policy, policy_version, candidate_count, created_at) "
                "VALUES(?, 'default', '1', 2, ?)",
                (run_id, day),
            )
            con.execute(
                "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at, visible_at) "
                "VALUES(?, 10, 1, 80.0, 1.0, ?, ?)",
                (run_id, day, day),
            )
        con.execute(
            "INSERT INTO recommendation_runs(id, policy, policy_version, candidate_count, created_at) "
            "VALUES('r5', 'default', '1', 2, '2026-11-13T10:00:00Z')"
        )
        con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at, visible_at) "
            "VALUES('r5', 10, 1, 80.0, 1.0, '2026-11-13T10:00:00Z', NULL)"
        )
        # Acted candidate has visible impressions and a save event
        con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at, visible_at) "
            "VALUES('r5', 11, 2, 79.0, 1.0, '2026-11-12T10:00:00Z', '2026-11-12T10:00:00Z')"
        )
        con.execute(
            "INSERT INTO recommendation_events(event_key, candidate_id, event_type, source, occurred_at) "
            "VALUES('ev1', 11, 'save', 'ui', '2026-11-12T11:00:00Z')"
        )
        con.commit()

        counts = load_unacted_visible_impression_counts(
            con,
            window_days=21,
            now=datetime(2026, 11, 15, tzinfo=timezone.utc),
        )
        assert counts == {10: 2}
    finally:
        con.close()
