from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afterword_engine.collaborative import CollabIndex, score_collaborative_residual


def test_score_collaborative_residual_requires_min_neighbors_and_bounds_score(tmp_path: Path) -> None:
    idx_path = tmp_path / "cf.db"
    con = sqlite3.connect(idx_path)
    try:
        con.execute("CREATE TABLE isbn_map(isbn TEXT PRIMARY KEY, item_id INTEGER NOT NULL)")
        con.execute(
            "CREATE TABLE item_neighbors(item_id INTEGER NOT NULL, neighbor_id INTEGER NOT NULL, weight REAL NOT NULL, "
            "PRIMARY KEY(item_id, neighbor_id))"
        )
        con.executemany(
            "INSERT INTO isbn_map(isbn, item_id) VALUES(?, ?)",
            [
                ("9780000000001", 1),
                ("9780000000002", 2),
                ("9780000000003", 3),
                ("9780000000100", 100),
                ("9780000000101", 101),
            ],
        )
        con.executemany(
            "INSERT INTO item_neighbors(item_id, neighbor_id, weight) VALUES(?, ?, ?)",
            [
                (100, 1, 0.8),
                (100, 2, 0.7),
                (100, 3, 0.6),
                (101, 1, 0.9),  # only 1 neighbor -> below min_neighbors=3
            ],
        )
        con.commit()
    finally:
        con.close()

    reads = [
        {"title": "R1", "author": "A", "isbn": "9780000000001", "rating": 5.0},
        {"title": "R2", "author": "A", "isbn": "9780000000002", "rating": 5.0},
        {"title": "R3", "author": "A", "isbn": "9780000000003", "rating": 5.0},
        {"title": "R4", "author": "A", "isbn": "9780000000999", "rating": 1.0},
    ]
    candidates = [
        {"id": 10, "isbn13": "978-0-00-000010-0"},
        {"id": 11, "isbn13": "978-0-00-000010-1"},
    ]

    index = CollabIndex(idx_path)
    try:
        adjustments = score_collaborative_residual(reads, candidates, index, min_neighbors=3)
    finally:
        index.close()

    assert 10 in adjustments
    assert 0.0 < adjustments[10] <= 5.0
    assert 11 not in adjustments
