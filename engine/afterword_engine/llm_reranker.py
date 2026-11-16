"""Opt-in grounded LLM listwise reranker for the top recommendation slate."""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

import httpx

from .config import settings

LOGGER = logging.getLogger(__name__)


def build_rerank_prompt(
    reads: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    *,
    top_k: int = 24,
) -> list[dict[str, str]]:
    """Build a grounded listwise reranking prompt from read history and candidates."""
    liked = [
        r for r in reads if r.get("rating") is not None and float(r["rating"]) >= 4.0
    ][-15:]
    disliked = [
        r for r in reads if r.get("rating") is not None and float(r["rating"]) <= 2.0
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

    user_payload = {
        "liked_books": [
            {"title": r.get("title"), "author": r.get("author"), "rating": r.get("rating")}
            for r in liked
        ],
        "disliked_books": [
            {"title": r.get("title"), "author": r.get("author"), "rating": r.get("rating")}
            for r in disliked
        ],
        "candidates": slate,
    }
    return [
        {
            "role": "system",
            "content": (
                "You rerank a fixed slate of candidate books for a reader. "
                "Only use candidate IDs present in `candidates`. Do not invent books. "
                "Return strict JSON: {\"rankings\": [{\"id\": <int>, \"reason\": <short string>}]}. "
                "Order from best fit to worst fit."
            ),
        },
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def apply_llm_rerank_order(
    candidates: Sequence[dict[str, Any]],
    llm_rankings: Sequence[dict[str, Any]],
    *,
    top_k: int = 24,
    max_nudge: float = 4.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply validated LLM listwise ordering with a bounded score nudge."""
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
    }


async def rerank_with_llm(
    reads: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    *,
    api_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    top_k: int | None = None,
    timeout_seconds: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Call the configured OpenAI-compatible LLM endpoint and rerank top-K candidates."""
    key = (api_key if api_key is not None else settings.llm_rerank_api_key).strip()
    url = (api_url if api_url is not None else settings.llm_rerank_api_url).strip()
    chosen_model = (model if model is not None else settings.llm_rerank_model).strip()
    k = top_k if top_k is not None else settings.llm_rerank_top_k
    timeout = timeout_seconds if timeout_seconds is not None else settings.llm_rerank_timeout_seconds

    if not key or not url or not chosen_model or len(candidates) < 2:
        return [dict(c) for c in candidates], {"applied": False, "reason": "not_configured"}

    messages = build_rerank_prompt(reads, candidates, top_k=k)
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
            rankings = parsed.get("rankings") or []
        return apply_llm_rerank_order(candidates, rankings, top_k=k)
    except Exception as exc:
        LOGGER.warning("LLM reranker failed; falling back to base order: %s", exc)
        return [dict(c) for c in candidates], {"applied": False, "reason": "fallback"}


def rerank_with_llm_sync(
    reads: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    *,
    api_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    top_k: int | None = None,
    timeout_seconds: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Synchronous variant of ``rerank_with_llm`` for sync serving paths."""
    key = (api_key if api_key is not None else settings.llm_rerank_api_key).strip()
    url = (api_url if api_url is not None else settings.llm_rerank_api_url).strip()
    chosen_model = (model if model is not None else settings.llm_rerank_model).strip()
    k = top_k if top_k is not None else settings.llm_rerank_top_k
    timeout = timeout_seconds if timeout_seconds is not None else settings.llm_rerank_timeout_seconds

    if not key or not url or not chosen_model or len(candidates) < 2:
        return [dict(c) for c in candidates], {"applied": False, "reason": "not_configured"}

    messages = build_rerank_prompt(reads, candidates, top_k=k)
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
            rankings = parsed.get("rankings") or []
        return apply_llm_rerank_order(candidates, rankings, top_k=k)
    except Exception as exc:
        LOGGER.warning("LLM reranker failed; falling back to base order: %s", exc)
        return [dict(c) for c in candidates], {"applied": False, "reason": "fallback"}
