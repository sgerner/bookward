import asyncio

import pytest

from afterword_engine.config import settings
from afterword_engine.database import initialize, transaction
from afterword_engine.digest import _candidate_rows
from afterword_engine.identity import (
    book_identity,
    book_identity_match_index,
    book_identity_matches,
    book_row_identity_match_keys,
)
from afterword_engine.main import recommendation_list
from afterword_engine.scoring import score_all


@pytest.fixture()
def database(tmp_path):
    settings.db = str(tmp_path / "identity.db")
    initialize()
    return settings.db


def add_candidate(title, author, status="recommended", score=90):
    with transaction() as con:
        return con.execute(
            "INSERT INTO candidates(title,author,score,status,normalized_key,source_id) "
            "VALUES(?,?,?,?,?,(SELECT id FROM sources WHERE url='builtin://upcoming'))",
            (title, author, score, status, f"{title} {author} {status}"),
        ).lastrowid


def test_identity_is_conservative_and_handles_audio_metadata():
    assert book_identity("Yellowface", "R.F. Kuang") == book_identity("Yellowface", "R. F. Kuang")
    assert book_identity("Dune Book 2", "Author") != book_identity("Dune", "Author")
    assert book_identity("Tales (Series, #1-3)", "Author") != book_identity("Tales", "Author")
    assert book_identity("Tales (Notebook 1)", "Author") != book_identity("Tales", "Author")
    assert book_identity("The Fifth Season", "N. K. Jemisin") == book_identity("The Fifth Season", "N K Jemisin")
    assert book_identity("The Expanse: Books, Book 1 (Unabridged)", "James Corey") == book_identity("The Expanse", "James Corey")
    assert book_identity("Book 1", "Author") != book_identity("Book 2", "Author")
    assert book_identity("MaddAddam (MaddAddam, #1)", "Margaret Atwood") == book_identity("MaddAddam", "Margaret Atwood")
    assert book_identity("Collected Tales (Books 1-3)", "Author") != book_identity("Collected Tales", "Author")
    assert book_identity("Dune 2", "Author") != book_identity("Dune", "Author")
    assert book_identity("Same Title", "Author A") != book_identity("Same Title", "Author B")


def test_identity_matching_handles_catalog_author_suffixes_and_subtitles():
    assert book_identity_matches(
        "Slaughterhouse-Five",
        "Kurt Vonnegut",
        "Slaughterhouse-Five",
        "Kurt Vonnegut Jr.",
    )
    assert book_identity_matches(
        "The Rise and Fall of the Third Reich",
        "William L. Shirer",
        "The Rise and Fall of the Third Reich: A History of Nazi Germany",
        "William L. Shirer",
    )
    assert book_identity_matches(
        "The Dispossessed",
        "Ursula K. Le Guin",
        "The Dispossessed: An Ambiguous Utopia",
        "Ursula K. Le Guin",
    )
    assert book_identity_matches(
        "Biological War",
        "Annie Jacobsen",
        "Biological War: A Scenario",
        "Annie Jacobsen",
    )
    assert not book_identity_matches("Dune", "Frank Herbert", "Dune: Messiah", "Frank Herbert")
    assert not book_identity_matches(
        "The Hunger Games",
        "Suzanne Collins",
        "The Hunger Games: Catching Fire",
        "Suzanne Collins",
    )
    assert not book_identity_matches(
        "The Lord of the Rings",
        "J. R. R. Tolkien",
        "The Lord of the Rings: The Two Towers",
        "J. R. R. Tolkien",
    )


def test_identity_index_matches_isbn_editions_and_provider_scoped_work_ids():
    reads = [
        {
            "title": "Unreasonable Hospitality",
            "author": "Will Guidara",
            "isbn": "0593418573",
        },
        {
            "title": "The Lost City",
            "author": "A. Reader",
            "work_id": "/works/OL123456W",
            "work_id_provider": "openlibrary",
        },
    ]
    read_keys = book_identity_match_index(reads)

    assert book_row_identity_match_keys(
        {
            "title": "Hospitality: A New Approach",
            "author": "Different Catalog Author",
            "isbn13": "9780593418574",
        }
    ) & read_keys
    assert book_row_identity_match_keys(
        {
            "title": "The Lost City: A Novel",
            "author": "Someone Else",
            "quality_work_id": "OL123456W",
            "quality_provider": "openlibrary",
        }
    ) & read_keys
    assert not book_row_identity_match_keys(
        {
            "title": "The Lost City: A Novel",
            "author": "Someone Else",
            "quality_work_id": "OL123456W",
            "quality_provider": "google_books",
        }
    ) & read_keys
    assert not book_row_identity_match_keys(
        {
            "title": "An unrelated candidate",
            "author": "Another Author",
            "isbn13": "9780593418573",
            "work_id": "OL123456W",
        }
    ) & read_keys


def test_recommendation_and_digest_hide_unrated_reads_but_keep_saved(database):
    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected'")
    add_candidate("Already Read", "Writer", score=100)
    add_candidate("Already Read", "Writer", status="saved", score=80)
    add_candidate("Already Read", "Writer", status="imported", score=70)
    add_candidate("Slaughterhouse-Five", "Kurt Vonnegut", score=95)
    add_candidate("The Rise and Fall of the Third Reich", "William L. Shirer", score=94)
    add_candidate("New Book", "Writer", score=90)
    # A read imported after scoring/candidate ingestion is hidden immediately.
    with transaction() as con:
        con.execute("INSERT INTO reads(title,author,rating,source) VALUES('Already Read','Writer',NULL,'test')")
        con.execute("INSERT INTO reads(title,author,rating,source) VALUES('Slaughterhouse-Five','Kurt Vonnegut Jr.',5,'test')")
        con.execute("INSERT INTO reads(title,author,rating,source) VALUES('The Rise and Fall of the Third Reich: A History of Nazi Germany','William L. Shirer',4,'test')")

    visible = recommendation_list(limit=1)
    assert [(item["title"], item["status"]) for item in visible] == [("New Book", "recommended")]
    visible_all = recommendation_list()
    assert {(item["title"], item["status"]) for item in visible_all if item["title"] == "Already Read"} == {
        ("Already Read", "saved"), ("Already Read", "imported")
    }
    digest = _candidate_rows({"minimum_score": 0, "maximum_books": 1, "only_new": False})
    assert "Already Read" not in {item["title"] for item in digest}
    assert "New Book" in {item["title"] for item in digest}


def test_recommendations_hide_recommended_duplicate_of_imported_work(database):
    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected'")
    recommended_id = add_candidate("Whistler", "Ann Patchett", score=90)
    imported_id = add_candidate("Whistler", "Ann Patchett", status="imported", score=80)

    visible = recommendation_list(limit=None)

    assert recommended_id not in {item["id"] for item in visible}
    assert imported_id in {item["id"] for item in visible}


def test_discovery_never_returns_the_three_read_book_identity_variants(database):
    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected'")

    unreasonable_id = add_candidate(
        "Unreasonable Hospitality: The Remarkable Power of Giving People More Than They Expect",
        "Unknown author",
        score=100,
    )
    dispossessed_id = add_candidate(
        "The Dispossessed",
        "Ursula K. Le Guin",
        score=99,
    )
    biological_war_id = add_candidate(
        "Biological War",
        "Annie Jacobsen",
        score=98,
    )
    fresh_id = add_candidate("A Different Book", "Another Writer", score=90)
    with transaction() as con:
        con.execute(
            "UPDATE candidates SET isbn13=? WHERE id=?",
            ("9780593418574", unreasonable_id),
        )
        con.execute(
            "INSERT INTO reads(title,author,isbn,source) VALUES(?,?,?,?)",
            (
                "Unreasonable Hospitality",
                "Will Guidara",
                "0593418573",
                "goodreads_csv",
            ),
        )
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            (
                "The Dispossessed: An Ambiguous Utopia",
                "Ursula K. Le Guin",
                "goodreads_csv",
            ),
        )
        con.execute(
            "INSERT INTO reads(title,author,source) VALUES(?,?,?)",
            (
                "Biological War: A Scenario",
                "Annie Jacobsen",
                "goodreads_csv",
            ),
        )

    visible = recommendation_list(status="recommended", limit=None)
    visible_ids = {item["id"] for item in visible}

    assert unreasonable_id not in visible_ids
    assert dispossessed_id not in visible_ids
    assert biological_war_id not in visible_ids
    assert fresh_id in visible_ids


def test_scoring_excludes_unrated_reads(database):
    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected'")
        con.execute("INSERT INTO reads(title,author,rating,source) VALUES('Already Read','Writer',NULL,'test')")
    add_candidate("Already Read", "Writer")
    add_candidate("New Book", "Writer")

    class FakeEmbedder:
        name, model = "test", "identity"

        async def embed(self, texts):
            return [[1.0, 0.0] if "New Book" in text else [0.0, 1.0] for text in texts]

    assert asyncio.run(score_all(embedder=FakeEmbedder())) == 1
    with transaction() as con:
        statuses = dict(con.execute("SELECT title,status FROM candidates WHERE title IN ('Already Read','New Book')"))
    assert statuses["Already Read"] == "recommended"
    assert statuses["New Book"] == "recommended"
