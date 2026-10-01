#!/usr/bin/env python3
"""Evaluate rating ranking from a private JSON export or read-only SQLite snapshot.

Prints aggregate metrics only. No network requests or database writes.
"""
import argparse
import base64
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import sqlite3
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afterword_engine.embeddings import content_hash
from afterword_engine.identity import book_identity
from afterword_engine.scoring import document


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


def load_corpus(path):
    if path.suffix == ".json":
        data = json.loads(path.read_text())
        for item in data["embeddings"]:
            item["vector"] = base64.b64decode(item["vector"], validate=True)
        return data
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        con.execute("BEGIN")
        return {
            # Preserve row-availability time for the separate feedback
            # sensitivity. Imported read_at dates do not imply when a row was
            # present in the database.
            "reads": [dict(r) for r in con.execute("SELECT * FROM reads")],
            "candidates": [dict(r) for r in con.execute("SELECT * FROM candidates")],
            "feedback": [dict(r) for r in con.execute("SELECT * FROM feedback")],
            "embeddings": [dict(r) for r in con.execute(
                "SELECT * FROM embeddings WHERE entity_type IN ('read','candidate')"
            )],
        }


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
    valid = []
    excluded = {"unrated": 0, "undated": 0, "missing_or_stale_vector": 0, "duplicate_work": 0}
    for item in data["reads"]:
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
    seen, records = set(), []
    for date, item, vector in valid:
        key = book_identity(item["title"], item["author"])
        if key in seen:
            excluded["duplicate_work"] += 1
            continue
        seen.add(key)
        records.append((date, item, vector))
    if len(records) < 100:
        raise ValueError("At least 100 dated, distinct rated works with valid vectors are required")
    return records, selected, excluded


def auc(labels, scores):
    positive, negative = scores[labels], scores[~labels]
    if not len(positive) or not len(negative):
        return None
    return float(((positive[:, None] > negative).sum()
                  + .5 * (positive[:, None] == negative).sum()) / (len(positive) * len(negative)))


def metrics(labels, scores):
    order = np.argsort(-scores, kind="stable")[:20]
    discounts = 1 / np.log2(np.arange(2, len(order) + 2))
    ideal = np.sort(labels)[::-1][:20] @ discounts
    return {"auc": auc(labels, scores), "precision_at_20": float(labels[order].mean()),
            "ndcg_at_20": float(labels[order] @ discounts / ideal) if ideal else None}


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


def feedback_check(data, records, selected):
    """Separate intent diagnostic; never train on workflow status or feedback."""
    from afterword_engine.ranking import rank_candidates
    candidates = {c["id"]: c for c in data.get("candidates", [])}
    cache = {e["entity_id"]: e for e in data["embeddings"]
             if e["entity_type"] == "candidate" and (e["backend"], e["model"]) == selected}
    latest = {}
    dated_events = [
        (read_time(e["created_at"]), e)
        for e in data.get("feedback", [])
        if not e.get("undone_at")
    ]
    for _, event in sorted(((date, e) for date, e in dated_events if date is not None),
                           key=lambda pair: (pair[0], pair[1]["id"])):
        latest[event["candidate_id"]] = event
    labels, old, new = [], [], []
    for candidate_id, event in latest.items():
        item, entry = candidates.get(candidate_id), cache.get(candidate_id)
        when = read_time(event["created_at"])
        if event["action"] not in {"save", "reject"} or when is None:
            continue
        if not item or not entry or entry["content_hash"] != content_hash(document(item)):
            continue
        identity = book_identity(item["title"], item["author"])
        prior = [(r, v) for date, r, v in records if date.date() < when.date()
                 and book_identity(r["title"], r["author"]) != identity]
        if not prior:
            continue
        history, vectors = [r for r, _ in prior], np.stack([v for _, v in prior])
        query = np.frombuffer(entry["vector"], dtype=np.float32)[None, :]
        if query.shape[1] != vectors.shape[1] or not np.isfinite(query).all():
            continue
        labels.append(event["action"] == "save")
        old.append(baseline(history, vectors, [item], query)[0])
        new.append(rank_candidates(history, vectors, [item], query)[0]["score"])
    if not labels:
        return None
    labels = np.array(labels)
    return {"usable_candidates": len(labels), "saved": int(labels.sum()),
            "excluded_candidates": len(latest) - len(labels),
            "baseline": metrics(labels, np.array(old)), "neighborhood": metrics(labels, np.array(new)),
            "limitations": "Small intent sample, current metadata, no impression log; not a rating or live A/B test."}


def feedback_history_available_at(records, when, candidate):
    """Select unrelated prior-day reads whose rows existed by the action time.

    Missing or malformed ``created_at`` is unknown and excluded. The returned
    counters describe only rows that would otherwise qualify by read date and
    identity, so callers can report the cost of this conservative gate.
    """
    identity = book_identity(candidate["title"], candidate["author"])
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
        if book_identity(item["title"], item["author"]) == identity:
            counts["same_identity"] += 1
            continue
        created_at = read_time(item.get("created_at"))
        if created_at is None:
            counts["unknown_created_at"] += 1
            continue
        if created_at > when:
            counts["created_after_action"] += 1
            continue
        prior.append((item, vector))
    counts["available"] = len(prior)
    return prior, counts


def feedback_check_engine_available_history(data, records, selected):
    """Feedback sensitivity gated by both read day and row creation time.

    This reports only current-ranker metrics. It deliberately keeps candidates
    with an empty available history in the cohort; the ranker supports an empty
    history and returns its neutral-history score. It does not invent a legacy
    baseline for that case.
    """
    from afterword_engine.ranking import rank_candidates

    candidates = {c["id"]: c for c in data.get("candidates", [])}
    cache = {e["entity_id"]: e for e in data["embeddings"]
             if e["entity_type"] == "candidate" and (e["backend"], e["model"]) == selected}
    latest = {}
    dated_events = [
        (read_time(e.get("created_at")), e)
        for e in data.get("feedback", [])
        if not e.get("undone_at")
    ]
    for _, event in sorted(((when, event) for when, event in dated_events if when is not None),
                           key=lambda pair: (pair[0], pair[1]["id"])):
        latest[event["candidate_id"]] = event

    labels, scores = [], []
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

    for candidate_id, event in latest.items():
        item = candidates.get(candidate_id)
        entry = cache.get(candidate_id)
        when = read_time(event.get("created_at"))
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
        labels.append(event["action"] == "save")
        scores.append(ranked[0]["score"])

    if not labels:
        return None
    labels = np.asarray(labels, dtype=bool)
    return {
        "usable_candidates": len(labels),
        "saved": int(labels.sum()),
        "excluded_candidates": len(latest) - len(labels),
        "neighborhood": metrics(labels, np.asarray(scores, dtype=float)),
        "history_availability": availability,
        "limitations": (
            "Conservative row-availability sensitivity only. read_at is gated to days before the action, "
            "and read.created_at must be known and no later than the exact action time. created_at "
            "records row creation, not later rating edits or when a rating became known. Missing "
            "availability is excluded and counted. Empty histories remain in this current-ranker cohort; "
            "no legacy baseline is reported for them."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--backend")
    parser.add_argument("--model")
    args = parser.parse_args()
    data = load_corpus(args.corpus)
    records, selected, excluded = prepare(data, args.backend, args.model)
    # Keep all reads from a boundary day together; a later same-day rating must
    # not enter a profile used to predict an earlier rating on that day.
    dates = [record[0].date() for record in records]
    cuts = []
    for fraction in (.6, .8):
        cut = int(len(records) * fraction)
        while cut < len(records) and dates[cut] == dates[cut - 1]:
            cut += 1
        cuts.append(cut)
    items = [record[1] for record in records]
    vectors = np.stack([record[2] for record in records])
    output = {"backend": selected[0], "model": selected[1], "works": len(items), "excluded": excluded,
              "labels": "4-5 stars positive; 1-3 stars non-positive",
              "protocol": "chronological 60/20/20 by unique work; whole-day boundaries; frozen formula",
              "limitations": "One reader, title/author read documents; retrospective ranking among read books, not live acceptance."}
    from afterword_engine.ranking import rank_candidates
    for name, start, stop in (("validation", cuts[0], cuts[1]), ("test", cuts[1], len(items))):
        if stop <= start:
            raise ValueError("Not enough distinct read dates for chronological evaluation")
        history, queries = items[:start], items[start:stop]
        labels = np.array([r["rating"] >= 4 for r in queries])
        old = baseline(history, vectors[:start], queries, vectors[start:stop])
        ranked = rank_candidates(history, vectors[:start], queries, vectors[start:stop])
        scores_by_id = {r["id"]: r["score"] for r in ranked}
        new = np.array([scores_by_id[r["id"]] for r in queries])
        rng = np.random.default_rng(20260920)
        differences = []
        for _ in range(1000):
            indexes = rng.integers(0, len(labels), len(labels))
            a, b = auc(labels[indexes], old[indexes]), auc(labels[indexes], new[indexes])
            if a is not None and b is not None:
                differences.append(b - a)
        output[name] = {"training": start, "held_out": len(labels), "positive_rate": float(labels.mean()),
                        "baseline": metrics(labels, old), "neighborhood": metrics(labels, new),
                        "auc_delta_bootstrap_95_percent": np.quantile(differences, [.025, .975]).tolist() if differences else None}
    output["explicit_feedback_diagnostic"] = feedback_check(data, records, selected)
    output["explicit_feedback_engine_available_history_sensitivity"] = (
        feedback_check_engine_available_history(data, records, selected)
    )
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
