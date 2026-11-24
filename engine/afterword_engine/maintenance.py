"""Automated SQLite retention maintenance for profile databases.

Prunes aged unattributed telemetry, stale/orphaned/dismissed embeddings, and
terminal operational rows in bounded, short-lived transaction batches while
preserving personalization signals, attributed evaluation history, active job
leases, and recent digest delivery periods.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import settings
from .database import connect, transaction
from .decision_evidence import (
    STORAGE_RETENTION_ENABLED_KEY,
    _unreferenced_scoring_batch_ids,
    is_storage_retention_enabled,
)
from .embeddings import resolve_embedding_target


TELEMETRY_RETENTION_DAYS = 90
TERMINAL_JOBS_RETENTION_DAYS = 30
ASSOCIATION_RUNS_RETENTION_DAYS = 30
ASSOCIATION_REQUESTS_RETENTION_DAYS = 30
ASSOCIATION_CACHE_EXPIRED_GRACE_DAYS = 7
DELIVERIES_RETENTION_DAYS = 180
DELIVERIES_KEEP_PER_CHANNEL = 2

MAINTENANCE_BATCH_SIZE = 200
MAINTENANCE_RUN_BATCH_SIZE = 50
MAINTENANCE_MAX_DURATION_SECONDS = 2.0
MAINTENANCE_LAST_RUN_KEY = "storage_last_maintenance_at"
MAINTENANCE_INTERVAL_HOURS = 6


def _utc_reference(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _sqlite_timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _index_upper_bound(moment: datetime) -> str:
    return (moment.astimezone(timezone.utc) + timedelta(days=1)).strftime(
        "%Y-%m-%dT23:59:59Z"
    )


def set_storage_retention_enabled(enabled: bool, connection=None) -> bool:
    """Persist the per-profile storage retention opt-in/opt-out setting."""

    normalized = bool(enabled)
    stored = "1" if normalized else "0"

    def _write(con) -> None:
        con.execute(
            "INSERT INTO settings(key, value, secret) VALUES(?, ?, 0) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, secret=0, "
            "updated_at=CURRENT_TIMESTAMP",
            (STORAGE_RETENTION_ENABLED_KEY, stored),
        )

    if connection is not None:
        _write(connection)
    else:
        with transaction() as con:
            _write(con)
    return normalized


def _read_with_connection(connection, fn: Callable[[Any], Any]) -> Any:
    if connection is not None:
        return fn(connection)
    with connect() as con:
        return fn(con)


class _BatchRunner:
    """Execute bounded DELETE steps across short independent transactions."""

    def __init__(
        self,
        connection=None,
        *,
        batch_size: int = MAINTENANCE_BATCH_SIZE,
        max_duration_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.connection = connection
        self.batch_size = max(1, int(batch_size))
        self.max_duration_seconds = (
            None
            if max_duration_seconds is None
            else max(0.0, float(max_duration_seconds))
        )
        self.clock = clock
        self.started_at = self.clock()
        self.batches_committed = 0
        self.truncated = False

    def budget_exhausted(self) -> bool:
        if self.truncated:
            return True
        if (
            self.max_duration_seconds is not None
            and self.batches_committed > 0
            and (self.clock() - self.started_at) >= self.max_duration_seconds
        ):
            self.truncated = True
            return True
        return False

    def step(self, fn: Callable[[Any, int], int], *, limit: int | None = None) -> int:
        step_limit = max(1, int(limit or self.batch_size))
        if self.connection is not None:
            deleted = max(0, int(fn(self.connection, step_limit) or 0))
            if deleted > 0:
                self.batches_committed += 1
            return deleted
        with transaction() as con:
            deleted = max(0, int(fn(con, step_limit) or 0))
        if deleted > 0:
            self.batches_committed += 1
        return deleted

    def drain(self, fn: Callable[[Any, int], int], *, limit: int | None = None) -> int:
        step_limit = max(1, int(limit or self.batch_size))
        total = 0
        while not self.budget_exhausted():
            deleted = self.step(fn, limit=step_limit)
            total += deleted
            if deleted < step_limit:
                break
        return total


def prune_unattributed_telemetry(
    connection=None,
    *,
    now: datetime | None = None,
    retention_days: int = TELEMETRY_RETENTION_DAYS,
    batch_size: int = MAINTENANCE_BATCH_SIZE,
    max_duration_seconds: float | None = None,
    _runner: _BatchRunner | None = None,
) -> dict[str, int]:
    """Delete unattributed runs and impressions older than `retention_days`.

    Preserves `recommendation_events`, `feedback`, `recommendation_outcomes`,
    `recommendation_evidence_outcomes`, action-attributed evidence runs, and
    parent `recommendation_runs` / `recommendation_impressions` required for
    offline evaluation.
    """

    runner = _runner or _BatchRunner(
        connection,
        batch_size=batch_size,
        max_duration_seconds=max_duration_seconds,
    )
    ref = _utc_reference(now)
    cutoff_dt = ref - timedelta(days=max(1, int(retention_days)))
    cutoff_iso = cutoff_dt.isoformat()
    cutoff_bound = _index_upper_bound(cutoff_dt)

    pruned_impressions = runner.drain(
        lambda con, limit: con.execute(
            """
            DELETE FROM recommendation_impressions
            WHERE id IN (
                SELECT imp.id
                FROM recommendation_impressions imp
                WHERE imp.presented_at < ?
                  AND datetime(imp.presented_at) < datetime(?)
                  AND NOT EXISTS (
                      SELECT 1 FROM recommendation_outcomes o
                      WHERE o.impression_id = imp.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM recommendation_impressions sibling
                      JOIN recommendation_outcomes o ON o.impression_id = sibling.id
                      WHERE sibling.run_id = imp.run_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM recommendation_evidence_outcomes eo
                      WHERE eo.run_id = imp.run_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM recommendation_evidence_events ee
                      WHERE ee.run_id = imp.run_id
                        AND ee.event_type != 'visible'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM recommendation_runs r
                      WHERE r.id = imp.run_id
                        AND datetime(r.created_at) >= datetime(?)
                  )
                LIMIT ?
            )
            """,
            (cutoff_bound, cutoff_iso, cutoff_iso, limit),
        ).rowcount
    )

    run_limit = min(runner.batch_size, MAINTENANCE_RUN_BATCH_SIZE)
    pruned_runs = runner.drain(
        lambda con, limit: con.execute(
            """
            DELETE FROM recommendation_runs
            WHERE id IN (
                SELECT r.id
                FROM recommendation_runs r
                WHERE r.created_at < ?
                  AND datetime(r.created_at) < datetime(?)
                  AND NOT EXISTS (
                      SELECT 1 FROM recommendation_impressions i
                      JOIN recommendation_outcomes o ON o.impression_id = i.id
                      WHERE i.run_id = r.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM recommendation_evidence_outcomes eo
                      WHERE eo.run_id = r.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM recommendation_evidence_events ee
                      WHERE ee.run_id = r.id
                        AND ee.event_type != 'visible'
                  )
                ORDER BY r.created_at ASC
                LIMIT ?
            )
            """,
            (cutoff_bound, cutoff_iso, limit),
        ).rowcount,
        limit=run_limit,
    )

    # Clean up any evidence links, unreferenced batches, or shared vectors
    # orphaned by pruned unattributed runs while preserving attributed run links.
    runner.drain(
        lambda con, limit: con.execute(
            """
            DELETE FROM recommendation_run_evidence_scoring_batches
            WHERE rowid IN (
                SELECT rowid
                FROM recommendation_run_evidence_scoring_batches l
                WHERE NOT EXISTS (
                    SELECT 1 FROM recommendation_run_evidence e
                    WHERE e.run_id = l.run_id
                      AND e.capture_status = 'complete'
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM recommendation_impressions i
                    JOIN recommendation_outcomes o ON o.impression_id = i.id
                    WHERE i.run_id = l.run_id
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM recommendation_evidence_outcomes eo
                    WHERE eo.run_id = l.run_id
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM recommendation_evidence_events ee
                    WHERE ee.run_id = l.run_id
                      AND ee.event_type != 'visible'
                )
                LIMIT ?
            )
            """,
            (limit,),
        ).rowcount
    )

    def _delete_unreferenced_batches(con, limit: int) -> int:
        batch_ids = _unreferenced_scoring_batch_ids(con, limit=min(limit, 64))
        if not batch_ids:
            return 0
        marks = ",".join("?" for _ in batch_ids)
        return int(
            con.execute(
                f"DELETE FROM scoring_batches WHERE id IN ({marks})",
                batch_ids,
            ).rowcount
            or 0
        )

    pruned_batches = runner.drain(
        _delete_unreferenced_batches, limit=min(runner.batch_size, 64)
    )

    pruned_vectors = runner.drain(
        lambda con, limit: con.execute(
            """
            DELETE FROM recommendation_evidence_vectors
            WHERE artifact_hash IN (
                SELECT v.artifact_hash
                FROM recommendation_evidence_vectors v
                WHERE NOT EXISTS (
                    SELECT 1 FROM recommendation_run_evidence_vectors r
                    WHERE r.artifact_hash = v.artifact_hash
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM scoring_batch_vectors s
                    WHERE s.artifact_hash = v.artifact_hash
                )
                LIMIT ?
            )
            """,
            (limit,),
        ).rowcount
    )

    return {
        "pruned_runs": max(0, pruned_runs),
        "pruned_impressions": max(0, pruned_impressions),
        "pruned_scoring_batches": max(0, pruned_batches),
        "pruned_evidence_vectors": max(0, pruned_vectors),
    }


def prune_stale_and_dismissed_embeddings(
    connection=None,
    *,
    batch_size: int = MAINTENANCE_BATCH_SIZE,
    max_duration_seconds: float | None = None,
    _runner: _BatchRunner | None = None,
) -> dict[str, int]:
    """Remove virtual, orphaned, stale-model, and dismissed candidate vectors."""

    runner = _runner or _BatchRunner(
        connection,
        batch_size=batch_size,
        max_duration_seconds=max_duration_seconds,
    )
    if runner.budget_exhausted():
        return {
            "removed_orphan_or_virtual": 0,
            "removed_stale_model": 0,
            "removed_dismissed": 0,
        }

    configured = _read_with_connection(
        connection,
        lambda con: {
            str(row["key"]): str(row["value"])
            for row in con.execute(
                "SELECT key, value FROM settings "
                "WHERE key IN ('embedding_backend', 'embedding_model')"
            ).fetchall()
        },
    )
    backend, model = resolve_embedding_target(
        configured.get("embedding_backend") or settings.embedding_backend,
        configured.get("embedding_model") or settings.embedding_model,
    )

    removed_virtual_or_orphan = runner.drain(
        lambda con, limit: con.execute(
            """
            DELETE FROM embeddings
            WHERE rowid IN (
                SELECT rowid
                FROM embeddings
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
                LIMIT ?
            )
            """,
            (limit,),
        ).rowcount
    )

    # Guard against cache thrashing: only remove vectors from other backend/model
    # pairs once the resolved active target is actually populated in `embeddings`.
    removed_stale_model = 0
    if not runner.budget_exhausted() and backend and model:
        has_active_embeddings = _read_with_connection(
            connection,
            lambda con: (
                con.execute(
                    "SELECT 1 FROM embeddings WHERE backend = ? AND model = ? LIMIT 1",
                    (backend, model),
                ).fetchone()
                is not None
            ),
        )
        if has_active_embeddings:
            removed_stale_model = runner.drain(
                lambda con, limit: con.execute(
                    """
                    DELETE FROM embeddings
                    WHERE rowid IN (
                        SELECT rowid
                        FROM embeddings
                        WHERE backend != ? OR model != ?
                        LIMIT ?
                    )
                    """,
                    (backend, model, limit),
                ).rowcount
            )

    removed_dismissed = runner.drain(
        lambda con, limit: con.execute(
            """
            DELETE FROM embeddings
            WHERE rowid IN (
                SELECT rowid
                FROM embeddings
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
                LIMIT ?
            )
            """,
            (limit,),
        ).rowcount
    )

    return {
        "removed_orphan_or_virtual": max(0, removed_virtual_or_orphan),
        "removed_stale_model": max(0, removed_stale_model),
        "removed_dismissed": max(0, removed_dismissed),
    }


def prune_operational_tables(
    connection=None,
    *,
    now: datetime | None = None,
    batch_size: int = MAINTENANCE_BATCH_SIZE,
    max_duration_seconds: float | None = None,
    _runner: _BatchRunner | None = None,
) -> dict[str, int]:
    """Enforce conservative age windows on terminal jobs, association logs/caches, and deliveries.

    Never deletes `queued` or `running` leased jobs, today's Goodreads RSS or
    association quota windows, pending deliveries, `digest_last_period`, or the
    2 most recent terminal deliveries per channel.
    """

    runner = _runner or _BatchRunner(
        connection,
        batch_size=batch_size,
        max_duration_seconds=max_duration_seconds,
    )
    if runner.budget_exhausted():
        return {
            "pruned_jobs": 0,
            "pruned_association_runs": 0,
            "pruned_association_requests": 0,
            "pruned_association_cache": 0,
            "pruned_notification_deliveries": 0,
        }

    ref = _utc_reference(now)
    today_date = ref.strftime("%Y-%m-%d")
    jobs_cutoff = _sqlite_timestamp(ref - timedelta(days=TERMINAL_JOBS_RETENTION_DAYS))
    assoc_cutoff_iso = (ref - timedelta(days=ASSOCIATION_RUNS_RETENTION_DAYS)).isoformat()
    assoc_req_cutoff_iso = (ref - timedelta(days=ASSOCIATION_REQUESTS_RETENTION_DAYS)).isoformat()
    assoc_cache_cutoff_iso = (
        ref - timedelta(days=ASSOCIATION_CACHE_EXPIRED_GRACE_DAYS)
    ).isoformat()
    deliveries_cutoff_iso = (ref - timedelta(days=DELIVERIES_RETENTION_DAYS)).isoformat()

    # 1. Terminal jobs (>30d): never touch queued/running or active leases, and
    # keep today's Goodreads RSS jobs so daily sync retry limits stay accurate.
    pruned_jobs = runner.drain(
        lambda con, limit: con.execute(
            """
            DELETE FROM jobs
            WHERE id IN (
                SELECT id FROM jobs
                WHERE status IN ('complete', 'failed')
                  AND lease_token IS NULL
                  AND NOT (kind = 'goodreads_rss' AND date(created_at) = date(?))
                  AND datetime(COALESCE(finished_at, started_at, created_at)) < datetime(?)
                LIMIT ?
            )
            """,
            (today_date, jobs_cutoff, limit),
        ).rowcount
    )

    # 2. Terminal association_runs (>30d): never touch running rows.
    pruned_association_runs = runner.drain(
        lambda con, limit: con.execute(
            """
            DELETE FROM association_runs
            WHERE id IN (
                SELECT id FROM association_runs
                WHERE status IN ('complete', 'failed')
                  AND datetime(COALESCE(finished_at, started_at)) < datetime(?)
                LIMIT ?
            )
            """,
            (assoc_cutoff_iso, limit),
        ).rowcount
    )

    # 3. association_requests (>30d): keep today's provider budget rows intact.
    pruned_association_requests = runner.drain(
        lambda con, limit: con.execute(
            """
            DELETE FROM association_requests
            WHERE id IN (
                SELECT id FROM association_requests
                WHERE date(requested_at) < date(?)
                  AND datetime(requested_at) < datetime(?)
                LIMIT ?
            )
            """,
            (today_date, assoc_req_cutoff_iso, limit),
        ).rowcount
    )

    # 4. association_cache: remove rows expired for more than 7 days past expires_at.
    pruned_association_cache = runner.drain(
        lambda con, limit: con.execute(
            """
            DELETE FROM association_cache
            WHERE rowid IN (
                SELECT rowid FROM association_cache
                WHERE datetime(expires_at) < datetime(?)
                LIMIT ?
            )
            """,
            (assoc_cache_cutoff_iso, limit),
        ).rowcount
    )

    # 5. notification_deliveries (>180d): preserve pending rows, digest_last_period,
    # and the 2 most recent terminal deliveries per channel.
    pruned_deliveries = 0
    if not runner.budget_exhausted():
        protected_period = _read_with_connection(
            connection,
            lambda con: (
                str(row["value"]).strip()
                if (
                    row := con.execute(
                        "SELECT value FROM settings WHERE key='digest_last_period'"
                    ).fetchone()
                )
                and row["value"]
                else ""
            ),
        )
        pruned_deliveries = runner.drain(
            lambda con, limit: con.execute(
                """
                DELETE FROM notification_deliveries
                WHERE id IN (
                    SELECT d.id
                    FROM notification_deliveries d
                    WHERE d.status IN ('sent', 'failed')
                      AND d.period_key != ?
                      AND datetime(COALESCE(d.sent_at, d.updated_at, d.created_at)) < datetime(?)
                      AND d.id NOT IN (
                          SELECT kept.id
                          FROM notification_deliveries kept
                          WHERE kept.channel = d.channel
                            AND kept.status IN ('sent', 'failed')
                          ORDER BY datetime(COALESCE(kept.sent_at, kept.updated_at, kept.created_at)) DESC,
                                   kept.rowid DESC
                          LIMIT ?
                      )
                    LIMIT ?
                )
                """,
                (
                    protected_period,
                    deliveries_cutoff_iso,
                    DELIVERIES_KEEP_PER_CHANNEL,
                    limit,
                ),
            ).rowcount
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
    force: bool = False,
    batch_size: int = MAINTENANCE_BATCH_SIZE,
    max_duration_seconds: float | None = MAINTENANCE_MAX_DURATION_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Execute bounded, batched retention maintenance for the active profile database."""

    ref = _utc_reference(now)
    if not force and not is_storage_retention_enabled(connection):
        return {
            "ran_at": ref.isoformat(),
            "skipped": True,
            "reason": "retention_disabled",
            "truncated": False,
            "batches_committed": 0,
            "telemetry": {
                "pruned_runs": 0,
                "pruned_impressions": 0,
                "pruned_scoring_batches": 0,
                "pruned_evidence_vectors": 0,
            },
            "embeddings": {
                "removed_orphan_or_virtual": 0,
                "removed_stale_model": 0,
                "removed_dismissed": 0,
            },
            "operational": {
                "pruned_jobs": 0,
                "pruned_association_runs": 0,
                "pruned_association_requests": 0,
                "pruned_association_cache": 0,
                "pruned_notification_deliveries": 0,
            },
        }

    runner = _BatchRunner(
        connection,
        batch_size=batch_size,
        max_duration_seconds=max_duration_seconds,
        clock=clock,
    )
    telemetry = prune_unattributed_telemetry(connection, now=ref, _runner=runner)
    embeddings = prune_stale_and_dismissed_embeddings(connection, _runner=runner)
    operational = prune_operational_tables(connection, now=ref, _runner=runner)

    # Only advance the 6-hour maintenance timestamp when the pass completed
    # without hitting the time slice cap; otherwise leave due so the next
    # scheduler tick continues draining the backlog smoothly.
    if not runner.truncated:
        def _record_last_run(con) -> None:
            con.execute(
                "INSERT INTO settings(key, value, secret) VALUES(?, ?, 0) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, secret=0, "
                "updated_at=CURRENT_TIMESTAMP",
                (MAINTENANCE_LAST_RUN_KEY, ref.isoformat()),
            )

        if connection is not None:
            _record_last_run(connection)
        else:
            with transaction() as con:
                _record_last_run(con)

    return {
        "ran_at": ref.isoformat(),
        "skipped": False,
        "truncated": runner.truncated,
        "batches_committed": runner.batches_committed,
        "telemetry": telemetry,
        "embeddings": embeddings,
        "operational": operational,
    }
