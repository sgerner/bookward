#!/usr/bin/env python3
"""Build a compact SQLite item-item collaborative index from UCSD Goodreads data."""

from __future__ import annotations

import argparse
import collections
import csv
import gzip
import json
import math
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afterword_engine.collaborative import normalize_isbn


def build_index(
    books_json_gz: Path,
    interactions_csv: Path,
    output_db: Path,
    *,
    min_item_ratings: int = 20,
    top_k_neighbors: int = 40,
    max_users: int = 200_000,
) -> dict[str, int]:
    """Stream UCSD Goodreads books & interactions into a compact CF SQLite index."""
    isbn_to_book: dict[str, int] = {}
    with gzip.open(books_json_gz, "rt", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            bid = int(row["book_id"])
            for field in ("isbn13", "isbn"):
                code = normalize_isbn(row.get(field))
                if code:
                    isbn_to_book.setdefault(code, bid)

    user_items: dict[int, list[tuple[int, float]]] = collections.defaultdict(list)
    item_counts: collections.Counter[int] = collections.Counter()
    opener = gzip.open if interactions_csv.suffix == ".gz" else open
    with opener(interactions_csv, "rt", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rating = int(row.get("rating") or 0)
            if rating <= 0:
                continue
            uid = int(row["user_id"])
            if len(user_items) >= max_users and uid not in user_items:
                continue
            bid = int(row["book_id"])
            user_items[uid].append((bid, float(rating)))
            item_counts[bid] += 1

    valid_items = {bid for bid, cnt in item_counts.items() if cnt >= min_item_ratings}
    item_norms: dict[int, float] = collections.defaultdict(float)
    co_dots: dict[tuple[int, int], float] = collections.defaultdict(float)

    for items in user_items.values():
        filtered = [(bid, r) for bid, r in items if bid in valid_items]
        if len(filtered) < 2 or len(filtered) > 300:
            continue
        u_mean = sum(r for _, r in filtered) / len(filtered)
        centered = [(bid, r - u_mean) for bid, r in filtered if r != u_mean]
        for bid, val in centered:
            item_norms[bid] += val * val
        for i, (b1, v1) in enumerate(centered):
            for b2, v2 in centered[i + 1 :]:
                pair = (b1, b2) if b1 < b2 else (b2, b1)
                co_dots[pair] += v1 * v2

    neighbors_by_item: dict[int, list[tuple[int, float]]] = collections.defaultdict(list)
    for (b1, b2), dot in co_dots.items():
        if dot <= 0:
            continue
        denom = math.sqrt(item_norms[b1] * item_norms[b2])
        if denom <= 0:
            continue
        sim = dot / denom
        if sim >= 0.05:
            neighbors_by_item[b1].append((b2, sim))
            neighbors_by_item[b2].append((b1, sim))

    if output_db.exists():
        output_db.unlink()
    con = sqlite3.connect(output_db)
    try:
        con.execute("CREATE TABLE isbn_map(isbn TEXT PRIMARY KEY, item_id INTEGER NOT NULL)")
        con.execute(
            "CREATE TABLE item_neighbors(item_id INTEGER NOT NULL, neighbor_id INTEGER NOT NULL, weight REAL NOT NULL, "
            "PRIMARY KEY(item_id, neighbor_id))"
        )
        con.executemany(
            "INSERT OR IGNORE INTO isbn_map(isbn, item_id) VALUES(?, ?)",
            ((code, bid) for code, bid in isbn_to_book.items() if bid in valid_items),
        )
        edge_count = 0
        for bid, nlist in neighbors_by_item.items():
            nlist.sort(key=lambda pair: -pair[1])
            top = nlist[:top_k_neighbors]
            con.executemany(
                "INSERT INTO item_neighbors(item_id, neighbor_id, weight) VALUES(?, ?, ?)",
                ((bid, nid, round(sim, 4)) for nid, sim in top),
            )
            edge_count += len(top)
        con.commit()
    finally:
        con.close()

    return {"valid_items": len(valid_items), "edges": edge_count}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("books_json_gz", type=Path)
    parser.add_argument("interactions_csv", type=Path)
    parser.add_argument("output_db", type=Path)
    parser.add_argument("--min-item-ratings", type=int, default=20)
    parser.add_argument("--top-k-neighbors", type=int, default=40)
    parser.add_argument("--max-users", type=int, default=200_000)
    args = parser.parse_args()
    stats = build_index(
        args.books_json_gz,
        args.interactions_csv,
        args.output_db,
        min_item_ratings=args.min_item_ratings,
        top_k_neighbors=args.top_k_neighbors,
        max_users=args.max_users,
    )
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
