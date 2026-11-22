"""Serving-time impression fatigue decay for stale unacted recommendations.

Applies a bounded score decay to ``recommended`` candidates that have been
visibly presented across multiple distinct days without any user action,
preventing stale top-of-slate repetition without turning unobserved
impressions into negative training labels.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import connect


def load_unacted_visible_impression_counts(
    connection=None,
    *,
    window_days: int = 21,
    now: datetime | None = None,
) -> dict[int, int]:
    """Return distinct visible impression days in the window per unacted candidate."""
    if window_days <= 0:
        return {}
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = (reference - timedelta(days=int(window_days))).isoformat()
    query = """
        SELECT i.candidate_id, COUNT(DISTINCT substr(i.visible_at, 1, 10)) AS visible_days
        FROM recommendation_impressions i
        JOIN candidates c ON c.id = i.candidate_id
        WHERE c.status = 'recommended'
          AND i.visible_at IS NOT NULL
          AND julianday(i.visible_at) >= julianday(?)
          AND julianday(i.visible_at) > COALESCE(
              (
                  SELECT MAX(reset_day)
                  FROM (
                      SELECT julianday(e.occurred_at) AS reset_day
                      FROM recommendation_events e
                      WHERE e.candidate_id = i.candidate_id
                        AND e.event_type IN ('maybe_later', 'restore')
                      UNION ALL
                      SELECT julianday(f.created_at) AS reset_day
                      FROM feedback f
                      WHERE f.candidate_id = i.candidate_id
                        AND f.action IN ('maybe_later', 'restore')
                        AND f.undone_at IS NULL
                        AND NOT EXISTS (
                            SELECT 1 FROM recommendation_events fe
                            WHERE fe.event_key = 'feedback:' || f.id
                        )
                  )
                  WHERE reset_day IS NOT NULL
              ),
              -1e9
          )
          AND NOT EXISTS (
              SELECT 1
              FROM recommendation_events e
              WHERE e.candidate_id = i.candidate_id
                AND e.event_type IN ('save', 'reject', 'librarr_import', 'read')
                AND julianday(e.occurred_at) > COALESCE(
                    (
                        SELECT MAX(restore_day)
                        FROM (
                            SELECT julianday(r.occurred_at) AS restore_day
                            FROM recommendation_events r
                            WHERE r.candidate_id = i.candidate_id
                              AND r.event_type = 'restore'
                            UNION ALL
                            SELECT julianday(rf.created_at) AS restore_day
                            FROM feedback rf
                            WHERE rf.candidate_id = i.candidate_id
                              AND rf.action = 'restore'
                              AND rf.undone_at IS NULL
                              AND NOT EXISTS (
                                  SELECT 1 FROM recommendation_events rfe
                                  WHERE rfe.event_key = 'feedback:' || rf.id
                              )
                        )
                        WHERE restore_day IS NOT NULL
                    ),
                    -1e9
                )
          )
        GROUP BY i.candidate_id
        """
    if connection is None:
        with connect() as con:
            rows = con.execute(query, (cutoff,)).fetchall()
    else:
        rows = connection.execute(query, (cutoff,)).fetchall()
    return {int(row["candidate_id"]): int(row["visible_days"]) for row in rows}


def apply_impression_fatigue(
    candidates: Sequence[dict[str, Any]],
    visible_day_counts: dict[int, int],
    *,
    grace_days: int = 3,
    penalty_per_day: float = 0.75,
    max_penalty: float = 4.5,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply bounded impression fatigue to ``recommended`` items and re-sort them."""
    if not candidates:
        return [], {"applied": False, "adjusted_candidates": 0, "max_penalty": 0.0}

    adjusted_recommended: list[dict[str, Any]] = []
    other_items: list[dict[str, Any]] = []
    adjusted_count = 0
    max_applied = 0.0

    for item in candidates:
        copy = dict(item)
        if copy.get("status") != "recommended":
            other_items.append(copy)
            continue
        candidate_id = int(copy.get("id") or 0)
        visible_days = int(visible_day_counts.get(candidate_id, 0))
        excess_days = max(0, visible_days - max(0, int(grace_days)))
        penalty = min(max(0.0, float(max_penalty)), excess_days * max(0.0, float(penalty_per_day)))
        if penalty > 0:
            base_score = float(copy.get("score") or 0.0)
            copy["score"] = round(max(0.0, base_score - penalty), 2)
            copy["fatigue_penalty"] = round(penalty, 2)
            adjusted_count += 1
            max_applied = max(max_applied, penalty)
        adjusted_recommended.append(copy)

    if adjusted_count:
        adjusted_recommended.sort(
            key=lambda row: (-float(row.get("score") or 0.0), -int(row.get("id") or 0))
        )

    return adjusted_recommended + other_items, {
        "applied": adjusted_count > 0,
        "adjusted_candidates": adjusted_count,
        "max_penalty": round(max_applied, 2),
    }
