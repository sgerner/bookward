# Shortlist and read history

Bookward is for book discovery. A recommendation can be shortlisted, passed on, or set aside for later. The shortlist has no reading-progress shelves or status filters.

Use **Mark Read** on a shortlisted book to add it to the account's read history, with an optional one-to-five-star rating. It leaves the shortlist and appears under **Read** in history. A successful Librarr handoff also removes the book from the shortlist.

The v22 SQLite migration removes the obsolete `reading_progress` table and its Up next, Reading, and Finished records. Read history is stored separately in `reads` and remains intact.

The v27 SQLite migration adds `additional_authors` (defaulting to `''`) to `reads` and `goodreads_shelf_items` with no historical backfill. Existing installations match secondary co-authors from Goodreads only after re-importing a Goodreads CSV export (RSS feeds expose only the primary author). Candidate records do not store a separate `additional_authors` column, so structured secondary-author matching is asymmetric: reads and shelf items with `additional_authors` match secondary-author candidates, while single-author reads match co-authored candidates only when `candidates.author` lists the contributor inline or the rows share a valid ISBN or verified work ID.

## Shortlist API

List shortlisted recommendations with a bearer token from Settings → API access:

```bash
curl 'https://your-bookward-host.example/api/v1/recommendations?status=saved' \
  -H 'Authorization: Bearer bkw_…'
```

Use `POST /api/v1/recommendations/{id}/feedback` with `{"action":"save"}` to shortlist a recommendation. The separate reading-list and reading-progress endpoints have been removed.
