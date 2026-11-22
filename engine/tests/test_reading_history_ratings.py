import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth_helpers import authenticated_headers

from afterword_engine import profiles
from afterword_engine.associations import Association, run_association_provider, select_seed_reads
from afterword_engine.config import settings
from afterword_engine.database import initialize, row, rows, transaction
from afterword_engine.ingestion import _read_metadata_identity_hash as read_metadata_identity_hash
from afterword_engine.interaction_personalization import load_interaction_events
from afterword_engine.llm_reranker import build_rerank_prompt
from afterword_engine.main import app, recommendation_list, tracked_recommendations
from afterword_engine.scoring import score_all
from afterword_engine.tenancy import profile_scope


@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "ratings.db")
    initialize()
    return settings.db


def test_inline_rating_set_change_clear_and_validation(database):
    headers = authenticated_headers()
    with TestClient(app, headers=headers) as client:
        with transaction() as con:
            read_id = con.execute(
                "INSERT INTO reads(title,author,rating,read_at,source) "
                "VALUES('Station Eleven','Emily St. John Mandel',NULL,'2026-08-14','manual')"
            ).lastrowid

        history_resp = client.get("/api/reading-history")
        assert history_resp.status_code == 200
        station = next(item for item in history_resp.json() if item["id"] == read_id)
        assert station["rating"] is None

        # Set whole-star rating 5 on an unrated read
        put_resp = client.put(f"/api/reading-history/{read_id}", json={"rating": 5})
        assert put_resp.status_code == 200
        body = put_resp.json()
        assert body["id"] == read_id
        assert body["rating"] == 5
        assert body["read_at"] == "2026-08-14"
        assert body["job_id"]

        # Change rating via PATCH from 5 -> 3 -> 1
        patch_resp = client.patch(f"/api/reading-history/{read_id}", json={"rating": "3"})
        assert patch_resp.status_code == 200
        assert patch_resp.json()["rating"] == 3

        low_resp = client.put(f"/api/reading-history/{read_id}", json={"rating": 1})
        assert low_resp.status_code == 200
        assert low_resp.json()["rating"] == 1

        # Overview history reflects the updated rating
        overview = client.get("/api/overview").json()
        recent_match = next(item for item in overview["history"] if item["id"] == read_id)
        assert recent_match["rating"] == 1

        # Clear rating back to Unrated (null and empty string)
        clear_resp = client.put(f"/api/reading-history/{read_id}", json={"rating": None})
        assert clear_resp.status_code == 200
        assert clear_resp.json()["rating"] is None
        assert row("SELECT rating FROM reads WHERE id=?", (read_id,))["rating"] is None

        client.put(f"/api/reading-history/{read_id}", json={"rating": 4})
        empty_clear = client.patch(f"/api/reading-history/{read_id}", json={"rating": ""})
        assert empty_clear.status_code == 200
        assert empty_clear.json()["rating"] is None

        # Reject invalid ratings and missing IDs
        for invalid in [0, -1, 6, 3.5, 4.0, True, False, "0", "6", "4.5", "five", [], {}]:
            bad = client.put(f"/api/reading-history/{read_id}", json={"rating": invalid})
            assert bad.status_code == 422, f"Expected 422 for {invalid!r}, got {bad.status_code}"

        assert client.put("/api/reading-history/999999", json={"rating": 4}).status_code == 404
        assert client.put("/api/reading-history/0", json={"rating": 4}).status_code == 404


def test_public_v1_reading_history_rating_authentication_and_profile_isolation(database):
    headers = authenticated_headers()
    with TestClient(app, headers=headers) as client:
        with transaction() as con:
            read_id = con.execute(
                "INSERT INTO reads(title,author,rating,read_at,source) "
                "VALUES('The Spear Cuts Through Water','Simon Jimenez',NULL,'2026-09-01','manual')"
            ).lastrowid
        token_resp = client.post("/api/settings/api-tokens", json={"name": "rating-cli"})
        assert token_resp.status_code == 200
        raw_token = token_resp.json()["token"]

    # Unauthenticated v1 calls fail with 401
    with TestClient(app) as anon_client:
        unauth_get = anon_client.get("/api/v1/reading-history")
        assert unauth_get.status_code == 401
        assert unauth_get.headers.get("www-authenticate") == "Bearer"

        unauth_put = anon_client.put(f"/api/v1/reading-history/{read_id}", json={"rating": 5})
        assert unauth_put.status_code == 401

    # Authenticated v1 client can list, rate, and clear ratings
    v1_headers = {"Authorization": f"Bearer {raw_token}"}
    with TestClient(app, headers=v1_headers) as v1_client:
        root_meta = v1_client.get("/api/v1").json()
        assert "reading_history" in root_meta["endpoints"]
        assert "reading_history_rating" in root_meta["endpoints"]

        listed = v1_client.get("/api/v1/reading-history")
        assert listed.status_code == 200
        assert any(item["id"] == read_id and item["rating"] is None for item in listed.json())

        rated = v1_client.put(f"/api/v1/reading-history/{read_id}", json={"rating": 4})
        assert rated.status_code == 200
        assert rated.json()["rating"] == 4

        cleared = v1_client.patch(f"/api/v1/reading-history/{read_id}", json={"rating": None})
        assert cleared.status_code == 200
        assert cleared.json()["rating"] is None

    # Another profile's token cannot access or mutate this profile's read entry
    other_account = profiles.create_account(
        "second-reader", "second-reader-password-long", role="user"
    )
    with profile_scope(other_account["profile_id"]):
        initialize()
    other_token = profiles.create_profile_api_token(other_account["profile_id"], "other-cli")["token"]
    with TestClient(app, headers={"Authorization": f"Bearer {other_token}"}) as other_client:
        not_found = other_client.put(f"/api/v1/reading-history/{read_id}", json={"rating": 5})
        assert not_found.status_code == 404


def test_rating_edits_drive_scoring_associations_llm_prompts_and_preserve_read_exclusion(database):
    headers = authenticated_headers()
    with transaction() as con:
        con.execute("DELETE FROM reads")
        con.execute("DELETE FROM candidates")
        source_id = con.execute("SELECT id FROM sources WHERE is_default=1 LIMIT 1").fetchone()[0]

        def insert_candidate(title: str, author: str, genres: str, description: str, status: str = "recommended") -> int:
            cid = con.execute(
                "INSERT INTO candidates(title,author,genres,description,score,status,source_id,normalized_key) "
                "VALUES(?,?,?,?,50,?,?,?)",
                (title, author, genres, description, status, source_id, f"{title.casefold()}::{author.casefold()}"),
            ).lastrowid
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.95,metadata_confidence=1.0 "
                "WHERE candidate_id=?",
                (cid,),
            )
            return int(cid)

        shortlisted_id = insert_candidate(
            "Oceanic Memory Archive",
            "Clara Vance",
            '[" literary science fiction ", "archival mystery"]',
            "An archivist reconstructs vanishing island memories across flooded libraries.",
            status="saved",
        )
        matching_candidate_id = insert_candidate(
            "Tidewater Cartography",
            "Clara Vance",
            '["literary science fiction", "archival mystery"]',
            "Flooded island libraries and memory archives chart a submerged coast.",
            status="recommended",
        )
        unrelated_candidate_id = insert_candidate(
            "Cast Iron Weeknight Grilling",
            "burnett cole",
            '["cooking", "barbecue"]',
            " Backyard grilling recipes for cast iron skillets and marinades.",
            status="recommended",
        )

    with TestClient(app, headers=headers) as client:
        # Mark shortlisted book as Read WITHOUT a rating
        mark_resp = client.post(f"/api/recommendations/{shortlisted_id}/read", json={})
        assert mark_resp.status_code == 200
        read_id = row("SELECT id FROM reads WHERE title='Oceanic Memory Archive'")["id"]

        # Unrated read is excluded from recommendations, association seeds, and LLM rated prompt lists
        visible_titles = [item["title"] for item in recommendation_list()]
        assert "Oceanic Memory Archive" not in visible_titles

        assert len(select_seed_reads(rows("SELECT * FROM reads"))) == 0
        prompt_unrated = build_rerank_prompt(
            rows("SELECT title, author, rating FROM reads ORDER BY id"),
            [{"id": matching_candidate_id, "title": "Tidewater Cartography", "author": "Clara Vance", "score": 50}],
        )
        unrated_payload = json.loads(prompt_unrated[1]["content"])
        assert unrated_payload["goodreads_reads_liked"] == []
        assert unrated_payload["goodreads_reads_disliked"] == []

        asyncio.run(score_all())
        unrated_match_score = row("SELECT score FROM candidates WHERE id=?", (matching_candidate_id,))["score"]

        # Rate 5 stars inline -> joins scoring, association seeds, and LLM liked prompt
        rate_high = client.put(f"/api/reading-history/{read_id}", json={"rating": 5})
        assert rate_high.status_code == 200
        asyncio.run(score_all())

        high_match = row("SELECT score,explanation FROM candidates WHERE id=?", (matching_candidate_id,))
        unrelated_match = row("SELECT score FROM candidates WHERE id=?", (unrelated_candidate_id,))
        assert high_match["score"] > unrated_match_score
        assert high_match["score"] > unrelated_match["score"]
        assert "You rated a book by this author highly" in high_match["explanation"]

        seeds = select_seed_reads(rows("SELECT * FROM reads"))
        assert [seed["title"] for seed in seeds] == ["Oceanic Memory Archive"]

        prompt_high = build_rerank_prompt(
            rows("SELECT title, author, rating FROM reads ORDER BY id"),
            [{"id": matching_candidate_id, "title": "Tidewater Cartography", "author": "Clara Vance", "score": high_match["score"]}],
        )
        high_payload = json.loads(prompt_high[1]["content"])
        assert [b["title"] for b in high_payload["goodreads_reads_liked"]] == ["Oceanic Memory Archive"]
        assert high_payload["goodreads_reads_disliked"] == []

        # Change rating to 1 star -> leaves association seeds, joins disliked signals
        rate_low = client.put(f"/api/reading-history/{read_id}", json={"rating": 1})
        assert rate_low.status_code == 200
        assert select_seed_reads(rows("SELECT * FROM reads")) == []

        asyncio.run(score_all())
        low_match_score = row("SELECT score FROM candidates WHERE id=?", (matching_candidate_id,))["score"]
        assert low_match_score < high_match["score"]

        prompt_low = build_rerank_prompt(
            rows("SELECT title, author, rating FROM reads ORDER BY id"),
            [{"id": matching_candidate_id, "title": "Tidewater Cartography", "author": "Clara Vance", "score": low_match_score}],
        )
        low_payload = json.loads(prompt_low[1]["content"])
        assert low_payload["goodreads_reads_liked"] == []
        assert [b["title"] for b in low_payload["goodreads_reads_disliked"]] == ["Oceanic Memory Archive"]

        # Clear rating back to Unrated -> stays Read and excluded from recommendations & association outputs
        clear_resp = client.put(f"/api/reading-history/{read_id}", json={"rating": None})
        assert clear_resp.status_code == 200
        assert row("SELECT rating FROM reads WHERE id=?", (read_id,))["rating"] is None
        assert select_seed_reads(rows("SELECT * FROM reads")) == []

        asyncio.run(score_all())
        visible_after_clear = [item["title"] for item in recommendation_list()]
        assert "Oceanic Memory Archive" not in visible_after_clear

        # Even if another 5-star seed proposes the cleared read via an association provider, it remains excluded
        with transaction() as con:
            anchor_id = con.execute(
                "INSERT INTO reads(title,author,rating,read_at,source,openlibrary_work_id) "
                "VALUES('Anchor Seed Book','Clara Vance',5,'2026-09-02','manual','/works/OL999W')"
            ).lastrowid

        class StubAssociationProvider:
            provider = "openlibrary_lists"

            async def collect(self, _reads):
                return [
                    Association(
                        provider=self.provider,
                        seed_read_id=int(anchor_id),
                        external_id="/works/OL111W",
                        title="Oceanic Memory Archive",
                        author="Clara Vance",
                        source_url="https://openlibrary.org/works/OL111W",
                    ),
                    Association(
                        provider=self.provider,
                        seed_read_id=int(anchor_id),
                        external_id="/works/OL222W",
                        title="Brand New Archipelago",
                        author="Clara Vance",
                        source_url="https://openlibrary.org/works/OL222W",
                    ),
                ]

        assoc_result = asyncio.run(
            run_association_provider(StubAssociationProvider(), rows("SELECT * FROM reads"))
        )
        assert assoc_result.seeds == 1
        assert assoc_result.persisted == 2
        with transaction() as con:
            con.execute(
                "UPDATE sources SET enabled=1 WHERE url='association://openlibrary_lists'"
            )
            con.execute(
                "UPDATE candidates SET status='recommended' WHERE title IN ('Oceanic Memory Archive','Brand New Archipelago')"
            )
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted',metadata_confidence=1.0 "
                "WHERE candidate_id IN (SELECT id FROM candidates WHERE title IN ('Oceanic Memory Archive','Brand New Archipelago'))"
            )
        assert "Brand New Archipelago" in [item["title"] for item in recommendation_list()]
        assert "Oceanic Memory Archive" not in [item["title"] for item in recommendation_list()]


def test_rating_edits_synchronize_attributed_outcomes_telemetry_and_evidence(database):
    headers = authenticated_headers()
    recs, _run_id = tracked_recommendations(limit=5)
    assert recs
    exposed_candidate = recs[0]
    candidate_id = int(exposed_candidate["id"])

    # Make sure impression timestamp strictly precedes manual read completion
    with transaction() as con:
        con.execute(
            "UPDATE recommendation_impressions SET presented_at='2026-01-01T00:00:00+00:00' "
            "WHERE candidate_id=?",
            (candidate_id,),
        )

    with TestClient(app, headers=headers) as client:
        # 1. Mark exposed recommendation as Read without a rating -> completed_unrated
        mark_resp = client.post(f"/api/recommendations/{candidate_id}/read", json={})
        assert mark_resp.status_code == 200
        read_row = row(
            "SELECT id FROM reads WHERE title=? AND author=?",
            (exposed_candidate["title"], exposed_candidate["author"]),
        )
        read_id = int(read_row["id"])

        outcome = row("SELECT * FROM recommendation_outcomes WHERE read_id=?", (read_id,))
        assert outcome["label_kind"] == "completed_unrated"
        assert outcome["label"] == pytest.approx(0.75)
        assert outcome["confidence"] == pytest.approx(0.4)

        # 2. Rate 5 stars -> rating_positive (1.0, confidence 1.0)
        assert client.put(f"/api/reading-history/{read_id}", json={"rating": 5}).status_code == 200
        outcome = row("SELECT * FROM recommendation_outcomes WHERE read_id=?", (read_id,))
        assert outcome["label_kind"] == "rating_positive"
        assert outcome["label"] == pytest.approx(1.0)
        assert outcome["confidence"] == pytest.approx(1.0)
        ev = row("SELECT value FROM recommendation_events WHERE id=?", (outcome["event_id"],))
        assert ev["value"] == pytest.approx(5.0)
        with transaction() as con:
            events = load_interaction_events(con)
        assert any(int(e["candidate_id"]) == candidate_id and e["value"] == pytest.approx(5.0) for e in events)

        # 3. Rate 3 stars -> rating_neutral (0.5, confidence 0.25)
        assert client.put(f"/api/reading-history/{read_id}", json={"rating": 3}).status_code == 200
        outcome = row("SELECT * FROM recommendation_outcomes WHERE read_id=?", (read_id,))
        assert outcome["label_kind"] == "rating_neutral"
        assert outcome["label"] == pytest.approx(0.5)
        assert outcome["confidence"] == pytest.approx(0.25)
        assert row("SELECT value FROM recommendation_events WHERE id=?", (outcome["event_id"],))["value"] == pytest.approx(3.0)

        # 4. Rate 2 stars -> rating_negative (0.0, confidence 1.0)
        assert client.put(f"/api/reading-history/{read_id}", json={"rating": 2}).status_code == 200
        outcome = row("SELECT * FROM recommendation_outcomes WHERE read_id=?", (read_id,))
        assert outcome["label_kind"] == "rating_negative"
        assert outcome["label"] == pytest.approx(0.0)
        assert outcome["confidence"] == pytest.approx(1.0)
        assert row("SELECT value FROM recommendation_events WHERE id=?", (outcome["event_id"],))["value"] == pytest.approx(2.0)

        # 5. Clear rating -> restores completed_unrated (0.75, confidence 0.4)
        assert client.put(f"/api/reading-history/{read_id}", json={"rating": None}).status_code == 200
        outcome = row("SELECT * FROM recommendation_outcomes WHERE read_id=?", (read_id,))
        assert outcome["label_kind"] == "completed_unrated"
        assert outcome["label"] == pytest.approx(0.75)
        assert outcome["confidence"] == pytest.approx(0.4)
        assert row("SELECT value FROM recommendation_events WHERE id=?", (outcome["event_id"],))["value"] is None

        # Decision evidence archive recorded immutable revisions for each state transition
        evidence_outcomes = rows(
            "SELECT label_kind FROM recommendation_evidence_outcomes "
            "WHERE candidate_id=? ORDER BY event_revision",
            (candidate_id,),
        )
        assert [item["label_kind"] for item in evidence_outcomes] == [
            "completed_unrated",
            "rating_positive",
            "rating_neutral",
            "rating_negative",
            "completed_unrated",
        ]


def test_rapid_rating_edits_coalesce_and_running_score_queues_single_followup(database):
    headers = authenticated_headers()
    with transaction() as con:
        read_a = con.execute(
            "INSERT INTO reads(title,author,rating,source) VALUES('Book A','Author A',NULL,'manual')"
        ).lastrowid
        read_b = con.execute(
            "INSERT INTO reads(title,author,rating,source) VALUES('Book B','Author B',4,'manual')"
        ).lastrowid
        con.execute("DELETE FROM jobs")

    with TestClient(app, headers=headers) as client:
        # Four rapid edits coalesce into one queued score job
        j1 = client.put(f"/api/reading-history/{read_a}", json={"rating": 5}).json()["job_id"]
        j2 = client.put(f"/api/reading-history/{read_b}", json={"rating": 2}).json()["job_id"]
        j3 = client.patch(f"/api/reading-history/{read_a}", json={"rating": 4}).json()["job_id"]
        j4 = client.put(f"/api/reading-history/{read_b}", json={"rating": None}).json()["job_id"]

        assert j1 == j2 == j3 == j4
        score_jobs = rows("SELECT id,status FROM jobs WHERE kind='score'")
        assert len(score_jobs) == 1
        assert score_jobs[0]["status"] == "queued"

        # Simulate the worker picking up j1 (running) while more rating edits arrive
        with transaction() as con:
            con.execute("UPDATE jobs SET status='running' WHERE id=?", (j1,))

        f1 = client.put(f"/api/reading-history/{read_a}", json={"rating": 1}).json()["job_id"]
        f2 = client.put(f"/api/reading-history/{read_b}", json={"rating": 5}).json()["job_id"]

        assert f1 != j1
        assert f1 == f2
        active_score_jobs = rows("SELECT id,status FROM jobs WHERE kind='score' ORDER BY status")
        assert {(item["id"], item["status"]) for item in active_score_jobs} == {
            (j1, "running"),
            (f1, "queued"),
        }


def test_rating_edits_preserve_cached_metadata_and_embeddings_without_lookup_jobs(database):
    headers = authenticated_headers()
    checked_at = datetime.now(timezone.utc).isoformat()
    with TestClient(app, headers=headers) as client:
        with transaction() as con:
            con.execute("DELETE FROM jobs")
            read_id = con.execute(
                "INSERT INTO reads(title,author,rating,read_at,isbn,source,openlibrary_work_id,"
                "openlibrary_lookup_attempted_at) "
                "VALUES('Piranesi Archive Edition','Susanna Clarke',4,'2026-06-01','9781635575637',"
                "'goodreads_csv','/works/OL20899838W',?)",
                (checked_at,),
            ).lastrowid
            read_record = dict(con.execute("SELECT * FROM reads WHERE id=?", (read_id,)).fetchone())
            identity_hash = read_metadata_identity_hash(read_record)
            con.execute(
                "INSERT INTO read_metadata("
                "read_id,verified_work_id,identity_provider,identity_provider_id,"
                "identity_hash,description,genres,cover_url,release_date,metadata_checked_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    read_id,
                    "/works/OL20899838W",
                    "openlibrary",
                    "/works/OL20899838W",
                    identity_hash,
                    "A house of infinite halls and tides.",
                    '["Fantasy", "Mystery"]',
                    "https://covers.openlibrary.org/b/id/10310443-L.jpg",
                    "2020-09-15",
                    checked_at,
                ),
            )
            con.execute(
                "INSERT INTO metadata_field_provenance("
                "entity_type,entity_id,field,provider,provider_id,confidence,source_payload,verified_at"
                ") VALUES('read',?,'description','openlibrary','/works/OL20899838W',0.95,?,?)",
                (read_id, '{"description":"A house of infinite halls and tides."}', checked_at),
            )

        asyncio.run(score_all())
        embedding_before = row(
            "SELECT content_hash,vector FROM embeddings WHERE entity_type='read' AND entity_id=?",
            (read_id,),
        )
        assert embedding_before is not None

        with transaction() as con:
            con.execute("DELETE FROM jobs")

        for next_rating in (5, 2, None, 4):
            resp = client.put(f"/api/reading-history/{read_id}", json={"rating": next_rating})
            assert resp.status_code == 200

        # Reading history profile scores recalculate live without reload
        scores_resp = client.get("/api/reading-history/scores?offset=0&limit=20")
        assert scores_resp.status_code == 200
        assert any(item["id"] == read_id and item["algorithm_score"] is not None for item in scores_resp.json())

        lookup_jobs = rows(
            "SELECT kind FROM jobs WHERE kind IN ('read_metadata','read_work_identities')"
        )
        assert lookup_jobs == []

    updated_read = row("SELECT * FROM reads WHERE id=?", (read_id,))
    assert updated_read["isbn"] == "9781635575637"
    assert updated_read["openlibrary_work_id"] == "/works/OL20899838W"
    assert read_metadata_identity_hash(updated_read) == identity_hash

    meta_after = row("SELECT * FROM read_metadata WHERE read_id=?", (read_id,))
    assert meta_after is not None
    assert meta_after["verified_work_id"] == "/works/OL20899838W"
    assert meta_after["identity_hash"] == identity_hash
    assert meta_after["metadata_checked_at"] == checked_at
    provenance_after = row(
        "SELECT * FROM metadata_field_provenance WHERE entity_type='read' AND entity_id=?",
        (read_id,),
    )
    assert provenance_after is not None

    embedding_after = row(
        "SELECT content_hash,vector FROM embeddings WHERE entity_type='read' AND entity_id=?",
        (read_id,),
    )
    assert embedding_after["content_hash"] == embedding_before["content_hash"]
    assert embedding_after["vector"] == embedding_before["vector"]

    lookup_jobs = rows(
        "SELECT kind FROM jobs WHERE kind IN ('read_metadata','read_work_identities')"
    )
    assert lookup_jobs == []
