#!/usr/bin/env python3
"""Evaluate candidate pool coverage and pre-read ranking on organic reads.

Reads the SQLite database in read-only mode (``file:...?mode=ro``) and measures
how often post-baseline organic reads appeared in the candidate pool and where
they were ranked in recommendation runs prior to being logged as read.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
import sqlite3
import statistics
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afterword_engine.identity import book_row_identity_match_keys


DEFAULT_SINCE = "2026-09-21"


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    resolved = db_path.resolve()
    con = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _rank_summary(ranks: list[int]) -> dict[str, Any]:
    if not ranks:
        return {
            "count": 0,
            "median_rank": None,
            "hit_at_8": 0,
            "hit_at_24": 0,
            "mrr": None,
        }
    return {
        "count": len(ranks),
        "median_rank": statistics.median(ranks),
        "hit_at_8": sum(1 for rank in ranks if rank <= 8),
        "hit_at_24": sum(1 for rank in ranks if rank <= 24),
        "mrr": round(sum(1.0 / rank for rank in ranks if rank > 0) / len(ranks), 4),
    }


def evaluate_organic_reads(db_path: Path, *, since: str = DEFAULT_SINCE) -> dict[str, Any]:
    """Compute organic-read pool recall and pre-read rank metrics."""
    con = _connect_readonly(db_path)
    try:
        reads = con.execute(
            "SELECT id, title, author, rating, read_at, source, created_at, isbn, "
            "openlibrary_work_id "
            "FROM reads "
            "WHERE created_at >= ? AND COALESCE(source, '') != 'manual_history' "
            "ORDER BY created_at, id",
            (since,),
        ).fetchall()
        candidates = con.execute(
            "SELECT c.id, c.title, c.author, c.source_id, c.created_at, "
            "c.isbn13, c.isbn10, q.work_id AS verified_work_id "
            "FROM candidates c "
            "LEFT JOIN candidate_quality q ON q.candidate_id = c.id"
        ).fetchall()
        by_key: dict[str, list[sqlite3.Row]] = collections.defaultdict(list)
        for cand in candidates:
            for key in book_row_identity_match_keys(dict(cand)):
                by_key[key].append(cand)

        details: list[dict[str, Any]] = []
        in_pool = 0
        all_ranks: list[int] = []
        high_ranks: list[int] = []
        low_mid_ranks: list[int] = []

        for read in reads:
            matched_by_id: dict[int, sqlite3.Row] = {}
            for key in book_row_identity_match_keys(dict(read)):
                for cand in by_key.get(key, ()):
                    matched_by_id[int(cand["id"])] = cand

            best_rank: int | None = None
            best_score: float | None = None
            for cand_id in matched_by_id:
                row = con.execute(
                    "SELECT i.rank, i.score "
                    "FROM recommendation_impressions i "
                    "JOIN recommendation_runs r ON r.id = i.run_id "
                    "WHERE i.candidate_id = ? AND r.created_at < ? "
                    "ORDER BY r.created_at DESC, i.rank ASC LIMIT 1",
                    (cand_id, read["created_at"]),
                ).fetchone()
                if row is not None:
                    rank = int(row["rank"])
                    if best_rank is None or rank < best_rank:
                        best_rank = rank
                        best_score = float(row["score"]) if row["score"] is not None else None

            pool_hit = bool(matched_by_id)
            if pool_hit:
                in_pool += 1
            rating = float(read["rating"]) if read["rating"] is not None else None
            if best_rank is not None:
                all_ranks.append(best_rank)
                if rating is not None and rating >= 4.0:
                    high_ranks.append(best_rank)
                elif rating is not None:
                    low_mid_ranks.append(best_rank)

            details.append(
                {
                    "title": read["title"],
                    "author": read["author"],
                    "rating": rating,
                    "read_at": read["read_at"],
                    "created_at": read["created_at"],
                    "in_pool": pool_hit,
                    "pre_read_rank": best_rank,
                    "pre_read_score": round(best_score, 2) if best_score is not None else None,
                }
            )

        total = len(reads)
        return {
            "since": since,
            "total_organic_reads": total,
            "in_pool_count": in_pool,
            "in_pool_rate": round(in_pool / total, 4) if total else 0.0,
            "pre_read_ranked_count": len(all_ranks),
            "all_ranked": _rank_summary(all_ranks),
            "high_rated_ranked": _rank_summary(high_ranks),
            "low_or_mid_ranked": _rank_summary(low_mid_ranks),
            "reads": details,
        }
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", type=Path, help="Path to SQLite database")
    parser.add_argument("--since", default=DEFAULT_SINCE, help="Minimum reads.created_at cutoff (ISO date)")
    args = parser.parse_args()
    report = evaluate_organic_reads(args.db_path, since=args.since)
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
