# Discovery interest pilot — October 2, 2026

The completed blinded interest form does **not** support deploying the tested
expanded retrieval policy. It does reveal useful new books in a random sample,
but the current scorer did not place those books in the first eight.

## Protocol and coverage

The source comparison used equal ceilings of 20 public catalog requests per
retrieval arm, at most 50 items per request, and one shared request per second.
The existing source path used six requests and returned no eligible new works;
one request failed. Typed subject/association queries used 16 requests and
returned 494 eligible works. Eleven already existed among the 375 eligible
production candidates, leaving 483 additional works. The existing arm's failure
is a coverage limitation; this capture cannot establish long-term superiority.

All 1,797 library entries, including unrated books, were excluded using the
current identity rules. New works were scored with the existing production read
vectors and policy. The four frozen policies crossed current/expanded pools
with the current slate/a bounded reserved-slot variant. No eligible swap met
the reserved-slot rule, so there were only two distinct lists in this snapshot.

The 44 cards contained the union of policy top-eight books and stratified random
coverage samples. Book order and opaque IDs were fixed before judgments; source
and model identities were hidden. All descriptions were fetched using the same
Open Library work endpoint. Random-sample inclusion probabilities were retained.
The target population is this single reader and these captured pools.

## Judgments

| Response | Count |
| --- | ---: |
| Interested | 14 |
| Not interested | 18 |
| Unsure / not now | 10 |
| Already read | 2 |

Ten cards had no qualifying synopsis. Nine received an unsure response and one
an already-read response. Missing descriptions and unsure responses are
**unknown**, not negative judgments. Already-read books are ineligible for
future discovery and are not dislike labels. Both already-read reports were
absent from the captured library; they expose incomplete known history rather
than a reproduced identity-exclusion failure.

| Frozen first-eight list | Interested | Not interested | Unsure | Already read |
| --- | ---: | ---: | ---: | ---: |
| Current pool and policy | 4 | 1 | 3 | 0 |
| Expanded pool and current policy | 3 | 2 | 2 | 1 |

The expansion removed one interested and one unknown book and added one
not-interested and one already-read book. Among known, eligible judgments,
interest was 4/5 versus 3/5. Allowing every unknown to be either uninterested or
interested gives current-list interest bounds of 4/8–7/8 and expanded-list bounds
of 3/7–5/7 after excluding the already-read book. These intervals overlap; they
do not justify a policy-quality claim or an automatic rollout.

The random typed-only sample had five interested, three not-interested, and
four unsure responses: 5/8 among known responses, with four unresolved.
This suggests retrieval can surface interesting books, but the sample is small
and description absence is informative. It is not an estimate of all expanded
candidates' utility under a missing-at-random assumption.

## Source and ranking follow-up

A uniform metadata recovery retry yielded no verified synopsis for the ten
missing cards: Open Library supplied no qualifying synopsis, and all ten Google
Books calls were rate-limited. Production has no Google Books key configured,
matching the retry's authentication setup. A separately bounded publisher
check found exact ISBN/title/author descriptions for four, work-level
descriptions with unresolved editions for two, and no usable matching source
for four. See [the source pilot](reading-experience-source-pilot-2026-10-02.md).
The four exact publisher cases already had nonempty production descriptions;
the existing publisher enrichment path reproduced all four descriptions and
matched their ISBNs. The form missed these because it fetched only Open Library
work descriptions. This is a form coverage gap, not evidence that production
publisher enrichment failed. The original answers are preserved; judgments
after seeing recovered text must be a separate follow-up, not retroactive edits
to this pilot.

For a newly frozen form, the card builder now uses one resolver across policy
arms: it first checks the canonical Open Library work description, then may use
the exact-ISBN Penguin Random House description only when the candidate already
has a direct HTTPS product link on an allowlisted publisher host. The source,
identifier, and description hash remain in private evidence files. The blinded
page embeds only the opaque card ID, title, author, and synopsis; it escapes
script-significant `<` characters in the JSON payload and renders text with
`textContent`. This changes future form generation only: the completed 44-card
form and its answers are not regenerated or edited. Any judgments made after
seeing recovered descriptions belong to a separate follow-up dataset.

Pipeline ablations were also scored against the sampled current-pool judgments.
Only 23 current-candidate cards had known binary judgments. Removing source or
metadata confidence changed rankings without supporting a utility improvement;
several ablation top-eight books were never sampled. Those partial, selected
labels cannot establish full-slate precision or select a new production policy.

## Limits and reproduction

This is one blinded interest judgment per displayed card, not a randomized
production A/B test or a measured reading outcome. Partial top-20 coverage is
reported as unobserved rather than converted to negatives. Some catalog
descriptions were reviews, opening prose, or contained HTML; a later form should
apply stronger display-quality checks consistently across arms before collecting
new answers. Do not alter this completed form's displayed evidence afterward.

Raw books, vectors, opaque card mappings, and judgments remain outside Git in
private local artifacts under `bookward-tracks-20261002/discovery`. Preparation,
blinded-card construction, and analysis are implemented by
`prepare_discovery_judgments.py`, `build_discovery_pilot.py`,
`analyze_discovery_judgments.py`, and `evaluate_pipeline_judgments.py` in
`engine/scripts`. Do not treat a changed input capture as a replay of this pilot.
