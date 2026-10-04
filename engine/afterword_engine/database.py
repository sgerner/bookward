import json
import sqlite3
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from .config import settings
from .covers import (
    METADATA_NORMALIZATION_VERSION,
    canonical_book_source_url,
    fallback_cover_url,
    is_weak_cover_url,
)
from .identity import book_identity, book_identity_matches
from .isbn import isbn_parts_from_amazon_url
from .secrets import unseal
from .tenancy import profile_database_path

METADATA_PROVENANCE_MAX_BYTES = 8192

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
            CHECK(auth_type IN ('api_key','openai_codex','claude_code'))
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
    (
        7,
        """
        CREATE TABLE IF NOT EXISTS association_runs (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            status TEXT NOT NULL,
            seed_count INTEGER NOT NULL DEFAULT 0,
            edge_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_association_runs_provider_started
            ON association_runs(provider, started_at DESC);

        CREATE TABLE IF NOT EXISTS association_requests (
            id INTEGER PRIMARY KEY,
            provider TEXT NOT NULL,
            request_key TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_association_requests_provider_time
            ON association_requests(provider, requested_at DESC);

        CREATE TABLE IF NOT EXISTS association_cache (
            provider TEXT NOT NULL,
            cache_key TEXT NOT NULL,
            payload TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY(provider, cache_key)
        );
        CREATE INDEX IF NOT EXISTS idx_association_cache_expiry
            ON association_cache(provider, expires_at);

        CREATE TABLE IF NOT EXISTS association_evidence (
            provider TEXT NOT NULL,
            seed_read_id INTEGER NOT NULL REFERENCES reads(id) ON DELETE CASCADE,
            candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
            external_id TEXT NOT NULL,
            provider_rank INTEGER,
            source_url TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            run_id TEXT REFERENCES association_runs(id) ON DELETE SET NULL,
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT,
            PRIMARY KEY(provider, seed_read_id, external_id)
        );
        CREATE INDEX IF NOT EXISTS idx_association_evidence_candidate
            ON association_evidence(candidate_id, provider);
        CREATE INDEX IF NOT EXISTS idx_association_evidence_seed
            ON association_evidence(seed_read_id, provider);
        CREATE INDEX IF NOT EXISTS idx_reads_recent
            ON reads(COALESCE(read_at, created_at) DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_queue
            ON jobs(status, created_at);
        """,
    ),
    (
        8,
        """
        CREATE TABLE IF NOT EXISTS candidate_quality (
            candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
            quality_status TEXT NOT NULL DEFAULT 'pending'
                CHECK(quality_status IN ('pending','accepted','quarantine','rejected')),
            quality_score REAL NOT NULL DEFAULT 0
                CHECK(quality_score >= 0 AND quality_score <= 1),
            flags_json TEXT NOT NULL DEFAULT '[]',
            provider TEXT NOT NULL DEFAULT '',
            provider_id TEXT NOT NULL DEFAULT '',
            work_id TEXT NOT NULL DEFAULT '',
            isbn13 TEXT NOT NULL DEFAULT '',
            isbn10 TEXT NOT NULL DEFAULT '',
            title_match REAL NOT NULL DEFAULT 0,
            author_match REAL NOT NULL DEFAULT 0,
            audit_version TEXT NOT NULL DEFAULT 'candidate-quality-v1',
            audited_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_candidate_quality_status
            ON candidate_quality(quality_status, quality_score DESC);
        CREATE INDEX IF NOT EXISTS idx_candidate_quality_isbn13
            ON candidate_quality(isbn13);
        CREATE TRIGGER IF NOT EXISTS candidate_quality_after_insert
        AFTER INSERT ON candidates
        BEGIN
            INSERT OR IGNORE INTO candidate_quality(candidate_id, quality_status)
            SELECT NEW.id,
                CASE WHEN EXISTS(
                    SELECT 1 FROM sources WHERE id=NEW.source_id AND kind='builtin'
                ) THEN 'accepted' ELSE 'pending' END;
        END;
        INSERT OR IGNORE INTO candidate_quality(candidate_id, quality_status, audit_version)
            SELECT id, 'accepted', 'legacy-pending-audit-v1' FROM candidates;
        """,
    ),
    (
        9,
        """
        ALTER TABLE llm_policies ADD COLUMN reasoning_effort TEXT NOT NULL DEFAULT 'medium';
        -- gpt-5 is an API model and cannot be selected by a ChatGPT account
        -- through Codex app-server.  Existing device-login connections used
        -- this early placeholder, so move them to a subscription model that
        -- the current app-server catalog advertises.
        UPDATE llm_connections
        SET model_id='gpt-5.6-terra', updated_at=CURRENT_TIMESTAMP
        WHERE auth_type='openai_codex' AND model_id='gpt-5';
        """,
    ),
    (
        10,
        """
        ALTER TABLE candidates ADD COLUMN isbn13 TEXT NOT NULL DEFAULT '';
        ALTER TABLE candidates ADD COLUMN isbn10 TEXT NOT NULL DEFAULT '';
        CREATE INDEX IF NOT EXISTS idx_candidates_isbn13 ON candidates(isbn13);
        CREATE INDEX IF NOT EXISTS idx_candidates_isbn10 ON candidates(isbn10);
        """,
    ),
    (
        11,
        """
        ALTER TABLE sources ADD COLUMN filters TEXT NOT NULL DEFAULT '{}';
        """,
    ),
    (
        12,
        """
        -- Remove the retired provider connections, policies, and run history
        -- so stored credentials do not survive the feature removal.
        DROP TABLE IF EXISTS llm_scores;
        DROP TABLE IF EXISTS llm_runs;
        DROP TABLE IF EXISTS llm_policies;
        DROP TABLE IF EXISTS llm_connections;
        DELETE FROM settings WHERE key GLOB 'llm_*' OR key GLOB 'models_catalog*';
        """,
    ),
    (
        13,
        """
        ALTER TABLE candidate_quality ADD COLUMN metadata_checked_at TEXT;
        ALTER TABLE candidate_quality ADD COLUMN metadata_provider TEXT NOT NULL DEFAULT '';
        ALTER TABLE candidate_quality ADD COLUMN metadata_provider_id TEXT NOT NULL DEFAULT '';
        CREATE INDEX IF NOT EXISTS idx_candidate_quality_metadata_checked
            ON candidate_quality(quality_status, metadata_checked_at);
        """,
    ),
    (
        14,
        """
        DROP TRIGGER IF EXISTS candidate_quality_after_insert;
        DROP INDEX IF EXISTS idx_candidate_quality_status;
        DROP INDEX IF EXISTS idx_candidate_quality_isbn13;
        DROP INDEX IF EXISTS idx_candidate_quality_metadata_checked;
        ALTER TABLE candidate_quality RENAME TO candidate_quality_before_metadata_confidence;
        CREATE TABLE candidate_quality (
            candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
            quality_status TEXT NOT NULL DEFAULT 'pending'
                CHECK(quality_status IN ('pending','accepted','quarantine','rejected')),
            quality_score REAL NOT NULL DEFAULT 0
                CHECK(quality_score >= 0 AND quality_score <= 1),
            flags_json TEXT NOT NULL DEFAULT '[]',
            provider TEXT NOT NULL DEFAULT '',
            provider_id TEXT NOT NULL DEFAULT '',
            work_id TEXT NOT NULL DEFAULT '',
            isbn13 TEXT NOT NULL DEFAULT '',
            isbn10 TEXT NOT NULL DEFAULT '',
            title_match REAL NOT NULL DEFAULT 0,
            author_match REAL NOT NULL DEFAULT 0,
            audit_version TEXT NOT NULL DEFAULT 'candidate-quality-v1',
            audited_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            metadata_checked_at TEXT,
            metadata_provider TEXT NOT NULL DEFAULT '',
            metadata_provider_id TEXT NOT NULL DEFAULT '',
            metadata_confidence REAL NOT NULL DEFAULT 0.5
                CHECK(metadata_confidence >= 0 AND metadata_confidence <= 1)
        );
        INSERT INTO candidate_quality(
            candidate_id,quality_status,quality_score,flags_json,provider,provider_id,
            work_id,isbn13,isbn10,title_match,author_match,audit_version,audited_at,
            created_at,updated_at,metadata_checked_at,metadata_provider,
            metadata_provider_id,metadata_confidence
        )
        SELECT candidate_id,quality_status,quality_score,flags_json,provider,provider_id,
            work_id,isbn13,isbn10,title_match,author_match,audit_version,audited_at,
            created_at,updated_at,metadata_checked_at,metadata_provider,
            metadata_provider_id,0.5
        FROM candidate_quality_before_metadata_confidence;
        DROP TABLE candidate_quality_before_metadata_confidence;
        CREATE INDEX idx_candidate_quality_status
            ON candidate_quality(quality_status, quality_score DESC);
        CREATE INDEX idx_candidate_quality_isbn13 ON candidate_quality(isbn13);
        CREATE INDEX idx_candidate_quality_metadata_checked
            ON candidate_quality(quality_status, metadata_checked_at);
        CREATE TRIGGER candidate_quality_after_insert
        AFTER INSERT ON candidates
        BEGIN
            INSERT OR IGNORE INTO candidate_quality(candidate_id, quality_status)
            SELECT NEW.id,
                CASE WHEN EXISTS(
                    SELECT 1 FROM sources WHERE id=NEW.source_id AND kind='builtin'
                ) THEN 'accepted' ELSE 'pending' END;
        END;
        """,
    ),
    (
        15,
        """
        ALTER TABLE reads ADD COLUMN openlibrary_work_id TEXT NOT NULL DEFAULT '';
        ALTER TABLE reads ADD COLUMN openlibrary_lookup_attempted_at TEXT;
        CREATE INDEX IF NOT EXISTS idx_reads_openlibrary_work_id
            ON reads(openlibrary_work_id);
        """,
    ),
    (
        16,
        """
        CREATE TABLE IF NOT EXISTS reading_progress (
            candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'saved'
                CHECK(status IN ('saved','reading','finished')),
            up_next INTEGER NOT NULL DEFAULT 0 CHECK(up_next IN (0,1)),
            rating INTEGER CHECK(rating IS NULL OR rating BETWEEN 1 AND 5),
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(up_next=0 OR status='saved')
        );
        CREATE INDEX IF NOT EXISTS idx_reading_progress_shelf
            ON reading_progress(status,up_next,updated_at DESC);
        INSERT OR IGNORE INTO reading_progress(candidate_id,status)
            SELECT id,'saved' FROM candidates WHERE status IN ('saved','imported');
        """,
    ),
    (
        17,
        """
        ALTER TABLE feedback ADD COLUMN previous_status TEXT NOT NULL DEFAULT '';
        ALTER TABLE feedback ADD COLUMN undone_at TEXT;
        CREATE INDEX IF NOT EXISTS idx_feedback_candidate_latest
            ON feedback(candidate_id, id DESC);
        """,
    ),
    (
        18,
        """
        ALTER TABLE candidate_quality ADD COLUMN metadata_version TEXT NOT NULL DEFAULT '';
        -- Enrichment values live on the canonical candidate/read metadata
        -- rows. Keep field-level attribution separately so one catalog's
        -- match identity is never mistaken for provenance of every field.
        CREATE TABLE IF NOT EXISTS metadata_field_provenance (
            id INTEGER PRIMARY KEY,
            entity_type TEXT NOT NULL CHECK(entity_type IN ('candidate','read')),
            entity_id INTEGER NOT NULL,
            field TEXT NOT NULL CHECK(field IN ('description','genres','cover_url','release_date')),
            provider TEXT NOT NULL,
            provider_id TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.0
                CHECK(confidence >= 0 AND confidence <= 1),
            source_payload TEXT NOT NULL DEFAULT '{}',
            verified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(entity_type,entity_id,field,provider,provider_id)
        );
        CREATE INDEX IF NOT EXISTS idx_metadata_provenance_entity
            ON metadata_field_provenance(entity_type,entity_id,field);
        CREATE INDEX IF NOT EXISTS idx_metadata_provenance_verified
            ON metadata_field_provenance(verified_at DESC);

        -- Read enrichment is a separate cache keyed by a verified work ID and
        -- the original read identity fingerprint. Imports and ratings remain
        -- authoritative in reads and are never rewritten by metadata jobs.
        CREATE TABLE IF NOT EXISTS read_metadata (
            read_id INTEGER PRIMARY KEY REFERENCES reads(id) ON DELETE CASCADE,
            verified_work_id TEXT NOT NULL,
            identity_provider TEXT NOT NULL DEFAULT '',
            identity_provider_id TEXT NOT NULL DEFAULT '',
            identity_evidence TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(identity_evidence)),
            metadata_provenance TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_provenance)),
            identity_hash TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            description_kind TEXT NOT NULL DEFAULT '',
            genres TEXT NOT NULL DEFAULT '[]',
            cover_url TEXT NOT NULL DEFAULT '',
            release_date TEXT NOT NULL DEFAULT '',
            date_kind TEXT NOT NULL DEFAULT '',
            metadata_checked_at TEXT,
            CHECK(json_valid(genres))
        );
        CREATE INDEX IF NOT EXISTS idx_read_metadata_verified_work
            ON read_metadata(verified_work_id);
        CREATE INDEX IF NOT EXISTS idx_read_metadata_checked
            ON read_metadata(metadata_checked_at);
        CREATE TRIGGER IF NOT EXISTS metadata_provenance_clear_after_candidate_delete
        AFTER DELETE ON candidates
        BEGIN
            DELETE FROM metadata_field_provenance
                WHERE entity_type='candidate' AND entity_id=OLD.id;
        END;
        CREATE TRIGGER IF NOT EXISTS metadata_provenance_clear_after_read_delete
        AFTER DELETE ON reads
        BEGIN
            DELETE FROM metadata_field_provenance
                WHERE entity_type='read' AND entity_id=OLD.id;
        END;
        CREATE TRIGGER IF NOT EXISTS read_metadata_clear_on_source_identity_change
        AFTER UPDATE OF title,author,isbn ON reads
        WHEN OLD.title IS NOT NEW.title OR OLD.author IS NOT NEW.author OR OLD.isbn IS NOT NEW.isbn
        BEGIN
            UPDATE reads SET openlibrary_work_id='',openlibrary_lookup_attempted_at=NULL
                WHERE id=NEW.id;
            DELETE FROM metadata_field_provenance
                WHERE entity_type='read' AND entity_id=NEW.id;
            DELETE FROM read_metadata WHERE read_id=NEW.id;
        END;
        CREATE TRIGGER IF NOT EXISTS read_metadata_clear_on_work_identity_change
        AFTER UPDATE OF openlibrary_work_id ON reads
        WHEN OLD.openlibrary_work_id IS NOT NEW.openlibrary_work_id
        BEGIN
            DELETE FROM metadata_field_provenance
                WHERE entity_type='read' AND entity_id=NEW.id;
            DELETE FROM read_metadata WHERE read_id=NEW.id;
        END;
        """,
    ),
    (
        19,
        """
        -- These run-linked copies intentionally have no candidate foreign key.
        -- Feed refresh cleanup may remove catalog rows; the run snapshot remains
        -- available for replay until an explicit privacy purge removes it.
        CREATE TABLE IF NOT EXISTS recommendation_run_evidence (
            run_id TEXT PRIMARY KEY REFERENCES recommendation_runs(id) ON DELETE CASCADE,
            schema_version INTEGER NOT NULL,
            capture_status TEXT NOT NULL
                CHECK(capture_status IN ('complete','incomplete')),
            reason TEXT NOT NULL DEFAULT '',
            captured_at TEXT NOT NULL,
            payload_encoding TEXT NOT NULL DEFAULT '',
            payload BLOB,
            payload_sha256 TEXT NOT NULL DEFAULT '',
            payload_bytes INTEGER NOT NULL DEFAULT 0 CHECK(payload_bytes >= 0),
            vector_count INTEGER NOT NULL DEFAULT 0 CHECK(vector_count >= 0),
            CHECK(
                (capture_status='complete' AND payload IS NOT NULL AND payload_sha256!='')
                OR (capture_status='incomplete' AND payload IS NULL AND payload_sha256='')
            )
        );

        -- Content-addressed float32 artifacts are deduplicated across runs and
        -- are not tied to the current mutable embeddings/candidates tables.
        CREATE TABLE IF NOT EXISTS recommendation_evidence_vectors (
            artifact_hash TEXT PRIMARY KEY,
            backend TEXT NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL CHECK(dimensions > 0),
            encoding TEXT NOT NULL CHECK(encoding='float32-le'),
            vector BLOB NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS recommendation_run_evidence_vectors (
            run_id TEXT NOT NULL REFERENCES recommendation_run_evidence(run_id) ON DELETE CASCADE,
            entity_type TEXT NOT NULL CHECK(entity_type IN ('candidate','interaction')),
            entity_id INTEGER NOT NULL,
            artifact_hash TEXT NOT NULL REFERENCES recommendation_evidence_vectors(artifact_hash) ON DELETE RESTRICT,
            PRIMARY KEY(run_id,entity_type,entity_id)
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_evidence_vectors_artifact
            ON recommendation_run_evidence_vectors(artifact_hash);

        -- Identity links let an explicit privacy purge remove every run whose
        -- replay artifact contains a requested catalog identity.
        CREATE TABLE IF NOT EXISTS recommendation_run_evidence_candidates (
            run_id TEXT NOT NULL REFERENCES recommendation_run_evidence(run_id) ON DELETE CASCADE,
            candidate_id INTEGER NOT NULL,
            identity_hash TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(run_id,candidate_id)
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_evidence_candidate
            ON recommendation_run_evidence_candidates(candidate_id,run_id);

        -- Durable copies of actually served slots and later first-party events
        -- remain joinable after ordinary catalog cleanup cascades old rows.
        CREATE TABLE IF NOT EXISTS recommendation_evidence_exposures (
            run_id TEXT NOT NULL REFERENCES recommendation_runs(id) ON DELETE CASCADE,
            candidate_id INTEGER NOT NULL,
            identity_hash TEXT NOT NULL DEFAULT '',
            rank INTEGER NOT NULL CHECK(rank > 0),
            score REAL NOT NULL,
            propensity REAL NOT NULL CHECK(propensity > 0 AND propensity <= 1),
            presented_at TEXT NOT NULL,
            candidate_snapshot_json TEXT NOT NULL DEFAULT '{}',
            snapshot_status TEXT NOT NULL DEFAULT 'complete'
                CHECK(snapshot_status IN ('complete','minimal')),
            PRIMARY KEY(run_id,candidate_id)
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_evidence_exposure_identity
            ON recommendation_evidence_exposures(identity_hash,presented_at DESC);

        CREATE TABLE IF NOT EXISTS recommendation_evidence_events (
            id INTEGER PRIMARY KEY,
            event_key TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision > 0),
            run_id TEXT REFERENCES recommendation_runs(id) ON DELETE CASCADE,
            candidate_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            value REAL,
            source TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(event_key,revision)
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_evidence_events_candidate
            ON recommendation_evidence_events(candidate_id,occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_recommendation_evidence_events_run
            ON recommendation_evidence_events(run_id,candidate_id,occurred_at);

        CREATE TABLE IF NOT EXISTS recommendation_evidence_outcomes (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES recommendation_runs(id) ON DELETE CASCADE,
            event_key TEXT NOT NULL,
            event_revision INTEGER NOT NULL CHECK(event_revision > 0),
            candidate_id INTEGER NOT NULL,
            rank INTEGER NOT NULL CHECK(rank > 0),
            score REAL NOT NULL,
            propensity REAL NOT NULL CHECK(propensity > 0 AND propensity <= 1),
            presented_at TEXT NOT NULL,
            visible_at TEXT,
            label REAL NOT NULL,
            label_kind TEXT NOT NULL,
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            attributed_at TEXT NOT NULL,
            UNIQUE(run_id,event_key,event_revision)
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_evidence_outcomes_run
            ON recommendation_evidence_outcomes(run_id,rank,attributed_at);

        -- Enforce append-only semantics for ordinary callers. DELETE remains
        -- available to the explicit privacy-purge helper below.
        CREATE TRIGGER recommendation_run_evidence_immutable
        BEFORE UPDATE ON recommendation_run_evidence
        BEGIN SELECT RAISE(ABORT,'recommendation run evidence is immutable'); END;
        CREATE TRIGGER recommendation_evidence_vectors_immutable
        BEFORE UPDATE ON recommendation_evidence_vectors
        BEGIN SELECT RAISE(ABORT,'recommendation evidence vectors are immutable'); END;
        CREATE TRIGGER recommendation_run_evidence_vectors_immutable
        BEFORE UPDATE ON recommendation_run_evidence_vectors
        BEGIN SELECT RAISE(ABORT,'recommendation evidence links are immutable'); END;
        CREATE TRIGGER recommendation_run_evidence_candidates_immutable
        BEFORE UPDATE ON recommendation_run_evidence_candidates
        BEGIN SELECT RAISE(ABORT,'recommendation evidence candidates are immutable'); END;
        CREATE TRIGGER recommendation_evidence_exposures_immutable
        BEFORE UPDATE ON recommendation_evidence_exposures
        BEGIN SELECT RAISE(ABORT,'recommendation evidence exposures are immutable'); END;
        CREATE TRIGGER recommendation_evidence_events_immutable
        BEFORE UPDATE ON recommendation_evidence_events
        BEGIN SELECT RAISE(ABORT,'recommendation evidence events are immutable'); END;
        CREATE TRIGGER recommendation_evidence_outcomes_immutable
        BEFORE UPDATE ON recommendation_evidence_outcomes
        BEGIN SELECT RAISE(ABORT,'recommendation evidence outcomes are immutable'); END;

        -- This counter bounds durable replay payloads and vector binaries. Once
        -- it reaches the application quota, later run records stay explicit
        -- but incomplete and the normal recommendation response still serves.
        CREATE TABLE IF NOT EXISTS recommendation_evidence_budget (
            id INTEGER PRIMARY KEY CHECK(id=1),
            used_bytes INTEGER NOT NULL DEFAULT 0 CHECK(used_bytes >= 0)
        );
        INSERT OR IGNORE INTO recommendation_evidence_budget(id,used_bytes) VALUES(1,0);
        CREATE TRIGGER recommendation_run_evidence_budget_insert
        AFTER INSERT ON recommendation_run_evidence
        WHEN NEW.payload IS NOT NULL
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=used_bytes+length(NEW.payload) WHERE id=1;
        END;
        CREATE TRIGGER recommendation_run_evidence_budget_delete
        AFTER DELETE ON recommendation_run_evidence
        WHEN OLD.payload IS NOT NULL
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=MAX(0,used_bytes-length(OLD.payload)) WHERE id=1;
        END;
        CREATE TRIGGER recommendation_evidence_vector_budget_insert
        AFTER INSERT ON recommendation_evidence_vectors
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=used_bytes+length(NEW.vector) WHERE id=1;
        END;
        CREATE TRIGGER recommendation_evidence_vector_budget_delete
        AFTER DELETE ON recommendation_evidence_vectors
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=MAX(0,used_bytes-length(OLD.vector)) WHERE id=1;
        END;
        CREATE TRIGGER recommendation_run_evidence_link_budget_insert
        AFTER INSERT ON recommendation_run_evidence_vectors
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=used_bytes+96 WHERE id=1;
        END;
        CREATE TRIGGER recommendation_run_evidence_link_budget_delete
        AFTER DELETE ON recommendation_run_evidence_vectors
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=MAX(0,used_bytes-96) WHERE id=1;
        END;
        CREATE TRIGGER recommendation_run_evidence_candidate_budget_insert
        AFTER INSERT ON recommendation_run_evidence_candidates
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=used_bytes+72+length(NEW.identity_hash) WHERE id=1;
        END;
        CREATE TRIGGER recommendation_run_evidence_candidate_budget_delete
        AFTER DELETE ON recommendation_run_evidence_candidates
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=MAX(0,used_bytes-72-length(OLD.identity_hash)) WHERE id=1;
        END;
        CREATE TRIGGER recommendation_evidence_exposure_budget_insert
        AFTER INSERT ON recommendation_evidence_exposures
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=used_bytes+160+length(CAST(NEW.candidate_snapshot_json AS BLOB)) WHERE id=1;
        END;
        CREATE TRIGGER recommendation_evidence_exposure_budget_delete
        AFTER DELETE ON recommendation_evidence_exposures
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=MAX(0,used_bytes-160-length(CAST(OLD.candidate_snapshot_json AS BLOB))) WHERE id=1;
        END;
        CREATE TRIGGER recommendation_evidence_event_budget_insert
        AFTER INSERT ON recommendation_evidence_events
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=used_bytes+128+length(CAST(NEW.event_key AS BLOB))
                +length(CAST(NEW.event_type AS BLOB))+length(CAST(NEW.source AS BLOB))
                +length(CAST(NEW.occurred_at AS BLOB)) WHERE id=1;
        END;
        CREATE TRIGGER recommendation_evidence_event_budget_delete
        AFTER DELETE ON recommendation_evidence_events
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=MAX(0,used_bytes-128-length(CAST(OLD.event_key AS BLOB))
                -length(CAST(OLD.event_type AS BLOB))-length(CAST(OLD.source AS BLOB))
                -length(CAST(OLD.occurred_at AS BLOB))) WHERE id=1;
        END;
        CREATE TRIGGER recommendation_evidence_outcome_budget_insert
        AFTER INSERT ON recommendation_evidence_outcomes
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=used_bytes+192+length(CAST(NEW.event_key AS BLOB))
                +length(CAST(NEW.label_kind AS BLOB))+length(CAST(NEW.presented_at AS BLOB))
                +length(CAST(NEW.attributed_at AS BLOB))
                +COALESCE(length(CAST(NEW.visible_at AS BLOB)),0) WHERE id=1;
        END;
        CREATE TRIGGER recommendation_evidence_outcome_budget_delete
        AFTER DELETE ON recommendation_evidence_outcomes
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=MAX(0,used_bytes-192-length(CAST(OLD.event_key AS BLOB))
                -length(CAST(OLD.label_kind AS BLOB))-length(CAST(OLD.presented_at AS BLOB))
                -length(CAST(OLD.attributed_at AS BLOB))
                -COALESCE(length(CAST(OLD.visible_at AS BLOB)),0)) WHERE id=1;
        END;
        """,
    ),
    (
        20,
        """
        ALTER TABLE candidates ADD COLUMN score_batch_id TEXT;
        CREATE INDEX IF NOT EXISTS idx_candidates_score_batch_id
            ON candidates(score_batch_id);

        -- A scoring batch is the immutable job-time basis for materialized
        -- candidate scores. Candidate links intentionally survive catalog
        -- cleanup; an explicit privacy purge removes the entire batch.
        CREATE TABLE IF NOT EXISTS scoring_batches (
            id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            capture_status TEXT NOT NULL
                CHECK(capture_status IN ('complete','incomplete')),
            reason TEXT NOT NULL DEFAULT '',
            captured_at TEXT NOT NULL,
            payload_encoding TEXT NOT NULL DEFAULT '',
            payload BLOB,
            payload_sha256 TEXT NOT NULL DEFAULT '',
            payload_bytes INTEGER NOT NULL DEFAULT 0 CHECK(payload_bytes >= 0),
            read_count INTEGER NOT NULL DEFAULT 0 CHECK(read_count >= 0),
            candidate_count INTEGER NOT NULL DEFAULT 0 CHECK(candidate_count >= 0),
            vector_count INTEGER NOT NULL DEFAULT 0 CHECK(vector_count >= 0),
            CHECK(
                (capture_status='complete' AND payload IS NOT NULL AND payload_sha256!='')
                OR (capture_status='incomplete' AND payload IS NULL AND payload_sha256='')
            )
        );
        CREATE TABLE IF NOT EXISTS scoring_batch_vectors (
            batch_id TEXT NOT NULL REFERENCES scoring_batches(id) ON DELETE CASCADE,
            entity_type TEXT NOT NULL CHECK(entity_type IN ('read','candidate')),
            entity_id INTEGER NOT NULL,
            artifact_hash TEXT NOT NULL REFERENCES recommendation_evidence_vectors(artifact_hash) ON DELETE RESTRICT,
            PRIMARY KEY(batch_id,entity_type,entity_id)
        );
        CREATE INDEX IF NOT EXISTS idx_scoring_batch_vector_artifact
            ON scoring_batch_vectors(artifact_hash);
        CREATE TABLE IF NOT EXISTS scoring_batch_candidates (
            batch_id TEXT NOT NULL REFERENCES scoring_batches(id) ON DELETE CASCADE,
            candidate_id INTEGER NOT NULL,
            score REAL NOT NULL,
            identity_hash TEXT NOT NULL,
            input_content_hash TEXT NOT NULL,
            PRIMARY KEY(batch_id,candidate_id)
        );
        CREATE INDEX IF NOT EXISTS idx_scoring_batch_candidate
            ON scoring_batch_candidates(candidate_id,batch_id);

        -- Read IDs support targeted history erasure without a live reads FK.
        -- A batch's copied ratings/text/vectors are erased as a unit when any
        -- read from its historical input set is intentionally deleted.
        CREATE TABLE IF NOT EXISTS scoring_batch_reads (
            batch_id TEXT NOT NULL REFERENCES scoring_batches(id) ON DELETE CASCADE,
            read_id INTEGER NOT NULL,
            PRIMARY KEY(batch_id,read_id)
        );
        CREATE INDEX IF NOT EXISTS idx_scoring_batch_read
            ON scoring_batch_reads(read_id,batch_id);

        -- Serving snapshots retain a copy of scorer-batch lineage. Keep a
        -- normalized reverse link so a read/candidate privacy purge can also
        -- remove those run payloads before deleting the referenced batch.
        CREATE TABLE IF NOT EXISTS recommendation_run_evidence_scoring_batches (
            run_id TEXT NOT NULL REFERENCES recommendation_runs(id) ON DELETE CASCADE,
            score_batch_id TEXT NOT NULL,
            PRIMARY KEY(run_id,score_batch_id)
        );
        CREATE INDEX IF NOT EXISTS idx_run_evidence_score_batch
            ON recommendation_run_evidence_scoring_batches(score_batch_id,run_id);

        CREATE TRIGGER scoring_batches_immutable
        BEFORE UPDATE ON scoring_batches
        BEGIN SELECT RAISE(ABORT,'scoring batch evidence is immutable'); END;
        CREATE TRIGGER scoring_batch_vectors_immutable
        BEFORE UPDATE ON scoring_batch_vectors
        BEGIN SELECT RAISE(ABORT,'scoring batch vector links are immutable'); END;
        CREATE TRIGGER scoring_batch_candidates_immutable
        BEFORE UPDATE ON scoring_batch_candidates
        BEGIN SELECT RAISE(ABORT,'scoring batch candidate links are immutable'); END;
        CREATE TRIGGER scoring_batch_reads_immutable
        BEFORE UPDATE ON scoring_batch_reads
        BEGIN SELECT RAISE(ABORT,'scoring batch read links are immutable'); END;
        CREATE TRIGGER run_evidence_scoring_batches_immutable
        BEFORE UPDATE ON recommendation_run_evidence_scoring_batches
        BEGIN SELECT RAISE(ABORT,'recommendation score-batch links are immutable'); END;

        CREATE TRIGGER scoring_batch_budget_insert
        AFTER INSERT ON scoring_batches
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=used_bytes+192+length(CAST(NEW.reason AS BLOB))
                +length(CAST(NEW.captured_at AS BLOB))
                +COALESCE(length(NEW.payload),0) WHERE id=1;
        END;
        CREATE TRIGGER scoring_batch_budget_delete
        AFTER DELETE ON scoring_batches
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=MAX(0,used_bytes-192-length(CAST(OLD.reason AS BLOB))
                -length(CAST(OLD.captured_at AS BLOB))
                -COALESCE(length(OLD.payload),0)) WHERE id=1;
        END;
        CREATE TRIGGER scoring_batch_vector_budget_insert
        AFTER INSERT ON scoring_batch_vectors
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=used_bytes+96 WHERE id=1;
        END;
        CREATE TRIGGER scoring_batch_vector_budget_delete
        AFTER DELETE ON scoring_batch_vectors
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=MAX(0,used_bytes-96) WHERE id=1;
        END;
        CREATE TRIGGER scoring_batch_candidate_budget_insert
        AFTER INSERT ON scoring_batch_candidates
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=used_bytes+96+length(NEW.identity_hash)
                +length(NEW.input_content_hash) WHERE id=1;
        END;
        CREATE TRIGGER scoring_batch_candidate_budget_delete
        AFTER DELETE ON scoring_batch_candidates
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=MAX(0,used_bytes-96-length(OLD.identity_hash)
                -length(OLD.input_content_hash)) WHERE id=1;
        END;
        CREATE TRIGGER scoring_batch_read_budget_insert
        AFTER INSERT ON scoring_batch_reads
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=used_bytes+64 WHERE id=1;
        END;
        CREATE TRIGGER scoring_batch_read_budget_delete
        AFTER DELETE ON scoring_batch_reads
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=MAX(0,used_bytes-64) WHERE id=1;
        END;
        CREATE TRIGGER run_evidence_scoring_batch_budget_insert
        AFTER INSERT ON recommendation_run_evidence_scoring_batches
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=used_bytes+64 WHERE id=1;
        END;
        CREATE TRIGGER run_evidence_scoring_batch_budget_delete
        AFTER DELETE ON recommendation_run_evidence_scoring_batches
        BEGIN
            UPDATE recommendation_evidence_budget
            SET used_bytes=MAX(0,used_bytes-64) WHERE id=1;
        END;
        """,
    ),
    (
        21,
        """
        ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT;
        ALTER TABLE jobs ADD COLUMN lease_token TEXT;
        CREATE INDEX IF NOT EXISTS idx_jobs_status_heartbeat
            ON jobs(status,heartbeat_at);
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

# Most default feeds are public. Keep NYT disabled until a key is configured;
# the key is stored encrypted and added only to outgoing Books API requests.
DEFAULT_SOURCES = (
    ("Apple Books · Top audiobooks", "https://itunes.apple.com/us/rss/topaudiobooks/limit=50/xml", 1),
    ("Apple Books · Top paid ebooks", "https://itunes.apple.com/us/rss/toppaidebooks/limit=50/xml", 1),
    ("Open Library · Science fiction", "https://openlibrary.org/subjects/science_fiction.json?limit=50", 1),
    ("Open Library · Fantasy", "https://openlibrary.org/subjects/fantasy.json?limit=50", 1),
    ("Goodreads · Science fiction", "https://www.goodreads.com/genres/science-fiction", 1),
    ("Goodreads · Speculative fiction", "https://www.goodreads.com/genres/speculative-fiction", 1),
    ("Goodreads · Mystery thriller", "https://www.goodreads.com/genres/mystery-thriller", 1),
    ("Goodreads · Literary fiction", "https://www.goodreads.com/genres/literary-fiction", 1),
    ("Publishers Weekly · Starred reviews this week", "https://www.publishersweekly.com/pw/reviews/starred.html", 1),
    ("Penguin Random House · New releases", "https://www.penguinrandomhouse.com/books/new-releases/", 1),
    ("Open Library · Mystery & detective", "https://openlibrary.org/subjects/mystery_and_detective_stories.json?limit=50", 0),
    ("Open Library · Literary fiction", "https://openlibrary.org/subjects/literary_fiction.json?limit=50", 0),
    ("NYT Books overview · API key required", "https://api.nytimes.com/svc/books/v3/lists/overview.json", 0),
)

class _ProfileDatabaseLock:
    """Allow concurrent connections and let backup/restore take an exclusive lock."""

    def __init__(self):
        self._condition = threading.Condition()
        self._writer: int | None = None
        self._write_depth = 0
        self._waiting_writers = 0
        self._connections = 0
        self._connections_by_thread: dict[int, int] = {}

    def acquire_connection(self) -> int:
        owner = threading.get_ident()
        with self._condition:
            owns_connection = self._connections_by_thread.get(owner, 0) > 0
            while (
                self._writer not in (None, owner)
                or (
                    self._waiting_writers
                    and self._writer != owner
                    and not owns_connection
                )
            ):
                self._condition.wait()
            self._connections += 1
            self._connections_by_thread[owner] = (
                self._connections_by_thread.get(owner, 0) + 1
            )
        return owner

    def release_connection(self, owner: int) -> None:
        with self._condition:
            count = self._connections_by_thread.get(owner, 0)
            if not count:
                return
            self._connections -= 1
            if count == 1:
                del self._connections_by_thread[owner]
            else:
                self._connections_by_thread[owner] = count - 1
            self._condition.notify_all()

    def acquire(self) -> None:
        owner = threading.get_ident()
        with self._condition:
            if self._writer == owner:
                self._write_depth += 1
                return
            self._waiting_writers += 1
            try:
                while self._writer is not None or self._connections:
                    self._condition.wait()
                self._writer = owner
                self._write_depth = 1
            finally:
                self._waiting_writers -= 1

    def release(self) -> None:
        owner = threading.get_ident()
        with self._condition:
            if self._writer != owner:
                raise RuntimeError("Profile database lock released by a non-owner")
            self._write_depth -= 1
            if not self._write_depth:
                self._writer = None
                self._condition.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()


_DATABASE_LOCKS: weakref.WeakValueDictionary[str, _ProfileDatabaseLock] = (
    weakref.WeakValueDictionary()
)
_DATABASE_LOCKS_GUARD = threading.Lock()
_DATABASE_SETUP_LOCKS: weakref.WeakValueDictionary[str, threading.RLock] = (
    weakref.WeakValueDictionary()
)


def database_lock(path: str | Path | None = None) -> _ProfileDatabaseLock:
    """Return the process lock for one profile database file."""

    key = str(Path(path) if path is not None else profile_database_path())
    with _DATABASE_LOCKS_GUARD:
        return _DATABASE_LOCKS.setdefault(key, _ProfileDatabaseLock())


def _database_setup_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _DATABASE_LOCKS_GUARD:
        return _DATABASE_SETUP_LOCKS.setdefault(key, threading.RLock())


class _LockedConnection(sqlite3.Connection):
    """Keep restores from replacing a profile database used by a live request."""

    def __init__(self, *args, **kwargs):
        self._database_lock = database_lock(args[0] if args else None)
        self._database_lock_owner = self._database_lock.acquire_connection()
        self._database_lock_held = True
        try:
            super().__init__(*args, **kwargs)
        except Exception:
            self._database_lock_held = False
            self._database_lock.release_connection(self._database_lock_owner)
            raise

    def close(self):
        held = getattr(self, "_database_lock_held", False)
        if held:
            self._database_lock_held = False
        try:
            super().close()
        finally:
            if held:
                self._database_lock.release_connection(self._database_lock_owner)

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect():
    db_path = profile_database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if db_path.parent.name == "profiles":
        try:
            db_path.parent.chmod(0o700)
        except OSError:
            pass
    con = sqlite3.connect(
        db_path, timeout=10, check_same_thread=False, factory=_LockedConnection
    )
    try:
        con.row_factory = sqlite3.Row
        con.create_function("book_identity", 2, book_identity, deterministic=True)
        con.create_function("book_identity_matches", 4, book_identity_matches, deterministic=True)
        with _database_setup_lock(db_path):
            mode = con.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
                con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA busy_timeout=5000")
            _restrict_database_files()
        return con
    except Exception:
        con.close()
        raise


def _restrict_database_files():
    """Keep the database and SQLite sidecars private to the engine user."""

    db_path = profile_database_path()
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        try:
            path.chmod(0o600)
        except FileNotFoundError:
            continue


def _backfill_source_isbns(con):
    """Recover source ISBNs from legacy Amazon links without accepting rows."""

    candidates = con.execute(
        "SELECT id,isbn13,isbn10,source_url FROM candidates "
        "WHERE source_url!='' AND (isbn13='' OR isbn10='')"
    ).fetchall()
    for candidate in candidates:
        isbn13, isbn10 = isbn_parts_from_amazon_url(candidate["source_url"])
        if not isbn13 and not isbn10:
            continue
        next_isbn13 = candidate["isbn13"] or isbn13
        next_isbn10 = candidate["isbn10"] or isbn10
        con.execute(
            "UPDATE candidates SET isbn13=?,isbn10=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (next_isbn13, next_isbn10, candidate["id"]),
        )
        # Do not overwrite a provider identifier that was already recorded for
        # an accepted or ambiguous match. Blank ledger fields are safe to fill
        # for pending/quarantined rows and let the next audit use source proof.
        con.execute(
            """UPDATE candidate_quality SET
                isbn13=CASE WHEN isbn13='' THEN ? ELSE isbn13 END,
                isbn10=CASE WHEN isbn10='' THEN ? ELSE isbn10 END,
                updated_at=CURRENT_TIMESTAMP
            WHERE candidate_id=? AND quality_status IN ('pending','quarantine')""",
            (next_isbn13, next_isbn10, candidate["id"]),
        )

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
        _restrict_database_files()


def persist_metadata_field_provenance(
    con,
    entity_type,
    entity_id,
    field,
    item,
    *,
    default_provider="",
    default_provider_id="",
    default_confidence=0.0,
):
    """Persist bounded, field-specific catalog attribution for stored values."""

    if field not in {"description", "genres", "cover_url", "release_date"}:
        return 0
    provenance = item.get("_metadata_provenance") or item.get("metadata_provenance") or {}
    fields = provenance.get("fields", {}) if isinstance(provenance, dict) else {}
    field_info = fields.get(field, {}) if isinstance(fields, dict) else {}
    sources = field_info.get("sources") if isinstance(field_info, dict) else None
    if not isinstance(sources, list):
        sources = [field_info] if isinstance(field_info, dict) and field_info else []
    if not sources:
        provider_key = "cover" if field == "cover_url" else field
        provider = item.get(f"_{provider_key}_provider") or item.get(f"{provider_key}_provider") or ""
        provider_id = item.get(f"_{provider_key}_provider_id") or item.get(f"{provider_key}_provider_id") or ""
        if provider or default_provider:
            sources = [{"provider": provider or default_provider, "provider_id": provider_id or default_provider_id}]
    if not sources and default_provider:
        sources = [{"provider": default_provider, "provider_id": default_provider_id}]

    payloads = provenance.get("source_payloads", []) if isinstance(provenance, dict) else []
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    written = 0
    seen = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        provider = str(source.get("provider") or default_provider or "")[:100]
        provider_id = str(source.get("provider_id") or default_provider_id or "")[:500]
        if not provider or (provider, provider_id) in seen:
            continue
        seen.add((provider, provider_id))
        source_payload = {}
        if isinstance(payloads, list):
            for candidate in payloads:
                if (
                    isinstance(candidate, dict)
                    and candidate.get("provider") == provider
                    and str(candidate.get("provider_id") or "") == provider_id
                ):
                    source_payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
                    break
        confidence = source.get("confidence", source.get("identity_quality_score", default_confidence))
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = float(default_confidence)
        bounded = {
            "field": field,
            "field_provenance": source,
            "payload": source_payload,
            "fetched_at": provenance.get("fetched_at", "") if isinstance(provenance, dict) else "",
        }
        encoded = json.dumps(bounded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > METADATA_PROVENANCE_MAX_BYTES:
            encoded = json.dumps(
                {"field": field, "provider": provider, "provider_id": provider_id, "truncated": True},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        con.execute(
            """INSERT INTO metadata_field_provenance(
                entity_type,entity_id,field,provider,provider_id,confidence,
                source_payload,verified_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(entity_type,entity_id,field,provider,provider_id) DO UPDATE SET
                confidence=excluded.confidence,source_payload=excluded.source_payload,
                verified_at=excluded.verified_at""",
            (entity_type, entity_id, field, provider, provider_id, confidence, encoded, now),
        )
        written += 1
    return written

def initialize(*, seed_demo: bool = True):
    db_path = profile_database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        applied = {item[0] for item in con.execute("SELECT version FROM schema_migrations")}
        for version, script in MIGRATIONS:
            if version not in applied:
                try:
                    con.executescript(script)
                except sqlite3.Error as exc:
                    raise sqlite3.DatabaseError(
                        f"Schema migration {version} failed: {exc}"
                    ) from exc
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
        con.execute(
            "INSERT OR IGNORE INTO settings(key,value,secret) VALUES('nyt_api_key','',1)"
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
        seed_items = json.loads(seed.read_text()) if seed_demo else []
        if seed_demo and not con.execute("SELECT 1 FROM candidates LIMIT 1").fetchone():
            for item in seed_items:
                release_date = (date.today() + timedelta(days=int(item.get("release_offset_days", 30)))).isoformat()
                con.execute("""INSERT OR IGNORE INTO candidates(title,author,description,cover_url,source_url,source_id,release_date,date_kind,genres,score,explanation,status,normalized_key)
                VALUES(?,?,?,?,?,?,?, ?,?,?,?,'recommended',?)""", (item["title"], item["author"], item["description"], fallback_cover_url(item["title"], item["author"], item.get("cover_url", ""), item.get("source_url", "")), canonical_book_source_url(item["title"], item["author"], item.get("source_url", "")), builtin_id, release_date, "demo", json.dumps(item["genres"]), item["score"], json.dumps(item["explanation"]), normalize_key(item["title"], item["author"])))
            for title, author, rating in [("Sea of Tranquility","Emily St. John Mandel",5),("The Fifth Season","N. K. Jemisin",5),("Piranesi","Susanna Clarke",4.5),("The Only Good Indians","Stephen Graham Jones",4)]:
                con.execute("INSERT OR IGNORE INTO reads(title,author,rating,source) VALUES(?,?,?,'demo')", (title,author,rating))
        # Built-in demo rows are curated and do not need an external catalog
        # round-trip before the first recommendation is visible.  All rows
        # discovered from a source remain pending until the quality worker
        # audits them.
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version='builtin-curated-v1',updated_at=CURRENT_TIMESTAMP "
            "WHERE candidate_id IN (SELECT id FROM candidates WHERE source_id=? AND status IN ('new','recommended')) "
            "AND audit_version='candidate-quality-v1'",
            (builtin_id,),
        )
        con.execute(
            "UPDATE candidate_quality SET metadata_version=? "
            "WHERE audit_version='builtin-curated-v1' AND metadata_version=''",
            (METADATA_NORMALIZATION_VERSION,),
        )
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
        _backfill_source_isbns(con)
    _restrict_database_files()

def normalize_key(title: str, author: str):
    import re
    return re.sub(r"[^a-z0-9]+", " ", f"{title} {author}".lower()).strip()

def rows(query: str, params=()):
    with connect() as con:
        return [dict(row) for row in con.execute(query, params).fetchall()]


def private_setting(key: str, default: str = "") -> str:
    """Read one setting, decrypting it when the database marks it secret."""

    found = row("SELECT value,secret FROM settings WHERE key=?", (key,))
    if not found:
        return default
    return unseal(found["value"]) if found["secret"] else found["value"]

def row(query: str, params=()):
    with connect() as con:
        found = con.execute(query, params).fetchone()
        return dict(found) if found else None
