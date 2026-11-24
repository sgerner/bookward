"""Shared offline defaults and non-loopback socket guard for engine tests."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import threading
from typing import Any

import anyio
import anyio._core._sockets as anyio_sockets
import httpcore._backends.anyio as httpcore_anyio
import httpcore._backends.sync as httpcore_sync
import pytest

from afterword_engine import covers
from afterword_engine.config import settings

pytest_plugins = ("pytester",)


class ExternalSocketBlockedError(pytest.fail.Exception):
    """Raised immediately when a test attempts an unmocked external connection."""


def _is_loopback_host(host: str | bytes | None) -> bool:
    if host is None:
        return False
    if isinstance(host, bytes):
        try:
            text = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    else:
        text = str(host)
    normalized = text.strip().lower().rstrip(".")
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    if "%" in normalized:
        normalized = normalized.split("%", 1)[0]
    if not normalized:
        return False
    if normalized == "localhost":
        return True
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    if parsed.is_loopback:
        return True
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped.is_loopback
    return False


def _is_allowed_socket_address(family: int, address: Any) -> bool:
    if hasattr(socket, "AF_UNIX") and family == socket.AF_UNIX:
        return True
    if isinstance(address, tuple) and len(address) >= 1:
        return _is_loopback_host(address[0])
    return False


def _format_target(target: Any) -> str:
    if isinstance(target, tuple) and len(target) >= 2:
        return f"{target[0]}:{target[1]}"
    return repr(target)


@pytest.fixture(autouse=True)
def disable_background_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep TestClient startup synchronous and worker-free unless opted in."""
    monkeypatch.setattr(settings, "enable_background_loops", False)


@pytest.fixture(autouse=True)
def no_real_rate_limit_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip real Open Library sleeps unless a test installs its own fake clock."""

    async def advance_without_wait(delay: float) -> None:
        return None

    monkeypatch.setattr(covers, "_openlibrary_rate_sleep", advance_without_wait)


@pytest.fixture(autouse=True)
def block_non_loopback_sockets(monkeypatch: pytest.MonkeyPatch):
    """Fail immediately on unmocked non-loopback TCP/UDP socket traffic."""
    main_thread = threading.current_thread()
    background_failures: list[str] = []

    def fail_external(target: Any) -> None:
        message = (
            f"Blocked unmocked non-loopback network connection to {_format_target(target)}. "
            "Mock outbound HTTP requests with respx/httpx.MockTransport or connect only to "
            "loopback (127.0.0.1, ::1, localhost)."
        )
        if threading.current_thread() is not main_thread:
            background_failures.append(message)
        raise ExternalSocketBlockedError(message, pytrace=True)

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto
    original_create_connection = socket.create_connection
    original_loop_create_connection = asyncio.BaseEventLoop.create_connection
    original_loop_sock_connect = asyncio.BaseEventLoop.sock_connect
    original_anyio_connect_tcp = anyio_sockets.connect_tcp
    original_httpcore_sync_connect = httpcore_sync.SyncBackend.connect_tcp
    original_httpcore_anyio_connect = httpcore_anyio.AnyIOBackend.connect_tcp

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        if not _is_allowed_socket_address(self.family, address):
            fail_external(address)
        return original_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> int:
        if not _is_allowed_socket_address(self.family, address):
            fail_external(address)
        return original_connect_ex(self, address)

    def guarded_sendto(self: socket.socket, data: Any, *args: Any) -> int:
        if args and not _is_allowed_socket_address(self.family, args[-1]):
            fail_external(args[-1])
        return original_sendto(self, data, *args)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        if not (isinstance(address, tuple) and len(address) >= 1 and _is_loopback_host(address[0])):
            fail_external(address)
        return original_create_connection(address, *args, **kwargs)

    async def guarded_loop_create_connection(
        self: asyncio.BaseEventLoop,
        protocol_factory: Any,
        host: Any = None,
        port: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if host is not None and not _is_loopback_host(host):
            fail_external((host, port))
        return await original_loop_create_connection(
            self, protocol_factory, host, port, *args, **kwargs
        )

    async def guarded_loop_sock_connect(
        self: asyncio.BaseEventLoop, sock: socket.socket, address: Any
    ) -> Any:
        if not _is_allowed_socket_address(sock.family, address):
            fail_external(address)
        return await original_loop_sock_connect(self, sock, address)

    async def guarded_anyio_connect_tcp(
        remote_host: Any, remote_port: int, *args: Any, **kwargs: Any
    ) -> Any:
        if not _is_loopback_host(remote_host):
            fail_external((remote_host, remote_port))
        return await original_anyio_connect_tcp(remote_host, remote_port, *args, **kwargs)

    def guarded_httpcore_sync_connect(
        self: Any, host: str, port: int, *args: Any, **kwargs: Any
    ) -> Any:
        if not _is_loopback_host(host):
            fail_external((host, port))
        return original_httpcore_sync_connect(self, host, port, *args, **kwargs)

    async def guarded_httpcore_anyio_connect(
        self: Any, host: str, port: int, *args: Any, **kwargs: Any
    ) -> Any:
        if not _is_loopback_host(host):
            fail_external((host, port))
        return await original_httpcore_anyio_connect(self, host, port, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(
        asyncio.BaseEventLoop, "create_connection", guarded_loop_create_connection
    )
    monkeypatch.setattr(asyncio.BaseEventLoop, "sock_connect", guarded_loop_sock_connect)
    monkeypatch.setattr(anyio_sockets, "connect_tcp", guarded_anyio_connect_tcp)
    monkeypatch.setattr(anyio, "connect_tcp", guarded_anyio_connect_tcp)
    monkeypatch.setattr(
        httpcore_sync.SyncBackend, "connect_tcp", guarded_httpcore_sync_connect
    )
    monkeypatch.setattr(
        httpcore_anyio.AnyIOBackend, "connect_tcp", guarded_httpcore_anyio_connect
    )

    yield

    if background_failures:
        raise ExternalSocketBlockedError(background_failures[0], pytrace=False)
