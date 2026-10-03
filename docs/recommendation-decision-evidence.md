# Recommendation decision evidence

Each recommendation run keeps a bounded copy of the inputs needed to replay the
online personalization and discovery-slate stages. It stores the ordered base,
personalized, and final pools; the interaction events used by the learner; the
actual cached float32 vectors passed to those stages; safe policy diagnostics;
and the request time and pagination/exploration parameters. It also keeps
independent copies of served cards and later first-party events and outcomes.
These copies survive ordinary source refresh cleanup, which may remove the
mutable catalog candidate and its older telemetry rows.

The captured base score is the value already materialized by the most recent
scoring job. The request does not have that job's historical read and candidate
inputs, so the evidence marks base-score derivation as `unknown`. A complete
record means the online serving stages can be replayed from their captured
inputs; it does not certify how the earlier scoring job produced its base
scores. Runtime source hashes identify the installed pipeline files when no
deployment build ID is available; they are not a substitute for a build
artifact or scoring-job record.

Evidence is local to the profile database and may contain book descriptions,
interaction history, and actual vector bytes. It does not copy session IDs,
provider credentials, or general settings. Capture is bounded to 5,000 rows in
each candidate pool, 5,000 interaction events, 6,000 vectors, 8,192 dimensions
per vector, 32 MiB of vector bytes, and 8 MiB of serialized evidence per run.
Served exposures, archived events, and outcomes have per-run caps of 5,000,
2,000, and 2,000 rows. A profile-wide 512 MiB evidence budget accounts for
payloads, vectors, links, and the JSON/data rows for exposures, events, and
outcomes. If a cap or budget is reached, the request still serves; replay
evidence is marked incomplete when its main snapshot cannot fit, and an
over-budget exposure, event, or outcome copy is skipped. SQLite page, index,
and WAL overhead is outside this approximate logical-payload budget.

An intentional deletion must remove any archived copy that contains the
deleted identity. Before deleting a candidate or clearing a reader's history,
the caller must invoke `purge_recommendation_evidence` in the same profile
database transaction, passing the affected candidate IDs or run IDs. Candidate
purge removes every run whose replay pool or served exposure included that
candidate, because another field in that run may contain its text or vector.
The helper also removes the candidate's ordinary feedback and recommendation
events, then deletes vector blobs no remaining run references. The current app
has no per-candidate delete or library/history reset endpoint; any such future
route must call this helper before removing source rows. Removing a profile
database removes its evidence too. Backups contain the evidence and restoring
one restores it, so a privacy deletion that includes backup copies must also
expire or replace those backups under the deployment's retention policy.

Routine source refresh is not an intentional privacy deletion and leaves the
run-linked evidence in place. This distinction keeps audit history available
through catalog churn while preserving an explicit purge path for user-directed
deletion.
