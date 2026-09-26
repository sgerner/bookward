# Shortlist and reading workflow

Books on the shortlist can move through three reading states:

| State | Meaning | Available actions |
| --- | --- | --- |
| Saved | A book you want to keep in mind | Mark it Up next, start reading, or mark it Finished |
| Reading | A book you have started | Move it back to Saved or mark it Finished |
| Finished | A book you have completed | Move it back to Reading |

Up next is a pin for books in Saved. Starting a book clears its pin. Finishing a book records it in reading history and accepts an optional rating from one to five stars. Returning a book to Saved clears its current progress and rating; the read-history entry remains available as a record of the earlier completion.

These fields are stored in SQLite in `reading_progress`, keyed to the recommendation candidate. The v16 migration creates the table and places existing saved and Librarr-imported candidates in Saved. Older recommendation clients can continue reading the existing `status` field; reading fields are additive.

## API

Use a Bookward API token from Settings → API access and send it as a bearer token. The API base URL is `/api/v1`.

List the shortlist:

```bash
curl https://your-bookward-host.example/api/v1/reading-list \
  -H 'Authorization: Bearer bkw_…'
```

The response includes each book's `reading_status`, `up_next`, `reading_rating`, `started_at`, and `finished_at`. The same fields are included on recommendation records when applicable.

Pin a saved book, start reading it, or finish it with an optional rating:

```bash
curl -X PUT https://your-bookward-host.example/api/v1/reading-list/42 \
  -H 'Authorization: Bearer bkw_…' \
  -H 'Content-Type: application/json' \
  -d '{"up_next":true}'

curl -X PUT https://your-bookward-host.example/api/v1/reading-list/42 \
  -H 'Authorization: Bearer bkw_…' \
  -H 'Content-Type: application/json' \
  -d '{"status":"reading"}'

curl -X PUT https://your-bookward-host.example/api/v1/reading-list/42 \
  -H 'Authorization: Bearer bkw_…' \
  -H 'Content-Type: application/json' \
  -d '{"status":"finished","rating":5}'
```

`up_next` can only be changed while a book is Saved. Reading-status updates apply to saved or Librarr-imported books. Ratings are integers from one to five and can be included when setting Finished.
