"""Opt-in grounded LLM reranker for the top recommendation slate."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Sequence
from urllib.parse import unquote, urlparse

import httpx

from .config import settings
from .security import validate_service_url

LOGGER = logging.getLogger(__name__)

LLM_RERANK_MODES = ("cloud", "ollama", "openai-compatible")
DEFAULT_OLLAMA_URL = "http://ollama:11434"
DEFAULT_CLOUD_LLM_URL = "https://api.openai.com/v1/chat/completions"
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", flags=re.DOTALL | re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", flags=re.DOTALL | re.IGNORECASE)
_SAFE_AUTHORITY_HOST_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?|\[[0-9a-f:]+\])")
_SAFE_OPTIONAL_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._/-]*)?")
_SAFE_ENDPOINT_PATH_RE = re.compile(r"/[A-Za-z0-9._/-]+")


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


def normalize_llm_rerank_mode(value: str | None) -> str:
    mode = str(value or "cloud").strip().lower()
    if mode not in LLM_RERANK_MODES:
        raise ValueError(f"Unsupported LLM reranker mode: {mode}")
    return mode


def llm_rerank_allowed_hosts(raw: str | None = None) -> set[str]:
    source = settings.llm_rerank_allowed_hosts if raw is None else raw
    return {
        host.strip().lower().rstrip(".")
        for host in str(source or "").split(",")
        if host.strip()
    }


def _validate_and_split_llm_origin(
    url: str, allowed_hosts: set[str] | None = None
) -> tuple[str, str, str, str]:
    cleaned = str(url or "").strip()
    if not cleaned:
        raise ValueError("LLM API URL is required")
    if "\\" in cleaned or any(ord(ch) < 33 or ord(ch) == 127 for ch in cleaned):
        raise ValueError("LLM API URL contains invalid characters")
    parsed = urlparse(cleaned)
    if parsed.scheme == "https":
        scheme = "https"
    elif parsed.scheme == "http":
        scheme = "http"
    else:
        raise ValueError("LLM API URL must use HTTP or HTTPS")
    if not parsed.hostname:
        raise ValueError("LLM API URL must use HTTP or HTTPS")
    if parsed.username or parsed.password or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("LLM API URL cannot contain credentials, query parameters, or fragments")
    try:
        port_num = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid port in LLM API URL") from exc

    port_str = ""
    if port_num is not None:
        if not (1 <= port_num <= 65535):
            raise ValueError("Invalid port in LLM API URL")
        port_str = str(int(port_num))
        if not port_str.isdigit():
            raise ValueError("Invalid port in LLM API URL")

    raw_path = parsed.path or ""
    unquoted_path = unquote(raw_path)
    if (
        "%" in raw_path
        or "\\" in unquoted_path
        or ".." in raw_path.split("/")
        or ".." in unquoted_path.split("/")
        or not _SAFE_OPTIONAL_PATH_RE.fullmatch(raw_path)
    ):
        raise ValueError("LLM API URL contains an invalid path")

    hosts = llm_rerank_allowed_hosts() if allowed_hosts is None else allowed_hosts
    raw_host = parsed.hostname.lower().rstrip(".")
    matched_allowed = next((allowed for allowed in hosts if allowed == raw_host), None)
    effective_host = matched_allowed if matched_allowed is not None else raw_host
    authority_host = (
        f"[{effective_host}]"
        if ":" in effective_host and not effective_host.startswith("[")
        else effective_host
    )
    if not _SAFE_AUTHORITY_HOST_RE.fullmatch(authority_host):
        raise ValueError("Invalid host in LLM API URL")

    authority = f"{authority_host}:{port_str}" if port_str else authority_host
    validate_service_url(f"{scheme}://{authority}{raw_path}", hosts)
    return scheme, authority_host, port_str, raw_path


def validate_llm_rerank_url(url: str, allowed_hosts: set[str] | None = None) -> str:
    scheme, authority_host, port_str, raw_path = _validate_and_split_llm_origin(
        url, allowed_hosts
    )
    authority = f"{authority_host}:{port_str}" if port_str else authority_host
    return f"{scheme}://{authority}{raw_path}"


def _build_validated_llm_target_url(
    url: str,
    *,
    mode: str,
    endpoint: str,
    allowed_hosts: set[str] | None = None,
) -> str:
    scheme, authority_host, port_str, raw_path = _validate_and_split_llm_origin(
        url, allowed_hosts
    )
    authority = f"{authority_host}:{port_str}" if port_str else authority_host
    canonical_input = f"{scheme}://{authority}{raw_path}"
    if mode == "ollama":
        base_path = urlparse(_normalize_ollama_base_url(canonical_input)).path.rstrip("/")
        suffix = "/api/tags" if endpoint == "models" else "/api/chat"
        endpoint_path = f"{base_path}{suffix}" if base_path else suffix
    elif endpoint == "models":
        endpoint_path = urlparse(_normalize_openai_models_url(canonical_input)).path
    else:
        endpoint_path = urlparse(_normalize_chat_url(canonical_input)).path

    if (
        ".." in endpoint_path.split("/")
        or not _SAFE_ENDPOINT_PATH_RE.fullmatch(endpoint_path)
    ):
        raise ValueError("Invalid endpoint path in LLM API URL")

    if port_str:
        return f"{scheme}://{authority_host}:{port_str}{endpoint_path}"
    return f"{scheme}://{authority_host}{endpoint_path}"


def _normalize_ollama_base_url(url: str) -> str:
    """Normalize an Ollama server URL to its base origin without trailing API paths."""
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return ""
    for suffix in (
        "/api/chat",
        "/api/tags",
        "/api/generate",
        "/v1/chat/completions",
        "/v1/models",
        "/api",
        "/v1",
    ):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].rstrip("/")
            break
    return cleaned


def _normalize_chat_url(url: str) -> str:
    """Normalize an OpenAI-compatible base or chat-completions endpoint URL."""
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return ""
    if cleaned.endswith("/chat/completions"):
        return cleaned
    if cleaned.endswith(("/v1", "/openai")):
        return f"{cleaned}/chat/completions"
    parsed = urlparse(cleaned)
    if parsed.scheme in {"http", "https"} and parsed.netloc and parsed.path in {"", "/"}:
        return f"{cleaned}/v1/chat/completions"
    return cleaned


def _normalize_openai_models_url(url: str) -> str:
    """Normalize an OpenAI-compatible base or chat URL to its models discovery endpoint."""
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return ""
    if cleaned.endswith("/models"):
        return cleaned
    if cleaned.endswith("/chat/completions"):
        return f"{cleaned[: -len('/chat/completions')]}/models"
    if cleaned.endswith(("/v1", "/openai")):
        return f"{cleaned}/models"
    return f"{cleaned}/v1/models"


def extract_llm_json(content: str) -> dict[str, Any]:
    """Parse strict JSON or extract a JSON object when local models omit strict schema flags."""
    text = str(content or "").strip()
    if not text:
        raise ValueError("LLM returned an empty response")

    candidates_to_parse = [text]
    stripped_think = _THINK_TAG_RE.sub("", text).strip()
    if stripped_think and stripped_think != text:
        candidates_to_parse.append(stripped_think)

    for candidate in list(candidates_to_parse):
        for match in _CODE_FENCE_RE.finditer(candidate):
            fenced = match.group(1).strip()
            if fenced:
                candidates_to_parse.append(fenced)

    for candidate in candidates_to_parse:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, ValueError):
            continue

    scan_target = stripped_think or text
    decoder = json.JSONDecoder()
    fallback_dict: dict[str, Any] | None = None
    pos = 0
    while True:
        brace = scan_target.find("{", pos)
        if brace == -1:
            break
        try:
            obj, _ = decoder.raw_decode(scan_target, brace)
            if isinstance(obj, dict):
                if "adjustments" in obj or "rankings" in obj:
                    return obj
                if fallback_dict is None:
                    fallback_dict = obj
        except ValueError:
            pass
        pos = brace + 1

    if fallback_dict is not None:
        return fallback_dict
    raise ValueError("LLM response did not contain a valid JSON object")


def _build_headers(api_key: str, *, include_content_type: bool = True) -> dict[str, str]:
    headers: dict[str, str] = {}
    if include_content_type:
        headers["Content-Type"] = "application/json"
    else:
        headers["Accept"] = "application/json"
    cleaned_key = api_key.strip()
    if cleaned_key:
        headers["Authorization"] = f"Bearer {cleaned_key}"
    return headers


def _extract_completion_text(payload: Any, mode: str) -> str:
    if not isinstance(payload, dict):
        raise ValueError("LLM endpoint returned a non-object JSON payload")
    if mode == "ollama":
        message = payload.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
        if isinstance(payload.get("response"), str):
            return payload["response"]
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            msg = first.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                return msg["content"]
    raise ValueError("LLM endpoint response did not include message content")


async def _request_llm_completion_async(
    *,
    mode: str,
    api_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    timeout: float,
    allowed_hosts: set[str] | None = None,
) -> str:
    target_url = _build_validated_llm_target_url(
        api_url,
        mode=mode,
        endpoint="chat",
        allowed_hosts=allowed_hosts,
    )
    headers = _build_headers(api_key, include_content_type=True)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        if mode == "ollama":
            strict_body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.0},
            }
            plain_body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": 0.0},
            }
        else:
            strict_body = {
                "model": model,
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
                "messages": messages,
            }
            plain_body = {
                "model": model,
                "temperature": 0.0,
                "messages": messages,
            }

        response = await client.post(target_url, headers=headers, json=strict_body)
        if response.status_code in {400, 422}:
            response = await client.post(target_url, headers=headers, json=plain_body)
        response.raise_for_status()
        return _extract_completion_text(response.json(), mode)


def _request_llm_completion_sync(
    *,
    mode: str,
    api_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    timeout: float,
    allowed_hosts: set[str] | None = None,
) -> str:
    target_url = _build_validated_llm_target_url(
        api_url,
        mode=mode,
        endpoint="chat",
        allowed_hosts=allowed_hosts,
    )
    headers = _build_headers(api_key, include_content_type=True)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        if mode == "ollama":
            strict_body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.0},
            }
            plain_body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": 0.0},
            }
        else:
            strict_body = {
                "model": model,
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
                "messages": messages,
            }
            plain_body = {
                "model": model,
                "temperature": 0.0,
                "messages": messages,
            }

        response = client.post(target_url, headers=headers, json=strict_body)
        if response.status_code in {400, 422}:
            response = client.post(target_url, headers=headers, json=plain_body)
        response.raise_for_status()
        return _extract_completion_text(response.json(), mode)


def _apply_parsed_rerank(
    candidates: Sequence[dict[str, Any]],
    parsed: dict[str, Any],
    *,
    top_k: int,
    max_delta: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(parsed.get("adjustments"), list):
        return apply_llm_rerank_adjustments(
            candidates, parsed["adjustments"], top_k=top_k, max_delta=max_delta
        )
    if isinstance(parsed.get("rankings"), list):
        return apply_llm_rerank_order(
            candidates, parsed["rankings"], top_k=top_k, max_nudge=max_delta
        )
    return [dict(c) for c in candidates], {"applied": False, "reason": "fallback"}


def _resolve_rerank_runtime(
    *,
    mode: str | None,
    api_url: str | None,
    api_key: str | None,
    model: str | None,
) -> tuple[str, str, str, str]:
    resolved_mode = normalize_llm_rerank_mode(
        mode if mode is not None else settings.llm_rerank_mode
    )
    key = (api_key if api_key is not None else settings.llm_rerank_api_key).strip()
    raw_url = (api_url if api_url is not None else settings.llm_rerank_api_url).strip()
    if resolved_mode == "ollama" and (
        not raw_url or (api_url is None and raw_url == DEFAULT_CLOUD_LLM_URL)
    ):
        raw_url = DEFAULT_OLLAMA_URL
    chosen_model = (model if model is not None else settings.llm_rerank_model).strip()
    return resolved_mode, raw_url, key, chosen_model


async def verify_llm_reranker(
    *,
    mode: str,
    api_url: str,
    api_key: str = "",
    model: str,
    timeout_seconds: float | None = None,
    allowed_hosts: set[str] | None = None,
) -> dict[str, Any]:
    """Verify endpoint allowlist, connectivity, and JSON parsing before saving active reranker changes."""
    resolved_mode = normalize_llm_rerank_mode(mode)
    raw_url = api_url.strip() or (DEFAULT_OLLAMA_URL if resolved_mode == "ollama" else "")
    chosen_model = model.strip()
    if not raw_url or not chosen_model:
        raise ValueError("LLM reranking requires both an API URL and a model")
    validate_llm_rerank_url(raw_url, allowed_hosts)
    if resolved_mode == "cloud" and not api_key.strip():
        raise ValueError("An API key is required when cloud LLM reranking is enabled")

    probe_messages = [
        {
            "role": "system",
            "content": (
                "You are verifying connectivity for a book recommendation reranker. "
                'Return JSON only: {"adjustments": [{"id": 1, "delta": 0.0, "reason": "ok"}]}'
            ),
        },
        {
            "role": "user",
            "content": '{"candidates": [{"id": 1, "title": "Connectivity Probe", "base_score": 80.0}]}',
        },
    ]
    timeout = timeout_seconds if timeout_seconds is not None else min(15.0, settings.llm_rerank_timeout_seconds)
    started = time.perf_counter()
    content = await _request_llm_completion_async(
        mode=resolved_mode,
        api_url=raw_url,
        api_key=api_key,
        model=chosen_model,
        messages=probe_messages,
        timeout=timeout,
        allowed_hosts=allowed_hosts,
    )
    parsed = extract_llm_json(content)
    if "adjustments" in parsed and not isinstance(parsed["adjustments"], list):
        raise ValueError("LLM response field 'adjustments' must be a JSON list")
    return {
        "ok": True,
        "mode": resolved_mode,
        "model": chosen_model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


async def discover_local_llm_models(
    *,
    mode: str,
    api_url: str,
    api_key: str = "",
    timeout_seconds: float = 10.0,
    allowed_hosts: set[str] | None = None,
) -> list[dict[str, str]]:
    """Discover installed models from an allowlisted Ollama or OpenAI-compatible server."""
    resolved_mode = normalize_llm_rerank_mode(mode)
    if resolved_mode not in {"ollama", "openai-compatible"}:
        raise ValueError("Local model discovery is supported for ollama and openai-compatible modes")
    raw_url = api_url.strip() or (DEFAULT_OLLAMA_URL if resolved_mode == "ollama" else "")
    if not raw_url:
        raise ValueError("Enter an endpoint URL to load models")
    target_url = _build_validated_llm_target_url(
        raw_url,
        mode=resolved_mode,
        endpoint="models",
        allowed_hosts=allowed_hosts,
    )
    headers = _build_headers(api_key, include_content_type=False)
    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        response = await client.get(target_url, headers=headers)
        response.raise_for_status()
        payload = response.json()

    if not isinstance(payload, dict):
        raise ValueError("Model discovery endpoint returned an invalid JSON payload")

    raw_items = (
        payload.get("models")
        if resolved_mode == "ollama"
        else (payload.get("data") if isinstance(payload.get("data"), list) else payload.get("models"))
    )
    if not isinstance(raw_items, list):
        raise ValueError("Model discovery endpoint did not return a model list")

    seen: set[str] = set()
    discovered: list[dict[str, str]] = []
    for entry in raw_items:
        if isinstance(entry, str):
            model_id = entry.strip()
            model_name = model_id
        elif isinstance(entry, dict):
            model_id = str(
                entry.get("id") or entry.get("model") or entry.get("name") or ""
            ).strip()
            model_name = str(entry.get("name") or model_id).strip()
        else:
            continue
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        discovered.append({"id": model_id, "name": model_name or model_id})

    discovered.sort(key=lambda item: item["name"].casefold())
    return discovered


async def rerank_with_llm(
    reads: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    *,
    interaction_events: Sequence[dict[str, Any]] | None = None,
    mode: str | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    top_k: int | None = None,
    max_delta: float | None = None,
    timeout_seconds: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Call the configured Ollama, OpenAI-compatible, or cloud LLM endpoint and rerank top-K candidates."""
    try:
        resolved_mode, url, key, chosen_model = _resolve_rerank_runtime(
            mode=mode, api_url=api_url, api_key=api_key, model=model
        )
    except ValueError as exc:
        LOGGER.warning("Invalid LLM reranker mode; falling back to base order: %s", exc)
        return [dict(c) for c in candidates], {"applied": False, "reason": "not_configured"}

    k = top_k if top_k is not None else settings.llm_rerank_top_k
    delta = max_delta if max_delta is not None else settings.llm_rerank_max_delta
    timeout = timeout_seconds if timeout_seconds is not None else settings.llm_rerank_timeout_seconds

    requires_key = resolved_mode == "cloud"
    if (requires_key and not key) or not url or not chosen_model or len(candidates) < 2:
        return [dict(c) for c in candidates], {"applied": False, "reason": "not_configured"}

    messages = build_rerank_prompt(reads, candidates, interaction_events=interaction_events, top_k=k)
    try:
        content = await _request_llm_completion_async(
            mode=resolved_mode,
            api_url=url,
            api_key=key,
            model=chosen_model,
            messages=messages,
            timeout=timeout,
        )
        parsed = extract_llm_json(content)
        return _apply_parsed_rerank(candidates, parsed, top_k=k, max_delta=delta)
    except Exception as exc:
        LOGGER.warning("LLM reranker failed; falling back to base order: %s", exc)
        return [dict(c) for c in candidates], {"applied": False, "reason": "fallback"}


def rerank_with_llm_sync(
    reads: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    *,
    interaction_events: Sequence[dict[str, Any]] | None = None,
    mode: str | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    top_k: int | None = None,
    max_delta: float | None = None,
    timeout_seconds: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Synchronous variant of ``rerank_with_llm`` for sync serving paths."""
    try:
        resolved_mode, url, key, chosen_model = _resolve_rerank_runtime(
            mode=mode, api_url=api_url, api_key=api_key, model=model
        )
    except ValueError as exc:
        LOGGER.warning("Invalid LLM reranker mode; falling back to base order: %s", exc)
        return [dict(c) for c in candidates], {"applied": False, "reason": "not_configured"}

    k = top_k if top_k is not None else settings.llm_rerank_top_k
    delta = max_delta if max_delta is not None else settings.llm_rerank_max_delta
    timeout = timeout_seconds if timeout_seconds is not None else settings.llm_rerank_timeout_seconds

    requires_key = resolved_mode == "cloud"
    if (requires_key and not key) or not url or not chosen_model or len(candidates) < 2:
        return [dict(c) for c in candidates], {"applied": False, "reason": "not_configured"}

    messages = build_rerank_prompt(reads, candidates, interaction_events=interaction_events, top_k=k)
    try:
        content = _request_llm_completion_sync(
            mode=resolved_mode,
            api_url=url,
            api_key=key,
            model=chosen_model,
            messages=messages,
            timeout=timeout,
        )
        parsed = extract_llm_json(content)
        return _apply_parsed_rerank(candidates, parsed, top_k=k, max_delta=delta)
    except Exception as exc:
        LOGGER.warning("LLM reranker failed; falling back to base order: %s", exc)
        return [dict(c) for c in candidates], {"applied": False, "reason": "fallback"}
