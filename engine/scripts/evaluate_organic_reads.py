#!/usr/bin/env python3
"""Evaluate candidate pool coverage and pre-read ranking on organic reads.

Reads the SQLite database in read-only mode (``file:...?mode=ro``) and measures
how often post-baseline organic reads appeared in the active accepted candidate
pool and where they were ranked in recommendation runs prior to being read.
"""

from __future__ import annotations

import argparse
import collections
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
from pathlib import Path
import sqlite3
import statistics
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afterword_engine.identity import book_row_identity_match_keys
from afterword_engine.ingestion import read_identity_rows_with_current_verified_work_ids
from afterword_engine.learning import HISTORICAL_READ_SOURCES
from afterword_engine.read_dates import read_day


DEFAULT_SINCE = "2026-09-21"
EXCLUDED_READ_SOURCES = HISTORICAL_READ_SOURCES | {"demo"}


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    resolved = db_path.resolve()
    con = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _parse_utc_timestamp(value: Any) -> tuple[datetime | None, bool]:
    """Parse supported SQLite, ISO-8601, slash, or RFC-2822 values into UTC.

    Returns ``(timestamp_utc, day_precision)`` where ``day_precision`` is True
    when the input carries only a calendar day (or midnight RFC-2822 from
    Goodreads RSS).
    """
    raw = str(value or "").strip()
    if not raw:
        return None, False
    if "T" not in raw and " " not in raw:
        day = read_day(raw)
        if day is None:
            return None, False
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc), True
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None, False
        if parsed is None:
            return None, False
        if (
            parsed.hour == 0
            and parsed.minute == 0
            and parsed.second == 0
            and parsed.microsecond == 0
        ):
            try:
                utc_day = (
                    parsed.replace(tzinfo=timezone.utc).date()
                    if parsed.tzinfo is None
                    else parsed.astimezone(timezone.utc).date()
                )
            except OverflowError:
                return None, False
            return (
                datetime(utc_day.year, utc_day.month, utc_day.day, tzinfo=timezone.utc),
                True,
            )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        try:
            parsed = parsed.astimezone(timezone.utc)
        except OverflowError:
            return None, False
    return parsed, False


def _read_completion_day(read: dict[str, Any] | sqlite3.Row) -> date | None:
    raw_read_at = str(read["read_at"] or "").strip()
    if raw_read_at:
        return read_day(raw_read_at)
    return read_day(read["created_at"])


def _read_cutoff(read: dict[str, Any] | sqlite3.Row) -> datetime | None:
    """Return the UTC pre-read cutoff timestamp for an organic read row."""
    raw_read_at = str(read["read_at"] or "").strip()
    created_ts, _ = _parse_utc_timestamp(read["created_at"])
    if not raw_read_at:
        return created_ts
    read_ts, day_precision = _parse_utc_timestamp(raw_read_at)
    if read_ts is None:
        return None
    if day_precision:
        if created_ts is not None and read_ts.date() == created_ts.date():
            return created_ts
        return datetime(read_ts.year, read_ts.month, read_ts.day, tzinfo=timezone.utc)
    return read_ts


def _normalized_rating(value: Any) -> float | None:
    if value is None:
        return None
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rating) or rating <= 0.0:
        return None
    return rating


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
    since_day = read_day(since)
    if since_day is None:
        raise ValueError(f"Invalid --since date: {since!r}")

    con = _connect_readonly(db_path)
    try:
        raw_reads = con.execute(
            "SELECT id, title, author, rating, read_at, source, created_at, isbn, "
            "openlibrary_work_id "
            "FROM reads "
            "ORDER BY created_at, id"
        ).fetchall()

        filtered_reads: list[sqlite3.Row] = []
        for read in raw_reads:
            source = str(read["source"] or "").strip().casefold()
            if source in EXCLUDED_READ_SOURCES:
                continue
            completion_day = _read_completion_day(read)
            if completion_day is None or completion_day < since_day:
                continue
            if _read_cutoff(read) is None:
                continue
            filtered_reads.append(read)

        filtered_reads.sort(
            key=lambda row: (
                _parse_utc_timestamp(row["created_at"])[0]
                or datetime.min.replace(tzinfo=timezone.utc),
                int(row["id"]),
            )
        )

        read_metadata_rows = con.execute(
            "SELECT read_id, verified_work_id, identity_provider, "
            "identity_provider_id, identity_hash "
            "FROM read_metadata "
            "WHERE verified_work_id != ''"
        ).fetchall()
        reads = read_identity_rows_with_current_verified_work_ids(
            filtered_reads, read_metadata_rows
        )

        candidates = con.execute(
            "SELECT c.id, c.title, c.author, c.source_id, c.source_url, c.created_at, "
            "c.isbn13, c.isbn10, "
            "q.work_id AS quality_work_id, q.provider AS quality_provider, "
            "q.isbn13 AS quality_isbn13, q.isbn10 AS quality_isbn10 "
            "FROM candidates c "
            "JOIN sources s ON s.id = c.source_id "
            "JOIN candidate_quality q ON q.candidate_id = c.id "
            "WHERE s.enabled = 1 AND q.quality_status = 'accepted'"
        ).fetchall()
        cand_created_by_id: dict[int, datetime | None] = {}
        by_key: dict[tuple[str, ...], list[sqlite3.Row]] = collections.defaultdict(list)
        for cand in candidates:
            cand_id = int(cand["id"])
            cand_created_by_id[cand_id] = _parse_utc_timestamp(cand["created_at"])[0]
            for key in book_row_identity_match_keys(cand):
                by_key[key].append(cand)

        details: list[dict[str, Any]] = []
        in_pool = 0
        completed_ranks: list[int] = []
        unrated_ranks: list[int] = []
        high_ranks: list[int] = []
        neutral_ranks: list[int] = []
        disliked_ranks: list[int] = []

        for read in reads:
            read_cutoff = _read_cutoff(read)
            assert read_cutoff is not None

            matched_by_id: dict[int, sqlite3.Row] = {}
            for key in book_row_identity_match_keys(read):
                for cand in by_key.get(key, ()):
                    cand_id = int(cand["id"])
                    cand_created = cand_created_by_id.get(cand_id)
                    if cand_created is not None and cand_created < read_cutoff:
                        matched_by_id[cand_id] = cand

            best_rank: int | None = None
            best_score: float | None = None
            for cand_id in matched_by_id:
                impression_rows = con.execute(
                    "SELECT i.rank, i.score, r.created_at AS run_created_at "
                    "FROM recommendation_impressions i "
                    "JOIN recommendation_runs r ON r.id = i.run_id "
                    "WHERE i.candidate_id = ?",
                    (cand_id,),
                ).fetchall()
                pre_read_impressions: list[tuple[datetime, int, sqlite3.Row]] = []
                for imp in impression_rows:
                    run_ts, _ = _parse_utc_timestamp(imp["run_created_at"])
                    if run_ts is not None and run_ts < read_cutoff:
                        pre_read_impressions.append((run_ts, -int(imp["rank"]), imp))
                if pre_read_impressions:
                    _, _, latest_imp = max(
                        pre_read_impressions, key=lambda item: (item[0], item[1])
                    )
                    rank = int(latest_imp["rank"])
                    if best_rank is None or rank < best_rank:
                        best_rank = rank
                        best_score = (
                            float(latest_imp["score"])
                            if latest_imp["score"] is not None
                            else None
                        )

            pool_hit = bool(matched_by_id)
            if pool_hit:
                in_pool += 1
            rating = _normalized_rating(read["rating"])
            if best_rank is not None:
                completed_ranks.append(best_rank)
                if rating is None:
                    unrated_ranks.append(best_rank)
                elif rating >= 4.0:
                    high_ranks.append(best_rank)
                elif rating <= 2.0:
                    disliked_ranks.append(best_rank)
                else:
                    neutral_ranks.append(best_rank)

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
            "pre_read_ranked_count": len(completed_ranks),
            "all_completed": _rank_summary(completed_ranks),
            "unrated_ranked": _rank_summary(unrated_ranks),
            "high_rated_ranked": _rank_summary(high_ranks),
            "neutral_rated_ranked": _rank_summary(neutral_ranks),
            "disliked_ranked": _rank_summary(disliked_ranks),
            "reads": details,
        }
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", type=Path, help="Path to SQLite database")
    parser.add_argument(
        "--since",
        default=DEFAULT_SINCE,
        help="Minimum UTC completion date cutoff (ISO date)",
    )
    args = parser.parse_args()
    report = evaluate_organic_reads(args.db_path, since=args.since)
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
