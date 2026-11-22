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


def test_weekly_digest_lines_prefer_positive_or_source_reason_and_drop_legacy_jargon():
    books = [
        {
            "title": "Caveat First Legacy",
            "author": "Writer A",
            "score": 84.2,
            "genres": ["Sci-Fi"],
            "explanation": [
                'Reduced: similar to a 1–2★ book ("Bad Read")',
                "Raised using books you saved, imported, or positively rated (subject sci fi)",
                "From Literary Hub",
            ],
        },
        {
            "title": "Source Fallback Only",
            "author": "Writer B",
            "score": 78.0,
            "explanation": [
                "Ranking confidence reduced: catalog details are sparse",
                "Moved up for variety so one author does not crowd your top picks",
                "From Booker Longlist",
            ],
        },
        {
            "title": "Only Caveats",
            "author": "Writer C",
            "score": 72.5,
            "explanation": [
                "Lowered slightly after appearing on several recent days without a decision",
                "Reduced: books from the 1970s tend to score lower in your reading history",
            ],
        },
    ]
    text = digest._lines(books, {"app_url": "https://bookward.example"}, "2026-W47")
    assert "%" not in text
    assert "held-out ratings" not in text
    assert "imported" not in text
    assert '1–2★ book ("Bad Read")' not in text
    assert "Moved up for variety" not in text
    assert "Lowered slightly" not in text
    assert "Raised using books you shortlisted, added to Librarr, or rated highly (subject Sci-Fi)" in text
    assert "From Booker Longlist" in text
    assert "• Caveat First Legacy — Writer A (ranking score 84)" in text
    assert "• Only Caveats — Writer C (ranking score 72)" in text
