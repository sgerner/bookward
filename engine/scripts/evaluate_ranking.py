#!/usr/bin/env python3
"""Evaluate rating ranking from a private JSON export or read-only SQLite snapshot.

Prints aggregate metrics only. No network requests or database writes.
"""
import argparse
import base64
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import sqlite3
import sys
import warnings

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afterword_engine.embeddings import content_hash
from afterword_engine.identity import ReadIdentityIndex, book_identity
from afterword_engine.ingestion import read_identity_rows_with_current_verified_work_ids
from afterword_engine.scoring import document


HISTORY_MODES = ("expanding", "frozen-prefix")


def read_time(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif not isinstance(value, str):
        return None
    else:
        result = None
    try:
        if result is None:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            result = datetime.strptime(value, "%Y/%m/%d")
        except ValueError:
            try:
                result = parsedate_to_datetime(value)
            except (ValueError, TypeError, AttributeError, IndexError):
                return None
    return result.replace(tzinfo=result.tzinfo or timezone.utc).astimezone(timezone.utc)


def _catalog_confidence_from_quality(quality_score, quality_status):
    try:
        score = float(quality_score) if quality_score is not None else 0.0
    except (TypeError, ValueError):
        score = 0.0
    if score > 0:
        return score
    if str(quality_status or "") == "accepted":
        return 0.85
    return 0.0


def _normalize_loaded_corpus(data, *, has_sources_table=False, has_quality_table=False):
    normalized = dict(data)
    reads = [dict(r) for r in normalized.get("reads", [])]
    read_metadata = [dict(r) for r in normalized.get("read_metadata", [])]
    field_provenance = [dict(r) for r in normalized.get("metadata_field_provenance", [])]
    sources_by_id = {
        row["id"]: dict(row)
        for row in normalized.get("sources", [])
        if isinstance(row, dict) and "id" in row
    }
    quality_by_candidate = {
        row["candidate_id"]: dict(row)
        for row in normalized.get("candidate_quality", [])
        if isinstance(row, dict) and "candidate_id" in row
    }
    has_sources = has_sources_table or ("sources" in normalized)
    has_quality = has_quality_table or ("candidate_quality" in normalized)

    candidates = []
    for raw in normalized.get("candidates", []):
        item = dict(raw)
        if has_sources and "source_id" in item:
            src = sources_by_id.get(item.get("source_id"))
            if "source_name" not in item:
                item["source_name"] = src.get("name") if src else None
            if "source_weight" not in item:
                item["source_weight"] = src.get("weight") if src else None
        if has_quality and "id" in item:
            qual = quality_by_candidate.get(item.get("id"))
            for target_key, qual_key in (
                ("quality_work_id", "work_id"),
                ("quality_provider", "provider"),
                ("quality_isbn13", "isbn13"),
                ("quality_isbn10", "isbn10"),
                ("quality_score", "quality_score"),
                ("quality_status", "quality_status"),
                ("metadata_confidence", "metadata_confidence"),
            ):
                if target_key not in item:
                    item[target_key] = qual.get(qual_key) if qual else None
            if "catalog_confidence" not in item:
                item["catalog_confidence"] = (
                    _catalog_confidence_from_quality(
                        qual.get("quality_score") if qual else None,
                        qual.get("quality_status") if qual else None,
                    )
                )
        elif (
            "catalog_confidence" not in item
            and (item.get("quality_score") is not None or item.get("quality_status") is not None)
        ):
            item["catalog_confidence"] = _catalog_confidence_from_quality(
                item.get("quality_score"),
                item.get("quality_status"),
            )
        candidates.append(item)

    events_by_key = {
        str(row.get("event_key")): dict(row)
        for row in normalized.get("recommendation_events", [])
        if isinstance(row, dict) and row.get("event_key")
    }
    impressions_by_run_candidate = {
        (str(row.get("run_id")), row.get("candidate_id")): dict(row)
        for row in normalized.get("recommendation_impressions", [])
        if isinstance(row, dict) and row.get("run_id") is not None and "candidate_id" in row
    }
    feedback = []
    for raw in normalized.get("feedback", []):
        event = dict(raw)
        linked_event = events_by_key.get(f"feedback:{event.get('id')}")
        if linked_event is not None and event.get("run_id") is None:
            event["run_id"] = linked_event.get("run_id")
        run_id = event.get("run_id")
        if run_id is not None and "candidate_id" in event:
            imp = impressions_by_run_candidate.get((str(run_id), event.get("candidate_id")))
            if imp is not None:
                for target_key, imp_key in (
                    ("impression_id", "id"),
                    ("impression_rank", "rank"),
                    ("impression_score", "score"),
                    ("presented_at", "presented_at"),
                    ("visible_at", "visible_at"),
                ):
                    if event.get(target_key) is None:
                        event[target_key] = imp.get(imp_key)
        feedback.append(event)

    embeddings = []
    for raw in normalized.get("embeddings", []):
        entry = dict(raw)
        vector = entry.get("vector")
        if isinstance(vector, str):
            entry["vector"] = base64.b64decode(vector, validate=True)
        elif isinstance(vector, (bytes, bytearray, memoryview)):
            entry["vector"] = bytes(vector)
        embeddings.append(entry)

    normalized["reads"] = reads
    normalized["candidates"] = candidates
    normalized["feedback"] = feedback
    normalized["embeddings"] = embeddings
    normalized["read_metadata"] = read_metadata
    normalized["metadata_field_provenance"] = field_provenance
    return normalized


def load_corpus(path):
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text())
        return _normalize_loaded_corpus(data)
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        con.execute("BEGIN")
        tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

        def columns_for(table):
            if table not in tables:
                return set()
            return {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}

        cand_cols = columns_for("candidates")
        source_cols = columns_for("sources")
        quality_cols = columns_for("candidate_quality")
        feedback_cols = columns_for("feedback")
        event_cols = columns_for("recommendation_events")
        impression_cols = columns_for("recommendation_impressions")
        embedding_cols = columns_for("embeddings")

        has_sources_join = "sources" in tables and "source_id" in cand_cols and "id" in source_cols
        has_quality_join = (
            "candidate_quality" in tables
            and "id" in cand_cols
            and "candidate_id" in quality_cols
        )

        if "candidates" in tables:
            select_parts = ["c.*"]
            joins = []
            if has_sources_join:
                joins.append("LEFT JOIN sources s ON s.id = c.source_id")
                if "source_name" not in cand_cols:
                    select_parts.append(
                        "s.name AS source_name" if "name" in source_cols else "NULL AS source_name"
                    )
                if "source_weight" not in cand_cols:
                    select_parts.append(
                        "s.weight AS source_weight"
                        if "weight" in source_cols
                        else "NULL AS source_weight"
                    )
            if has_quality_join:
                joins.append("LEFT JOIN candidate_quality q ON q.candidate_id = c.id")
                for alias, col in (
                    ("quality_work_id", "work_id"),
                    ("quality_provider", "provider"),
                    ("quality_isbn13", "isbn13"),
                    ("quality_isbn10", "isbn10"),
                    ("quality_score", "quality_score"),
                    ("quality_status", "quality_status"),
                    ("metadata_confidence", "metadata_confidence"),
                ):
                    if alias not in cand_cols:
                        select_parts.append(
                            f"q.{col} AS {alias}" if col in quality_cols else f"NULL AS {alias}"
                        )
                if "catalog_confidence" not in cand_cols:
                    if {"quality_score", "quality_status"} <= quality_cols:
                        select_parts.append(
                            "CASE WHEN q.quality_score > 0 THEN q.quality_score "
                            "WHEN q.quality_status = 'accepted' THEN 0.85 ELSE 0.0 END "
                            "AS catalog_confidence"
                        )
                    elif "quality_score" in quality_cols:
                        select_parts.append(
                            "CASE WHEN q.quality_score > 0 THEN q.quality_score ELSE 0.0 END "
                            "AS catalog_confidence"
                        )
                    else:
                        select_parts.append("0.0 AS catalog_confidence")
            cand_sql = f"SELECT {', '.join(select_parts)} FROM candidates c {' '.join(joins)}"
            candidates = [dict(r) for r in con.execute(cand_sql)]
        else:
            candidates = []

        if "feedback" in tables:
            fb_parts = ["f.*"]
            fb_joins = []
            if (
                "recommendation_events" in tables
                and "id" in feedback_cols
                and {"event_key", "run_id"} <= event_cols
            ):
                fb_joins.append(
                    "LEFT JOIN recommendation_events e ON e.event_key = ('feedback:' || f.id)"
                )
                if "run_id" not in feedback_cols:
                    fb_parts.append("e.run_id AS run_id")
                if (
                    "recommendation_impressions" in tables
                    and "candidate_id" in feedback_cols
                    and {"run_id", "candidate_id"} <= impression_cols
                ):
                    fb_joins.append(
                        "LEFT JOIN recommendation_impressions i "
                        "ON i.run_id = e.run_id AND i.candidate_id = f.candidate_id"
                    )
                    for alias, col in (
                        ("impression_id", "id"),
                        ("impression_rank", "rank"),
                        ("impression_score", "score"),
                        ("presented_at", "presented_at"),
                        ("visible_at", "visible_at"),
                    ):
                        if alias not in feedback_cols:
                            fb_parts.append(
                                f"i.{col} AS {alias}"
                                if col in impression_cols
                                else f"NULL AS {alias}"
                            )
            fb_sql = f"SELECT {', '.join(fb_parts)} FROM feedback f {' '.join(fb_joins)}"
            feedback = [dict(r) for r in con.execute(fb_sql)]
        else:
            feedback = []

        if "embeddings" in tables:
            if "entity_type" in embedding_cols:
                embeddings = [
                    dict(r)
                    for r in con.execute(
                        "SELECT * FROM embeddings WHERE entity_type IN ('read','candidate')"
                    )
                ]
            else:
                embeddings = [dict(r) for r in con.execute("SELECT * FROM embeddings")]
        else:
            embeddings = []

        raw_data = {
            # Preserve row-availability time and verified library metadata across
            # snapshots and exports. Imported read_at dates do not imply when a row
            # entered the database.
            "reads": [dict(r) for r in con.execute("SELECT * FROM reads")] if "reads" in tables else [],
            "candidates": candidates,
            "feedback": feedback,
            "embeddings": embeddings,
            "read_metadata": (
                [dict(r) for r in con.execute("SELECT * FROM read_metadata")]
                if "read_metadata" in tables
                else []
            ),
            "metadata_field_provenance": (
                [dict(r) for r in con.execute("SELECT * FROM metadata_field_provenance")]
                if "metadata_field_provenance" in tables
                else []
            ),
        }
        return _normalize_loaded_corpus(
            raw_data,
            has_sources_table=has_sources_join,
            has_quality_table=has_quality_join,
        )


def prepare(data, backend, model):
    pairs = {(e["backend"], e["model"]) for e in data["embeddings"]
             if e["entity_type"] == "read"
             and (not backend or e["backend"] == backend)
             and (not model or e["model"] == model)}
    if len(pairs) != 1:
        raise ValueError("Select one read embedding cache using --backend and --model")
    selected = pairs.pop()
    cache = {e["entity_id"]: e for e in data["embeddings"]
             if e["entity_type"] == "read" and (e["backend"], e["model"]) == selected}
    projected_reads = read_identity_rows_with_current_verified_work_ids(
        data.get("reads", []),
        data.get("read_metadata", []),
    )
    valid = []
    excluded = {"unrated": 0, "undated": 0, "missing_or_stale_vector": 0, "duplicate_work": 0}
    for item in projected_reads:
        if item.get("rating") is None or not 1 <= float(item["rating"]) <= 5:
            excluded["unrated"] += 1
            continue
        date = read_time(item.get("read_at"))
        if date is None:
            excluded["undated"] += 1
            continue
        entry = cache.get(item["id"])
        if not entry or entry["content_hash"] != content_hash(document(item)):
            excluded["missing_or_stale_vector"] += 1
            continue
        vector = np.frombuffer(entry["vector"], dtype=np.float32)
        if len(vector) != entry["dimensions"] or not len(vector) or not np.isfinite(vector).all():
            raise ValueError("Invalid cached vector; rebuild the cache before evaluation")
        valid.append((date, item, vector))
    valid.sort(key=lambda record: (record[0], record[1]["id"]))
    seen_match_keys = set()
    seen_title_keys = set()
    seen_unknown_author_title_keys = set()
    seen_index = ReadIdentityIndex(
        seen_match_keys,
        seen_title_keys,
        seen_unknown_author_title_keys,
    )
    records = []
    for date, item, vector in valid:
        if seen_index.matches(item):
            excluded["duplicate_work"] += 1
            continue
        single = ReadIdentityIndex.from_reads([item])
        seen_match_keys.update(single.match_keys)
        seen_title_keys.update(single.title_keys)
        seen_unknown_author_title_keys.update(single.unknown_author_title_keys)
        records.append((date, item, vector))
    if len(records) < 100:
        raise ValueError("At least 100 dated, distinct rated works with valid vectors are required")
    return records, selected, excluded


def _exact_count_or_float(value):
    rounded = round(float(value))
    return int(rounded) if abs(float(value) - rounded) <= 1e-12 else float(value)


def tail_metrics_at_k(scores, values, k=20, *, lowest=False):
    """Compute order-invariant tie-aware (expected_sum, expected_mean) for top/bottom-k."""
    scores_arr = np.asarray(scores, dtype=np.float64)
    values_arr = np.asarray(values, dtype=np.float64)
    if len(scores_arr) != len(values_arr):
        raise ValueError("scores and values must have equal lengths")
    n = len(scores_arr)
    m = min(int(k), n)
    if m <= 0:
        return 0, None
    key = -scores_arr if lowest else scores_arr
    threshold = float(np.partition(key, n - m)[n - m])
    above = key > threshold
    tied = key == threshold
    n_above = int(above.sum())
    n_tied = int(tied.sum())
    tie_weight = (m - n_above) / n_tied
    total = float(values_arr[above].sum() + tie_weight * values_arr[tied].sum())
    return _exact_count_or_float(total), float(total / m)


def ndcg_at_k(scores, gains, k=20):
    """Compute order-invariant tie-aware NDCG@k by averaging discounts across tied scores."""
    scores_arr = np.asarray(scores, dtype=np.float64)
    gains_arr = np.asarray(gains, dtype=np.float64)
    if len(scores_arr) != len(gains_arr):
        raise ValueError("scores and gains must have equal lengths")
    n = len(scores_arr)
    m = min(int(k), n)
    if m <= 0:
        return None
    discounts = np.zeros(n, dtype=np.float64)
    discounts[:m] = 1.0 / np.log2(np.arange(2, m + 2, dtype=np.float64))
    ideal = float(np.sort(gains_arr)[::-1][:m] @ discounts[:m])
    if ideal <= 0:
        return None
    order = np.argsort(-scores_arr, kind="mergesort")
    sorted_scores = scores_arr[order]
    sorted_gains = gains_arr[order]
    dcg = 0.0
    start = 0
    while start < m:
        stop = start + 1
        while stop < n and sorted_scores[stop] == sorted_scores[start]:
            stop += 1
        avg_discount = float(discounts[start:stop].sum() / (stop - start))
        dcg += avg_discount * float(sorted_gains[start:stop].sum())
        start = stop
    return float(dcg / ideal)


def auc(labels, scores):
    labels = np.asarray(labels, dtype=bool)
    scores = np.asarray(scores, dtype=np.float64)
    positive, negative = scores[labels], scores[~labels]
    if not len(positive) or not len(negative):
        return None
    return float(((positive[:, None] > negative).sum()
                  + .5 * (positive[:, None] == negative).sum()) / (len(positive) * len(negative)))


def metrics(labels, scores):
    labels_arr = np.asarray(labels, dtype=bool)
    scores_arr = np.asarray(scores, dtype=np.float64)
    _, precision_at_20 = tail_metrics_at_k(scores_arr, labels_arr, k=20, lowest=False)
    return {
        "auc": auc(labels_arr, scores_arr),
        "precision_at_20": precision_at_20,
        "ndcg_at_20": ndcg_at_k(scores_arr, labels_arr.astype(np.float64), k=20),
    }


def baseline(reads, vectors, candidates, candidate_vectors):
    # The original production formula, including its unnormalized author comparison.
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    history = vectors / np.maximum(norms, 1e-12)
    query = candidate_vectors / np.maximum(np.linalg.norm(candidate_vectors, axis=1, keepdims=True), 1e-12)
    similarities = query @ history.T
    positive = np.array([r["rating"] >= 4 for r in reads])
    negative = np.array([r["rating"] <= 2 for r in reads])
    best_positive = similarities[:, positive].max(axis=1) if positive.any() else np.full(len(candidates), .25)
    best_negative = similarities[:, negative].max(axis=1) if negative.any() else np.zeros(len(candidates))
    authors = {r["author"].casefold() for r in reads if r["rating"] >= 4}
    bonus = np.array([7 if c["author"].casefold() in authors else 0 for c in candidates])
    return np.round(np.clip(42 + 48 * best_positive - 24 * best_negative + bonus, 0, 100), 1)


def assess_candidate_provenance(candidates, *, strict=False):
    """Warn or fail closed when catalog provenance is absent on evaluated candidates."""
    rows = list(candidates)
    total = len(rows)
    with_source_weight = sum(item.get("source_weight") is not None for item in rows)
    with_catalog_confidence = sum(
        item.get("catalog_confidence") is not None or item.get("quality_score") is not None
        for item in rows
    )
    missing_source_weight = total - with_source_weight
    missing_catalog_confidence = total - with_catalog_confidence
    warnings_list = []
    if missing_catalog_confidence > 0:
        msg = (
            f"{missing_catalog_confidence}/{total} evaluated candidates lack catalog_confidence "
            "and quality_score; rank_candidates defaults missing catalog provenance to full "
            "metadata_confidence=1.0 without shrinkage."
        )
        warnings_list.append(msg)
    if missing_source_weight > 0:
        msg = (
            f"{missing_source_weight}/{total} evaluated candidates lack source_weight; "
            "rank_candidates defaults missing source_weight to 1.0."
        )
        warnings_list.append(msg)
    if warnings_list:
        joined = " ".join(warnings_list)
        if strict:
            raise ValueError(f"Missing required candidate catalog provenance: {joined}")
        warnings.warn(joined, UserWarning, stacklevel=2)
    return {
        "usable_candidates": total,
        "with_source_weight": with_source_weight,
        "missing_source_weight": missing_source_weight,
        "with_catalog_confidence_or_quality_score": with_catalog_confidence,
        "missing_catalog_confidence": missing_catalog_confidence,
        "status": "ok" if not warnings_list else "missing_catalog_provenance_warning",
        "warnings": warnings_list,
    }


def prepare_feedback_streaks(data):
    """Collapse unbroken feedback streaks per candidate, keeping the earliest timestamp.

    Repeat events of the same action (such as download_librarr inserting a second
    ``save`` row) retain the initial decision time and initial run linkage so later
    library imports cannot leak into the history available when the user decided.
    """
    normalized = _normalize_loaded_corpus(data) if "feedback" in data else {"feedback": []}
    dated_events = []
    for index, raw in enumerate(normalized.get("feedback", [])):
        if raw.get("undone_at"):
            continue
        when = read_time(raw.get("created_at"))
        if when is None:
            continue
        sort_id = raw.get("id", index)
        try:
            sort_id = int(sort_id)
        except (TypeError, ValueError):
            sort_id = index
        dated_events.append((when, sort_id, dict(raw)))
    dated_events.sort(key=lambda item: (item[0], item[1]))

    active = {}
    repeat_events_collapsed = 0
    candidates_with_collapsed_repeats = set()
    for when, _sort_id, event in dated_events:
        candidate_id = event.get("candidate_id")
        previous = active.get(candidate_id)
        if previous is not None and previous[1].get("action") == event.get("action"):
            repeat_events_collapsed += 1
            candidates_with_collapsed_repeats.add(candidate_id)
            continue
        active[candidate_id] = (when, event)

    return active, {
        "distinct_candidates_with_feedback": len(active),
        "repeat_events_collapsed": repeat_events_collapsed,
        "candidates_with_collapsed_repeats": len(candidates_with_collapsed_repeats),
    }


def same_work_matches(read_item, candidate_item):
    """Return whether a read row and candidate row identify the same work/edition."""
    return ReadIdentityIndex.from_reads([read_item]).matches(candidate_item)


def feedback_history_available_at(records, when, candidate, *, allow_missing_created_at=False):
    """Select unrelated prior-day reads whose rows existed by the action time.

    Missing or malformed ``created_at`` is unknown and excluded by default. The
    returned counters describe only rows that would otherwise qualify by read date
    and work identity across editions, so callers can report the cost of this gate.
    """
    prior = []
    counts = {
        "available": 0,
        "unknown_created_at": 0,
        "created_after_action": 0,
        "same_day_read_date": 0,
        "same_identity": 0,
    }
    for read_date, item, vector in records:
        day = read_date.date() if isinstance(read_date, datetime) else read_date
        if day >= when.date():
            if day == when.date():
                counts["same_day_read_date"] += 1
            continue
        if same_work_matches(item, candidate):
            counts["same_identity"] += 1
            continue
        raw_created = item.get("created_at")
        created_at = read_time(raw_created)
        if created_at is None:
            if allow_missing_created_at and (raw_created is None or str(raw_created).strip() == ""):
                prior.append((item, vector))
            else:
                counts["unknown_created_at"] += 1
            continue
        if created_at > when:
            counts["created_after_action"] += 1
            continue
        prior.append((item, vector))
    counts["available"] = len(prior)
    return prior, counts


def summarize_same_run_cohort(entries, score_Keys, metric_fn):
    """Evaluate same-run feedback slates strictly within each run_id without cross-run pooling."""
    by_run = defaultdict(list)
    for row in entries:
        run_id = row.get("run_id")
        if run_id is not None and str(run_id).strip() != "":
            by_run[str(run_id)].append(row)

    def _evaluate_subset(run_groups):
        action_count = sum(len(rows) for rows in run_groups.values())
        saves = sum(int(bool(row["saved"])) for rows in run_groups.values() for row in rows)
        rejects = action_count - saves
        mixed_runs = {
            run_id: rows
            for run_id, rows in run_groups.items()
            if 0 < sum(int(bool(r["saved"])) for r in rows) < len(rows)
        }
        by_variant = {}
        for key in score_Keys:
            per_run_aucs = []
            concordant = 0.0
            total_pairs = 0
            per_run_metrics = []
            for rows in run_groups.values():
                labels = np.asarray([bool(r["saved"]) for r in rows], dtype=bool)
                scores = np.asarray([float(r["scores"][key]) for r in rows], dtype=np.float64)
                per_run_metrics.append(metric_fn(labels, scores))
                pos = scores[labels]
                neg = scores[~labels]
                if len(pos) and len(neg):
                    pairs = len(pos) * len(neg)
                    wins = float((pos[:, None] > neg).sum() + 0.5 * (pos[:, None] == neg).sum())
                    per_run_aucs.append(wins / pairs)
                    concordant += wins
                    total_pairs += pairs
            by_variant[key] = {
                "within_run_macro_auc": (
                    float(np.mean(per_run_aucs)) if per_run_aucs else None
                ),
                "within_run_pair_weighted_auc": (
                    float(concordant / total_pairs) if total_pairs else None
                ),
                "runs_evaluated": len(run_groups),
                "mixed_label_runs": len(mixed_runs),
                "within_run_pair_count": int(total_pairs),
                "mean_run_top20_precision": (
                    float(
                        np.mean([
                            m["precision_at_20"]
                            for m in per_run_metrics
                            if m.get("precision_at_20") is not None
                        ])
                    )
                    if any(m.get("precision_at_20") is not None for m in per_run_metrics)
                    else None
                ),
            }
        return {
            "runs": len(run_groups),
            "mixed_label_runs": len(mixed_runs),
            "usable": action_count,
            "saves": saves,
            "rejects": rejects,
            "metrics": by_variant,
        }

    visible_by_run = {
        run_id: [r for r in rows if r.get("visible_at")]
        for run_id, rows in by_run.items()
    }
    visible_by_run = {run_id: rows for run_id, rows in visible_by_run.items() if rows}
    return {
        "exact_same_run": _evaluate_subset(by_run),
        "visible_same_run": _evaluate_subset(visible_by_run),
    }


def _cohort_binary_summary(entries, *, include_baseline):
    saves = sum(int(bool(r["saved"])) for r in entries)
    usable = len(entries)
    if not usable:
        summary = {
            "usable_candidates": 0,
            "saved": 0,
            "rejected": 0,
            "neighborhood": None,
        }
        if include_baseline:
            summary["baseline"] = None
        return summary
    labels = np.asarray([bool(r["saved"]) for r in entries], dtype=bool)
    new_scores = np.asarray([float(r["scores"]["neighborhood"]) for r in entries], dtype=np.float64)
    summary = {
        "usable_candidates": usable,
        "saved": saves,
        "rejected": usable - saves,
        "neighborhood": metrics(labels, new_scores),
    }
    if include_baseline:
        old_scores = np.asarray([float(r["scores"]["baseline"]) for r in entries], dtype=np.float64)
        summary["baseline"] = metrics(labels, old_scores)
    return summary


def feedback_check(data, records, selected, *, strict_provenance=False):
    """Separate shortlist-intent diagnostic stratified by history regime without pooling."""
    from afterword_engine.ranking import rank_candidates

    candidates = {c["id"]: c for c in data.get("candidates", [])}
    cache = {e["entity_id"]: e for e in data["embeddings"]
             if e["entity_type"] == "candidate" and (e["backend"], e["model"]) == selected}
    streaks, streak_info = prepare_feedback_streaks(data)
    any_known_created_at = any(
        read_time(item.get("created_at")) is not None for _, item, _ in records
    )
    read_dimensions = int(records[0][2].shape[0]) if records else 0

    cold_start_entries = []
    history_backed_entries = []
    all_entries = []
    evaluated_candidates = []

    for candidate_id, (when, event) in streaks.items():
        item, entry = candidates.get(candidate_id), cache.get(candidate_id)
        if event.get("action") not in {"save", "reject"} or when is None:
            continue
        if not item or not entry or entry.get("content_hash") != content_hash(document(item)):
            continue
        query = np.frombuffer(entry["vector"], dtype=np.float32)[None, :]
        if (
            not query.shape[1]
            or not np.isfinite(query).all()
            or (read_dimensions and query.shape[1] != read_dimensions)
        ):
            continue
        prior, _counts = feedback_history_available_at(
            records,
            when,
            item,
            allow_missing_created_at=not any_known_created_at,
        )
        evaluated_candidates.append(item)
        saved = event["action"] == "save"
        if prior:
            history = [r for r, _ in prior]
            vectors = np.stack([v for _, v in prior])
            old_score = float(baseline(history, vectors, [item], query)[0])
            new_score = float(rank_candidates(history, vectors, [item], query)[0]["score"])
            row = {
                "candidate_id": candidate_id,
                "saved": saved,
                "when": when,
                "run_id": event.get("run_id"),
                "visible_at": event.get("visible_at"),
                "scores": {"baseline": old_score, "neighborhood": new_score},
            }
            history_backed_entries.append(row)
            all_entries.append(row)
        else:
            empty_vectors = np.empty((0, query.shape[1]), dtype=np.float32)
            new_score = float(rank_candidates([], empty_vectors, [item], query)[0]["score"])
            row = {
                "candidate_id": candidate_id,
                "saved": saved,
                "when": when,
                "run_id": event.get("run_id"),
                "visible_at": event.get("visible_at"),
                "scores": {"neighborhood": new_score},
            }
            cold_start_entries.append(row)
            all_entries.append(row)

    if not all_entries:
        return None

    provenance = assess_candidate_provenance(evaluated_candidates, strict=strict_provenance)
    history_summary = _cohort_binary_summary(history_backed_entries, include_baseline=True)
    cold_summary = _cohort_binary_summary(cold_start_entries, include_baseline=False)
    same_run_summary = summarize_same_run_cohort(
        all_entries,
        ("neighborhood",),
        metrics,
    )
    total_saved = sum(int(bool(r["saved"])) for r in all_entries)
    return {
        "usable_candidates": len(all_entries),
        "saved": total_saved,
        "excluded_candidates": len(streaks) - len(all_entries),
        "pooled_across_regimes": False,
        "baseline": history_summary["baseline"],
        "neighborhood": history_summary["neighborhood"],
        "cohorts": {
            "cold_start_zero_history": cold_summary,
            "history_backed": history_summary,
            "same_run_slates": same_run_summary,
        },
        "streak_preparation": streak_info,
        "provenance": provenance,
        "missing_created_at_fallback": not any_known_created_at,
        "limitations": (
            "Shortlist intent diagnostic only; cold-start zero-history actions, history-backed "
            "actions, and same-run slates are reported in separate cohorts without pooling "
            "incompatible regimes. Not a rating or live A/B test."
        ),
    }


def feedback_check_engine_available_history(data, records, selected, *, strict_provenance=False):
    """Feedback sensitivity gated by both read day and row creation time.

    Preserves streak-initial timestamps, edition exclusion, and legacy summary
    keys while also exposing separated cold-start, history-backed, and same-run
    cohorts so zero-history neutral scores do not need to be interpreted pooled.
    """
    from afterword_engine.ranking import rank_candidates

    candidates = {c["id"]: c for c in data.get("candidates", [])}
    cache = {e["entity_id"]: e for e in data["embeddings"]
             if e["entity_type"] == "candidate" and (e["backend"], e["model"]) == selected}
    streaks, streak_info = prepare_feedback_streaks(data)

    labels, scores = [], []
    cold_start_entries = []
    history_backed_entries = []
    all_entries = []
    evaluated_candidates = []
    availability = {
        "prepared_history_rows": len(records),
        "prepared_rows_with_known_created_at": sum(
            read_time(item.get("created_at")) is not None for _, item, _ in records
        ),
        "prepared_rows_with_unknown_created_at": sum(
            read_time(item.get("created_at")) is None for _, item, _ in records
        ),
        "cases_with_unknown_prior_row_availability": 0,
        "cases_with_rows_created_after_action": 0,
        "cases_with_no_available_history": 0,
        "date_eligible_rows_missing_created_at_encounters": 0,
        "date_eligible_rows_created_after_action_encounters": 0,
        "date_eligible_unrelated_rows_encounters": 0,
        "available_history_rows_across_cases_not_unique": 0,
    }
    read_dimensions = int(records[0][2].shape[0]) if records else 0

    for candidate_id, (when, event) in streaks.items():
        item = candidates.get(candidate_id)
        entry = cache.get(candidate_id)
        if event.get("action") not in {"save", "reject"} or when is None:
            continue
        if not item or not entry or entry.get("content_hash") != content_hash(document(item)):
            continue
        query_vector = np.frombuffer(entry["vector"], dtype=np.float32)
        if (not query_vector.size or not np.isfinite(query_vector).all()
                or (read_dimensions and query_vector.size != read_dimensions)):
            continue

        prior, counts = feedback_history_available_at(records, when, item)
        availability["cases_with_unknown_prior_row_availability"] += counts["unknown_created_at"] > 0
        availability["cases_with_rows_created_after_action"] += counts["created_after_action"] > 0
        availability["cases_with_no_available_history"] += not prior
        availability["date_eligible_rows_missing_created_at_encounters"] += counts["unknown_created_at"]
        availability["date_eligible_rows_created_after_action_encounters"] += counts["created_after_action"]
        availability["date_eligible_unrelated_rows_encounters"] += (
            counts["available"] + counts["unknown_created_at"] + counts["created_after_action"]
        )
        availability["available_history_rows_across_cases_not_unique"] += counts["available"]

        history = [read for read, _ in prior]
        read_vectors = (
            np.stack([vector for _, vector in prior])
            if prior else np.empty((0, query_vector.size), dtype=np.float32)
        )
        ranked = rank_candidates(history, read_vectors, [item], query_vector[None, :])
        score = float(ranked[0]["score"])
        saved = event["action"] == "save"
        labels.append(saved)
        scores.append(score)
        evaluated_candidates.append(item)
        entry_row = {
            "candidate_id": candidate_id,
            "saved": saved,
            "when": when,
            "run_id": event.get("run_id"),
            "visible_at": event.get("visible_at"),
            "scores": {"neighborhood": score},
        }
        all_entries.append(entry_row)
        if prior:
            history_backed_entries.append(entry_row)
        else:
            cold_start_entries.append(entry_row)

    if not labels:
        return None
    provenance = assess_candidate_provenance(evaluated_candidates, strict=strict_provenance)
    labels = np.asarray(labels, dtype=bool)
    return {
        "usable_candidates": len(labels),
        "saved": int(labels.sum()),
        "excluded_candidates": len(streaks) - len(labels),
        "neighborhood": metrics(labels, np.asarray(scores, dtype=float)),
        "cohorts": {
            "cold_start_zero_history": _cohort_binary_summary(
                cold_start_entries, include_baseline=False
            ),
            "history_backed": _cohort_binary_summary(
                history_backed_entries, include_baseline=False
            ),
            "same_run_slates": summarize_same_run_cohort(
                all_entries, ("neighborhood",), metrics
            ),
        },
        "streak_preparation": streak_info,
        "provenance": provenance,
        "history_availability": availability,
        "limitations": (
            "Conservative row-availability sensitivity. read_at is gated to days before the action, "
            "and read.created_at must be known and no later than the initial streak action time. "
            "Use cohorts.cold_start_zero_history, cohorts.history_backed, and cohorts.same_run_slates "
            "rather than pooled neighborhood metrics across regimes."
        ),
    }


def _score_query_batch_against_history(history_items, history_vectors, query_items, query_vectors, rank_fn):
    """Score held-out reads while excluding any cross-split edition matches."""
    query_index = ReadIdentityIndex.from_reads(query_items)
    if not any(query_index.matches(item) for item in history_items):
        old_scores = np.asarray(
            baseline(history_items, history_vectors, query_items, query_vectors),
            dtype=np.float64,
        )
        ranked = rank_fn(history_items, history_vectors, query_items, query_vectors)
        by_id = {row["id"]: float(row["score"]) for row in ranked}
        new_scores = np.asarray([by_id[item["id"]] for item in query_items], dtype=np.float64)
        return old_scores, new_scores

    old_scores = np.empty(len(query_items), dtype=np.float64)
    new_scores = np.empty(len(query_items), dtype=np.float64)
    for idx, q_item in enumerate(query_items):
        q_index = ReadIdentityIndex.from_reads([q_item])
        keep = [i for i, h_item in enumerate(history_items) if not q_index.matches(h_item)]
        if not keep:
            raise ValueError("No unrelated prior history remained after edition exclusion")
        sub_history = [history_items[i] for i in keep]
        sub_vectors = history_vectors[keep]
        q_vec = query_vectors[idx : idx + 1]
        old_scores[idx] = float(baseline(sub_history, sub_vectors, [q_item], q_vec)[0])
        ranked = rank_fn(sub_history, sub_vectors, [q_item], q_vec)
        new_scores[idx] = float(ranked[0]["score"])
    return old_scores, new_scores


def evaluate_splits(records, *, history_mode="expanding"):
    """Evaluate chronological 60/20/20 splits using expanding history by default."""
    if history_mode not in HISTORY_MODES:
        raise ValueError(f"Unsupported history_mode {history_mode!r}; expected one of {HISTORY_MODES}")
    from afterword_engine.ranking import rank_candidates

    dates = [record[0].date() for record in records]
    cuts = []
    for fraction in (.6, .8):
        cut = int(len(records) * fraction)
        while cut < len(records) and dates[cut] == dates[cut - 1]:
            cut += 1
        cuts.append(cut)
    items = [record[1] for record in records]
    vectors = np.stack([record[2] for record in records])
    protocol = (
        "chronological 60/20/20 by unique work; whole-day boundaries; "
        "per-book strictly earlier-day expanding history; frozen formula"
        if history_mode == "expanding"
        else "chronological 60/20/20 by unique work; whole-day boundaries; "
        "optional frozen-prefix history (non-default sensitivity); frozen formula"
    )
    splits = {}
    for name, start, stop in (("validation", cuts[0], cuts[1]), ("test", cuts[1], len(items))):
        if stop <= start:
            raise ValueError("Not enough distinct read dates for chronological evaluation")
        queries = items[start:stop]
        labels = np.array([r["rating"] >= 4 for r in queries], dtype=bool)
        if history_mode == "frozen-prefix":
            old, new = _score_query_batch_against_history(
                items[:start],
                vectors[:start],
                queries,
                vectors[start:stop],
                rank_candidates,
            )
            max_history = start
        else:
            old = np.empty(stop - start, dtype=np.float64)
            new = np.empty(stop - start, dtype=np.float64)
            day_start = start
            max_history = start
            while day_start < stop:
                history_end = bisect_left(dates, dates[day_start])
                day_stop = day_start + 1
                while day_stop < stop and dates[day_stop] == dates[day_start]:
                    day_stop += 1
                if history_end <= 0:
                    raise ValueError("Not enough strictly earlier-day history for expanding replay")
                max_history = max(max_history, history_end)
                batch_old, batch_new = _score_query_batch_against_history(
                    items[:history_end],
                    vectors[:history_end],
                    items[day_start:day_stop],
                    vectors[day_start:day_stop],
                    rank_candidates,
                )
                old[day_start - start : day_stop - start] = batch_old
                new[day_start - start : day_stop - start] = batch_new
                day_start = day_stop
        rng = np.random.default_rng(20260920)
        differences = []
        for _ in range(1000):
            indexes = rng.integers(0, len(labels), len(labels))
            a, b = auc(labels[indexes], old[indexes]), auc(labels[indexes], new[indexes])
            if a is not None and b is not None:
                differences.append(b - a)
        splits[name] = {
            "history_mode": history_mode,
            "training": start,
            "max_history": max_history,
            "held_out": len(labels),
            "positive_rate": float(labels.mean()),
            "baseline": metrics(labels, old),
            "neighborhood": metrics(labels, new),
            "auc_delta_bootstrap_95_percent": (
                np.quantile(differences, [.025, .975]).tolist() if differences else None
            ),
        }
    return protocol, splits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--backend")
    parser.add_argument("--model")
    parser.add_argument(
        "--history-mode",
        choices=HISTORY_MODES,
        default="expanding",
        help="History window for held-out rating splits (default: strictly earlier-day expanding history)",
    )
    parser.add_argument(
        "--require-catalog-provenance",
        action="store_true",
        help="Fail closed when evaluated feedback candidates lack source_weight or catalog_confidence",
    )
    args = parser.parse_args()
    data = load_corpus(args.corpus)
    if args.require_catalog_provenance and data.get("candidates"):
        assess_candidate_provenance(data["candidates"], strict=True)
    records, selected, excluded = prepare(data, args.backend, args.model)
    protocol, splits = evaluate_splits(records, history_mode=args.history_mode)
    output = {
        "backend": selected[0],
        "model": selected[1],
        "works": len(records),
        "excluded": excluded,
        "history_mode": args.history_mode,
        "labels": "4-5 stars positive; 1-3 stars non-positive",
        "protocol": protocol,
        "limitations": (
            "One reader, title/author read documents; retrospective ranking among read books, "
            "not live acceptance."
        ),
        **splits,
    }
    output["explicit_feedback_diagnostic"] = feedback_check(
        data, records, selected, strict_provenance=args.require_catalog_provenance
    )
    output["explicit_feedback_engine_available_history_sensitivity"] = (
        feedback_check_engine_available_history(
            data, records, selected, strict_provenance=args.require_catalog_provenance
        )
    )
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
