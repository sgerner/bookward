"""Smoke-test the built web app and engine against disposable data.

Run after npm run build:
    uv run --project engine python scripts/community_smoke.py
"""

import os
from html.parser import HTMLParser
from pathlib import Path
import socket
import subprocess
import tempfile
import time

import httpx


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


def main():
    root = Path(__file__).resolve().parents[1]
    engine_port, web_port = free_port(), free_port()
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
                   HOST="127.0.0.1", PORT=str(web_port), OIDC_AUTO_LOGIN="false")
        processes = []
        with tempfile.TemporaryFile(mode="w+") as logs:
            try:
                processes.append(subprocess.Popen([
                    str(root / "engine/.venv/bin/python"), "-m", "uvicorn",
                    "afterword_engine.main:app", "--app-dir", "engine", "--host", "127.0.0.1",
                    "--port", str(engine_port),
                ], cwd=root, env=env, stdout=logs, stderr=logs))
                processes.append(subprocess.Popen(["node", "build"], cwd=root, env=env, stdout=logs, stderr=logs))
                with httpx.Client(base_url=origin, timeout=15, headers={"accept": "text/html"}) as client:
                    for _ in range(100):
                        if any(process.poll() is not None for process in processes):
                            raise RuntimeError("A smoke-test service exited before startup")
                        try:
                            if client.get("/login").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(.1)
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
                    print("PASS: setup, sign-in/out, authenticated views, SSR form bindings, stale-profile guard, API authentication")
            finally:
                for process in processes:
                    process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    main()
