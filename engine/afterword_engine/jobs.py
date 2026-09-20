import json
import uuid
from datetime import datetime, timezone
from .database import transaction
from .security import safe_error_message

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
    import asyncio
    from .database import row
    with transaction() as con: con.execute("UPDATE jobs SET status='queued',started_at=NULL WHERE status='running'")
    while not stop.is_set():
        job = row("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1")
        if not job:
            try: await asyncio.wait_for(stop.wait(), timeout=.5)
            except TimeoutError: pass
            continue
        with transaction() as con:
            claimed = con.execute("UPDATE jobs SET status='running',started_at=? WHERE id=? AND status='queued'", (now(), job["id"])).rowcount
        if not claimed: continue
        try:
            result = await handler(job["kind"])
            with transaction() as con: con.execute("UPDATE jobs SET status='complete',progress=1,result=?,error=NULL,finished_at=? WHERE id=?", (json.dumps(result), now(), job["id"]))
        except Exception as exc:
            with transaction() as con: con.execute("UPDATE jobs SET status='failed',error=?,finished_at=? WHERE id=?", (safe_error_message(exc, limit=2000), now(), job["id"]))
