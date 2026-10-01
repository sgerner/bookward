import hashlib
import json
import asyncio
import logging
import base64
import hmac
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from contextlib import asynccontextmanager
import httpx
from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, HttpUrl, field_validator
from .config import settings
from .database import connect, initialize, row, rows, transaction
from .backups import (
    BackupError,
    backup_scheduler_loop,
    backup_status,
    create_backup,
    recover_interrupted_restore,
    restore_backup,
)
from .embeddings import get_embedder
from .covers import canonical_book_source_url, fallback_cover_url
from .ingestion import (
    import_goodreads_csv,
    import_goodreads_rss,
    normalize_source_filters,
    preview_source,
    refresh_read_work_identities,
    refresh_missing_candidate_metadata,
    scan_source,
)
from .security import safe_error_message, validate_public_url, validate_service_url
from .jobs import enqueue_job, worker_loop
from .scoring import rebuild_all_embeddings, score_all
from .secrets import seal, unseal
from .librarr import (
    StreamingUnsupportedError,
    download as librarr_download,
    normalize_media_type,
    open_search_stream as librarr_open_search_stream,
    search as librarr_search,
)
from .exploration import epsilon_tail_explore
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
from .quality import QUALITY_VERSION, audit_candidates, quality_summary
from .identity import (
    book_identity_match_index,
    book_openlibrary_work_id,
    book_row_identity_match_keys,
)
from . import profiles as identity_store
from .tenancy import current_profile_id, profile_scope
import jwt

SOURCE_SYNC_MIN_HOURS = 0
SOURCE_SYNC_MAX_HOURS = 720
SOURCE_SYNC_ERROR_RETRY_SECONDS = 300
SCHEDULER_INITIAL_DELAY_SECONDS = 5
SCHEDULER_POLL_SECONDS = 60
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
        "WHERE enabled=1 AND kind NOT IN ('builtin','association') "
        "AND lifecycle='permanent'"
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


def _needs_candidate_quality_audit():
    """Backfill confidence only for candidate rows that remain eligible."""

    return bool(
        row(
            """SELECT 1 FROM candidate_quality q
            JOIN candidates c ON c.id=q.candidate_id
            WHERE c.status!='rejected'
              AND (q.audit_version='legacy-pending-audit-v1'
                   OR q.quality_status='pending')
            LIMIT 1"""
        )
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
        for profile_id in identity_store.profile_ids():
            try:
                with profile_scope(profile_id):
                    if source_sync_is_due() and not active_job("sync"):
                        enqueue_job("sync", dedupe=True)
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
                with profile_scope(profile_id):
                    if digest_is_due(private_settings()) and not active_job("digest"):
                        enqueue_job("digest", dedupe=True)
            except Exception:
                LOGGER.exception("Digest scheduler failed for profile %s", profile_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=SCHEDULER_POLL_SECONDS)
        except TimeoutError:
            pass

async def handle_job(kind: str):
    config = private_settings()
    if kind == "read_work_identities":
        result = await refresh_read_work_identities()
        if result["remaining"]:
            enqueue_job("read_work_identities", dedupe=False)
        return result
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
        quality = await audit_candidates(only_pending=True)
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
        return {"metadata": await refresh_missing_candidate_metadata()}
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
        quality = await audit_candidates(only_pending=True)
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
        if source["lifecycle"] == "one_time" and source["last_scanned_at"]:
            return {"skipped": "already_scanned", "source_id": source_id}
        try:
            collected = await scan_source(source)
        except Exception as exc:
            message = safe_error_message(exc)
            with transaction() as con:
                con.execute(
                    "UPDATE sources SET last_status=?, "
                    "last_scanned_at=CASE WHEN lifecycle='one_time' THEN last_scanned_at ELSE CURRENT_TIMESTAMP END "
                    "WHERE id=?",
                    (f"error:{message}", source_id),
                )
            raise
        metadata = await refresh_missing_candidate_metadata()
        quality = await audit_candidates(only_pending=True)
        enqueue_job("read_work_identities", dedupe=True)
        scored = await score_all(config.get("embedding_backend"),config.get("embedding_model"),config.get("embedding_url"),config.get("embedding_api_key"))
        digest_job_id = queue_digest_if_due()
        return {"collected":collected,"metadata":metadata,"quality":quality,"scored":scored,"source_id":source_id,"digest_job_id":digest_job_id}
    if kind == "sync":
        total = 0
        errors = []
        sources = rows(
            "SELECT * FROM sources WHERE enabled=1 "
            "AND kind NOT IN ('builtin','association') "
            "AND (lifecycle='permanent' OR (lifecycle='one_time' AND last_scanned_at IS NULL))"
        )
        for source in sources:
            try:
                total += await scan_source(source)
            except Exception as exc:
                message = safe_error_message(exc)
                with transaction() as con:
                    con.execute(
                        "UPDATE sources SET last_status=?, "
                        "last_scanned_at=CASE WHEN lifecycle='one_time' THEN last_scanned_at ELSE CURRENT_TIMESTAMP END "
                        "WHERE id=?",
                        (f"error:{message}", source["id"]),
                    )
                errors.append({"id": source["id"], "name": source["name"], "error": message})
        metadata = await refresh_missing_candidate_metadata()
        quality = await audit_candidates(only_pending=True)
        enqueue_job("read_work_identities", dedupe=True)
        scored = await score_all(config.get("embedding_backend"),config.get("embedding_model"),config.get("embedding_url"),config.get("embedding_api_key"))
        digest_job_id = queue_digest_if_due()
        return {"collected":total,"metadata":metadata,"quality":quality,"scored":scored,"errors":errors,"digest_job_id":digest_job_id}
    raise ValueError(f"Unsupported job kind: {kind}")

@asynccontextmanager
async def lifespan(app):
    identity_store.initialize_auth()
    with profile_scope("legacy"):
        recover_interrupted_restore()
        initialize()
        legacy_tokens = rows("SELECT id,name,token_prefix,token_hash,created_at,last_used_at,revoked_at FROM api_tokens")
    identity_store.migrate_legacy_api_tokens(legacy_tokens)
    for profile_id in identity_store.profile_ids():
        if profile_id != "legacy":
            with profile_scope(profile_id):
                recover_interrupted_restore()
            _ensure_profile_database(profile_id)
    stop = asyncio.Event(); worker = asyncio.create_task(worker_loop(handle_job, stop)); scheduler = asyncio.create_task(source_scheduler_loop(stop)); digest_scheduler = asyncio.create_task(digest_scheduler_loop(stop)); backup_scheduler = asyncio.create_task(backup_scheduler_loop(stop))
    # Backfill older rows in the worker so catalog lookups never delay API
    # startup. The dedupe flag keeps restarts from creating duplicate work.
    for profile_id in identity_store.profile_ids():
        with profile_scope(profile_id):
            enqueue_job("metadata", dedupe=True)
            needs_full_quality_audit = _needs_candidate_quality_audit()
            if needs_full_quality_audit:
                enqueue_job("candidate_quality", dedupe=True)
            elif row(
                "SELECT 1 FROM candidate_quality WHERE quality_status='quarantine' "
                "AND audit_version!=? AND candidate_id IN ("
                "SELECT id FROM candidates WHERE isbn13!='' OR isbn10!='') LIMIT 1",
                (QUALITY_VERSION,),
            ):
                enqueue_job("candidate_quality_recovery", dedupe=True)
            enqueue_job("read_work_identities", dedupe=True)
    yield
    stop.set(); await scheduler; await digest_scheduler; await backup_scheduler; await worker

app = FastAPI(title="Bookward Engine", version="0.1.0", lifespan=lifespan)
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
        token = identity_store.authenticate_api_token(candidate)
        if not token:
            return JSONResponse(
                {"detail": "A valid API token is required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )
        request.state.api_token = token
        request.state.account = {"profile_id": token["profile_id"], "role": "user", "id": "api-token"}
        with profile_scope(token["profile_id"]):
            response = await call_next(request)
        _mark_private_no_store(response)
        return response

    public_auth = path in {"/auth/config", "/auth/login", "/auth/setup"}
    oidc_login_start = path == "/auth/oidc/start" and request.query_params.get("intent", "login") == "login"
    oidc_callback = path == "/auth/oidc/callback"
    if oidc_callback and request.headers.get("x-bookward-session"):
        request.state.account = identity_store.resolve_session(request.headers.get("x-bookward-session"))
    if public_auth or oidc_login_start or oidc_callback:
        response = await call_next(request)
        response.headers["cache-control"] = "no-store"
        return response

    account = identity_store.resolve_session(request.headers.get("x-bookward-session"))
    if not account:
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    request.state.account = account
    with profile_scope(account["profile_id"]):
        response = await call_next(request)
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
        _ensure_profile_database(account["profile_id"])
    if not account:
        raise HTTPException(403, "This SSO identity is not linked to a Bookward account")
    raw, account = identity_store.create_session(account["id"])
    return {"session_token": raw, "max_age": identity_store.SESSION_IDLE_HOURS * 3600, "account": account}


@app.get("/api/backups")
def get_backup_status():
    return backup_status()


@app.post("/api/backups/create")
def make_backup():
    try:
        created = create_backup()
    except BackupError as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"backup": created, "status": backup_status()}


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
    filters: SourceFilters = Field(default_factory=SourceFilters)


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


class ReadingProgressIn(BaseModel):
    status: Literal["saved", "reading", "finished"] | None = None
    up_next: bool | None = None
    rating: int | None = Field(default=None, ge=1, le=5)


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


class NYTApiKeyIn(BaseModel):
    api_key: str | None = Field(default=None, max_length=500)


class AssociationSettingsIn(BaseModel):
    openlibrary_enabled: bool = False
    openlibrary_contact: str = Field(default="", max_length=200)
    librarything_enabled: bool = False
    librarything_api_key: str | None = Field(default=None, max_length=500)
    google_books_api_key: str | None = Field(default=None, max_length=500)


def api_token_list():
    """Return token metadata without ever returning a token value."""
    return identity_store.list_profile_api_tokens(current_profile_id() or "legacy")


def tracked_recommendations(
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    connection=None,
    recommended_limit: int | None = None,
):
    discovery_metadata = empty_discovery_slate_diagnostics("not_discovery_request")
    learning_metadata = {
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
    if can_personalize:
        try:
            # Score the full eligible recommendation pool before pagination so
            # a learned preference can promote a strong match from below the
            # unpersonalized page boundary.
            ranked = recommendation_list(
                connection, status=status, limit=None, offset=0
            )
            interaction_events = load_interaction_events(connection)
            cached_vectors = load_cached_candidate_vectors(
                [*ranked, *interaction_events], connection
            )
            interaction_vectors = {
                int(event["candidate_id"]): cached_vectors[int(event["candidate_id"])]
                for event in interaction_events
                if int(event["candidate_id"]) in cached_vectors
            }
            ranked, diagnostics = personalize_recommendations(
                ranked,
                interaction_events,
                interaction_vectors=interaction_vectors,
                candidate_vectors=cached_vectors,
            )
            learning_metadata.update(diagnostics)
            recommended = [
                item for item in ranked if item.get("status") == "recommended"
            ]
            if recommended:
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
            if recommended_limit is not None and status is None and offset == 0:
                recommended = [item for item in ranked if item.get("status") == "recommended"]
                remaining = [
                    item for item in ranked
                    if item.get("status") in {"saved", "imported", "new"}
                ]
                recommendations = recommended[:max(0, int(recommended_limit))] + remaining
            else:
                start = max(0, int(offset))
                end = None if limit is None else start + max(0, int(limit))
                recommendations = ranked[start:end]
        except Exception:
            # Personalization must never take the recommendation service down.
            # On malformed legacy evidence, return the existing champion order.
            LOGGER.exception(
                "Interaction personalization failed; serving the base ranking"
            )
            recommendations = recommendation_list(
                connection,
                status=status,
                limit=limit,
                offset=offset,
                recommended_limit=recommended_limit,
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
            recommended_limit=recommended_limit,
        )
    # Keep exploration outside the scorer and behind an environment flag.  A
    # disabled deployment receives the same deterministic order and scores as
    # before, while enabled traffic records exact tail propensities.
    if settings.exploration_enabled:
        recommendations = epsilon_tail_explore(
            recommendations,
            epsilon=settings.exploration_epsilon,
            stable_top_k=settings.exploration_stable_top_k,
        )
    run_id = create_recommendation_run(
        recommendations,
        status=status,
        limit=limit,
        ranking_metadata=learning_metadata,
        discovery_slate_metadata=discovery_metadata,
    )
    return recommendations, run_id


def overview_payload(*, include_api_tokens: bool = True, recommendation_limit: int | None = None):
    def source_payload(source):
        item = dict(source)
        item["filters"] = normalize_source_filters(item.get("filters"))
        return item

    with connect() as connection:
        counts = {
            name: connection.execute(f"SELECT COUNT(*) count FROM {name}").fetchone()["count"]
            for name in ("reads", "candidates", "sources")
        }
        recommendations, run_id = tracked_recommendations(
            connection=connection,
            limit=recommendation_limit or 100,
            recommended_limit=recommendation_limit,
        )
        decisions = recommendation_list(
            connection, status="decisions", limit=None
        )
        return {
            "counts": counts,
            "recommendations": recommendations,
            "decisions": decisions,
            "recommendation_run_id": run_id,
            "sources": [source_payload(item) for item in connection.execute(
                "SELECT * FROM sources ORDER BY is_default DESC,name"
            ).fetchall()],
            "history": [dict(item) for item in connection.execute(
                "SELECT * FROM reads ORDER BY COALESCE(read_at,created_at) DESC LIMIT 12"
            ).fetchall()],
            "settings": safe_settings(
                include_api_tokens=include_api_tokens,
                connection=connection,
            ),
        }


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/overview")
def overview(
    recommendation_limit: int | None = Query(default=None, ge=1, le=100),
):
    return overview_payload(recommendation_limit=recommendation_limit)


@app.get("/api/reading-history")
def reading_history():
    """Return the complete read list with any prior recommendation score."""
    with connect() as connection:
        items = [dict(item) for item in connection.execute(
            "SELECT id,title,author,rating,read_at,source,created_at FROM reads "
            "ORDER BY COALESCE(read_at,created_at) DESC,id DESC"
        ).fetchall()]
        scores = {}
        for candidate in connection.execute(
            "SELECT title,author,score FROM candidates WHERE status!='new' "
            "ORDER BY updated_at DESC,id DESC"
        ).fetchall():
            key = (str(candidate["title"]).strip().casefold(), str(candidate["author"]).strip().casefold())
            scores.setdefault(key, candidate["score"])
    for item in items:
        key = (str(item["title"]).strip().casefold(), str(item["author"]).strip().casefold())
        item["rank_score"] = scores.get(key)
    return items

def _recommendation_rows(
    connection=None,
    *,
    statuses: tuple[str, ...] | None = None,
    limit: int | None = 100,
    offset: int = 0,
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
                    "WHERE f.candidate_id=c.id AND f.action='reject'))"
                )
            if "maybe_later" in statuses:
                archive_terms.append("c.status='maybe_later'")
            clauses.append("(" + " OR ".join(archive_terms) + ")")
        else:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"c.status IN ({placeholders})")
            params.extend(statuses)
    query = (
        "SELECT c.*,s.name source_name,q.metadata_confidence,"
        "q.work_id quality_work_id,"
        "q.provider quality_provider,q.isbn13 quality_isbn13,"
        "q.isbn10 quality_isbn10,"
        "CASE WHEN c.status IN ('saved','imported') "
        "THEN COALESCE(rp.status,'saved') END reading_status,"
        "COALESCE(rp.up_next,0) up_next,rp.rating reading_rating,"
        "rp.started_at,rp.finished_at,rp.updated_at reading_updated_at "
        "FROM candidates c "
        "LEFT JOIN sources s ON s.id=c.source_id "
        "LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
        "LEFT JOIN reading_progress rp ON rp.candidate_id=c.id WHERE "
        + " AND ".join(clauses)
        + " ORDER BY CASE c.status WHEN 'recommended' THEN 0 ELSE 1 END,"
        " CASE WHEN rp.status='saved' AND rp.up_next=1 THEN 0 "
        "WHEN rp.status='reading' THEN 1 WHEN rp.status='saved' THEN 2 "
        "WHEN rp.status='finished' THEN 3 ELSE 4 END, c.score DESC"
    )
    result = (
        [dict(item) for item in connection.execute(query, params).fetchall()]
        if connection is not None
        else rows(query, params)
    )
    fetch = (
        lambda sql, values=(): [dict(item) for item in connection.execute(sql, values).fetchall()]
        if connection is not None
        else rows(sql, values)
    )
    read_keys = book_identity_match_index(fetch("SELECT * FROM reads"))
    shortlisted_keys = book_identity_match_index(
        fetch(
            "SELECT c.*,q.work_id quality_work_id,q.provider quality_provider,"
            "q.isbn13 quality_isbn13,q.isbn10 quality_isbn10 "
            "FROM candidates c LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
            "WHERE c.status IN ('saved','imported')"
        )
    )
    visible = []
    for item in result:
        if item["status"] in {"saved", "imported"}:
            visible.append(item)
            continue
        keys = book_row_identity_match_keys(item)
        if keys & read_keys or keys & shortlisted_keys:
            continue
        visible.append(item)
    end = None if limit is None else offset + limit
    return visible[offset:end]


def recommendation_list(
    connection=None,
    *,
    status: str | None = None,
    limit: int | None = 100,
    offset: int = 0,
    recommended_limit: int | None = None,
):
    if recommended_limit is not None and status is None and offset == 0:
        result = _recommendation_rows(
            connection, statuses=("recommended",), limit=recommended_limit
        )
        result.extend(
            _recommendation_rows(
                connection,
                statuses=("saved", "imported", "new"),
                limit=None,
            )
        )
    else:
        statuses = (
            ("rejected", "maybe_later")
            if status == "decisions"
            else None if not status or status == "all" else (status,)
        )
        result = _recommendation_rows(
            connection, statuses=statuses, limit=limit, offset=offset
        )
    for item in result:
        item["cover_url"] = fallback_cover_url(item["title"], item["author"], item.get("cover_url", ""), item.get("source_url", ""))
        source_url = item.get("source_url", "")
        parsed_source = urlparse(source_url)
        if (
            (parsed_source.hostname or "").casefold().rstrip(".")
            in {"openlibrary.org", "www.openlibrary.org"}
            and parsed_source.path.casefold().startswith("/people/")
            and "/lists/" in parsed_source.path.casefold()
        ):
            work_url = book_openlibrary_work_id(item)
            if work_url:
                source_url = f"https://openlibrary.org{work_url}"
        item["source_url"] = canonical_book_source_url(item["title"], item["author"], source_url)
        item["genres"] = json.loads(item["genres"] or "[]"); item["explanation"] = json.loads(item["explanation"] or "[]")
    return result

@app.get("/api/recommendations")
def recommendations(
    response: Response,
    status: Literal["recommended", "saved", "imported", "all", "decisions", "rejected", "maybe_later"] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    values, run_id = tracked_recommendations(
        status=status,
        limit=limit,
        offset=offset,
    )
    response.headers["X-Bookward-Recommendation-Run"] = run_id
    return values


def _ensure_reading_progress(con, candidate_id: int):
    con.execute(
        "INSERT OR IGNORE INTO reading_progress(candidate_id,status) VALUES(?,'saved')",
        (candidate_id,),
    )


def _mark_candidate_read_in_connection(con, candidate, rating=None, session_id=None):
    existing = con.execute(
        "SELECT id,rating FROM reads WHERE title=? AND author=?",
        (candidate["title"], candidate["author"]),
    ).fetchone()
    saved_rating = rating if rating is not None else existing["rating"] if existing else None
    openlibrary_work_id = book_openlibrary_work_id(candidate)
    con.execute(
        "INSERT INTO reads(title,author,rating,read_at,isbn,source,"
        "openlibrary_work_id,openlibrary_lookup_attempted_at) "
        "VALUES(?,?,?,NULL,NULL,'manual',?,"
        "CASE WHEN ?!='' THEN CURRENT_TIMESTAMP ELSE NULL END) "
        "ON CONFLICT(title,author) DO UPDATE SET "
        "rating=COALESCE(excluded.rating,reads.rating),"
        "openlibrary_work_id=CASE WHEN reads.openlibrary_work_id='' "
        "THEN excluded.openlibrary_work_id ELSE reads.openlibrary_work_id END,"
        "openlibrary_lookup_attempted_at=CASE "
        "WHEN reads.openlibrary_work_id='' AND excluded.openlibrary_work_id!='' "
        "THEN CURRENT_TIMESTAMP ELSE reads.openlibrary_lookup_attempted_at END",
        (
            candidate["title"],
            candidate["author"],
            rating,
            openlibrary_work_id,
            openlibrary_work_id,
        ),
    )
    read = con.execute(
        "SELECT id,rating FROM reads WHERE title=? AND author=?",
        (candidate["title"], candidate["author"]),
    ).fetchone()
    try:
        record_event_in_connection(
            con,
            event_key=f"manual-read:{uuid.uuid4()}",
            candidate_id=int(candidate["id"]),
            event_type="read",
            value=saved_rating,
            source="manual_read",
            metadata={"session_id": session_id} if session_id else {},
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return read, existing is not None


def _reading_list_items(con):
    return [
        item
        for item in recommendation_list(connection=con, status="all", limit=None)
        if item["status"] in {"saved", "imported"}
    ]


@app.get("/api/reading-list")
def get_reading_list():
    with connect() as con:
        return _reading_list_items(con)


@app.put("/api/reading-list/{candidate_id}")
def update_reading_progress(candidate_id: int, payload: ReadingProgressIn):
    fields = payload.model_fields_set
    if not fields:
        raise HTTPException(400, "Choose a reading status or update Up next.")
    if "rating" in fields and payload.status != "finished":
        raise HTTPException(400, "A rating can only be saved when a book is Finished.")

    with transaction() as con:
        candidate = con.execute(
            "SELECT c.id,c.title,c.author,c.status,c.source_url,"
            "q.work_id quality_work_id,q.provider quality_provider "
            "FROM candidates c LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
            "WHERE c.id=?",
            (candidate_id,),
        ).fetchone()
        if not candidate:
            raise HTTPException(404, "Book not found")
        if candidate["status"] not in {"saved", "imported"}:
            raise HTTPException(409, "Shortlist this book before updating its reading status.")

        _ensure_reading_progress(con, candidate_id)
        progress = con.execute(
            "SELECT * FROM reading_progress WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        current_status = progress["status"]
        next_status = payload.status or current_status
        if "up_next" in fields:
            if current_status != "saved" or next_status != "saved":
                raise HTTPException(409, "Up next is available for books in Saved.")
            con.execute(
                "UPDATE reading_progress SET up_next=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE candidate_id=?",
                (int(bool(payload.up_next)), candidate_id),
            )

        if payload.status is not None:
            if next_status == "saved":
                con.execute(
                    "UPDATE reading_progress SET status='saved',up_next=0,rating=NULL,"
                    "started_at=NULL,finished_at=NULL,updated_at=CURRENT_TIMESTAMP "
                    "WHERE candidate_id=?",
                    (candidate_id,),
                )
            elif next_status == "reading":
                con.execute(
                    "UPDATE reading_progress SET status='reading',up_next=0,rating=NULL,"
                    "started_at=COALESCE(started_at,CURRENT_TIMESTAMP),finished_at=NULL,"
                    "updated_at=CURRENT_TIMESTAMP WHERE candidate_id=?",
                    (candidate_id,),
                )
            else:
                if current_status == "finished" and "rating" not in fields:
                    read_rating = progress["rating"]
                else:
                    read, _already_present = _mark_candidate_read_in_connection(
                        con,
                        candidate,
                        rating=payload.rating,
                    )
                    read_rating = read["rating"]
                con.execute(
                    "UPDATE reading_progress SET status='finished',up_next=0,rating=?,"
                    "finished_at=COALESCE(finished_at,CURRENT_TIMESTAMP),"
                    "updated_at=CURRENT_TIMESTAMP WHERE candidate_id=?",
                    (read_rating, candidate_id),
                )
        updated = con.execute(
            "SELECT status,up_next,rating,started_at,finished_at,updated_at "
            "FROM reading_progress WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()

    if payload.status == "finished":
        enqueue_job("score", dedupe=True)
    return {
        "id": candidate_id,
        "status": updated["status"],
        "up_next": bool(updated["up_next"]),
        "rating": updated["rating"],
        "started_at": updated["started_at"],
        "finished_at": updated["finished_at"],
        "updated_at": updated["updated_at"],
    }

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
        if payload.action == "save":
            _ensure_reading_progress(con, candidate_id)
        elif payload.action == "restore":
            con.execute("DELETE FROM reading_progress WHERE candidate_id=?", (candidate_id,))
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
    if payload.action in {"save", "reject", "maybe_later"}:
        result["decision_id"] = int(feedback_id)
    return result


@app.post("/api/recommendations/{candidate_id}/undo")
def undo_feedback(candidate_id: int, payload: UndoFeedbackIn):
    with transaction() as con:
        decision = con.execute(
            "SELECT id,action,previous_status,undone_at FROM feedback "
            "WHERE id=? AND candidate_id=?",
            (payload.decision_id, candidate_id),
        ).fetchone()
        if not decision or not decision["previous_status"]:
            raise HTTPException(404, "Undo is no longer available")
        if decision["undone_at"]:
            raise HTTPException(409, "This decision has already been undone")
        if decision["action"] not in {"save", "reject", "maybe_later"}:
            raise HTTPException(404, "Undo is no longer available")
        latest = con.execute(
            "SELECT id FROM feedback WHERE candidate_id=? "
            "ORDER BY id DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if not latest or int(latest["id"]) != payload.decision_id:
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
        if decision["action"] == "save":
            if previous_status in {"saved", "imported"}:
                _ensure_reading_progress(con, candidate_id)
            else:
                con.execute(
                    "DELETE FROM reading_progress WHERE candidate_id=?",
                    (candidate_id,),
                )
        con.execute(
            "UPDATE candidates SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (previous_status, candidate_id),
        )
        con.execute(
            "UPDATE feedback SET undone_at=CURRENT_TIMESTAMP WHERE id=?",
            (payload.decision_id,),
        )
        con.execute(
            "DELETE FROM recommendation_outcomes WHERE event_id=("
            "SELECT id FROM recommendation_events WHERE event_key=?)",
            (f"feedback:{payload.decision_id}",),
        )
        compensation = (
            "save" if previous_status in {"saved", "imported"}
            else "reject" if previous_status == "rejected"
            else "maybe_later" if previous_status == "maybe_later"
            else "restore"
        )
        try:
            record_event_in_connection(
                con,
                event_key=f"undo-feedback:{payload.decision_id}",
                candidate_id=candidate_id,
                event_type=compensation,
                source="ui_undo",
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return {"id": candidate_id, "status": previous_status}


@app.post("/api/recommendations/{candidate_id}/read")
def mark_recommendation_read(candidate_id: int, payload: MarkReadIn):
    with transaction() as con:
        candidate = con.execute(
            "SELECT c.id,c.title,c.author,c.source_url,"
            "q.work_id quality_work_id,q.provider quality_provider "
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

    job_id = enqueue_job("score", dedupe=True)
    return {
        "id": candidate_id,
        "read_id": int(read["id"]),
        "status": "read",
        "rating": read["rating"],
        "job_id": job_id,
        "already_present": already_present,
    }


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
            if payload.action == "save":
                _ensure_reading_progress(con, candidate_id)
            elif payload.action == "restore":
                con.execute("DELETE FROM reading_progress WHERE candidate_id=?", (candidate_id,))
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
            _ensure_reading_progress(con, candidate_id)
        return {"id":candidate_id,"status":"imported","remote_id":str(data.get("id","imported"))}
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


@app.post("/api/librarr/download")
async def download_librarr(payload: LibrarrDownloadIn):
    try:
        media = normalize_media_type(payload.media_type)
        candidate = row("SELECT id,status FROM candidates WHERE id=?", (payload.candidate_id,)) if payload.candidate_id else None
        if payload.candidate_id and not candidate:
            raise HTTPException(404, "Recommendation not found")
        encoded = json.dumps(payload.result, sort_keys=True, separators=(",", ":"))
        key = hashlib.sha256(f"{media}:{encoded}".encode()).hexdigest()
        config = {**private_settings(), "librarr_allowed_hosts": settings.librarr_allowed_hosts}
        result = await librarr_download(config, payload.result, media, key)
        response: dict[str, Any] = {"ok": True, "media_type": media, "result": result}
        if candidate:
            status = candidate["status"]
            if status not in {"saved", "imported"}:
                with transaction() as con:
                    con.execute(
                        "UPDATE candidates SET status='saved',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (candidate["id"],),
                    )
                    _ensure_reading_progress(con, int(candidate["id"]))
                    feedback_id = con.execute(
                        "INSERT INTO feedback(candidate_id,action) VALUES(?,?)",
                        (candidate["id"], "save"),
                    ).lastrowid
                    record_event_in_connection(
                        con,
                        event_key=f"feedback:{feedback_id}",
                        candidate_id=int(candidate["id"]),
                        event_type="save",
                        source="librarr",
                        label=1.0,
                        label_kind="explicit_feedback",
                        confidence=0.9,
                    )
                status = "saved"
            response["candidate"] = {"id": candidate["id"], "status": status}
        return response
    except ValueError as exc:
        raise HTTPException(400, safe_error_message(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Librarr download failed: {safe_error_message(exc)}") from exc

@app.post("/api/import/goodreads/csv")
async def goodreads_csv(file: UploadFile = File(...)):
    content = bytearray()
    while chunk := await file.read(1024 * 1024):
        content.extend(chunk)
        if len(content) > 10_000_000: raise HTTPException(413, "CSV is larger than 10 MB")
    try: count = import_goodreads_csv(bytes(content))
    except ValueError as exc: raise HTTPException(400,safe_error_message(exc))
    enqueue_job("read_work_identities", dedupe=True)
    return {"imported":count,"job_id":enqueue_job("score", dedupe=True)}

@app.post("/api/import/goodreads/rss")
async def goodreads_rss(payload: UrlIn):
    try: count = await import_goodreads_rss(str(payload.url))
    except (ValueError,httpx.HTTPError) as exc: raise HTTPException(400,safe_error_message(exc))
    return {"imported":count,"job_id":enqueue_job("score", dedupe=True)}

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


@app.put("/api/sources/{source_id}")
def update_source(source_id: int, payload: SourceUpdateIn):
    filters = payload.filters.model_dump()
    with transaction() as con:
        source = con.execute("SELECT enabled,kind FROM sources WHERE id=?", (source_id,)).fetchone()
        if not source: raise HTTPException(404, "Source not found")
        if source["kind"] == "builtin": raise HTTPException(400, "The built-in source cannot be filtered")
        con.execute("UPDATE sources SET filters=? WHERE id=?", (json.dumps(filters), source_id))
    job_id = (
        enqueue_job(f"source:{source_id}", dedupe=True)
        if source["enabled"] and source["kind"] != "association"
        else None
    )
    return {"id": source_id, "filters": filters, "job_id": job_id}


def _save_digest_settings(updates: dict):
    current = private_settings()
    existing = digest_config(current)
    merged = {**existing, **updates}
    try:
        validate_digest_config(merged)
    except ValueError as exc:
        raise HTTPException(400, safe_error_message(exc)) from exc
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
def update_association_settings(payload: AssociationSettingsIn):
    current = private_settings()
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
    with transaction() as con:
        for key, value in values.items():
            secret = key.endswith("api_key")
            stored = seal(value) if secret and value else value
            con.execute(
                "INSERT INTO settings(key,value,secret) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=excluded.secret,updated_at=CURRENT_TIMESTAMP",
                (key, stored, 1 if secret else 0),
            )
    return {"saved": True, "associations": safe_association_settings()}


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


def safe_association_settings(connection=None):
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


def safe_settings(*, include_api_tokens: bool = True, connection=None):
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
        "librarr_media_type": media_type,
        "nyt_api_key_set": bool(private.get("nyt_api_key")),
        "source_sync_interval_hours": source_sync_interval_hours(),
        "digest": digest,
        "associations": safe_association_settings(connection),
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
async def update_settings(payload: EngineSettings):
    try: payload.validate_values()
    except ValueError as exc: raise HTTPException(400,safe_error_message(exc))
    current = private_settings(); values = payload.model_dump()
    for key in ("embedding_api_key","librarr_api_key"):
        if not values[key]: values[key] = current.get(key,"")
    test = await asyncio.to_thread(get_embedder, values["embedding_backend"],values["embedding_model"],values["embedding_url"],values["embedding_api_key"])
    health = await test.health()
    if not health.get("ok"): raise HTTPException(400, f"Embedding provider check failed: {safe_error_message(health.get('error','unknown error'))}")
    values["embedding_model"] = test.model
    with transaction() as con:
        for key,value in values.items():
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
            "reading_list": "GET /api/v1/reading-list",
            "reading_progress": "PUT /api/v1/reading-list/{id}",
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
    status: Literal["recommended", "saved", "imported", "all", "decisions", "rejected", "maybe_later"] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    values, run_id = tracked_recommendations(status=status, limit=limit, offset=offset)
    response.headers["X-Bookward-Recommendation-Run"] = run_id
    return values


@api_v1.get("/reading-list")
def api_reading_list():
    return get_reading_list()


@api_v1.put("/reading-list/{candidate_id}")
def api_update_reading_progress(candidate_id: int, payload: ReadingProgressIn):
    return update_reading_progress(candidate_id, payload)


@api_v1.post("/recommendations/bulk-feedback")
def api_bulk_feedback(payload: BulkFeedbackIn):
    return bulk_feedback(payload)


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
async def api_import_librarr(candidate_id: int):
    return await import_librarr(candidate_id)


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
