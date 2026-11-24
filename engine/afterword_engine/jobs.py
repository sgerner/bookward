import json
import uuid
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
import time

from .database import rows, transaction
from .security import safe_error_message

LOGGER = logging.getLogger(__name__)
JOB_LEASE_SECONDS = 300
JOB_HEARTBEAT_SECONDS = 30
JOB_REAPER_SECONDS = 30
JOB_MAX_ATTEMPTS = 3
PROFILE_RETRY_SECONDS = 60

def now(): return datetime.now(timezone.utc).isoformat()


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

def _poll_and_claim_job(
    profile_id: str,
    *,
    need_init: bool,
    need_reap: bool,
) -> tuple[str, bool, bool, tuple[dict, str] | None]:
    from .database import database_lock, initialize, row
    from .tenancy import profile_scope

    with profile_scope(profile_id), database_lock().try_shared() as acquired:
        if not acquired:
            return "locked", False, False, None
        initialized = False
        if need_init:
            initialize(seed_demo=False)
            initialized = True
        reaped = False
        if need_reap:
            _reap_abandoned_jobs()
            reaped = True
        job = row("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1")
        if not job:
            return "idle", initialized, reaped, None
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
            return "idle", initialized, reaped, None
        return "claimed", initialized, reaped, (job, lease_token)


def _complete_job(profile_id: str, job_id: str, lease_token: str, result) -> None:
    from .tenancy import profile_scope

    encoded = json.dumps(result)
    with profile_scope(profile_id), transaction() as con:
        con.execute(
            "UPDATE jobs SET status='complete',progress=1,result=?,error=NULL,"
            "finished_at=?,heartbeat_at=NULL,lease_token=NULL "
            "WHERE id=? AND status='running' AND lease_token=?",
            (encoded, now(), job_id, lease_token),
        )


def _fail_job(profile_id: str, job_id: str, lease_token: str, exc: BaseException) -> None:
    from .tenancy import profile_scope

    error_text = safe_error_message(exc, limit=2000)
    with profile_scope(profile_id), transaction() as con:
        con.execute(
            "UPDATE jobs SET status='failed',error=?,finished_at=?,"
            "heartbeat_at=NULL,lease_token=NULL "
            "WHERE id=? AND status='running' AND lease_token=?",
            (error_text, now(), job_id, lease_token),
        )


async def worker_loop(handler, stop):
    from .profiles import profile_ids
    from .tenancy import profile_scope
    last_reaped_at: dict[str, float] = {}
    ready_profiles: set[str] = set()
    profile_retry_after: dict[str, float] = {}
    while not stop.is_set():
        worked = False
        try:
            active_profiles = await asyncio.to_thread(profile_ids)
        except Exception:
            LOGGER.exception("Worker could not enumerate active profiles")
            active_profiles = []
        for profile_id in active_profiles:
            if stop.is_set():
                break
            current = time.monotonic()
            if current < profile_retry_after.get(profile_id, 0):
                continue
            need_init = profile_id not in ready_profiles
            need_reap = (
                profile_id not in last_reaped_at
                or current - last_reaped_at[profile_id] >= JOB_REAPER_SECONDS
            )
            try:
                state, initialized, reaped, claimed_job = await asyncio.to_thread(
                    _poll_and_claim_job,
                    profile_id,
                    need_init=need_init,
                    need_reap=need_reap,
                )
                if initialized:
                    ready_profiles.add(profile_id)
                if reaped:
                    last_reaped_at[profile_id] = current
                if state != "claimed" or claimed_job is None:
                    continue
                job, lease_token = claimed_job
                worked = True
                heartbeat_stop = Event()
                heartbeat = Thread(
                    target=_heartbeat,
                    args=(profile_id, job["id"], lease_token, heartbeat_stop),
                    name=f"job-heartbeat-{job['id'][:8]}",
                    daemon=True,
                )
                heartbeat.start()
                try:
                    with profile_scope(profile_id):
                        handler_task = asyncio.create_task(handler(job["kind"]))
                    result = await handler_task
                    await asyncio.to_thread(
                        _complete_job, profile_id, job["id"], lease_token, result
                    )
                except asyncio.CancelledError:
                    handler_task.cancel()
                    await asyncio.gather(handler_task, return_exceptions=True)
                    raise
                except Exception as exc:
                    await asyncio.to_thread(
                        _fail_job, profile_id, job["id"], lease_token, exc
                    )
                finally:
                    heartbeat_stop.set()
                    try:
                        await asyncio.to_thread(heartbeat.join)
                    except asyncio.CancelledError:
                        heartbeat.join(timeout=1.0)
                        raise
            except Exception:
                ready_profiles.discard(profile_id)
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

    from .database import database_lock
    from .tenancy import profile_scope

    while not stop.wait(JOB_HEARTBEAT_SECONDS):
        try:
            with profile_scope(profile_id), database_lock().try_shared() as acquired:
                if not acquired:
                    LOGGER.info(
                        "Deferred lease renewal for job %s while profile database lock is held",
                        job_id,
                    )
                    continue
                with transaction() as con:
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
