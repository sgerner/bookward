import ipaddress
import re
import socket
from urllib.parse import urlparse, urlsplit, urlunsplit

BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal"}
_ERROR_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def _redact_error_url(match):
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;:)]}":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        authority = hostname + port
        path = "/[redacted]" if "/webhooks/" in parsed.path.casefold() else parsed.path
        query = "[redacted]" if parsed.query else ""
        return urlunsplit((parsed.scheme, authority, path, query, "")) + trailing
    except ValueError:
        return "[redacted URL]" + trailing


def safe_error_message(error, limit=400):
    """Keep provider failures useful without persisting URL credentials."""

    message = str(error or "").strip() or "Request failed"
    return _ERROR_URL_RE.sub(_redact_error_url, message)[:limit]

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
