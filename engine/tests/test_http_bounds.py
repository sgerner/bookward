import asyncio
from collections.abc import Iterator
import gzip
import json
from pathlib import Path
import socket
import tracemalloc
import zlib

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest
import respx

from auth_helpers import authenticated_headers
from afterword_engine import covers, digest, embeddings, ingestion, librarr, llm_reranker, main, quality
from afterword_engine.association_sources.common import fetch_pinned_association_json
from afterword_engine.association_sources.google_books import GoogleBooksClient
from afterword_engine.association_sources.librarything import LibraryThingClient
from afterword_engine.association_sources.openlibrary import OpenLibraryClient
from afterword_engine.associations import RateLimiter
from afterword_engine.config import settings
from afterword_engine.database import initialize, row, transaction
from afterword_engine.http_bounds import (
    InvalidContentLengthError,
    JSON_CONTENT_TYPES,
    ResponseTooLargeError,
    UnsupportedContentTypeError,
    stream_json_async,
    stream_json_sync,
    stream_response_async,
)
from afterword_engine.secrets import seal


PINNED_IP = "93.184.216.34"


class CountingAsyncStream(httpx.AsyncByteStream):
    def __init__(self, chunk: bytes, count: int) -> None:
        self._chunk = chunk
        self._count = count
        self.chunks_yielded = 0
        self.bytes_yielded = 0
        self.closed = False

    async def __aiter__(self):
        for _ in range(self._count):
            self.chunks_yielded += 1
            self.bytes_yielded += len(self._chunk)
            yield self._chunk

    async def aclose(self) -> None:
        self.closed = True


class CountingSyncStream(httpx.SyncByteStream):
    def __init__(self, chunk: bytes, count: int) -> None:
        self._chunk = chunk
        self._count = count
        self.chunks_yielded = 0
        self.bytes_yielded = 0
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(self._count):
            self.chunks_yielded += 1
            self.bytes_yielded += len(self._chunk)
            yield self._chunk

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "http-bounds.db")
    initialize()
    return settings.db


def pin_public_dns(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PINNED_IP, 443))
        ],
    )


def test_fetch_bytes_rejects_10mb_video_mp4_with_zero_chunks_read(monkeypatch) -> None:
    pin_public_dns(monkeypatch)
    stream = CountingAsyncStream(b"v" * 100_000, 100)  # 10 MB

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "video/mp4", "content-length": "10000000"},
            stream=stream,
            request=request,
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            await ingestion.fetch_bytes("https://media.example.test/trailer.mp4", client=client)

    with pytest.raises(UnsupportedContentTypeError, match="unsupported content type"):
        asyncio.run(run())

    assert stream.chunks_yielded == 0
    assert stream.bytes_yielded == 0
    assert stream.closed is True


def test_fetch_bytes_rejects_oversized_content_length_with_zero_chunks_read(monkeypatch) -> None:
    pin_public_dns(monkeypatch)
    stream = CountingAsyncStream(b"<item/>" * 10_000, 150)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/rss+xml; charset=utf-8",
                "content-length": str(settings.source_max_bytes + 1),
            },
            stream=stream,
            request=request,
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            await ingestion.fetch_bytes("https://feeds.example.test/rss.xml", client=client)

    with pytest.raises(ResponseTooLargeError, match="Source response is too large"):
        asyncio.run(run())

    assert stream.chunks_yielded == 0
    assert stream.closed is True


@pytest.mark.parametrize(
    ("family", "private_ip"),
    [
        (socket.AF_INET, "127.0.0.1"),
        (socket.AF_INET, "10.0.0.8"),
        (socket.AF_INET, "169.254.169.254"),
        (socket.AF_INET6, "::1"),
    ],
)
def test_request_pinned_rejects_private_ips_before_sending_requests(
    monkeypatch, family: int, private_ip: str
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (family, socket.SOCK_STREAM, 6, "", (private_ip, 443))
        ],
    )
    requests_sent = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests_sent
        requests_sent += 1
        return httpx.Response(200, text="unexpected", request=request)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            await ingestion.fetch_bytes("https://feeds.example.test/rss.xml", client=client)

    with pytest.raises(ValueError, match="Private, loopback, and link-local"):
        asyncio.run(run())

    assert requests_sent == 0


def test_request_pinned_rejects_unsafe_missing_and_excessive_redirects_without_reading_bodies(
    monkeypatch,
) -> None:
    def resolve_by_host(host, *_args, **_kwargs):
        if host == "private.example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PINNED_IP, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve_by_host)

    # 1. Public source redirecting to a host that resolves to a loopback IP.
    private_redirect_stream = CountingAsyncStream(b"r" * 10_000, 5)
    private_hop_requests = 0

    def private_redirect_handler(request: httpx.Request) -> httpx.Response:
        nonlocal private_hop_requests
        private_hop_requests += 1
        assert request.headers["host"] == "feeds.example.test"
        return httpx.Response(
            302,
            headers={"location": "https://private.example.test/internal"},
            stream=private_redirect_stream,
            request=request,
        )

    async def run_url(handler, url: str):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            await ingestion.fetch_bytes(url, client=client)

    with pytest.raises(ValueError, match="Private, loopback, and link-local"):
        asyncio.run(run_url(private_redirect_handler, "https://feeds.example.test/rss.xml"))

    assert private_hop_requests == 1
    assert private_redirect_stream.chunks_yielded == 0
    assert private_redirect_stream.closed is True

    # 2. Public HTTPS source redirecting to plain HTTP when allow_goodreads_http=False.
    http_redirect_stream = CountingAsyncStream(b"h" * 10_000, 5)

    def http_redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "http://feeds.example.test/insecure.xml"},
            stream=http_redirect_stream,
            request=request,
        )

    with pytest.raises(ValueError, match="Sources must use HTTPS"):
        asyncio.run(run_url(http_redirect_handler, "https://feeds.example.test/rss.xml"))

    assert http_redirect_stream.chunks_yielded == 0
    assert http_redirect_stream.closed is True

    # 3. Redirect response with missing Location header.
    missing_loc_stream = CountingAsyncStream(b"m" * 10_000, 5)

    def missing_loc_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, stream=missing_loc_stream, request=request)

    with pytest.raises(ValueError, match="Source returned an invalid redirect"):
        asyncio.run(run_url(missing_loc_handler, "https://feeds.example.test/rss.xml"))

    assert missing_loc_stream.chunks_yielded == 0
    assert missing_loc_stream.closed is True

    # 4. Redirect chain exceeding SOURCE_MAX_REDIRECTS closes every hop stream without reading bytes.
    loop_streams: list[CountingAsyncStream] = []

    def loop_handler(request: httpx.Request) -> httpx.Response:
        hop_stream = CountingAsyncStream(b"l" * 10_000, 5)
        loop_streams.append(hop_stream)
        return httpx.Response(
            302,
            headers={"location": "/next"},
            stream=hop_stream,
            request=request,
        )

    with pytest.raises(ValueError, match="Source returned too many redirects"):
        asyncio.run(run_url(loop_handler, "https://feeds.example.test/start"))

    assert len(loop_streams) == ingestion.SOURCE_MAX_REDIRECTS + 1
    assert all(s.chunks_yielded == 0 and s.closed is True for s in loop_streams)


def test_fetch_bytes_aborts_10mb_chunked_rss_right_after_source_max_bytes(monkeypatch) -> None:
    pin_public_dns(monkeypatch)
    monkeypatch.setattr(settings, "source_max_bytes", 2_000_000)
    stream = CountingAsyncStream(b"x" * 100_000, 100)  # 10 MB total, 100 KB chunks

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/rss+xml"},
            stream=stream,
            request=request,
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            await ingestion.fetch_bytes("https://feeds.example.test/huge.xml", client=client)

    with pytest.raises(ValueError, match="Source response is too large"):
        asyncio.run(run())

    assert stream.chunks_yielded == 21
    assert stream.bytes_yielded == 2_100_000
    assert stream.closed is True


def test_openlibrary_edition_and_goodreads_tooltips_abort_oversized_streams(monkeypatch) -> None:
    pin_public_dns(monkeypatch)
    monkeypatch.setattr(covers, "OPEN_LIBRARY_MIN_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(settings, "source_max_bytes", 200_000)

    redirect_stream = CountingAsyncStream(b"r" * 50_000, 10)
    edition_stream = CountingAsyncStream(b"e" * 50_000, 20)  # 1 MB > 200 KB
    tooltip_stream = CountingAsyncStream(b"t" * 50_000, 20)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/isbn/"):
            return httpx.Response(
                302,
                headers={"location": "/books/OL999M.json"},
                stream=redirect_stream,
                request=request,
            )
        if request.url.path == "/books/OL999M.json":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=edition_stream,
                request=request,
            )
        if request.url.path == "/book/auto_complete_tooltip" or "tooltip" in request.url.path:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=tooltip_stream,
                request=request,
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def run_edition():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            await ingestion._request_openlibrary_edition(client, "9780593418574")

    with pytest.raises(ResponseTooLargeError, match="Open Library response is too large"):
        asyncio.run(run_edition())

    assert redirect_stream.chunks_yielded == 0
    assert redirect_stream.closed is True
    assert edition_stream.chunks_yielded == 5
    assert edition_stream.closed is True

    original_items = [
        {
            "title": "Card Title",
            "author": "Card Author",
            "source_url": "https://www.goodreads.com/book/show/42-card",
        }
    ]

    async def run_tooltips():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            return await ingestion.enrich_goodreads_blog_items(original_items, client)

    retained = asyncio.run(run_tooltips())
    assert retained == original_items
    assert tooltip_stream.chunks_yielded == 5
    assert tooltip_stream.closed is True


@pytest.mark.parametrize("provider_kind", ["google_books", "openlibrary", "librarything"])
def test_association_clients_abort_10mb_streams_right_after_source_max_bytes(
    database, monkeypatch, provider_kind: str
) -> None:
    pin_public_dns(monkeypatch)
    monkeypatch.setattr(settings, "source_max_bytes", 350_000)
    stream = CountingAsyncStream(b"a" * 100_000, 100)  # 10 MB

    with respx.mock:
        respx.get(url__regex=r"^https://93\.184\.216\.34/.*").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=stream,
            )
        )
        if provider_kind == "google_books":
            client = GoogleBooksClient(api_key="secret-google-key", rate_limiter=RateLimiter(0))
            coro = client.get_json("/books/v1/volumes", {"q": "Dune"})
        elif provider_kind == "openlibrary":
            client = OpenLibraryClient(rate_limiter=RateLimiter(0))
            coro = client.get_json("/search.json", {"q": "Dune"})
        else:
            client = LibraryThingClient("secret-lt-key", rate_limiter=RateLimiter(0))
            coro = client.recommendations(["9780441172719"])

        with pytest.raises(ValueError, match="response is too large"):
            asyncio.run(coro)

    assert stream.chunks_yielded == 4
    assert stream.bytes_yielded == 400_000
    assert stream.closed is True


def test_google_books_preview_error_redacts_api_key(database, monkeypatch) -> None:
    async def failing_collect(_self, _reads):
        req = httpx.Request(
            "GET",
            "https://www.googleapis.com/books/v1/volumes?q=Dune&key=super-secret-google-key",
        )
        resp = httpx.Response(503, request=req)
        raise httpx.HTTPStatusError(
            f"Server error '503 Service Unavailable' for url '{req.url}'",
            request=req,
            response=resp,
        )

    monkeypatch.setattr(main.GoogleBooksAssociatedProvider, "collect", failing_collect)

    with TestClient(main.app, headers=authenticated_headers()) as client:
        response = client.post("/api/associations/google_books/preview")

    assert response.status_code == 502
    assert "super-secret-google-key" not in response.text
    assert "[redacted]" in response.json()["detail"]


def test_gzip_compressed_bomb_counts_decompressed_bytes_async_and_sync() -> None:
    # Single raw compressed chunk expanding to 20 MB (~20 KB on wire) must abort
    # incrementally around max_bytes without allocating the full 20 MB payload.
    compressed = gzip.compress(b'{"pad":"' + (b"z" * 20_000_000) + b'"}')
    raw_deflator = zlib.compressobj(level=6, wbits=-zlib.MAX_WBITS)
    raw_deflate = raw_deflator.compress(b'{"pad":"' + (b"y" * 10_000_000) + b'"}') + raw_deflator.flush()
    assert len(compressed) < 30_000
    assert len(raw_deflate) < 20_000

    async_gzip_stream = CountingAsyncStream(compressed, 1)
    async_deflate_stream = CountingAsyncStream(raw_deflate, 1)
    sync_gzip_stream = CountingSyncStream(compressed, 1)

    def async_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/deflate":
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "deflate",
                    "content-length": str(len(raw_deflate)),
                },
                stream=async_deflate_stream,
                request=request,
            )
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
                "content-length": str(len(compressed)),
            },
            stream=async_gzip_stream,
            request=request,
        )

    def sync_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
                "content-length": str(len(compressed)),
            },
            stream=sync_gzip_stream,
            request=request,
        )

    async def run_async():
        async with httpx.AsyncClient(transport=httpx.MockTransport(async_handler)) as client:
            with pytest.raises(ResponseTooLargeError):
                await stream_json_async(client, "GET", "https://example.test/bomb", max_bytes=100_000)
            with pytest.raises(ResponseTooLargeError):
                await stream_json_async(client, "GET", "https://example.test/deflate", max_bytes=100_000)

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        asyncio.run(run_async())
        _cur_async, peak_async = tracemalloc.get_traced_memory()

        tracemalloc.reset_peak()
        with httpx.Client(transport=httpx.MockTransport(sync_handler)) as sync_client:
            with pytest.raises(ResponseTooLargeError):
                stream_json_sync(sync_client, "GET", "https://example.test/bomb", max_bytes=100_000)
        _cur_sync, peak_sync = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert async_gzip_stream.closed is True
    assert async_deflate_stream.closed is True
    assert sync_gzip_stream.closed is True
    assert peak_async < 2_000_000
    assert peak_sync < 2_000_000


def test_public_response_materialization_and_json_content_type_guards(monkeypatch) -> None:
    pin_public_dns(monkeypatch)
    monkeypatch.setattr(covers, "OPEN_LIBRARY_MIN_REQUEST_INTERVAL_SECONDS", 0)
    compressed_edition = gzip.compress(
        json.dumps({"isbn_13": ["9780593418574"], "works": [{"key": "/works/OL123W"}]}).encode("utf-8")
    )
    not_found_stream = CountingAsyncStream(b"ignored-404-body", 3)
    html_async_stream = CountingAsyncStream(b"<html>not json</html>", 5)
    html_sync_stream = CountingSyncStream(b"<html>not json</html>", 5)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/isbn/9780593418574.json":
            return httpx.Response(
                200,
                content=compressed_edition,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                    "content-length": str(len(compressed_edition)),
                },
                request=request,
            )
        if request.url.path == "/isbn/9780000000002.json":
            return httpx.Response(
                404,
                headers={"content-type": "application/json"},
                stream=not_found_stream,
                request=request,
            )
        if request.url.path == "/bad-length":
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "content-length": "not-an-int"},
                stream=html_async_stream,
                request=request,
            )
        if request.url.path == "/html":
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                stream=html_async_stream,
                request=request,
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            ok_resp = await ingestion._request_openlibrary_edition(client, "9780593418574")
            missing_resp = await ingestion._request_openlibrary_edition(client, "9780000000002")
            streamed_resp = await stream_response_async(
                client,
                "GET",
                "https://openlibrary.org/isbn/9780593418574.json",
                max_bytes=100_000,
                allowed_content_types=JSON_CONTENT_TYPES,
            )
            with pytest.raises(UnsupportedContentTypeError):
                await stream_json_async(client, "GET", "https://example.test/html", max_bytes=100_000)
            with pytest.raises(InvalidContentLengthError):
                await stream_json_async(client, "GET", "https://example.test/bad-length", max_bytes=100_000)
            return ok_resp, missing_resp, streamed_resp

    ok_resp, missing_resp, streamed_resp = asyncio.run(run())
    assert ok_resp.status_code == 200
    assert ok_resp.json()["works"] == [{"key": "/works/OL123W"}]
    assert "content-encoding" not in ok_resp.headers
    assert missing_resp.status_code == 404
    assert missing_resp.content == b""
    assert not_found_stream.chunks_yielded == 0
    assert not_found_stream.closed is True
    assert streamed_resp.json()["isbn_13"] == ["9780593418574"]
    assert html_async_stream.chunks_yielded == 0
    assert html_async_stream.closed is True

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                stream=html_sync_stream,
                request=req,
            )
        )
    ) as sync_client:
        with pytest.raises(UnsupportedContentTypeError):
            stream_json_sync(sync_client, "GET", "https://example.test/html", max_bytes=100_000)
    assert html_sync_stream.chunks_yielded == 0
    assert html_sync_stream.closed is True


def test_catalog_and_quality_clients_reject_oversized_streams_and_fallback(monkeypatch) -> None:
    monkeypatch.setattr(covers, "OPEN_LIBRARY_MIN_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(settings, "source_max_bytes", 150_000)

    ol_stream = CountingAsyncStream(b"o" * 50_000, 10)  # 500 KB > 150 KB
    work_stream = CountingAsyncStream(b"w" * 50_000, 10)
    apple_stream = CountingAsyncStream(b"i" * 50_000, 10)

    def handler(request: httpx.Request) -> httpx.Response:
        target = (request.url.host, request.url.path)
        if target == ("openlibrary.org", "/search.json"):
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=ol_stream,
                request=request,
            )
        if target == ("openlibrary.org", "/works/OL123W.json"):
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=work_stream,
                request=request,
            )
        if target == ("www.googleapis.com", "/books/v1/volumes"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "gb-1",
                            "volumeInfo": {
                                "title": "Fallback Book",
                                "authors": ["Fallback Author"],
                                "description": "Recovered from Google Books after oversized Open Library stream.",
                            },
                        }
                    ]
                },
                request=request,
            )
        if target == ("itunes.apple.com", "/search"):
            return httpx.Response(
                200,
                headers={"content-type": "text/javascript; charset=utf-8"},
                stream=apple_stream,
                request=request,
            )
        raise AssertionError(f"Unexpected URL: {request.url}")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolved = await covers.resolve_book_metadata("Fallback Book", "Fallback Author", client=client)
            work = await covers.resolve_openlibrary_work_metadata("/works/OL123W", client=client)
            trace: list[dict] = []
            apple = await quality._apple_books_match("Fallback Book", "Fallback Author", client, trace=trace)
            return resolved, work, apple, trace

    resolved, work, apple, trace = asyncio.run(run())
    assert ol_stream.chunks_yielded == 4
    assert ol_stream.closed is True
    assert resolved["provider"] == "google_books"
    assert "Recovered from Google Books" in resolved["description"]

    assert work_stream.chunks_yielded == 4
    assert work_stream.closed is True
    work_trace = work["metadata_provenance"]["fetch_trace"][0]
    assert work_trace["status"] == "request_error"
    assert work_trace["error_type"] == "ResponseTooLargeError"

    assert apple is None
    assert apple_stream.chunks_yielded == 4
    assert apple_stream.closed is True
    assert trace[0]["status"] == "request_error"
    assert trace[0]["error_type"] == "ResponseTooLargeError"


@respx.mock
def test_librarr_json_and_sse_stream_enforce_byte_caps(database, monkeypatch) -> None:
    monkeypatch.setattr(settings, "librarr_max_bytes", 100_000)
    monkeypatch.setattr(settings, "librarr_stream_max_bytes", 120_000)
    with transaction() as con:
        con.execute("UPDATE candidates SET status='saved' WHERE id=1")
        con.execute("INSERT INTO settings(key,value,secret) VALUES('librarr_url','http://librarr:5050',0)")
        con.execute("INSERT INTO settings(key,value,secret) VALUES('librarr_api_key',?,1)", (seal("test-key"),))

    search_stream = CountingAsyncStream(b"s" * 40_000, 10)  # 400 KB > 100 KB
    wishlist_stream = CountingAsyncStream(b"w" * 40_000, 10)
    sse_chunk = b'event: results\ndata: {"results":[{"id":"1","title":"Book"}]}\n\n'
    # Each sse_chunk * 500 is ~31 KB; 10 chunks ~ 310 KB > 120 KB cap
    sse_stream = CountingAsyncStream(sse_chunk * 500, 10)

    respx.get("http://librarr:5050/api/search").mock(
        return_value=httpx.Response(200, headers={"content-type": "application/json"}, stream=search_stream)
    )
    respx.post("http://librarr:5050/api/wishlist").mock(
        return_value=httpx.Response(200, headers={"content-type": "application/json"}, stream=wishlist_stream)
    )
    respx.get("http://librarr:5050/api/search/stream").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=sse_stream)
    )
    respx.get("http://librarr:5050/api/search/audiobooks/stream").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "content-length": "999999"},
            stream=CountingAsyncStream(sse_chunk, 5),
        )
    )

    with TestClient(main.app, headers=authenticated_headers()) as client:
        search_resp = client.get("/api/librarr/search", params={"q": "Dune", "media_type": "ebook"})
        import_resp = client.post("/api/recommendations/1/import")
        sse_resp = client.get("/api/librarr/search/stream", params={"q": "Dune", "media_type": "ebook"})
        declared_sse = client.get("/api/librarr/search/stream", params={"q": "Dune", "media_type": "audiobook"})
        bad_media_sse = client.get("/api/librarr/search/stream", params={"q": "Dune", "media_type": "vinyl"})

    assert search_resp.status_code == 502
    assert search_stream.chunks_yielded == 3
    assert search_stream.closed is True

    assert import_resp.status_code == 502
    assert wishlist_stream.chunks_yielded == 3
    assert wishlist_stream.closed is True
    assert row("SELECT status FROM librarr_imports WHERE candidate_id=1")["status"] == "failed"

    assert sse_resp.status_code == 200
    assert sse_stream.chunks_yielded == 4
    assert sse_stream.closed is True
    sse_frames = [frame for frame in sse_resp.text.split("\n\n") if frame.strip()]
    assert sse_frames[-1].startswith("event: error\ndata: ")
    error_data = json.loads(sse_frames[-1].split("data: ", 1)[1])
    assert error_data == {"message": "Librarr search stream exceeded the maximum response size."}

    assert declared_sse.status_code == 502
    assert "too large" in declared_sse.json()["detail"]
    assert bad_media_sse.status_code == 400


@respx.mock
def test_embeddings_rerankers_and_discord_enforce_byte_caps(database, monkeypatch) -> None:
    # 1. 64-item embedding batch succeeds under 16 MB while oversized stream aborts early.
    vectors_64 = [{"index": idx, "embedding": [0.125] * 64} for idx in range(64)]
    oversized_embed = CountingAsyncStream(b"v" * 100_000, 20)

    respx.post("https://embed.example.test/v1/embeddings").mock(
        side_effect=[
            httpx.Response(200, json={"data": vectors_64}),
            httpx.Response(200, headers={"content-type": "application/json"}, stream=oversized_embed),
        ]
    )
    embedder = embeddings.OpenAICompatibleEmbedder("https://embed.example.test/v1", "emb-model", "secret")
    ok_batch = asyncio.run(embedder.embed([f"book-{i}" for i in range(64)]))
    assert len(ok_batch) == 64

    monkeypatch.setattr(settings, "embedding_max_bytes", 250_000)
    with pytest.raises(ResponseTooLargeError, match="Embedding response is too large"):
        asyncio.run(embedder.embed(["too-big"]))
    assert oversized_embed.chunks_yielded == 3
    assert oversized_embed.closed is True

    # 2. Sync LLM reranker closes redirect streams without reading bodies and aborts oversized streams.
    pin_public_dns(monkeypatch)
    llm_reranker.clear_llm_rerank_cache()
    monkeypatch.setattr(settings, "llm_rerank_max_bytes", 150_000)
    sync_redirect_stream = CountingSyncStream(b"r" * 40_000, 5)
    sync_llm_stream = CountingSyncStream(b"c" * 60_000, 10)
    candidates = [
        {"id": 1, "title": "First", "author": "A", "score": 90.0},
        {"id": 2, "title": "Second", "author": "B", "score": 85.0},
    ]

    respx.post("https://llm-sync.example.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            307,
            headers={"location": "https://llm-sync.example.test/v2/chat/completions"},
            stream=sync_redirect_stream,
        )
    )
    respx.post("https://llm-sync.example.test/v2/chat/completions").mock(
        return_value=httpx.Response(200, headers={"content-type": "application/json"}, stream=sync_llm_stream)
    )
    reranked_sync, diag_sync = llm_reranker.rerank_with_llm_sync(
        [{"title": "Dune", "author": "Frank Herbert", "rating": 5.0}],
        candidates,
        api_url="https://llm-sync.example.test/v1",
        api_key="sk-test",
        model="gpt-4o-mini",
    )
    assert diag_sync == {"applied": False, "reason": "fallback"}
    assert [c["id"] for c in reranked_sync] == [1, 2]
    assert sync_redirect_stream.chunks_yielded == 0
    assert sync_redirect_stream.closed is True
    assert sync_llm_stream.chunks_yielded == 3
    assert sync_llm_stream.closed is True

    # 3. Discord webhook enforces 1 MB cap without leaking webhook token.
    monkeypatch.setattr(settings, "discord_max_bytes", 100_000)
    discord_stream = CountingAsyncStream(b"d" * 40_000, 10)
    secret_webhook = "https://discord.com/api/webhooks/999/super-secret-webhook-token"
    respx.post(secret_webhook).mock(
        return_value=httpx.Response(200, headers={"content-type": "text/plain"}, stream=discord_stream)
    )
    with pytest.raises(RuntimeError, match="Discord webhook request failed") as discord_exc:
        asyncio.run(digest._send_discord({"discord_webhook_url": secret_webhook}, {"content": "hi"}))
    assert "super-secret-webhook-token" not in str(discord_exc.value)
    assert discord_stream.chunks_yielded == 3
    assert discord_stream.closed is True

    # Non-Discord webhook hosts are rejected by validate_discord_webhook_url before sending.
    with pytest.raises(ValueError, match="Discord webhook URL must point to Discord"):
        asyncio.run(
            digest._send_discord(
                {"discord_webhook_url": "https://evil.example.test/api/webhooks/1/secret"},
                {"content": "hi"},
            )
        )


def test_models_dev_catalog_enforces_byte_cap_and_serves_stale_cache(monkeypatch) -> None:
    monkeypatch.setitem(main._MODELS_DEV_CACHE, "providers", [])
    monkeypatch.setitem(main._MODELS_DEV_CACHE, "expires_at", 0.0)

    real_async_client = httpx.AsyncClient
    initial_calls = 0

    def ok_handler(request: httpx.Request) -> httpx.Response:
        nonlocal initial_calls
        initial_calls += 1
        return httpx.Response(
            200,
            json={
                "anthropic": {
                    "name": "Anthropic",
                    "models": {"claude-sonnet": {"id": "claude-sonnet", "name": "Claude Sonnet"}},
                }
            },
            request=request,
        )

    monkeypatch.setattr(
        main.httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_async_client(
            *args, transport=httpx.MockTransport(ok_handler), **kwargs
        ),
    )
    recovered = asyncio.run(main.llm_models_catalog())
    cached_hit = asyncio.run(main.llm_models_catalog())
    assert initial_calls == 1
    assert recovered["cached"] is False
    assert cached_hit["cached"] is True
    assert recovered["providers"][0]["id"] == "anthropic"

    # Expired stale cache returns stale=True when an oversized stream exceeds models_dev_max_bytes.
    monkeypatch.setattr(settings, "models_dev_max_bytes", 150_000)
    main._MODELS_DEV_CACHE["expires_at"] = 0.0
    oversized_models = CountingAsyncStream(b"m" * 50_000, 10)  # 500 KB > 150 KB

    def oversized_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=oversized_models,
            request=request,
        )

    monkeypatch.setattr(
        main.httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_async_client(
            *args, transport=httpx.MockTransport(oversized_handler), **kwargs
        ),
    )
    stale = asyncio.run(main.llm_models_catalog())
    assert oversized_models.chunks_yielded == 4
    assert oversized_models.closed is True
    assert stale == {"providers": recovered["providers"], "cached": True, "stale": True}

    # Cold cache surfaces 502 when models.dev exceeds models_dev_max_bytes.
    main._MODELS_DEV_CACHE["providers"] = []
    main._MODELS_DEV_CACHE["expires_at"] = 0.0
    cold_oversized = CountingAsyncStream(b"m" * 50_000, 10)
    monkeypatch.setattr(
        main.httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_async_client(
            *args,
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=cold_oversized,
                    request=req,
                )
            ),
            **kwargs,
        ),
    )
    with pytest.raises(HTTPException) as cold_exc:
        asyncio.run(main.llm_models_catalog())
    assert cold_exc.value.status_code == 502
    assert cold_oversized.chunks_yielded == 4
    assert cold_oversized.closed is True


def test_streaming_pinned_requests_redact_query_secrets_and_sanitize_errors(
    database, monkeypatch
) -> None:
    pin_public_dns(monkeypatch)

    class MidStreamTimeoutStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        async def __aiter__(self):
            yield b'{"partial":'
            raise httpx.ReadTimeout(f"Timed out reading from {PINNED_IP}:443")

        async def aclose(self) -> None:
            self.closed = True

    ok_stream = CountingAsyncStream(b'{"ok":true}', 1)
    status_err_stream = CountingAsyncStream(b'{"error":"denied"}', 1)
    mid_timeout_stream = MidStreamTimeoutStream()

    def ingestion_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == PINNED_IP
        assert request.headers["host"] == "feeds.example.test"
        if request.url.path == "/ok.json":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=ok_stream,
                request=request,
            )
        if request.url.path == "/denied.xml":
            return httpx.Response(
                401,
                headers={"content-type": "application/xml"},
                stream=status_err_stream,
                request=request,
            )
        if request.url.path == "/connect-timeout":
            raise httpx.ConnectTimeout(
                f"Timed out connecting to {PINNED_IP}:443 for https://{PINNED_IP}/connect-timeout?token=secret-123"
            )
        if request.url.path == "/read-timeout":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=mid_timeout_stream,
                request=request,
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def run_ingestion_checks():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(ingestion_handler),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with ingestion._request_pinned(
                client,
                "GET",
                "https://feeds.example.test/ok.json?token=secret-123",
            ) as resp:
                public_req_url = str(resp.request.url)

            with pytest.raises(httpx.HTTPStatusError) as status_exc:
                await ingestion.fetch_bytes(
                    "https://feeds.example.test/denied.xml?token=secret-123",
                    client=client,
                )

            with pytest.raises(httpx.ConnectTimeout) as connect_exc:
                await ingestion.fetch_bytes(
                    "https://feeds.example.test/connect-timeout?token=secret-123",
                    client=client,
                )

            with pytest.raises(httpx.ReadTimeout) as read_exc:
                await ingestion.fetch_bytes(
                    "https://feeds.example.test/read-timeout?token=secret-123",
                    client=client,
                )
            return public_req_url, status_exc.value, connect_exc.value, read_exc.value

    public_req_url, status_err, connect_err, read_err = asyncio.run(run_ingestion_checks())
    assert public_req_url == "https://feeds.example.test/ok.json?[redacted]"
    assert ok_stream.closed is True
    assert status_err_stream.closed is True
    assert mid_timeout_stream.closed is True

    for err in (status_err, connect_err, read_err):
        assert PINNED_IP not in str(err)
        assert "secret-123" not in str(err)
        assert err.request.url.host == "feeds.example.test"
        assert "secret-123" not in str(err.request.url)
        assert "[redacted]" in str(err.request.url)

    # Verify the same guarantees on fetch_pinned_association_json across 200, 401, ConnectTimeout, and mid-stream ReadTimeout.
    assoc_ok_stream = CountingAsyncStream(b'{"items":[]}', 1)
    assoc_401_stream = CountingAsyncStream(b'{"error":"invalid key"}', 1)
    assoc_read_timeout = MidStreamTimeoutStream()
    observed_assoc_urls: list[str] = []

    with respx.mock:
        respx.get(f"https://{PINNED_IP}/assoc/ok").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=assoc_ok_stream,
            )
        )
        respx.get(f"https://{PINNED_IP}/assoc/denied").mock(
            return_value=httpx.Response(
                401,
                headers={"content-type": "application/json"},
                stream=assoc_401_stream,
            )
        )
        respx.get(f"https://{PINNED_IP}/assoc/connect-timeout").mock(
            side_effect=httpx.ConnectTimeout(
                f"Connect timeout to {PINNED_IP}:443 for https://{PINNED_IP}/assoc/connect-timeout?key=assoc-secret-999"
            )
        )
        respx.get(f"https://{PINNED_IP}/assoc/read-timeout").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=assoc_read_timeout,
            )
        )

        payload, _headers = asyncio.run(
            fetch_pinned_association_json(
                "https://assoc.example.test/assoc/ok",
                params={"key": "assoc-secret-999"},
                provider="google_books",
                request_key="ok",
                source_label="Association Test",
                timeout_seconds=5.0,
                max_bytes=65536,
                on_response=lambda resp: observed_assoc_urls.append(str(resp.request.url)),
            )
        )
        assert payload == {"items": []}
        assert observed_assoc_urls == ["https://assoc.example.test/assoc/ok?[redacted]"]
        assert assoc_ok_stream.closed is True

        with pytest.raises(httpx.HTTPStatusError) as assoc_status_exc:
            asyncio.run(
                fetch_pinned_association_json(
                    "https://assoc.example.test/assoc/denied",
                    params={"key": "assoc-secret-999"},
                    provider="google_books",
                    request_key="denied",
                    source_label="Association Test",
                    timeout_seconds=5.0,
                    max_bytes=65536,
                )
            )
        assert assoc_401_stream.closed is True

        with pytest.raises(httpx.ConnectTimeout) as assoc_connect_exc:
            asyncio.run(
                fetch_pinned_association_json(
                    "https://assoc.example.test/assoc/connect-timeout",
                    params={"key": "assoc-secret-999"},
                    provider="google_books",
                    request_key="connect-timeout",
                    source_label="Association Test",
                    timeout_seconds=5.0,
                    max_bytes=65536,
                )
            )

        with pytest.raises(httpx.ReadTimeout) as assoc_read_exc:
            asyncio.run(
                fetch_pinned_association_json(
                    "https://assoc.example.test/assoc/read-timeout",
                    params={"key": "assoc-secret-999"},
                    provider="google_books",
                    request_key="read-timeout",
                    source_label="Association Test",
                    timeout_seconds=5.0,
                    max_bytes=65536,
                )
            )
        assert assoc_read_timeout.closed is True

    for err in (assoc_status_exc.value, assoc_connect_exc.value, assoc_read_exc.value):
        assert PINNED_IP not in str(err)
        assert "assoc-secret-999" not in str(err)
        assert err.request.url.host == "assoc.example.test"
        assert "assoc-secret-999" not in str(err.request.url)
        assert "[redacted]" in str(err.request.url)


def test_google_books_key_validation_and_error_streams_enforce_byte_caps(monkeypatch) -> None:
    monkeypatch.setattr(settings, "source_max_bytes", 150_000)
    oversized_probe = CountingAsyncStream(b"g" * 50_000, 10)  # 500 KB > 150 KB
    html_error_stream = CountingAsyncStream(b"<html>502 Bad Gateway</html>", 5)

    def handler(request: httpx.Request) -> httpx.Response:
        target = (request.url.host, request.url.path)
        if target == ("www.googleapis.com", "/books/v1/volumes"):
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=oversized_probe,
                request=request,
            )
        if target == ("example.test", "/html-502"):
            return httpx.Response(
                502,
                headers={"content-type": "text/html"},
                stream=html_error_stream,
                request=request,
            )
        raise AssertionError(f"Unexpected URL: {request.url}")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            validation = await covers.validate_google_books_api_key("probe-secret-key", client=client)
            err_resp = await stream_response_async(
                client,
                "GET",
                "https://example.test/html-502",
                max_bytes=150_000,
                allowed_content_types=JSON_CONTENT_TYPES,
                allow_missing_content_type=True,
                raise_for_status=False,
            )
            return validation, err_resp

    validation, err_resp = asyncio.run(run())
    assert validation == {
        "status": "deferred",
        "warning": "Google Books returned an unexpected verification response; key saved.",
    }
    assert oversized_probe.chunks_yielded == 4
    assert oversized_probe.closed is True
    assert err_resp.status_code == 502
    assert err_resp.content == b""
    assert html_error_stream.chunks_yielded == 0
    assert html_error_stream.closed is True


