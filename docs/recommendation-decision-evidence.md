# Recommendation decision evidence

Each recommendation run keeps a bounded copy of the inputs needed to replay the
online personalization and discovery-slate stages. It stores the ordered base,
personalized, and final pools; the interaction events used by the learner; the
actual cached float32 vectors passed to those stages; safe policy diagnostics;
and the request time and pagination/exploration parameters. It also keeps
independent copies of served cards and later first-party events and outcomes.
These copies survive ordinary source refresh cleanup, which may remove the
mutable catalog candidate and its older telemetry rows.

Each successful scoring job now writes an immutable batch containing the exact
rated-read rows and vectors passed to the scorer, the full read identity
projection used for exclusion, candidate scorer inputs and vectors, runtime
source hashes/dependency versions, and the exact scores and explanations
written by that job. A candidate row points to the batch that produced its
current materialized score. Serving evidence checks that pointer against the
stored score and book identity, and separately reports whether the candidate's
serving-time embedding document still matches the original scorer input. A
later description or genre edit can make inputs stale while the archived batch
still reproduces the historical score. Scores without a valid complete batch
link remain `unknown`, `incomplete`, or `partial`; a serving run is complete
when its own online stages are replayable, regardless of older unlinked base
scores. Runtime source hashes identify installed pipeline files when no
deployment build ID is available; they are not a substitute for a build
artifact.

Evidence is local to the profile database and may contain book descriptions,
interaction history, and actual vector bytes. It does not copy session IDs,
provider credentials, or general settings. Capture is bounded to 5,000 rows in
each candidate pool, 5,000 interaction events, 6,000 vectors, 8,192 dimensions
per vector, 32 MiB of vector bytes, and 8 MiB of serialized evidence per run.
Served exposures, archived events, and outcomes have per-run caps of 5,000,
2,000, and 2,000 rows. A scoring batch is capped at 10,000 history rows, 5,000
candidate inputs/outputs, 15,000 vectors, 8,192 dimensions per vector, 64 MiB
of vector bytes, and 16 MiB of serialized evidence. Both serving and scoring
evidence share a profile-wide 512 MiB logical budget that includes payloads,
deduplicated vector bytes, reference rows, exposure/event/outcome data, and
scoring-batch read links. If a cap or budget is reached, scoring still writes
its score outputs and a small `incomplete` batch marker when possible; the
normal recommendation request still serves and marks its run snapshot
incomplete only when that snapshot cannot fit. SQLite page, index, and WAL
overhead is outside this approximate logical-payload budget.

An intentional deletion must remove every archived copy that contains the
deleted identity. Run snapshots keep normalized links to every referenced
scoring batch so purging a batch also removes those run payloads, rather than
leaving stale score-lineage claims. Before deleting one candidate, call
`purge_recommendation_evidence(connection, candidate_ids=[...])` in the same
profile transaction; this removes every serving run and scoring batch that
contains that candidate, including shared pool/input text and vectors, clears
current score pointers for other candidates in those batches, and removes
ordinary feedback/events for the deleted candidate. Before deleting individual
read-history rows, pass their IDs as `read_ids=[...]`; each scoring batch that
copied any selected read is removed as a unit, including its candidate score
links and read/candidate vectors. Passing `read_ids=[...]` also removes scoring
batches indexed by those IDs, serving runs linked to those batches, and
first-party read event/outcome copies keyed to the selected ID. This is a
targeted cleanup helper, not a complete library-history privacy reset: legacy
or unlinked serving snapshots and materialized scores/explanations may still
reflect that history. A library or profile history reset must
call `purge_recommendation_evidence(connection, clear_all_evidence=True)` before
deleting live history; this clears all run snapshots, scorer batches, archived
events/outcomes/exposures, and ordinary feedback/recommendation events. These
helpers only purge evidence; the route remains responsible for deleting the
live source rows in the same transaction. A read/history reset must also clear
or recompute materialized candidate scores and explanation strings derived
from the deleted reads before those candidates are served again; purging the
archive alone does not remove those live derived values. The current app has
no per-candidate delete or library/history reset endpoint. Removing a profile
database removes its evidence too. Backups contain the evidence and restoring
one restores it, so a privacy deletion that includes backup copies must also
expire or replace those backups under the deployment's retention policy.

Routine source refresh is not an intentional privacy deletion and leaves the
run-linked evidence in place. This distinction keeps audit history available
through catalog churn while preserving an explicit purge path for user-directed
deletion.
