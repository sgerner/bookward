import json
import uuid
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from .database import transaction
from .security import safe_error_message

LOGGER = logging.getLogger(__name__)
JOB_LEASE_SECONDS = 300
JOB_HEARTBEAT_SECONDS = 30

def now(): return datetime.now(timezone.utc).isoformat()

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
    from .database import row
    from .profiles import profile_ids
    from .tenancy import profile_scope
    while not stop.is_set():
        worked = False
        for profile_id in profile_ids():
            if stop.is_set():
                break
            with profile_scope(profile_id):
                with transaction() as con:
                    stale_before = (
                        datetime.now(timezone.utc) - timedelta(seconds=JOB_LEASE_SECONDS)
                    ).isoformat()
                    con.execute(
                        "UPDATE jobs SET status='queued',started_at=NULL,heartbeat_at=NULL,lease_token=NULL "
                        "WHERE status='running' AND (COALESCE(heartbeat_at,started_at) IS NULL "
                        "OR julianday(COALESCE(heartbeat_at,started_at)) IS NULL "
                        "OR julianday(COALESCE(heartbeat_at,started_at)) < julianday(?))",
                        (stale_before,),
                    )
                job = row("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1")
                if not job:
                    continue
                lease_token = uuid.uuid4().hex
                claimed_at = now()
                with transaction() as con:
                    claimed = con.execute(
                        "UPDATE jobs SET status='running',started_at=?,heartbeat_at=?,lease_token=? "
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
                try:
                    result = await handler(job["kind"])
                    with profile_scope(profile_id), transaction() as con:
                        con.execute(
                            "UPDATE jobs SET status='complete',progress=1,result=?,error=NULL,finished_at=?,heartbeat_at=NULL,lease_token=NULL "
                            "WHERE id=? AND status='running' AND lease_token=?",
                            (json.dumps(result), now(), job["id"], lease_token),
                        )
                except Exception as exc:
                    with profile_scope(profile_id), transaction() as con:
                        con.execute(
                            "UPDATE jobs SET status='failed',error=?,finished_at=?,heartbeat_at=NULL,lease_token=NULL "
                            "WHERE id=? AND status='running' AND lease_token=?",
                            (safe_error_message(exc, limit=2000), now(), job["id"], lease_token),
                        )
                finally:
                    heartbeat_stop.set()
                    await asyncio.to_thread(heartbeat.join)
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
