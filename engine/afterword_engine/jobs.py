import json
import uuid
import asyncio
from contextvars import ContextVar
import logging
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
import time
from typing import Any

from .database import rows, transaction
from .security import safe_error_message

LOGGER = logging.getLogger(__name__)
JOB_LEASE_SECONDS = 300
JOB_HEARTBEAT_SECONDS = 30
JOB_REAPER_SECONDS = 30
JOB_MAX_ATTEMPTS = 3
PROFILE_RETRY_SECONDS = 60
_ACTIVE_JOB_CONTEXT: ContextVar[tuple[str, str, Any] | None] = ContextVar(
    "afterword_active_job", default=None
)

def now(): return datetime.now(timezone.utc).isoformat()


def current_job_context() -> tuple[str, str, Any] | None:
    """Return the active (job_id, lease_token, stop_event) in this worker task."""

    return _ACTIVE_JOB_CONTEXT.get()


def current_job_stop_requested(stop: Any = None) -> bool:
    """Return whether the caller's or worker's stop signal is set."""

    if stop is not None:
        return bool(stop.is_set())
    context = _ACTIVE_JOB_CONTEXT.get()
    return bool(context and context[2] is not None and context[2].is_set())


def update_job_progress(
    progress: float,
    *,
    job_id: str | None = None,
    con=None,
) -> int:
    """Persist fractional job progress, reusing an open transaction when given."""

    bounded = max(0.0, min(1.0, float(progress)))
    context = _ACTIVE_JOB_CONTEXT.get()

    def _apply(connection) -> int:
        if job_id is not None:
            return connection.execute(
                "UPDATE jobs SET progress=? WHERE id=? AND status='running'",
                (bounded, job_id),
            ).rowcount
        if context is not None:
            return connection.execute(
                "UPDATE jobs SET progress=? WHERE id=? AND status='running' AND lease_token=?",
                (bounded, context[0], context[1]),
            ).rowcount
        return connection.execute(
            "UPDATE jobs SET progress=? WHERE id=("
            "SELECT id FROM jobs WHERE kind IN ('candidate_quality','candidate_quality_recovery') "
            "AND status='running' ORDER BY rowid DESC LIMIT 1)",
            (bounded,),
        ).rowcount

    if con is not None:
        return _apply(con)
    with transaction() as managed:
        return _apply(managed)


def _reap_abandoned_jobs() -> int:
    """Recover expired leases, failing jobs after bounded attempts."""

    stale_before = (
        datetime.now(timezone.utc) - timedelta(seconds=JOB_LEASE_SECONDS)
    ).isoformat()
    running = rows(
        "SELECT id,lease_token,attempts,"
        "CASE WHEN COALESCE(heartbeat_at,started_at) IS NULL "
        "OR julianday(COALESCE(heartbeat_at,started_at)) IS NULL "
        "OR julianday(COALESCE(heartbeat_at,started_at)) < julianday(?) "
        "THEN 1 ELSE 0 END AS expired "
        "FROM jobs WHERE status='running'",
        (stale_before,),
    )
    abandoned = [item for item in running if item["expired"]]
    if not abandoned:
        return 0

    recovered = 0
    with transaction() as con:
        for item in abandoned:
            if int(item["attempts"] or 0) >= JOB_MAX_ATTEMPTS:
                updated = con.execute(
                    "UPDATE jobs SET status='failed',error=?,finished_at=?,"
                    "heartbeat_at=NULL,lease_token=NULL "
                    "WHERE id=? AND status='running' AND lease_token IS ? "
                    "AND (COALESCE(heartbeat_at,started_at) IS NULL "
                    "OR julianday(COALESCE(heartbeat_at,started_at)) IS NULL "
                    "OR julianday(COALESCE(heartbeat_at,started_at)) < julianday(?))",
                    (
                        "Job lease expired after maximum attempts",
                        now(),
                        item["id"],
                        item["lease_token"],
                        stale_before,
                    ),
                ).rowcount
            else:
                updated = con.execute(
                    "UPDATE jobs SET status='queued',started_at=NULL,"
                    "heartbeat_at=NULL,lease_token=NULL "
                    "WHERE id=? AND status='running' AND lease_token IS ? "
                    "AND (COALESCE(heartbeat_at,started_at) IS NULL "
                    "OR julianday(COALESCE(heartbeat_at,started_at)) IS NULL "
                    "OR julianday(COALESCE(heartbeat_at,started_at)) < julianday(?))",
                    (item["id"], item["lease_token"], stale_before),
                ).rowcount
            recovered += updated
    return recovered

def enqueue_job(kind: str, dedupe: bool = False):
    with transaction() as con:
        if dedupe:
            existing = con.execute(
                "SELECT id FROM jobs WHERE kind=? AND status IN ('queued','running') LIMIT 1",
                (kind,),
            ).fetchone()
            if existing:
                return existing[0]
        job_id = str(uuid.uuid4())
        con.execute("INSERT INTO jobs(id,kind,status) VALUES(?,?,'queued')", (job_id, kind))
        return job_id

async def worker_loop(handler, stop):
    from .backups import has_restore_journal, recover_interrupted_restore
    from .database import row
    from .database import initialize
    from .profiles import profile_ids
    from .tenancy import profile_scope
    last_reaped_at: dict[str, float] = {}
    ready_profiles: set[str] = set()
    profile_retry_after: dict[str, float] = {}
    fenced_profiles: set[str] = set()
    while not stop.is_set():
        worked = False
        for profile_id in profile_ids():
            if stop.is_set():
                break
            current = time.monotonic()
            if current < profile_retry_after.get(profile_id, 0):
                if profile_id in fenced_profiles:
                    if not has_restore_journal(profile_id):
                        profile_retry_after.pop(profile_id, None)
                        fenced_profiles.discard(profile_id)
                    else:
                        continue
                else:
                    continue
            try:
                with profile_scope(profile_id):
                    recovered = (
                        recover_interrupted_restore()
                        if has_restore_journal()
                        else False
                    )
                    fenced_profiles.discard(profile_id)
                    if recovered or profile_id not in ready_profiles:
                        initialize(seed_demo=False)
                        ready_profiles.add(profile_id)
                    if (
                        profile_id not in last_reaped_at
                        or current - last_reaped_at[profile_id] >= JOB_REAPER_SECONDS
                    ):
                        _reap_abandoned_jobs()
                        last_reaped_at[profile_id] = current
                    job = row("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1")
                    if not job:
                        continue
                    lease_token = uuid.uuid4().hex
                    claimed_at = now()
                    with transaction() as con:
                        claimed = con.execute(
                            "UPDATE jobs SET status='running',started_at=?,heartbeat_at=?,"
                            "lease_token=?,attempts=attempts+1 "
                            "WHERE id=? AND status='queued'",
                            (claimed_at, claimed_at, lease_token, job["id"]),
                        ).rowcount
                    if not claimed:
                        continue
                    worked = True
                    heartbeat_stop = Event()
                    heartbeat = Thread(
                        target=_heartbeat,
                        args=(profile_id, job["id"], lease_token, heartbeat_stop),
                        name=f"job-heartbeat-{job['id'][:8]}",
                        daemon=True,
                    )
                    heartbeat.start()
                    job_token = _ACTIVE_JOB_CONTEXT.set((job["id"], lease_token, stop))
                    try:
                        result = await handler(job["kind"])
                        with transaction() as con:
                            con.execute(
                                "UPDATE jobs SET status='complete',progress=1,result=?,error=NULL,finished_at=?,heartbeat_at=NULL,lease_token=NULL "
                                "WHERE id=? AND status='running' AND lease_token=?",
                                (json.dumps(result), now(), job["id"], lease_token),
                            )
                    except Exception as exc:
                        with transaction() as con:
                            con.execute(
                                "UPDATE jobs SET status='failed',error=?,finished_at=?,heartbeat_at=NULL,lease_token=NULL "
                                "WHERE id=? AND status='running' AND lease_token=?",
                                (safe_error_message(exc, limit=2000), now(), job["id"], lease_token),
                            )
                    finally:
                        _ACTIVE_JOB_CONTEXT.reset(job_token)
                        heartbeat_stop.set()
                        await asyncio.to_thread(heartbeat.join)
            except Exception:
                ready_profiles.discard(profile_id)
                if has_restore_journal(profile_id):
                    fenced_profiles.add(profile_id)
                profile_retry_after[profile_id] = current + PROFILE_RETRY_SECONDS
                LOGGER.exception("Worker skipped unavailable profile %s", profile_id)
        if not worked:
            try: await asyncio.wait_for(stop.wait(), timeout=.5)
            except TimeoutError: pass


def _heartbeat(
    profile_id: str,
    job_id: str,
    lease_token: str,
    stop: Event,
) -> None:
    """Keep a live job leased so another worker cannot mistake it for a crash."""

    from .tenancy import profile_scope

    while not stop.wait(JOB_HEARTBEAT_SECONDS):
        try:
            with profile_scope(profile_id), transaction() as con:
                updated = con.execute(
                    "UPDATE jobs SET heartbeat_at=? "
                    "WHERE id=? AND status='running' AND lease_token=?",
                    (now(), job_id, lease_token),
                ).rowcount
        except Exception:
            LOGGER.exception("Could not renew lease for job %s", job_id)
            continue
        if not updated:
            return
