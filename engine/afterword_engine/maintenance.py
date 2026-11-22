"""Automated SQLite retention maintenance for profile databases.

Prunes aged unattributed telemetry, stale/orphaned/dismissed embeddings, and
terminal operational rows while preserving personalization signals, attributed
evaluation outcomes, active job leases, and recent digest delivery periods.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import settings
from .database import transaction
from .decision_evidence import (
    _prune_unreferenced_evidence_vectors,
    _unreferenced_scoring_batch_ids,
    rotate_recommendation_evidence_if_needed,
)


TELEMETRY_RETENTION_DAYS = 90
TERMINAL_JOBS_RETENTION_DAYS = 14
TERMINAL_JOBS_MAX_ROWS = 200
ASSOCIATION_RUNS_RETENTION_DAYS = 30
ASSOCIATION_RUNS_MAX_ROWS = 200
ASSOCIATION_REQUESTS_RETENTION_DAYS = 30
ASSOCIATION_REQUESTS_MAX_ROWS = 1_000
ASSOCIATION_CACHE_MAX_PER_PROVIDER = 512
DELIVERIES_RECENT_PROTECTED_DAYS = 30
DELIVERIES_RETENTION_DAYS = 90
DELIVERIES_MAX_TERMINAL_ROWS = 200
MAINTENANCE_LAST_RUN_KEY = "storage_last_maintenance_at"
MAINTENANCE_INTERVAL_HOURS = 6


def _utc_reference(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _sqlite_timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def prune_unattributed_telemetry(
    connection,
    *,
    now: datetime | None = None,
    retention_days: int = TELEMETRY_RETENTION_DAYS,
) -> dict[str, int]:
    """Delete unattributed runs and impressions older than `retention_days`.

    Preserves `recommendation_events`, `feedback`, `recommendation_outcomes`,
    and parent `recommendation_runs` / `recommendation_impressions` referenced
    by `recommendation_outcomes` for offline evaluation.
    """

    ref = _utc_reference(now)
    cutoff_iso = (ref - timedelta(days=max(1, int(retention_days)))).isoformat()

    pruned_impressions = int(
        connection.execute(
            """
            DELETE FROM recommendation_impressions
            WHERE datetime(presented_at) < datetime(?)
              AND NOT EXISTS (
                  SELECT 1 FROM recommendation_outcomes o
                  WHERE o.impression_id = recommendation_impressions.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM recommendation_impressions sibling
                  JOIN recommendation_outcomes o ON o.impression_id = sibling.id
                  WHERE sibling.run_id = recommendation_impressions.run_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM recommendation_runs r
                  WHERE r.id = recommendation_impressions.run_id
                    AND datetime(r.created_at) >= datetime(?)
              )
            """,
            (cutoff_iso, cutoff_iso),
        ).rowcount
        or 0
    )

    pruned_runs = int(
        connection.execute(
            """
            DELETE FROM recommendation_runs
            WHERE datetime(created_at) < datetime(?)
              AND NOT EXISTS (
                  SELECT 1 FROM recommendation_impressions i
                  JOIN recommendation_outcomes o ON o.impression_id = i.id
                  WHERE i.run_id = recommendation_runs.id
              )
            """,
            (cutoff_iso,),
        ).rowcount
        or 0
    )

    # Clean up any evidence rows or unreferenced batches orphaned by deleted runs.
    connection.execute(
        """
        DELETE FROM recommendation_run_evidence_scoring_batches
        WHERE NOT EXISTS (
            SELECT 1 FROM recommendation_run_evidence e
            WHERE e.run_id = recommendation_run_evidence_scoring_batches.run_id
              AND e.capture_status = 'complete'
        )
        """
    )
    pruned_batches = 0
    while True:
        batch_ids = _unreferenced_scoring_batch_ids(connection, limit=64)
        if not batch_ids:
            break
        marks = ",".join("?" for _ in batch_ids)
        pruned_batches += int(
            connection.execute(
                f"DELETE FROM scoring_batches WHERE id IN ({marks})",
                batch_ids,
            ).rowcount
            or 0
        )
    pruned_vectors = _prune_unreferenced_evidence_vectors(connection)

    return {
        "pruned_runs": max(0, pruned_runs),
        "pruned_impressions": max(0, pruned_impressions),
        "pruned_scoring_batches": max(0, pruned_batches),
        "pruned_evidence_vectors": max(0, pruned_vectors),
    }


def prune_stale_and_dismissed_embeddings(connection) -> dict[str, int]:
    """Remove virtual, orphaned, stale-model, and dismissed candidate vectors."""

    configured = {
        str(row["key"]): str(row["value"])
        for row in connection.execute(
            "SELECT key, value FROM settings "
            "WHERE key IN ('embedding_backend', 'embedding_model')"
        ).fetchall()
    }
    backend = configured.get("embedding_backend") or settings.embedding_backend
    model = configured.get("embedding_model") or settings.embedding_model

    removed_virtual_or_orphan = int(
        connection.execute(
            """
            DELETE FROM embeddings
            WHERE entity_type = 'read_candidate'
               OR entity_id <= 0
               OR entity_type NOT IN ('candidate', 'read')
               OR (
                   entity_type = 'candidate'
                   AND NOT EXISTS (
                       SELECT 1 FROM candidates c WHERE c.id = embeddings.entity_id
                   )
               )
               OR (
                   entity_type = 'read'
                   AND NOT EXISTS (
                       SELECT 1 FROM reads r WHERE r.id = embeddings.entity_id
                   )
               )
            """
        ).rowcount
        or 0
    )

    removed_stale_model = int(
        connection.execute(
            """
            DELETE FROM embeddings
            WHERE backend != ? OR model != ?
            """,
            (backend, model),
        ).rowcount
        or 0
    )

    removed_dismissed = int(
        connection.execute(
            """
            DELETE FROM embeddings
            WHERE entity_type = 'candidate'
              AND EXISTS (
                  SELECT 1 FROM candidates c
                  LEFT JOIN candidate_quality q ON q.candidate_id = c.id
                  WHERE c.id = embeddings.entity_id
                    AND (
                        c.status IN ('rejected', 'hidden', 'read')
                        OR q.quality_status = 'rejected'
                    )
              )
              AND entity_id NOT IN (
                  SELECT candidate_id FROM (
                      SELECT candidate_id, occurred_at
                      FROM recommendation_events
                      WHERE event_type IN ('save', 'reject', 'maybe_later', 'restore', 'read', 'librarr_import')
                      UNION ALL
                      SELECT f.candidate_id, f.created_at AS occurred_at
                      FROM feedback f
                      WHERE f.action IN ('save', 'reject', 'maybe_later', 'restore')
                        AND NOT EXISTS (
                            SELECT 1 FROM recommendation_events e
                            WHERE e.event_key = 'feedback:' || f.id
                        )
                  )
                  ORDER BY datetime(occurred_at) DESC
                  LIMIT 5000
              )
            """
        ).rowcount
        or 0
    )

    return {
        "removed_orphan_or_virtual": max(0, removed_virtual_or_orphan),
        "removed_stale_model": max(0, removed_stale_model),
        "removed_dismissed": max(0, removed_dismissed),
    }


def prune_operational_tables(
    connection,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Enforce age and row caps on terminal jobs, association logs/caches, and deliveries.

    Never deletes `queued` or `running` leased jobs.
    """

    ref = _utc_reference(now)
    now_iso = ref.isoformat()
    today_date = ref.strftime("%Y-%m-%d")
    jobs_cutoff = _sqlite_timestamp(ref - timedelta(days=TERMINAL_JOBS_RETENTION_DAYS))
    assoc_cutoff_iso = (ref - timedelta(days=ASSOCIATION_RUNS_RETENTION_DAYS)).isoformat()
    assoc_req_cutoff_iso = (ref - timedelta(days=ASSOCIATION_REQUESTS_RETENTION_DAYS)).isoformat()
    deliveries_recent_iso = (ref - timedelta(days=DELIVERIES_RECENT_PROTECTED_DAYS)).isoformat()
    deliveries_cutoff_iso = (ref - timedelta(days=DELIVERIES_RETENTION_DAYS)).isoformat()

    # 1. Terminal jobs: never touch queued/running or active leases, and keep
    # today's Goodreads RSS jobs so daily sync retry limits stay accurate.
    pruned_jobs = int(
        connection.execute(
            """
            DELETE FROM jobs
            WHERE status IN ('complete', 'failed')
              AND lease_token IS NULL
              AND NOT (kind = 'goodreads_rss' AND date(created_at) = date(?))
              AND (
                  datetime(COALESCE(finished_at, started_at, created_at)) < datetime(?)
                  OR id NOT IN (
                      SELECT id FROM jobs
                      WHERE status IN ('complete', 'failed')
                        AND lease_token IS NULL
                      ORDER BY datetime(COALESCE(finished_at, started_at, created_at)) DESC,
                               rowid DESC
                      LIMIT ?
                  )
              )
            """,
            (today_date, jobs_cutoff, TERMINAL_JOBS_MAX_ROWS),
        ).rowcount
        or 0
    )

    # 2. Terminal association_runs: never touch running rows.
    pruned_association_runs = int(
        connection.execute(
            """
            DELETE FROM association_runs
            WHERE status IN ('complete', 'failed')
              AND (
                  datetime(COALESCE(finished_at, started_at)) < datetime(?)
                  OR id NOT IN (
                      SELECT id FROM association_runs
                      WHERE status IN ('complete', 'failed')
                      ORDER BY datetime(COALESCE(finished_at, started_at)) DESC,
                               id DESC
                      LIMIT ?
                  )
              )
            """,
            (assoc_cutoff_iso, ASSOCIATION_RUNS_MAX_ROWS),
        ).rowcount
        or 0
    )

    # 3. association_requests: keep today's provider budget rows intact.
    pruned_association_requests = int(
        connection.execute(
            """
            DELETE FROM association_requests
            WHERE date(requested_at) < date(?)
              AND (
                  datetime(requested_at) < datetime(?)
                  OR id NOT IN (
                      SELECT id FROM association_requests
                      ORDER BY datetime(requested_at) DESC, id DESC
                      LIMIT ?
                  )
              )
            """,
            (today_date, assoc_req_cutoff_iso, ASSOCIATION_REQUESTS_MAX_ROWS),
        ).rowcount
        or 0
    )

    # 4. association_cache: remove expired rows and enforce per-provider cap.
    pruned_association_cache = int(
        connection.execute(
            """
            DELETE FROM association_cache
            WHERE datetime(expires_at) <= datetime(?)
            """,
            (now_iso,),
        ).rowcount
        or 0
    )
    providers = [
        str(row["provider"])
        for row in connection.execute(
            "SELECT DISTINCT provider FROM association_cache"
        ).fetchall()
    ]
    for provider in providers:
        pruned_association_cache += int(
            connection.execute(
                """
                DELETE FROM association_cache
                WHERE provider = ?
                  AND rowid NOT IN (
                      SELECT rowid FROM association_cache
                      WHERE provider = ?
                      ORDER BY datetime(fetched_at) DESC, rowid DESC
                      LIMIT ?
                  )
                """,
                (provider, provider, ASSOCIATION_CACHE_MAX_PER_PROVIDER),
            ).rowcount
            or 0
        )

    # 5. notification_deliveries: preserve pending rows, digest_last_period,
    # and recent periods; rely on found=False fallback for aged expired links.
    last_period_row = connection.execute(
        "SELECT value FROM settings WHERE key='digest_last_period'"
    ).fetchone()
    protected_period = str(last_period_row["value"]).strip() if last_period_row and last_period_row["value"] else ""

    pruned_deliveries = int(
        connection.execute(
            """
            DELETE FROM notification_deliveries
            WHERE status IN ('sent', 'failed')
              AND period_key != ?
              AND datetime(COALESCE(sent_at, updated_at, created_at)) < datetime(?)
              AND (
                  datetime(COALESCE(sent_at, updated_at, created_at)) < datetime(?)
                  OR id NOT IN (
                      SELECT id FROM notification_deliveries
                      WHERE status IN ('sent', 'failed')
                      ORDER BY datetime(COALESCE(sent_at, updated_at, created_at)) DESC,
                               rowid DESC
                      LIMIT ?
                  )
              )
            """,
            (
                protected_period,
                deliveries_recent_iso,
                deliveries_cutoff_iso,
                DELIVERIES_MAX_TERMINAL_ROWS,
            ),
        ).rowcount
        or 0
    )

    return {
        "pruned_jobs": max(0, pruned_jobs),
        "pruned_association_runs": max(0, pruned_association_runs),
        "pruned_association_requests": max(0, pruned_association_requests),
        "pruned_association_cache": max(0, pruned_association_cache),
        "pruned_notification_deliveries": max(0, pruned_deliveries),
    }


def run_storage_maintenance(
    connection=None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute full retention maintenance for the active profile database."""

    ref = _utc_reference(now)

    def _apply(con) -> dict[str, Any]:
        evidence = rotate_recommendation_evidence_if_needed(con)
        telemetry = prune_unattributed_telemetry(con, now=ref)
        embeddings = prune_stale_and_dismissed_embeddings(con)
        operational = prune_operational_tables(con, now=ref)
        con.execute(
            "INSERT INTO settings(key, value, secret) VALUES(?, ?, 0) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, secret=0, "
            "updated_at=CURRENT_TIMESTAMP",
            (MAINTENANCE_LAST_RUN_KEY, ref.isoformat()),
        )
        return {
            "ran_at": ref.isoformat(),
            "evidence": evidence,
            "telemetry": telemetry,
            "embeddings": embeddings,
            "operational": operational,
        }

    if connection is not None:
        return _apply(connection)
    with transaction() as con:
        return _apply(con)
