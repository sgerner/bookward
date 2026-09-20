"""Weekly recommendation digests and notification channel adapters.

The digest is deliberately an engine concern rather than a SvelteKit concern:
it keeps running when the browser is closed, stores idempotent delivery
records, and never exposes provider credentials to the web application.
"""

from __future__ import annotations

import asyncio
import json
import re
import smtplib
import ssl
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .database import row, rows, transaction
from .security import safe_error_message, validate_public_url


CHANNELS = {"discord", "email"}
SMTP_SECURITY = {"none", "starttls", "ssl"}
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value, default, minimum=None, maximum=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _float(value, default, minimum=None, maximum=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def parse_channels(value) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = str(value or "").split(",")
    result = []
    for channel in values:
        channel = str(channel).strip().lower()
        if channel in CHANNELS and channel not in result:
            result.append(channel)
    return result


def digest_config(values: dict | None = None) -> dict:
    """Return typed internal config from persisted settings."""

    values = values or {}
    timezone_name = str(values.get("digest_timezone") or "UTC").strip() or "UTC"
    # Invalid zones are rejected on write. A bad legacy value should not take
    # down the scheduler, so normalize it to UTC here.
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone_name = "UTC"
    time_value = str(values.get("digest_time") or "09:00")
    if not _TIME_RE.fullmatch(time_value):
        time_value = "09:00"
    return {
        "enabled": _bool(values.get("digest_enabled")),
        "channels": parse_channels(values.get("digest_channels")),
        "day": _int(values.get("digest_day"), 1, 1, 7),
        "time": time_value,
        "timezone": timezone_name,
        "minimum_score": _float(values.get("digest_minimum_score"), 80, 0, 100),
        "maximum_books": _int(values.get("digest_maximum_books"), 5, 1, 20),
        "only_new": _bool(values.get("digest_only_new"), True),
        "app_url": str(values.get("digest_app_url") or "http://localhost:5173").rstrip("/"),
        "discord_webhook_url": str(values.get("digest_discord_webhook_url") or ""),
        "email_to": str(values.get("digest_email_to") or ""),
        "email_from": str(values.get("digest_email_from") or ""),
        "smtp_host": str(values.get("digest_smtp_host") or ""),
        "smtp_port": _int(values.get("digest_smtp_port"), 587, 1, 65535),
        "smtp_security": str(values.get("digest_smtp_security") or "starttls").lower(),
        "smtp_username": str(values.get("digest_smtp_username") or ""),
        "smtp_password": str(values.get("digest_smtp_password") or ""),
        "last_period": str(values.get("digest_last_period") or ""),
    }


def validate_digest_config(config: dict) -> None:
    """Validate user-provided settings before they are persisted."""

    try:
        ZoneInfo(config["timezone"])
    except (ZoneInfoNotFoundError, KeyError) as exc:
        raise ValueError("Digest timezone is not recognized") from exc
    if not _TIME_RE.fullmatch(str(config.get("time", ""))):
        raise ValueError("Digest time must be HH:MM")
    raw_channels = config.get("channels")
    if isinstance(raw_channels, (list, tuple, set)):
        raw_channel_values = raw_channels
    else:
        raw_channel_values = str(raw_channels or "").split(",")
    normalized_channel_values = [str(channel).strip().lower() for channel in raw_channel_values if str(channel).strip()]
    if any(channel not in CHANNELS for channel in normalized_channel_values):
        raise ValueError("Unsupported digest channel")
    channels = parse_channels(normalized_channel_values)
    # Turning the digest off is a kill switch. Do not make an operator repair
    # a stale webhook or SMTP credential before they can stop delivery. The
    # full provider validation runs again when the digest is enabled.
    enabled = _bool(config.get("enabled"), True)
    app_url = str(config.get("app_url") or "")
    parsed_app = urlparse(app_url)
    if (
        parsed_app.scheme not in {"http", "https"}
        or not parsed_app.hostname
        or parsed_app.username
        or parsed_app.password
        or parsed_app.query
        or parsed_app.fragment
        or any(c in app_url for c in "\r\n")
    ):
        raise ValueError("Digest app URL must use HTTP or HTTPS")
    webhook = str(config.get("discord_webhook_url") or "")
    if enabled and "discord" in channels:
        if not webhook:
            raise ValueError("Discord webhook URL is required when Discord is enabled")
        if not webhook.startswith("https://"):
            raise ValueError("Discord webhook URL must use HTTPS")
        hostname = (urlparse(webhook).hostname or "").lower().rstrip(".")
        if hostname not in {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}:
            raise ValueError("Discord webhook URL must point to Discord")
        # Validate the address at write time and again immediately before the
        # request to reduce SSRF risk if DNS changes after configuration.
        validate_public_url(webhook)
    if config.get("smtp_security") not in SMTP_SECURITY:
        raise ValueError("SMTP security must be none, starttls, or ssl")
    if enabled and "email" in channels:
        for label in ("email_to", "email_from"):
            if not _valid_email(config.get(label, "")):
                raise ValueError(f"Valid {label.replace('_', ' ')} is required when email is enabled")
        if not str(config.get("smtp_host") or "").strip():
            raise ValueError("SMTP host is required when email is enabled")


def _valid_email(value: str) -> bool:
    name, address = parseaddr(str(value or ""))
    return bool(address and "@" in address and not any(c in address for c in "\r\n"))


def period_key(at: datetime | None = None, timezone_name: str = "UTC") -> str:
    at = at or datetime.now(timezone.utc)
    try:
        local = at.astimezone(ZoneInfo(timezone_name))
    except ZoneInfoNotFoundError:
        local = at.astimezone(timezone.utc)
    year, week, _ = local.isocalendar()
    return f"{year}-W{week:02d}"


def digest_is_due(values: dict | None = None, at: datetime | None = None) -> bool:
    config = digest_config(values)
    if not config["enabled"] or not config["channels"]:
        return False
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    try:
        local = at.astimezone(ZoneInfo(config["timezone"]))
    except ZoneInfoNotFoundError:
        local = at.astimezone(timezone.utc)
    hour, minute = (int(part) for part in config["time"].split(":"))
    if local.isoweekday() != config["day"] or (local.hour, local.minute) < (hour, minute):
        return False
    current_period = period_key(at, config["timezone"])
    if config["last_period"] == current_period:
        return False
    # Failed providers are retried by the scheduler, but a short backoff keeps
    # a bad webhook or unavailable SMTP host from being hammered once a minute.
    failed = row(
        "SELECT updated_at FROM notification_deliveries WHERE period_key=? AND status='failed' ORDER BY updated_at DESC LIMIT 1",
        (current_period,),
    )
    if failed and failed.get("updated_at"):
        try:
            updated = datetime.fromisoformat(str(failed["updated_at"]).replace(" ", "T").replace("Z", "+00:00"))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if at - updated < timedelta(minutes=15):
                return False
        except ValueError:
            pass
    return True


def _candidate_rows(config: dict, include_seen: bool = False) -> list[dict]:
    params: list = [config["minimum_score"]]
    query = (
        "SELECT c.*, s.name AS source_name FROM candidates c "
        "LEFT JOIN sources s ON s.id=c.source_id "
        "WHERE c.status='recommended' AND c.score>=? AND COALESCE(s.enabled, 1)=1 "
        "AND book_identity(c.title,c.author) NOT IN (SELECT book_identity(title,author) FROM reads)"
    )
    if not include_seen and config["only_new"]:
        query += " AND NOT EXISTS (SELECT 1 FROM digest_items d WHERE d.candidate_id=c.id)"
    query += " ORDER BY c.score DESC, c.updated_at DESC, c.id DESC LIMIT ?"
    params.append(config["maximum_books"])
    result = rows(query, params)
    for item in result:
        try:
            item["genres"] = json.loads(item.get("genres") or "[]")
        except (TypeError, json.JSONDecodeError):
            item["genres"] = []
        try:
            item["explanation"] = json.loads(item.get("explanation") or "[]")
        except (TypeError, json.JSONDecodeError):
            item["explanation"] = []
    return result


def digest_preview(values: dict | None = None) -> dict:
    config = digest_config(values)
    items = _candidate_rows(config)
    return {
        "period": period_key(timezone_name=config["timezone"]),
        "items": items,
        "count": len(items),
        "channels": config["channels"],
        "minimum_score": config["minimum_score"],
        "maximum_books": config["maximum_books"],
    }


def _link(config: dict, key: str) -> str:
    # Keep the public link intentionally simple. The period is useful for
    # support/debugging, while the boolean digest flag activates the review
    # affordance in the Svelte app.
    return f"{config['app_url']}?view=discover&digest=1&digest_period={quote(key)}"


def _lines(items: list[dict], config: dict, key: str, test: bool = False) -> str:
    if test:
        return (
            "Bookward weekly digest test\n\n"
            "This is a test notification. Your digest channel is configured correctly.\n"
            f"Open Bookward: {config['app_url']}"
        )
    lines = [f"Bookward · new recommendations ({key})", ""]
    for item in items:
        explanation = item.get("explanation") or []
        reason = f" — {explanation[0]}" if explanation else ""
        lines.append(f"• {item['title']} — {item['author']} ({float(item['score']):.0f}%){reason}")
    lines += ["", f"Review and shortlist these books in Bookward: {_link(config, key)}"]
    return "\n".join(lines)


def _discord_payload(items: list[dict], config: dict, key: str, test=False) -> dict:
    content = _lines(items, config, key, test)
    # Discord rejects content over 2,000 characters. The selection limit keeps
    # this uncommon, but truncate as a final guard for long titles/reasons.
    return {"username": "Bookward", "content": content[:1990]}


async def _send_discord(config: dict, payload: dict) -> str:
    validate_public_url(config["discord_webhook_url"])
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            response = await client.post(config["discord_webhook_url"], json=payload)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"Discord webhook returned HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("Discord webhook request failed") from exc
    return "discord webhook"


def _smtp_send(config: dict, subject: str, body: str) -> str:
    host = config["smtp_host"].strip()
    security_mode = config["smtp_security"]
    context = ssl.create_default_context()
    if security_mode == "ssl":
        client = smtplib.SMTP_SSL(host, config["smtp_port"], timeout=20, context=context)
    else:
        client = smtplib.SMTP(host, config["smtp_port"], timeout=20)
    with client:
        client.ehlo()
        if security_mode == "starttls":
            client.starttls(context=context)
            client.ehlo()
        if config["smtp_username"]:
            client.login(config["smtp_username"], config["smtp_password"])
        message = EmailMessage()
        message["From"] = config["email_from"]
        message["To"] = config["email_to"]
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=False)
        message.set_content(body)
        client.send_message(message)
    return config["email_to"]


async def _send_email(config: dict, subject: str, body: str) -> str:
    return await asyncio.to_thread(_smtp_send, config, subject, body)


def _insert_delivery(key: str, channel: str, recipient: str, payload: dict, candidate_ids: list[int]) -> str:
    delivery_id = str(uuid.uuid4())
    with transaction() as con:
        con.execute(
            "INSERT INTO notification_deliveries(id,period_key,channel,status,recipient,payload,candidate_ids) "
            "VALUES(?,?,?,'pending',?,?,?) ON CONFLICT(period_key,channel) DO UPDATE SET "
            "status='pending',payload=excluded.payload,candidate_ids=excluded.candidate_ids,error=NULL,updated_at=CURRENT_TIMESTAMP",
            (delivery_id, key, channel, recipient, json.dumps(payload), json.dumps(candidate_ids)),
        )
        existing = con.execute(
            "SELECT id FROM notification_deliveries WHERE period_key=? AND channel=?", (key, channel)
        ).fetchone()
    return existing[0]


def _delivery_result(delivery_id: str, success: bool, error: str | None = None) -> None:
    with transaction() as con:
        if success:
            con.execute(
                "UPDATE notification_deliveries SET status='sent',attempts=attempts+1,error=NULL,sent_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (delivery_id,),
            )
        else:
            con.execute(
                "UPDATE notification_deliveries SET status='failed',attempts=attempts+1,error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (safe_error_message(error, limit=2000), delivery_id),
            )


def _mark_period_sent(key: str, candidate_ids: list[int]) -> None:
    """Atomically mark a fully delivered period and its candidates as seen."""

    with transaction() as con:
        for candidate_id in candidate_ids:
            con.execute(
                "INSERT OR IGNORE INTO digest_items(candidate_id,first_period) VALUES(?,?)",
                (candidate_id, key),
            )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES('digest_last_period',?,0) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=0,updated_at=CURRENT_TIMESTAMP",
            (key,),
        )


async def send_digest(values: dict | None = None, *, test_channel: str | None = None) -> dict:
    config = digest_config(values)
    channels = [test_channel] if test_channel else config["channels"]
    channels = [channel for channel in channels if channel in CHANNELS]
    key = f"test-{uuid.uuid4().hex[:12]}" if test_channel else period_key(timezone_name=config["timezone"])
    test = bool(test_channel)
    configured_channels = list(channels)
    items = [] if test else _candidate_rows(config)
    existing_deliveries = {
        item["channel"]: item
        for item in rows(
            "SELECT channel,status,candidate_ids FROM notification_deliveries WHERE period_key=?",
            (key,),
        )
    } if not test else {}
    # If one channel succeeded and another failed, retry only the missing
    # channel with the same candidate set. This prevents duplicate Discord or
    # email notifications while the failed channel is being recovered.
    if not test and existing_deliveries:
        for delivery in existing_deliveries.values():
            if delivery["candidate_ids"]:
                try:
                    existing_ids = [int(value) for value in json.loads(delivery["candidate_ids"])]
                except (TypeError, ValueError, json.JSONDecodeError):
                    existing_ids = []
                if existing_ids:
                    placeholders = ",".join("?" for _ in existing_ids)
                    items = rows(
                        f"SELECT c.*, s.name AS source_name FROM candidates c LEFT JOIN sources s ON s.id=c.source_id WHERE c.id IN ({placeholders}) ORDER BY c.score DESC, c.id DESC",
                        existing_ids,
                    )
                    for item in items:
                        try:
                            item["genres"] = json.loads(item.get("genres") or "[]")
                        except (TypeError, json.JSONDecodeError):
                            item["genres"] = []
                        try:
                            item["explanation"] = json.loads(item.get("explanation") or "[]")
                        except (TypeError, json.JSONDecodeError):
                            item["explanation"] = []
                    break
    if not test:
        channels = [
            channel
            for channel in channels
            if existing_deliveries.get(channel, {}).get("status") != "sent"
        ]
    if not test and not items:
        # The scheduler records the period even when the threshold produced no
        # matches, preventing a tight loop until the next week.
        if not test:
            with transaction() as con:
                con.execute(
                    "INSERT INTO settings(key,value,secret) VALUES('digest_last_period',?,0) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=0,updated_at=CURRENT_TIMESTAMP",
                    (key,),
                )
        return {"status": "skipped", "reason": "no_new_recommendations", "period": key, "items": 0, "deliveries": []}
    if not channels:
        if (
            not test
            and items
            and configured_channels
            and all(existing_deliveries.get(channel, {}).get("status") == "sent" for channel in configured_channels)
        ):
            _mark_period_sent(key, [int(item["id"]) for item in items])
            return {"status": "skipped", "reason": "already_delivered", "period": key, "items": len(items), "deliveries": []}
        return {"status": "skipped", "reason": "no_channels", "period": key, "items": len(items), "deliveries": []}
    candidate_ids = [int(item["id"]) for item in items]
    body = _lines(items, config, key, test)
    subject = "Bookward weekly digest test" if test else f"Bookward · {len(items)} new book recommendations"
    deliveries = []
    for channel in channels:
        if channel == "discord":
            payload = _discord_payload(items, config, key, test)
            recipient = "Discord webhook"
        else:
            payload = {"subject": subject, "body": body}
            recipient = config["email_to"]
        delivery_id = _insert_delivery(key, channel, recipient, payload, candidate_ids)
        try:
            if channel == "discord":
                await _send_discord(config, payload)
            else:
                await _send_email(config, subject, body)
            _delivery_result(delivery_id, True)
            deliveries.append({"id": delivery_id, "channel": channel, "status": "sent"})
        except Exception as exc:
            error = safe_error_message(exc)
            _delivery_result(delivery_id, False, error)
            deliveries.append({"id": delivery_id, "channel": channel, "status": "failed", "error": error})
    successful = {
        item["channel"]
        for item in deliveries
        if item["status"] == "sent"
    }
    successful.update(
        channel
        for channel, delivery in existing_deliveries.items()
        if delivery.get("status") == "sent"
    )
    # A recommendation is considered seen only after every configured channel
    # received the message. Failed channels remain retryable independently.
    if not test and configured_channels and successful == set(configured_channels):
        _mark_period_sent(key, candidate_ids)
    return {
        "status": "sent" if (test and successful) or (not test and configured_channels and successful == set(configured_channels)) else "failed",
        "period": key,
        "items": len(items),
        "deliveries": deliveries,
    }


async def retry_delivery(delivery_id: str, values: dict | None = None) -> dict:
    delivery = row("SELECT * FROM notification_deliveries WHERE id=?", (delivery_id,))
    if not delivery:
        raise ValueError("Delivery not found")
    if delivery["status"] == "sent":
        return {"id": delivery_id, "status": "sent", "duplicate": True}
    config = digest_config(values)
    channel = delivery["channel"]
    try:
        payload = json.loads(delivery["payload"] or "{}")
    except (TypeError, json.JSONDecodeError):
        error = "Stored delivery payload is invalid"
        _delivery_result(delivery_id, False, error)
        return {"id": delivery_id, "status": "failed", "error": error}
    try:
        if channel == "discord":
            await _send_discord(config, payload)
        elif channel == "email":
            await _send_email(config, payload.get("subject", "Bookward digest"), payload.get("body", ""))
        else:
            raise ValueError("Unsupported delivery channel")
        _delivery_result(delivery_id, True)
        configured_channels = set(config["channels"])
        delivered = rows(
            "SELECT channel,status FROM notification_deliveries WHERE period_key=?",
            (delivery["period_key"],),
        )
        if configured_channels and configured_channels.issubset(
            {item["channel"] for item in delivered if item["status"] == "sent"}
        ):
            try:
                candidate_ids = [
                    int(value) for value in json.loads(delivery["candidate_ids"] or "[]")
                ]
            except (TypeError, ValueError, json.JSONDecodeError):
                candidate_ids = []
            _mark_period_sent(delivery["period_key"], candidate_ids)
        return {"id": delivery_id, "status": "sent"}
    except Exception as exc:
        error = safe_error_message(exc)
        _delivery_result(delivery_id, False, error)
        return {"id": delivery_id, "status": "failed", "error": error}


def safe_digest_settings(values: dict | None = None, connection=None) -> dict:
    config = digest_config(values)
    query = (
        "SELECT id,period_key,channel,status,error,created_at,updated_at,sent_at "
        "FROM notification_deliveries ORDER BY created_at DESC LIMIT 1"
    )
    latest_row = connection.execute(query).fetchone() if connection is not None else row(query)
    latest = dict(latest_row) if latest_row else None
    return {
        "enabled": config["enabled"],
        "channels": config["channels"],
        "day": config["day"],
        "time": config["time"],
        "timezone": config["timezone"],
        "minimum_score": config["minimum_score"],
        "maximum_books": config["maximum_books"],
        "only_new": config["only_new"],
        "app_url": config["app_url"],
        "discord_webhook_set": bool(config["discord_webhook_url"]),
        "email_to": config["email_to"],
        "email_from": config["email_from"],
        "smtp_host": config["smtp_host"],
        "smtp_port": config["smtp_port"],
        "smtp_security": config["smtp_security"],
        "smtp_username_set": bool(config["smtp_username"]),
        "smtp_password_set": bool(config["smtp_password"]),
        "last_period": config["last_period"],
        "last_delivery": latest,
    }
