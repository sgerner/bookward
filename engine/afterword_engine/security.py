import ipaddress
import re
import socket
from urllib.parse import urlparse, urlsplit, urlunsplit

BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal"}
_ERROR_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def _redact_single_url(raw: str) -> str:
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        authority = hostname + port
        path = "/[redacted]" if "/webhooks/" in parsed.path.casefold() else parsed.path
        query = "[redacted]" if parsed.query else ""
        return urlunsplit((parsed.scheme, authority, path, query, ""))
    except ValueError:
        return "[redacted URL]"


def _redact_error_url(match):
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;:)]}":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    return _redact_single_url(raw) + trailing


def redact_url(url: object) -> str:
    """Redact query parameters and webhook paths on a single URL."""

    raw = str(url or "").strip()
    if not raw:
        return ""
    return _redact_single_url(raw)


def safe_error_message(error, limit=400):
    """Keep provider failures useful without persisting URL credentials."""

    message = str(error or "").strip() or "Request failed"
    return _ERROR_URL_RE.sub(_redact_error_url, message)[:limit]


def sanitize_pinned_error_message(
    error: object,
    *,
    address: str,
    hostname: str,
    limit: int = 400,
) -> str:
    """Replace pinned IP literals on address boundaries and redact URL secrets."""

    message = str(error or "").strip() or "Request failed"
    if address and hostname:
        try:
            parsed_ip = ipaddress.ip_address(address)
        except ValueError:
            parsed_ip = None
        if parsed_ip is not None and parsed_ip.version == 6:
            message = re.sub(
                re.escape(f"[{address}]"),
                hostname,
                message,
                flags=re.IGNORECASE,
            )
            message = re.sub(
                rf"(?<![0-9A-Fa-f:]){re.escape(address)}(?![0-9A-Fa-f:])",
                hostname,
                message,
                flags=re.IGNORECASE,
            )
        elif address:
            message = re.sub(
                rf"(?<![0-9.]){re.escape(address)}(?![0-9.])",
                hostname,
                message,
            )
    return safe_error_message(message, limit=limit)

def resolve_public_target(url: str, allow_http=False):
    parsed = urlparse(url)
    if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}): raise ValueError("Sources must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password: raise ValueError("Invalid public URL")
    host = parsed.hostname.lower().rstrip('.')
    if host in BLOCKED_HOSTS or host.endswith('.local'): raise ValueError("Local addresses are not allowed")
    try: infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except OSError as exc: raise ValueError("Host could not be resolved") from exc
    addresses = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global: raise ValueError("Private, loopback, and link-local addresses are not allowed")
        addresses.append(str(ip))
    if not addresses: raise ValueError("Host did not resolve to an address")
    return url, addresses[0], host

def validate_public_url(url: str, allow_http=False):
    resolve_public_target(url, allow_http)
    return url

def validate_service_url(url: str, allowed_hosts: set[str]):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Service URL must be a plain HTTP(S) origin")
    if parsed.hostname.lower().rstrip(".") in allowed_hosts: return url
    return validate_public_url(url, allow_http=parsed.scheme == "http")
