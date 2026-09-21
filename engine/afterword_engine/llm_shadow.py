"""Asynchronous shadow ranking runs.

This module records model output for evaluation only.  It never updates
``candidates.score`` or ``candidates.status`` and therefore cannot alter the
visible recommendation order.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from .config import settings
from .database import row, rows, transaction
from .llm import build_client, build_prompt
from .secrets import unseal


DEFAULT_SHADOW_TOP_K = 20
DEFAULT_SHADOW_PROMPT_VERSION = "shadow-v1"


def safe_connections() -> list[dict[str, Any]]:
    return rows(
        "SELECT id,name,provider_id,model_id,endpoint,auth_type,enabled,"
        "last_status,last_error,last_used_at,created_at,updated_at "
        "FROM llm_connections ORDER BY updated_at DESC,id DESC"
    )


def _backfill_default_shadow_policies() -> None:
    """Repair connections created before automatic policy provisioning."""

    for connection in rows(
        "SELECT c.id FROM llm_connections c "
        "WHERE NOT EXISTS (SELECT 1 FROM llm_policies p WHERE p.connection_id=c.id)"
    ):
        ensure_default_shadow_policy(int(connection["id"]))


def safe_policies() -> list[dict[str, Any]]:
    _backfill_default_shadow_policies()
    return rows(
        "SELECT p.id,p.name,p.connection_id,p.enabled,p.top_k,p.prompt_version,"
        "p.created_at,p.updated_at,c.name connection_name,c.provider_id,c.model_id "
        "FROM llm_policies p JOIN llm_connections c ON c.id=p.connection_id "
        "ORDER BY p.updated_at DESC,p.id DESC"
    )


def ensure_default_shadow_policy(connection_id: int, *, name: str | None = None) -> dict[str, Any]:
    """Ensure a connection has one enabled default shadow policy.

    Connections are usable as soon as they are saved.  Keeping this operation
    idempotent makes it safe to call after a normal connection save and after
    subscription authentication completes (including repeated device-login
    status polls).
    """

    connection = row("SELECT id,name FROM llm_connections WHERE id=?", (connection_id,))
    if not connection:
        raise ValueError("LLM connection not found")
    policy_name = (name or f"{connection['name']} shadow ranking").strip()[:100]
    with transaction() as con:
        existing = con.execute(
            "SELECT id,name,connection_id,enabled,top_k,prompt_version,created_at,updated_at "
            "FROM llm_policies WHERE connection_id=? ORDER BY id LIMIT 1",
            (connection_id,),
        ).fetchone()
        if existing:
            return dict(existing)
        cursor = con.execute(
            "INSERT INTO llm_policies(name,connection_id,enabled,top_k,prompt_version) VALUES(?,?,?,?,?)",
            (policy_name, connection_id, 1, DEFAULT_SHADOW_TOP_K, DEFAULT_SHADOW_PROMPT_VERSION),
        )
        policy_id = cursor.lastrowid
    return row(
        "SELECT id,name,connection_id,enabled,top_k,prompt_version,created_at,updated_at "
        "FROM llm_policies WHERE id=?",
        (policy_id,),
    )


def _candidate_rows(top_k: int) -> list[dict[str, Any]]:
    return rows(
        "SELECT c.*,s.name source_name FROM candidates c "
        "LEFT JOIN sources s ON s.id=c.source_id "
        "JOIN candidate_quality q ON q.candidate_id=c.id "
        "WHERE c.status='recommended' AND q.quality_status='accepted' "
        "AND (s.id IS NULL OR s.enabled=1) "
        "AND NOT EXISTS (SELECT 1 FROM reads r "
        "WHERE book_identity_matches(c.title,c.author,r.title,r.author)) "
        "ORDER BY c.score DESC,c.id ASC LIMIT ?",
        (top_k,),
    )


def _read_rows() -> list[dict[str, Any]]:
    return rows(
        "SELECT id,title,author,rating,read_at FROM reads "
        "WHERE rating BETWEEN 1 AND 5 ORDER BY COALESCE(read_at,created_at) DESC,id DESC "
        "LIMIT ?",
        (max(1, settings.llm_shadow_max_reads),),
    )


def _hash_payload(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mark_run(run_id: str, *, status: str, error: str | None = None, latency_ms: int | None = None, input_tokens: int | None = None, output_tokens: int | None = None):
    with transaction() as con:
        con.execute(
            "UPDATE llm_runs SET status=?,error=?,latency_ms=?,input_tokens=?,output_tokens=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, error[:2000] if error else None, latency_ms, input_tokens, output_tokens, run_id),
        )


async def run_shadow_policy(policy_id: int) -> dict[str, Any]:
    policy = row(
        "SELECT p.*,c.provider_id,c.model_id,c.endpoint,c.auth_type,c.secret,c.enabled connection_enabled "
        "FROM llm_policies p JOIN llm_connections c ON c.id=p.connection_id WHERE p.id=?",
        (policy_id,),
    )
    if not policy:
        raise ValueError("Shadow policy not found")
    if not policy["enabled"] or not policy["connection_enabled"]:
        return {"status": "skipped", "policy_id": policy_id, "reason": "disabled"}
    candidates = _candidate_rows(max(1, min(100, int(policy["top_k"]))))
    if not candidates:
        return {"status": "skipped", "policy_id": policy_id, "reason": "no_candidates"}
    reads = _read_rows()
    prompt = build_prompt(reads, candidates)
    candidate_hash = _hash_payload([{"id": item["id"], "score": item["score"], "title": item["title"], "author": item["author"], "description": item["description"], "genres": item["genres"]} for item in candidates])
    request_hash = _hash_payload({"policy": policy["id"], "prompt_version": policy["prompt_version"], "provider": policy["provider_id"], "model": policy["model_id"], "prompt": prompt})
    cached = row(
        "SELECT id FROM llm_runs WHERE request_hash=? AND status='complete' ORDER BY created_at DESC LIMIT 1",
        (request_hash,),
    )
    if cached:
        return {"status": "cached", "policy_id": policy_id, "run_id": cached["id"], "candidate_count": len(candidates)}

    run_id = str(uuid.uuid4())
    with transaction() as con:
        con.execute(
            "INSERT INTO llm_runs(id,policy_id,connection_id,request_hash,candidate_hash,status,candidate_count) VALUES(?,?,?,?,?,?,?)",
            (run_id, policy_id, policy["connection_id"], request_hash, candidate_hash, "running", len(candidates)),
        )
    started = time.perf_counter()
    try:
        secret = unseal(policy["secret"]) if policy["secret"] else ""
        client = build_client(
            policy["provider_id"],
            policy["model_id"],
            policy["endpoint"],
            secret,
            auth_type=policy["auth_type"],
        )
        result = await client.rank(prompt, {int(item["id"]) for item in candidates})
        latency_ms = round((time.perf_counter() - started) * 1000)
        with transaction() as con:
            for rank, item in enumerate(result.rankings, start=1):
                con.execute(
                    "INSERT INTO llm_scores(run_id,candidate_id,rank,score,confidence,reason_codes) VALUES(?,?,?,?,?,?)",
                    (run_id, item["candidate_id"], rank, item["score"], item["confidence"], json.dumps(item["reason_codes"], separators=(",", ":"))),
                )
            con.execute(
                "UPDATE llm_runs SET status='complete',latency_ms=?,input_tokens=?,output_tokens=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
                (latency_ms, result.input_tokens, result.output_tokens, run_id),
            )
            con.execute(
                "UPDATE llm_connections SET last_status='ok',last_error=NULL,last_used_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (policy["connection_id"],),
            )
        return {"status": "complete", "policy_id": policy_id, "run_id": run_id, "candidate_count": len(candidates), "latency_ms": latency_ms}
    except Exception as exc:
        latency_ms = round((time.perf_counter() - started) * 1000)
        message = str(exc)[:2000]
        _mark_run(run_id, status="failed", error=message, latency_ms=latency_ms)
        with transaction() as con:
            con.execute(
                "UPDATE llm_connections SET last_status='error',last_error=?,last_used_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (message, policy["connection_id"]),
            )
        return {"status": "failed", "policy_id": policy_id, "run_id": run_id, "error": message, "latency_ms": latency_ms}
