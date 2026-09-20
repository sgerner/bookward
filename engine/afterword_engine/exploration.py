"""Pure, opt-in tail exploration for recommendation responses.

The first four results are always kept in their ranked order.  When enabled,
the remaining tail is sampled from a mixture of the deterministic ranking and
a uniformly shuffled tail.  The returned ``propensity`` is the exact
marginal probability of the observed item occupying its observed tail slot;
callers can persist it with the impression for inverse-propensity evaluation.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, MutableMapping
from typing import Any


DEFAULT_STABLE_TOP_K = 4


def epsilon_tail_explore(
    recommendations: Iterable[MutableMapping[str, Any]],
    *,
    epsilon: float = 0.0,
    stable_top_k: int = DEFAULT_STABLE_TOP_K,
    rng: random.Random | Any | None = None,
) -> list[dict[str, Any]]:
    """Return a sampled ranking with an exact propensity on every item.

    ``epsilon=0`` is the disabled path and preserves list order, item values,
    and scores.  A local copy is returned so adding telemetry never mutates a
    caller's recommendation objects.  ``rng`` may be an injected
    ``random.Random`` instance (or another object implementing ``random`` and
    ``shuffle``) for deterministic tests.
    """

    try:
        epsilon = float(epsilon)
    except (TypeError, ValueError) as exc:
        raise ValueError("epsilon must be between 0 and 1") from exc
    if not 0 <= epsilon <= 1:
        raise ValueError("epsilon must be between 0 and 1")
    if isinstance(stable_top_k, bool) or int(stable_top_k) < 0:
        raise ValueError("stable_top_k must be non-negative")
    stable_top_k = int(stable_top_k)
    items = [dict(item) for item in recommendations]
    if not items:
        return []

    # Always emit a propensity.  This makes a run self-describing and gives
    # existing deterministic traffic propensity 1 without another flag.
    for item in items:
        item["propensity"] = 1.0
    tail = items[stable_top_k:]
    if epsilon <= 0 or len(tail) <= 1:
        return items

    source_positions = {id(item): position for position, item in enumerate(tail)}
    # Keep the original item identity until after the probability is computed.
    # The copy list above means this cannot mutate caller-owned objects.
    sampled_tail = list(tail)
    generator = rng if rng is not None else random.SystemRandom()
    if generator.random() < epsilon:
        generator.shuffle(sampled_tail)

    tail_size = len(tail)
    uniform_mass = epsilon / tail_size
    for position, item in enumerate(sampled_tail):
        original_position = source_positions[id(item)]
        baseline_mass = 1.0 - epsilon if original_position == position else 0.0
        item["propensity"] = baseline_mass + uniform_mass
    return items[:stable_top_k] + sampled_tail


def apply_epsilon_tail_exploration(
    recommendations: Iterable[MutableMapping[str, Any]],
    epsilon: float = 0.0,
    *,
    stable_top_k: int = DEFAULT_STABLE_TOP_K,
    rng: random.Random | Any | None = None,
) -> list[dict[str, Any]]:
    """Compatibility-friendly alias for :func:`epsilon_tail_explore`."""

    return epsilon_tail_explore(
        recommendations,
        epsilon=epsilon,
        stable_top_k=stable_top_k,
        rng=rng,
    )

