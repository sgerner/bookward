"""Tests for lifespan background-loop gating and the offline socket guard."""

from __future__ import annotations

import asyncio
from pathlib import Path
import socket
import threading
import time

from fastapi.testclient import TestClient
import httpx
import pytest
import respx

from afterword_engine import main as main_module
from afterword_engine import profiles
from afterword_engine.api_tokens import generate_api_token, legacy_hash_api_token
from afterword_engine.config import Settings, settings
from afterword_engine.covers import OPEN_LIBRARY_SEARCH
from afterword_engine.database import connect, initialize, transaction
from afterword_engine.main import app
from afterword_engine.quality import QUALITY_VERSION, audit_candidates
from afterword_engine.tenancy import profile_scope
from conftest import ExternalSocketBlockedError, _is_loopback_host


def test_settings_enable_background_loops_defaults_true_and_reads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AFTERWORD_ENABLE_BACKGROUND_LOOPS", raising=False)
    assert Settings().enable_background_loops is True

    monkeypatch.setenv("AFTERWORD_ENABLE_BACKGROUND_LOOPS", "false")
    assert Settings().enable_background_loops is False

    # The test harness disables background loops on the shared settings instance.
    assert settings.enable_background_loops is False


def test_lifespan_skips_workers_schedulers_and_backfills_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "db", str(tmp_path / "afterword.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)

    profiles.initialize_auth()
    profiles.create_first_admin("owner", "owner-password-with-enough-length")
    alice = profiles.create_account("alice", "alice-password-with-enough-length")

    legacy_token = generate_api_token()
    with profile_scope("legacy"):
        initialize(seed_demo=False)
        with transaction() as con:
            con.execute(
                "INSERT INTO api_tokens(id,name,token_prefix,token_hash) VALUES(1,?,?,?)",
                ("Legacy token", legacy_token[:12], legacy_hash_api_token(legacy_token)),
            )

    started_loops: list[str] = []

    def unexpected_loop(name: str):
        async def _loop(*_args, **_kwargs):
            started_loops.append(name)

        return _loop

    for loop_name in (
        "worker_loop",
        "source_scheduler_loop",
        "digest_scheduler_loop",
        "goodreads_rss_scheduler_loop",
        "backup_scheduler_loop",
    ):
        monkeypatch.setattr(main_module, loop_name, unexpected_loop(loop_name))

    assert settings.enable_background_loops is False
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True
        overview = client.get(
            "/api/v1/overview",
            headers={"Authorization": f"Bearer {legacy_token}"},
        )
        assert overview.status_code == 200

    assert started_loops == []
    for pid in ("legacy", alice["profile_id"]):
        with profile_scope(pid), connect() as con:
            queued = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert queued == 0


def test_lifespan_starts_and_stops_workers_schedulers_and_queues_backfills_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "db", str(tmp_path / "afterword.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.setattr(settings, "enable_background_loops", True)
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)

    profiles.initialize_auth()
    profiles.create_first_admin("owner", "owner-password-with-enough-length")
    alice = profiles.create_account("alice", "alice-password-with-enough-length")

    with profile_scope("legacy"):
        initialize(seed_demo=False)
        with transaction() as con:
            source_id = con.execute(
                "SELECT id FROM sources WHERE url='builtin://upcoming'"
            ).fetchone()[0]
            cid = con.execute(
                "INSERT INTO candidates(title,author,source_url,source_id,status,normalized_key) "
                "VALUES('Pending Book','A Writer','https://example.com/pending',?,'new','pending-book')",
                (source_id,),
            ).lastrowid
            con.execute(
                "UPDATE candidate_quality SET quality_status='pending',audit_version=? "
                "WHERE candidate_id=?",
                (QUALITY_VERSION, cid),
            )

    started: list[str] = []
    stopped: list[str] = []

    async def fake_worker_loop(_handler, stop: asyncio.Event) -> None:
        started.append("worker_loop")
        await stop.wait()
        stopped.append("worker_loop")

    def make_fake_scheduler(name: str):
        async def _scheduler(stop: asyncio.Event) -> None:
            started.append(name)
            await stop.wait()
            stopped.append(name)

        return _scheduler

    monkeypatch.setattr(main_module, "worker_loop", fake_worker_loop)
    for scheduler_name in (
        "source_scheduler_loop",
        "digest_scheduler_loop",
        "goodreads_rss_scheduler_loop",
        "backup_scheduler_loop",
    ):
        monkeypatch.setattr(
            main_module, scheduler_name, make_fake_scheduler(scheduler_name)
        )

    expected_loops = {
        "worker_loop",
        "source_scheduler_loop",
        "digest_scheduler_loop",
        "goodreads_rss_scheduler_loop",
        "backup_scheduler_loop",
    }

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert set(started) == expected_loops
        assert stopped == []

        with profile_scope("legacy"), connect() as con:
            legacy_kinds = {
                item[0] for item in con.execute("SELECT kind FROM jobs").fetchall()
            }
        with profile_scope(alice["profile_id"]), connect() as con:
            alice_kinds = {
                item[0] for item in con.execute("SELECT kind FROM jobs").fetchall()
            }

        assert legacy_kinds == {
            "metadata",
            "candidate_quality_recovery",
            "read_work_identities",
            "read_metadata",
        }
        assert alice_kinds == {
            "metadata",
            "read_work_identities",
            "read_metadata",
        }

    assert set(stopped) == expected_loops


def test_socket_guard_blocks_unmocked_external_socket_and_httpx_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = time.monotonic()

    with pytest.raises(ExternalSocketBlockedError, match="93.184.216.34:443"):
        socket.create_connection(("93.184.216.34", 443), timeout=5)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw_sock:
        with pytest.raises(ExternalSocketBlockedError, match="93.184.216.34:443"):
            raw_sock.connect(("93.184.216.34", 443))
        with pytest.raises(ExternalSocketBlockedError, match="93.184.216.34:443"):
            raw_sock.connect_ex(("93.184.216.34", 443))

    with pytest.raises(ExternalSocketBlockedError, match="openlibrary.org:443"):
        httpx.get(OPEN_LIBRARY_SEARCH, timeout=5)

    async def run_async_request() -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.get("https://www.googleapis.com/books/v1/volumes")

    with pytest.raises(ExternalSocketBlockedError, match="www.googleapis.com:443"):
        asyncio.run(run_async_request())

    # Even provider callers wrapped in `except Exception:` fail immediately.
    monkeypatch.setattr(settings, "db", str(tmp_path / "guard-quality.db"))
    initialize(seed_demo=False)
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE url='builtin://upcoming'"
        ).fetchone()[0]
        con.execute(
            "INSERT INTO candidates(title,author,source_url,source_id,status,normalized_key) "
            "VALUES('Unmocked Candidate','Real Author','https://example.com/book',?,'new','unmocked-candidate')",
            (source_id,),
        )
    with pytest.raises(ExternalSocketBlockedError, match="openlibrary.org:443"):
        asyncio.run(audit_candidates(only_pending=False))

    assert time.monotonic() - started < 1.0


def test_socket_guard_allows_loopback_asgi_and_respx_transports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _is_loopback_host("127.0.0.1") is True
    assert _is_loopback_host("::1") is True
    assert _is_loopback_host("[::1]") is True
    assert _is_loopback_host("localhost") is True
    assert _is_loopback_host("openlibrary.org") is False
    assert _is_loopback_host("93.184.216.34") is False

    ready = threading.Event()

    def serve_once(server_sock: socket.socket) -> None:
        ready.set()
        conn, _ = server_sock.accept()
        with conn:
            conn.recv(1024)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
            )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        port = server.getsockname()[1]
        worker = threading.Thread(target=serve_once, args=(server,), daemon=True)
        worker.start()
        ready.wait(timeout=2)
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
        worker.join(timeout=2)
        assert response.status_code == 200
        assert response.text == "ok"

        ready.clear()
        worker = threading.Thread(target=serve_once, args=(server,), daemon=True)
        worker.start()
        ready.wait(timeout=2)
        with socket.create_connection(("localhost", port), timeout=2) as conn:
            conn.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            assert b"200 OK" in conn.recv(1024)
        worker.join(timeout=2)

    with respx.mock:
        respx.get(OPEN_LIBRARY_SEARCH).mock(
            return_value=httpx.Response(200, json={"docs": []})
        )
        mocked = httpx.get(OPEN_LIBRARY_SEARCH)
        assert mocked.status_code == 200
        assert mocked.json() == {"docs": []}

    monkeypatch.setattr(settings, "db", str(tmp_path / "asgi.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200


def test_pytester_unmocked_external_call_fails_active_test_immediately(
    pytester: pytest.Pytester,
) -> None:
    conftest_source = Path(__file__).with_name("conftest.py").read_text()
    pytester.makeconftest(conftest_source)
    pytester.makepyfile(
        test_unmocked="""
        import socket
        import httpx

        def test_unmocked_httpx_fails_immediately():
            httpx.get("https://openlibrary.org/search.json", timeout=5)

        def test_unmocked_socket_fails_immediately():
            socket.create_connection(("93.184.216.34", 443), timeout=5)
        """
    )

    started = time.monotonic()
    result = pytester.runpytest_inprocess("-q")
    elapsed = time.monotonic() - started

    result.assert_outcomes(failed=2)
    result.stdout.fnmatch_lines(
        [
            "*Blocked unmocked non-loopback network connection to openlibrary.org:443*",
            "*Blocked unmocked non-loopback network connection to 93.184.216.34:443*",
        ]
    )
    assert elapsed < 2.0
