import asyncio

import pytest

from afterword_engine.config import settings
from afterword_engine.database import initialize, transaction
from afterword_engine.digest import _candidate_rows
from afterword_engine.identity import book_identity, book_identity_matches
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
    assert not book_identity_matches("Dune", "Frank Herbert", "Dune: Messiah", "Frank Herbert")


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
