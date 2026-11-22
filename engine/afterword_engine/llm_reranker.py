"""Opt-in grounded LLM reranker for the top recommendation slate."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import settings
from .security import resolve_public_target, safe_error_message, validate_service_url
from .tenancy import current_profile_id, profile_database_path

LOGGER = logging.getLogger(__name__)

MAX_CACHED_PROFILES = 64
MAX_CACHE_ENTRIES_PER_PROFILE = 8
LLM_CIRCUIT_COOLDOWN_SECONDS = 300.0
_KEY_FINGERPRINT_SALT = secrets.token_bytes(16)
_KEY_FINGERPRINT_ITERATIONS = 100_000
_SAFE_LLM_PATH_RE = re.compile(r"^/[A-Za-z0-9._~:/=-]*$")

_STATE_LOCK = threading.Lock()
_SLATE_CACHE: OrderedDict[str, OrderedDict[str, dict[str, Any]]] = OrderedDict()
_CIRCUIT_BREAKER: OrderedDict[str, dict[str, Any]] = OrderedDict()


def current_llm_profile_key() -> str:
    """Return an isolated per-profile cache and circuit-breaker scope key."""
    profile = current_profile_id() or "legacy"
    try:
        db_path = str(profile_database_path())
    except Exception:
        db_path = settings.db
    return f"{profile}::{db_path}"


def llm_allowed_hosts(raw: str | None = None) -> set[str]:
    source = settings.llm_rerank_allowed_hosts if raw is None else raw
    return {
        host.strip().lower().strip("[]").rstrip(".")
        for host in str(source or "").split(",")
        if host.strip()
    }


def _normalize_chat_url(url: str) -> str:
    """Normalize an OpenAI-compatible base or chat-completions endpoint URL."""
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return ""
    if cleaned.endswith("/chat/completions"):
        return cleaned
    if cleaned.endswith(("/v1", "/openai")):
        return f"{cleaned}/chat/completions"
    return cleaned


def validate_llm_api_url(url: str, allowed_hosts: set[str] | None = None) -> str:
    """Validate an LLM endpoint URL against SSRF guards and normalize its chat path."""
    cleaned = str(url or "").strip()
    if not cleaned:
        raise ValueError("LLM API URL is required")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("LLM API URL must use HTTP or HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("LLM API URL cannot contain credentials, query parameters, or fragments")
    hosts = llm_allowed_hosts() if allowed_hosts is None else allowed_hosts
    validate_service_url(cleaned, hosts)
    normalized = _normalize_chat_url(cleaned)
    norm_path = urlparse(normalized).path or "/"
    if ".." in norm_path.split("/") or not _SAFE_LLM_PATH_RE.fullmatch(norm_path):
        raise ValueError("LLM API URL contains an invalid path")
    return normalized


def _prepare_llm_request_target(
    url: str, allowed_hosts: set[str] | None = None
) -> tuple[str, dict[str, str], dict[str, str] | None]:
    """Validate and pin an outbound LLM endpoint URL against SSRF and DNS rebinding."""
    normalized = validate_llm_api_url(url, allowed_hosts=allowed_hosts)
    parsed = urlparse(normalized)
    if parsed.scheme == "https":
        scheme = "https"
        default_port = 443
    elif parsed.scheme == "http":
        scheme = "http"
        default_port = 80
    else:
        raise ValueError("LLM API URL must use HTTP or HTTPS")

    port = parsed.port
    port_suffix = ""
    if port is not None and port != default_port:
        port_str = str(port)
        if not port_str.isdigit():
            raise ValueError("Invalid LLM API URL port")
        port_suffix = f":{port_str}"

    raw_path = parsed.path or "/"
    if ".." in raw_path.split("/") or not _SAFE_LLM_PATH_RE.fullmatch(raw_path):
        raise ValueError("LLM API URL contains an invalid path")

    requested_host = (parsed.hostname or "").lower().rstrip(".")
    if not requested_host:
        raise ValueError("LLM API URL must include a hostname")

    hosts = llm_allowed_hosts() if allowed_hosts is None else allowed_hosts
    matched_allowed: str | None = None
    for allowed_host in hosts:
        if allowed_host and requested_host == allowed_host:
            matched_allowed = allowed_host
            break

    if matched_allowed is not None:
        connect_host = f"[{matched_allowed}]" if ":" in matched_allowed else matched_allowed
        return f"{scheme}://{connect_host}{port_suffix}{raw_path}", {}, None

    _, address, resolved_host = resolve_public_target(
        normalized, allow_http=(scheme == "http")
    )
    try:
        ipaddress.ip_address(resolved_host)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False
    connect_host = f"[{address}]" if ":" in address else address
    formatted_host = f"[{resolved_host}]" if ":" in resolved_host else resolved_host
    host_header = formatted_host if not port_suffix else f"{formatted_host}{port_suffix}"
    extensions = (
        {"sni_hostname": resolved_host}
        if scheme == "https" and not is_ip_literal
        else None
    )
    return (
        f"{scheme}://{connect_host}{port_suffix}{raw_path}",
        {"Host": host_header},
        extensions,
    )


def sanitize_llm_error(error: Any, api_key: str = "", limit: int = 300) -> str:
    """Return a bounded, credential-redacted error message."""
    raw = str(error or "").strip() or "Request failed"
    secret = str(api_key or "").strip()
    if secret and secret in raw:
        raw = raw.replace(secret, "[redacted]")
    redacted = safe_error_message(raw, limit=limit)
    if secret and secret in redacted:
        redacted = redacted.replace(secret, "[redacted]")[:limit]
    return redacted


def _derive_api_key_fingerprint(api_key: str) -> str:
    """Derive a non-reversible PBKDF2 fingerprint for in-memory cache isolation."""
    if not api_key:
        return ""
    return hashlib.pbkdf2_hmac(
        "sha256",
        api_key.encode("utf-8"),
        _KEY_FINGERPRINT_SALT,
        _KEY_FINGERPRINT_ITERATIONS,
        dklen=16,
    ).hex()


def _config_fingerprint(
    *,
    api_url: str,
    api_key: str,
    model: str,
    top_k: int,
    max_delta: float,
) -> str:
    non_secret_payload = json.dumps(
        {
            "url": _normalize_chat_url(api_url),
            "model": model.strip(),
            "top_k": int(top_k),
            "max_delta": round(float(max_delta), 4),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    cfg_digest = hashlib.sha256(non_secret_payload.encode("utf-8")).hexdigest()[:24]
    key_fp = _derive_api_key_fingerprint(api_key)
    return f"{cfg_digest}:{key_fp}"


def build_slate_cache_key(
    *,
    messages: Sequence[dict[str, str]],
    interaction_events: Sequence[dict[str, Any]] | None,
    api_url: str,
    api_key: str,
    model: str,
    top_k: int,
    max_delta: float,
) -> str:
    """Compute a deterministic cache key invalidated by slate, feedback, or LLM setting changes."""
    cfg_fp = _config_fingerprint(
        api_url=api_url,
        api_key=api_key,
        model=model,
        top_k=top_k,
        max_delta=max_delta,
    )
    feedback_tail = [
        (
            event.get("id"),
            event.get("candidate_id"),
            event.get("event_type") or event.get("action"),
            event.get("value"),
        )
        for event in (interaction_events or ())
    ]
    digest_payload = json.dumps(
        {
            "messages": list(messages),
            "feedback": feedback_tail,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    slate_digest = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
    return f"{cfg_fp}:{slate_digest}"


def get_cached_slate_payload(profile_key: str, cache_key: str) -> dict[str, Any] | None:
    with _STATE_LOCK:
        bucket = _SLATE_CACHE.get(profile_key)
        if not bucket or cache_key not in bucket:
            return None
        _SLATE_CACHE.move_to_end(profile_key)
        bucket.move_to_end(cache_key)
        entry = bucket[cache_key]
        return {
            "mode": entry["mode"],
            "entries": [dict(item) for item in entry["entries"] if isinstance(item, dict)],
        }


def set_cached_slate_payload(
    profile_key: str,
    cache_key: str,
    payload: dict[str, Any],
) -> None:
    stored = {
        "mode": str(payload.get("mode") or "adjustments"),
        "entries": [
            dict(item)
            for item in (payload.get("entries") or ())
            if isinstance(item, dict)
        ],
    }
    with _STATE_LOCK:
        bucket = _SLATE_CACHE.get(profile_key)
        if bucket is None:
            bucket = OrderedDict()
            _SLATE_CACHE[profile_key] = bucket
        _SLATE_CACHE.move_to_end(profile_key)
        bucket[cache_key] = stored
        bucket.move_to_end(cache_key)
        while len(bucket) > MAX_CACHE_ENTRIES_PER_PROFILE:
            bucket.popitem(last=False)
        while len(_SLATE_CACHE) > MAX_CACHED_PROFILES:
            _SLATE_CACHE.popitem(last=False)


def get_circuit_breaker_state(
    profile_key: str,
    *,
    api_url: str,
    api_key: str,
    model: str,
    top_k: int | None = None,
    max_delta: float | None = None,
) -> dict[str, Any] | None:
    now = time.monotonic()
    with _STATE_LOCK:
        entry = _CIRCUIT_BREAKER.get(profile_key)
        if not entry:
            return None
        if now >= float(entry.get("open_until") or 0.0):
            _CIRCUIT_BREAKER.pop(profile_key, None)
            return None
    cfg_fp = _config_fingerprint(
        api_url=api_url,
        api_key=api_key,
        model=model,
        top_k=settings.llm_rerank_top_k if top_k is None else top_k,
        max_delta=settings.llm_rerank_max_delta if max_delta is None else max_delta,
    )
    now = time.monotonic()
    with _STATE_LOCK:
        entry = _CIRCUIT_BREAKER.get(profile_key)
        if not entry:
            return None
        if entry.get("cfg") != cfg_fp or now >= float(entry.get("open_until") or 0.0):
            _CIRCUIT_BREAKER.pop(profile_key, None)
            return None
        _CIRCUIT_BREAKER.move_to_end(profile_key)
        return dict(entry)


def trip_circuit_breaker(
    profile_key: str,
    *,
    api_url: str,
    api_key: str,
    model: str,
    error: str,
    cooldown_seconds: float = LLM_CIRCUIT_COOLDOWN_SECONDS,
    top_k: int | None = None,
    max_delta: float | None = None,
) -> dict[str, Any]:
    cfg_fp = _config_fingerprint(
        api_url=api_url,
        api_key=api_key,
        model=model,
        top_k=settings.llm_rerank_top_k if top_k is None else top_k,
        max_delta=settings.llm_rerank_max_delta if max_delta is None else max_delta,
    )
    cooldown = max(1.0, float(cooldown_seconds))
    now_mono = time.monotonic()
    cooldown_until = (
        datetime.now(timezone.utc) + timedelta(seconds=cooldown)
    ).isoformat(timespec="seconds")
    record = {
        "cfg": cfg_fp,
        "open_until": now_mono + cooldown,
        "cooldown_until": cooldown_until,
        "error": error,
    }
    with _STATE_LOCK:
        _CIRCUIT_BREAKER[profile_key] = record
        _CIRCUIT_BREAKER.move_to_end(profile_key)
        while len(_CIRCUIT_BREAKER) > MAX_CACHED_PROFILES:
            _CIRCUIT_BREAKER.popitem(last=False)
    return dict(record)


def clear_llm_rerank_state(profile_key: str | None = None, *, clear_all: bool = False) -> None:
    with _STATE_LOCK:
        if clear_all:
            _SLATE_CACHE.clear()
            _CIRCUIT_BREAKER.clear()
            return
        target = current_llm_profile_key() if profile_key is None else profile_key
        _SLATE_CACHE.pop(target, None)
        _CIRCUIT_BREAKER.pop(target, None)


def clear_circuit_breaker(profile_key: str | None = None) -> None:
    target = current_llm_profile_key() if profile_key is None else profile_key
    with _STATE_LOCK:
        _CIRCUIT_BREAKER.pop(target, None)


def build_rerank_prompt(
    reads: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    *,
    interaction_events: Sequence[dict[str, Any]] | None = None,
    top_k: int = 24,
    max_history_items: int = 15,
) -> list[dict[str, str]]:
    """Build a grounded reranking prompt from read history, discovery events, and candidates."""
    liked = [
        {"title": r.get("title"), "author": r.get("author"), "rating": r.get("rating")}
        for r in reads
        if r.get("rating") is not None and float(r["rating"]) >= 4.0
    ][-15:]
    disliked = [
        {"title": r.get("title"), "author": r.get("author"), "rating": r.get("rating")}
        for r in reads
        if r.get("rating") is not None and float(r["rating"]) <= 2.0
    ][-8:]

    slate = [
        {
            "id": int(c["id"]),
            "title": str(c.get("title") or "")[:140],
            "author": str(c.get("author") or "")[:100],
            "genres": list(c.get("genres") or [])[:4] if isinstance(c.get("genres"), list) else [],
            "description": " ".join(str(c.get("description") or "").split())[:240],
            "base_score": float(c.get("score") or 0.0),
        }
        for c in list(candidates)[: max(1, int(top_k))]
        if c.get("id") is not None
    ]
    slate_ids = {int(c["id"]) for c in slate}

    saved_discovery: list[dict[str, Any]] = []
    rejected_discovery: list[dict[str, Any]] = []
    if interaction_events:
        seen_saved: set[str] = set()
        seen_rejected: set[str] = set()
        # Reverse iterate (most recent first) and exclude candidates in slate to guarantee zero data leakage
        for event in reversed(interaction_events):
            cid_val = event.get("candidate_id") if event.get("candidate_id") is not None else event.get("id")
            try:
                cid = int(cid_val)
            except (TypeError, ValueError):
                cid = -1
            if cid in slate_ids:
                continue

            event_type = str(event.get("event_type") or event.get("action") or "").lower()
            title = str(event.get("title") or "")[:120].strip()
            author = str(event.get("author") or "")[:80].strip()
            if not title:
                continue
            key = f"{title.lower()}::{author.lower()}"

            genres = event.get("genres")
            if isinstance(genres, str):
                try:
                    genres = json.loads(genres)
                except Exception:
                    genres = []
            genres_list = list(genres)[:4] if isinstance(genres, list) else []

            item_info = {"title": title, "author": author, "genres": genres_list}
            if (
                event_type in {"save", "librarr_import"}
                and key not in seen_saved
                and len(saved_discovery) < max_history_items
            ):
                seen_saved.add(key)
                saved_discovery.append(item_info)
            elif (
                event_type == "reject"
                and key not in seen_rejected
                and len(rejected_discovery) < max_history_items
            ):
                seen_rejected.add(key)
                rejected_discovery.append(item_info)

    user_payload: dict[str, Any] = {
        "goodreads_reads_liked": liked,
        "goodreads_reads_disliked": disliked,
        "recent_discovery_saved_books": saved_discovery,
        "recent_discovery_rejected_books": rejected_discovery,
        "candidates": slate,
    }

    system_prompt = (
        "You are an expert personalized recommendation reranker for a book discovery engine.\n"
        "The candidates have already been scored by an interaction-aware model (`base_score`), which has high baseline precision.\n"
        "The user uses Discovery to find fresh, relevant reading, but frequently encounters two types of false positives:\n"
        "1. CANON / CLASSICS: Widely known 20th-century or older classics and school canon (e.g. Orwell, Angelou, Ellison, Le Guin, Chekhov, Sinclair Lewis, Tolkien, Austen, Burgess) which the user already knows or does not want in their feed.\n"
        "2. GENERIC SELF-HELP / FORMULAIC FICTION: Generic pop self-help/influencer advice or mass-market formulaic cozy/romance/action thrillers.\n"
        "Conversely, the user explicitly SAVES: contemporary speculative fiction/sci-fi, thoughtful investigative/social-science non-fiction, and sharp contemporary literature.\n\n"
        "TASK: Provide conservative score adjustments `delta` in [-1.5, +1.5] to correct obvious misplacements:\n"
        "- Apply a negative penalty (-0.6 to -1.5) to obvious classic canon, generic self-help, or formulaic fluff.\n"
        "- Apply a modest boost (+0.4 to +1.0) to strong contemporary speculative or investigative non-fiction matches.\n"
        "- Keep delta at 0.0 for all other books. Preserve the base ordering unless you have clear evidence.\n\n"
        "Return strict JSON:\n"
        "{\"adjustments\": [{\"id\": <int>, \"delta\": <float>, \"reason\": <short string>}]}"
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def apply_llm_rerank_adjustments(
    candidates: Sequence[dict[str, Any]],
    adjustments: Sequence[dict[str, Any]],
    *,
    top_k: int = 24,
    max_delta: float = 1.2,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply bounded calibrated LLM score adjustments."""
    items = [dict(c) for c in candidates]
    head = items[: max(0, int(top_k))]
    tail = items[max(0, int(top_k)) :]
    by_id = {int(c["id"]): c for c in head if c.get("id") is not None}
    if len(by_id) < 2:
        return items, {"applied": False, "reason": "insufficient_candidates"}

    adjusted_count = 0
    for entry in adjustments:
        if not isinstance(entry, dict):
            continue
        try:
            cid = int(entry["id"])
            if cid in by_id:
                raw_delta = float(entry.get("delta", 0.0))
                clipped_delta = max(-max_delta, min(max_delta, raw_delta))
                cand = by_id[cid]
                base_score = float(cand.get("score") or 0.0)
                cand["score"] = round(max(0.0, min(100.0, base_score + clipped_delta)), 2)
                reason = " ".join(str(entry.get("reason") or "").split())[:140]
                if reason:
                    explanation = list(cand.get("explanation") or [])
                    explanation.append(f"LLM delta ({clipped_delta:+.2f}): {reason}")
                    cand["explanation"] = explanation
                if abs(clipped_delta) > 0.001:
                    adjusted_count += 1
        except (KeyError, TypeError, ValueError):
            continue

    reranked_head = sorted(
        head,
        key=lambda row: (-float(row.get("score") or 0.0), -int(row.get("id") or 0)),
    )
    return reranked_head + tail, {
        "applied": True,
        "adjusted_count": adjusted_count,
        "mode": "adjustments",
    }


def apply_llm_rerank_order(
    candidates: Sequence[dict[str, Any]],
    llm_rankings: Sequence[dict[str, Any]],
    *,
    top_k: int = 24,
    max_nudge: float = 1.2,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply validated LLM listwise ordering with a bounded score nudge (backward compatibility)."""
    items = [dict(c) for c in candidates]
    head = items[: max(0, int(top_k))]
    tail = items[max(0, int(top_k)) :]
    by_id = {int(c["id"]): c for c in head if c.get("id") is not None}
    if len(by_id) < 2:
        return items, {"applied": False, "reason": "insufficient_candidates"}

    ordered_ids: list[int] = []
    reasons: dict[int, str] = {}
    for entry in llm_rankings:
        if not isinstance(entry, dict):
            continue
        try:
            cid = int(entry["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if cid in by_id and cid not in ordered_ids:
            ordered_ids.append(cid)
            reason = " ".join(str(entry.get("reason") or "").split())[:140]
            if reason:
                reasons[cid] = reason

    if not ordered_ids:
        return items, {"applied": False, "reason": "no_valid_ids"}

    for c in head:
        cid = int(c["id"])
        if cid not in ordered_ids:
            ordered_ids.append(cid)

    n = len(ordered_ids)
    for pos, cid in enumerate(ordered_ids):
        cand = by_id[cid]
        nudge = max_nudge * (1.0 - 2.0 * pos / max(1, n - 1))
        cand["score"] = round(max(0.0, min(100.0, float(cand.get("score") or 0.0) + nudge)), 2)
        if cid in reasons:
            explanation = list(cand.get("explanation") or [])
            explanation.append(f"LLM rerank: {reasons[cid]}")
            cand["explanation"] = explanation

    reranked_head = sorted(
        head,
        key=lambda row: (-float(row.get("score") or 0.0), -int(row.get("id") or 0)),
    )
    return reranked_head + tail, {
        "applied": True,
        "reranked_count": len(reasons) or n,
        "mode": "listwise",
    }


def apply_cached_llm_payload(
    candidates: Sequence[dict[str, Any]],
    cached_payload: dict[str, Any],
    *,
    top_k: int = 24,
    max_delta: float = 1.2,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    entries = cached_payload.get("entries") or []
    if cached_payload.get("mode") == "listwise":
        reranked, diag = apply_llm_rerank_order(
            candidates, entries, top_k=top_k, max_nudge=max_delta
        )
    else:
        reranked, diag = apply_llm_rerank_adjustments(
            candidates, entries, top_k=top_k, max_delta=max_delta
        )
    return reranked, {**diag, "cached": True}


def _extract_response_content(response: httpx.Response) -> dict[str, Any]:
    is_redirect = getattr(response, "is_redirect", False) or (
        300 <= int(getattr(response, "status_code", 200)) < 400
    )
    if is_redirect:
        raise ValueError("Redirects are not allowed for the LLM endpoint")
    status_code = int(getattr(response, "status_code", 200))
    if status_code >= 400:
        detail = ""
        try:
            err_body = response.json()
            if isinstance(err_body, dict):
                raw_err = err_body.get("error")
                if isinstance(raw_err, dict):
                    detail = str(raw_err.get("message") or "").strip()
                elif isinstance(raw_err, str):
                    detail = raw_err.strip()
        except Exception:
            detail = ""
        if detail:
            raise ValueError(f"HTTP {status_code}: {detail}")
        response.raise_for_status()
    else:
        response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("LLM endpoint returned a non-JSON-object response")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("LLM endpoint returned no completion choices")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM endpoint returned an empty completion message")
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("LLM completion content must be a JSON object")
    return parsed


def _parse_rerank_payload(
    candidates: Sequence[dict[str, Any]],
    parsed: dict[str, Any],
    *,
    top_k: int,
    max_delta: float,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
    if "adjustments" in parsed and isinstance(parsed["adjustments"], list):
        entries = [dict(item) for item in parsed["adjustments"] if isinstance(item, dict)]
        reranked, diag = apply_llm_rerank_adjustments(
            candidates, entries, top_k=top_k, max_delta=max_delta
        )
        cache_entry = {"mode": "adjustments", "entries": entries} if diag.get("applied") else None
        return reranked, diag, cache_entry
    rankings = parsed.get("rankings")
    if isinstance(rankings, list):
        entries = [dict(item) for item in rankings if isinstance(item, dict)]
        reranked, diag = apply_llm_rerank_order(
            candidates, entries, top_k=top_k, max_nudge=max_delta
        )
        cache_entry = {"mode": "listwise", "entries": entries} if diag.get("applied") else None
        return reranked, diag, cache_entry
    raise ValueError("LLM completion JSON did not contain adjustments or rankings")


async def probe_llm_reranker(
    *,
    api_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float | None = None,
    allowed_hosts: set[str] | None = None,
) -> dict[str, Any]:
    """Send a lightweight JSON probe to verify live LLM endpoint connectivity and credentials."""
    key = str(api_key or "").strip()
    chosen_model = str(model or "").strip()
    if not key:
        raise ValueError("An API key is required to test the LLM reranker")
    if not chosen_model:
        raise ValueError("A model is required to test the LLM reranker")
    target_url, host_headers, extensions = _prepare_llm_request_target(
        api_url, allowed_hosts=allowed_hosts
    )
    timeout = timeout_seconds if timeout_seconds is not None else settings.llm_rerank_timeout_seconds
    probe_messages = [
        {
            "role": "system",
            "content": 'Connection check. Return strict JSON: {"adjustments": []}',
        },
        {
            "role": "user",
            "content": '{"candidates": []}',
        },
    ]
    extra_kwargs: dict[str, Any] = {"extensions": extensions} if extensions else {}
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                target_url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    **host_headers,
                },
                json={
                    "model": chosen_model,
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                    "messages": probe_messages,
                },
                **extra_kwargs,
            )
            _extract_response_content(response)
    except Exception as exc:
        raise RuntimeError(sanitize_llm_error(exc, api_key=key)) from exc
    return {"ok": True, "checked": True, "model": chosen_model}


def rerank_with_llm_sync(
    reads: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    *,
    interaction_events: Sequence[dict[str, Any]] | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    top_k: int | None = None,
    max_delta: float | None = None,
    timeout_seconds: float | None = None,
    allowed_hosts: set[str] | None = None,
    prebuilt_messages: Sequence[dict[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Call the configured OpenAI-compatible LLM endpoint synchronously and rerank top-K candidates."""
    key = (api_key if api_key is not None else settings.llm_rerank_api_key).strip()
    raw_url = (api_url if api_url is not None else settings.llm_rerank_api_url).strip()
    chosen_model = (model if model is not None else settings.llm_rerank_model).strip()
    k = top_k if top_k is not None else settings.llm_rerank_top_k
    delta = max_delta if max_delta is not None else settings.llm_rerank_max_delta
    timeout = timeout_seconds if timeout_seconds is not None else settings.llm_rerank_timeout_seconds

    if not key or not raw_url or not chosen_model or len(candidates) < 2:
        return [dict(c) for c in candidates], {"applied": False, "reason": "not_configured"}

    try:
        target_url, host_headers, extensions = _prepare_llm_request_target(
            raw_url, allowed_hosts=allowed_hosts
        )
        messages = (
            list(prebuilt_messages)
            if prebuilt_messages is not None
            else build_rerank_prompt(
                reads, candidates, interaction_events=interaction_events, top_k=k
            )
        )
        extra_kwargs: dict[str, Any] = {"extensions": extensions} if extensions else {}
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.post(
                target_url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    **host_headers,
                },
                json={
                    "model": chosen_model,
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                    "messages": messages,
                },
                **extra_kwargs,
            )
            parsed = _extract_response_content(response)
            reranked, diag, cache_entry = _parse_rerank_payload(
                candidates, parsed, top_k=k, max_delta=delta
            )
            if cache_entry is not None:
                diag = {**diag, "_cache_payload": cache_entry}
            return reranked, diag
    except Exception as exc:
        err = sanitize_llm_error(exc, api_key=key)
        LOGGER.warning("LLM reranker failed; falling back to base order: %s", err)
        return [dict(c) for c in candidates], {
            "applied": False,
            "reason": "fallback",
            "error": err,
        }
