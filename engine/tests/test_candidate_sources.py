import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import numpy as np
import pytest
import respx
from fastapi.testclient import TestClient

from auth_helpers import authenticated_headers

from afterword_engine import profiles
from afterword_engine.associations import Association, persist_associations
from afterword_engine.config import settings
from afterword_engine.database import (
    MIGRATIONS,
    initialize,
    normalize_key,
    row,
    rows,
    transaction,
)
from afterword_engine.digest import _candidate_rows, digest_config
from afterword_engine.ingestion import refresh_read_work_identities, scan_source
from afterword_engine.main import app, toggle_source
from afterword_engine.quality import _dedupe_and_hide
from afterword_engine.scoring import score_all


class FakeEmbedder:
    name = "fake"
    model = "fake-v1"

    async def embed(self, texts: list[str]) -> np.ndarray:
        vectors = []
        for text in texts:
            seed = sum(ord(ch) for ch in text) % 97
            vec = np.array(
                [1.0, (seed + 1) / 100.0, ((seed * 3) % 89 + 1) / 100.0, 0.5],
                dtype=np.float32,
            )
            vec /= np.linalg.norm(vec)
            vectors.append(vec)
        return np.vstack(vectors) if vectors else np.zeros((0, 4), dtype=np.float32)


@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "candidate-sources.db")
    initialize(seed_demo=False)
    profiles.initialize_auth()
    return settings.db


def test_migration_backfills_candidate_sources_cascades_delete_and_recovers_duplicates(
    tmp_path: Path,
):
    db_path = tmp_path / "upgrade-v26.db"
    settings.db = str(db_path)

    with sqlite3.connect(db_path) as con:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, "
            "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, script in MIGRATIONS:
            if version >= 27:
                break
            con.executescript(script)
            con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))

        disabled_sid = con.execute(
            "INSERT INTO sources(name,url,enabled,weight) VALUES(?,?,0,1.5) RETURNING id",
            ("Disabled Feed", "https://example.com/disabled"),
        ).fetchone()[0]
        enabled_sid = con.execute(
            "INSERT INTO sources(name,url,enabled,weight) VALUES(?,?,1,1.0) RETURNING id",
            ("Enabled Feed", "https://example.com/enabled"),
        ).fetchone()[0]
        assoc_sid = con.execute(
            "INSERT INTO sources(name,url,kind,enabled) VALUES(?,?,?,0) RETURNING id",
            ("Association · openlibrary_lists", "association://openlibrary_lists", "association"),
        ).fetchone()[0]
        read_id = con.execute(
            "INSERT INTO reads(title,author,rating,source) VALUES(?,?,5,'manual') RETURNING id",
            ("Seed Book", "Seed Author"),
        ).fetchone()[0]

        disabled_winner = con.execute(
            "INSERT INTO candidates(title,author,source_url,source_id,status,normalized_key,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) RETURNING id",
            (
                "Shared Work (Collector Edition)",
                "Ada Writer",
                "https://example.com/disabled/work",
                disabled_sid,
                "new",
                "shared work collector edition ada writer",
                "2026-01-02 03:04:05",
                "2026-01-05 06:07:08",
            ),
        ).fetchone()[0]
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.95,"
            "provider='openlibrary',work_id='/works/OLMIG1' WHERE candidate_id=?",
            (disabled_winner,),
        )

        recoverable_sibling = con.execute(
            "INSERT INTO candidates(title,author,source_url,source_id,status,normalized_key) "
            "VALUES(?,?,?,?,?,?) RETURNING id",
            (
                "Shared Work",
                "Ada Writer",
                "https://example.com/enabled/work",
                enabled_sid,
                "rejected",
                "shared work ada writer",
            ),
        ).fetchone()[0]
        con.execute(
            "UPDATE candidate_quality SET quality_status='rejected',quality_score=0.82,"
            "flags_json='[\"duplicate_identity\"]',provider='openlibrary',work_id='/works/OLMIG1' "
            "WHERE candidate_id=?",
            (recoverable_sibling,),
        )

        user_rejected_sibling = con.execute(
            "INSERT INTO candidates(title,author,source_url,source_id,status,normalized_key) "
            "VALUES(?,?,?,?,?,?) RETURNING id",
            (
                "Shared Work (Paperback)",
                "Ada Writer",
                "https://example.com/enabled/work-pb",
                enabled_sid,
                "rejected",
                "shared work paperback ada writer",
            ),
        ).fetchone()[0]
        con.execute(
            "UPDATE candidate_quality SET quality_status='rejected',quality_score=0.90,"
            "flags_json='[\"duplicate_identity\"]',provider='openlibrary',work_id='/works/OLMIG1' "
            "WHERE candidate_id=?",
            (user_rejected_sibling,),
        )
        con.execute(
            "INSERT INTO feedback(candidate_id,action,previous_status) VALUES(?,?,?)",
            (user_rejected_sibling, "reject", "recommended"),
        )

        cascade_cid = con.execute(
            "INSERT INTO candidates(title,author,source_url,source_id,status,normalized_key) "
            "VALUES(?,?,?,?,?,?) RETURNING id",
            (
                "Cascade Probe",
                "Temp Author",
                "https://example.com/enabled/cascade",
                enabled_sid,
                "new",
                "cascade probe temp author",
            ),
        ).fetchone()[0]
        con.execute(
            "INSERT INTO association_evidence("
            "provider,seed_read_id,candidate_id,external_id,source_url,fetched_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                "openlibrary_lists",
                read_id,
                cascade_cid,
                "/works/OLCASCADE",
                "https://openlibrary.org/works/OLCASCADE",
                "2026-02-01 00:00:00",
            ),
        )

    initialize(seed_demo=False)

    assert {item["version"] for item in rows("SELECT version FROM schema_migrations")} >= {27}
    winner_membership = row(
        "SELECT * FROM candidate_sources WHERE candidate_id=? AND source_id=?",
        (disabled_winner, disabled_sid),
    )
    assert winner_membership is not None
    assert winner_membership["source_url"] == "https://example.com/disabled/work"
    assert winner_membership["first_seen_at"] == "2026-01-02 03:04:05"
    assert winner_membership["last_seen_at"] == "2026-01-05 06:07:08"

    cascade_memberships = {
        int(item["source_id"])
        for item in rows(
            "SELECT source_id FROM candidate_sources WHERE candidate_id=?",
            (cascade_cid,),
        )
    }
    assert cascade_memberships == {enabled_sid, assoc_sid}
    assert row("SELECT source_id FROM candidates WHERE id=?", (cascade_cid,))["source_id"] == enabled_sid

    assert row("SELECT status FROM candidates WHERE id=?", (recoverable_sibling,))["status"] == "new"
    recovered_q = row("SELECT quality_status,flags_json FROM candidate_quality WHERE candidate_id=?", (recoverable_sibling,))
    assert recovered_q == {"quality_status": "accepted", "flags_json": "[]"}

    assert row("SELECT status FROM candidates WHERE id=?", (disabled_winner,))["status"] == "rejected"
    assert json.loads(
        row("SELECT flags_json FROM candidate_quality WHERE candidate_id=?", (disabled_winner,))["flags_json"]
    ) == ["duplicate_identity"]

    assert row("SELECT status FROM candidates WHERE id=?", (user_rejected_sibling,))["status"] == "rejected"

    with transaction() as con:
        con.execute("DELETE FROM candidates WHERE id=?", (cascade_cid,))
    assert rows("SELECT * FROM candidate_sources WHERE candidate_id=?", (cascade_cid,)) == []


def test_scan_source_and_associations_promote_highest_weight_enabled_non_association_source(
    database, monkeypatch
):
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(id,title,author,rating,source) VALUES(1,'Seed','Author',5,'manual')"
        )
        disabled_feed_id = con.execute(
            "INSERT INTO sources(name,url,enabled,weight) VALUES(?,?,0,2.0) RETURNING id",
            ("Disabled High Weight", "https://example.com/disabled-high"),
        ).fetchone()[0]
        early_feed_id = con.execute(
            "INSERT INTO sources(name,url,enabled,weight) VALUES(?,?,1,1.2) RETURNING id",
            ("Early Enabled Feed", "https://example.com/early-enabled"),
        ).fetchone()[0]
        tied_feed_id = con.execute(
            "INSERT INTO sources(name,url,enabled,weight) VALUES(?,?,1,1.2) RETURNING id",
            ("Later Tied Feed", "https://example.com/later-tied"),
        ).fetchone()[0]
        heavier_feed_id = con.execute(
            "INSERT INTO sources(name,url,enabled,weight) VALUES(?,?,1,1.8) RETURNING id",
            ("Heavier Enabled Feed", "https://example.com/heavier-enabled"),
        ).fetchone()[0]

    persist_associations(
        [
            Association(
                provider="openlibrary_lists",
                seed_read_id=1,
                external_id="/works/OLSHARED",
                title="Shared Discovery",
                author="Clara Novelist",
                source_url="https://openlibrary.org/works/OLSHARED",
            )
        ]
    )
    assoc_source = row("SELECT id,enabled,kind FROM sources WHERE url='association://openlibrary_lists'")
    assert assoc_source["enabled"] == 0
    assert assoc_source["kind"] == "association"

    key = normalize_key("Shared Discovery", "Clara Novelist")
    candidate = row("SELECT id,source_id FROM candidates WHERE normalized_key=?", (key,))
    assert candidate["source_id"] == assoc_source["id"]

    async def fake_fetch(url):
        return "application/json", [
            {
                "title": "Shared Discovery",
                "author": "Clara Novelist",
                "source_url": f"{url}/book-1",
            }
        ]

    async def no_enrichment(items):
        return items

    monkeypatch.setattr("afterword_engine.ingestion._fetch_and_parse_source", fake_fetch)
    monkeypatch.setattr("afterword_engine.ingestion.enrich_book_metadata", no_enrichment)

    # Scanning a disabled source records membership and beats association among disabled sources,
    # while still keeping candidate non-active until an enabled feed scans it.
    assert asyncio.run(scan_source(row("SELECT * FROM sources WHERE id=?", (disabled_feed_id,)))) == 1
    assert row("SELECT source_id FROM candidates WHERE id=?", (candidate["id"],))["source_id"] == disabled_feed_id

    # Scanning an enabled feed promotes candidates.source_id to the enabled feed.
    assert asyncio.run(scan_source(row("SELECT * FROM sources WHERE id=?", (tied_feed_id,)))) == 1
    assert row("SELECT source_id FROM candidates WHERE id=?", (candidate["id"],))["source_id"] == tied_feed_id

    # Scanning an earlier (lower-id) enabled feed with equal weight wins the tie-breaker.
    assert asyncio.run(scan_source(row("SELECT * FROM sources WHERE id=?", (early_feed_id,)))) == 1
    assert row("SELECT source_id FROM candidates WHERE id=?", (candidate["id"],))["source_id"] == early_feed_id

    with transaction() as con:
        con.execute(
            "UPDATE candidate_sources SET first_seen_at='2026-01-01 00:00:00', "
            "last_seen_at='2026-01-01 00:00:00' WHERE candidate_id=? AND source_id=?",
            (candidate["id"], early_feed_id),
        )
    assert asyncio.run(scan_source(row("SELECT * FROM sources WHERE id=?", (early_feed_id,)))) == 1
    early_link = row(
        "SELECT first_seen_at,last_seen_at FROM candidate_sources WHERE candidate_id=? AND source_id=?",
        (candidate["id"], early_feed_id),
    )
    assert early_link["first_seen_at"] == "2026-01-01 00:00:00"
    assert early_link["last_seen_at"] != "2026-01-01 00:00:00"

    # Scanning a higher-weight enabled feed promotes candidates.source_id to the heavier feed.
    assert asyncio.run(scan_source(row("SELECT * FROM sources WHERE id=?", (heavier_feed_id,)))) == 1
    assert row("SELECT source_id FROM candidates WHERE id=?", (candidate["id"],))["source_id"] == heavier_feed_id

    # Subsequent association persistence refreshes association membership without stealing source_id.
    persist_associations(
        [
            Association(
                provider="openlibrary_lists",
                seed_read_id=1,
                external_id="/works/OLSHARED",
                title="Shared Discovery",
                author="Clara Novelist",
                source_url="https://openlibrary.org/works/OLSHARED",
            )
        ]
    )
    assert row("SELECT source_id FROM candidates WHERE id=?", (candidate["id"],))["source_id"] == heavier_feed_id
    all_source_ids = {
        int(item["source_id"])
        for item in rows(
            "SELECT source_id FROM candidate_sources WHERE candidate_id=?",
            (candidate["id"],),
        )
    }
    assert all_source_ids == {
        assoc_source["id"],
        disabled_feed_id,
        early_feed_id,
        tied_feed_id,
        heavier_feed_id,
    }


def test_dedupe_and_hide_ranks_enabled_ahead_of_disabled_and_recovers_siblings(database):
    with transaction() as con:
        disabled_sid = con.execute(
            "INSERT INTO sources(name,url,enabled) VALUES('Disabled','https://example.com/d',0) RETURNING id"
        ).fetchone()[0]
        enabled_sid = con.execute(
            "INSERT INTO sources(name,url,enabled) VALUES('Enabled','https://example.com/e',1) RETURNING id"
        ).fetchone()[0]

        disabled_cid = con.execute(
            "INSERT INTO candidates(title,author,description,cover_url,source_id,status,normalized_key) "
            "VALUES('Work Deluxe','Writer','Full synopsis','https://example.com/c.jpg',?,'new','work deluxe writer') RETURNING id",
            (disabled_sid,),
        ).fetchone()[0]
        enabled_cid = con.execute(
            "INSERT INTO candidates(title,author,source_id,status,normalized_key) "
            "VALUES('Work Standard','Writer',?,'new','work standard writer') RETURNING id",
            (enabled_sid,),
        ).fetchone()[0]

        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.99,"
            "provider='openlibrary',work_id='/works/OLDEDUPE' WHERE candidate_id=?",
            (disabled_cid,),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.70,"
            "provider='openlibrary',work_id='/works/OLDEDUPE' WHERE candidate_id=?",
            (enabled_cid,),
        )

        changed = _dedupe_and_hide(con)
        assert changed == 1

    assert row("SELECT status FROM candidates WHERE id=?", (enabled_cid,))["status"] == "new"
    assert row("SELECT quality_status FROM candidate_quality WHERE candidate_id=?", (enabled_cid,))["quality_status"] == "accepted"
    assert row("SELECT status FROM candidates WHERE id=?", (disabled_cid,))["status"] == "rejected"

    # Simulate an enabled winner later becoming disabled while an undone-reject sibling and
    # an active user-rejected sibling exist on enabled sources.
    with transaction() as con:
        other_enabled_sid = con.execute(
            "INSERT INTO sources(name,url,enabled) VALUES('Other Enabled','https://example.com/e2',1) RETURNING id"
        ).fetchone()[0]
        undone_sibling = con.execute(
            "INSERT INTO candidates(title,author,source_id,status,normalized_key) "
            "VALUES('Work Audio','Writer',?,'rejected','work audio writer') RETURNING id",
            (other_enabled_sid,),
        ).fetchone()[0]
        con.execute(
            "UPDATE candidate_quality SET quality_status='rejected',quality_score=0.80,"
            "flags_json='[\"duplicate_identity\"]',provider='openlibrary',work_id='/works/OLDEDUPE' "
            "WHERE candidate_id=?",
            (undone_sibling,),
        )
        con.execute(
            "INSERT INTO feedback(candidate_id,action,previous_status,undone_at) "
            "VALUES(?,'reject','recommended','2026-03-01 00:00:00')",
            (undone_sibling,),
        )

        rejected_sibling = con.execute(
            "INSERT INTO candidates(title,author,source_id,status,normalized_key) "
            "VALUES('Work Hardcover','Writer',?,'rejected','work hardcover writer') RETURNING id",
            (other_enabled_sid,),
        ).fetchone()[0]
        con.execute(
            "UPDATE candidate_quality SET quality_status='rejected',quality_score=0.95,"
            "flags_json='[\"duplicate_identity\"]',provider='openlibrary',work_id='/works/OLDEDUPE' "
            "WHERE candidate_id=?",
            (rejected_sibling,),
        )
        con.execute(
            "INSERT INTO feedback(candidate_id,action,previous_status) VALUES(?,'reject','recommended')",
            (rejected_sibling,),
        )

        con.execute("UPDATE sources SET enabled=0 WHERE id=?", (enabled_sid,))
        changed_after_disable = _dedupe_and_hide(con)
        assert changed_after_disable == 2
        assert _dedupe_and_hide(con) == 0

    assert row("SELECT status FROM candidates WHERE id=?", (undone_sibling,))["status"] == "new"
    assert row("SELECT quality_status FROM candidate_quality WHERE candidate_id=?", (undone_sibling,))["quality_status"] == "accepted"
    assert row("SELECT status FROM candidates WHERE id=?", (rejected_sibling,))["status"] == "rejected"
    assert row("SELECT status FROM candidates WHERE id=?", (enabled_cid,))["status"] == "rejected"


def test_chart_pruning_preserves_shared_one_time_and_association_candidates(
    database, monkeypatch
):
    items_by_url: dict[str, list[dict]] = {
        "https://example.com/chart-a": [
            {"title": "Shared One-Time Book", "author": "Author One"},
            {"title": "Shared Assoc Book", "author": "Author Two"},
            {"title": "Chart Only Unread", "author": "Author Three"},
            {"title": "Chart Only Saved", "author": "Author Four"},
        ],
        "https://example.com/one-time-import": [
            {"title": "Shared One-Time Book", "author": "Author One"},
        ],
    }

    async def fake_fetch(url):
        return "application/json", list(items_by_url[url])

    async def no_enrichment(items):
        return items

    monkeypatch.setattr("afterword_engine.ingestion._fetch_and_parse_source", fake_fetch)
    monkeypatch.setattr("afterword_engine.ingestion.enrich_book_metadata", no_enrichment)

    with transaction() as con:
        con.execute(
            "INSERT INTO reads(id,title,author,rating,source) VALUES(1,'Seed','Author',5,'manual')"
        )
        chart_id = con.execute(
            "INSERT INTO sources(name,url,enabled,weight,lifecycle) VALUES(?,?,1,1.8,'permanent') RETURNING id",
            ("Weekly Chart", "https://example.com/chart-a"),
        ).fetchone()[0]
        one_time_id = con.execute(
            "INSERT INTO sources(name,url,enabled,weight,lifecycle) VALUES(?,?,1,1.0,'one_time') RETURNING id",
            ("One-Time Shelf", "https://example.com/one-time-import"),
        ).fetchone()[0]

    assert asyncio.run(scan_source(row("SELECT * FROM sources WHERE id=?", (one_time_id,)))) == 1
    assert asyncio.run(scan_source(row("SELECT * FROM sources WHERE id=?", (chart_id,)))) == 4
    persist_associations(
        [
            Association(
                provider="openlibrary_lists",
                seed_read_id=1,
                external_id="/works/OLASSOC2",
                title="Shared Assoc Book",
                author="Author Two",
                source_url="https://openlibrary.org/works/OLASSOC2",
            )
        ]
    )
    assoc_id = row("SELECT id FROM sources WHERE url='association://openlibrary_lists'")["id"]

    shared_one_time = row(
        "SELECT id,source_id FROM candidates WHERE title='Shared One-Time Book'"
    )
    shared_assoc = row(
        "SELECT id,source_id FROM candidates WHERE title='Shared Assoc Book'"
    )
    chart_unread = row(
        "SELECT id FROM candidates WHERE title='Chart Only Unread'"
    )
    chart_saved = row(
        "SELECT id FROM candidates WHERE title='Chart Only Saved'"
    )
    assert shared_one_time["source_id"] == chart_id
    assert shared_assoc["source_id"] == chart_id

    with transaction() as con:
        for cid, work_id in (
            (shared_one_time["id"], "/works/OLOT1"),
            (shared_assoc["id"], "/works/OLASSOC2"),
            (chart_unread["id"], "/works/OLUNREAD3"),
        ):
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.91,"
                "provider='openlibrary',work_id=? WHERE candidate_id=?",
                (work_id, cid),
            )
            con.execute(
                "UPDATE candidates SET status='recommended',score=88.0 WHERE id=?",
                (cid,),
            )
        con.execute("UPDATE candidates SET status='saved' WHERE id=?", (chart_saved["id"],))

    # Rotate Chart A to a completely new item.
    items_by_url["https://example.com/chart-a"] = [
        {"title": "Fresh Chart Arrival", "author": "Author Five"}
    ]
    assert asyncio.run(scan_source(row("SELECT * FROM sources WHERE id=?", (chart_id,)))) == 1

    # Shared book backed by enabled one_time source stays recommended and transfers source_id.
    after_one_time = row("SELECT source_id,status,score FROM candidates WHERE id=?", (shared_one_time["id"],))
    assert after_one_time == {
        "source_id": one_time_id,
        "status": "recommended",
        "score": 88.0,
    }
    assert row("SELECT quality_status,work_id FROM candidate_quality WHERE candidate_id=?", (shared_one_time["id"],)) == {
        "quality_status": "accepted",
        "work_id": "/works/OLOT1",
    }

    # Shared book backed only by disabled association source keeps candidate + candidate_quality,
    # falls back to assoc_id, and demotes status from recommended to new.
    after_assoc = row("SELECT source_id,status,score FROM candidates WHERE id=?", (shared_assoc["id"],))
    assert after_assoc == {
        "source_id": assoc_id,
        "status": "new",
        "score": 0.0,
    }
    assert row("SELECT quality_status,work_id FROM candidate_quality WHERE candidate_id=?", (shared_assoc["id"],)) == {
        "quality_status": "accepted",
        "work_id": "/works/OLASSOC2",
    }

    # Unread book with zero remaining memberships is deleted.
    assert row("SELECT id FROM candidates WHERE id=?", (chart_unread["id"],)) is None

    # Saved book is retained along with historical attribution.
    after_saved = row("SELECT source_id,status FROM candidates WHERE id=?", (chart_saved["id"],))
    assert after_saved == {"source_id": chart_id, "status": "saved"}


def test_toggle_source_transfers_shared_candidates_before_demoting_orphans(
    database, monkeypatch
):
    monkeypatch.setattr("afterword_engine.main.enqueue_job", lambda *_args, **_kwargs: "job-stub")
    with transaction() as con:
        primary_id = con.execute(
            "INSERT INTO sources(name,url,enabled,weight,lifecycle) VALUES(?,?,1,1.7,'permanent') RETURNING id",
            ("Primary Feed", "https://example.com/primary"),
        ).fetchone()[0]
        backup_one_time_id = con.execute(
            "INSERT INTO sources(name,url,enabled,weight,lifecycle,last_scanned_at) "
            "VALUES(?,?,1,1.0,'one_time','2026-04-01 00:00:00') RETURNING id",
            ("One-Time Backup", "https://example.com/backup-one-time"),
        ).fetchone()[0]

        shared_cid = con.execute(
            "INSERT INTO candidates(title,author,source_id,status,score,normalized_key) "
            "VALUES('Shared Recommended','Writer One',?,'recommended',91.5,'shared recommended writer one') RETURNING id",
            (primary_id,),
        ).fetchone()[0]
        con.execute(
            "INSERT INTO candidate_sources(candidate_id,source_id,source_url) VALUES(?,?,?)",
            (shared_cid, backup_one_time_id, "https://example.com/backup-one-time/1"),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.9 WHERE candidate_id=?",
            (shared_cid,),
        )

        exclusive_cid = con.execute(
            "INSERT INTO candidates(title,author,source_id,status,score,normalized_key) "
            "VALUES('Exclusive Recommended','Writer Two',?,'recommended',84.0,'exclusive recommended writer two') RETURNING id",
            (primary_id,),
        ).fetchone()[0]
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.9 WHERE candidate_id=?",
            (exclusive_cid,),
        )

    toggle_source(primary_id)

    shared_after = row("SELECT source_id,status,score FROM candidates WHERE id=?", (shared_cid,))
    assert shared_after == {
        "source_id": backup_one_time_id,
        "status": "recommended",
        "score": 91.5,
    }
    exclusive_after = row("SELECT source_id,status,score FROM candidates WHERE id=?", (exclusive_cid,))
    assert exclusive_after == {
        "source_id": primary_id,
        "status": "new",
        "score": 0.0,
    }

    # Disabling the remaining one_time owner now demotes the shared candidate.
    toggle_source(backup_one_time_id)
    assert row("SELECT status,score FROM candidates WHERE id=?", (shared_cid,)) == {
        "status": "new",
        "score": 0.0,
    }

    # Re-enabling Primary Feed promotes primary_id back as candidates.source_id.
    toggle_source(primary_id)
    assert row("SELECT source_id FROM candidates WHERE id=?", (shared_cid,))["source_id"] == primary_id


@respx.mock
def test_scoring_work_lookup_serving_and_digest_include_shared_active_candidate(
    database, monkeypatch
):
    async def fake_fetch(url):
        return "application/json", [
            {
                "title": "The Shared Horizon",
                "author": "Elena Vance",
                "description": "An interstellar mystery across generational ships.",
                "genres": ["Science fiction"],
                "source_url": f"{url}/shared-horizon",
            }
        ]

    async def no_enrichment(items):
        return items

    monkeypatch.setattr("afterword_engine.ingestion._fetch_and_parse_source", fake_fetch)
    monkeypatch.setattr("afterword_engine.ingestion.enrich_book_metadata", no_enrichment)

    with transaction() as con:
        con.execute(
            "INSERT INTO reads(id,title,author,rating,isbn,source) "
            "VALUES(1,'Earlier Voyage','Elena Vance',5,'9780306406157','goodreads')"
        )
        enabled_feed_id = con.execute(
            "INSERT INTO sources(name,url,enabled,weight) VALUES(?,?,1,1.5) RETURNING id",
            ("Curated Sci-Fi", "https://example.com/curated-scifi"),
        ).fetchone()[0]
        second_enabled_id = con.execute(
            "INSERT INTO sources(name,url,enabled,weight) VALUES(?,?,1,1.1) RETURNING id",
            ("Secondary Sci-Fi", "https://example.com/secondary-scifi"),
        ).fetchone()[0]

    # Discover first via background association (enabled=0), then via two enabled feeds.
    persist_associations(
        [
            Association(
                provider="openlibrary_lists",
                seed_read_id=1,
                external_id="/works/OL99999W",
                title="The Shared Horizon",
                author="Elena Vance",
                source_url="https://openlibrary.org/works/OL99999W",
            )
        ]
    )
    assert asyncio.run(scan_source(row("SELECT * FROM sources WHERE id=?", (second_enabled_id,)))) == 1
    assert asyncio.run(scan_source(row("SELECT * FROM sources WHERE id=?", (enabled_feed_id,)))) == 1

    candidate = row("SELECT id,source_id FROM candidates WHERE title='The Shared Horizon'")
    assert candidate["source_id"] == enabled_feed_id

    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.93,"
            "provider='openlibrary',provider_id='/works/OL99999W',work_id='/works/OL99999W' "
            "WHERE candidate_id=?",
            (candidate["id"],),
        )

    # 1. Scoring includes the shared candidate and uses Curated Sci-Fi's weight & name.
    scored_count = asyncio.run(score_all(embedder=FakeEmbedder()))
    assert scored_count == 1
    scored_row = row("SELECT status,score,explanation FROM candidates WHERE id=?", (candidate["id"],))
    assert scored_row["status"] == "recommended"
    assert scored_row["score"] > 0
    assert "From Curated Sci-Fi" in json.loads(scored_row["explanation"])

    # 2. Read work identity lookup includes the shared active Open Library candidate.
    async def fake_edition_lookup(_client, isbn13):
        assert isbn13 == "9780306406157"
        return httpx.Response(
            200,
            json={
                "isbn_13": ["9780306406157"],
                "works": [{"key": "/works/OL11111W"}],
            },
            request=httpx.Request("GET", f"https://openlibrary.org/isbn/{isbn13}.json"),
        )

    monkeypatch.setattr("afterword_engine.ingestion._request_openlibrary_edition", fake_edition_lookup)
    monkeypatch.setattr("afterword_engine.covers.OPEN_LIBRARY_MIN_REQUEST_INTERVAL_SECONDS", 0)
    lookup_stats = asyncio.run(refresh_read_work_identities())
    assert lookup_stats["checked"] == 1
    assert lookup_stats["matched"] == 1
    assert row("SELECT openlibrary_work_id FROM reads WHERE id=1")["openlibrary_work_id"] == "/works/OL11111W"

    # 3. Serving endpoint returns the shared candidate once (no join fan-out) with primary source_name.
    with TestClient(app, headers=authenticated_headers()) as client:
        served = client.get("/api/recommendations").json()
    matching_served = [item for item in served if item["id"] == candidate["id"]]
    assert len(matching_served) == 1
    assert matching_served[0]["source_name"] == "Curated Sci-Fi"

    # 4. Digest selection includes the shared candidate once with primary source_name.
    cfg = digest_config({"digest_minimum_score": "0", "digest_maximum_books": "10", "digest_only_new": "1"})
    digest_candidates = _candidate_rows(cfg)
    matching_digest = [item for item in digest_candidates if item["id"] == candidate["id"]]
    assert len(matching_digest) == 1
    assert matching_digest[0]["source_name"] == "Curated Sci-Fi"
