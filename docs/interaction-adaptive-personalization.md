# Interaction-adaptive personalization

## Product behavior

Bookward learns from actions people already take and applies a supported
preference to the recommendations they receive next. It adds no prompt,
preference screen, control, or second list, and it does not wait in shadow mode.
When evidence is uncertain, the existing neighborhood ranker is served as-is.
When the confidence gate passes, a bounded adjustment is applied to that same
ranker before pagination.

Bookward is designed for one reader per self-hosted installation, so every
browser and device shares the same learned profile. The engine aggregates all
eligible actions in its local database, including actions attached to older
browser sessions and legacy feedback rows. Recommendation requests do not need
a device cookie or session header for personalization. Historical run session
identifiers remain telemetry only; they do not split the learning data.

## Existing interaction signals

The learner uses the current `recommendation_events` log and legacy `feedback`
rows that are not duplicated by an event:

| Action | Satisfaction label | Weight |
| --- | --- | ---: |
| Save | Positive | 1.00 |
| Reject | Negative | 1.00 |
| Restore | Neutral correction; clears the prior label for that work | 0 |
| Attributed read rated 5 | Positive | 1.00 |
| Attributed read rated 4 | Positive | 0.85 |
| Attributed read rated 1 | Negative | 1.00 |
| Attributed read rated 2 | Negative | 0.85 |
| Attributed three-star or unrated read | Neutral; clears the prior label | 0 |
| Librarr import | Positive intent, not confirmed enjoyment | 0.70 |
| Card shown without action, detail/source open, or search | No label | 0 |

Only reads already attributed to a recommendation impression are used. Merely
being shown a book or opening its details is never interpreted as dislike. The
latest meaningful action replaces older actions for a canonical title-author
identity, preventing repeated clicks or duplicate imports from counting as
independent books. At most 5,000 recent action events are read, and evidence
uses a two-year half-life.

## Learning and serving rules

The engine combines two kinds of evidence across the installation's action
history:

1. **Explicit metadata preferences.** It compares save/positive-rating rates
   for an author's books or normalized subjects against the installation's other
   interacted books. Both sides need at least six effective distinct books and
   4.5 total reliability/recency weight. The feature is used only when the two
   95% Wilson intervals do not overlap.
2. **Semantic neighbors.** The engine compares each recommendation's existing
   content vector with vectors for interacted books. Similarities below 0.45
   are excluded; qualifying neighbor weights are multiplied by cosine
   similarity squared. A local neighborhood needs at least four distinct
   neighbors, four effective books, and 1.25 total weight. Its complement needs
   six effective books and 4.5 total weight. Local and complement 95% Wilson
   intervals must not overlap. This lets consistent behavior generalize to
   related books without requiring repeated catalog labels.

Both tests use the same saved/read-positive versus rejected/poorly-rated
outcomes. They produce additive movement on the existing 0–100 score: author
effects are capped at four points, each subject at two, semantic movement at
four, and the combined adjustment at eight. `metadata_confidence` scales the
movement down; missing or invalid confidence means no movement. Only the
`recommended` state changes score. Saved/imported records remain in their
existing state and order. The complete eligible pool is adjusted before
pagination so a qualified preference can promote a candidate that used to sit
below the page boundary. The existing explanation field says when observed
actions affected a result.

The request path reads only cached vectors whose content hash matches the
current candidate text. It never calls an embedding provider while serving a
request. Scoring jobs also refresh vectors for the 5,000 most recent interacted
candidate records, including saved or rejected books outside the active scoring
pool. This makes metadata changes available to semantic learning without an
extra model call in the recommendation response.

## Confidence, fallback, and monitoring

Each recommendation run records the policy version and aggregate diagnostics:
whether an adjustment was applied, number of distinct labeled books, exact
features that qualified, interaction vectors available, semantic candidates
adjusted, total candidates adjusted, and maximum score movement. It records no
preference profile, author, subject, or interaction text in that metadata.

The confidence gate is the live activation rule, rather than a shadow challenger
or a user choice. If the evidence does not pass, score and order stay with the
existing ranker. If the learner encounters malformed data or a runtime error,
the service logs the failure and serves the base ranking. The caps, metadata
confidence multiplier, and candidate-state filter limit the effect of any one
signal.

For evaluation, compare later saves, rejects, and attributed ratings by policy
version, keeping unseen outcomes unknown. Do not interpret one installation's
history as proof of population-wide improvement. Retain the current thresholds
until independent reader histories demonstrate that the live adjustments help
without degrading low-rating outcomes.

## Checks and current data note

Automated checks cover exact and semantic preference effects, low/high rating
signals, evidence thresholds, neutral corrections, recency decay, metadata
confidence, aggregation across historical browser sessions, pre-pagination
reordering, legacy feedback deduplication, and refreshing vectors for
interacted candidates. Web checks also confirm that recommendation and read
requests work without creating or forwarding a device cookie.

A read-only snapshot of the production event history found 75 distinct labeled
actions and no exact author/subject effect above the confidence gate. The
interaction vectors in that snapshot were stale under the current content
hashes, so the semantic path could not yet produce a production result. The
scoring change refreshes those vectors on the next scoring run; live semantic
adjustments should then remain conditional on the same confidence rules above.
