"""Reproducible Playwright browser audit for WCAG 1.4.10 reflow and top-layer overlays.

Spins up an isolated Bookward engine + built SvelteKit server + local mock Librarr
server against temporary SQLite storage, then verifies in headless Chromium:

1. 320x256 (400% zoom reflow) across Settings (default & expanded subsections),
   Discover, Sources, Decisions, and Shortlist: zero horizontal overflow and zero
   viewport-clipped interactive controls.
2. Top-layer <dialog> modals (Mark Read & Librarr search via ModalDialog):
   scrollTop == 0 on open, visible initial focus, single-container scroll at 320x256,
   sticky-header inner scroll at 1280x800, and non-intercepted Escape on <select>.
3. Top-layer PopoverDialog (#profile-menu-panel + nested compact ThemePicker):
   :popover-open stacking above sticky glassmorphic shell, all 24 theme radio options
   reachable, single-owner focus restoration on first Escape, and profile trigger
   focus restoration on second Escape.
4. Zero-dependency fallback path when showPopover / showModal are unavailable:
   document.body portal (z-50 / z-[55]) for PopoverDialog topLayer and fixed z-[60]
   backdrop fallback for ModalDialog.

Run after `npm run build`:
    python3 scripts/reflow_overlay_audit.py
    python3 scripts/reflow_overlay_audit.py --screenshots-dir /tmp
"""

from __future__ import annotations

import argparse
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, HTTPServer
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from playwright.sync_api import Error as PlaywrightError, sync_playwright


class FormBindings(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.profile_id: str | None = None

    def handle_starttag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        attrs = dict(attributes)
        if tag == "input" and attrs.get("name") == "__profile_id" and attrs.get("value"):
            self.profile_id = attrs["value"]


class MockLibrarrHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        payload = {
            "results": [
                {
                    "id": "lib-1",
                    "title": "Orbital",
                    "author": "Samantha Harvey",
                    "year": 2023,
                    "media_type": "ebook",
                    "formats": ["EPUB"],
                    "provider": "OpenLibrary",
                },
                {
                    "id": "lib-2",
                    "title": "The Ministry of Time",
                    "author": "Kaliane Bradley",
                    "year": 2024,
                    "media_type": "ebook",
                    "formats": ["EPUB", "MOBI"],
                    "provider": "Library",
                },
            ]
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _launch_chromium(playwright):
    launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
    try:
        return playwright.chromium.launch(headless=True, args=launch_args)
    except PlaywrightError:
        pass
    for candidate in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(candidate)
        if found:
            try:
                return playwright.chromium.launch(
                    headless=True, executable_path=found, args=launch_args
                )
            except PlaywrightError:
                continue
    return playwright.chromium.launch(headless=True, args=launch_args)


AUDIT_JS = """
() => {
  const vw = window.innerWidth;
  const selectors = [
    'button',
    'input:not([type="hidden"])',
    'select',
    'textarea',
    'a[href]',
    'summary',
    '[role="switch"]',
    'code'
  ];
  const clipped = [];
  for (const el of document.querySelectorAll(selectors.join(','))) {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    if (el.closest('[hidden]')) continue;
    if (el.closest('dialog:not([open])')) continue;
    if (el.closest('[popover]:not(:popover-open)')) continue;
    if (
      el.classList.contains('sr-only') ||
      (el.classList.contains('peer') && style.position === 'absolute' && el.offsetWidth <= 2)
    ) {
      continue;
    }
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    if (rect.left < -0.5 || rect.right > vw + 0.5) {
      clipped.push({
        tag: el.tagName.toLowerCase(),
        id: el.id || null,
        name: el.getAttribute('name') || null,
        text: (el.textContent || '').trim().slice(0, 60),
        className: (el.className || '').toString().slice(0, 80),
        left: Math.round(rect.left * 10) / 10,
        right: Math.round(rect.right * 10) / 10,
        width: Math.round(rect.width * 10) / 10
      });
    }
  }
  const dataViews = Array.from(document.querySelectorAll('[data-view]')).map((el) => {
    const r = el.getBoundingClientRect();
    return {
      view: el.getAttribute('data-view'),
      left: Math.round(r.left * 10) / 10,
      right: Math.round(r.right * 10) / 10,
      width: Math.round(r.width * 10) / 10
    };
  });
  return {
    innerWidth: vw,
    scrollWidth: document.documentElement.scrollWidth,
    dataViews,
    clippedCount: clipped.length,
    clipped
  };
}
"""


def _maybe_screenshot(page, screenshots_dir: Path | None, filename: str) -> None:
    if screenshots_dir is not None:
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshots_dir / filename))


def main(argv: list[str] | None = None) -> None:
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument(
        "--screenshots-dir",
        type=Path,
        default=None,
        help="Optional directory to write verification PNG screenshots.",
    )
    args = arg_parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    engine_python = root / "engine/.venv/bin/python"
    engine_cmd_prefix = (
        [str(engine_python)]
        if engine_python.exists()
        else ["uv", "run", "--project", "engine", "python"]
    )

    ports: set[int] = set()
    while len(ports) < 3:
        ports.add(free_port())
    engine_port, web_port, librarr_port = tuple(ports)
    origin = f"http://127.0.0.1:{web_port}"

    librarr_server = HTTPServer(("127.0.0.1", librarr_port), MockLibrarrHandler)
    librarr_thread = threading.Thread(target=librarr_server.serve_forever, daemon=True)
    librarr_thread.start()

    with tempfile.TemporaryDirectory(prefix="bookward-reflow-") as directory:
        env = dict(
            os.environ,
            AFTERWORD_DB=f"{directory}/engine.db",
            AFTERWORD_BACKUP_DIR=f"{directory}/backups",
            AFTERWORD_BACKUP_INTERVAL_HOURS="24",
            AFTERWORD_AUTH_PASSWORD="",
            AFTERWORD_ENABLE_BACKGROUND_LOOPS="false",
            AFTERWORD_SERVICE_SECRET="isolated-reflow-secret",
            AFTERWORD_SOURCE_SYNC_INTERVAL_HOURS="0",
            AFTERWORD_EMBEDDING_BACKEND="local",
            AFTERWORD_EMBEDDING_MODEL="hashing-768",
            AFTERWORD_EMBEDDING_API_KEY="",
            AFTERWORD_SECRET_KEY="",
            AFTERWORD_OIDC_ISSUER="",
            AFTERWORD_OIDC_CLIENT_ID="",
            AFTERWORD_PUBLIC_URL=origin,
            AFTERWORD_LIBRARR_ALLOWED_HOSTS="127.0.0.1,localhost,librarr",
            ENGINE_SERVICE_SECRET="isolated-reflow-secret",
            ENGINE_URL=f"http://127.0.0.1:{engine_port}",
            ORIGIN=origin,
            HOST="127.0.0.1",
            PORT=str(web_port),
            OIDC_AUTO_LOGIN="false",
            PROTOCOL_HEADER="x-forwarded-proto",
            PYTHONUNBUFFERED="1",
        )
        processes: list[subprocess.Popen] = []
        with tempfile.TemporaryFile(mode="w+") as logs:
            try:
                processes.append(
                    subprocess.Popen(
                        [
                            *engine_cmd_prefix,
                            "-m",
                            "uvicorn",
                            "afterword_engine.main:app",
                            "--app-dir",
                            "engine",
                            "--host",
                            "127.0.0.1",
                            "--port",
                            str(engine_port),
                        ],
                        cwd=root,
                        env=env,
                        stdout=logs,
                        stderr=logs,
                    )
                )
                processes.append(
                    subprocess.Popen(
                        ["node", "build"],
                        cwd=root,
                        env=env,
                        stdout=logs,
                        stderr=logs,
                    )
                )

                cj = CookieJar()
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
                for _ in range(120):
                    if any(p.poll() is not None for p in processes):
                        logs.seek(0)
                        raise RuntimeError(f"Service exited early:\n{logs.read()}")
                    try:
                        req = urllib.request.Request(
                            f"{origin}/login",
                            headers={"accept": "text/html", "x-forwarded-proto": "http"},
                        )
                        with opener.open(req, timeout=5) as resp:
                            if resp.status == 200:
                                break
                    except Exception:
                        pass
                    time.sleep(0.1)
                else:
                    logs.seek(0)
                    raise RuntimeError(f"Services did not become ready:\n{logs.read()}")

                setup_data = urllib.parse.urlencode(
                    {
                        "username": "reflow-reader",
                        "password": "Disposable-reflow-password-17",
                    }
                ).encode("utf-8")
                setup_req = urllib.request.Request(
                    f"{origin}/login?/setup",
                    data=setup_data,
                    headers={
                        "accept": "text/html",
                        "content-type": "application/x-www-form-urlencoded",
                        "origin": origin,
                        "x-forwarded-proto": "http",
                    },
                    method="POST",
                )
                with opener.open(setup_req, timeout=15) as resp:
                    home_html = resp.read().decode("utf-8")

                session_cookie = next(
                    (c.value for c in cj if c.name == "bookward_session"), None
                )
                assert session_cookie, "Missing bookward_session cookie"
                parser = FormBindings()
                parser.feed(home_html)
                assert parser.profile_id, "Missing __profile_id hidden input"

                def eng_req(method: str, path: str, payload: dict | None = None) -> dict:
                    data = None if payload is None else json.dumps(payload).encode("utf-8")
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{engine_port}{path}",
                        data=data,
                        headers={
                            "content-type": "application/json",
                            "x-bookward-service": "isolated-reflow-secret",
                            "x-bookward-session": session_cookie,
                        },
                        method=method,
                    )
                    with urllib.request.urlopen(req, timeout=15) as r:
                        body = r.read().decode("utf-8")
                        return json.loads(body) if body else {}

                ov = eng_req("GET", "/api/overview?recommendation_limit=24&decision_limit=24")
                recs = ov["recommendations"]
                assert len(recs) >= 2, f"Expected seeded recommendations, got {len(recs)}"
                eng_req("POST", f"/api/recommendations/{recs[0]['id']}/feedback", {"action": "save"})
                eng_req("POST", f"/api/recommendations/{recs[1]['id']}/feedback", {"action": "save"})
                if len(recs) >= 3:
                    eng_req("POST", f"/api/recommendations/{recs[2]['id']}/read", {"rating": 5})
                eng_req(
                    "PUT",
                    "/api/settings",
                    {
                        "embedding_backend": "local",
                        "embedding_model": "hashing-768",
                        "embedding_url": "http://ollama:11434",
                        "embedding_api_key": "",
                        "librarr_url": f"http://127.0.0.1:{librarr_port}",
                        "librarr_api_key": "test-librarr-key",
                        "librarr_media_type": "ebook",
                    },
                )
                eng_req("POST", "/api/settings/api-tokens", {"name": "Reflow Audit Token"})

                auth_cookie = [
                    {
                        "name": "bookward_session",
                        "value": session_cookie,
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                ]

                results: dict[str, object] = {}
                with sync_playwright() as p:
                    browser = _launch_chromium(p)

                    # --- Context 1: 320x256 WCAG 1.4.10 Reflow + Native Top-Layer ---
                    context = browser.new_context(viewport={"width": 320, "height": 256})
                    context.add_cookies(auth_cookie)
                    page = context.new_page()

                    # 1. Settings view (default + expanded conditional subsections)
                    page.goto(f"{origin}/?view=settings", wait_until="domcontentloaded")
                    page.wait_for_timeout(250)
                    backup_btn = page.locator('button:has-text("Create backup")')
                    if backup_btn.count() > 0:
                        backup_btn.first.click()
                        page.wait_for_timeout(400)
                    csv_rect = page.locator('input[type="file"][name="file"]').bounding_box()
                    digest_switch_rect = page.locator("#digest-enabled").bounding_box()
                    settings_default_audit = page.evaluate(AUDIT_JS)

                    page.locator("#digest-enabled").click()
                    page.wait_for_timeout(100)
                    for toggle_selector in ("#digest-discord", "#digest-email"):
                        sw = page.locator(toggle_selector)
                        if sw.count() > 0:
                            sw.click()
                    page.wait_for_timeout(150)
                    settings_expanded_audit = page.evaluate(AUDIT_JS)
                    page.locator('input[type="file"][name="file"]').scroll_into_view_if_needed()
                    _maybe_screenshot(page, args.screenshots_dir, "settings-320x256.png")

                    results["settings_default"] = settings_default_audit
                    results["settings_expanded"] = settings_expanded_audit
                    results["csv_input_rect"] = csv_rect
                    results["digest_switch_rect"] = digest_switch_rect

                    # 2. Discover, Sources, Decisions, and Shortlist views at 320x256
                    for extra_view in ("discover", "sources", "decisions"):
                        page.goto(f"{origin}/?view={extra_view}", wait_until="domcontentloaded")
                        page.wait_for_timeout(200)
                        results[f"view_{extra_view}"] = page.evaluate(AUDIT_JS)

                    page.goto(f"{origin}/?view=saved", wait_until="domcontentloaded")
                    page.wait_for_timeout(250)
                    results["saved"] = page.evaluate(AUDIT_JS)
                    _maybe_screenshot(page, args.screenshots_dir, "shortlist-320x256.png")

                    # 3. Mark Read modal at 320x256
                    mark_read_trigger = page.locator('button:has-text("Mark Read")').first
                    mark_read_trigger.scroll_into_view_if_needed()
                    mark_read_trigger.click()
                    page.wait_for_selector('dialog[open][aria-labelledby="mark-read-title"]')
                    page.wait_for_timeout(100)
                    mark_read_metrics = page.evaluate(
                        """() => {
                          const dlg = document.querySelector('dialog[open][aria-labelledby="mark-read-title"]');
                          const rect = dlg.getBoundingClientRect();
                          const style = getComputedStyle(dlg);
                          const active = document.activeElement;
                          const activeRect = active ? active.getBoundingClientRect() : null;
                          return {
                            scrollTop: dlg.scrollTop,
                            overflowY: style.overflowY,
                            dialogTop: Math.round(rect.top * 10) / 10,
                            dialogBottom: Math.round(rect.bottom * 10) / 10,
                            activeTag: active ? active.tagName.toLowerCase() : null,
                            activeAriaLabel: active ? active.getAttribute('aria-label') : null,
                            activeVisibleInViewport: activeRect
                              ? activeRect.top >= 0 &&
                                activeRect.bottom <= window.innerHeight &&
                                activeRect.left >= 0 &&
                                activeRect.right <= window.innerWidth
                              : false
                          };
                        }"""
                    )
                    _maybe_screenshot(page, args.screenshots_dir, "mark-read-320x256.png")
                    mark_read_metrics["submitReachableOnScroll"] = page.evaluate(
                        """() => {
                          const dlg = document.querySelector('dialog[open][aria-labelledby="mark-read-title"]');
                          dlg.scrollTop = dlg.scrollHeight;
                          const r = dlg.querySelector('button[type="submit"]').getBoundingClientRect();
                          return r.top >= 0 && r.bottom <= window.innerHeight;
                        }"""
                    )
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(100)
                    mark_read_metrics["closedOnEscape"] = (
                        page.locator('dialog[open][aria-labelledby="mark-read-title"]').count() == 0
                    )
                    results["mark_read"] = mark_read_metrics

                    # 4. Librarr search modal at 320x256 + <select> Escape non-interception
                    librarr_trigger = page.locator('button:has-text("Find in Librarr")').first
                    librarr_trigger.scroll_into_view_if_needed()
                    librarr_trigger.click()
                    page.wait_for_selector('dialog[open][aria-labelledby="librarr-search-title"]')
                    page.wait_for_timeout(200)
                    librarr_metrics = page.evaluate(
                        """() => {
                          const dlg = document.querySelector('dialog[open][aria-labelledby="librarr-search-title"]');
                          const rect = dlg.getBoundingClientRect();
                          const active = document.activeElement;
                          const activeRect = active ? active.getBoundingClientRect() : null;
                          const closeRect = dlg.querySelector('button[aria-label="Close Librarr search"]').getBoundingClientRect();
                          const nestedScrollers = Array.from(dlg.querySelectorAll('*')).filter((el) => {
                            const oy = getComputedStyle(el).overflowY;
                            return oy === 'auto' || oy === 'scroll';
                          });
                          const mediaSelect = document.getElementById('librarr-media-type');
                          let selectSawEscape = false;
                          const onSelectKeydown = (event) => {
                            if (event.key === 'Escape') {
                              selectSawEscape = !event.defaultPrevented;
                              event.preventDefault();
                            }
                          };
                          mediaSelect.addEventListener('keydown', onSelectKeydown, { once: true });
                          mediaSelect.dispatchEvent(
                            new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
                          );
                          return {
                            scrollTop: dlg.scrollTop,
                            dialogOverflowY: getComputedStyle(dlg).overflowY,
                            dialogTop: Math.round(rect.top * 10) / 10,
                            closeTop: Math.round(closeRect.top * 10) / 10,
                            activeVisibleInViewport: activeRect
                              ? activeRect.top >= 0 &&
                                activeRect.bottom <= window.innerHeight &&
                                activeRect.left >= 0 &&
                                activeRect.right <= window.innerWidth
                              : false,
                            nestedScrollContainers: nestedScrollers.length,
                            selectSawUnpreventedEscape: selectSawEscape,
                            stillOpenAfterSelectEscape: dlg.hasAttribute('open')
                          };
                        }"""
                    )
                    _maybe_screenshot(page, args.screenshots_dir, "librarr-search-320x256.png")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(100)
                    librarr_metrics["closedOnDialogEscape"] = (
                        page.locator('dialog[open][aria-labelledby="librarr-search-title"]').count() == 0
                    )
                    results["librarr_search_320x256"] = librarr_metrics

                    # 5. Profile menu (#profile-menu-panel) + nested Appearance (PopoverDialog topLayer)
                    page.evaluate("window.scrollTo(0, 0)")
                    profile_btn = page.locator('button[aria-controls="profile-menu-panel"]')
                    profile_btn.click()
                    page.wait_for_timeout(150)

                    profile_metrics = page.evaluate(
                        """() => {
                          const panel = document.getElementById('profile-menu-panel');
                          const style = getComputedStyle(panel);
                          panel.scrollTop = panel.scrollHeight;
                          const signOutBtn = Array.from(panel.querySelectorAll('button')).find(
                            (b) => b.textContent.trim().includes('Sign out')
                          );
                          const sRect = signOutBtn.getBoundingClientRect();
                          const hitEl = document.elementFromPoint(
                            sRect.left + sRect.width / 2,
                            sRect.top + sRect.height / 2
                          );
                          return {
                            position: style.position,
                            popoverAttr: panel.getAttribute('popover'),
                            isPopoverOpen: panel.matches(':popover-open'),
                            signOutHit: Boolean(hitEl && (hitEl === signOutBtn || signOutBtn.contains(hitEl)))
                          };
                        }"""
                    )
                    _maybe_screenshot(page, args.screenshots_dir, "profile-menu-320x256.png")

                    appearance_panel_id = page.evaluate(
                        """() => {
                          const panel = document.getElementById('profile-menu-panel');
                          panel.scrollTop = 0;
                          const btn = panel.querySelector('button[aria-label="Choose appearance"]');
                          window.__appearanceFocusCount = 0;
                          const origFocus = btn.focus.bind(btn);
                          btn.focus = (...args) => {
                            window.__appearanceFocusCount += 1;
                            return origFocus(...args);
                          };
                          return btn.getAttribute('aria-controls');
                        }"""
                    )
                    page.locator('#profile-menu-panel button[aria-label="Choose appearance"]').click()
                    page.wait_for_selector(f"#{appearance_panel_id}:not([hidden])")
                    page.wait_for_timeout(150)

                    appearance_metrics = page.evaluate(
                        """(panelId) => {
                          const profilePanel = document.getElementById('profile-menu-panel');
                          const appPanel = document.getElementById(panelId);
                          const appRect = appPanel.getBoundingClientRect();
                          const nestedScrollers = Array.from(appPanel.querySelectorAll('*')).filter((el) => {
                            const oy = getComputedStyle(el).overflowY;
                            return oy === 'auto' || oy === 'scroll';
                          });
                          const allRadios = Array.from(appPanel.querySelectorAll('button[role="radio"]'));
                          const themeRadios = Array.from(
                            appPanel.querySelectorAll('[aria-labelledby="appearance-theme-legend"] button[role="radio"]')
                          );
                          const unreachable = [];
                          for (const btn of allRadios) {
                            btn.scrollIntoView({ block: 'nearest' });
                            const r = btn.getBoundingClientRect();
                            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                            if (!hit || !(hit === btn || btn.contains(hit))) {
                              unreachable.push(btn.textContent.trim());
                            }
                          }
                          return {
                            parentPopoverStillOpen: profilePanel.matches(':popover-open'),
                            appearancePopoverOpen: appPanel.matches(':popover-open'),
                            dialogTop: Math.round(appRect.top * 10) / 10,
                            dialogBottom: Math.round(appRect.bottom * 10) / 10,
                            nestedScrollContainers: nestedScrollers.length,
                            themeCount: themeRadios.length,
                            wintryPresent: themeRadios.some((b) => b.textContent.includes('Wintry')),
                            unreachableCount: unreachable.length,
                            unreachable
                          };
                        }""",
                        appearance_panel_id,
                    )
                    _maybe_screenshot(page, args.screenshots_dir, "appearance-wintry-320x256.png")

                    # First Escape closes Appearance, keeps #profile-menu-panel open, focuses trigger ONCE
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(120)
                    after_first_escape = page.evaluate(
                        """(panelId) => {
                          const profilePanel = document.getElementById('profile-menu-panel');
                          const appPanel = document.getElementById(panelId);
                          const appearanceBtn = profilePanel
                            ? profilePanel.querySelector('button[aria-label="Choose appearance"]')
                            : null;
                          return {
                            appearanceHidden: Boolean(appPanel && appPanel.hasAttribute('hidden')),
                            profilePopoverOpen: Boolean(profilePanel && profilePanel.matches(':popover-open')),
                            appearanceFocusCount: window.__appearanceFocusCount,
                            activeIsAppearanceBtn: document.activeElement === appearanceBtn
                          };
                        }""",
                        appearance_panel_id,
                    )

                    # Second Escape closes #profile-menu-panel and restores focus to profile menu trigger
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(120)
                    after_second_escape = page.evaluate(
                        """() => {
                          const profilePanel = document.getElementById('profile-menu-panel');
                          const profileBtn = document.querySelector('button[aria-controls="profile-menu-panel"]');
                          return {
                            profileHidden: Boolean(profilePanel && profilePanel.hasAttribute('hidden')),
                            activeIsProfileBtn: document.activeElement === profileBtn
                          };
                        }"""
                    )
                    appearance_metrics["afterFirstEscape"] = after_first_escape
                    appearance_metrics["afterSecondEscape"] = after_second_escape
                    results["profile_menu"] = profile_metrics
                    results["appearance_dialog"] = appearance_metrics
                    context.close()

                    # --- Context 2: 1280x800 Desktop Regression & Sticky-Header Librarr ---
                    desktop_ctx = browser.new_context(viewport={"width": 1280, "height": 800})
                    desktop_ctx.add_cookies(auth_cookie)
                    dpage = desktop_ctx.new_page()
                    dpage.goto(f"{origin}/", wait_until="domcontentloaded")
                    dpage.wait_for_timeout(250)
                    _maybe_screenshot(dpage, args.screenshots_dir, "discover-desktop-1280x800.png")
                    desktop_audit = dpage.evaluate(AUDIT_JS)

                    dpage.goto(f"{origin}/?view=saved", wait_until="domcontentloaded")
                    dpage.wait_for_timeout(200)
                    dpage.locator('button:has-text("Find in Librarr")').first.click()
                    dpage.wait_for_selector('dialog[open][aria-labelledby="librarr-search-title"]')
                    dpage.wait_for_timeout(150)
                    desktop_librarr = dpage.evaluate(
                        """() => {
                          const dlg = document.querySelector('dialog[open][aria-labelledby="librarr-search-title"]');
                          const bodyScroller = dlg.children[1];
                          return {
                            dialogOverflowY: getComputedStyle(dlg).overflowY,
                            bodyOverflowY: getComputedStyle(bodyScroller).overflowY
                          };
                        }"""
                    )
                    results["desktop_1280x800"] = {
                        "scrollWidth": desktop_audit["scrollWidth"],
                        "clippedCount": desktop_audit["clippedCount"],
                        "librarr": desktop_librarr,
                    }
                    desktop_ctx.close()

                    # --- Context 3: Legacy Fallback (no Popover API / no HTMLDialogElement.showModal) ---
                    fallback_ctx = browser.new_context(viewport={"width": 320, "height": 256})
                    fallback_ctx.add_init_script(
                        """
                        delete HTMLElement.prototype.showPopover;
                        delete HTMLElement.prototype.hidePopover;
                        delete HTMLElement.prototype.togglePopover;
                        delete HTMLDialogElement.prototype.showModal;
                        delete HTMLDialogElement.prototype.close;
                        """
                    )
                    fallback_ctx.add_cookies(auth_cookie)
                    fpage = fallback_ctx.new_page()
                    fpage.goto(f"{origin}/?view=saved", wait_until="domcontentloaded")
                    fpage.wait_for_timeout(250)

                    # Verify ModalDialog fallback (Mark Read)
                    fpage.locator('button:has-text("Mark Read")').first.click()
                    fpage.wait_for_selector('dialog[data-modal-fallback][open][aria-labelledby="mark-read-title"]')
                    fpage.wait_for_timeout(100)
                    fallback_modal = fpage.evaluate(
                        """() => {
                          const dlg = document.querySelector('dialog[aria-labelledby="mark-read-title"]');
                          const style = getComputedStyle(dlg);
                          return {
                            position: style.position,
                            zIndex: style.zIndex
                          };
                        }"""
                    )
                    fpage.keyboard.press("Escape")
                    fpage.wait_for_timeout(100)
                    fallback_modal["closedOnEscape"] = (
                        fpage.locator('dialog[open][aria-labelledby="mark-read-title"]').count() == 0
                    )

                    # Verify PopoverDialog topLayer fallback (#profile-menu-panel + nested Appearance)
                    fpage.evaluate("window.scrollTo(0, 0)")
                    fpage.locator('button[aria-controls="profile-menu-panel"]').click()
                    fpage.wait_for_selector('#profile-menu-panel[data-popover-fallback]:not([hidden])')
                    fpage.wait_for_timeout(100)
                    fallback_popover = fpage.evaluate(
                        """() => {
                          const panel = document.getElementById('profile-menu-panel');
                          const style = getComputedStyle(panel);
                          panel.scrollTop = panel.scrollHeight;
                          const signOutBtn = Array.from(panel.querySelectorAll('button')).find(
                            (b) => b.textContent.trim().includes('Sign out')
                          );
                          const sRect = signOutBtn.getBoundingClientRect();
                          const hitEl = document.elementFromPoint(
                            sRect.left + sRect.width / 2,
                            sRect.top + sRect.height / 2
                          );
                          panel.scrollTop = 0;
                          const appBtn = panel.querySelector('button[aria-label="Choose appearance"]');
                          return {
                            portaledToBody: panel.parentElement === document.body,
                            position: style.position,
                            zIndex: style.zIndex,
                            signOutHit: Boolean(hitEl && (hitEl === signOutBtn || signOutBtn.contains(hitEl))),
                            appearancePanelId: appBtn.getAttribute('aria-controls')
                          };
                        }"""
                    )
                    fb_app_id = fallback_popover.pop("appearancePanelId")
                    fpage.locator('#profile-menu-panel button[aria-label="Choose appearance"]').click()
                    fpage.wait_for_selector(f"#{fb_app_id}[data-popover-fallback]:not([hidden])")
                    fpage.wait_for_timeout(100)
                    fallback_nested = fpage.evaluate(
                        """(panelId) => {
                          const appPanel = document.getElementById(panelId);
                          const profilePanel = document.getElementById('profile-menu-panel');
                          const style = getComputedStyle(appPanel);
                          return {
                            portaledToBody: appPanel.parentElement === document.body,
                            position: style.position,
                            zIndex: style.zIndex,
                            profileStillOpen: !profilePanel.hasAttribute('hidden')
                          };
                        }""",
                        fb_app_id,
                    )
                    fpage.keyboard.press("Escape")
                    fpage.wait_for_timeout(100)
                    fallback_after_esc = fpage.evaluate(
                        """(panelId) => {
                          const appPanel = document.getElementById(panelId);
                          const profilePanel = document.getElementById('profile-menu-panel');
                          return {
                            appearanceRestoredAndHidden:
                              appPanel.parentElement !== document.body && appPanel.hasAttribute('hidden'),
                            profileStillOpen: !profilePanel.hasAttribute('hidden')
                          };
                        }""",
                        fb_app_id,
                    )
                    results["legacy_fallbacks"] = {
                        "modal": fallback_modal,
                        "popover": fallback_popover,
                        "nestedPopover": fallback_nested,
                        "afterFirstEscape": fallback_after_esc,
                    }
                    fallback_ctx.close()
                    browser.close()

                print(json.dumps(results, indent=2))

                # --- Strict Assertions ---
                for key in ("settings_default", "settings_expanded", "saved"):
                    section = results[key]
                    assert section["scrollWidth"] <= 320, section
                    assert section["clippedCount"] == 0, section["clipped"]
                    for dv in section["dataViews"]:
                        assert dv["width"] <= 288.5, dv

                for extra_view in ("discover", "sources", "decisions"):
                    ev = results[f"view_{extra_view}"]
                    assert ev["scrollWidth"] <= 320, ev
                    assert ev["clippedCount"] == 0, ev["clipped"]

                csv_r = results["csv_input_rect"]
                assert csv_r["x"] >= 0 and csv_r["x"] + csv_r["width"] <= 320, csv_r
                dig_r = results["digest_switch_rect"]
                assert dig_r["x"] >= 0 and dig_r["x"] + dig_r["width"] <= 320, dig_r

                mr = results["mark_read"]
                assert mr["scrollTop"] == 0, mr
                assert mr["overflowY"] == "auto", mr
                assert mr["dialogTop"] >= 15.5, mr
                assert mr["activeVisibleInViewport"], mr
                assert mr["submitReachableOnScroll"], mr
                assert mr["closedOnEscape"], mr

                lb = results["librarr_search_320x256"]
                assert lb["scrollTop"] == 0, lb
                assert lb["dialogOverflowY"] == "auto", lb
                assert lb["dialogTop"] >= 15.5, lb
                assert lb["closeTop"] >= 16, lb
                assert lb["activeVisibleInViewport"], lb
                assert lb["nestedScrollContainers"] == 0, lb
                assert lb["selectSawUnpreventedEscape"], lb
                assert lb["stillOpenAfterSelectEscape"], lb
                assert lb["closedOnDialogEscape"], lb

                pm = results["profile_menu"]
                assert pm["position"] == "fixed", pm
                assert pm["popoverAttr"] == "manual", pm
                assert pm["isPopoverOpen"], pm
                assert pm["signOutHit"], pm

                ap = results["appearance_dialog"]
                assert ap["parentPopoverStillOpen"], ap
                assert ap["appearancePopoverOpen"], ap
                assert ap["dialogTop"] >= 15.5, ap
                assert ap["dialogBottom"] <= 240.5, ap
                assert ap["nestedScrollContainers"] == 0, ap
                assert ap["themeCount"] == 24, ap
                assert ap["wintryPresent"], ap
                assert ap["unreachableCount"] == 0, ap
                assert ap["afterFirstEscape"] == {
                    "appearanceHidden": True,
                    "profilePopoverOpen": True,
                    "appearanceFocusCount": 1,
                    "activeIsAppearanceBtn": True,
                }, ap["afterFirstEscape"]
                assert ap["afterSecondEscape"] == {
                    "profileHidden": True,
                    "activeIsProfileBtn": True,
                }, ap["afterSecondEscape"]

                desk = results["desktop_1280x800"]
                assert desk["scrollWidth"] <= 1280, desk
                assert desk["clippedCount"] == 0, desk
                assert desk["librarr"] == {
                    "dialogOverflowY": "hidden",
                    "bodyOverflowY": "auto",
                }, desk["librarr"]

                fb = results["legacy_fallbacks"]
                assert fb["modal"] == {
                    "position": "fixed",
                    "zIndex": "60",
                    "closedOnEscape": True,
                }, fb["modal"]
                assert fb["popover"] == {
                    "portaledToBody": True,
                    "position": "fixed",
                    "zIndex": "50",
                    "signOutHit": True,
                }, fb["popover"]
                assert fb["nestedPopover"] == {
                    "portaledToBody": True,
                    "position": "fixed",
                    "zIndex": "55",
                    "profileStillOpen": True,
                }, fb["nestedPopover"]
                assert fb["afterFirstEscape"] == {
                    "appearanceRestoredAndHidden": True,
                    "profileStillOpen": True,
                }, fb["afterFirstEscape"]

                print("ALL REFLOW, TOP-LAYER OVERLAY, AND FALLBACK ASSERTIONS PASSED!")
            finally:
                librarr_server.shutdown()
                for process in processes:
                    process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"reflow_overlay_audit failed: {exc}", file=sys.stderr)
        raise
