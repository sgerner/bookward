#!/usr/bin/env python3
"""Print a read-only JSON report for logged recommendation outcomes.

The input may be a SQLite database or an export containing
``recommendation_runs``, ``recommendation_impressions``, and
``recommendation_outcomes`` arrays. The script never mutates a database and
never turns an unobserved impression into a negative outcome.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afterword_engine.evaluation import DEFAULT_BOOTSTRAP_ITERATIONS, build_report, load_rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="SQLite snapshot or JSON export")
    parser.add_argument("--bootstrap-iterations", type=int, default=DEFAULT_BOOTSTRAP_ITERATIONS)
    parser.add_argument("--bootstrap-seed", type=int, default=20260920)
    parser.add_argument("--minimum-runs", type=int, default=20)
    parser.add_argument("--minimum-labeled-impressions", type=int, default=200)
    parser.add_argument("--minimum-positive-impressions", type=int, default=20)
    parser.add_argument("--minimum-negative-impressions", type=int, default=20)
    args = parser.parse_args()
    if args.bootstrap_iterations < 0:
        parser.error("--bootstrap-iterations must be non-negative")
    if any(value < 0 for value in (
        args.minimum_runs,
        args.minimum_labeled_impressions,
        args.minimum_positive_impressions,
        args.minimum_negative_impressions,
    )):
        parser.error("minimum sample gates must be non-negative")
    rows = load_rows(args.source)
    report = build_report(
        rows,
        bootstrap_iterations=args.bootstrap_iterations,
        bootstrap_seed=args.bootstrap_seed,
        minimums={
            "runs": args.minimum_runs,
            "labeled_impressions": args.minimum_labeled_impressions,
            "positive_impressions": args.minimum_positive_impressions,
            "negative_impressions": args.minimum_negative_impressions,
        },
    )
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()

