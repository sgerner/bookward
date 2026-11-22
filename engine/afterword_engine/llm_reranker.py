"""Opt-in grounded LLM reranker for the top recommendation slate."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

import httpx

from .config import settings

LOGGER = logging.getLogger(__name__)


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


async def rerank_with_llm(
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
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Call the configured OpenAI-compatible LLM endpoint and rerank top-K candidates."""
    key = (api_key if api_key is not None else settings.llm_rerank_api_key).strip()
    url = _normalize_chat_url(api_url if api_url is not None else settings.llm_rerank_api_url)
    chosen_model = (model if model is not None else settings.llm_rerank_model).strip()
    k = top_k if top_k is not None else settings.llm_rerank_top_k
    delta = max_delta if max_delta is not None else settings.llm_rerank_max_delta
    timeout = timeout_seconds if timeout_seconds is not None else settings.llm_rerank_timeout_seconds

    if not key or not url or not chosen_model or len(candidates) < 2:
        return [dict(c) for c in candidates], {"applied": False, "reason": "not_configured"}

    messages = build_rerank_prompt(reads, candidates, interaction_events=interaction_events, top_k=k)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": chosen_model,
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                    "messages": messages,
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            if "adjustments" in parsed and isinstance(parsed["adjustments"], list):
                return apply_llm_rerank_adjustments(candidates, parsed["adjustments"], top_k=k, max_delta=delta)
            rankings = parsed.get("rankings") or []
            return apply_llm_rerank_order(candidates, rankings, top_k=k, max_nudge=delta)
    except Exception as exc:
        LOGGER.warning("LLM reranker failed; falling back to base order: %s", exc)
        return [dict(c) for c in candidates], {"applied": False, "reason": "fallback"}


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
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Synchronous variant of ``rerank_with_llm`` for sync serving paths."""
    key = (api_key if api_key is not None else settings.llm_rerank_api_key).strip()
    url = _normalize_chat_url(api_url if api_url is not None else settings.llm_rerank_api_url)
    chosen_model = (model if model is not None else settings.llm_rerank_model).strip()
    k = top_k if top_k is not None else settings.llm_rerank_top_k
    delta = max_delta if max_delta is not None else settings.llm_rerank_max_delta
    timeout = timeout_seconds if timeout_seconds is not None else settings.llm_rerank_timeout_seconds

    if not key or not url or not chosen_model or len(candidates) < 2:
        return [dict(c) for c in candidates], {"applied": False, "reason": "not_configured"}

    messages = build_rerank_prompt(reads, candidates, interaction_events=interaction_events, top_k=k)
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": chosen_model,
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                    "messages": messages,
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            if "adjustments" in parsed and isinstance(parsed["adjustments"], list):
                return apply_llm_rerank_adjustments(candidates, parsed["adjustments"], top_k=k, max_delta=delta)
            rankings = parsed.get("rankings") or []
            return apply_llm_rerank_order(candidates, rankings, top_k=k, max_nudge=delta)
    except Exception as exc:
        LOGGER.warning("LLM reranker failed; falling back to base order: %s", exc)
        return [dict(c) for c in candidates], {"applied": False, "reason": "fallback"}
