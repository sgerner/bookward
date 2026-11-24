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
        webhook_url = "https://discord.com/api/webhooks/999/webhook-id"
        discord_route = router.post(webhook_url).mock(return_value=httpx.Response(204))

        client = TestClient(app)
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
        assert "webhook-id" not in json.dumps(saved.json())
        stored_secret = row("SELECT value,secret FROM settings WHERE key='digest_discord_webhook_url'")
        assert stored_secret["secret"] == 1
        assert "webhook-id" not in stored_secret["value"]

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

        # One-step schedule disable and channel disable work even if stored secrets cannot unseal.
        with transaction() as con:
            con.execute("UPDATE settings SET value='1' WHERE key='digest_enabled'")
            con.execute(
                "UPDATE settings SET value='',secret=1 WHERE key='digest_discord_webhook_url'"
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


def test_kill_switch_rejects_changed_webhook_host_and_invalid_email(tmp_path, monkeypatch):
    import socket
    from fastapi.testclient import TestClient
    from afterword_engine.main import app

    monkeypatch.setattr(settings, "db", str(tmp_path / "kill-switch-guard.db"))
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("162.159.135.42", 443))],
    )
    initialize()

    client = TestClient(app)
    headers = _auth_headers(client)
    valid_webhook = "https://discord.com/api/webhooks/123/valid-hook"

    active = client.put(
        "/api/digest/settings",
        headers=headers,
        json={
            "enabled": True,
            "channels": ["discord", "email"],
            "app_url": "https://books.example.com",
            "discord_webhook_url": valid_webhook,
            "email_to": "reader@example.com",
            "email_from": "bookward@example.com",
            "smtp_host": "smtp.example.com",
        },
    )
    assert active.status_code == 200

    # Kill-switch PUT (enabled=True -> False) with a non-Discord host MUST 400 and never persist.
    hijack = client.put(
        "/api/digest/settings",
        headers=headers,
        json={
            "enabled": False,
            "channels": ["discord", "email"],
            "discord_webhook_url": "https://attacker.example/x",
        },
    )
    assert hijack.status_code == 400
    assert "Discord webhook URL must point to Discord" in hijack.json()["detail"]

    insecure = client.put(
        "/api/digest/settings",
        headers=headers,
        json={
            "enabled": False,
            "channels": ["discord", "email"],
            "discord_webhook_url": "http://discord.com/api/webhooks/123/insecure",
        },
    )
    assert insecure.status_code == 400
    assert "HTTPS" in insecure.json()["detail"]

    bad_email = client.put(
        "/api/digest/settings",
        headers=headers,
        json={
            "enabled": False,
            "channels": ["discord", "email"],
            "email_to": "not-an-email",
        },
    )
    assert bad_email.status_code == 400
    assert "Valid email to is required" in bad_email.json()["detail"]

    # Stored settings remain untouched after all rejected kill-switch payloads.
    assert row("SELECT value FROM settings WHERE key='digest_enabled'")["value"] == "1"
    assert row("SELECT value FROM settings WHERE key='digest_email_to'")["value"] == "reader@example.com"


def test_stored_non_discord_webhook_is_refused_at_delivery_time(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(settings, "db", str(tmp_path / "stored-non-discord.db"))
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    initialize()

    attacker_url = "https://attacker.example/x"
    with respx.mock(assert_all_called=False) as router:
        attacker_route = router.post(attacker_url).mock(return_value=httpx.Response(204))

        with pytest.raises(ValueError, match="Discord webhook URL must point to Discord"):
            asyncio.run(digest._send_discord({"discord_webhook_url": attacker_url}, {"content": "hi"}))
        assert attacker_route.call_count == 0

        outcome = asyncio.run(
            digest.send_digest(
                {
                    "digest_enabled": "0",
                    "digest_channels": "discord",
                    "digest_minimum_score": "0",
                    "digest_discord_webhook_url": attacker_url,
                }
            )
        )
        assert outcome["status"] == "failed"
        assert "Discord webhook URL must point to Discord" in outcome["deliveries"][0]["error"]
        assert attacker_route.call_count == 0

        delivery_id = row("SELECT id FROM notification_deliveries LIMIT 1")["id"]
        retry = asyncio.run(
            digest.retry_delivery(
                delivery_id,
                {
                    "digest_enabled": "0",
                    "digest_channels": "discord",
                    "digest_minimum_score": "0",
                    "digest_discord_webhook_url": attacker_url,
                },
            )
        )
        assert retry["status"] == "failed"
        assert "Discord webhook URL must point to Discord" in retry["error"]
        assert attacker_route.call_count == 0


def test_public_url_upgrades_untouched_docker_default_and_preserves_custom_url(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from afterword_engine.main import DigestSettingsIn, app

    monkeypatch.setattr(settings, "db", str(tmp_path / "docker-url.db"))
    monkeypatch.setattr(settings, "public_url", "http://127.0.0.1:3000")
    initialize()

    assert row("SELECT value FROM settings WHERE key='digest_app_url'")["value"] == "http://127.0.0.1:3000"
    assert digest.digest_config({})["app_url"] == "http://127.0.0.1:3000"
    assert DigestSettingsIn().app_url is None

    # Operator configures AFTERWORD_PUBLIC_URL and restarts container: untouched 127.0.0.1:3000 upgrades.
    monkeypatch.setattr(settings, "public_url", "https://bookward.example.org")
    initialize()
    assert row("SELECT value FROM settings WHERE key='digest_app_url'")["value"] == "https://bookward.example.org"

    # Operator saves a custom URL in Settings: partial PUTs omitting app_url and restarts preserve it.
    client = TestClient(app)
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
    assert res.json()["digest"]["app_url"] == "https://custom-reader.example.net"

    partial = client.put(
        "/api/digest/settings",
        headers=headers,
        json={
            "enabled": False,
            "channels": [],
            "minimum_score": 85,
        },
    )
    assert partial.status_code == 200
    assert partial.json()["digest"]["app_url"] == "https://custom-reader.example.net"
    assert row("SELECT value FROM settings WHERE key='digest_app_url'")["value"] == "https://custom-reader.example.net"
    assert row("SELECT value FROM settings WHERE key='digest_app_url_custom'")["value"] == "1"

    monkeypatch.setattr(settings, "public_url", "https://another-host.example.org")
    initialize()
    assert row("SELECT value FROM settings WHERE key='digest_app_url'")["value"] == "https://custom-reader.example.net"


def test_existing_install_with_custom_loopback_app_url_is_preserved_on_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "legacy-loopback.db"))
    monkeypatch.setattr(settings, "public_url", "http://localhost:5173")
    initialize()

    # Simulate an existing installation before digest_app_url_custom existed where the
    # operator saved a custom loopback port (http://localhost:3000).
    with transaction() as con:
        con.execute("UPDATE settings SET value='http://localhost:3000' WHERE key='digest_app_url'")
        con.execute("DELETE FROM settings WHERE key='digest_app_url_custom'")

    monkeypatch.setattr(settings, "public_url", "https://bookward.example.org")
    initialize()

    assert row("SELECT value FROM settings WHERE key='digest_app_url'")["value"] == "http://localhost:3000"
    assert row("SELECT value FROM settings WHERE key='digest_app_url_custom'")["value"] == "1"


@pytest.mark.parametrize(
    "blocked_host",
    [
        "127.0.0.1",
        "::1",
        "[::1]",
        "::ffff:127.0.0.1",
        "10.0.0.5",
        "192.168.1.10",
        "169.254.169.254",
        "localhost",
        "mail.local",
    ],
)
def test_smtp_host_rejects_loopback_private_and_local_targets_unless_allowlisted(
    tmp_path, monkeypatch, blocked_host
):
    from fastapi.testclient import TestClient
    from afterword_engine.main import app

    monkeypatch.setattr(settings, "db", str(tmp_path / "smtp-guard.db"))
    monkeypatch.setattr(settings, "smtp_allowed_hosts", "")
    initialize()

    client = TestClient(app)
    headers = _auth_headers(client)

    rejected = client.put(
        "/api/digest/settings",
        headers=headers,
        json={
            "enabled": False,
            "channels": ["email"],
            "email_to": "reader@example.com",
            "email_from": "bookward@example.com",
            "smtp_host": blocked_host,
        },
    )
    assert rejected.status_code == 400
    assert "SMTP host" in rejected.json()["detail"]

    with pytest.raises(ValueError, match="SMTP host"):
        digest._smtp_send(
            {
                "smtp_host": blocked_host,
                "smtp_port": 587,
                "smtp_security": "starttls",
                "smtp_username": "",
                "smtp_password": "",
                "email_from": "bookward@example.com",
                "email_to": "reader@example.com",
            },
            "Subject",
            "Body",
        )

    # Explicit operator allowlist permits local mail relays (e.g. self-hosted bridge).
    monkeypatch.setattr(settings, "smtp_allowed_hosts", blocked_host)
    allowed = client.put(
        "/api/digest/settings",
        headers=headers,
        json={
            "enabled": False,
            "channels": ["email"],
            "email_to": "reader@example.com",
            "email_from": "bookward@example.com",
            "smtp_host": blocked_host,
        },
    )
    assert allowed.status_code == 200


