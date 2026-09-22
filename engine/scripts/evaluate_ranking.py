#!/usr/bin/env python3
"""Evaluate rating ranking from a private JSON export or read-only SQLite snapshot.

Prints aggregate metrics only. No network requests or database writes.
"""
import argparse
import base64
from datetime import datetime, timezone
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
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        try:
            result = datetime.strptime(value, "%Y/%m/%d")
        except (ValueError, TypeError):
            try:
                result = parsedate_to_datetime(value)
            except (ValueError, TypeError, AttributeError):
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
    dated_events = [(read_time(e["created_at"]), e) for e in data.get("feedback", [])]
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
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
