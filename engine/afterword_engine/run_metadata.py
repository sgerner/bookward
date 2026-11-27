"""Pure helpers for recommendation run metadata classification.

This module depends only on the Python standard library so both online
serving/attribution and offline evaluators can inspect ``recommendation_runs``
metadata without importing database connections or tenancy state.

Legacy rows with ``NULL``, empty (``""``), or ``{}`` metadata (or omitted
``status``) remain classified as discovery runs, whereas rows with an explicit
non-discovery ``status`` or malformed/non-JSON ``metadata`` (including
non-object JSON payloads) are excluded from outcome attribution, session-run
pagination, and discovery-run filtering.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

DISCOVERY_RUN_STATUSES = frozenset({"", "recommended", "all"})
NON_DISCOVERY_ITEM_STATUSES = frozenset(
    {"saved", "imported", "rejected", "maybe_later", "decisions", "excluded", "read"}
)


def parse_run_metadata(raw_metadata: Any) -> Mapping[str, Any] | None:
    """Decode run metadata into a mapping, or return ``None`` if non-JSON/non-object."""

    if raw_metadata is None or raw_metadata == "":
        return {}
    if isinstance(raw_metadata, str):
        try:
            parsed = json.loads(raw_metadata)
        except (TypeError, ValueError):
            return None
    elif isinstance(raw_metadata, Mapping):
        parsed = raw_metadata
    else:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def is_discovery_run_metadata(raw_metadata: Any) -> bool:
    """Return True when run metadata belongs to a discovery recommendation run.

    Empty/null/``{}`` metadata is treated as legacy discovery, while explicit
    non-discovery statuses and malformed/non-JSON metadata return ``False``.
    """

    parsed = parse_run_metadata(raw_metadata)
    if parsed is None:
        return False
    status = parsed.get("status")
    if status is None:
        return True
    return isinstance(status, str) and status.strip().lower() in DISCOVERY_RUN_STATUSES


def is_matching_run_profile(raw_metadata: Any, expected_profile_id: str) -> bool:
    """Return True when run metadata matches ``expected_profile_id`` (or is legacy unlabeled)."""

    parsed = parse_run_metadata(raw_metadata)
    if parsed is None:
        return False
    recorded_profile = parsed.get("profile_id")
    if recorded_profile is None or recorded_profile == "":
        return True
    if not isinstance(recorded_profile, str):
        return False
    target = expected_profile_id.strip() or "legacy"
    return recorded_profile.strip() == target
