import hashlib
import json
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlparse
from contextlib import asynccontextmanager
import httpx
from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, HttpUrl, field_validator
from .config import settings
from .database import initialize, row, rows, transaction
from .api_tokens import (
    generate_api_token,
    hash_api_token,
    legacy_hash_api_token,
    token_prefix,
)
from .embeddings import get_embedder
from .covers import canonical_book_source_url, fallback_cover_url
from .ingestion import import_goodreads_csv, import_goodreads_rss, preview_source, refresh_missing_candidate_metadata, scan_source
from .security import validate_public_url, validate_service_url
from .jobs import enqueue_job, worker_loop
from .scoring import rebuild_all_embeddings, score_all
from .secrets import seal, unseal
from .librarr import download as librarr_download, normalize_media_type, search as librarr_search
from .llm_catalog import get_catalog
from .llm import validate_endpoint
from .llm_shadow import run_shadow_policy, safe_connections, safe_policies
from .digest import (
    digest_config,
    digest_is_due,
    digest_preview,
    retry_delivery,
    safe_digest_settings,
    send_digest,
    validate_digest_config,
)
from .learning import (
    EVENT_TYPES,
    create_recommendation_run,
    record_event_in_connection,
    record_events,
)

SOURCE_SYNC_MIN_HOURS = 0
SOURCE_SYNC_MAX_HOURS = 720
SOURCE_SYNC_ERROR_RETRY_SECONDS = 300
SCHEDULER_INITIAL_DELAY_SECONDS = 5
SCHEDULER_POLL_SECONDS = 60


def source_sync_interval_hours():
    """Return the persisted permanent-source cadence in hours.

    ``0`` means manual-only. Keeping the value in SQLite makes the schedule
    portable across Docker restarts while the environment setting remains a
    useful first-install default.
    """

    raw = private_settings().get(
        "source_sync_interval_hours", str(settings.source_sync_interval_hours)
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = settings.source_sync_interval_hours
    return max(SOURCE_SYNC_MIN_HOURS, min(SOURCE_SYNC_MAX_HOURS, value))


def source_sync_is_due():
    """Whether at least one enabled permanent source needs a scheduled scan."""

    interval = source_sync_interval_hours()
    if interval <= 0:
        return False
    now = datetime.now(timezone.utc)
    sources = rows(
        "SELECT last_scanned_at,last_status FROM sources "
        "WHERE enabled=1 AND kind!='builtin' AND lifecycle='permanent'"
    )
    for source in sources:
        if not source["last_scanned_at"]:
            return True
        try:
            last = datetime.fromisoformat(
                str(source["last_scanned_at"]).replace(" ", "T").replace("Z", "+00:00")
            )
        except ValueError:
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if str(source["last_status"] or "").startswith("error:"):
            if now - last >= timedelta(seconds=SOURCE_SYNC_ERROR_RETRY_SECONDS):
                return True
            continue
        if now - last >= timedelta(hours=interval):
            return True
    return False


def active_job(kind: str):
    return row(
        "SELECT 1 FROM jobs WHERE kind=? AND status IN ('queued','running') LIMIT 1",
        (kind,),
    )


def queue_digest_if_due():
    """Schedule a digest immediately after scoring, when its window is open."""

    try:
        if digest_is_due(private_settings()) and not active_job("digest"):
            return enqueue_job("digest", dedupe=True)
    except Exception:
        return None
    return None


async def source_scheduler_loop(stop):
    """Queue a single sync when permanent sources become due.

    The loop lives in the engine so Docker installs do not need cron or a
    host-specific scheduler. A minute-level poll is deliberately simple and
    bounded; the actual cadence is persisted in ``settings``.
    """

    try:
        await asyncio.wait_for(stop.wait(), timeout=SCHEDULER_INITIAL_DELAY_SECONDS)
    except TimeoutError:
        pass
    while not stop.is_set():
        try:
            if source_sync_is_due() and not active_job("sync"):
                enqueue_job("sync", dedupe=True)
        except Exception:
            # A transient database/read error must not kill the scheduler. The
            # next poll will retry without affecting the API or worker.
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=SCHEDULER_POLL_SECONDS)
        except TimeoutError:
            pass


async def digest_scheduler_loop(stop):
    """Queue the configured weekly digest without relying on host cron."""

    try:
        await asyncio.wait_for(stop.wait(), timeout=SCHEDULER_INITIAL_DELAY_SECONDS + 2)
    except TimeoutError:
        pass
    while not stop.is_set():
        try:
            if digest_is_due(private_settings()) and not active_job("digest"):
                enqueue_job("digest", dedupe=True)
        except Exception:
            # Delivery errors belong to the persisted job. A malformed legacy
            # setting or transient DB error must not stop future source scans.
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=SCHEDULER_POLL_SECONDS)
        except TimeoutError:
            pass

async def handle_job(kind: str):
    config = private_settings()
    if kind == "digest":
        return await send_digest(config)
    if kind in {"digest:manual", "digest:force"}:
        # Manual runs bypass the time window through the job kind; the same
        # idempotent candidate/channel rules still apply to avoid duplicates.
        return await send_digest(config)
    if kind.startswith("digest:test:"):
        channel = kind.partition(":test:")[2]
        return await send_digest(config, test_channel=channel)
    if kind.startswith("notification_retry:"):
        delivery_id = kind.partition(":")[2]
        return await retry_delivery(delivery_id, config)
    if kind.startswith("llm_shadow:"):
        try:
            policy_id = int(kind.partition(":")[2])
        except ValueError as exc:
            raise ValueError("Invalid shadow policy job") from exc
        return await run_shadow_policy(policy_id)
    if kind == "rebuild_embeddings":
        # Embedding vectors are model-specific. Rebuild the selected provider's
        # set first, then remove incompatible vectors only after success so an
        # unavailable remote provider cannot erase the previous cache.
        result = await rebuild_all_embeddings(config.get("embedding_backend"), config.get("embedding_model"), config.get("embedding_url"), config.get("embedding_api_key"))
        with transaction() as con:
            con.execute(
                "DELETE FROM embeddings WHERE backend != ? OR model != ?",
                (result["backend"], result["model"]),
            )
        return {"embeddings": result["embeddings"], "scored": result["scored"]}
    if kind == "score":
        scored = await score_all(config.get("embedding_backend"),config.get("embedding_model"),config.get("embedding_url"),config.get("embedding_api_key"))
        return {"scored": scored, "digest_job_id": queue_digest_if_due()}
    if kind == "metadata":
        return {"metadata": await refresh_missing_candidate_metadata()}
    if kind.startswith("source:"):
        try:
            source_id = int(kind.partition(":")[2])
        except ValueError as exc:
            raise ValueError("Invalid source job") from exc
        source = row("SELECT * FROM sources WHERE id=?", (source_id,))
        if not source or source["kind"] == "builtin":
            raise ValueError("Source not found")
        if not source["enabled"]:
            return {"skipped": "disabled", "source_id": source_id}
        if source["lifecycle"] == "one_time" and source["last_scanned_at"]:
            return {"skipped": "already_scanned", "source_id": source_id}
        try:
            collected = await scan_source(source)
        except Exception as exc:
            message = str(exc)[:400]
            with transaction() as con:
                con.execute(
                    "UPDATE sources SET last_status=?, "
                    "last_scanned_at=CASE WHEN lifecycle='one_time' THEN last_scanned_at ELSE CURRENT_TIMESTAMP END "
                    "WHERE id=?",
                    (f"error:{message}", source_id),
                )
            raise
        metadata = await refresh_missing_candidate_metadata()
        scored = await score_all(config.get("embedding_backend"),config.get("embedding_model"),config.get("embedding_url"),config.get("embedding_api_key"))
        digest_job_id = queue_digest_if_due()
        return {"collected":collected,"metadata":metadata,"scored":scored,"source_id":source_id,"digest_job_id":digest_job_id}
    if kind == "sync":
        total = 0
        errors = []
        sources = rows(
            "SELECT * FROM sources WHERE enabled=1 AND kind!='builtin' "
            "AND (lifecycle='permanent' OR (lifecycle='one_time' AND last_scanned_at IS NULL))"
        )
        for source in sources:
            try:
                total += await scan_source(source)
            except Exception as exc:
                message = str(exc)[:400]
                with transaction() as con:
                    con.execute(
                        "UPDATE sources SET last_status=?, "
                        "last_scanned_at=CASE WHEN lifecycle='one_time' THEN last_scanned_at ELSE CURRENT_TIMESTAMP END "
                        "WHERE id=?",
                        (f"error:{message}", source["id"]),
                    )
                errors.append({"id": source["id"], "name": source["name"], "error": message})
        metadata = await refresh_missing_candidate_metadata()
        scored = await score_all(config.get("embedding_backend"),config.get("embedding_model"),config.get("embedding_url"),config.get("embedding_api_key"))
        digest_job_id = queue_digest_if_due()
        return {"collected":total,"metadata":metadata,"scored":scored,"errors":errors,"digest_job_id":digest_job_id}
    raise ValueError(f"Unsupported job kind: {kind}")

@asynccontextmanager
async def lifespan(app):
    initialize()
    stop = asyncio.Event(); worker = asyncio.create_task(worker_loop(handle_job, stop)); scheduler = asyncio.create_task(source_scheduler_loop(stop)); digest_scheduler = asyncio.create_task(digest_scheduler_loop(stop))
    # Backfill older rows in the worker so catalog lookups never delay API
    # startup. The dedupe flag keeps restarts from creating duplicate work.
    enqueue_job("metadata", dedupe=True)
    yield
    stop.set(); await scheduler; await digest_scheduler; await worker

app = FastAPI(title="Bookward Engine", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    # The web app's public proxy owns cross-origin access. Keep the engine's
    # internal surface restricted to its configured browser origin so exposing
    # the engine port does not turn every legacy endpoint into a wildcard CORS
    # API.
    allow_origins=[settings.cors_origin],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["accept", "content-type", "authorization", "x-api-key", "idempotency-key"],
    allow_credentials=False,
)

class SourceIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    url: HttpUrl
    enabled: bool = True
    weight: float = Field(default=1, ge=.25, le=2)
    lifecycle: Literal["permanent", "one_time"] = "permanent"


class SourceScheduleIn(BaseModel):
    interval_hours: int = Field(ge=SOURCE_SYNC_MIN_HOURS, le=SOURCE_SYNC_MAX_HOURS)


class DigestSettingsIn(BaseModel):
    enabled: bool = False
    channels: list[Literal["discord", "email"]] = Field(default_factory=list)
    day: int = Field(default=1, ge=1, le=7)
    time: str = "09:00"
    timezone: str = "UTC"
    minimum_score: float = Field(default=80, ge=0, le=100)
    maximum_books: int = Field(default=5, ge=1, le=20)
    only_new: bool = True
    app_url: str = "http://localhost:5173"
    # Optional secrets preserve the existing value when omitted and clear it
    # when explicitly sent as an empty string.
    discord_webhook_url: str | None = None
    email_to: str = ""
    email_from: str = ""
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: Literal["none", "starttls", "ssl"] = "starttls"
    smtp_username: str | None = None
    smtp_password: str | None = None


class DigestTestIn(BaseModel):
    channel: Literal["discord", "email"] | None = None

class UrlIn(BaseModel): url: HttpUrl


class FeedbackIn(BaseModel):
    action: str
    run_id: str | None = Field(default=None, min_length=8, max_length=128)


class TelemetryEventIn(BaseModel):
    event_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
    candidate_id: int = Field(gt=0)
    event_type: str
    run_id: str | None = Field(default=None, min_length=8, max_length=128)
    value: float | None = None
    source: str = Field(default="ui", min_length=1, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def valid_event_type(cls, value: str) -> str:
        if value not in EVENT_TYPES - {"save", "reject", "restore", "read"}:
            raise ValueError("Unsupported recommendation event type")
        return value

    @field_validator("metadata")
    @classmethod
    def valid_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 32:
            raise ValueError("Event metadata must have at most 32 fields")
        return value


class TelemetryBatchIn(BaseModel):
    events: list[TelemetryEventIn] = Field(min_length=1, max_length=100)


class BulkFeedbackIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=100)
    action: Literal["save", "reject", "restore"]
    run_id: str | None = Field(default=None, min_length=8, max_length=128)


class ApiTokenCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class LLMConnectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    model_id: str = Field(min_length=1, max_length=300)
    endpoint: str = Field(default="", max_length=500)
    api_key: str | None = Field(default=None, max_length=20_000)
    enabled: bool = True


class LLMPolicyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    connection_id: int = Field(gt=0)
    top_k: int = Field(default=20, ge=1, le=100)
    prompt_version: str = Field(default="shadow-v1", min_length=1, max_length=100)
    enabled: bool = True

class EngineSettings(BaseModel):
    embedding_backend: str = "local"
    embedding_model: str = "hashing-768"
    embedding_url: str = ""
    embedding_api_key: str = ""
    librarr_url: str = ""
    librarr_api_key: str = ""
    librarr_media_type: str = "audiobook"

    def validate_values(self):
        if self.embedding_backend not in {"local","fastembed","ollama","openai-compatible"}: raise ValueError("Unsupported embedding backend")
        for label, value in (("embedding",self.embedding_url),("Librarr",self.librarr_url)):
            parsed = urlparse(value) if value else None
            if parsed and parsed.scheme not in {"http","https"}: raise ValueError(f"{label} URL must use HTTP or HTTPS")
            if parsed and (parsed.username or parsed.password or parsed.query or parsed.fragment): raise ValueError(f"{label} URL cannot contain credentials, query parameters, or fragments")
        if self.embedding_backend in {"ollama","openai-compatible"} and not self.embedding_url: raise ValueError("Remote embedding providers require an endpoint URL")
        normalize_media_type(self.librarr_media_type)
        if self.librarr_url:
            allowed = {host.strip().lower() for host in settings.librarr_allowed_hosts.split(",") if host.strip()}
            validate_service_url(self.librarr_url, allowed)

def api_token_list():
    """Return token metadata without ever returning a token value."""

    return rows(
        "SELECT id,name,token_prefix,created_at,last_used_at,revoked_at "
        "FROM api_tokens ORDER BY created_at DESC, id DESC"
    )


def tracked_recommendations(
    *,
    status: str | None = None,
    limit: int = 100,
    session_id: str = "",
):
    recommendations = recommendation_list(status=status, limit=limit)
    run_id = create_recommendation_run(
        recommendations,
        session_id=session_id,
        status=status,
        limit=limit,
    )
    return recommendations, run_id


def overview_payload(*, include_api_tokens: bool = True, session_id: str = ""):
    counts = {
        name: row(f"SELECT COUNT(*) count FROM {name}")["count"]
        for name in ("reads", "candidates", "sources")
    }
    recommendations, run_id = tracked_recommendations(session_id=session_id)
    return {
        "counts": counts,
        "recommendations": recommendations,
        "recommendation_run_id": run_id,
        "sources": rows("SELECT * FROM sources ORDER BY is_default DESC,name"),
        "history": rows(
            "SELECT * FROM reads ORDER BY COALESCE(read_at,created_at) DESC LIMIT 12"
        ),
        "settings": safe_settings(include_api_tokens=include_api_tokens),
    }


@app.get("/api/health")
async def health():
    config = safe_settings(include_api_tokens=False)
    return {
        "ok": True,
        "database": True,
        "embedding": {
            "backend": config["embedding_backend"],
            "model": config["embedding_model"],
            "configured": True,
        },
    }


@app.get("/api/overview")
def overview(
    x_bookward_session: str | None = Header(default=None, alias="X-Bookward-Session"),
):
    return overview_payload(session_id=x_bookward_session or "")

def recommendation_list(status: str | None = None, limit: int = 100):
    clauses = [
        "c.status!='rejected'",
        "(c.status IN ('saved','imported') OR s.enabled=1)",
        "(c.status IN ('saved','imported') OR book_identity(c.title,c.author) NOT IN (SELECT book_identity(title,author) FROM reads))",
    ]
    params: list[Any] = []
    if status and status != "all":
        clauses.append("c.status=?")
        params.append(status)
    result = rows(
        "SELECT c.*,s.name source_name FROM candidates c "
        "LEFT JOIN sources s ON s.id=c.source_id WHERE "
        + " AND ".join(clauses)
        + " ORDER BY CASE c.status WHEN 'recommended' THEN 0 WHEN 'saved' THEN 1 ELSE 2 END, c.score DESC LIMIT ?",
        (*params, limit),
    )
    for item in result:
        item["cover_url"] = fallback_cover_url(item["title"], item["author"], item.get("cover_url", ""), item.get("source_url", ""))
        item["source_url"] = canonical_book_source_url(item["title"], item["author"], item.get("source_url", ""))
        item["genres"] = json.loads(item["genres"] or "[]"); item["explanation"] = json.loads(item["explanation"] or "[]")
    return result

@app.get("/api/recommendations")
def recommendations(
    response: Response,
    x_bookward_session: str | None = Header(default=None, alias="X-Bookward-Session"),
):
    values, run_id = tracked_recommendations(session_id=x_bookward_session or "")
    response.headers["X-Bookward-Recommendation-Run"] = run_id
    return values

@app.post("/api/recommendations/{candidate_id}/feedback")
def feedback(candidate_id: int, payload: FeedbackIn):
    status = {"save":"saved","reject":"rejected","restore":"recommended"}.get(payload.action)
    if not status: raise HTTPException(400, "Unknown feedback action")
    with transaction() as con:
        if not con.execute("SELECT 1 FROM candidates WHERE id=?", (candidate_id,)).fetchone(): raise HTTPException(404, "Recommendation not found")
        con.execute("UPDATE candidates SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status,candidate_id))
        feedback_id = con.execute(
            "INSERT INTO feedback(candidate_id,action) VALUES(?,?)",
            (candidate_id, payload.action),
        ).lastrowid
        label = 1.0 if payload.action == "save" else 0.0 if payload.action == "reject" else None
        try:
            record_event_in_connection(
                con,
                event_key=f"feedback:{feedback_id}",
                candidate_id=candidate_id,
                event_type=payload.action,
                run_id=payload.run_id,
                source="ui",
                label=label,
                label_kind="explicit_feedback" if label is not None else None,
                confidence=0.8 if payload.run_id and label is not None else 0.5,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return {"id":candidate_id,"status":status}


@app.post("/api/recommendations/bulk-feedback")
def bulk_feedback(payload: BulkFeedbackIn):
    status = {"save": "saved", "reject": "rejected", "restore": "recommended"}[payload.action]
    ids = list(dict.fromkeys(payload.ids))
    with transaction() as con:
        placeholders = ",".join("?" for _ in ids)
        found = {
            int(item["id"])
            for item in con.execute(f"SELECT id FROM candidates WHERE id IN ({placeholders})", ids).fetchall()
        }
        for candidate_id in found:
            con.execute("UPDATE candidates SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, candidate_id))
            feedback_id = con.execute(
                "INSERT INTO feedback(candidate_id,action) VALUES(?,?)",
                (candidate_id, payload.action),
            ).lastrowid
            label = 1.0 if payload.action == "save" else 0.0 if payload.action == "reject" else None
            try:
                record_event_in_connection(
                    con,
                    event_key=f"feedback:{feedback_id}",
                    candidate_id=candidate_id,
                    event_type=payload.action,
                    run_id=payload.run_id,
                    source="ui",
                    label=label,
                    label_kind="explicit_feedback" if label is not None else None,
                    confidence=0.8 if payload.run_id and label is not None else 0.5,
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
    return {"updated": len(found), "skipped": len(ids) - len(found), "status": status, "ids": sorted(found)}


def telemetry_events(payload: TelemetryBatchIn):
    try:
        return record_events(
            {
                "event_key": event.event_key,
                "candidate_id": event.candidate_id,
                "event_type": event.event_type,
                "run_id": event.run_id,
                "value": event.value,
                "source": event.source,
                "metadata": event.metadata,
            }
            for event in payload.events
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/telemetry/events")
def post_telemetry_events(payload: TelemetryBatchIn):
    return telemetry_events(payload)

@app.post("/api/recommendations/{candidate_id}/import")
async def import_librarr(candidate_id: int):
    candidate = row("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    if not candidate: raise HTTPException(404, "Recommendation not found")
    key = hashlib.sha256(f"{candidate_id}:{candidate['normalized_key']}".encode()).hexdigest()
    existing = row("SELECT * FROM librarr_imports WHERE idempotency_key=? AND status='complete'", (key,))
    if existing: return {"id":candidate_id,"status":"imported","remote_id":existing["remote_id"],"duplicate":True}
    if candidate["status"] != "saved": raise HTTPException(409, "Shortlist this book before importing it")
    config = private_settings()
    if not config.get("librarr_url") or not config.get("librarr_api_key"): raise HTTPException(409, "Connect Librarr first")
    with transaction() as con:
        attempt = con.execute("SELECT status FROM librarr_imports WHERE idempotency_key=?",(key,)).fetchone()
        if attempt and attempt["status"] == "pending": raise HTTPException(409,"This book is already being imported")
        if attempt: con.execute("UPDATE librarr_imports SET status='pending',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE idempotency_key=?",(key,))
        else: con.execute("INSERT INTO librarr_imports(candidate_id,idempotency_key,status) VALUES(?,?,'pending')",(candidate_id,key))
    try:
        allowed = {host.strip().lower() for host in settings.librarr_allowed_hosts.split(",") if host.strip()}
        validate_service_url(config["librarr_url"], allowed)
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            media_type = normalize_media_type(config.get("librarr_media_type"))
            response = await client.post(f"{config['librarr_url'].rstrip('/')}/api/wishlist", headers={"X-Api-Key":config["librarr_api_key"],"Idempotency-Key":key}, json={"title":candidate["title"],"author":candidate["author"],"media_type":media_type})
            response.raise_for_status()
            try: data = response.json()
            except ValueError: data = {}
        with transaction() as con:
            con.execute("INSERT INTO librarr_imports(candidate_id,idempotency_key,status,remote_id) VALUES(?,?, 'complete',?) ON CONFLICT(idempotency_key) DO UPDATE SET status='complete',remote_id=excluded.remote_id,updated_at=CURRENT_TIMESTAMP", (candidate_id,key,str(data.get("id","imported"))))
            con.execute("UPDATE candidates SET status='imported' WHERE id=?",(candidate_id,))
        return {"id":candidate_id,"status":"imported","remote_id":str(data.get("id","imported"))}
    except (httpx.HTTPError, ValueError) as exc:
        with transaction() as con: con.execute("UPDATE librarr_imports SET status='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE idempotency_key=?",(str(exc)[:1000],key))
        raise HTTPException(502, f"Librarr request failed: {exc}")


@app.get("/api/librarr/search")
async def search_librarr(q: str = Query(min_length=2, max_length=200), media_type: str = Query(default="audiobook")):
    try:
        media = normalize_media_type(media_type)
        config = {**private_settings(), "librarr_allowed_hosts": settings.librarr_allowed_hosts}
        return await librarr_search(config, q, media)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Librarr search failed: {exc}") from exc


class LibrarrDownloadIn(BaseModel):
    media_type: str = "audiobook"
    result: dict[str, Any]


@app.post("/api/librarr/download")
async def download_librarr(payload: LibrarrDownloadIn):
    try:
        media = normalize_media_type(payload.media_type)
        encoded = json.dumps(payload.result, sort_keys=True, separators=(",", ":"))
        key = hashlib.sha256(f"{media}:{encoded}".encode()).hexdigest()
        config = {**private_settings(), "librarr_allowed_hosts": settings.librarr_allowed_hosts}
        result = await librarr_download(config, payload.result, media, key)
        return {"ok": True, "media_type": media, "result": result}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Librarr download failed: {exc}") from exc

@app.post("/api/import/goodreads/csv")
async def goodreads_csv(file: UploadFile = File(...)):
    content = bytearray()
    while chunk := await file.read(1024 * 1024):
        content.extend(chunk)
        if len(content) > 10_000_000: raise HTTPException(413, "CSV is larger than 10 MB")
    try: count = import_goodreads_csv(bytes(content))
    except ValueError as exc: raise HTTPException(400,str(exc))
    return {"imported":count,"job_id":enqueue_job("score")}

@app.post("/api/import/goodreads/rss")
async def goodreads_rss(payload: UrlIn):
    try: count = await import_goodreads_rss(str(payload.url))
    except (ValueError,httpx.HTTPError) as exc: raise HTTPException(400,str(exc))
    return {"imported":count,"job_id":enqueue_job("score")}

@app.post("/api/sources/preview")
async def source_preview(payload: UrlIn):
    try: return await preview_source(str(payload.url))
    except (ValueError,httpx.HTTPError) as exc: raise HTTPException(400,str(exc))

@app.post("/api/sources")
def add_source(payload: SourceIn):
    try: validate_public_url(str(payload.url))
    except ValueError as exc: raise HTTPException(400,str(exc))
    with transaction() as con:
        try: cursor=con.execute("INSERT INTO sources(name,url,enabled,weight,lifecycle) VALUES(?,?,?,?,?)",(payload.name,str(payload.url),payload.enabled,payload.weight,payload.lifecycle))
        except Exception as exc: raise HTTPException(409,"Source already exists") from exc
    job_id = enqueue_job(f"source:{cursor.lastrowid}") if payload.enabled else None
    return {"id":cursor.lastrowid, "job_id":job_id}

@app.put("/api/sources/{source_id}/toggle")
def toggle_source(source_id:int):
    with transaction() as con:
        source = con.execute("SELECT enabled,kind,lifecycle FROM sources WHERE id=?", (source_id,)).fetchone()
        if not source: raise HTTPException(404,"Source not found")
        became_enabled = not source["enabled"]
        con.execute(
            "UPDATE sources SET enabled=1-enabled, "
            "last_scanned_at=CASE WHEN enabled=0 AND lifecycle='one_time' THEN NULL ELSE last_scanned_at END "
            "WHERE id=?",
            (source_id,),
        )
    job_id = enqueue_job(f"source:{source_id}") if became_enabled and source["kind"] != "builtin" else None
    return {"id":source_id, "job_id":job_id}


@app.put("/api/sources/schedule")
def update_source_schedule(payload: SourceScheduleIn):
    with transaction() as con:
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,0) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=0,updated_at=CURRENT_TIMESTAMP",
            ("source_sync_interval_hours", str(payload.interval_hours)),
        )
    return {"saved": True, "interval_hours": payload.interval_hours}


def _save_digest_settings(updates: dict):
    current = private_settings()
    existing = digest_config(current)
    merged = {**existing, **updates}
    try:
        validate_digest_config(merged)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    storage = {
        "digest_enabled": "1" if merged["enabled"] else "0",
        "digest_channels": ",".join(merged["channels"]),
        "digest_day": str(merged["day"]),
        "digest_time": merged["time"],
        "digest_timezone": merged["timezone"],
        "digest_minimum_score": str(merged["minimum_score"]),
        "digest_maximum_books": str(merged["maximum_books"]),
        "digest_only_new": "1" if merged["only_new"] else "0",
        "digest_app_url": merged["app_url"],
        "digest_discord_webhook_url": merged["discord_webhook_url"],
        "digest_email_to": merged["email_to"],
        "digest_email_from": merged["email_from"],
        "digest_smtp_host": merged["smtp_host"],
        "digest_smtp_port": str(merged["smtp_port"]),
        "digest_smtp_security": merged["smtp_security"],
        "digest_smtp_username": merged["smtp_username"],
        "digest_smtp_password": merged["smtp_password"],
    }
    secret_keys = {"digest_discord_webhook_url", "digest_smtp_username", "digest_smtp_password"}
    with transaction() as con:
        for key, value in storage.items():
            is_secret = key in secret_keys
            stored = seal(value) if is_secret and value else value
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=excluded.secret,updated_at=CURRENT_TIMESTAMP",
                (key, stored, 1 if is_secret else 0),
            )
    return safe_digest_settings(private_settings())


@app.get("/api/digest/settings")
def get_digest_settings():
    return safe_digest_settings(private_settings())


@app.put("/api/digest/settings")
def update_digest_settings(payload: DigestSettingsIn):
    # None means "leave the existing credential alone"; an explicit empty
    # string is a deliberate clear action.
    return {"saved": True, "digest": _save_digest_settings(payload.model_dump(exclude_none=True))}


@app.get("/api/digest/preview")
def preview_digest():
    return digest_preview(private_settings())


@app.get("/api/digest/review")
def digest_review(period: str = Query(min_length=8, max_length=8)):
    """Return the candidate IDs that were included in a digest period.

    The public review link carries only an ISO week key. Candidate metadata
    remains behind the normal overview/auth boundary; this endpoint exposes
    only IDs so the web layer can render the same cards without copying book
    data into a notification URL.
    """

    if len(period) != 8 or period[4] != "-" or period[5] != "W" or not period[0:4].isdigit() or not period[6:8].isdigit():
        raise HTTPException(400, "Invalid digest period")
    ids: list[int] = []
    deliveries = rows(
        "SELECT candidate_ids FROM notification_deliveries WHERE period_key=? ORDER BY created_at DESC",
        (period,),
    )
    for delivery in deliveries:
        try:
            values = json.loads(delivery["candidate_ids"] or "[]")
        except (TypeError, json.JSONDecodeError):
            values = []
        for value in values:
            try:
                candidate_id = int(value)
            except (TypeError, ValueError):
                continue
            if candidate_id not in ids:
                ids.append(candidate_id)
    # ``found`` lets the web app distinguish a valid digest with zero books
    # from a stale link whose delivery history has been pruned or never
    # existed. Stale links should return readers to the normal Discover feed.
    return {"period": period, "ids": ids, "found": bool(deliveries)}


@app.get("/api/digest/deliveries")
def digest_deliveries(limit: int = Query(default=20, ge=1, le=100)):
    return {"deliveries": rows(
        "SELECT id,period_key,channel,status,recipient,candidate_ids,attempts,error,created_at,updated_at,sent_at "
        "FROM notification_deliveries ORDER BY created_at DESC LIMIT ?", (limit,)
    )}


@app.post("/api/digest/test")
def test_digest(payload: DigestTestIn = DigestTestIn()):
    config = digest_config(private_settings())
    channel = payload.channel
    if channel is None:
        if len(config["channels"]) != 1:
            raise HTTPException(400, "Choose a configured digest channel to test")
        channel = config["channels"][0]
    if channel not in config["channels"]:
        raise HTTPException(400, "That digest channel is not enabled")
    return {"job_id": enqueue_job(f"digest:test:{channel}")}


@app.post("/api/digest/run")
def run_digest():
    return {"job_id": enqueue_job("digest:manual")}


@app.post("/api/digest/deliveries/{delivery_id}/retry")
def retry_digest_delivery(delivery_id: str):
    if not row("SELECT id FROM notification_deliveries WHERE id=?", (delivery_id,)):
        raise HTTPException(404, "Delivery not found")
    return {"job_id": enqueue_job(f"notification_retry:{delivery_id}")}

@app.post("/api/sync")
async def sync():
    return {"job_id":enqueue_job("sync")}

@app.post("/api/score")
async def score():
    return {"job_id":enqueue_job("score")}

@app.post("/api/embeddings/rebuild")
async def rebuild_embeddings():
    return {"job_id":enqueue_job("rebuild_embeddings")}

@app.get("/api/jobs/{job_id}")
def job(job_id:str):
    found=row("SELECT * FROM jobs WHERE id=?",(job_id,))
    if not found: raise HTTPException(404,"Job not found")
    return found

def private_settings():
    return {
        item["key"]: unseal(item["value"]) if item["secret"] else item["value"]
        for item in rows("SELECT key,value,secret FROM settings")
    }


def safe_settings(*, include_api_tokens: bool = True):
    private = private_settings()
    try:
        media_type = normalize_media_type(private.get("librarr_media_type"))
    except ValueError:
        media_type = "audiobook"
    digest = safe_digest_settings(private)
    result = {
        "embedding_backend": private.get("embedding_backend", settings.embedding_backend),
        "embedding_model": private.get("embedding_model", settings.embedding_model),
        "embedding_url": private.get("embedding_url", settings.embedding_url),
        "embedding_api_key_set": bool(private.get("embedding_api_key")),
        "librarr_url": private.get("librarr_url", ""),
        "librarr_api_key_set": bool(private.get("librarr_api_key")),
        "librarr_media_type": media_type,
        "source_sync_interval_hours": source_sync_interval_hours(),
        "digest": digest,
        "llm_connections": safe_connections(),
        "llm_policies": safe_policies(),
    }
    if include_api_tokens:
        result["api_tokens"] = api_token_list()
    return result


def _create_api_token(name: str):
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(400, "Token name cannot be blank")
    token = generate_api_token()
    with transaction() as con:
        cursor = con.execute(
            "INSERT INTO api_tokens(name,token_prefix,token_hash) VALUES(?,?,?)",
            (clean_name, token_prefix(token), hash_api_token(token)),
        )
        token_id = cursor.lastrowid
        created = con.execute(
            "SELECT id,name,token_prefix,created_at,last_used_at,revoked_at "
            "FROM api_tokens WHERE id=?",
            (token_id,),
        ).fetchone()
    return {
        "token": token,
        "id": created["id"],
        "name": created["name"],
        "token_prefix": created["token_prefix"],
        "created_at": created["created_at"],
        "last_used_at": created["last_used_at"],
        "revoked_at": created["revoked_at"],
    }


def _revoke_api_token(token_id: int):
    with transaction() as con:
        found = con.execute("SELECT id FROM api_tokens WHERE id=?", (token_id,)).fetchone()
        if not found:
            raise HTTPException(404, "API token not found")
        con.execute(
            "UPDATE api_tokens SET revoked_at=COALESCE(revoked_at,CURRENT_TIMESTAMP) WHERE id=?",
            (token_id,),
        )
    return {"revoked": True, "id": token_id}


@app.get("/api/settings/api-tokens")
@app.get("/api/tokens")
def list_api_tokens():
    return {"tokens": api_token_list()}


@app.get("/api/llm/catalog")
async def llm_catalog(refresh: bool = Query(default=False)):
    try:
        return await get_catalog(force=refresh)
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc


def _llm_default_endpoint(provider_id: str) -> str:
    return {
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com/v1",
    }.get(provider_id.strip().lower(), "")


def _llm_connection_values(payload: LLMConnectionIn, current_key: str = "") -> tuple[str, str]:
    provider_id = payload.provider_id.strip()
    endpoint = validate_endpoint(payload.endpoint, default=_llm_default_endpoint(provider_id))
    key = payload.api_key.strip() if payload.api_key is not None else current_key
    if not key:
        raise HTTPException(400, "An API key is required for this connection")
    return endpoint, key


@app.get("/api/llm/connections")
def llm_connections():
    return {"connections": safe_connections()}


@app.post("/api/llm/connections")
def create_llm_connection(payload: LLMConnectionIn):
    try:
        endpoint, key = _llm_connection_values(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with transaction() as con:
        cursor = con.execute(
            "INSERT INTO llm_connections(name,provider_id,model_id,endpoint,auth_type,secret,enabled) VALUES(?,?,?,?,?,?,?)",
            (payload.name.strip(), payload.provider_id.strip(), payload.model_id.strip(), endpoint, "api_key", seal(key), int(payload.enabled)),
        )
    return {"id": cursor.lastrowid, "connection": next(item for item in safe_connections() if item["id"] == cursor.lastrowid)}


@app.put("/api/llm/connections/{connection_id}")
def update_llm_connection(connection_id: int, payload: LLMConnectionIn):
    current = row("SELECT * FROM llm_connections WHERE id=?", (connection_id,))
    if not current:
        raise HTTPException(404, "LLM connection not found")
    try:
        endpoint, key = _llm_connection_values(payload, unseal(current["secret"]) if current["secret"] else "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with transaction() as con:
        con.execute(
            "UPDATE llm_connections SET name=?,provider_id=?,model_id=?,endpoint=?,auth_type='api_key',secret=?,enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (payload.name.strip(), payload.provider_id.strip(), payload.model_id.strip(), endpoint, seal(key), int(payload.enabled), connection_id),
        )
    return {"id": connection_id, "connection": next(item for item in safe_connections() if item["id"] == connection_id)}


@app.delete("/api/llm/connections/{connection_id}")
def disable_llm_connection(connection_id: int):
    with transaction() as con:
        changed = con.execute(
            "UPDATE llm_connections SET enabled=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (connection_id,),
        ).rowcount
    if not changed:
        raise HTTPException(404, "LLM connection not found")
    return {"id": connection_id, "enabled": False}


@app.get("/api/llm/policies")
def llm_policies():
    return {"policies": safe_policies()}


@app.post("/api/llm/policies")
def create_llm_policy(payload: LLMPolicyIn):
    if not row("SELECT id FROM llm_connections WHERE id=?", (payload.connection_id,)):
        raise HTTPException(404, "LLM connection not found")
    with transaction() as con:
        cursor = con.execute(
            "INSERT INTO llm_policies(name,connection_id,enabled,top_k,prompt_version) VALUES(?,?,?,?,?)",
            (payload.name.strip(), payload.connection_id, int(payload.enabled), payload.top_k, payload.prompt_version.strip()),
        )
    return {"id": cursor.lastrowid, "policy": next(item for item in safe_policies() if item["id"] == cursor.lastrowid)}


@app.put("/api/llm/policies/{policy_id}")
def update_llm_policy(policy_id: int, payload: LLMPolicyIn):
    if not row("SELECT id FROM llm_policies WHERE id=?", (policy_id,)):
        raise HTTPException(404, "LLM policy not found")
    if not row("SELECT id FROM llm_connections WHERE id=?", (payload.connection_id,)):
        raise HTTPException(404, "LLM connection not found")
    with transaction() as con:
        con.execute(
            "UPDATE llm_policies SET name=?,connection_id=?,enabled=?,top_k=?,prompt_version=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (payload.name.strip(), payload.connection_id, int(payload.enabled), payload.top_k, payload.prompt_version.strip(), policy_id),
        )
    return {"id": policy_id, "policy": next(item for item in safe_policies() if item["id"] == policy_id)}


@app.delete("/api/llm/policies/{policy_id}")
def disable_llm_policy(policy_id: int):
    with transaction() as con:
        changed = con.execute(
            "UPDATE llm_policies SET enabled=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (policy_id,),
        ).rowcount
    if not changed:
        raise HTTPException(404, "LLM policy not found")
    return {"id": policy_id, "enabled": False}


@app.post("/api/llm/policies/{policy_id}/run")
def run_llm_policy(policy_id: int):
    if not row("SELECT id FROM llm_policies WHERE id=?", (policy_id,)):
        raise HTTPException(404, "LLM policy not found")
    return {"job_id": enqueue_job(f"llm_shadow:{policy_id}", dedupe=True)}


@app.get("/api/llm/runs")
def llm_runs(limit: int = Query(default=20, ge=1, le=100)):
    return {"runs": rows("SELECT id,policy_id,connection_id,request_hash,candidate_hash,status,candidate_count,latency_ms,input_tokens,output_tokens,error,created_at,finished_at FROM llm_runs ORDER BY created_at DESC LIMIT ?", (limit,))}


@app.get("/api/llm/runs/{run_id}")
def llm_run(run_id: str):
    found = row("SELECT id,policy_id,connection_id,request_hash,candidate_hash,status,candidate_count,latency_ms,input_tokens,output_tokens,error,created_at,finished_at FROM llm_runs WHERE id=?", (run_id,))
    if not found:
        raise HTTPException(404, "LLM run not found")
    found["scores"] = rows("SELECT candidate_id,rank,score,confidence,reason_codes FROM llm_scores WHERE run_id=? ORDER BY rank", (run_id,))
    for score in found["scores"]:
        try:
            score["reason_codes"] = json.loads(score["reason_codes"] or "[]")
        except ValueError:
            score["reason_codes"] = []
    return found


@app.post("/api/settings/api-tokens")
@app.post("/api/tokens")
def create_api_token(payload: ApiTokenCreateIn):
    return _create_api_token(payload.name)


@app.delete("/api/settings/api-tokens/{token_id}")
@app.delete("/api/tokens/{token_id}")
def delete_api_token(token_id: int):
    return _revoke_api_token(token_id)


@app.post("/api/settings/api-tokens/{token_id}/revoke")
@app.post("/api/tokens/{token_id}/revoke")
def revoke_api_token(token_id: int):
    return _revoke_api_token(token_id)

@app.put("/api/settings")
async def update_settings(payload: EngineSettings):
    try: payload.validate_values()
    except ValueError as exc: raise HTTPException(400,str(exc))
    current = private_settings(); values = payload.model_dump()
    for key in ("embedding_api_key","librarr_api_key"):
        if not values[key]: values[key] = current.get(key,"")
    test = await asyncio.to_thread(get_embedder, values["embedding_backend"],values["embedding_model"],values["embedding_url"],values["embedding_api_key"])
    health = await test.health()
    if not health.get("ok"): raise HTTPException(400, f"Embedding provider check failed: {health.get('error','unknown error')}")
    values["embedding_model"] = test.model
    with transaction() as con:
        for key,value in values.items():
            secret = key.endswith("api_key")
            stored = seal(value) if secret and value else value
            con.execute("INSERT INTO settings(key,value,secret) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=excluded.secret,updated_at=CURRENT_TIMESTAMP",(key,stored,1 if secret else 0))
    return {"saved":True,"embedding":health}


bearer_scheme = HTTPBearer(auto_error=False)


def require_api_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Authenticate the public API without accepting browser credentials.

    ``HTTPBearer`` is used as a dependency rather than parsing the header by
    hand so FastAPI advertises the authentication requirement in OpenAPI.
    ``X-API-Key`` remains supported for clients that use API-key conventions.
    """

    candidate = credentials.credentials.strip() if credentials else ""
    if not candidate and x_api_key:
        candidate = x_api_key.strip()
    if not candidate or any(character.isspace() for character in candidate):
        raise HTTPException(
            status_code=401,
            detail="A valid API token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    current_hash = hash_api_token(candidate)
    legacy_hash = legacy_hash_api_token(candidate)
    found = row(
        "SELECT id,name,token_hash FROM api_tokens "
        "WHERE token_hash IN (?,?) AND revoked_at IS NULL",
        (current_hash, legacy_hash),
    )
    if not found:
        raise HTTPException(
            status_code=401,
            detail="A valid API token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    with transaction() as con:
        if found["token_hash"] == legacy_hash:
            # Tokens created before the PBKDF2 upgrade remain valid and are
            # upgraded on their first successful use. The predicate makes
            # concurrent requests harmless if another request wins the race.
            con.execute(
                "UPDATE api_tokens SET token_hash=? WHERE id=? AND token_hash=?",
                (current_hash, found["id"], legacy_hash),
            )
        con.execute(
            "UPDATE api_tokens SET last_used_at=CURRENT_TIMESTAMP WHERE id=?",
            (found["id"],),
        )
    return found


api_v1 = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_api_token)],
    tags=["public-api"],
)


@api_v1.get("")
def api_root():
    return {
        "name": "Bookward API",
        "version": "1",
        "authentication": "Bearer API token",
        "documentation": "GET /openapi.json or /docs on the engine",
        "endpoints": {
            "overview": "GET /api/v1/overview",
            "recommendations": "GET /api/v1/recommendations",
            "feedback": "POST /api/v1/recommendations/{id}/feedback",
            "sources": "GET /api/v1/sources",
            "sync": "POST /api/v1/sync",
        },
    }


@api_v1.get("/health")
async def api_health():
    return await health()


@api_v1.get("/overview")
def api_overview():
    return overview_payload(include_api_tokens=False)


@api_v1.get("/settings")
def api_settings():
    return safe_settings(include_api_tokens=False)


@api_v1.get("/recommendations")
def api_recommendations(
    response: Response,
    status: Literal["recommended", "saved", "imported", "all"] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100),
):
    values, run_id = tracked_recommendations(status=status, limit=limit)
    response.headers["X-Bookward-Recommendation-Run"] = run_id
    return values


@api_v1.post("/recommendations/bulk-feedback")
def api_bulk_feedback(payload: BulkFeedbackIn):
    return bulk_feedback(payload)


@api_v1.post("/recommendations/{candidate_id}/feedback")
def api_feedback(candidate_id: int, payload: FeedbackIn):
    return feedback(candidate_id, payload)


@api_v1.post("/telemetry/events")
def api_telemetry_events(payload: TelemetryBatchIn):
    return telemetry_events(payload)


@api_v1.post("/recommendations/{candidate_id}/import")
async def api_import_librarr(candidate_id: int):
    return await import_librarr(candidate_id)


@api_v1.get("/sources")
def api_sources():
    return rows("SELECT * FROM sources ORDER BY is_default DESC,name")


@api_v1.post("/sources/preview")
async def api_source_preview(payload: UrlIn):
    return await source_preview(payload)


@api_v1.post("/sources")
def api_add_source(payload: SourceIn):
    return add_source(payload)


@api_v1.put("/sources/{source_id}/toggle")
def api_toggle_source(source_id: int):
    return toggle_source(source_id)


@api_v1.put("/sources/schedule")
def api_update_source_schedule(payload: SourceScheduleIn):
    return update_source_schedule(payload)


@api_v1.post("/import/goodreads/rss")
async def api_goodreads_rss(payload: UrlIn):
    return await goodreads_rss(payload)


@api_v1.post("/sync")
async def api_sync():
    return await sync()


@api_v1.post("/score")
async def api_score():
    return await score()


@api_v1.get("/jobs/{job_id}")
def api_job(job_id: str):
    return job(job_id)


@api_v1.get("/librarr/search")
async def api_librarr_search(
    q: str = Query(min_length=2, max_length=200),
    media_type: str = Query(default="audiobook"),
):
    return await search_librarr(q=q, media_type=media_type)


@api_v1.post("/librarr/download")
async def api_librarr_download(payload: LibrarrDownloadIn):
    return await download_librarr(payload)


app.include_router(api_v1)
