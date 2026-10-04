# Association source identity correction

The Open Library and Google Books association adapters now share the same
conservative read-identity rules used by recommendation eligibility. A known
author match uses normalized title/author aliases; a matching provider-scoped
work ID or checksum-valid ISBN can identify the same work across title or
author variations. Work IDs stay within their provider namespace. If the
source result or read history gives no author or the exact `Unknown author`
placeholder, a title-only fallback remains to avoid reintroducing a possible
read. Distinct books with known authors on both sides remain eligible.

This corrects a source-stage false exclusion: a candidate with the same title
as a read but a different known author is no longer discarded solely because
the title matches. The adapters collect optional association evidence and do
not change visible recommendation scores.

## Offline evidence and limits

An audit of one current production profile compared its 1,801-read ledger with
an 858-work frozen source pool. Five candidates had a normalized title match
to a read but a different author and no matching title/author, work ID, or
ISBN identity. Four remained among 739 candidates that passed the current
policy-eligibility checks; one of those four had a verified description among
the 89-card fresh synopsis pool. None of the five appeared in the 32 completed
fresh judgments.

This establishes a bounded correctness case in the source filter. The fresh
judgments provide no preference evidence for these candidates, and the single
profile audit does not establish a user-quality lift or a result for all
profiles. No scoring or visible source policy is changed.
