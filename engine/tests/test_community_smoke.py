import importlib.util
from pathlib import Path
import sys
import tempfile
import time

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "community_smoke.py"
SPEC = importlib.util.spec_from_file_location("community_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


IDLE_SERVICE_CODE = """
import os
import signal
import sys
import time

def _stop(signum, frame):
    print(f"received signal {signum}, pythonunbuffered={os.environ.get('PYTHONUNBUFFERED')}", flush=True)
    sys.exit(0)

signal.signal(signal.SIGTERM, _stop)
print(f"idle service booted pythonunbuffered={os.environ.get('PYTHONUNBUFFERED')}")
while True:
    time.sleep(0.05)
"""


MOCK_WEB_SERVER_CODE = """
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import signal
import sys
from urllib.parse import parse_qs

MODE = sys.argv[1]
PORT = int(os.environ["PORT"])
PROFILE_ID = "profile-smoke-1"
SESSION_COOKIE = "bookward_session=valid-session"


class SmokeHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("web access: " + (fmt % args) + "\\n")
        sys.stderr.flush()

    def _has_session(self):
        cookie = self.headers.get("Cookie", "")
        return SESSION_COOKIE in cookie

    def _send(self, status, body="", headers=None):
        payload = body.encode("utf-8")
        extra_headers = dict(headers or {})
        content_type = extra_headers.pop("Content-Type", "text/html; charset=utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in extra_headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/login":
            self._send(200, "<html><body>login</body></html>")
            return
        if MODE == "fail_root_500" and self.path == "/":
            print("Traceback: simulated SSR 500 on GET /", file=sys.stderr)
            self._send(500, "Internal Server Error")
            return
        if self.path == "/api/v1/recommendations":
            self._send(401, "Unauthorized")
            return
        if self.path in {"/", "/?view=sources", "/?view=settings", "/account", "/admin"}:
            if not self._has_session():
                self._send(303, "", {"Location": "/login"})
                return
            html = (
                "<html><body>"
                f'<form method="post"><input name="__profile_id" value="{PROFILE_ID}"></form>'
                "</body></html>"
            )
            self._send(200, html, {"Cache-Control": "private, no-store"})
            return
        self._send(404, "Not Found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw_bytes = self.rfile.read(length)
        if self.path == "/?/importGoodreadsCsv":
            import json
            if not self._has_session():
                self._send(303, "", {"Location": "/login"})
                return
            limit_msg = "Goodreads CSV exports must be 10 MB or smaller (up to 20,000 books)."
            if length > 12_000_000:
                if self.headers.get("x-sveltekit-action") == "true":
                    self._send(
                        413,
                        json.dumps({"detail": limit_msg, "message": limit_msg}),
                        {"Content-Type": "application/json"},
                    )
                else:
                    self._send(413, f"<html><body>{limit_msg}</body></html>")
                return
            if PROFILE_ID.encode("utf-8") not in raw_bytes:
                self._send(409, "Stale profile")
                return
            if length > 10_000_000 or b"oversized-10m.csv" in raw_bytes:
                self._send(
                    413,
                    json.dumps({"type": "failure", "status": 413, "data": limit_msg}),
                    {"Content-Type": "application/json"},
                )
                return
            if b"bad.csv" in raw_bytes:
                self._send(
                    400,
                    json.dumps({"type": "failure", "status": 400}),
                    {"Content-Type": "application/json"},
                )
                return
            self._send(
                200,
                json.dumps({"type": "success", "status": 200}),
                {"Content-Type": "application/json"},
            )
            return
        raw = raw_bytes.decode("utf-8")
        data = parse_qs(raw)
        if self.path in {"/login?/setup", "/login?/login"}:
            self._send(
                303,
                "",
                {
                    "Location": "/",
                    "Set-Cookie": f"{SESSION_COOKIE}; Path=/; HttpOnly",
                },
            )
            return
        if self.path == "/auth/logout":
            submitted = data.get("__profile_id", [""])[0]
            if submitted != PROFILE_ID:
                self._send(409, "Stale profile")
                return
            self._send(
                303,
                "",
                {
                    "Location": "/login",
                    "Set-Cookie": "bookward_session=; Path=/; Max-Age=0",
                },
            )
            return
        self._send(404, "Not Found")


server = HTTPServer(("127.0.0.1", PORT), SmokeHandler)


def _shutdown(signum, frame):
    print("web server shutting down cleanly")
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown)
print(f"mock web listening on {PORT} mode={MODE}")
server.serve_forever()
"""


def _smoke_temp_dirs() -> set[Path]:
    tmp_root = Path(tempfile.gettempdir())
    return set(tmp_root.glob("bookward-smoke-*"))


def test_startup_crash_reports_exited_service_and_dumps_logs(capsys, tmp_path):
    before_dirs = _smoke_temp_dirs()
    engine_ready = tmp_path / "engine.ready"
    engine_code = (
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))\n"
        "print(f'engine ready pythonunbuffered={os.environ.get(\"PYTHONUNBUFFERED\")}')\n"
        f"pathlib.Path({str(engine_ready)!r}).write_text('ok')\n"
        "while True:\n"
        "    time.sleep(0.05)\n"
    )
    web_crash_code = (
        "import pathlib, sys, time\n"
        f"ready = pathlib.Path({str(engine_ready)!r})\n"
        "deadline = time.monotonic() + 5\n"
        "while not ready.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "print('web boot step 1')\n"
        "sys.stderr.write('Error: Cannot find module build/index.js\\n')\n"
        "sys.exit(3)\n"
    )

    with pytest.raises(RuntimeError, match=r"web.*3") as exc_info:
        smoke.main(
            engine_cmd=[sys.executable, "-c", engine_code],
            web_cmd=[sys.executable, "-c", web_crash_code],
        )

    assert "before startup" in str(exc_info.value).lower()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "=== smoke log: engine (pre-stop: running, returncode: 0) ===" in captured.err
    assert "engine ready pythonunbuffered=1" in captured.err
    assert "=== smoke log: web (pre-stop: exited (3), returncode: 3) ===" in captured.err
    assert "web boot step 1" in captured.err
    assert "Error: Cannot find module build/index.js" in captured.err
    assert _smoke_temp_dirs() == before_dirs


def test_second_popen_failure_stops_first_service_and_dumps_its_log(capsys, tmp_path):
    before_dirs = _smoke_temp_dirs()
    engine_ready = tmp_path / "engine-first.ready"
    engine_code = (
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))\n"
        "print(f'first service alive pythonunbuffered={os.environ.get(\"PYTHONUNBUFFERED\")}')\n"
        f"pathlib.Path({str(engine_ready)!r}).write_text('ok')\n"
        "while True:\n"
        "    time.sleep(0.05)\n"
    )

    def missing_web_cmd(_engine_port, _web_port):
        deadline = time.monotonic() + 5
        while not engine_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return ["/definitely-missing-bookward-smoke-binary"]

    with pytest.raises(FileNotFoundError):
        smoke.main(
            engine_cmd=[sys.executable, "-c", engine_code],
            web_cmd=missing_web_cmd,
        )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "=== smoke log: engine (pre-stop: running, returncode: 0) ===" in captured.err
    assert "first service alive pythonunbuffered=1" in captured.err
    assert "=== smoke log: web" not in captured.err
    assert _smoke_temp_dirs() == before_dirs


def test_readiness_timeout_stops_both_services_and_prints_diagnostics(capsys):
    before_dirs = _smoke_temp_dirs()
    web_hang_code = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))\n"
        "print('web bound wrong port or still starting')\n"
        "while True:\n"
        "    time.sleep(0.05)\n"
    )

    with pytest.raises(RuntimeError, match="Smoke-test services did not become ready"):
        smoke.main(
            engine_cmd=[sys.executable, "-c", IDLE_SERVICE_CODE],
            web_cmd=[sys.executable, "-c", web_hang_code],
            ready_attempts=3,
            ready_interval=0.02,
        )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("pre-stop: running") == 2
    assert "returncode: 0" in captured.err
    assert "idle service booted pythonunbuffered=1" in captured.err
    assert "web bound wrong port or still starting" in captured.err
    assert _smoke_temp_dirs() == before_dirs


def test_http_assertion_failure_flushes_and_dumps_running_service_logs(capsys):
    before_dirs = _smoke_temp_dirs()

    with pytest.raises(AssertionError):
        smoke.main(
            engine_cmd=[sys.executable, "-c", IDLE_SERVICE_CODE],
            web_cmd=[sys.executable, "-c", MOCK_WEB_SERVER_CODE, "fail_root_500"],
        )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback: simulated SSR 500 on GET /" in captured.err
    assert "web server shutting down cleanly" in captured.err
    assert "idle service booted pythonunbuffered=1" in captured.err
    assert captured.err.count("pre-stop: running") == 2
    assert _smoke_temp_dirs() == before_dirs


def test_log_output_truncates_to_last_200_lines_and_replaces_invalid_utf8(capsys):
    spam_and_crash_code = (
        "import sys\n"
        "sys.stdout.buffer.write(b'bad-bytes:\\xff\\xfe\\n')\n"
        "for i in range(1, 251):\n"
        "    print(f'spam-line-{i:03d}')\n"
        "sys.stdout.flush()\n"
        "sys.exit(2)\n"
    )

    with pytest.raises(RuntimeError, match=r"web.*2"):
        smoke.main(
            engine_cmd=[sys.executable, "-c", IDLE_SERVICE_CODE],
            web_cmd=[sys.executable, "-c", spam_and_crash_code],
        )

    captured = capsys.readouterr()
    assert "spam-line-050" not in captured.err
    assert "bad-bytes:" not in captured.err
    assert "spam-line-051" in captured.err
    assert "spam-line-250" in captured.err
    spam_lines = [
        line for line in captured.err.splitlines() if line.startswith("spam-line-")
    ]
    assert len(spam_lines) == 200

    # Also verify invalid UTF-8 within the last 200 lines is decoded with replacement.
    bad_utf8_code = (
        "import sys\n"
        "sys.stderr.buffer.write(b'invalid-utf8-tail:\\xff\\xfe-end\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(4)\n"
    )
    with pytest.raises(RuntimeError, match=r"engine.*4"):
        smoke.main(
            engine_cmd=[sys.executable, "-c", bad_utf8_code],
            web_cmd=[sys.executable, "-c", IDLE_SERVICE_CODE],
        )

    captured_utf8 = capsys.readouterr()
    assert "invalid-utf8-tail:\ufffd\ufffd-end" in captured_utf8.err


def test_passing_run_keeps_stderr_silent_and_cleans_up_tempdir(capsys):
    before_dirs = _smoke_temp_dirs()

    smoke.main(
        engine_cmd=[sys.executable, "-c", IDLE_SERVICE_CODE],
        web_cmd=[sys.executable, "-c", MOCK_WEB_SERVER_CODE, "pass"],
    )

    captured = capsys.readouterr()
    assert captured.out.strip() == (
        "PASS: setup, sign-in/out, authenticated views, SSR form bindings, "
        "stale-profile guard, CSV uploads, API authentication"
    )
    assert captured.err == ""
    assert _smoke_temp_dirs() == before_dirs
