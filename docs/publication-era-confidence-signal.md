# Confidence-gated publication-era preferences

Publication year is an optional, per-profile signal. The existing content and
rating score remains the baseline. The era feature contributes **zero** unless
it can predict rating residuals beyond that baseline on later held-out books.
Year is a proxy for a reader's observed preferences; it does not establish a
book's politics, gender roles, writing style, or suitability for a demographic.

## Evidence and behavior

The metadata loader accepts only Open Library **work-level first publication**
with a matching verified work identity. A normalized date requires its field
hash; a retained raw payload must name the same work and an explicit
`first_publish_year` or `first_publish_date`. Edition dates, generic release
dates, stale read identities, conflicting evidence, missing dates, and a
publication year after the reading year remain unknown. The original read text
and embedding representation stay the same.

A Gaussian curve with a fixed ten-year bandwidth can capture a preferred era
or several supported eras without assuming that newer or older is always
better. It models residuals after calibrating the existing rank score to
ratings. A deterministic sample of at most 128 calibration targets bounds
work; other dated reads remain available as baseline history. Three expanding
validation blocks use histories strictly before the first held-out day. The
seed block uses training-only scores with each query identity excluded; it is
not a causal outcome test. Same-day and future ratings cannot enter a validation history.

The profile must pass every validation block's paired day and author confidence
checks, rather than merely have many dated books. Candidate adjustments also
require supported local years, effective sample size, multiple authors and
reading days, and a correction interval excluding zero. The fixed requirements
are at least 40 seed targets, 24 rated training books with verified years, eight distinct years,
eight effective local observations, and eight local authors and days. A year
more than 20 years from its nearest supported training year stays neutral.
Each held-out fold needs at least eight day and author groups and 80% known
author coverage, positive mean improvement, and positive one-sided 95% paired
bootstrap lower bounds for both groupings. Unsupported years and extrapolation
contribute zero. The validated correction is capped at two score
points after the existing catalog-confidence adjustment, before clamping and
rounding the final score. Validation tests this actual bounded correction, rather than an uncapped surrogate.

Confidence intervals are conservative activation rules, not promises that a
reader will enjoy an individual book. Synthetic profiles verify activation and
abstention; they do not establish recommendation quality across real users.
The captured production corpus contains 1,792 rated reads, including 822 with
verified original years, and 370 eligible candidates, including 195 with
verified years. The final replay **abstained and changed 0 of 370 scores**.
After identity reduction the fit had 1,790 works, 1,779 dated rows, and 51
verified years among its 128 calibration targets. The first fold had only 22
usable era training rows; neither later fold found a locally confident
correction. The full ranking replay took about 0.30 seconds on the local
single-threaded test runtime. This is evidence of safe abstention, not an
observed recommendation-quality improvement for this reader.

The scoring capture was taken at `2026-10-04T00:04:30Z` (October 3 locally).
Capture SHA-256 starts `008c41096714c6c2`; the separate field-ledger capture starts
`2d967fe272e074eb`. The private result records full input hashes and the complete
16-module runtime lineage. Reading data and vectors remain outside the repo.

## Replay and operations

`rank_candidates(..., publication_era=False)` reproduces the baseline. The
reading-history leave-one-out diagnostic also omits the fitted era feature so
a target's own rating cannot affect the feature's activation gate. This means
its diagnostic score may differ from a future recommendation score when an
era model is active.

Scoring batches archive the normalized year and verified work inputs and model
diagnostics. Repeated editions or aliases of a verified work contribute only
one rating to the era fit, preventing self-rating leakage through another title.
Source lineage includes both feature modules. Verified read-year or work-identity changes enqueue a
rescore; unchanged evidence does not. No database schema change,
new embedding service, birth-year inference, or profile-shared learned weights
are required.

An aggregate private replay can be run with:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 engine/.venv/bin/python \
  engine/scripts/audit_publication_era_gate.py \
  --capture /private/current-scoring-capture.json \
  --provenance /private/publication-ledger-capture.json \
  --output /private/era-gate-results.json
```

The tool verifies exact captured embedding documents, uses the production
metadata loader and ranker, and emits no book titles or individual ratings.
It evaluates captured base scores, not fresh interest outcomes or a full
serving-slate replay. The earlier complete-pipeline audit's frozen V1 file
allowlist remains unchanged; it is not a replay protocol for this new runtime. Prior publication-year studies found small uncertain
changes, so this feature should be expected to abstain for many profiles.
