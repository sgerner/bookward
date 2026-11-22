from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afterword_engine.config import settings
from afterword_engine.database import initialize
from afterword_engine.ingestion import _read_metadata_identity_hash
from scripts.evaluate_organic_reads import evaluate_organic_reads


def _init_db(tmp_path: Path, monkeypatch, name: str = "organic.db") -> Path:
    db_path = tmp_path / name
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)
    return db_path


def _accept_candidate(
    con: sqlite3.Connection,
    candidate_id: int,
    *,
    provider: str = "openlibrary",
    work_id: str = "",
    isbn13: str = "",
    isbn10: str = "",
) -> None:
    con.execute(
        "UPDATE candidate_quality "
        "SET quality_status = 'accepted', provider = ?, work_id = ?, isbn13 = ?, isbn10 = ? "
        "WHERE candidate_id = ?",
        (provider, work_id, isbn13, isbn10, candidate_id),
    )


def test_evaluate_organic_reads_computes_pool_and_pre_read_ranks(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = _init_db(tmp_path, monkeypatch)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute(
            "INSERT OR IGNORE INTO sources(id, name, url, kind, enabled) "
            "VALUES(999, 'List', 'https://example.com', 'rss', 1)"
        )
        con.execute(
            "INSERT INTO candidates(id, source_id, title, author, isbn13, normalized_key, created_at) "
            "VALUES(10, 999, 'Dune', 'Frank Herbert', '9780441172719', 'dune|frankherbert', '2026-09-22T00:00:00Z')"
        )
        _accept_candidate(con, 10, isbn13="9780441172719")
        con.execute(
            "INSERT INTO candidates(id, source_id, title, author, normalized_key, created_at) "
            "VALUES(11, 999, 'Foundation', 'Isaac Asimov', 'foundation|isaacasimov', '2026-09-22T00:00:00Z')"
        )
        _accept_candidate(con, 11)

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
        # Post-read impression for Foundation (read_at 2026-10-02, synced later via default created_at)
        con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at) "
            "VALUES('run-2', 11, 2, 88.0, 1.0, '2026-10-05T00:00:00Z')"
        )

        # Excluded: pre-cutoff read_at, manual_history, goodreads_csv, and demo
        # All use default SQLite space-separated CURRENT_TIMESTAMP for created_at.
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, source) "
            "VALUES(1, 'Old Book', 'Author A', 5, '2026-09-01', 'goodreads_rss')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, source) "
            "VALUES(2, 'Manual Book', 'Author B', 5, '2026-10-01', 'manual_history')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, source) "
            "VALUES(6, 'CSV Export Book', 'Author D', 5, '2026-10-01', 'goodreads_csv')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, source) "
            "VALUES(7, 'Seed Demo Book', 'Author E', 5, 'demo')"
        )

        # Included organic reads with default SQLite reads.created_at timestamps
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, isbn, source) "
            "VALUES(3, 'Dune (Deluxe Edition)', 'Frank Herbert', 5, '2026-10-01', '9780441172719', 'goodreads_rss')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, source) "
            "VALUES(4, 'Foundation', 'Isaac Asimov', 3, '2026-10-02', 'goodreads_rss')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, source) "
            "VALUES(5, 'Unseen Gem', 'Author C', 4, 'goodreads_rss')"
        )
        con.commit()
    finally:
        con.close()

    report = evaluate_organic_reads(db_path, since="2026-09-21")
    assert "all_ranked" not in report
    assert "low_or_mid_ranked" not in report
    assert report["total_organic_reads"] == 3
    assert report["in_pool_count"] == 2
    assert report["in_pool_rate"] == 0.6667
    assert report["pre_read_ranked_count"] == 1
    assert report["all_completed"]["count"] == 1
    assert report["all_completed"]["median_rank"] == 5
    assert report["all_completed"]["hit_at_8"] == 1
    assert report["all_completed"]["mrr"] == 0.2
    assert report["high_rated_ranked"]["count"] == 1
    assert report["neutral_rated_ranked"]["count"] == 0
    assert report["disliked_ranked"]["count"] == 0
    assert report["unrated_ranked"]["count"] == 0


def test_same_day_iso_runs_and_delayed_sync_start_of_day_cutoff(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = _init_db(tmp_path, monkeypatch, "cutoffs.db")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute(
            "INSERT INTO sources(id, name, url, kind, enabled) "
            "VALUES(999, 'Feed', 'https://example.com/feed', 'rss', 1)"
        )
        for cid, title in (
            (10, "Same Day Empty ReadAt"),
            (11, "Same Day Date ReadAt"),
            (12, "Delayed Sync Date ReadAt"),
            (13, "Default Timestamp Read"),
        ):
            con.execute(
                "INSERT INTO candidates(id, source_id, title, author, normalized_key, created_at) "
                "VALUES(?, 999, ?, 'Author', ?, '2026-09-22 08:00:00')",
                (cid, title, f"{title.lower()}|author"),
            )
            _accept_candidate(con, cid)

        # Candidate 10: empty read_at, SQLite space-separated created_at on 2026-10-10 18:00:00.
        # Morning ISO 'T' run must match despite 'T' > ' ' in raw ASCII order; evening run must not.
        con.execute(
            "INSERT INTO recommendation_runs(id, policy, policy_version, candidate_count, created_at) "
            "VALUES('run-morning', 'default', '1', 4, '2026-10-10T09:00:00+00:00')"
        )
        con.execute(
            "INSERT INTO recommendation_runs(id, policy, policy_version, candidate_count, created_at) "
            "VALUES('run-evening', 'default', '1', 4, '2026-10-10T21:00:00+00:00')"
        )
        con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at) "
            "VALUES('run-morning', 10, 4, 84.0, 1.0, '2026-10-10T09:00:00+00:00')"
        )
        con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at) "
            "VALUES('run-evening', 10, 1, 95.0, 1.0, '2026-10-10T21:00:00+00:00')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, source, created_at) "
            "VALUES(1, 'Same Day Empty ReadAt', 'Author', 5, NULL, 'goodreads_rss', '2026-10-10 18:00:00')"
        )

        # Candidate 11: date-only read_at on the same UTC day as reads.created_at uses reads.created_at.
        con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at) "
            "VALUES('run-morning', 11, 6, 79.0, 1.0, '2026-10-10T09:00:00+00:00')"
        )
        con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at) "
            "VALUES('run-evening', 11, 2, 91.0, 1.0, '2026-10-10T21:00:00+00:00')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, source, created_at) "
            "VALUES(2, 'Same Day Date ReadAt', 'Author', 4, '2026-10-10', 'goodreads_rss', '2026-10-10 18:00:00')"
        )

        # Candidate 12: delayed sync (read_at 2026-10-10, synced 2026-10-14) uses start-of-day UTC,
        # so both same-day (2026-10-10T09:00:00+00:00) and post-read pre-sync runs are excluded,
        # while a strictly prior-day run (2026-10-09T23:30:00+00:00) is kept.
        con.execute(
            "INSERT INTO recommendation_runs(id, policy, policy_version, candidate_count, created_at) "
            "VALUES('run-prior-day', 'default', '1', 4, '2026-10-09T23:30:00+00:00')"
        )
        con.execute(
            "INSERT INTO recommendation_runs(id, policy, policy_version, candidate_count, created_at) "
            "VALUES('run-between-read-and-sync', 'default', '1', 4, '2026-10-12T12:00:00+00:00')"
        )
        con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at) "
            "VALUES('run-prior-day', 12, 12, 72.0, 1.0, '2026-10-09T23:30:00+00:00')"
        )
        con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at) "
            "VALUES('run-morning', 12, 3, 88.0, 1.0, '2026-10-10T09:00:00+00:00')"
        )
        con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at) "
            "VALUES('run-between-read-and-sync', 12, 1, 94.0, 1.0, '2026-10-12T12:00:00+00:00')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, source, created_at) "
            "VALUES(3, 'Delayed Sync Date ReadAt', 'Author', 5, '2026-10-10', 'goodreads_rss', '2026-10-14 08:00:00')"
        )

        # Candidate 13: default SQLite CURRENT_TIMESTAMP read with an ISO 'T' run 1 microsecond earlier.
        con.execute(
            "INSERT INTO reads(id, title, author, rating, source) "
            "VALUES(4, 'Default Timestamp Read', 'Author', 5, 'manual')"
        )
        default_created = con.execute(
            "SELECT created_at FROM reads WHERE id = 4"
        ).fetchone()["created_at"]
        created_utc = datetime.fromisoformat(default_created).replace(tzinfo=timezone.utc)
        prior_iso = (created_utc - timedelta(microseconds=1)).isoformat()
        con.execute(
            "INSERT INTO recommendation_runs(id, policy, policy_version, candidate_count, created_at) "
            "VALUES('run-default-ts', 'default', '1', 4, ?)",
            (prior_iso,),
        )
        con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at) "
            "VALUES('run-default-ts', 13, 7, 80.5, 1.0, ?)",
            (prior_iso,),
        )

        # Non-empty malformed read_at must NOT fall back to created_at.
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, source) "
            "VALUES(5, 'Malformed Read Date', 'Author', 5, 'not-a-date', 'goodreads_rss')"
        )
        con.commit()
    finally:
        con.close()

    report = evaluate_organic_reads(db_path, since="2026-09-21")
    by_title = {item["title"]: item for item in report["reads"]}
    assert "Malformed Read Date" not in by_title
    assert by_title["Same Day Empty ReadAt"]["pre_read_rank"] == 4
    assert by_title["Same Day Date ReadAt"]["pre_read_rank"] == 6
    assert by_title["Delayed Sync Date ReadAt"]["pre_read_rank"] == 12
    assert by_title["Default Timestamp Read"]["pre_read_rank"] == 7


def test_pool_filters_ineligible_or_post_read_candidates_and_matches_audited_keys(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = _init_db(tmp_path, monkeypatch, "pool.db")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute(
            "INSERT INTO sources(id, name, url, kind, enabled) "
            "VALUES(999, 'Enabled Feed', 'https://example.com/enabled', 'rss', 1)"
        )
        con.execute(
            "INSERT INTO sources(id, name, url, kind, enabled) "
            "VALUES(998, 'Disabled Feed', 'https://example.com/disabled', 'rss', 0)"
        )

        # 1. Rejected quality_status candidate -> excluded
        con.execute(
            "INSERT INTO candidates(id, source_id, title, author, normalized_key, created_at) "
            "VALUES(20, 999, 'Rejected Book', 'Author R', 'rejectedbook|authorr', '2026-09-22 00:00:00')"
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status = 'rejected' WHERE candidate_id = 20"
        )

        # 2. Pending quality_status candidate (default trigger status) -> excluded
        con.execute(
            "INSERT INTO candidates(id, source_id, title, author, normalized_key, created_at) "
            "VALUES(21, 999, 'Pending Book', 'Author P', 'pendingbook|authorp', '2026-09-22 00:00:00')"
        )

        # 3. Disabled source candidate (even if accepted) -> excluded
        con.execute(
            "INSERT INTO candidates(id, source_id, title, author, normalized_key, created_at) "
            "VALUES(22, 998, 'Disabled Source Book', 'Author D', 'disabledsourcebook|authord', '2026-09-22 00:00:00')"
        )
        _accept_candidate(con, 22)

        # 4. Post-read candidate (created after delayed-sync read_at start-of-day) -> excluded from in_pool
        con.execute(
            "INSERT INTO candidates(id, source_id, title, author, normalized_key, created_at) "
            "VALUES(23, 999, 'Post Read Discovery', 'Author L', 'postreaddiscovery|authorl', '2026-10-02T10:00:00Z')"
        )
        _accept_candidate(con, 23)

        # 5. Candidate matched via audited quality_isbn13 when c.isbn13 is empty
        con.execute(
            "INSERT INTO candidates(id, source_id, title, author, normalized_key, created_at) "
            "VALUES(24, 999, 'Completely Different Feed Title', 'Wrong Byline', 'diff|wrong', '2026-09-22 00:00:00')"
        )
        _accept_candidate(con, 24, isbn13="9780143127741")

        # 6. Candidate matched via audited quality_work_id + read_metadata projection
        con.execute(
            "INSERT INTO candidates(id, source_id, title, author, normalized_key, created_at) "
            "VALUES(25, 999, 'Translated Feed Edition', 'Another Form', 'trans|form', '2026-09-22 00:00:00')"
        )
        _accept_candidate(con, 25, provider="openlibrary", work_id="/works/OL424242W")

        # 7. Candidate matched via Open Library work URL in c.source_url
        con.execute(
            "INSERT INTO candidates(id, source_id, title, author, source_url, normalized_key, created_at) "
            "VALUES(26, 999, 'Alt Catalog Entry', 'Alt Author', 'https://openlibrary.org/works/OL999999W', 'alt|alt', '2026-09-22 00:00:00')"
        )
        _accept_candidate(con, 26, provider="", work_id="")

        # 8. Candidate with work_id matched against stale read_metadata (bad identity_hash) -> must NOT match
        con.execute(
            "INSERT INTO candidates(id, source_id, title, author, normalized_key, created_at) "
            "VALUES(27, 999, 'Unrelated Candidate', 'Unrelated Author', 'unrel|unrel', '2026-09-22 00:00:00')"
        )
        _accept_candidate(con, 27, provider="openlibrary", work_id="/works/OL777777W")

        # Reads (using default SQLite reads.created_at)
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, source) "
            "VALUES(1, 'Rejected Book', 'Author R', 5, '2026-10-05', 'goodreads_rss')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, source) "
            "VALUES(2, 'Pending Book', 'Author P', 5, '2026-10-05', 'goodreads_rss')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, source) "
            "VALUES(3, 'Disabled Source Book', 'Author D', 5, '2026-10-05', 'goodreads_rss')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, source) "
            "VALUES(4, 'Post Read Discovery', 'Author L', 5, '2026-10-02', 'goodreads_rss')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, isbn, source) "
            "VALUES(5, 'Audited ISBN Read', 'Real Author', 4, '2026-10-05', '0143127748', 'goodreads_rss')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, source) "
            "VALUES(6, 'Metadata Work Read', 'Real Author Two', 5, '2026-10-05', 'goodreads_rss')"
        )
        read_6 = dict(con.execute("SELECT * FROM reads WHERE id = 6").fetchone())
        con.execute(
            "INSERT INTO read_metadata(read_id, verified_work_id, identity_provider, identity_provider_id, identity_hash) "
            "VALUES(6, '/works/OL424242W', 'openlibrary', '/works/OL424242W', ?)",
            (_read_metadata_identity_hash(read_6),),
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, source, openlibrary_work_id) "
            "VALUES(7, 'Source URL Work Read', 'Real Author Three', 4, '2026-10-05', 'goodreads_rss', '/works/OL999999W')"
        )
        con.execute(
            "INSERT INTO reads(id, title, author, rating, read_at, source) "
            "VALUES(8, 'Stale Metadata Read', 'Real Author Four', 4, '2026-10-05', 'goodreads_rss')"
        )
        con.execute(
            "INSERT INTO read_metadata(read_id, verified_work_id, identity_provider, identity_provider_id, identity_hash) "
            "VALUES(8, '/works/OL777777W', 'openlibrary', '/works/OL777777W', 'stale-hash')"
        )
        con.commit()
    finally:
        con.close()

    report = evaluate_organic_reads(db_path, since="2026-09-21")
    by_title = {item["title"]: item["in_pool"] for item in report["reads"]}
    assert by_title == {
        "Rejected Book": False,
        "Pending Book": False,
        "Disabled Source Book": False,
        "Post Read Discovery": False,
        "Audited ISBN Read": True,
        "Metadata Work Read": True,
        "Source URL Work Read": True,
        "Stale Metadata Read": False,
    }
    assert report["total_organic_reads"] == 8
    assert report["in_pool_count"] == 3


def test_separates_completion_and_rating_enjoyment_cohorts(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = _init_db(tmp_path, monkeypatch, "cohorts.db")

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO sources(id, name, url, kind, enabled) "
            "VALUES(999, 'Feed', 'https://example.com/feed', 'rss', 1)"
        )
        con.execute(
            "INSERT INTO recommendation_runs(id, policy, policy_version, candidate_count, created_at) "
            "VALUES('run-1', 'default', '1', 6, '2026-09-25T00:00:00Z')"
        )

        # (id, title, rating, pre_read_rank)
        fixtures = [
            (1, "Loved Five Star", 5.0, 2),
            (2, "Liked Four Star", 4.0, 8),
            (3, "Neutral Three Star", 3.0, 15),
            (4, "Disliked Two Star", 2.0, 20),
            (5, "Hated One Star", 1.0, 4),
            (6, "Unrated Completion", None, 6),
        ]
        for idx, title, rating, rank in fixtures:
            cand_id = 100 + idx
            con.execute(
                "INSERT INTO candidates(id, source_id, title, author, normalized_key, created_at) "
                "VALUES(?, 999, ?, 'Author', ?, '2026-09-22 00:00:00')",
                (cand_id, title, f"{title.lower()}|author"),
            )
            _accept_candidate(con, cand_id)
            con.execute(
                "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at) "
                "VALUES('run-1', ?, ?, 80.0, 1.0, '2026-09-25T00:00:00Z')",
                (cand_id, rank),
            )
            con.execute(
                "INSERT INTO reads(id, title, author, rating, read_at, source) "
                "VALUES(?, ?, 'Author', ?, '2026-10-05', 'goodreads_rss')",
                (idx, title, rating),
            )
        con.commit()
    finally:
        con.close()

    report = evaluate_organic_reads(db_path, since="2026-09-21")
    assert report["total_organic_reads"] == 6
    assert report["in_pool_count"] == 6
    assert report["pre_read_ranked_count"] == 6

    # Completion cohorts
    assert report["all_completed"]["count"] == 6
    assert report["all_completed"]["hit_at_8"] == 4  # ranks 2, 8, 4, 6
    assert report["unrated_ranked"] == {
        "count": 1,
        "median_rank": 6,
        "hit_at_8": 1,
        "hit_at_24": 1,
        "mrr": round(1.0 / 6.0, 4),
    }

    # Enjoyment cohorts: 1-star rank 4 hit must ONLY appear in disliked_ranked, never high_rated_ranked
    assert report["high_rated_ranked"] == {
        "count": 2,
        "median_rank": 5.0,
        "hit_at_8": 2,
        "hit_at_24": 2,
        "mrr": round((1.0 / 2.0 + 1.0 / 8.0) / 2.0, 4),
    }
    assert report["neutral_rated_ranked"] == {
        "count": 1,
        "median_rank": 15,
        "hit_at_8": 0,
        "hit_at_24": 1,
        "mrr": round(1.0 / 15.0, 4),
    }
    assert report["disliked_ranked"] == {
        "count": 2,
        "median_rank": 12.0,
        "hit_at_8": 1,
        "hit_at_24": 2,
        "mrr": round((1.0 / 20.0 + 1.0 / 4.0) / 2.0, 4),
    }
