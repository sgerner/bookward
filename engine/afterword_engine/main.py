import hashlib
import json
import asyncio
import logging
import base64
import hmac
import re
import secrets
import sqlite3
import time
import uuid
import weakref
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from contextlib import asynccontextmanager
import httpx
from fastapi import APIRouter, Body, Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, HttpUrl, field_validator
from .config import settings
from .database import (
    DEFAULT_DIGEST_APP_URLS,
    RestoreEpochError,
    bind_request_restore_epoch,
    connect,
    database_lock,
    fenced_async,
    initialize,
    normalize_key,
    row,
    rows,
    scheduler_fenced,
    transaction,
)
from .backups import (
    BackupError,
    RESTORE_FENCE_DETAIL,
    backup_scheduler_loop,
    backup_status,
    clear_interrupted_restore_journal,
    compact_database,
    create_backup,
    has_restore_journal,
    identity_backup_status,
    recover_interrupted_restore,
    restore_backup,
)
from .decision_evidence import set_evidence_rotation_enabled
from .maintenance import set_storage_retention_enabled
from .embeddings import get_embedder
from .covers import canonical_book_source_url, fallback_cover_url, validate_google_books_api_key
from . import ingestion
from .ingestion import (
    import_csv,
    import_goodreads_csv,
    import_goodreads_rss,
    validate_goodreads_rss_url,
    import_goodreads_shelves,
    normalize_source_filters,
    preview_source,
    candidate_metadata_refresh_remaining,
    read_metadata_refresh_remaining,
    read_identity_rows_with_current_verified_work_ids,
    refresh_read_metadata,
    refresh_read_work_identities,
    refresh_missing_candidate_metadata,
    scan_source,
)
from .security import safe_error_message, validate_public_url, validate_service_url
from .jobs import enqueue_job, worker_loop
from .scoring import cached_vectors, rebuild_all_embeddings, score_all, _vector_dimensions
from .ranking import rank_candidates
from .secrets import auth_registry_key, installation_key_path, seal, unseal
from .librarr import (
    StreamingUnsupportedError,
    download as librarr_download,
    normalize_media_type,
    open_search_stream as librarr_open_search_stream,
    search as librarr_search,
)
from .exploration import epsilon_tail_explore
from .impression_fatigue import (
    apply_impression_fatigue,
    load_unacted_visible_impression_counts,
)
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
    build_decision_evidence,
    record_event_in_connection,
    record_events,
    validate_candidate_run_id,
    _impression_precedes,
    _read_label,
    _read_time,
)
from .interaction_personalization import (
    load_interaction_events,
    load_cached_candidate_vectors,
    personalize_recommendations,
)
from .discovery_slate import (
    diversify_discovery_slate,
    empty_diagnostics as empty_discovery_slate_diagnostics,
)
from .associations import run_association_provider
from .association_sources.openlibrary import OpenLibraryListProvider
from .association_sources.librarything import LibraryThingProvider
from .association_sources.google_books import GoogleBooksAssociatedProvider
from .quality import (
    QUALITY_AUDIT_BATCH_SIZE,
    audit_candidates,
    has_candidate_quality_audit_candidates,
    quality_summary,
)
from . import llm_reranker
from .llm_reranker import (
    clear_llm_rerank_cache,
    is_custom_llm_endpoint,
    rerank_with_llm_sync as _default_rerank_with_llm_sync,
    resolve_llm_rerank_credentials,
)

rerank_with_llm_sync = _default_rerank_with_llm_sync
from .identity import (
    ReadIdentityIndex,
    book_identity,
    book_identity_match_index,
    book_openlibrary_work_id,
    book_row_identity_match_keys,
)
from .isbn import isbn_parts
from . import profiles as identity_store
from .tenancy import current_profile_id, profile_scope
import jwt

SOURCE_SYNC_MIN_HOURS = 0
SOURCE_SYNC_MAX_HOURS = 720
SOURCE_SYNC_ERROR_RETRY_SECONDS = 6 * 60 * 60
GOODREADS_RSS_MAX_ATTEMPTS = 3
SCHEDULER_INITIAL_DELAY_SECONDS = 5
SCHEDULER_POLL_SECONDS = 60
ENRICHMENT_RETRY_POLL_SECONDS = 300
GOODREADS_RSS_SYNC_HOUR_UTC = 2
LOGGER = logging.getLogger(__name__)


class LocalLoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=1, max_length=1024)


class InitialAdminIn(LocalLoginIn):
    pass


class PasswordChangeIn(BaseModel):
    current_password: str = Field(default="", max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


class AccountCreateIn(BaseModel):
    username: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=12, max_length=1024)
    display_name: str = Field(default="", max_length=160)
    role: Literal["user", "admin"] = "user"


class AccountPasswordIn(BaseModel):
    password: str = Field(min_length=12, max_length=1024)


def _ensure_profile_database(profile_id: str):
    with profile_scope(profile_id):
        recover_interrupted_restore()
        initialize(seed_demo=profile_id == "legacy")


def _principal(request):
    account = getattr(request.state, "account", None)
    if account is None:
        raise HTTPException(401, "Authentication required")
    return account


def _require_admin(request):
    account = _principal(request)
    if account["role"] != "admin":
        raise HTTPException(403, "Administrator access required")
    return account


def _require_recent_authentication(account):
    try:
        authenticated_at = datetime.fromisoformat(account.get("session_created_at", ""))
    except (TypeError, ValueError):
        raise HTTPException(401, "Sign in again to confirm this change")
    if datetime.now(timezone.utc) - authenticated_at > timedelta(minutes=10):
        raise HTTPException(401, "Sign in again before adding a login method")


def _mark_private_no_store(response: Response) -> None:
    """Prevent shared/private caching while preserving stream directives."""
    content_type = response.headers.get("content-type", "").lower()
    if content_type.startswith("text/event-stream"):
        directives = [item.strip() for item in response.headers.get("cache-control", "").split(",") if item.strip()]
        directives = [item for item in directives if item.casefold() != "public"]
        folded = {item.casefold() for item in directives}
        for required in ("private", "no-store"):
            if required not in folded:
                directives.append(required)
        response.headers["cache-control"] = ", ".join(directives)
    else:
        response.headers["cache-control"] = "private, no-store"


def source_sync_interval_hours(private=None):
    """Return the persisted permanent-source cadence in hours.

    ``0`` means manual-only. Keeping the value in SQLite makes the schedule
    portable across Docker restarts while the environment setting remains a
    useful first-install default.
    """

    if private is None:
        private = private_settings()
    raw = private.get(
        "source_sync_interval_hours", str(settings.source_sync_interval_hours)
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = settings.source_sync_interval_hours
    return max(SOURCE_SYNC_MIN_HOURS, min(SOURCE_SYNC_MAX_HOURS, value))


def source_sync_due_sources(now=None):
    """Return only enabled permanent sources due for a scheduled scan."""

    interval = source_sync_interval_hours()
    if interval <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    sources = rows(
        "SELECT * FROM sources WHERE enabled=1 "
        "AND kind NOT IN ('builtin','association') AND lifecycle='permanent'"
    )
    due = []
    for source in sources:
        attempt_at = source["last_attempted_at"] or source["last_scanned_at"]
        if not attempt_at:
            due.append(source)
            continue
        try:
            last = datetime.fromisoformat(
                str(attempt_at).replace(" ", "T").replace("Z", "+00:00")
            )
        except ValueError:
            due.append(source)
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if str(source["last_status"] or "").startswith("error:"):
            if now - last >= timedelta(seconds=SOURCE_SYNC_ERROR_RETRY_SECONDS):
                due.append(source)
            continue
        if now - last >= timedelta(hours=interval):
            due.append(source)
    return due


def source_sync_is_due():
    """Whether at least one enabled permanent source needs a scheduled scan."""

    return bool(source_sync_due_sources())


def active_job(kind: str):
    return row(
        "SELECT 1 FROM jobs WHERE kind=? AND status IN ('queued','running') LIMIT 1",
        (kind,),
    )


def enqueue_followup_job(kind: str):
    """Keep at most one queued continuation while the current job is running."""

    with transaction() as con:
        existing = con.execute(
            "SELECT id FROM jobs WHERE kind=? AND status='queued' LIMIT 1",
            (kind,),
        ).fetchone()
        if existing:
            return existing["id"]
        job_id = str(uuid.uuid4())
        con.execute(
            "INSERT INTO jobs(id,kind,status) VALUES(?,?,'queued')",
            (job_id, kind),
        )
        return job_id


def _enqueue_candidate_quality_followup():
    if has_candidate_quality_audit_candidates():
        return enqueue_followup_job("candidate_quality_recovery")
    return None


@scheduler_fenced(default=dict)
def _queue_due_enrichment_retries():
    """Schedule bounded retry jobs without performing provider requests here."""

    queued = {}
    if not active_job("candidate_quality_recovery") and has_candidate_quality_audit_candidates():
        queued["candidate_quality_recovery"] = enqueue_job("candidate_quality_recovery", dedupe=True)
    if not active_job("metadata") and candidate_metadata_refresh_remaining():
        queued["metadata"] = enqueue_job("metadata", dedupe=True)
    if not active_job("read_metadata") and read_metadata_refresh_remaining():
        queued["read_metadata"] = enqueue_job("read_metadata", dedupe=True)
    return queued


@scheduler_fenced()
def queue_digest_if_due():
    """Schedule a digest immediately after scoring, when its window is open."""

    try:
        if digest_is_due(private_settings()) and not active_job("digest"):
            return enqueue_job("digest", dedupe=True)
    except RestoreEpochError:
        raise
    except Exception:
        return None
    return None


async def source_scheduler_loop(stop):
    """Queue a single sync when permanent sources become due.

    The loop lives in the engine so Docker installs do not need cron or a
    host-specific scheduler. A minute-level poll is deliberately simple and
    bounded; the actual cadence is persisted in ``settings``.
    """

    next_enrichment_retry_check = 0.0
    try:
        await asyncio.wait_for(stop.wait(), timeout=SCHEDULER_INITIAL_DELAY_SECONDS)
    except TimeoutError:
        pass
    while not stop.is_set():
        current_monotonic = asyncio.get_running_loop().time()
        check_enrichment_retries = current_monotonic >= next_enrichment_retry_check
        if check_enrichment_retries:
            next_enrichment_retry_check = current_monotonic + ENRICHMENT_RETRY_POLL_SECONDS
        for profile_id in identity_store.profile_ids():
            try:
                with profile_scope(profile_id), database_lock().scheduler_guard() as can_schedule, bind_request_restore_epoch():
                    if not can_schedule:
                        continue
                    if (
                        source_sync_is_due()
                        and not active_job("sync")
                        and not active_job("sync_due")
                    ):
                        enqueue_job("sync_due", dedupe=True)
                    if check_enrichment_retries:
                        _queue_due_enrichment_retries()
            except RestoreEpochError:
                continue
            except Exception:
                # A transient profile database error must not stop schedules
                # for other profiles or terminate the scheduler.
                LOGGER.exception("Source scheduler failed for profile %s", profile_id)
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
        for profile_id in identity_store.profile_ids():
            try:
                with profile_scope(profile_id), database_lock().scheduler_guard() as can_schedule, bind_request_restore_epoch():
                    if not can_schedule:
                        continue
                    if digest_is_due(private_settings()) and not active_job("digest"):
                        enqueue_job("digest", dedupe=True)
            except RestoreEpochError:
                continue
            except Exception:
                LOGGER.exception("Digest scheduler failed for profile %s", profile_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=SCHEDULER_POLL_SECONDS)
        except TimeoutError:
            pass


@scheduler_fenced()
def queue_goodreads_rss_if_due(current_time=None):
    """Queue the nightly Goodreads feed sync and retry failures up to three times."""

    now = current_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    if now.hour < GOODREADS_RSS_SYNC_HOUR_UTC:
        return None
    if not private_settings().get("goodreads_rss_url", "").strip():
        return None

    sync_date = now.date().isoformat()
    with transaction() as con:
        scheduled = con.execute(
            "SELECT id,status,attempts FROM jobs WHERE kind='goodreads_rss' "
            "AND date(created_at)=? ORDER BY created_at DESC LIMIT 1",
            (sync_date,),
        ).fetchone()
        if scheduled:
            if scheduled["status"] in {"queued", "running", "complete"}:
                return scheduled["id"] if scheduled["status"] != "complete" else None
            if scheduled["status"] == "failed":
                if int(scheduled["attempts"] or 0) >= GOODREADS_RSS_MAX_ATTEMPTS:
                    return None
                updated = con.execute(
                    "UPDATE jobs SET status='queued',error=NULL,finished_at=NULL "
                    "WHERE id=? AND status='failed' AND attempts<?",
                    (scheduled["id"], GOODREADS_RSS_MAX_ATTEMPTS),
                ).rowcount
                return scheduled["id"] if updated else None
        active = con.execute(
            "SELECT id FROM jobs WHERE kind='goodreads_rss' "
            "AND status IN ('queued','running') LIMIT 1"
        ).fetchone()
        if active:
            return active["id"]
        job_id = str(uuid.uuid4())
        con.execute(
            "INSERT INTO jobs(id,kind,status) VALUES(?,'goodreads_rss','queued')",
            (job_id,),
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES('goodreads_rss_last_scheduled_date',?,0) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=0,updated_at=CURRENT_TIMESTAMP",
            (sync_date,),
        )
        return job_id


async def goodreads_rss_scheduler_loop(stop):
    """Queue the configured Goodreads feed each night without host cron."""

    try:
        await asyncio.wait_for(stop.wait(), timeout=SCHEDULER_INITIAL_DELAY_SECONDS + 3)
    except TimeoutError:
        pass
    while not stop.is_set():
        for profile_id in identity_store.profile_ids():
            try:
                with profile_scope(profile_id):
                    queue_goodreads_rss_if_due()
            except RestoreEpochError:
                continue
            except Exception:
                LOGGER.exception("Goodreads RSS scheduler failed for profile %s", profile_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=SCHEDULER_POLL_SECONDS)
        except TimeoutError:
            pass

@fenced_async
async def handle_job(kind: str):
    config = private_settings()
    if kind == "read_work_identities":
        result = await refresh_read_work_identities()
        if result["remaining"]:
            enqueue_followup_job("read_work_identities")
        if result["matched"]:
            enqueue_job("read_metadata", dedupe=True)
        return result
    if kind == "read_metadata":
        from .publication_year_metadata import attach_publication_years

        def rated_publication_years():
            rated = rows("SELECT * FROM reads WHERE rating BETWEEN 1 AND 5 ORDER BY id")
            with connect() as con:
                attach_publication_years(con, rated, [])
            return {item["id"]: (item.get("first_publication_year"), item.get("publication_work_id")) for item in rated}

        before_years = rated_publication_years()
        result = await refresh_read_metadata()
        if result["remaining"]:
            enqueue_followup_job("read_metadata")
        score_job_id = (
            enqueue_job("score", dedupe=True)
            if rated_publication_years() != before_years else None
        )
        return {**result, "score_job_id": score_job_id}
    if kind == "digest":
        return await send_digest(config, automatic=True)
    if kind == "goodreads_rss":
        url = config.get("goodreads_rss_url", "").strip()
        if not url:
            return {"skipped": "not_configured"}
        result = await _sync_goodreads_rss(url)
        result["job_id"] = enqueue_job("score", dedupe=True)
        return result
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
    if kind.startswith("association:"):
        provider_name = kind.partition(":")[2]
        provider_settings = safe_association_settings()
        reads = rows("SELECT * FROM reads ORDER BY id")
        if provider_name == "openlibrary":
            if not provider_settings["openlibrary_enabled"]:
                return {"skipped": "disabled", "provider": provider_name}
            provider = OpenLibraryListProvider()
            result = await run_association_provider(provider, reads)
        elif provider_name == "librarything":
            if not provider_settings["librarything_enabled"]:
                return {"skipped": "disabled", "provider": provider_name}
            api_key = config.get("association_librarything_api_key", "")
            if not api_key:
                raise ValueError("LibraryThing API key is not configured")
            provider = LibraryThingProvider(api_key)
            result = await run_association_provider(provider, reads)
        elif provider_name == "google_books":
            # Google results remain process-memory-only. The job records a
            # provider run and counts edges but never stores Google content.
            provider = GoogleBooksAssociatedProvider(
                config.get("association_google_books_api_key", "")
            )
            result = await run_association_provider(provider, reads, persist=False)
        else:
            raise ValueError("Unknown association provider")
        quality = await audit_candidates(only_pending=True, limit=QUALITY_AUDIT_BATCH_SIZE)
        _enqueue_candidate_quality_followup()
        enqueue_job("read_work_identities", dedupe=True)
        return {
            "run_id": result.id,
            "provider": result.provider,
            "status": result.status,
            "seeds": result.seeds,
            "edges": result.edges,
            "persisted": result.persisted,
            "quality": quality,
        }
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
        metadata = await refresh_missing_candidate_metadata()
        remaining = candidate_metadata_refresh_remaining()
        score_job_id = enqueue_job("score", dedupe=True) if metadata else None
        if remaining:
            enqueue_followup_job("metadata")
        return {"metadata": metadata, "remaining": remaining, "score_job_id": score_job_id}
    if kind == "candidate_quality":
        quality = await audit_candidates(only_pending=False)
        enqueue_job("read_work_identities", dedupe=True)
        scored = await score_all(
            config.get("embedding_backend"),
            config.get("embedding_model"),
            config.get("embedding_url"),
            config.get("embedding_api_key"),
        )
        return {**quality, "scored": scored}
    if kind == "candidate_quality_recovery":
        quality = await audit_candidates(only_pending=True, limit=QUALITY_AUDIT_BATCH_SIZE)
        _enqueue_candidate_quality_followup()
        enqueue_job("read_work_identities", dedupe=True)
        scored = await score_all(
            config.get("embedding_backend"),
            config.get("embedding_model"),
            config.get("embedding_url"),
            config.get("embedding_api_key"),
        )
        return {**quality, "scored": scored}
    if kind.startswith("source:"):
        try:
            source_id = int(kind.partition(":")[2])
        except ValueError as exc:
            raise ValueError("Invalid source job") from exc
        source = row("SELECT * FROM sources WHERE id=?", (source_id,))
        if not source or source["kind"] == "builtin":
            raise ValueError("Source not found")
        if source["kind"] == "association":
            return {"skipped": "managed_by_association_provider", "source_id": source_id}
        if not source["enabled"]:
            return {"skipped": "disabled", "source_id": source_id}
        if source["lifecycle"] == "one_time" and _one_time_source_completed(source):
            return {"skipped": "already_scanned", "source_id": source_id}
        try:
            collected = await scan_source(source)
        except RestoreEpochError:
            raise
        except Exception as exc:
            message = safe_error_message(exc)
            with transaction() as con:
                con.execute(
                    "UPDATE sources SET last_status=?, last_attempted_at=CURRENT_TIMESTAMP WHERE id=?",
                    (f"error:{message}", source_id),
                )
            raise
        metadata = await refresh_missing_candidate_metadata()
        if candidate_metadata_refresh_remaining():
            enqueue_job("metadata", dedupe=True)
        quality = await audit_candidates(only_pending=True, limit=QUALITY_AUDIT_BATCH_SIZE)
        _enqueue_candidate_quality_followup()
        enqueue_job("read_work_identities", dedupe=True)
        scored = await score_all(config.get("embedding_backend"),config.get("embedding_model"),config.get("embedding_url"),config.get("embedding_api_key"))
        digest_job_id = queue_digest_if_due()
        return {"collected":collected,"metadata":metadata,"quality":quality,"scored":scored,"source_id":source_id,"digest_job_id":digest_job_id}
    if kind in {"sync", "sync_due"}:
        total = 0
        errors = []
        if kind == "sync_due":
            sources = source_sync_due_sources()
            if not sources:
                return {"skipped": "not_due", "collected": 0, "errors": []}
        else:
            sources = rows(
                "SELECT * FROM sources WHERE enabled=1 "
                "AND kind NOT IN ('builtin','association') "
                "AND (lifecycle='permanent' OR ("
                "lifecycle='one_time' AND last_scanned_at IS NULL "
                "AND NOT (last_attempted_at IS NOT NULL AND COALESCE(last_status,'') IN ('empty:0','filtered:0'))"
                "))"
            )
        for source in sources:
            try:
                total += await scan_source(source)
            except RestoreEpochError:
                raise
            except Exception as exc:
                message = safe_error_message(exc)
                with transaction() as con:
                    con.execute(
                        "UPDATE sources SET last_status=?, last_attempted_at=CURRENT_TIMESTAMP WHERE id=?",
                        (f"error:{message}", source["id"]),
                    )
                errors.append({"id": source["id"], "name": source["name"], "error": message})
        if kind == "sync_due" and len(errors) == len(sources):
            return {"collected": total, "errors": errors, "skipped": "all_sources_failed"}
        metadata = await refresh_missing_candidate_metadata()
        if candidate_metadata_refresh_remaining():
            enqueue_job("metadata", dedupe=True)
        quality = await audit_candidates(only_pending=True, limit=QUALITY_AUDIT_BATCH_SIZE)
        _enqueue_candidate_quality_followup()
        enqueue_job("read_work_identities", dedupe=True)
        scored = await score_all(config.get("embedding_backend"),config.get("embedding_model"),config.get("embedding_url"),config.get("embedding_api_key"))
        digest_job_id = queue_digest_if_due()
        return {"collected":total,"metadata":metadata,"quality":quality,"scored":scored,"errors":errors,"digest_job_id":digest_job_id}
    raise ValueError(f"Unsupported job kind: {kind}")

@asynccontextmanager
async def lifespan(app):
    identity_store.initialize_auth()
    ready_profiles: set[str] = set()
    try:
        with profile_scope("legacy"):
            recover_interrupted_restore()
            initialize()
            legacy_tokens = rows("SELECT id,name,token_prefix,token_hash,created_at,last_used_at,revoked_at FROM api_tokens")
        identity_store.migrate_legacy_api_tokens(legacy_tokens)
        ready_profiles.add("legacy")
    except Exception:
        LOGGER.exception("Could not initialize profile legacy during startup")
    if not has_restore_journal("legacy") or installation_key_path().is_file():
        auth_registry_key()
    for profile_id in identity_store.profile_ids():
        if profile_id != "legacy":
            try:
                _ensure_profile_database(profile_id)
                ready_profiles.add(profile_id)
            except Exception:
                # A broken profile must not prevent the rest of the installation
                # from serving. Its worker retries initialization with backoff.
                LOGGER.exception("Could not initialize profile %s during startup", profile_id)
    if not settings.enable_background_loops:
        yield
        return
    stop = asyncio.Event()
    worker = asyncio.create_task(worker_loop(handle_job, stop))
    scheduler = asyncio.create_task(source_scheduler_loop(stop))
    digest_scheduler = asyncio.create_task(digest_scheduler_loop(stop))
    goodreads_scheduler = asyncio.create_task(goodreads_rss_scheduler_loop(stop))
    backup_scheduler = asyncio.create_task(backup_scheduler_loop(stop))
    # Backfill older rows in the worker so catalog lookups never delay API
    # startup. The dedupe flag keeps restarts from creating duplicate work.
    for profile_id in identity_store.profile_ids():
        if profile_id not in ready_profiles:
            continue
        try:
            with profile_scope(profile_id):
                enqueue_job("metadata", dedupe=True)
                if has_candidate_quality_audit_candidates():
                    enqueue_job("candidate_quality_recovery", dedupe=True)
                enqueue_job("read_work_identities", dedupe=True)
                enqueue_job("read_metadata", dedupe=True)
        except Exception:
            LOGGER.exception("Could not queue startup work for profile %s", profile_id)
    try:
        yield
    finally:
        stop.set()
        await scheduler
        await digest_scheduler
        await goodreads_scheduler
        await backup_scheduler
        await worker

app = FastAPI(title="Bookward Engine", version="0.1.0", lifespan=lifespan)


@app.exception_handler(RestoreEpochError)
async def handle_restore_epoch_error(_request: Request, _exc: RestoreEpochError):
    return JSONResponse(
        {"detail": "Profile database was restored during this request; retry the action."},
        status_code=409,
        headers={"cache-control": "private, no-store"},
    )


app.add_middleware(
    CORSMiddleware,
    # The web app's public proxy owns cross-origin access. Keep the engine's
    # internal surface restricted to its configured browser origin so exposing
    # the engine port does not turn every legacy endpoint into a wildcard CORS
    # API.
    allow_origins=[settings.cors_origin],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["accept", "content-type", "authorization", "x-api-key", "idempotency-key", "x-bookward-service", "x-bookward-session", "x-oidc-state"],
    allow_credentials=False,
)


_API_TOKEN_DERIVATION_LIMITERS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()


def _api_token_derivation_limiter() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limiter = _API_TOKEN_DERIVATION_LIMITERS.get(loop)
    if limiter is None:
        limiter = asyncio.Semaphore(identity_store.TOKEN_DERIVATION_CONCURRENCY)
        _API_TOKEN_DERIVATION_LIMITERS[loop] = limiter
    return limiter


@asynccontextmanager
async def _acquire_api_token_derivation_slot():
    limiter = _api_token_derivation_limiter()
    acquired = False
    try:
        async with asyncio.timeout(identity_store.TOKEN_DERIVATION_WAIT_SECONDS):
            await limiter.acquire()
            acquired = True
        yield
    finally:
        if acquired:
            limiter.release()


_RESTORE_ROUTE_RE = re.compile(r"^/api/backups/[^/]+/restore$")


def _recover_profile_for_request(profile_id: str) -> None:
    with profile_scope(profile_id):
        recovered = recover_interrupted_restore()
    if recovered:
        _ensure_profile_database(profile_id)


@app.middleware("http")
async def authenticate_engine_request(request: Request, call_next):
    """Select a profile only from a verified session or profile-owned API token."""
    path = request.url.path
    if request.method == "OPTIONS" or path == "/api/health":
        return await call_next(request)
    if settings.service_secret:
        supplied = request.headers.get("x-bookward-service", "")
        if not hmac.compare_digest(supplied, settings.service_secret):
            return JSONResponse(
                {"detail": "Engine service authentication required"},
                status_code=401,
                headers={"cache-control": "no-store"},
            )

    if path == "/api/v1" or path.startswith("/api/v1/"):
        authorization = request.headers.get("authorization", "")
        api_key_header = request.headers.get("x-api-key")
        bearer = ""
        if authorization:
            if not authorization.lower().startswith("bearer "):
                return JSONResponse(
                    {"detail": "A valid API token is required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                )
            bearer = authorization[7:].strip()
            if not bearer:
                return JSONResponse(
                    {"detail": "A valid API token is required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                )
        api_key = api_key_header.strip() if api_key_header is not None else ""
        if ((api_key_header is not None and not api_key) or
                (bearer and api_key and not hmac.compare_digest(bearer.encode(), api_key.encode()))):
            return JSONResponse(
                {"detail": "A valid API token is required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )
        candidate = bearer or api_key
        if (
            identity_store.is_valid_api_token_format(candidate)
            and not await asyncio.to_thread(identity_store.has_cached_api_token, candidate)
        ):
            try:
                async with _acquire_api_token_derivation_slot():
                    token = await asyncio.to_thread(
                        identity_store.authenticate_api_token, candidate
                    )
            except TimeoutError:
                return JSONResponse(
                    {"detail": "API token verification is busy. Try again shortly."},
                    status_code=503,
                    headers={"Cache-Control": "no-store", "Retry-After": "1"},
                )
        else:
            token = await asyncio.to_thread(identity_store.authenticate_api_token, candidate)
        if not token:
            return JSONResponse(
                {"detail": "A valid API token is required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )
        request.state.api_token = token
        request.state.account = {"profile_id": token["profile_id"], "role": "user", "id": "api-token"}
        if has_restore_journal(token["profile_id"]):
            try:
                await asyncio.to_thread(_recover_profile_for_request, token["profile_id"])
            except Exception:
                LOGGER.exception(
                    "Authenticated profile is unavailable during API request recovery"
                )
                return JSONResponse(
                    {"detail": RESTORE_FENCE_DETAIL},
                    status_code=503,
                    headers={"cache-control": "private, no-store"},
                )
        with profile_scope(token["profile_id"]), bind_request_restore_epoch():
            try:
                response = await call_next(request)
            except RestoreEpochError:
                return JSONResponse(
                    {"detail": "Profile database was restored during this request; retry the action."},
                    status_code=409,
                    headers={"cache-control": "private, no-store"},
                )
        _mark_private_no_store(response)
        return response

    public_auth = path in {"/auth/config", "/auth/login", "/auth/setup"}
    oidc_login_start = path == "/auth/oidc/start" and request.query_params.get("intent", "login") == "login"
    oidc_callback = path == "/auth/oidc/callback"
    if oidc_callback and request.headers.get("x-bookward-session"):
        request.state.account = await asyncio.to_thread(
            identity_store.resolve_session, request.headers.get("x-bookward-session")
        )
    if public_auth or oidc_login_start or oidc_callback:
        response = await call_next(request)
        response.headers["cache-control"] = "no-store"
        return response

    account = await asyncio.to_thread(
        identity_store.resolve_session, request.headers.get("x-bookward-session")
    )
    if not account:
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    request.state.account = account
    is_restore_route = (
        request.method == "POST" and _RESTORE_ROUTE_RE.fullmatch(path) is not None
    )
    if (
        not path.startswith("/auth/")
        and not path.startswith("/admin/")
        and not is_restore_route
        and has_restore_journal(account["profile_id"])
    ):
        try:
            await asyncio.to_thread(_recover_profile_for_request, account["profile_id"])
        except Exception:
            if not (request.method == "GET" and path == "/api/backups"):
                LOGGER.exception(
                    "Profile %s is unavailable during request recovery",
                    account["profile_id"],
                )
                return JSONResponse(
                    {"detail": RESTORE_FENCE_DETAIL},
                    status_code=503,
                    headers={"cache-control": "private, no-store"},
                )
    with profile_scope(account["profile_id"]), bind_request_restore_epoch():
        try:
            response = await call_next(request)
        except RestoreEpochError:
            return JSONResponse(
                {"detail": "Profile database was restored during this request; retry the action."},
                status_code=409,
                headers={"cache-control": "private, no-store"},
            )
    _mark_private_no_store(response)
    return response


@app.get("/auth/config")
def auth_config():
    setup_pending = identity_store.setup_required()
    return {
        "setup_required": setup_pending,
        "local_login_enabled": not setup_pending,
        "oidc_enabled": bool(settings.oidc_issuer and settings.oidc_client_id and settings.oidc_client_secret),
    }


@app.post("/auth/login")
def local_login(payload: LocalLoginIn, request: Request):
    account = identity_store.authenticate_local(
        payload.username, payload.password,
        request.client.host if request.client else "unknown",
    )
    if not account:
        raise HTTPException(401, "Username or password is incorrect")
    raw, account = identity_store.create_session(account["id"])
    return {"session_token": raw, "max_age": identity_store.SESSION_IDLE_HOURS * 3600, "account": account}


@app.post("/auth/setup")
def initial_admin_setup(payload: InitialAdminIn):
    try:
        account = identity_store.create_first_admin(payload.username, payload.password)
        _ensure_profile_database("legacy")
        raw, account = identity_store.create_session(account["id"])
        return {"session_token": raw, "max_age": identity_store.SESSION_IDLE_HOURS * 3600, "account": account}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/auth/me")
def auth_me(request: Request):
    account = _principal(request)
    return {key: account[key] for key in ("id", "profile_id", "username", "display_name", "role", "must_change_password")}


@app.post("/auth/logout")
def auth_logout(request: Request):
    identity_store.revoke_session(request.headers.get("x-bookward-session"))
    return {"logged_out": True}


@app.post("/auth/password/change")
def change_password(payload: PasswordChangeIn, request: Request):
    account = _principal(request)
    methods = identity_store.auth_methods(account["id"])
    if methods["password_enabled"]:
        verified = identity_store.authenticate_local(
            account["username"], payload.current_password,
            request.client.host if request.client else "unknown",
        )
        if not verified or verified["id"] != account["id"]:
            raise HTTPException(401, "Current password is incorrect")
    else:
        _require_recent_authentication(account)
    identity_store.update_password(account["id"], payload.new_password)
    raw, _ = identity_store.create_session(account["id"])
    return {"changed": True, "session_token": raw, "max_age": identity_store.SESSION_IDLE_HOURS * 3600}


@app.get("/auth/identities")
def auth_identities(request: Request):
    return identity_store.auth_methods(_principal(request)["id"])


@app.delete("/auth/identities/{identity_id}")
def auth_unlink_identity(identity_id: int, request: Request):
    try:
        identity_store.unlink_oidc_identity(_principal(request)["id"], identity_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"unlinked": True, "identity_id": identity_id}


@app.get("/admin/accounts")
def admin_accounts(request: Request):
    _require_admin(request)
    return {"accounts": identity_store.accounts_list()}


@app.post("/admin/accounts")
def admin_create_account(payload: AccountCreateIn, request: Request):
    _require_admin(request)
    try:
        account = identity_store.create_account(
            payload.username, payload.password, payload.display_name, payload.role
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "That username is already in use") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        _ensure_profile_database(account["profile_id"])
    except Exception as exc:
        identity_store.set_account_status(account["id"], "disabled")
        LOGGER.exception("Could not initialize profile database for account %s", account["id"])
        raise HTTPException(500, "The account was created but its profile could not be initialized") from exc
    return {key: account[key] for key in ("id", "profile_id", "username", "display_name", "role", "status")}


@app.post("/admin/accounts/{account_id}/password")
def admin_reset_password(account_id: str, payload: AccountPasswordIn, request: Request):
    admin = _require_admin(request)
    if admin["id"] == account_id:
        raise HTTPException(400, "Use Account settings to change your own password")
    try:
        identity_store.reset_password(account_id, payload.password)
    except (ValueError, LookupError) as exc:
        raise HTTPException(400 if isinstance(exc, ValueError) else 404, str(exc)) from exc
    return {"changed": True, "account_id": account_id}


@app.put("/admin/accounts/{account_id}/status")
def admin_set_account_status(account_id: str, payload: dict, request: Request):
    admin = _require_admin(request)
    status = payload.get("status") if isinstance(payload, dict) else None
    if status not in {"active", "disabled"}:
        raise HTTPException(400, "Choose active or disabled status")
    accounts = identity_store.accounts_list()
    found = next((item for item in accounts if item["id"] == account_id), None)
    if not found:
        raise HTTPException(404, "Account not found")
    if account_id == admin["id"] and status == "disabled":
        raise HTTPException(400, "You cannot disable your own account")
    if status == "active":
        # Disabled profiles are excluded from startup/background work. Apply
        # any migrations accumulated while this account was disabled before
        # making its data available to a new session.
        _ensure_profile_database(found["profile_id"])
    try:
        identity_store.set_account_status(account_id, status)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"updated": True, "account_id": account_id, "status": status}


@app.delete("/admin/profiles/{profile_id}/restore-journal")
def admin_delete_restore_journal(profile_id: str, request: Request):
    _require_admin(request)
    if (
        profile_id != "legacy"
        and profile_id not in identity_store.profile_ids(active_only=False)
    ):
        raise HTTPException(404, "Profile not found")
    with profile_scope(profile_id):
        cleared = clear_interrupted_restore_journal()
    return {"cleared": cleared, "profile_id": profile_id}


def _valid_oidc_url(value: str, *, allow_local_http: bool = False) -> bool:
    if any(character.isspace() for character in value):
        return False
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return False
    secure = parsed.scheme == "https" or (
        allow_local_http and parsed.scheme == "http" and hostname in {"localhost", "127.0.0.1"}
    )
    return bool(
        parsed.netloc and hostname and secure and parsed.username is None
        and parsed.password is None and "#" not in value
    )


def _oidc_config():
    if not (settings.oidc_issuer and settings.oidc_client_id and settings.oidc_client_secret):
        raise HTTPException(404, "Single sign-on is not configured")
    issuer = settings.oidc_issuer
    if not _valid_oidc_url(issuer, allow_local_http=True):
        raise HTTPException(500, "The configured SSO issuer must use HTTPS")
    parsed_issuer = urlparse(issuer)
    if "?" in issuer:
        raise HTTPException(500, "The configured SSO issuer must use HTTPS")
    allow_local_http = (
        parsed_issuer.scheme == "http"
        and parsed_issuer.hostname in {"localhost", "127.0.0.1"}
    )
    discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    redirect_uri = settings.oidc_redirect_uri or f"{settings.public_url.rstrip('/')}/auth/oidc/callback"
    if not _valid_oidc_url(redirect_uri, allow_local_http=True):
        raise HTTPException(500, "The SSO callback URL must use HTTPS")
    return issuer, discovery_url, redirect_uri, allow_local_http


async def _oidc_metadata():
    issuer, discovery_url, redirect_uri, allow_local_http = _oidc_config()
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(discovery_url)
            response.raise_for_status()
            metadata = response.json()
    except Exception as exc:
        raise HTTPException(502, "The SSO provider could not be reached") from exc
    if not isinstance(metadata, dict) or metadata.get("issuer") != issuer or not all(
        isinstance(metadata.get(field), str) and metadata[field]
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri")
    ):
        raise HTTPException(502, "The SSO provider returned invalid discovery metadata")
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not _valid_oidc_url(metadata[field], allow_local_http=allow_local_http):
            raise HTTPException(502, "The SSO provider returned an insecure endpoint")
    return issuer, redirect_uri, metadata


@app.get("/auth/oidc/start")
async def oidc_start(request: Request, intent: Literal["login", "link"] = "login"):
    account = getattr(request.state, "account", None)
    if intent == "link" and not account:
        raise HTTPException(401, "Sign in before linking an SSO identity")
    if intent == "link":
        _require_recent_authentication(account)
    issuer, redirect_uri, metadata = await _oidc_metadata()
    state = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    try:
        identity_store.create_oidc_transaction(
            state=state, csrf=csrf, nonce=nonce, verifier=verifier,
            purpose=intent, account_id=account["id"] if intent == "link" else None,
            session_token=request.headers.get("x-bookward-session") if intent == "link" else None,
        )
    except RuntimeError as exc:
        raise HTTPException(429, "Too many pending SSO sign-ins. Try again shortly.") from exc
    query_values = {
        "client_id": settings.oidc_client_id,
        "response_type": "code",
        "scope": "openid profile email",
        "redirect_uri": redirect_uri,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorization_endpoint = urlparse(metadata["authorization_endpoint"])
    query = urlencode(
        {**dict(parse_qsl(authorization_endpoint.query, keep_blank_values=True)), **query_values}
    )
    authorization_url = urlunparse(authorization_endpoint._replace(query=query))
    return {"authorization_url": authorization_url, "csrf": csrf}


@app.get("/auth/oidc/callback")
async def oidc_callback(request: Request, code: str = Query(min_length=1, max_length=4096),
                        state: str = Query(min_length=1, max_length=256),
                        x_oidc_state: str | None = Header(default=None, alias="X-OIDC-State")):
    transaction_record = identity_store.consume_oidc_transaction(
        state, x_oidc_state or "", request.headers.get("x-bookward-session")
    )
    if not transaction_record:
        raise HTTPException(400, "The SSO sign-in expired or could not be verified. Please try again.")
    issuer, redirect_uri, metadata = await _oidc_metadata()
    auth_methods = metadata.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
    if not isinstance(auth_methods, list) or not all(isinstance(method, str) for method in auth_methods):
        raise HTTPException(502, "The SSO provider returned invalid token authentication metadata")
    if "client_secret_basic" in auth_methods:
        token_auth = (settings.oidc_client_id, settings.oidc_client_secret)
        token_client_fields = {}
    elif "client_secret_post" in auth_methods:
        token_auth = None
        token_client_fields = {
            "client_id": settings.oidc_client_id,
            "client_secret": settings.oidc_client_secret,
        }
    else:
        raise HTTPException(502, "The SSO provider does not support a configured client authentication method")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            token_response = await client.post(
                metadata["token_endpoint"],
                data={
                    "grant_type": "authorization_code", "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": transaction_record["code_verifier"],
                    **token_client_fields,
                },
                auth=token_auth,
            )
            token_response.raise_for_status()
            tokens = token_response.json()
            id_token = tokens.get("id_token", "")
            header = jwt.get_unverified_header(id_token)
            keys_response = await client.get(metadata["jwks_uri"])
            keys_response.raise_for_status()
            jwks = keys_response.json().get("keys", [])
        jwk = next((key for key in jwks if key.get("kid") == header.get("kid")), None)
        if not jwk:
            raise ValueError("Unknown signing key")
        signing_key = jwt.PyJWK.from_dict(jwk).key
        claims = jwt.decode(
            id_token, signing_key, algorithms=["RS256", "ES256", "PS256", "EdDSA"],
            audience=settings.oidc_client_id, issuer=issuer, leeway=60,
            options={"require": ["exp", "iat", "iss", "sub", "aud", "nonce"]},
        )
        if not hmac.compare_digest(str(claims.get("nonce", "")), transaction_record["nonce"]):
            raise ValueError("Invalid nonce")
        audience = claims.get("aud")
        authorized_party = claims.get("azp")
        if authorized_party is not None and authorized_party != settings.oidc_client_id:
            raise ValueError("Invalid authorized party")
        if isinstance(audience, list) and len(audience) > 1 and authorized_party != settings.oidc_client_id:
            raise ValueError("Invalid authorized party")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise ValueError("Invalid subject")
    except Exception as exc:
        LOGGER.info("Rejected OIDC callback: %s", type(exc).__name__)
        raise HTTPException(401, "The SSO provider could not verify this sign-in") from exc

    email_claim = claims.get("email")
    email = email_claim[:320] if isinstance(email_claim, str) else ""
    name_claim = claims.get("name") or claims.get("preferred_username")
    name = (name_claim if isinstance(name_claim, str) else email or "SSO user")[:160]
    if transaction_record["purpose"] == "link":
        current = getattr(request.state, "account", None)
        if not current or current["id"] != transaction_record["account_id"]:
            raise HTTPException(401, "Sign in again before linking this identity")
        try:
            identity_store.link_oidc_identity(current["id"], issuer, subject, email)
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"linked": True, "account": current}

    account = identity_store.account_for_oidc_identity(issuer, subject)
    if not account and settings.oidc_auto_provision:
        try:
            account = identity_store.create_oidc_account(issuer, subject, email, name)
        except PermissionError as exc:
            raise HTTPException(403, "This SSO account is disabled") from exc
    if not account:
        raise HTTPException(403, "This SSO identity is not linked to a Bookward account")
    try:
        _ensure_profile_database(account["profile_id"])
    except Exception as exc:
        LOGGER.exception("Could not initialize OIDC profile %s", account["profile_id"])
        raise HTTPException(503, "Your Bookward profile is temporarily unavailable. Please try again.") from exc
    raw, account = identity_store.create_session(account["id"])
    return {"session_token": raw, "max_age": identity_store.SESSION_IDLE_HOURS * 3600, "account": account}


@app.get("/api/backups")
def get_backup_status():
    return backup_status()


@app.get("/admin/auth-backups")
def get_identity_backup_status(request: Request):
    _require_admin(request)
    return identity_backup_status()


@app.post("/api/backups/create")
def make_backup():
    try:
        created = create_backup()
    except BackupError as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"backup": created, "status": backup_status()}


class StorageRetentionIn(BaseModel):
    enabled: bool


@app.post("/api/backups/compact")
def compact_database_storage():
    try:
        return compact_database()
    except BackupError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/backups/retention")
def update_storage_retention(payload: StorageRetentionIn):
    set_storage_retention_enabled(payload.enabled)
    status = backup_status()
    return {
        "retention_enabled": bool(status.get("retention_enabled", payload.enabled)),
        "status": status,
    }


@app.post("/api/backups/evidence-rotation")
def update_evidence_rotation(payload: StorageRetentionIn):
    set_evidence_rotation_enabled(payload.enabled)
    status = backup_status()
    return {
        "evidence_rotation_enabled": bool(
            status.get("evidence_rotation_enabled", payload.enabled)
        ),
        "status": status,
    }


@app.post("/api/backups/{backup_id}/restore")
def restore_database_backup(backup_id: str):
    try:
        return restore_backup(backup_id)
    except BackupError as exc:
        raise HTTPException(400, str(exc)) from exc

class SourceFilters(BaseModel):
    include_genres: list[str] = Field(default_factory=list, max_length=12)
    exclude_genres: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("include_genres", "exclude_genres", mode="before")
    @classmethod
    def normalize_genres(cls, value):
        return normalize_source_filters({"include_genres": value})["include_genres"]


class SourceIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    url: HttpUrl
    enabled: bool = True
    weight: float = Field(default=1, ge=.25, le=2)
    lifecycle: Literal["permanent", "one_time"] = "permanent"
    filters: SourceFilters = Field(default_factory=SourceFilters)


class SourceUpdateIn(BaseModel):
    url: HttpUrl | None = None
    filters: SourceFilters | None = None
    lifecycle: Literal["permanent", "one_time"] | None = None


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
    app_url: str | None = None
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


class GoodreadsRSSSettingsIn(BaseModel):
    url: str = Field(default="", max_length=2048)


class SourcePreviewIn(UrlIn):
    filters: SourceFilters = Field(default_factory=SourceFilters)


class FeedbackIn(BaseModel):
    action: Literal["save", "reject", "maybe_later", "restore"]
    run_id: str | None = Field(default=None, min_length=8, max_length=128)


class UndoFeedbackIn(BaseModel):
    decision_id: int = Field(gt=0)


class MarkReadIn(BaseModel):
    rating: int | None = Field(default=None, ge=1, le=5)
    session_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
    )


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
        if value not in EVENT_TYPES - {
            "save", "reject", "maybe_later", "restore", "read"
        }:
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


class BulkUndoItemIn(BaseModel):
    id: int = Field(gt=0)
    decision_id: int = Field(gt=0)


class BulkUndoFeedbackIn(BaseModel):
    decisions: list[BulkUndoItemIn] = Field(min_length=1, max_length=100)


class ApiTokenCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class EngineSettings(BaseModel):
    embedding_backend: str = "local"
    embedding_model: str = "hashing-768"
    embedding_url: str = ""
    embedding_api_key: str = ""
    librarr_url: str = ""
    librarr_api_key: str = ""
    librarr_media_type: str = "audiobook"

    def validate_values(
        self,
        *,
        validate_embedding: bool = True,
        validate_librarr: bool = True,
        check_librarr_host: bool = True,
    ):
        if validate_embedding:
            if self.embedding_backend not in {"local","fastembed","ollama","openai-compatible"}: raise ValueError("Unsupported embedding backend")
            parsed = urlparse(self.embedding_url) if self.embedding_url else None
            if parsed and parsed.scheme not in {"http","https"}: raise ValueError("embedding URL must use HTTP or HTTPS")
            if parsed and (parsed.username or parsed.password or parsed.query or parsed.fragment): raise ValueError("embedding URL cannot contain credentials, query parameters, or fragments")
            if self.embedding_backend in {"ollama","openai-compatible"} and not self.embedding_url: raise ValueError("Remote embedding providers require an endpoint URL")
        if validate_librarr:
            parsed = urlparse(self.librarr_url) if self.librarr_url else None
            if parsed and parsed.scheme not in {"http","https"}: raise ValueError("Librarr URL must use HTTP or HTTPS")
            if parsed and (parsed.username or parsed.password or parsed.query or parsed.fragment): raise ValueError("Librarr URL cannot contain credentials, query parameters, or fragments")
            normalize_media_type(self.librarr_media_type)
            if check_librarr_host and self.librarr_url:
                allowed = {host.strip().lower() for host in settings.librarr_allowed_hosts.split(",") if host.strip()}
                validate_service_url(self.librarr_url, allowed)


class NYTApiKeyIn(BaseModel):
    api_key: str | None = Field(default=None, max_length=500)


class AssociationSettingsIn(BaseModel):
    openlibrary_enabled: bool = False
    openlibrary_contact: str = Field(default="", max_length=200)
    librarything_enabled: bool = False
    librarything_api_key: str | None = Field(default=None, max_length=500)
    google_books_api_key: str | None = Field(default=None, max_length=500)


class GoogleBooksKeyIn(BaseModel):
    api_key: str | None = Field(default=None, max_length=500)
    clear: bool = False


class LLMRerankSettingsIn(BaseModel):
    enabled: bool = False
    mode: str | None = Field(default=None, max_length=40)
    provider: str = Field(default="", max_length=120)
    api_url: str = Field(default="", max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    model: str = Field(default="", max_length=200)
    clear_api_key: bool = False

    def validate_values(self):
        url = self.api_url.strip()
        if url:
            llm_reranker.validate_llm_api_url(url, has_api_key=True)
        if self.enabled and (not url or not self.model.strip()):
            raise ValueError("LLM reranking requires both an API URL and a model")


class LLMRerankTestIn(BaseModel):
    mode: str | None = Field(default=None, max_length=40)
    provider: str | None = Field(default=None, max_length=120)
    api_url: str = Field(default="", max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    model: str = Field(default="", max_length=200)
    clear_api_key: bool = False


def _persist_llm_rerank_status(
    *,
    status: str,
    error: str = "",
    attempted_at: str | None = None,
    private: dict | None = None,
) -> dict[str, str]:
    stamp = attempted_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    updates: dict[str, str] = {
        "llm_rerank_last_status": status,
        "llm_rerank_last_attempt_at": stamp,
        "llm_rerank_last_error": error if status == "failed" else "",
    }
    if status == "succeeded":
        updates["llm_rerank_last_success_at"] = stamp
    with transaction() as con:
        for key, value in updates.items():
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,0) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=0,updated_at=CURRENT_TIMESTAMP",
                (key, value),
            )
    if private is not None:
        private.update(updates)
    return updates


def api_token_list():
    """Return token metadata without ever returning a token value."""
    return identity_store.list_profile_api_tokens(current_profile_id() or "legacy")


def _parse_json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _distinct_genres(items: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, str] = {}
    for item in items:
        for genre in _parse_json_list(item.get("genres")):
            cleaned = str(genre).strip()
            if cleaned:
                seen.setdefault(cleaned.casefold(), cleaned)
    return sorted(seen.values(), key=lambda value: (value.casefold(), value))


def _matches_recommendation_filter(
    item: dict[str, Any],
    *,
    search: str | None = None,
    genre: str | None = None,
) -> bool:
    search_norm = (search or "").strip().casefold()
    genre_norm = (genre or "").strip().casefold()
    if not search_norm and not genre_norm:
        return True
    genres = [str(value).strip() for value in _parse_json_list(item.get("genres")) if str(value).strip()]
    if genre_norm and not any(value.casefold() == genre_norm for value in genres):
        return False
    if search_norm:
        haystack = (
            f"{item.get('title', '')} {item.get('author', '')} "
            f"{' '.join(genres)} {item.get('description', '')}"
        ).casefold()
        if search_norm not in haystack:
            return False
    return True


def _format_recommendation_item(
    item: dict[str, Any], *, format_urls: bool = True
) -> dict[str, Any]:
    formatted = dict(item)
    formatted["genres"] = _parse_json_list(formatted.get("genres"))
    formatted["explanation"] = _parse_json_list(formatted.get("explanation"))
    if format_urls:
        _format_recommendation_urls([formatted])
    return formatted


def _load_llm_rerank_inputs(connection, interaction_events: list[dict[str, Any]]):
    fetch = (
        (lambda sql, values=(): [dict(item) for item in connection.execute(sql, values).fetchall()])
        if connection is not None
        else rows
    )
    raw_reads = fetch(
        "SELECT * FROM reads ORDER BY COALESCE(read_at, created_at) DESC, id DESC"
    )
    try:
        read_metadata_rows = fetch(
            "SELECT read_id,verified_work_id,identity_provider,identity_provider_id,identity_hash "
            "FROM read_metadata WHERE verified_work_id!=''"
        )
    except sqlite3.OperationalError as exc:
        if "no such table: read_metadata" not in str(exc).casefold():
            raise
        read_metadata_rows = []
    read_rows = read_identity_rows_with_current_verified_work_ids(
        raw_reads, read_metadata_rows
    )

    event_cids = sorted(
        {
            int(ev["candidate_id"])
            for ev in interaction_events
            if ev.get("candidate_id") is not None
        }
    )
    candidate_identities: dict[int, dict[str, Any]] = {}
    for start in range(0, len(event_cids), 500):
        batch = event_cids[start : start + 500]
        if not batch:
            continue
        marks = ",".join("?" for _ in batch)
        try:
            found = fetch(
                "SELECT c.id,c.source_url,c.isbn13,c.isbn10,"
                "q.work_id quality_work_id,q.provider quality_provider,"
                "q.isbn13 quality_isbn13,q.isbn10 quality_isbn10 "
                "FROM candidates c LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
                f"WHERE c.id IN ({marks})",
                batch,
            )
            for item in found:
                candidate_identities[int(item["id"])] = dict(item)
        except sqlite3.OperationalError:
            break

    llm_events = [
        {**candidate_identities.get(int(ev["candidate_id"]), {}), **ev}
        if ev.get("candidate_id") is not None
        else dict(ev)
        for ev in interaction_events
    ]
    return read_rows, llm_events


def _prepare_tracked_recommendations(
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    search: str | None = None,
    genre: str | None = None,
    connection=None,
    recommended_limit: int | None = None,
    identity_context=None,
    include_imported: bool = False,
    private: dict | None = None,
    pool_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ranking_at = datetime.now(timezone.utc)
    is_filtered = bool((search and search.strip()) or (genre and genre.strip()))
    base_pool = personalized_pool = None
    ranked = None
    recommendations = None
    interaction_events: list[dict[str, Any]] = []
    cached_vectors: dict[int, Any] = {}
    read_rows: list[dict[str, Any]] = []
    llm_events: list[dict[str, Any]] = []
    llm_cfg = {
        "enabled": False,
        "provider": "",
        "mode": "",
        "api_url": "",
        "api_key": "",
        "model": "",
    }
    llm_active = False
    llm_needs_network = False
    cached_llm_result: tuple[list[dict[str, Any]], dict[str, Any]] | None = None
    profile_id = current_profile_id() or "legacy"
    profile_key = llm_reranker.current_llm_profile_key()
    top_k = max(0, int(settings.llm_rerank_top_k))
    discovery_metadata = empty_discovery_slate_diagnostics("not_discovery_request")
    learning_metadata: dict[str, Any] = {
        "mode": "confidence_gated_live",
        "scope": "installation",
        "applied": False,
        "observed_books": 0,
        "qualified_features": 0,
        "semantic_evidence_books": 0,
        "semantic_adjusted_candidates": 0,
        "adjusted_candidates": 0,
        "max_score_adjustment": 0.0,
    }
    can_personalize = status in (None, "", "all", "recommended")
    fallback_triggered = False
    if can_personalize:
        try:
            # Score the full eligible recommendation pool before pagination so
            # a learned preference can promote a strong match from below the
            # unpersonalized page boundary.
            ranked = recommendation_list(
                connection,
                status=status,
                limit=None,
                offset=0,
                identity_context=identity_context,
                include_imported=include_imported,
                format_urls=False,
            )
            if not is_filtered:
                base_pool = [dict(item) for item in ranked]
            interaction_events = load_interaction_events(connection)
            # The event primary key orders actions; cached vectors are keyed by
            # candidate id. Normalize only the cache inputs so learner event
            # ordering and latest-event semantics keep their original ids.
            cache_events = [
                {**event, "id": event["candidate_id"]}
                for event in interaction_events
            ]
            cached_vectors = load_cached_candidate_vectors(
                [*ranked, *cache_events], connection
            )
            interaction_vectors = {
                int(event["candidate_id"]): cached_vectors[int(event["candidate_id"])]
                for event in interaction_events
                if int(event["candidate_id"]) in cached_vectors
            }
            ranked, diagnostics = personalize_recommendations(
                ranked,
                interaction_events,
                now=ranking_at,
                interaction_vectors=interaction_vectors,
                candidate_vectors=cached_vectors,
            )
            if settings.impression_fatigue_enabled:
                visible_day_counts = load_unacted_visible_impression_counts(
                    connection,
                    window_days=settings.impression_fatigue_window_days,
                    now=ranking_at,
                )
                ranked, fatigue_diagnostics = apply_impression_fatigue(
                    ranked,
                    visible_day_counts,
                    grace_days=settings.impression_fatigue_grace_days,
                    penalty_per_day=settings.impression_fatigue_penalty_per_day,
                    max_penalty=settings.impression_fatigue_max_penalty,
                )
                diagnostics["impression_fatigue"] = fatigue_diagnostics
            if not is_filtered:
                personalized_pool = [dict(item) for item in ranked]
            learning_metadata.update(diagnostics)
            recommended_candidates = [
                item for item in ranked if item.get("status") == "recommended"
            ]
            if not is_filtered and recommended_candidates:
                try:
                    llm_cfg = effective_llm_rerank_settings(connection, private)
                    if llm_cfg["enabled"] and llm_cfg["api_key"].strip():
                        skip_zero_head = (
                            top_k <= 0
                            or (
                                recommended_limit is not None
                                and int(recommended_limit) <= 0
                            )
                            or (limit is not None and int(limit) <= 0)
                        )
                        if skip_zero_head:
                            learning_metadata["llm_rerank"] = {
                                "applied": False,
                                "reason": "skipped_zero_head",
                            }
                        elif int(offset) >= top_k:
                            learning_metadata["llm_rerank"] = {
                                "applied": False,
                                "reason": "skipped_offset",
                            }
                        elif len(recommended_candidates) < 2:
                            learning_metadata["llm_rerank"] = {
                                "applied": False,
                                "reason": "insufficient_candidates",
                            }
                        else:
                            circuit = (
                                None
                                if rerank_with_llm_sync is not _default_rerank_with_llm_sync
                                else llm_reranker.get_circuit_breaker_state(
                                    profile_key,
                                    api_url=llm_cfg["api_url"],
                                    api_key=llm_cfg["api_key"],
                                    model=llm_cfg["model"],
                                    top_k=top_k,
                                    max_delta=settings.llm_rerank_max_delta,
                                    mode=llm_cfg.get("mode", ""),
                                )
                            )
                            if circuit is not None:
                                learning_metadata["llm_rerank"] = {
                                    "applied": False,
                                    "reason": "circuit_open",
                                    "cooldown_until": circuit.get("cooldown_until", ""),
                                }
                            else:
                                llm_active = True
                                read_rows, llm_events = _load_llm_rerank_inputs(
                                    connection, interaction_events
                                )
                                if rerank_with_llm_sync is _default_rerank_with_llm_sync:
                                    cached_llm_result = llm_reranker.try_cached_rerank(
                                        read_rows,
                                        recommended_candidates,
                                        interaction_events=llm_events,
                                        profile_id=profile_id,
                                        api_url=llm_cfg["api_url"],
                                        api_key=llm_cfg["api_key"],
                                        model=llm_cfg["model"],
                                        top_k=top_k,
                                        max_delta=settings.llm_rerank_max_delta,
                                        mode=llm_cfg.get("mode", ""),
                                    )
                                llm_needs_network = cached_llm_result is None
                except Exception as exc:
                    LOGGER.warning(
                        "Failed to load LLM rerank inputs; serving personalized base slate: %s",
                        safe_error_message(exc),
                    )
                    llm_active = False
                    llm_needs_network = False
        except Exception:
            LOGGER.exception(
                "Interaction personalization failed; serving the base ranking"
            )
            fallback_triggered = True
            base_pool = personalized_pool = None
            ranked = None
            if pool_summary is not None:
                fallback_recommended = _recommendation_rows(
                    connection,
                    statuses=("recommended",),
                    limit=None,
                    identity_context=identity_context,
                )
                pool_summary["recommended_total"] = len(fallback_recommended)
                pool_summary["recommendation_genres"] = _distinct_genres(
                    fallback_recommended
                )
            recommendations = recommendation_list(
                connection,
                status=status,
                limit=limit,
                offset=offset,
                search=search,
                genre=genre,
                recommended_limit=recommended_limit,
                identity_context=identity_context,
                include_imported=include_imported,
            )
            learning_metadata = {
                "mode": "base_ranker_fallback",
                "scope": "installation",
                "applied": False,
                "observed_books": 0,
                "qualified_features": 0,
                "semantic_evidence_books": 0,
                "semantic_adjusted_candidates": 0,
                "adjusted_candidates": 0,
                "max_score_adjustment": 0.0,
            }
            discovery_metadata = empty_discovery_slate_diagnostics(
                "base_ranker_fallback"
            )
    else:
        recommendations = recommendation_list(
            connection,
            status=status,
            limit=limit,
            offset=offset,
            search=search,
            genre=genre,
            recommended_limit=recommended_limit,
            identity_context=identity_context,
            include_imported=include_imported,
        )

    return {
        "status": status,
        "limit": limit,
        "offset": offset,
        "search": search,
        "genre": genre,
        "is_filtered": is_filtered,
        "pool_summary": pool_summary,
        "recommended_limit": recommended_limit,
        "identity_context": identity_context,
        "include_imported": include_imported,
        "ranking_at": ranking_at,
        "can_personalize": can_personalize,
        "fallback_triggered": fallback_triggered,
        "base_pool": base_pool,
        "personalized_pool": personalized_pool,
        "ranked": ranked,
        "recommendations": recommendations,
        "interaction_events": interaction_events,
        "cached_vectors": cached_vectors,
        "learning_metadata": learning_metadata,
        "discovery_metadata": discovery_metadata,
        "llm_active": llm_active,
        "llm_needs_network": llm_needs_network,
        "cached_llm_result": cached_llm_result,
        "llm_cfg": llm_cfg,
        "read_rows": read_rows,
        "llm_events": llm_events,
        "profile_id": profile_id,
        "profile_key": profile_key,
        "top_k": top_k,
        "private": private,
    }


def _complete_tracked_recommendations(
    prepared: dict[str, Any],
    *,
    connection=None,
):
    status = prepared["status"]
    limit = prepared["limit"]
    offset = prepared["offset"]
    search = prepared["search"]
    genre = prepared["genre"]
    is_filtered = prepared["is_filtered"]
    pool_summary = prepared["pool_summary"]
    recommended_limit = prepared["recommended_limit"]
    ranking_at = prepared["ranking_at"]
    can_personalize = prepared["can_personalize"]
    base_pool = prepared["base_pool"]
    personalized_pool = prepared["personalized_pool"]
    ranked = prepared["ranked"]
    recommendations = prepared["recommendations"]
    interaction_events = prepared["interaction_events"]
    cached_vectors = prepared["cached_vectors"]
    learning_metadata = prepared["learning_metadata"]
    discovery_metadata = prepared["discovery_metadata"]
    final_pool = None

    if can_personalize and not prepared["fallback_triggered"] and ranked is not None:
        try:
            recommended = [
                item for item in ranked if item.get("status") == "recommended"
            ]
            if recommended:
                if not is_filtered and prepared["llm_active"]:
                    if prepared["cached_llm_result"] is not None:
                        recommended, llm_diag = prepared["cached_llm_result"]
                        learning_metadata["llm_rerank"] = llm_diag
                    else:
                        llm_cfg = prepared["llm_cfg"]
                        profile_key = prepared["profile_key"]
                        llm_mode = llm_cfg.get("mode", "")
                        unreranked_recommended = [dict(item) for item in recommended]
                        rerank_fn = (
                            rerank_with_llm_sync
                            if rerank_with_llm_sync is not _default_rerank_with_llm_sync
                            else llm_reranker.rerank_with_llm_sync
                        )
                        direct_exception: Exception | None = None
                        try:
                            rerank_kwargs: dict[str, Any] = {
                                "interaction_events": prepared["llm_events"],
                                "profile_id": prepared["profile_id"],
                                "api_url": llm_cfg["api_url"],
                                "api_key": llm_cfg["api_key"],
                                "model": llm_cfg["model"],
                            }
                            if rerank_fn is llm_reranker._DEFAULT_RERANK_SYNC_FN:
                                rerank_kwargs["mode"] = llm_mode
                            recommended, llm_diag = rerank_fn(
                                prepared["read_rows"],
                                recommended,
                                **rerank_kwargs,
                            )
                        except Exception as exc:
                            direct_exception = exc
                            LOGGER.warning(
                                "LLM reranker failed; preserving personalized slate: %s",
                                safe_error_message(exc),
                            )
                            recommended = unreranked_recommended
                            llm_diag = {"applied": False, "reason": "fallback"}

                        if llm_diag.get("applied") and not llm_diag.get("cached"):
                            llm_reranker.clear_circuit_breaker(profile_key)
                            _persist_llm_rerank_status(
                                status="succeeded",
                                error="",
                                private=prepared.get("private"),
                            )
                        elif llm_diag.get("reason") == "fallback":
                            raw_err = (
                                direct_exception
                                if direct_exception is not None
                                else (
                                    llm_reranker.last_rerank_error()
                                    or "LLM reranking request failed"
                                )
                            )
                            err_msg = llm_reranker.sanitize_llm_error(
                                raw_err,
                                api_key=llm_cfg["api_key"],
                            )
                            llm_reranker.trip_circuit_breaker(
                                profile_key,
                                api_url=llm_cfg["api_url"],
                                api_key=llm_cfg["api_key"],
                                model=llm_cfg["model"],
                                error=err_msg,
                                top_k=prepared["top_k"],
                                max_delta=settings.llm_rerank_max_delta,
                                mode=llm_mode,
                            )
                            _persist_llm_rerank_status(
                                status="failed",
                                error=err_msg,
                                private=prepared.get("private"),
                            )
                        learning_metadata["llm_rerank"] = llm_diag
                # Apply bounded anti-redundancy swaps after LLM score adjustments.
                diversified, discovery_metadata = diversify_discovery_slate(
                    recommended, cached_vectors
                )
                ranked = diversified + [
                    item for item in ranked if item.get("status") != "recommended"
                ]
            else:
                discovery_metadata = empty_discovery_slate_diagnostics(
                    "no_recommended_candidates"
                )
            if pool_summary is not None:
                all_recommended = [
                    item for item in ranked if item.get("status") == "recommended"
                ]
                pool_summary["recommended_total"] = len(all_recommended)
                pool_summary["recommendation_genres"] = _distinct_genres(all_recommended)
            if not is_filtered:
                final_pool = [dict(item) for item in ranked]
            else:
                ranked = [
                    item
                    for item in ranked
                    if _matches_recommendation_filter(item, search=search, genre=genre)
                ]
            if recommended_limit is not None and status is None and offset == 0:
                recommended_out = [
                    item for item in ranked if item.get("status") == "recommended"
                ]
                remaining = [
                    item
                    for item in ranked
                    if item.get("status") in {"saved", "imported", "new"}
                ]
                recommendations = (
                    recommended_out[: max(0, int(recommended_limit))] + remaining
                )
            else:
                start = max(0, int(offset))
                end = None if limit is None else start + max(0, int(limit))
                recommendations = ranked[start:end]
            _format_recommendation_urls(recommendations)
        except Exception:
            LOGGER.exception(
                "Post-personalization slate assembly failed; serving base ranking"
            )
            base_pool = personalized_pool = final_pool = None
            if pool_summary is not None:
                fallback_recommended = _recommendation_rows(
                    connection,
                    statuses=("recommended",),
                    limit=None,
                    identity_context=prepared["identity_context"],
                )
                pool_summary["recommended_total"] = len(fallback_recommended)
                pool_summary["recommendation_genres"] = _distinct_genres(fallback_recommended)
            recommendations = recommendation_list(
                connection,
                status=status,
                limit=limit,
                offset=offset,
                search=search,
                genre=genre,
                recommended_limit=recommended_limit,
                identity_context=prepared["identity_context"],
                include_imported=prepared["include_imported"],
            )
            learning_metadata = {
                "mode": "base_ranker_fallback",
                "scope": "installation",
                "applied": False,
                "observed_books": 0,
                "qualified_features": 0,
                "semantic_evidence_books": 0,
                "semantic_adjusted_candidates": 0,
                "adjusted_candidates": 0,
                "max_score_adjustment": 0.0,
            }
            discovery_metadata = empty_discovery_slate_diagnostics(
                "base_ranker_fallback"
            )

    if not can_personalize:
        # Non-ranking requests (shortlist, history, decisions) never create a
        # recommendation run or explore the tail.
        return recommendations, ""
    # Keep exploration outside the scorer and behind an environment flag.  A
    # disabled deployment receives the same deterministic order and scores as
    # before, while enabled traffic records exact tail propensities.
    if settings.exploration_enabled:
        recommendations = epsilon_tail_explore(
            recommendations,
            epsilon=settings.exploration_epsilon,
            stable_top_k=settings.exploration_stable_top_k,
        )
    if is_filtered:
        decision_evidence = {
            "schema_version": 1,
            "capture_status": "incomplete",
            "reason": "filtered_query",
            "captured_at": ranking_at.isoformat(),
        }
    elif base_pool is not None and personalized_pool is not None and final_pool is not None:
        decision_evidence = build_decision_evidence(
            connection,
            base_pool=base_pool,
            personalized_pool=personalized_pool,
            final_pool=final_pool,
            interaction_events=interaction_events,
            cached_vectors=cached_vectors,
            ranking_metadata=learning_metadata,
            discovery_slate_metadata=discovery_metadata,
            request_context={
                "status": status,
                "limit": limit,
                "offset": offset,
                "recommended_limit": recommended_limit,
                "ranking_at": ranking_at.isoformat(),
                "exploration_enabled": settings.exploration_enabled,
                "exploration_epsilon": settings.exploration_epsilon,
                "exploration_stable_top_k": settings.exploration_stable_top_k,
            },
        )
    else:
        decision_evidence = {
            "schema_version": 1,
            "capture_status": "incomplete",
            "reason": "base_ranker_fallback" if can_personalize else "non_ranking_request",
            "captured_at": ranking_at.isoformat(),
        }
    run_id = create_recommendation_run(
        recommendations,
        decision_evidence=decision_evidence,
        status=status,
        limit=limit,
        ranking_metadata=learning_metadata,
        discovery_slate_metadata=discovery_metadata,
    )
    return recommendations, run_id


def tracked_recommendations(
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    search: str | None = None,
    genre: str | None = None,
    connection=None,
    recommended_limit: int | None = None,
    identity_context=None,
    include_imported: bool = False,
    private: dict | None = None,
    pool_summary: dict[str, Any] | None = None,
):
    if connection is not None:
        prepared = _prepare_tracked_recommendations(
            status=status,
            limit=limit,
            offset=offset,
            search=search,
            genre=genre,
            connection=connection,
            recommended_limit=recommended_limit,
            identity_context=identity_context,
            include_imported=include_imported,
            private=private,
            pool_summary=pool_summary,
        )
        return _complete_tracked_recommendations(prepared, connection=connection)

    with connect() as read_con:
        prepared = _prepare_tracked_recommendations(
            status=status,
            limit=limit,
            offset=offset,
            search=search,
            genre=genre,
            connection=read_con,
            recommended_limit=recommended_limit,
            identity_context=identity_context,
            include_imported=include_imported,
            private=private,
            pool_summary=pool_summary,
        )
        if not prepared["llm_needs_network"]:
            return _complete_tracked_recommendations(prepared, connection=read_con)
    # Release _LockedConnection before executing outbound LLM network I/O.
    return _complete_tracked_recommendations(prepared, connection=None)


def overview_payload(
    *,
    include_api_tokens: bool = True,
    recommendation_limit: int | None = None,
    decision_limit: int = 24,
):
    def source_payload(source):
        item = dict(source)
        item["filters"] = normalize_source_filters(item.get("filters"))
        return item

    deferred_prepared = None
    recommendations = None
    run_id = None
    pool_summary: dict[str, Any] = {}
    with connect() as connection:
        counts = {
            name: connection.execute(f"SELECT COUNT(*) count FROM {name}").fetchone()["count"]
            for name in ("reads", "candidates", "sources")
        }
        identity_context = _recommendation_identity_context(connection)
        private = private_settings(connection)
        prepared = _prepare_tracked_recommendations(
            connection=connection,
            limit=recommendation_limit or 100,
            recommended_limit=recommendation_limit,
            identity_context=identity_context,
            include_imported=True,
            private=private,
            pool_summary=pool_summary,
        )
        if prepared["llm_needs_network"]:
            deferred_prepared = prepared
        else:
            recommendations, run_id = _complete_tracked_recommendations(
                prepared, connection=connection
            )
        all_decision_rows = _recommendation_rows(
            connection,
            statuses=("rejected", "maybe_later"),
            limit=None,
            identity_context=identity_context,
        )
        decision_genres = _distinct_genres(all_decision_rows)
        decisions_has_more = len(all_decision_rows) > decision_limit
        decisions = [
            _format_recommendation_item(item)
            for item in all_decision_rows[:decision_limit]
        ]
        sources_list = [
            source_payload(item)
            for item in connection.execute(
                "SELECT * FROM sources ORDER BY is_default DESC,name"
            ).fetchall()
        ]
        history_list = [
            dict(item)
            for item in connection.execute(
                "SELECT * FROM reads ORDER BY COALESCE(read_at,created_at) DESC LIMIT 12"
            ).fetchall()
        ]
        settings_payload = safe_settings(
            include_api_tokens=include_api_tokens,
            connection=connection,
            private=private,
        )

    if deferred_prepared is not None:
        recommendations, run_id = _complete_tracked_recommendations(
            deferred_prepared, connection=None
        )
        settings_payload["llm_rerank"] = safe_llm_rerank_settings(
            connection=None, private=private
        )
    served_recommended = sum(
        1 for item in recommendations if item.get("status") == "recommended"
    )
    recommendations_has_more = (
        int(pool_summary.get("recommended_total", served_recommended))
        > served_recommended
    )
    recommendation_genres = list(pool_summary.get("recommendation_genres") or [])
    return {
        "counts": counts,
        "recommendations": recommendations,
        "recommendations_has_more": recommendations_has_more,
        "recommendation_genres": recommendation_genres,
        "decisions": decisions,
        "decisions_has_more": decisions_has_more,
        "decision_genres": decision_genres,
        "recommendation_run_id": run_id,
        "sources": sources_list,
        "history": history_list,
        "settings": settings_payload,
    }


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/overview")
def overview(
    recommendation_limit: int | None = Query(default=None, ge=1, le=100),
    decision_limit: int = Query(default=24, ge=1, le=24),
):
    return overview_payload(
        recommendation_limit=recommendation_limit,
        decision_limit=decision_limit,
    )


@app.get("/api/reading-history")
def reading_history():
    """Return the read list without waiting for current-profile scoring."""
    with connect() as connection:
        items = [dict(item) for item in connection.execute(
            "SELECT id,title,author,rating,read_at,source,created_at FROM reads "
            "ORDER BY COALESCE(read_at,created_at) DESC,id DESC"
        ).fetchall()]
        existing_scores = []
        candidate_keys = list(dict.fromkeys(
            normalize_key(item["title"], item["author"]) for item in items
        ))
        for start in range(0, len(candidate_keys), 500):
            key_batch = candidate_keys[start:start + 500]
            placeholders = ",".join("?" for _ in key_batch)
            existing_scores.extend(connection.execute(
                "SELECT title,author,score FROM candidates "
                f"WHERE status!='new' AND normalized_key IN ({placeholders}) "
                "ORDER BY updated_at DESC,id DESC",
                key_batch,
            ).fetchall())
    scores = {}
    for candidate in existing_scores:
        key = (str(candidate["title"]).strip().casefold(), str(candidate["author"]).strip().casefold())
        scores.setdefault(key, candidate["score"])
    for item in items:
        key = (str(item["title"]).strip().casefold(), str(item["author"]).strip().casefold())
        item["rank_score"] = scores.get(key)
        item["algorithm_score"] = None
        item["algorithm_explanation"] = []
        item["metadata_confidence"] = None
    return items


@app.get("/api/reading-history/scores")
async def reading_history_scores(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=20),
):
    """Rank a bounded slice of read books against the current reading profile."""
    with connect() as connection:
        items = [dict(item) for item in connection.execute(
            "SELECT id,title,author,rating,read_at,source,created_at FROM reads "
            "ORDER BY COALESCE(read_at,created_at) DESC,id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()]
        candidate_keys = list(dict.fromkeys(
            normalize_key(item["title"], item["author"]) for item in items
        ))
        placeholders = ",".join("?" for _ in candidate_keys)
        existing_candidates = (
            [dict(item) for item in connection.execute(
                "SELECT c.title,c.author,c.description,c.genres,c.status,"
                "q.quality_score,q.quality_status,s.weight source_weight,s.name source_name "
                "FROM candidates c "
                "LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
                "LEFT JOIN sources s ON s.id=c.source_id "
                f"WHERE c.status!='new' AND c.normalized_key IN ({placeholders}) "
                "ORDER BY c.updated_at DESC,c.id DESC",
                candidate_keys,
            ).fetchall()]
            if candidate_keys else []
        )
    if not items:
        return []
    candidates_by_identity = {}
    for candidate in existing_candidates:
        key = (str(candidate["title"]).strip().casefold(), str(candidate["author"]).strip().casefold())
        candidates_by_identity.setdefault(key, candidate)
    virtual_candidates = []
    for item in items:
        key = (str(item["title"]).strip().casefold(), str(item["author"]).strip().casefold())
        matched = candidates_by_identity.get(key)
        virtual = {
            "id": -int(item["id"]),
            "title": item["title"],
            "author": item["author"],
            "description": matched.get("description", "") if matched else "",
            "genres": matched.get("genres", "[]") if matched else "[]",
            "source_weight": (matched.get("source_weight") or 1) if matched else 1,
        }
        if matched:
            quality_score = float(matched.get("quality_score") or 0)
            virtual["catalog_confidence"] = (
                quality_score
                if quality_score > 0
                else 0.85 if matched.get("quality_status") == "accepted" else 0
            )
            virtual["source_name"] = matched.get("source_name") or ""
        virtual_candidates.append(virtual)

    try:
        if virtual_candidates:
            config = private_settings()
            embedder = get_embedder(
                config.get("embedding_backend"),
                config.get("embedding_model"),
                config.get("embedding_url"),
                config.get("embedding_api_key"),
            )
            rated_reads = rows(
                "SELECT * FROM reads WHERE rating BETWEEN 1 AND 5 ORDER BY id"
            )
            read_vectors = await cached_vectors(embedder, "read", rated_reads)
            candidate_vectors = await cached_vectors(
                embedder,
                "read_candidate",
                virtual_candidates,
                persist=False,
                expected_dimensions=_vector_dimensions(read_vectors),
            )
            ranked = rank_candidates(
                rated_reads,
                read_vectors,
                virtual_candidates,
                candidate_vectors,
                exclude_candidate_identity=True,
            )
            ranked_by_id = {int(item["id"]): item for item in ranked}
            return [
                {
                    "id": int(item["id"]),
                    "algorithm_score": ranked_by_id.get(-int(item["id"]), {}).get("score", 50.0),
                    "algorithm_explanation": ranked_by_id.get(-int(item["id"]), {}).get("explanation", []),
                    "metadata_confidence": ranked_by_id.get(-int(item["id"]), {}).get("metadata_confidence", 1.0),
                }
                for item in items
            ]
    except Exception:
        LOGGER.exception("Could not calculate current-profile reading history scores")
        raise

def _recommendation_identity_context(connection=None):
    fetch = (
        lambda sql, values=(): [dict(item) for item in connection.execute(sql, values).fetchall()]
        if connection is not None
        else rows(sql, values)
    )
    read_rows = fetch("SELECT * FROM reads")
    try:
        read_metadata_rows = fetch(
            "SELECT read_id,verified_work_id,identity_provider,identity_provider_id,identity_hash "
            "FROM read_metadata WHERE verified_work_id!=''"
        )
    except sqlite3.OperationalError as exc:
        if "no such table: read_metadata" not in str(exc).casefold():
            raise
        read_metadata_rows = []
    read_identity_rows = read_identity_rows_with_current_verified_work_ids(
        read_rows, read_metadata_rows
    )
    read_identity_index = ReadIdentityIndex.from_reads(read_identity_rows)
    shortlist_rows = fetch(
        "SELECT c.*,q.work_id quality_work_id,q.provider quality_provider,"
        "q.provider_id AS quality_provider_id,"
        "q.isbn13 quality_isbn13,q.isbn10 quality_isbn10 "
        "FROM candidates c LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
        "WHERE c.status IN ('saved','imported')"
    )
    shortlisted_keys = book_identity_match_index(shortlist_rows)
    return read_identity_index, shortlisted_keys


def _recommendation_rows(
    connection=None,
    *,
    statuses: tuple[str, ...] | None = None,
    limit: int | None = 100,
    offset: int = 0,
    search: str | None = None,
    genre: str | None = None,
    identity_context=None,
    include_imported: bool = False,
):
    decision_statuses = {"rejected", "maybe_later"}
    is_decision_archive = bool(statuses) and set(statuses).issubset(decision_statuses)
    if is_decision_archive:
        clauses = []
    else:
        clauses = [
            "c.status NOT IN ('rejected','maybe_later')",
            "(c.status IN ('saved','imported') OR q.quality_status='accepted')",
            "(c.status IN ('saved','imported') OR s.enabled=1)",
        ]
    params: list[object] = []
    if statuses:
        if is_decision_archive:
            archive_terms = []
            if "rejected" in statuses:
                archive_terms.append(
                    "(c.status='rejected' AND EXISTS(SELECT 1 FROM feedback f "
                    "WHERE f.candidate_id=c.id AND f.action='reject' AND f.undone_at IS NULL))"
                )
            if "maybe_later" in statuses:
                archive_terms.append("c.status='maybe_later'")
            clauses.append("(" + " OR ".join(archive_terms) + ")")
        else:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"c.status IN ({placeholders})")
            params.extend(statuses)
    decision_select = (
        ",COALESCE((SELECT f.created_at FROM feedback f "
        "WHERE f.candidate_id=c.id AND f.undone_at IS NULL "
        "AND f.action=CASE c.status WHEN 'rejected' THEN 'reject' ELSE 'maybe_later' END "
        "ORDER BY f.id DESC LIMIT 1),c.updated_at,c.created_at) active_decision_at"
        ",COALESCE((SELECT f.id FROM feedback f "
        "WHERE f.candidate_id=c.id AND f.undone_at IS NULL "
        "AND f.action=CASE c.status WHEN 'rejected' THEN 'reject' ELSE 'maybe_later' END "
        "ORDER BY f.id DESC LIMIT 1),0) active_decision_id"
        if is_decision_archive
        else ""
    )
    order_clause = (
        " ORDER BY COALESCE(unixepoch(active_decision_at,'subsec'),0) DESC,"
        "active_decision_at DESC,active_decision_id DESC,c.score DESC,c.id DESC"
        if is_decision_archive
        else " ORDER BY CASE c.status WHEN 'recommended' THEN 0 ELSE 1 END, c.score DESC, c.id DESC"
    )
    query = (
        "SELECT c.*,s.name source_name,q.metadata_confidence,"
        "q.work_id quality_work_id,"
        "q.provider quality_provider,q.provider_id AS quality_provider_id,"
        "q.isbn13 quality_isbn13,"
        f"q.isbn10 quality_isbn10{decision_select} "
        "FROM candidates c "
        "LEFT JOIN sources s ON s.id=c.source_id "
        "LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
        "WHERE "
        + " AND ".join(clauses)
        + order_clause
    )
    result = (
        [dict(item) for item in connection.execute(query, params).fetchall()]
        if connection is not None
        else rows(query, params)
    )
    if identity_context is None:
        identity_context = _recommendation_identity_context(connection)
    read_identity_index, shortlisted_keys = identity_context
    has_filters = bool((search and search.strip()) or (genre and genre.strip()))
    visible = []
    seen_recommendation_keys = set()
    for item in result:
        item.pop("active_decision_at", None)
        item.pop("active_decision_id", None)
        if item["status"] == "saved":
            visible.append(item)
            continue
        keys = book_row_identity_match_keys(item)
        if item["status"] == "imported":
            if not include_imported:
                continue
            visible.append(item)
            continue
        if read_identity_index.matches(item) or keys & shortlisted_keys:
            continue
        if item["status"] == "recommended" and keys & seen_recommendation_keys:
            continue
        if item["status"] == "recommended":
            seen_recommendation_keys.update(keys)
        visible.append(item)
    if has_filters:
        visible = [
            item
            for item in visible
            if _matches_recommendation_filter(item, search=search, genre=genre)
        ]
    end = None if limit is None else offset + limit
    return visible[offset:end]


def _format_recommendation_urls(items):
    for item in items:
        source_url = item.get("source_url", "")
        item["cover_url"] = fallback_cover_url(
            item["title"],
            item["author"],
            item.get("cover_url", ""),
            source_url,
        )
        item["source_url"] = canonical_book_source_url(
            item["title"],
            item["author"],
            source_url,
            item=item,
        )
    return items


def recommendation_list(
    connection=None,
    *,
    status: str | None = None,
    limit: int | None = 100,
    offset: int = 0,
    search: str | None = None,
    genre: str | None = None,
    recommended_limit: int | None = None,
    identity_context=None,
    include_imported: bool = False,
    format_urls: bool = True,
):
    effective_include_imported = include_imported or status in {"saved", "imported", "all"}
    if identity_context is None:
        identity_context = _recommendation_identity_context(connection)
    if recommended_limit is not None and status is None and offset == 0:
        result = _recommendation_rows(
            connection,
            statuses=("recommended",),
            limit=recommended_limit,
            search=search,
            genre=genre,
            identity_context=identity_context,
        )
        result.extend(
            _recommendation_rows(
                connection,
                statuses=("saved", "imported", "new"),
                limit=None,
                search=search,
                genre=genre,
                identity_context=identity_context,
                include_imported=effective_include_imported,
            )
        )
    else:
        statuses = (
            ("rejected", "maybe_later")
            if status == "decisions"
            else ("saved", "imported")
            if status == "saved" and effective_include_imported
            else None if not status or status == "all" else (status,)
        )
        result = _recommendation_rows(
            connection,
            statuses=statuses,
            limit=limit,
            offset=offset,
            search=search,
            genre=genre,
            identity_context=identity_context,
            include_imported=effective_include_imported,
        )
    for item in result:
        item["genres"] = _parse_json_list(item.get("genres"))
        item["explanation"] = _parse_json_list(item.get("explanation"))
    if format_urls:
        _format_recommendation_urls(result)
    return result

@app.get("/api/recommendations")
def recommendations(
    response: Response,
    status: Annotated[Literal["recommended", "saved", "imported", "all", "decisions", "rejected", "maybe_later"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    search: Annotated[str | None, Query(max_length=200)] = None,
    genre: Annotated[str | None, Query(max_length=100)] = None,
):
    values, run_id = tracked_recommendations(
        status=status,
        limit=limit,
        offset=offset,
        search=search,
        genre=genre,
        include_imported=status in {"saved", "imported", "all"},
    )
    for item in values:
        item["recommendation_run_id"] = run_id
    if run_id:
        response.headers["X-Bookward-Recommendation-Run"] = run_id
    return values


def _mark_candidate_read_in_connection(con, candidate, rating=None, session_id=None):
    existing = con.execute(
        "SELECT id,rating,read_at,source FROM reads WHERE title=? AND author=?",
        (candidate["title"], candidate["author"]),
    ).fetchone()
    saved_rating = rating if rating is not None else existing["rating"] if existing else None
    read_at = datetime.now(timezone.utc).isoformat()
    openlibrary_work_id = book_openlibrary_work_id(candidate)
    isbn13, _isbn10 = isbn_parts(
        candidate["quality_isbn13"], candidate["quality_isbn10"]
    )
    con.execute(
        "INSERT INTO reads(title,author,rating,read_at,isbn,source,"
        "openlibrary_work_id,openlibrary_lookup_attempted_at) "
        "VALUES(?,?,?,?,?,'manual',?,"
        "CASE WHEN ?!='' THEN CURRENT_TIMESTAMP ELSE NULL END) "
        "ON CONFLICT(title,author) DO UPDATE SET "
        "rating=COALESCE(excluded.rating,reads.rating),"
        "read_at=COALESCE(reads.read_at,excluded.read_at),"
        "isbn=COALESCE(reads.isbn,excluded.isbn)",
        (
            candidate["title"],
            candidate["author"],
            rating,
            read_at,
            isbn13 or None,
            openlibrary_work_id,
            openlibrary_work_id,
        ),
    )
    read = con.execute(
        "SELECT id,rating,read_at,source,isbn,openlibrary_work_id "
        "FROM reads WHERE title=? AND author=?",
        (candidate["title"], candidate["author"]),
    ).fetchone()
    if openlibrary_work_id:
        con.execute(
            "UPDATE reads SET openlibrary_work_id=?,"
            "openlibrary_lookup_attempted_at=COALESCE(openlibrary_lookup_attempted_at,CURRENT_TIMESTAMP) "
            "WHERE id=? AND COALESCE(openlibrary_work_id,'')=''",
            (openlibrary_work_id, read["id"]),
        )
        read = con.execute(
            "SELECT id,rating,read_at,source,isbn,openlibrary_work_id "
            "FROM reads WHERE id=?",
            (read["id"],),
        ).fetchone()
    read_time, day_precision = _read_time(read["read_at"])
    impression = None
    if read_time is not None:
        prior_impressions = con.execute(
            "SELECT id,run_id,rank,score,propensity,presented_at,visible_at "
            "FROM recommendation_impressions WHERE candidate_id=? "
            "ORDER BY COALESCE(visible_at,presented_at) DESC,id DESC",
            (candidate["id"],),
        ).fetchall()
        impression = next(
            (
                item for item in prior_impressions
                if _impression_precedes(item, read_time, day_precision)
            ),
            None,
        )
    label, label_kind, confidence = _read_label({"rating": saved_rating})
    event_metadata = {
        "read_source": str(read["source"] or "unknown")[:32],
        "time_precision": "day" if day_precision else "timestamp",
    }
    try:
        record_event_in_connection(
            con,
            event_key=f"manual-read:{int(candidate['id'])}:{int(read['id']):08d}",
            candidate_id=int(candidate["id"]),
            event_type="read",
            value=saved_rating,
            source="manual_read",
            run_id=str(impression["run_id"]) if impression is not None else None,
            metadata=event_metadata,
            occurred_at=read["read_at"] or read_at,
            label=label if impression is not None else None,
            label_kind=label_kind if impression is not None else None,
            confidence=confidence if impression is not None else None,
            read_id=int(read["id"]) if impression is not None else None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return read, existing is not None


@app.post("/api/recommendations/{candidate_id}/feedback")
def feedback(candidate_id: int, payload: FeedbackIn):
    status = {
        "save": "saved",
        "reject": "rejected",
        "maybe_later": "maybe_later",
        "restore": "recommended",
    }[payload.action]
    with transaction() as con:
        candidate = con.execute(
            "SELECT status FROM candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        if not candidate:
            raise HTTPException(404, "Recommendation not found")
        previous_status = str(candidate["status"])
        con.execute("UPDATE candidates SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status,candidate_id))
        feedback_id = con.execute(
            "INSERT INTO feedback(candidate_id,action,previous_status) VALUES(?,?,?)",
            (candidate_id, payload.action, previous_status),
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
    result = {
        "id": candidate_id,
        "status": status,
    }
    if (
        payload.action in {"save", "reject", "maybe_later", "restore"}
        and previous_status
        and previous_status != "read"
    ):
        result["decision_id"] = int(feedback_id)
    return result


def _undo_feedback_in_connection(con, candidate_id: int, decision_id: int):
    decision = con.execute(
        "SELECT id,action,previous_status,undone_at FROM feedback "
        "WHERE id=? AND candidate_id=?",
        (decision_id, candidate_id),
    ).fetchone()
    if (
        not decision
        or not decision["previous_status"]
        or decision["previous_status"] == "read"
    ):
        raise HTTPException(404, "Undo is no longer available")
    if decision["undone_at"]:
        raise HTTPException(409, "This decision has already been undone")
    if decision["action"] not in {"save", "reject", "maybe_later", "restore"}:
        raise HTTPException(404, "Undo is no longer available")
    latest = con.execute(
        "SELECT id FROM feedback WHERE candidate_id=? "
        "ORDER BY id DESC LIMIT 1",
        (candidate_id,),
    ).fetchone()
    if not latest or int(latest["id"]) != decision_id:
        raise HTTPException(409, "Only the latest decision can be undone")
    current = con.execute(
        "SELECT status FROM candidates WHERE id=?", (candidate_id,)
    ).fetchone()
    if not current:
        raise HTTPException(404, "Recommendation not found")
    expected_status = {
        "save": "saved",
        "reject": "rejected",
        "maybe_later": "maybe_later",
        "restore": "recommended",
    }[decision["action"]]
    if current["status"] != expected_status:
        raise HTTPException(409, "The recommendation changed after this decision")
    previous_status = str(decision["previous_status"])
    con.execute(
        "UPDATE candidates SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (previous_status, candidate_id),
    )
    con.execute(
        "UPDATE feedback SET undone_at=CURRENT_TIMESTAMP WHERE id=?",
        (decision_id,),
    )
    con.execute(
        "DELETE FROM recommendation_outcomes WHERE event_id=("
        "SELECT id FROM recommendation_events WHERE event_key=?)",
        (f"feedback:{decision_id}",),
    )
    compensation = (
        "librarr_import" if previous_status == "imported"
        else "save" if previous_status == "saved"
        else "reject" if previous_status == "rejected"
        else "maybe_later" if previous_status == "maybe_later"
        else "restore"
    )
    try:
        record_event_in_connection(
            con,
            event_key=f"undo-feedback:{decision_id}",
            candidate_id=candidate_id,
            event_type=compensation,
            source="ui_undo",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": candidate_id, "status": previous_status}


@app.post("/api/recommendations/{candidate_id}/undo")
def undo_feedback(candidate_id: int, payload: UndoFeedbackIn):
    with transaction() as con:
        return _undo_feedback_in_connection(con, candidate_id, payload.decision_id)


@app.post("/api/recommendations/{candidate_id}/read")
def mark_recommendation_read(candidate_id: int, payload: MarkReadIn):
    with transaction() as con:
        candidate = con.execute(
            "SELECT c.id,c.title,c.author,c.source_url,"
            "q.work_id quality_work_id,q.provider quality_provider,"
            "COALESCE(NULLIF(q.isbn13,''),NULLIF(c.isbn13,''),'') quality_isbn13,"
            "COALESCE(NULLIF(q.isbn10,''),NULLIF(c.isbn10,''),'') quality_isbn10 "
            "FROM candidates c LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
            "WHERE c.id=?",
            (candidate_id,),
        ).fetchone()
        if not candidate:
            raise HTTPException(404, "Recommendation not found")
        read, already_present = _mark_candidate_read_in_connection(
            con,
            candidate,
            rating=payload.rating,
            session_id=payload.session_id,
        )
        con.execute(
            "UPDATE candidates SET status='read',updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (candidate_id,),
        )

    read_work_identities_job_id = (
        enqueue_job("read_work_identities", dedupe=True) if read["isbn"] else None
    )
    read_metadata_job_id = (
        enqueue_job("read_metadata", dedupe=True) if read["isbn"] else None
    )
    job_id = enqueue_job("score", dedupe=True)
    return {
        "id": candidate_id,
        "read_id": int(read["id"]),
        "status": "read",
        "rating": read["rating"],
        "job_id": job_id,
        "read_work_identities_job_id": read_work_identities_job_id,
        "read_metadata_job_id": read_metadata_job_id,
        "already_present": already_present,
    }


@app.post("/api/recommendations/bulk-feedback")
def bulk_feedback(payload: BulkFeedbackIn):
    status = {"save": "saved", "reject": "rejected", "restore": "recommended"}[payload.action]
    ids = list(dict.fromkeys(payload.ids))
    decisions = []
    with transaction() as con:
        placeholders = ",".join("?" for _ in ids)
        found = {
            int(item["id"]): str(item["status"])
            for item in con.execute(
                f"SELECT id,status FROM candidates WHERE id IN ({placeholders})", ids
            ).fetchall()
        }
        for candidate_id in ids:
            if candidate_id not in found:
                continue
            previous_status = found[candidate_id]
            con.execute("UPDATE candidates SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, candidate_id))
            feedback_id = con.execute(
                "INSERT INTO feedback(candidate_id,action,previous_status) VALUES(?,?,?)",
                (candidate_id, payload.action, previous_status),
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
            if (
                payload.action in {"save", "reject", "restore"}
                and previous_status
                and previous_status != "read"
            ):
                decisions.append({"id": candidate_id, "decision_id": int(feedback_id)})
    return {
        "updated": len(found),
        "skipped": len(ids) - len(found),
        "status": status,
        "ids": sorted(found),
        "decisions": decisions,
    }


@app.post("/api/recommendations/bulk-undo")
def bulk_undo_feedback(payload: BulkUndoFeedbackIn):
    items = list(dict.fromkeys((item.id, item.decision_id) for item in payload.decisions))
    restored = []
    with transaction() as con:
        for candidate_id, decision_id in items:
            restored.append(_undo_feedback_in_connection(con, candidate_id, decision_id))
    return {"undone": len(restored), "items": restored}


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

class LibrarrImportIn(BaseModel):
    run_id: str | None = Field(default=None, min_length=8, max_length=128)


def _record_librarr_import_in_connection(con, candidate_id: int, *, run_id: str | None = None):
    next_seq = int(
        con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM recommendation_events").fetchone()[0]
    )
    return record_event_in_connection(
        con,
        event_key=f"librarr-import:{int(candidate_id)}:{next_seq:08d}",
        candidate_id=int(candidate_id),
        event_type="librarr_import",
        run_id=run_id,
        source="librarr",
        label=1.0,
        label_kind="librarr_import",
        confidence=0.7,
    )


@app.post("/api/recommendations/{candidate_id}/import")
@fenced_async
async def import_librarr(candidate_id: int, payload: LibrarrImportIn | None = Body(default=None)):
    run_id = payload.run_id if isinstance(payload, LibrarrImportIn) else None
    with connect() as con:
        candidate_row = con.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if not candidate_row:
            raise HTTPException(404, "Recommendation not found")
        candidate = dict(candidate_row)
        try:
            validate_candidate_run_id(con, candidate_id, run_id)
        except ValueError as exc:
            raise HTTPException(400, safe_error_message(exc)) from exc
    key = hashlib.sha256(f"{candidate_id}:{candidate['normalized_key']}".encode()).hexdigest()
    existing = row("SELECT * FROM librarr_imports WHERE idempotency_key=? AND status='complete'", (key,))
    if existing:
        if candidate["status"] not in {"saved", "imported", "read"}:
            raise HTTPException(409, "Shortlist this book before importing it")
        with transaction() as con:
            current = con.execute("SELECT status FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            current_status = str(current["status"]) if current else str(candidate["status"])
            if current_status != "read":
                if current_status != "imported":
                    con.execute(
                        "UPDATE candidates SET status='imported',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status!='read'",
                        (candidate_id,),
                    )
                current_status = "imported"
                _record_librarr_import_in_connection(con, candidate_id, run_id=run_id)
        return {"id": candidate_id, "status": current_status, "remote_id": existing["remote_id"], "duplicate": True}
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
        remote_id = str(data.get("id", "imported"))
        with transaction() as con:
            con.execute("INSERT INTO librarr_imports(candidate_id,idempotency_key,status,remote_id) VALUES(?,?, 'complete',?) ON CONFLICT(idempotency_key) DO UPDATE SET status='complete',remote_id=excluded.remote_id,updated_at=CURRENT_TIMESTAMP", (candidate_id, key, remote_id))
            current = con.execute("SELECT status FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            current_status = str(current["status"]) if current else "imported"
            if current_status != "read":
                con.execute("UPDATE candidates SET status='imported',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status!='read'", (candidate_id,))
                current_status = "imported"
                _record_librarr_import_in_connection(con, candidate_id, run_id=run_id)
        return {"id": candidate_id, "status": current_status, "remote_id": remote_id}
    except (httpx.HTTPError, ValueError) as exc:
        message = safe_error_message(exc, limit=1000)
        with transaction() as con: con.execute("UPDATE librarr_imports SET status='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE idempotency_key=?",(message,key))
        raise HTTPException(502, f"Librarr request failed: {message}")


@app.get("/api/librarr/search")
async def search_librarr(q: str = Query(min_length=2, max_length=200), media_type: str = Query(default="audiobook")):
    try:
        media = normalize_media_type(media_type)
        config = {**private_settings(), "librarr_allowed_hosts": settings.librarr_allowed_hosts}
        return await librarr_search(config, q, media)
    except ValueError as exc:
        raise HTTPException(400, safe_error_message(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Librarr search failed: {safe_error_message(exc)}") from exc


@app.get("/api/librarr/search/stream")
async def stream_librarr_search(
    q: str = Query(min_length=2, max_length=200),
    media_type: str = Query(default="audiobook"),
):
    client: httpx.AsyncClient | None = None
    upstream: httpx.Response | None = None
    try:
        media = normalize_media_type(media_type)
        config = {**private_settings(), "librarr_allowed_hosts": settings.librarr_allowed_hosts}
        client, upstream = await librarr_open_search_stream(config, q, media)
    except StreamingUnsupportedError as exc:
        raise HTTPException(501, safe_error_message(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, safe_error_message(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Librarr search failed: {safe_error_message(exc)}") from exc

    assert client is not None and upstream is not None

    async def stream_bytes():
        try:
            async for chunk in upstream.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_bytes(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


class LibrarrDownloadIn(BaseModel):
    media_type: str = "audiobook"
    result: dict[str, Any]
    candidate_id: int | None = Field(default=None, gt=0)
    run_id: str | None = Field(default=None, min_length=8, max_length=128)


@app.post("/api/librarr/download")
@fenced_async
async def download_librarr(payload: LibrarrDownloadIn):
    try:
        media = normalize_media_type(payload.media_type)
        with connect() as con:
            candidate_row = (
                con.execute("SELECT id,status FROM candidates WHERE id=?", (payload.candidate_id,)).fetchone()
                if payload.candidate_id
                else None
            )
            if payload.candidate_id and not candidate_row:
                raise HTTPException(404, "Recommendation not found")
            candidate = dict(candidate_row) if candidate_row else None
            validate_candidate_run_id(con, int(candidate["id"]) if candidate else None, payload.run_id)
        encoded = json.dumps(payload.result, sort_keys=True, separators=(",", ":"))
        key = hashlib.sha256(f"{media}:{encoded}".encode()).hexdigest()
        config = {**private_settings(), "librarr_allowed_hosts": settings.librarr_allowed_hosts}
        result = await librarr_download(config, payload.result, media, key)
        response: dict[str, Any] = {"ok": True, "media_type": media, "result": result}
        if candidate:
            with transaction() as con:
                current = con.execute(
                    "SELECT status FROM candidates WHERE id=?", (candidate["id"],)
                ).fetchone()
                status = str(current["status"]) if current else str(candidate["status"])
                if status != "read":
                    if status != "imported":
                        con.execute(
                            "UPDATE candidates SET status='imported',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status!='read'",
                            (candidate["id"],),
                        )
                    status = "imported"
                    _record_librarr_import_in_connection(
                        con, int(candidate["id"]), run_id=payload.run_id
                    )
            response["candidate"] = {"id": candidate["id"], "status": status}
        return response
    except ValueError as exc:
        raise HTTPException(400, safe_error_message(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Librarr download failed: {safe_error_message(exc)}") from exc

@fenced_async
async def _import_csv_upload(file: UploadFile):
    content = bytearray()
    while chunk := await file.read(1024 * 1024):
        content.extend(chunk)
        if len(content) > 10_000_000: raise HTTPException(413, "CSV is larger than 10 MB")
    try: result = import_csv(bytes(content))
    except ValueError as exc: raise HTTPException(400, safe_error_message(exc))
    enqueue_job("read_work_identities", dedupe=True)
    return {
        "imported": result["imported"],
        "source": result["source"],
        "job_id": enqueue_job("score", dedupe=True),
    }


@app.post("/api/import/csv")
async def reading_csv(file: UploadFile = File(...)):
    return await _import_csv_upload(file)


@app.post("/api/import/goodreads/csv")
async def goodreads_csv(file: UploadFile = File(...)):
    return await _import_csv_upload(file)


@fenced_async
async def _sync_goodreads_rss(url: str):
    try:
        async with ingestion._source_client() as client:
            read_result, shelf_results = await asyncio.gather(
                import_goodreads_rss(url, client=client),
                ingestion._fetch_goodreads_shelves(url, client=client),
                return_exceptions=True,
            )
        if isinstance(read_result, BaseException):
            raise read_result
        if isinstance(shelf_results, BaseException):
            raise shelf_results
        count = read_result
        shelves = ingestion._apply_goodreads_shelves(shelf_results)
    except RestoreEpochError:
        raise
    except Exception as exc:
        attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        message = safe_error_message(exc, limit=300)
        with transaction() as con:
            for key, value in (
                ("goodreads_rss_last_status", "failed"),
                ("goodreads_rss_last_attempt_at", attempted_at),
                ("goodreads_rss_last_error", message),
            ):
                con.execute(
                    "INSERT INTO settings(key,value,secret) VALUES(?,?,0) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=0,updated_at=CURRENT_TIMESTAMP",
                    (key, value),
                )
        raise
    synced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with transaction() as con:
        for key, value in (
            ("goodreads_rss_last_sync_at", synced_at),
            ("goodreads_rss_last_status", "succeeded"),
            ("goodreads_rss_last_attempt_at", synced_at),
            ("goodreads_rss_last_error", ""),
        ):
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,0) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=0,updated_at=CURRENT_TIMESTAMP",
                (key, value),
            )
    return {"imported": count, "shelves": shelves, "synced_at": synced_at}


@app.put("/api/settings/goodreads-rss")
def update_goodreads_rss_settings(payload: GoodreadsRSSSettingsIn):
    url = payload.url.strip()
    if url:
        try:
            url = validate_goodreads_rss_url(url)
        except ValueError as exc:
            raise HTTPException(400, safe_error_message(exc)) from exc
    with transaction() as con:
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES('goodreads_rss_url',?,1) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=1,updated_at=CURRENT_TIMESTAMP",
            (seal(url) if url else "",),
        )
    return {"saved": True, "configured": bool(url)}


@app.post("/api/import/goodreads/rss")
async def goodreads_rss(payload: UrlIn):
    try: result = await _sync_goodreads_rss(str(payload.url))
    except (ValueError,httpx.HTTPError) as exc: raise HTTPException(400,safe_error_message(exc))
    return {**result, "job_id": enqueue_job("score", dedupe=True)}

@app.post("/api/sources/preview")
async def source_preview(payload: SourcePreviewIn):
    _reject_inline_nyt_key(str(payload.url))
    try: return await preview_source(str(payload.url), payload.filters.model_dump())
    except (ValueError,httpx.HTTPError) as exc: raise HTTPException(400,safe_error_message(exc))

def _reject_inline_nyt_key(url: str):
    parsed = urlparse(url)
    if (parsed.hostname or "").casefold() == "api.nytimes.com" and any(
        key.casefold() == "api-key" for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        raise HTTPException(
            400,
            "Store NYT credentials in encrypted NYT API settings instead of the source URL.",
        )

@app.post("/api/sources")
def add_source(payload: SourceIn):
    _reject_inline_nyt_key(str(payload.url))
    try: validate_public_url(str(payload.url))
    except ValueError as exc: raise HTTPException(400,safe_error_message(exc))
    with transaction() as con:
        try: cursor=con.execute("INSERT INTO sources(name,url,enabled,weight,lifecycle,filters) VALUES(?,?,?,?,?,?)",(payload.name,str(payload.url),payload.enabled,payload.weight,payload.lifecycle,json.dumps(payload.filters.model_dump())))
        except Exception as exc: raise HTTPException(409,"Source already exists") from exc
    job_id = enqueue_job(f"source:{cursor.lastrowid}", dedupe=True) if payload.enabled else None
    return {"id":cursor.lastrowid, "filters": payload.filters.model_dump(), "job_id":job_id}


def _toggle_source_row(con, source_id: int, source) -> bool:
    became_enabled = not source["enabled"]
    con.execute(
        "UPDATE sources SET enabled=1-enabled, "
        "last_scanned_at=CASE WHEN enabled=0 AND lifecycle='one_time' THEN NULL ELSE last_scanned_at END, "
        "last_attempted_at=CASE WHEN enabled=0 AND lifecycle='one_time' THEN NULL ELSE last_attempted_at END "
        "WHERE id=?",
        (source_id,),
    )
    if not became_enabled:
        con.execute(
            "UPDATE candidates SET status='new', score=0, explanation='[]', "
            "score_batch_id=NULL, updated_at=CURRENT_TIMESTAMP "
            "WHERE source_id=? AND status='recommended'",
            (source_id,),
        )
    return became_enabled


@app.put("/api/sources/default/toggle")
def toggle_default_source():
    with transaction() as con:
        source = con.execute(
            "SELECT id,enabled,kind,lifecycle FROM sources WHERE is_default=1 ORDER BY name,id LIMIT 1"
        ).fetchone()
        if not source:
            raise HTTPException(404, "Default source not found.")
        source_id = source["id"]
        became_enabled = _toggle_source_row(con, source_id, source)
    job_id = (
        enqueue_job(f"source:{source_id}", dedupe=True)
        if became_enabled and source["kind"] not in {"builtin", "association"}
        else None
    )
    return {"id": source_id, "enabled": int(became_enabled), "job_id": job_id}


@app.put("/api/sources/{source_id}/toggle")
def toggle_source(source_id:int):
    with transaction() as con:
        source = con.execute("SELECT enabled,kind,lifecycle FROM sources WHERE id=?", (source_id,)).fetchone()
        if not source: raise HTTPException(404,"Source not found")
        became_enabled = _toggle_source_row(con, source_id, source)
    job_id = (
        enqueue_job(f"source:{source_id}", dedupe=True)
        if became_enabled and source["kind"] not in {"builtin", "association"}
        else None
    )
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


@app.get("/api/settings/nyt")
def nyt_settings():
    return {"api_key_set": bool(private_settings().get("nyt_api_key"))}


@app.put("/api/settings/nyt")
def update_nyt_settings(payload: NYTApiKeyIn):
    current = private_settings().get("nyt_api_key", "")
    api_key = current if payload.api_key is None else payload.api_key.strip()
    stored = seal(api_key) if api_key else ""
    with transaction() as con:
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES('nyt_api_key',?,1) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=1,"
            "updated_at=CURRENT_TIMESTAMP",
            (stored,),
        )
    return {"saved": True, "api_key_set": bool(api_key)}


def _one_time_source_completed(source) -> bool:
    return bool(
        source["last_scanned_at"]
        or (
            source["last_attempted_at"]
            and str(source["last_status"] or "") in {"empty:0", "filtered:0"}
        )
    )


def _clear_google_books_metadata_lockouts(con) -> bool:
    con.execute(
        """UPDATE candidate_quality
        SET metadata_checked_at=NULL,
            metadata_retry_status='',
            updated_at=CURRENT_TIMESTAMP
        WHERE metadata_checked_at IS NOT NULL
          AND metadata_retry_status IN ('google_books_transient', 'google_books_error')
          AND candidate_id IN (
            SELECT id FROM candidates WHERE status!='rejected'
          )"""
    )
    requeued = con.execute(
        """UPDATE candidate_quality
        SET quality_status='pending',
            metadata_retry_status='',
            updated_at=CURRENT_TIMESTAMP
        WHERE quality_status='quarantine'
          AND metadata_retry_status IN ('google_books_transient', 'google_books_error')
          AND (
            flags_json LIKE '%catalog_provider_unavailable%'
            OR flags_json LIKE '%catalog_provider_rate_limited%'
          )
          AND candidate_id IN (
            SELECT id FROM candidates WHERE status!='rejected'
          )"""
    ).rowcount
    con.execute(
        """UPDATE read_metadata
        SET metadata_checked_at=NULL
        WHERE metadata_checked_at IS NOT NULL
          AND (
            description=''
            OR cover_url='' OR cover_url LIKE '%/b/isbn/%'
            OR cover_url LIKE 'https://placehold.co/%'
            OR release_date=''
            OR CASE WHEN json_valid(genres) THEN json_array_length(genres) ELSE 0 END=0
          )
          AND EXISTS (
            SELECT 1
            FROM json_each(
              CASE
                WHEN json_valid(metadata_provenance)
                  AND json_type(metadata_provenance, '$.last_attempt.fetch_trace')='array'
                THEN json_extract(metadata_provenance, '$.last_attempt.fetch_trace')
                WHEN json_valid(metadata_provenance)
                  AND json_type(metadata_provenance, '$.fetch_trace')='array'
                THEN json_extract(metadata_provenance, '$.fetch_trace')
                ELSE '[]'
              END
            )
            WHERE json_extract(value, '$.provider')='google_books'
              AND (
                json_extract(value, '$.status') IN (
                  'credential_error',
                  'rate_limited',
                  'server_error',
                  'request_error',
                  'timeout',
                  'provider_unavailable'
                )
                OR json_extract(value, '$.http_status') IN (400, 401, 403, 408, 429)
                OR (
                  json_type(value, '$.http_status')='integer'
                  AND json_extract(value, '$.http_status')>=500
                )
              )
          )"""
    )
    return bool(requeued)


def _queue_google_books_metadata_recovery(*, requeued_quality: bool) -> dict[str, int | None]:
    job_ids = {
        "metadata": enqueue_job("metadata", dedupe=True),
        "read_metadata": enqueue_job("read_metadata", dedupe=True),
    }
    if requeued_quality:
        _enqueue_candidate_quality_followup()
    return job_ids


@app.put("/api/settings/google-books")
async def update_google_books_settings(payload: GoogleBooksKeyIn):
    current = private_settings().get("association_google_books_api_key", "")
    submitted_new_key = False
    validation: dict[str, str] | None = None
    if payload.clear:
        api_key = ""
    elif payload.api_key is None or not payload.api_key.strip():
        api_key = current
    else:
        api_key = payload.api_key.strip()
        submitted_new_key = True
        try:
            validation = await validate_google_books_api_key(api_key)
        except ValueError as exc:
            raise HTTPException(400, safe_error_message(exc)) from exc
    stored = seal(api_key) if api_key else ""
    requeued_quality = False
    with transaction() as con:
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES('association_google_books_api_key',?,1) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=1,"
            "updated_at=CURRENT_TIMESTAMP",
            (stored,),
        )
        if submitted_new_key:
            requeued_quality = _clear_google_books_metadata_lockouts(con)
    job_ids = (
        _queue_google_books_metadata_recovery(requeued_quality=requeued_quality)
        if submitted_new_key
        else {}
    )
    return {
        "saved": True,
        "google_books_api_key_set": bool(api_key),
        **({"job_ids": job_ids} if job_ids else {}),
        **(
            {
                "validation_status": validation["status"],
                **(
                    {"validation_warning": validation["warning"]}
                    if validation.get("warning")
                    else {}
                ),
            }
            if validation
            else {}
        ),
    }


def _saved_llm_target_matches(
    current: dict,
    *,
    target_url: str,
    provider: str | None,
    mode: str | None = None,
) -> bool:
    """Return True only when target_url, provider, and mode match the saved target.

    Note: legacy rows storing a blank `llm_rerank_api_url` alongside a stored key
    have `saved_origin == ""`, so enabled saves require providing a fresh API key
    rather than implicitly binding the legacy key to `settings.llm_rerank_api_url`.
    Likewise, saving with reranking disabled and a blank URL passes `target_url=""`,
    which returns False and drops any stored key instead of leaving an unbound secret.
    """
    saved_url = (
        current["llm_rerank_api_url"].strip()
        if "llm_rerank_api_url" in current
        else settings.llm_rerank_api_url.strip()
    )
    saved_provider = current.get("llm_rerank_provider", "").strip().lower()
    saved_mode = current.get("llm_rerank_mode", "").strip().lower()
    req_provider = saved_provider if provider is None else provider.strip().lower()
    req_mode = saved_mode if mode is None else mode.strip().lower()
    saved_origin = llm_reranker.normalized_llm_origin(saved_url)
    target_origin = llm_reranker.normalized_llm_origin(target_url)
    return (
        bool(target_origin)
        and target_origin == saved_origin
        and llm_reranker.canonical_llm_url(target_url)
        == llm_reranker.canonical_llm_url(saved_url)
        and req_provider == saved_provider
        and req_mode == saved_mode
    )


@app.get("/api/settings/llm-rerank")
def get_llm_rerank_settings():
    return safe_llm_rerank_settings()


@app.put("/api/settings/llm-rerank")
def update_llm_rerank_settings(payload: LLMRerankSettingsIn):
    current = private_settings()
    current_effective = effective_llm_rerank_settings(private=current)
    stored_key = current.get("llm_rerank_api_key", "").strip()
    env_key = settings.llm_rerank_api_key.strip()
    provided_key = "" if payload.clear_api_key else (payload.api_key or "").strip()
    saved_mode = current.get("llm_rerank_mode", "").strip().lower()
    effective_mode = (
        saved_mode if payload.mode is None else payload.mode.strip().lower()
    )
    req_url = payload.api_url.strip()
    effective_url = req_url or settings.llm_rerank_api_url
    effective_model = payload.model.strip() or settings.llm_rerank_model
    same_saved_target = _saved_llm_target_matches(
        current,
        target_url=req_url if (not payload.enabled and not req_url) else effective_url,
        provider=payload.provider,
        mode=payload.mode,
    )

    if payload.clear_api_key:
        profile_api_key = ""
        effective_key = ""
    elif provided_key:
        profile_api_key = provided_key
        effective_key = provided_key
    elif same_saved_target:
        profile_api_key = stored_key
        _, effective_key = resolve_llm_rerank_credentials(
            effective_url, profile_api_key
        )
    else:
        profile_api_key = ""
        effective_key = ""

    try:
        payload.validate_values()
        if payload.enabled and effective_url:
            llm_reranker.validate_llm_api_url(effective_url, has_api_key=True)
    except ValueError as exc:
        raise HTTPException(
            400,
            llm_reranker.sanitize_llm_error(
                exc, api_key=effective_key or provided_key
            ),
        ) from exc

    if payload.enabled and not effective_key:
        if not same_saved_target and not payload.clear_api_key and stored_key:
            raise HTTPException(
                400,
                "Provide a new API key when changing the LLM API URL, provider, or mode",
            )
        if is_custom_llm_endpoint(effective_url):
            raise HTTPException(
                400, "A profile API key is required for custom LLM endpoint URLs"
            )
        if not same_saved_target and not payload.clear_api_key and env_key:
            raise HTTPException(
                400,
                "Provide a new API key when changing the LLM API URL, provider, or mode",
            )
        raise HTTPException(400, "An API key is required when LLM reranking is enabled")

    llm_changed = (
        not current_effective["enabled"]
        or llm_reranker.canonical_llm_url(effective_url)
        != llm_reranker.canonical_llm_url(current_effective["api_url"])
        or effective_model != current_effective["model"]
        or effective_key != current_effective["api_key"]
        or payload.provider.strip().lower()
        != current_effective.get("provider", "").strip().lower()
        or effective_mode != current_effective.get("mode", "").strip().lower()
    )
    checked = False
    if payload.enabled and llm_changed:
        try:
            llm_reranker.probe_llm_reranker(
                api_url=effective_url,
                api_key=effective_key,
                model=effective_model,
            )
            checked = True
        except ValueError as exc:
            raise HTTPException(
                400, llm_reranker.sanitize_llm_error(exc, api_key=effective_key)
            ) from exc
        except Exception as exc:
            err = llm_reranker.sanitize_llm_error(exc, api_key=effective_key)
            raise HTTPException(502, f"LLM connection check failed: {err}") from exc

    values = {
        "llm_rerank_enabled": "1" if payload.enabled else "0",
        "llm_rerank_mode": effective_mode,
        "llm_rerank_provider": payload.provider.strip(),
        "llm_rerank_api_url": req_url,
        "llm_rerank_model": payload.model.strip(),
        "llm_rerank_api_key": profile_api_key,
    }
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status_updates: dict[str, str] = {}
    if checked:
        status_updates = {
            "llm_rerank_last_status": "succeeded",
            "llm_rerank_last_attempt_at": now_iso,
            "llm_rerank_last_success_at": now_iso,
            "llm_rerank_last_error": "",
        }
    elif not payload.enabled:
        status_updates = {
            "llm_rerank_last_status": "",
            "llm_rerank_last_error": "",
        }

    with transaction() as con:
        for key, value in values.items():
            secret = key == "llm_rerank_api_key"
            stored = seal(value) if secret and value else value
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=excluded.secret,updated_at=CURRENT_TIMESTAMP",
                (key, stored, 1 if secret else 0),
            )
        for key, value in status_updates.items():
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,0) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=0,updated_at=CURRENT_TIMESTAMP",
                (key, value),
            )
    if llm_changed or not payload.enabled:
        llm_reranker.clear_llm_rerank_state()
    else:
        clear_llm_rerank_cache(current_profile_id() or "legacy")
    return {
        "saved": True,
        "checked": checked,
        "llm_rerank": safe_llm_rerank_settings(),
    }


@app.post("/api/settings/llm-rerank/test")
def test_llm_rerank_settings(payload: LLMRerankTestIn = LLMRerankTestIn()):
    current = private_settings()
    current_effective = effective_llm_rerank_settings(private=current)
    saved_url = (
        current["llm_rerank_api_url"].strip()
        if "llm_rerank_api_url" in current
        else settings.llm_rerank_api_url.strip()
    )
    api_url = payload.api_url.strip() or saved_url
    model = payload.model.strip() or current_effective["model"]
    stored_key = current.get("llm_rerank_api_key", "").strip()
    env_key = settings.llm_rerank_api_key.strip()
    provided_key = "" if payload.clear_api_key else (payload.api_key or "").strip()
    same_saved_target = _saved_llm_target_matches(
        current,
        target_url=api_url,
        provider=payload.provider,
        mode=payload.mode,
    )

    if payload.clear_api_key:
        api_key = ""
    elif provided_key:
        api_key = provided_key
    elif same_saved_target:
        _, api_key = resolve_llm_rerank_credentials(api_url, stored_key)
    else:
        api_key = ""

    try:
        if not api_url or not model:
            raise ValueError("Testing LLM reranking requires both an API URL and a model")
        normalized_url = llm_reranker.validate_llm_api_url(api_url, has_api_key=True)
        if not api_key:
            if not same_saved_target and not payload.clear_api_key and (stored_key or env_key):
                raise ValueError(
                    "Provide an API key when testing a different LLM API URL, provider, or mode"
                )
            raise ValueError("An API key is required to test the LLM reranker")
    except ValueError as exc:
        raise HTTPException(
            400, llm_reranker.sanitize_llm_error(exc, api_key=api_key or provided_key)
        ) from exc

    matches_saved = (
        same_saved_target
        and llm_reranker.canonical_llm_url(normalized_url)
        == llm_reranker.canonical_llm_url(current_effective["api_url"])
        and model == current_effective["model"]
        and api_key == current_effective["api_key"]
    )
    profile_key = llm_reranker.current_llm_profile_key()
    llm_mode = (
        current_effective.get("mode", "")
        if payload.mode is None
        else payload.mode.strip().lower()
    )
    try:
        result = llm_reranker.probe_llm_reranker(
            api_url=normalized_url,
            api_key=api_key,
            model=model,
        )
    except Exception as exc:
        err = llm_reranker.sanitize_llm_error(exc, api_key=api_key)
        if matches_saved or not current_effective["enabled"]:
            _persist_llm_rerank_status(status="failed", error=err)
        if matches_saved and current_effective["enabled"]:
            llm_reranker.trip_circuit_breaker(
                profile_key,
                api_url=normalized_url,
                api_key=api_key,
                model=model,
                error=err,
                mode=llm_mode,
            )
        raise HTTPException(502, f"LLM connection test failed: {err}") from exc

    if matches_saved or not current_effective["enabled"]:
        _persist_llm_rerank_status(status="succeeded", error="")
    if matches_saved:
        llm_reranker.clear_circuit_breaker(profile_key)
    return {
        **result,
        "llm_rerank": safe_llm_rerank_settings(),
    }


_MODELS_DEV_CACHE: dict[str, object] = {"expires_at": 0.0, "providers": []}
_KNOWN_LLM_CHAT_URLS = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "mistral": "https://api.mistral.ai/v1/chat/completions",
    "xai": "https://api.x.ai/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "togetherai": "https://api.together.xyz/v1/chat/completions",
    "fireworks-ai": "https://api.fireworks.ai/inference/v1/chat/completions",
}


def _normalize_models_dev_catalog(raw: dict) -> list[dict]:
    providers: list[dict] = []
    for provider_id, entry in (raw or {}).items():
        if not isinstance(entry, dict):
            continue
        raw_models = entry.get("models")
        if not isinstance(raw_models, dict) or not raw_models:
            continue
        models = []
        for model_id, meta in raw_models.items():
            if not isinstance(meta, dict):
                continue
            resolved_id = str(meta.get("id") or model_id).strip()
            if not resolved_id:
                continue
            models.append(
                {
                    "id": resolved_id,
                    "name": str(meta.get("name") or resolved_id).strip(),
                }
            )
        if not models:
            continue
        models.sort(key=lambda item: item["name"].casefold())
        raw_api = str(entry.get("api") or "").strip().rstrip("/")
        if raw_api and not raw_api.endswith("/chat/completions"):
            if raw_api.endswith(("/v1", "/openai")):
                raw_api = f"{raw_api}/chat/completions"
        api_url = _KNOWN_LLM_CHAT_URLS.get(provider_id) or raw_api
        providers.append(
            {
                "id": str(provider_id),
                "name": str(entry.get("name") or provider_id).strip(),
                "api": api_url,
                "npm": str(entry.get("npm") or "").strip(),
                "models": models,
            }
        )
    providers.sort(key=lambda item: item["name"].casefold())
    return providers


@app.get("/api/llm/models")
async def llm_models_catalog():
    now = time.monotonic()
    cached = _MODELS_DEV_CACHE.get("providers") or []
    if cached and now < float(_MODELS_DEV_CACHE.get("expires_at") or 0.0):
        return {"providers": cached, "cached": True}
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get("https://models.dev/api.json")
            resp.raise_for_status()
            providers = _normalize_models_dev_catalog(resp.json())
        _MODELS_DEV_CACHE["providers"] = providers
        _MODELS_DEV_CACHE["expires_at"] = now + 3600.0
        return {"providers": providers, "cached": False}
    except Exception as exc:
        if cached:
            return {"providers": cached, "cached": True, "stale": True}
        raise HTTPException(502, f"Unable to fetch models.dev catalog: {safe_error_message(exc)}") from exc


@app.put("/api/sources/{source_id}")
def update_source(source_id: int, payload: SourceUpdateIn):
    next_url: str | None = None
    if payload.url is not None:
        next_url = str(payload.url)
        _reject_inline_nyt_key(next_url)
        try:
            validate_public_url(next_url)
        except ValueError as exc:
            raise HTTPException(400, safe_error_message(exc)) from exc
    updates = []
    values = []
    with transaction() as con:
        source = con.execute(
            "SELECT id,url,enabled,kind,is_default,lifecycle,filters,last_status,last_scanned_at,last_attempted_at "
            "FROM sources WHERE id=?",
            (source_id,),
        ).fetchone()
        if not source:
            raise HTTPException(404, "Source not found")
        if next_url is not None:
            if source["is_default"] or source["kind"] in {"builtin", "association"}:
                raise HTTPException(400, "This source URL cannot be changed")
            updates.append("url=?")
            values.append(next_url)
        if payload.filters is not None:
            if source["kind"] == "builtin":
                raise HTTPException(400, "The built-in source cannot be filtered")
            updates.append("filters=?")
            values.append(json.dumps(payload.filters.model_dump()))
        if payload.lifecycle is not None:
            if source["kind"] in {"builtin", "association"}:
                raise HTTPException(400, "This source lifecycle cannot be changed")
            updates.append("lifecycle=?")
            values.append(payload.lifecycle)
        effective_lifecycle = payload.lifecycle or source["lifecycle"]
        if effective_lifecycle == "one_time" and (
            next_url is not None or payload.filters is not None
        ):
            updates.append("last_scanned_at=NULL")
            updates.append("last_attempted_at=NULL")
        if updates:
            try:
                con.execute(
                    f"UPDATE sources SET {','.join(updates)} WHERE id=?",
                    (*values, source_id),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "Source already exists") from exc
    filters = (
        payload.filters.model_dump()
        if payload.filters is not None
        else normalize_source_filters(source["filters"])
    )
    should_queue = bool(
        updates
        and source["enabled"]
        and source["kind"] not in {"builtin", "association"}
        and not (
            effective_lifecycle == "one_time"
            and next_url is None
            and payload.filters is None
            and _one_time_source_completed(source)
        )
    )
    job_id = (
        enqueue_job(f"source:{source_id}", dedupe=True)
        if should_queue
        else None
    )
    return {
        "id": source_id,
        "url": next_url or source["url"],
        "filters": filters,
        "lifecycle": effective_lifecycle,
        "job_id": job_id,
    }


def _save_digest_settings(updates: dict):
    current = private_settings()
    existing = digest_config(current)
    merged = {**existing, **updates}
    try:
        validate_digest_config(merged, previous=existing)
    except ValueError as exc:
        raise HTTPException(400, safe_error_message(exc)) from exc
    next_app_url = str(merged["app_url"] or "").strip().rstrip("/")
    prev_app_url = str(existing["app_url"] or "").strip().rstrip("/")
    default_public_url = str(settings.public_url or "").strip().rstrip("/")
    if "digest_app_url_custom" in current:
        prev_custom = str(current["digest_app_url_custom"]) == "1"
    else:
        prev_custom = bool(prev_app_url and prev_app_url not in DEFAULT_DIGEST_APP_URLS)
    if "app_url" in updates and next_app_url != prev_app_url:
        app_url_custom = next_app_url != default_public_url
    else:
        app_url_custom = prev_custom
    storage = {
        "digest_enabled": "1" if merged["enabled"] else "0",
        "digest_channels": ",".join(merged["channels"]),
        "digest_day": str(merged["day"]),
        "digest_time": merged["time"],
        "digest_timezone": merged["timezone"],
        "digest_minimum_score": str(merged["minimum_score"]),
        "digest_maximum_books": str(merged["maximum_books"]),
        "digest_only_new": "1" if merged["only_new"] else "0",
        "digest_app_url": next_app_url,
        "digest_app_url_custom": "1" if app_url_custom else "0",
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
    """Return the candidate IDs and unread book cards for a digest period.

    The public review link carries only an ISO week key. Candidate metadata
    remains behind the normal overview/auth boundary; this authenticated
    endpoint hydrates unread period picks on the server without copying book
    data into a notification URL.
    """

    if len(period) != 8 or period[4] != "-" or period[5] != "W" or not period[0:4].isdigit() or not period[6:8].isdigit():
        raise HTTPException(400, "Invalid digest period")
    ids: list[int] = []
    with connect() as connection:
        deliveries = [
            dict(item)
            for item in connection.execute(
                "SELECT candidate_ids FROM notification_deliveries WHERE period_key=? ORDER BY created_at DESC",
                (period,),
            ).fetchall()
        ]
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
        if not deliveries:
            # ``found`` lets the web app distinguish a valid digest with zero
            # books from a stale link whose delivery history has been pruned or
            # never existed.
            return {"period": period, "ids": [], "found": False}
        items: list[dict[str, Any]] = []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            candidate_rows = {
                int(item["id"]): dict(item)
                for item in connection.execute(
                    "SELECT c.*,s.name source_name,q.metadata_confidence,"
                    "q.work_id quality_work_id,"
                    "q.provider quality_provider,q.provider_id AS quality_provider_id,"
                    "q.isbn13 quality_isbn13,"
                    "q.isbn10 quality_isbn10 "
                    "FROM candidates c "
                    "LEFT JOIN sources s ON s.id=c.source_id "
                    "LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
                    f"WHERE c.id IN ({placeholders}) "
                    "AND c.status='recommended' "
                    "AND q.quality_status='accepted' "
                    "AND COALESCE(s.enabled,1)=1",
                    ids,
                ).fetchall()
            }
            read_identity_index, shortlisted_keys = _recommendation_identity_context(
                connection
            )
            seen_keys = set()
            for candidate_id in ids:
                candidate = candidate_rows.get(candidate_id)
                if not candidate:
                    continue
                keys = book_row_identity_match_keys(candidate)
                if read_identity_index.matches(candidate) or keys & shortlisted_keys or keys & seen_keys:
                    continue
                seen_keys.update(keys)
                items.append(_format_recommendation_item(candidate))
    return {"period": period, "ids": ids, "found": True, "items": items}


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
    return {"job_id": enqueue_job(f"digest:test:{channel}", dedupe=True)}


@app.post("/api/digest/run")
def run_digest():
    return {"job_id": enqueue_job("digest:manual", dedupe=True)}


@app.post("/api/digest/deliveries/{delivery_id}/retry")
def retry_digest_delivery(delivery_id: str):
    if not row("SELECT id FROM notification_deliveries WHERE id=?", (delivery_id,)):
        raise HTTPException(404, "Delivery not found")
    return {"job_id": enqueue_job(f"notification_retry:{delivery_id}", dedupe=True)}

@app.post("/api/sync")
async def sync():
    return {"job_id":enqueue_job("sync", dedupe=True)}

@app.post("/api/score")
async def score():
    return {"job_id":enqueue_job("score", dedupe=True)}

@app.post("/api/candidates/audit")
async def candidate_audit():
    """Queue a full catalog identity and quality audit."""

    return {"job_id": enqueue_job("candidate_quality", dedupe=True)}

@app.get("/api/candidates/quality")
def candidate_quality():
    return quality_summary()

@app.post("/api/embeddings/rebuild")
async def rebuild_embeddings():
    return {"job_id":enqueue_job("rebuild_embeddings", dedupe=True)}

@app.get("/api/jobs/{job_id}")
def job(job_id:str):
    found=row("SELECT * FROM jobs WHERE id=?",(job_id,))
    if not found: raise HTTPException(404,"Job not found")
    return found


@app.get("/api/associations/settings")
def association_settings():
    return safe_association_settings()


@app.put("/api/associations/settings")
async def update_association_settings(payload: AssociationSettingsIn):
    current = private_settings()
    submitted_new_google_key = bool(
        payload.google_books_api_key is not None
        and payload.google_books_api_key.strip()
    )
    values = {
        "association_openlibrary_enabled": "1" if payload.openlibrary_enabled else "0",
        "association_openlibrary_contact": payload.openlibrary_contact.strip(),
        "association_librarything_enabled": "1" if payload.librarything_enabled else "0",
        "association_librarything_api_key": (
            current.get("association_librarything_api_key", "")
            if payload.librarything_api_key is None
            else payload.librarything_api_key.strip()
        ),
        "association_google_books_api_key": (
            current.get("association_google_books_api_key", "")
            if payload.google_books_api_key is None
            else payload.google_books_api_key.strip()
        ),
    }
    if values["association_librarything_enabled"] == "1" and not values["association_librarything_api_key"]:
        raise HTTPException(400, "LibraryThing API key is required when the provider is enabled")
    google_validation: dict[str, str] | None = None
    if submitted_new_google_key:
        try:
            google_validation = await validate_google_books_api_key(
                values["association_google_books_api_key"]
            )
        except ValueError as exc:
            raise HTTPException(400, safe_error_message(exc)) from exc
    requeued_quality = False
    with transaction() as con:
        for key, value in values.items():
            secret = key.endswith("api_key")
            stored = seal(value) if secret and value else value
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=excluded.secret,updated_at=CURRENT_TIMESTAMP",
                (key, stored, 1 if secret else 0),
            )
        if submitted_new_google_key:
            requeued_quality = _clear_google_books_metadata_lockouts(con)
    if submitted_new_google_key:
        _queue_google_books_metadata_recovery(requeued_quality=requeued_quality)
    return {
        "saved": True,
        "associations": safe_association_settings(),
        **(
            {"google_books_validation_warning": google_validation["warning"]}
            if google_validation and google_validation.get("warning")
            else {}
        ),
    }


def _association_provider_name(value: str) -> str:
    provider = value.strip().casefold()
    aliases = {
        "openlibrary": "openlibrary",
        "openlibrary_lists": "openlibrary",
        "librarything": "librarything",
        "librarything_multirecommendations": "librarything",
        "google": "google_books",
        "google_books": "google_books",
        "google_books_associated": "google_books",
    }
    normalized = aliases.get(provider)
    if not normalized:
        raise HTTPException(404, "Unknown association provider")
    return normalized


@app.post("/api/associations/{provider}/run")
def run_associations(provider: str):
    normalized = _association_provider_name(provider)
    configured = safe_association_settings()
    if normalized == "librarything":
        if not configured["librarything_enabled"]:
            raise HTTPException(409, "Enable LibraryThing associations first")
        if not configured["librarything_api_key_set"]:
            raise HTTPException(409, "Configure a LibraryThing API key first")
    if normalized == "openlibrary" and not configured["openlibrary_enabled"]:
        raise HTTPException(409, "Enable Open Library associations first")
    return {"job_id": enqueue_job(f"association:{normalized}")}


@app.post("/api/associations/google_books/preview")
async def preview_google_book_associations():
    """Return ephemeral Google associations without writing provider content."""

    config = private_settings()
    provider = GoogleBooksAssociatedProvider(config.get("association_google_books_api_key", ""))
    try:
        associations = await provider.collect(rows("SELECT * FROM reads ORDER BY id"))
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"Google Books preview failed: {exc}") from exc
    return {
        "provider": provider.provider,
        "count": len(associations),
        "results": [
            {
                "title": item.title,
                "author": item.author,
                "rank": item.rank,
                "source_url": item.source_url,
                "metadata": dict(item.metadata),
            }
            for item in associations
        ],
        "persistent": False,
    }


@app.get("/api/associations/runs")
def association_runs(limit: int = Query(default=20, ge=1, le=100)):
    return {
        "runs": rows(
            "SELECT id,provider,status,seed_count,edge_count,error,started_at,finished_at "
            "FROM association_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ),
        "librarything_requests_today": rows(
            "SELECT COUNT(*) AS count FROM association_requests "
            "WHERE provider=? AND requested_at>=date('now')",
            ("librarything_multirecommendations",),
        )[0]["count"],
    }

def private_settings(connection=None):
    items = connection.execute("SELECT key,value,secret FROM settings").fetchall() if connection is not None else rows("SELECT key,value,secret FROM settings")
    return {
        item["key"]: unseal(item["value"]) if item["secret"] else item["value"]
        for item in items
    }


def safe_association_settings(connection=None, private=None):
    if private is None:
        private = private_settings(connection)
    return {
        "openlibrary_enabled": private.get("association_openlibrary_enabled", "0") == "1",
        "openlibrary_contact": private.get(
            "association_openlibrary_contact", settings.openlibrary_contact
        ),
        "librarything_enabled": private.get("association_librarything_enabled", "0") == "1",
        "librarything_api_key_set": bool(private.get("association_librarything_api_key")),
        "google_books_api_key_set": bool(private.get("association_google_books_api_key")),
    }


def effective_llm_rerank_settings(connection=None, private=None) -> dict:
    if private is None:
        private = private_settings(connection)
    enabled = (
        private["llm_rerank_enabled"] == "1"
        if "llm_rerank_enabled" in private
        else bool(settings.llm_rerank_enabled)
    )
    api_url, api_key = resolve_llm_rerank_credentials(
        private.get("llm_rerank_api_url"),
        private.get("llm_rerank_api_key"),
    )
    return {
        "enabled": enabled,
        "provider": private.get("llm_rerank_provider", ""),
        "mode": private.get("llm_rerank_mode", ""),
        "api_url": api_url,
        "api_key": api_key,
        "model": private.get("llm_rerank_model") or settings.llm_rerank_model,
    }


def safe_llm_rerank_settings(connection=None, private=None) -> dict:
    if private is None:
        private = private_settings(connection)
    effective = effective_llm_rerank_settings(connection, private)
    circuit = llm_reranker.get_circuit_breaker_state(
        llm_reranker.current_llm_profile_key(),
        api_url=effective["api_url"],
        api_key=effective["api_key"],
        model=effective["model"],
        mode=effective.get("mode", ""),
    )
    return {
        "enabled": effective["enabled"],
        "mode": effective.get("mode", ""),
        "provider": effective["provider"],
        "api_url": effective["api_url"],
        "model": effective["model"],
        "api_key_set": bool(effective["api_key"]),
        "last_status": private.get("llm_rerank_last_status", ""),
        "last_error": private.get("llm_rerank_last_error", ""),
        "last_attempt_at": private.get("llm_rerank_last_attempt_at", ""),
        "last_success_at": private.get("llm_rerank_last_success_at", ""),
        "cooldown_until": circuit.get("cooldown_until", "") if circuit else "",
    }


def safe_settings(*, include_api_tokens: bool = True, connection=None, private=None):
    if private is None:
        private = private_settings(connection)
    try:
        media_type = normalize_media_type(private.get("librarr_media_type"))
    except ValueError:
        media_type = "audiobook"
    digest = safe_digest_settings(private, connection)
    result = {
        "embedding_backend": private.get("embedding_backend", settings.embedding_backend),
        "embedding_model": private.get("embedding_model", settings.embedding_model),
        "embedding_url": private.get("embedding_url", settings.embedding_url),
        "embedding_api_key_set": bool(private.get("embedding_api_key")),
        "librarr_url": private.get("librarr_url", ""),
        "librarr_api_key_set": bool(private.get("librarr_api_key")),
        "goodreads_rss_url": private.get("goodreads_rss_url", ""),
        "goodreads_rss_last_sync_at": private.get("goodreads_rss_last_sync_at", ""),
        "goodreads_rss_last_status": private.get("goodreads_rss_last_status", ""),
        "goodreads_rss_last_attempt_at": private.get("goodreads_rss_last_attempt_at", ""),
        "goodreads_rss_last_error": private.get("goodreads_rss_last_error", ""),
        "librarr_media_type": media_type,
        "nyt_api_key_set": bool(private.get("nyt_api_key")),
        "google_books_api_key_set": bool(private.get("association_google_books_api_key")),
        "source_sync_interval_hours": source_sync_interval_hours(private),
        "digest": digest,
        "associations": safe_association_settings(connection, private),
        "llm_rerank": safe_llm_rerank_settings(connection, private),
    }
    if include_api_tokens:
        result["api_tokens"] = api_token_list()
    return result


def _create_api_token(name: str):
    try:
        return identity_store.create_profile_api_token(current_profile_id() or "legacy", name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _revoke_api_token(token_id: int):
    try:
        identity_store.revoke_profile_api_token(current_profile_id() or "legacy", token_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"revoked": True, "id": token_id}


@app.get("/api/settings/api-tokens")
@app.get("/api/tokens")
def list_api_tokens():
    return {"tokens": api_token_list()}


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
@fenced_async
async def update_settings(payload: EngineSettings):
    embedding_fields = (
        "embedding_backend", "embedding_model", "embedding_url", "embedding_api_key"
    )
    librarr_fields = (
        "librarr_url", "librarr_api_key", "librarr_media_type"
    )
    updates = payload.model_dump(exclude_unset=True)
    for key in ("embedding_api_key", "librarr_api_key"):
        if key in updates:
            updates[key] = updates[key].strip()

    def _merge_settings(stored: dict) -> tuple[dict, dict]:
        base = {
            "embedding_backend": stored.get("embedding_backend", settings.embedding_backend),
            "embedding_model": stored.get("embedding_model", settings.embedding_model),
            "embedding_url": stored.get("embedding_url", settings.embedding_url),
            "embedding_api_key": stored.get("embedding_api_key", settings.embedding_api_key),
            "librarr_url": stored.get("librarr_url", ""),
            "librarr_api_key": stored.get("librarr_api_key", ""),
            "librarr_media_type": stored.get("librarr_media_type", "audiobook"),
        }
        merged = {**base, **updates}
        for key in ("embedding_api_key", "librarr_api_key"):
            if not merged[key]:
                merged[key] = stored.get(key, "")
        return base, merged

    current = private_settings()
    current_values, values = _merge_settings(current)
    has_embedding_fields = any(key in payload.model_fields_set for key in embedding_fields)
    has_librarr_fields = any(key in payload.model_fields_set for key in librarr_fields)
    try:
        EngineSettings(**values).validate_values(
            validate_embedding=has_embedding_fields or not payload.model_fields_set,
            validate_librarr=has_librarr_fields or not payload.model_fields_set,
            check_librarr_host="librarr_url" in payload.model_fields_set,
        )
    except ValueError as exc:
        raise HTTPException(400, safe_error_message(exc))
    if (
        "librarr_url" in payload.model_fields_set
        and values["librarr_url"].strip()
        and not values["librarr_api_key"].strip()
    ):
        raise HTTPException(400, "Enter a Librarr API key for the first connection.")
    embedding_changed = any(
        key in payload.model_fields_set and values[key] != current_values[key]
        for key in embedding_fields
    )
    if embedding_changed:
        test = await asyncio.to_thread(
            get_embedder,
            values["embedding_backend"],
            values["embedding_model"],
            values["embedding_url"],
            values["embedding_api_key"],
        )
        health = await test.health()
        if not health.get("ok"):
            raise HTTPException(
                400,
                f"Embedding provider check failed: {safe_error_message(health.get('error','unknown error'))}",
            )
        updates["embedding_model"] = test.model
        values["embedding_model"] = test.model
    else:
        health = {"ok": True, "checked": False, "model": values["embedding_model"]}
    with transaction() as con:
        _, final_values = _merge_settings(private_settings(con))
        if (
            "librarr_url" in payload.model_fields_set
            and final_values["librarr_url"].strip()
            and not final_values["librarr_api_key"].strip()
        ):
            raise HTTPException(400, "Enter a Librarr API key for the first connection.")
        for key, value in final_values.items():
            secret = key.endswith("api_key")
            stored = seal(value) if secret and value else value
            con.execute("INSERT INTO settings(key,value,secret) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=excluded.secret,updated_at=CURRENT_TIMESTAMP",(key,stored,1 if secret else 0))
    return {"saved":True,"embedding":health}


bearer_scheme = HTTPBearer(auto_error=False)


def require_api_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Authenticate the public API without accepting browser credentials.

    ``HTTPBearer`` is used as a dependency rather than parsing the header by
    hand so FastAPI advertises the authentication requirement in OpenAPI.
    ``X-API-Key`` remains supported for clients that use API-key conventions.
    """

    cached = getattr(request.state, "api_token", None)
    if cached:
        return cached
    candidate = credentials.credentials.strip() if credentials else ""
    api_key = x_api_key.strip() if x_api_key else ""
    if candidate and api_key and not hmac.compare_digest(candidate.encode(), api_key.encode()):
        raise HTTPException(
            status_code=401,
            detail="A valid API token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    candidate = candidate or api_key
    found = identity_store.authenticate_api_token(candidate)
    if not found:
        raise HTTPException(
            status_code=401,
            detail="A valid API token is required",
            headers={"WWW-Authenticate": "Bearer"},
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
            "undo": "POST /api/v1/recommendations/{id}/undo",
            "sources": "GET /api/v1/sources",
            "sync": "POST /api/v1/sync",
        },
    }


@api_v1.get("/health")
async def api_health():
    return await health()


@api_v1.get("/me")
def api_me(request: Request):
    account = request.state.account
    return {"profile_id": account["profile_id"], "credential": "api_token"}


@api_v1.get("/overview")
def api_overview():
    return overview_payload(include_api_tokens=False)


@api_v1.get("/settings")
def api_settings():
    return safe_settings(include_api_tokens=False)


@api_v1.get("/recommendations")
def api_recommendations(
    response: Response,
    status: Annotated[Literal["recommended", "saved", "imported", "all", "decisions", "rejected", "maybe_later"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    search: Annotated[str | None, Query(max_length=200)] = None,
    genre: Annotated[str | None, Query(max_length=100)] = None,
):
    return recommendations(response, status, limit, offset, search=search, genre=genre)


@api_v1.post("/recommendations/bulk-feedback")
def api_bulk_feedback(payload: BulkFeedbackIn):
    return bulk_feedback(payload)


@api_v1.post("/recommendations/bulk-undo")
def api_bulk_undo_feedback(payload: BulkUndoFeedbackIn):
    return bulk_undo_feedback(payload)


@api_v1.post("/recommendations/{candidate_id}/feedback")
def api_feedback(candidate_id: int, payload: FeedbackIn):
    return feedback(candidate_id, payload)


@api_v1.post("/recommendations/{candidate_id}/undo")
def api_undo_feedback(candidate_id: int, payload: UndoFeedbackIn):
    return undo_feedback(candidate_id, payload)


@api_v1.post("/telemetry/events")
def api_telemetry_events(payload: TelemetryBatchIn):
    return telemetry_events(payload)


@api_v1.post("/recommendations/{candidate_id}/import")
async def api_import_librarr(candidate_id: int, payload: LibrarrImportIn | None = Body(default=None)):
    return await import_librarr(candidate_id, payload)


@api_v1.get("/sources")
def api_sources():
    return [
        {**source, "filters": normalize_source_filters(source.get("filters"))}
        for source in rows("SELECT * FROM sources ORDER BY is_default DESC,name")
    ]


@api_v1.post("/sources/preview")
async def api_source_preview(payload: SourcePreviewIn):
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


@api_v1.put("/sources/{source_id}")
def api_update_source(source_id: int, payload: SourceUpdateIn):
    return update_source(source_id, payload)


@api_v1.post("/import/goodreads/rss")
async def api_goodreads_rss(payload: UrlIn):
    return await goodreads_rss(payload)


@api_v1.post("/sync")
async def api_sync():
    return await sync()


@api_v1.post("/score")
async def api_score():
    return await score()


@api_v1.get("/associations/settings")
def api_association_settings():
    return association_settings()


@api_v1.get("/associations/runs")
def api_association_runs(limit: int = Query(default=20, ge=1, le=100)):
    return association_runs(limit=limit)


@api_v1.post("/associations/{provider}/run")
def api_run_associations(provider: str):
    return run_associations(provider)


@api_v1.post("/associations/google_books/preview")
async def api_preview_google_book_associations():
    return await preview_google_book_associations()


@api_v1.get("/jobs/{job_id}")
def api_job(job_id: str):
    return job(job_id)


@api_v1.get("/librarr/search")
async def api_librarr_search(
    q: str = Query(min_length=2, max_length=200),
    media_type: str = Query(default="audiobook"),
):
    return await search_librarr(q=q, media_type=media_type)


@api_v1.get("/librarr/search/stream")
async def api_librarr_search_stream(
    q: str = Query(min_length=2, max_length=200),
    media_type: str = Query(default="audiobook"),
):
    return await stream_librarr_search(q=q, media_type=media_type)


@api_v1.post("/librarr/download")
async def api_librarr_download(payload: LibrarrDownloadIn):
    return await download_librarr(payload)


app.include_router(api_v1)
