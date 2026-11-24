import asyncio
import json
from pathlib import Path
import socket
import sqlite3
import sys

from fastapi.testclient import TestClient
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auth_helpers import authenticated_headers
from afterword_engine import database as db_module
from afterword_engine import ingestion
from afterword_engine.config import settings
from afterword_engine.database import (
    MIGRATIONS,
    _execute_migration_script,
    initialize,
    normalize_key,
)
from afterword_engine.secrets import seal
from afterword_engine.digest import digest_preview
from afterword_engine.ingestion import (
    goodreads_shelf_rss_url,
    import_goodreads_csv,
    import_goodreads_rss,
    import_goodreads_shelves,
    validate_goodreads_rss_url,
)
from afterword_engine.learning import create_recommendation_run
from afterword_engine.main import app
from afterword_engine.scoring import score_all
from afterword_engine.tenancy import profile_scope


def test_goodreads_shelf_rss_url_rewrites_shelf_param() -> None:
    read_url = "https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read"
    assert (
        goodreads_shelf_rss_url(read_url, "to-read")
        == "https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=to-read"
    )
    assert (
        goodreads_shelf_rss_url(read_url, "currently-reading")
        == "https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=currently-reading"
    )


def test_goodreads_read_feed_uses_only_the_read_shelf() -> None:
    url = "https://www.goodreads.com/review/list_rss/12345?key=secret"
    assert validate_goodreads_rss_url(url) == f"{url}&shelf=read"
    with pytest.raises(ValueError, match="read shelf"):
        validate_goodreads_rss_url(f"{url}&shelf=read&shelf=to-read")


def test_import_goodreads_shelves_stores_separately_from_reads(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "shelves.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    to_read_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Project Hail Mary</title>
        <author_name>Andy Weir</author_name>
        <isbn13>9780593135204</isbn13>
      </item>
    </channel></rss>"""
    reading_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Piranesi</title>
        <author_name>Susanna Clarke</author_name>
      </item>
    </channel></rss>"""

    async def fake_fetch_bytes(url: str, **kwargs):
        if "shelf=to-read" in url:
            return to_read_xml, "application/xml"
        if "shelf=currently-reading" in url:
            return reading_xml, "application/xml"
        raise AssertionError(f"Unexpected url: {url}")

    monkeypatch.setattr("afterword_engine.ingestion.fetch_bytes", fake_fetch_bytes)

    counts = asyncio.run(
        import_goodreads_shelves("https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read")
    )
    assert counts == {"to-read": 1, "currently-reading": 1}

    con = sqlite3.connect(db_path)
    try:
        reads_count = con.execute("SELECT COUNT(*) FROM reads").fetchone()[0]
        shelf_rows = con.execute(
            "SELECT shelf, source, title, author FROM goodreads_shelf_items ORDER BY shelf"
        ).fetchall()
    finally:
        con.close()

    assert reads_count == 0
    assert shelf_rows == [
        ("currently-reading", "goodreads_rss", "Piranesi", "Susanna Clarke"),
        ("to-read", "goodreads_rss", "Project Hail Mary", "Andy Weir"),
    ]


def test_score_all_computes_pure_scores_without_hiding_or_shelf_bonuses(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "score_shelves.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT OR IGNORE INTO sources(id, name, url, kind, enabled) "
            "VALUES(999, 'List', 'https://example.com', 'rss', 1)"
        )
        con.execute(
            "INSERT INTO reads(title, author, rating, source) "
            "VALUES('The Martian', 'Andy Weir', 5, 'goodreads_rss')"
        )
        for cid, title, author, status in [
            (10, "Project Hail Mary", "Andy Weir", "new"),
            (11, "Piranesi", "Susanna Clarke", "new"),
            (12, "Artemis", "Andy Weir", "rejected"),
        ]:
            con.execute(
                "INSERT INTO candidates(id, source_id, title, author, status, score, normalized_key) "
                "VALUES(?, 999, ?, ?, ?, 0, ?)",
                (cid, title, author, status, normalize_key(title, author)),
            )
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted', metadata_confidence=1.0 "
                "WHERE candidate_id=?",
                (cid,),
            )
        con.execute(
            "INSERT INTO feedback(candidate_id, action, previous_status) VALUES(12, 'reject', 'recommended')"
        )
        con.commit()
    finally:
        con.close()

    # First score pass without any shelf rows establishes pure baseline scores.
    asyncio.run(score_all())
    con = sqlite3.connect(db_path)
    try:
        baseline = {
            cid: (status, score, json.loads(explanation))
            for cid, status, score, explanation in con.execute(
                "SELECT id, status, score, explanation FROM candidates WHERE id IN (10, 11, 12) ORDER BY id"
            ).fetchall()
        }
        # Populate both Goodreads shelves to verify score_all neither hides
        # currently-reading nor adds a +10 bonus, and leaves rejected rows untouched.
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, title, author) "
            "VALUES('to-read', 'Project Hail Mary', 'Andy Weir')"
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, title, author) "
            "VALUES('currently-reading', 'Piranesi', 'Susanna Clarke')"
        )
        con.commit()
    finally:
        con.close()

    assert baseline[12][0] == "rejected"

    asyncio.run(score_all())

    con = sqlite3.connect(db_path)
    try:
        with_shelves = {
            cid: (status, score, json.loads(explanation))
            for cid, status, score, explanation in con.execute(
                "SELECT id, status, score, explanation FROM candidates WHERE id IN (10, 11, 12) ORDER BY id"
            ).fetchall()
        }
    finally:
        con.close()

    for cid in (10, 11):
        assert with_shelves[cid][0] == "recommended"
        assert with_shelves[cid][1] == baseline[cid][1]
        assert with_shelves[cid][1] > 0
        assert "On your Goodreads to-read shelf" not in with_shelves[cid][2]
    assert with_shelves[12] == ("rejected", 0.0, [])


def test_migration_27_recovers_hidden_rows_preserving_user_feedback_dedupes_shelves_and_isolates_profiles(
    tmp_path: Path, monkeypatch
) -> None:
    root_db = tmp_path / "afterword.db"
    monkeypatch.setattr(settings, "db", str(root_db))

    profile_a_id = "a" * 32
    profile_b_id = "b" * 32
    profile_a_db = tmp_path / "profiles" / f"{profile_a_id}.sqlite3"
    profile_b_db = tmp_path / "profiles" / f"{profile_b_id}.sqlite3"
    profile_a_db.parent.mkdir(parents=True, exist_ok=True)

    # Construct a genuine schema-v26 database for profile_a with dual-shelf
    # stale CSV rows, legacy currently-reading status='hidden' candidates, and
    # user-rejected / user-deferred candidates that must NOT be reset to 'new'.
    con = sqlite3.connect(profile_a_db)
    try:
        con.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, script in MIGRATIONS:
            if version >= 27:
                continue
            _execute_migration_script(con, script)
            con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
        con.execute(
            "INSERT INTO sources(id, name, url, kind, enabled) VALUES(999, 'Feed', 'https://example.com/rss', 'rss', 1)"
        )
        con.execute(
            "INSERT INTO reads(title, author, rating, source) VALUES('The Martian', 'Andy Weir', 5, 'goodreads_csv')"
        )
        seeded_candidates = [
            # 50: legacy currently-reading hidden candidate -> must reset to 'new'
            (50, "Piranesi", "Susanna Clarke", "hidden", 0.0, "[]"),
            # 51: user-rejected candidate -> must stay 'rejected'
            (51, "Rejected Novel", "Author R", "rejected", 79.5, '["Not for me"]'),
            # 52: user maybe_later candidate -> must stay 'maybe_later'
            (52, "Deferred Novel", "Author D", "maybe_later", 81.0, '["Later"]'),
            # 53: hidden candidate that also has an active non-undone user reject -> must NOT reset to 'new'
            (53, "Hidden After Reject", "Author H", "hidden", 0.0, "[]"),
            # 54: hidden candidate whose earlier reject was undone -> must reset to 'new'
            (54, "Undone Reject Novel", "Author U", "hidden", 0.0, "[]"),
        ]
        for cid, title, author, status, score, explanation in seeded_candidates:
            con.execute(
                "INSERT INTO candidates(id, source_id, title, author, status, score, explanation, normalized_key) "
                "VALUES(?, 999, ?, ?, ?, ?, ?, ?)",
                (cid, title, author, status, score, explanation, normalize_key(title, author)),
            )
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted', metadata_confidence=1.0 WHERE candidate_id=?",
                (cid,),
            )
        con.execute(
            "INSERT INTO feedback(id, candidate_id, action, previous_status) VALUES(1, 51, 'reject', 'recommended')"
        )
        con.execute(
            "INSERT INTO recommendation_events(event_key, candidate_id, event_type, source) "
            "VALUES('feedback:1', 51, 'reject', 'ui')"
        )
        con.execute(
            "INSERT INTO feedback(id, candidate_id, action, previous_status) VALUES(2, 52, 'maybe_later', 'recommended')"
        )
        con.execute(
            "INSERT INTO recommendation_events(event_key, candidate_id, event_type, source) "
            "VALUES('feedback:2', 52, 'maybe_later', 'ui')"
        )
        con.execute(
            "INSERT INTO feedback(id, candidate_id, action, previous_status) VALUES(3, 53, 'reject', 'recommended')"
        )
        con.execute(
            "INSERT INTO recommendation_events(event_key, candidate_id, event_type, source) "
            "VALUES('feedback:3', 53, 'reject', 'ui')"
        )
        con.execute(
            "INSERT INTO feedback(id, candidate_id, action, previous_status, undone_at) "
            "VALUES(4, 54, 'reject', 'recommended', '2026-01-15 00:00:00')"
        )
        con.execute(
            "INSERT INTO recommendation_events(event_key, candidate_id, event_type, source) "
            "VALUES('feedback:4', 54, 'reject', 'ui')"
        )
        con.execute(
            "INSERT INTO recommendation_events(event_key, candidate_id, event_type, source) "
            "VALUES('undo-feedback:4', 54, 'restore', 'ui_undo')"
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, title, author, isbn, updated_at) "
            "VALUES('currently-reading', 'Piranesi', 'Susanna Clarke', '9781635575637', '2026-01-01 00:00:00')"
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, title, author, isbn, updated_at) "
            "VALUES('to-read', 'Piranesi', 'Susanna Clarke', NULL, '2026-02-01 00:00:00')"
        )
        con.commit()
    finally:
        con.close()

    with profile_scope(profile_b_id):
        initialize(seed_demo=False)
        con_b = sqlite3.connect(profile_b_db)
        try:
            con_b.execute(
                "INSERT INTO goodreads_shelf_items(shelf, source, title, author) "
                "VALUES('currently-reading', 'goodreads_csv', 'Isolated Book', 'Other Author')"
            )
            con_b.commit()
        finally:
            con_b.close()

    with profile_scope(profile_a_id):
        initialize(seed_demo=False)
        con_a = sqlite3.connect(profile_a_db)
        try:
            migrated_shelves = con_a.execute(
                "SELECT shelf, source, title, author, isbn FROM goodreads_shelf_items"
            ).fetchall()
            migrated_statuses = {
                cid: (status, score)
                for cid, status, score in con_a.execute(
                    "SELECT id, status, score FROM candidates WHERE id IN (50, 51, 52, 53, 54) ORDER BY id"
                ).fetchall()
            }
        finally:
            con_a.close()

        assert migrated_shelves == [
            ("to-read", "goodreads_csv", "Piranesi", "Susanna Clarke", "9781635575637")
        ]
        assert migrated_statuses[50] == ("new", 0.0)
        assert migrated_statuses[51] == ("rejected", 79.5)
        assert migrated_statuses[52] == ("maybe_later", 81.0)
        assert migrated_statuses[53] == ("hidden", 0.0)
        assert migrated_statuses[54] == ("new", 0.0)

        asyncio.run(score_all())
        con_a = sqlite3.connect(profile_a_db)
        try:
            rescored = {
                cid: (status, score)
                for cid, status, score in con_a.execute(
                    "SELECT id, status, score FROM candidates WHERE id IN (50, 51, 52, 53, 54) ORDER BY id"
                ).fetchall()
            }
        finally:
            con_a.close()
        assert rescored[50][0] == "recommended" and rescored[50][1] > 0
        assert rescored[51] == ("rejected", 79.5)
        assert rescored[52] == ("maybe_later", 81.0)
        assert rescored[53] == ("hidden", 0.0)
        assert rescored[54][0] == "recommended" and rescored[54][1] > 0

    # Profile B remains completely isolated.
    con_b = sqlite3.connect(profile_b_db)
    try:
        b_shelves = con_b.execute(
            "SELECT shelf, source, title, author FROM goodreads_shelf_items"
        ).fetchall()
    finally:
        con_b.close()
    assert b_shelves == [("currently-reading", "goodreads_csv", "Isolated Book", "Other Author")]


def test_migration_27_tags_v26_rss_origin_rows_and_disconnect_clears_exclusions_and_priors(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "v26_rss_upgrade.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    rss_url = "https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read"

    # Build a populated schema-v26 database containing RSS-origin shelf rows
    # (written without a `source` column, matching v26 `_apply_goodreads_shelves`).
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, script in MIGRATIONS:
            if version >= 27:
                continue
            _execute_migration_script(con, script)
            con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
        con.execute(
            "INSERT INTO settings(key, value, secret) VALUES('goodreads_rss_url', ?, 1)",
            (seal(rss_url),),
        )
        con.execute(
            "INSERT INTO settings(key, value, secret) VALUES('goodreads_rss_last_sync_at', '2026-11-20T02:00:00+00:00', 0)"
        )
        con.execute(
            "INSERT INTO sources(id, name, url, kind, enabled) "
            "VALUES(999, 'Curated', 'https://example.com/feed', 'rss', 1)"
        )
        # Include both an early CSV read and a subsequent RSS read to reflect a
        # real installation that imported CSV history and then enabled RSS sync.
        con.execute(
            "INSERT INTO reads(title, author, rating, source) VALUES('Old CSV Book', 'Other Writer', 4, 'goodreads_csv')"
        )
        con.execute(
            "INSERT INTO reads(title, author, rating, source) VALUES('The Martian', 'Andy Weir', 5, 'goodreads_rss')"
        )
        con.executemany(
            "INSERT INTO goodreads_shelf_items(shelf, title, author, isbn, updated_at) VALUES(?, ?, ?, ?, ?)",
            [
                ("to-read", "Project Hail Mary", "Andy Weir", "9780593135204", "2026-11-20 02:00:00"),
                ("currently-reading", "Piranesi", "Susanna Clarke", "9781635575637", "2026-11-20 02:00:00"),
            ],
        )
        for cid, title, author, base_score in [
            (701, "Project Hail Mary", "Andy Weir", 88.0),
            (702, "Piranesi", "Susanna Clarke", 86.0),
            (703, "Artemis", "Andy Weir", 80.0),
        ]:
            con.execute(
                "INSERT INTO candidates(id, source_id, title, author, status, score, explanation, normalized_key) "
                "VALUES(?, 999, ?, ?, 'recommended', ?, '[\"Baseline match\"]', ?)",
                (cid, title, author, base_score, normalize_key(title, author)),
            )
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted', metadata_confidence=1.0 "
                "WHERE candidate_id=?",
                (cid,),
            )
        con.commit()
    finally:
        con.close()

    initialize(seed_demo=False)

    con = sqlite3.connect(db_path)
    try:
        migrated = con.execute(
            "SELECT shelf, source, title, author FROM goodreads_shelf_items ORDER BY shelf"
        ).fetchall()
    finally:
        con.close()
    assert migrated == [
        ("currently-reading", "goodreads_rss", "Piranesi", "Susanna Clarke"),
        ("to-read", "goodreads_rss", "Project Hail Mary", "Andy Weir"),
    ]

    with TestClient(app, headers=authenticated_headers()) as client:
        # Before disconnecting RSS: Project Hail Mary and Piranesi are dynamically
        # excluded, and Artemis receives the bounded +0.35 to-read author prior.
        before_disconnect = client.get("/api/recommendations").json()
        assert [item["id"] for item in before_disconnect] == [703]
        assert before_disconnect[0]["score"] == pytest.approx(80.35, abs=0.05)
        assert any(
            "Goodreads to-read shelf" in reason
            for reason in before_disconnect[0]["explanation"]
        )

        # Disconnect Goodreads RSS: all migrated RSS shelf rows must be removed
        # immediately so neither stale exclusions nor stale priors persist.
        disconnect_res = client.put("/api/settings/goodreads-rss", json={"url": ""})
        assert disconnect_res.status_code == 200
        assert disconnect_res.json() == {"saved": True, "configured": False}

        after_disconnect = client.get("/api/recommendations").json()
        by_id = {item["id"]: item for item in after_disconnect}
        assert set(by_id) == {701, 702, 703}
        assert by_id[703]["score"] == pytest.approx(80.0, abs=0.01)
        assert not any(
            "Goodreads to-read shelf" in reason
            for reason in by_id[703]["explanation"]
        )

    con = sqlite3.connect(db_path)
    try:
        remaining = con.execute("SELECT COUNT(*) FROM goodreads_shelf_items").fetchone()[0]
    finally:
        con.close()
    assert remaining == 0

    # Also verify a v26 DB where RSS had already been disconnected (`goodreads_rss_url=''`)
    # before upgrading purges orphaned v26 RSS shelf rows during Migration 27.
    disconnected_v26_db = tmp_path / "v26_already_disconnected.db"
    monkeypatch.setattr(settings, "db", str(disconnected_v26_db))
    con = sqlite3.connect(disconnected_v26_db)
    try:
        con.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, script in MIGRATIONS:
            if version >= 27:
                continue
            _execute_migration_script(con, script)
            con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
        con.execute(
            "INSERT INTO settings(key, value, secret) VALUES('goodreads_rss_url', '', 1)"
        )
        con.execute(
            "INSERT INTO settings(key, value, secret) VALUES('goodreads_rss_last_sync_at', '2026-11-18T02:00:00+00:00', 0)"
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, title, author) "
            "VALUES('to-read', 'Orphaned RSS Book', 'Author X')"
        )
        con.commit()
    finally:
        con.close()

    initialize(seed_demo=False)
    con = sqlite3.connect(disconnected_v26_db)
    try:
        orphaned_count = con.execute("SELECT COUNT(*) FROM goodreads_shelf_items").fetchone()[0]
    finally:
        con.close()
    assert orphaned_count == 0


def test_initialize_rejects_duplicate_migration_version_numbers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "db", str(tmp_path / "collision.db"))
    latest_version = MIGRATIONS[-1][0]
    monkeypatch.setattr(
        db_module,
        "MIGRATIONS",
        [*MIGRATIONS, (latest_version, "SELECT 1;")],
    )
    with pytest.raises(RuntimeError, match="unique sequential versions"):
        initialize(seed_demo=False)


def test_discovery_and_digest_exclude_shelves_across_editions_while_shortlist_and_impressions_stay_clean(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "serving_shelves.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT OR IGNORE INTO sources(id, name, url, kind, enabled) "
            "VALUES(999, 'Curated', 'https://example.com/feed', 'rss', 1)"
        )
        # Shelf items:
        # 1. Exact currently-reading match
        # 2. Base title on to-read matching a subtitled candidate
        # 3. UK edition ISBN matching an Open Library work via verified read/catalog work_id
        # 4. Title that user ALSO explicitly saved to Shortlist in Bookward
        # 5. Title that user ALSO imported to Librarr in Bookward
        con.executemany(
            "INSERT INTO goodreads_shelf_items(shelf, source, title, author, isbn, openlibrary_work_id) "
            "VALUES(?, 'goodreads_csv', ?, ?, ?, ?)",
            [
                ("currently-reading", "Piranesi", "Susanna Clarke", None, None),
                ("currently-reading", "Stranded Hidden Candidate", "Some Author", None, None),
                ("to-read", "Project Hail Mary", "Andy Weir", None, None),
                ("to-read", "UK Edition Queue Title", "Kazuo Ishiguro", "9780571364886", "/works/OL21636499W"),
                ("to-read", "Shortlisted Favorite", "Emily St. John Mandel", None, None),
                ("currently-reading", "Imported Waitlist Book", "N. K. Jemisin", None, None),
            ],
        )
        candidates = [
            # Suppressed: currently-reading exact match
            (101, "Piranesi", "Susanna Clarke", "recommended", 88.0, "", ""),
            # Suppressed: to-read subtitle variant (previously promoted from 73 -> 83 into Digest!)
            (102, "Project Hail Mary: A Novel", "Andy Weir", "recommended", 85.0, "", ""),
            # Suppressed: cross-edition Open Library work_id match
            (103, "Klara and the Sun", "Kazuo Ishiguro", "recommended", 87.0, "/works/OL21636499W", "openlibrary"),
            # Visible on Shortlist despite being on Goodreads to-read
            (104, "Shortlisted Favorite", "Emily St. John Mandel", "saved", 84.0, "", ""),
            # Visible in overview (include_imported=True) despite being on currently-reading
            (105, "Imported Waitlist Book", "N. K. Jemisin", "imported", 83.0, "", ""),
            # Never served: legacy hidden status at score=0
            (106, "Stranded Hidden Candidate", "Some Author", "hidden", 0.0, "", ""),
            # Served in Discovery and Digest: novel book
            (107, "The Ministry of Time", "Kaliane Bradley", "recommended", 86.0, "/works/OL999999W", "openlibrary"),
        ]
        for cid, title, author, status, score, work_id, provider in candidates:
            con.execute(
                "INSERT INTO candidates(id, source_id, title, author, status, score, explanation, normalized_key) "
                "VALUES(?, 999, ?, ?, ?, ?, '[\"Strong match\"]', ?)",
                (cid, title, author, status, score, normalize_key(title, author)),
            )
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted', metadata_confidence=1.0, "
                "work_id=?, provider=? WHERE candidate_id=?",
                (work_id, provider, cid),
            )
        con.commit()
    finally:
        con.close()

    with TestClient(app, headers=authenticated_headers()) as client:
        # Also inject a raw runtime hidden row after lifespan startup to verify
        # serving queries defensively exclude both recovered unscored rows (106)
        # and any raw status='hidden' rows (108).
        con = sqlite3.connect(db_path)
        try:
            con.execute(
                "INSERT INTO candidates(id, source_id, title, author, status, score, explanation, normalized_key) "
                "VALUES(108, 999, 'Runtime Hidden Candidate', 'Other Author', 'hidden', 0.0, '[\"Hidden\"]', ?)",
                (normalize_key("Runtime Hidden Candidate", "Other Author"),),
            )
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted', metadata_confidence=1.0 "
                "WHERE candidate_id=108"
            )
            con.commit()
        finally:
            con.close()

        rec_res = client.get("/api/recommendations")
        assert rec_res.status_code == 200
        rec_items = rec_res.json()
        assert [item["id"] for item in rec_items] == [107, 104]
        assert all(item["status"] != "hidden" for item in rec_items)

        rec_only_res = client.get("/api/recommendations", params={"status": "recommended"})
        assert rec_only_res.status_code == 200
        assert [item["id"] for item in rec_only_res.json()] == [107]

        saved_res = client.get("/api/recommendations", params={"status": "saved"})
        assert saved_res.status_code == 200
        assert [item["id"] for item in saved_res.json()] == [104, 105]

        imported_res = client.get("/api/recommendations", params={"status": "imported"})
        assert imported_res.status_code == 200
        assert [item["id"] for item in imported_res.json()] == [105]

        overview_res = client.get("/api/overview")
        assert overview_res.status_code == 200
        overview_ids = {item["id"] for item in overview_res.json()["recommendations"]}
        assert overview_ids == {107, 104, 105}

    preview = digest_preview({"minimum_score": 80, "maximum_books": 10, "only_new": True})
    assert [item["id"] for item in preview["items"]] == [107]

    # Direct defensive check on create_recommendation_run with a hidden item.
    direct_run_id = create_recommendation_run(
        [
            {"id": 108, "status": "hidden", "score": 0.0},
            {"id": 107, "status": "recommended", "score": 86.0},
        ]
    )

    con = sqlite3.connect(db_path)
    try:
        logged_impressions = con.execute(
            "SELECT candidate_id, score FROM recommendation_impressions ORDER BY id"
        ).fetchall()
        direct_impressions = con.execute(
            "SELECT candidate_id, score FROM recommendation_impressions WHERE run_id=?",
            (direct_run_id,),
        ).fetchall()
    finally:
        con.close()

    logged_ids = {cid for cid, _ in logged_impressions}
    assert logged_ids.isdisjoint({101, 102, 103, 106, 108})
    assert all(score > 0 for _, score in logged_impressions)
    assert direct_impressions == [(107, 86.0)]


def test_pausing_currently_reading_restores_discovery_and_completion_preserves_reading_history_rank_score(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "lifecycle_history.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT OR IGNORE INTO sources(id, name, url, kind, enabled) "
            "VALUES(999, 'List', 'https://example.com/list', 'rss', 1)"
        )
        con.execute(
            "INSERT INTO reads(title, author, rating, source) "
            "VALUES('The Martian', 'Andy Weir', 5, 'goodreads_csv')"
        )
        for cid, title, author, isbn13 in [
            (201, "Project Hail Mary", "Andy Weir", "9780593135204"),
            (202, "Piranesi", "Susanna Clarke", "9781635575637"),
        ]:
            con.execute(
                "INSERT INTO candidates(id, source_id, title, author, isbn13, status, normalized_key) "
                "VALUES(?, 999, ?, ?, ?, 'new', ?)",
                (cid, title, author, isbn13, normalize_key(title, author)),
            )
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted', metadata_confidence=1.0, isbn13=? "
                "WHERE candidate_id=?",
                (isbn13, cid),
            )
        con.commit()
    finally:
        con.close()

    # Reader puts both books on currently-reading via CSV, then scores.
    initial_csv = (
        "Title,Author,My Rating,Date Read,Date Added,Exclusive Shelf,ISBN13\n"
        "Project Hail Mary,Andy Weir,0,,2026/01/10,currently-reading,=\"9780593135204\"\n"
        "Piranesi,Susanna Clarke,0,,2026/02/01,currently-reading,=\"9781635575637\"\n"
    ).encode("utf-8")
    import_goodreads_csv(initial_csv)
    asyncio.run(score_all())

    con = sqlite3.connect(db_path)
    try:
        stored_scores = dict(
            con.execute("SELECT id, score FROM candidates WHERE id IN (201, 202)").fetchall()
        )
    finally:
        con.close()
    assert stored_scores[201] > 0
    assert stored_scores[202] > 0

    with TestClient(app, headers=authenticated_headers()) as client:
        # Both are dynamically excluded while on currently-reading.
        assert client.get("/api/recommendations").json() == []

        # Reader pauses/removes Project Hail Mary from currently-reading in a revised CSV.
        paused_csv = (
            "Title,Author,My Rating,Date Read,Date Added,Exclusive Shelf,ISBN13\n"
            "Piranesi,Susanna Clarke,0,,2026/02/01,currently-reading,=\"9781635575637\"\n"
        ).encode("utf-8")
        import_goodreads_csv(paused_csv)

        # Project Hail Mary is immediately eligible again at its computed score!
        restored_recs = client.get("/api/recommendations").json()
        assert [item["id"] for item in restored_recs] == [201]
        assert restored_recs[0]["score"] == stored_scores[201]

        # Reader finishes Project Hail Mary manually in Bookward and finishes Piranesi
        # via Goodreads CSV with a subtitle variant.
        mark_res = client.post("/api/recommendations/201/read", json={"rating": 5})
        assert mark_res.status_code == 200

        finished_csv = (
            "Title,Author,My Rating,Date Read,Date Added,Exclusive Shelf,ISBN13\n"
            "Piranesi: Special Anniversary Edition,Susanna Clarke,5,2026/03/01,2026/02/01,read,=\"9781635575637\"\n"
        ).encode("utf-8")
        import_goodreads_csv(finished_csv)

        history = client.get("/api/reading-history").json()
        by_title = {item["title"]: item for item in history}
        assert by_title["Project Hail Mary"]["rank_score"] == stored_scores[201]
        assert by_title["Piranesi: Special Anniversary Edition"]["rank_score"] == stored_scores[202]

    con = sqlite3.connect(db_path)
    try:
        remaining_shelves = con.execute("SELECT COUNT(*) FROM goodreads_shelf_items").fetchone()[0]
    finally:
        con.close()
    assert remaining_shelves == 0


def test_import_goodreads_csv_populates_replaces_and_prunes_shelf_items_across_editions(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "csv_shelves.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    # Seed an accepted Open Library candidate mapping UK & US editions to one work,
    # plus an existing RSS shelf item to verify cross-source completion pruning.
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT OR IGNORE INTO sources(id, name, url, kind, enabled) "
            "VALUES(999, 'OL', 'https://openlibrary.org', 'rss', 1)"
        )
        con.execute(
            "INSERT INTO candidates(id, source_id, title, author, isbn13, status, normalized_key) "
            "VALUES(301, 999, 'Children of Time', 'Adrian Tchaikovsky', '9780316452502', 'recommended', ?)",
            (normalize_key("Children of Time", "Adrian Tchaikovsky"),),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted', provider='openlibrary', "
            "work_id='/works/OL17352669W', isbn13='9780316452502' WHERE candidate_id=301"
        )
        con.execute(
            "INSERT INTO candidates(id, source_id, title, author, isbn13, status, normalized_key) "
            "VALUES(302, 999, 'Children of Time (UK)', 'Adrian Tchaikovsky', '9781447273288', 'recommended', ?)",
            (normalize_key("Children of Time (UK)", "Adrian Tchaikovsky"),),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted', provider='openlibrary', "
            "work_id='/works/OL17352669W', isbn13='9781447273288' WHERE candidate_id=302"
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, source, title, author, isbn) "
            "VALUES('to-read', 'goodreads_rss', 'Dune', 'Frank Herbert', '9780441172719')"
        )
        con.commit()
    finally:
        con.close()

    csv_first = (
        "Title,Author,My Rating,Date Read,Date Added,Exclusive Shelf,ISBN13\n"
        "Project Hail Mary,Andy Weir,0,,2026/01/10,currently-reading,=\"9780593135204\"\n"
        "Piranesi,Susanna Clarke,0,,2026/02/01,to-read,=\"9781635575637\"\n"
        "Children of Time,Adrian Tchaikovsky,0,,2026/02/05,to-read,=\"9781447273288\"\n"
        "The Martian,Andy Weir,5,2025/05/01,2025/04/01,read,=\"9780804139021\"\n"
    ).encode("utf-8")

    assert import_goodreads_csv(csv_first) == 1

    # Second CSV:
    # - Moves Project Hail Mary from currently-reading -> to-read (must leave 1 row, not 2!)
    # - Deletes Piranesi from queue entirely (must be removed!)
    # - Marks US edition of Children of Time as read under a different edition title (must prune UK shelf row via work_id!)
    # - Marks Dune: Deluxe Edition as read (must prune RSS Dune row via subtitle/ISBN!)
    csv_second = (
        "Title,Author,My Rating,Date Read,Date Added,Exclusive Shelf,ISBN13\n"
        "Project Hail Mary,Andy Weir,0,,2026/01/10,to-read,=\"9780593135204\"\n"
        "Children of Time: US Hardcover,Adrian Tchaikovsky,5,2026/03/10,2026/02/05,read,=\"9780316452502\"\n"
        "Dune: Deluxe Edition,Frank Herbert,5,2026/03/12,2026/01/01,read,=\"9780441172719\"\n"
    ).encode("utf-8")

    assert import_goodreads_csv(csv_second) == 2

    con = sqlite3.connect(db_path)
    try:
        shelves = con.execute(
            "SELECT shelf, source, title, author, isbn FROM goodreads_shelf_items ORDER BY source, shelf, title"
        ).fetchall()
    finally:
        con.close()

    assert shelves == [
        ("to-read", "goodreads_csv", "Project Hail Mary", "Andy Weir", "9780593135204"),
    ]


def test_rss_private_404_clears_stale_rss_preserving_csv_and_disconnect_clears_rss(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "rss_lifecycle.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    # 1. Import CSV fallback shelf row.
    csv_payload = (
        "Title,Author,My Rating,Date Read,Date Added,Exclusive Shelf,ISBN13\n"
        "CSV Fallback Book,Ursula K. Le Guin,0,,2026/01/05,to-read,=\"9780441478125\"\n"
    ).encode("utf-8")
    import_goodreads_csv(csv_payload)

    rss_url = "https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read"
    to_read_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>RSS Queued Book</title>
        <author_name>Octavia E. Butler</author_name>
        <isbn13>9780446675505</isbn13>
      </item>
    </channel></rss>"""
    reading_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>RSS Nightstand Book</title>
        <author_name>Ted Chiang</author_name>
      </item>
    </channel></rss>"""

    async def initial_fetch(url: str, **kwargs):
        if "shelf=to-read" in url:
            return to_read_xml, "application/xml"
        if "shelf=currently-reading" in url:
            return reading_xml, "application/xml"
        raise AssertionError(url)

    monkeypatch.setattr("afterword_engine.ingestion.fetch_bytes", initial_fetch)
    assert asyncio.run(import_goodreads_shelves(rss_url)) == {
        "to-read": 1,
        "currently-reading": 1,
    }

    # 2. Next RSS sync gets HTTP 404 on private to-read feed while currently-reading stays 200.
    async def private_to_read_fetch(url: str, **kwargs):
        if "shelf=to-read" in url:
            req = httpx.Request("GET", url)
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("404 Not Found", request=req, response=resp)
        if "shelf=currently-reading" in url:
            return reading_xml, "application/xml"
        raise AssertionError(url)

    monkeypatch.setattr("afterword_engine.ingestion.fetch_bytes", private_to_read_fetch)
    assert asyncio.run(import_goodreads_shelves(rss_url)) == {
        "to-read": 0,
        "currently-reading": 1,
    }

    con = sqlite3.connect(db_path)
    try:
        after_404 = con.execute(
            "SELECT shelf, source, title FROM goodreads_shelf_items ORDER BY source, shelf"
        ).fetchall()
    finally:
        con.close()
    assert after_404 == [
        ("to-read", "goodreads_csv", "CSV Fallback Book"),
        ("currently-reading", "goodreads_rss", "RSS Nightstand Book"),
    ]

    # 3. RSS read import with subtitle variant prunes matching shelf item across sources.
    read_feed_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>RSS Nightstand Book: Stories of Your Life</title>
        <author_name>Ted Chiang</author_name>
        <user_rating>5</user_rating>
      </item>
    </channel></rss>"""

    async def read_fetch(url: str, **kwargs):
        return read_feed_xml, "application/xml"

    monkeypatch.setattr("afterword_engine.ingestion.fetch_bytes", read_fetch)
    assert asyncio.run(import_goodreads_rss(rss_url)) == 1

    # Re-add an RSS shelf item, connect RSS in settings, then disconnect RSS (url="")
    # and verify RSS shelf rows are removed while CSV fallback rows remain.
    monkeypatch.setattr("afterword_engine.ingestion.fetch_bytes", initial_fetch)
    asyncio.run(import_goodreads_shelves(rss_url))

    with TestClient(app, headers=authenticated_headers()) as client:
        save_res = client.put("/api/settings/goodreads-rss", json={"url": rss_url})
        assert save_res.status_code == 200
        assert save_res.json() == {"saved": True, "configured": True}

        disconnect_res = client.put("/api/settings/goodreads-rss", json={"url": ""})
        assert disconnect_res.status_code == 200
        assert disconnect_res.json() == {"saved": True, "configured": False}

    con = sqlite3.connect(db_path)
    try:
        after_disconnect = con.execute(
            "SELECT shelf, source, title FROM goodreads_shelf_items ORDER BY source, shelf"
        ).fetchall()
    finally:
        con.close()
    assert after_disconnect == [
        ("to-read", "goodreads_csv", "CSV Fallback Book"),
    ]


def test_goodreads_imports_delete_matching_items_from_both_shelves_using_title_author_index(
    tmp_path: Path, monkeypatch
) -> None:
    from afterword_engine import ingestion
    from afterword_engine.ingestion import import_goodreads_csv, import_goodreads_rss

    db_path = tmp_path / "shelf_delete_index.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    con = sqlite3.connect(db_path)
    try:
        plan_details = [
            row[3]
            for row in con.execute(
                "EXPLAIN QUERY PLAN DELETE FROM goodreads_shelf_items WHERE title=? AND author=?",
                ("Project Hail Mary", "Andy Weir"),
            ).fetchall()
        ]
        shelf_plan_details = [
            row[3]
            for row in con.execute(
                "EXPLAIN QUERY PLAN DELETE FROM goodreads_shelf_items WHERE shelf=?",
                ("to-read",),
            ).fetchall()
        ]
        con.executemany(
            "INSERT INTO goodreads_shelf_items(shelf, source, title, author, isbn) VALUES(?, ?, ?, ?, ?)",
            [
                ("to-read", "goodreads_csv", "Project Hail Mary", "Andy Weir", "9780593135204"),
                ("currently-reading", "goodreads_rss", "Project Hail Mary", "Andy Weir", "9780593135204"),
                ("to-read", "goodreads_csv", "Artemis", "Andy Weir", None),
                ("to-read", "goodreads_csv", "Piranesi", "Susanna Clarke", "9781635575637"),
                ("currently-reading", "goodreads_rss", "Piranesi", "Susanna Clarke", "9781635575637"),
                ("to-read", "goodreads_csv", "Dune", "Frank Herbert", "9780441172719"),
            ],
        )
        con.commit()
    finally:
        con.close()

    assert any(
        "SEARCH goodreads_shelf_items USING INDEX idx_goodreads_shelf_items_title_author" in detail
        for detail in plan_details
    )
    assert any(
        "SEARCH goodreads_shelf_items USING INDEX idx_goodreads_shelf_items_shelf" in detail
        for detail in shelf_plan_details
    )

    csv_promotion = (
        "Title,Author,My Rating,Date Read,Date Added,Exclusive Shelf,ISBN13\n"
        "Project Hail Mary,Andy Weir,5,2026/03/10,2026/01/10,read,=\"9780593135204\"\n"
    ).encode("utf-8")
    assert import_goodreads_csv(csv_promotion) == 1

    rss_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Piranesi</title>
        <author_name>Susanna Clarke</author_name>
        <user_rating>5</user_rating>
        <user_read_at>2026/03/15</user_read_at>
        <isbn13>9781635575637</isbn13>
      </item>
    </channel></rss>"""

    async def fake_fetch_bytes(url: str, **kwargs):
        return rss_xml, "application/rss+xml"

    monkeypatch.setattr(ingestion, "fetch_bytes", fake_fetch_bytes)
    assert (
        asyncio.run(
            import_goodreads_rss("https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read")
        )
        == 1
    )

    con = sqlite3.connect(db_path)
    try:
        remaining = con.execute(
            "SELECT shelf, title, author FROM goodreads_shelf_items ORDER BY title, shelf"
        ).fetchall()
    finally:
        con.close()

    # Non-matching shelf rows by the same or other authors remain queued while
    # both to-read and currently-reading rows across CSV and RSS are deleted for promoted books.
    assert remaining == [
        ("to-read", "Artemis", "Andy Weir"),
        ("to-read", "Dune", "Frank Herbert"),
    ]


def test_import_goodreads_shelves_shares_client_fetches_concurrently_and_writes_in_order(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "concurrent_shelves.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    to_read_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Project Hail Mary</title>
        <author_name>Andy Weir</author_name>
      </item>
    </channel></rss>"""
    reading_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Piranesi</title>
        <author_name>Susanna Clarke</author_name>
      </item>
    </channel></rss>"""

    created_clients: list[httpx.AsyncClient] = []
    seen_clients: list[httpx.AsyncClient | None] = []
    in_flight = 0
    max_in_flight = 0
    original_source_client = ingestion._source_client

    def tracking_source_client() -> httpx.AsyncClient:
        client = original_source_client()
        created_clients.append(client)
        return client

    async def fake_fetch_bytes(url: str, allow_goodreads_http: bool = False, client=None):
        nonlocal in_flight, max_in_flight
        seen_clients.append(client)
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            if "shelf=to-read" in url:
                # Finish after currently-reading so rowid order verifies deterministic SQLite writes.
                await asyncio.sleep(0.06)
                return to_read_xml, "application/xml"
            if "shelf=currently-reading" in url:
                await asyncio.sleep(0.02)
                return reading_xml, "application/xml"
            raise AssertionError(f"Unexpected url: {url}")
        finally:
            in_flight -= 1

    monkeypatch.setattr(ingestion, "_source_client", tracking_source_client)
    monkeypatch.setattr(ingestion, "fetch_bytes", fake_fetch_bytes)

    counts = asyncio.run(
        import_goodreads_shelves("https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read")
    )
    assert counts == {"to-read": 1, "currently-reading": 1}
    assert len(created_clients) == 1
    assert seen_clients == [created_clients[0], created_clients[0]]
    assert max_in_flight == 2

    con = sqlite3.connect(db_path)
    try:
        ordered_shelves = [
            shelf
            for (shelf,) in con.execute(
                "SELECT shelf FROM goodreads_shelf_items ORDER BY id ASC"
            ).fetchall()
        ]
    finally:
        con.close()

    assert ordered_shelves == ["to-read", "currently-reading"]


def test_import_goodreads_shelves_preserves_existing_rows_on_http_404(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "private_shelves.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, title, author) "
            "VALUES('to-read', 'Kept To Read', 'Author One')"
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, source, title, author) "
            "VALUES('currently-reading', 'goodreads_rss', 'Old Reading', 'Author Two')"
        )
        con.commit()
    finally:
        con.close()

    reading_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>New Reading</title>
        <author_name>Author Three</author_name>
      </item>
    </channel></rss>"""

    async def handler(request: httpx.Request) -> httpx.Response:
        shelf = request.url.params.get("shelf")
        if shelf == "to-read":
            return httpx.Response(404, content=b"Not Found", headers={"content-type": "text/plain"})
        if shelf == "currently-reading":
            return httpx.Response(200, content=reading_xml, headers={"content-type": "application/rss+xml"})
        raise AssertionError(f"Unexpected shelf: {shelf}")

    monkeypatch.setattr(
        ingestion,
        "_source_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=settings.source_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ),
    )

    counts = asyncio.run(
        import_goodreads_shelves("https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read")
    )
    assert counts == {"to-read": 0, "currently-reading": 1}

    con = sqlite3.connect(db_path)
    try:
        shelf_rows = con.execute(
            "SELECT shelf, title, author FROM goodreads_shelf_items ORDER BY shelf"
        ).fetchall()
    finally:
        con.close()

    assert shelf_rows == [
        ("currently-reading", "New Reading", "Author Three"),
        ("to-read", "Kept To Read", "Author One"),
    ]

def test_import_storygraph_csv_populates_shelves_skips_dnf_and_clears_completed_reads(
    tmp_path: Path, monkeypatch
) -> None:
    from afterword_engine.ingestion import import_csv

    db_path = tmp_path / "storygraph_shelves.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    csv_data = (
        "Title,Authors,Contributors,ISBN/UID,Format,Read Status,Date Added,Last Date Read,Dates Read,Star Rating\n"
        'Project Hail Mary,Andy Weir,,9780593135204,hardcover,to-read,2026/01/10,,,\n'
        'Piranesi,"Susanna Clarke, Coauthor",,b4f901a2-7c4d-4f1b-9a2e-1234567890ab,digital,currently-reading,2026/02/01,,,\n'
        'Abandoned Tome,Some Writer,,9780306406157,paperback,did-not-finish,2026/02/10,2026/02/15,2026/02/10-2026/02/15,1.5\n'
        'The Martian,Andy Weir,,0553418025,paperback,read,2025/04/01,2025/05/01,2025/04/20-2025/05/01,4.75\n'
    ).encode("utf-8")

    result = import_csv(csv_data)
    assert result == {"imported": 1, "source": "storygraph_csv"}

    con = sqlite3.connect(db_path)
    try:
        reads_rows = con.execute(
            "SELECT title, author, rating, read_at, isbn, source FROM reads"
        ).fetchall()
        shelves = con.execute(
            "SELECT shelf, title, author, isbn FROM goodreads_shelf_items ORDER BY shelf"
        ).fetchall()
        shortlist_count = con.execute(
            "SELECT COUNT(*) FROM candidates WHERE status='saved'"
        ).fetchone()[0]
    finally:
        con.close()

    assert reads_rows == [
        ("The Martian", "Andy Weir", 4.75, "2025/05/01", "9780553418026", "storygraph_csv")
    ]
    assert shelves == [
        ("currently-reading", "Piranesi", "Susanna Clarke", None),
        ("to-read", "Project Hail Mary", "Andy Weir", "9780593135204"),
    ]
    assert shortlist_count == 0

    followup_csv = (
        "Title,Authors,ISBN/UID,Read Status,Last Date Read,Star Rating\n"
        "Project Hail Mary,Andy Weir,9780593135204,read,2026/03/01,5.0\n"
    ).encode("utf-8")
    assert import_csv(followup_csv) == {"imported": 1, "source": "storygraph_csv"}

    con = sqlite3.connect(db_path)
    try:
        remaining_shelves = con.execute(
            "SELECT shelf, title, author FROM goodreads_shelf_items"
        ).fetchall()
    finally:
        con.close()

    assert remaining_shelves == [
        ("currently-reading", "Piranesi", "Susanna Clarke"),
    ]

