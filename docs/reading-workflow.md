# Shortlist and read history

Bookward is for book discovery. A recommendation can be shortlisted, passed on, or set aside for later. The shortlist has no reading-progress shelves or status filters.

Use **Mark Read** on a shortlisted book to add it to the account's read history, with an optional one-to-five-star rating. It leaves the shortlist and appears under **Read** in history.

A successful Librarr download or waitlist handoff keeps an unread book on the shortlist as `imported` (with the **Added to Librarr** badge) until you mark it Read or remove it. Shortlisted `imported` cards hide duplicate waitlist prompts while keeping **Find in Librarr** (when Librarr is connected, so you can grab another format or replacement file), **Mark Read**, and **Remove** (which returns the book to Discover as a neutral `restore` decision with **Undo** support, without adding a false read).

The v22 SQLite migration removes the obsolete `reading_progress` table and its Up next, Reading, and Finished records. Read history is stored separately in `reads` and remains intact.

## Shortlist API

List shortlisted recommendations (including both `saved` and Librarr-`imported` books) with a bearer token from Settings → API access:

```bash
curl 'https://your-bookward-host.example/api/v1/recommendations?status=saved' \
  -H 'Authorization: Bearer bkw_…'
```

Pass `status=imported` to return only Librarr-acquired shortlist books:

```bash
curl 'https://your-bookward-host.example/api/v1/recommendations?status=imported' \
  -H 'Authorization: Bearer bkw_…'
```

Use `POST /api/v1/recommendations/{id}/feedback` with `{"action":"save"}` to shortlist a recommendation, or `{"action":"restore"}` (followed optionally by `POST /api/v1/recommendations/{id}/undo`) to remove a `saved` or `imported` book from the shortlist and return it to Discover. The separate reading-list and reading-progress endpoints have been removed.
