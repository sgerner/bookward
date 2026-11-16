from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afterword_engine.config import settings
from afterword_engine.database import initialize
from scripts.evaluate_organic_reads import evaluate_organic_reads


def test_evaluate_organic_reads_computes_pool_and_pre_read_ranks(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "organic.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    con = sqlite3.connect(db_path)
    try:
        con.execute("INSERT OR IGNORE INTO sources(id, name, url, kind, enabled) VALUES(999, 'List', 'https://example.com', 'rss', 1)")
        con.execute(
            "INSERT INTO candidates(id, source_id, title, author, isbn13, normalized_key, created_at) "
            "VALUES(10, 999, 'Dune', 'Frank Herbert', '9780441172719', 'dune|frankherbert', '2026-09-22T00:00:00Z')"
        )
        con.execute(
            "INSERT INTO candidates(id, source_id, title, author, normalized_key, created_at) "
            "VALUES(11, 999, 'Foundation', 'Isaac Asimov', 'foundation|isaacasimov', '2026-09-22T00:00:00Z')"
        )
        con.execute(
            "INSERT INTO recommendation_runs(id, policy, policy_version, candidate_count, created_at) "
            "VALUES('run-1', 'default', '1', 2, '2026-09-25T00:00:00Z')"
        )
        con.execute(
            "INSERT INTO recommendation_runs(id, policy, policy_version, candidate_count, created_at) "
            "VALUES('run-2', 'default', '1', 2, '2026-10-05T00:00:00Z')"
        )
        # Pre-read impression for Dune at rank 5
        con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at) "
            "VALUES('run-1', 10, 5, 81.2, 1.0, '2026-09-25T00:00:00Z')"
        )
        # Post-read impression for Foundation (should NOT count as pre-read rank)
        con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at) "
            "VALUES('run-2', 11, 2, 88.0, 1.0, '2026-10-05T00:00:00Z')"
        )
        # Excluded: manual_history and pre-cutoff
        con.execute(
            "INSERT INTO reads(id, title, author, rating, source, created_at) "
            "VALUES(1, 'Old Book', 'Author A', 5, 'goodreads_rss', '2026-09-01T00:00:00Z')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, source, created_at) "
            "VALUES(2, 'Manual Book', 'Author B', 5, 'manual_history', '2026-10-01T00:00:00Z')"
        )
        # Included organic reads
        con.execute(
            "INSERT INTO reads(id, title, author, rating, isbn, source, created_at) "
            "VALUES(3, 'Dune (Deluxe Edition)', 'Frank Herbert', 5, '9780441172719', 'goodreads_rss', '2026-10-01T00:00:00Z')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, source, created_at) "
            "VALUES(4, 'Foundation', 'Isaac Asimov', 3, 'goodreads_rss', '2026-10-02T00:00:00Z')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, source, created_at) "
            "VALUES(5, 'Unseen Gem', 'Author C', 4, 'goodreads_rss', '2026-10-03T00:00:00Z')"
        )
        con.commit()
    finally:
        con.close()

    report = evaluate_organic_reads(db_path, since="2026-09-21")
    assert report["total_organic_reads"] == 3
    assert report["in_pool_count"] == 2
    assert report["in_pool_rate"] == 0.6667
    assert report["pre_read_ranked_count"] == 1
    assert report["all_ranked"]["median_rank"] == 5
    assert report["all_ranked"]["hit_at_8"] == 1
    assert report["all_ranked"]["mrr"] == 0.2
    assert report["high_rated_ranked"]["count"] == 1
    assert report["low_or_mid_ranked"]["count"] == 0
