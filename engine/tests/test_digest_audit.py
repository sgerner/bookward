import asyncio
import json

import httpx
import pytest
import respx

from afterword_engine import digest
from afterword_engine.config import settings
from afterword_engine.database import initialize, row


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
