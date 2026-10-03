# Reading-experience source pilot — October 2, 2026

The fixed, evidence-backed reading-experience cues did not meet the frozen
source-coverage gate. The appeal component remains disabled, and no appeal or
residual model was fitted. Every aligned target retains the current score.

## Historical synopsis pilot

The cohort contains 1,677 eligible targets, with verified synopsis provenance
for 610 and verified catalog subjects for 724. The original report showed 322
verified subject fields because its provenance check used the eight-subject
feature limit; the corrected check now hashes all 12 subjects accepted by
production. The downstream topic representation remains capped at eight
subjects, and its subject-known mask covers 724 targets.

The original label-blind stratified sample of 60 targets is unchanged: its
frozen pre-correction strata contained 34 targets with no verified synopsis,
10 rich-fiction targets, 4 rich-nonfiction targets, and 12 with unknown genre.
Those are the strata used to select the original sample, not a resample under
the corrected verifier. The corrected 12-subject descriptive classification
has 1,067 targets with no verified synopsis, 479 rich-fiction targets, 7
rich-nonfiction targets, and 124 with unknown genre. The same 60 sample IDs
still contain a verified synopsis for 26; only one sampled synopsis has a span
matching a frozen cue phrase.

Across the full cohort, 45 targets have at least one literal cue: 10 pace, 33
tone, 1 density, and 2 structure matches. Only tone reaches the frozen minimum
of 20 matches in a cue family. In the first 300 targets, the family counts are
3 pace, 7 tone, 0 density, and 0 structure, below the required early-prefix
minimum of 8 per family. The corrected rerun reproduces these synopsis and cue
counts exactly.

The gate still fails. It required at least 100 targets with cues, at least 20
matches in each of three families, and sufficient early-prefix and source-audit
support. Unmatched cue families remain unknown; they are not treated as
evidence against a book. No outcome labels were accessed for this pilot. An
aligned fallback archive records the current score for all 1,677 targets
without fitting an appeal model. Its scores, IDs, and train/validation/later/OOF
masks match the original fallback archive exactly.

## Publisher-source check

A targeted check of ten previously unresolved cards found official publisher
description text with an exact ISBN and author/title identity for 4. Two more
descriptions matched only at the work/title-author level, with the card edition
unresolved. The remaining four were excluded because readable source text or a
unique matching official record was unavailable. Among the four exact-identity
descriptions, one contained a literal frozen cue phrase. This source check was
targeted rather than randomly sampled; it does not change the historical pilot's
coverage gate or show that other publishers or extraction methods would fail.

## Why four cards still said no verified synopsis

The four exact-identity cases are Penguin Random House candidates with direct
publisher product links and ISBNs. Their candidate records already contained
publisher descriptions. Re-running the existing host-restricted publisher
enrichment path on those links returned the same description text in all four
cases, and every page ISBN matched its candidate ISBN. No production write or
runtime change was needed to recover them.

The completed interest form used a deliberately narrower shared card resolver:
it fetched only each candidate's canonical Open Library work description so
current and union-pool cards received the same source treatment. The recovery
script likewise called the Open Library/Google Books catalog resolver using
title, author, and ISBN; it did not pass the candidate's publisher URL to the
existing Penguin Random House enrichment path. Thus “No verified synopsis
available” described that form's Open Library-only evidence path. It did not
mean that the candidate record lacked publisher copy or that production's
publisher enrichment had failed. Keep the completed form and first-round
answers unchanged; any form that uses publisher descriptions should be a
separate follow-up with the source and edition ISBN recorded for each card.

## Runtime implication

The runtime metadata resolver uses Open Library and Google Books catalog
records. Ingestion also has an HTTPS-host-restricted Penguin Random House
product-page enrichment path for items already linked from that publisher; it
checks page identity before applying publisher description metadata and records
field provenance. That path already served the four exact-identity candidate
descriptions. The ten-card check does not support adding generic publisher-page
scraping: publisher markup and edition identity vary, and several source records
were work-level or ambiguous. Keep unsupported descriptions unknown. A future
pilot should use one shared description resolver for every arm, preserve its
provider and edition evidence, and audit the source before extracting features.

A focused correctness follow-up makes the existing linked-product path require
a matching page ISBN whenever the input candidate supplies an ISBN, in addition
to its title/author match. Candidates without an ISBN still use the existing
work identity check. A reusable resolver returns the verified description and
provider evidence for future pilot cards. This prevents substituting another
edition's metadata; it does not establish recommendation lift. The four traced
production cases already pass the stricter identity check.

The cue extractor deliberately uses a small, fixed literal lexicon. Its low
coverage may reflect both sparse verified synopsis sources and alternate
wording the lexicon cannot recognize. These results do not establish that
publisher excerpts or other evidence-grounded approaches are ineffective.
