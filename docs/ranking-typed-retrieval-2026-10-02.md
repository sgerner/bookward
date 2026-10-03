# Typed retrieval probe: October 2, 2026

The bounded current-catalog probe found that verified author/subject expansion
can produce a large pool of quality-gated candidate works under a small public
request budget. It found no evidence that those pools cover the reader's future
favorite or disliked books, and the unchanged ranker's top 20 surfaced none of
those targets. Pool size by itself is not utility evidence, so this result does
not support a production change.

The study used the frozen private corpus, protocol, and feature cuts. Each
retrieval arm received a ceiling of 20 GET requests at each of two whole-day
boundaries, with at most 50 response items per GET, a shared global rate of one
GET per second, and no automatic retries. The run stopped when an arm's
predeclared seed lookups and expansions were exhausted; it did not send filler
requests. Baseline and typed arms used the same strict-prefix current query at
each boundary. The recent and mixed arms used four distinct, earlier, 4–5-star
seeds. Diffusion used only the typed-current capture and added no requests.

| Arm | Validation GETs | Qualified works | Works / GET | Later GETs | Qualified works | Works / GET | Unseen author-name works, validation / later |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Existing list provider (`list_current`) | 6 | 0 | 0.0 | 6 | 0 | 0.0 | 0 / 0 |
| Typed current seeds | 16 | 496 | 31.0 | 16 | 495 | 30.9 | 487 / 484 |
| Typed recent seeds | 17 | 485 | 28.5 | 4 | 0 | 0.0 | 476 / 0 |
| Typed mixed seeds | 17 | 439 | 25.8 | 10 | 241 | 24.1 | 431 / 234 |
| Diffusion, alpha 0.6 | 0 extra | 496 | — | 0 extra | 495 | — | Same pool as typed current |

The current-seed policy had the highest qualified-work yield at both
boundaries. Typed pools were almost entirely shared-subject edges. Validation
had 2/641 same-author/shared-subject edges for current, 0/715 for recent, and
6/613 for mixed; the later boundary had 2/639, 0/0, and 6/248. No explicit
series edges were verified. The later recent arm resolved four seed works but
none had a verified author key, so it stopped after its four seed-search GETs.
Missing author IDs were counted rather than inferred from names.

The unseen-author work count compares normalized author names to the earlier
read history; it is a coverage proxy, not verified unique-person identity.

Candidate gates required a canonical Open Library work ID, a trusted public
source URL, a passing local title/author quality check, no identity conflict,
and no match to the strict-prefix read history. Known first-publication dates
after the boundary were excluded; unknown and year-only same-year dates stayed
eligible and were counted. One validation mixed-arm candidate was excluded as
known post-cutoff. Among eligible candidates, publication dates were unknown
for 2 validation current-seed works and 1 later current-seed work. These are
current catalog metadata checks: Open Library's present-day record cannot
reconstruct what was available on the historical read date.

The existing list-provider baseline returned no qualified works and had one
`list_items` HTTP status failure at each boundary. Its empty pool is therefore
not a clean estimate of a functioning list source. The typed current arm used
four seed searches and twelve subject requests at each boundary, with no HTTP
failures. Recent and mixed used up to four seed searches, one author expansion
when a verified author key existed, and up to twelve subject requests; their
actual request counts are shown above. All requests and failed requests count
against the arm's ceiling.

Retrieval was frozen before consulting future ratings. The fixed future-rated
cohort contained 355 validation targets (84 high, 69 low, 202 neutral) and 356
later targets (153 high, 54 low, 149 neutral). Candidate matching used verified
Open Library work IDs, ISBNs, or exact title-plus-author identity; title-only
matches were prohibited. The first replay used work IDs already present on the
prepared read rows. A separate identity sensitivity joined `read_metadata`
only where its `identity_hash` exactly matched the original read under the
production identity-hash function, retained original work IDs, and added 820
source-verified cached Open Library work IDs to prepared reads; no ID conflicts
were skipped. This removed no additional already-read candidate from any saved
pool. With those IDs, validation overlap remains zero high and low targets, with
one neutral target in typed-current and diffusion. Later overlap is zero high,
one low, and zero neutral targets in typed-current and diffusion; the other
arms have no target overlap. Thus the pool still retrieves no future favorites,
but the identity-complete pool sensitivity does find one future low-rated
target. No fold has both a matched high and low outcome, so the target-vector
AUC remains undefined. These identity joins do not depend on candidate text.

The saved ranker replay retained only aggregate top-20 counts, so its original
zero high/low result cannot establish the position of that newly matched low
target. A local compatible-text replay of only the later 495-work typed-current
pool used the frozen encoder and fixed ranker; its candidate-text fingerprint
matched the saved compatible replay exactly. The low target ranked 166 of 495,
scored 53.3, and was at the 66.6th best-score percentile ((495 - rank) / 494),
outside the top 20.
This rerun embedded 494 unique candidate texts locally and made no public HTTP
requests. Its full ranks and candidate vectors are saved privately for audit.
A separate read-only check of the 1,934 current production candidates found no
additional already-read matches from these cached IDs.

The prior enriched-text sensitivity is retained only as a legacy projection,
not evidence. A post-run audit found that the evaluator populated its
description field from `description` or `first_sentence`, and could stringify
Open Library's `{type, value}` description object. The saved captures contain
only flattened description strings and omit the original catalog field shape
and provenance, so the projection cannot be safely reconstructed. No new
catalog requests or embeddings were made to revise it. The corrected helper
accepts only a description string or a string `description.value`; it never
uses `first_sentence`.

The compatible title-author candidate-text replay used the frozen local
`qwen3-embedding:4b` model (2,560 dimensions; digest
`df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907`). The
saved capture was replayed without more catalog requests. The current ranker
and query vectors were fixed across arms. The later partition has been
examined in prior work and is exploratory; this remains one retrospective
reader's future-rated target cohort, not an evaluation of all unseen books or a
live recommendation outcome. The old run's 1,998 distinct candidate-text
embeddings describe its workload only; they do not validate the legacy
enriched-text sensitivity. The 1,000-candidate scoring cap covered every
captured pool; the largest pool had 496 works.

For any follow-up runtime comparison, preserve the four-seed current policy as
the pool-yield reference, but do not promote it from this result. Pass the
provider the full strict-prefix read history so its own seed policy and
already-read exclusion see all earlier reads; pre-truncating to 25 favorite
seeds would change recent/mixed coverage and exclusion behavior. Keep absent
author and series metadata unknown. A useful next probe needs reliable seed
author IDs and enough verified identity overlap with relevant outcomes to
measure high/low target coverage. No runtime or production data was changed in
this study.

Private artifact identities:

- Frozen protocol: `8c58739a896acb2d0284f3c51eb5752121ed9262a415ecd313b8f124d7dd935d`
- Corpus: `0973bf5e029f1ee0f2a6ad8957313e64da639b813e20d31491a9ba4b8558eeb9`
- Exact captured pools: `f631848e42ef9ca1552332d24f2ce674d49b7b30e0e665624beab3056a0a707e`
- Aggregate retrieval report: `24357479913825fd7b65003d9871a8a180c1b3de6c27b7c7fc3d42dd9480435f`
- Fixed-ranker replay: `7ea169674bea086330117d0dac718dd9ac103cbc950e51b74794782c243f9aec`
- Cached-work-ID identity audit: `b977ae0d224664bdc97232de5e59a41836ecf63652b1fd30c219fb6c22d7c666`
- Later typed-current compatible full-rank replay: `33e3109c36f4f1ad532d69e62b34a230d61d34645fa1ffb9eda77f34c551c030`
- Later typed-current compatible candidate vectors: `d6a9f5a0aa2116224e13619ba3a22917aa0c3db87ad27bbf849a8f66df8c026a`
