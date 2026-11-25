# Recommendation decision evidence

Each recommendation run keeps a bounded copy of the inputs needed to replay the
online personalization, impression-fatigue, optional LLM reranking, and
discovery-slate stages deterministically without network calls. It stores the ordered
base, personalized, and final pools (including `fatigue_penalty` when applied);
the interaction events used by the learner (including `release_date` and
`first_publication_year` for publication-era learning); unacted visible-day
counts and active impression-fatigue parameters; safe LLM reranker configuration
and validated adjustments or rank nudges; the actual cached float32 vectors
passed to those stages; safe policy diagnostics; and the request time and
pagination/exploration parameters. It also keeps independent copies of served
cards and later first-party events and outcomes. These copies survive ordinary
source refresh cleanup, which may remove the mutable catalog candidate and its
older telemetry rows.

Each successful scoring job now writes an immutable batch containing the exact
rated-read rows and vectors passed to the scorer, the full read identity
projection used for exclusion, candidate-scoped `to_read_candidate_ids` (recording
only which scored candidates in that batch matched the Goodreads `to-read` shelf,
without copying raw shelf titles, authors, or ISBNs), candidate scorer inputs and
vectors, runtime source hashes/dependency versions across all twenty ranking and
evidence modules, and both the pre-boost `base_score` and post-shelf `score` and
explanations written by that job. A candidate row points to the batch that
produced its current materialized score. Serving evidence checks that pointer
against the stored score and book identity, and separately reports whether the
candidate's serving-time embedding document still matches the original scorer
input. A later description or genre edit can make inputs stale while the archived
batch still reproduces the historical score. Archive loaders and replay helpers
accept both legacy schema v1 and extended schema v2 snapshots. Scores without a
valid complete batch link remain `unknown`, `incomplete`, or `partial`; a serving
run is complete when its own online stages are replayable, regardless of older
unlinked base scores. Runtime source hashes identify installed pipeline files
when no deployment build ID is available; they are not a substitute for a build
artifact.

Evidence is local to the profile database and may contain book descriptions,
interaction history, and actual vector bytes. It does not copy unscored Goodreads
shelf contents, session IDs, API keys, bearer tokens, URL paths/queries/fragments
or userinfo on LLM endpoints (persisting only scheme/host/port origin), or
general settings. Capture is bounded to 5,000 rows in
each candidate pool, 5,000 interaction events, 6,000 vectors, 8,192 dimensions
per vector, 32 MiB of vector bytes, and 8 MiB of serialized evidence per run.
Served exposures, archived events, and outcomes have per-run caps of 5,000,
2,000, and 2,000 rows. A scoring batch is capped at 10,000 history rows, 5,000
candidate inputs/outputs, 15,000 vectors, 8,192 dimensions per vector, 64 MiB of
vector bytes, and 16 MiB of serialized evidence. Both serving and scoring
evidence share a profile-wide 512 MiB logical budget that includes payloads,
deduplicated vector bytes, reference rows, exposure/event/outcome data, and
scoring-batch read links. Replay-evidence watermark rotation is conservatively
off by default (`AFTERWORD_EVIDENCE_ROTATION_ENABLED=false`) and decoupled from
scheduled operational/telemetry retention (`AFTERWORD_RETENTION_ENABLED=false`);
either can be opted into globally via environment variables or per profile in
Settings -> Backup & recovery. When evidence rotation is enabled and a new
capture sees usage cross the 80 percent high watermark of that 512 MiB quota,
Bookward rotates unreferenced scoring batches and the oldest unattributed
run-linked evidence down to 65 percent of quota. Runs with attributed outcomes
(`recommendation_outcomes` or `recommendation_evidence_outcomes`) or
non-`visible` user events (`recommendation_evidence_events`), active
`candidates.score_batch_id` batches, retained or attributed run score-batch
links (`recommendation_run_evidence_scoring_batches`), `recommendation_events`,
and `feedback` are never evicted by rotation or scheduled telemetry pruning.
When evidence rotation is off, or if protected attributed evidence alone fills
the 512 MiB budget, scoring writes its score outputs and a small `incomplete`
batch marker when possible; the normal recommendation request still serves and
marks its run snapshot incomplete only when that snapshot cannot fit. SQLite
page, index, and WAL overhead is outside this approximate logical-payload
budget.

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
batches indexed by those IDs, pre-promotion scoring batches that recorded a
Goodreads `to-read` boost for those read identities, serving runs linked to
those batches, and first-party read event/outcome copies keyed to the selected
ID. When Goodreads shelf items are removed during shelf re-import or deleted
without being read, pass `shelf_items=[...]` to `purge_recommendation_evidence`
in the same transaction; this removes every scoring batch and serving run that
recorded shelf-derived boosts or explanations for the removed shelf items while
preserving user feedback rows. Shelf rows removed because a book transitioned
into `reads` (`import_csv`, `import_goodreads_rss`, and `_apply_goodreads_shelves`)
intentionally retain prior batches and runs so `attribute_read_outcomes` and
base-score provenance stay intact while the book remains in `reads`. This is a
targeted cleanup helper, not a complete library-history privacy reset: legacy
or unlinked serving snapshots and materialized scores/explanations may still
reflect that history. A library or profile history reset must
call `purge_recommendation_evidence(connection, clear_all_evidence=True)` before
deleting live history; this clears all run snapshots, scorer batches, archived
events/outcomes/exposures, and live `recommendation_events` while preserving
user `feedback` (`save`/`reject`) curation rows. These helpers only purge
evidence; the route remains responsible for deleting the live source rows (and
explicitly deleting `feedback` alongside resetting `candidates.status` if
shortlist/dismissed curation is also being cleared) in the same transaction. A
read/history reset must also clear
or recompute materialized candidate scores and explanation strings derived
from the deleted reads before those candidates are served again; purging the
archive alone does not remove those live derived values. The current app exposes profile history reset via `POST /auth/account/reset-history`
(under **Account → Reset reading history**), which calls
`purge_recommendation_evidence(connection, clear_all_evidence=True)`, deletes
read/import/embedding/association history, and zeroes materialized candidate
scores and explanations while preserving the profile's backup snapshots as
restore points. It still has no per-candidate delete endpoint. Deleting an
account via `DELETE /auth/account` or `DELETE /admin/accounts/{account_id}`
removes the profile database, its decision evidence, and its per-profile backup
snapshots, scrubs the erased account from retained `auth-registry/` snapshots,
and writes a durable tombstone marker so restarts never recreate deleted profile
files; disabled profiles also have expired snapshots pruned on the scheduled
backup window.

Routine source refresh is not an intentional privacy deletion and leaves the
run-linked evidence in place. This distinction keeps audit history available
through catalog churn while preserving an explicit purge path for user-directed
deletion.
