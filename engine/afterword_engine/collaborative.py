"""Opt-in collaborative-filtering residual scorer backed by a local CF index."""

from __future__ import annotations

from pathlib import Path
import re
import sqlite3
from typing import Any, Sequence


def normalize_isbn(value: object) -> str:
    """Return digits/X of an ISBN-10 or ISBN-13, or empty string."""
    cleaned = re.sub(r"[^0-9Xx]", "", str(value or "")).upper()
    if len(cleaned) in {10, 13}:
        return cleaned
    return ""


def _candidate_isbns(row: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    for field in ("isbn13", "isbn10", "isbn"):
        code = normalize_isbn(row.get(field))
        if code and code not in codes:
            codes.append(code)
    return codes


class CollabIndex:
    """Read-only handle to a prebuilt item-item collaborative similarity SQLite index."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._con = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)

    def close(self) -> None:
        self._con.close()

    def lookup_item_id(self, row: dict[str, Any]) -> int | None:
        for code in _candidate_isbns(row):
            found = self._con.execute(
                "SELECT item_id FROM isbn_map WHERE isbn=? LIMIT 1", (code,)
            ).fetchone()
            if found is not None:
                return int(found[0])
        return None

    def neighbors(self, item_id: int) -> list[tuple[int, float]]:
        rows = self._con.execute(
            "SELECT neighbor_id, weight FROM item_neighbors WHERE item_id=?",
            (item_id,),
        ).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]


def load_collab_index(path_str: str) -> CollabIndex | None:
    """Open ``CollabIndex`` if ``path_str`` points to an existing file."""
    raw = (path_str or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_file():
        return None
    return CollabIndex(path)


def score_collaborative_residual(
    reads: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    index: CollabIndex,
    *,
    min_neighbors: int = 3,
    shrinkage: float = 2.0,
    scale_per_star: float = 4.0,
    max_adjustment: float = 5.0,
) -> dict[int, float]:
    """Compute bounded collaborative rating-residual adjustments per candidate ID."""
    rated_reads = [
        r for r in reads if r.get("rating") is not None and 1.0 <= float(r["rating"]) <= 5.0
    ]
    if not rated_reads or not candidates:
        return {}
    global_mean = sum(float(r["rating"]) for r in rated_reads) / len(rated_reads)

    read_residuals: dict[int, float] = {}
    for read in rated_reads:
        item_id = index.lookup_item_id(read)
        if item_id is None:
            continue
        residual = float(read["rating"]) - global_mean
        if item_id not in read_residuals:
            read_residuals[item_id] = residual

    if not read_residuals:
        return {}

    adjustments: dict[int, float] = {}
    for cand in candidates:
        cand_id = cand.get("id")
        if cand_id is None:
            continue
        item_id = index.lookup_item_id(cand)
        if item_id is None:
            continue
        matched_weight = 0.0
        weighted_sum = 0.0
        matched_count = 0
        for neighbor_id, weight in index.neighbors(item_id):
            if neighbor_id in read_residuals and weight > 0:
                weighted_sum += weight * read_residuals[neighbor_id]
                matched_weight += weight
                matched_count += 1
        if matched_count < min_neighbors or matched_weight <= 0:
            continue
        predicted_residual = weighted_sum / (matched_weight + shrinkage)
        adj = max(-max_adjustment, min(max_adjustment, predicted_residual * scale_per_star))
        if abs(adj) >= 0.05:
            adjustments[int(cand_id)] = round(adj, 2)
    return adjustments
