# Embedding cache integrity correction

## Finding

The cache lookup previously accepted an embedding when its content hash
matched, without checking whether the stored BLOB was a usable float32 vector
or agreed with the stored dimension count. Controlled matching-hash fixtures
reproduced reuse of NaN, empty, malformed, and declared-dimension-mismatched
values. The fixture showed the generic reliability failure; no corrupted
production cache entry was observed.

## Correction

`cached_vectors` now reuses a cached value only when its BLOB is nonempty,
float32-aligned, finite, and exactly matches the positive integer dimensions
stored with it. A zero vector remains valid. Invalid matching-hash entries are
treated as missing and regenerated. Provider results are converted to
float32 and validated before any cache transaction, catching overflow such
as finite Python `1e100` becoming float32 infinity. The engine rejects
inconsistent dimensions between valid cached vectors and new provider output
before persisting, instead of silently mixing vector spaces.

The expected dimension also crosses the engine's separate read and candidate
cache calls. A scoring pass establishes it from validated read vectors;
candidate cache hits and newly generated candidate vectors must match before
they can be returned or written. Reading-history scoring uses the same guard.
Forced full-cache rebuilds propagate the freshly generated read dimension to
candidate generation while remaining non-persistent until both sets pass
validation.

## Verification

The focused cache-recovery selection passed: 15 passed, 88 deselected. It covers
the four corrupt-cache forms, zero-vector reuse, cached/new dimension mismatch,
cross-call expected-dimension failures for cached and newly generated
candidate vectors, forced rebuild behavior, float32 overflow, invalid provider
values, and full `score_all` recovery. The end-to-end test records normal
deterministic scores, corrupts one matching-hash cached candidate vector,
reruns scoring, then asserts exact score equality and finite repaired cache
contents. The focused production change passed all 499 engine tests on the
latest main dependency lock, with two existing deprecation warnings. The
separate research archive also passed 512 tests, including 13 benchmark fixtures.

An independent valid-cache parity check covered 1,803 matching read vectors
(including 9 unrated reads) and 549 matching candidate vectors. Every value
was reused exactly and the provider was called zero times. Private receipt SHA-256:
`7852e9cb4336cce68429ac5215992a71e6af419d5b6f880eb4cf0be547214745`.

This is a generic correctness and reliability fix. It has no measured effect
on user preferences, ranking quality, or the case for changing shared ranker
weights.
