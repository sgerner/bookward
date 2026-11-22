import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from afterword_engine import digest
from afterword_engine.config import settings
from afterword_engine.database import initialize, row, transaction


@pytest.mark.parametrize("zone", ["/etc/passwd", "../UTC", "America/../UTC"])
def test_invalid_timezone_is_rejected_and_legacy_value_is_safe(zone):
    assert digest.digest_config({"digest_timezone": zone})["timezone"] == "UTC"
    with pytest.raises(ValueError, match="timezone is not recognized"):
        digest.validate_digest_config({"timezone": zone})


def test_untrusted_catalog_text_cannot_trigger_discord_mentions():
    payload = digest._discord_payload(
        [{"title": "@everyone", "author": "<@123>", "score": 90}],
        {"app_url": "https://example.com"},
        "2026-W40",
    )
    assert payload["allowed_mentions"] == {"parse": []}
    assert "@everyone" in payload["content"]
    assert "ranking score 90" in payload["content"]
    assert "90%" not in payload["content"]


def test_legacy_discord_payloads_also_disable_mentions_on_delivery(monkeypatch):
    monkeypatch.setattr(digest, "validate_public_url", lambda url: None)
    url = "https://discord.com/api/webhooks/test"
    with respx.mock as router:
        route = router.post(url).mock(return_value=httpx.Response(204))
        asyncio.run(digest._send_discord({"discord_webhook_url": url}, {"content": "@everyone"}))
        assert json.loads(route.calls[0].request.content)["allowed_mentions"] == {"parse": []}


def test_removing_successful_channel_does_not_block_partial_delivery_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "digest.db"))
    initialize()
    calls = []

    async def discord(*args):
        calls.append("discord")

    async def email(*args):
        calls.append("email")
        if calls.count("email") == 1:
            raise RuntimeError("temporary failure")

    monkeypatch.setattr(digest, "_send_discord", discord)
    monkeypatch.setattr(digest, "_send_email", email)
    values = {"digest_channels": "discord,email", "digest_minimum_score": 0}
    assert asyncio.run(digest.send_digest(values))["status"] == "failed"
    values["digest_channels"] = "email"
    result = asyncio.run(digest.send_digest(values))
    assert result["status"] == "sent"
    assert calls == ["discord", "email", "email"]
    assert row("SELECT value FROM settings WHERE key='digest_last_period'")["value"] == result["period"]


def test_automatic_digest_retries_are_capped_but_manual_run_can_recover(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "bounded-digest.db"))
    initialize()

    async def fail(*_args):
        raise RuntimeError("temporary provider outage")

    monkeypatch.setattr(digest, "_send_discord", fail)
    values = {
        "digest_enabled": "1",
        "digest_channels": "discord",
        "digest_minimum_score": 0,
    }
    for _ in range(digest.MAX_DELIVERY_ATTEMPTS):
        assert asyncio.run(digest.send_digest(values, automatic=True))["status"] == "failed"
    stopped = asyncio.run(digest.send_digest(values, automatic=True))
    assert stopped["reason"] == "retry_limit_reached"
    assert row("SELECT attempts FROM notification_deliveries")["attempts"] == digest.MAX_DELIVERY_ATTEMPTS

    async def succeed(*_args):
        return None

    monkeypatch.setattr(digest, "_send_discord", succeed)
    recovered = asyncio.run(digest.send_digest(values))
    assert recovered["status"] == "sent"
    assert row("SELECT attempts FROM notification_deliveries")["attempts"] == digest.MAX_DELIVERY_ATTEMPTS + 1


def test_capped_channel_does_not_block_another_eligible_digest_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "channel-retry.db"))
    initialize()
    now = datetime.now(timezone.utc)
    key = digest.period_key(now, "UTC")
    with transaction() as con:
        con.execute(
            "INSERT INTO notification_deliveries(id,period_key,channel,status,attempts,updated_at) "
            "VALUES('capped-discord',?,'discord','failed',?,'2000-01-01 00:00:00')",
            (key, digest.MAX_DELIVERY_ATTEMPTS),
        )

    assert digest.digest_is_due(
        {
            "digest_enabled": "1",
            "digest_channels": "discord,email",
            "digest_timezone": "UTC",
            "digest_day": now.isoweekday(),
            "digest_time": "00:00",
        },
        at=now,
    )


def test_digest_channels_are_delivered_concurrently(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "concurrent-digest.db"))
    initialize()
    started = set()
    both_started = asyncio.Event()

    async def wait_for_peer(channel):
        started.add(channel)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)

    monkeypatch.setattr(digest, "_send_discord", lambda *_args: wait_for_peer("discord"))
    monkeypatch.setattr(digest, "_send_email", lambda *_args: wait_for_peer("email"))

    result = asyncio.run(
        digest.send_digest(
            {"digest_enabled": "1", "digest_channels": "discord,email", "digest_minimum_score": 0}
        )
    )
    assert started == {"discord", "email"}
    assert result["status"] == "sent"
    assert {item["status"] for item in result["deliveries"]} == {"sent"}


from auth_helpers import authenticated_headers


def _auth_headers(_client=None):
    return authenticated_headers()


def test_saving_channels_while_schedule_off_validates_and_enables_test_delivery(tmp_path, monkeypatch):
    import socket
    from fastapi.testclient import TestClient
    from afterword_engine.main import app, handle_job

    monkeypatch.setattr(settings, "db", str(tmp_path / "save-off.db"))
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("162.159.135.42", 443))],
    )
    initialize()

    with respx.mock(assert_all_called=False) as router:
        webhook_url = "https://discord.com/api/webhooks/999/secret-token"
        discord_route = router.post(webhook_url).mock(return_value=httpx.Response(204))

        with TestClient(app) as client:
            headers = _auth_headers(client)

            # Saving an enabled channel with missing webhook fails even when enabled=False.
            missing = client.put(
                "/api/digest/settings",
                headers=headers,
                json={
                    "enabled": False,
                    "channels": ["discord"],
                    "app_url": "https://books.example.com",
                },
            )
            assert missing.status_code == 400
            assert "Discord webhook URL is required" in missing.json()["detail"]

            # Saving valid Discord settings with enabled=False succeeds and encrypts webhook.
            saved = client.put(
                "/api/digest/settings",
                headers=headers,
                json={
                    "enabled": False,
                    "channels": ["discord"],
                    "app_url": "https://books.example.com",
                    "discord_webhook_url": webhook_url,
                },
            )
            assert saved.status_code == 200
            body = saved.json()["digest"]
            assert body["enabled"] is False
            assert body["channels"] == ["discord"]
            assert body["discord_webhook_set"] is True
            assert "secret-token" not in json.dumps(saved.json())
            stored_secret = row("SELECT value,secret FROM settings WHERE key='digest_discord_webhook_url'")
            assert stored_secret["secret"] == 1
            assert "secret-token" not in stored_secret["value"]

            # Test notification succeeds without HTTP 400 while schedule stays off.
            queued = client.post(
                "/api/digest/test",
                headers=headers,
                json={"channel": "discord"},
            )
            assert queued.status_code == 200
            job_result = asyncio.run(handle_job("digest:test:discord"))
            assert job_result["status"] == "sent"
            assert discord_route.call_count == 1

            # One-step schedule disable and channel disable work even if stored settings go stale.
            with transaction() as con:
                con.execute("UPDATE settings SET value='1' WHERE key='digest_enabled'")
                con.execute(
                    "UPDATE settings SET value='not-a-valid-webhook',secret=0 WHERE key='digest_discord_webhook_url'"
                )
            disable_schedule = client.put(
                "/api/digest/settings",
                headers=headers,
                json={
                    "enabled": False,
                    "channels": ["discord"],
                    "app_url": "https://books.example.com",
                },
            )
            assert disable_schedule.status_code == 200
            disable_channel = client.put(
                "/api/digest/settings",
                headers=headers,
                json={
                    "enabled": False,
                    "channels": [],
                    "app_url": "https://books.example.com",
                },
            )
            assert disable_channel.status_code == 200


def test_public_url_upgrades_untouched_docker_default_and_preserves_custom_url(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from afterword_engine.main import DigestSettingsIn, app

    monkeypatch.setattr(settings, "db", str(tmp_path / "docker-url.db"))
    monkeypatch.setattr(settings, "public_url", "http://127.0.0.1:3000")
    initialize()

    assert row("SELECT value FROM settings WHERE key='digest_app_url'")["value"] == "http://127.0.0.1:3000"
    assert digest.digest_config({})["app_url"] == "http://127.0.0.1:3000"
    assert DigestSettingsIn().app_url == "http://127.0.0.1:3000"

    # Operator configures AFTERWORD_PUBLIC_URL and restarts container: untouched 127.0.0.1:3000 upgrades.
    monkeypatch.setattr(settings, "public_url", "https://bookward.example.org")
    initialize()
    assert row("SELECT value FROM settings WHERE key='digest_app_url'")["value"] == "https://bookward.example.org"

    # Operator saves a custom URL in Settings: future AFTERWORD_PUBLIC_URL changes preserve it.
    with TestClient(app) as client:
        headers = _auth_headers(client)
        res = client.put(
            "/api/digest/settings",
            headers=headers,
            json={
                "enabled": False,
                "channels": [],
                "app_url": "https://custom-reader.example.net",
            },
        )
        assert res.status_code == 200

    monkeypatch.setattr(settings, "public_url", "https://another-host.example.org")
    initialize()
    assert row("SELECT value FROM settings WHERE key='digest_app_url'")["value"] == "https://custom-reader.example.net"


def test_twenty_book_discord_digest_stays_within_limit_and_keeps_review_link():
    items = [
        {
            "id": index,
            "title": f"Long Speculative Novel Title Number {index}: Chronicles of the Distant Horizon",
            "author": f"Author Name {index}",
            "score": 96 - (index * 0.5),
            "explanation": [
                f"Strong thematic overlap with your 5-star science fiction reads and literary worldbuilding ({index})"
            ],
        }
        for index in range(1, 21)
    ]
    config = {"app_url": "https://bookward.example.org"}
    payload = digest._discord_payload(items, config, "2026-W47")
    content = payload["content"]
    expected_link = "https://bookward.example.org?view=discover&digest=1&digest_period=2026-W47"

    assert len(content) <= 1990
    assert payload["allowed_mentions"] == {"parse": []}
    assert content.endswith(f"Review and shortlist these books in Bookward: {expected_link}")
    bullet_lines = [line for line in content.splitlines() if line.startswith("• ")]
    assert 1 <= len(bullet_lines) < 20
    # Every included book line is a complete entry (never severed mid-line).
    for index, line in enumerate(bullet_lines, start=1):
        assert line == digest._book_line(items[index - 1])
    remaining = 20 - len(bullet_lines)
    assert f"…and {remaining} more book" in content


def test_smtp_digest_sends_multipart_escaped_html_with_validated_covers(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "smtp-html.db"))
    initialize()

    sent_messages = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            self.host, self.port, self.timeout = host, port, timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def ehlo(self):
            return None

        def starttls(self, context):
            assert context is not None

        def login(self, username, password):
            return None

        def send_message(self, message):
            sent_messages.append(message)

    monkeypatch.setattr(digest.smtplib, "SMTP", FakeSMTP)
    with transaction() as con:
        con.execute("DELETE FROM candidates")
        src_id = row("SELECT id FROM sources LIMIT 1")["id"]
        con.execute(
            """INSERT INTO candidates(id,title,author,description,cover_url,source_url,source_id,score,explanation,status,normalized_key)
            VALUES(101,?,?,?,?,?,?,95,?,'recommended','xss-book-1'),(102,?,?,?,?,?,?,91,?,'recommended','untrusted-cover-2')""",
            (
                "Book <script>alert(1)</script>",
                'Ada "Lovelace" & Co.',
                "Desc",
                "https://covers.openlibrary.org/b/id/98765-L.jpg",
                "https://openlibrary.org/works/OL1W",
                src_id,
                json.dumps(["Matched <b>Piranesi</b>"]),
                "Untrusted Pixel Book",
                " Bob Writer ",
                "Desc",
                "https://evil-tracker.example/pixel.png",
                "https://evil-tracker.example/book",
                src_id,
                json.dumps(["Safe reason"]),
            ),
        )
        con.execute(
            "INSERT OR REPLACE INTO candidate_quality(candidate_id,quality_status,audit_version) VALUES(101,'accepted','v1'),(102,'accepted','v1')"
        )

    values = {
        "digest_enabled": "0",
        "digest_channels": "email",
        "digest_minimum_score": "80",
        "digest_maximum_books": "10",
        "digest_app_url": "https://bookward.example.org",
        "digest_email_to": "reader@example.com",
        "digest_email_from": "bookward@example.com",
        "digest_smtp_host": "smtp.example.com",
        "digest_smtp_port": "587",
        "digest_smtp_security": "starttls",
    }
    outcome = asyncio.run(digest.send_digest(values))
    assert outcome["status"] == "sent"
    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert msg.is_multipart()
    plain_part = msg.get_body(("plain",))
    html_part = msg.get_body(("html",))
    assert plain_part is not None and html_part is not None
    plain_text = plain_part.get_content()
    html_text = html_part.get_content()

    import html as html_lib

    expected_link = "https://bookward.example.org?view=discover&digest=1&digest_period=" + outcome["period"]
    assert expected_link in plain_text
    assert f'href="{html_lib.escape(expected_link, quote=True)}"' in html_text
    assert "<script>alert(1)</script>" not in html_text
    assert "Book &lt;script&gt;alert(1)&lt;/script&gt;" in html_text
    assert "Matched &lt;b&gt;Piranesi&lt;/b&gt;" in html_text
    assert "https://covers.openlibrary.org/b/id/98765-L.jpg" in html_text
    assert "evil-tracker.example" not in html_text


def test_bulk_feedback_preserves_rejected_books_and_logs_run_confidence(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from afterword_engine.main import app

    monkeypatch.setattr(settings, "db", str(tmp_path / "bulk-triage.db"))
    initialize()

    with TestClient(app) as client:
        headers = _auth_headers(client)
        overview = client.get("/api/overview?recommendation_limit=24", headers=headers).json()
        run_id = overview["recommendation_run_id"]
        rec_ids = [item["id"] for item in overview["recommendations"][:3]]
        assert len(rec_ids) == 3 and len(run_id) >= 8

        # Reader passes on the second recommendation in digest review mode.
        passed = client.post(
            f"/api/recommendations/{rec_ids[1]}/feedback",
            headers=headers,
            json={"action": "reject", "run_id": run_id},
        )
        assert passed.status_code == 200

        # Bulk shortlisting even with the passed ID included preserves 'rejected' and updates only recommended rows.
        bulk = client.post(
            "/api/recommendations/bulk-feedback",
            headers=headers,
            json={"ids": rec_ids, "action": "save", "run_id": run_id},
        )
        assert bulk.status_code == 200
        body = bulk.json()
        assert body["updated"] == 2
        assert body["skipped"] == 1
        assert body["ids"] == sorted([rec_ids[0], rec_ids[2]])

        assert row("SELECT status FROM candidates WHERE id=?", (rec_ids[1],))["status"] == "rejected"
        assert row("SELECT status FROM candidates WHERE id=?", (rec_ids[0],))["status"] == "saved"
        assert row("SELECT status FROM candidates WHERE id=?", (rec_ids[2],))["status"] == "saved"

        for saved_id in (rec_ids[0], rec_ids[2]):
            fb = row(
                "SELECT action,previous_status FROM feedback WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
                (saved_id,),
            )
            assert fb == {"action": "save", "previous_status": "recommended"}
            outcome = row(
                """SELECT o.confidence,o.label FROM recommendation_outcomes o
                JOIN recommendation_events e ON e.id=o.event_id
                WHERE e.candidate_id=? AND e.event_type='save'""",
                (saved_id,),
            )
            assert outcome == {"confidence": 0.8, "label": 1.0}

