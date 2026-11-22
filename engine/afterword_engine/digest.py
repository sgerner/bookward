"""Weekly recommendation digests and notification channel adapters.

The digest is deliberately an engine concern rather than a SvelteKit concern:
it keeps running when the browser is closed, stores idempotent delivery
records, and never exposes provider credentials to the web application.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import smtplib
import sqlite3
import ssl
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .config import settings
from .covers import placeholder_cover_url, safe_cover_url
from .database import row, rows, transaction
from .identity import ReadIdentityIndex, book_row_identity_match_keys
from .ingestion import read_identity_rows_with_current_verified_work_ids
from .security import safe_error_message, validate_public_url


CHANNELS = {"discord", "email"}
SMTP_SECURITY = {"none", "starttls", "ssl"}
MAX_DELIVERY_ATTEMPTS = 3
DISCORD_MAX_CONTENT = 1990
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_HTML_IMG_TAG_RE = re.compile(r"<img\b[^>]*\bsrc=([\"'])(.*?)\1[^>]*>", re.IGNORECASE)


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
    except (ZoneInfoNotFoundError, ValueError):
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
        "app_url": str(
            values.get("digest_app_url") or settings.public_url or "http://localhost:5173"
        ).rstrip("/"),
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


def validate_digest_config(config: dict, *, previous: dict | None = None) -> None:
    """Validate user-provided settings before they are persisted."""

    try:
        ZoneInfo(config["timezone"])
    except (ZoneInfoNotFoundError, KeyError, ValueError, TypeError) as exc:
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
    # Turning an active schedule off is a one-step kill switch: do not make an
    # operator repair a stale webhook or SMTP credential before stopping
    # automatic delivery. When saving channels directly (even while scheduling
    # stays off), validate every active channel.
    enabled = _bool(config.get("enabled"), True)
    if previous is None:
        channels_to_validate = set(channels) if enabled else set()
    else:
        prev_enabled = _bool(previous.get("enabled"), False)
        prev_channels = set(parse_channels(previous.get("channels")))
        if prev_enabled and not enabled:
            channels_to_validate = set(channels) - prev_channels
        else:
            channels_to_validate = set(channels)
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
    if "discord" in channels_to_validate:
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
    if "email" in channels_to_validate:
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
    except (ZoneInfoNotFoundError, ValueError):
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
    deliveries = {
        item["channel"]: item
        for item in rows(
            "SELECT channel,status,updated_at,attempts FROM notification_deliveries "
            "WHERE period_key=?",
            (current_period,),
        )
    }
    pending_channels = [
        channel
        for channel in config["channels"]
        if deliveries.get(channel, {}).get("status") != "sent"
        and int(deliveries.get(channel, {}).get("attempts") or 0)
        < MAX_DELIVERY_ATTEMPTS
    ]
    if not pending_channels:
        return False
    eligible_failures = [
        deliveries[channel]
        for channel in pending_channels
        if deliveries.get(channel, {}).get("status") == "failed"
    ]
    if any(failed.get("updated_at") for failed in eligible_failures):
        try:
            for failed in eligible_failures:
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
        "SELECT c.*, s.name AS source_name,q.work_id quality_work_id,"
        "q.provider quality_provider,q.isbn13 quality_isbn13,"
        "q.isbn10 quality_isbn10 FROM candidates c "
        "LEFT JOIN sources s ON s.id=c.source_id "
        "JOIN candidate_quality q ON q.candidate_id=c.id "
        "WHERE c.status='recommended' AND q.quality_status='accepted' "
        "AND c.score>=? AND COALESCE(s.enabled, 1)=1 "
    )
    if not include_seen and config["only_new"]:
        query += " AND NOT EXISTS (SELECT 1 FROM digest_items d WHERE d.candidate_id=c.id)"
    query += " ORDER BY c.score DESC, c.updated_at DESC, c.id DESC"
    result = rows(query, params)
    read_rows = rows("SELECT * FROM reads")
    try:
        read_metadata = rows(
            "SELECT read_id,verified_work_id,identity_provider,identity_provider_id,identity_hash "
            "FROM read_metadata WHERE verified_work_id!=''"
        )
    except sqlite3.OperationalError as exc:
        if "no such table: read_metadata" not in str(exc).casefold():
            raise
        read_metadata = []
    read_index = ReadIdentityIndex.from_reads(
        read_identity_rows_with_current_verified_work_ids(read_rows, read_metadata)
    )
    shortlist_index = ReadIdentityIndex.from_reads(
        rows(
            "SELECT c.*,q.work_id quality_work_id,q.provider quality_provider,"
            "q.isbn13 quality_isbn13,q.isbn10 quality_isbn10 "
            "FROM candidates c LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
            "WHERE c.status IN ('saved','imported')"
        )
    )
    selected = []
    seen_keys = set()
    for item in result:
        keys = book_row_identity_match_keys(item)
        if read_index.matches(item) or shortlist_index.matches(item) or keys & seen_keys:
            continue
        selected.append(item)
        seen_keys.update(keys)
        if len(selected) >= config["maximum_books"]:
            break
    result = selected
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


def _book_line(item: dict, *, include_reason: bool = True) -> str:
    explanation = (item.get("explanation") or []) if include_reason else []
    reason = f" — {explanation[0]}" if explanation else ""
    return f"• {item['title']} — {item['author']} (ranking score {float(item['score']):.0f}){reason}"


def _lines(items: list[dict], config: dict, key: str, test: bool = False) -> str:
    if test:
        return (
            "Bookward weekly digest test\n\n"
            "This is a test notification. Your digest channel is configured correctly.\n"
            f"Open Bookward: {config['app_url']}"
        )
    lines = [f"Bookward · new recommendations ({key})", ""]
    for item in items:
        lines.append(_book_line(item))
    lines += ["", f"Review and shortlist these books in Bookward: {_link(config, key)}"]
    return "\n".join(lines)


def _overflow_line(remaining: int) -> str:
    noun = "book" if remaining == 1 else "books"
    return f"…and {remaining} more {noun} in this digest."


def _discord_payload(items: list[dict], config: dict, key: str, test=False) -> dict:
    if test:
        header = (
            "Bookward weekly digest test\n\n"
            "This is a test notification. Your digest channel is configured correctly."
        )
        footer = f"Open Bookward: {config['app_url']}"
        content = f"{header}\n{footer}"
        if len(content) > DISCORD_MAX_CONTENT and len(footer) < DISCORD_MAX_CONTENT:
            budget = DISCORD_MAX_CONTENT - len(footer) - 2
            content = f"{header[:max(0, budget)].rstrip()}…\n{footer}" if budget > 0 else footer
        return {
            "username": "Bookward",
            "content": content[:DISCORD_MAX_CONTENT],
            "allowed_mentions": {"parse": []},
        }

    header = f"Bookward · new recommendations ({key})"
    footer = f"Review and shortlist these books in Bookward: {_link(config, key)}"
    book_lines = [_book_line(item) for item in items]
    full = "\n".join([header, "", *book_lines, "", footer])
    if len(full) <= DISCORD_MAX_CONTENT:
        content = full
    else:
        content = ""
        for count in range(len(book_lines) - 1, 0, -1):
            overflow = _overflow_line(len(book_lines) - count)
            candidate = "\n".join([header, "", *book_lines[:count], overflow, "", footer])
            if len(candidate) <= DISCORD_MAX_CONTENT:
                content = candidate
                break
        if not content and items:
            remaining = len(items) - 1
            overflow_parts = [_overflow_line(remaining)] if remaining > 0 else []
            compact_first = _book_line(items[0], include_reason=False)
            candidate = "\n".join([header, "", compact_first, *overflow_parts, "", footer])
            if len(candidate) <= DISCORD_MAX_CONTENT:
                content = candidate
            else:
                shell_len = len("\n".join([header, "", "", *overflow_parts, "", footer]))
                budget = DISCORD_MAX_CONTENT - shell_len
                if budget > 4:
                    clipped_first = f"{compact_first[: budget - 1].rstrip()}…"
                    content = "\n".join([header, "", clipped_first, *overflow_parts, "", footer])
                else:
                    fallback = "\n".join([header, "", _overflow_line(len(items)), "", footer])
                    content = fallback if len(fallback) <= DISCORD_MAX_CONTENT else footer[:DISCORD_MAX_CONTENT]
        elif not content:
            content = full[:DISCORD_MAX_CONTENT]
    return {
        "username": "Bookward",
        "content": content[:DISCORD_MAX_CONTENT],
        "allowed_mentions": {"parse": []},
    }


async def _send_discord(config: dict, payload: dict) -> str:
    validate_public_url(config["discord_webhook_url"])
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            # Enforce this at delivery too, including retries of old payloads.
            response = await client.post(config["discord_webhook_url"], json={
                **payload, "allowed_mentions": {"parse": []},
            })
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"Discord webhook returned HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("Discord webhook request failed") from exc
    return "discord webhook"


def _email_cover_url(item: dict) -> str:
    """Return a validated HTTPS cover URL on KNOWN_COVER_HOSTS or empty string."""

    raw_cover = str(item.get("cover_url") or "").strip()
    if raw_cover:
        return safe_cover_url(raw_cover)
    return safe_cover_url(
        placeholder_cover_url(str(item.get("title") or ""), str(item.get("author") or ""))
    )


def _sanitize_html_images(markup: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw_url = html.unescape(match.group(2))
        validated = safe_cover_url(raw_url)
        if not validated:
            return ""
        return match.group(0)

    return _HTML_IMG_TAG_RE.sub(replace, markup)


def _email_html(items: list[dict], config: dict, key: str, test: bool = False) -> str:
    app_url = html.escape(str(config.get("app_url") or ""), quote=True)
    if test:
        return (
            "<!doctype html><html><body style=\"margin:0;padding:24px;background:#0f172a;"
            "color:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;\">"
            "<div style=\"max-width:600px;margin:0 auto;background:#1e293b;border-radius:12px;padding:24px;\">"
            "<h1 style=\"margin:0 0 12px;font-size:20px;color:#f8fafc;\">Bookward weekly digest test</h1>"
            "<p style=\"margin:0 0 20px;line-height:1.6;color:#cbd5e1;\">"
            "This is a test notification. Your digest channel is configured correctly."
            "</p>"
            f"<p style=\"margin:0;\"><a href=\"{app_url}\" style=\"display:inline-block;"
            "background:#38bdf8;color:#0f172a;text-decoration:none;font-weight:600;"
            "padding:10px 16px;border-radius:8px;\">Open Bookward</a></p>"
            "</div></body></html>"
        )

    review_url = html.escape(_link(config, key), quote=True)
    escaped_key = html.escape(key, quote=True)
    cards: list[str] = []
    for item in items:
        title = html.escape(str(item.get("title") or ""), quote=True)
        author = html.escape(str(item.get("author") or ""), quote=True)
        score = f"{float(item.get('score') or 0):.0f}"
        explanation = item.get("explanation") or []
        reason = html.escape(str(explanation[0]), quote=True) if explanation else ""
        cover = _email_cover_url(item)
        cover_cell = (
            f"<td width=\"84\" valign=\"top\" style=\"padding-right:16px;\">"
            f"<img src=\"{html.escape(cover, quote=True)}\" alt=\"Cover of {title}\" "
            f"width=\"84\" height=\"126\" style=\"display:block;width:84px;height:126px;"
            f"object-fit:cover;border-radius:6px;background:#0f172a;\" />"
            f"</td>"
            if cover
            else ""
        )
        reason_html = (
            f"<p style=\"margin:8px 0 0;font-size:14px;line-height:1.5;color:#cbd5e1;\">{reason}</p>"
            if reason
            else ""
        )
        cards.append(
            "<div style=\"margin:0 0 16px;padding:16px;background:#1e293b;"
            "border:1px solid #334155;border-radius:10px;\">"
            "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" width=\"100%\"><tr>"
            f"{cover_cell}"
            "<td valign=\"top\">"
            f"<div style=\"display:inline-block;margin:0 0 6px;padding:2px 8px;border-radius:999px;"
            f"background:#0f172a;color:#38bdf8;font-size:12px;font-weight:600;\">Ranking score {score}</div>"
            f"<h2 style=\"margin:0;font-size:17px;line-height:1.35;color:#f8fafc;\">{title}</h2>"
            f"<p style=\"margin:4px 0 0;font-size:14px;color:#94a3b8;\">{author}</p>"
            f"{reason_html}"
            f"<p style=\"margin:10px 0 0;font-size:13px;\"><a href=\"{review_url}\" "
            "style=\"color:#38bdf8;text-decoration:underline;\">Review in Bookward</a></p>"
            "</td></tr></table></div>"
        )

    return (
        "<!doctype html><html><body style=\"margin:0;padding:24px;background:#0f172a;"
        "color:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;\">"
        "<div style=\"max-width:640px;margin:0 auto;\">"
        f"<h1 style=\"margin:0 0 6px;font-size:22px;color:#f8fafc;\">Bookward · new recommendations ({escaped_key})</h1>"
        "<p style=\"margin:0 0 20px;font-size:14px;color:#94a3b8;\">"
        "Your highest-scoring new picks are ready to review."
        "</p>"
        f"{''.join(cards)}"
        "<div style=\"margin-top:24px;padding-top:16px;border-top:1px solid #334155;\">"
        f"<a href=\"{review_url}\" style=\"display:inline-block;background:#38bdf8;color:#0f172a;"
        "text-decoration:none;font-weight:600;padding:12px 18px;border-radius:8px;\">"
        "Review and shortlist these books in Bookward</a>"
        f"<p style=\"margin:12px 0 0;font-size:12px;color:#94a3b8;word-break:break-all;\">{review_url}</p>"
        "</div></div></body></html>"
    )


def _smtp_send(
    config: dict,
    subject: str,
    body: str | dict,
    html_body: str | None = None,
) -> str:
    if isinstance(body, dict):
        text_body = str(body.get("body") or "")
        resolved_html = html_body if html_body is not None else body.get("html")
    else:
        text_body = str(body or "")
        resolved_html = html_body
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
        message.set_content(text_body)
        if resolved_html:
            message.add_alternative(_sanitize_html_images(str(resolved_html)), subtype="html")
        client.send_message(message)
    return config["email_to"]


async def _send_email(
    config: dict,
    subject: str,
    body: str | dict,
    html_body: str | None = None,
) -> str:
    if html_body is None:
        return await asyncio.to_thread(_smtp_send, config, subject, body)
    return await asyncio.to_thread(_smtp_send, config, subject, body, html_body)


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


async def send_digest(
    values: dict | None = None,
    *,
    test_channel: str | None = None,
    automatic: bool = False,
) -> dict:
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
            "SELECT channel,status,candidate_ids,attempts FROM notification_deliveries WHERE period_key=?",
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
                        f"SELECT c.*, s.name AS source_name FROM candidates c LEFT JOIN sources s ON s.id=c.source_id JOIN candidate_quality q ON q.candidate_id=c.id WHERE c.id IN ({placeholders}) ORDER BY c.score DESC, c.id DESC",
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
            and (
                not automatic
                or int(existing_deliveries.get(channel, {}).get("attempts") or 0)
                < MAX_DELIVERY_ATTEMPTS
            )
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
        if automatic and any(
            int(existing_deliveries.get(channel, {}).get("attempts") or 0)
            >= MAX_DELIVERY_ATTEMPTS
            for channel in configured_channels
        ):
            return {"status": "failed", "reason": "retry_limit_reached", "period": key, "items": len(items), "deliveries": []}
        return {"status": "skipped", "reason": "no_channels", "period": key, "items": len(items), "deliveries": []}
    candidate_ids = [int(item["id"]) for item in items]
    body = _lines(items, config, key, test)
    html_body = _email_html(items, config, key, test)
    subject = "Bookward weekly digest test" if test else f"Bookward · {len(items)} new book recommendations"
    pending = []
    for channel in channels:
        if channel == "discord":
            payload = _discord_payload(items, config, key, test)
            recipient = "Discord webhook"
        else:
            payload = {"subject": subject, "body": body, "html": html_body}
            recipient = config["email_to"]
        delivery_id = _insert_delivery(key, channel, recipient, payload, candidate_ids)

        pending.append((channel, payload, delivery_id))

    async def deliver(channel, payload, delivery_id):
        try:
            if channel == "discord":
                await _send_discord(config, payload)
            else:
                await _send_email(config, subject, payload)
            _delivery_result(delivery_id, True)
            return {"id": delivery_id, "channel": channel, "status": "sent"}
        except Exception as exc:
            error = safe_error_message(exc)
            _delivery_result(delivery_id, False, error)
            return {"id": delivery_id, "channel": channel, "status": "failed", "error": error}

    deliveries = await asyncio.gather(
        *(deliver(channel, payload, delivery_id) for channel, payload, delivery_id in pending)
    )
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
    fully_delivered = bool(configured_channels) and set(configured_channels).issubset(successful)
    if not test and fully_delivered:
        _mark_period_sent(key, candidate_ids)
    return {
        "status": "sent" if (test and successful) or (not test and fully_delivered) else "failed",
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
            if "html" not in payload and "body" in payload:
                escaped_lines = "<br />".join(
                    html.escape(line, quote=True) for line in str(payload.get("body") or "").splitlines()
                )
                payload = {
                    **payload,
                    "html": f"<!doctype html><html><body><p>{escaped_lines}</p></body></html>",
                }
            await _send_email(config, payload.get("subject", "Bookward digest"), payload)
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
