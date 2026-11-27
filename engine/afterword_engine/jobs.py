import json
import uuid
import asyncio
from contextvars import ContextVar
import logging
from datetime import datetime, timedelta, timezone
from threading import Event, Thread, get_ident
import time
from typing import Any

from .database import (
    RestoreEpochError,
    bind_restore_epoch,
    current_restore_epoch,
    database_lock,
    profile_restore_generation,
    rows,
    transaction,
)
from .security import safe_error_message
from .tenancy import ProfileDeletedError

LOGGER = logging.getLogger(__name__)
JOB_LEASE_SECONDS = 300
JOB_HEARTBEAT_SECONDS = 30
JOB_DEFERRED_HEARTBEAT_RETRY_SECONDS = 1.0
JOB_REAPER_SECONDS = 30
JOB_MAX_ATTEMPTS = 3
PROFILE_RETRY_SECONDS = 60
QUALITY_RECOVERY_COOLDOWN_SECONDS = 60
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


def _claim_profile_job(
    profile_id: str,
    current: float,
    ready_profiles: set[str],
    ready_profile_epochs: dict[str, int],
    seen_restore_generations: dict[str, int],
    last_reaped_at: dict[str, float],
    profile_retry_after: dict[str, float],
    fenced_profiles: set[str],
    last_quality_recovery_at: float | None = None,
):
    from .backups import has_restore_journal, recover_interrupted_restore
    from .database import initialize, row
    from .tenancy import profile_scope

    with profile_scope(profile_id):
        lock = database_lock()
        if lock.is_deleted:
            raise ProfileDeletedError("Profile has been deleted")
        if lock.is_locked():
            return ("busy", None, None, None)
        recovered = False
        if has_restore_journal():
            recovered = recover_interrupted_restore()
            if recovered:
                fenced_profiles.discard(profile_id)
                ready_profiles.discard(profile_id)
                ready_profile_epochs.pop(profile_id, None)
                last_reaped_at.pop(profile_id, None)
        else:
            fenced_profiles.discard(profile_id)
        with lock.try_shared() as acquired:
            if not acquired:
                return ("busy", None, None, None)
            generation = profile_restore_generation()
            if seen_restore_generations.get(profile_id, 0) != generation:
                seen_restore_generations[profile_id] = generation
                ready_profiles.discard(profile_id)
                ready_profile_epochs.pop(profile_id, None)
                last_reaped_at.pop(profile_id, None)
                profile_retry_after.pop(profile_id, None)
            if current < profile_retry_after.get(profile_id, 0):
                return ("backoff", None, None, None)
            epoch = (
                current_restore_epoch()
                if (not recovered and profile_id in ready_profiles)
                else None
            )
            if (
                profile_id in ready_profiles
                and ready_profile_epochs.get(profile_id) != epoch
            ):
                ready_profiles.discard(profile_id)
                ready_profile_epochs.pop(profile_id, None)
                last_reaped_at.pop(profile_id, None)
            if recovered or profile_id not in ready_profiles:
                initialize(seed_demo=False)
                epoch = current_restore_epoch()
                ready_profiles.add(profile_id)
                ready_profile_epochs[profile_id] = epoch
                seen_restore_generations[profile_id] = profile_restore_generation()
            assert epoch is not None
            with bind_restore_epoch(epoch):
                if (
                    profile_id not in last_reaped_at
                    or current - last_reaped_at[profile_id] >= JOB_REAPER_SECONDS
                ):
                    _reap_abandoned_jobs()
                    last_reaped_at[profile_id] = current
                cooldown = max(0.0, float(QUALITY_RECOVERY_COOLDOWN_SECONDS))
                recovery_throttled = bool(
                    cooldown > 0
                    and last_quality_recovery_at is not None
                    and current - last_quality_recovery_at < cooldown
                )
                if not recovery_throttled and cooldown > 0:
                    cutoff = (
                        datetime.now(timezone.utc) - timedelta(seconds=cooldown)
                    ).isoformat()
                    recovery_throttled = bool(
                        row(
                            "SELECT 1 FROM jobs WHERE kind='candidate_quality_recovery' "
                            "AND status='complete' AND finished_at IS NOT NULL "
                            "AND julianday(finished_at) > julianday(?) LIMIT 1",
                            (cutoff,),
                        )
                    )
                if recovery_throttled:
                    job = row(
                        "SELECT * FROM jobs WHERE status='queued' "
                        "AND kind!='candidate_quality_recovery' ORDER BY created_at LIMIT 1"
                    )
                else:
                    job = row(
                        "SELECT * FROM jobs WHERE status='queued' "
                        "ORDER BY CASE WHEN kind='candidate_quality_recovery' THEN 1 ELSE 0 END, "
                        "created_at LIMIT 1"
                    )
                if not job:
                    return ("idle", None, None, None)
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
                    return ("idle", None, None, None)
                return ("claimed", job, lease_token, epoch)


def _complete_claimed_job(
    profile_id: str, job_id: str, lease_token: str, epoch: int, result
) -> None:
    from .tenancy import profile_scope

    with profile_scope(profile_id), bind_restore_epoch(epoch), transaction() as con:
        con.execute(
            "UPDATE jobs SET status='complete',progress=1,result=?,error=NULL,"
            "finished_at=?,heartbeat_at=NULL,lease_token=NULL "
            "WHERE id=? AND status='running' AND lease_token=?",
            (json.dumps(result), now(), job_id, lease_token),
        )


def _fail_claimed_job(
    profile_id: str, job_id: str, lease_token: str, epoch: int, error_message: str
) -> None:
    from .tenancy import profile_scope

    with profile_scope(profile_id), bind_restore_epoch(epoch), transaction() as con:
        con.execute(
            "UPDATE jobs SET status='failed',error=?,finished_at=?,"
            "heartbeat_at=NULL,lease_token=NULL "
            "WHERE id=? AND status='running' AND lease_token=?",
            (error_message, now(), job_id, lease_token),
        )


def _requeue_aborted_job(
    profile_id: str, job_id: str, lease_token: str, epoch: int
) -> bool:
    """Return a cancelled job to ``queued`` without waiting on an exclusive lock."""

    from .tenancy import profile_scope

    with (
        profile_scope(profile_id),
        database_lock().try_shared(allow_during_quiesce=True) as acquired,
    ):
        if not acquired:
            return False
        with bind_restore_epoch(epoch), transaction() as con:
            updated = con.execute(
                "UPDATE jobs SET status='queued',started_at=NULL,heartbeat_at=NULL,"
                "lease_token=NULL,attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END "
                "WHERE id=? AND status='running' AND lease_token=?",
                (job_id, lease_token),
            ).rowcount
        return bool(updated)


async def worker_loop(handler, stop):
    from .backups import has_restore_journal
    from .profiles import profile_ids
    from .tenancy import profile_scope

    loop = asyncio.get_running_loop()
    loop_thread_id = get_ident()
    last_reaped_at: dict[str, float] = {}
    ready_profiles: set[str] = set()
    ready_profile_epochs: dict[str, int] = {}
    seen_restore_generations: dict[str, int] = {}
    profile_retry_after: dict[str, float] = {}
    fenced_profiles: set[str] = set()
    last_quality_recovery_at: float | None = None

    def reset_profile_ready(profile_id: str) -> None:
        ready_profiles.discard(profile_id)
        ready_profile_epochs.pop(profile_id, None)
        last_reaped_at.pop(profile_id, None)

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
            try:
                with profile_scope(profile_id):
                    lock = database_lock()
                    if lock.is_deleted:
                        raise ProfileDeletedError("Profile has been deleted")
                    if lock.is_locked():
                        continue
                    generation = profile_restore_generation()
                    if seen_restore_generations.get(profile_id, 0) != generation:
                        seen_restore_generations[profile_id] = generation
                        reset_profile_ready(profile_id)
                        profile_retry_after.pop(profile_id, None)
                        fenced_profiles.discard(profile_id)
                if current < profile_retry_after.get(profile_id, 0):
                    if profile_id in fenced_profiles:
                        has_journal = await asyncio.to_thread(
                            has_restore_journal, profile_id
                        )
                        if not has_journal:
                            profile_retry_after.pop(profile_id, None)
                            fenced_profiles.discard(profile_id)
                        else:
                            continue
                    else:
                        continue
                status, job, lease_token, epoch = await asyncio.to_thread(
                    _claim_profile_job,
                    profile_id,
                    current,
                    ready_profiles,
                    ready_profile_epochs,
                    seen_restore_generations,
                    last_reaped_at,
                    profile_retry_after,
                    fenced_profiles,
                    last_quality_recovery_at,
                )
                if status != "claimed":
                    continue
                worked = True
                assert job is not None and lease_token is not None and epoch is not None
                lease_started_monotonic = time.monotonic()
                with profile_scope(profile_id), bind_restore_epoch(epoch):
                    heartbeat_stop = Event()
                    job_aborted = Event()
                    job_token = _ACTIVE_JOB_CONTEXT.set((job["id"], lease_token, stop))
                    handler_task = asyncio.create_task(handler(job["kind"]))

                    def cancel_handler() -> None:
                        job_aborted.set()
                        if get_ident() == loop_thread_id:
                            handler_task.cancel()
                            return
                        try:
                            loop.call_soon_threadsafe(handler_task.cancel)
                        except RuntimeError:
                            pass

                    try:
                        with lock.track_worker_job(cancel_handler, loop_thread_id):
                            heartbeat = Thread(
                                target=_heartbeat,
                                args=(
                                    profile_id,
                                    job["id"],
                                    lease_token,
                                    heartbeat_stop,
                                    cancel_handler,
                                    epoch,
                                ),
                                kwargs={
                                    "lease_started_monotonic": lease_started_monotonic,
                                },
                                name=f"job-heartbeat-{job['id'][:8]}",
                                daemon=True,
                            )
                            heartbeat.start()
                            should_record_recovery_cooldown = False
                            try:
                                result = await handler_task
                                await asyncio.to_thread(
                                    _complete_claimed_job,
                                    profile_id,
                                    job["id"],
                                    lease_token,
                                    epoch,
                                    result,
                                )
                                if job_aborted.is_set():
                                    reset_profile_ready(profile_id)
                                else:
                                    should_record_recovery_cooldown = True
                            except asyncio.CancelledError:
                                outer = asyncio.current_task()
                                outer_cancelling = (
                                    outer is not None and outer.cancelling() > 0
                                )
                                if not job_aborted.is_set() or outer_cancelling:
                                    handler_task.cancel()
                                    await asyncio.gather(
                                        handler_task, return_exceptions=True
                                    )
                                reset_profile_ready(profile_id)
                                try:
                                    await asyncio.to_thread(
                                        _requeue_aborted_job,
                                        profile_id,
                                        job["id"],
                                        lease_token,
                                        epoch,
                                    )
                                except ProfileDeletedError:
                                    raise
                                except RestoreEpochError:
                                    pass
                                except asyncio.CancelledError:
                                    if not outer_cancelling:
                                        raise
                                except Exception:
                                    LOGGER.debug(
                                        "Could not immediately requeue cancelled job %s",
                                        job["id"],
                                        exc_info=True,
                                    )
                                if not job_aborted.is_set() or outer_cancelling:
                                    raise
                            except ProfileDeletedError:
                                raise
                            except RestoreEpochError:
                                reset_profile_ready(profile_id)
                            except Exception as exc:
                                if job_aborted.is_set():
                                    reset_profile_ready(profile_id)
                                else:
                                    should_record_recovery_cooldown = True
                                try:
                                    await asyncio.to_thread(
                                        _fail_claimed_job,
                                        profile_id,
                                        job["id"],
                                        lease_token,
                                        epoch,
                                        safe_error_message(exc, limit=2000),
                                    )
                                except ProfileDeletedError:
                                    raise
                                except RestoreEpochError:
                                    reset_profile_ready(profile_id)
                            finally:
                                if (
                                    should_record_recovery_cooldown
                                    and job["kind"] == "candidate_quality_recovery"
                                ):
                                    last_quality_recovery_at = time.monotonic()
                                heartbeat_stop.set()
                                outer = asyncio.current_task()
                                if outer is not None and outer.cancelling() > 0:
                                    heartbeat.join(timeout=1.0)
                                else:
                                    try:
                                        await asyncio.to_thread(heartbeat.join)
                                    except asyncio.CancelledError:
                                        heartbeat.join(timeout=1.0)
                                        raise
                    finally:
                        _ACTIVE_JOB_CONTEXT.reset(job_token)
            except ProfileDeletedError:
                reset_profile_ready(profile_id)
                seen_restore_generations.pop(profile_id, None)
                fenced_profiles.discard(profile_id)
                profile_retry_after.pop(profile_id, None)
            except RestoreEpochError:
                reset_profile_ready(profile_id)
            except Exception:
                reset_profile_ready(profile_id)
                try:
                    has_journal = await asyncio.to_thread(
                        has_restore_journal, profile_id
                    )
                except Exception:
                    has_journal = False
                if has_journal:
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
    on_lost_lease=None,
    expected_epoch: int | None = None,
    *,
    lease_started_monotonic: float | None = None,
) -> None:
    """Keep a live job leased so another worker cannot mistake it for a crash.

    When an exclusive profile lock defers renewal, ``try_shared()`` avoids
    piling up blocked SQLite connections and retries every
    ``JOB_DEFERRED_HEARTBEAT_RETRY_SECONDS`` so normal restores renew as soon
    as cutover finishes.  If the lock outlives ``JOB_LEASE_SECONDS``, the lease
    can no longer be guaranteed against ``_reap_abandoned_jobs()``, so
    ``on_lost_lease`` cancels the in-flight handler before another worker can
    execute a duplicate copy.
    """

    from .tenancy import profile_scope

    last_renewed_monotonic = (
        time.monotonic()
        if lease_started_monotonic is None
        else lease_started_monotonic
    )
    last_deferred_log_monotonic: float | None = None
    wait_seconds: float = min(JOB_HEARTBEAT_SECONDS, max(0.0, JOB_LEASE_SECONDS))

    while not stop.wait(wait_seconds):
        try:
            with profile_scope(profile_id), database_lock().try_shared() as acquired:
                if not acquired:
                    now_monotonic = time.monotonic()
                    if now_monotonic - last_renewed_monotonic >= JOB_LEASE_SECONDS:
                        LOGGER.warning(
                            "Lease expired for job %s while profile database lock was held; cancelling active handler",
                            job_id,
                        )
                        if on_lost_lease is not None:
                            on_lost_lease()
                        return
                    if (
                        last_deferred_log_monotonic is None
                        or now_monotonic - last_deferred_log_monotonic
                        >= JOB_HEARTBEAT_SECONDS
                    ):
                        LOGGER.info(
                            "Deferred lease renewal for job %s while profile database lock is held",
                            job_id,
                        )
                        last_deferred_log_monotonic = now_monotonic
                    remaining_lease = max(
                        0.0,
                        JOB_LEASE_SECONDS - (now_monotonic - last_renewed_monotonic),
                    )
                    wait_seconds = min(
                        JOB_HEARTBEAT_SECONDS,
                        JOB_DEFERRED_HEARTBEAT_RETRY_SECONDS,
                        remaining_lease,
                    )
                    continue
                with bind_restore_epoch(expected_epoch), transaction() as con:
                    updated = con.execute(
                        "UPDATE jobs SET heartbeat_at=? "
                        "WHERE id=? AND status='running' AND lease_token=?",
                        (now(), job_id, lease_token),
                    ).rowcount
        except (ProfileDeletedError, RestoreEpochError):
            if on_lost_lease is not None:
                on_lost_lease()
            return
        except Exception:
            now_monotonic = time.monotonic()
            if now_monotonic - last_renewed_monotonic >= JOB_LEASE_SECONDS:
                LOGGER.warning(
                    "Lease expired for job %s after renewal errors; cancelling active handler",
                    job_id,
                )
                if on_lost_lease is not None:
                    on_lost_lease()
                return
            LOGGER.exception("Could not renew lease for job %s", job_id)
            wait_seconds = min(
                JOB_HEARTBEAT_SECONDS,
                JOB_DEFERRED_HEARTBEAT_RETRY_SECONDS,
                max(0.0, JOB_LEASE_SECONDS - (now_monotonic - last_renewed_monotonic)),
            )
            continue
        if not updated:
            if on_lost_lease is not None:
                on_lost_lease()
            return
        last_renewed_monotonic = time.monotonic()
        last_deferred_log_monotonic = None
        wait_seconds = min(JOB_HEARTBEAT_SECONDS, max(0.0, JOB_LEASE_SECONDS))
