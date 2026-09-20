import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import date, timedelta
from .config import settings
from .covers import canonical_book_source_url, fallback_cover_url, is_weak_cover_url
from .identity import book_identity

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, secret INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS reads (id INTEGER PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL, rating REAL, read_at TEXT, isbn TEXT, source TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(title, author));
CREATE TABLE IF NOT EXISTS sources (id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'web', enabled INTEGER NOT NULL DEFAULT 1, is_default INTEGER NOT NULL DEFAULT 0, weight REAL NOT NULL DEFAULT 1, last_status TEXT, last_scanned_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS candidates (id INTEGER PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', cover_url TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '', source_id INTEGER REFERENCES sources(id), release_date TEXT, date_kind TEXT NOT NULL DEFAULT 'unknown', genres TEXT NOT NULL DEFAULT '[]', score REAL NOT NULL DEFAULT 0, explanation TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'new', normalized_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS embeddings (entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, backend TEXT NOT NULL, model TEXT NOT NULL, vector BLOB NOT NULL, dimensions INTEGER NOT NULL, content_hash TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(entity_type, entity_id, backend, model));
CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY, candidate_id INTEGER NOT NULL REFERENCES candidates(id), action TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0, result TEXT, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, started_at TEXT, finished_at TEXT);
CREATE TABLE IF NOT EXISTS librarr_imports (id INTEGER PRIMARY KEY, candidate_id INTEGER NOT NULL REFERENCES candidates(id), idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL, remote_id TEXT, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_candidates_status_score ON candidates(status, score DESC);
CREATE INDEX IF NOT EXISTS idx_candidates_source ON candidates(source_id);
CREATE INDEX IF NOT EXISTS idx_reads_rating ON reads(rating);
"""
MIGRATIONS = [
    (1, SCHEMA),
    (
        2,
        """
        ALTER TABLE sources ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'permanent';
        CREATE INDEX IF NOT EXISTS idx_sources_lifecycle_enabled ON sources(lifecycle, enabled, last_scanned_at);
        """,
    ),
    (
        3,
        """
        CREATE TABLE IF NOT EXISTS digest_items (
            candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
            first_period TEXT NOT NULL,
            first_sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS notification_deliveries (
            id TEXT PRIMARY KEY,
            period_key TEXT NOT NULL,
            channel TEXT NOT NULL,
            status TEXT NOT NULL,
            recipient TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '{}',
            candidate_ids TEXT NOT NULL DEFAULT '[]',
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TEXT,
            UNIQUE(period_key, channel)
        );
        CREATE INDEX IF NOT EXISTS idx_notification_deliveries_created
            ON notification_deliveries(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_notification_deliveries_status
            ON notification_deliveries(status, updated_at);
        """,
    ),
    (
        4,
        """
        CREATE TABLE IF NOT EXISTS api_tokens (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            token_prefix TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at TEXT,
            revoked_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_api_tokens_active
            ON api_tokens(revoked_at, created_at DESC);
        """,
    ),
    (
        5,
        """
        CREATE TABLE IF NOT EXISTS recommendation_runs (
            id TEXT PRIMARY KEY,
            policy TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            candidate_count INTEGER NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS recommendation_impressions (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES recommendation_runs(id) ON DELETE CASCADE,
            candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
            rank INTEGER NOT NULL CHECK(rank > 0),
            score REAL NOT NULL,
            propensity REAL NOT NULL CHECK(propensity > 0 AND propensity <= 1),
            presented_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            visible_at TEXT,
            UNIQUE(run_id, candidate_id)
        );
        CREATE TABLE IF NOT EXISTS recommendation_events (
            id INTEGER PRIMARY KEY,
            event_key TEXT NOT NULL UNIQUE,
            run_id TEXT REFERENCES recommendation_runs(id) ON DELETE SET NULL,
            candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            value REAL,
            source TEXT NOT NULL,
            occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS recommendation_outcomes (
            id INTEGER PRIMARY KEY,
            impression_id INTEGER NOT NULL REFERENCES recommendation_impressions(id) ON DELETE CASCADE,
            event_id INTEGER NOT NULL REFERENCES recommendation_events(id) ON DELETE CASCADE,
            read_id INTEGER REFERENCES reads(id) ON DELETE SET NULL,
            label REAL NOT NULL,
            label_kind TEXT NOT NULL,
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            attributed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(impression_id, event_id)
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_impressions_candidate_time
            ON recommendation_impressions(candidate_id, presented_at DESC);
        CREATE INDEX IF NOT EXISTS idx_recommendation_impressions_run_rank
            ON recommendation_impressions(run_id, rank);
        CREATE INDEX IF NOT EXISTS idx_recommendation_events_candidate_time
            ON recommendation_events(candidate_id, occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_recommendation_events_type_time
            ON recommendation_events(event_type, occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_recommendation_outcomes_read
            ON recommendation_outcomes(read_id, attributed_at DESC);
        """,
    ),
    (
        6,
        """
        CREATE TABLE IF NOT EXISTS llm_connections (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            endpoint TEXT NOT NULL DEFAULT '',
            auth_type TEXT NOT NULL DEFAULT 'api_key',
            secret TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            last_status TEXT,
            last_error TEXT,
            last_used_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(auth_type IN ('api_key'))
        );
        CREATE INDEX IF NOT EXISTS idx_llm_connections_enabled
            ON llm_connections(enabled, updated_at DESC);
        CREATE TABLE IF NOT EXISTS llm_policies (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            connection_id INTEGER NOT NULL REFERENCES llm_connections(id),
            enabled INTEGER NOT NULL DEFAULT 1,
            top_k INTEGER NOT NULL DEFAULT 20,
            prompt_version TEXT NOT NULL DEFAULT 'shadow-v1',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(top_k BETWEEN 1 AND 100)
        );
        CREATE INDEX IF NOT EXISTS idx_llm_policies_enabled
            ON llm_policies(enabled, updated_at DESC);
        CREATE TABLE IF NOT EXISTS llm_runs (
            id TEXT PRIMARY KEY,
            policy_id INTEGER NOT NULL REFERENCES llm_policies(id),
            connection_id INTEGER NOT NULL REFERENCES llm_connections(id),
            request_hash TEXT NOT NULL,
            candidate_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            latency_ms INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_llm_runs_policy_created
            ON llm_runs(policy_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_llm_runs_request
            ON llm_runs(request_hash, created_at DESC);
        CREATE TABLE IF NOT EXISTS llm_scores (
            run_id TEXT NOT NULL REFERENCES llm_runs(id) ON DELETE CASCADE,
            candidate_id INTEGER NOT NULL REFERENCES candidates(id),
            rank INTEGER NOT NULL,
            score REAL NOT NULL,
            confidence REAL,
            reason_codes TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY(run_id, candidate_id),
            UNIQUE(run_id, rank)
        );
        CREATE INDEX IF NOT EXISTS idx_llm_scores_candidate
            ON llm_scores(candidate_id, run_id);
        """,
    ),
]

# Digest settings are stored in the same encrypted key/value store as the
# other installation settings. The API exposes a safe, typed projection while
# this map documents the defaults and lets existing installations upgrade
# without a separate config file.
DIGEST_SETTING_DEFAULTS = {
    "digest_enabled": "0",
    "digest_channels": "",
    "digest_day": "1",
    "digest_time": "09:00",
    "digest_timezone": "UTC",
    "digest_minimum_score": "80",
    "digest_maximum_books": "5",
    "digest_only_new": "1",
    "digest_app_url": settings.public_url,
    "digest_discord_webhook_url": "",
    "digest_email_to": "",
    "digest_email_from": "",
    "digest_smtp_host": "",
    "digest_smtp_port": "587",
    "digest_smtp_security": "starttls",
    "digest_smtp_username": "",
    "digest_smtp_password": "",
    "digest_last_period": "",
}

# These feeds are intentionally public and require no per-user credentials.
# Keep the NYT API visible but disabled because its overview endpoint returns
# 401 without a user API key; users can add their keyed URL when they want it.
DEFAULT_SOURCES = (
    ("Apple Books · Top audiobooks", "https://itunes.apple.com/us/rss/topaudiobooks/limit=50/xml", 1),
    ("Apple Books · Top paid ebooks", "https://itunes.apple.com/us/rss/toppaidebooks/limit=50/xml", 1),
    ("Open Library · Science fiction", "https://openlibrary.org/subjects/science_fiction.json?limit=50", 1),
    ("Open Library · Fantasy", "https://openlibrary.org/subjects/fantasy.json?limit=50", 1),
    ("Goodreads · Science fiction", "https://www.goodreads.com/genres/science-fiction", 1),
    ("Goodreads · Speculative fiction", "https://www.goodreads.com/genres/speculative-fiction", 1),
    ("Goodreads · Mystery thriller", "https://www.goodreads.com/genres/mystery-thriller", 1),
    ("Goodreads · Literary fiction", "https://www.goodreads.com/genres/literary-fiction", 1),
    ("Open Library · Mystery & detective", "https://openlibrary.org/subjects/mystery_and_detective_stories.json?limit=50", 0),
    ("Open Library · Literary fiction", "https://openlibrary.org/subjects/literary_fiction.json?limit=50", 0),
    ("NYT Books overview · API key required", "https://api.nytimes.com/svc/books/v3/lists/overview.json", 0),
)

def connect():
    Path(settings.db).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(settings.db, timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.create_function("book_identity", 2, book_identity, deterministic=True)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    return con

@contextmanager
def transaction():
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def initialize():
    with connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        applied = {item[0] for item in con.execute("SELECT version FROM schema_migrations")}
        for version, script in MIGRATIONS:
            if version not in applied:
                con.executescript(script)
                con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
        con.execute(
            "INSERT OR IGNORE INTO settings(key,value,secret) VALUES(?,?,0)",
            ("source_sync_interval_hours", str(settings.source_sync_interval_hours)),
        )
        for key, value in DIGEST_SETTING_DEFAULTS.items():
            # Credential-shaped values are marked secret even when empty so a
            # later update never accidentally returns or logs them in plain
            # text. Blank secrets are preserved by the settings endpoint.
            secret = 1 if key in {
                "digest_discord_webhook_url",
                "digest_smtp_username",
                "digest_smtp_password",
            } else 0
            con.execute(
                "INSERT OR IGNORE INTO settings(key,value,secret) VALUES(?,?,?)",
                (key, value, secret),
            )
        # Upgrade an older install's placeholder link when the deployment now
        # advertises a different public origin, without overwriting a URL the
        # user explicitly chose in Settings.
        if settings.public_url and settings.public_url != "http://localhost:5173":
            con.execute(
                "UPDATE settings SET value=?,updated_at=CURRENT_TIMESTAMP WHERE key='digest_app_url' AND value='http://localhost:5173'",
                (settings.public_url,),
            )
        con.execute("INSERT OR IGNORE INTO sources(name,url,kind,enabled,is_default) VALUES(?,?,?,?,?)", ("Bookward demo upcoming books", "builtin://upcoming", "builtin", 1, 1))
        con.execute(
            "UPDATE sources SET name=? WHERE url='builtin://upcoming' AND name='Afterword demo upcoming books'",
            ("Bookward demo upcoming books",),
        )
        builtin_id = con.execute("SELECT id FROM sources WHERE url='builtin://upcoming'").fetchone()[0]
        for name, url, enabled in DEFAULT_SOURCES:
            con.execute(
                "INSERT OR IGNORE INTO sources(name,url,kind,enabled,is_default) VALUES(?,?,?,?,0)",
                (name, url, "web", enabled),
            )
        seed = Path(__file__).with_name("seed.json")
        seed_items = json.loads(seed.read_text())
        if not con.execute("SELECT 1 FROM candidates LIMIT 1").fetchone():
            for item in seed_items:
                release_date = (date.today() + timedelta(days=int(item.get("release_offset_days", 30)))).isoformat()
                con.execute("""INSERT OR IGNORE INTO candidates(title,author,description,cover_url,source_url,source_id,release_date,date_kind,genres,score,explanation,status,normalized_key)
                VALUES(?,?,?,?,?,?,?, ?,?,?,?,'recommended',?)""", (item["title"], item["author"], item["description"], fallback_cover_url(item["title"], item["author"], item.get("cover_url", ""), item.get("source_url", "")), canonical_book_source_url(item["title"], item["author"], item.get("source_url", "")), builtin_id, release_date, "demo", json.dumps(item["genres"]), item["score"], json.dumps(item["explanation"]), normalize_key(item["title"], item["author"])))
            for title, author, rating in [("Sea of Tranquility","Emily St. John Mandel",5),("The Fifth Season","N. K. Jemisin",5),("Piranesi","Susanna Clarke",4.5),("The Only Good Indians","Stephen Graham Jones",4)]:
                con.execute("INSERT OR IGNORE INTO reads(title,author,rating,source) VALUES(?,?,?,'demo')", (title,author,rating))
        # Keep an existing self-hosted installation from displaying the old
        # Open Library ISBN URLs that render as 1x1 transparent GIFs.  This is
        # deliberately network-free; the next refresh can enrich placeholders
        # through the provider resolver.
        for item in seed_items:
            key = normalize_key(item["title"], item["author"])
            current = con.execute("SELECT id,cover_url,source_url FROM candidates WHERE normalized_key=?", (key,)).fetchone()
            if current:
                source_value = canonical_book_source_url(item["title"], item["author"], current["source_url"] or item.get("source_url", ""))
                if source_value and source_value != current["source_url"]:
                    con.execute("UPDATE candidates SET source_url=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (source_value, current["id"]))
            if current and (is_weak_cover_url(current["cover_url"]) or current["cover_url"].startswith("https://placehold.co/")) and not is_weak_cover_url(item.get("cover_url", "")):
                con.execute("UPDATE candidates SET cover_url=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (fallback_cover_url(item["title"], item["author"], item.get("cover_url", ""), item.get("source_url", "")), current["id"]))

def normalize_key(title: str, author: str):
    import re
    return re.sub(r"[^a-z0-9]+", " ", f"{title} {author}".lower()).strip()

def rows(query: str, params=()):
    with connect() as con:
        return [dict(row) for row in con.execute(query, params).fetchall()]

def row(query: str, params=()):
    with connect() as con:
        found = con.execute(query, params).fetchone()
        return dict(found) if found else None
