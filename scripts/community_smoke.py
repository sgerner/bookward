"""Smoke-test the built web app and engine against disposable data.

Run after npm run build:
    uv run --project engine python scripts/community_smoke.py
"""

from dataclasses import dataclass
from html.parser import HTMLParser
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx

MAX_LOG_LINES = 200


@dataclass
class _ServiceRun:
    name: str
    process: subprocess.Popen
    log_path: Path
    pre_stop: int | None = None
    returncode: int | None = None


class FormBindings(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.current = None

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "form":
            self.current = {"method": attributes.get("method", "get").lower(), "profile": ""}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None and attributes.get("name") == "__profile_id":
            self.current["profile"] = attributes.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _resolve_cmd(cmd, default, engine_port, web_port):
    if cmd is None:
        return default
    if callable(cmd):
        return list(cmd(engine_port, web_port))
    return list(cmd)


def _dump_service_log(service: _ServiceRun) -> None:
    pre_stop = (
        "running"
        if service.pre_stop is None
        else f"exited ({service.pre_stop})"
    )
    header = (
        f"=== smoke log: {service.name} "
        f"(pre-stop: {pre_stop}, returncode: {service.returncode}) ==="
    )
    try:
        text = service.log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        body = f"(unable to read log: {exc})"
    else:
        lines = text.splitlines()
        if not lines:
            body = "(no log output)"
        elif len(lines) > MAX_LOG_LINES:
            omitted = len(lines) - MAX_LOG_LINES
            body = "\n".join(
                [
                    f"... ({omitted} earlier lines omitted; showing last {MAX_LOG_LINES} lines)",
                    *lines[-MAX_LOG_LINES:],
                ]
            )
        else:
            body = "\n".join(lines)
    footer = f"=== end smoke log: {service.name} ==="
    print(f"{header}\n{body}\n{footer}", file=sys.stderr, flush=True)


def main(
    *,
    engine_cmd=None,
    web_cmd=None,
    ready_attempts: int = 100,
    ready_interval: float = 0.1,
):
    root = Path(__file__).resolve().parents[1]
    engine_port, web_port = free_port(), free_port()
    while web_port == engine_port:
        web_port = free_port()
    origin = f"http://127.0.0.1:{web_port}"
    with tempfile.TemporaryDirectory(prefix="bookward-smoke-") as directory:
        env = dict(os.environ, AFTERWORD_DB=f"{directory}/engine.db",
                   AFTERWORD_BACKUP_DIR=f"{directory}/backups", AFTERWORD_BACKUP_INTERVAL_HOURS="0",
                   AFTERWORD_AUTH_PASSWORD="", AFTERWORD_SERVICE_SECRET="isolated-smoke-service",
                   AFTERWORD_SOURCE_SYNC_INTERVAL_HOURS="0", AFTERWORD_EMBEDDING_BACKEND="local",
                   AFTERWORD_EMBEDDING_MODEL="hashing-768", AFTERWORD_EMBEDDING_API_KEY="",
                   AFTERWORD_SECRET_KEY="", AFTERWORD_OIDC_ISSUER="", AFTERWORD_OIDC_CLIENT_ID="",
                   AFTERWORD_PUBLIC_URL=origin, ENGINE_SERVICE_SECRET="isolated-smoke-service",
                   ENGINE_URL=f"http://127.0.0.1:{engine_port}", ORIGIN=origin,
                   HOST="127.0.0.1", PORT=str(web_port), OIDC_AUTO_LOGIN="false",
                   BODY_SIZE_LIMIT="12M", PROTOCOL_HEADER="x-forwarded-proto", PYTHONUNBUFFERED="1")
        default_engine_cmd = [
            str(root / "engine/.venv/bin/python"), "-m", "uvicorn",
            "afterword_engine.main:app", "--app-dir", "engine", "--host", "127.0.0.1",
            "--port", str(engine_port),
        ]
        service_specs = (
            ("engine", engine_cmd, default_engine_cmd),
            ("web", web_cmd, ["node", "build"]),
        )
        services: list[_ServiceRun] = []
        open_files = []
        failed = False
        try:
            for name, custom_cmd, default_cmd in service_specs:
                cmd = _resolve_cmd(custom_cmd, default_cmd, engine_port, web_port)
                log_path = Path(directory) / f"{name}.log"
                log_file = log_path.open("wb")
                open_files.append(log_file)
                process = subprocess.Popen(
                    cmd, cwd=root, env=env, stdout=log_file, stderr=log_file
                )
                services.append(_ServiceRun(name=name, process=process, log_path=log_path))

            with httpx.Client(base_url=origin, timeout=15, headers={"accept": "text/html", "x-forwarded-proto": "http"}) as client:
                for _ in range(ready_attempts):
                    exited = [
                        f"{service.name} (exit code {service.process.returncode})"
                        for service in services
                        if service.process.poll() is not None
                    ]
                    if exited:
                        raise RuntimeError(
                            f"Smoke-test service exited before startup: {', '.join(exited)}"
                        )
                    try:
                        if client.get("/login").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(ready_interval)
                else:
                    raise RuntimeError("Smoke-test services did not become ready")
                assert client.get("/").status_code == 303
                response = client.post("/login?/setup", data={"username": "smoke-reader",
                    "password": "Disposable-smoke-password-17"}, headers={"origin": origin})
                assert response.status_code == 303, (response.status_code, response.text[:200])
                assert "bookward_session" in client.cookies
                profile_id = None
                for path in ["/", "/?view=sources", "/?view=settings", "/account", "/admin"]:
                    response = client.get(path)
                    assert response.status_code == 200, (path, response.status_code)
                    assert response.headers["cache-control"] == "private, no-store"
                    forms = FormBindings()
                    forms.feed(response.text)
                    for form in forms.forms:
                        if form["method"] == "post":
                            assert form["profile"], (path, "POST form missing server-rendered profile binding")
                            profile_id = profile_id or form["profile"]
                            assert form["profile"] == profile_id, (path, "inconsistent profile binding")
                assert client.post("/auth/logout", data={"__profile_id": "wrong"},
                    headers={"origin": origin}).status_code == 409
                assert client.get("/api/v1/recommendations").status_code == 401
                assert profile_id
                assert client.post("/auth/logout", data={"__profile_id": profile_id},
                    headers={"origin": origin}).status_code == 303
                assert client.get("/").status_code == 303
                response = client.post("/login?/login", data={"username": "smoke-reader",
                    "password": "Disposable-smoke-password-17"}, headers={"origin": origin})
                assert response.status_code == 303
                assert client.get("/").status_code == 200
                action_headers = {
                    "origin": origin,
                    "accept": "application/json",
                    "x-sveltekit-action": "true",
                }
                review_padding = "r" * 10_500
                csv_rows = [
                    "Book Id,Title,Author,Exclusive Shelf,My Rating,Date Read,My Review"
                ] + [
                    f'{idx},"Smoke Book {idx}",Smoke Author,read,5,2026/01/02,"{review_padding}"'
                    for idx in range(1, 61)
                ]
                csv_600kb = ("\n".join(csv_rows) + "\n").encode("utf-8")
                assert len(csv_600kb) > 600_000
                csv_ok = client.post(
                    "/?/importGoodreadsCsv",
                    data={"__profile_id": profile_id},
                    files={"file": ("goodreads_library_export.csv", csv_600kb, "text/csv")},
                    headers=action_headers,
                )
                assert csv_ok.status_code == 200, (csv_ok.status_code, csv_ok.text[:200])
                assert csv_ok.json()["type"] == "success"

                bad_csv = client.post(
                    "/?/importGoodreadsCsv",
                    data={"__profile_id": profile_id},
                    files={"file": ("bad.csv", b"not,goodreads\n1,2\n", "text/csv")},
                    headers=action_headers,
                )
                assert bad_csv.status_code == 400, (bad_csv.status_code, bad_csv.text[:200])
                assert bad_csv.json()["type"] == "failure"

                over_10mb_csv = b"Title,Author\n" + (b"x" * 10_050_000)
                over_10mb = client.post(
                    "/?/importGoodreadsCsv",
                    data={"__profile_id": profile_id},
                    files={"file": ("oversized-10m.csv", over_10mb_csv, "text/csv")},
                    headers=action_headers,
                )
                assert over_10mb.status_code == 413, (over_10mb.status_code, over_10mb.text[:200])
                assert over_10mb.json()["type"] == "failure"
                assert "10 MB" in over_10mb.text

                over_body_limit = b"Title,Author\n" + (b"x" * 13_000_000)
                over_body_json = client.post(
                    "/?/importGoodreadsCsv",
                    data={"__profile_id": profile_id},
                    files={"file": ("oversized-13m.csv", over_body_limit, "text/csv")},
                    headers=action_headers,
                )
                assert over_body_json.status_code == 413, (over_body_json.status_code, over_body_json.text[:200])
                over_body_payload = over_body_json.json()
                assert "10 MB" in over_body_payload["detail"]
                assert over_body_payload["message"] == over_body_payload["detail"]

                over_body_html = client.post(
                    "/?/importGoodreadsCsv",
                    data={"__profile_id": profile_id},
                    files={"file": ("oversized-13m.csv", over_body_limit, "text/csv")},
                    headers={"origin": origin},
                )
                assert over_body_html.status_code == 413, (over_body_html.status_code, over_body_html.text[:200])
                assert "text/html" in over_body_html.headers.get("content-type", "")
                assert "10 MB" in over_body_html.text

                print("PASS: setup, sign-in/out, authenticated views, SSR form bindings, stale-profile guard, CSV uploads, API authentication")
        except BaseException:
            failed = True
            raise
        finally:
            try:
                for service in services:
                    service.pre_stop = service.process.poll()
                for service in services:
                    if service.pre_stop is None:
                        try:
                            service.process.terminate()
                        except ProcessLookupError:
                            pass
                for service in services:
                    try:
                        service.returncode = service.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        service.process.kill()
                        service.returncode = service.process.wait()
            finally:
                for log_file in open_files:
                    log_file.close()
            if failed:
                for service in services:
                    _dump_service_log(service)


if __name__ == "__main__":
    main()
