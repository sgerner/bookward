# Shortlist and read history

Bookward is for book discovery. A recommendation can be shortlisted, passed on, or set aside for later. The shortlist has no reading-progress shelves or status filters.

Use **Mark Read** on a shortlisted book to add it to the account's read history, with an optional one-to-five-star rating. It leaves the shortlist and appears under **Read** in history. A successful Librarr handoff also removes the book from the shortlist.

The v22 SQLite migration removes the obsolete `reading_progress` table and its Up next, Reading, and Finished records. Read history is stored separately in `reads` and remains intact.

## Shortlist API

List shortlisted recommendations with a bearer token from Settings → API access:

```bash
curl 'https://your-bookward-host.example/api/v1/recommendations?status=saved' \
  -H 'Authorization: Bearer bkw_…'
```

Use `POST /api/v1/recommendations/{id}/feedback` with `{"action":"save"}` to shortlist a recommendation. The separate reading-list and reading-progress endpoints have been removed.
