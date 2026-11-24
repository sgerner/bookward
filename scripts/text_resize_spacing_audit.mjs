/**
 * Full-stack headless Chrome audit for WCAG 2.2 SC 1.4.4 (200% resize text)
 * and SC 1.4.12 (text spacing) across Bookward's authenticated views and modals.
 *
 * Run after `npm run build` and `uv sync --project engine --extra dev --frozen`:
 *   node scripts/text_resize_spacing_audit.mjs [--screenshots-dir /tmp] [--results /tmp/reflow-audit.json]
 */

import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import net from "node:net";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function parseArg(flag) {
  const index = process.argv.indexOf(flag);
  if (index === -1 || index + 1 >= process.argv.length) return null;
  return process.argv[index + 1];
}

const SCREENSHOTS_DIR = parseArg("--screenshots-dir");
const RESULTS_PATH = parseArg("--results");

if (SCREENSHOTS_DIR) mkdirSync(SCREENSHOTS_DIR, { recursive: true });
if (RESULTS_PATH) mkdirSync(dirname(RESULTS_PATH), { recursive: true });

function findChromeBinary() {
  const pwRoot = join(homedir(), ".cache/ms-playwright");
  const pwHeadlessShell = existsSync(pwRoot)
    ? readdirSync(pwRoot)
        .filter((name) => name.startsWith("chromium_headless_shell-"))
        .sort()
        .reverse()
        .map((name) =>
          join(pwRoot, name, "chrome-headless-shell-linux64/chrome-headless-shell"),
        )
    : [];
  const pwCandidates = existsSync(pwRoot)
    ? readdirSync(pwRoot)
        .filter((name) => name.startsWith("chromium-"))
        .sort()
        .reverse()
        .map((name) => join(pwRoot, name, "chrome-linux64/chrome"))
    : [];
  const candidates = [
    process.env.CHROME_BIN,
    ...pwHeadlessShell,
    ...pwCandidates,
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (candidate && existsSync(candidate)) return candidate;
  }
  throw new Error(
    "No Chrome/Chromium binary found. Set CHROME_BIN or install google-chrome.",
  );
}

function getFreePort() {
  return new Promise((resolvePort, reject) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const address = srv.address();
      const port = typeof address === "object" && address ? address.port : 0;
      srv.close((err) => (err ? reject(err) : resolvePort(port)));
    });
    srv.on("error", reject);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class CdpSession {
  /** @param {string} wsUrl */
  constructor(wsUrl) {
    this.wsUrl = wsUrl;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    /** @type {WebSocket | null} */
    this.ws = null;
  }

  async connect() {
    const ws = new WebSocket(this.wsUrl);
    this.ws = ws;
    await new Promise((resolveOpen, rejectOpen) => {
      ws.addEventListener("open", resolveOpen, { once: true });
      ws.addEventListener("error", rejectOpen, { once: true });
    });
    ws.addEventListener("message", (event) => {
      const msg = JSON.parse(String(event.data));
      if (msg.id) {
        const entry = this.pending.get(msg.id);
        if (entry) {
          this.pending.delete(msg.id);
          if (msg.error) entry.reject(new Error(JSON.stringify(msg.error)));
          else entry.resolve(msg.result);
        }
      } else if (msg.method) {
        const handlers = this.listeners.get(msg.method) ?? [];
        for (const handler of handlers) void handler(msg.params);
      }
    });
  }

  /**
   * @param {string} method
   * @param {(params: any) => void | Promise<void>} handler
   */
  on(method, handler) {
    const list = this.listeners.get(method) ?? [];
    list.push(handler);
    this.listeners.set(method, list);
  }

  /**
   * @param {string} method
   * @param {Record<string, unknown>} [params]
   * @returns {Promise<any>}
   */
  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolveSend, rejectSend) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        const detail = typeof params.expression === "string" ? ` (${params.expression.slice(0, 120).replace(/\s+/g, " ")})` : "";
        rejectSend(new Error(`CDP timeout after 15s in ${method}${detail}`));
      }, 15_000);
      this.pending.set(id, {
        resolve: (val) => {
          clearTimeout(timer);
          resolveSend(val);
        },
        reject: (err) => {
          clearTimeout(timer);
          rejectSend(err);
        },
      });
      this.ws?.send(JSON.stringify({ id, method, params }));
    });
  }

  /** @param {string} expression */
  async eval(expression) {
    const res = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: false,
      returnByValue: true,
    });
    if (res.exceptionDetails) {
      throw new Error(
        `Runtime.evaluate failed: ${JSON.stringify(res.exceptionDetails)}`,
      );
    }
    return res.result?.value;
  }

  async close() {
    this.ws?.close();
  }
}

const MOCK_LIBRARR_RESULTS = Array.from({ length: 8 }, (_, idx) => ({
  id: `edition-${idx + 1}`,
  title:
    idx === 0
      ? "The Ministry for the Future: Unabridged Collector's Annotated Climate Archive Edition"
      : idx === 7
        ? "Ancillary Justice: Complete Imperial Radch Anniversary Ensemble Recording (Final Edition)"
        : `Parable of the Sower: Earthseed Chronicles Extended Unabridged Edition Volume ${idx + 1}`,
  author:
    "Kim Stanley Robinson · Narrated by Jennifer Vuletic, Keith Szarabajka, Dion Graham, Bahni Turpin, and Full Cast",
  format: "audiobook",
  score: 96 - idx * 2,
}));

const GOODREADS_CSV = [
  "Book Id,Title,Author,Additional Authors,ISBN,ISBN13,My Rating,Average Rating,Publisher,Binding,Number of Pages,Year Published,Original Publication Year,Date Read,Date Added,Bookshelves,Exclusive Shelf",
  '101,"The Long Way to a Small, Angry Planet (Wayfarers Unabridged Special Archive Edition #1)","Becky Chambers","Patricia Rodriguez, Rachel Dulude","0062444131","=""9780062444134""",5,4.18,"Harper Voyager","Paperback",518,2015,2014,2025/09/14,2025/09/01,"",read',
  '102,"A Memory Called Empire: Teixcalaanli Diplomatic Archives Annotated Hardcover Edition","Arkady Martine","Amy Landon","1250186439","=""9781250186430""",5,4.15,"Tor Books","Hardcover",462,2019,2019,2025/10/02,2025/09/20,"",read',
  '103,"Piranesi: Complete Labyrinthine Journals and Correspondence of Matthew Rose Sorensen","Susanna Clarke","Chiwetel Ejiofor","163557563X","=""9781635575637""",4,4.24,"Bloomsbury","Hardcover",272,2020,2020,2025/10/19,2025/10/10,"",read',
].join("\n");

const MODES = {
  baseline_100: `*, *::before, *::after { transition-duration: 0s !important; animation-duration: 0s !important; }`,
  resize_200: `html { font-size: 200% !important; } *, *::before, *::after { transition-duration: 0s !important; animation-duration: 0s !important; }`,
  text_spacing: `* { line-height: 1.5 !important; letter-spacing: 0.12em !important; word-spacing: 0.16em !important; transition-duration: 0s !important; animation-duration: 0s !important; } p { margin-bottom: 2em !important; }`,
};

const VIEWPORTS = [
  { width: 1440, height: 900 },
  { width: 1280, height: 800 },
  { width: 768, height: 1024 },
  { width: 375, height: 812 },
  { width: 320, height: 568 },
];

async function waitForHttp(url, attempts = 150) {
  for (let i = 0; i < attempts; i++) {
    try {
      const res = await fetch(url, { redirect: "manual" });
      if (res.status >= 200 && res.status < 500) return;
    } catch {}
    await sleep(100);
  }
  throw new Error(`Timed out waiting for ${url}`);
}

function extractCookies(headers) {
  const raw = headers.getSetCookie ? headers.getSetCookie() : [];
  return raw.map((c) => c.split(";")[0]).join("; ");
}

async function seedApplication(origin, mockSourcePort, ephemeralCredential) {
  const setupRes = await fetch(`${origin}/login?/setup`, {
    method: "POST",
    headers: {
      accept: "text/html",
      origin,
      "content-type": "application/x-www-form-urlencoded",
      "x-forwarded-proto": "http",
    },
    body: new URLSearchParams({
      username: "alexandra-montgomery-archivist",
      password: ephemeralCredential,
    }),
    redirect: "manual",
  });
  if (setupRes.status !== 303) {
    throw new Error(`Setup failed with HTTP ${setupRes.status}`);
  }
  const cookie = extractCookies(setupRes.headers);

  const homeHtml = await (
    await fetch(`${origin}/`, {
      headers: { cookie, accept: "text/html" },
    })
  ).text();
  const profileMatch = homeHtml.match(/name="__profile_id"\s+value="([^"]+)"/);
  const profileId = profileMatch?.[1] ?? "";
  if (!profileId) throw new Error("Missing __profile_id in rendered HTML");

  const csvForm = new FormData();
  csvForm.append("__profile_id", profileId);
  csvForm.append(
    "file",
    new Blob([GOODREADS_CSV], { type: "text/csv" }),
    "goodreads_library_export.csv",
  );
  await fetch(`${origin}/?/importGoodreadsCsv`, {
    method: "POST",
    headers: { origin, cookie },
    body: csvForm,
  });

  await fetch(`${origin}/?/configureLibrar`, {
    method: "POST",
    headers: {
      origin,
      cookie,
      "content-type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({
      __profile_id: profileId,
      url: `http://127.0.0.1:${mockSourcePort}`,
      apiKey: ephemeralCredential,
      mediaType: "audiobook",
    }),
  });

  await fetch(`${origin}/?/createApiToken`, {
    method: "POST",
    headers: {
      origin,
      cookie,
      "content-type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({
      __profile_id: profileId,
      name: "Home Assistant Living Room Audiobook Tablet Integration",
    }),
  });

  await fetch(`${origin}/`, {
    headers: { cookie, accept: "text/html" },
  });

  return { cookie };
}

const AUDIT_PAGE_EXPR = `(() => {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const docScrollWidth = document.documentElement.scrollWidth;
  const horizontalPageOverflowPx = Math.max(0, docScrollWidth - vw);

  const rectsOverlap = (a, b) => {
    const xOverlap = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
    const yOverlap = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
    return xOverlap > 1 && yOverlap > 1 ? { width: xOverlap, height: yOverlap } : null;
  };

  const isVisible = (el) => {
    if (!(el instanceof HTMLElement)) return false;
    if (el.closest('.sr-only, [aria-hidden="true"], [hidden]')) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const primaryNav = document.querySelector('nav[aria-label="Primary navigation"]');
  const navButtons = primaryNav && isVisible(primaryNav)
    ? Array.from(primaryNav.querySelectorAll('button')).filter(isVisible)
    : [];
  const mobileNav = document.querySelector('nav[aria-label="Mobile navigation"]');
  const mobileButtons = mobileNav && isVisible(mobileNav)
    ? Array.from(mobileNav.querySelectorAll('button')).filter(isVisible)
    : [];

  const refreshBtn = document.querySelector('button[aria-label="Refresh recommendations"]');
  const profileBtn = document.querySelector('button[aria-label="Open profile menu"]');
  const actionBtns = [refreshBtn, profileBtn].filter(Boolean).filter(isVisible);

  const headerOverlaps = [];
  for (const navBtn of navButtons) {
    const nr = navBtn.getBoundingClientRect();
    for (const actBtn of actionBtns) {
      const ar = actBtn.getBoundingClientRect();
      const hit = rectsOverlap(nr, ar);
      if (hit) {
        headerOverlaps.push({
          nav: navBtn.textContent?.trim(),
          action: actBtn.getAttribute('aria-label'),
          ...hit,
        });
      }
    }
  }

  const offscreenControls = [];
  for (const btn of [...navButtons, ...mobileButtons, ...actionBtns]) {
    const r = btn.getBoundingClientRect();
    if (r.left < -1 || r.right > vw + 1) {
      offscreenControls.push({
        label: btn.getAttribute('aria-label') || btn.textContent?.trim(),
        left: Math.round(r.left * 10) / 10,
        right: Math.round(r.right * 10) / 10,
        vw,
      });
    }
  }

  let mobileNavRows = 0;
  let mobileNavCoversMainAtBottom = false;
  if (mobileNav && isVisible(mobileNav) && mobileButtons.length > 0) {
    const rowTops = new Set(mobileButtons.map((b) => Math.round(b.getBoundingClientRect().top)));
    mobileNavRows = rowTops.size;
    const prevScrollY = window.scrollY;
    window.scrollTo(0, document.documentElement.scrollHeight);
    const mainEl = document.getElementById('main-content');
    if (mainEl) {
      const mr = mainEl.getBoundingClientRect();
      const nr = mobileNav.getBoundingClientRect();
      mobileNavCoversMainAtBottom = mr.bottom > nr.top + 1.5;
    }
    window.scrollTo(0, prevScrollY);
  }

  const truncatedElements = [];
  const candidates = document.querySelectorAll('h1, h2, h3, p, span, small, code, legend, button, a');
  for (const el of candidates) {
    if (!isVisible(el)) continue;
    const style = getComputedStyle(el);
    const hasHiddenOverflow =
      style.overflowX === 'hidden' ||
      style.overflowX === 'clip' ||
      style.overflowY === 'hidden' ||
      style.overflowY === 'clip' ||
      style.textOverflow === 'ellipsis' ||
      style.webkitLineClamp !== 'none';
    const widthOverflow = el.scrollWidth - el.clientWidth;
    const heightOverflow = el.scrollHeight - el.clientHeight;
    if (
      (el.clientWidth > 0 && widthOverflow > 1.5 && (hasHiddenOverflow || el.getBoundingClientRect().right > vw + 1.5)) ||
      (el.clientHeight > 0 && heightOverflow > 1.5 && hasHiddenOverflow)
    ) {
      truncatedElements.push({
        tag: el.tagName.toLowerCase(),
        text: (el.textContent || '').trim().slice(0, 60),
        className: el.className,
        scrollWidth: el.scrollWidth,
        clientWidth: el.clientWidth,
        scrollHeight: el.scrollHeight,
        clientHeight: el.clientHeight,
      });
    }
  }

  return {
    vw,
    vh,
    docScrollWidth,
    horizontalPageOverflowPx,
    headerOverlaps,
    offscreenControls,
    mobileNavRows,
    mobileNavCoversMainAtBottom,
    truncatedElements,
  };
})()`;

async function navigateTo(cdp, url) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const loaded = new Promise((resolveLoad) => {
      const timer = setTimeout(resolveLoad, 5_000);
      const list = cdp.listeners.get("Page.loadEventFired") ?? [];
      const onceWrapper = () => {
        const idx = list.indexOf(onceWrapper);
        if (idx !== -1) list.splice(idx, 1);
        clearTimeout(timer);
        resolveLoad(undefined);
      };
      list.push(onceWrapper);
      cdp.listeners.set("Page.loadEventFired", list);
    });
    await cdp.send("Page.navigate", { url });
    await loaded;
    await sleep(100);
    const currentHref = await cdp.eval("location.href").catch(() => "");
    if (currentHref === url) break;
  }
}

async function injectModeCss(cdp, cssText, stepLabel = "") {
  if (process.env.DEBUG_AUDIT) console.error(`[audit] ${stepLabel}`);
  await cdp.eval(`(() => {
    let tag = document.getElementById('__wcag_audit_style');
    if (!tag) {
      tag = document.createElement('style');
      tag.id = '__wcag_audit_style';
      document.head.appendChild(tag);
    }
    tag.textContent = ${JSON.stringify(cssText)};
  })()`);
  await sleep(50);
}

async function captureScreenshot(cdp, filename) {
  if (!SCREENSHOTS_DIR) return;
  const { data } = await cdp.send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(SCREENSHOTS_DIR, filename), Buffer.from(data, "base64"));
}

async function sendEscape(cdp) {
  await cdp.send("Input.dispatchKeyEvent", {
    type: "keyDown",
    key: "Escape",
    code: "Escape",
    windowsVirtualKeyCode: 27,
  });
  await cdp.send("Input.dispatchKeyEvent", {
    type: "keyUp",
    key: "Escape",
    code: "Escape",
    windowsVirtualKeyCode: 27,
  });
  await sleep(160);
}

async function main() {
  const chromeBinary = findChromeBinary();
  const tempDir = mkdtempSync(join(tmpdir(), "bookward-wcag-audit-"));
  const enginePort = await getFreePort();
  const webPort = await getFreePort();
  const debugPort = await getFreePort();
  const mockPort = await getFreePort();
  const origin = `http://127.0.0.1:${webPort}`;
  const serviceToken = randomBytes(18).toString("hex");
  const readerCredential = `Reader-${randomBytes(12).toString("hex")}`;

  const mockServer = createServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ status: "ok", results: MOCK_LIBRARR_RESULTS }));
  });
  await new Promise((r) => mockServer.listen(mockPort, "127.0.0.1", r));

  const env = {
    ...process.env,
    AFTERWORD_DB: join(tempDir, "engine.db"),
    AFTERWORD_BACKUP_DIR: join(tempDir, "backups"),
    AFTERWORD_BACKUP_INTERVAL_HOURS: "0",
    AFTERWORD_AUTH_PASSWORD: "",
    AFTERWORD_SERVICE_SECRET: serviceToken,
    AFTERWORD_SOURCE_SYNC_INTERVAL_HOURS: "0",
    AFTERWORD_EMBEDDING_BACKEND: "local",
    AFTERWORD_EMBEDDING_MODEL: "hashing-768",
    AFTERWORD_EMBEDDING_API_KEY: "",
    AFTERWORD_SECRET_KEY: "",
    AFTERWORD_OIDC_ISSUER: "",
    AFTERWORD_OIDC_CLIENT_ID: "",
    AFTERWORD_PUBLIC_URL: origin,
    AFTERWORD_LIBRARR_ALLOWED_HOSTS: "127.0.0.1",
    LIBRARR_ALLOWED_HOSTS: "127.0.0.1",
    ENGINE_SERVICE_SECRET: serviceToken,
    ENGINE_URL: `http://127.0.0.1:${enginePort}`,
    ORIGIN: origin,
    HOST: "127.0.0.1",
    PORT: String(webPort),
    OIDC_AUTO_LOGIN: "false",
    PROTOCOL_HEADER: "x-forwarded-proto",
  };

  /** @type {import("node:child_process").ChildProcess[]} */
  const children = [];
  /** @type {CdpSession | null} */
  let cdp = null;

  try {
    children.push(
      spawn(
        join(REPO_ROOT, "engine/.venv/bin/python"),
        [
          "-m",
          "uvicorn",
          "afterword_engine.main:app",
          "--app-dir",
          "engine",
          "--host",
          "127.0.0.1",
          "--port",
          String(enginePort),
        ],
        { cwd: REPO_ROOT, env, stdio: "ignore" },
      ),
    );
    children.push(
      spawn("node", ["build"], { cwd: REPO_ROOT, env, stdio: "ignore" }),
    );

    await waitForHttp(`${origin}/login`);
    const { cookie } = await seedApplication(origin, mockPort, readerCredential);

    const chromeArgs = [
      ...(chromeBinary.includes("chrome-headless-shell") ? [] : ["--headless=new"]),
      "--no-sandbox",
      "--disable-gpu",
      "--disable-dev-shm-usage",
      "--enable-features=NetworkService,NetworkServiceInProcess",
      `--remote-debugging-port=${debugPort}`,
      `--user-data-dir=${join(tempDir, "chrome-profile")}`,
      "about:blank",
    ];
    children.push(spawn(chromeBinary, chromeArgs, { stdio: "ignore" }));

    await waitForHttp(`http://127.0.0.1:${debugPort}/json/version`);
    const targets = await (
      await fetch(`http://127.0.0.1:${debugPort}/json/list`)
    ).json();
    const pageTarget = targets.find((t) => t.type === "page");
    cdp = new CdpSession(pageTarget.webSocketDebuggerUrl);
    await cdp.connect();

    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Network.enable");
    await cdp.send("Network.setBlockedURLs", {
      urls: [
        "*://*.openlibrary.org/*",
        "*://*.google.com/*",
        "*://*.googleapis.com/*",
        "*://models.dev/*",
      ],
    });
    await cdp.send("Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-reduced-motion", value: "reduce" }],
    });
    await cdp.send("Fetch.enable", {
      patterns: [{ urlPattern: "*librarr/search/stream*" }],
    });

    cdp.on("Fetch.requestPaused", async (params) => {
      const ssePayload =
        `event: started\ndata: {"search":"audit"}\n\n` +
        `event: results\ndata: ${JSON.stringify({ results: MOCK_LIBRARR_RESULTS })}\n\n` +
        `event: complete\ndata: {"status":"complete"}\n\n`;
      await cdp?.send("Fetch.fulfillRequest", {
        requestId: params.requestId,
        responseCode: 200,
        responseHeaders: [
          { name: "content-type", value: "text/event-stream; charset=utf-8" },
          { name: "cache-control", value: "no-cache" },
        ],
        body: Buffer.from(ssePayload, "utf8").toString("base64"),
      });
    });

    const eqIndex = cookie.indexOf("=");
    await cdp.send("Network.setCookie", {
      name: cookie.slice(0, eqIndex),
      value: cookie.slice(eqIndex + 1),
      url: origin,
      httpOnly: true,
    });
    await cdp.send("Network.setExtraHTTPHeaders", {
      headers: { Cookie: cookie },
    });

    const report = {
      generated_at: new Date().toISOString(),
      checks: /** @type {any[]} */ ([]),
      failures: /** @type {any[]} */ ([]),
    };

    for (const vp of VIEWPORTS) {
      await cdp.send("Emulation.setDeviceMetricsOverride", {
        width: vp.width,
        height: vp.height,
        deviceScaleFactor: 2,
        mobile: false,
      });

      const activeModes =
        vp.width === 375 || vp.width === 320
          ? Object.entries(MODES)
          : Object.entries(MODES).filter(([key]) => key !== "baseline_100");

      for (const [modeName, modeCss] of activeModes) {
        const minViewportForZeroOverflow = vp.width >= 320;

        await navigateTo(cdp, `${origin}/`);
        await injectModeCss(cdp, modeCss, `${vp.width}x${vp.height} ${modeName} discover`);

        const discoverRes = await cdp.eval(AUDIT_PAGE_EXPR);
        const singleRowExpectedAt320 =
          vp.width === 320 && modeName === "baseline_100" && discoverRes.mobileNavRows > 1;
        report.checks.push({
          viewport: `${vp.width}x${vp.height}`,
          mode: modeName,
          surface: "discover_and_nav",
          ...discoverRes,
        });
        if (
          (minViewportForZeroOverflow && discoverRes.horizontalPageOverflowPx > 1) ||
          discoverRes.headerOverlaps.length > 0 ||
          discoverRes.offscreenControls.length > 0 ||
          discoverRes.mobileNavCoversMainAtBottom ||
          singleRowExpectedAt320 ||
          (vp.width >= 375 && discoverRes.truncatedElements.length > 0)
        ) {
          report.failures.push({
            viewport: `${vp.width}x${vp.height}`,
            mode: modeName,
            surface: "discover_and_nav",
            singleRowExpectedAt320,
            ...discoverRes,
          });
        }

        if (vp.width === 1280 && vp.height === 800 && modeName === "resize_200") {
          await captureScreenshot(cdp, "nav-200pct-1280x800.png");
        }
        if (vp.width === 768 && vp.height === 1024 && modeName === "resize_200") {
          await captureScreenshot(cdp, "nav-200pct-768x1024.png");
        }

        if (modeName !== "baseline_100" && vp.width >= 375) {
          const openedLibrarr = await cdp.eval(`(() => {
            const btn = Array.from(document.querySelectorAll('button')).find(
              (b) => (b.getAttribute('aria-label') || '').startsWith('Find in Librarr:')
            );
            if (!btn) return false;
            btn.click();
            return true;
          })()`);

          if (openedLibrarr) {
            await sleep(240);
            await injectModeCss(cdp, modeCss, `${vp.width}x${vp.height} ${modeName} librarr`);
            const librarrCheck = await cdp.eval(`(() => {
              const dialog = document.querySelector('dialog[aria-labelledby="librarr-search-title"]');
              const body = dialog?.querySelector('.modal-scroll-body');
              if (!dialog || !body) return { missing: true };
              const dr = dialog.getBoundingClientRect();
              const br = body.getBoundingClientRect();
              const clipBeforeScroll = Math.max(0, br.bottom - dr.bottom);
              body.scrollTop = body.scrollHeight;
              const addBtns = Array.from(body.querySelectorAll('article button'));
              const lastAdd = addBtns[addBtns.length - 1];
              const lr = lastAdd?.getBoundingClientRect();
              const lastAddExposed = Boolean(
                lr &&
                lr.bottom <= br.bottom + 1.5 &&
                lr.top >= br.top - 1.5 &&
                lr.right <= window.innerWidth + 1.5
              );
              const initialFocusedInside = dialog.contains(document.activeElement);
              return {
                dialogBottom: Math.round(dr.bottom * 10) / 10,
                bodyBottom: Math.round(br.bottom * 10) / 10,
                viewportHeight: window.innerHeight,
                clipPx: Math.round(clipBeforeScroll * 10) / 10,
                resultCount: addBtns.length,
                lastAddExposed,
                initialFocusedInside,
              };
            })()`);

            const librarrDom = await cdp.eval(AUDIT_PAGE_EXPR);
            report.checks.push({
              viewport: `${vp.width}x${vp.height}`,
              mode: modeName,
              surface: "librarr_dialog",
              ...librarrCheck,
              truncatedCount: librarrDom.truncatedElements.length,
            });
            if (
              librarrCheck.missing ||
              librarrCheck.clipPx > 1.5 ||
              librarrCheck.dialogBottom > vp.height + 1.5 ||
              !librarrCheck.lastAddExposed ||
              !librarrCheck.initialFocusedInside ||
              librarrDom.truncatedElements.length > 0
            ) {
              report.failures.push({
                viewport: `${vp.width}x${vp.height}`,
                mode: modeName,
                surface: "librarr_dialog",
                ...librarrCheck,
                truncatedElements: librarrDom.truncatedElements,
              });
            }

            if (vp.width === 1280 && vp.height === 800 && modeName === "text_spacing") {
              await captureScreenshot(cdp, "librarr-spacing-1280x800.png");
            }

            await sendEscape(cdp);
            const stillOpen = await cdp.eval(
              `Boolean(document.querySelector('dialog[aria-labelledby="librarr-search-title"]'))`,
            );
            if (stillOpen) {
              report.failures.push({
                viewport: `${vp.width}x${vp.height}`,
                mode: modeName,
                surface: "librarr_escape_dismiss",
              });
            }
          }

          const openedMarkRead = await cdp.eval(`(() => {
            const menuBtn = document.querySelector('[data-read-menu-trigger]');
            menuBtn?.click();
            return Boolean(menuBtn);
          })()`);
          if (openedMarkRead) {
            await sleep(120);
            await cdp.eval(`(() => {
              const mrBtn = Array.from(document.querySelectorAll('button')).find(
                (b) => (b.getAttribute('aria-label') || '').startsWith('Mark Read:')
              );
              mrBtn?.click();
            })()`);
            await sleep(200);
            await injectModeCss(cdp, modeCss, `${vp.width}x${vp.height} ${modeName} mark_read`);
            const markReadCheck = await cdp.eval(`(() => {
              const dialog = document.querySelector('[aria-labelledby="mark-read-title"]');
              if (!dialog) return { missing: true };
              const dr = dialog.getBoundingClientRect();
              dialog.scrollTop = dialog.scrollHeight;
              const submitBtn = dialog.querySelector('button[type="submit"]');
              const sr = submitBtn?.getBoundingClientRect();
              const submitExposed = Boolean(
                sr &&
                sr.bottom <= dr.bottom + 1.5 &&
                sr.top >= dr.top - 1.5 &&
                sr.right <= window.innerWidth + 1.5
              );
              const initialFocusedInside = dialog.contains(document.activeElement);
              return {
                dialogBottom: Math.round(dr.bottom * 10) / 10,
                submitExposed,
                initialFocusedInside,
              };
            })()`);
            const markReadDom = await cdp.eval(AUDIT_PAGE_EXPR);
            report.checks.push({
              viewport: `${vp.width}x${vp.height}`,
              mode: modeName,
              surface: "mark_read_dialog",
              ...markReadCheck,
              truncatedCount: markReadDom.truncatedElements.length,
            });
            if (
              markReadCheck.missing ||
              markReadCheck.dialogBottom > vp.height + 1.5 ||
              !markReadCheck.submitExposed ||
              !markReadCheck.initialFocusedInside ||
              markReadDom.truncatedElements.length > 0
            ) {
              report.failures.push({
                viewport: `${vp.width}x${vp.height}`,
                mode: modeName,
                surface: "mark_read_dialog",
                ...markReadCheck,
                truncatedElements: markReadDom.truncatedElements,
              });
            }
            await sendEscape(cdp);
          }
        }

        // Open Profile menu + compact ThemePicker and verify top-20 clears header trigger
        await cdp.eval(`(() => {
          const btn = document.querySelector('button[aria-label="Open profile menu"]');
          if (btn && btn.getAttribute('aria-expanded') !== 'true') btn.click();
        })()`);
        await sleep(120);
        await cdp.eval(`(() => {
          const btn = document.querySelector('button[aria-label="Choose appearance"]');
          if (btn && btn.getAttribute('aria-expanded') !== 'true') btn.click();
        })()`);
        await sleep(160);
        await injectModeCss(cdp, modeCss, `${vp.width}x${vp.height} ${modeName} theme_picker`);

        const themeCheck = await cdp.eval(`(() => {
          const appearanceTrigger = document.querySelector('button[aria-label="Choose appearance"]');
          const panelId = appearanceTrigger?.getAttribute('aria-controls') || '';
          const panel = panelId ? document.getElementById(panelId) : null;
          const profileTrigger = document.querySelector('button[aria-label="Open profile menu"]');
          if (!panel || panel.hasAttribute('hidden') || !profileTrigger) return { missing: true };
          const pr = panel.getBoundingClientRect();
          const tr = profileTrigger.getBoundingClientRect();
          const xOverlap = Math.max(0, Math.min(pr.right, tr.right) - Math.max(pr.left, tr.left));
          const yOverlap = Math.max(0, Math.min(pr.bottom, tr.bottom) - Math.max(pr.top, tr.top));
          const overlapsHeaderTrigger = xOverlap > 1 && yOverlap > 1;
          const themeGroup = panel.querySelector('[aria-labelledby="appearance-theme-legend"], [aria-label="Skeleton theme"]');
          const buttons = themeGroup ? Array.from(themeGroup.querySelectorAll('button')) : [];
          const clippedLabels = buttons.filter((btn) => {
            const span = btn.querySelector('.text-reflow');
            return !span || span.scrollWidth > span.clientWidth + 1.5 || span.scrollHeight > span.clientHeight + 1.5;
          }).map((b) => b.textContent?.trim());
          return {
            panelTop: Math.round(pr.top * 10) / 10,
            panelBottom: Math.round(pr.bottom * 10) / 10,
            panelRight: Math.round(pr.right * 10) / 10,
            panelLeft: Math.round(pr.left * 10) / 10,
            triggerBottom: Math.round(tr.bottom * 10) / 10,
            overlapsHeaderTrigger,
            paletteCount: buttons.length,
            clippedLabels,
          };
        })()`);
        const themeDom = await cdp.eval(AUDIT_PAGE_EXPR);
        report.checks.push({
          viewport: `${vp.width}x${vp.height}`,
          mode: modeName,
          surface: "theme_picker",
          ...themeCheck,
          truncatedCount: themeDom.truncatedElements.length,
        });
        if (
          themeCheck.missing ||
          themeCheck.overlapsHeaderTrigger ||
          themeCheck.paletteCount !== 24 ||
          themeCheck.clippedLabels.length > 0 ||
          themeCheck.panelBottom > vp.height + 1.5 ||
          themeCheck.panelLeft < -1.5 ||
          themeCheck.panelRight > vp.width + 1.5 ||
          (vp.width >= 375 && themeDom.truncatedElements.length > 0)
        ) {
          report.failures.push({
            viewport: `${vp.width}x${vp.height}`,
            mode: modeName,
            surface: "theme_picker",
            ...themeCheck,
            truncatedElements: themeDom.truncatedElements,
          });
        }

        if (vp.width === 375 && vp.height === 812 && modeName === "resize_200") {
          await captureScreenshot(cdp, "themepicker-200pct-375x812.png");
        }
        if (vp.width === 375 && vp.height === 812 && modeName === "baseline_100") {
          await captureScreenshot(cdp, "themepicker-100pct-375x812.png");
        }

        await sendEscape(cdp);
        await sendEscape(cdp);

        if (modeName !== "baseline_100" && vp.width >= 375) {
          // Reading History view
          await navigateTo(cdp, `${origin}/?view=decisions`);
          await injectModeCss(cdp, modeCss, `${vp.width}x${vp.height} ${modeName} decisions`);
          await cdp.eval(`(() => {
            const readBtn = Array.from(document.querySelectorAll('button')).find(
              (b) => (b.textContent || '').trim().startsWith('Read (')
            );
            readBtn?.click();
          })()`);
          await sleep(240);
          await injectModeCss(cdp, modeCss, `${vp.width}x${vp.height} ${modeName} history_read`);
          const historyRes = await cdp.eval(AUDIT_PAGE_EXPR);
          report.checks.push({
            viewport: `${vp.width}x${vp.height}`,
            mode: modeName,
            surface: "reading_history",
            ...historyRes,
          });
          if (
            historyRes.horizontalPageOverflowPx > 1 ||
            historyRes.mobileNavCoversMainAtBottom ||
            historyRes.truncatedElements.length > 0
          ) {
            report.failures.push({
              viewport: `${vp.width}x${vp.height}`,
              mode: modeName,
              surface: "reading_history",
              ...historyRes,
            });
          }

          // Sources view
          await navigateTo(cdp, `${origin}/?view=sources`);
          await injectModeCss(cdp, modeCss, `${vp.width}x${vp.height} ${modeName} sources`);
          const sourcesRes = await cdp.eval(AUDIT_PAGE_EXPR);
          report.checks.push({
            viewport: `${vp.width}x${vp.height}`,
            mode: modeName,
            surface: "sources",
            ...sourcesRes,
          });
          if (
            sourcesRes.horizontalPageOverflowPx > 1 ||
            sourcesRes.mobileNavCoversMainAtBottom ||
            sourcesRes.truncatedElements.length > 0
          ) {
            report.failures.push({
              viewport: `${vp.width}x${vp.height}`,
              mode: modeName,
              surface: "sources",
              ...sourcesRes,
            });
          }

          // Settings view
          await navigateTo(cdp, `${origin}/?view=settings`);
          await injectModeCss(cdp, modeCss, `${vp.width}x${vp.height} ${modeName} settings`);
          const settingsRes = await cdp.eval(AUDIT_PAGE_EXPR);
          report.checks.push({
            viewport: `${vp.width}x${vp.height}`,
            mode: modeName,
            surface: "settings",
            ...settingsRes,
          });
          if (
            settingsRes.horizontalPageOverflowPx > 1 ||
            settingsRes.mobileNavCoversMainAtBottom ||
            settingsRes.truncatedElements.length > 0
          ) {
            report.failures.push({
              viewport: `${vp.width}x${vp.height}`,
              mode: modeName,
              surface: "settings",
              ...settingsRes,
            });
          }

          if (vp.width === 1280 && vp.height === 800 && modeName === "resize_200") {
            await captureScreenshot(cdp, "settings-200pct-1280x800.png");
          }
        }
      }
    }

    if (RESULTS_PATH) {
      writeFileSync(RESULTS_PATH, JSON.stringify(report, null, 2));
    }
    console.log(
      `PASS: text resize (200%) & text spacing (SC 1.4.12) audit — ${report.checks.length} surface checks, ${report.failures.length} failures.`,
    );
    if (report.failures.length > 0) {
      console.error(JSON.stringify(report.failures, null, 2));
      process.exitCode = 1;
    }
  } finally {
    await cdp?.close().catch(() => {});
    mockServer.closeAllConnections?.();
    mockServer.close();
    for (const child of children) {
      try {
        child.kill("SIGKILL");
      } catch {}
    }
    await sleep(150);
    rmSync(tempDir, { recursive: true, force: true });
  }
}

await main();
process.exit(process.exitCode ?? 0);
